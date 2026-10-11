"""SmartAPI REST calls: historical candles, quotes, scrip lookup, scrip master.

Verified request shapes (official docs + SDK, 2026-10-11):

* ``POST /rest/secure/angelbroking/historical/v1/getCandleData``
  body ``{symboltoken, exchangeType, interval, fromdate, todate}`` with
  ``fromdate``/``todate`` in ``"YYYY-MM-DD HH:MM"`` **on the IST exchange
  clock**; rate limit 3/second, 120/minute, 5000/hour.
* ``POST /rest/secure/angelbroking/market/v1/quote``
  body ``{mode: "FULL", exchangeTokens: {NSE: [token, ...]}}`` — the docs prose
  says "50 symbols in one request with 1 request per second" while the rate
  table says 10/second; the stricter reading is used.
* ``POST /rest/secure/angelbroking/order/v1/searchScrip``
  body ``{exchange: "NSE", searchscrip: "RELIANCE"}`` — 1/second.

Everything is rate-limited locally so the worker never depends on the API's
403 to tell it to slow down, and every failure returns an empty result rather
than raising into the feed loop: the caller decides how loud to be.
"""

from __future__ import annotations

import asyncio
import collections
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from ..config import AngelSettings
from ..logging_setup import get_logger, scrub
from ..models import Candle, DataQuality
from ..timeutils import angel_rest_stamp, parse_ist_stamp, to_ist
from .smartapi_auth import SmartApiAuth

log = get_logger(__name__)

HISTORICAL_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"
QUOTE_PATH = "/rest/secure/angelbroking/market/v1/quote"
SEARCH_SCRIP_PATH = "/rest/secure/angelbroking/order/v1/searchScrip"

EXCHANGE_NSE = "NSE"

# Docs "Max Days in one Request" for getCandleData.
MAX_DAYS_BY_INTERVAL: dict[str, int] = {
    "ONE_MINUTE": 30,
    "THREE_MINUTE": 60,
    "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100,
    "FIFTEEN_MINUTE": 200,
    "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400,
    "ONE_DAY": 2000,
}

INTERVAL_MINUTES: dict[str, int] = {
    "ONE_MINUTE": 1,
    "THREE_MINUTE": 3,
    "FIVE_MINUTE": 5,
    "TEN_MINUTE": 10,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
    "ONE_DAY": 1440,
}


class RestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class RateLimiter:
    """Sliding-window limiter: at most ``max_calls`` per ``window_seconds``."""

    def __init__(self, *, max_calls: int, window_seconds: float, name: str = "limiter") -> None:
        self.max_calls = max_calls
        self.window = float(window_seconds)
        self.name = name
        self._calls: collections.deque[float] = collections.deque()
        self._lock = asyncio.Lock()

    def wait_seconds(self, *, now: float) -> float:
        """Pure, testable: how long until a slot frees (0 when allowed now)."""
        if self.max_calls <= 0:
            return 0.0
        cutoff = now - self.window
        stamps = [t for t in self._calls if t > cutoff]
        if len(stamps) < self.max_calls:
            return 0.0
        return max(0.0, stamps[0] + self.window - now)

    async def acquire(self) -> float:
        """Block until a slot is free; returns the seconds waited."""
        waited_total = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self.window
                while self._calls and self._calls[0] <= cutoff:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return waited_total
                delay = self._calls[0] + self.window - now
            delay = max(0.01, delay)
            await asyncio.sleep(delay)
            waited_total += delay


@dataclass
class SmartApiRest:
    """Authenticated REST client with the documented limits applied locally."""

    settings: AngelSettings
    auth: SmartApiAuth
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _limits: dict[str, RateLimiter] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._limits = {
            # Values from the official rate-limit table (per client code).
            "historical": RateLimiter(max_calls=3, window_seconds=1.0, name="historical"),
            "historical_min": RateLimiter(max_calls=120, window_seconds=60.0, name="historical_min"),
            "quote": RateLimiter(max_calls=1, window_seconds=1.0, name="quote"),
            "quote_min": RateLimiter(max_calls=500, window_seconds=60.0, name="quote_min"),
            "search": RateLimiter(max_calls=1, window_seconds=1.0, name="search"),
        }

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                transport=self.transport,
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── generic call ──────────────────────────────────────────────────────

    async def _post(
        self, path: str, payload: dict[str, object], *, limiter: str
    ) -> httpx.Response:
        headers = await self.auth.rest_headers()
        client = await self._http()
        minute_key = f"{limiter}_min"
        if minute_key in self._limits:
            await self._limits[minute_key].acquire()
        await self._limits[limiter].acquire()
        return await client.post(
            f"{self.settings.base_url}{path}",
            json=payload,
            headers=headers,
            timeout=self.settings.request_timeout_seconds,
        )

    async def call_json(
        self, path: str, payload: dict[str, object], *, limiter: str
    ) -> dict[str, object] | None:
        """One authenticated POST → parsed envelope, or ``None`` on failure."""
        last_error: str = ""
        for attempt in range(2):
            try:
                response = await self._post(path, payload, limiter=limiter)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                # A transport failure is retried once with a fresh session.
                if attempt == 0:
                    continue
                log.warning("smartapi_rest_network", path=path, error=scrub(last_error))
                return None
            status = response.status_code
            if status in (401, 403):
                try:
                    await self.auth.force_reauth(f"HTTP {status} on {path}")
                except Exception as exc:  # noqa: BLE001 - never kill the feed loop
                    log.error("smartapi_rest_reauth_failed", path=path, error=scrub(str(exc)))
                    return None
                continue
            if status == 429:
                log.warning("smartapi_rest_rate_limited", path=path)
                await asyncio.sleep(2.0)
                continue
            if status >= 400:
                log.warning(
                    "smartapi_rest_http_error", path=path, status=status, body=scrub(response.text[:200])
                )
                return None
            try:
                parsed = response.json()
            except ValueError:
                log.warning("smartapi_rest_bad_json", path=path)
                return None
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        log.warning("smartapi_rest_exhausted", path=path, error=scrub(last_error))
        return None

    # ── candles ───────────────────────────────────────────────────────────

    async def get_candles(
        self,
        *,
        instrument_id: int,
        symboltoken: str,
        interval: str = "ONE_MINUTE",
        from_dt: datetime,
        to_dt: datetime,
        exchange: str = EXCHANGE_NSE,
        treat_timestamp_as: str = "open",
    ) -> list[Candle]:
        """Historical OHLCV bars, oldest → newest, IST-aware.

        The API's own cap on the range per request is enforced here by clamping
        (docs: ONE_MINUTE = 30 days, …), so a misconfigured warm-up window
        degrades to the largest legal request instead of an HTTP error.

        ``treat_timestamp_as`` exists because Angel does not document whether a
        bar's stamp is its open or close time (see the research notes); the
        community-verified reading is *open time*, which is also the convention
        used by the candle engine.
        """
        if interval not in MAX_DAYS_BY_INTERVAL:
            raise ValueError(f"unknown interval {interval!r}")
        max_days = MAX_DAYS_BY_INTERVAL[interval]
        from_ist, to_ist_dt = to_ist(from_dt), to_ist(to_dt)
        if (to_ist_dt - from_ist).days > max_days:
            from_ist = to_ist_dt - timedelta(days=max_days)
            log.info(
                "smartapi_candle_window_clamped",
                interval=interval,
                max_days=max_days,
                symboltoken=symboltoken,
            )
        payload = {
            "symboltoken": symboltoken,
            "exchangeType": exchange,
            "interval": interval,
            "fromdate": angel_rest_stamp(from_ist),
            "todate": angel_rest_stamp(to_ist_dt),
        }
        envelope = await self.call_json(HISTORICAL_PATH, payload, limiter="historical")
        if envelope is None:
            return []
        rows = unwrap_rows(envelope.get("data"))
        if rows is None:
            code = envelope.get("errorcode") or envelope.get("errorCode")
            log.warning("smartapi_candle_empty", symboltoken=symboltoken, errorcode=scrub(str(code)))
            return []
        step = INTERVAL_MINUTES[interval]
        candles: list[Candle] = []
        skipped = 0
        for row in rows:
            candle = row_to_candle(
                row,
                instrument_id=instrument_id,
                interval_minutes=step,
                treat_timestamp_as=treat_timestamp_as,
            )
            if candle is None:
                skipped += 1
                continue
            candles.append(candle)
        if skipped:
            log.warning("smartapi_candle_rows_skipped", symboltoken=symboltoken, count=skipped)
        candles.sort(key=lambda c: c.candle_start)
        return candles

    # ── quotes ────────────────────────────────────────────────────────────

    async def get_quotes(
        self, symboltokens: Sequence[str], *, batch_size: int = 45
    ) -> dict[str, dict[str, object]]:
        """FULL-mode quote rows keyed by symbol token.

        Docs allow 50 symbols per request; 45 leaves headroom, and the tracker's
        measured behaviour was that a burst beyond ~1 request/second gets refused
        inside the same minute — the limiter above enforces that.
        """
        out: dict[str, dict[str, object]] = {}
        tokens = [t for t in dict.fromkeys(symboltokens) if t]
        for start in range(0, len(tokens), batch_size):
            chunk = tokens[start : start + batch_size]
            envelope = await self.call_json(
                QUOTE_PATH,
                {"mode": "FULL", "exchangeTokens": {EXCHANGE_NSE: chunk}},
                limiter="quote",
            )
            if envelope is None:
                continue
            data = envelope.get("data")
            fetched = data.get("fetched") if isinstance(data, dict) else None
            if not isinstance(fetched, list):
                log.warning("smartapi_quote_no_rows", requested=len(chunk))
                continue
            for row in fetched:
                if isinstance(row, dict):
                    token = str(row.get("symbolToken") or row.get("symboltoken") or "")
                    if token:
                        out[token] = row
            unfetched = data.get("unfetched") if isinstance(data, dict) else None
            if isinstance(unfetched, list) and unfetched:
                log.info("smartapi_quote_unfetched", count=len(unfetched))
        return out

    # ── scrip master / search ─────────────────────────────────────────────

    async def search_scrip(self, symbol: str, *, exchange: str = EXCHANGE_NSE) -> list[dict[str, object]]:
        envelope = await self.call_json(
            SEARCH_SCRIP_PATH, {"exchange": exchange, "searchscrip": symbol}, limiter="search"
        )
        if envelope is None:
            return []
        return [row for row in (unwrap_rows(envelope.get("data")) or []) if isinstance(row, dict)]

    async def fetch_instrument_master(self) -> list[dict[str, object]]:
        """Download the daily scrip master (~35 MB, 150k rows) as a list.

        The file is plain JSON today even though the docs still describe a
        gzipped CSV; the request accepts gzip anyway, and httpx decodes it.
        A failure returns ``[]`` so the caller can fall back to the last known
        mapping stored in the database — never to a hardcoded token list.
        """
        client = await self._http()
        try:
            response = await client.get(
                self.settings.instrument_master_url,
                headers={"accept": "application/json", "accept-encoding": "gzip, deflate"},
                timeout=max(60.0, self.settings.request_timeout_seconds * 5),
            )
        except httpx.HTTPError as exc:
            log.error("instrument_master_download_failed", error=scrub(str(exc)))
            return []
        if response.status_code >= 400:
            log.error("instrument_master_http", status=response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            log.error("instrument_master_not_json")
            return []
        if not isinstance(payload, list):
            log.error("instrument_master_unexpected_shape", shape=type(payload).__name__)
            return []
        return [row for row in payload if isinstance(row, dict)]


def unwrap_rows(payload: object) -> list[object] | None:
    """Angel wraps lists as ``[...]``, ``{data: [...]}`` or ``{data: {data: [...]}}``."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            deeper = inner.get("data")
            if isinstance(deeper, list):
                return deeper
    return None


def row_to_candle(
    row: object,
    *,
    instrument_id: int,
    interval_minutes: int = 1,
    treat_timestamp_as: str = "open",
) -> Candle | None:
    """``[timestamp, open, high, low, close, volume]`` → :class:`Candle`.

    Never fabricates: a row missing any OHLC component or with a non-positive
    close is dropped (and counted by the caller) rather than zero-filled.
    """
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return None
    raw_stamp, raw_open, raw_high, raw_low, raw_close, raw_volume = row[:6]
    try:
        stamp = parse_ist_stamp(str(raw_stamp))
    except ValueError:
        return None
    if treat_timestamp_as == "close":
        stamp = stamp - timedelta(minutes=interval_minutes)
    open_p, high_p, low_p, close_p = (_num(raw_open), _num(raw_high), _num(raw_low), _num(raw_close))
    volume = _int(raw_volume)
    if None in (open_p, high_p, low_p, close_p) or close_p is None or close_p <= 0:
        return None
    if high_p < low_p:
        return None
    candle_start = stamp.replace(microsecond=0)
    return Candle(
        instrument_id=instrument_id,
        trading_date=candle_start.date(),
        candle_start=candle_start,
        open=float(open_p),
        high=float(high_p),
        low=float(low_p),
        close=float(close_p),
        volume=max(0, volume),
        is_complete=True,
        # Historical bars have no tick-level provenance; they are good enough to
        # warm up EMA/volume history and are labelled so nowhere is claimed to
        # be a live-verified candle.
        data_quality=DataQuality.OK,
        source_timestamp=candle_start,
        tick_count=0,
    )


def _num(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # NaN → None


def _int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0

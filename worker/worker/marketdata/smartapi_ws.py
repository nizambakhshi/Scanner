"""SmartAPI V2 ``smart-stream`` WebSocket client (asyncio).

Why this is hand-rolled instead of using ``smartapi-python``: the official SDK
pins ``twisted``/``autobahn``/``logzero``, its V2 example mismatches the docs on
the heartbeat (docs require a *text* ``ping`` every ≤30 s; the SDK sends
protocol-level pings), and it cannot be driven from an asyncio worker without a
second event loop. The wire contract itself is implemented verbatim from the
official docs (see ``docs/research/smartapi-ws-findings.md``):

* handshake headers ``Authorization`` / ``x-api-key`` / ``x-client-code`` /
  ``x-feed-token``;
* JSON text frames for subscribe/unsubscribe, little-endian binary frames for
  market data (``feed_protocol.decode_packet``);
* text ``ping`` → ``pong`` heartbeat;
* JSON error frames (``E1001`` / ``E1002``) arrive on the same socket;
* quota: 1000 token-mode subscriptions per session, 3 concurrent sockets per
  client code.

The client owns *transport* only. It never fabricates a tick, it reports
connection health, and it moves the feed to STALE / DISCONNECTED on silence so
the pipeline can suspend signals instead of emitting alerts on old data
(spec §7.2, §7.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import websockets

from ..config import AngelSettings
from ..logging_setup import get_logger, scrub
from ..models import FeedStatus, Tick
from ..timeutils import now_ist
from .feed_protocol import (
    FeedDecodeError,
    FeedPacket,
    build_subscribe_message,
    build_unsubscribe_message,
    decode_packet,
    error_text,
    is_subscription_error,
    parse_text_frame,
    scale_price,
)

log = get_logger(__name__)


def connect_smart_stream(url: str, **kwargs: object) -> object:
    """``websockets.connect`` with the implicit keepalive disabled.

    Live behaviour (2026-10-12): Angel's smart-stream never answers
    protocol-level ping frames, so the library default (ping every 20 s,
    pong timeout 20 s) closed our healthy socket every ~40 s with
    ``1011 keepalive ping timeout``. The docs' heartbeat is a *text*
    ``ping`` frame — which ``_heartbeat`` already sends — so the socket
    must not be killed by the binary-ping watchdog.
    """
    kwargs.setdefault("ping_interval", None)
    kwargs.setdefault("ping_timeout", None)
    return websockets.connect(url, **kwargs)


TickHandler = Callable[[Tick], Awaitable[None]]
StatusHandler = Callable[[FeedStatus, str], Awaitable[None]]
Resolver = Callable[[str, str], "int | None"]  # (exchange, token) -> instrument_id

# Docs: "The client must send a heartbeat message for each WebSocket connection
# every 30 sec to keep the connection alive." We sit well inside that.
HEARTBEAT_INTERVAL_SECONDS = 20.0

EXCHANGE_TYPE_NSE_CM = 1


class FeedQuotaError(RuntimeError):
    """Raised when a subscription would exceed the documented session quota."""


@dataclass
class ReconnectPolicy:
    """Exponential backoff with jitter and an absolute cap."""

    base_seconds: float = 1.0
    multiplier: float = 2.0
    max_seconds: float = 60.0
    jitter_ratio: float = 0.25
    max_attempts: int = 0  # 0 = never give up

    def delay(self, attempt: int) -> float:
        raw = min(self.max_seconds, self.base_seconds * (self.multiplier ** max(0, attempt - 1)))
        jitter = raw * self.jitter_ratio
        return max(0.1, raw + random.uniform(-jitter, jitter))  # noqa: S311 - not security


@dataclass
class FeedStats:
    frames_binary: int = 0
    frames_text: int = 0
    decode_errors: int = 0
    pings_sent: int = 0
    pongs_received: int = 0
    reconnects: int = 0
    subscription_errors: int = 0
    ticks_emitted: int = 0
    last_frame_monotonic: float = 0.0
    last_tick_monotonic: float = 0.0
    last_error: str = ""

    def seconds_since_frame(self, *, now: float) -> float:
        if not self.last_frame_monotonic:
            return float("inf")
        return max(0.0, now - self.last_frame_monotonic)

    def seconds_since_tick(self, *, now: float) -> float:
        if not self.last_tick_monotonic:
            return float("inf")
        return max(0.0, now - self.last_tick_monotonic)

    def as_dict(self) -> dict[str, object]:
        return {
            "frames_binary": self.frames_binary,
            "frames_text": self.frames_text,
            "decode_errors": self.decode_errors,
            "pings_sent": self.pings_sent,
            "pongs_received": self.pongs_received,
            "reconnects": self.reconnects,
            "subscription_errors": self.subscription_errors,
            "ticks_emitted": self.ticks_emitted,
            "last_error": scrub(self.last_error),
        }


class SmartApiFeed:
    """One compliant WebSocket session with automatic resubscribe."""

    def __init__(
        self,
        *,
        settings: AngelSettings,
        auth: object,
        resolve: Resolver,
        on_tick: TickHandler,
        on_status: StatusHandler | None = None,
        heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        stale_after_seconds: float = 45.0,
        reconnect_after_seconds: float = 90.0,
        policy: ReconnectPolicy | None = None,
        connect: Callable[..., Awaitable[object]] = connect_smart_stream,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 5.0 <= heartbeat_seconds <= 30.0:
            raise ValueError("heartbeat_seconds must be between 5 and 30 (docs require <=30s)")
        if reconnect_after_seconds < stale_after_seconds:
            raise ValueError("reconnect_after_seconds must exceed stale_after_seconds")
        self.settings = settings
        self.auth = auth
        self.resolve = resolve
        self.on_tick = on_tick
        self.on_status = on_status
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.stale_after_seconds = float(stale_after_seconds)
        self.reconnect_after_seconds = float(reconnect_after_seconds)
        self.policy = policy or ReconnectPolicy()
        self._connect = connect
        self._monotonic = monotonic
        self.stats = FeedStats()
        self.status = FeedStatus.DISCONNECTED
        self._subscribed: dict[int, set[str]] = {}
        self._pending: dict[int, set[str]] = {}
        self._socket: object | None = None
        self._stop = asyncio.Event()

    # ── subscription bookkeeping ──────────────────────────────────────────

    @property
    def subscription_count(self) -> int:
        """Token-mode subscriptions in play — the quota the API actually counts."""
        return sum(len(tokens) for tokens in self._subscribed.values())

    @property
    def tokens(self) -> set[str]:
        return {token for tokens in self._subscribed.values() for token in tokens}

    def subscribed_tokens(self, mode: int | None = None) -> set[str]:
        feed_mode = self._mode(mode)
        return set(self._subscribed.get(feed_mode, set()))

    def quota_remaining(self, mode: int | None = None) -> int:
        limit = self.settings.max_tokens_per_session
        used = (
            len(self._subscribed.get(self._mode(mode), set()))
            if mode is not None
            else self.subscription_count
        )
        return max(0, limit - used)

    def _mode(self, mode: int | None) -> int:
        return int(self.settings.feed_mode if mode is None else mode)

    async def subscribe(self, tokens: Sequence[str], *, mode: int | None = None) -> int:
        """Queue tokens for subscription; returns how many were accepted.

        The documented quota counts a token *per mode*, so tokens beyond the
        session limit are refused here rather than sent and rejected by an
        ``E1002`` mid-session — the caller reports the shortfall as coverage.
        """
        feed_mode = self._mode(mode)
        wanted = [str(token) for token in tokens if str(token)]
        if not wanted:
            return 0
        already = self._subscribed.get(feed_mode, set()) | self._pending.get(feed_mode, set())
        fresh = [token for token in dict.fromkeys(wanted) if token not in already]
        room = self.quota_remaining(feed_mode)
        accepted = fresh[:room]
        if len(fresh) > room:
            log.warning(
                "feed_quota_refused",
                mode=feed_mode,
                requested=len(fresh),
                accepted=room,
                limit=self.settings.max_tokens_per_session,
            )
        if accepted:
            self._pending.setdefault(feed_mode, set()).update(accepted)
            if self._socket is not None:
                await self.push_subscriptions()
        return len(accepted)

    async def push_subscriptions(self) -> None:
        """Send every pending token in compliant-sized messages."""
        socket = self._socket
        if socket is None:
            return
        for mode, pending in list(self._pending.items()):
            tokens = sorted(pending)
            if not tokens:
                continue
            for start in range(0, len(tokens), self.settings.max_tokens_per_message):
                chunk = tokens[start : start + self.settings.max_tokens_per_message]
                message = build_subscribe_message(
                    correlation_id=self._correlation_id(),
                    mode=mode,
                    tokens=chunk,
                    exchange_type=EXCHANGE_TYPE_NSE_CM,
                )
                await socket.send(json.dumps(message, separators=(",", ":")))  # type: ignore[union-attr]
                self._subscribed.setdefault(mode, set()).update(chunk)
            pending.clear()

    async def unsubscribe(self, tokens: Sequence[str], *, mode: int | None = None) -> int:
        feed_mode = self._mode(mode)
        wanted = {str(token) for token in tokens}
        self._pending.get(feed_mode, set()).difference_update(wanted)
        current = self._subscribed.get(feed_mode, set())
        remove = sorted(current & wanted)
        if not remove or self._socket is None:
            return len(remove)
        for start in range(0, len(remove), self.settings.max_tokens_per_message):
            chunk = remove[start : start + self.settings.max_tokens_per_message]
            await self._socket.send(  # type: ignore[union-attr]
                json.dumps(
                    build_unsubscribe_message(
                        correlation_id=self._correlation_id(),
                        mode=feed_mode,
                        tokens=chunk,
                        exchange_type=EXCHANGE_TYPE_NSE_CM,
                    ),
                    separators=(",", ":"),
                )
            )
            current.difference_update(chunk)
        return len(remove)

    def stop(self) -> None:
        self._stop.set()

    @property
    def should_stop(self) -> bool:
        return self._stop.is_set()

    async def set_status(self, status: FeedStatus, reason: str = "") -> None:
        """Status transitions are the pipeline's only evidence of feed health."""
        if status is self.status:
            return
        self.status = status
        if reason:
            self.stats.last_error = reason[:300]
        log.info("feed_status", status=status.value, reason=scrub(reason))
        if self.on_status is not None:
            await self.on_status(status, reason)

    # ── the loop ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, stream, reconnect until :meth:`stop` is requested."""
        attempt = 0
        while not self._stop.is_set():
            started_at = self._monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport errors are routine
                self.stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("feed_session_error", error=scrub(self.stats.last_error))
            if self._stop.is_set():
                break
            if (self._monotonic() - started_at) >= 30.0:
                attempt = 0  # a healthy session resets the backoff ladder
            attempt += 1
            if self.policy.max_attempts and attempt > self.policy.max_attempts:
                await self.set_status(
                    FeedStatus.ERROR, f"giving up after {attempt - 1} reconnect attempts"
                )
                return
            await self.set_status(FeedStatus.DISCONNECTED, "reconnecting")
            delay = self.policy.delay(attempt)
            self.stats.reconnects += 1
            log.info("feed_reconnect_wait", seconds=round(delay, 2), attempt=attempt)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _session(self) -> None:
        headers_source = getattr(self.auth, "websocket_headers", None)
        if headers_source is None:
            raise RuntimeError("feed needs an auth object exposing websocket_headers()")
        headers = await headers_source()
        await self.set_status(FeedStatus.CONNECTING, "opening smart-stream")
        async with self._connect(self.settings.ws_url, additional_headers=headers) as socket:
            self._socket = socket
            await self.set_status(FeedStatus.CONNECTED, "socket open")
            await self.push_subscriptions()
            tasks = [
                asyncio.create_task(self._receive(socket), name="feed-receive"),
                asyncio.create_task(self._heartbeat(socket), name="feed-ping"),
                asyncio.create_task(self._watchdog(socket), name="feed-watchdog"),
            ]
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    error = task.exception()  # re-raise so run() logs it
                    if error is not None:
                        raise error
            finally:
                self._socket = None
                for task in tasks:
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*tasks)
                await _close(socket)

    async def _receive(self, socket: object) -> None:
        async for raw in socket:  # type: ignore[union-attr]
            if self._stop.is_set():
                return
            self.stats.last_frame_monotonic = self._monotonic()
            if isinstance(raw, (bytes, bytearray)):
                self.stats.frames_binary += 1
                try:
                    packet = decode_packet(bytes(raw))
                except FeedDecodeError as exc:
                    self.stats.decode_errors += 1
                    self.stats.last_error = scrub(str(exc))[:300]
                    continue
                await self._handle_packet(packet)
                continue
            self.stats.frames_text += 1
            payload = parse_text_frame(raw)
            if payload is None:  # a bare "pong"
                self.stats.pongs_received += 1
                continue
            if is_subscription_error(payload):
                self.stats.subscription_errors += 1
                text = error_text(payload)
                self.stats.last_error = scrub(text)[:300]
                log.error(
                    "feed_subscription_error",
                    code=payload.get("errorCode"),
                    message=scrub(text),
                    correlation_id=payload.get("correlationID"),
                )
                # A refused message means those tokens never went live; clearing
                # pending stops a reconnect loop against the same rejection.
                for pending in self._pending.values():
                    pending.clear()
                await self.set_status(FeedStatus.ERROR, f"subscription error: {text}")
                continue
            log.info("feed_text_frame", frame=scrub(json.dumps(payload, default=str))[:200])

    async def _handle_packet(self, packet: FeedPacket) -> None:
        instrument_id = self.resolve(packet.exchange_type_name, packet.token)
        if instrument_id is None:
            return  # not in this universe: the frame is ignored, never guessed at
        price = scale_price(packet.last_traded_price_paise, self.settings.price_scale)
        if price is None or price <= 0:
            self.stats.decode_errors += 1
            return
        try:
            exchange_ts = packet.exchange_timestamp
        except FeedDecodeError as exc:
            self.stats.decode_errors += 1
            self.stats.last_error = scrub(str(exc))[:300]
            return
        tick = Tick(
            instrument_id=instrument_id,
            instrument_token=packet.token,
            exchange=packet.exchange_type_name,
            exchange_timestamp=exchange_ts,
            received_at=now_ist(),
            last_traded_price=float(price),
            last_traded_quantity=packet.last_traded_quantity,
            cumulative_volume=packet.volume_trade_for_the_day,
            average_traded_price=scale_price(
                packet.average_traded_price_paise, self.settings.price_scale
            ),
            open_price=scale_price(packet.open_price_paise, self.settings.price_scale),
            high_price=scale_price(packet.high_price_paise, self.settings.price_scale),
            low_price=scale_price(packet.low_price_paise, self.settings.price_scale),
            previous_close=scale_price(packet.closed_price_paise, self.settings.price_scale),
            sequence_number=packet.sequence_number,
            feed_mode=packet.subscription_mode,
        )
        self.stats.ticks_emitted += 1
        self.stats.last_tick_monotonic = self._monotonic()
        if self.status is not FeedStatus.STREAMING:
            await self.set_status(FeedStatus.STREAMING, "ticks arriving")
        await self.on_tick(tick)

    async def _heartbeat(self, socket: object) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_seconds)
                return
            except TimeoutError:
                pass
            try:
                await socket.send("ping")  # type: ignore[union-attr]
                self.stats.pings_sent += 1
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"heartbeat failed: {type(exc).__name__}"[:300]
                log.warning("feed_heartbeat_failed", error=scrub(self.stats.last_error))
                await _close(socket)
                return

    async def _watchdog(self, socket: object) -> None:
        """Turn silence into an explicit state instead of a silent stall."""
        was_stale = False
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            quiet = self.stats.seconds_since_frame(now=self._monotonic())
            if quiet >= self.reconnect_after_seconds:
                log.warning("feed_watchdog_reconnect", quiet_seconds=round(quiet, 1))
                await _close(socket)
                return
            if quiet >= self.stale_after_seconds:
                if not was_stale:
                    await self.set_status(FeedStatus.STALE, f"no frame for {quiet:.0f}s")
                    was_stale = True
            elif was_stale and self.stats.last_tick_monotonic:
                was_stale = False
                await self.set_status(FeedStatus.STREAMING, "frames resumed")

    def _correlation_id(self) -> str:
        return f"sc{int(self._monotonic() * 1000) % 1_000_000_000:09d}"[:10]


async def _close(socket: object) -> None:
    closer = getattr(socket, "close", None)
    if closer is not None:
        with contextlib.suppress(Exception):
            await closer()

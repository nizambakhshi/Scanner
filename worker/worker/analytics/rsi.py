"""Wilder RSI: the intraday reading, and the two-timeframe pair behind it.

Operator rule, restated 2026-10-15 (it *reversed* the 2026-10-12 reading):

* RSI **below 40** is the green side — it is an **entry**.
* RSI **above 60** is the red side — **overbought**.
* the 40–60 band, and anything without enough history to judge, is **NEUTRAL**.

Two facts carry that rule, because they answer different questions:

``classify_intraday(rsi_5m, trend)`` — the entry rule as the operator described it:
"RSI *at the time of* a bullish or bearish trend". Wilder RSI-14 on the session's
5-minute closes, read beside the trend the scanner already measures (close above
session VWAP and fast EMA above slow EMA). An oversold print inside an uptrend is
the pullback entry; the same print while the tape is below VWAP with EMAs crossed
down is not, and would page all day. The mirror lives in SQL and on the board, so
this is the definition both must agree with.

``classify(daily_rsi, weekly_rsi)`` — the nightly pair ``scanner-sync-daily``
writes to ``stock_details``: both timeframes below 40 → ``ENTRY``, both above 60 →
``OVERBOUGHT``, anything else (including one side agreeing and the other not, and
values exactly on 40 or 60) → ``NEUTRAL``.

Why the stored words changed rather than the colours: a row that says ``BULLISH``
while the page paints it red and Telegram says "stay away" is a trap for whoever
reads it next — the database, the badge and the alert now name the same side.

Why Wilder smoothing and not a simple average of gains/losses: RSI is defined
that way (Wilder 1978) and every charting package the operator compares against
uses it, so a different smoothing here would silently disagree with the
platform they check by eye. The recursion and the SMA seed are stated in
:func:`wilder_rsi` and asserted against hand-computed numbers in
``tests/unit/test_rsi.py``.

Weekly bars are **resampled from the daily series** (ISO weeks, Monday start)
rather than fetched from the broker: Angel's ONE_DAY history is corporate-action
adjusted (verified 2026-10-12), so resampling from it keeps the weekly series
consistent with the daily one by construction, and the newest row is allowed to
be a partial week — a Monday close *is* a weekly close until Friday replaces it.
That matches how the operator reads a weekly RSI on a live chart: it moves
during the week.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

#: The operator's two lines, verbatim: below 40 is the entry side, above 60 is the
#: expensive side. Exactly 40.0 or 60.0 falls in the band — there is no dead zone
#: where a value belongs to no bucket.
ENTRY_THRESHOLD = 40.0
OVERBOUGHT_THRESHOLD = 60.0

RSI_PERIOD = 14

#: The bucket the intraday reading is measured on: five one-minute closes, the same
#: partition the /strategy board and the ORB detector use.
BUCKET_SECONDS = 300

#: The three states; also the values stored in stock_details.rsi_bias and in
#: indicator_snapshots.rsi_state.
ENTRY = "ENTRY"
OVERBOUGHT = "OVERBOUGHT"
NEUTRAL = "NEUTRAL"

#: The two trend sides an RSI reading is judged against. Names, not booleans,
#: because "mixed" and "not evaluable yet" are different answers.
TREND_UP = "UP"
TREND_DOWN = "DOWN"
TREND_MIXED = "MIXED"


class DailyBarLike(Protocol):
    """Anything shaped like a daily bar: Candle, a DB row, a test double."""

    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class WeeklyBar:
    """One ISO week of daily bars (Monday ``week_start``; last week may be partial)."""

    week_start: date
    open: float
    high: float
    low: float
    close: float
    volume: int


def wilder_rsi(closes: Sequence[float], period: int = RSI_PERIOD) -> list[float | None]:
    """RSI series aligned 1:1 with ``closes``; ``None`` until there is enough history.

    Definition used (Wilder's original):

    * changes ``d_t = C_t - C_{t-1}`` for ``t >= 1``;
    * the first RSI (index ``period``) is seeded with the **simple average** of
      the first ``period`` gains and losses;
    * afterwards ``avg_t = (avg_{t-1} * (period - 1) + current_t) / period`` —
      Wilder's smoothing, i.e. an EMA with ``alpha = 1/period``;
    * ``RSI = 100 - 100 / (1 + avg_gain / avg_loss)``.

    Degenerate cases, chosen explicitly because charts disagree here and the
    test suite pins the choice:

    * ``avg_loss == 0`` with any gain → ``100.0`` (only up moves can run here);
    * ``avg_gain == 0`` with any loss → ``0.0``;
    * both zero (perfectly flat window) → ``50.0`` — undefined by the formula,
      rendered as the neutral midpoint rather than a fake extreme.

    Raises ``ValueError`` on a non-positive or non-finite close: a bad price
    must surface as a data problem, not as an RSI that looks plausible.
    """
    if period < 2:
        raise ValueError(f"RSI period must be >= 2, got {period}")
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    for close in closes:
        try:
            value = float(close)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"RSI close must be a number, got {close!r}") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"RSI close must be a positive finite number, got {close!r}")

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def latest_rsi(closes: Sequence[float], period: int = RSI_PERIOD) -> float | None:
    """Last value of :func:`wilder_rsi` — ``None`` when history is too short."""
    series = wilder_rsi(closes, period=period)
    return series[-1] if series else None


def week_start_of(day: date) -> date:
    """Monday of the ISO week containing ``day``."""
    return day - timedelta(days=day.weekday())


def resample_weekly(bars: Iterable[DailyBarLike]) -> list[WeeklyBar]:
    """Aggregate daily bars into ISO weeks, oldest first.

    ``open`` = first day's open, ``close`` = last day's close, ``high``/``low``
    = extremes, ``volume`` = sum. Input order is irrelevant (sorted here);
    duplicate dates are kept as-is because the daily table's primary key makes
    them impossible upstream.
    """
    grouped: dict[date, list[DailyBarLike]] = {}
    for bar in bars:
        grouped.setdefault(week_start_of(bar.trading_date), []).append(bar)
    out: list[WeeklyBar] = []
    for week, days in sorted(grouped.items()):
        days.sort(key=lambda bar: bar.trading_date)
        out.append(
            WeeklyBar(
                week_start=week,
                open=float(days[0].open),
                high=max(float(day.high) for day in days),
                low=min(float(day.low) for day in days),
                close=float(days[-1].close),
                volume=sum(int(day.volume) for day in days),
            )
        )
    return out


def resample_and_compute(
    bars: Iterable[DailyBarLike], period: int = RSI_PERIOD
) -> tuple[list[WeeklyBar], list[float | None]]:
    """Weekly bars plus their RSI series (index-aligned with the returned bars)."""
    weekly = resample_weekly(bars)
    closes = [bar.close for bar in weekly]
    return weekly, wilder_rsi(closes, period=period)


def classify(daily_rsi: float | None, weekly_rsi: float | None) -> str | None:
    """The nightly two-timeframe state: ENTRY / OVERBOUGHT / NEUTRAL.

    ``None`` when either value is missing — an uninformed pair must not present
    as NEUTRAL, because NEUTRAL is a real observation the alert engine acts on
    (it clears the previous state), while ``None`` means "not evaluable yet".
    """
    if daily_rsi is None or weekly_rsi is None:
        return None
    if daily_rsi < ENTRY_THRESHOLD and weekly_rsi < ENTRY_THRESHOLD:
        return ENTRY
    if daily_rsi > OVERBOUGHT_THRESHOLD and weekly_rsi > OVERBOUGHT_THRESHOLD:
        return OVERBOUGHT
    return NEUTRAL


# ── the intraday rule ───────────────────────────────────────────────────────


def trend_state(
    *,
    close: float | None,
    vwap: float | None,
    ema_fast: float | None,
    ema_slow: float | None,
) -> str:
    """Which side of its own trend a candle finished on.

    Only the two *direction* conditions the scanner already evaluates: a trend is
    up when the close is above session VWAP **and** the fast EMA is above the slow
    one, and down when both fail on the same candle — the same pair of tests as
    :meth:`Scanner.is_removal_condition`. The relative-volume gate is deliberately
    *not* here: volume says people are watching, not which way price is trending,
    and an entry rule that required it would ignore every quiet pullback.

    Returns ``TREND_MIXED`` when the two disagree (the tape is chopping) or when a
    value needed to judge is missing. Mixed is a real answer, not an absence: a
    reading during a chop is neither an entry nor a warning.
    """
    if close is None or vwap is None or ema_fast is None or ema_slow is None:
        return TREND_MIXED
    above_vwap = close > vwap
    emas_up = ema_fast > ema_slow
    if above_vwap and emas_up:
        return TREND_UP
    if not above_vwap and not emas_up:
        return TREND_DOWN
    return TREND_MIXED


def classify_intraday(
    rsi_5m: float | None,
    trend: str,
    *,
    entry_threshold: float = ENTRY_THRESHOLD,
    overbought_threshold: float = OVERBOUGHT_THRESHOLD,
) -> str | None:
    """The entry rule as the operator put it: oversold *while the trend is up*.

    ``ENTRY``  — RSI below ``entry_threshold`` while the trend is up. A dip inside
                 an uptrend, which is the one place a low RSI is a buy rather than
                 a falling tape.
    ``OVERBOUGHT`` — RSI above ``overbought_threshold`` while the trend is down. An
                 expensive print inside a downtrend.
    ``NEUTRAL`` — the band, or an extreme that the trend does not confirm.
    ``None``   — no reading yet (a young session has fewer than 15 five-minute
                 buckets, and inventing a state for that would be worse than an
                 em dash).
    """
    if rsi_5m is None:
        return None
    if rsi_5m < entry_threshold and trend == TREND_UP:
        return ENTRY
    if rsi_5m > overbought_threshold and trend == TREND_DOWN:
        return OVERBOUGHT
    return NEUTRAL


def closes_bucket(candle_start: datetime) -> bool:
    """Is this 1-minute candle the last one of its five-minute bucket?

    ``minute % 5 == 4`` is the wall-clock form of the board's
    ``floor(epoch / 300)`` partition: NSE's session opens at 09:15, an exact
    five-minute boundary, so the candle that starts at ``:19`` is the one whose
    close ends the 09:15–09:20 bucket. Written from the clock rather than the
    epoch on purpose — ``datetime.timestamp()`` of a *naive* value resolves
    against the machine's zone, and a bucket that moved by an hour would silently
    shift the whole series.
    """
    return candle_start.second == 0 and candle_start.minute % 5 == 4


class WilderRsi:
    """Streaming Wilder RSI-14 over a series of closes.

    Same recursion, seed and degenerate cases as :func:`wilder_rsi` — the test
    suite feeds both the same list and requires the running value to equal the
    series' last entry, because a second implementation that drifts is the bug the
    operator would see as a dashboard disagreeing with itself.

    Streaming rather than recomputed each minute because this runs for every
    instrument on every bucket close (212 names × 5-min buckets, 75 sessions of
    history kept in the process), and a 14-step recursion per candle is cheaper to
    maintain than a window function over the day's tape.
    """

    def __init__(self, period: int = RSI_PERIOD) -> None:
        if period < 2:
            raise ValueError(f"RSI period must be >= 2, got {period}")
        self.period = period
        self._closes: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def observations(self) -> int:
        """How many closes have been fed — ``period + 1`` is the first readable one."""
        return len(self._closes)

    def update(self, close: float) -> float | None:
        try:
            value = float(close)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"RSI close must be a number, got {close!r}") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"RSI close must be a positive finite number, got {close!r}")
        self._closes.append(value)
        if len(self._closes) <= self.period:
            # Still gathering the seed window: no previous close to compare with
            # until the second print, and no average until the fifteenth.
            return None
        change = self._closes[-1] - self._closes[-2]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if self._avg_gain is None or self._avg_loss is None:
            gains = [
                max(self._closes[i] - self._closes[i - 1], 0.0)
                for i in range(1, self.period + 1)
            ]
            losses = [
                max(-(self._closes[i] - self._closes[i - 1]), 0.0)
                for i in range(1, self.period + 1)
            ]
            self._avg_gain = sum(gains) / self.period
            self._avg_loss = sum(losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        self._value = _rsi_from_averages(self._avg_gain, self._avg_loss)
        return self._value

    def to_dict(self) -> dict[str, Any]:
        """Checkpointable view. ``closes`` is bounded by the period, not the day."""
        return {
            "period": self.period,
            "closes": self._closes[-(self.period + 1) :],
            "avg_gain": self._avg_gain,
            "avg_loss": self._avg_loss,
            "value": self._value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WilderRsi:
        """Restore a checkpoint. ``closes`` is the tail written by :meth:`to_dict`.

        Read back as stored rather than recomputed from the tail: the tail is only
        long enough to produce the *next* value, while the averages carry the whole
        history behind the current one. Feeding the tail again would silently
        re-seed a series that had 200 buckets behind it.
        """
        engine = cls(period=int(payload["period"]))
        engine._closes = [float(close) for close in payload.get("closes", [])]
        engine._avg_gain = None if payload.get("avg_gain") is None else float(payload["avg_gain"])
        engine._avg_loss = None if payload.get("avg_loss") is None else float(payload["avg_loss"])
        engine._value = None if payload.get("value") is None else float(payload["value"])
        return engine


def as_dict(daily_rsi: float | None, weekly_rsi: float | None) -> dict[str, Any]:
    """Small payload helper for logs and heartbeat-style reporting."""
    return {
        "daily_rsi": None if daily_rsi is None else round(daily_rsi, 4),
        "weekly_rsi": None if weekly_rsi is None else round(weekly_rsi, 4),
        "rsi_bias": classify(daily_rsi, weekly_rsi),
    }

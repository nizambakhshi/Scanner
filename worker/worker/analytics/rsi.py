"""Wilder RSI on daily and weekly closes, plus the two-timeframe bias rule.

Operator feature (2026-10-12): the /stocks page and the watchlist show a daily
and a weekly RSI, and the scanner raises an alert when the pair changes state:

* weekly RSI **and** daily RSI both above 60 → ``BULLISH``
* weekly RSI **and** daily RSI both below 40 → ``BEARISH``
* anything else (including one side agreeing and the other not, and values
  exactly on 40 or 60) → ``NEUTRAL``

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
from datetime import date, timedelta
from typing import Any, Protocol

#: Operator thresholds, verbatim: above 60 bullish, below 40 bearish, between
#: neutral. Exactly 60.0 or 40.0 falls in "between" — there is no dead zone
#: where a value belongs to no bucket.
BULLISH_THRESHOLD = 60.0
BEARISH_THRESHOLD = 40.0

RSI_PERIOD = 14

#: The three bias values; also the values stored in stock_details.rsi_bias.
BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"


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
    """The operator's two-timeframe bias: BULLISH / BEARISH / NEUTRAL.

    ``None`` when either value is missing — an uninformed pair must not present
    as NEUTRAL, because NEUTRAL is a real observation the alert engine acts on
    (it clears the previous bias), while ``None`` means "not evaluable yet".
    """
    if daily_rsi is None or weekly_rsi is None:
        return None
    if daily_rsi > BULLISH_THRESHOLD and weekly_rsi > BULLISH_THRESHOLD:
        return BULLISH
    if daily_rsi < BEARISH_THRESHOLD and weekly_rsi < BEARISH_THRESHOLD:
        return BEARISH
    return NEUTRAL


def as_dict(daily_rsi: float | None, weekly_rsi: float | None) -> dict[str, Any]:
    """Small payload helper for logs and heartbeat-style reporting."""
    return {
        "daily_rsi": None if daily_rsi is None else round(daily_rsi, 4),
        "weekly_rsi": None if weekly_rsi is None else round(weekly_rsi, 4),
        "rsi_bias": classify(daily_rsi, weekly_rsi),
    }

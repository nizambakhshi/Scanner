"""Time, timezone and trading-session calendar helpers.

Everything in the system is keyed to the *exchange* clock (Asia/Kolkata), not
the machine clock: a worker deployed from Europe or restarted mid-session must
still put ticks into the right one-minute bucket and must not treat yesterday's
indicators as today's session VWAP (spec §6).

The calendar is data-driven: regular session hours and the holiday list come
from ``data/calendar/nse_holidays.json`` when present, so an exceptional
session can be handled without a code change. When that file is missing or
stale the worker still runs, but ``calendar_status()`` reports it and the
session manager surfaces the warning instead of silently guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

IST_STR = "Asia/Kolkata"

# NSE regular equity session defaults (spec §6).
DEFAULT_OPEN = time(9, 15)
DEFAULT_CLOSE = time(15, 30)
DEFAULT_PRE_OPEN_START = time(9, 0)
# NSE market timings: continuous trading ends 15:30, the closing session runs
# 15:40–16:00 (verified from nseindia.com/static/market-data/market-timings).
DEFAULT_POST_CLOSE_END = time(16, 0)

# A session is 375 completed one-minute candles: 09:15 -> 15:30.
SESSION_CANDLE_SLOTS = 375

CALENDAR_PATH = Path(__file__).resolve().parents[2] / "data" / "calendar" / "nse_holidays.json"
CALENDAR_MAX_AGE_DAYS = 120


class SessionPhase(str):
    """Named session phases. Kept as a plain str subclass so it stores cleanly."""

    HOLIDAY = "HOLIDAY"
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    ACTIVE = "ACTIVE"
    POST_CLOSE = "POST_CLOSE"


def ist_zoneinfo() -> ZoneInfo:
    try:
        return ZoneInfo(IST_STR)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - needs tzdata
        # tzdata is a hard dependency; if the OS has no database at all, UTC
        # plus an explicit offset is better than crashing the feed loop.
        return ZoneInfo("UTC")


def to_ist(value: datetime) -> datetime:
    """Convert any aware/naive-UTC datetime to Asia/Kolkata."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ist_zoneinfo())


def from_epoch_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=ZoneInfo("UTC"))


def to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; attach a timezone")
    return int(value.timestamp() * 1000)


def now_ist() -> datetime:
    return datetime.now(tz=ist_zoneinfo())


def parse_ist_stamp(value: str) -> datetime:
    """Parse an Angel-style IST stamp that carries no offset.

    Angel One historically returns ``YYYY-MM-DD HH:MM:SS`` strings in IST and
    never says so. Newer responses include a real ISO offset. Reading either as
    UTC shifts every bar by 5.5 hours, so: use the offset when present,
    otherwise assume IST explicitly.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    normalized = text.replace("T", " ") if "T" in text and " " not in text else text
    has_offset = normalized.endswith("Z") or normalized[-6:-3] in {"+05", "-05", "+00", "-00"} or (
        len(normalized) > 19 and normalized[19] in "+-"
    )
    if has_offset:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return to_ist(parsed)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=ist_zoneinfo())
    raise ValueError(f"unsupported timestamp format: {value!r}")


def angel_rest_stamp(value: datetime) -> str:
    """Format ``YYYY-MM-DD HH:MM`` on the exchange clock (Angel's REST format)."""
    local = to_ist(value)
    return local.strftime("%Y-%m-%d %H:%M")


def candle_start(value: datetime) -> datetime:
    """Floor a timestamp to its one-minute candle start (IST, second-aligned)."""
    local = to_ist(value)
    return local.replace(second=0, microsecond=0)


def candle_end(start: datetime) -> datetime:
    """Inclusive-style end boundary used for DB uniqueness: start + 1 minute."""
    return candle_start(start) + timedelta(minutes=1)


def is_same_session(start: datetime, other: datetime) -> bool:
    return candle_start(start).date() == candle_start(other).date()


@dataclass(frozen=True)
class SessionWindow:
    trading_date: date
    open_at: datetime
    close_at: datetime
    pre_open_start_at: datetime
    post_close_end_at: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.close_at - self.open_at).total_seconds() // 60)

    def candle_index(self, value: datetime) -> int:
        """1-based index of the minute bucket that ``value`` belongs to.

        Ticks before the open or after the close return 0 so callers can reject
        them rather than filing them into the first/last candle.
        """
        local = to_ist(value)
        if local < self.open_at or local >= self.close_at:
            return 0
        return int((local - self.open_at).total_seconds() // 60) + 1


@dataclass(frozen=True)
class TradingCalendar:
    """Weekday + holiday aware NSE equity calendar."""

    holidays: frozenset[date]
    holiday_reasons: dict[str, str]
    open_time: time
    close_time: time
    pre_open_start: time
    post_close_end: time
    source: str
    source_type: str
    retrieved_at: str | None
    loaded_from_file: bool
    stale: bool
    #: Date overrides from the dashboard's Days page (`ops_settings['days']`),
    #: mutated in place by the 60-second adoption loop — the pipeline shares
    #: this object, so an override reaches every consumer at once. A mutable
    #: field on a frozen dataclass: the *reference* never changes, its contents
    #: are the live calendar exceptions.
    off_days: set[date] = field(default_factory=set)
    on_days: set[date] = field(default_factory=set)

    def is_trading_day(self, day: date) -> bool:
        # The Days page's rule, same precedence as the app's own gate: a
        # forced-open date beats everything (holiday or weekend), a marked-off
        # date is closed even on a plain weekday, and with neither the
        # weekday-plus-holidays rule below is exactly what ran before this.
        if day in self.on_days:
            return True
        if day in self.off_days:
            return False
        return day.weekday() < 5 and day not in self.holidays

    def session(self, day: date) -> SessionWindow | None:
        if not self.is_trading_day(day):
            return None
        zone = ist_zoneinfo()
        return SessionWindow(
            trading_date=day,
            open_at=datetime.combine(day, self.open_time, tzinfo=zone),
            close_at=datetime.combine(day, self.close_time, tzinfo=zone),
            pre_open_start_at=datetime.combine(day, self.pre_open_start, tzinfo=zone),
            post_close_end_at=datetime.combine(day, self.post_close_end, tzinfo=zone),
        )

    def phase_at(self, moment: datetime) -> str:
        local = to_ist(moment)
        window = self.session(local.date())
        if window is None:
            return SessionPhase.HOLIDAY if local.weekday() < 5 else SessionPhase.CLOSED
        if local < window.pre_open_start_at:
            return SessionPhase.CLOSED
        if local < window.open_at:
            return SessionPhase.PRE_OPEN
        if local < window.close_at:
            return SessionPhase.ACTIVE
        if local < window.post_close_end_at:
            return SessionPhase.POST_CLOSE
        return SessionPhase.CLOSED

    def next_trading_day(self, after: date) -> date:
        cursor = after + timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise RuntimeError("no trading day found within 370 days — calendar looks broken")

    def previous_trading_day(self, before: date) -> date:
        cursor = before - timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise RuntimeError("no previous trading day found within 370 days — calendar looks broken")

    def current_session(self, moment: datetime) -> SessionWindow | None:
        """Session for the trading day ``moment`` falls in (None when closed)."""
        local = to_ist(moment)
        window = self.session(local.date())
        if window is None:
            return None
        if local >= window.post_close_end_at:
            return None
        return window

    def minutes_until_open(self, moment: datetime) -> float | None:
        local = to_ist(moment)
        window = self.session(local.date())
        if window is None:
            return None
        if local >= window.open_at:
            return 0.0
        return (window.open_at - local).total_seconds() / 60.0

    def calendar_status(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_type": self.source_type,
            "retrieved_at": self.retrieved_at,
            "loaded_from_file": self.loaded_from_file,
            "holiday_count": len(self.holidays),
            "stale": self.stale,
            "warning": (
                "holiday calendar is missing or older than "
                f"{CALENDAR_MAX_AGE_DAYS} days; weekday-only assumption"
                if self.stale
                else None
            ),
        }


def _parse_time(value: str | None, default: time) -> time:
    if not value:
        return default
    try:
        hour, minute = value.strip().split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return default


def load_calendar(path: Path | None = None, *, today: date | None = None) -> TradingCalendar:
    """Load the calendar file, degrading loudly (never silently) when absent."""
    target = path or CALENDAR_PATH
    reference = today or now_ist().date()
    holidays: set[date] = set()
    reasons: dict[str, str] = {}
    open_time, close_time = DEFAULT_OPEN, DEFAULT_CLOSE
    pre_open_start, post_close_end = DEFAULT_PRE_OPEN_START, DEFAULT_POST_CLOSE_END
    source = "builtin-weekday-rule"
    source_type = "unverified"
    retrieved_at: str | None = None
    loaded = False
    stale = True

    if target.is_file():
        try:
            payload = json.loads(target.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"trading calendar {target} is unreadable: {exc}") from exc
        for entry in payload.get("holidays", []) or []:
            raw_date = entry.get("date") if isinstance(entry, dict) else entry
            if not raw_date:
                continue
            try:
                parsed = date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            holidays.add(parsed)
            if isinstance(entry, dict) and entry.get("reason"):
                reasons[parsed.isoformat()] = str(entry["reason"])
        regular = payload.get("regular_session", {}) or {}
        open_time = _parse_time(regular.get("open"), DEFAULT_OPEN)
        close_time = _parse_time(regular.get("close"), DEFAULT_CLOSE)
        pre_open_start = _parse_time(regular.get("pre_open_start"), DEFAULT_PRE_OPEN_START)
        post_close_end = _parse_time(regular.get("post_close_end"), DEFAULT_POST_CLOSE_END)
        source = str(payload.get("source", source))
        source_type = str(payload.get("source_type", "unverified"))
        retrieved_at = payload.get("retrieved_at")
        loaded = True
        if retrieved_at:
            try:
                age = reference - date.fromisoformat(str(retrieved_at)[:10])
                stale = age.days > CALENDAR_MAX_AGE_DAYS
            except ValueError:
                stale = True
        else:
            stale = True
        if not holidays:
            stale = True  # file exists but carries no dates — treat as unverified

    return TradingCalendar(
        holidays=frozenset(holidays),
        holiday_reasons=reasons,
        open_time=open_time,
        close_time=close_time,
        pre_open_start=pre_open_start,
        post_close_end=post_close_end,
        source=source,
        source_type=source_type,
        retrieved_at=retrieved_at,
        loaded_from_file=loaded,
        stale=stale,
    )


def parse_days(document: object) -> tuple[set[date], set[date]]:
    """Parse the dashboard's ``ops_settings['days']`` document.

    The app's /days page owns the shape — ``{off_days: [{date, note}], on_days:
    [{date, note}]}`` — and refuses malformed dates at save time, so anything
    unparseable here means a hand-edited row. That is *reported* (ValueError,
    which the adoption loop logs) rather than obeyed halfway: the same rule as
    the strategies document, because a calendar the operator cannot see working
    is worse than one that keeps the last good exceptions.
    """
    if not isinstance(document, dict):
        raise ValueError(f"days document must be an object, got {type(document).__name__}")
    return (_read_day_list(document, "off_days"), _read_day_list(document, "on_days"))


def _read_day_list(document: dict, field: str) -> set[date]:
    raw = document.get(field)
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError(f"days.{field} must be a list")
    days: set[date] = set()
    for entry in raw:
        value = entry.get("date") if isinstance(entry, dict) else entry
        if not value:
            continue
        try:
            days.add(date.fromisoformat(str(value)[:10]))
        except ValueError as exc:
            raise ValueError(f"days.{field} carries an unreadable date {value!r}") from exc
    return days

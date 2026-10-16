"""Unit tests for ``worker.timeutils`` — session calendar and candle boundaries.

All times are built on the fixed Asia/Kolkata date 2026-10-12 (a Monday); the
tests never read the wall clock and always pass an explicit ``today`` to the
loader so holiday/weekday logic is deterministic. The calendar under test is
constructed directly (via the frozen dataclass) so no data file is required.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from worker import timeutils as T
from worker.timeutils import TradingCalendar, ist_zoneinfo

IST = ist_zoneinfo()
MONDAY = date(2026, 10, 12)  # a weekday, not a holiday
TUESDAY = date(2026, 10, 13)
SATURDAY = date(2026, 10, 17)
SUNDAY = date(2026, 10, 18)
HOLIDAY = date(2026, 11, 9)  # a Monday used as a simulated holiday


def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """IST-aware wall-clock instant."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST)


def build_calendar(holidays: tuple[date, ...] = ()) -> TradingCalendar:
    """A calendar with default NSE timings and an explicit holiday set, no I/O."""
    return TradingCalendar(
        holidays=frozenset(holidays),
        holiday_reasons={h.isoformat(): "test holiday" for h in holidays},
        open_time=time(9, 15),
        close_time=time(15, 30),
        pre_open_start=time(9, 0),
        post_close_end=time(16, 0),
        source="test",
        source_type="test",
        retrieved_at=None,
        loaded_from_file=False,
        stale=False,
    )


CALENDAR = build_calendar((HOLIDAY,))


# ── documented session constants ──────────────────────────────────────────


def test_default_session_window_is_0915_to_1530() -> None:
    expected_open, expected_close = time(9, 15), time(15, 30)
    assert expected_open == T.DEFAULT_OPEN
    assert expected_close == T.DEFAULT_CLOSE
    assert T.SESSION_CANDLE_SLOTS == 375


# ── candle boundaries ─────────────────────────────────────────────────────


def test_candle_start_floors_to_the_minute() -> None:
    stamped = at(MONDAY, 9, 15, 45) + timedelta(microseconds=123)
    floored = T.candle_start(stamped)
    assert floored == at(MONDAY, 9, 15, 0)
    assert floored.second == 0 and floored.microsecond == 0


def test_candle_start_and_end_are_one_minute_apart() -> None:
    start = at(MONDAY, 10, 2, 30)
    floored = T.candle_start(start)
    assert T.candle_end(start) == floored + timedelta(minutes=1)


def test_is_same_session_compares_trading_dates() -> None:
    # is_same_session is about the trading DAY, not whether both are inside the
    # session window: any two instants on the same date belong to one session.
    assert T.is_same_session(at(MONDAY, 9, 15), at(MONDAY, 15, 29))
    assert T.is_same_session(at(MONDAY, 15, 30), at(MONDAY, 9, 16))
    assert not T.is_same_session(at(MONDAY, 15, 29), at(TUESDAY, 9, 16))


# ── epoch helpers ─────────────────────────────────────────────────────────


def test_to_epoch_ms_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        T.to_epoch_ms(datetime(2026, 10, 12, 9, 15))  # naive


def test_epoch_ms_round_trips_through_ist_and_utc() -> None:
    moment = at(MONDAY, 9, 15)
    ms = T.to_epoch_ms(moment)
    assert T.from_epoch_ms(ms) == moment.astimezone(T.ZoneInfo("UTC"))
    # to_ist attaches UTC to a naive input and returns an IST-aware value.
    naive = datetime(2026, 10, 12, 3, 45)
    assert T.to_ist(naive) == moment


# ── trading-day rules ─────────────────────────────────────────────────────


def test_is_trading_day_weekday_but_not_holiday() -> None:
    assert CALENDAR.is_trading_day(MONDAY) is True
    assert CALENDAR.is_trading_day(SATURDAY) is False
    assert CALENDAR.is_trading_day(SUNDAY) is False
    assert CALENDAR.is_trading_day(HOLIDAY) is False, "a holiday must never be a trading day"


def test_session_returns_window_only_on_trading_days() -> None:
    window = CALENDAR.session(MONDAY)
    assert window is not None
    assert window.open_at == at(MONDAY, 9, 15)
    assert window.close_at == at(MONDAY, 15, 30)
    assert window.duration_minutes == 375
    assert CALENDAR.session(HOLIDAY) is None
    assert CALENDAR.session(SATURDAY) is None


def test_candle_index_is_one_based_within_the_session() -> None:
    window = CALENDAR.session(MONDAY)
    assert window is not None
    assert window.candle_index(at(MONDAY, 9, 15)) == 1
    assert window.candle_index(at(MONDAY, 9, 16)) == 2
    assert window.candle_index(at(MONDAY, 15, 29)) == 375
    # Before the open and at/after the close are rejected (index 0).
    assert window.candle_index(at(MONDAY, 9, 0)) == 0
    assert window.candle_index(at(MONDAY, 15, 30)) == 0


# ── phase_at ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (8, 30, T.SessionPhase.CLOSED),
        (9, 0, T.SessionPhase.PRE_OPEN),
        (9, 14, T.SessionPhase.PRE_OPEN),
        (9, 15, T.SessionPhase.ACTIVE),
        (12, 0, T.SessionPhase.ACTIVE),
        (15, 29, T.SessionPhase.ACTIVE),
        (15, 30, T.SessionPhase.POST_CLOSE),
        (15, 59, T.SessionPhase.POST_CLOSE),
        (16, 0, T.SessionPhase.CLOSED),
    ],
)
def test_phase_at_distinguishes_every_window(hour: int, minute: int, expected: str) -> None:
    assert CALENDAR.phase_at(at(MONDAY, hour, minute)) == expected


def test_phase_at_on_weekend_and_holiday() -> None:
    assert CALENDAR.phase_at(at(SATURDAY, 10, 0)) == T.SessionPhase.CLOSED
    assert CALENDAR.phase_at(at(HOLIDAY, 10, 0)) == T.SessionPhase.HOLIDAY


# ── current_session / minutes_until_open ──────────────────────────────────


def test_current_session_and_minutes_until_open() -> None:
    assert CALENDAR.current_session(at(MONDAY, 10, 0)) is not None
    assert CALENDAR.current_session(at(MONDAY, 16, 1)) is None, "past post-close"
    assert CALENDAR.current_session(at(HOLIDAY, 10, 0)) is None
    assert CALENDAR.minutes_until_open(at(MONDAY, 9, 0)) == pytest.approx(15.0)
    assert CALENDAR.minutes_until_open(at(MONDAY, 9, 20)) == pytest.approx(0.0)
    assert CALENDAR.minutes_until_open(at(HOLIDAY, 10, 0)) is None


# ── adjacent trading days ─────────────────────────────────────────────────


def test_next_and_previous_trading_day_skip_weekends() -> None:
    assert CALENDAR.next_trading_day(date(2026, 10, 16)) == date(2026, 10, 19)  # Fri -> next Mon
    assert CALENDAR.previous_trading_day(MONDAY) == date(2026, 10, 9)  # Mon -> previous Fri
    assert CALENDAR.next_trading_day(HOLIDAY) != HOLIDAY


# ── days-page overrides (the /days document) ──────────────────────────────


def test_overrides_beat_the_weekday_and_holiday_rules() -> None:
    calendar = build_calendar((HOLIDAY,))
    calendar.off_days.add(MONDAY)  # operator closes a plain weekday
    calendar.on_days.add(SATURDAY)  # operator opens a special-session Saturday
    assert calendar.is_trading_day(MONDAY) is False
    assert calendar.is_trading_day(SATURDAY) is True
    assert calendar.is_trading_day(TUESDAY) is True, "untouched days keep the plain rule"
    calendar.on_days.add(HOLIDAY)
    assert calendar.is_trading_day(HOLIDAY) is True, "on_days outranks everything"


def test_parse_days_reads_both_lists_in_either_shape() -> None:
    off, on = T.parse_days(
        {
            "version": 1,
            "off_days": [{"date": "2026-10-26", "note": "Diwali"}, "2026-01-01"],
            "on_days": [{"date": "2026-11-14", "note": ""}],
        }
    )
    assert off == {date(2026, 10, 26), date(2026, 1, 1)}
    assert on == {date(2026, 11, 14)}


def test_parse_days_missing_or_blank_fields_are_empty_not_errors() -> None:
    assert T.parse_days({}) == (set(), set())
    off, on = T.parse_days({"off_days": [None, {"date": ""}], "on_days": []})
    assert (off, on) == (set(), set())


@pytest.mark.parametrize(
    "document",
    [
        "not a document",
        {"off_days": "closed"},
        {"on_days": [{"date": "next tuesday"}]},
    ],
    ids=["not-an-object", "field-not-a-list", "unreadable-date"],
)
def test_parse_days_rejects_malformed_documents(document) -> None:
    with pytest.raises(ValueError):
        T.parse_days(document)


# ── stamp parsing ─────────────────────────────────────────────────────────


def test_parse_ist_stamp_treats_offsetless_string_as_ist_not_utc() -> None:
    parsed = T.parse_ist_stamp("2026-10-12 09:15:00")
    assert parsed.utcoffset() == timedelta(hours=5, minutes=30)
    assert parsed == at(MONDAY, 9, 15)


def test_parse_ist_stamp_honours_explicit_offset() -> None:
    parsed = T.parse_ist_stamp("2026-10-12T09:15:00+05:30")
    assert parsed == at(MONDAY, 9, 15)


def test_angel_rest_stamp_is_minute_precision_ist() -> None:
    assert T.angel_rest_stamp(at(MONDAY, 9, 15, 40)) == "2026-10-12 09:15"


# ── loader degradation ────────────────────────────────────────────────────


def test_load_calendar_degrades_loudly_when_file_missing(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.json"
    calendar = T.load_calendar(missing, today=MONDAY)
    assert calendar.loaded_from_file is False
    assert calendar.stale is True
    assert calendar.calendar_status()["warning"] is not None
    assert calendar.open_time == time(9, 15) and calendar.close_time == time(15, 30)


def test_load_calendar_reads_holidays_and_session_times(tmp_path) -> None:
    path = tmp_path / "nse_holidays.json"
    path.write_text(
        '{"holidays": [{"date": "2026-11-09", "reason": "Republic"}],'
        ' "regular_session": {"open": "09:15", "close": "15:30"},'
        ' "retrieved_at": "2026-10-11"}'
    )
    calendar = T.load_calendar(path, today=MONDAY)
    assert calendar.loaded_from_file is True
    assert calendar.stale is False
    assert calendar.is_trading_day(HOLIDAY) is False
    assert calendar.holiday_reasons[HOLIDAY.isoformat()] == "Republic"

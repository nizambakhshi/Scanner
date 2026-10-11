"""Shared fixtures for the worker test suite.

All tests use a fixed IST base time so candle boundaries, session rollovers and
cooldown windows are deterministic; nothing here reads the wall clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from worker.config import ScannerParams, WorkerSettings
from worker.models import Candle, DataQuality, Tick
from worker.timeutils import ist_zoneinfo

BASE_DAY = date(2026, 10, 12)  # a Monday
SESSION_OPEN = datetime(2026, 10, 12, 9, 15, tzinfo=ist_zoneinfo())
SESSION_CLOSE = datetime(2026, 10, 12, 15, 30, tzinfo=ist_zoneinfo())


@pytest.fixture()
def params() -> ScannerParams:
    return ScannerParams()


@pytest.fixture()
def settings(params: ScannerParams) -> WorkerSettings:
    return WorkerSettings(
        scan_mode="simulation",
        simulation_label=True,
        timezone="Asia/Kolkata",
        app_url="http://localhost:3000",
        scanner=params,
    )


def minute(offset: int, *, second: int = 0) -> datetime:
    """Candle-start helper: ``minute(0)`` is the 09:15 bucket."""
    return SESSION_OPEN + timedelta(minutes=offset, seconds=second)


def tick(
    instrument_id: int = 1,
    *,
    price: float,
    offset_minutes: int,
    second: int = 0,
    cumulative_volume: int | None = None,
    last_traded_quantity: int | None = None,
    token: str = "2885",
) -> Tick:
    stamp = minute(offset_minutes, second=second)
    return Tick(
        instrument_id=instrument_id,
        instrument_token=token,
        exchange="NSE",
        exchange_timestamp=stamp,
        received_at=stamp + timedelta(seconds=1),
        last_traded_price=price,
        last_traded_quantity=last_traded_quantity,
        cumulative_volume=cumulative_volume,
    )


def candle(
    *,
    close: float,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
    volume: int = 1000,
    offset_minutes: int = 0,
    instrument_id: int = 1,
    is_complete: bool = True,
    data_quality: DataQuality = DataQuality.OK,
) -> Candle:
    start = minute(offset_minutes)
    return Candle(
        instrument_id=instrument_id,
        trading_date=start.date(),
        candle_start=start,
        open=open_ if open_ is not None else close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
        is_complete=is_complete,
        data_quality=data_quality,
    )


@pytest.fixture()
def make_tick():
    return tick


@pytest.fixture()
def make_candle():
    return candle


@pytest.fixture()
def session_open() -> datetime:
    return SESSION_OPEN


@pytest.fixture()
def session_close() -> datetime:
    return SESSION_CLOSE

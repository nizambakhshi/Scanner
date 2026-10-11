"""Conformance suite: InMemoryStore and PostgresStore must behave identically.

The same behavioural assertions run against :class:`InMemoryStore` always and
against :class:`PostgresStore` only when ``TEST_DATABASE_URL`` points at a
disposable database. Without that env var every Postgres case is skipped with
``pytest.mark.integration`` + ``skipif`` (reason below).

To actually run the Postgres side::

    # from the repo root: apply supabase/migrations first
    python -m worker.scripts.migrate --database-url "$TEST_DATABASE_URL" \
        --dir ../supabase/migrations
    TEST_DATABASE_URL="$TEST_DATABASE_URL" \
        .venv/bin/python -m pytest tests/integration/test_store_conformance.py

Every test is deterministic: all datetimes are derived from the fixed IST base
``2026-10-12 09:15`` (a Monday) via :mod:`worker.timeutils`, never the wall
clock.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from worker.config import ScannerParams
from worker.models import (
    Candle,
    Channel,
    DataQuality,
    DeliveryStatus,
    IndicatorSnapshot,
    Instrument,
    NotificationDelivery,
    Signal,
    SignalType,
    WatchlistState,
)
from worker.persistence.memory import InMemoryStore
from worker.persistence.postgres import PostgresStore
from worker.timeutils import ist_zoneinfo

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
_SKIP_REASON = (
    "PostgresStore conformance requires TEST_DATABASE_URL pointing at a disposable "
    "database with supabase/migrations applied (python -m worker.scripts.migrate); "
    "no local PostgreSQL is available by default"
)

IST = ist_zoneinfo()
BASE_DATE = date(2026, 10, 12)  # a Monday
BASE = datetime.combine(BASE_DATE, datetime.min.time().replace(hour=9, minute=15), tzinfo=IST)


def minute(offset: int) -> datetime:
    """Session-relative, IST-aware timestamp derived from the fixed base."""
    return BASE + timedelta(minutes=offset)


STORE_PARAMS = [
    pytest.param("memory", id="memory"),
    pytest.param(
        "postgres",
        id="postgres",
        marks=[pytest.mark.integration, pytest.mark.skipif(not TEST_DATABASE_URL, reason=_SKIP_REASON)],
    ),
]


@pytest.fixture(params=STORE_PARAMS)
async def store(request: Any) -> Any:
    if request.param == "postgres":
        postgres_store = PostgresStore(TEST_DATABASE_URL)
        await postgres_store.connect()
        # Each test starts from an empty schema; sequences restart, so the
        # ids both stores hand out stay comparable across implementations.
        await postgres_store.truncate_all(allow=True)
        try:
            yield postgres_store
        finally:
            await postgres_store.close()
    else:
        memory_store = InMemoryStore()
        await memory_store.connect()
        yield memory_store


async def seed_instrument(store: Any, *, symbol: str = "CONFTEST", token: str = "999999") -> int:
    await store.upsert_instruments(
        [
            Instrument(
                id=0,
                exchange="NSE",
                symbol=symbol,
                trading_symbol=symbol,
                company_name="Conformance Test Ltd",
                instrument_token=token,
            )
        ]
    )
    rows = await store.list_universe()
    matches = [row for row in rows if row.symbol == symbol]
    assert len(matches) == 1
    return matches[0].id


@pytest.fixture()
async def instrument_id(store: Any) -> int:
    return await seed_instrument(store)


@pytest.fixture()
async def session_id(store: Any) -> int:
    session = await store.ensure_session(BASE_DATE, minute(0), minute(375))
    return session.id


def make_candle(iid: int, offset: int, close: float, *, volume: int = 1000) -> Candle:
    return Candle(
        instrument_id=iid,
        trading_date=BASE_DATE,
        candle_start=minute(offset),
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=volume,
        is_complete=True,
        data_quality=DataQuality.OK,
    )


def make_snapshot(iid: int, offset: int) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        instrument_id=iid,
        trading_date=BASE_DATE,
        candle_start=minute(offset),
        vwap=100.0,
        ema_fast=99.5,
        ema_slow=98.25,
        average_volume=1200.0,
        relative_volume=1.25,
        intraday_change_pct=0.85,
        distance_from_vwap_pct=0.30,
        ema_spread_pct=1.10,
        close=100.5,
        previous_close=99.0,
        session_high=101.0,
        session_low=98.5,
        distance_from_high_pct=0.50,
        bullish_qualified=True,
        reasons=("above_vwap", "volume_spike"),
        failed_reasons=(),
        data_quality=DataQuality.OK,
        warming_up=False,
    )


def make_signal(iid: int, sid: int, key: str) -> Signal:
    return Signal(
        instrument_id=iid,
        trading_session_id=sid,
        signal_type=SignalType.BULLISH_ENTRY,
        qualifying_episode_id=f"episode-{key}",
        idempotency_key=key,
        event_timestamp=minute(10),
        candle_start=minute(9),
        price=100.5,
        vwap=100.0,
        ema_fast=99.5,
        ema_slow=98.25,
        relative_volume=1.25,
        intraday_change_pct=0.85,
        score=0.72,
        reasons=("above_vwap",),
        message="conf signal",
    )


# ── candles + indicators ──────────────────────────────────────────────────────


async def test_candle_upsert_is_idempotent_per_minute(store: Any, instrument_id: int) -> None:
    first = make_candle(instrument_id, 1, close=100.0, volume=1000)
    assert await store.upsert_candles([first]) == 1

    replayed = make_candle(instrument_id, 1, close=101.0, volume=1500)
    assert await store.upsert_candles([replayed]) == 1

    loaded = await store.load_completed_candles(
        [instrument_id], until=minute(60), limit_per_instrument=10
    )
    rows = loaded[instrument_id]
    assert len(rows) == 1, "same (instrument, minute) must be one row, not two"
    assert rows[0].close == pytest.approx(101.0), "the replay must update the values"
    assert rows[0].volume == 1500


async def test_load_completed_candles_sorts_ascending_and_limits(
    store: Any, instrument_id: int
) -> None:
    offsets = [1, 2, 3, 4]
    await store.upsert_candles([make_candle(instrument_id, off, close=100.0 + off) for off in offsets])
    loaded = await store.load_completed_candles(
        [instrument_id], until=minute(60), limit_per_instrument=2
    )
    rows = loaded[instrument_id]
    assert [row.candle_start for row in rows] == [minute(3), minute(4)]


async def test_indicator_snapshot_upsert_is_idempotent(store: Any, instrument_id: int) -> None:
    snapshot = make_snapshot(instrument_id, 1)
    first_id = await store.upsert_indicator_snapshot(snapshot)
    second_id = await store.upsert_indicator_snapshot(snapshot)
    assert first_id == second_id
    other = await store.upsert_indicator_snapshot(make_snapshot(instrument_id, 2))
    assert other != first_id


# ── signals + notifications ───────────────────────────────────────────────────


async def test_signal_idempotency_returns_existing_id(store: Any, instrument_id: int, session_id: int) -> None:
    result = await store.insert_signal(make_signal(instrument_id, session_id, "conf:entry:1"))
    assert result.created is True

    duplicate = await store.insert_signal(make_signal(instrument_id, session_id, "conf:entry:1"))
    assert duplicate.created is False
    assert duplicate.signal_id == result.signal_id


async def test_enqueue_unique_per_signal_channel(store: Any, instrument_id: int, session_id: int) -> None:
    signal = await store.insert_signal(make_signal(instrument_id, session_id, "conf:entry:2"))

    def delivery(channel: Channel) -> NotificationDelivery:
        return NotificationDelivery(
            id=None,
            signal_id=signal.signal_id,
            channel=channel,
            destination_reference="@conformance",
            delivery_status=DeliveryStatus.QUEUED,
        )

    telegram_first = await store.enqueue_notification(delivery(Channel.TELEGRAM))
    telegram_second = await store.enqueue_notification(delivery(Channel.TELEGRAM))
    assert telegram_first == telegram_second, "(signal, channel) must not queue twice"

    dashboard = await store.enqueue_notification(delivery(Channel.DASHBOARD))
    assert dashboard != telegram_first


async def test_claim_marks_in_flight_and_is_not_returned_again(store: Any, instrument_id: int, session_id: int) -> None:
    signal = await store.insert_signal(make_signal(instrument_id, session_id, "conf:entry:3"))
    await store.enqueue_notification(
        NotificationDelivery(
            id=None,
            signal_id=signal.signal_id,
            channel=Channel.TELEGRAM,
            destination_reference="@conformance",
            delivery_status=DeliveryStatus.QUEUED,
        )
    )

    claims = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(11), limit=10)
    assert len(claims) == 1
    assert claims[0].delivery.delivery_status == DeliveryStatus.IN_FLIGHT

    again = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(12), limit=10)
    assert again == [], "an IN_FLIGHT delivery must never be double-dispatched"


# ── leases ────────────────────────────────────────────────────────────────────


async def test_lease_blocks_other_holder_until_expiry(store: Any) -> None:
    assert await store.acquire_lease("session-main", "worker-A", 60, minute(0)) is True
    assert await store.acquire_lease("session-main", "worker-B", 60, minute(0)) is False
    assert await store.renew_lease("session-main", "worker-A", 60, minute(0)) is True
    assert await store.renew_lease("session-main", "worker-B", 60, minute(0)) is False

    # A stops heartbeating; B may take over one second after expiry.
    assert await store.acquire_lease("session-main", "worker-B", 60, minute(2)) is True
    assert await store.renew_lease("session-main", "worker-A", 60, minute(2)) is False


# ── sessions + config ─────────────────────────────────────────────────────────


async def test_ensure_session_is_idempotent_per_date(store: Any) -> None:
    first = await store.ensure_session(BASE_DATE, minute(0), minute(375))
    second = await store.ensure_session(BASE_DATE, minute(0), minute(375))
    assert first.id == second.id
    assert second.trading_date == BASE_DATE


async def test_add_stale_seconds_accumulates(store: Any) -> None:
    session = await store.ensure_session(BASE_DATE, minute(0), minute(375))
    await store.add_stale_seconds(session.id, 30)
    await store.add_stale_seconds(session.id, 30)
    row = await store.get_session_for_date(BASE_DATE)
    assert row is not None
    assert row.stale_data_seconds == 60


async def test_save_config_increments_version(store: Any) -> None:
    name = "conformance"
    first = await store.save_config(ScannerParams(), name=name, created_by="test")
    assert first.config_version == 1

    second = await store.save_config(
        ScannerParams(fast_ema_period=5, slow_ema_period=21), name=name, created_by="test"
    )
    assert second.config_version == 2

    active = await store.active_config(name=name)
    assert active is not None
    assert active.config_version == 2
    assert active.params.fast_ema_period == 5
    assert active.params.slow_ema_period == 21


# ── instruments + watchlist ───────────────────────────────────────────────────


async def test_set_previous_closes(store: Any, instrument_id: int) -> None:
    updated = await store.set_previous_closes({instrument_id: 234.56})
    assert updated == 1
    item = await store.get_instrument(instrument_id)
    assert item is not None
    assert item.previous_close == pytest.approx(234.56)


async def test_freeze_session_watchlist_deactivates_memberships(
    store: Any, instrument_id: int, session_id: int
) -> None:
    membership = await store.create_membership(
        session_id,
        instrument_id,
        WatchlistState.ACTIVE,
        entered_at=minute(10),
        price=100.0,
        score=0.80,
        snapshot_id=None,
        is_simulated=True,
    )
    assert membership.is_active is True

    assert await store.freeze_session_watchlist(session_id) == 1
    rows = await store.list_memberships(session_id)
    assert len(rows) == 1
    assert rows[0].is_active is False, "history is preserved but nothing stays active"
    assert await store.freeze_session_watchlist(session_id) == 0

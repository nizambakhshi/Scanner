"""Domain-model contract tests and InMemoryStore behavioural gaps.

Two related concerns live here:

1. ``worker.models`` — the dataclasses reject naive datetimes (``ensure_aware``),
   the enums are ``StrEnum``, and ``Candle`` exposes ``typical_price`` /
   ``candle_end`` / ``to_row`` with 4-decimal rounding and an integer volume.
2. ``worker.persistence.memory.InMemoryStore`` — the edges of its contract that
   the shared ``tests/integration/test_store_conformance.py`` suite does NOT
   already pin down (it asserts the happy paths against both stores; the tests
   below probe the incomplete-row filter, ``until`` inclusivity, per-name config
   chains, the strict lease-expiry boundary, retry scheduling and the negative
   stale-seconds clamp). Every store test is ``async`` (pytest-asyncio auto
   mode) and derives all timestamps from a fixed IST base — never the wall clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from worker.config import ScannerParams
from worker.models import (
    Candle,
    Channel,
    DataQuality,
    DeliveryStatus,
    FeedStatus,
    IndicatorSnapshot,
    Instrument,
    MappingStatus,
    NotificationDelivery,
    Signal,
    SignalType,
    WatchlistState,
    ensure_aware,
)
from worker.persistence.memory import InMemoryStore
from worker.timeutils import ist_zoneinfo

IST = ist_zoneinfo()
MONDAY = date(2026, 10, 12)
TUESDAY = date(2026, 10, 13)
BASE = datetime.combine(MONDAY, datetime.min.time().replace(hour=9, minute=15), tzinfo=IST)


def minute(offset: int) -> datetime:
    return BASE + timedelta(minutes=offset)


def make_candle(
    iid: int = 1,
    offset: int = 1,
    close: float = 100.0,
    *,
    complete: bool = True,
    trading_date: date = MONDAY,
) -> Candle:
    return Candle(
        instrument_id=iid,
        trading_date=trading_date,
        candle_start=minute(offset),
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=1000,
        is_complete=complete,
        data_quality=DataQuality.OK if complete else DataQuality.INCOMPLETE,
    )


def make_snapshot(iid: int = 1, offset: int = 1, close: float = 100.0) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        instrument_id=iid,
        trading_date=MONDAY,
        candle_start=minute(offset),
        vwap=100.0,
        ema_fast=99.5,
        ema_slow=98.0,
        average_volume=1200.0,
        relative_volume=1.25,
        intraday_change_pct=0.85,
        distance_from_vwap_pct=0.30,
        ema_spread_pct=1.10,
        close=close,
        previous_close=99.0,
        session_high=101.0,
        session_low=98.5,
        distance_from_high_pct=0.50,
        bullish_qualified=True,
        reasons=("above_vwap",),
        failed_reasons=(),
        data_quality=DataQuality.OK,
        warming_up=False,
    )


def make_signal(key: str) -> Signal:
    return Signal(
        instrument_id=1,
        trading_session_id=1,
        signal_type=SignalType.BULLISH_ENTRY,
        qualifying_episode_id=f"episode-{key}",
        idempotency_key=key,
        event_timestamp=minute(10),
    )


def make_delivery(
    signal_id: int,
    *,
    channel: Channel = Channel.TELEGRAM,
    status: DeliveryStatus = DeliveryStatus.QUEUED,
    next_retry_at: datetime | None = None,
) -> NotificationDelivery:
    return NotificationDelivery(
        id=None,
        signal_id=signal_id,
        channel=channel,
        destination_reference="@unit",
        delivery_status=status,
        next_retry_at=next_retry_at,
    )


# ══════════════════════════════════════════════════════════════════════════
# models.py
# ══════════════════════════════════════════════════════════════════════════


def test_ensure_aware_accepts_aware_and_rejects_naive() -> None:
    aware = minute(0)
    assert ensure_aware(aware) is aware
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_aware(datetime(2026, 10, 12, 9, 15))  # naive


def test_candle_rejects_naive_candle_start() -> None:
    with pytest.raises(ValueError, match="Candle.candle_start"):
        Candle(
            instrument_id=1,
            trading_date=MONDAY,
            candle_start=datetime(2026, 10, 12, 9, 15),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=0,
        )


def test_tick_requires_aware_timestamps_and_positive_price() -> None:
    with pytest.raises(ValueError, match="Tick.exchange_timestamp"):
        _tick(exchange_timestamp=datetime(2026, 10, 12, 9, 15))
    with pytest.raises(ValueError, match="Tick.received_at"):
        _tick(received_at=datetime(2026, 10, 12, 9, 15))
    with pytest.raises(ValueError, match="must be positive"):
        _tick(price=-5.0)
    with pytest.raises(ValueError, match="must be positive"):
        _tick(price=0.0)
    assert _tick(price=None).last_traded_price is None  # a print may legitimately be absent


def _tick(
    *,
    exchange_timestamp: datetime | None = None,
    received_at: datetime | None = None,
    price: float | None = 100.0,
) -> Any:
    from worker.models import Tick

    return Tick(
        instrument_id=1,
        instrument_token="2885",
        exchange="NSE",
        exchange_timestamp=exchange_timestamp if exchange_timestamp is not None else minute(0),
        received_at=received_at if received_at is not None else minute(0),
        last_traded_price=price,
    )


def test_indicator_snapshot_and_signal_reject_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="IndicatorSnapshot.candle_start"):
        make_snapshot().__class__(**{**make_snapshot().__dict__, "candle_start": datetime(2026, 10, 12, 9, 15)})
    with pytest.raises(ValueError, match="Signal.event_timestamp"):
        Signal(
            instrument_id=1,
            trading_session_id=1,
            signal_type=SignalType.BULLISH_ENTRY,
            qualifying_episode_id="e",
            idempotency_key="k",
            event_timestamp=datetime(2026, 10, 12, 9, 15),
        )


def test_candle_typical_price_and_candle_end() -> None:
    candle = Candle(
        instrument_id=1,
        trading_date=MONDAY,
        candle_start=minute(0),
        open=10.0,
        high=20.0,
        low=15.0,
        close=18.0,
        volume=5,
    )
    assert candle.typical_price == pytest.approx((20.0 + 15.0 + 18.0) / 3.0)
    assert candle.candle_end == minute(0) + timedelta(minutes=1)


def test_candle_to_row_shape_rounding_and_types() -> None:
    candle = Candle(
        instrument_id=7,
        trading_date=MONDAY,
        candle_start=minute(3),
        open=10.123456,
        high=11.987654,
        low=9.000001,
        close=10.5,
        volume=4321,
        is_complete=True,
        data_quality=DataQuality.OK,
        tick_count=9,
        is_simulated=True,
    )
    row = candle.to_row()
    assert row["instrument_id"] == 7
    assert row["candle_start_at"] == minute(3)
    assert row["trading_date"] == MONDAY
    # Prices are rounded to the 4-decimal NUMERIC(16,4) column precision.
    assert row["open"] == round(10.123456, 4)
    assert row["high"] == round(11.987654, 4)
    assert row["low"] == round(9.000001, 4)
    assert row["close"] == 10.5
    assert isinstance(row["volume"], int) and row["volume"] == 4321
    assert row["data_quality_status"] == "OK"
    assert row["tick_count"] == 9 and row["is_complete"] is True and row["is_simulated"] is True
    assert "vwap_price_sum" not in row, "the DB row must not carry the internal VWAP accumulator"


def test_instrument_key_and_enum_string_semantics() -> None:
    instrument = Instrument(
        id=1,
        exchange="NSE",
        symbol="RELIANCE",
        trading_symbol="RELIANCE-EQ",
        company_name="Reliance",
        instrument_token="2885",
    )
    assert instrument.key == "NSE:2885"
    assert instrument.mapping_status is MappingStatus.MAPPED
    assert DataQuality.OK == "OK" and DataQuality("OK") is DataQuality.OK
    assert DeliveryStatus.SIMULATED == "SIMULATED" and Channel.TELEGRAM == "TELEGRAM"
    assert SignalType.BULLISH_ENTRY == "BULLISH_ENTRY"
    assert FeedStatus.STALE == "STALE" and WatchlistState.REQUALIFIED == "REQUALIFIED"
    assert {s.value for s in DataQuality} >= {"OK", "PARTIAL", "STALE", "INCOMPLETE"}


# ══════════════════════════════════════════════════════════════════════════
# persistence/memory.py — gaps the conformance suite does not pin down
# ══════════════════════════════════════════════════════════════════════════


async def test_upsert_candles_reports_input_count_but_collapses_same_minute() -> None:
    store = InMemoryStore()
    # Two writes to the same (instrument, minute) collapse to one row, yet the
    # return value mirrors how many rows were handed in (Postgres parity).
    assert await store.upsert_candles([make_candle(close=100.0), make_candle(close=101.0)]) == 2
    assert len(store.candles) == 1
    assert store.candle_rows(1)[0].close == pytest.approx(101.0)


async def test_load_completed_candles_excludes_incomplete_and_includes_until() -> None:
    store = InMemoryStore()
    await store.upsert_candles(
        [
            make_candle(offset=1, complete=True),
            make_candle(offset=2, complete=False),
            make_candle(offset=3, complete=True),
        ]
    )
    loaded = await store.load_completed_candles([1], until=minute(3), limit_per_instrument=10)
    starts = [c.candle_start for c in loaded[1]]
    assert starts == [minute(1), minute(3)], "incomplete minute 2 dropped, `until` boundary inclusive"


async def test_load_completed_candles_returns_key_for_every_requested_instrument() -> None:
    store = InMemoryStore()
    await store.upsert_candles([make_candle(iid=1, offset=1), make_candle(iid=2, offset=1)])
    loaded = await store.load_completed_candles([1, 2, 3], until=minute(60), limit_per_instrument=10)
    assert set(loaded) == {1, 2, 3}
    assert loaded[1] and loaded[2]
    assert loaded[3] == [], "an instrument with no candles still gets an (empty) key"


async def test_latest_completed_candle_at() -> None:
    store = InMemoryStore()
    assert await store.latest_completed_candle_at(1) is None
    await store.upsert_candles([make_candle(offset=2), make_candle(offset=5), make_candle(offset=3, complete=False)])
    assert await store.latest_completed_candle_at(1) == minute(5)


async def test_count_candles_for_session_only_complete_for_that_date() -> None:
    store = InMemoryStore()
    session = await store.ensure_session(MONDAY, minute(0), minute(375))
    await store.upsert_candles(
        [
            make_candle(iid=1, offset=1, complete=True),
            make_candle(iid=1, offset=2, complete=True),
            make_candle(iid=1, offset=3, complete=False),
            make_candle(iid=2, offset=5, complete=True, trading_date=TUESDAY),
        ]
    )
    assert await store.count_candles_for_session(session.id) == 2


async def test_snapshot_reupsert_keeps_id_but_replaces_content() -> None:
    store = InMemoryStore()
    first = await store.upsert_indicator_snapshot(make_snapshot(offset=1, close=100.0))
    second = await store.upsert_indicator_snapshot(make_snapshot(offset=1, close=999.0))
    assert first == second
    assert store.snapshots[(1, minute(1))].close == pytest.approx(999.0)


async def test_snapshot_uniqueness_is_per_instrument_not_per_minute() -> None:
    store = InMemoryStore()
    a = await store.upsert_indicator_snapshot(make_snapshot(iid=1, offset=1))
    b = await store.upsert_indicator_snapshot(make_snapshot(iid=2, offset=1))
    assert a != b, "same candle minute but different instruments are distinct rows"


async def test_active_config_none_then_independent_version_chains() -> None:
    store = InMemoryStore()
    assert await store.active_config(name="missing") is None
    alpha1 = await store.save_config(ScannerParams(), name="alpha", created_by="t")
    beta1 = await store.save_config(ScannerParams(), name="beta", created_by="t")
    alpha2 = await store.save_config(ScannerParams(fast_ema_period=5, slow_ema_period=21), name="alpha", created_by="t")
    assert (alpha1.config_version, alpha2.config_version) == (1, 2)
    assert beta1.config_version == 1, "different names version independently"
    active = await store.active_config(name="alpha")
    assert active is not None and active.config_version == 2
    assert active.params.fast_ema_period == 5


async def test_save_config_stamps_row_version_into_params() -> None:
    store = InMemoryStore()
    row = await store.save_config(ScannerParams(), name="cfg", created_by="t")
    assert row.params.config_version == row.config_version


async def test_lease_allows_same_holder_and_rejects_renew_for_unknown_lease() -> None:
    store = InMemoryStore()
    assert await store.acquire_lease("L", "A", 60, minute(0)) is True
    # The holder is never blocked by its own unexpired lease.
    assert await store.acquire_lease("L", "A", 60, minute(0)) is True
    assert await store.renew_lease("never-acquired", "A", 60, minute(0)) is False


async def test_lease_renew_extends_takeover_boundary() -> None:
    store = InMemoryStore()
    await store.acquire_lease("L", "A", 60, minute(0))  # expires at minute(1)
    assert await store.renew_lease("L", "A", 120, minute(0)) is True  # now expires at minute(2)
    assert await store.acquire_lease("L", "B", 60, minute(1)) is False, "renew pushed the expiry out"
    assert await store.acquire_lease("L", "B", 60, minute(2)) is True, "takeover allowed at exact expiry"


async def test_lease_expiry_boundary_is_exclusive() -> None:
    store = InMemoryStore()
    await store.acquire_lease("L", "A", 60, minute(0))  # held until minute(1)
    assert await store.acquire_lease("L", "B", 60, minute(0)) is False
    # The guard is `expiry > now`: at the exact expiry instant B can take over.
    assert await store.acquire_lease("L", "B", 60, minute(1)) is True


async def test_claim_respects_next_retry_schedule() -> None:
    store = InMemoryStore()
    signal = await store.insert_signal(make_signal("sched"))
    await store.enqueue_notification(make_delivery(signal.signal_id, next_retry_at=minute(20)))
    assert await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(10)) == []
    claims = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(25))
    assert len(claims) == 1
    assert claims[0].delivery.delivery_status == DeliveryStatus.IN_FLIGHT


async def test_claim_honours_limit_and_only_returns_unclaimed() -> None:
    store = InMemoryStore()
    signal = await store.insert_signal(make_signal("limit"))
    await store.enqueue_notification(make_delivery(signal.signal_id))
    signal2 = await store.insert_signal(make_signal("limit2"))
    await store.enqueue_notification(make_delivery(signal2.signal_id))
    first = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(10), limit=1)
    assert len(first) == 1
    assert await store.pending_notification_count() == 2, "the IN_FLIGHT one still counts as pending"
    second = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(10), limit=1)
    assert len(second) == 1 and second[0].delivery.id != first[0].delivery.id
    assert await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(10), limit=1) == []


async def test_failed_delivery_is_reclaimable_after_update() -> None:
    store = InMemoryStore()
    signal = await store.insert_signal(make_signal("retry"))
    delivery_id = await store.enqueue_notification(make_delivery(signal.signal_id))
    claims = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(10))
    assert [c.delivery.id for c in claims] == [delivery_id]
    # A failed send releases the claim so the row can be dispatched again.
    await store.update_delivery(
        delivery_id, status=DeliveryStatus.FAILED, attempt_count=1, last_attempt_at=minute(10), next_retry_at=minute(10)
    )
    again = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(11))
    assert len(again) == 1
    assert again[0].delivery.delivery_status == DeliveryStatus.IN_FLIGHT


async def test_delivered_notifications_are_no_longer_pending() -> None:
    store = InMemoryStore()
    signal = await store.insert_signal(make_signal("done"))
    delivery_id = await store.enqueue_notification(make_delivery(signal.signal_id))
    await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(10))
    await store.update_delivery(
        delivery_id,
        status=DeliveryStatus.DELIVERED,
        attempt_count=1,
        last_attempt_at=minute(10),
        delivered_at=minute(10),
    )
    assert await store.pending_notification_count() == 0
    assert store.deliveries[delivery_id].delivered_at == minute(10)


async def test_add_stale_seconds_ignores_negative_increments() -> None:
    store = InMemoryStore()
    session = await store.ensure_session(MONDAY, minute(0), minute(375))
    await store.add_stale_seconds(session.id, 45)
    await store.add_stale_seconds(session.id, -100)
    row = await store.get_session_for_date(MONDAY)
    assert row is not None
    assert row.stale_data_seconds == 45, "negative contributions are clamped to zero"

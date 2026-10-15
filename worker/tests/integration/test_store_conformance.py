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
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from worker.config import ScannerParams
from worker.models import (
    Candle,
    Channel,
    DailyCandle,
    DataQuality,
    DeliveryStatus,
    DiscoverySnapshotRow,
    IndicatorSnapshot,
    Instrument,
    NotificationDelivery,
    Signal,
    SignalType,
    StockProfile,
    StockTechnicals,
    WatchlistEvent,
    WatchlistState,
    WeeklyCandle,
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


async def test_the_intraday_rsi_reading_round_trips_on_its_own_candle(
    store: Any, instrument_id: int
) -> None:
    """``rsi_5m`` / ``rsi_state`` are the two newest columns on this table, and the
    insert lists thirty placeholders by hand. Reading the stored row back is the only
    way to prove each value landed in its own column instead of a neighbour's — a
    swapped pair would show the dashboard an RSI where it expects a VWAP and nothing
    else would ever look wrong again."""
    await store.upsert_indicator_snapshot(
        replace(make_snapshot(instrument_id, 1), rsi_5m=37.25, rsi_state="ENTRY")
    )
    row = await read_snapshot_row(store, instrument_id, 1)
    assert row["rsi_5m"] == pytest.approx(37.25)
    assert row["rsi_state"] == "ENTRY"
    # The columns it sits between keep their own values.
    assert row["distance_from_high_pct"] == pytest.approx(0.50)
    assert row["bullish_qualified"] is True
    assert row["vwap"] == pytest.approx(100.0)

    # A bucket the detector cannot judge yet stores NULL, not a word invented for it:
    # the dashboard's em dash and the worker's "not evaluable" are the same absence.
    await store.upsert_indicator_snapshot(make_snapshot(instrument_id, 2))
    blank = await read_snapshot_row(store, instrument_id, 2)
    assert blank["rsi_5m"] is None
    assert blank["rsi_state"] is None


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


# ── daily/weekly bars + stock details (two-timeframe RSI) ─────────────────────


def make_daily_candle(iid: int, day: date, close: float) -> DailyCandle:
    return DailyCandle(
        instrument_id=iid,
        trading_date=day,
        open=close - 1.0,
        high=close + 1.5,
        low=close - 2.0,
        close=close,
        volume=5000,
        rsi14=61.25,
    )


def make_weekly_candle(iid: int, week: date, close: float) -> WeeklyCandle:
    return WeeklyCandle(
        instrument_id=iid,
        week_start=week,
        open=close - 5.0,
        high=close + 6.0,
        low=close - 7.0,
        close=close,
        volume=25000,
        rsi14=58.5,
    )


async def read_stock_details(store: Any, instrument_id: int) -> dict[str, Any]:
    """The raw ``stock_details`` row, read through the concrete store.

    The ScannerStore read surface deliberately exposes only what the worker
    consumes (``list_rsi_states``); these tests reach the row itself to prove
    the two writers own *disjoint* column sets and the COALESCE rules hold.
    NUMERIC columns come back as ``Decimal`` from psycopg; converted to float
    here so both stores present the same types (the store's own mappers use
    ``_f`` for exactly this reason).
    """
    if isinstance(store, InMemoryStore):
        return dict(store.stock_details.get(instrument_id) or {})
    sql = "SELECT * FROM stock_details WHERE instrument_id = %s"
    async with store._pool.connection() as conn, conn.transaction():
        row = await (await conn.execute(sql, (instrument_id,))).fetchone()
    if row is None:
        return {}
    return {key: _plain(value) for key, value in dict(row).items()}


def _plain(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value


async def read_snapshot_row(store: Any, instrument_id: int, offset: int) -> dict[str, Any]:
    """The raw ``indicator_snapshots`` row for one (instrument, minute).

    Same reason as :func:`read_stock_details`: the store surface exposes no snapshot
    read at all (the worker never queries its own history), so a test that cares
    about *which column a value went into* has to reach past the API. The memory
    store holds the dataclass it was given, so it is flattened to the same dict shape
    and both stores answer with floats rather than ``Decimal``.
    """
    if isinstance(store, InMemoryStore):
        return {key: _plain(value) for key, value in asdict(store.snapshots[(instrument_id, minute(offset))]).items()}
    sql = "SELECT * FROM indicator_snapshots WHERE instrument_id = %s AND candle_start_at = %s"
    async with store._pool.connection() as conn, conn.transaction():
        row = await (await conn.execute(sql, (instrument_id, minute(offset)))).fetchone()
    assert row is not None, "the snapshot was written but cannot be read back"
    return {key: _plain(value) for key, value in dict(row).items()}


async def test_daily_candle_upsert_is_idempotent_per_date(store: Any, instrument_id: int) -> None:
    first = make_daily_candle(instrument_id, date(2026, 10, 8), close=100.0)
    second = make_daily_candle(instrument_id, date(2026, 10, 9), close=101.0)
    assert await store.upsert_daily_candles([second, first]) == 2

    replayed = replace(first, close=100.5, rsi14=62.0)
    assert await store.upsert_daily_candles([replayed]) == 1

    rows = await store.load_daily_candles(instrument_id)
    assert [row.trading_date for row in rows] == [date(2026, 10, 8), date(2026, 10, 9)]
    assert rows[0].close == pytest.approx(100.5), "the replay must update the values"
    assert rows[0].rsi14 == pytest.approx(62.0)
    assert rows[0].source == "angel-one-day"
    assert rows[1].rsi14 == pytest.approx(61.25)


async def test_weekly_candle_upsert_is_idempotent_per_week(store: Any, instrument_id: int) -> None:
    newer = make_weekly_candle(instrument_id, date(2026, 10, 5), close=110.0)
    older = make_weekly_candle(instrument_id, date(2026, 9, 28), close=105.0)
    assert await store.upsert_weekly_candles([newer, older]) == 2

    replayed = replace(older, close=106.0, rsi14=57.0)
    assert await store.upsert_weekly_candles([replayed]) == 1

    rows = await store.load_weekly_candles(instrument_id)
    assert [row.week_start for row in rows] == [date(2026, 9, 28), date(2026, 10, 5)]
    assert rows[0].close == pytest.approx(106.0)
    assert rows[0].rsi14 == pytest.approx(57.0)
    assert rows[0].source == "resampled-from-candles-1d"


async def test_stock_technicals_and_alert_bias_round_trip(store: Any, instrument_id: int) -> None:
    # The bias vocabulary is the operator's 2026-10-15 rename: both timeframes above
    # 60 is OVERBOUGHT (the expensive side, red), not the "BULLISH" a momentum
    # reading called it. The CHECK on both columns rejects the old words, so this
    # test is also the proof that the store writes the shape the database accepts.
    await store.update_stock_technicals(
        [
            StockTechnicals(
                instrument_id=instrument_id,
                daily_rsi=63.2,
                weekly_rsi=61.8,
                rsi_bias="OVERBOUGHT",
                rsi_as_of=date(2026, 10, 9),
                data_start=date(2002, 1, 1),
                high_52w=1700.0,
                low_52w=1200.0,
                ath=1700.0,
                atl=10.0,
            )
        ]
    )
    states = await store.list_rsi_states()
    assert len(states) == 1, "one row for one instrument, not one per column"
    assert states[0].instrument_id == instrument_id
    assert states[0].daily_rsi == pytest.approx(63.2)
    assert states[0].weekly_rsi == pytest.approx(61.8)
    assert states[0].rsi_bias == "OVERBOUGHT"
    assert states[0].last_alert_bias is None, "the worker baselines this, sync never does"

    await store.set_last_alert_bias(instrument_id, "OVERBOUGHT")
    states = await store.list_rsi_states()
    assert states[0].last_alert_bias == "OVERBOUGHT"

    # A nightly refresh rewrites the technicals columns; the alert bookkeeping
    # column is the worker's and must survive it, and a NULL data_start in the
    # new row must not blank the stored one (COALESCE).
    await store.update_stock_technicals(
        [
            StockTechnicals(
                instrument_id=instrument_id,
                daily_rsi=64.0,
                weekly_rsi=62.5,
                rsi_bias="OVERBOUGHT",
                rsi_as_of=date(2026, 10, 10),
                data_start=None,
                high_52w=1710.0,
                low_52w=1200.0,
                ath=1710.0,
                atl=10.0,
            )
        ]
    )
    states = await store.list_rsi_states()
    assert states[0].daily_rsi == pytest.approx(64.0)
    assert states[0].last_alert_bias == "OVERBOUGHT", "sync must not clobber the worker's column"
    assert (await read_stock_details(store, instrument_id))["data_start"] == date(2002, 1, 1)

    # Anti-drift parity: setting a bias for a row that does not exist is a
    # silent no-op (the SQL UPDATE matches zero rows), never an error.
    await store.set_last_alert_bias(instrument_id + 4242, "OVERBOUGHT")


# ── daily discovery snapshots (Stage 1 persistence, migration 0012) ──────────


def make_discovery_row(
    iid: int, day: date, *, group: str = "STRONG_BULLISH", **changes: Any
) -> DiscoverySnapshotRow:
    base = DiscoverySnapshotRow(
        trading_date=day,
        instrument_id=iid,
        group=group,
        trend_class="STRONG_BULL",
        liquidity_class="HIGH",
        volatility_class="MEDIUM",
        relative_strength=87.5,
        eligibility=True,
        reason="ema20>ema50>ema200, fresh high inside 5 sessions",
        indicators={"ema20": 101.5, "adx14": 32.25, "fresh_high": True, "roc12": None},
        completeness=0.75,
        config_version=3,
    )
    return replace(base, **changes)


async def test_discovery_snapshot_upsert_is_idempotent_per_day(
    store: Any, instrument_id: int
) -> None:
    # One row per (trading_date, instrument): a re-run of the discovery must
    # overwrite the day's verdict in place. The replay below re-classifies the
    # same stock on the same day — the excluded re-run is the final word.
    day = date(2026, 10, 9)
    assert await store.upsert_discovery_snapshots([make_discovery_row(instrument_id, day)]) == 1

    replayed = make_discovery_row(
        instrument_id,
        day,
        group="EXCLUDED",
        eligibility=False,
        reason="stale_last_bar:2026-09-29",
    )
    assert await store.upsert_discovery_snapshots([replayed]) == 1

    rows = await store.load_discovery_snapshots(day)
    assert len(rows) == 1, "same (day, instrument) must be one row, not two"
    assert rows[0].group == "EXCLUDED"
    assert rows[0].eligibility is False
    assert rows[0].reason == "stale_last_bar:2026-09-29"


async def test_discovery_snapshot_round_trips_indicators_jsonb_and_classes(
    store: Any, instrument_id: int
) -> None:
    # Every column is its own fact: the class vocabulary, the jsonb fold with a
    # None hole in it (an indicator that did not exist is stored as null, never
    # a fabricated zero), and the day records that identify the run.
    day = date(2026, 10, 9)
    await store.upsert_discovery_snapshots([make_discovery_row(instrument_id, day)])

    stored = (await store.load_discovery_snapshots(day))[0]
    assert stored.instrument_id == instrument_id
    assert stored.trading_date == day
    assert stored.trend_class == "STRONG_BULL"
    assert stored.liquidity_class == "HIGH"
    assert stored.volatility_class == "MEDIUM"
    assert stored.relative_strength == pytest.approx(87.5)
    assert stored.indicators == {
        "ema20": 101.5,
        "adx14": 32.25,
        "fresh_high": True,
        "roc12": None,
    }, "jsonb must come back as the dict that went in, hole included"
    assert stored.completeness == pytest.approx(0.75)
    assert stored.universe == "FNO"
    assert stored.config_version == 3
    assert stored.is_simulated is False

    # A day nobody ran is empty, and ordering is deterministic by instrument_id.
    assert await store.load_discovery_snapshots(date(2026, 10, 8)) == []
    await store.upsert_instruments(
        [
            Instrument(
                id=0,
                exchange="NSE",
                symbol="CONFTEST2",
                trading_symbol="CONFTEST2",
                company_name="Conformance Two Ltd",
                instrument_token="999998",
            )
        ]
    )
    second = [row for row in await store.list_universe() if row.symbol == "CONFTEST2"][0]
    await store.upsert_discovery_snapshots(
        [make_discovery_row(second.id, day, group="NEUTRAL_WATCH", trend_class="NEUTRAL")]
    )
    rows = await store.load_discovery_snapshots(day)
    assert [row.instrument_id for row in rows] == sorted(row.instrument_id for row in rows)
    assert {row.group for row in rows} == {"STRONG_BULLISH", "NEUTRAL_WATCH"}


async def test_stock_profiles_coalesce_listing_date_across_sources(
    store: Any, instrument_id: int
) -> None:
    await store.upsert_stock_profiles(
        [
            StockProfile(
                instrument_id=instrument_id,
                pe_ratio=21.5,
                market_cap_cr=1_569_999.0,
                listing_date=date(1995, 11, 29),
                profile_source="yahoo+equity_l",
            )
        ]
    )
    row = await read_stock_details(store, instrument_id)
    assert row["pe_ratio"] == pytest.approx(21.5)
    assert row["market_cap_cr"] == pytest.approx(1_569_999.0)
    assert row["listing_date"] == date(1995, 11, 29)

    # A Yahoo-only run must refresh its columns but never blank the listing
    # date: the NSE equity list is the only source for it (COALESCE).
    await store.upsert_stock_profiles(
        [
            StockProfile(
                instrument_id=instrument_id,
                pe_ratio=22.1,
                market_cap_cr=1_600_000.0,
                listing_date=None,
                profile_source="yahoo",
            )
        ]
    )
    row = await read_stock_details(store, instrument_id)
    assert row["pe_ratio"] == pytest.approx(22.1)
    assert row["listing_date"] == date(1995, 11, 29)
    assert row["profile_source"] == "yahoo"

    # The profile writer owns pe/mcap/listing; the technicals columns on the
    # same row are untouched by it (disjoint write sets, one shared row).
    states = await store.list_rsi_states()
    assert states[0].daily_rsi is None, "no technicals were written for this instrument yet"


# ── jsonb parameters ──────────────────────────────────────────────────────────


async def test_jsonb_columns_round_trip_dicts_and_lists(
    store: Any, instrument_id: int, session_id: int
) -> None:
    # The first real Telegram dispatch exposed psycopg3 refusing to adapt a
    # bare dict ("cannot adapt type 'dict'") — every jsonb write site had to
    # get the Json wrapper (notification payload, event detail, audit values,
    # summary final_watchlist). A feed-less run never reaches these paths, so
    # this test exists to keep them honest on both stores.
    signal = await store.insert_signal(make_signal(instrument_id, session_id, "conf:jsonb:1"))
    payload = {"text": "BULLISH RELIANCE @ 1234.5", "deep_link": "/instrument/reliance"}
    delivery_id = await store.enqueue_notification(
        NotificationDelivery(
            id=None,
            signal_id=signal.signal_id,
            channel=Channel.TELEGRAM,
            destination_reference="@conformance",
            delivery_status=DeliveryStatus.QUEUED,
            payload=payload,
        )
    )
    claims = await store.claim_due_notifications(channel=Channel.TELEGRAM, now=minute(11), limit=1)
    assert [claim.delivery.id for claim in claims] == [delivery_id]
    assert claims[0].delivery.payload == payload, "payload must come back as a dict, not a string"

    await store.create_membership(
        session_id,
        instrument_id,
        WatchlistState.ACTIVE,
        entered_at=minute(10),
        price=100.0,
        score=0.80,
        snapshot_id=None,
        is_simulated=True,
    )
    rows = await store.list_memberships(session_id)
    event_id = await store.add_watchlist_event(
        WatchlistEvent(
            watchlist_id=rows[0].id,
            event_type="STRENGTHENED",
            previous_state=WatchlistState.WEAKENING,
            new_state=WatchlistState.ACTIVE,
            event_timestamp=minute(12),
            reason="rv recovered",
            price=101.0,
            detail={"relative_volume": 2.5},
        )
    )
    assert event_id > 0

    await store.audit(
        actor="conformance",
        action="TEST",
        entity_type="instrument",
        entity_id=str(instrument_id),
        previous_value={"rank": 9},
        new_value={"rank": 3},
    )

    await store.write_daily_summary(
        session_id,
        BASE_DATE,
        {
            "qualified_count": 1,
            "signals_generated": 1,
            "final_watchlist": [{"symbol": "CONFTEST", "rank": 1, "score": 0.8}],
        },
    )
    await store.write_daily_summary(
        session_id,
        BASE_DATE,
        {"qualified_count": 2, "final_watchlist": []},
    )


async def test_ops_setting_decodes_whatever_shape_the_column_holds(store: Any) -> None:
    """`get_ops_setting` reads the row the dashboard actually wrote, in either shape.

    An earlier build of the page passed ``JSON.stringify(doc)`` to ``$n::jsonb``, so
    the column held a jsonb *string* — ``"{\\"days\\": 3}"`` — rather than an object.
    Every caller treats ``None`` as "the settings page never touched this" and falls
    back to its environment, which made that row a knob that moved on screen and did
    nothing in the process. It reached production unnoticed, so the reader decodes
    the string instead of reporting the row absent, and this test pins all three
    shapes the column can hold: object, double-encoded string, and a scalar that is
    not a document at all.
    """
    if isinstance(store, InMemoryStore):
        pytest.skip("these are shapes a jsonb column can hold; a Python dict is not one")
    rows = (
        ("alert_strategies", '{"version": 1, "strategies": {}}'),
        ("retention", '"{\\"days\\": 3}"'),
        ("theme", '"3"'),
    )
    async with store._pool.connection() as conn, conn.transaction():
        for key, raw in rows:
            await conn.execute(
                "INSERT INTO ops_settings (key, value) VALUES (%s, %s::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, raw),
            )
    assert await store.get_ops_setting("alert_strategies") == {"version": 1, "strategies": {}}
    assert await store.get_ops_setting("retention") == {"days": 3}, "double-encoded row must still be obeyed"
    assert await store.get_ops_setting("theme") is None, "a scalar is not a document"
    assert await store.get_ops_setting("absent") is None

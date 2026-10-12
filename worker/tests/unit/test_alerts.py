"""Behavioural tests for the alert engine (spec §12).

Covers the idempotency key contract, cooldown windows, simulation honesty and
the Telegram/dashboard delivery rules, plus the message format.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import BASE_DAY, minute
from worker.alerts import AlertEngine, episode_id_for, format_message, idempotency_key
from worker.config import ScannerParams
from worker.models import (
    Channel,
    DeliveryStatus,
    NotificationDelivery,
    Signal,
    SignalType,
)
from worker.persistence.memory import InMemoryStore

CHAT_ID = "chat-99"
APP_URL = "http://localhost:3000"


def engine_for(store: InMemoryStore, params: ScannerParams, **overrides: Any) -> AlertEngine:
    base: dict[str, Any] = {
        "telegram_enabled": True,
        "telegram_chat_id": CHAT_ID,
        "app_url": APP_URL,
        "simulation": False,
    }
    base.update(overrides)
    return AlertEngine(store=store, params=params, **base)


async def push(
    alerts: AlertEngine,
    *,
    signal_type: SignalType = SignalType.BULLISH_ENTRY,
    instrument_id: int | None = 1,
    episode: str | None = "2026-10-12-1-e1",
    at: Any = None,
    symbol: str | None = "RELIANCE",
    **kw: Any,
):
    at = at if at is not None else minute(0)
    args: dict[str, Any] = {
        "price": 1234.5,
        "vwap": 1230.0,
        "ema_fast": 1232.0,
        "ema_slow": 1228.0,
        "relative_volume": 1.8,
        "intraday_change_pct": 2.5,
        "score": 77.5,
        "reasons": ("CLOSE_ABOVE_VWAP", "EMA9_ABOVE_EMA20"),
        "candle_start": at,
        "symbol": symbol,
        "company_name": "Reliance Industries",
        "exchange": "NSE",
    }
    args.update(kw)
    return await alerts.raise_alert(
        signal_type=signal_type,
        instrument_id=instrument_id,
        session_id=1,
        event_timestamp=at,
        episode_id=episode,
        **args,
    )


def make_signal(**overrides: Any) -> Signal:
    base: dict[str, Any] = {
        "instrument_id": 1,
        "trading_session_id": 1,
        "signal_type": SignalType.BULLISH_ENTRY,
        "qualifying_episode_id": "2026-10-12-1-e1",
        "idempotency_key": "BULLISH_ENTRY:1:2026-10-12-1-e1",
        "event_timestamp": minute(0),
        "price": 1234.5,
        "vwap": 1230.0,
        "ema_fast": 1232.0,
        "ema_slow": 1228.0,
        "relative_volume": 1.8,
        "intraday_change_pct": 2.5,
        "score": 77.5,
        # two reasons so format_message's joined "Why:" line is exercised
        "reasons": ("CLOSE_ABOVE_VWAP", "EMA9_ABOVE_EMA20"),
        "symbol": "RELIANCE",
        "company_name": "Reliance Industries",
        "exchange": "NSE",
    }
    base.update(overrides)
    return Signal(**base)


def telegram_rows(store: InMemoryStore) -> list[NotificationDelivery]:
    return [d for d in store.deliveries.values() if d.channel == Channel.TELEGRAM]


def dashboard_rows(store: InMemoryStore) -> list[NotificationDelivery]:
    return [d for d in store.deliveries.values() if d.channel == Channel.DASHBOARD]


@pytest.fixture()
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture()
def alerts(store: InMemoryStore, params: ScannerParams) -> AlertEngine:
    return engine_for(store, params)


# ── §6 idempotency ───────────────────────────────────────────────────────────


def test_idempotency_key_shapes():
    # §6.1 key = signal_type + instrument + episode when an episode exists.
    assert (
        idempotency_key(
            signal_type=SignalType.BULLISH_ENTRY, instrument_id=1, episode_id="2026-10-12-1-e1"
        )
        == "BULLISH_ENTRY:1:2026-10-12-1-e1"
    )
    # without an episode it falls back to the candle minute; session-wide
    # events (no instrument) key on kind + session only.
    assert (
        idempotency_key(signal_type="WATCHLIST_ADDED", instrument_id=2, candle_start=minute(5))
        == f"WATCHLIST_ADDED:2:{minute(5).strftime('%Y%m%d%H%M')}"
    )
    assert (
        idempotency_key(signal_type=SignalType.DAILY_SCAN_COMPLETE, instrument_id=None, session_id=7)
        == "DAILY_SCAN_COMPLETE:session-7"
    )


def test_episode_id_is_deterministic():
    assert episode_id_for(trading_date=BASE_DAY, instrument_id=1, episode=2) == "2026-10-12-1-e2"


async def test_duplicate_alert_mints_no_signal_and_no_delivery(alerts, store):
    # §6.2 re-raising the same episode: created=False/duplicate=True, no new
    # signal row and no extra queue rows.
    first = await push(alerts)
    assert first.created is True
    signals_before = len(store.signals)
    deliveries_before = len(store.deliveries)

    second = await push(alerts)
    assert second.created is False
    assert second.duplicate is True
    assert second.suppressed_reason == "duplicate"
    assert second.notified is False
    assert len(store.signals) == signals_before
    assert len(store.deliveries) == deliveries_before


# ── §7 cooldown ──────────────────────────────────────────────────────────────


async def test_cooldown_suppresses_within_fifteen_minutes(alerts, store):
    # §7.1 same instrument + type within the 15-minute window (different
    # episode, so not a duplicate): the signal persists but delivery is only a
    # SUPPRESSED dashboard row and NO second Telegram queue entry is added —
    # the first push's row stays the only one.
    await push(alerts, at=minute(0))
    assert len(telegram_rows(store)) == 1, "the first entry alert must push"

    suppressed = await push(alerts, episode="2026-10-12-1-e2", at=minute(9))
    assert suppressed.created is True
    assert suppressed.suppressed_reason == "cooldown 15m"
    assert suppressed.delivery_channels == (Channel.DASHBOARD,)
    assert len(store.signals) == 2
    assert len(telegram_rows(store)) == 1  # §7 queues NO second Telegram row
    rows = [d for d in dashboard_rows(store) if d.signal_id == suppressed.signal.id]
    assert [d.delivery_status for d in rows] == [DeliveryStatus.SUPPRESSED]


async def test_entry_alert_pushes_first_and_lapses_after_the_window(alerts, store):
    # §7/§9 under the default 15-minute cooldown the FIRST BULLISH_ENTRY must
    # push exactly one Telegram row, and a second episode at the 15-minute
    # boundary must not be suppressed ((now - last) < 900s is false there).
    first = await push(alerts, at=minute(0))
    assert first.suppressed_reason is None
    assert len(telegram_rows(store)) == 1

    later = await push(alerts, episode="2026-10-12-1-e2", at=minute(15))
    assert later.suppressed_reason is None
    assert len(telegram_rows(store)) == 2


async def test_types_outside_cooldown_never_suppressed(alerts, store):
    # §7.2 WATCHLIST_REMOVED is not in COOLDOWN_APPLIED: two removals one
    # minute apart both record normally.
    first = await push(
        alerts, signal_type=SignalType.WATCHLIST_REMOVED, episode="2026-10-12-1-e1", at=minute(0)
    )
    second = await push(
        alerts, signal_type=SignalType.WATCHLIST_REMOVED, episode="2026-10-12-1-e2", at=minute(1)
    )
    assert first.suppressed_reason is None
    assert second.suppressed_reason is None
    assert second.created is True
    assert len(telegram_rows(store)) == 2


async def test_cooldown_is_per_instrument(store):
    # §7.3 the window keys on (instrument, signal type): instrument 1's entry
    # must not affect instrument 2. Cooldown is disabled so the self-
    # suppression bug pinned above cannot mask the per-instrument semantics.
    alerts = engine_for(store, ScannerParams(alert_cooldown_minutes=0))
    one = await push(alerts, instrument_id=1)
    two = await push(alerts, instrument_id=2, episode="2026-10-12-2-e1")
    assert one.suppressed_reason is None
    assert two.suppressed_reason is None
    assert two.created is True
    assert len(telegram_rows(store)) == 2


# ── §8 simulation mode ───────────────────────────────────────────────────────


async def test_simulation_never_queues_telegram(store, params):
    # §8 even with telegram enabled + chat id set, a simulated signal only gets
    # a SIMULATED dashboard delivery and persists with is_simulated=True.
    alerts = engine_for(store, params, simulation=True)
    outcome = await push(alerts)
    assert outcome.created is True
    assert outcome.signal.is_simulated is True
    assert store.signals[outcome.signal.id].is_simulated is True
    assert telegram_rows(store) == []
    rows = dashboard_rows(store)
    assert [d.delivery_status for d in rows] == [DeliveryStatus.SIMULATED]
    assert outcome.suppressed_reason == "simulation mode"
    assert outcome.delivery_channels == (Channel.DASHBOARD,)


# ── §9 delivery routing ──────────────────────────────────────────────────────


async def test_pushed_type_queues_exactly_one_telegram(alerts, store):
    # §9.1 a pushed signal (WATCHLIST_REMOVED, so the §7 cooldown bug cannot
    # interfere) writes one QUEUED TELEGRAM row addressed to the chat id,
    # whose payload text is format_message(...) output; DASHBOARD is recorded
    # DELIVERED.
    outcome = await push(alerts, signal_type=SignalType.WATCHLIST_REMOVED)
    stored = store.signals[outcome.signal.id]

    tg = telegram_rows(store)
    assert len(tg) == 1
    assert tg[0].delivery_status is DeliveryStatus.QUEUED
    assert tg[0].destination_reference == CHAT_ID
    assert tg[0].signal_id == stored.id
    expected_text = format_message(stored, deep_link=alerts.deep_link(stored))
    assert tg[0].payload["text"] == expected_text
    assert tg[0].payload["signal_type"] == "WATCHLIST_REMOVED"
    assert tg[0].payload["is_simulated"] is False
    assert tg[0].next_retry_at == stored.event_timestamp

    dash = [d for d in dashboard_rows(store) if d.signal_id == stored.id]
    assert [d.delivery_status for d in dash] == [DeliveryStatus.DELIVERED]
    assert outcome.delivery_channels == (Channel.DASHBOARD, Channel.TELEGRAM)
    assert outcome.notified is True


async def test_entry_type_queues_telegram_once_when_cooldown_disabled(store):
    # §9.2 BULLISH_ENTRY channel routing itself (exactly one QUEUED Telegram +
    # one DELIVERED dashboard row) with the window turned off, so rule 9 holds
    # for the canonical entry type despite the §7 self-suppression bug.
    alerts = engine_for(store, ScannerParams(alert_cooldown_minutes=0))
    outcome = await push(alerts)
    assert outcome.suppressed_reason is None
    tg = telegram_rows(store)
    assert len(tg) == 1
    assert tg[0].delivery_status is DeliveryStatus.QUEUED
    assert tg[0].destination_reference == CHAT_ID
    assert [d.delivery_status for d in dashboard_rows(store)] == [DeliveryStatus.DELIVERED]
    assert outcome.signal.id is not None
    assert len(store.signals) == 1


async def test_non_pushed_type_records_dashboard_only(alerts, store):
    # §9.3 WATCHLIST_WEAKENING is bookkeeping, not a phone ping.
    outcome = await push(alerts, signal_type=SignalType.WATCHLIST_WEAKENING)
    assert outcome.created is True
    assert telegram_rows(store) == []
    assert [d.delivery_status for d in dashboard_rows(store)] == [DeliveryStatus.DELIVERED]


async def test_telegram_enabled_without_chat_id_queues_nothing(store, params):
    # §9.4 a missing destination must not fabricate a queue row.
    alerts = engine_for(store, params, telegram_chat_id="")
    outcome = await push(alerts)
    assert outcome.created is True
    assert telegram_rows(store) == []
    assert len(outcome.delivery_ids) == 1  # dashboard only


async def test_raise_alert_persists_the_full_signal(alerts, store):
    # §9.5 the stored row carries every decision input, and the engine adopts
    # the id assigned by the repository.
    outcome = await push(alerts, previous_close=1204.0, message="Breakout over VWAP.")
    stored = store.signals[outcome.signal.id]
    assert stored.id == outcome.signal.id
    assert stored.idempotency_key == "BULLISH_ENTRY:1:2026-10-12-1-e1"
    assert stored.qualifying_episode_id == "2026-10-12-1-e1"
    assert stored.reasons == ("CLOSE_ABOVE_VWAP", "EMA9_ABOVE_EMA20")
    assert stored.score == 77.5
    assert stored.previous_close == 1204.0
    assert stored.message == "Breakout over VWAP."
    assert stored.config_version == 1
    assert stored.candle_start == minute(0)


# ── §10 message format ───────────────────────────────────────────────────────


def test_format_message_contains_every_expected_line():
    # §10.1 symbol, ₹ price with intraday %, VWAP/EMA line, relative volume,
    # Why:, IST timestamp; no raw float artefacts.
    text = format_message(
        make_signal(
            relative_volume=10.899999,
            reasons=("CLOSE_ABOVE_VWAP", "EMA9_ABOVE_EMA20"),
        )
    )
    lines = text.splitlines()
    assert lines[0] == "🟢 Bullish Entry — RELIANCE"
    assert "Reliance Industries" in lines
    assert "Price: ₹1,234.50 (+2.50% vs prev close)" in text
    assert "VWAP ₹1,230.00 · EMA9 ₹1,232.00 · EMA20 ₹1,228.00" in text
    assert "Relative volume: 10.90×" in text
    assert "10.899999" not in text  # formatted, never a raw float dump
    assert "Score: 77.5" in text
    assert "Why: close above vwap, ema9 above ema20" in text
    assert "2026-10-12 09:15:00 IST" in text
    assert "SIMULATED" not in text


def test_format_message_simulated_banner_and_deep_link():
    # §10.2 banner + configured app deep link are explicit lines.
    text = format_message(make_signal(is_simulated=True), deep_link=f"{APP_URL}/instrument/reliance")
    assert "SIMULATED — not a live market signal" in text
    assert text.splitlines()[-1] == "http://localhost:3000/instrument/reliance"


def test_format_message_falls_back_to_instrument_id_and_skips_missing_fields():
    # §10.3 no symbol -> "instrument #id"; absent metrics omit their lines.
    text = format_message(
        make_signal(
            instrument_id=7,
            symbol=None,
            company_name=None,
            price=None,
            vwap=None,
            ema_fast=None,
            ema_slow=None,
            relative_volume=None,
            score=None,
            reasons=(),
        )
    )
    assert "Bullish Entry — instrument #7" in text
    assert "Price:" not in text
    assert "VWAP" not in text
    assert "Why:" not in text
    assert "SIMULATED" not in text


def test_deep_link_needs_app_url_and_symbol(store, params):
    # §10.4 trailing slash normalised; None when either part is missing — and
    # never leaks credentials into the link.
    linked = engine_for(store, params, app_url="http://localhost:3000/")
    signal = make_signal(symbol="RELIANCE")
    assert linked.deep_link(signal) == "http://localhost:3000/instrument/reliance"
    bare = engine_for(store, params, app_url="")
    assert bare.deep_link(signal) is None
    assert linked.deep_link(make_signal(symbol=None)) is None


# ── feed helpers (session-wide events) ───────────────────────────────────────


async def test_feed_interrupted_and_recovered_are_pushed(alerts, store):
    # operational alerts have no instrument; both are pushed types and record
    # their outage episode.
    down = await alerts.feed_interrupted(
        session_id=1, at=minute(0), reason="socket closed", outage_episode="2026-10-12-outage-1"
    )
    up = await alerts.feed_recovered(
        session_id=1, at=minute(3), outage_episode="2026-10-12-outage-1", quiet_seconds=180.0
    )
    assert down.created is True and up.created is True
    assert down.signal.instrument_id is None
    assert down.signal.message == "Market-data feed interrupted: socket closed"
    assert up.signal.message == "Feed restored after 180s of silence."
    assert up.signal.qualifying_episode_id == "2026-10-12-outage-1"
    assert len(telegram_rows(store)) == 2  # cooldown skipped: no instrument id


# ── config knobs ─────────────────────────────────────────────────────────────


async def test_zero_cooldown_disables_suppression(store):
    params = ScannerParams(alert_cooldown_minutes=0)
    alerts = engine_for(store, params)
    first = await push(alerts, at=minute(0))
    second = await push(alerts, episode="2026-10-12-1-e2", at=minute(1))
    assert first.suppressed_reason is None
    assert second.suppressed_reason is None
    assert len(telegram_rows(store)) == 2

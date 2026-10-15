"""Behavioural tests for the alert engine (spec §12).

Covers the idempotency key contract, cooldown windows, simulation honesty and
the Telegram/dashboard delivery rules, plus the message format.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import BASE_DAY, minute
from worker.alerts import AlertEngine, episode_id_for, format_message, idempotency_key
from worker.analytics.breakouts import BreakoutEvent
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


# ── level breaks: raise_breakout (operator feature 2026-10-14) ───────────────


def event(**overrides: Any) -> BreakoutEvent:
    """A detector event as the tracker would hand it over.

    Defaults are a day-high break on RELIANCE at 100.5 over a 100.0 level.
    """
    base: dict[str, Any] = {
        "strategy": "session_break",
        "direction": "UP",
        "instrument_id": 1,
        "symbol": "RELIANCE",
        "level": 100.0,
        "price": 100.5,
        "candle_start": minute(0),
        "trading_date": BASE_DAY,
        "volume_ratio": 1.8,
        "cooldown_minutes": 15,
        "telegram": True,
        "reasons": ("strategy:session_break", "day_high_break", "volume_1.80x"),
    }
    base.update(overrides)
    return BreakoutEvent(**base)


def as_orb(**overrides: Any) -> BreakoutEvent:
    base: dict[str, Any] = {
        "strategy": "orb",
        "level": 99.0,
        "price": 100.5,
        "cooldown_minutes": 0,
        "reasons": ("strategy:orb", "orb_high_break", "volume_2.10x"),
    }
    base.update(overrides)
    return event(**base)


async def test_raise_breakout_writes_the_direction_type_and_the_strategy_reason(alerts, store):
    # Two signal types carry three strategies: the direction is what the alert *is*,
    # the strategy is data, and the strategy has to survive in a column.
    up = await alerts.raise_breakout(event(), session_id=1, previous_close=100.0)
    down = await alerts.raise_breakout(
        event(direction="DOWN", level=98.0, price=97.5,
              reasons=("strategy:session_break", "day_low_break")),
        session_id=1,
        previous_close=100.0,
    )
    assert up.signal.signal_type is SignalType.BREAKOUT_UP
    assert down.signal.signal_type is SignalType.BREAKDOWN
    assert up.signal.reasons[0] == "strategy:session_break"
    assert "broke the day high" in (up.signal.message or "")
    assert "broke the day low" in (down.signal.message or "")
    assert up.signal.intraday_change_pct == pytest.approx(0.5, abs=1e-9)
    assert up.signal.price == 100.5 and up.signal.relative_volume == 1.8
    assert up.signal.candle_start == minute(0)
    assert telegram_rows(store) and dashboard_rows(store)


async def test_only_the_vwap_strategy_puts_its_level_in_the_vwap_column(alerts):
    # A day break's level is a price the stock had already traded at, not the session
    # VWAP. Writing it into the vwap column would draw a line the chart never printed.
    day = await alerts.raise_breakout(event(), session_id=1)
    reclaim = await alerts.raise_breakout(
        as_orb(strategy="vwap", level=100.2,
               reasons=("strategy:vwap", "vwap_reclaim", "volume_1.80x")),
        session_id=1,
    )
    assert day.signal.vwap is None
    assert reclaim.signal.vwap == 100.2


async def test_the_same_level_never_alerts_twice_even_from_another_process(alerts, store):
    # Restart replay: the detector re-derives the same event, the level-keyed episode
    # id makes the idempotency key identical, and the row is the database's to refuse.
    first = await alerts.raise_breakout(event(), session_id=1)
    replay = await alerts.raise_breakout(event(), session_id=1)
    assert first.created is True
    assert replay.created is False and replay.duplicate is True
    assert replay.suppressed_reason == "duplicate"
    assert len(store.signals) == 1, "the replay must not queue a second delivery either"


async def test_a_higher_level_is_a_new_leg_and_the_cooldown_window_decides_its_timing(
    alerts, store
):
    # The day break re-arms after a pullback, so a second break of a *higher* high is
    # new news: a different episode id, so the database does not swallow it — and the
    # strategy's own window is what keeps a grinding tape from paging every minute.
    first = await alerts.raise_breakout(event(), session_id=1)
    within = await alerts.raise_breakout(event(level=101.0, candle_start=minute(5)), session_id=1)
    after = await alerts.raise_breakout(event(level=102.0, candle_start=minute(30)), session_id=1)
    assert first.created and within.created and after.created
    assert first.suppressed_reason is None
    assert within.suppressed_reason == "cooldown 15m", "window in force, board row kept"
    assert after.suppressed_reason is None, "30 minutes later the window has passed"
    assert len(store.signals) == 3
    assert len(telegram_rows(store)) == 2, "the suppressed leg pages nobody"


async def test_each_strategy_keeps_its_own_cooldown_window(alerts, store):
    # Three strategies share BREAKOUT_UP. An ORB break at the same minute as a day
    # high must not be muted by it — and ORB's own window is 0 because its level is
    # fixed for the day, so a second bucket crossing is impossible anyway.
    day = await alerts.raise_breakout(event(), session_id=1)
    orb = await alerts.raise_breakout(as_orb(candle_start=minute(1)), session_id=1)
    assert day.created and orb.created
    assert day.suppressed_reason is None and orb.suppressed_reason is None, (
        "the strategy discriminator in reasons is what keeps these independent"
    )
    # ...while a second day break 1 minute later is muted by the day break's window.
    again = await alerts.raise_breakout(
        event(level=101.0, candle_start=minute(2)), session_id=1
    )
    assert again.created is True and again.suppressed_reason == "cooldown 15m"
    assert len(store.signals) == 3


async def test_telegram_false_records_the_alert_without_paging_anybody(alerts, store):
    # The operator's per-strategy switch: the board still shows the break, the phone
    # does not ring. This is the delivery decision, not a detection one.
    quiet = await alerts.raise_breakout(event(telegram=False), session_id=1)
    loud = await alerts.raise_breakout(as_orb(candle_start=minute(1)), session_id=1)
    assert quiet.created is True and loud.created is True
    quiet_channels = {d.channel for d in store.deliveries.values() if d.signal_id == quiet.signal.id}
    loud_channels = {d.channel for d in store.deliveries.values() if d.signal_id == loud.signal.id}
    assert quiet_channels == {Channel.DASHBOARD}
    assert loud_channels == {Channel.DASHBOARD, Channel.TELEGRAM}


async def test_a_breakout_is_suppressed_by_cooldown_only_after_a_pushed_alert(store):
    # Cooldown counts *delivered* alerts. With Telegram off, only the dashboard row
    # exists — and DASHBOARD/DELIVERED is in the alerted set, so the window still
    # applies; a SUPPRESSED row must not push the window forward forever.
    alerts = engine_for(store, ScannerParams(), telegram_enabled=False)
    first = await alerts.raise_breakout(event(), session_id=1)
    second = await alerts.raise_breakout(event(level=101.0, candle_start=minute(3)), session_id=1)
    third = await alerts.raise_breakout(event(level=102.0, candle_start=minute(6)), session_id=1)
    assert first.suppressed_reason is None
    assert second.suppressed_reason == "cooldown 15m"
    assert third.suppressed_reason == "cooldown 15m", "the window does not extend itself"
    assert all(d.delivery_status is DeliveryStatus.SUPPRESSED for d in dashboard_rows(store)[1:])


# ── the fourth strategy: a reading, not a price move (operator rule 2026-10-15) ─


def as_rsi(**overrides: Any) -> BreakoutEvent:
    """The green side exactly as the ``rsi`` detector hands it over: Wilder RSI-14 on
    the five-minute closes printed 37.2 while the tape was up, so the break *down*
    through 40 is the entry. Note what is absent: no volume ratio (the rule is
    deliberately not gated on volume) and no VWAP (40 is not a price)."""
    base: dict[str, Any] = {
        "strategy": "rsi",
        "direction": "DOWN",
        "level": 40.0,
        "reading": 37.2,
        "price": 100.5,
        "volume_ratio": None,
        "cooldown_minutes": 30,
        "reasons": ("strategy:rsi", "rsi_oversold", "confirmed_2_candles"),
    }
    base.update(overrides)
    return event(**base)


async def test_the_two_rsi_sides_get_their_own_signal_types(alerts):
    # Not BREAKOUT_UP / BREAKDOWN: a green, actionable dip must not be filed under a
    # word that reads like a warning, which is the whole reason the reversal happened.
    entry = await alerts.raise_breakout(as_rsi(), session_id=1)
    warning = await alerts.raise_breakout(
        as_rsi(
            direction="UP",
            level=60.0,
            reading=63.4,
            reasons=("strategy:rsi", "rsi_overbought", "confirmed_2_candles"),
        ),
        session_id=1,
    )
    assert entry.signal.signal_type is SignalType.RSI_ENTRY
    assert warning.signal.signal_type is SignalType.RSI_OVERBOUGHT
    assert entry.signal.reasons[0] == "strategy:rsi"
    assert entry.signal.vwap is None, "40 is an RSI threshold; a chart must not draw it as a VWAP"
    assert "RSI-14 37.2 below 40 in an up-trend — entry at ₹100.50" in (entry.signal.message or "")
    assert (
        "RSI-14 63.4 above 60 in a down-trend — overbought at ₹100.50"
        in (warning.signal.message or "")
    )


async def test_an_rsi_entry_is_pushed_and_reads_like_a_person_wrote_it(alerts, store):
    # The type name reaches a phone through format_message's first line, and title()
    # of RSI_ENTRY is "Rsi Entry" — hence the hand-written labels.
    result = await alerts.raise_breakout(as_rsi(), session_id=1, previous_close=100.0)
    assert result.created is True, "RSI_ENTRY is in the pushed set"
    text = format_message(result.signal, deep_link=None)
    assert text.startswith("🟢 RSI entry — RELIANCE")
    assert "(+0.50% vs prev close)" in text
    assert "RSI-14 37.2 below 40" in text
    assert "Relative volume" not in text, "this rule has no volume evidence to show"
    assert telegram_rows(store), "the entry side pages the phone when Telegram is armed"


async def test_the_overbought_side_is_the_red_one(alerts):
    result = await alerts.raise_breakout(
        as_rsi(direction="UP", level=60.0, reading=63.4), session_id=1
    )
    assert format_message(result.signal, deep_link=None).startswith("🔴 RSI overbought — RELIANCE")


async def test_the_retired_pair_still_renders_and_pages_nobody(alerts, store):
    # RSI_BULLISH / RSI_BEARISH are raised by nothing any more, but rows written
    # before today stay readable: the emoji table keeps their glyphs so an old row
    # re-rendered on the board does not turn into an information icon, and neither
    # type is in the pushed set, so re-raising one cannot page anybody.
    old = await alerts.raise_alert(
        signal_type=SignalType.RSI_BULLISH,
        instrument_id=1,
        session_id=1,
        event_timestamp=minute(0),
        episode_id="history-1",
        symbol="RELIANCE",
        message="Daily RSI 63.2 · weekly RSI 61.8 — both above 60",
    )
    assert old.created is True
    assert format_message(old.signal, deep_link=None).startswith("📈 Rsi Bullish — RELIANCE")
    assert telegram_rows(store) == [], "a retired type is not a pushed type"


async def test_the_rsi_window_is_its_own_and_never_touches_a_price_break(alerts, store):
    day = await alerts.raise_breakout(event(), session_id=1)
    rsi = await alerts.raise_breakout(as_rsi(candle_start=minute(2)), session_id=1)
    assert day.created and rsi.created
    assert rsi.suppressed_reason is None, "a reading through 40 is not a second leg of a day high"

    # The same side again 18 minutes later is the same drift: new episode id (the
    # reading moved), so the database accepts the row — and the strategy's 30-minute
    # window is what keeps it off the phone.
    again = await alerts.raise_breakout(
        as_rsi(candle_start=minute(20), reading=36.1), session_id=1
    )
    assert again.created is True
    assert again.suppressed_reason == "cooldown 30m"
    assert len(telegram_rows(store)) == 2, "the day break and the first entry, and no third"


async def test_the_nightly_pair_and_the_intraday_rule_do_not_mute_each_other(alerts, store):
    # Both horizons raise RSI_ENTRY. The nightly one asks for no window at all — its
    # (type, instrument, session) key already makes it once per session — so a
    # session-start alert can never be muted by an intraday reading, and vice versa.
    intraday = await alerts.raise_breakout(as_rsi(), session_id=1)
    nightly = await alerts.raise_alert(
        signal_type=SignalType.RSI_ENTRY,
        instrument_id=1,
        session_id=1,
        event_timestamp=minute(3),
        episode_id="nightly-1",
        cooldown_minutes=0,
        reasons=("daily_below_40", "weekly_below_40"),
        message="Daily RSI 38.4 · weekly RSI 35.1 — both below 40",
        symbol="RELIANCE",
    )
    assert intraday.created and nightly.created
    assert intraday.suppressed_reason is None and nightly.suppressed_reason is None
    assert len(telegram_rows(store)) == 2

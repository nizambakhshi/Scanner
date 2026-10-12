"""Two-timeframe RSI alerts: one ping per bias *change*, never per session.

The operator rule (2026-10-12): daily and weekly RSI both above 60 → BULLISH,
both below 40 → BEARISH, the 40–60 band → NEUTRAL. ``_emit_rsi_transitions``
runs once at session start against ``stock_details`` and must hold four line
rules: a first sighting baselines silently (rollout day cannot page 212 times),
a standing state stays quiet, only a transition into BULLISH/BEARISH alerts,
and a transition into NEUTRAL is recorded as the new baseline so rotating back
out of the band later reads as a real change.
"""

from __future__ import annotations

from datetime import date

from tests.conftest import BASE_DAY, SESSION_CLOSE, SESSION_OPEN
from worker.config import WorkerSettings
from worker.main import WorkerRuntime, _rsi_message
from worker.models import (
    Instrument,
    RsiState,
    Signal,
    SignalType,
    StockTechnicals,
)
from worker.persistence.memory import InMemoryStore
from worker.timeutils import load_calendar


def runtime(store: InMemoryStore) -> WorkerRuntime:
    settings = WorkerSettings(
        scan_mode="live",
        simulation_label=False,
        timezone="Asia/Kolkata",
        app_url="",
    )
    return WorkerRuntime(settings=settings, store=store, calendar=load_calendar())


def instrument(iid: int, symbol: str) -> Instrument:
    return Instrument(
        id=iid,
        exchange="NSE",
        symbol=symbol,
        trading_symbol=symbol,
        company_name=f"{symbol} Limited",
        instrument_token=str(1000 + iid),
    )


async def seed(
    store: InMemoryStore,
    instrument_id: int,
    *,
    bias: str | None,
    last: str | None = None,
    daily: float | None = 63.2,
    weekly: float | None = 61.8,
) -> None:
    await store.update_stock_technicals(
        [
            StockTechnicals(
                instrument_id=instrument_id,
                daily_rsi=daily,
                weekly_rsi=weekly,
                rsi_bias=bias,
                rsi_as_of=date(2026, 10, 10),
                data_start=date(2002, 1, 1),
                high_52w=1700.0,
                low_52w=1200.0,
                ath=1700.0,
                atl=10.0,
            )
        ]
    )
    if last is not None:
        await store.set_last_alert_bias(instrument_id, last)


def signals_of(store: InMemoryStore, kind: SignalType) -> list[Signal]:
    return [row for row in store.signals.values() if row.signal_type == kind]


async def emit(store: InMemoryStore, instruments: dict[int, Instrument]):
    rt = runtime(store)
    rt._build_alerts()
    session = await store.ensure_session(BASE_DAY, SESSION_OPEN, SESSION_CLOSE)
    await rt._emit_rsi_transitions(session, instruments)
    return session


async def test_first_sighting_baselines_silently() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="BULLISH", last=None)
    await emit(store, {1: instrument(1, "RELIANCE")})

    assert signals_of(store, SignalType.RSI_BULLISH) == []
    assert store.stock_details[1]["last_alert_bias"] == "BULLISH"


async def test_transition_into_bullish_alerts_once_keyed_on_session() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="BULLISH", last="NEUTRAL")
    session = await emit(store, {1: instrument(1, "RELIANCE")})

    emitted = signals_of(store, SignalType.RSI_BULLISH)
    assert len(emitted) == 1
    signal = emitted[0]
    assert signal.idempotency_key == f"RSI_BULLISH:1:{session.id}"
    assert signal.symbol == "RELIANCE"
    assert "above 60" in (signal.message or "")
    assert signal.reasons == ("daily rsi above 60", "weekly rsi above 60")
    assert store.stock_details[1]["last_alert_bias"] == "BULLISH"
    deliveries = [row for row in store.deliveries.values() if row.signal_id == signal.id]
    assert [row.channel.value for row in deliveries] == ["DASHBOARD"]

    # A second start inside the same session sees a standing state: quiet.
    await emit(store, {1: instrument(1, "RELIANCE")})
    assert len(signals_of(store, SignalType.RSI_BULLISH)) == 1


async def test_restart_between_alert_and_bias_write_cannot_alert_twice() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="BULLISH", last="NEUTRAL")
    await emit(store, {1: instrument(1, "RELIANCE")})
    # Simulate the crash window: the signal row survived, the bias write did not.
    store.stock_details[1]["last_alert_bias"] = "NEUTRAL"

    await emit(store, {1: instrument(1, "RELIANCE")})

    assert len(signals_of(store, SignalType.RSI_BULLISH)) == 1
    assert store.stock_details[1]["last_alert_bias"] == "BULLISH"


async def test_neutral_transition_is_quiet_and_becomes_the_new_baseline() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="NEUTRAL", last="BULLISH", daily=52.0, weekly=48.0)
    await emit(store, {1: instrument(1, "RELIANCE")})

    assert signals_of(store, SignalType.RSI_BULLISH) == []
    assert signals_of(store, SignalType.RSI_BEARISH) == []
    assert store.stock_details[1]["last_alert_bias"] == "NEUTRAL"

    # Rotating back out of the band is a real change from the recorded baseline.
    await seed(store, 1, bias="BEARISH", daily=35.4, weekly=37.1)
    await emit(store, {1: instrument(1, "RELIANCE")})

    emitted = signals_of(store, SignalType.RSI_BEARISH)
    assert len(emitted) == 1
    assert "below 40" in (emitted[0].message or "")
    assert emitted[0].reasons == ("daily rsi below 40", "weekly rsi below 40")


async def test_unevaluable_and_out_of_universe_rows_are_skipped() -> None:
    store = InMemoryStore()
    # No daily/weekly pair to judge — the sync could not classify it.
    await seed(store, 1, bias=None, last=None, daily=None, weekly=None)
    # Classified, but not part of today's subscribed universe.
    await seed(store, 99, bias="BULLISH", last="NEUTRAL")
    await emit(store, {1: instrument(1, "RELIANCE")})

    assert signals_of(store, SignalType.RSI_BULLISH) == []
    assert store.stock_details[1].get("last_alert_bias") is None
    assert store.stock_details[99]["last_alert_bias"] == "NEUTRAL"


def test_rsi_message_formats_the_pair() -> None:
    state = RsiState(
        instrument_id=1,
        daily_rsi=63.25,
        weekly_rsi=61.84,
        rsi_bias="BULLISH",
        last_alert_bias="NEUTRAL",
    )
    assert _rsi_message(state) == "Daily RSI 63.2 · weekly RSI 61.8 — both above 60"

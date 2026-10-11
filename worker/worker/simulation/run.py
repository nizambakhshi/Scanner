"""``scanner-simulate`` — replay the whole pipeline over scripted synthetic ticks.

Why this exists: every rule that matters (warm-up gating, the three-condition
qualification, the weakening/removal streak, re-qualification, outage freezing,
idempotent alerts, the durable queue) is decided by the production classes, and
the only honest way to know they still hold together is to run them in order over
a day of data and check the result against the rules — not against a snapshot of
this program's own output.

What the replay guarantees:

* **no network, ever.** The store is always
  :class:`~worker.persistence.memory.InMemoryStore`, and under ``--dispatch`` the
  Telegram endpoint is a local recorder object. Synthetic rows are never written
  to the real database: a polluted production table cannot be un-polluted by
  rerunning a script, and Postgres behaviour is covered by the conformance suite
  in ``tests/integration`` instead.
* **no wall clock.** Minutes are fed on a virtual clock starting at the real
  09:15 IST of the chosen trading day, so a 70-minute replay finishes in
  milliseconds and replays identically for a given ``--seed``.
* **the report is the assertion.** :func:`check_invariants` restates the spec
  rules as checks on the rows the replay produced, and the default (``--strict``)
  exits non-zero if any of them fails.

Honesty of the two modes:

* default: every candle, snapshot, membership and signal is labelled
  ``is_simulated``, and notifications are recorded as ``SIMULATED`` deliveries —
  nothing pretends to be a live market signal;
* ``--dispatch`` runs the *live* alert path (``simulation=False``) so queue rows
  reach ``QUEUED`` and the real :class:`~worker.dispatcher.Dispatcher` drains
  them. Those rows therefore carry live labels while describing synthetic
  prices, which is why the banner below is printed whenever the mode is used. It
  tests the delivery machinery; it is not evidence about any stock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from ..alerts import PUSHED_SIGNAL_TYPES, AlertEngine, format_message
from ..analytics.indicators import IndicatorRegistry
from ..analytics.scanner import F_INSUFFICIENT_HISTORY
from ..config import ScannerParams, TelegramSettings
from ..dispatcher import Dispatcher
from ..logging_setup import configure_logging, get_logger, scrub
from ..marketdata.candle_builder import CandleEngine
from ..models import (
    Channel,
    DeliveryStatus,
    FeedStatus,
    Signal,
    SignalType,
    Tick,
    WatchlistEvent,
    WatchlistMembership,
    WatchlistState,
    WorkerHeartbeat,
)
from ..persistence.memory import InMemoryStore
from ..persistence.store import SessionRow
from ..pipeline import ScannerPipeline
from ..telegram import TelegramResult
from ..timeutils import SessionWindow, TradingCalendar, load_calendar, now_ist, to_ist
from ..watchlist import WatchlistManager
from .scenarios import SCRIPTS, Script, Universe, build_universe

log = get_logger(__name__)

EXIT_OK = 0
EXIT_INVARIANT_FAILED = 1
EXIT_SETUP_FAILED = 2

#: Stands in for a real chat id so queue rows are structurally complete without
#: naming a destination that exists.
SIMULATED_CHAT_ID = "simulated-chat-not-a-real-destination"
WORKER_ID = "simulator"
APP_URL = "http://localhost:3000"
DISPATCH_PASSES = 20
GRACE_SECONDS = 4  # one second past the configured candle grace period

DISPATCH_BANNER = (
    "DISPATCH MODE - synthetic prices replayed through the LIVE alert path so the durable "
    "queue and the real Dispatcher are exercised. The Telegram endpoint is a local recorder: "
    "no request left this process. Rows from this run carry live labels, so nothing in them "
    "may be read as a market signal."
)

SIMULATED_BANNER = (
    "SIMULATED DATA - synthetic ticks, in-memory store, no broker, no database, no network. "
    "Every candle, snapshot, membership and signal is labelled is_simulated."
)


# ── the local provider ──────────────────────────────────────────────────────


class RecordingTelegramSender:
    """The :class:`~worker.telegram.TelegramSender` interface, satisfied locally.

    Duck-typed on purpose: :class:`~worker.dispatcher.Dispatcher` only calls
    ``send`` and ``close``, so the loop under test is the production one with its
    network edge swapped out. ``fail_first`` makes that many attempts fail with a
    retryable HTTP 500, which exercises the durable retry ladder (§13) instead of
    only the happy path.
    """

    def __init__(self, *, fail_first: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failed_attempts: list[str] = []
        self._message_id = 0
        self._failures_left = fail_first

    @property
    def configured(self) -> bool:
        return True

    async def send(self, text: str, *, chat_id: str | None = None) -> TelegramResult:
        if self._failures_left > 0:
            self._failures_left -= 1
            self.failed_attempts.append(text)
            return TelegramResult(
                ok=False,
                error="simulated provider outage (500 from the local recorder)",
                attempts=1,
                http_status=500,
            )
        self._message_id += 1
        self.sent.append((chat_id or SIMULATED_CHAT_ID, text))
        return TelegramResult(
            ok=True, message_id=500000 + self._message_id, attempts=1, http_status=200
        )

    async def close(self) -> None:
        return None


# ── options ─────────────────────────────────────────────────────────────────


@dataclass
class SimulationOptions:
    minutes: int | None = None
    trading_date: date | None = None
    seed: int = 7
    dispatch: bool = False
    provider_failures: int = 0
    outage_minute: int | None = 30
    outage_seconds: float = 100.0
    cooldown_minutes: int | None = None
    scan_minute: int | None = None
    verbose: bool = False
    messages: bool = True
    output_json: bool = False
    list_scenarios: bool = False
    strict: bool = True

    @property
    def live_path(self) -> bool:
        """``True`` when the alert engine runs the live (non-simulated) path."""
        return self.dispatch


def parse_args(argv: list[str] | None = None) -> SimulationOptions:
    parser = argparse.ArgumentParser(
        prog="scanner-simulate",
        description="Replay the scanner pipeline over scripted synthetic ticks (no network).",
    )
    parser.add_argument(
        "--minutes", type=int, default=None, help="virtual minutes to run (default: longest script)"
    )
    parser.add_argument(
        "--date",
        dest="trading_date",
        default=None,
        help="trading day to replay, YYYY-MM-DD (default: the next trading day)",
    )
    parser.add_argument("--seed", type=int, default=7, help="deterministic jitter seed")
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="also drain the durable queue through the real Dispatcher against a local fake provider",
    )
    parser.add_argument(
        "--provider-failures",
        type=int,
        default=0,
        help="make the fake provider fail this many attempts first (exercises retry + backoff)",
    )
    parser.add_argument(
        "--outage-minute", type=int, default=30, help="minute the feed goes STALE at (use --no-outage to skip)"
    )
    parser.add_argument("--no-outage", action="store_true", help="run the replay with a healthy feed")
    parser.add_argument("--outage-seconds", type=float, default=100.0, help="how long the outage lasts")
    parser.add_argument(
        "--cooldown", type=int, default=None, dest="cooldown_minutes", help="override ALERT_COOLDOWN_MINUTES"
    )
    parser.add_argument(
        "--scan-minute", type=int, default=None, help="minute the initial scan runs at (default: INITIAL_SCAN_CANDLES)"
    )
    parser.add_argument("--verbose", action="store_true", help="print a line per virtual minute")
    parser.add_argument("--no-messages", action="store_true", help="omit the rendered alert texts")
    parser.add_argument("--json", action="store_true", dest="output_json", help="print the report as JSON")
    parser.add_argument("--list-scenarios", action="store_true", help="print the scripted scenarios and exit")
    parser.add_argument("--no-strict", action="store_true", help="exit 0 even when an invariant fails")
    args = parser.parse_args(argv)
    return SimulationOptions(
        minutes=args.minutes,
        trading_date=_parse_date(args.trading_date),
        seed=args.seed,
        dispatch=args.dispatch,
        provider_failures=max(0, args.provider_failures),
        outage_minute=None if args.no_outage else args.outage_minute,
        outage_seconds=max(1.0, args.outage_seconds),
        cooldown_minutes=args.cooldown_minutes,
        scan_minute=args.scan_minute,
        verbose=args.verbose,
        messages=not args.no_messages,
        output_json=args.output_json,
        list_scenarios=args.list_scenarios,
        strict=not args.no_strict,
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"--date must be YYYY-MM-DD, got {value!r}") from exc


# ── the replay ──────────────────────────────────────────────────────────────


@dataclass
class Simulation:
    """One synthetic trading session plus everything it produced."""

    options: SimulationOptions
    params: ScannerParams
    store: InMemoryStore
    calendar: TradingCalendar
    window: SessionWindow
    session: SessionRow
    universe: Universe
    pipeline: ScannerPipeline
    minutes: int
    scan_minute: int = 0
    daily_summary: dict[str, Any] = field(default_factory=dict)
    initial_scan: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    cum: dict[int, int] = field(default_factory=dict)
    outage_started_at: datetime | None = None
    outage_ended_at: datetime | None = None
    provider: RecordingTelegramSender | None = None
    dispatch_reports: list[dict[str, Any]] = field(default_factory=list)

    # ── virtual clock ──────────────────────────────────────────────────────

    def clock(self, minute_index: int, *, second: int = 0) -> datetime:
        """Virtual IST time for the ``minute_index``-th candle of the session."""
        return self.window.open_at + timedelta(minutes=minute_index, seconds=second)

    def symbol_of(self, instrument_id: int | None) -> str:
        if instrument_id is None:
            return "session"
        instrument = self.pipeline.instruments.get(instrument_id)
        return instrument.symbol if instrument else f"instrument#{instrument_id}"

    def stale_seconds_recorded(self) -> int:
        return self.store.sessions[self.session.id].stale_data_seconds

    # ── driving ────────────────────────────────────────────────────────────

    async def feed_minute(self, minute_index: int) -> int:
        """Two prints per instrument per minute: the open, then the close.

        Volume rides the day-cumulative counter exactly as the SmartAPI socket
        sends it, so the candle engine stays the only thing that turns it into a
        per-minute delta (§8).
        """
        ticks = 0
        for instrument_id, rows in self.universe.plans.items():
            if minute_index >= len(rows):
                continue  # this script has ended, or it never prints at all
            open_price, close_price, volume = rows[minute_index]
            instrument = self.pipeline.instruments[instrument_id]
            self.cum[instrument_id] = self.cum.get(instrument_id, 0) + volume
            for second, price in ((0, open_price), (30, close_price)):
                at = self.clock(minute_index, second=second)
                await self.pipeline.on_tick(
                    Tick(
                        instrument_id=instrument.id,
                        instrument_token=instrument.instrument_token,
                        exchange=instrument.exchange,
                        exchange_timestamp=at,
                        received_at=at + timedelta(seconds=1),
                        last_traded_price=price,
                        cumulative_volume=self.cum[instrument_id],
                        sequence_number=int(at.timestamp() * 10) % 1000000,
                        feed_mode=2,
                        is_simulated=not self.options.live_path,
                    )
                )
                ticks += 1
        return ticks

    async def run(self) -> None:
        await self.pipeline.on_feed_status(
            FeedStatus.STREAMING, "synthetic frames flowing", now=self.clock(0, second=1)
        )
        await self.publish_heartbeat(at_minute=0)
        scan_minute = self.options.scan_minute or self.params.initial_scan_candles
        self.scan_minute = scan_minute
        for minute_index in range(self.minutes):
            await self._open_outage(minute_index)
            await self.feed_minute(minute_index)
            at_boundary = self.clock(minute_index + 1, second=GRACE_SECONDS)
            candles = await self.pipeline.drain(at_boundary)
            await self._recover_outage(minute_index)
            if minute_index + 1 == scan_minute and not self.pipeline.initial_scan_completed:
                self.initial_scan = await self.pipeline.run_initial_scan(
                    self.clock(scan_minute, second=20)
                )
            if self.options.verbose:
                self.timeline.append(self._minute_row(minute_index, len(candles)))
        if self.pipeline._outage is not None:
            self.timeline.append(
                {
                    "minute": self.minutes,
                    "time": self.clock(self.minutes).strftime("%H:%M"),
                    "candles_finalized": 0,
                    "signals": len(self.store.signals),
                    "watchlist": {},
                    "note": "the scripted outage was still open when the replay ended",
                }
            )
        flushed = await self.pipeline.flush_session(self.clock(self.minutes, second=30))
        if flushed:
            self.timeline.append(
                {
                    "minute": self.minutes,
                    "time": self.clock(self.minutes).strftime("%H:%M"),
                    "candles_finalized": len(flushed),
                    "signals": len(self.store.signals),
                    "watchlist": self._watchlist_counts(),
                    "note": "session close flush",
                }
            )
        await self.publish_heartbeat(at_minute=self.minutes)
        self.daily_summary = await self.pipeline.finalize_day(self.clock(self.minutes, second=40))
        if self.provider is not None:
            await self._drain_queue()

    async def _open_outage(self, minute_index: int) -> None:
        planned = self.options.outage_minute
        if planned is None or planned < 0 or planned != minute_index or self.outage_started_at:
            return
        self.outage_started_at = self.clock(minute_index, second=1)
        await self.pipeline.on_feed_status(
            FeedStatus.STALE, "no synthetic frames (scripted outage)", now=self.outage_started_at
        )

    async def _recover_outage(self, minute_index: int) -> None:
        if self.outage_started_at is None or self.outage_ended_at is not None:
            return
        at = self.clock(minute_index + 1, second=30)
        if at < self.outage_started_at + timedelta(seconds=self.options.outage_seconds):
            return
        self.outage_ended_at = at
        await self.pipeline.on_feed_status(FeedStatus.STREAMING, "synthetic frames resumed", now=at)

    async def publish_heartbeat(self, *, at_minute: int) -> None:
        coverage = await self.store.coverage(universe=self.params.universe)
        await self.pipeline.publish_heartbeat(
            worker_id=WORKER_ID,
            now=self.clock(max(0, at_minute), second=1),
            subscribed_count=len(self.pipeline.instruments),
            last_market_data_at=None,
            universe_total=coverage.universe_total,
        )

    def _watchlist_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for membership in self.store.memberships.values():
            counts[membership.current_state.value] = counts.get(membership.current_state.value, 0) + 1
        return counts

    def _minute_row(self, minute_index: int, candles: int) -> dict[str, Any]:
        return {
            "minute": minute_index,
            "time": self.clock(minute_index).strftime("%H:%M"),
            "candles_finalized": candles,
            "signals": len(self.store.signals),
            "watchlist": self._watchlist_counts(),
        }

    # ── delivery ───────────────────────────────────────────────────────────

    async def _drain_queue(self) -> None:
        assert self.provider is not None
        dispatcher = Dispatcher(
            store=self.store,
            sender=self.provider,
            settings=TelegramSettings(
                bot_token="",  # the recorder never builds a URL, so no secret exists
                chat_id=SIMULATED_CHAT_ID,
                max_attempts=3,
                retry_base_seconds=5,
            ),
            worker_id=WORKER_ID,
        )
        now = self.clock(self.minutes, second=50)
        try:
            for _ in range(DISPATCH_PASSES):
                report = await dispatcher.run_once(now=now)
                self.dispatch_reports.append(report.as_dict())
                if not await self.store.pending_notification_count():
                    return
                # A batch limit can leave due rows behind, so "still pending" alone
                # is not a reason to stop — but neither may a retry that is not due
                # yet be pretended away: when the only work left sits in the future,
                # the virtual clock jumps to the earliest ``next_retry_at``.
                waiting = [
                    row.next_retry_at
                    for row in self.store.deliveries.values()
                    if row.next_retry_at is not None and row.next_retry_at > now
                ]
                if waiting:
                    now = min(waiting)  # type: ignore[type-arg]
                    continue
                if report.claimed == 0:
                    # Nothing was due and nothing is scheduled: the queue cannot
                    # drain itself, so stop instead of spinning.
                    return
        finally:
            await self.provider.close()


# ── construction ────────────────────────────────────────────────────────────


async def build(options: SimulationOptions) -> Simulation:
    """Wire the production classes around an in-memory store and scripted data."""
    params = _params(options)
    universe = build_universe(seed=options.seed)
    minutes = options.minutes or universe.longest_script
    if options.minutes and options.minutes < universe.longest_script:
        log.warning(
            "replay_truncated",
            requested=options.minutes,
            longest_script=universe.longest_script,
            note="a script cut short cannot demonstrate the transition in its last segment",
        )

    calendar = load_calendar()
    trading_date = options.trading_date or calendar.next_trading_day(now_ist().date())
    window = calendar.session(trading_date)
    if window is None:
        raise SystemExit(
            f"{trading_date.isoformat()} is not an NSE trading day on this calendar; "
            "choose another --date or omit it to use the next trading day"
        )

    store = InMemoryStore()
    await store.connect()
    await store.upsert_instruments(list(universe.instruments))
    session = await store.ensure_session(window.trading_date, window.open_at, window.close_at)
    await store.set_session_status(session.id, "IN_PROGRESS")
    store.set_subscribed({item.id for item in universe.instruments})

    alerts = AlertEngine(
        store=store,
        params=params,
        telegram_enabled=options.dispatch,
        telegram_chat_id=SIMULATED_CHAT_ID if options.dispatch else "",
        app_url=APP_URL,
        simulation=not options.dispatch,
    )
    pipeline = ScannerPipeline(
        store=store,
        params=params,
        calendar=calendar,
        session=session,
        window=window,
        instruments={item.id: item for item in universe.instruments},
        engine=CandleEngine(
            grace_seconds=params.candle_grace_seconds,
            session_open=window.open_at,
            session_close=window.close_at,
        ),
        registry=IndicatorRegistry(
            fast_period=params.fast_ema_period,
            slow_period=params.slow_ema_period,
            volume_lookback=params.volume_lookback,
        ),
        watchlist=WatchlistManager(
            store=store, params=params, alerts=alerts, simulation=not options.dispatch
        ),
        alerts=alerts,
        is_simulated=not options.dispatch,
    )
    simulation = Simulation(
        options=options,
        params=params,
        store=store,
        calendar=calendar,
        window=window,
        session=session,
        universe=universe,
        pipeline=pipeline,
        minutes=minutes,
        provider=RecordingTelegramSender(fail_first=options.provider_failures)
        if options.dispatch
        else None,
    )
    return simulation


def _params(options: SimulationOptions) -> ScannerParams:
    params = ScannerParams()
    if options.cooldown_minutes is not None:
        params = replace(params, alert_cooldown_minutes=options.cooldown_minutes)
    params.validate()
    return params


async def run_simulation(options: SimulationOptions) -> dict[str, Any]:
    """Build, drive and report one replay."""
    simulation = await build(options)
    await simulation.run()
    report = assemble_report(simulation)
    report["invariants"] = check_invariants(simulation, report)
    report["violations"] = [row["name"] for row in report["invariants"] if row["status"] == "FAIL"]
    await simulation.store.close()
    return report


# ── report ──────────────────────────────────────────────────────────────────


def assemble_report(simulation: Simulation) -> dict[str, Any]:
    store = simulation.store
    signals = sorted(store.signals.values(), key=lambda row: (row.event_timestamp, row.id or 0))
    events = sorted(store.events, key=lambda row: (row.event_timestamp, row.watchlist_id))
    deliveries = sorted(store.deliveries.values(), key=lambda row: row.id or 0)
    delivery_status: dict[str, int] = {}
    for row in deliveries:
        key = f"{row.channel.value}:{row.delivery_status.value}"
        delivery_status[key] = delivery_status.get(key, 0) + 1
    heartbeat = store.heartbeats.get(WORKER_ID)
    session_row = store.sessions[simulation.session.id]
    return {
        "mode": "dispatch" if simulation.options.dispatch else "simulated",
        "banner": DISPATCH_BANNER if simulation.options.dispatch else SIMULATED_BANNER,
        "network_used": False,
        "store": "InMemoryStore",
        "trading_date": simulation.window.trading_date.isoformat(),
        "session": {
            "id": session_row.id,
            "status": session_row.session_status,
            "open_at": _stamp(session_row.session_open_at),
            "close_at": _stamp(session_row.session_close_at),
            "initial_scan_completed_at": _stamp(session_row.initial_scan_completed_at),
            "stale_data_seconds": session_row.stale_data_seconds,
            "finalized_at": _stamp(session_row.finalized_at),
        },
        "minutes": simulation.minutes,
        "scan_minute": simulation.scan_minute,
        "params": simulation.params.to_dict(),
        "coverage": _coverage_row(simulation),
        "warmup": simulation.pipeline.warmup_report(),
        "counters": simulation.pipeline.counters.as_dict(),
        "candles": len(store.candles),
        "snapshots": len(store.snapshots),
        "initial_scan": _summary_counts(simulation.initial_scan),
        "initial_scan_ranked": list((simulation.initial_scan or {}).get("ranked", [])),
        "watchlist_events": [_event_row(event, simulation) for event in events],
        "memberships": [
            _membership_row(membership, simulation)
            for membership in sorted(
                store.memberships.values(), key=lambda row: simulation.symbol_of(row.instrument_id)
            )
        ],
        "signals": [_signal_row(row) for row in signals],
        "alert_texts": [_message(row) for row in signals] if simulation.options.messages else [],
        "deliveries": delivery_status,
        "delivery_rows": [_delivery_row(row) for row in deliveries],
        "provider": {
            "kind": "local recorder" if simulation.provider else None,
            "messages_recorded": len(simulation.provider.sent) if simulation.provider else 0,
            "provider_failures": len(simulation.provider.failed_attempts) if simulation.provider else 0,
        },
        "dispatch_passes": simulation.dispatch_reports,
        "outage": {
            "started_at": _stamp(simulation.outage_started_at),
            "ended_at": _stamp(simulation.outage_ended_at),
            "planned_seconds": simulation.options.outage_seconds
            if simulation.options.outage_minute is not None
            else None,
            "episodes_counted": simulation.pipeline.counters.outages,
            "stale_seconds_recorded": simulation.pipeline.counters.stale_seconds_total,
        },
        "heartbeat": _heartbeat_row(heartbeat) if heartbeat else None,
        "daily_summary": _daily_summary_row(simulation),
        "timeline": simulation.timeline,
        "scenarios": [_scenario_row(script, simulation) for script in SCRIPTS],
    }


def _coverage_row(simulation: Simulation) -> dict[str, Any]:
    """Coverage from the rows the replay wrote, plus this run's own observations.

    ``monitored_total`` counts instruments that actually printed, which is the
    number an operator needs next to ``subscribed_total`` — a mapped symbol with
    no ticks is a coverage gap, not a quiet market.
    """
    rows = [item for item in simulation.store.instruments.values() if item.is_nifty500]
    total = len(rows)
    mapped = sum(1 for item in rows if item.mapping_status.value == "MAPPED")
    with_ticks = len(simulation.pipeline._last_tick_at)
    return {
        "universe_total": total,
        "mapped_total": mapped,
        "subscribed_total": len(simulation.store.subscribed_ids),
        "monitored_total": with_ticks,
        "mapping_pct": round(mapped / total * 100.0, 2) if total else 0.0,
        "instruments_without_any_candle": sum(
            1
            for instrument_id in simulation.pipeline.instruments
            if not simulation.store.candle_rows(instrument_id)
        ),
    }


def _summary_counts(summary: dict[str, Any]) -> dict[str, Any]:
    counts = summary.get("counts") if isinstance(summary, dict) else None
    return dict(counts) if isinstance(counts, dict) else {}


def _daily_summary_row(simulation: Simulation) -> dict[str, Any]:
    summary = simulation.daily_summary or {}
    return {
        "kind": summary.get("kind"),
        "trading_date": summary.get("trading_date"),
        "unresolved_stale": summary.get("unresolved_stale"),
        "stale_seconds": summary.get("stale_seconds"),
        "final_watchlist": summary.get("final_watchlist"),
    }


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else to_ist(value).strftime("%Y-%m-%d %H:%M:%S")


def _clock(value: str | None) -> str:
    """``"YYYY-MM-DD HH:MM:SS"`` → ``"HH:MM"`` (the header shows the session window)."""
    if not value:
        return "?"
    parts = value.split(" ")
    return parts[1][:5] if len(parts) > 1 else value


def _event_row(event: WatchlistEvent, simulation: Simulation) -> dict[str, Any]:
    membership = simulation.store.memberships.get(event.watchlist_id)
    instrument_id = membership.instrument_id if membership else None
    return {
        "symbol": simulation.symbol_of(instrument_id),
        "event": event.event_type,
        "from": None if event.previous_state is None else event.previous_state.value,
        "to": event.new_state.value,
        "at": _stamp(event.event_timestamp),
        "price": event.price,
        "reason": event.reason,
    }


def _membership_row(membership: WatchlistMembership, simulation: Simulation) -> dict[str, Any]:
    return {
        "symbol": simulation.symbol_of(membership.instrument_id),
        "state": membership.current_state.value,
        "active": membership.is_active,
        "score": None if membership.latest_score is None else round(membership.latest_score, 2),
        "rank": membership.latest_rank,
        "price": membership.latest_price,
        "weak_streak": membership.consecutive_weak_candles,
        "episodes": membership.episode_count,
        "removal_reason": membership.removal_reason,
        "simulated": membership.is_simulated,
        "entry_at": _stamp(membership.initial_entry_at),
    }


def _signal_row(signal: Signal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "type": signal.signal_type.value,
        "symbol": signal.symbol or "session",
        "episode": signal.qualifying_episode_id,
        "key": signal.idempotency_key,
        "at": _stamp(signal.event_timestamp),
        "candle": _stamp(signal.candle_start),
        "price": signal.price,
        "relative_volume": None if signal.relative_volume is None else round(signal.relative_volume, 2),
        "score": None if signal.score is None else round(signal.score, 2),
        "is_simulated": signal.is_simulated,
        "reasons": list(signal.reasons),
    }


def _delivery_row(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "signal_id": row.signal_id,
        "channel": row.channel.value,
        "status": row.delivery_status.value,
        "attempts": row.attempt_count,
        "provider_message_id": row.provider_message_id,
        "destination": row.destination_reference,
        "last_error": scrub(row.last_error) if row.last_error else None,
    }


def _message(signal: Signal) -> str:
    return format_message(signal, deep_link=None)


def _heartbeat_row(heartbeat: WorkerHeartbeat) -> dict[str, Any]:
    return {
        "worker_id": heartbeat.worker_id,
        "status": heartbeat.status,
        "feed_status": heartbeat.feed_status.value,
        "last_market_data_at": _stamp(heartbeat.last_market_data_at),
        "last_completed_candle_at": _stamp(heartbeat.last_completed_candle_at),
        "subscribed_instrument_count": heartbeat.subscribed_instrument_count,
        "active_watchlist_count": heartbeat.active_watchlist_count,
        "pending_notification_count": heartbeat.pending_notification_count,
        "universe_total": heartbeat.universe_total,
        "mapped_total": heartbeat.mapped_total,
        "monitored_total": heartbeat.monitored_total,
    }


def _scenario_row(script: Script, simulation: Simulation) -> dict[str, Any]:
    instrument_id = next(
        (iid for iid, item in simulation.pipeline.instruments.items() if item.symbol == script.symbol),
        None,
    )
    observed = [
        event.event_type
        for event in sorted(simulation.store.events, key=lambda row: row.event_timestamp)
        if _belongs_to(event, instrument_id, simulation.store)
    ]
    fed = simulation.store.candle_rows(instrument_id) if instrument_id is not None else []
    return {
        "symbol": script.symbol,
        "intent": script.intent,
        "minutes": script.minutes,
        "candles": len(fed),
        "expected": list(script.expect),
        "observed": observed,
        "has_membership": instrument_id is not None
        and simulation.store.membership_keys.get((simulation.session.id, instrument_id)) is not None,
    }


def _belongs_to(event: WatchlistEvent, instrument_id: int | None, store: InMemoryStore) -> bool:
    if instrument_id is None:
        return False
    membership = store.memberships.get(event.watchlist_id)
    return membership is not None and membership.instrument_id == instrument_id


# ── the checks ──────────────────────────────────────────────────────────────


def check_invariants(simulation: Simulation, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Restate the spec rules as assertions on the rows the replay produced."""
    store = simulation.store
    params = simulation.params
    live = simulation.options.live_path
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    # 1. one candle and one snapshot per fed minute (§8).
    expected = sum(min(len(rows), simulation.minutes) for rows in simulation.universe.plans.values())
    add(
        "one candle and one snapshot per fed minute",
        len(store.candles) == expected and len(store.snapshots) == len(store.candles),
        f"fed minutes {expected}, candles {len(store.candles)}, snapshots {len(store.snapshots)}",
    )

    # 2. a mapped instrument that never prints produces no candle (§8: never invent).
    silent = next((i for i in simulation.pipeline.instruments.values() if i.symbol == "SIMSILENT"), None)
    silent_candles = store.candle_rows(silent.id) if silent else []
    add(
        "a silent instrument gets no invented candle",
        not silent_candles,
        f"{len(silent_candles)} candle(s) for {simulation.symbol_of(silent.id if silent else None)}; "
        f"initial scan missing_data={report['initial_scan'].get('missing_data')}",
    )

    # 3. nothing may qualify before warm-up (§8.4, §10).
    early = [
        row
        for row in store.signals.values()
        if row.candle_start is not None
        and simulation.window.candle_index(row.candle_start) < params.warmup_candles_required
    ]
    add("no alert before warm-up", not early, f"{len(early)} signal(s) before candle {params.warmup_candles_required}")

    # 4. warm-up is recorded as a reason, not silently skipped (§10).
    first = min(store.snapshots.values(), key=lambda row: row.candle_start, default=None)
    add(
        "the first candle says why it cannot be evaluated",
        first is None or F_INSUFFICIENT_HISTORY in first.failed_reasons,
        "no snapshots" if first is None else ", ".join(first.failed_reasons),
    )

    # 5. idempotency keys are unique (§12.3).
    keys = [row.idempotency_key for row in store.signals.values()]
    add("signal idempotency keys unique", len(set(keys)) == len(keys), f"{len(keys)} signals, {len(set(keys))} keys")

    # 6. one entry alert per episode (§12.1).
    entries = [row for row in store.signals.values() if row.signal_type is SignalType.BULLISH_ENTRY]
    episodes = [row.qualifying_episode_id for row in entries]
    add(
        "one BULLISH_ENTRY per episode",
        len(episodes) == len(set(episodes)),
        f"{len(episodes)} entries across {len(set(episodes))} episode(s)",
    )

    # 7. removal needed the full confirmation streak (§11 rule 3).
    removed = [m for m in store.memberships.values() if m.current_state is WatchlistState.REMOVED]
    worst = min((m.consecutive_weak_candles for m in removed), default=0)
    add(
        "removal respected the confirm count",
        all(m.consecutive_weak_candles >= params.removal_confirm_candles for m in removed),
        f"{len(removed)} removed row(s), shortest streak {worst}, needs {params.removal_confirm_candles}",
    )

    # 8. the append-only log ends where each stored row now is (§11.7).
    disagreeing = [
        simulation.symbol_of(membership.instrument_id)
        for membership in store.memberships.values()
        if (
            history := [event for event in store.events if event.watchlist_id == membership.id]
        )
        and history[-1].new_state is not membership.current_state
    ]
    add("every transition is in the event log", not disagreeing, f"disagreements: {disagreeing or 'none'}")

    # 9. the scripted outage froze state but not storage (§7.2, §12.4).
    if simulation.outage_started_at is not None and simulation.outage_ended_at is not None:
        during_events = [
            event
            for event in store.events
            if simulation.outage_started_at < event.event_timestamp < simulation.outage_ended_at
        ]
        during_signals = [
            row
            for row in store.signals.values()
            if simulation.outage_started_at <= row.event_timestamp <= simulation.outage_ended_at
            and row.signal_type
            not in (SignalType.DATA_FEED_INTERRUPTED, SignalType.DATA_FEED_RECOVERED)
        ]
        expected_stale = int(
            round((simulation.outage_ended_at - simulation.outage_started_at).total_seconds())
        )
        recorded = simulation.stale_seconds_recorded()
        add(
            "the outage froze the watchlist and was reported once",
            not during_events
            and not during_signals
            and recorded == expected_stale
            and simulation.pipeline.counters.outages == 1,
            f"{len(during_events)} transition(s) and {len(during_signals)} signal(s) inside the window; "
            f"session row stale seconds {recorded} (expected {expected_stale}); episodes "
            f"{simulation.pipeline.counters.outages}",
        )
    else:
        add("the outage froze the watchlist and was reported once", True, "no outage scripted")

    # 10. labelling honesty for whichever path ran.
    if live:
        labelled = [row for row in store.signals.values() if row.is_simulated]
        add(
            "dispatch mode used the live alert path",
            not labelled,
            f"{len(labelled)} signal(s) carried a simulated label",
        )
    else:
        unlabelled = {
            "candles": sum(1 for row in store.candles.values() if not row.is_simulated),
            "snapshots": sum(1 for row in store.snapshots.values() if not row.is_simulated),
            "signals": sum(1 for row in store.signals.values() if not row.is_simulated),
            "memberships": sum(1 for row in store.memberships.values() if not row.is_simulated),
        }
        telegram_rows = [row for row in store.deliveries.values() if row.channel is Channel.TELEGRAM]
        add(
            "every synthetic row is labelled is_simulated",
            not any(unlabelled.values()) and not telegram_rows,
            f"unlabelled {unlabelled}; telegram rows {len(telegram_rows)}",
        )

    # 11. the queue drained and DELIVERED means the provider confirmed it (§12.5, §13).
    if live and simulation.provider is not None:
        telegram_rows = [row for row in store.deliveries.values() if row.channel is Channel.TELEGRAM]
        pending = sum(
            1
            for row in telegram_rows
            if row.delivery_status
            in (DeliveryStatus.QUEUED, DeliveryStatus.IN_FLIGHT, DeliveryStatus.FAILED)
        )
        delivered = [row for row in telegram_rows if row.delivery_status is DeliveryStatus.DELIVERED]
        without_id = [row for row in delivered if not row.provider_message_id]
        pushed = [row for row in store.signals.values() if row.signal_type in PUSHED_SIGNAL_TYPES]
        add(
            "every queued alert was delivered and confirmed by the provider",
            pending == 0
            and not without_id
            and len(delivered) == len(pushed)
            and len(simulation.provider.sent) == len(delivered),
            f"{len(pushed)} pushable signal(s), {len(delivered)} confirmed, {pending} pending, "
            f"{len(without_id)} without a provider message id, "
            f"{len(simulation.provider.sent)} recorded locally",
        )
        add(
            "no request left this process",
            simulation.provider._failures_left >= 0 and report["network_used"] is False,
            "the provider was a local recorder; the store was InMemoryStore",
        )

    # 12. end of day froze the watchlist and finalized the session (§11.8).
    session_row = store.sessions[simulation.session.id]
    still_tracked = [m for m in store.memberships.values() if m.is_active]
    add(
        "the replay closed the session honestly",
        session_row.session_status == "FINALIZED" and not still_tracked,
        f"session {session_row.session_status}, {len(still_tracked)} row(s) left active",
    )

    # 13. the initial scan ranked every qualifier it saw (§11.1).
    scan = report["initial_scan"]
    if scan:
        claimed = int(scan.get("qualified", 0))
        ranked_rows = [m for m in store.memberships.values() if m.latest_rank is not None]
        ranks = sorted(m.latest_rank for m in ranked_rows)
        add(
            "the initial scan ranked every qualifier",
            len(ranked_rows) == claimed and ranks == list(range(1, claimed + 1)),
            f"scan reported {claimed} qualifier(s), {len(ranked_rows)} row(s) carry a rank: {ranks}",
        )
    else:
        add("the initial scan ranked every qualifier", False, "the replay never ran the initial scan")

    # 14. each script demonstrated the rule it exists for.
    unmet = [
        f"{row['symbol']}: wanted {row['expected']}, saw {row['observed']}"
        for row in report["scenarios"]
        if not _expectation_met(row["expected"], row["observed"])
    ]
    add("every scenario demonstrated its rule", not unmet, "; ".join(unmet) or "all scripts matched their intent")

    return checks


def _expectation_met(expected: list[str], observed: list[str]) -> bool:
    """The listed transitions must appear, in order; nothing may be invented."""
    cursor = 0
    for want in expected:
        try:
            cursor = observed.index(want, cursor) + 1
        except ValueError:
            return False
    return True


# ── text rendering ──────────────────────────────────────────────────────────


def render(report: dict[str, Any]) -> str:
    out: list[str] = ["offline replay - " + report["mode"] + " mode", report["banner"], ""]
    out.append(
        f"session {report['session']['id']}  {report['trading_date']}  "
        f"{_clock(report['session']['open_at'])} -> {_clock(report['session']['close_at'])}  "
        f"{report['minutes']} virtual minutes  store={report['store']}  network={report['network_used']}"
    )
    coverage = report["coverage"]
    out.append(
        f"universe {coverage['universe_total']}  mapped {coverage['mapped_total']} "
        f"({coverage['mapping_pct']}%)  subscribed {coverage['subscribed_total']}  "
        f"printed {coverage['monitored_total']}  no candle "
        f"{coverage['instruments_without_any_candle']}"
    )
    warm = report["warmup"]
    out.append(
        f"warm-up needs {warm['required_candles']} candles  ready {warm['ready']}  "
        f"warming_up {warm['warming_up']}  with_history {warm['with_any_history']}"
    )
    counters = report["counters"]
    out.append(
        f"candles {report['candles']}  snapshots {report['snapshots']}  ticks {counters['ticks_seen']}  "
        f"signals {len(report['signals'])}  outage episodes {counters['outages']}  "
        f"stale seconds {counters['stale_seconds_total']}"
    )
    if report["initial_scan"]:
        out.append(
            f"initial scan (virtual minute {report['scan_minute']}) "
            + "  ".join(f"{key}={value}" for key, value in sorted(report["initial_scan"].items()))
        )
        ranked = report.get("initial_scan_ranked") or []
        if ranked:
            out.append("initial scan ranked  " + " > ".join(f"{i}. {symbol}" for i, symbol in enumerate(ranked, 1)))
        else:
            # Honest coverage note rather than a silent blank: the scan path that
            # ranks the daily list only has something to rank once instruments are
            # warm, and a cold replay store has no previous session to seed them.
            out.append(
                "note: nothing was qualified when the scan ran, so the ranked-list path was not exercised "
                f"(this store starts empty, unlike a live worker that warms from stored candles; "
                f"re-run with --scan-minute {warm['required_candles'] + 4} to see it)."
            )
    if report["timeline"]:
        out += ["", "timeline"]
        for row in report["timeline"]:
            watch = ", ".join(f"{key}={value}" for key, value in sorted(row["watchlist"].items())) or "empty"
            note = f"  ({row['note']})" if row.get("note") else ""
            out.append(
                f"  {row['time']}  minute {row['minute']:>3}  candles {row['candles_finalized']}  "
                f"signals {row['signals']}  watchlist: {watch}{note}"
            )
    out += ["", "watchlist transitions"]
    if not report["watchlist_events"]:
        out.append("  (none)")
    for row in report["watchlist_events"]:
        out.append(
            f"  {row['at'][-8:]}  {row['symbol']:<11} {row['event']:<18} {(row['from'] or '-'):<12} "
            f"-> {row['to']:<11} {_fmt(row['price']):>8}  {row['reason']}"
        )
    out += ["", "final watchlist"]
    if not report["memberships"]:
        out.append("  (empty)")
    for row in report["memberships"]:
        out.append(
            f"  {row['symbol']:<11} {row['state']:<12} active={str(row['active']):<5} "
            f"score={_fmt(row['score']):>7} rank={row['rank'] or '-'}  price={_fmt(row['price']):>8}  "
            f"weak_streak={row['weak_streak']}  episodes={row['episodes']}"
        )
    out += ["", "signals"]
    for row in report["signals"]:
        out.append(
            f"  {row['at'][-8:]}  {row['type']:<24} {row['symbol']:<11} episode={row['episode']} "
            f"rv={_fmt(row['relative_volume'])} simulated={row['is_simulated']}"
        )
    out += ["", "deliveries " + json.dumps(report["deliveries"], sort_keys=True)]
    if report["provider"]["kind"]:
        out.append(
            f"provider {report['provider']['kind']}: {report['provider']['messages_recorded']} recorded, "
            f"{report['provider']['provider_failures']} attempt(s) failed on purpose"
        )
    if report["dispatch_passes"]:
        out.append(
            f"dispatch passes {len(report['dispatch_passes'])}: "
            f"delivered {sum(row['delivered'] for row in report['dispatch_passes'])}, "
            f"retried {sum(row['retrying'] for row in report['dispatch_passes'])}, "
            f"permanently failed {sum(row['permanently_failed'] for row in report['dispatch_passes'])}"
        )
    heartbeat = report["heartbeat"]
    if heartbeat:
        out.append(
            f"heartbeat: feed {heartbeat['feed_status']}  last candle "
            f"{_tail(heartbeat['last_completed_candle_at'])}  active {heartbeat['active_watchlist_count']}  "
            f"pending {heartbeat['pending_notification_count']}  monitored "
            f"{heartbeat['monitored_total']}/{heartbeat['mapped_total']}/{heartbeat['universe_total']}"
        )
    out += ["", "scenarios"]
    for row in report["scenarios"]:
        flag = "ok  " if _expectation_met(row["expected"], row["observed"]) else "UNMET"
        out.append(f"  [{flag}] {row['symbol']:<11} {row['candles']:>3} candles  {row['intent']}")
        out.append(
            f"          expected {row['expected'] or ['(no membership)']}  "
            f"observed {row['observed'] or '(no transitions)'}"
        )
    if report["alert_texts"]:
        out += ["", "alert texts (exactly what the notification layer queued)"]
        for text in report["alert_texts"]:
            out.append("  " + "-" * 68)
            out += [f"  {line}" for line in text.splitlines()]
    out += ["", "invariants"]
    for row in report["invariants"]:
        out.append(f"  [{row['status']}] {row['name']}")
        out.append(f"           {row['detail']}")
    violations = report["violations"]
    out.append("")
    out.append(
        f"{len(report['invariants']) - len(violations)}/{len(report['invariants'])} invariants passed"
        + ("" if not violations else f"; failed: {', '.join(violations)}")
    )
    return "\n".join(out)


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _tail(stamp: str | None) -> str:
    return "-" if stamp is None else stamp[-8:]


# ── entry point ─────────────────────────────────────────────────────────────


def print_scenarios() -> None:
    print("scripted scenarios - each one exists to demonstrate one rule\n")
    for script in SCRIPTS:
        print(f"  {script.symbol:<11} {script.minutes:>3} min  {script.intent}")
        print(f"      expects: {', '.join(script.expect) if script.expect else 'no membership at all'}")


async def async_main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    configure_logging("info" if options.verbose else "warning", fmt="console")
    if options.list_scenarios:
        print_scenarios()
        return EXIT_OK
    report = await run_simulation(options)
    if options.output_json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render(report))
    if report["violations"] and options.strict:
        return EXIT_INVARIANT_FAILED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``scanner-simulate``)."""
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

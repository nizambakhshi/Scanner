"""The persistent worker process: lease → session → feed → minute loop (§17).

This is the only module that knows about *transport and lifecycle*. The feed
(``marketdata``), the scanner (``analytics``), the state machine
(``watchlist``), the alert queue (``alerts``) and the delivery loop
(``dispatcher``) are all driven from here against whichever store
implementation the mode requires.

Lifecycle rules that are easy to get wrong and are therefore explicit here:

* **one replica at a time** — a database lease (``worker_leases``) is acquired
  before anything touches the market and renewed on a timer. Losing the lease
  stops the session immediately, because two workers raising the same alert is
  worse than no worker;
* **restart recovery** — the open session row is reused, warm-up reads the
  candles this database already holds, and a completed initial scan is not run
  twice (§17 item 16);
* **shutdown never fakes a candle** — on stop the forming minute is persisted
  with ``is_complete=False`` and the session stays unfinalized, so the next
  start can rebuild it from broker history instead of treating a partial minute
  as real (§7.3);
* **the exchange clock owns timing** — every schedule decision is made against
  ``Asia/Kolkata`` from the trading calendar, never against the host timezone.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .alerts import AlertEngine
from .analytics.indicators import IndicatorRegistry
from .config import WorkerSettings, load_settings, safe_summary
from .dispatcher import Dispatcher
from .logging_setup import configure_logging, get_logger, scrub
from .marketdata.candle_builder import CandleEngine
from .marketdata.smartapi_auth import SmartApiAuth
from .marketdata.smartapi_rest import SmartApiRest
from .marketdata.smartapi_ws import SmartApiFeed
from .models import FeedStatus, Instrument, SessionStatus
from .persistence.memory import InMemoryStore
from .persistence.postgres import PostgresStore
from .persistence.store import ScannerStore, SessionRow
from .pipeline import ScannerPipeline
from .telegram import TelegramSender
from .timeutils import (
    SessionPhase,
    SessionWindow,
    TradingCalendar,
    load_calendar,
    now_ist,
    to_ist,
)
from .watchlist import WatchlistManager

log = get_logger(__name__)

LEASE_NAME = "market-data-session"
MINUTE_LOOP_SECONDS = 1.0
CLOSE_GRACE_SECONDS = 45.0
SHUTDOWN_FLUSH_SECONDS = 20.0


@dataclass
class WorkerRuntime:
    """Owns every coroutine of one worker process."""

    settings: WorkerSettings
    store: ScannerStore
    calendar: TradingCalendar
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    feed: SmartApiFeed | None = None
    pipeline: ScannerPipeline | None = None
    session: SessionRow | None = None
    window: SessionWindow | None = None
    last_market_data_at: datetime | None = None
    last_error: str | None = None
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list, repr=False)
    _lease_holder: str = field(default="", repr=False)
    _auth: SmartApiAuth | None = field(default=None, repr=False)
    _rest: SmartApiRest | None = field(default=None, repr=False)
    _sender: TelegramSender | None = field(default=None, repr=False)
    _universe_total: int = field(default=0, repr=False)
    _recovered_scan_at: datetime | None = field(default=None, repr=False)

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    def build(cls, settings: WorkerSettings) -> WorkerRuntime:
        store = PostgresStore(settings.database_url) if settings.database_url else InMemoryStore()
        return cls(settings=settings, store=store, calendar=load_calendar())

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> int:
        """Start the process, then run every session until the stop signal."""
        await self.store.connect()
        if not await self.store.healthcheck():
            log.error("store_unreachable", note="database did not answer the healthcheck")
            await self.store.close()
            return 1
        self._lease_holder = f"{self.settings.worker_id}:{id(self):x}"
        if not await self._acquire_lease():
            log.error(
                "lease_refused",
                holder=self._lease_holder,
                note="another worker holds the market-data lease; exiting",
            )
            await self.store.close()
            return 2
        code = 0
        try:
            while not self.stop.is_set():
                code = await self._run_today()
                await self._teardown_day()
                if code:
                    return code
                if not self.stop.is_set():
                    await self._wait_for_next_session()
        finally:
            await self._teardown_day()
            with contextlib.suppress(Exception):
                await self.store.release_lease(LEASE_NAME, self._lease_holder)
            await self.store.close()
        return code

    async def _acquire_lease(self) -> bool:
        return await self.store.acquire_lease(
            LEASE_NAME, self._lease_holder, self.settings.session_lease_seconds, now_ist()
        )

    async def _run_today(self) -> int:
        info = self.calendar.calendar_status()
        if info["stale"]:
            log.warning(
                "trading_calendar_stale", **{k: v for k, v in info.items() if v is not None}
            )
        moment = now_ist()
        phase = self.calendar.phase_at(moment)
        window = self.calendar.session(moment.date())
        if phase == SessionPhase.POST_CLOSE:
            log.info("session_already_closed", trading_date=moment.date().isoformat())
            return 0
        if window is None:
            log.info(
                "non_trading_day",
                trading_date=moment.date().isoformat(),
                next_trading_day=self.calendar.next_trading_day(moment.date()).isoformat(),
            )
            return 0
        if phase in (SessionPhase.CLOSED, SessionPhase.PRE_OPEN):
            existing = await self.store.get_session_for_date(window.trading_date)
            if existing is not None and existing.session_status == SessionStatus.FINALIZED.value:
                return 0
            log.info("waiting_for_open", opens_at=window.open_at.isoformat())
            await self._idle_until(window.open_at - timedelta(minutes=2))
        return await self._run_session()

    async def _wait_for_next_session(self) -> None:
        """Park the process until the next trading day is about to open."""
        moment = now_ist()
        next_day = self.calendar.next_trading_day(moment.date())
        target = datetime.combine(
            next_day, self.calendar.pre_open_start, tzinfo=moment.tzinfo
        ) - timedelta(minutes=15)
        log.info("idle_until_next_session", next_trading_day=next_day.isoformat())
        await self._idle_until(target)

    async def _idle_until(self, target: datetime) -> None:
        """Wait without burning CPU, still responsive to a stop signal."""
        while not self.stop.is_set():
            remaining = (to_ist(target) - now_ist()).total_seconds()
            if remaining <= 0:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=min(remaining, 30.0))

    async def _run_session(self) -> int:
        moment = now_ist()
        window = self.calendar.session(moment.date())
        if window is None:  # pragma: no cover - guarded by _run_today
            log.error("no_session_for_date", date=moment.date().isoformat())
            return 3
        self.window = window
        self.session = await self._resolve_session(window)
        instruments = await self._load_instruments()
        if not instruments:
            log.error(
                "empty_universe",
                universe=self.settings.scanner.universe,
                action_required="run `scanner-sync-instruments` before starting the worker",
            )
            return 4
        await self.store.set_session_status(self.session.id, SessionStatus.IN_PROGRESS.value)
        self._ensure_client()
        self.pipeline = self._build_pipeline(window, self.session, instruments)
        warmup = await self.pipeline.warm_up(
            now=now_ist(), rest=self._rest, fetch_historical=self._rest is not None
        )
        log.info("warmup_complete", **{k: v for k, v in warmup.items() if not isinstance(v, dict)})
        await self._start_feed(instruments)
        self._spawn_support_tasks()
        log.info(
            "session_started",
            session_id=self.session.id,
            trading_date=window.trading_date.isoformat(),
            instruments=len(instruments),
            scan_mode=self.settings.scan_mode,
        )
        await self._minute_loop(window)
        return 0

    async def _resolve_session(self, window: SessionWindow) -> SessionRow:
        """Reuse today's open session after a restart; never silently duplicate it."""
        existing = await self.store.get_session_for_date(window.trading_date)
        if existing is not None and existing.session_status != SessionStatus.FINALIZED.value:
            log.info(
                "session_recovered",
                session_id=existing.id,
                status=existing.session_status,
                initial_scan_already_ran=existing.initial_scan_completed_at is not None,
            )
            if existing.initial_scan_completed_at is not None and self.pipeline is None:
                # Marker read before the pipeline exists; the pipeline copies it
                # on construction so the initial scan is not run twice (§17.16).
                self._recovered_scan_at = existing.initial_scan_completed_at
            return existing
        return await self.store.ensure_session(window.trading_date, window.open_at, window.close_at)

    async def _load_instruments(self) -> dict[int, Instrument]:
        rows = await self.store.list_universe(universe=self.settings.scanner.universe)
        usable = [row for row in rows if row.instrument_token and row.is_active]
        if not usable:
            return {}
        coverage = await self.store.coverage(universe=self.settings.scanner.universe)
        log.info(
            "universe_loaded",
            requested=len(rows),
            subscribable=len(usable),
            coverage=f"{coverage.coverage_pct:.1f}%",
        )
        self._universe_total = len(rows)
        return {instrument.id: instrument for instrument in usable}

    def _build_pipeline(
        self,
        window: SessionWindow,
        session: SessionRow,
        instruments: dict[int, Instrument],
    ) -> ScannerPipeline:
        params = self.settings.scanner
        engine = CandleEngine(
            grace_seconds=params.candle_grace_seconds,
            session_open=window.open_at,
            session_close=window.close_at,
        )
        registry = IndicatorRegistry(
            fast_period=params.fast_ema_period,
            slow_period=params.slow_ema_period,
            volume_lookback=params.volume_lookback,
        )
        alerts = self._build_alerts()
        watchlist = WatchlistManager(
            store=self.store, params=params, alerts=alerts, simulation=not self.settings.is_live
        )
        pipeline = ScannerPipeline(
            store=self.store,
            params=params,
            calendar=self.calendar,
            session=session,
            window=window,
            instruments=instruments,
            engine=engine,
            registry=registry,
            watchlist=watchlist,
            alerts=alerts,
            is_simulated=not self.settings.is_live,
        )
        if self._recovered_scan_at is not None:
            pipeline.initial_scan_completed = True
        return pipeline

    def _build_alerts(self) -> AlertEngine:
        telegram = self.settings.telegram
        self._sender = TelegramSender(settings=telegram)
        return AlertEngine(
            store=self.store,
            params=self.settings.scanner,
            telegram_enabled=self.settings.notifications_enabled and telegram.configured,
            telegram_chat_id=telegram.chat_id,
            app_url=self.settings.app_url,
            simulation=not self.settings.is_live,
        )

    # ── feed ──────────────────────────────────────────────────────────────

    def _ensure_client(self) -> None:
        """Create the authenticated REST/auth clients once per session.

        Warm-up needs the broker's history endpoint, so these exist before the
        WebSocket starts rather than being built inside the feed.
        """
        if not self.settings.is_live or not self.settings.angel.configured:
            if self.settings.is_live:
                log.error(
                    "angel_credentials_missing",
                    note="SCAN_MODE=live but SmartAPI credentials are not configured",
                )
            return
        self._auth = SmartApiAuth(settings=self.settings.angel)
        self._rest = SmartApiRest(settings=self.settings.angel, auth=self._auth)

    async def _start_feed(self, instruments: dict[int, Instrument]) -> None:
        if not self.settings.is_live:
            log.info("feed_skipped", reason="simulation mode drives the pipeline itself")
            return
        pipeline = self.pipeline
        assert pipeline is not None and self.window is not None
        if self._auth is None:
            log.error(
                "feed_not_started",
                reason="no SmartAPI session available",
                note="the pipeline stays DISCONNECTED, which suspends signal generation",
            )
            return
        assert self._rest is not None
        tokens = sorted(
            {
                instrument.instrument_token
                for instrument in instruments.values()
                if instrument.instrument_token
            }
        )
        feed = SmartApiFeed(
            settings=self.settings.angel,
            auth=self._auth,
            resolve=pipeline.resolve_instrument_id,
            on_tick=self._on_tick,
            on_status=self._on_status,
            heartbeat_seconds=min(20.0, float(self.settings.heartbeat_seconds)),
            stale_after_seconds=float(self.settings.scanner.feed_staleness_seconds),
            reconnect_after_seconds=max(
                90.0, float(self.settings.scanner.feed_staleness_seconds) * 2
            ),
        )
        self.feed = feed
        accepted = await feed.subscribe(tokens)
        log.info(
            "feed_subscription_planned",
            requested=len(tokens),
            accepted=accepted,
            quota_remaining=feed.quota_remaining(),
        )
        self._tasks.append(asyncio.create_task(self._guard(feed.run(), "feed"), name="feed"))

    async def _on_tick(self, tick: Any) -> None:
        pipeline = self.pipeline
        if pipeline is None:
            return
        await pipeline.on_tick(tick)
        self.last_market_data_at = tick.received_at

    async def _on_status(self, status: FeedStatus, reason: str) -> None:
        pipeline = self.pipeline
        if pipeline is not None:
            await pipeline.on_feed_status(status, reason, now=now_ist())

    async def _guard(self, awaitable: Any, name: str) -> None:
        """A support loop must never take the process down silently."""
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{name}: {type(exc).__name__}: {exc}"[:300]
            log.error("loop_crashed", loop=name, error=scrub(self.last_error))
            self.stop.set()

    def _spawn_support_tasks(self) -> None:
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        self._tasks.append(asyncio.create_task(self._lease_loop(), name="lease"))
        if self._sender is not None:
            dispatcher = Dispatcher(
                store=self.store,
                sender=self._sender,
                settings=self.settings.telegram,
                worker_id=self.settings.worker_id,
            )
            self._tasks.append(
                asyncio.create_task(
                    self._guard(
                        dispatcher.run_forever(stop=self.stop, poll_seconds=5.0), "dispatcher"
                    ),
                    name="dispatcher",
                )
            )
        if self.settings.is_live:
            self._tasks.append(asyncio.create_task(self._reauth_loop(), name="reauth"))

    # ── the minute loop ───────────────────────────────────────────────────

    async def _minute_loop(self, window: SessionWindow) -> None:
        pipeline = self.pipeline
        assert pipeline is not None
        params = self.settings.scanner
        next_scan_at = window.open_at + timedelta(
            minutes=params.initial_scan_candles, seconds=params.candle_grace_seconds + 2
        )
        scan_scheduled = not pipeline.initial_scan_completed
        while not self.stop.is_set():
            now = now_ist()
            try:
                await pipeline.drain(now)
            except Exception as exc:  # noqa: BLE001 - one bad minute must not stop the day
                self.last_error = f"drain: {type(exc).__name__}: {exc}"[:300]
                log.error("minute_drain_failed", error=scrub(self.last_error))
            if scan_scheduled and now >= next_scan_at:
                scan_scheduled = False
                with contextlib.suppress(Exception):
                    summary = await pipeline.run_initial_scan(now)
                    log.info("initial_scan", counts=summary.get("counts"))
            if now >= window.close_at + timedelta(seconds=CLOSE_GRACE_SECONDS):
                await self._finalize_session(now)
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=MINUTE_LOOP_SECONDS)
        # Stopped before the close: leave the session open for recovery.
        await self._persist_partial_candles()

    async def _finalize_session(self, now: datetime) -> None:
        pipeline = self.pipeline
        if pipeline is None:
            return
        flushed = await pipeline.flush_session(now)
        log.info("session_flushed", candles=len(flushed))
        summary = await pipeline.finalize_day(now)
        log.info(
            "session_finalized",
            trading_date=self.window.trading_date.isoformat() if self.window else None,
            final_watchlist=len(summary["final_watchlist"]),
            stale_seconds=summary["stale_seconds"],
        )

    async def _persist_partial_candles(self) -> None:
        """Store forming candles as ``is_complete=False`` so recovery can rebuild."""
        pipeline = self.pipeline
        if pipeline is None:
            return
        partial = [
            candle
            for instrument_id in pipeline.instruments
            if (candle := pipeline.engine.forming_candle(instrument_id)) is not None
        ]
        if partial:
            await self.store.upsert_candles(partial)
            log.info("partial_candles_stored", count=len(partial))

    # ── support loops ─────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        pipeline = self.pipeline
        interval = float(self.settings.heartbeat_seconds)
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=interval)
            if pipeline is None or self.stop.is_set():
                continue
            try:
                subscribed = self.feed.subscription_count if self.feed else 0
                await pipeline.publish_heartbeat(
                    worker_id=self.settings.worker_id,
                    now=now_ist(),
                    subscribed_count=subscribed,
                    last_market_data_at=self.last_market_data_at,
                    last_error=self.last_error,
                    universe_total=getattr(self, "_universe_total", len(pipeline.instruments)),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat_failed", error=scrub(f"{type(exc).__name__}: {exc}"))

    async def _lease_loop(self) -> None:
        ttl = self.settings.session_lease_seconds
        interval = max(5.0, ttl / 3.0)
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=interval)
            if self.stop.is_set():
                continue
            if not await self.store.renew_lease(LEASE_NAME, self._lease_holder, ttl, now_ist()):
                log.error(
                    "lease_lost", holder=self._lease_holder, reason="another replica took over"
                )
                self.stop.set()
                return

    async def _reauth_loop(self) -> None:
        """Renew the SmartAPI JWT ahead of expiry instead of after a 401."""
        minutes = self.settings.angel.reauth_minutes
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=minutes * 60.0)
            if self.stop.is_set() or self._auth is None:
                return
            try:
                await self._auth.force_reauth()
                log.info("feed_token_renewed")
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"reauth: {type(exc).__name__}"[:200]
                log.warning("reauth_failed", error=scrub(str(exc)))

    async def _teardown_day(self) -> None:
        """Stop every per-session task and release per-session clients.

        Idempotent: the process-wide shutdown path calls this again after the
        daily loop has already called it.
        """
        if self.feed is not None:
            self.feed.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=SHUTDOWN_FLUSH_SECONDS)
        self._tasks.clear()
        if self.pipeline is not None and self.session is not None:
            # A final health row is more useful than a fake feed transition:
            # the queue depth and watchlist counts are what an operator checks
            # after a restart.
            with contextlib.suppress(Exception):
                await self.pipeline.publish_heartbeat(
                    worker_id=self.settings.worker_id,
                    now=now_ist(),
                    subscribed_count=self.feed.subscription_count if self.feed else 0,
                    last_market_data_at=self.last_market_data_at,
                    last_error=self.last_error,
                    universe_total=self._universe_total,
                )
        for closer in (self._rest, self._auth, self._sender):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        self.feed = None
        self.pipeline = None
        self.session = None
        self.window = None
        self._auth = None
        self._rest = None
        self._sender = None
        self._recovered_scan_at = None
        self.last_market_data_at = None
        self.last_error = None


# ── process entry ──────────────────────────────────────────────────────────


def install_signal_handlers(runtime: WorkerRuntime) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, runtime.stop.set)


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scanner-worker", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("live", "simulation"), default=None)
    parser.add_argument("--summary", action="store_true", help="print the config and exit")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.mode:
        from dataclasses import replace

        settings = replace(settings, scan_mode=args.mode)
    configure_logging(settings.log_level, fmt=settings.log_format)
    log.info("worker_configuration", **scrub(safe_summary(settings)))
    if args.summary:
        return 0
    runtime = WorkerRuntime.build(settings)
    install_signal_handlers(runtime)
    return await runtime.run()


def run(argv: list[str] | None = None) -> int:
    """Console entry point (``scanner-worker``)."""
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        log.warning("worker_interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())

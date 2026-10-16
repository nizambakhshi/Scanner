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
import hashlib
import signal
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .alerts import AlertEngine
from .analytics.breakouts import StrategiesConfig, parse_strategies, unknown_strategies_in
from .analytics.indicators import IndicatorRegistry
from .analytics.rsi import ENTRY, OVERBOUGHT
from .config import (
    ConfigError,
    ScannerParams,
    TelegramSettings,
    WorkerSettings,
    load_settings,
    safe_summary,
)
from .dispatcher import Dispatcher
from .logging_setup import configure_logging, get_logger, scrub
from .marketdata.candle_builder import CandleEngine
from .marketdata.sectors import SECTOR_FLUSH_SECONDS, SectorTracker
from .marketdata.smartapi_auth import SmartApiAuth
from .marketdata.smartapi_rest import SmartApiRest
from .marketdata.smartapi_ws import SmartApiFeed
from .models import (
    FeedStatus,
    IndexTick,
    Instrument,
    RsiState,
    SessionStatus,
    SignalType,
    WorkerHeartbeat,
)
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
#: Retention re-runs a few times per session; the startup purge is the one that
#: actually matters, this only trims what a multi-day uptime accumulates.
RETENTION_LOOP_SECONDS = 6 * 3600.0
#: How often a parked worker refreshes its heartbeat row. Between sessions
#: nothing else writes it, so without this the dashboard keeps displaying the
#: last session's beat — STREAMING with the old subscription counts — long
#: after the process stopped feeding (observed 2026-10-12: the board showed a
#: worker that had died at 13:24 as live until the next open).
IDLE_HEARTBEAT_SECONDS = 60.0
#: How often the running worker re-reads the two ``ops_settings`` documents the
#: /alerts page writes. A minute is short enough that saving a Telegram token or
#: flipping a strategy feels immediate, and long enough that the poll is a rounding
#: error next to the retention loop it lives beside.
ALERT_SETTINGS_POLL_SECONDS = 60.0


def _rsi_message(state: RsiState) -> str:
    """The one-line why for a two-timeframe RSI alert (bias is ENTRY or OVERBOUGHT)."""
    condition = "below 40" if state.rsi_bias == ENTRY else "above 60"
    daily = f"{state.daily_rsi:.1f}" if state.daily_rsi is not None else "n/a"
    weekly = f"{state.weekly_rsi:.1f}" if state.weekly_rsi is not None else "n/a"
    return f"Daily RSI {daily} · weekly RSI {weekly} — both {condition}"


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
    #: The params this session runs on. Starts as the env-derived defaults and
    #: is replaced by the newest ``scanner_configs`` row when the session opens
    #: (see :meth:`_adopt_scanner_config`) — the dashboard's edit path writes
    #: that table, so this field is what makes a saved config actually run.
    params: ScannerParams = field(init=False)
    #: The armed level-break strategies. Env has none: the shipped defaults stand
    #: until ``ops_settings['alert_strategies']`` says otherwise.
    strategies: StrategiesConfig = field(init=False, default_factory=StrategiesConfig)
    #: Telegram credentials this process sends with. Starts from the environment
    #: and is overridden by ``ops_settings['telegram']`` when the dashboard holds them.
    telegram: TelegramSettings = field(init=False)
    _telegram_fingerprint: str = field(default="", repr=False)
    _telegram_source: str = field(default="env", repr=False)
    _strategies_source: str = field(default="defaults", repr=False)
    _config_version: int | None = field(default=None, repr=False)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list, repr=False)
    _lease_holder: str = field(default="", repr=False)
    _auth: SmartApiAuth | None = field(default=None, repr=False)
    _rest: SmartApiRest | None = field(default=None, repr=False)
    _sender: TelegramSender | None = field(default=None, repr=False)
    _alerts: AlertEngine | None = field(default=None, repr=False)
    _universe_total: int = field(default=0, repr=False)
    _recovered_scan_at: datetime | None = field(default=None, repr=False)
    _sectors: SectorTracker | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.params = self.settings.scanner
        self.telegram = self.settings.telegram

    @property
    def telegram_active(self) -> bool:
        """Send-eligibility judged on the ADOPTED credentials, not the environment's.

        ``settings.notifications_enabled`` only sees env keys; a token saved on the
        dashboard lives in ``self.telegram`` after adoption, and the env can stay empty.
        """
        return self.telegram.configured and not self.settings.simulation_label

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
        # A previous process died between claim and update; those IN_FLIGHT rows
        # would never be retried. Sweeping at boot is safe because the lease we
        # just took means this is the only sender.
        swept = await self.store.requeue_in_flight_deliveries()
        if swept:
            log.warning("in_flight_deliveries_requeued", count=swept)
        code = 0
        try:
            while not self.stop.is_set():
                code = await self._run_today()
                await self._teardown_day()
                if code:
                    return code
                if not self.stop.is_set():
                    await self._idle_maintenance()
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
        """Wait without burning CPU, still responsive to a stop signal.

        While parked the worker keeps saying so on the heartbeat row: a process
        that finished its session must overwrite the last STREAMING beat with
        an honest idle one, otherwise the dashboard presents it as live until
        the next open (§17: never let stale data pose as current).
        """
        await self._safe_idle_beat()
        last_beat = time.monotonic()
        while not self.stop.is_set():
            remaining = (to_ist(target) - now_ist()).total_seconds()
            if remaining <= 0:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=min(remaining, 30.0))
            if time.monotonic() - last_beat >= IDLE_HEARTBEAT_SECONDS:
                await self._safe_idle_beat()
                last_beat = time.monotonic()

    async def _write_idle_heartbeat(self) -> None:
        """The parked truth: no feed, nothing subscribed, nothing monitored.

        ``last_market_data_at`` is deliberately cleared — this process has seen
        no ticks while parked, and the health page renders that field next to
        the row's own age, so "never · updated moments ago" is the honest
        pairing rather than yesterday's tick next to a fresh timestamp.
        """
        await self.store.upsert_heartbeat(
            WorkerHeartbeat(
                worker_id=self.settings.worker_id,
                status=FeedStatus.DISCONNECTED.value,
                feed_status=FeedStatus.DISCONNECTED,
                scan_mode=self.settings.scan_mode,
                simulation_label=self.settings.scan_mode == "simulation",
            )
        )

    async def _safe_idle_beat(self) -> None:
        try:
            await self._write_idle_heartbeat()
        except Exception as exc:  # noqa: BLE001 — a heartbeat blip must not kill the wait loop
            log.warning("idle_heartbeat_failed", error=scrub(f"{type(exc).__name__}: {exc}"))

    async def _run_session(self) -> int:
        moment = now_ist()
        window = self.calendar.session(moment.date())
        if window is None:  # pragma: no cover - guarded by _run_today
            log.error("no_session_for_date", date=moment.date().isoformat())
            return 3
        self.window = window
        self.session = await self._resolve_session(window)
        await self._adopt_scanner_config()
        await self._adopt_alert_settings()
        instruments = await self._load_instruments()
        if not instruments:
            log.error(
                "empty_universe",
                universe=self.params.universe,
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
        await self._recover_breakout_levels()
        await self._start_feed(instruments)
        self._spawn_support_tasks()
        log.info(
            "session_started",
            session_id=self.session.id,
            trading_date=window.trading_date.isoformat(),
            instruments=len(instruments),
            scan_mode=self.settings.scan_mode,
            scanner_config_version=self._config_version,
        )
        await self._emit_rsi_transitions(self.session, instruments)
        await self._minute_loop(window)
        return 0

    async def _recover_breakout_levels(self) -> None:
        """Rebuild today's levels when today's tape is already in the database.

        A fresh 09:15 open has nothing to recover. A worker that comes up at 11:40 —
        after a crash, a deploy, or a Mini that slept — would otherwise spend the
        rest of the day with no opening range and a day high made of afternoon
        candles, so the first genuine new high after the restart would be judged
        against the wrong level. The minutes are already stored; replaying them
        through the detector rebuilds exactly what the live tape would have built,
        *silently* — those alerts were judged when they happened, and re-paging the
        operator for 09:20 at 11:40 is not a recovery, it is spam.
        """
        pipeline = self.pipeline
        if pipeline is None or self.session is None:
            return
        try:
            stored_candles = await self.store.count_candles_for_session(self.session.id)
        except Exception as exc:  # noqa: BLE001 — a counting hiccup must not stop the day
            log.warning(
                "breakout_recovery_skipped",
                error=scrub(f"{type(exc).__name__}: {exc}")[:200],
                note="levels start from the live tape; a mid-day restart sees no ORB today",
            )
            return
        if stored_candles == 0:
            log.info("breakout_levels_seeded_from_live_tape", candles_in_session=0)
            return
        try:
            summary = await pipeline.recover_breakouts()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "breakout_recovery_failed",
                error=scrub(f"{type(exc).__name__}: {exc}")[:200],
                action_required="level breaks may misjudge today's first new high",
            )
            return
        log.info("breakout_recovery_complete", candles_in_session=stored_candles, **summary)

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

    async def _adopt_scanner_config(self) -> None:
        """Run this session on the newest ``scanner_configs`` row, if one exists.

        The dashboard's config page saves versioned rows into that table (every
        save bumps ``config_version``); adopting it here is what makes "save"
        mean something — universe filter, qualification thresholds and
        watchlist behaviour all come from :attr:`params` for the whole session.
        The env-derived params remain only the fallback for a missing or
        invalid row, and every decline is logged, never silent.

        Read once per session: a mid-session edit waits for the next session
        start, because the indicator engines are constructed with the EMA
        periods and volume lookback they will keep all day (§10.4 — the row is
        versioned precisely so historical signals stay interpretable).
        """
        try:
            row = await self.store.active_config()
        except Exception as exc:  # noqa: BLE001 — a config read must never kill the day
            log.warning(
                "scanner_config_read_failed",
                error=scrub(f"{type(exc).__name__}: {exc}")[:200],
                note="running on env-derived defaults; the saved row was unreadable",
            )
            return
        if row is None:
            log.info(
                "scanner_config_fallback",
                source="env-defaults",
                note="no scanner_configs row exists; the session runs on the worker's built-in defaults",
            )
            return
        # The ₹ cap on tracked names is an operator-level rule carried by the
        # environment, not a per-universe knob the dashboard edits: the
        # schema-bound config row (explicit columns) cannot express it yet, so
        # adoption *merges* it in rather than silently reverting the env value
        # to the dataclass default. Everything else comes from the row.
        adopted = replace(row.params, max_price=self.params.max_price)
        try:
            adopted.validate()
        except ConfigError as exc:
            log.error(
                "scanner_config_invalid",
                version=row.config_version,
                error=scrub(str(exc))[:200],
                action_required="save a valid config from the dashboard; running on env defaults until then",
            )
            return
        self.params = adopted
        self._config_version = row.config_version
        log.info(
            "scanner_config_adopted",
            source="scanner_configs",
            version=row.config_version,
            universe=adopted.universe,
            fast_ema_period=adopted.fast_ema_period,
            slow_ema_period=adopted.slow_ema_period,
            volume_lookback=adopted.volume_lookback,
            relative_volume_threshold=adopted.relative_volume_threshold,
            alert_cooldown_minutes=adopted.alert_cooldown_minutes,
            removal_confirm_candles=adopted.removal_confirm_candles,
            max_price=adopted.max_price,
        )

    async def _adopt_alert_settings(self) -> dict[str, Any]:
        """Arm the detectors and the Telegram credentials from ``ops_settings``.

        Two documents the /alerts page owns, read the same way the retention knob
        is read: the dashboard is the writer, the worker is the reader, and a value
        this process cannot parse is *reported* rather than quietly replaced by a
        default — a page that says "armed" while the detector never runs is the
        failure mode this feature cannot have.

        * ``alert_strategies`` — the three level-break strategies and their knobs.
          Malformed → the previously adopted document stays in force.
        * ``telegram`` — bot token and chat id. The environment remains the
          fallback, so a database that has never seen the page still delivers what
          ``TELEGRAM_BOT_TOKEN`` was wired for. The token itself is never logged and
          never leaves this method; only a hash of it is kept, to notice a change.
        """
        summary: dict[str, Any] = {
            "strategies": self._strategies_source,
            "telegram": self._telegram_source,
        }
        try:
            document = await self.store.get_ops_setting("alert_strategies")
        except Exception as exc:  # noqa: BLE001 — an unreadable knob must not stop the day
            log.warning(
                "alert_strategies_read_failed",
                error=scrub(f"{type(exc).__name__}: {exc}")[:200],
                note="detectors stay on the configuration already in force",
            )
            document = None
        if document is not None:
            try:
                parsed = parse_strategies(document)
            except ValueError as exc:
                log.error(
                    "alert_strategies_invalid",
                    error=scrub(str(exc))[:300],
                    action_required="re-save /alerts; the stored document cannot be obeyed",
                )
            else:
                unknown = unknown_strategies_in(document)
                if unknown:
                    log.warning("alert_strategies_unknown", names=unknown)
                if parsed != self.strategies:
                    self.strategies = parsed
                    self._strategies_source = "ops_settings"
                    log.info("alert_strategies_adopted", source="ops_settings", **parsed.summary())
        else:
            log.info(
                "alert_strategies_fallback",
                source="built-in-defaults",
                note="no ops_settings['alert_strategies'] row; the shipped defaults run",
                **self.strategies.summary(),
            )
        try:
            credentials = await self.store.get_ops_setting("telegram")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "telegram_setting_read_failed",
                error=scrub(f"{type(exc).__name__}: {exc}")[:200],
                note="staying on the environment credentials",
            )
            credentials = None
        if credentials:
            token = str(credentials.get("bot_token") or "").strip()
            chat_id = str(credentials.get("chat_id") or "").strip()
            if token and chat_id:
                # `enabled` is the page's master switch; a saved token that the
                # operator switched off must not page the phone just because it
                # is still sitting in the row.
                enabled = credentials.get("enabled")
                self.telegram = replace(
                    self.settings.telegram, bot_token=token, chat_id=chat_id
                )
                if isinstance(enabled, bool) and not enabled:
                    # Blanked rather than left configured: `configured` is what the
                    # alert engine gates on, so switching off must switch off.
                    self.telegram = replace(self.telegram, bot_token="", chat_id="")
                self._telegram_source = "ops_settings"
            else:
                # Two different states share this branch, and only one of them is a
                # problem. A row with *one* value is a save that went off halfway —
                # the operator must see it. A row with neither is the placeholder
                # migration 0010 seeds, i.e. a database that has never been used by
                # the page; this method runs every 60 s, so warning about that would
                # print several hundred lines a day and bury the half-saved case.
                if token or chat_id:
                    log.warning(
                        "telegram_row_incomplete",
                        note="bot_token and chat_id must both be present; the "
                        "environment credentials take over",
                        has_token=bool(token),
                        has_chat_id=bool(chat_id),
                    )
                self._release_saved_credentials()
        else:
            # The row was deleted since the last read: the environment is the
            # credential source again rather than a stale copy of the old page.
            self._release_saved_credentials()
        summary["telegram"] = self._telegram_source
        summary["strategies"] = self._strategies_source
        summary["telegram_configured"] = self.telegram.configured
        self._apply_alert_settings()
        return summary

    def _release_saved_credentials(self) -> None:
        """Hand the credentials back to the environment.

        A row without a complete pair — cleared with the page's *Clear stored*
        button, deleted outright, or the empty placeholder migration 0010 seeds — is
        not a credential source. It must also not leave this process holding the
        token that row supplied a minute ago: a dashboard that stops showing a pair
        while the worker keeps paging it is precisely the half-done state this
        method exists to prevent, and it is why the deleted-row case and the
        blanked-row case take the same exit.
        """
        self.telegram = self.settings.telegram
        self._telegram_source = "env"

    def _apply_alert_settings(self) -> None:
        """Push whatever was just adopted into the live objects, without a restart.

        The sender and the alert engine hold the credentials they queue with, so a
        token saved mid-session takes effect on the next queued message rather than
        the next open. Levels are deliberately preserved by ``reconfigure``: an
        opening range is a fact about the morning, not about the configuration.
        """
        pipeline = self.pipeline
        if pipeline is not None:
            pipeline.reconfigure_breakouts(self.strategies)
        # Over token *and* chat id: pasting a different chat id is a change that has
        # to reach the sender, and neither value can be logged, so neither is echoed.
        fingerprint = hashlib.sha256(
            f"{self.telegram.bot_token}\n{self.telegram.chat_id}".encode()
        ).hexdigest()[:12]
        if fingerprint == self._telegram_fingerprint:
            return
        self._telegram_fingerprint = fingerprint
        if self._sender is not None:
            self._sender.settings = self.telegram
        if self._alerts is not None:
            self._alerts.telegram_enabled = self.telegram_active
            self._alerts.telegram_chat_id = self.telegram.chat_id
        # Only the fingerprint travels: a rotated token must be visible in the log
        # without the token ever appearing in it.
        log.info(
            "telegram_credentials_applied",
            source=self._telegram_source,
            fingerprint=fingerprint,
            configured=self.telegram.configured,
        )

    async def _alert_settings_loop(self) -> None:
        """Re-read the alert documents, so a save on the page needs no restart."""
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.stop.wait(), timeout=ALERT_SETTINGS_POLL_SECONDS
                )
            if self.stop.is_set():
                return
            try:
                await self._adopt_alert_settings()
            except Exception as exc:  # noqa: BLE001 — the tape outranks a config hiccup
                log.warning(
                    "alert_settings_refresh_failed",
                    error=scrub(f"{type(exc).__name__}: {exc}")[:200],
                )

    async def _load_instruments(self) -> dict[int, Instrument]:
        rows = await self.store.list_universe(universe=self.params.universe)
        usable = [row for row in rows if row.instrument_token and row.is_active]
        if not usable:
            return {}
        coverage = await self.store.coverage(universe=self.params.universe)
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
        params = self.params
        engine = CandleEngine(
            grace_seconds=params.candle_grace_seconds,
            session_open=window.open_at,
            session_close=window.close_at,
        )
        registry = IndicatorRegistry(
            fast_period=params.fast_ema_period,
            slow_period=params.slow_ema_period,
            volume_lookback=params.volume_lookback,
            macd_fast_period=params.macd_fast_period,
            macd_slow_period=params.macd_slow_period,
            macd_signal_period=params.macd_signal_period,
            atr_period=params.atr_period,
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
            strategies=self.strategies,
        )
        if self._recovered_scan_at is not None:
            pipeline.initial_scan_completed = True
        return pipeline

    def _build_alerts(self) -> AlertEngine:
        # ``self.telegram``, not ``self.settings.telegram``: by the time a pipeline is
        # built the runtime has already adopted whatever the dashboard saved, and the
        # environment is only the fallback it started from.
        telegram = self.telegram
        self._sender = TelegramSender(settings=telegram)
        self._alerts = AlertEngine(
            store=self.store,
            params=self.params,
            telegram_enabled=self.telegram_active,
            telegram_chat_id=telegram.chat_id,
            app_url=self.settings.app_url,
            simulation=not self.settings.is_live,
        )
        return self._alerts

    async def _emit_rsi_transitions(
        self, session: SessionRow, instruments: dict[int, Instrument]
    ) -> None:
        """Two-timeframe RSI alerts, one per bias *change*, at session start.

        The operator rule, as of 2026-10-15: daily and weekly RSI both **below 40**
        → ENTRY (the green, actionable side), both **above 60** → OVERBOUGHT (red),
        the 40–60 band → NEUTRAL. The pair is recomputed nightly by
        ``scanner-sync-daily`` into ``stock_details``; ``last_alert_bias`` is the bias
        this worker last acted on. Equal → a standing state, nothing to say. A NULL
        ``last_alert_bias`` is the first sighting since the feature shipped: the bias
        is adopted silently, so rollout day does not page the operator once per
        instrument. Only a transition *into* ENTRY or OVERBOUGHT alerts; a transition
        into NEUTRAL is written quietly as the new baseline so rotating back out of
        the band later reads as a real change. The (type, instrument, session)
        idempotency key makes a mid-session restart unable to alert twice, and
        ``cooldown_minutes=0`` keeps this horizon out of the intraday rule's window:
        the nightly pair and the five-minute detector raise the same two types from
        different measurements, and neither one gets to mute the other.
        """
        engine = self._alerts
        if engine is None:
            return
        try:
            states = await self.store.list_rsi_states()
        except Exception as exc:  # noqa: BLE001 — a details hiccup must not stop the day
            log.warning("rsi_states_read_failed", error=scrub(f"{type(exc).__name__}: {exc}"))
            return
        counts = {"entry": 0, "overbought": 0, "neutral": 0, "baselined": 0}
        for state in states:
            instrument = instruments.get(state.instrument_id)
            if instrument is None or state.rsi_bias is None:
                # Outside today's universe, or a pair the sync could not judge.
                continue
            if state.last_alert_bias == state.rsi_bias:
                continue
            try:
                if state.last_alert_bias is None:
                    counts["baselined"] += 1
                elif state.rsi_bias in (ENTRY, OVERBOUGHT):
                    entry = state.rsi_bias == ENTRY
                    await engine.raise_alert(
                        signal_type=SignalType.RSI_ENTRY if entry else SignalType.RSI_OVERBOUGHT,
                        instrument_id=state.instrument_id,
                        session_id=session.id,
                        event_timestamp=now_ist(),
                        symbol=instrument.symbol,
                        company_name=instrument.company_name,
                        exchange=instrument.exchange,
                        previous_close=instrument.previous_close,
                        reasons=(
                            ("daily rsi below 40", "weekly rsi below 40")
                            if entry
                            else ("daily rsi above 60", "weekly rsi above 60")
                        ),
                        message=_rsi_message(state),
                        cooldown_minutes=0,
                    )
                    counts["entry" if entry else "overbought"] += 1
                else:
                    counts["neutral"] += 1
                await self.store.set_last_alert_bias(state.instrument_id, state.rsi_bias)
            except Exception as exc:  # noqa: BLE001 — one instrument must not sink the rest
                log.warning(
                    "rsi_transition_failed",
                    symbol=instrument.symbol,
                    error=scrub(f"{type(exc).__name__}: {exc}"),
                )
        log.info("rsi_transitions_evaluated", **counts)

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
        # Sector indices ride the same socket and count against the same token
        # quota, so they subscribe before the quota check below — a refused
        # stock token is a coverage problem, a refused index token is a tile
        # that silently stays empty.
        index_rows = await self.store.list_sector_indices()
        index_tokens = {row.instrument_token: row.index_symbol for row in index_rows}
        self._sectors = SectorTracker()
        feed = SmartApiFeed(
            settings=self.settings.angel,
            auth=self._auth,
            resolve=pipeline.resolve_instrument_id,
            on_tick=self._on_tick,
            on_status=self._on_status,
            index_tokens=index_tokens,
            on_index_tick=self._on_index_tick,
            heartbeat_seconds=min(20.0, float(self.settings.heartbeat_seconds)),
            stale_after_seconds=float(self.params.feed_staleness_seconds),
            reconnect_after_seconds=max(
                90.0, float(self.params.feed_staleness_seconds) * 2
            ),
        )
        self.feed = feed
        accepted = await feed.subscribe([*tokens, *index_tokens])
        log.info(
            "feed_subscription_planned",
            requested=len(tokens) + len(index_tokens),
            stocks=len(tokens),
            indices=len(index_tokens),
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

    async def _on_index_tick(self, tick: IndexTick) -> None:
        if self._sectors is not None:
            self._sectors.on_tick(tick)

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
        if self._sectors is not None:
            self._tasks.append(
                asyncio.create_task(self._sector_flush_loop(), name="sector-flush")
            )
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="retention"))
        self._tasks.append(
            asyncio.create_task(self._alert_settings_loop(), name="alert-settings")
        )
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
        params = self.params
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
                # Ageing sweep here, not at the open: this DELETE is the second
                # most expensive statement in the database (3.1 s per call on the
                # live pooler, §0009) and at 09:15 it competes with the warm-up
                # read and the first scans of the day. After the close the only
                # reader is the dashboard.
                await self._purge_old_data()
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
                await self._auth.force_reauth("scheduled_renewal")
                log.info("feed_token_renewed")
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"reauth: {type(exc).__name__}"[:200]
                log.warning("reauth_failed", error=scrub(str(exc)))

    async def _idle_maintenance(self) -> None:
        """Age the hot tables while the market sleeps.

        `_run_today` returns 0 on every path that does not run a session — the
        evening after a finalized day, a holiday, a weekend — and this is the
        one hook all of them share. A process that boots at 22:00 therefore
        purges at 22:00, and a process that has been parked since this morning's
        close finds nothing left to delete: `_purge_old_data` logs only when a
        DELETE actually removed rows.
        """
        if self._market_is_live(now_ist()):
            return
        await self._purge_old_data()

    async def _retention_days(self) -> int | None:
        """The ageing window: the dashboard's saved value, or the env default.

        The dashboard owns this knob (``ops_settings['retention'].days``); the
        worker reads it at purge time rather than caching it at startup, so a
        change on the dashboard applies without a restart. ``None`` means keep
        everything: the dashboard writes 0 for a database that is meant to
        accumulate history (the staging install on the Mini), and the purge
        then skips. Anything unreadable still falls back to ``RETENTION_DAYS``
        so a broken setting can never silently stop ageing on its own.
        """
        fallback = int(self.settings.retention_days)
        try:
            setting = await self.store.get_ops_setting("retention")
        except Exception as exc:  # noqa: BLE001 — a missing table must not stop the purge
            log.warning("retention_setting_unreadable", error=scrub(f"{type(exc).__name__}: {exc}"))
            return fallback
        raw = setting.get("days") if setting else None
        try:
            days = int(raw) if raw is not None else fallback  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback
        if days <= 0:
            return None
        return min(30, days)

    async def _purge_old_data(self) -> None:
        """Delete candles and snapshots past retention; log what actually went.

        A ``None`` window is an explicit operator choice (0 on the dashboard),
        not an error: the sweep is skipped and says so, once per run, so the
        worker log alone can tell "keeping history" from "purge crashed".
        """
        days = await self._retention_days()
        if days is None:
            log.info("retention_keep_forever", note="ops window is 0; nothing is deleted")
            return
        try:
            counts = await self.store.purge_older_than(days, now=now_ist())
        except Exception as exc:  # noqa: BLE001 — retention must never stop the session
            log.warning("retention_purge_failed", error=scrub(f"{type(exc).__name__}: {exc}"))
            return
        if any(counts.values()):
            log.info("retention_purge", retention_days=days, deleted=counts)

    async def _retention_loop(self) -> None:
        """Safety net for a session that ran long or restarted mid-day.

        The scheduled sweep is the one after the close (§0009); this tick exists
        so a worker that never reaches a close still ages its tables. It stays
        quiet while the market is open — the purge is not allowed to compete
        with the live scan.
        """
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=RETENTION_LOOP_SECONDS)
            if self.stop.is_set():
                continue
            if self._market_is_live(now_ist()):
                log.info("retention_deferred", reason="market session is live")
                continue
            await self._purge_old_data()

    def _market_is_live(self, moment: datetime) -> bool:
        """True from pre-open to close — the window in which the dashboard is
        reading candles and the scanner is writing them."""
        if self.window is None:
            return False
        phase = self.calendar.phase_at(moment)
        return phase in (SessionPhase.PRE_OPEN, SessionPhase.ACTIVE)

    async def _sector_flush_loop(self) -> None:
        """Push the tracker's newest-wins buffer into ``sector_quotes`` on a cadence."""
        tracker = self._sectors
        assert tracker is not None  # spawned only when the tracker exists
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=SECTOR_FLUSH_SECONDS)
            if self.stop.is_set():
                continue
            rows = tracker.drain()
            if not rows:
                continue
            try:
                await self.store.upsert_sector_quotes(rows)
            except Exception as exc:  # noqa: BLE001 — the next tick re-dirties the row
                log.warning("sector_flush_failed", error=scrub(f"{type(exc).__name__}: {exc}"))

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
        # End-of-day honesty: the pipeline beat above still carries the last
        # feed status; overwrite it with the parked state so an evening glance
        # reads DISCONNECTED, not STREAMING.
        with contextlib.suppress(Exception):
            await self._write_idle_heartbeat()
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
        self._alerts = None
        self._recovered_scan_at = None
        self._sectors = None
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

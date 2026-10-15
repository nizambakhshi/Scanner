"""The all-day scanning pipeline: ticks → candles → indicators → watchlist.

This is the loop the master spec describes in §17 items 8–14, kept free of
transport and database details so it can be driven by the live WebSocket, by
the simulator, or by a unit test with the same code path.

Ordering rules that the correctness of the whole system depends on:

* a candle is processed **once**, when time has passed its boundary plus the
  grace period — never because a later tick happened to arrive (§7.3);
* candles are persisted before their indicator snapshot (foreign key) and the
  snapshot before the watchlist transition that references it;
* session VWAP only ever sees candles of the current session; EMA and volume
  history may be seeded from earlier sessions (§6, §8.4, §9.1);
* feed problems pause signal generation; they never fabricate data (§7.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .alerts import AlertEngine
from .analytics.antichase import AntiChasePolicy, AntiChaseReport, MarketContext
from .analytics.breakouts import BreakoutEvent, BreakoutTracker, StrategiesConfig
from .analytics.extension import ExtensionClassifier, ExtensionMeasures
from .analytics.indicators import IndicatorRegistry, IndicatorValues
from .analytics.levels import LevelBook
from .analytics.readiness import READINESS_ENTRY_READY, ReadinessInputs, classify_readiness
from .analytics.scanner import ScanContext, Scanner
from .analytics.score import ScoreInputs, compute_score
from .analytics.setups import ReclaimCandidate, RetestCandidate, SetupTracker
from .config import ScannerParams
from .logging_setup import get_logger
from .marketdata.candle_builder import CandleEngine
from .models import (
    Candle,
    FeedStatus,
    IndicatorSnapshot,
    Instrument,
    SignalType,
    Tick,
    WatchlistState,
    WorkerHeartbeat,
)
from .persistence.store import ScannerStore, SessionRow
from .timeutils import (
    SESSION_CANDLE_SLOTS,
    SessionWindow,
    TradingCalendar,
    to_ist,
)
from .watchlist import WatchlistManager

log = get_logger(__name__)

OUTAGE_STATUSES = {FeedStatus.STALE, FeedStatus.ERROR, FeedStatus.DISCONNECTED}
HEALTHY_STATUSES = {FeedStatus.STREAMING, FeedStatus.CONNECTED}


@dataclass
class OutageEpisode:
    """One continuous period of unusable market data (deduplicated alerting)."""

    episode_id: str
    started_at: datetime
    status: FeedStatus
    reason: str
    stale_seconds: float = 0.0
    ended_at: datetime | None = None


@dataclass
class PipelineCounters:
    candles_finalized: int = 0
    snapshots_written: int = 0
    signals_raised: int = 0
    ticks_seen: int = 0
    warm_instruments: int = 0
    outages: int = 0
    stale_seconds_total: float = 0.0
    #: Level breaks the detectors found, and how many of them survived idempotency
    #: and cooldown. The gap between the two is the operator's evidence that a
    #: strategy is running and being suppressed, rather than not running at all.
    breakouts_detected: int = 0
    breakouts_alerted: int = 0
    #: Breakout-retests that reclaimed their level (worker.analytics.setups).
    #: Observations only in this phase — no alert, no row — so the counter is the
    #: evidence that the watch runs and finds its shapes, or honestly finds none.
    retests_detected: int = 0
    #: VWAP reclaims detected — the second setup of the same tracker, on the same
    #: evidence rule as ``retests_detected``.
    reclaims_detected: int = 0
    #: Candidates the anti-chase policy did not clear (worker.analytics.antichase).
    #: Zero while the operator's limits are unset — the count is the suppression
    #: evidence that silence cannot give, and it turns nonzero the moment a limit
    #: is configured and a chase walks into it.
    antichase_blocked: int = 0
    #: Candidates that went through the Stage 2/3 observation ensemble (the
    #: audit's Phase B): extension verdict, readiness state, §6 score — measured
    #: and logged, never alerted and never persisted until their phases land.
    candidates_judged: int = 0
    #: Candidates the readiness machine would call ENTRY READY. The number the
    #: alert phase will read: zero alerts is not the same as zero readiness.
    entry_ready_detected: int = 0
    #: Candidates the extension classifier measured at or beyond an armed
    #: limit — zero under the shipped defaults, and honest evidence the moment
    #: an operator document arms one.
    extension_extended_detected: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "candles_finalized": self.candles_finalized,
            "snapshots_written": self.snapshots_written,
            "signals_raised": self.signals_raised,
            "ticks_seen": self.ticks_seen,
            "warm_instruments": self.warm_instruments,
            "outages": self.outages,
            "stale_seconds_total": round(self.stale_seconds_total, 1),
            "breakouts_detected": self.breakouts_detected,
            "breakouts_alerted": self.breakouts_alerted,
            "retests_detected": self.retests_detected,
            "reclaims_detected": self.reclaims_detected,
            "antichase_blocked": self.antichase_blocked,
            "candidates_judged": self.candidates_judged,
            "entry_ready_detected": self.entry_ready_detected,
            "extension_extended_detected": self.extension_extended_detected,
        }


@dataclass
class ScannerPipeline:
    """Stateful orchestration for one trading session."""

    store: ScannerStore
    params: ScannerParams
    calendar: TradingCalendar
    session: SessionRow
    window: SessionWindow
    instruments: dict[int, Instrument]
    engine: CandleEngine
    registry: IndicatorRegistry
    watchlist: WatchlistManager
    alerts: AlertEngine | None = None
    is_simulated: bool = False
    feed_status: FeedStatus = FeedStatus.DISCONNECTED
    counters: PipelineCounters = field(default_factory=PipelineCounters)
    initial_scan_completed: bool = False
    #: The level-break strategies the operator armed (`ops_settings['alert_strategies']`,
    #: :func:`worker.analytics.breakouts.parse_strategies`). Supplied by the runtime at
    #: session start; absent means the shipped defaults.
    strategies: StrategiesConfig | None = field(default=None, repr=False)
    #: Runs the day-break / opening-range / VWAP detectors over every candle of
    #: the whole subscribed universe, not only the watchlist.
    breakouts: BreakoutTracker | None = field(default=None, repr=False)
    #: Watches the ORB breaks the detector above raises for a retest of the level
    #: (:class:`worker.analytics.setups.SetupTracker`). Observation only in this
    #: phase: the pipeline feeds it so the watch is exercised end to end, and the
    #: phases that score and alert on its candidates land next.
    setups: SetupTracker | None = field(default=None, repr=False)
    #: The anti-chase policy every candidate from the tracker is handed to
    #: (:class:`worker.analytics.antichase.AntiChasePolicy`). Inert until the
    #: operator's limits exist (audit D7): with no limits configured every gate
    #: reports ``skip``, every candidate clears, and the verdicts go to the log.
    antichase: AntiChasePolicy | None = field(default=None, repr=False)
    #: The Stage 2/3 observation ensemble (the audit's Phase B): the four-way
    #: chase classifier — every limit unset, so it measures without judging —
    #: feeding the readiness machine and the §6 score. Judged per candidate and
    #: read by the log and the counters only; alerts and persistence wait for
    #: their own phases.
    extension: ExtensionClassifier | None = field(default=None, repr=False)
    #: Per-instrument level inventories (worker.analytics.levels) — a pure fold of
    #: today's trustworthy candles. The first consumer is the anti-chase
    #: resistance gate; a restart rebuilds them by replay, like the ATR.
    levels: dict[int, LevelBook] = field(default_factory=dict, repr=False)
    _token_index: dict[str, int] = field(default_factory=dict, repr=False)
    _last_tick_at: dict[int, datetime] = field(default_factory=dict, repr=False)
    _last_exchange_ts: dict[int, datetime] = field(default_factory=dict, repr=False)
    _session_candle_count: dict[int, int] = field(default_factory=dict, repr=False)
    _session_first_candle: dict[int, tuple[float, float]] = field(
        default_factory=dict, repr=False
    )
    _session_high: dict[int, float] = field(default_factory=dict, repr=False)
    _session_low: dict[int, float] = field(default_factory=dict, repr=False)
    _latest_snapshot: dict[int, tuple[Candle, IndicatorSnapshot, bool, bool, tuple[str, ...]]] = (
        field(default_factory=dict, repr=False)
    )
    _last_completed_candle_at: datetime | None = field(default=None, repr=False)
    _outage: OutageEpisode | None = field(default=None, repr=False)
    _outage_seq: int = 0

    def __post_init__(self) -> None:
        self.scanner = Scanner(self.params)
        self.strategies = self.strategies or StrategiesConfig()
        self.breakouts = BreakoutTracker(strategies=self.strategies, window=self.window)
        self.setups = SetupTracker()
        self.antichase = AntiChasePolicy()
        self.extension = self.extension or ExtensionClassifier()
        for instrument in self.instruments.values():
            if instrument.instrument_token:
                self._token_index[instrument.instrument_token] = instrument.id

    # ── feed plumbing ─────────────────────────────────────────────────────

    def resolve_instrument_id(self, exchange: str, token: str) -> int | None:
        """Token → instrument id; unknown tokens are ignored, never invented."""
        return self._token_index.get(token)

    def instruments_for_subscription(self) -> list[str]:
        return sorted(
            {
                instrument.instrument_token
                for instrument in self.instruments.values()
                if instrument.instrument_token
            }
        )

    async def on_tick(self, tick: Tick) -> None:
        self.counters.ticks_seen += 1
        self._last_tick_at[tick.instrument_id] = tick.received_at
        self._last_exchange_ts[tick.instrument_id] = tick.exchange_timestamp
        self.engine.on_tick(tick)

    async def on_feed_status(self, status: FeedStatus, reason: str, *, now: datetime) -> None:
        """Outage episodes are the dedupe unit for interruption alerts (§12.4)."""
        previous = self.feed_status
        self.feed_status = status
        if status in OUTAGE_STATUSES and self._outage is None:
            self._outage_seq += 1
            self.counters.outages += 1
            self._outage = OutageEpisode(
                episode_id=f"outage-{self.session.id}-{self._outage_seq}",
                started_at=now,
                status=status,
                reason=reason,
            )
            if self.alerts is not None and previous not in OUTAGE_STATUSES:
                await self.alerts.feed_interrupted(
                    session_id=self.session.id,
                    at=now,
                    reason=reason or status.value,
                    outage_episode=self._outage.episode_id,
                )
        elif status in HEALTHY_STATUSES and self._outage is not None:
            episode = self._outage
            episode.ended_at = now
            quiet = (now - episode.started_at).total_seconds()
            self.counters.stale_seconds_total += quiet
            self._outage = None
            await self.store.add_stale_seconds(self.session.id, int(round(quiet)))
            if self.alerts is not None:
                await self.alerts.feed_recovered(
                    session_id=self.session.id,
                    at=now,
                    outage_episode=episode.episode_id,
                    quiet_seconds=quiet,
                )

    def feed_age_seconds(self, instrument_id: int, *, now: datetime) -> float | None:
        stamp = self._last_tick_at.get(instrument_id)
        if stamp is None:
            return None
        return max(0.0, (to_ist(now) - stamp).total_seconds())

    # ── warm-up / recovery ────────────────────────────────────────────────

    async def warm_up(
        self,
        *,
        now: datetime,
        rest: Any = None,
        fetch_historical: bool = True,
        limit_per_instrument: int | None = None,
    ) -> dict[str, Any]:
        """Seed EMA + volume history; session VWAP always starts from zero.

        Sources, in order of trust: candles this database already holds (the
        normal case from day two onward), then the broker's historical endpoint
        for whatever is still short. Anything still short stays WARMING_UP —
        the scanner refuses to guess an EMA it has not seen (§8.4).
        """
        needed = limit_per_instrument or (self.params.warmup_candles_required + 10)
        ids = list(self.instruments)
        stored = await self.store.load_completed_candles(
            ids, until=self.window.open_at, limit_per_instrument=needed
        )
        short: list[int] = []
        seeded = 0
        for instrument_id, candles in stored.items():
            engine = self.registry.get(instrument_id)
            if candles:
                seeded += engine.seed(candles, session_candles=False)
            if engine.candles_seen < self.params.warmup_candles_required:
                short.append(instrument_id)
        fetched = 0
        if fetch_historical and rest is not None and short:
            fetched = await self._warm_from_rest(short, rest=rest, now=now)
        still_short = [
            instrument_id
            for instrument_id in ids
            if self.registry.get(instrument_id).candles_seen < self.params.warmup_candles_required
        ]
        self.counters.warm_instruments = len(still_short)
        summary = {
            "from_database": seeded,
            "from_broker": fetched,
            "warming_up": len(still_short),
            "ready": len(ids) - len(still_short),
            "candles_required": self.params.warmup_candles_required,
        }
        if still_short:
            log.info(
                "warmup_incomplete",
                warming_up=len(still_short),
                total=len(ids),
                note="those instruments cannot emit signals until history exists",
            )
        return summary

    async def _warm_from_rest(self, instrument_ids: list[int], *, rest: Any, now: datetime) -> int:
        """Pull previous-session 1-minute bars for instruments the DB cannot serve."""
        # The REST window is an operator choice (HISTORICAL_WARMUP_DAYS), not a
        # formula over indicator periods: warmup needs are already satisfied
        # from the DB, and per-day broker limits are spent here. The getattr
        # keeps the simulator's light-weight rest doubles working.
        window_days = max(1, int(getattr(getattr(rest, "settings", None), "historical_warmup_days", 3) or 3))
        from_dt = self.window.open_at - timedelta(days=window_days)
        total = 0
        for instrument_id in instrument_ids:
            instrument = self.instruments.get(instrument_id)
            if instrument is None or not instrument.instrument_token:
                continue
            try:
                candles = await rest.get_candles(
                    instrument_id=instrument_id,
                    symboltoken=instrument.instrument_token,
                    interval="ONE_MINUTE",
                    from_dt=from_dt,
                    to_dt=self.window.open_at,
                )
            except Exception as exc:  # noqa: BLE001 - warm-up must never kill the session
                log.warning("warmup_fetch_failed", symbol=instrument.symbol, error=str(exc)[:200])
                continue
            # Only candles strictly before this session may seed history; the
            # live session's own VWAP must start empty (§9.1).
            prior = [candle for candle in candles if candle.candle_start < self.window.open_at]
            if prior:
                total += self.registry.get(instrument_id).seed(prior, session_candles=False)
        return total

    # ── the minute loop ───────────────────────────────────────────────────

    async def drain(self, now: datetime) -> list[Candle]:
        """Finalize every due minute and run it through the scanner."""
        due = self.engine.finalize_due(now)
        if due:
            await self.process_candles(due, now=now)
        return due

    async def flush_session(self, now: datetime) -> list[Candle]:
        """Session close: flush the forming minute, then the day's last ORB bucket.

        The closing bucket of the session has no following candle to finalize it,
        and the /strategy board reads it because it queries a finished day. Flushing
        the tracker here is what makes the live alerts agree with that board.
        """
        remaining = self.engine.finalize_all(now)
        if remaining:
            await self.process_candles(remaining, now=now)
        if self.feed_status not in OUTAGE_STATUSES:
            events = self.breakouts.flush()
            self.counters.breakouts_detected += len(events)
            for event in events:
                instrument = self.instruments.get(event.instrument_id)
                if instrument is not None:
                    await self._raise_breakout(event, instrument=instrument)
        return remaining

    async def process_candles(self, candles: list[Candle], *, now: datetime) -> None:
        if not candles:
            return
        if self.is_simulated:
            # The candle engine has no notion of provenance — it faithfully builds
            # OHLCV from whatever ticks it receives. Labeling happens here, at the
            # single point where synthetic candles become durable rows, so a replay
            # can never be mistaken for real market history (§7.4).
            for candle in candles:
                candle.is_simulated = True
        await self.store.upsert_candles(candles)
        for candle in candles:
            await self._process_one(candle, now=now)

    async def _process_one(self, candle: Candle, *, now: datetime) -> None:
        instrument = self.instruments.get(candle.instrument_id)
        if instrument is None:
            log.warning("candle_for_unknown_instrument", instrument_id=candle.instrument_id)
            return
        in_session = candle.trading_date == self.window.trading_date
        engine = self.registry.get(candle.instrument_id)
        values = engine.on_candle(
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            in_current_session=in_session,
            candle_start=candle.candle_start,
        )
        if in_session:
            count = self._session_candle_count.get(candle.instrument_id, 0) + 1
            self._session_candle_count[candle.instrument_id] = count
            if candle.instrument_id not in self._session_first_candle:
                # The session's opening print and first close — raw facts the
                # extension measures derive the gap and the change-from-open
                # from, recorded once and never revised.
                self._session_first_candle[candle.instrument_id] = (candle.open, candle.close)
            if self._last_completed_candle_at is None or (
                candle.candle_start > self._last_completed_candle_at
            ):
                self._last_completed_candle_at = candle.candle_start
            self._session_high[candle.instrument_id] = max(
                self._session_high.get(candle.instrument_id, candle.high), candle.high
            )
            self._session_low[candle.instrument_id] = min(
                self._session_low.get(candle.instrument_id, candle.low), candle.low
            )
        context = ScanContext(
            instrument_id=candle.instrument_id,
            symbol=instrument.symbol,
            in_universe=True,
            previous_close=instrument.previous_close,
            feed_age_seconds=self.feed_age_seconds(candle.instrument_id, now=now),
            session_candle_count=self._session_candle_count.get(candle.instrument_id, 0),
            evaluation_time=now,
            is_simulated=self.is_simulated,
        )
        result = self.scanner.evaluate(
            candle,
            values,
            context,
            session_high=self._session_high.get(candle.instrument_id),
            session_low=self._session_low.get(candle.instrument_id),
        )
        # The detectors are folded in *before* the snapshot is written, not after it:
        # the RSI rule's verdict belongs on this candle's row, and judging once and
        # reading the same verdict back is what keeps the snapshot, the board and the
        # Telegram line from telling three versions of one event. A feed we cannot
        # trust does not enter the detectors at all — a flat minute would build a
        # level and a streak out of nothing — so the row carries the last verdict the
        # trustworthy tape produced, the same treatment its EMA and VWAP get.
        events: list[BreakoutEvent] = []
        retests: list[RetestCandidate] = []
        reclaims: list[ReclaimCandidate] = []
        if self.feed_status not in OUTAGE_STATUSES:
            events = self.breakouts.on_candle(
                candle,
                symbol=instrument.symbol,
                vwap=values.vwap,
                relative_volume=values.relative_volume,
                rsi_5m=values.rsi_5m,
                trend=result.trend,
            )
            # The level book is a fold of the trustworthy tape, like every other
            # level here, and it folds this candle *before* the candidates are
            # judged: "what is overhead" then already knows the print that just
            # happened, including the candidate candle's own wick.
            book = self.levels.get(candle.instrument_id)
            if book is None:
                book = LevelBook()
                self.levels[candle.instrument_id] = book
            if in_session:
                book.fold(high=candle.high, low=candle.low)
            # The setup watch rides the same trusted tape as the detectors. Open
            # legs and the below-VWAP dip advance *before* this candle's events
            # are handed over, so the candle that broke a level (or crossed back
            # above the VWAP) is judged at its close and never counts as its own
            # first retest bar — or as part of its own dip.
            retests = self.setups.on_candle(
                candle,
                symbol=instrument.symbol,
                relative_volume=values.relative_volume,
                vwap=values.vwap,
            )
            for event in events:
                candidate = self.setups.on_event(event, atr=values.atr)
                if isinstance(candidate, ReclaimCandidate):
                    reclaims.append(candidate)
            if retests or reclaims:
                context = self._market_context(candle, vwap=values.vwap, atr=values.atr)
                for candidate in (*retests, *reclaims):
                    report = self.antichase.evaluate(candidate, context=context)
                    if not report.cleared:
                        self.counters.antichase_blocked += 1
                    log.info(
                        "antichase_verdict",
                        **report.to_log_dict(),
                        candle_start=candle.candle_start.isoformat(),
                    )
                    self._observe_candidate(
                        candidate,
                        policy_report=report,
                        instrument=instrument,
                        candle=candle,
                        values=values,
                        bullish=result.bullish,
                    )
        snapshot = result.to_snapshot(
            trading_date=self.window.trading_date,
            config_version=self.params.config_version,
            is_simulated=self.is_simulated,
            rsi_state=self.breakouts.rsi_state(candle.instrument_id),
        )
        snapshot_id = await self.store.upsert_indicator_snapshot(snapshot)
        self.counters.snapshots_written += 1
        self._latest_snapshot[candle.instrument_id] = (
            candle,
            snapshot,
            result.bullish,
            result.evaluable,
            result.reasons,
        )
        self.counters.candles_finalized += 1

        # A feed we cannot trust suspends state changes entirely: the candle is
        # still stored, the snapshot is still stored, but no alert is minted.
        if self.feed_status in OUTAGE_STATUSES:
            return
        # Level breaks are judged for every instrument the feed reaches, not only
        # the ones that qualified: a day high on a stock the scanner never liked is
        # still a day high, and the operator asked for the whole F&O universe.
        self.counters.breakouts_detected += await self._raise_breakouts(
            events, instrument=instrument
        )
        self.counters.retests_detected += len(retests)
        self.counters.reclaims_detected += len(reclaims)
        outcome = await self.watchlist.on_candle(
            session_id=self.session.id,
            trading_date=self.window.trading_date,
            instrument=instrument,
            candle=candle,
            snapshot=snapshot,
            bullish=result.bullish,
            evaluable=result.evaluable,
            reasons=result.reasons,
            now=now,
            snapshot_row_id=snapshot_id,
        )
        if outcome.signaled:
            self.counters.signals_raised += 1

    def _market_context(
        self, candle: Candle, *, vwap: float | None, atr: float | None
    ) -> MarketContext:
        """The anti-chase policy's inputs, gathered once per judged candle.

        Every level comes from its one derivation, merged here rather than
        re-derived: the inventory from the book, the range ends from the
        breakout tracker. Prior-day ends land when the recovery path reads
        ``candles_1d`` (a later phase); until then the inventory holds what
        today's tape and the detectors know.
        """
        orb = self.breakouts.orb_range(candle.instrument_id)
        book = self.levels.get(candle.instrument_id)
        levels = None
        if book is not None:
            levels = tuple(
                book.inventory(
                    vwap=vwap,
                    orb_high=None if orb is None else orb[0],
                    orb_low=None if orb is None else orb[1],
                )
            )
        return MarketContext(
            vwap=vwap,
            atr=atr,
            orb_low=None if orb is None else orb[1],
            levels=levels,
        )

    def _observe_candidate(
        self,
        candidate: RetestCandidate | ReclaimCandidate,
        *,
        policy_report: AntiChaseReport,
        instrument: Instrument,
        candle: Candle,
        values: IndicatorValues,
        bullish: bool,
    ) -> None:
        """The audit Phase B ensemble: measure the chase, name the state, score it.

        Observation only — counters and one log line per candidate, exactly like
        ``retests_detected`` before it: no alert and no row (the alert edits wait
        for their phase, the persistence for migration 0012's approval). Every
        input is a fact some other module already produced — the policy report
        from the gates above, the extension class from the classifier, the
        level's presence from the candidate itself — so nothing here re-derives
        a verdict it was handed.
        """
        first_candle = self._session_first_candle.get(candidate.instrument_id)
        orb = self.breakouts.orb_range(candidate.instrument_id)
        verdict = self.extension.classify(
            ExtensionMeasures(
                completed_candles=self._session_candle_count.get(candidate.instrument_id, 0),
                price=candle.close,
                previous_close=instrument.previous_close,
                session_open=None if first_candle is None else first_candle[0],
                first_candle_close=None if first_candle is None else first_candle[1],
                vwap=values.vwap,
                fast_ema=values.ema_fast,
                atr=values.atr,
                orb_high=None if orb is None else orb[0],
                # The gate verdicts are the caller-merged hard blocks, as the
                # classifier's contract states: only this layer holds them.
                hard_block_reasons=() if policy_report.cleared else policy_report.reasons(),
            )
        )
        readiness = classify_readiness(
            ReadinessInputs(
                setup_trigger_confirmed=True,
                mandatory_cleared=bullish and policy_report.cleared,
                extension_class=verdict.classification,
                invalidation_level=self._invalidation_level(candidate),
            )
        )
        fast_above_slow = None
        if values.ema_fast is not None and values.ema_slow is not None:
            fast_above_slow = values.ema_fast > values.ema_slow
        score = compute_score(
            ScoreInputs(
                # daily_trend_class and liquidity_class stay None until Stage 1
                # snapshots feed the pipeline; completeness reports the gap.
                fast_above_slow=fast_above_slow,
                price=candle.close,
                vwap=values.vwap,
                atr=values.atr,
                rvol=values.relative_volume,
                rvol_threshold=self.params.relative_volume_threshold,
                rsi_5m=values.rsi_5m,
                macd_histogram=values.macd_histogram,
                structure_evidence=(self._structure_id(candidate),),
                extension_class=verdict.classification,
            )
        )
        self.counters.candidates_judged += 1
        if verdict.extended:
            self.counters.extension_extended_detected += 1
        if readiness.state == READINESS_ENTRY_READY:
            self.counters.entry_ready_detected += 1
        log.info(
            "candidate_judgement",
            symbol=candidate.symbol,
            candle_start=candle.candle_start.isoformat(),
            extension=verdict.to_log_dict(),
            readiness=readiness.to_log_dict(),
            score=score.to_dict(),
        )

    @staticmethod
    def _invalidation_level(candidate: RetestCandidate | ReclaimCandidate) -> float | None:
        """The candidate's own risk line: a reclaim's dip low, a retest's low.

        The brief asks whether a *reliable* invalidation level exists; the
        candidate carries the one its pattern produced, and the readiness
        machine treats its absence as an incomplete risk block rather than
        inventing a stop.
        """
        if isinstance(candidate, ReclaimCandidate):
            return candidate.dip_low
        return candidate.min_low

    @staticmethod
    def _structure_id(candidate: RetestCandidate | ReclaimCandidate) -> str:
        if isinstance(candidate, ReclaimCandidate):
            return "vwap_reclaim"
        return "orb_retest"

    async def _raise_breakouts(
        self, events: list[BreakoutEvent], *, instrument: Instrument
    ) -> int:
        """Alert on what the detectors found; returns how many they found.

        The detector itself is pure and holds no database handle — this is the only
        place its output meets the alert engine. It is a separate method from the
        judging call because the snapshot has to be written with the RSI verdict
        first, and one call per candle is the whole reason the two can agree.
        """
        for event in events:
            await self._raise_breakout(event, instrument=instrument)
        return len(events)

    async def _raise_breakout(self, event: BreakoutEvent, *, instrument: Instrument) -> None:
        if self.alerts is None:
            return
        outcome = await self.alerts.raise_breakout(
            event,
            session_id=self.session.id,
            company_name=instrument.company_name,
            previous_close=instrument.previous_close,
        )
        # ``created`` rather than ``notified`` because a cooldown-suppressed or
        # simulated alert is still a row the operator can read on the board; the
        # gap between detected and created is the dedupe working, and it belongs
        # in the log line rather than in a counter that looks like a bug.
        if outcome.created:
            self.counters.breakouts_alerted += 1
        log.info(
            "breakout",
            **event.to_log_dict(),
            created=outcome.created,
            suppressed=outcome.suppressed_reason,
        )

    async def recover_breakouts(self) -> dict[str, Any]:
        """Rebuild today's levels from the candles this database already holds.

        A worker that comes up at 11:40 — after a crash, a deploy, or a Mini that
        slept — has never seen today's opening range. Without this the ORB detector
        stays mute until tomorrow and the first new high after the restart is
        reported as a day break against a level made of afternoon candles. The
        minutes are already stored, so replaying them through the same code rebuilds
        exactly what the live tape would have built, silently: those alerts were
        judged when they happened.
        """
        stored = await self.store.load_completed_candles(
            list(self.instruments),
            until=self.window.close_at,
            limit_per_instrument=SESSION_CANDLE_SLOTS,
        )
        folded = 0
        instruments = 0
        for instrument_id, candles in stored.items():
            instrument = self.instruments.get(instrument_id)
            if instrument is None:
                continue
            rows: list[tuple[Candle, float | None]] = []
            typical_volume = 0.0
            volume_total = 0
            for candle in candles:
                # Session VWAP from stored bars, the same Σ(TP·V)/ΣV the indicator
                # engine maintains live. Only today's minutes count toward it.
                if candle.trading_date == self.window.trading_date and candle.volume > 0:
                    typical_volume += candle.typical_price * candle.volume
                    volume_total += candle.volume
                rows.append((candle, typical_volume / volume_total if volume_total else None))
            count = self.breakouts.recover(rows, symbol=instrument.symbol)
            if count:
                folded += count
                instruments += 1
        summary = {
            "instruments_recovered": instruments,
            "candles_folded": folded,
            "levels": self.breakouts.armed_summary(),
        }
        if folded:
            log.info("breakout_levels_recovered", **summary)
        return summary

    def reconfigure_breakouts(self, strategies: StrategiesConfig) -> dict[str, Any]:
        """Apply an edited strategy document mid-session, keeping today's levels.

        The thresholds, cooldowns and Telegram switches change; the opening range
        and the day high do not, because they are facts about the tape rather than
        facts about the configuration.
        """
        self.strategies = strategies
        self.breakouts.reconfigure(strategies)
        return self.breakouts.armed_summary()

    def latest_evaluations(
        self,
    ) -> dict[int, tuple[Candle, IndicatorSnapshot, bool, bool, tuple[str, ...]]]:
        return dict(self._latest_snapshot)

    # ── scheduled phases ──────────────────────────────────────────────────

    async def run_initial_scan(self, now: datetime) -> dict[str, Any]:
        """§11.1 — the first watchlist, after the configured candle count."""
        counts = self._session_candle_count
        if counts and max(counts.values()) < self.params.initial_scan_candles:
            return {"kind": "initial_scan", "skipped": "fewer candles than required"}
        summary = await self.watchlist.initial_scan(
            session_id=self.session.id,
            trading_date=self.window.trading_date,
            instruments=list(self.instruments.values()),
            latest=self.latest_evaluations(),
            now=now,
        )
        self.initial_scan_completed = True
        await self.store.mark_initial_scan_completed(self.session.id, now)
        # The scan's per-bucket counts (evaluated / qualified / warming_up /
        # missing_data …) are the operator's proof of what the universe actually
        # looked like at open. ``daily_summaries`` has fixed columns and cannot
        # hold them, and ``v_session_history`` would show a row of zeros, so the
        # summary is journaled as an audit event the dashboard can read back (§13.12).
        await self.store.audit(
            actor="worker",
            actor_role="SYSTEM",
            action="initial_scan",
            entity_type="trading_session",
            entity_id=str(self.session.id),
            new_value=summary,
        )
        if self.alerts is not None:
            await self.alerts.raise_alert(
                signal_type=SignalType.DAILY_SCAN_COMPLETE,
                instrument_id=None,
                session_id=self.session.id,
                event_timestamp=now,
                message=(
                    f"Initial scan: {summary['counts']['qualified']} qualified, "
                    f"{summary['counts']['warming_up']} warming up, "
                    f"{summary['counts']['stale']} stale, "
                    f"{summary['counts']['missing_data']} without data "
                    f"({len(self.instruments)} mapped)."
                ),
            )
        await self.store.write_daily_summary(self.session.id, self.window.trading_date, summary)
        return summary

    async def finalize_day(self, now: datetime) -> dict[str, Any]:
        summary = await self.watchlist.finalize_day(session_id=self.session.id, now=now)
        memberships = await self.store.list_memberships(self.session.id, include_inactive=True)
        ranked = sorted(
            (m for m in memberships if m.latest_score is not None),
            key=lambda m: m.latest_score or 0.0,
            reverse=True,
        )
        coverage = await self.store.coverage(universe=self.params.universe)
        signals_generated = await self.store.count_signals_since(
            self.session.id, since=self.window.open_at
        )
        rollup = await self.store.delivery_rollup(session_id=self.session.id)
        payload = {
            "kind": "daily_summary",
            "trading_date": self.window.trading_date.isoformat(),
            "candles": self.counters.as_dict(),
            "final_watchlist": [
                {
                    "instrument_id": membership.instrument_id,
                    "symbol": self.instruments[membership.instrument_id].symbol,
                    "state": membership.current_state.value,
                    "score": membership.latest_score,
                    "rank": index + 1,
                }
                for index, membership in enumerate(ranked)
            ],
            "unresolved_stale": summary["unresolved_stale_memberships"],
            "stale_seconds": self.counters.stale_seconds_total,
            # ``daily_summaries`` has these columns and ``v_session_history`` reads
            # them; omitted from the payload they silently stayed at their DEFAULT 0,
            # so the history page reported a qualified/signalled/alerted count of
            # zero for a day that plainly had alerts (§13.12, §17).
            "universe_count": coverage.universe_total,
            "mapped_count": coverage.mapped_total,
            "subscribed_count": coverage.subscribed_total,
            "monitored_count": coverage.monitored_total,
            "candles_persisted": self.counters.candles_finalized,
            # A watchlist row is created only when an instrument qualifies, so the
            # rows this session holds *are* the set that qualified today.
            "qualified_count": len(memberships),
            "signals_generated": signals_generated,
            "telegram_delivered": rollup.get("TELEGRAM_DELIVERED", 0),
            "telegram_failed": rollup.get("TELEGRAM_FAILED", 0)
            + rollup.get("TELEGRAM_PERMANENTLY_FAILED", 0),
            "stale_data_seconds": int(round(self.counters.stale_seconds_total)),
        }
        await self.store.write_daily_summary(self.session.id, self.window.trading_date, payload)
        await self.store.finalize_session(self.session.id, now)
        return payload

    # ── health ────────────────────────────────────────────────────────────

    async def publish_heartbeat(
        self,
        *,
        worker_id: str,
        now: datetime,
        subscribed_count: int,
        last_market_data_at: datetime | None,
        last_error: str | None = None,
        universe_total: int | None = None,
    ) -> None:
        """Write the operator-facing health row (§17 item 15).

        Every number here must be observed, not inferred: ``last_market_data_at``
        falls back to the newest exchange stamp this process has actually seen,
        and ``last_completed_candle_at`` is the newest candle the engine
        finalized in *this* session — a restart resets it, which is the truth.
        """
        active = await self.store.list_memberships(
            self.session.id, states=(WatchlistState.ACTIVE, WatchlistState.WEAKENING)
        )
        pending = await self.store.pending_notification_count()
        latest_tick_stamp = max((stamp for stamp in self._last_exchange_ts.values()), default=None)
        await self.store.upsert_heartbeat(
            WorkerHeartbeat(
                worker_id=worker_id,
                status=self.feed_status.value,
                last_market_data_at=last_market_data_at or latest_tick_stamp,
                last_completed_candle_at=self._last_completed_candle_at,
                subscribed_instrument_count=subscribed_count,
                active_watchlist_count=len(active),
                pending_notification_count=pending,
                queue_depth=pending,
                last_error=last_error,
                feed_status=self.feed_status,
                universe_total=universe_total
                if universe_total is not None
                else len(self.instruments),
                mapped_total=len(self.instruments),
                monitored_total=len(self._last_tick_at),
                # The dashboard's SimChip reads these off the heartbeat row
                # (v_worker_status); without them a simulation run would report
                # itself live, which §17 forbids.
                scan_mode="simulation" if self.is_simulated else "live",
                simulation_label=self.is_simulated,
            )
        )

    def warmup_report(self) -> dict[str, Any]:
        required = self.params.warmup_candles_required
        seen = {iid: self.registry.get(iid).candles_seen for iid in self.instruments}
        return {
            "required_candles": required,
            "ready": sum(1 for count in seen.values() if count >= required),
            "warming_up": sum(1 for count in seen.values() if count < required),
            "with_any_history": sum(1 for count in seen.values() if count > 0),
        }

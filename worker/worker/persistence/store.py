"""The storage contract used by the worker pipeline.

Why an interface instead of direct SQL in the pipeline:

* the simulator and the test suite must exercise the real pipeline without a
  broker or a database (spec §20.2 "never require real API credentials"),
* restart-recovery behaviour has to be reproducible deterministically,
* and the Postgres implementation stays reviewable: SQL lives in one file.

Both implementations MUST behave identically for: candle upserts, snapshot
upserts, signal idempotency, notification queue claiming, and cooldown lookups.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime

from ..config import ScannerParams
from ..models import (
    Candle,
    Channel,
    DailyCandle,
    DeliveryStatus,
    DiscoverySnapshotRow,
    IndicatorSnapshot,
    Instrument,
    NotificationDelivery,
    RsiState,
    SectorIndex,
    SectorQuote,
    Signal,
    StockProfile,
    StockTechnicals,
    WatchlistEvent,
    WatchlistMembership,
    WatchlistState,
    WeeklyCandle,
    WorkerHeartbeat,
)


@dataclass(frozen=True)
class SignalInsertResult:
    """Whether this call created the signal or a duplicate was suppressed."""

    signal_id: int
    created: bool

    def __bool__(self) -> bool:  # `if result:` reads as "signal is new"
        return self.created


#: Delivery statuses that mean "someone already has, or is about to get, this
#: alert". ``SUPPRESSED``, ``SIMULATED`` and ``PERMANENTLY_FAILED`` are
#: deliberately absent: nobody saw those, so they must not mute the next alert.
#: Shared by both stores so the cooldown window cannot mean two different things
#: depending on which backend is configured.
ALERTED_DELIVERY_STATUSES: frozenset[DeliveryStatus] = frozenset(
    {
        DeliveryStatus.QUEUED,
        DeliveryStatus.IN_FLIGHT,
        DeliveryStatus.DELIVERED,
        DeliveryStatus.FAILED,
    }
)


@dataclass(frozen=True)
class CoverageCounts:
    universe_total: int = 0
    mapped_total: int = 0
    subscribed_total: int = 0
    monitored_total: int = 0
    unmapped_total: int = 0
    ambiguous_total: int = 0
    invalid_total: int = 0

    @property
    def coverage_pct(self) -> float:
        if self.universe_total <= 0:
            return 0.0
        return round(self.monitored_total / self.universe_total * 100.0, 2)


@dataclass
class NotificationClaim:
    delivery: NotificationDelivery
    channel: Channel


@dataclass(frozen=True)
class SessionRow:
    id: int
    trading_date: date
    session_status: str
    session_open_at: datetime
    session_close_at: datetime
    initial_scan_completed_at: datetime | None = None
    finalized_at: datetime | None = None
    # Schema/contract reconciliation: trading_sessions.stale_data_seconds exists
    # in 0001_init.sql and InMemoryStore.add_stale_seconds accumulates it, but
    # this row had no field for it — Postgres and memory could not agree.
    stale_data_seconds: int = 0


@dataclass(frozen=True)
class ConfigRow:
    id: int
    name: str
    params: ScannerParams
    is_enabled: bool
    config_version: int


@dataclass(frozen=True)
class WatchlistFilter:
    trading_date: date
    states: tuple[WatchlistState, ...] = ()
    include_inactive: bool = False


class ScannerStore(ABC):
    """Everything the worker needs to durably remember."""

    # ── lifecycle ──────────────────────────────────────────────────────────
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def healthcheck(self) -> bool: ...

    # ── instruments ────────────────────────────────────────────────────────
    @abstractmethod
    async def upsert_instruments(self, instruments: list[Instrument]) -> int: ...

    @abstractmethod
    async def list_universe(self, *, universe: str = "NIFTY500", mapped_only: bool = True) -> list[Instrument]: ...

    @abstractmethod
    async def get_instrument(self, instrument_id: int) -> Instrument | None: ...

    @abstractmethod
    async def set_previous_closes(self, values: dict[int, float]) -> int: ...

    @abstractmethod
    async def mark_mapping_status(
        self, unmapped_symbols: list[str], status: str, *, universe: str = "NIFTY500"
    ) -> int: ...

    @abstractmethod
    async def coverage(self, *, universe: str = "NIFTY500") -> CoverageCounts: ...

    # ── F&O universe + sector tile ─────────────────────────────────────────
    @abstractmethod
    async def set_fno_symbols(self, symbols: set[str]) -> int:
        """Recompute the F&O flag: TRUE exactly for these symbols, FALSE for the rest.

        Sync-time truth, not an increment: every sync run reflects the whole
        NFO underlying set it just read, so a stock that drops out of F&O is
        unflagged by the same call that flags the newcomers.
        """

    @abstractmethod
    async def upsert_sector_indices(self, rows: list[SectorIndex]) -> int: ...

    @abstractmethod
    async def list_sector_indices(self) -> list[SectorIndex]: ...

    @abstractmethod
    async def upsert_sector_quotes(self, rows: list[SectorQuote]) -> int:
        """Newest-wins upsert; ``sector_quotes`` is a scratchpad, not a log."""

    @abstractmethod
    async def list_sector_quotes(self) -> list[SectorQuote]: ...

    # ── daily/weekly bars + stock details (two-timeframe RSI, 2026-10-12) ──
    @abstractmethod
    async def upsert_daily_candles(self, rows: list[DailyCandle]) -> int:
        """Newest-wins upsert into ``candles_1d`` (PK instrument + date).

        Re-running the same sync must be free: the sync scripts recompute the
        recent window every night (it re-fetches it anyway to heal corporate
        action adjustments), so the write path is one idempotent upsert.
        """

    @abstractmethod
    async def upsert_weekly_candles(self, rows: list[WeeklyCandle]) -> int:
        """Newest-wins upsert into ``candles_1w`` (PK instrument + week start)."""

    @abstractmethod
    async def load_daily_candles(self, instrument_id: int) -> list[DailyCandle]:
        """All stored daily bars for one instrument, oldest → newest.

        One instrument at a time, deliberately: the deep history is thousands
        of rows per symbol and nightly computation needs exactly one symbol's
        series at once — a load-everything query would materialize hundreds of
        thousands of rows to use them sequentially.
        """

    @abstractmethod
    async def load_weekly_candles(self, instrument_id: int) -> list[WeeklyCandle]: ...

    @abstractmethod
    async def upsert_discovery_snapshots(self, rows: list[DiscoverySnapshotRow]) -> int:
        """Idempotent upsert into ``daily_discovery_snapshots`` (PK date + instrument).

        One row per day per instrument: re-running the discovery must overwrite
        the day's verdicts in place, never append a second set. Returns how many
        rows the batch touched.
        """

    @abstractmethod
    async def load_discovery_snapshots(self, trading_date: date) -> list[DiscoverySnapshotRow]:
        """One day's stored discovery rows, ordered by ``instrument_id``.

        This is the read half of "skip when unchanged": the discovery runner
        compares the fresh fold against the stored row and writes only what
        moved, so it has to be able to read the day back. Deterministic order
        keeps that comparison position-by-position.
        """

    @abstractmethod
    async def update_stock_technicals(self, rows: list[StockTechnicals]) -> int:
        """Upsert the candles-derived columns of ``stock_details``.

        Must not touch the profile columns (pe/mcap/listing) — the two writers
        own disjoint column sets and can therefore run in either order.
        """

    @abstractmethod
    async def upsert_stock_profiles(self, rows: list[StockProfile]) -> int:
        """Upsert the fundamentals columns of ``stock_details``.

        ``listing_date`` merges with COALESCE: the NSE equity list is the only
        source for it, so a later Yahoo-only run must not blank it.
        """

    @abstractmethod
    async def list_rsi_states(self) -> list[RsiState]:
        """Every ``stock_details`` row — the worker's RSI-alert input.

        The caller intersects with the configured universe; the table is small
        (the F&O set), so an unfiltered read with a universe join applied in
        memory is cheaper than a per-universe SQL variant.
        """

    @abstractmethod
    async def set_last_alert_bias(self, instrument_id: int, bias: str) -> None:
        """Record the bias the engine last acted on for this instrument.

        Also written for NEUTRAL transitions, which are deliberately silent:
        the column is "what the operator last heard", not "what was bullish".
        """

    @abstractmethod
    async def purge_older_than(self, days: int, *, now: datetime) -> dict[str, int]:
        """Delete candles and indicator snapshots older than ``days``.

        Returns per-table deleted-row counts keyed by table name, so the log
        line states what was removed instead of implying it. Watchlist,
        signal, and summary history is deliberately never purged here — it is
        the small, interpretable part of the database.
        """

    @abstractmethod
    async def get_ops_setting(self, key: str) -> dict[str, object] | None:
        """Read one row of ``ops_settings`` (the dashboard's operations knobs).

        Returns the ``value`` document or ``None`` when unset — the caller
        decides the default. The worker only ever reads here; the dashboard is
        the writer, so the two cannot disagree about what a knob means.
        """

    # ── sessions ───────────────────────────────────────────────────────────
    @abstractmethod
    async def ensure_session(
        self,
        trading_date: date,
        session_open_at: datetime,
        session_close_at: datetime,
        *,
        exchange: str = "NSE",
    ) -> SessionRow: ...

    @abstractmethod
    async def set_session_status(self, session_id: int, status: str) -> None: ...

    @abstractmethod
    async def mark_initial_scan_completed(self, session_id: int, at: datetime) -> None: ...

    @abstractmethod
    async def add_stale_seconds(self, session_id: int, seconds: int) -> None: ...

    @abstractmethod
    async def finalize_session(self, session_id: int, at: datetime) -> None: ...

    @abstractmethod
    async def get_session_for_date(self, trading_date: date, *, exchange: str = "NSE") -> SessionRow | None: ...

    @abstractmethod
    async def get_open_session(self) -> SessionRow | None:
        """Most recent session not yet finalized — used by restart recovery."""

    # ── scanner configuration ──────────────────────────────────────────────
    @abstractmethod
    async def active_config(self, *, name: str = "default") -> ConfigRow | None: ...

    @abstractmethod
    async def save_config(self, params: ScannerParams, *, name: str = "default", created_by: str) -> ConfigRow: ...

    # ── candles + indicators ───────────────────────────────────────────────
    @abstractmethod
    async def upsert_candles(self, candles: list[Candle]) -> int: ...

    @abstractmethod
    async def load_completed_candles(
        self, instrument_ids: list[int], *, until: datetime, limit_per_instrument: int
    ) -> dict[int, list[Candle]]: ...

    @abstractmethod
    async def latest_completed_candle_at(self, instrument_id: int) -> datetime | None: ...

    @abstractmethod
    async def upsert_indicator_snapshot(self, snapshot: IndicatorSnapshot) -> int: ...

    @abstractmethod
    async def count_candles_for_session(self, session_id: int) -> int: ...

    # ── watchlist ──────────────────────────────────────────────────────────
    @abstractmethod
    async def get_membership(self, session_id: int, instrument_id: int) -> WatchlistMembership | None: ...

    @abstractmethod
    async def create_membership(
        self,
        session_id: int,
        instrument_id: int,
        state: WatchlistState,
        *,
        entered_at: datetime,
        price: float,
        score: float | None,
        snapshot_id: int | None,
        is_simulated: bool,
    ) -> WatchlistMembership: ...

    @abstractmethod
    async def update_membership(
        self,
        membership_id: int,
        *,
        state: WatchlistState | None = None,
        state_changed_at: datetime | None = None,
        latest_price: float | None = None,
        latest_score: float | None = None,
        latest_rank: int | None = None,
        removal_reason: str | None = None,
        consecutive_weak_candles: int | None = None,
        episode_count: int | None = None,
        snapshot_id: int | None = None,
        is_active: bool | None = None,
    ) -> None: ...

    @abstractmethod
    async def add_watchlist_event(self, event: WatchlistEvent) -> int: ...

    @abstractmethod
    async def list_memberships(
        self, session_id: int, *, states: tuple[WatchlistState, ...] = (), include_inactive: bool = True
    ) -> list[WatchlistMembership]: ...

    @abstractmethod
    async def freeze_session_watchlist(self, session_id: int) -> int:
        """Mark every membership inactive at session close, preserving history."""

    # ── signals + notifications ────────────────────────────────────────────
    @abstractmethod
    async def insert_signal(self, signal: Signal) -> SignalInsertResult: ...

    @abstractmethod
    async def last_pushed_signal_at(
        self,
        instrument_id: int,
        signal_type: str,
        *,
        since: datetime,
        reason: str | None = None,
    ) -> datetime | None:
        """Newest signal for this (instrument, type) that reached a channel.

        The cooldown window has to be measured against alerts a person actually
        received. Signal rows are written even when their delivery is suppressed,
        so a plain "last signal" query lets a continuously qualifying instrument
        mute itself forever — one alert, then silence for the rest of the day.

        ``reason`` narrows the match to rows whose ``reasons`` array contains it.
        Breakout alerts share one signal type across three strategies with three
        different cooldowns, so a VWAP alert must not be muted by an opening-range
        alert of the same minute — ``strategy:<name>`` is the discriminator
        (:func:`worker.analytics.breakouts.strategy_reason`).
        """

    @abstractmethod
    async def count_signals_since(self, session_id: int, since: datetime) -> int: ...

    @abstractmethod
    async def enqueue_notification(self, delivery: NotificationDelivery) -> int: ...

    @abstractmethod
    async def claim_due_notifications(
        self, *, channel: Channel, now: datetime, limit: int = 10
    ) -> list[NotificationClaim]: ...

    @abstractmethod
    async def update_delivery(
        self,
        delivery_id: int,
        *,
        status: DeliveryStatus,
        attempt_count: int,
        last_attempt_at: datetime,
        delivered_at: datetime | None = None,
        next_retry_at: datetime | None = None,
        provider_message_id: str | None = None,
        last_error: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def pending_notification_count(self, *, channel: Channel | None = None) -> int: ...

    @abstractmethod
    async def delivery_rollup(self, *, session_id: int) -> dict[str, int]:
        """Delivery counts for one session's signals.

        Keys are ``"<CHANNEL>_<STATUS>"`` (``"TELEGRAM_DELIVERED"``,
        ``"DASHBOARD_SIMULATED"`` …), values are row counts. The daily summary
        needs delivered / failed numbers per channel, and a guess from queue
        depth would be exactly the kind of inferred coverage number the spec
        forbids: this counts the rows that exist.
        """

    # ── operations ─────────────────────────────────────────────────────────
    @abstractmethod
    async def upsert_heartbeat(self, heartbeat: WorkerHeartbeat) -> None: ...

    @abstractmethod
    async def write_daily_summary(
        self, session_id: int, trading_date: date, payload: dict[str, object]
    ) -> None: ...

    @abstractmethod
    async def audit(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        previous_value: dict[str, object] | None = None,
        new_value: dict[str, object] | None = None,
        actor_role: str | None = None,
        request_ip: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def acquire_lease(self, name: str, holder: str, ttl_seconds: int, now: datetime) -> bool: ...

    @abstractmethod
    async def renew_lease(self, name: str, holder: str, ttl_seconds: int, now: datetime) -> bool: ...

    @abstractmethod
    async def release_lease(self, name: str, holder: str) -> bool:
        """Give up a lease on clean shutdown.

        Without this, a restarted replica has to wait for the TTL to expire
        before it can take over — correct but needlessly slow.
        """

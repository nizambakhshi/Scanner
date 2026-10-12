"""Domain models shared by the feed, scanner, watchlist and notification layers.

Design notes:

* These are plain frozen-ish dataclasses, not ORM rows. The worker computes on
  them in memory and the repository layer maps them to PostgreSQL rows. That
  keeps the analytics testable without a database.
* All datetimes are timezone-aware (Asia/Kolkata in practice). Naive datetimes
  are rejected by :func:`ensure_aware` because a naive candle boundary is the
  single most common source of silent 5.5-hour data corruption in IST systems.
* Prices/volumes are ``float`` with rounding applied at the persistence
  boundary (``NUMERIC(16,4)`` / ``BIGINT`` columns). Chosen deliberately over
  ``Decimal`` for indicator throughput at 500 instruments/minute; documented in
  ``docs/LIMITATIONS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any


class DataQuality(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"          # built from ticks but with a known gap
    WARMING_UP = "WARMING_UP"    # not enough history for all indicators
    STALE = "STALE"              # feed exceeded the staleness threshold
    INCOMPLETE = "INCOMPLETE"    # still forming / not finalized
    NO_TRADE = "NO_TRADE"        # zero volume in the bucket (not a trade print)
    SUSPECT = "SUSPECT"          # failed a sanity check (e.g. cumulative reset)


class FeedStatus(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"      # socket open — NOT proof of valid data
    STREAMING = "STREAMING"      # ticks are arriving and parsing
    STALE = "STALE"
    ERROR = "ERROR"


class MappingStatus(StrEnum):
    MAPPED = "MAPPED"
    UNMAPPED = "UNMAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID = "INVALID"
    STALE_MASTER = "STALE_MASTER"


class WatchlistState(StrEnum):
    WARMING_UP = "WARMING_UP"
    ACTIVE = "ACTIVE"
    WEAKENING = "WEAKENING"
    REMOVED = "REMOVED"
    REQUALIFIED = "REQUALIFIED"
    DATA_STALE = "DATA_STALE"


# The states in which an instrument is still tracked for the day. ``is_active``
# in ``daily_watchlists`` means exactly this, so both stores and the state
# machine must agree on one definition rather than three near-identical tuples.
TRACKED_STATES = frozenset(
    {
        WatchlistState.ACTIVE,
        WatchlistState.WEAKENING,
        WatchlistState.REQUALIFIED,
        WatchlistState.DATA_STALE,
    }
)


def is_tracked(state: WatchlistState) -> bool:
    return state in TRACKED_STATES


class SignalType(StrEnum):
    BULLISH_ENTRY = "BULLISH_ENTRY"
    WATCHLIST_ADDED = "WATCHLIST_ADDED"
    WATCHLIST_WEAKENING = "WATCHLIST_WEAKENING"
    WATCHLIST_REMOVED = "WATCHLIST_REMOVED"
    WATCHLIST_REQUALIFIED = "WATCHLIST_REQUALIFIED"
    DATA_FEED_INTERRUPTED = "DATA_FEED_INTERRUPTED"
    DATA_FEED_RECOVERED = "DATA_FEED_RECOVERED"
    DAILY_SCAN_COMPLETE = "DAILY_SCAN_COMPLETE"
    # Two-timeframe RSI transitions (operator rule 2026-10-12): both daily and
    # weekly RSI above 60 → BULLISH, both below 40 → BEARISH. Raised only when
    # the pair *changes* state, never repeatedly for a standing state.
    RSI_BULLISH = "RSI_BULLISH"
    RSI_BEARISH = "RSI_BEARISH"


class DeliveryStatus(StrEnum):
    QUEUED = "QUEUED"
    IN_FLIGHT = "IN_FLIGHT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    PERMANENTLY_FAILED = "PERMANENTLY_FAILED"
    SUPPRESSED = "SUPPRESSED"     # cooldown / dedupe decided not to notify
    SIMULATED = "SIMULATED"       # simulation mode: never sent to a provider


class Channel(StrEnum):
    TELEGRAM = "TELEGRAM"
    DASHBOARD = "DASHBOARD"


class SessionStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    INITIALIZED = "INITIALIZED"
    FINALIZED = "FINALIZED"
    ABORTED = "ABORTED"


def ensure_aware(value: datetime, *, context: str = "datetime") -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{context} must be timezone-aware, got naive {value!r}")
    return value


@dataclass(frozen=True)
class Instrument:
    """A validated NSE equity in the configured universe."""

    id: int
    exchange: str
    symbol: str
    trading_symbol: str
    company_name: str
    instrument_token: str
    instrument_type: str = "EQ"
    is_nifty500: bool = True
    # UNIVERSE=FNO filters on this flag (sync recomputes it from the scrip
    # master's NFO underlyings); NIFTY500 rows keep it False unless they are
    # also F&O — the flag is additive, not a replacement.
    is_fno: bool = False
    is_active: bool = True
    mapping_status: MappingStatus = MappingStatus.MAPPED
    last_verified_at: datetime | None = None
    previous_close: float | None = None

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.instrument_token}"


@dataclass(frozen=True)
class Tick:
    """One parsed market-data event for one instrument.

    ``exchange_timestamp`` is when NSE matched the trade; ``received_at`` is
    when this process got the frame. Both are stored: candle bucketing uses the
    exchange clock, staleness detection uses the receipt clock (spec §7.3).
    """

    instrument_id: int
    instrument_token: str
    exchange: str
    exchange_timestamp: datetime
    received_at: datetime
    last_traded_price: float | None = None
    last_traded_quantity: int | None = None
    cumulative_volume: int | None = None      # day volume reported by the feed
    average_traded_price: float | None = None
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    previous_close: float | None = None
    sequence_number: int | None = None
    feed_mode: int | None = None
    is_simulated: bool = False

    def __post_init__(self) -> None:
        ensure_aware(self.exchange_timestamp, context="Tick.exchange_timestamp")
        ensure_aware(self.received_at, context="Tick.received_at")
        if self.last_traded_price is not None and self.last_traded_price <= 0:
            raise ValueError(f"tick price must be positive, got {self.last_traded_price}")


@dataclass(frozen=True)
class IndexTick:
    """One parsed quote event for an index (the sector tile), not a stock.

    Indices are not scanned, so they never enter the candle pipeline: they are
    routed straight to the sector tracker. ``previous_close`` is whatever the
    feed reported — when absent, the change percentage stays ``None`` rather
    than being inferred from the session open.
    """

    index_symbol: str
    instrument_token: str
    exchange_timestamp: datetime
    received_at: datetime
    last_price: float
    previous_close: float | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.exchange_timestamp, context="IndexTick.exchange_timestamp")
        ensure_aware(self.received_at, context="IndexTick.received_at")
        if self.last_price <= 0:
            raise ValueError(f"index tick price must be positive, got {self.last_price}")


@dataclass(frozen=True)
class SectorIndex:
    """A Nifty sector index the feed should subscribe (dashboard tile)."""

    index_symbol: str
    display_name: str
    instrument_token: str
    display_order: int = 0


@dataclass(frozen=True)
class SectorQuote:
    """The live scratchpad row behind one sector-tile chip."""

    index_symbol: str
    last_price: float
    previous_close: float | None = None
    change_pct: float | None = None
    source_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.source_updated_at is not None:
            ensure_aware(self.source_updated_at, context="SectorQuote.source_updated_at")


@dataclass(frozen=True)
class DailyCandle:
    """One adjusted daily OHLCV bar (``candles_1d``).

    ``rsi14`` is the Wilder RSI-14 of this bar's close over the instrument's
    daily series; it is ``None`` for the first 14 bars of history, where the
    indicator is not defined. ``source`` records who wrote the bar — daily bars
    are a one-shot batch fact, not a live stream, so provenance is cheap and
    useful after a corporate action rewrites history.
    """

    instrument_id: int
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    rsi14: float | None = None
    source: str = "angel-one-day"

    def __post_init__(self) -> None:
        if self.close <= 0:
            raise ValueError(f"daily close must be positive, got {self.close}")
        if self.high < self.low:
            raise ValueError(f"daily high {self.high} below low {self.low}")


@dataclass(frozen=True)
class WeeklyCandle:
    """One ISO-week bar (``candles_1w``), resampled from daily bars.

    ``week_start`` is the Monday of the week; the newest row may be a partial
    week whose close is the latest daily close — the value the operator sees on
    a live chart.
    """

    instrument_id: int
    week_start: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    rsi14: float | None = None
    source: str = "resampled-from-candles-1d"


@dataclass(frozen=True)
class StockTechnicals:
    """The candles-derived half of ``stock_details`` (written by sync-daily).

    A full-history upsert per run: all fields are derived from the same daily
    series the sync just refreshed, so they always agree with each other.
    ``data_start`` records how far back the ATH/ATL actually reach — the column
    is labeled "All Time" but the broker's history begins in 2002, and the
    dashboard says so rather than implying 1977 for RELIANCE.
    """

    instrument_id: int
    daily_rsi: float | None
    weekly_rsi: float | None
    rsi_bias: str | None
    rsi_as_of: date | None
    data_start: date | None
    high_52w: float | None
    low_52w: float | None
    ath: float | None
    atl: float | None


@dataclass(frozen=True)
class StockProfile:
    """The fundamentals half of ``stock_details`` (written by sync-profiles).

    ``profile_source`` names the upstreams that contributed in this run
    (``"yahoo+equity_l"``); a Yahoo miss must not erase a listing date that the
    NSE equity list provided, so those two merge per column, not per row.
    """

    instrument_id: int
    pe_ratio: float | None
    market_cap_cr: float | None
    listing_date: date | None
    profile_source: str


@dataclass(frozen=True)
class RsiState:
    """What the worker reads at session start to gate two-timeframe RSI alerts.

    ``rsi_bias`` is the stored classification for the newest synced session;
    ``last_alert_bias`` is the last bias the worker *alerted on* (or baselined
    silently). The engine emits a signal only when the two differ, so a stock
    that sits above 60/60 for weeks alerts once, not every morning.
    """

    instrument_id: int
    daily_rsi: float | None
    weekly_rsi: float | None
    rsi_bias: str | None
    last_alert_bias: str | None


@dataclass
class Candle:
    """A one-minute OHLCV bucket in the regular NSE session."""

    instrument_id: int
    trading_date: date
    candle_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    is_complete: bool = False
    data_quality: DataQuality = DataQuality.INCOMPLETE
    source_timestamp: datetime | None = None
    tick_count: int = 0
    vwap_price_sum: float = 0.0
    is_simulated: bool = False

    def __post_init__(self) -> None:
        ensure_aware(self.candle_start, context="Candle.candle_start")

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def candle_end(self) -> datetime:
        return self.candle_start + timedelta(minutes=1)

    def to_row(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "trading_date": self.trading_date,
            "candle_start_at": self.candle_start,
            "open": round(self.open, 4),
            "high": round(self.high, 4),
            "low": round(self.low, 4),
            "close": round(self.close, 4),
            "volume": int(self.volume),
            "is_complete": self.is_complete,
            "data_quality_status": self.data_quality.value,
            "source_timestamp": self.source_timestamp,
            "tick_count": self.tick_count,
            "is_simulated": self.is_simulated,
        }


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Indicator values evaluated on one completed candle (spec §9, §13.5)."""

    instrument_id: int
    trading_date: date
    candle_start: datetime
    vwap: float | None
    ema_fast: float | None
    ema_slow: float | None
    average_volume: float | None
    relative_volume: float | None
    intraday_change_pct: float | None
    distance_from_vwap_pct: float | None
    ema_spread_pct: float | None
    close: float
    previous_close: float | None
    session_high: float | None
    session_low: float | None
    distance_from_high_pct: float | None
    bullish_qualified: bool
    reasons: tuple[str, ...]
    failed_reasons: tuple[str, ...]
    data_quality: DataQuality
    warming_up: bool
    score: float | None = None
    rank: int | None = None
    is_simulated: bool = False
    config_version: int = 1

    def __post_init__(self) -> None:
        ensure_aware(self.candle_start, context="IndicatorSnapshot.candle_start")


@dataclass
class WatchlistMembership:
    """The tracked row for one instrument in one session.

    Deliberately *not* frozen: :class:`~worker.watchlist.WatchlistManager`
    applies a transition by writing it through the store and then refreshing
    its in-memory view of the row, so the next candle sees the state that was
    just persisted without re-reading the database. The store remains the
    source of truth; this object is that store row's working copy.
    """

    id: int
    trading_session_id: int
    instrument_id: int
    current_state: WatchlistState
    initial_entry_at: datetime | None
    latest_state_change_at: datetime | None
    initial_entry_price: float | None
    latest_price: float | None
    latest_score: float | None
    removal_reason: str | None
    is_active: bool
    consecutive_weak_candles: int = 0
    episode_count: int = 1
    is_simulated: bool = False
    last_indicator_snapshot_id: int | None = None
    latest_rank: int | None = None


@dataclass(frozen=True)
class WatchlistEvent:
    watchlist_id: int
    event_type: str
    previous_state: WatchlistState | None
    new_state: WatchlistState
    event_timestamp: datetime
    reason: str
    indicator_snapshot_id: int | None = None
    price: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    """A persisted, idempotent notification-worthy event (spec §12)."""

    instrument_id: int | None
    trading_session_id: int | None
    signal_type: SignalType
    qualifying_episode_id: str
    idempotency_key: str
    event_timestamp: datetime
    candle_start: datetime | None = None
    price: float | None = None
    vwap: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    relative_volume: float | None = None
    intraday_change_pct: float | None = None
    score: float | None = None
    reasons: tuple[str, ...] = ()
    message: str | None = None
    config_version: int = 1
    is_simulated: bool = False
    symbol: str | None = None
    company_name: str | None = None
    exchange: str | None = None
    previous_close: float | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.event_timestamp, context="Signal.event_timestamp")


@dataclass
class NotificationDelivery:
    id: int | None
    signal_id: int
    channel: Channel
    destination_reference: str
    delivery_status: DeliveryStatus
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    provider_message_id: str | None = None
    last_error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerHeartbeat:
    worker_id: str
    status: str
    last_market_data_at: datetime | None = None
    last_completed_candle_at: datetime | None = None
    subscribed_instrument_count: int = 0
    active_watchlist_count: int = 0
    pending_notification_count: int = 0
    queue_depth: int = 0
    last_error: str | None = None
    feed_status: FeedStatus = FeedStatus.DISCONNECTED
    universe_total: int = 0
    mapped_total: int = 0
    monitored_total: int = 0
    # §17 honesty rule: a simulated worker must never claim to be live on the
    # operator dashboard. Defaults match the column defaults so an unwired
    # construction still round-trips, but the pipeline always sets these.
    scan_mode: str = "live"
    simulation_label: bool = False


@dataclass(frozen=True)
class ScannerConfigRow:
    id: int
    name: str
    params: Any  # ScannerParams (typed at the call site to avoid a cycle)
    is_enabled: bool
    config_version: int
    created_by: str = "system"

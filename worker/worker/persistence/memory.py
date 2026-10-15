"""In-memory :class:`ScannerStore` implementation.

Used by the simulator, the automated test suite, and a local run before Angel
One / database credentials exist. It is a faithful behavioural clone of
``PostgresStore`` for the rules the pipeline depends on:

* one candle / one indicator snapshot row per (instrument, candle minute),
* unique signal idempotency keys — a retry returns ``created=False``,
* watchlist membership unique per (session, instrument) with event history,
* notification queue rows unique per (signal, channel) with claim semantics.

It is deliberately not a substitute for production storage: state dies with the
process, which is exactly why the worker requires PostgreSQL in live mode.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta
from typing import Any

from ..config import ScannerParams
from ..models import (
    Candle,
    Channel,
    DailyCandle,
    DeliveryStatus,
    DiscoverySnapshotRow,
    FeedStatus,
    IndicatorSnapshot,
    Instrument,
    MappingStatus,
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
    is_tracked,
)
from .store import (
    ALERTED_DELIVERY_STATUSES,
    ConfigRow,
    CoverageCounts,
    NotificationClaim,
    ScannerStore,
    SessionRow,
    SignalInsertResult,
)


class InMemoryStore(ScannerStore):
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.instruments: dict[int, Instrument] = {}
        self.sessions: dict[int, SessionRow] = {}
        self.configs: dict[int, ConfigRow] = {}
        self.candles: dict[tuple[int, datetime], Candle] = {}
        self.snapshots: dict[tuple[int, datetime], IndicatorSnapshot] = {}
        self.snapshot_ids: dict[tuple[int, datetime], int] = {}
        self.memberships: dict[int, WatchlistMembership] = {}
        self.membership_keys: dict[tuple[int, int], int] = {}
        self.events: list[WatchlistEvent] = []
        self.signals: dict[int, Signal] = {}
        self.signal_by_key: dict[str, int] = {}
        self.deliveries: dict[int, NotificationDelivery] = {}
        self.delivery_keys: dict[tuple[int, Channel], int] = {}
        self.claimed: set[int] = set()
        self.heartbeats: dict[str, WorkerHeartbeat] = {}
        self.summaries: dict[date, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []
        self.leases: dict[str, tuple[str, datetime]] = {}
        self.coverage_counts = CoverageCounts()
        self.subscribed_ids: set[int] = set()
        self.sector_indices: dict[str, SectorIndex] = {}
        self.sector_quotes: dict[str, SectorQuote] = {}
        self.ops_settings: dict[str, dict[str, object]] = {}
        # candles_1d / candles_1w keyed by (instrument, bar start), newest-wins
        # exactly like the Postgres PKs. stock_details is one merged column
        # bag per instrument because the two writers own disjoint columns —
        # mirroring the single-row-per-instrument table rather than splitting
        # it and having to re-merge on every read.
        self.daily_candles: dict[tuple[int, date], DailyCandle] = {}
        self.weekly_candles: dict[tuple[int, date], WeeklyCandle] = {}
        # daily_discovery_snapshots keyed by (trading_date, instrument), the
        # table's primary key: an upsert replaces the day's verdict in place.
        self.discovery_snapshots: dict[tuple[date, int], DiscoverySnapshotRow] = {}
        self.stock_details: dict[int, dict[str, object]] = {}
        self.connected = False

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def healthcheck(self) -> bool:
        return self.connected

    # ── instruments ─────────────────────────────────────────────────────────
    async def upsert_instruments(self, instruments: list[Instrument]) -> int:
        written = 0
        for item in instruments:
            existing = self._find_instrument(item.exchange, item.instrument_token)
            if existing is None and item.id:
                self.instruments[item.id] = item
                existing = item
                written += 1
                continue
            if existing is None:
                new_id = next(self._ids)
                stored = Instrument(**{**item.__dict__, "id": new_id})
                self.instruments[new_id] = stored
                written += 1
            else:
                merged = Instrument(**{**existing.__dict__, **{k: v for k, v in item.__dict__.items() if v not in (None, "")}})
                self.instruments[existing.id] = merged
                written += 1
        return written

    def _find_instrument(self, exchange: str, token: str) -> Instrument | None:
        for item in self.instruments.values():
            if item.exchange == exchange and item.instrument_token == token:
                return item
        return None

    async def list_universe(self, *, universe: str = "NIFTY500", mapped_only: bool = True) -> list[Instrument]:
        rows = [
            item
            for item in self.instruments.values()
            if (universe != "NIFTY500" or item.is_nifty500)
            and (universe != "FNO" or item.is_fno)
            and item.is_active
            and (not mapped_only or item.mapping_status == MappingStatus.MAPPED)
        ]
        return sorted(rows, key=lambda row: row.symbol)

    async def get_instrument(self, instrument_id: int) -> Instrument | None:
        return self.instruments.get(instrument_id)

    async def set_previous_closes(self, values: dict[int, float]) -> int:
        updated = 0
        for instrument_id, close in values.items():
            item = self.instruments.get(instrument_id)
            if item is None:
                continue
            self.instruments[instrument_id] = Instrument(**{**item.__dict__, "previous_close": close})
            updated += 1
        return updated

    async def mark_mapping_status(
        self, unmapped_symbols: list[str], status: str, *, universe: str = "NIFTY500"
    ) -> int:
        count = 0
        wanted = {symbol.upper() for symbol in unmapped_symbols}
        for item in list(self.instruments.values()):
            if item.symbol.upper() in wanted:
                self.instruments[item.id] = Instrument(
                    **{**item.__dict__, "mapping_status": MappingStatus(status)}
                )
                count += 1
        return count

    async def coverage(self, *, universe: str = "NIFTY500") -> CoverageCounts:
        rows = [
            item
            for item in self.instruments.values()
            if (item.is_nifty500 or universe != "NIFTY500")
            and (item.is_fno or universe != "FNO")
        ]
        # ``monitored`` must mean "data actually arrived", not "we asked for it":
        # the Postgres store reads it from the heartbeat, which the pipeline fills
        # from the instruments it has seen ticks for (§5.2 — never imply full
        # coverage from a subscription list).
        printed = {instrument_id for instrument_id, _ in self.candles}
        return CoverageCounts(
            universe_total=len(rows),
            mapped_total=sum(1 for r in rows if r.mapping_status == MappingStatus.MAPPED),
            subscribed_total=len(self.subscribed_ids),
            monitored_total=len(printed & self.subscribed_ids),
            unmapped_total=sum(1 for r in rows if r.mapping_status == MappingStatus.UNMAPPED),
            ambiguous_total=sum(1 for r in rows if r.mapping_status == MappingStatus.AMBIGUOUS),
            invalid_total=sum(1 for r in rows if r.mapping_status == MappingStatus.INVALID),
        )

    def set_subscribed(self, instrument_ids: set[int]) -> None:
        """Test/simulation hook mirroring the worker's subscription bookkeeping."""
        self.subscribed_ids = set(instrument_ids)

    async def set_fno_symbols(self, symbols: set[str]) -> int:
        wanted = {symbol.upper() for symbol in symbols}
        changed = 0
        for item in list(self.instruments.values()):
            if item.exchange != "NSE":
                continue
            flag = item.symbol.upper() in wanted
            if flag != item.is_fno:
                self.instruments[item.id] = Instrument(**{**item.__dict__, "is_fno": flag})
                changed += 1
        return changed

    async def upsert_sector_indices(self, rows: list[SectorIndex]) -> int:
        self.sector_indices = {row.index_symbol: row for row in rows}
        return len(rows)

    async def list_sector_indices(self) -> list[SectorIndex]:
        return sorted(
            self.sector_indices.values(), key=lambda row: (row.display_order, row.index_symbol)
        )

    async def upsert_sector_quotes(self, rows: list[SectorQuote]) -> int:
        writable = [row for row in rows if row.source_updated_at is not None]
        for row in writable:
            existing = self.sector_quotes.get(row.index_symbol)
            # Newest-wins on the exchange clock, matching the Postgres scratchpad.
            if existing is None or (row.source_updated_at and existing.source_updated_at
                                    and row.source_updated_at >= existing.source_updated_at):
                self.sector_quotes[row.index_symbol] = row
        return len(writable)

    async def list_sector_quotes(self) -> list[SectorQuote]:
        return list(self.sector_quotes.values())

    # ── daily/weekly bars + stock details (two-timeframe RSI) ───────────────
    async def upsert_daily_candles(self, rows: list[DailyCandle]) -> int:
        for row in rows:
            self.daily_candles[(row.instrument_id, row.trading_date)] = row
        return len(rows)

    async def upsert_weekly_candles(self, rows: list[WeeklyCandle]) -> int:
        for row in rows:
            self.weekly_candles[(row.instrument_id, row.week_start)] = row
        return len(rows)

    async def load_daily_candles(self, instrument_id: int) -> list[DailyCandle]:
        rows = [row for (iid, _), row in self.daily_candles.items() if iid == instrument_id]
        return sorted(rows, key=lambda row: row.trading_date)

    async def load_weekly_candles(self, instrument_id: int) -> list[WeeklyCandle]:
        rows = [row for (iid, _), row in self.weekly_candles.items() if iid == instrument_id]
        return sorted(rows, key=lambda row: row.week_start)

    async def upsert_discovery_snapshots(self, rows: list[DiscoverySnapshotRow]) -> int:
        for row in rows:
            self.discovery_snapshots[(row.trading_date, row.instrument_id)] = row
        return len(rows)

    async def load_discovery_snapshots(self, trading_date: date) -> list[DiscoverySnapshotRow]:
        rows = [row for (day, _), row in self.discovery_snapshots.items() if day == trading_date]
        return sorted(rows, key=lambda row: row.instrument_id)

    async def update_stock_technicals(self, rows: list[StockTechnicals]) -> int:
        for row in rows:
            detail = self.stock_details.setdefault(row.instrument_id, {})
            detail.update(
                {
                    "daily_rsi": row.daily_rsi,
                    "weekly_rsi": row.weekly_rsi,
                    "rsi_bias": row.rsi_bias,
                    "rsi_as_of": row.rsi_as_of,
                    # COALESCE semantics, matching the SQL upsert.
                    "data_start": row.data_start
                    if row.data_start is not None
                    else detail.get("data_start"),
                    "high_52w": row.high_52w,
                    "low_52w": row.low_52w,
                    "ath": row.ath,
                    "atl": row.atl,
                }
            )
        return len(rows)

    async def upsert_stock_profiles(self, rows: list[StockProfile]) -> int:
        for row in rows:
            detail = self.stock_details.setdefault(row.instrument_id, {})
            detail.update(
                {
                    "pe_ratio": row.pe_ratio,
                    "market_cap_cr": row.market_cap_cr,
                    # COALESCE semantics, matching the SQL upsert: the NSE
                    # equity list is the only listing-date source, so a
                    # Yahoo-only run must not blank it.
                    "listing_date": row.listing_date
                    if row.listing_date is not None
                    else detail.get("listing_date"),
                    "profile_source": row.profile_source,
                }
            )
        return len(rows)

    async def list_rsi_states(self) -> list[RsiState]:
        return [
            RsiState(
                instrument_id=instrument_id,
                daily_rsi=detail.get("daily_rsi"),
                weekly_rsi=detail.get("weekly_rsi"),
                rsi_bias=detail.get("rsi_bias"),
                last_alert_bias=detail.get("last_alert_bias"),
            )
            for instrument_id, detail in sorted(self.stock_details.items())
        ]

    async def set_last_alert_bias(self, instrument_id: int, bias: str) -> None:
        detail = self.stock_details.get(instrument_id)
        # Parity with the SQL UPDATE: a missing row is silently unaffected
        # (the concrete statement would match zero rows).
        if detail is not None:
            detail["last_alert_bias"] = bias

    async def purge_older_than(self, days: int, *, now: datetime) -> dict[str, int]:
        cutoff = now - timedelta(days=days)
        dead_candles = [key for key in self.candles if key[1] < cutoff]
        for key in dead_candles:
            del self.candles[key]
        dead_snapshots = [key for key in self.snapshots if key[1] < cutoff]
        for key in dead_snapshots:
            del self.snapshots[key]
        return {"candles_1m": len(dead_candles), "indicator_snapshots": len(dead_snapshots)}

    async def get_ops_setting(self, key: str) -> dict[str, object] | None:
        return self.ops_settings.get(key)

    # ── sessions ────────────────────────────────────────────────────────────
    async def ensure_session(
        self,
        trading_date: date,
        session_open_at: datetime,
        session_close_at: datetime,
        *,
        exchange: str = "NSE",
    ) -> SessionRow:
        for row in self.sessions.values():
            # ``SessionRow`` carries no exchange column because this system is
            # NSE-only; the parameter exists for parity with PostgresStore.
            if row.trading_date == trading_date:
                return row
        new = SessionRow(
            id=next(self._ids),
            trading_date=trading_date,
            session_status="PENDING",
            session_open_at=session_open_at,
            session_close_at=session_close_at,
        )
        self.sessions[new.id] = new
        return new

    async def set_session_status(self, session_id: int, status: str) -> None:
        row = self.sessions[session_id]
        self.sessions[session_id] = SessionRow(**{**row.__dict__, "session_status": status})

    async def mark_initial_scan_completed(self, session_id: int, at: datetime) -> None:
        row = self.sessions[session_id]
        self.sessions[session_id] = SessionRow(**{**row.__dict__, "initial_scan_completed_at": at})

    async def add_stale_seconds(self, session_id: int, seconds: int) -> None:
        row = self.sessions[session_id]
        self.sessions[session_id] = SessionRow(
            **{**row.__dict__, "stale_data_seconds": row.stale_data_seconds + max(0, seconds)}
        )

    async def finalize_session(self, session_id: int, at: datetime) -> None:
        row = self.sessions[session_id]
        self.sessions[session_id] = SessionRow(
            **{**row.__dict__, "session_status": "FINALIZED", "finalized_at": at}
        )

    async def get_session_for_date(self, trading_date: date, *, exchange: str = "NSE") -> SessionRow | None:
        for row in self.sessions.values():
            if row.trading_date == trading_date:
                return row
        return None

    async def get_open_session(self) -> SessionRow | None:
        open_rows = [row for row in self.sessions.values() if row.session_status != "FINALIZED"]
        if not open_rows:
            return None
        return max(open_rows, key=lambda row: row.trading_date)

    # ── configuration ───────────────────────────────────────────────────────
    async def active_config(self, *, name: str = "default") -> ConfigRow | None:
        rows = [row for row in self.configs.values() if row.name == name]
        if not rows:
            return None
        return max(rows, key=lambda row: row.config_version)

    async def save_config(self, params: ScannerParams, *, name: str = "default", created_by: str) -> ConfigRow:
        current = await self.active_config(name=name)
        version = 1 if current is None else current.config_version + 1
        row = ConfigRow(
            id=next(self._ids),
            name=name,
            params=ScannerParams(**{**params.__dict__, "config_version": version}),
            is_enabled=params.is_enabled,
            config_version=version,
        )
        self.configs[row.id] = row
        return row

    # ── candles + indicators ────────────────────────────────────────────────
    async def upsert_candles(self, candles: list[Candle]) -> int:
        for candle in candles:
            self.candles[(candle.instrument_id, candle.candle_start)] = candle
        return len(candles)

    async def load_completed_candles(
        self, instrument_ids: list[int], *, until: datetime, limit_per_instrument: int
    ) -> dict[int, list[Candle]]:
        out: dict[int, list[Candle]] = {instrument_id: [] for instrument_id in instrument_ids}
        for (instrument_id, start), candle in self.candles.items():
            if instrument_id in out and candle.is_complete and start <= until:
                out[instrument_id].append(candle)
        for instrument_id, rows in out.items():
            rows.sort(key=lambda row: row.candle_start)
            out[instrument_id] = rows[-limit_per_instrument:]
        return out

    async def latest_completed_candle_at(self, instrument_id: int) -> datetime | None:
        stamps = [
            start
            for (iid, start), candle in self.candles.items()
            if iid == instrument_id and candle.is_complete
        ]
        return max(stamps) if stamps else None

    async def upsert_indicator_snapshot(self, snapshot: IndicatorSnapshot) -> int:
        key = (snapshot.instrument_id, snapshot.candle_start)
        existing_id = self.snapshot_ids.get(key)
        if existing_id is None:
            new_id = next(self._ids)
            self.snapshot_ids[key] = new_id
            self.snapshots[key] = snapshot
            return new_id
        self.snapshots[key] = snapshot
        return existing_id

    async def count_candles_for_session(self, session_id: int) -> int:
        session = self.sessions.get(session_id)
        if session is None:
            return 0
        return sum(
            1
            for candle in self.candles.values()
            if candle.trading_date == session.trading_date and candle.is_complete
        )

    # ── watchlist ───────────────────────────────────────────────────────────
    async def get_membership(self, session_id: int, instrument_id: int) -> WatchlistMembership | None:
        membership_id = self.membership_keys.get((session_id, instrument_id))
        return self.memberships.get(membership_id) if membership_id else None

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
    ) -> WatchlistMembership:
        key = (session_id, instrument_id)
        if key in self.membership_keys:  # unique (session, instrument) in Postgres
            return self.memberships[self.membership_keys[key]]
        membership = WatchlistMembership(
            id=next(self._ids),
            trading_session_id=session_id,
            instrument_id=instrument_id,
            current_state=state,
            initial_entry_at=entered_at,
            latest_state_change_at=entered_at,
            initial_entry_price=price,
            latest_price=price,
            latest_score=score,
            removal_reason=None,
            is_active=is_tracked(state),
            consecutive_weak_candles=0,
            episode_count=1,
            is_simulated=is_simulated,
            last_indicator_snapshot_id=snapshot_id,
        )
        self.memberships[membership.id] = membership
        self.membership_keys[key] = membership.id
        return membership

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
    ) -> None:
        current = self.memberships[membership_id]
        self.memberships[membership_id] = WatchlistMembership(
            **{
                **current.__dict__,
                **{
                    k: v
                    for k, v in {
                        "current_state": state,
                        "latest_state_change_at": state_changed_at,
                        "latest_price": latest_price,
                        "latest_score": latest_score,
                        "latest_rank": latest_rank,
                        "removal_reason": removal_reason,
                        "consecutive_weak_candles": consecutive_weak_candles,
                        "episode_count": episode_count,
                        "last_indicator_snapshot_id": snapshot_id,
                        "is_active": is_active,
                    }.items()
                    if v is not None
                },
            }
        )

    async def add_watchlist_event(self, event: WatchlistEvent) -> int:
        self.events.append(event)
        return len(self.events)

    async def list_memberships(
        self, session_id: int, *, states: tuple[WatchlistState, ...] = (), include_inactive: bool = True
    ) -> list[WatchlistMembership]:
        rows = [row for row in self.memberships.values() if row.trading_session_id == session_id]
        if states:
            rows = [row for row in rows if row.current_state in states]
        if not include_inactive:
            rows = [row for row in rows if row.is_active]
        return sorted(rows, key=lambda row: (row.latest_score is None, -(row.latest_score or 0)))

    async def freeze_session_watchlist(self, session_id: int) -> int:
        frozen = 0
        for membership_id, row in list(self.memberships.items()):
            if row.trading_session_id == session_id and row.is_active:
                self.memberships[membership_id] = WatchlistMembership(**{**row.__dict__, "is_active": False})
                frozen += 1
        return frozen

    # ── signals + notifications ─────────────────────────────────────────────
    async def insert_signal(self, signal: Signal) -> SignalInsertResult:
        existing_id = self.signal_by_key.get(signal.idempotency_key)
        if existing_id is not None:
            return SignalInsertResult(signal_id=existing_id, created=False)
        new_id = next(self._ids)
        signal.id = new_id
        self.signals[new_id] = signal
        self.signal_by_key[signal.idempotency_key] = new_id
        return SignalInsertResult(signal_id=new_id, created=True)

    async def last_pushed_signal_at(
        self,
        instrument_id: int,
        signal_type: str,
        *,
        since: datetime,
        reason: str | None = None,
    ) -> datetime | None:
        pushed_signal_ids = {
            delivery.signal_id
            for delivery in self.deliveries.values()
            if delivery.delivery_status in ALERTED_DELIVERY_STATUSES
        }
        stamps = [
            row.event_timestamp
            for row in self.signals.values()
            if row.id in pushed_signal_ids
            and row.instrument_id == instrument_id
            and row.signal_type.value == signal_type
            and row.event_timestamp >= since
            # Mirrors the SQL containment check, so a cooldown test that passes
            # against the in-memory store means the same thing against Postgres.
            and (reason is None or reason in row.reasons)
        ]
        return max(stamps) if stamps else None

    async def count_signals_since(self, session_id: int, since: datetime) -> int:
        return sum(
            1
            for row in self.signals.values()
            if row.trading_session_id == session_id and row.event_timestamp >= since
        )

    async def enqueue_notification(self, delivery: NotificationDelivery) -> int:
        key = (delivery.signal_id, delivery.channel)
        if key in self.delivery_keys:
            return self.delivery_keys[key]
        delivery.id = next(self._ids)
        self.deliveries[delivery.id] = delivery
        self.delivery_keys[key] = delivery.id
        return delivery.id

    async def claim_due_notifications(
        self, *, channel: Channel, now: datetime, limit: int = 10
    ) -> list[NotificationClaim]:
        due = [
            row
            for row in self.deliveries.values()
            if row.channel == channel
            and row.id not in self.claimed
            and row.delivery_status in (DeliveryStatus.QUEUED, DeliveryStatus.FAILED)
            and (row.next_retry_at is None or row.next_retry_at <= now)
        ]
        # FIFO by queue position. ``created_at`` is not part of the delivery
        # model, so ordering by the surrogate id is the only honest choice.
        due.sort(key=lambda row: row.id)
        claims: list[NotificationClaim] = []
        for row in due[:limit]:
            self.claimed.add(row.id)
            self.deliveries[row.id] = NotificationDelivery(**{**row.__dict__, "delivery_status": DeliveryStatus.IN_FLIGHT})
            claims.append(NotificationClaim(delivery=self.deliveries[row.id], channel=channel))
        return claims

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
    ) -> None:
        row = self.deliveries[delivery_id]
        self.deliveries[delivery_id] = NotificationDelivery(
            **{
                **row.__dict__,
                "delivery_status": status,
                "attempt_count": attempt_count,
                "last_attempt_at": last_attempt_at,
                "delivered_at": delivered_at or row.delivered_at,
                "next_retry_at": next_retry_at,
                "provider_message_id": provider_message_id or row.provider_message_id,
                "last_error": last_error,
            }
        )
        self.claimed.discard(delivery_id)

    async def pending_notification_count(self, *, channel: Channel | None = None) -> int:
        return sum(
            1
            for row in self.deliveries.values()
            if row.delivery_status in (DeliveryStatus.QUEUED, DeliveryStatus.FAILED, DeliveryStatus.IN_FLIGHT)
            and (channel is None or row.channel == channel)
        )

    async def delivery_rollup(self, *, session_id: int) -> dict[str, int]:
        signal_ids = {
            key
            for key, row in self.signals.items()
            if row.trading_session_id == session_id
        }
        counts: dict[str, int] = {}
        for row in self.deliveries.values():
            if row.signal_id not in signal_ids:
                continue
            key = f"{row.channel.value}_{row.delivery_status.value}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    # ── operations ──────────────────────────────────────────────────────────
    async def upsert_heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        self.heartbeats[heartbeat.worker_id] = heartbeat

    async def write_daily_summary(
        self, session_id: int, trading_date: date, payload: dict[str, object]
    ) -> None:
        self.summaries[trading_date] = {"session_id": session_id, **payload}

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
    ) -> None:
        self.audit_rows.append(
            {
                "actor": actor,
                "actor_role": actor_role,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "previous_value": previous_value,
                "new_value": new_value,
                "request_ip": request_ip,
                "created_at": datetime.now(tz=self._tz()),
            }
        )

    @staticmethod
    def _tz():
        from ..timeutils import ist_zoneinfo

        return ist_zoneinfo()

    async def acquire_lease(self, name: str, holder: str, ttl_seconds: int, now: datetime) -> bool:
        current = self.leases.get(name)
        if current and current[0] != holder and current[1] > now:
            return False
        self.leases[name] = (holder, now + timedelta(seconds=ttl_seconds))
        return True

    async def renew_lease(self, name: str, holder: str, ttl_seconds: int, now: datetime) -> bool:
        current = self.leases.get(name)
        if current is None or current[0] != holder:
            return False
        self.leases[name] = (holder, now + timedelta(seconds=ttl_seconds))
        return True

    async def release_lease(self, name: str, holder: str) -> bool:
        current = self.leases.get(name)
        if current is None or current[0] != holder:
            return False
        del self.leases[name]
        return True

    # ── test helpers (not part of the production interface) ─────────────────
    def candle_rows(self, instrument_id: int) -> list[Candle]:
        return sorted(
            (candle for (iid, _), candle in self.candles.items() if iid == instrument_id),
            key=lambda row: row.candle_start,
        )

    def feed_status_probe(self) -> FeedStatus:
        return FeedStatus.STREAMING if self.connected else FeedStatus.DISCONNECTED

"""PostgreSQL :class:`ScannerStore` — the production twin of ``memory.py``.

All worker SQL lives in this file (see ``worker/persistence/store.py`` for why).
Non-obvious rules implemented here:

* one :class:`psycopg_pool.AsyncConnectionPool` for the whole process; every
  call checks out at most one connection, and every batched write uses
  ``executemany`` — never a connection per tick (spec §22);
* idempotency is enforced by the database's UNIQUE constraints, not by
  application memory (spec §12.3);
* ``claim_due_notifications`` uses ``FOR UPDATE SKIP LOCKED`` so two worker
  replicas can never dispatch the same delivery (spec §17 concurrency);
* psycopg returns ``Decimal`` for NUMERIC columns — converted to ``float`` on
  the way out of the store and rounded on the way in (prices NUMERIC(16,4),
  percentages NUMERIC(8,4), score NUMERIC(6,2));
* enum-like TEXT columns map onto the ``StrEnum`` classes in ``worker.models``.

Connection strings are never passed into log kwargs, even though the
structlog pipeline redacts them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..config import ScannerParams
from ..logging_setup import get_logger
from ..models import (
    Candle,
    Channel,
    DataQuality,
    DeliveryStatus,
    IndicatorSnapshot,
    Instrument,
    MappingStatus,
    NotificationDelivery,
    Signal,
    WatchlistEvent,
    WatchlistMembership,
    WatchlistState,
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

log = get_logger("worker.persistence.postgres")

#: Every table this store owns. Used by :meth:`PostgresStore.truncate_all`
#: (test-only) and as the schema contract checklist for reviewers.
SCHEMA_TABLES: tuple[str, ...] = (
    "instruments",
    "trading_sessions",
    "scanner_configs",
    "candles_1m",
    "indicator_snapshots",
    "daily_watchlists",
    "watchlist_events",
    "signals",
    "notification_deliveries",
    "worker_heartbeats",
    "audit_logs",
    "worker_leases",
    "daily_summaries",
)

# Columns daily_summaries accepts from an arbitrary summary payload.
_SUMMARY_COLUMNS: tuple[str, ...] = (
    "universe_count",
    "mapped_count",
    "subscribed_count",
    "monitored_count",
    "candles_persisted",
    "qualified_count",
    "warming_up_count",
    "excluded_count",
    "signals_generated",
    "telegram_delivered",
    "telegram_failed",
    "stale_data_seconds",
    "final_watchlist",
    "notes",
)

_CANDLE_UPSERT = """
INSERT INTO candles_1m (
    instrument_id, trading_date, candle_start_at,
    "open", "high", "low", "close", volume,
    is_complete, data_quality_status, source_timestamp, tick_count, is_simulated
) VALUES (
    %(instrument_id)s, %(trading_date)s, %(candle_start_at)s,
    %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s,
    %(is_complete)s, %(data_quality_status)s, %(source_timestamp)s,
    %(tick_count)s, %(is_simulated)s
)
ON CONFLICT (instrument_id, candle_start_at) DO UPDATE SET
    trading_date = EXCLUDED.trading_date,
    "open" = EXCLUDED."open",
    "high" = EXCLUDED."high",
    "low" = EXCLUDED."low",
    "close" = EXCLUDED."close",
    volume = EXCLUDED.volume,
    is_complete = EXCLUDED.is_complete,
    data_quality_status = EXCLUDED.data_quality_status,
    source_timestamp = EXCLUDED.source_timestamp,
    tick_count = EXCLUDED.tick_count,
    is_simulated = EXCLUDED.is_simulated
"""

_INSTRUMENT_UPSERT = """
INSERT INTO instruments (
    exchange, symbol, trading_symbol, company_name, instrument_token,
    instrument_type, is_nifty500, is_active, mapping_status,
    last_verified_at, previous_close
) VALUES (
    %(exchange)s, %(symbol)s, %(trading_symbol)s, %(company_name)s,
    %(instrument_token)s, %(instrument_type)s, %(is_nifty500)s, %(is_active)s,
    %(mapping_status)s, %(last_verified_at)s, %(previous_close)s
)
ON CONFLICT (exchange, instrument_token) DO UPDATE SET
    symbol = EXCLUDED.symbol,
    trading_symbol = EXCLUDED.trading_symbol,
    company_name = COALESCE(EXCLUDED.company_name, instruments.company_name),
    instrument_type = EXCLUDED.instrument_type,
    is_nifty500 = EXCLUDED.is_nifty500,
    is_active = EXCLUDED.is_active,
    mapping_status = EXCLUDED.mapping_status,
    last_verified_at = COALESCE(EXCLUDED.last_verified_at, instruments.last_verified_at),
    previous_close = COALESCE(EXCLUDED.previous_close, instruments.previous_close)
"""

_SESSION_SELECT_COLUMNS = """
    id, trading_date, session_status, session_open_at, session_close_at,
    initial_scan_completed_at, finalized_at, stale_data_seconds
"""

_CONFIG_SELECT_COLUMNS = """
    id, name, universe, fast_ema_period, slow_ema_period, volume_lookback,
    relative_volume_threshold, alert_cooldown_minutes, removal_confirm_candles,
    feed_staleness_threshold_seconds, candle_grace_seconds, initial_scan_candles,
    max_subscribed_instruments, rank_weights, is_enabled, config_version
"""

_MEMBERSHIP_SELECT_COLUMNS = """
    id, trading_session_id, instrument_id, current_state, initial_entry_at,
    latest_state_change_at, initial_entry_price, latest_price, latest_score,
    removal_reason, is_active, consecutive_weak_candles, episode_count,
    is_simulated, latest_rank, last_indicator_snapshot_id
"""

_DELIVERY_SELECT_COLUMNS = """
    id, signal_id, channel, destination_reference, delivery_status,
    attempt_count, next_retry_at, last_attempt_at, delivered_at,
    provider_message_id, last_error, payload
"""

# FOR UPDATE SKIP LOCKED inside the CTE is the anti-double-dispatch mechanism:
# a concurrent replica simply sees zero candidate rows (spec §17).
_CLAIM_DUE_SQL = """
WITH picked AS (
    SELECT nd.id
    FROM notification_deliveries nd
    WHERE nd.channel = %(channel)s
      AND nd.delivery_status IN ('QUEUED', 'FAILED')
      AND (nd.next_retry_at IS NULL OR nd.next_retry_at <= %(now)s)
    ORDER BY nd.created_at
    LIMIT %(limit)s
    FOR UPDATE OF nd SKIP LOCKED
)
UPDATE notification_deliveries target
SET delivery_status = 'IN_FLIGHT',
    attempt_count = target.attempt_count + 1,
    last_attempt_at = %(now)s
FROM picked
WHERE target.id = picked.id
RETURNING target.*
"""

# Two replicas cannot both hold the lease: the DO UPDATE guard fails closed.
_ACQUIRE_LEASE_SQL = """
INSERT INTO worker_leases (lease_name, holder, acquired_at, expires_at)
VALUES (%(name)s, %(holder)s, %(now)s, %(expires_at)s)
ON CONFLICT (lease_name) DO UPDATE SET
    holder = EXCLUDED.holder,
    acquired_at = %(now)s,
    expires_at = %(expires_at)s
WHERE worker_leases.holder = EXCLUDED.holder OR worker_leases.expires_at < %(now)s
RETURNING lease_name
"""

_RENEW_LEASE_SQL = """
UPDATE worker_leases
SET expires_at = %(expires_at)s
WHERE lease_name = %(name)s AND holder = %(holder)s
RETURNING lease_name
"""

# Only the holder may give the lease up; a stale replica that lost it must not
# be able to delete the new owner's row.
_RELEASE_LEASE_SQL = """
DELETE FROM worker_leases
WHERE lease_name = %(name)s AND holder = %(holder)s
RETURNING lease_name
"""


def _conninfo_with_tls(database_url: str) -> str:
    """Force a TLS-friendly conninfo without touching URLs that already say.

    Supabase pooler/ direct URLs arrive with ``?sslmode=require`` and must pass
    through byte-for-byte; only URLs that never mention sslmode get the
    default added. Non-URL DSNs (key/value form) are passed through unchanged.
    """
    if not database_url.startswith(("postgres://", "postgresql://")):
        return database_url
    if "sslmode" in database_url.lower():
        return database_url
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}sslmode=require"


def _f(value: Any) -> float | None:
    """psycopg returns Decimal for NUMERIC columns; the domain model is float."""
    return None if value is None else float(value)


def _r(value: float | None, ndigits: int = 4) -> float | None:
    return None if value is None else round(float(value), ndigits)


def _i(value: Any) -> int | None:
    """Nullable INTEGER/BIGINT column → ``int`` or ``None``, never ``0``."""
    return None if value is None else int(value)


def _instrument_from_row(row: dict[str, Any]) -> Instrument:
    return Instrument(
        id=int(row["id"]),
        exchange=row["exchange"],
        symbol=row["symbol"],
        trading_symbol=row["trading_symbol"],
        company_name=row["company_name"] or "",
        instrument_token=row["instrument_token"],
        instrument_type=row["instrument_type"],
        is_nifty500=bool(row["is_nifty500"]),
        is_active=bool(row["is_active"]),
        mapping_status=MappingStatus(row["mapping_status"]),
        last_verified_at=row["last_verified_at"],
        previous_close=_f(row["previous_close"]),
    )


def _candle_from_row(row: dict[str, Any]) -> Candle:
    return Candle(
        instrument_id=int(row["instrument_id"]),
        trading_date=row["trading_date"],
        candle_start=row["candle_start_at"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"]),
        is_complete=bool(row["is_complete"]),
        data_quality=DataQuality(row["data_quality_status"]),
        source_timestamp=row["source_timestamp"],
        tick_count=int(row["tick_count"]),
        is_simulated=bool(row["is_simulated"]),
    )


def _session_from_row(row: dict[str, Any]) -> SessionRow:
    return SessionRow(
        id=int(row["id"]),
        trading_date=row["trading_date"],
        session_status=row["session_status"],
        session_open_at=row["session_open_at"],
        session_close_at=row["session_close_at"],
        initial_scan_completed_at=row["initial_scan_completed_at"],
        finalized_at=row["finalized_at"],
        stale_data_seconds=int(row["stale_data_seconds"] or 0),
    )


def _params_from_row(row: dict[str, Any]) -> ScannerParams:
    weights = row["rank_weights"] or {}
    return ScannerParams(
        universe=row["universe"],
        fast_ema_period=int(row["fast_ema_period"]),
        slow_ema_period=int(row["slow_ema_period"]),
        volume_lookback=int(row["volume_lookback"]),
        relative_volume_threshold=float(row["relative_volume_threshold"]),
        alert_cooldown_minutes=int(row["alert_cooldown_minutes"]),
        removal_confirm_candles=int(row["removal_confirm_candles"]),
        feed_staleness_seconds=int(row["feed_staleness_threshold_seconds"]),
        candle_grace_seconds=int(row["candle_grace_seconds"]),
        initial_scan_candles=int(row["initial_scan_candles"]),
        max_subscribed_instruments=int(row["max_subscribed_instruments"]),
        rank_weight_price_strength=float(weights.get("price_strength", 0.30)),
        rank_weight_relative_volume=float(weights.get("relative_volume", 0.30)),
        rank_weight_vwap_strength=float(weights.get("vwap_strength", 0.20)),
        rank_weight_ema_spread=float(weights.get("ema_spread", 0.20)),
        is_enabled=bool(row["is_enabled"]),
        config_version=int(row["config_version"]),
    )


def _config_from_row(row: dict[str, Any]) -> ConfigRow:
    return ConfigRow(
        id=int(row["id"]),
        name=row["name"],
        params=_params_from_row(row),
        is_enabled=bool(row["is_enabled"]),
        config_version=int(row["config_version"]),
    )


def _membership_from_row(row: dict[str, Any]) -> WatchlistMembership:
    return WatchlistMembership(
        id=int(row["id"]),
        trading_session_id=int(row["trading_session_id"]),
        instrument_id=int(row["instrument_id"]),
        current_state=WatchlistState(row["current_state"]),
        initial_entry_at=row["initial_entry_at"],
        latest_state_change_at=row["latest_state_change_at"],
        initial_entry_price=_f(row["initial_entry_price"]),
        latest_price=_f(row["latest_price"]),
        latest_score=_f(row["latest_score"]),
        removal_reason=row["removal_reason"],
        is_active=bool(row["is_active"]),
        consecutive_weak_candles=int(row["consecutive_weak_candles"]),
        episode_count=int(row["episode_count"]),
        is_simulated=bool(row["is_simulated"]),
        # ``.get`` rather than ``[]``: an older column list must degrade to
        # ``None`` here instead of raising KeyError inside the hot path.
        last_indicator_snapshot_id=_i(row.get("last_indicator_snapshot_id")),
        latest_rank=_i(row.get("latest_rank")),
    )


def _delivery_from_row(row: dict[str, Any]) -> NotificationDelivery:
    return NotificationDelivery(
        id=int(row["id"]),
        signal_id=int(row["signal_id"]),
        channel=Channel(row["channel"]),
        destination_reference=row["destination_reference"] or "",
        delivery_status=DeliveryStatus(row["delivery_status"]),
        attempt_count=int(row["attempt_count"]),
        next_retry_at=row["next_retry_at"],
        last_attempt_at=row["last_attempt_at"],
        delivered_at=row["delivered_at"],
        provider_message_id=row["provider_message_id"],
        last_error=row["last_error"],
        payload=row["payload"] or {},
    )


class PostgresStore(ScannerStore):
    """Pooled, constraint-driven implementation of :class:`ScannerStore`."""

    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 6,
        application_name: str = "scanner-worker",
    ) -> None:
        self._min_size = min_size
        self._max_size = max_size
        self._pool = AsyncConnectionPool(
            conninfo=_conninfo_with_tls(database_url),
            open=False,
            min_size=min_size,
            max_size=max_size,
            max_idle=timedelta(seconds=30),
            check=AsyncConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "application_name": application_name,
                # Supabase pooler needs statement-level timeouts far below the
                # LB idle cut; keep server-side auto-reap honest.
                "connect_timeout": 10,
            },
        )

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def connect(self) -> None:
        # `pool.closed` is the property; `pool.open` is the *method*, so the
        # tempting `if not self._pool.open` is always False and silently skips
        # the open forever (this exact bug made every worker DB write raise
        # "the pool 'pool-1' is not open yet"). A closed pool — including one
        # closed by a previous teardown — is opened here again.
        if self._pool.closed:
            try:
                await self._pool.open(wait=True, timeout=15.0)
            except Exception as exc:  # noqa: BLE001 — re-raised after logging
                # Never log the exception text: psycopg dsn errors can echo
                # parts of the conninfo. The type name is enough to act on.
                log.warning("postgres_pool.open_failed", error_type=type(exc).__name__)
                raise
            log.info(
                "postgres_pool.ready", min_size=self._min_size, max_size=self._max_size
            )

    async def close(self) -> None:
        await self._pool.close()
        log.info("postgres_pool.closed")

    async def healthcheck(self) -> bool:
        try:
            async with self._pool.connection() as conn, conn.transaction():
                row = await (await conn.execute("SELECT 1 AS ok")).fetchone()
            return bool(row and row["ok"] == 1)
        except Exception as exc:  # noqa: BLE001 — healthcheck must not raise
            log.warning("postgres_healthcheck.failed", error_type=type(exc).__name__)
            return False

    # ── instruments ─────────────────────────────────────────────────────────
    async def upsert_instruments(self, instruments: list[Instrument]) -> int:
        if not instruments:
            return 0
        rows = [
            {
                "exchange": item.exchange,
                "symbol": item.symbol,
                "trading_symbol": item.trading_symbol,
                "company_name": item.company_name or None,
                "instrument_token": item.instrument_token,
                "instrument_type": item.instrument_type,
                "is_nifty500": item.is_nifty500,
                "is_active": item.is_active,
                "mapping_status": item.mapping_status.value,
                "last_verified_at": item.last_verified_at,
                "previous_close": _r(item.previous_close),
            }
            for item in instruments
        ]
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(_INSTRUMENT_UPSERT, rows)
        return len(instruments)

    async def list_universe(
        self, *, universe: str = "NIFTY500", mapped_only: bool = True
    ) -> list[Instrument]:
        sql = """
            SELECT * FROM instruments
            WHERE (%(only_500)s IS FALSE OR is_nifty500)
              AND is_active
              AND (%(mapped_only)s IS FALSE OR mapping_status = 'MAPPED')
            ORDER BY symbol
        """
        params = {"only_500": universe == "NIFTY500", "mapped_only": mapped_only}
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute(sql, params)).fetchall()
        return [_instrument_from_row(row) for row in rows]

    async def get_instrument(self, instrument_id: int) -> Instrument | None:
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute("SELECT * FROM instruments WHERE id = %s", (instrument_id,))
            ).fetchone()
        return _instrument_from_row(row) if row else None

    async def set_previous_closes(self, values: dict[int, float]) -> int:
        if not values:
            return 0
        rows = [{"close": _r(close), "id": instrument_id} for instrument_id, close in values.items()]
        sql = "UPDATE instruments SET previous_close = %(close)s WHERE id = %(id)s"
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(sql, rows)
            return int(cur.rowcount)
        return 0  # pragma: no cover — unreachable, keeps mypy happy on paths

    async def mark_mapping_status(
        self, unmapped_symbols: list[str], status: str, *, universe: str = "NIFTY500"
    ) -> int:
        symbols = [symbol.upper() for symbol in unmapped_symbols]
        if not symbols:
            return 0
        sql = "UPDATE instruments SET mapping_status = %(status)s WHERE upper(symbol) = ANY(%(symbols)s)"
        async with self._pool.connection() as conn, conn.transaction():
            result = await conn.execute(sql, {"status": status, "symbols": symbols})
            return int(result.rowcount)
        return 0  # pragma: no cover

    async def coverage(self, *, universe: str = "NIFTY500") -> CoverageCounts:
        aggregate_sql = """
            SELECT
                COUNT(*)::int AS universe_total,
                COUNT(*) FILTER (WHERE mapping_status = 'MAPPED')::int   AS mapped_total,
                COUNT(*) FILTER (WHERE mapping_status = 'UNMAPPED')::int AS unmapped_total,
                COUNT(*) FILTER (WHERE mapping_status = 'AMBIGUOUS')::int AS ambiguous_total,
                COUNT(*) FILTER (WHERE mapping_status = 'INVALID')::int  AS invalid_total
            FROM instruments
            WHERE (%(only_500)s IS FALSE OR is_nifty500)
        """
        heartbeat_sql = """
            SELECT subscribed_instrument_count, monitored_instrument_count
            FROM worker_heartbeats
            ORDER BY updated_at DESC
            LIMIT 1
        """
        async with self._pool.connection() as conn, conn.transaction():
            totals = await (
                await conn.execute(aggregate_sql, {"only_500": universe == "NIFTY500"})
            ).fetchone()
            heartbeat = await (await conn.execute(heartbeat_sql)).fetchone()
        counts = CoverageCounts(
            universe_total=int(totals["universe_total"]),
            mapped_total=int(totals["mapped_total"]),
            subscribed_total=int(heartbeat["subscribed_instrument_count"]) if heartbeat else 0,
            monitored_total=int(heartbeat["monitored_instrument_count"]) if heartbeat else 0,
            unmapped_total=int(totals["unmapped_total"]),
            ambiguous_total=int(totals["ambiguous_total"]),
            invalid_total=int(totals["invalid_total"]),
        )
        return counts

    # ── sessions ────────────────────────────────────────────────────────────
    async def ensure_session(
        self,
        trading_date: date,
        session_open_at: datetime,
        session_close_at: datetime,
        *,
        exchange: str = "NSE",
    ) -> SessionRow:
        sql = f"""
            INSERT INTO trading_sessions (trading_date, exchange, session_open_at, session_close_at)
            VALUES (%(trading_date)s, %(exchange)s, %(open_at)s, %(close_at)s)
            ON CONFLICT (trading_date, exchange) DO UPDATE SET updated_at = now()
            RETURNING {_SESSION_SELECT_COLUMNS}
        """
        params = {
            "trading_date": trading_date,
            "exchange": exchange,
            "open_at": session_open_at,
            "close_at": session_close_at,
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
        return _session_from_row(row)

    async def set_session_status(self, session_id: int, status: str) -> None:
        await self._execute(
            "UPDATE trading_sessions SET session_status = %s WHERE id = %s", (status, session_id)
        )

    async def mark_initial_scan_completed(self, session_id: int, at: datetime) -> None:
        await self._execute(
            "UPDATE trading_sessions SET initial_scan_completed_at = %s WHERE id = %s",
            (at, session_id),
        )

    async def add_stale_seconds(self, session_id: int, seconds: int) -> None:
        await self._execute(
            "UPDATE trading_sessions SET stale_data_seconds = stale_data_seconds + GREATEST(%s, 0) WHERE id = %s",
            (seconds, session_id),
        )

    async def finalize_session(self, session_id: int, at: datetime) -> None:
        await self._execute(
            """
            UPDATE trading_sessions
            SET session_status = 'FINALIZED', finalized_at = %s
            WHERE id = %s
            """,
            (at, session_id),
        )

    async def get_session_for_date(
        self, trading_date: date, *, exchange: str = "NSE"
    ) -> SessionRow | None:
        sql = f"""
            SELECT {_SESSION_SELECT_COLUMNS} FROM trading_sessions
            WHERE trading_date = %s AND exchange = %s
        """
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, (trading_date, exchange))).fetchone()
        return _session_from_row(row) if row else None

    async def get_open_session(self) -> SessionRow | None:
        sql = f"""
            SELECT {_SESSION_SELECT_COLUMNS} FROM trading_sessions
            WHERE session_status <> 'FINALIZED'
            ORDER BY trading_date DESC
            LIMIT 1
        """
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql)).fetchone()
        return _session_from_row(row) if row else None

    # ── scanner configuration ───────────────────────────────────────────────
    async def active_config(self, *, name: str = "default") -> ConfigRow | None:
        sql = f"""
            SELECT {_CONFIG_SELECT_COLUMNS} FROM scanner_configs
            WHERE name = %s
            ORDER BY config_version DESC
            LIMIT 1
        """
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, (name,))).fetchone()
        return _config_from_row(row) if row else None

    async def save_config(
        self, params: ScannerParams, *, name: str = "default", created_by: str
    ) -> ConfigRow:
        insert_sql = f"""
            INSERT INTO scanner_configs (
                name, universe, fast_ema_period, slow_ema_period, volume_lookback,
                relative_volume_threshold, alert_cooldown_minutes, removal_confirm_candles,
                feed_staleness_threshold_seconds, candle_grace_seconds,
                initial_scan_candles, max_subscribed_instruments, rank_weights,
                is_enabled, config_version, created_by
            ) VALUES (
                %(name)s, %(universe)s, %(fast_ema_period)s, %(slow_ema_period)s,
                %(volume_lookback)s, %(relative_volume_threshold)s,
                %(alert_cooldown_minutes)s, %(removal_confirm_candles)s,
                %(feed_staleness_threshold_seconds)s, %(candle_grace_seconds)s,
                %(initial_scan_candles)s, %(max_subscribed_instruments)s,
                %(rank_weights)s, %(is_enabled)s, %(config_version)s, %(created_by)s
            )
            RETURNING {_CONFIG_SELECT_COLUMNS}
        """
        # One transaction: the max+1 read and the insert cannot interleave
        # with a second writer picking the same version.
        async with self._pool.connection() as conn, conn.transaction():
            top = await (
                await conn.execute(
                    "SELECT COALESCE(MAX(config_version), 0) + 1 AS next_version "
                    "FROM scanner_configs WHERE name = %s",
                    (name,),
                )
            ).fetchone()
            version = int(top["next_version"])
            row = await (
                await conn.execute(insert_sql, _config_write_params(params, name, created_by, version))
            ).fetchone()
        return _config_from_row(row)

    # ── candles + indicators ────────────────────────────────────────────────
    async def upsert_candles(self, candles: list[Candle]) -> int:
        """Batch write — one connection, one executemany, regardless of size."""
        if not candles:
            return 0
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(_CANDLE_UPSERT, [candle.to_row() for candle in candles])
        return len(candles)

    async def load_completed_candles(
        self, instrument_ids: list[int], *, until: datetime, limit_per_instrument: int
    ) -> dict[int, list[Candle]]:
        """One windowed query for ALL instruments — never N round-trips."""
        out: dict[int, list[Candle]] = {instrument_id: [] for instrument_id in instrument_ids}
        if not instrument_ids:
            return out
        sql = """
            SELECT * FROM (
                SELECT
                    c.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY c.instrument_id ORDER BY c.candle_start_at DESC
                    ) AS rn
                FROM candles_1m c
                WHERE c.instrument_id = ANY(%(ids)s)
                  AND c.is_complete
                  AND c.candle_start_at <= %(until)s
            ) recent
            WHERE recent.rn <= %(limit)s
            ORDER BY recent.instrument_id, recent.candle_start_at ASC
        """
        params = {"ids": list(instrument_ids), "until": until, "limit": limit_per_instrument}
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute(sql, params)).fetchall()
        for row in rows:
            out[int(row["instrument_id"])].append(_candle_from_row(row))
        return out

    async def latest_completed_candle_at(self, instrument_id: int) -> datetime | None:
        sql = "SELECT MAX(candle_start_at) AS at FROM candles_1m WHERE instrument_id = %s AND is_complete"
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, (instrument_id,))).fetchone()
        return row["at"] if row else None

    async def upsert_indicator_snapshot(self, snapshot: IndicatorSnapshot) -> int:
        sql = """
            INSERT INTO indicator_snapshots (
                instrument_id, trading_date, candle_start_at, close, previous_close,
                vwap, ema9, ema20, average_volume20, relative_volume,
                intraday_change_pct, distance_from_vwap_pct, ema_spread_pct,
                session_high, session_low, distance_from_high_pct,
                bullish_qualified, reasons, failed_reasons, data_quality_status,
                warming_up, score, "rank", config_version, is_simulated
            ) VALUES (
                %(instrument_id)s, %(trading_date)s, %(candle_start_at)s, %(close)s,
                %(previous_close)s, %(vwap)s, %(ema9)s, %(ema20)s, %(average_volume20)s,
                %(relative_volume)s, %(intraday_change_pct)s, %(distance_from_vwap_pct)s,
                %(ema_spread_pct)s, %(session_high)s, %(session_low)s,
                %(distance_from_high_pct)s, %(bullish_qualified)s, %(reasons)s,
                %(failed_reasons)s, %(data_quality_status)s, %(warming_up)s,
                %(score)s, %(rank)s, %(config_version)s, %(is_simulated)s
            )
            ON CONFLICT (instrument_id, candle_start_at) DO UPDATE SET
                trading_date = EXCLUDED.trading_date,
                close = EXCLUDED.close,
                previous_close = EXCLUDED.previous_close,
                vwap = EXCLUDED.vwap,
                ema9 = EXCLUDED.ema9,
                ema20 = EXCLUDED.ema20,
                average_volume20 = EXCLUDED.average_volume20,
                relative_volume = EXCLUDED.relative_volume,
                intraday_change_pct = EXCLUDED.intraday_change_pct,
                distance_from_vwap_pct = EXCLUDED.distance_from_vwap_pct,
                ema_spread_pct = EXCLUDED.ema_spread_pct,
                session_high = EXCLUDED.session_high,
                session_low = EXCLUDED.session_low,
                distance_from_high_pct = EXCLUDED.distance_from_high_pct,
                bullish_qualified = EXCLUDED.bullish_qualified,
                reasons = EXCLUDED.reasons,
                failed_reasons = EXCLUDED.failed_reasons,
                data_quality_status = EXCLUDED.data_quality_status,
                warming_up = EXCLUDED.warming_up,
                score = EXCLUDED.score,
                "rank" = EXCLUDED."rank",
                config_version = EXCLUDED.config_version,
                is_simulated = EXCLUDED.is_simulated
            RETURNING id
        """
        params = {
            "instrument_id": snapshot.instrument_id,
            "trading_date": snapshot.trading_date,
            "candle_start_at": snapshot.candle_start,
            "close": _r(snapshot.close),
            "previous_close": _r(snapshot.previous_close),
            "vwap": _r(snapshot.vwap),
            "ema9": _r(snapshot.ema_fast),
            "ema20": _r(snapshot.ema_slow),
            "average_volume20": _r(snapshot.average_volume),
            "relative_volume": _r(snapshot.relative_volume),
            "intraday_change_pct": _r(snapshot.intraday_change_pct),
            "distance_from_vwap_pct": _r(snapshot.distance_from_vwap_pct),
            "ema_spread_pct": _r(snapshot.ema_spread_pct),
            "session_high": _r(snapshot.session_high),
            "session_low": _r(snapshot.session_low),
            "distance_from_high_pct": _r(snapshot.distance_from_high_pct),
            "bullish_qualified": snapshot.bullish_qualified,
            "reasons": list(snapshot.reasons),
            "failed_reasons": list(snapshot.failed_reasons),
            "data_quality_status": snapshot.data_quality.value,
            "warming_up": snapshot.warming_up,
            "score": _r(snapshot.score, 2),
            "rank": snapshot.rank,
            "config_version": snapshot.config_version,
            "is_simulated": snapshot.is_simulated,
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
        return int(row["id"])

    async def count_candles_for_session(self, session_id: int) -> int:
        sql = """
            SELECT COUNT(*)::int AS total FROM candles_1m
            WHERE is_complete
              AND trading_date = (SELECT trading_date FROM trading_sessions WHERE id = %s)
        """
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, (session_id,))).fetchone()
        return int(row["total"]) if row else 0

    # ── watchlist ───────────────────────────────────────────────────────────
    async def get_membership(self, session_id: int, instrument_id: int) -> WatchlistMembership | None:
        sql = f"""
            SELECT {_MEMBERSHIP_SELECT_COLUMNS} FROM daily_watchlists
            WHERE trading_session_id = %s AND instrument_id = %s
        """
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, (session_id, instrument_id))).fetchone()
        return _membership_from_row(row) if row else None

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
        # The DO UPDATE of the row's own value is the "return the existing row"
        # half of upsert semantics (daily_watchlists is unique per
        # (session, instrument), exactly like InMemoryStore).
        sql = f"""
            INSERT INTO daily_watchlists (
                trading_session_id, instrument_id, current_state, initial_entry_at,
                latest_state_change_at, initial_entry_price, latest_price,
                latest_score, last_indicator_snapshot_id, is_active,
                consecutive_weak_candles, episode_count, is_simulated
            ) VALUES (
                %(session_id)s, %(instrument_id)s, %(state)s, %(entered_at)s,
                %(entered_at)s, %(price)s, %(price)s, %(score)s, %(snapshot_id)s,
                %(is_active)s, 0, 1, %(is_simulated)s
            )
            ON CONFLICT (trading_session_id, instrument_id)
            DO UPDATE SET current_state = daily_watchlists.current_state
            RETURNING {_MEMBERSHIP_SELECT_COLUMNS}
        """
        params = {
            "session_id": session_id,
            "instrument_id": instrument_id,
            "state": WatchlistState(state).value,
            "entered_at": entered_at,
            "price": _r(price),
            "score": _r(score, 2),
            "snapshot_id": snapshot_id,
            # One definition of "still tracked today", shared with InMemoryStore
            # and the watchlist state machine.
            "is_active": is_tracked(WatchlistState(state)),
            "is_simulated": is_simulated,
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
        return _membership_from_row(row)

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
        candidates = {
            "current_state": None if state is None else WatchlistState(state).value,
            "latest_state_change_at": state_changed_at,
            "latest_price": _r(latest_price),
            "latest_score": _r(latest_score, 2),
            "latest_rank": latest_rank,
            "removal_reason": removal_reason,
            "consecutive_weak_candles": consecutive_weak_candles,
            "episode_count": episode_count,
            "last_indicator_snapshot_id": snapshot_id,
            "is_active": is_active,
        }
        updates = {key: value for key, value in candidates.items() if value is not None}
        if not updates:
            return
        set_clause = ", ".join(f"{column} = %({column})s" for column in updates)
        updates["membership_id"] = membership_id
        # Column names come from the fixed candidates map above, never from
        # user input, so interpolating the SET clause is injection-safe.
        await self._execute(
            f"UPDATE daily_watchlists SET {set_clause} WHERE id = %(membership_id)s", updates
        )

    async def add_watchlist_event(self, event: WatchlistEvent) -> int:
        sql = """
            INSERT INTO watchlist_events (
                watchlist_id, event_type, previous_state, new_state,
                event_timestamp, reason, price, indicator_snapshot_id, detail
            ) VALUES (
                %(watchlist_id)s, %(event_type)s, %(previous_state)s, %(new_state)s,
                %(event_timestamp)s, %(reason)s, %(price)s, %(indicator_snapshot_id)s,
                %(detail)s
            )
            RETURNING id
        """
        params = {
            "watchlist_id": event.watchlist_id,
            "event_type": event.event_type,
            "previous_state": None if event.previous_state is None else WatchlistState(event.previous_state).value,
            "new_state": WatchlistState(event.new_state).value,
            "event_timestamp": event.event_timestamp,
            "reason": event.reason,
            "price": _r(event.price),
            "indicator_snapshot_id": event.indicator_snapshot_id,
            "detail": event.detail or {},
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
        return int(row["id"])

    async def list_memberships(
        self,
        session_id: int,
        *,
        states: tuple[WatchlistState, ...] = (),
        include_inactive: bool = True,
    ) -> list[WatchlistMembership]:
        sql = f"""
            SELECT {_MEMBERSHIP_SELECT_COLUMNS} FROM daily_watchlists
            WHERE trading_session_id = %(session_id)s
              AND (%(states)s::text[] IS NULL OR current_state = ANY(%(states)s::text[]))
              AND (%(include_inactive)s IS TRUE OR is_active)
            ORDER BY (latest_score IS NULL), latest_score DESC NULLS LAST
        """
        params = {
            "session_id": session_id,
            "states": [WatchlistState(state).value for state in states] or None,
            "include_inactive": include_inactive,
        }
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute(sql, params)).fetchall()
        return [_membership_from_row(row) for row in rows]

    async def freeze_session_watchlist(self, session_id: int) -> int:
        sql = "UPDATE daily_watchlists SET is_active = FALSE WHERE trading_session_id = %s AND is_active"
        async with self._pool.connection() as conn, conn.transaction():
            result = await conn.execute(sql, (session_id,))
            return int(result.rowcount)
        return 0  # pragma: no cover

    # ── signals + notifications ─────────────────────────────────────────────
    async def insert_signal(self, signal: Signal) -> SignalInsertResult:
        sql = """
            INSERT INTO signals (
                instrument_id, trading_session_id, signal_type, qualifying_episode_id,
                candle_start_at, event_timestamp, price, previous_close,
                intraday_change_pct, vwap, ema9, ema20, relative_volume, score,
                reasons, message, idempotency_key, config_version, is_simulated
            ) VALUES (
                %(instrument_id)s, %(trading_session_id)s, %(signal_type)s,
                %(qualifying_episode_id)s, %(candle_start_at)s, %(event_timestamp)s,
                %(price)s, %(previous_close)s, %(intraday_change_pct)s, %(vwap)s,
                %(ema9)s, %(ema20)s, %(relative_volume)s, %(score)s, %(reasons)s,
                %(message)s, %(idempotency_key)s, %(config_version)s, %(is_simulated)s
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
        """
        params = {
            "instrument_id": signal.instrument_id,
            "trading_session_id": signal.trading_session_id,
            "signal_type": signal.signal_type.value,
            "qualifying_episode_id": signal.qualifying_episode_id,
            "candle_start_at": signal.candle_start,
            "event_timestamp": signal.event_timestamp,
            "price": _r(signal.price),
            "previous_close": _r(signal.previous_close),
            "intraday_change_pct": _r(signal.intraday_change_pct),
            "vwap": _r(signal.vwap),
            "ema9": _r(signal.ema_fast),
            "ema20": _r(signal.ema_slow),
            "relative_volume": _r(signal.relative_volume),
            "score": _r(signal.score, 2),
            "reasons": list(signal.reasons),
            "message": signal.message,
            "idempotency_key": signal.idempotency_key,
            "config_version": signal.config_version,
            "is_simulated": signal.is_simulated,
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
            if row is not None:
                return SignalInsertResult(signal_id=int(row["id"]), created=True)
            # DO NOTHING fired: the retry lost the race or replayed.
            # Return the existing id so downstream notification linking
            # still works (spec §12.3).
            existing = await (
                await conn.execute(
                    "SELECT id FROM signals WHERE idempotency_key = %s",
                    (signal.idempotency_key,),
                )
            ).fetchone()
            return SignalInsertResult(signal_id=int(existing["id"]), created=False)

    async def last_pushed_signal_at(
        self,
        instrument_id: int,
        signal_type: str,
        *,
        since: datetime,
    ) -> datetime | None:
        statuses = [status.value for status in ALERTED_DELIVERY_STATUSES]
        sql = """
            SELECT MAX(s.event_timestamp) AS at
            FROM signals s
            JOIN notification_deliveries d ON d.signal_id = s.id
            WHERE s.instrument_id = %(instrument_id)s
              AND s.signal_type = %(signal_type)s
              AND s.event_timestamp >= %(since)s
              AND d.delivery_status = ANY(%(statuses)s)
        """
        params = {
            "instrument_id": instrument_id,
            "signal_type": signal_type,
            "since": since,
            "statuses": statuses,
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
        return row["at"] if row else None

    async def count_signals_since(self, session_id: int, since: datetime) -> int:
        sql = "SELECT COUNT(*)::int AS total FROM signals WHERE trading_session_id = %s AND event_timestamp >= %s"
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, (session_id, since))).fetchone()
        return int(row["total"]) if row else 0

    async def enqueue_notification(self, delivery: NotificationDelivery) -> int:
        sql = """
            INSERT INTO notification_deliveries (
                signal_id, channel, destination_reference, delivery_status,
                attempt_count, next_retry_at, last_attempt_at, delivered_at,
                provider_message_id, last_error, payload
            ) VALUES (
                %(signal_id)s, %(channel)s, %(destination_reference)s,
                %(delivery_status)s, %(attempt_count)s, %(next_retry_at)s,
                %(last_attempt_at)s, %(delivered_at)s, %(provider_message_id)s,
                %(last_error)s, %(payload)s
            )
            ON CONFLICT (signal_id, channel)
            DO UPDATE SET signal_id = notification_deliveries.signal_id
            RETURNING id
        """
        params = {
            "signal_id": delivery.signal_id,
            "channel": Channel(delivery.channel).value,
            "destination_reference": delivery.destination_reference or None,
            "delivery_status": DeliveryStatus(delivery.delivery_status).value,
            "attempt_count": delivery.attempt_count,
            "next_retry_at": delivery.next_retry_at,
            "last_attempt_at": delivery.last_attempt_at,
            "delivered_at": delivery.delivered_at,
            "provider_message_id": delivery.provider_message_id,
            "last_error": delivery.last_error,
            "payload": delivery.payload or {},
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
        return int(row["id"])

    async def claim_due_notifications(
        self, *, channel: Channel, now: datetime, limit: int = 10
    ) -> list[NotificationClaim]:
        """Atomically move QUEUED/FAILED rows to IN_FLIGHT (spec §17).

        ``FOR UPDATE SKIP LOCKED`` + the CTE make claim-and-mark a single
        statement inside one transaction: a second replica claiming at the
        same instant either gets different rows or nothing at all.
        """
        params = {"channel": Channel(channel).value, "now": now, "limit": limit}
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute(_CLAIM_DUE_SQL, params)).fetchall()
        return [NotificationClaim(delivery=_delivery_from_row(row), channel=Channel(channel)) for row in rows]

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
        sql = """
            UPDATE notification_deliveries
            SET delivery_status = %(status)s,
                attempt_count = %(attempt_count)s,
                last_attempt_at = %(last_attempt_at)s,
                delivered_at = COALESCE(%(delivered_at)s, delivered_at),
                next_retry_at = %(next_retry_at)s,
                provider_message_id = COALESCE(%(provider_message_id)s, provider_message_id),
                last_error = %(last_error)s
            WHERE id = %(delivery_id)s
        """
        params = {
            "status": DeliveryStatus(status).value,
            "attempt_count": attempt_count,
            "last_attempt_at": last_attempt_at,
            "delivered_at": delivered_at,
            "next_retry_at": next_retry_at,
            "provider_message_id": provider_message_id,
            "last_error": last_error,
            "delivery_id": delivery_id,
        }
        await self._execute(sql, params)

    async def pending_notification_count(self, *, channel: Channel | None = None) -> int:
        sql = """
            SELECT COUNT(*)::int AS total FROM notification_deliveries
            WHERE delivery_status IN ('QUEUED', 'FAILED', 'IN_FLIGHT')
              AND (%(channel)s::text IS NULL OR channel = %(channel)s)
        """
        params = {"channel": None if channel is None else Channel(channel).value}
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(sql, params)).fetchone()
        return int(row["total"]) if row else 0

    async def delivery_rollup(self, *, session_id: int) -> dict[str, int]:
        sql = """
            SELECT nd.channel || '_' || nd.delivery_status AS key, COUNT(*)::int AS total
            FROM notification_deliveries nd
            JOIN signals s ON s.id = nd.signal_id
            WHERE s.trading_session_id = %(session_id)s
            GROUP BY 1
        """
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute(sql, {"session_id": session_id})).fetchall()
        return {str(row["key"]): int(row["total"]) for row in rows}

    # ── operations ──────────────────────────────────────────────────────────
    async def upsert_heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        sql = """
            INSERT INTO worker_heartbeats (
                worker_id, status, feed_status, last_market_data_at,
                last_completed_candle_at, subscribed_instrument_count,
                monitored_instrument_count, universe_instrument_count,
                mapped_instrument_count, active_watchlist_count,
                pending_notification_count, queue_depth, last_error
            ) VALUES (
                %(worker_id)s, %(status)s, %(feed_status)s, %(last_market_data_at)s,
                %(last_completed_candle_at)s, %(subscribed_instrument_count)s,
                %(monitored_instrument_count)s, %(universe_instrument_count)s,
                %(mapped_instrument_count)s, %(active_watchlist_count)s,
                %(pending_notification_count)s, %(queue_depth)s, %(last_error)s
            )
            ON CONFLICT (worker_id) DO UPDATE SET
                status = EXCLUDED.status,
                feed_status = EXCLUDED.feed_status,
                last_market_data_at = EXCLUDED.last_market_data_at,
                last_completed_candle_at = EXCLUDED.last_completed_candle_at,
                subscribed_instrument_count = EXCLUDED.subscribed_instrument_count,
                monitored_instrument_count = EXCLUDED.monitored_instrument_count,
                universe_instrument_count = EXCLUDED.universe_instrument_count,
                mapped_instrument_count = EXCLUDED.mapped_instrument_count,
                active_watchlist_count = EXCLUDED.active_watchlist_count,
                pending_notification_count = EXCLUDED.pending_notification_count,
                queue_depth = EXCLUDED.queue_depth,
                last_error = EXCLUDED.last_error
        """
        params = {
            "worker_id": heartbeat.worker_id,
            "status": heartbeat.status,
            "feed_status": heartbeat.feed_status.value,
            "last_market_data_at": heartbeat.last_market_data_at,
            "last_completed_candle_at": heartbeat.last_completed_candle_at,
            "subscribed_instrument_count": heartbeat.subscribed_instrument_count,
            "monitored_instrument_count": heartbeat.monitored_total,
            "universe_instrument_count": heartbeat.universe_total,
            "mapped_instrument_count": heartbeat.mapped_total,
            "active_watchlist_count": heartbeat.active_watchlist_count,
            "pending_notification_count": heartbeat.pending_notification_count,
            "queue_depth": heartbeat.queue_depth,
            "last_error": heartbeat.last_error,
        }
        await self._execute(sql, params)

    async def write_daily_summary(
        self, session_id: int, trading_date: date, payload: dict[str, object]
    ) -> None:
        columns = [column for column in _SUMMARY_COLUMNS if column in payload]
        names = ", ".join(["trading_session_id", "trading_date", *columns])
        placeholders = ", ".join(
            ["%(session_id)s", "%(trading_date)s", *[f"%({column})s" for column in columns]]
        )
        updates = ", ".join([f"{column} = EXCLUDED.{column}" for column in columns])
        update_clause = f", {updates}" if updates else ""
        sql = (
            f"INSERT INTO daily_summaries ({names}) VALUES ({placeholders}) "
            "ON CONFLICT (trading_date) DO UPDATE SET "
            f"trading_session_id = EXCLUDED.trading_session_id{update_clause}"
        )
        params: dict[str, object] = {"session_id": session_id, "trading_date": trading_date}
        params.update({column: payload[column] for column in columns})
        await self._execute(sql, params)

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
        sql = """
            INSERT INTO audit_logs (
                actor, actor_role, action, entity_type, entity_id,
                previous_value, new_value, request_ip
            ) VALUES (
                %(actor)s, %(actor_role)s, %(action)s, %(entity_type)s,
                %(entity_id)s, %(previous_value)s, %(new_value)s, %(request_ip)s
            )
        """
        params = {
            "actor": actor,
            "actor_role": actor_role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "previous_value": previous_value,
            "new_value": new_value,
            "request_ip": request_ip,
        }
        await self._execute(sql, params)

    async def acquire_lease(self, name: str, holder: str, ttl_seconds: int, now: datetime) -> bool:
        params = {
            "name": name,
            "holder": holder,
            "now": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(_ACQUIRE_LEASE_SQL, params)).fetchone()
        return row is not None

    async def renew_lease(self, name: str, holder: str, ttl_seconds: int, now: datetime) -> bool:
        params = {
            "name": name,
            "holder": holder,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        }
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(_RENEW_LEASE_SQL, params)).fetchone()
        return row is not None

    async def release_lease(self, name: str, holder: str) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(_RELEASE_LEASE_SQL, {"name": name, "holder": holder})
            ).fetchone()
        return row is not None

    # ── test helper ─────────────────────────────────────────────────────────
    async def truncate_all(self, *, allow: bool = False) -> None:
        """Wipe every store table. Test-only; refuses to run without the flag."""
        if not allow:
            raise RuntimeError(
                "truncate_all is a test helper only — pass allow=True from a test harness"
            )
        async with self._pool.connection() as conn, conn.transaction():
            for table in SCHEMA_TABLES:
                # Table names come from the fixed SCHEMA_TABLES allowlist.
                await conn.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')

    # ── internals ───────────────────────────────────────────────────────────
    async def _execute(self, sql: str, params: dict[str, Any] | tuple[Any, ...]) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(sql, params)


def _config_write_params(
    params: ScannerParams, name: str, created_by: str, version: int
) -> dict[str, Any]:
    return {
        "name": name,
        "universe": params.universe,
        "fast_ema_period": params.fast_ema_period,
        "slow_ema_period": params.slow_ema_period,
        "volume_lookback": params.volume_lookback,
        "relative_volume_threshold": _r(params.relative_volume_threshold),
        "alert_cooldown_minutes": params.alert_cooldown_minutes,
        "removal_confirm_candles": params.removal_confirm_candles,
        # Interface/schema disagreement: ScannerParams calls this field
        # feed_staleness_seconds; the table column is
        # feed_staleness_threshold_seconds. Mapped, never renamed.
        "feed_staleness_threshold_seconds": params.feed_staleness_seconds,
        "candle_grace_seconds": params.candle_grace_seconds,
        "initial_scan_candles": params.initial_scan_candles,
        "max_subscribed_instruments": params.max_subscribed_instruments,
        "rank_weights": {
            "price_strength": params.rank_weight_price_strength,
            "relative_volume": params.rank_weight_relative_volume,
            "vwap_strength": params.rank_weight_vwap_strength,
            "ema_spread": params.rank_weight_ema_spread,
        },
        "is_enabled": params.is_enabled,
        "config_version": version,
        "created_by": created_by,
    }

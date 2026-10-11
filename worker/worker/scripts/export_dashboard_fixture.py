"""Export an offline replay as dashboard-shaped rows (``scanner-dashboard-data``).

Why this exists
---------------
The Next.js dashboard reads PostgreSQL views (``v_current_watchlist``,
``v_signal_feed``, ``v_worker_status``, ``v_instrument_coverage``,
``v_session_history``). Those views can only be exercised against a database,
which a laptop or a CI job does not have. This script drives the same production
pipeline over the scripted synthetic ticks, then writes the rows those views
would return using the *view column names* — so the dashboard can render a
real, internally consistent session before it is connected to one.

Honesty rules
-------------
* Every row keeps its ``is_simulated`` flag from the replay, and ``meta.simulated``
  is ``true``; the dashboard must show that as a banner, never hide it.
* ``instrument_token`` is deliberately absent. The browser has no use for a broker
  identifier, and leaving it out of the file is stronger than filtering it later.
* No credential, chat id or provider secret is ever written here.
* The fixture is only written when the replay's own invariant checks pass —
  a sample that contradicts the spec is worse than no sample.

Usage::

    .venv/bin/python -m worker.scripts.export_dashboard_fixture
    … --seed 7 --out ../../app/data/replay-fixture.json --pretty
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..models import IndicatorSnapshot, NotificationDelivery, Signal, WatchlistMembership
from ..persistence.memory import InMemoryStore
from ..persistence.store import ConfigRow, SessionRow
from ..simulation.run import (
    SIMULATED_BANNER,
    Simulation,
    SimulationOptions,
    assemble_report,
    build,
    check_invariants,
)
from ..timeutils import to_ist

#: Where the checked-in sample lives, resolved from this file: ``<repo>/app/data/…``
DEFAULT_OUTPUT = Path(__file__).resolve().parents[3] / "app" / "data" / "replay-fixture.json"

SCHEMA_VERSION = 1

#: Column sets of the views in 0003_views.sql. The ``assert`` in each builder
#: below compares against these, so a migration that renames a column breaks
#: this exporter loudly instead of quietly feeding the dashboard wrong keys.
WATCHLIST_COLUMNS = frozenset(
    {
        "watchlist_id", "trading_session_id", "trading_date", "instrument_id", "symbol",
        "trading_symbol", "company_name", "exchange", "current_state", "initial_entry_at",
        "latest_state_change_at", "initial_entry_price", "latest_price", "latest_score",
        "latest_rank", "removal_reason", "consecutive_weak_candles", "episode_count",
        "is_active", "is_simulated", "vwap", "ema9", "ema20", "relative_volume",
        "intraday_change_pct", "distance_from_vwap_pct", "ema_spread_pct", "session_high",
        "session_low", "distance_from_high_pct", "bullish_qualified", "reasons",
        "failed_reasons", "data_quality", "warming_up", "latest_candle_at",
        "indicator_snapshot_id",
    }
)

SIGNAL_COLUMNS = frozenset(
    {
        "id", "signal_type", "event_timestamp", "candle_start_at", "price", "previous_close",
        "intraday_change_pct", "vwap", "ema9", "ema20", "relative_volume", "score", "rank",
        "reasons", "message", "config_version", "is_simulated", "trading_session_id",
        "trading_date", "instrument_id", "symbol", "trading_symbol", "company_name", "exchange",
        "telegram_delivered", "telegram_failed", "telegram_status", "telegram_attempts",
        "telegram_error",
    }
)

COVERAGE_COLUMNS = frozenset(
    {
        "nifty500_count", "mapped_count", "unmapped_count", "ambiguous_count", "invalid_count",
        "active_universe_count", "last_verified_at",
    }
)

WORKER_STATUS_COLUMNS = frozenset(
    {
        "worker_id", "status", "feed_status", "last_market_data_at", "last_completed_candle_at",
        "subscribed_instrument_count", "monitored_instrument_count", "universe_instrument_count",
        "mapped_instrument_count", "active_watchlist_count", "pending_notification_count",
        "queue_depth", "processing_latency_ms", "last_error", "scan_mode", "simulation_label",
        "started_at", "updated_at",
    }
)

HISTORY_COLUMNS = frozenset(
    {
        "trading_session_id", "trading_date", "session_status", "session_open_at",
        "session_close_at", "initial_scan_completed_at", "finalized_at", "stale_data_seconds",
        "universe_count", "mapped_count", "subscribed_count", "monitored_count",
        "qualified_count", "signals_generated", "telegram_delivered", "telegram_failed", "notes",
    }
)

SNAPSHOT_COLUMNS = frozenset(
    {
        "id", "instrument_id", "trading_date", "candle_start_at", "close", "previous_close",
        "vwap", "ema9", "ema20", "average_volume20", "relative_volume", "intraday_change_pct",
        "distance_from_vwap_pct", "ema_spread_pct", "session_high", "session_low",
        "distance_from_high_pct", "bullish_qualified", "reasons", "failed_reasons",
        "data_quality_status", "warming_up", "score", "rank", "config_version", "is_simulated",
    }
)

CANDLE_COLUMNS = frozenset(
    {
        "instrument_id", "trading_date", "candle_start_at", "open", "high", "low", "close",
        "volume", "is_complete", "data_quality_status", "source_timestamp", "tick_count",
        "is_simulated",
    }
)

EVENT_COLUMNS = frozenset(
    {
        "id", "watchlist_id", "symbol", "event_type", "previous_state", "new_state",
        "event_timestamp", "reason", "price", "indicator_snapshot_id", "detail",
    }
)

DELIVERY_COLUMNS = frozenset(
    {
        "id", "signal_id", "channel", "delivery_status", "attempt_count", "next_retry_at",
        "last_attempt_at", "delivered_at", "provider_message_id", "last_error", "is_simulated",
    }
)

INSTRUMENT_COLUMNS = frozenset(
    {
        "id", "exchange", "symbol", "trading_symbol", "company_name", "instrument_type",
        "is_nifty500", "is_active", "mapping_status", "previous_close", "last_verified_at",
    }
)


def _stamp(value: datetime | None) -> str | None:
    """ISO-8601 in Asia/Kolkata — the format the API hands the browser."""
    return None if value is None else to_ist(value).isoformat()


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _r(value: float | None, ndigits: int = 4) -> float | None:
    return None if value is None else round(value, ndigits)


def _check(row: dict[str, Any], columns: frozenset[str], label: str) -> dict[str, Any]:
    if set(row) != columns:
        missing = sorted(columns - set(row))
        extra = sorted(set(row) - columns)
        raise SystemExit(f"{label} shape drifted from the migration (missing={missing}, extra={extra})")
    return row


def instrument_row(instrument: Any) -> dict[str, Any]:
    """A mapped universe instrument — without ``instrument_token`` (§19)."""
    return _check(
        {
            "id": instrument.id,
            "exchange": instrument.exchange,
            "symbol": instrument.symbol,
            "trading_symbol": instrument.trading_symbol,
            "company_name": instrument.company_name,
            "instrument_type": instrument.instrument_type,
            "is_nifty500": instrument.is_nifty500,
            "is_active": instrument.is_active,
            "mapping_status": instrument.mapping_status.value,
            "previous_close": _r(instrument.previous_close),
            "last_verified_at": _stamp(instrument.last_verified_at),
        },
        INSTRUMENT_COLUMNS,
        "instruments",
    )


def candle_row(candle: Any) -> dict[str, Any]:
    row = candle.to_row()
    return _check(
        {
            "instrument_id": row["instrument_id"],
            "trading_date": _day(row["trading_date"]),
            "candle_start_at": _stamp(row["candle_start_at"]),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "is_complete": row["is_complete"],
            "data_quality_status": row["data_quality_status"],
            "source_timestamp": _stamp(row["source_timestamp"]),
            "tick_count": row["tick_count"],
            "is_simulated": row["is_simulated"],
        },
        CANDLE_COLUMNS,
        "candles_1m",
    )


def snapshot_row(snapshot: IndicatorSnapshot, snapshot_id: int) -> dict[str, Any]:
    """``indicator_snapshots`` as 0001_init.sql defines it (§13.5)."""
    return _check(
        {
            "id": snapshot_id,
            "instrument_id": snapshot.instrument_id,
            "trading_date": _day(snapshot.trading_date),
            "candle_start_at": _stamp(snapshot.candle_start),
            "close": _r(snapshot.close),
            "previous_close": _r(snapshot.previous_close),
            "vwap": _r(snapshot.vwap),
            "ema9": _r(snapshot.ema_fast),
            "ema20": _r(snapshot.ema_slow),
            "average_volume20": _r(snapshot.average_volume),
            "relative_volume": _r(snapshot.relative_volume, 2),
            "intraday_change_pct": _r(snapshot.intraday_change_pct, 2),
            "distance_from_vwap_pct": _r(snapshot.distance_from_vwap_pct, 2),
            "ema_spread_pct": _r(snapshot.ema_spread_pct, 2),
            "session_high": _r(snapshot.session_high),
            "session_low": _r(snapshot.session_low),
            "distance_from_high_pct": _r(snapshot.distance_from_high_pct, 2),
            "bullish_qualified": snapshot.bullish_qualified,
            "reasons": list(snapshot.reasons),
            "failed_reasons": list(snapshot.failed_reasons),
            "data_quality_status": snapshot.data_quality.value,
            "warming_up": snapshot.warming_up,
            "score": _r(snapshot.score, 2),
            "rank": snapshot.rank,
            "config_version": snapshot.config_version,
            "is_simulated": snapshot.is_simulated,
        },
        SNAPSHOT_COLUMNS,
        "indicator_snapshots",
    )


def watchlist_row(
    membership: WatchlistMembership, simulation: Simulation, snapshots_by_id: dict[int, IndicatorSnapshot]
) -> dict[str, Any]:
    instrument = simulation.store.instruments.get(membership.instrument_id)
    snapshot = snapshots_by_id.get(membership.last_indicator_snapshot_id or -1)
    return _check(
        {
            "watchlist_id": membership.id,
            "trading_session_id": membership.trading_session_id,
            "trading_date": _day(simulation.session.trading_date),
            "instrument_id": membership.instrument_id,
            "symbol": instrument.symbol if instrument else None,
            "trading_symbol": instrument.trading_symbol if instrument else None,
            "company_name": instrument.company_name if instrument else None,
            "exchange": instrument.exchange if instrument else None,
            "current_state": membership.current_state.value,
            "initial_entry_at": _stamp(membership.initial_entry_at),
            "latest_state_change_at": _stamp(membership.latest_state_change_at),
            "initial_entry_price": _r(membership.initial_entry_price),
            "latest_price": _r(membership.latest_price),
            "latest_score": _r(membership.latest_score, 2),
            "latest_rank": membership.latest_rank,
            "removal_reason": membership.removal_reason,
            "consecutive_weak_candles": membership.consecutive_weak_candles,
            "episode_count": membership.episode_count,
            "is_active": membership.is_active,
            "is_simulated": membership.is_simulated,
            "vwap": _r(snapshot.vwap) if snapshot else None,
            "ema9": _r(snapshot.ema_fast) if snapshot else None,
            "ema20": _r(snapshot.ema_slow) if snapshot else None,
            "relative_volume": _r(snapshot.relative_volume, 2) if snapshot else None,
            "intraday_change_pct": _r(snapshot.intraday_change_pct, 2) if snapshot else None,
            "distance_from_vwap_pct": _r(snapshot.distance_from_vwap_pct, 2) if snapshot else None,
            "ema_spread_pct": _r(snapshot.ema_spread_pct, 2) if snapshot else None,
            "session_high": _r(snapshot.session_high) if snapshot else None,
            "session_low": _r(snapshot.session_low) if snapshot else None,
            "distance_from_high_pct": _r(snapshot.distance_from_high_pct, 2) if snapshot else None,
            "bullish_qualified": bool(snapshot.bullish_qualified) if snapshot else False,
            "reasons": list(snapshot.reasons) if snapshot else [],
            "failed_reasons": list(snapshot.failed_reasons) if snapshot else [],
            "data_quality": snapshot.data_quality.value if snapshot else None,
            "warming_up": bool(snapshot.warming_up) if snapshot else True,
            "latest_candle_at": _stamp(snapshot.candle_start) if snapshot else None,
            "indicator_snapshot_id": membership.last_indicator_snapshot_id,
        },
        WATCHLIST_COLUMNS,
        "v_current_watchlist",
    )


def telegram_rollup(rows: list[NotificationDelivery]) -> dict[str, Any]:
    """Mirror the ``bool_or`` / ``max`` aggregate the view computes over deliveries."""
    telegram = [row for row in rows if row.channel.value == "TELEGRAM"]
    statuses = {row.delivery_status.value for row in telegram}
    return {
        "telegram_delivered": "DELIVERED" in statuses,
        "telegram_failed": bool(telegram) and statuses <= {"FAILED", "PERMANENTLY_FAILED"},
        "telegram_status": telegram[-1].delivery_status.value if telegram else None,
        "telegram_attempts": max((row.attempt_count for row in telegram), default=0),
        "telegram_error": next((row.last_error for row in reversed(telegram) if row.last_error), None),
    }


def signal_row(signal: Signal, simulation: Simulation) -> dict[str, Any]:
    instrument = simulation.store.instruments.get(signal.instrument_id) if signal.instrument_id else None
    deliveries = [row for row in simulation.store.deliveries.values() if row.signal_id == signal.id]
    return _check(
        {
            "id": signal.id,
            "signal_type": signal.signal_type.value,
            "event_timestamp": _stamp(signal.event_timestamp),
            "candle_start_at": _stamp(signal.candle_start),
            "price": _r(signal.price),
            "previous_close": _r(signal.previous_close),
            "intraday_change_pct": _r(signal.intraday_change_pct, 2),
            "vwap": _r(signal.vwap),
            "ema9": _r(signal.ema_fast),
            "ema20": _r(signal.ema_slow),
            "relative_volume": _r(signal.relative_volume, 2),
            "score": _r(signal.score, 2),
            # signals.rank is only filled where a rank existed at push time; the
            # Signal dataclass does not carry one, so the honest value is NULL.
            "rank": None,
            "reasons": list(signal.reasons),
            "message": signal.message,
            "config_version": signal.config_version,
            "is_simulated": signal.is_simulated,
            "trading_session_id": signal.trading_session_id,
            "trading_date": _day(simulation.session.trading_date),
            "instrument_id": signal.instrument_id,
            "symbol": signal.symbol or (instrument.symbol if instrument else None),
            "trading_symbol": instrument.trading_symbol if instrument else signal.symbol,
            "company_name": signal.company_name or (instrument.company_name if instrument else None),
            "exchange": signal.exchange or (instrument.exchange if instrument else None),
            **telegram_rollup(deliveries),
        },
        SIGNAL_COLUMNS,
        "v_signal_feed",
    )


def event_row(event: Any, simulation: Simulation, ordinal: int) -> dict[str, Any]:
    membership = simulation.store.memberships.get(event.watchlist_id)
    instrument = simulation.store.instruments.get(membership.instrument_id) if membership else None
    return _check(
        {
            "id": ordinal,
            "watchlist_id": event.watchlist_id,
            "symbol": instrument.symbol if instrument else "session",
            "event_type": event.event_type,
            "previous_state": None if event.previous_state is None else event.previous_state.value,
            "new_state": event.new_state.value,
            "event_timestamp": _stamp(event.event_timestamp),
            "reason": event.reason,
            "price": _r(event.price),
            "indicator_snapshot_id": event.indicator_snapshot_id,
            "detail": event.detail,
        },
        EVENT_COLUMNS,
        "watchlist_events",
    )


def delivery_row(delivery: NotificationDelivery) -> dict[str, Any]:
    """Queue state for the delivery panel — without ``destination_reference`` (§19)."""
    return _check(
        {
            "id": delivery.id,
            "signal_id": delivery.signal_id,
            "channel": delivery.channel.value,
            "delivery_status": delivery.delivery_status.value,
            "attempt_count": delivery.attempt_count,
            "next_retry_at": _stamp(delivery.next_retry_at),
            "last_attempt_at": _stamp(delivery.last_attempt_at),
            "delivered_at": _stamp(delivery.delivered_at),
            "provider_message_id": delivery.provider_message_id,
            "last_error": delivery.last_error,
            "is_simulated": delivery.channel.value == "DASHBOARD",
        },
        DELIVERY_COLUMNS,
        "notification_deliveries",
    )


def coverage_row(store: InMemoryStore) -> dict[str, Any]:
    rows = [item for item in store.instruments.values() if item.is_nifty500]
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.mapping_status.value] = counts.get(item.mapping_status.value, 0) + 1
    verified = [item.last_verified_at for item in rows if item.last_verified_at]
    return _check(
        {
            "nifty500_count": len(rows),
            "mapped_count": counts.get("MAPPED", 0),
            "unmapped_count": counts.get("UNMAPPED", 0),
            "ambiguous_count": counts.get("AMBIGUOUS", 0),
            "invalid_count": counts.get("INVALID", 0),
            "active_universe_count": sum(1 for item in rows if item.is_active),
            "last_verified_at": _stamp(max(verified) if verified else None),
        },
        COVERAGE_COLUMNS,
        "v_instrument_coverage",
    )


def worker_status_row(simulation: Simulation) -> dict[str, Any]:
    heartbeat = simulation.store.heartbeats.get("simulator")
    if heartbeat is None:
        raise SystemExit("the replay never published a heartbeat; nothing to export")
    return _check(
        {
            "worker_id": heartbeat.worker_id,
            "status": heartbeat.status,
            "feed_status": heartbeat.feed_status.value,
            "last_market_data_at": _stamp(heartbeat.last_market_data_at),
            "last_completed_candle_at": _stamp(heartbeat.last_completed_candle_at),
            "subscribed_instrument_count": heartbeat.subscribed_instrument_count,
            "monitored_instrument_count": heartbeat.monitored_total,
            "universe_instrument_count": heartbeat.universe_total,
            "mapped_instrument_count": heartbeat.mapped_total,
            "active_watchlist_count": heartbeat.active_watchlist_count,
            "pending_notification_count": heartbeat.pending_notification_count,
            "queue_depth": heartbeat.queue_depth,
            "processing_latency_ms": None,
            "last_error": heartbeat.last_error,
            "scan_mode": "simulation",
            "simulation_label": True,
            "started_at": _stamp(simulation.session.session_open_at),
            "updated_at": _stamp(heartbeat.last_completed_candle_at),
        },
        WORKER_STATUS_COLUMNS,
        "v_worker_status",
    )


def history_row(simulation: Simulation) -> dict[str, Any]:
    session: SessionRow = simulation.store.sessions[simulation.session.id]
    summary: dict[str, Any] = simulation.store.summaries.get(session.trading_date, {})
    counters = simulation.pipeline.counters.as_dict()
    telegram = [row for row in simulation.store.deliveries.values() if row.channel.value == "TELEGRAM"]
    tracked = sum(1 for row in simulation.store.memberships.values() if row.is_active)
    return _check(
        {
            "trading_session_id": session.id,
            "trading_date": _day(session.trading_date),
            "session_status": session.session_status,
            "session_open_at": _stamp(session.session_open_at),
            "session_close_at": _stamp(session.session_close_at),
            "initial_scan_completed_at": _stamp(session.initial_scan_completed_at),
            "finalized_at": _stamp(session.finalized_at),
            "stale_data_seconds": session.stale_data_seconds,
            "universe_count": int(summary.get("universe_count", len(simulation.store.instruments))),
            "mapped_count": int(summary.get("mapped_count", len(simulation.pipeline.instruments))),
            "subscribed_count": int(
                summary.get("subscribed_count", len(simulation.store.subscribed_ids))
            ),
            "monitored_count": int(summary.get("monitored_count", len(simulation.pipeline._last_tick_at))),
            "qualified_count": int(summary.get("qualified_count", tracked)),
            "signals_generated": int(
                summary.get("signals_generated", counters.get("signals_raised", len(simulation.store.signals)))
            ),
            "telegram_delivered": sum(1 for row in telegram if row.delivery_status.value == "DELIVERED"),
            "telegram_failed": sum(
                1 for row in telegram if row.delivery_status.value in ("FAILED", "PERMANENTLY_FAILED")
            ),
            "notes": summary.get("notes"),
        },
        HISTORY_COLUMNS,
        "v_session_history",
    )


def config_row(simulation: Simulation) -> dict[str, Any]:
    """The parameter set this replay ran with, in ``scanner_configs`` shape.

    The in-memory store only holds a config row when one was saved, which the
    replay does not do; ``params.to_dict()`` is the same public shape (§13.3) and
    carries no secret, so it is written directly with a synthetic id.
    """
    stored = next(iter(simulation.store.configs.values()), None)
    config: ConfigRow | None = stored
    params = simulation.params.to_dict()
    return {
        "id": config.id if config else 0,
        "name": config.name if config else "default",
        "is_enabled": config.is_enabled if config else True,
        "config_version": config.config_version if config else params["config_version"],
        "created_by": config.created_by if config else "simulation",
        **params,
    }


def build_fixture(simulation: Simulation) -> dict[str, Any]:
    """Everything the dashboard's queries would return for this replayed session."""
    store = simulation.store
    snapshots_by_id: dict[int, IndicatorSnapshot] = {}
    for (instrument_id, candle_start), snapshot_id in store.snapshot_ids.items():
        snapshot = store.snapshots.get((instrument_id, candle_start))
        if snapshot is not None:
            snapshots_by_id[snapshot_id] = snapshot

    candles: dict[str, list[dict[str, Any]]] = {}
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for instrument in sorted(store.instruments.values(), key=lambda row: row.symbol):
        rows = store.candle_rows(instrument.id)
        if not rows:
            continue  # a silent instrument has no invented candle (§8)
        candles[instrument.symbol] = [candle_row(row) for row in rows]
        items = [
            snapshot_row(snapshot, store.snapshot_ids[(instrument.id, snapshot.candle_start)])
            for (stored_id, _), snapshot in store.snapshots.items()
            if stored_id == instrument.id
        ]
        snapshots[instrument.symbol] = sorted(items, key=lambda row: row["candle_start_at"] or "")

    memberships = sorted(
        store.memberships.values(),
        key=lambda row: (
            0 if row.current_state.value in ("ACTIVE", "REQUALIFIED") else 1,
            -(row.latest_score or 0.0),
            row.instrument_id,
        ),
    )
    events = sorted(store.events, key=lambda row: row.event_timestamp)
    return {
        "meta": {
            "kind": "scanner-replay-fixture",
            "schema_version": SCHEMA_VERSION,
            "generated_at": _stamp(datetime.now(tz=to_ist(simulation.session.session_open_at).tzinfo)),
            "source": "worker.scripts.export_dashboard_fixture (offline replay, in-memory store)",
            "simulated": True,
            "banner": SIMULATED_BANNER,
            "trading_date": _day(simulation.session.trading_date),
            "seed": simulation.options.seed,
            "minutes": simulation.minutes,
            "scan_minute": simulation.scan_minute,
            "dispatch_mode": simulation.options.dispatch,
            "note": (
                "Synthetic ticks driven through the production pipeline. No broker, no network, "
                "no real alerts. instrument_token and destination_reference are intentionally absent."
            ),
        },
        "coverage": coverage_row(store),
        "worker_status": worker_status_row(simulation),
        "config": config_row(simulation),
        "initial_scan": {
            # The same dict the pipeline journals into ``audit_logs`` in production,
            # so both sources hand the dashboard identical keys.
            **(simulation.initial_scan or {}),
            "completed_at": _stamp(simulation.store.sessions[simulation.session.id].initial_scan_completed_at),
        },
        "session": history_row(simulation),
        "history": [history_row(simulation)],
        "instruments": [instrument_row(item) for item in sorted(store.instruments.values(), key=lambda r: r.symbol)],
        "watchlist": [watchlist_row(row, simulation, snapshots_by_id) for row in memberships],
        "events": [event_row(event, simulation, index) for index, event in enumerate(events, start=1)],
        "signals": [
            signal_row(signal, simulation)
            for signal in sorted(store.signals.values(), key=lambda row: row.event_timestamp)
        ],
        "deliveries": [
            delivery_row(row)
            for row in sorted(store.deliveries.values(), key=lambda item: item.id or 0)
        ],
        "candles": candles,
        "snapshots": snapshots,
    }


#: The replay's own initial-scan minute. ``ScannerParams.initial_scan_candles``
#: is 5, but warm-up needs 20 candles, so a scan at minute 5 finds every
#: instrument WARMING_UP and the ranked list is legitimately empty — a sample
#: with no ranks would not exercise the dashboard's ranking column. 24 is the
#: first minute at which the scripted symbols have qualified, which is exactly
#: what ``scanner-simulate --scan-minute=24`` does.
DEFAULT_SCAN_MINUTE = 24


async def generate(seed: int, *, trading_date: date | None, scan_minute: int) -> dict[str, Any]:
    """Run the replay (no dispatch: every row must keep its simulated label) and gate it."""
    options = SimulationOptions(
        seed=seed, trading_date=trading_date or date(2026, 10, 12), scan_minute=scan_minute
    )
    simulation = await build(options)
    await simulation.run()
    checks = check_invariants(simulation, assemble_report(simulation))
    failed = [row["name"] for row in checks if row["status"] != "PASS"]
    if failed:
        raise SystemExit("refusing to write a fixture from a failing replay: " + "; ".join(failed))
    fixture = build_fixture(simulation)
    fixture["meta"]["invariants_passed"] = len(checks)
    return fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scanner-dashboard-data",
        description="Write the offline replay as dashboard-shaped JSON (simulated sample data).",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="destination JSON path")
    parser.add_argument("--seed", type=int, default=7, help="deterministic jitter seed")
    parser.add_argument("--date", dest="trading_date", help="YYYY-MM-DD (default 2026-10-12)")
    parser.add_argument(
        "--scan-minute",
        type=int,
        default=DEFAULT_SCAN_MINUTE,
        help="virtual minute the initial scan runs at (default %(default)s)",
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON (larger, diffable)")
    args = parser.parse_args(argv)

    trading_date = date.fromisoformat(args.trading_date) if args.trading_date else None
    fixture = asyncio.run(generate(args.seed, trading_date=trading_date, scan_minute=args.scan_minute))
    path: Path = args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            fixture,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    size_kb = path.stat().st_size / 1024
    print(
        f"wrote {path} ({size_kb:.0f} KB): {len(fixture['watchlist'])} watchlist row(s), "
        f"{len(fixture['signals'])} signal(s), {len(fixture['candles'])} symbol(s) with candles, "
        f"{fixture['meta']['invariants_passed']} invariant checks passed"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

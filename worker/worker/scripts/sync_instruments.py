"""Build/refresh the instrument table: universe CSV → validated SmartAPI tokens.

Usage::

    python -m worker.scripts.sync_instruments              # live sync into the DB
    python -m worker.scripts.sync_instruments --dry-run     # report, write nothing
    python -m worker.scripts.sync_instruments --local-master FILE.json
    python -m worker.scripts.sync_instruments --status      # what the DB already holds

Why this is a separate command rather than part of worker startup: the scrip
master is ~35 MB and changes daily, while the worker must come up fast after a
restart. Keeping the two apart also means a mapping failure is an operator
action with a clear message, not a silent 30-minute data gap (§8.2 coverage).

Honest-coverage rules enforced here:

* only NSE **cash-segment equities** are eligible. Per the verified live scrip
  master (docs/research/smartapi-ws-findings.md:260-266) those rows have
  ``exch_seg == "NSE"``, an **empty** ``instrumenttype`` and a ``symbol`` ending
  in ``-EQ``; ``instrumenttype == "EQUITY"`` does not appear in the file, and
  index rows carry ``AMXIDX``, so the ``-EQ`` suffix is the discriminator rather
  than ``exch_seg`` alone. No F&O or index token is ever reused for a cash symbol;
* an ambiguous or duplicate token is reported, never guessed;
* a symbol the table already holds but this run could not map is downgraded in
  place to ``UNMAPPED`` / ``AMBIGUOUS``. ``mark_mapping_status`` only writes the
  status column, which is sufficient: ``list_universe`` defaults to
  ``mapped_only=True``, so a demoted row is absent from every later subscription
  set (worker/main.py:236);
* a universe row that has *never* mapped is not written as an instrument, because
  ``instrument_token`` is ``NOT NULL`` and unique per exchange
  (supabase/migrations/0001_init.sql:38,49) — the visible gap for those symbols is
  the heartbeat's ``universe_instrument_count`` versus ``mapped_instrument_count``,
  which the worker fills from the snapshot this script also reads;
* the previous close is taken from the quote feed when available; a missing
  previous close is left NULL rather than inferred, because the intraday change
  condition cannot be evaluated without it. The quote payload's field casing has
  drifted between API versions (and has not been probed live from this host), so
  ``extract_previous_close`` tries every documented spelling and keeps only a
  positive number — anything else stays NULL and is counted in the warning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from ..config import WorkerSettings, load_settings
from ..logging_setup import configure_logging, get_logger, scrub
from ..marketdata.instruments import (
    ScripRow,
    load_universe,
    map_universe,
    parse_instrument_master,
)
from ..marketdata.sectors import sector_indices_from_master
from ..marketdata.smartapi_auth import SmartApiAuth
from ..marketdata.smartapi_rest import SmartApiRest
from ..models import MappingStatus
from ..persistence.memory import InMemoryStore
from ..persistence.postgres import PostgresStore
from ..persistence.store import CoverageCounts, ScannerStore
from ..timeutils import now_ist

log = get_logger(__name__)


def mapping_pct(counts: CoverageCounts) -> float:
    """Mapped share of the instruments table.

    Deliberately not ``CoverageCounts.coverage_pct``, which divides
    ``monitored_total`` by the universe: immediately after a sync no worker
    heartbeat exists yet, so that figure reads 0.0% and would fail the
    ``--min-coverage`` gate for reasons that have nothing to do with mapping.
    """
    if counts.universe_total <= 0:
        return 0.0
    return round(counts.mapped_total / counts.universe_total * 100.0, 2)


def build_store(settings: WorkerSettings) -> ScannerStore:
    return PostgresStore(settings.database_url) if settings.database_url else InMemoryStore()


def read_local_master(local_path: Path) -> list[dict[str, object]]:
    """Parse a saved scrip master. Its own function so the caller can thread it."""
    if not local_path.is_file():
        raise SystemExit(f"--local-master file not found: {local_path}")
    payload = json.loads(local_path.read_text())
    if not isinstance(payload, list):
        raise SystemExit("--local-master must be a JSON array of scrip rows")
    return payload


async def fetch_master(
    rest: SmartApiRest | None, local_path: Path | None
) -> tuple[list[dict[str, object]], str]:
    """Prefer a local snapshot (offline runs, CI), then the broker file."""
    if local_path is not None:
        # ~35 MB of JSON: parsed off the event loop so the CLI stays responsive.
        rows = await asyncio.to_thread(read_local_master, local_path)
        return rows, f"local:{local_path.name}"
    if rest is None:
        return [], "none"
    rows = await rest.fetch_instrument_master()
    return rows, "angel-scrip-master" if rows else "unavailable"


PREVIOUS_CLOSE_KEYS = (
    "previousClose",
    "previous_close",
    "preClose",
    "prevClose",
    "pClose",
    "pclose",
)


def extract_previous_close(row: dict[str, object]) -> float | None:
    """Pull a trustworthy previous close out of an Angel quote row.

    Angel's quote payload has changed field casing between API versions, so
    every documented spelling is tried and the value must be a positive number.
    If none of them is present the close stays ``None``: the scanner then treats
    the instrument as non-evaluable for the change condition rather than inventing
    a base price. No live quote probe has been run from this host yet, so the
    ``previous_closes`` count in the ``sync_complete`` log is the check — if it is
    zero while quotes were fetched, none of these keys matched and the real field
    name still has to be observed.
    """
    for key in PREVIOUS_CLOSE_KEYS:
        if key in row:
            try:
                value = float(row[key])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if value == value and value > 0:
                return value
    return None


async def collect_previous_closes(
    rest: SmartApiRest | None, token_to_id: dict[str, int]
) -> dict[int, float]:
    """Quote the mapped tokens and keep only a trustworthy previous close."""
    if rest is None or not token_to_id:
        return {}
    try:
        quotes = await rest.get_quotes(list(token_to_id))
    except Exception as exc:  # noqa: BLE001 - previous closes are optional input
        log.warning("quote_fetch_failed", error=scrub(str(exc))[:200])
        return {}
    out: dict[int, float] = {}
    missing = 0
    for token, row in quotes.items():
        instrument_id = token_to_id.get(str(token))
        if instrument_id is None or not isinstance(row, dict):
            continue
        close = extract_previous_close(row)
        if close is None:
            missing += 1
            continue
        out[instrument_id] = close
    if missing:
        log.warning(
            "previous_close_absent",
            count=missing,
            note="those instruments cannot be evaluated for the intraday change condition",
        )
    return out


def fno_underlyings(raw_rows: list[dict[str, object]]) -> set[str]:
    """F&O stock underlyings from a fresh scrip master.

    NFO stock-derivative rows (``FUTSTK``/``OPTSTK``) carry the underlying in
    ``name`` — that set *is* the F&O universe, so UNIVERSE=FNO filters on a
    flag recomputed from the same file the tokens come from, never from a
    hand-maintained list. Index derivatives (``FUTIDX``/``OPTIDX``) and NSE's
    test instruments are excluded.
    """
    names: set[str] = set()
    for row in raw_rows:
        if str(row.get("exch_seg") or "").upper() != "NFO":
            continue
        if str(row.get("instrumenttype") or "").upper() not in ("FUTSTK", "OPTSTK"):
            continue
        name = str(row.get("name") or "").strip().upper()
        if not name or name.startswith("NNNSETEST"):
            continue
        names.add(name)
    return names


def log_master_shape(raw_rows: list[dict[str, object]], master: list[ScripRow]) -> None:
    """Report what the cash-equity rule saw, so a silent filter change is visible.

    The eligibility test (``exch_seg == "NSE"`` plus a ``-EQ`` suffix) is the only
    place a whole universe can disappear without an error. If Angel ever ships the
    documented ``nse_cm`` casing instead of ``NSE``, every row is rejected and the
    mapping simply reports 0% — this line distinguishes "the master changed shape"
    from "the index changed constituents".
    """
    eq_rows = [row for row in raw_rows if str(row.get("symbol") or "").upper().endswith("-EQ")]
    segments = Counter(
        str(row.get("exch_seg") or "").strip().upper() or "(blank)" for row in eq_rows
    )
    log.info(
        "scrip_master_shape",
        raw_rows=len(raw_rows),
        parsed=len(master),
        eq_symbol_rows=len(eq_rows),
        eligible=sum(1 for row in master if row.is_nse_cash_equity),
        eq_segments=dict(segments.most_common(6)),
    )
    if not eq_rows:
        log.error(
            "master_shape_unexpected",
            note="no -EQ trading symbols in the master; refusing to treat that as an empty index",
        )


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    store = build_store(settings)
    await store.connect()

    auth: SmartApiAuth | None = None
    rest: SmartApiRest | None = None
    if not args.offline:
        if not settings.angel.configured:
            log.warning(
                "no_angel_credentials",
                note="use --offline plus --local-master to sync without the broker",
            )
        else:
            auth = SmartApiAuth(settings=settings.angel)
            rest = SmartApiRest(settings=settings.angel, auth=auth)

    try:
        universe = load_universe(today=now_ist().date())
        if universe.warning:
            log.warning("universe_snapshot", warning=universe.warning, rows=universe.size)
        else:
            log.info(
                "universe_snapshot",
                rows=universe.size,
                source=universe.source_file,
                retrieved_at=universe.retrieved_at,
            )

        if args.status:
            counts = await store.coverage(universe=settings.scanner.universe)
            log.info(
                "db_coverage",
                universe_rows=universe.size,
                instruments=counts.universe_total,
                mapped=counts.mapped_total,
                unmapped=counts.unmapped_total,
                ambiguous=counts.ambiguous_total,
                invalid=counts.invalid_total,
                subscribed=counts.subscribed_total,
                monitored=counts.monitored_total,
                mapping_pct=f"{mapping_pct(counts):.2f}",
                monitored_pct=f"{counts.coverage_pct:.2f}",
            )
            if counts.universe_total < universe.size:
                log.warning(
                    "universe_gap",
                    snapshot_rows=universe.size,
                    stored=counts.universe_total,
                    missing=universe.size - counts.universe_total,
                    note="symbols with no token in the table are absent by design; "
                    "the worker heartbeat reports the gap as universe vs mapped counts",
                )
            return 0

        raw_rows, master_source = await fetch_master(
            rest, Path(args.local_master) if args.local_master else None
        )
        if not raw_rows:
            log.error(
                "no_scrip_master",
                source=master_source,
                note="refusing to sync: an empty master would blank the mapping",
            )
            return 1
        master = parse_instrument_master(raw_rows)
        log_master_shape(raw_rows, master)
        result = map_universe(universe.rows, master)
        coverage = result.coverage()
        coverage["master_source"] = master_source
        log.info("mapping_result", **{k: v for k, v in coverage.items() if k != "notes"})
        for note in result.notes:
            log.info("mapping_note", note=note)
        if result.unmapped:
            log.warning(
                "unmapped_symbols",
                count=len(result.unmapped),
                examples=result.unmapped[:15],
                note="these universe symbols have no NSE cash-equity token in the master",
            )
        if result.ambiguous:
            log.warning(
                "ambiguous_symbols",
                count=len(result.ambiguous),
                examples=list(result.ambiguous.items())[:10],
            )

        if args.dry_run:
            log.info(
                "dry_run_no_write",
                mapped=result.mapped,
                unmapped=len(result.unmapped),
                ambiguous=len(result.ambiguous),
                universe_snapshot=universe.size,
                coverage_pct=coverage["coverage_pct"],
            )
            return 0

        # Flag F&O underlyings *before* the upsert so the written rows are
        # already correct; ``set_fno_symbols`` afterwards sweeps rows this
        # upsert did not touch (e.g. a stock that dropped out of the universe
        # but still lingers in the table) in the same pass.
        fno = fno_underlyings(raw_rows)
        instruments = [
            replace(item, is_fno=item.symbol.upper() in fno) for item in result.instruments
        ]
        if settings.scanner.universe == "FNO":
            # Operator scope (2026-10-12): the instruments table holds F&O
            # underlyings only — the NIFTY500-only mappings are deliberately
            # not stored, so no non-F&O row can re-enter via a re-sync.
            # Restoring full breadth is a revert of this filter plus a re-sync.
            before = len(instruments)
            instruments = [item for item in instruments if item.is_fno]
            log.info(
                "scope_filtered",
                universe="FNO",
                stored=len(instruments),
                skipped=before - len(instruments),
            )

        written = await store.upsert_instruments(instruments)
        flagged = await store.set_fno_symbols(fno)
        sector_rows = sector_indices_from_master(raw_rows)
        sectors_written = 0
        if sector_rows is not None:
            sectors_written = await store.upsert_sector_indices(sector_rows)
        else:
            log.warning(
                "sector_indices_missing",
                note="AMXIDX rows did not cover every expected index; "
                "the previous sector_indices table is left untouched",
            )
        log.info(
            "fno_universe",
            underlyings=len(fno),
            flag_flips=flagged,
            sector_indices=sectors_written,
        )
        # ``map_universe`` assigns no primary key, so the ids quotes map onto
        # must be read back from the database rather than guessed here.
        stored = await store.list_universe(universe=settings.scanner.universe)
        token_to_id = {
            row.instrument_token: row.id for row in stored if row.instrument_token and row.id
        }
        known = {row.symbol.upper() for row in stored}
        closes = await collect_previous_closes(rest, token_to_id)
        if closes:
            await store.set_previous_closes(closes)

        # A symbol the table still carries as live but this run could not map is
        # downgraded in place. ``mark_mapping_status`` only writes the status, and
        # that is enough: ``list_universe`` defaults to ``mapped_only=True``, so a
        # demoted row stops appearing in every later subscription set.
        stale_unmapped = [symbol for symbol in result.unmapped if symbol.upper() in known]
        stale_ambiguous = [symbol for symbol in result.ambiguous if symbol.upper() in known]
        demoted = 0
        if stale_unmapped:
            demoted += await store.mark_mapping_status(stale_unmapped, MappingStatus.UNMAPPED.value)
        if stale_ambiguous:
            demoted += await store.mark_mapping_status(
                stale_ambiguous, MappingStatus.AMBIGUOUS.value
            )
        new_gaps = len(result.unmapped) - len(stale_unmapped)
        log.info(
            "stale_mapping",
            demoted=demoted,
            never_mapped=new_gaps,
            note="never-mapped symbols are not inserted; see the module docstring",
        )

        final = await store.coverage(universe=settings.scanner.universe)
        run_pct = float(str(coverage["coverage_pct"]))
        log.info(
            "sync_complete",
            upserted=written,
            demoted=demoted,
            previous_closes=len(closes),
            db_instruments=final.universe_total,
            db_mapped=final.mapped_total,
            db_mapping_pct=f"{mapping_pct(final):.2f}",
            run_mapping_pct=f"{run_pct:.2f}",
        )
        # The gate uses this run's rate (mapped / universe rows seen), not the
        # table's ratio: a partial sync must not pass because old rows linger.
        if run_pct < args.min_coverage:
            log.error(
                "coverage_below_minimum",
                run_mapping_pct=f"{run_pct:.2f}",
                required=args.min_coverage,
                master_source=master_source,
            )
            return 5
        return 0
    finally:
        if rest is not None:
            await rest.close()
        if auth is not None:
            await auth.close()
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scanner-sync-instruments", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--dry-run", action="store_true", help="report coverage, write nothing")
    parser.add_argument("--status", action="store_true", help="print the DB coverage and exit")
    parser.add_argument("--offline", action="store_true", help="never call the broker")
    parser.add_argument("--local-master", help="path to a saved OpenAPIScripMaster.json")
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=90.0,
        help="exit non-zero when THIS RUN maps fewer than this percentage of the "
        "universe rows it read (not the table's historical ratio)",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary: one clear line, no traceback
        log.error("sync_failed", error=scrub(str(exc))[:300])
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

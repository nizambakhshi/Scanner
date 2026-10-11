"""Universe loading and Angel One instrument-token mapping (spec §5).

Two distinct concerns are kept deliberately separate:

1. **Membership** — who is in Nifty 500 today. Read from a dated CSV snapshot
   (``data/universe/``) whose hash and provenance live in a manifest. If the
   snapshot cannot be refreshed the last known list is still used, but it is
   flagged ``stale`` and the dashboard says so.
2. **Mapping** — which Angel One ``symboltoken`` represents that company today.
   Read from the official instrument master (``OpenAPIScripMaster.json``),
   which is regenerated daily.

Hard-coding token IDs is forbidden: NSE renames symbols and re-issues tokens,
and a wrong token silently produces a wrong chart. Everything here therefore
matches on documented fields (``exch_seg``, ``symbol``, ``name``,
``instrumenttype``) and reports what it could not map instead of guessing.

Verified against the live master file on 2026-10-11: NSE cash-equity rows have
``symbol == "RELIANCE-EQ"``, ``name == "RELIANCE"``, ``instrumenttype == ""``,
``exch_seg == "NSE"``; there is no ``EQ`` instrumenttype at all.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..logging_setup import get_logger
from ..models import Instrument, MappingStatus
from ..timeutils import now_ist

log = get_logger(__name__)

UNIVERSE_DIR = Path(__file__).resolve().parents[3] / "data" / "universe"

# Rows in the index file that are not ordinary equities (REITs, inv trusts).
# They still trade, so they stay in the universe; they are only excluded from
# the "equity" count the dashboard reports.
NON_EQUITY_SERIES = {"RR", "IT", "BE", "Z", "MT", "SM", "DT"}

_TOKEN_SAFE = re.compile(r"^[0-9A-Za-z._-]{1,24}$")


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    company_name: str
    industry: str
    series: str
    isin: str


@dataclass(frozen=True)
class UniverseSnapshot:
    rows: tuple[UniverseRow, ...]
    source_file: str
    retrieved_at: str | None
    sha256: str | None
    stale: bool
    warning: str | None = None

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows)

    def status(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "retrieved_at": self.retrieved_at,
            "sha256": self.sha256,
            "row_count": self.size,
            "stale": self.stale,
            "warning": self.warning,
            "series": dict(Counter(row.series for row in self.rows)),
        }


def _latest_universe_file(directory: Path) -> Path | None:
    candidates = sorted(directory.glob("*.csv"), key=lambda p: p.name, reverse=True)
    return candidates[0] if candidates else None


def load_universe(
    path: Path | None = None,
    *,
    directory: Path = UNIVERSE_DIR,
    today: date | None = None,
    max_age_days: int = 45,
) -> UniverseSnapshot:
    """Read the newest Nifty 500 snapshot and judge how trustworthy it is."""
    target = path or _latest_universe_file(directory)
    reference = today or now_ist().date()
    if target is None or not target.is_file():
        return UniverseSnapshot(
            rows=(),
            source_file=str(target) if target else "(none)",
            retrieved_at=None,
            sha256=None,
            stale=True,
            warning=f"no universe CSV found in {directory}",
        )

    manifest = directory / f"{target.stem}_manifest.json"
    if not manifest.is_file():
        manifest = directory / "nifty500_manifest.json"
    meta: dict[str, object] = {}
    if manifest.is_file():
        try:
            meta = json.loads(manifest.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("universe_manifest_unreadable", path=str(manifest), error=str(exc)[:200])

    warning: str | None = None
    expected_rows = meta.get("row_count")
    retrieved_at = str(meta.get("retrieved_at") or "") or None

    text = target.read_text(encoding="utf-8-sig")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    expected_sha = meta.get("sha256")
    if isinstance(expected_sha, str) and expected_sha and expected_sha != digest:
        warning = (
            f"{target.name} does not match its manifest hash "
            "(the file changed after it was recorded)"
        )

    rows: list[UniverseRow] = []
    reader = csv.DictReader(text.splitlines())
    columns = {(raw or "").strip().lower(): raw for raw in (reader.fieldnames or [])}
    key_symbol = columns.get("symbol", "Symbol")
    key_company = columns.get("company name", "Company Name")
    key_industry = columns.get("industry", "Industry")
    key_series = columns.get("series", "Series")
    key_isin = columns.get("isin code", "ISIN Code")
    for raw in reader:
        symbol = (raw.get(key_symbol) or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            UniverseRow(
                symbol=symbol,
                company_name=(raw.get(key_company) or symbol).strip(),
                industry=(raw.get(key_industry) or "").strip(),
                series=(raw.get(key_series) or "EQ").strip().upper(),
                isin=(raw.get(key_isin) or "").strip().upper(),
            )
        )
    if not rows:
        warning = f"{target.name} parsed but contained no usable rows"

    if isinstance(expected_rows, int) and expected_rows and expected_rows != len(rows):
        warning = (
            (warning + "; " if warning else "")
            + f"row count {len(rows)} differs from the manifest's {expected_rows}"
        )

    stale = warning is not None
    if retrieved_at:
        try:
            age = reference - date.fromisoformat(retrieved_at[:10])
            if age.days > max_age_days:
                stale = True
                warning = (
                    (warning + "; " if warning else "")
                    + f"universe snapshot is {age.days} days old (limit {max_age_days})"
                )
        except ValueError:
            stale = True
            warning = (warning + "; " if warning else "") + f"unusable retrieved_at {retrieved_at!r}"
    else:
        stale = True
        warning = (warning + "; " if warning else "") + "universe snapshot has no retrieved_at date"

    if warning:
        log.warning("universe_stale_or_suspect", file=target.name, warning=warning[:400])
    return UniverseSnapshot(
        rows=tuple(rows),
        source_file=target.name,
        retrieved_at=retrieved_at,
        sha256=digest,
        stale=stale,
        warning=warning,
    )


@dataclass(frozen=True)
class ScripRow:
    """The fields of the Angel instrument master this system depends on."""

    token: str
    symbol: str
    name: str
    exchange: str
    instrument_type: str
    tick_size: float | None = None
    lot_size: int = 1
    expiry: str = ""
    strike: str = ""

    @property
    def is_nse_cash_equity(self) -> bool:
        return self.exchange == "NSE" and self.instrument_type == "" and self.symbol.endswith("-EQ")


def parse_instrument_master(rows: Iterable[dict[str, object]]) -> list[ScripRow]:
    parsed: list[ScripRow] = []
    for row in rows:
        token = str(row.get("token") or "").strip()
        symbol = str(row.get("symbol") or "").strip().upper()
        if not token or not symbol:
            continue
        parsed.append(
            ScripRow(
                token=token,
                symbol=symbol,
                name=str(row.get("name") or "").strip().upper(),
                exchange=str(row.get("exch_seg") or "").strip().upper(),
                instrument_type=str(row.get("instrumenttype") or "").strip().upper(),
                tick_size=_optional_float(row.get("tick_size")),
                lot_size=int(_optional_float(row.get("lotsize")) or 1),
                expiry=str(row.get("expiry") or ""),
                strike=str(row.get("strike") or ""),
            )
        )
    return parsed


@dataclass
class MappingResult:
    """What the sync produced, and honestly what it could not."""

    instruments: list[Instrument] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    duplicates: list[str] = field(default_factory=list)
    master_retrieved_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def mapped(self) -> int:
        return len(self.instruments)

    def coverage(self) -> dict[str, object]:
        total = self.mapped + len(self.unmapped) + len(self.ambiguous)
        return {
            "universe_rows": total,
            "mapped": self.mapped,
            "unmapped": len(self.unmapped),
            "ambiguous": len(self.ambiguous),
            "duplicate_tokens": len(self.duplicates),
            "coverage_pct": round(100.0 * self.mapped / total, 2) if total else 0.0,
            "notes": list(self.notes),
        }


def map_universe(
    universe: Sequence[UniverseRow],
    master: Sequence[ScripRow],
    *,
    verified_at: datetime | None = None,
) -> MappingResult:
    """Match index symbols to NSE cash-equity tokens without guessing.

    Matching order (documented field semantics, not string tricks):
    ``SYMBOL-EQ`` trading symbol → bare ``name`` on an ``-EQ`` row → unique
    ``name`` match. Anything else is reported, never assumed.
    """
    stamp = verified_at or now_ist()
    by_symbol: dict[str, list[ScripRow]] = {}
    by_name: dict[str, list[ScripRow]] = {}
    seen_tokens: Counter[str] = Counter()
    for row in master:
        if not row.is_nse_cash_equity:
            continue
        seen_tokens[row.token] += 1
        by_symbol.setdefault(row.symbol, []).append(row)
        bare = row.symbol[: -len("-EQ")]
        by_symbol.setdefault(bare, []).append(row)
        if row.name:
            by_name.setdefault(row.name, []).append(row)

    duplicates = sorted(token for token, count in seen_tokens.items() if count > 1)
    result = MappingResult(master_retrieved_at=stamp, duplicates=duplicates)
    used_tokens: set[str] = set()

    for entry in universe:
        symbol = entry.symbol.upper()
        candidates = by_symbol.get(f"{symbol}-EQ") or by_symbol.get(symbol) or []
        if not candidates:
            candidates = [row for row in by_name.get(symbol, []) if row.is_nse_cash_equity]
        exact = [row for row in candidates if row.token]
        if not exact:
            result.unmapped.append(entry.symbol)
            continue
        if len(exact) > 1 and len({row.token for row in exact}) > 1:
            result.ambiguous[entry.symbol] = sorted({row.token for row in exact})
            continue
        chosen = exact[0]
        if chosen.token in used_tokens:
            # Two index symbols pointing at one token: keep the first, flag it.
            result.duplicates.append(f"{entry.symbol}:{chosen.token}")
            continue
        used_tokens.add(chosen.token)
        result.instruments.append(
            Instrument(
                id=0,  # the database assigns the primary key on upsert
                exchange="NSE",
                symbol=entry.symbol,
                trading_symbol=chosen.symbol,
                company_name=entry.company_name or chosen.name,
                instrument_token=chosen.token,
                instrument_type=entry.series or "EQ",
                is_nifty500=True,
                is_active=True,
                mapping_status=MappingStatus.MAPPED,
                last_verified_at=stamp,
            )
        )
    return result


def instruments_for_universe_status(universe: UniverseSnapshot) -> list[Instrument]:
    """Rows the DB should keep but be marked unmapped (membership known, token not)."""
    return [
        Instrument(
            id=0,
            exchange="NSE",
            symbol=row.symbol,
            trading_symbol=row.symbol,
            company_name=row.company_name,
            instrument_token="",
            instrument_type=row.series,
            is_nifty500=True,
            is_active=False,
            mapping_status=MappingStatus.UNMAPPED,
        )
        for row in universe.rows
    ]


def _optional_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None

"""Nifty sector indices: the fixed token map + the live tracker behind the dashboard tile.

The sector tile streams Nifty *indices* (NIFTY IT, NIFTY BANK, …) off the same
smart-stream socket the stocks use. Two pieces live here:

* ``SECTOR_INDICES`` — NSE's stable index codes, verified against the scrip
  master's ``AMXIDX`` rows on 2026-10-12 (``margincalculator.angelone.in`` CDN,
  no login). ``sync_instruments`` cross-checks these tokens against the master
  every run and overwrites the ``sector_indices`` table, so a drifted token is
  corrected by a re-sync rather than silently wrong here;
* :class:`SectorTracker` — the worker-side buffer between the feed callback and
  the database. Index frames arrive many times per second per index; the store
  upsert runs on a fixed cadence (``SECTOR_FLUSH_SECONDS``), so the tracker
  keeps only the newest quote per index and the flush writes a bounded, small
  batch. ``sector_quotes`` is a scratchpad, never an append log.
"""

from __future__ import annotations

from ..models import IndexTick, SectorIndex, SectorQuote

#: (index_symbol, display_name, token) in tile order. ``index_symbol`` matches
#: the scrip master's AMXIDX ``name`` so the cross-check is exact.
SECTOR_INDICES: tuple[tuple[str, str, str], ...] = (
    ("NIFTY", "Nifty 50", "26000"),
    ("BANKNIFTY", "Nifty Bank", "26009"),
    ("FINNIFTY", "Nifty Fin Service", "26037"),
    ("NIFTY IT", "Nifty IT", "26008"),
    ("NIFTY AUTO", "Nifty Auto", "26029"),
    ("NIFTY PHARMA", "Nifty Pharma", "26023"),
    ("NIFTY FMCG", "Nifty FMCG", "26021"),
    ("NIFTY METAL", "Nifty Metal", "26030"),
    ("NIFTY ENERGY", "Nifty Energy", "26020"),
    ("NIFTY REALTY", "Nifty Realty", "26018"),
    ("NIFTY MEDIA", "Nifty Media", "26031"),
    ("NIFTY PSU BANK", "Nifty PSU Bank", "26025"),
    ("NIFTY PVT BANK", "Nifty Private Bank", "26047"),
)


def default_sector_indices() -> list[SectorIndex]:
    """The built-in map, in tile order."""
    return [
        SectorIndex(index_symbol=symbol, display_name=display, instrument_token=token, display_order=order)
        for order, (symbol, display, token) in enumerate(SECTOR_INDICES)
    ]


def sector_indices_from_master(rows: list[dict[str, object]]) -> list[SectorIndex] | None:
    """Cross-check the built-in tokens against AMXIDX rows from a fresh master.

    Returns ``None`` when any index is missing from the master (a partial or
    unexpected file) — the caller then keeps the previous table rather than
    writing a half-verified set. Scrip-master index tokens carry a ``999``
    prefix on the wire form; the raw NSE code is what the feed expects.
    """
    found: dict[str, str] = {}
    for row in rows:
        if row.get("exch_seg") == "NSE" and row.get("instrumenttype") == "AMXIDX":
            name = str(row.get("name") or "").strip().upper()
            token = str(row.get("token") or "").strip()
            if name and token:
                found[name] = token.removeprefix("999")
    out: list[SectorIndex] = []
    for symbol, display, _default_token in SECTOR_INDICES:
        token = found.get(symbol)
        if not token:
            return None
        out.append(
            SectorIndex(index_symbol=symbol, display_name=display, instrument_token=token, display_order=len(out))
        )
    return out


#: How often the worker flushes dirty sector quotes into ``sector_quotes``.
SECTOR_FLUSH_SECONDS = 5.0


class SectorTracker:
    """Newest-wins buffer between the feed callback and the store flush."""

    def __init__(self) -> None:
        self._latest: dict[str, SectorQuote] = {}
        self._dirty: set[str] = set()
        self.updates = 0

    def on_tick(self, tick: IndexTick) -> None:
        change_pct: float | None = None
        if tick.previous_close is not None and tick.previous_close > 0:
            change_pct = (tick.last_price - tick.previous_close) / tick.previous_close * 100.0
        self._latest[tick.index_symbol] = SectorQuote(
            index_symbol=tick.index_symbol,
            last_price=tick.last_price,
            previous_close=tick.previous_close,
            change_pct=change_pct,
            source_updated_at=tick.exchange_timestamp,
        )
        self._dirty.add(tick.index_symbol)
        self.updates += 1

    def drain(self) -> list[SectorQuote]:
        """Rows changed since the last drain, tile order; clears the dirty set."""
        rows = [self._latest[symbol] for symbol in sorted(self._dirty)]
        self._dirty.clear()
        return rows

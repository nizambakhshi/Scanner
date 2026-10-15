"""Daily watchlist state machine (spec §11).

One row per (session, instrument) — the schema enforces that uniqueness — while
*every* transition is appended to ``watchlist_events``. A stock that is removed
and later re-qualifies therefore keeps its full day history in the event log
and the row simply advances ``episode_count``; nothing is overwritten (§11.7).

Transition rules, implemented exactly as specified:

``ADDED``
    all three primary conditions hold on a valid completed candle.
``WEAKENING``
    close < VWAP **or** EMA(fast) <= EMA(slow). The stock stays visible.
``REMOVED``
    close < VWAP **and** EMA(fast) <= EMA(slow), sustained over
    ``removal_confirm_candles`` *valid* candles. Incomplete, stale or missing
    candles never count toward the streak — and never reset it either, because a
    feed gap is not evidence about the price.
``REQUALIFIED``
    a removed stock passes all conditions again.
``DATA_STALE``
    the feed for this instrument cannot be trusted right now; the row is parked,
    not removed.

The price cap (``max_price``) gates *entry only*: a name trading above the cap
is not taken onto the watchlist and does not re-qualify, but a membership that
already exists keeps its whole state machine — the cap can keep names out, it
never removes one that is already being tracked. 0 disables the cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .alerts import AlertEngine, episode_id_for
from .analytics.ranking import score_snapshot
from .analytics.scanner import Scanner
from .config import ScannerParams
from .logging_setup import get_logger
from .models import (
    TRACKED_STATES,
    Candle,
    DataQuality,
    IndicatorSnapshot,
    Instrument,
    SignalType,
    WatchlistEvent,
    WatchlistMembership,
    WatchlistState,
    is_tracked,
)
from .persistence.store import ScannerStore

log = get_logger(__name__)

USABLE_QUALITIES = frozenset({DataQuality.OK, DataQuality.PARTIAL})

# States in which the instrument is still being tracked this session. A
# REQUALIFIED row is active: it re-entered after removal and must keep reacting
# to later candles rather than being closed out by ``_transition``.
ACTIVE_STATES = tuple(TRACKED_STATES)


def is_usable(candle: Candle) -> bool:
    """Only a completed candle with acceptable quality may change a state."""
    return bool(candle.is_complete) and candle.data_quality in USABLE_QUALITIES


@dataclass
class WatchlistOutcome:
    """The result of feeding one candle through the state machine."""

    instrument_id: int
    symbol: str
    previous_state: WatchlistState | None
    state: WatchlistState
    changed: bool = False
    event_type: str = "NO_CHANGE"
    reason: str = ""
    episode_id: str | None = None
    signaled: bool = False
    membership: WatchlistMembership | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class WatchlistManager:
    """Applies §11 to the stream of finalized candles."""

    store: ScannerStore
    params: ScannerParams
    alerts: AlertEngine | None = None
    simulation: bool = False

    def __post_init__(self) -> None:
        self.scanner = Scanner(self.params)
        # Instrument ids whose latest candle could not be used, for reporting.
        self.skipped_unusable: set[int] = set()

    # ── per-candle transition ─────────────────────────────────────────────

    async def on_candle(
        self,
        *,
        session_id: int,
        trading_date: date,
        instrument: Instrument,
        candle: Candle,
        snapshot: IndicatorSnapshot,
        bullish: bool,
        evaluable: bool,
        reasons: tuple[str, ...],
        now: datetime,
        score: float | None = None,
        snapshot_row_id: int | None = None,
    ) -> WatchlistOutcome:
        membership = await self.store.get_membership(session_id, instrument.id)
        previous = membership.current_state if membership else None
        usable = is_usable(candle)
        snap_id = snapshot_row_id

        if not usable:
            self.skipped_unusable.add(instrument.id)
            # A gap is not evidence: park an *existing* active row as stale and
            # never invent a transition from data we do not have.
            if membership and membership.current_state in ACTIVE_STATES:
                await self._transition(
                    membership,
                    WatchlistState.DATA_STALE,
                    event_type="DATA_STALE",
                    reason=f"candle quality {candle.data_quality.value}",
                    now=now,
                    snapshot_id=snap_id,
                    price=candle.close,
                )
                return WatchlistOutcome(
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    previous_state=previous,
                    state=WatchlistState.DATA_STALE,
                    changed=True,
                    event_type="DATA_STALE",
                    reason="unusable candle",
                    membership=membership,
                )
            return WatchlistOutcome(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                previous_state=previous,
                state=previous or WatchlistState.WARMING_UP,
                reason="unusable candle",
                membership=membership,
            )

        self.skipped_unusable.discard(instrument.id)

        # Ranking is refreshed on every valid candle for anything we track.
        rank_score = score if score is not None else score_snapshot(snapshot, self.params).score

        if membership is None:
            if bullish and evaluable:
                if self._above_price_cap(snapshot):
                    return WatchlistOutcome(
                        instrument_id=instrument.id,
                        symbol=instrument.symbol,
                        previous_state=None,
                        state=WatchlistState.REMOVED,
                        reason="above tracking cap",
                        details={"max_price": self.params.max_price, "close": snapshot.close},
                    )
                created = await self._enter(
                    session_id=session_id,
                    instrument=instrument,
                    snapshot=snapshot,
                    reasons=reasons,
                    now=now,
                    score=rank_score,
                    episode=1,
                    requalified=False,
                    snapshot_row_id=snap_id,
                )
                return created
            return WatchlistOutcome(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                previous_state=None,
                state=WatchlistState.WARMING_UP if not evaluable else WatchlistState.REMOVED,
                reason="not qualified",
                details={"evaluable": evaluable, "bullish": bullish},
            )

        # An existing row. `latest_price` and the snapshot link are always
        # supplied through the transition helper, so they must not also appear
        # here (duplicate keyword arguments).
        updates: dict[str, Any] = {
            "latest_score": rank_score,
        }

        if bullish and evaluable:
            if membership.current_state in (WatchlistState.REMOVED, WatchlistState.DATA_STALE):
                if self._above_price_cap(snapshot):
                    # A capped name does not re-enter; the row keeps whatever
                    # state it already had (REMOVED / DATA_STALE) and only its
                    # score note moves, exactly like any other untracked row.
                    await self.store.update_membership(membership.id, **updates)
                    return WatchlistOutcome(
                        instrument_id=instrument.id,
                        symbol=instrument.symbol,
                        previous_state=previous,
                        state=membership.current_state,
                        reason="above tracking cap: not requalified",
                        details={"max_price": self.params.max_price, "close": snapshot.close},
                        membership=membership,
                    )
                outcome = await self._enter(
                    session_id=session_id,
                    instrument=instrument,
                    snapshot=snapshot,
                    reasons=reasons,
                    now=now,
                    score=rank_score,
                    episode=membership.episode_count + 1,
                    requalified=True,
                    snapshot_row_id=snap_id,
                )
                return outcome
            if membership.current_state is WatchlistState.WEAKENING:
                # Recovery restarts the "consecutive" count: without this, the
                # streak from the pre-recovery candles is inherited and a single
                # bearish candle removes an instrument that has just become fully
                # qualified again (spec rule 3 requires three *consecutive*).
                await self._transition(
                    membership,
                    WatchlistState.ACTIVE,
                    event_type="RECOVERY_TO_ACTIVE",
                    reason="all bullish conditions hold again",
                    now=now,
                    snapshot_id=snap_id,
                    price=candle.close,
                    extra={**updates, "consecutive_weak_candles": 0},
                )
                membership.consecutive_weak_candles = 0
                return WatchlistOutcome(
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    previous_state=previous,
                    state=WatchlistState.ACTIVE,
                    changed=True,
                    event_type="RECOVERY_TO_ACTIVE",
                    reason="recovered",
                    episode_id=episode_id_for(
                        trading_date=trading_date,
                        instrument_id=instrument.id,
                        episode=membership.episode_count,
                    ),
                    membership=membership,
                )
            await self.store.update_membership(membership.id, consecutive_weak_candles=0, **updates)
            membership.consecutive_weak_candles = 0
            return WatchlistOutcome(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                previous_state=previous,
                state=membership.current_state,
                membership=membership,
                reason="still qualified",
            )

        # Not bullish (or not evaluable): evaluate the documented weakening and
        # removal predicates. Warm-up-only gaps must not remove a stock.
        if not evaluable:
            await self.store.update_membership(membership.id, **updates)
            return WatchlistOutcome(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                previous_state=previous,
                state=membership.current_state,
                reason="indicators not evaluable",
                membership=membership,
            )

        # A row that is not tracked any more stays untracked. Only a fully bullish
        # re-qualification (the branch above) may bring it back; running the
        # weakening/removal machinery on it would resurrect a REMOVED instrument as
        # tracked WEAKENING, or re-emit WATCHLIST_REMOVED on every bearish candle
        # (spec rule 4).
        if not is_tracked(membership.current_state):
            await self.store.update_membership(membership.id, **updates)
            return WatchlistOutcome(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                previous_state=previous,
                state=membership.current_state,
                reason="untracked: only re-qualification re-enters",
                membership=membership,
            )

        if self.scanner.is_removal_condition(snapshot):
            streak = membership.consecutive_weak_candles + 1
            if streak >= self.params.removal_confirm_candles:
                await self._transition(
                    membership,
                    WatchlistState.REMOVED,
                    event_type="REMOVED",
                    reason=(
                        f"close below VWAP and EMA{self.params.fast_ema_period} at or below "
                        f"EMA{self.params.slow_ema_period} for {streak} consecutive valid candles"
                    ),
                    now=now,
                    snapshot_id=snap_id,
                    price=candle.close,
                    is_active=False,
                    removal_reason="PERSISTENT_BELOW_VWAP_AND_EMA_CROSS_DOWN",
                    extra={**updates, "consecutive_weak_candles": streak},
                )
                await self._signal_removal(membership, snapshot, candle, now, instrument)
                return WatchlistOutcome(
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    previous_state=previous,
                    state=WatchlistState.REMOVED,
                    changed=True,
                    event_type="REMOVED",
                    reason=f"{streak} consecutive bearish candles",
                    membership=membership,
                )
            # Entering WEAKENING is a state change, so it goes through
            # ``_transition`` and lands in the append-only event log like every
            # other one (spec rule 4); a row already WEAKENING only extends its
            # streak.
            was_weakening = membership.current_state is WatchlistState.WEAKENING
            if was_weakening:
                await self.store.update_membership(
                    membership.id, consecutive_weak_candles=streak, **updates
                )
            else:
                await self._transition(
                    membership,
                    WatchlistState.WEAKENING,
                    event_type="WEAKENING",
                    reason=f"removal streak {streak}/{self.params.removal_confirm_candles}",
                    now=now,
                    snapshot_id=snap_id,
                    price=candle.close,
                    extra={**updates, "consecutive_weak_candles": streak},
                )
            membership.consecutive_weak_candles = streak
            return WatchlistOutcome(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                previous_state=previous,
                state=WatchlistState.WEAKENING,
                changed=not was_weakening,
                event_type="WEAKENING",
                reason=f"removal streak {streak}/{self.params.removal_confirm_candles}",
                membership=membership,
            )

        # Weakening (one of the two conditions) without a removal streak.
        new_state = (
            WatchlistState.WEAKENING
            if self.scanner.is_weakening(snapshot)
            else WatchlistState.ACTIVE
        )
        if new_state is not membership.current_state:
            await self._transition(
                membership,
                new_state,
                event_type="WEAKENING" if new_state is WatchlistState.WEAKENING else "RECOVERY_TO_ACTIVE",
                reason=(
                    "close below VWAP or EMA spread non-positive"
                    if new_state is WatchlistState.WEAKENING
                    else "conditions recovered"
                ),
                now=now,
                snapshot_id=snap_id,
                price=candle.close,
                extra={**updates, "consecutive_weak_candles": 0},
            )
            if new_state is WatchlistState.WEAKENING and previous is WatchlistState.ACTIVE:
                await self._signal_weakening(membership, snapshot, candle, now, instrument)
            return WatchlistOutcome(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                previous_state=previous,
                state=new_state,
                changed=True,
                event_type="WEAKENING" if new_state is WatchlistState.WEAKENING else "RECOVERY_TO_ACTIVE",
                reason="predicate changed",
                membership=membership,
            )
        await self.store.update_membership(
            membership.id, consecutive_weak_candles=0, **updates
        )
        return WatchlistOutcome(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            previous_state=previous,
            state=new_state,
            reason="no change",
            membership=membership,
        )

    # ── initial scan (§11.1) ──────────────────────────────────────────────

    async def initial_scan(
        self,
        *,
        session_id: int,
        trading_date: date,
        instruments: list[Instrument],
        latest: dict[int, tuple[Candle, IndicatorSnapshot, bool, bool, tuple[str, ...]]],
        now: datetime,
    ) -> dict[str, Any]:
        """Evaluate every mapped instrument once and rank the qualifiers."""
        qualified: list[tuple[float, Instrument]] = []
        counts = {
            "evaluated": 0,
            "qualified": 0,
            "warming_up": 0,
            "weakening": 0,
            "stale": 0,
            "not_qualified": 0,
            "missing_data": 0,
            "above_price_cap": 0,
        }
        for instrument in instruments:
            payload = latest.get(instrument.id)
            if payload is None:
                counts["missing_data"] += 1
                continue
            candle, snapshot, bullish, evaluable, reasons = payload
            counts["evaluated"] += 1
            if self._above_price_cap(snapshot):
                # A capped name with no membership anywhere is counted and
                # skipped outright. One that *has* a membership (from an
                # earlier scan or before the cap was configured) still goes
                # through ``on_candle``: the gates there block re-entry while
                # leaving its existing state machine untouched.
                membership = await self.store.get_membership(session_id, instrument.id)
                if membership is None:
                    counts["above_price_cap"] += 1
                    continue
            outcome = await self.on_candle(
                session_id=session_id,
                trading_date=trading_date,
                instrument=instrument,
                candle=candle,
                snapshot=snapshot,
                bullish=bullish,
                evaluable=evaluable,
                reasons=reasons,
                now=now,
            )
            if outcome.state is WatchlistState.WARMING_UP:
                counts["warming_up"] += 1
            elif outcome.state is WatchlistState.DATA_STALE:
                counts["stale"] += 1
            elif outcome.state in (WatchlistState.ACTIVE, WatchlistState.REQUALIFIED):
                # §11.1 ranks *the qualifiers* — the set of names that currently
                # satisfy the strategy — not merely the admissions that happened
                # inside this call. A name that entered at 09:16 and is still
                # ACTIVE at the 09:20 scan is a qualifier; filtering on
                # ``outcome.changed`` used to skip it, so every early entrant was
                # missing from the daily ranked list and kept ``latest_rank``
                # NULL for the rest of the session.
                counts["qualified"] += 1
                score = outcome.membership.latest_score if outcome.membership else None
                qualified.append((score if score is not None else snapshot.relative_volume or 0.0, instrument))
            elif outcome.state is WatchlistState.WEAKENING:
                counts["weakening"] += 1
            else:
                counts["not_qualified"] += 1

        qualified.sort(key=lambda item: item[0], reverse=True)
        ranked = [instrument.symbol for _, instrument in qualified]
        for rank, (_, instrument) in enumerate(qualified, start=1):
            membership = await self.store.get_membership(session_id, instrument.id)
            if membership is not None:
                await self.store.update_membership(membership.id, latest_rank=rank)
        summary: dict[str, Any] = {
            "kind": "initial_scan",
            "trading_date": trading_date.isoformat(),
            "universe": len(instruments),
            "counts": counts,
            "ranked": ranked,
            "candles_required": self.params.warmup_candles_required,
            "initial_scan_candles": self.params.initial_scan_candles,
        }
        return summary

    # ── end of day (§11.8) ────────────────────────────────────────────────

    async def finalize_day(self, *, session_id: int, now: datetime) -> dict[str, Any]:
        frozen = await self.store.freeze_session_watchlist(session_id)
        memberships = await self.store.list_memberships(session_id, include_inactive=True)
        unresolved_stale = [
            m.id for m in memberships if m.current_state is WatchlistState.DATA_STALE
        ]
        return {
            "kind": "end_of_day",
            "frozen_rows": frozen,
            "total_rows": len(memberships),
            "unresolved_stale_memberships": unresolved_stale,
            "finalized_at": now.isoformat(),
        }

    # ── internals ─────────────────────────────────────────────────────────

    def _above_price_cap(self, snapshot: IndicatorSnapshot) -> bool:
        """The operator's price cap: F&O names above ``max_price`` are not
        taken onto the watchlist.

        Compares the snapshot close — the price the entry itself would record —
        so the gate and the recorded ``initial_entry_price`` can never disagree.
        Active only while the cap is positive; ``0`` disables it entirely. This
        gates *entry* (fresh admission and re-qualification); it never removes
        a membership that is already tracked.
        """
        return self.params.max_price > 0 and snapshot.close > self.params.max_price

    async def _enter(
        self,
        *,
        session_id: int,
        instrument: Instrument,
        snapshot: IndicatorSnapshot,
        reasons: tuple[str, ...],
        now: datetime,
        score: float | None,
        episode: int,
        requalified: bool,
        snapshot_row_id: int | None = None,
    ) -> WatchlistOutcome:
        existing = await self.store.get_membership(session_id, instrument.id)
        state = WatchlistState.REQUALIFIED if requalified else WatchlistState.ACTIVE
        if existing is None:
            membership = await self.store.create_membership(
                session_id,
                instrument.id,
                state,
                entered_at=now,
                price=snapshot.close,
                score=score,
                snapshot_id=snapshot_row_id,
                is_simulated=self.simulation,
            )
            # The module contract says *every* transition is appended. A row that
            # is created rather than updated still entered the watchlist, so its
            # entry belongs in the event log too — otherwise the log's first entry
            # for an instrument would look like it started life ACTIVE with no
            # recorded admission (§11.7).
            await self.store.add_watchlist_event(
                WatchlistEvent(
                    watchlist_id=membership.id,
                    event_type="REQUALIFIED" if requalified else "ADDED",
                    previous_state=None,
                    new_state=state,
                    event_timestamp=now,
                    reason="all bullish conditions hold",
                    indicator_snapshot_id=snapshot_row_id,
                    price=snapshot.close,
                    detail={"episode_count": membership.episode_count, "created": True},
                )
            )
        else:
            membership = existing
            await self._transition(
                existing,
                state,
                event_type="REQUALIFIED" if requalified else "ADDED",
                reason="all bullish conditions hold",
                now=now,
                snapshot_id=snapshot_row_id,
                price=snapshot.close,
                episode_count=episode,
                is_active=True,
                removal_reason="",
                extra={"consecutive_weak_candles": 0, "latest_score": score},
            )
        ep = episode_id_for(
            trading_date=snapshot.trading_date,
            instrument_id=instrument.id,
            episode=episode,
        )
        signaled = False
        if self.alerts is not None:
            signal_type = (
                SignalType.WATCHLIST_REQUALIFIED if requalified else SignalType.WATCHLIST_ADDED
            )
            outcome = await self.alerts.raise_alert(
                signal_type=signal_type,
                instrument_id=instrument.id,
                session_id=session_id,
                event_timestamp=now,
                episode_id=ep,
                candle_start=snapshot.candle_start,
                price=snapshot.close,
                vwap=snapshot.vwap,
                ema_fast=snapshot.ema_fast,
                ema_slow=snapshot.ema_slow,
                relative_volume=snapshot.relative_volume,
                intraday_change_pct=snapshot.intraday_change_pct,
                score=score,
                reasons=reasons,
                symbol=instrument.symbol,
                company_name=instrument.company_name,
                exchange=instrument.exchange,
                previous_close=snapshot.previous_close,
            )
            signaled = outcome.created
            # The entry itself is the headline alert: BULLISH_ENTRY shares the
            # episode so a duplicate episode can never fire twice (§12.3).
            await self.alerts.raise_alert(
                signal_type=SignalType.BULLISH_ENTRY,
                instrument_id=instrument.id,
                session_id=session_id,
                event_timestamp=now,
                episode_id=ep,
                candle_start=snapshot.candle_start,
                price=snapshot.close,
                vwap=snapshot.vwap,
                ema_fast=snapshot.ema_fast,
                ema_slow=snapshot.ema_slow,
                relative_volume=snapshot.relative_volume,
                intraday_change_pct=snapshot.intraday_change_pct,
                score=score,
                reasons=reasons,
                symbol=instrument.symbol,
                company_name=instrument.company_name,
                exchange=instrument.exchange,
                previous_close=snapshot.previous_close,
            )
        return WatchlistOutcome(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            previous_state=existing.current_state if existing else None,
            state=state,
            changed=True,
            event_type="REQUALIFIED" if requalified else "ADDED",
            reason="qualified",
            episode_id=ep,
            signaled=signaled,
            membership=membership,
            details={"score": score, "reasons": list(reasons)},
        )

    async def _transition(
        self,
        membership: WatchlistMembership,
        new_state: WatchlistState,
        *,
        event_type: str,
        reason: str,
        now: datetime,
        snapshot_id: int | None,
        price: float | None,
        is_active: bool | None = None,
        removal_reason: str | None = None,
        episode_count: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        previous = membership.current_state
        await self.store.update_membership(
            membership.id,
            state=new_state,
            state_changed_at=now,
            is_active=is_tracked(new_state) if is_active is None else is_active,
            removal_reason=removal_reason,
            episode_count=episode_count,
            snapshot_id=snapshot_id,
            latest_price=price,
            **(extra or {}),
        )
        await self.store.add_watchlist_event(
            WatchlistEvent(
                watchlist_id=membership.id,
                event_type=event_type,
                previous_state=previous,
                new_state=new_state,
                event_timestamp=now,
                reason=reason,
                indicator_snapshot_id=snapshot_id,
                price=price,
                detail={"episode_count": episode_count or membership.episode_count},
            )
        )
        membership.current_state = new_state
        membership.latest_state_change_at = now

    async def _signal_weakening(
        self,
        membership: WatchlistMembership,
        snapshot: IndicatorSnapshot,
        candle: Candle,
        now: datetime,
        instrument: Instrument,
    ) -> None:
        if self.alerts is None:
            return
        await self.alerts.raise_alert(
            signal_type=SignalType.WATCHLIST_WEAKENING,
            instrument_id=membership.instrument_id,
            session_id=membership.trading_session_id,
            event_timestamp=now,
            episode_id=episode_id_for(
                trading_date=candle.trading_date,
                instrument_id=membership.instrument_id,
                episode=membership.episode_count,
            ),
            candle_start=candle.candle_start,
            price=snapshot.close,
            vwap=snapshot.vwap,
            ema_fast=snapshot.ema_fast,
            ema_slow=snapshot.ema_slow,
            relative_volume=snapshot.relative_volume,
            intraday_change_pct=snapshot.intraday_change_pct,
            score=membership.latest_score,
            reasons=snapshot.failed_reasons or ("CLOSE_BELOW_VWAP_OR_EMA_SPREAD",),
            # Without these the rendered notification reads "Watchlist Weakening —
            # instrument #42", which tells a trader nothing about *which* name is
            # deteriorating. The entry and removal alerts name the symbol; so must
            # every intermediate one.
            symbol=instrument.symbol,
            company_name=instrument.company_name,
            exchange=instrument.exchange,
            previous_close=instrument.previous_close,
        )

    async def _signal_removal(
        self,
        membership: WatchlistMembership,
        snapshot: IndicatorSnapshot,
        candle: Candle,
        now: datetime,
        instrument: Instrument,
    ) -> None:
        if self.alerts is None:
            return
        await self.alerts.raise_alert(
            signal_type=SignalType.WATCHLIST_REMOVED,
            instrument_id=membership.instrument_id,
            session_id=membership.trading_session_id,
            event_timestamp=now,
            episode_id=episode_id_for(
                trading_date=candle.trading_date,
                instrument_id=membership.instrument_id,
                episode=membership.episode_count,
            ),
            candle_start=candle.candle_start,
            price=snapshot.close,
            vwap=snapshot.vwap,
            ema_fast=snapshot.ema_fast,
            ema_slow=snapshot.ema_slow,
            relative_volume=snapshot.relative_volume,
            intraday_change_pct=snapshot.intraday_change_pct,
            score=membership.latest_score,
            reasons=("REMOVED_AFTER_CONFIRMED_WEAK_CANDLES",),
            symbol=instrument.symbol,
            company_name=instrument.company_name,
            exchange=instrument.exchange,
            previous_close=instrument.previous_close,
            message=(
                f"Removed after {self.params.removal_confirm_candles} consecutive valid candles "
                "below VWAP with EMA9 at or below EMA20."
            ),
        )


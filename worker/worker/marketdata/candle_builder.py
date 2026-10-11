"""One-minute candle engine (spec §8).

The SmartAPI V2 socket has **no candle mode**: it emits ticks whose
``volume_trade_for_the_day`` is *day-cumulative* (verified in
``docs/research/smartapi-ws-findings.md``). This module is therefore the only
place allowed to turn ticks into OHLCV, and it is written so that the risky
parts are explicit rather than implicit:

* bucket boundary comes from the **exchange** timestamp in Asia/Kolkata, never
  from the wall clock of the machine running the worker;
* candle volume is a **delta of the cumulative counter**, not a sum of
  cumulative values;
* a minute with no ticks produces **no candle** (no invented zero-volume bars);
* a candle is finalized only after its minute boundary plus a configurable
  grace period, so a late tick does not silently rewrite history;
* ticks that arrive after finalization are counted and reported as
  ``late_corrections`` rather than dropped quietly;
* reconnects and counter resets are surfaced as gaps so the session manager can
  backfill from validated REST history instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..models import Candle, DataQuality, Tick
from ..timeutils import candle_start, to_ist


@dataclass
class _InstrumentState:
    """Mutable per-instrument accumulator. Owned by one worker, single-threaded."""

    instrument_id: int
    bucket_start: datetime | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int = 0
    tick_count: int = 0
    last_cumulative_volume: int | None = None
    last_sequence: int | None = None
    last_tick_signature: tuple[int, int, int] | None = None
    seen_signatures: set[tuple[int, int, int]] = field(default_factory=set)
    partial_quality: DataQuality = DataQuality.OK
    trading_date: date | None = None
    last_source_timestamp: datetime | None = None


@dataclass(frozen=True)
class CandleGap:
    """A minute inside the session for which the engine has no candle."""

    instrument_id: int
    candle_start: datetime
    candle_end: datetime
    reason: str  # NO_TICKS | RECONNECT | COUNTER_RESET | OUT_OF_SESSION


@dataclass
class EngineStats:
    ticks_received: int = 0
    ticks_duplicate: int = 0
    ticks_rejected: int = 0
    ticks_out_of_order: int = 0
    late_corrections: int = 0
    candles_finalized: int = 0
    counter_resets: int = 0
    gap_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "ticks_received": self.ticks_received,
            "ticks_duplicate": self.ticks_duplicate,
            "ticks_rejected": self.ticks_rejected,
            "ticks_out_of_order": self.ticks_out_of_order,
            "late_corrections": self.late_corrections,
            "candles_finalized": self.candles_finalized,
            "counter_resets": self.counter_resets,
            "gap_count": self.gap_count,
        }


class CandleEngine:
    """Builds and finalizes 1-minute candles from ticks."""

    def __init__(
        self,
        *,
        grace_seconds: int = 3,
        session_open: datetime | None = None,
        session_close: datetime | None = None,
        max_seen_signatures: int = 8,
    ) -> None:
        if grace_seconds < 0 or grace_seconds > 30:
            raise ValueError("grace_seconds must be between 0 and 30")
        self.grace = timedelta(seconds=grace_seconds)
        self.session_open = to_ist(session_open) if session_open else None
        self.session_close = to_ist(session_close) if session_close else None
        self.max_seen_signatures = max_seen_signatures
        self._states: dict[int, _InstrumentState] = {}
        self._finalized: dict[int, dict[datetime, Candle]] = {}
        self._gaps: list[CandleGap] = []
        self.stats = EngineStats()

    # ── ingestion ───────────────────────────────────────────────────────────
    def on_tick(self, tick: Tick) -> None:
        """Fold one tick into its bucket. Raises nothing on bad data — it
        records why the tick was ignored, because the health panel must show it."""
        self.stats.ticks_received += 1

        if tick.last_traded_price is None or tick.last_traded_price <= 0:
            self.stats.ticks_rejected += 1
            return

        exchange_ts = to_ist(tick.exchange_timestamp)
        if self.session_open and exchange_ts < to_ist(self.session_open):
            # A pre-open print is not part of the regular session's candles.
            self.stats.ticks_rejected += 1
            return
        if self.session_close and exchange_ts >= to_ist(self.session_close):
            self.stats.ticks_rejected += 1
            return

        bucket = candle_start(exchange_ts).replace(microsecond=0)
        state = self._states.get(tick.instrument_id) or _InstrumentState(instrument_id=tick.instrument_id)

        signature = (int(exchange_ts.timestamp() * 1000), int(tick.last_traded_price * 100), int(tick.sequence_number or 0))
        if state.last_tick_signature == signature or signature in state.seen_signatures:
            self.stats.ticks_duplicate += 1
            self._states[tick.instrument_id] = state
            return

        # already-finalized minute?
        finalized = self._finalized.setdefault(tick.instrument_id, {})
        if bucket in finalized:
            self.stats.late_corrections += 1
            self._states[tick.instrument_id] = state
            return

        # session / day rollover
        if state.trading_date is not None and state.trading_date != bucket.date():
            state = _InstrumentState(instrument_id=tick.instrument_id)
            self._states[tick.instrument_id] = state
        if state.trading_date is None:
            state.trading_date = bucket.date()

        # new bucket → roll the previous one into the finalized-by-time buffer
        if state.bucket_start is not None and bucket > state.bucket_start:
            if bucket - state.bucket_start > timedelta(minutes=1):
                # minutes with no ticks: recorded as gaps, never invented candles
                for missed in self._missing_starts(state.bucket_start, bucket):
                    self.record_gap(tick.instrument_id, missed, reason="NO_TICKS")
            self._flush_state(state)
            state.bucket_start = None
            state.open = state.high = state.low = state.close = None
            state.volume = 0
            state.tick_count = 0
            state.partial_quality = DataQuality.OK

        if state.bucket_start is None:
            state.bucket_start = bucket
            state.open = tick.last_traded_price
            state.high = tick.last_traded_price
            state.low = tick.last_traded_price
        else:
            if exchange_ts < to_ist(state.last_source_timestamp or tick.exchange_timestamp):
                # out-of-order tick: price extremes still apply, sequence flags it
                self.stats.ticks_out_of_order += 1
                state.partial_quality = DataQuality.PARTIAL if state.tick_count else state.partial_quality
            state.high = max(state.high or tick.last_traded_price, tick.last_traded_price)
            state.low = min(state.low or tick.last_traded_price, tick.last_traded_price)
        state.close = tick.last_traded_price
        state.tick_count += 1
        state.last_source_timestamp = exchange_ts

        # volume: delta of the day-cumulative counter (never a sum of cumulatives)
        if tick.cumulative_volume is not None:
            if state.last_cumulative_volume is None:
                if tick.cumulative_volume == 0:
                    delta = 0
                elif state.tick_count == 1 and self._is_session_start(bucket):
                    # First print of the day: cumulative == traded-so-far.
                    delta = tick.cumulative_volume
                else:
                    # Joined mid-session: traded volume before connect is unknown.
                    delta = 0
                    state.partial_quality = DataQuality.PARTIAL
                    self.record_gap(
                        tick.instrument_id,
                        bucket,
                        reason="RECONNECT",
                    )
                state.volume += int(delta)
            elif tick.cumulative_volume >= state.last_cumulative_volume:
                delta = tick.cumulative_volume - state.last_cumulative_volume
                state.volume += int(delta)
            else:
                # The counter went backwards. Either the day's cumulative value
                # restarted (a new session) or the feed corrected itself
                # mid-session. In both cases increments resume normally from
                # here, but the minute can no longer be trusted blindly.
                self.stats.counter_resets += 1
                self.record_gap(tick.instrument_id, bucket, reason="COUNTER_RESET")
                if self._is_session_start(bucket):
                    # Day rollover: the new day counts from ~zero, so the value
                    # itself is the only available estimate of what has traded.
                    # Volume measured earlier in this bucket against yesterday's
                    # baseline is replaced, never added to it.
                    state.volume = int(max(0, tick.cumulative_volume))
                    state.partial_quality = DataQuality.PARTIAL
                else:
                    # Mid-session anomaly: keep the increments that were valid,
                    # add nothing, and flag the candle as suspect.
                    state.partial_quality = DataQuality.SUSPECT
            state.last_cumulative_volume = int(tick.cumulative_volume)
        elif tick.last_traded_quantity:
            # LTP-mode feeds carry no cumulative counter; per-trade qty is only a
            # floor for the minute, so the candle is explicitly PARTIAL.
            state.volume += int(tick.last_traded_quantity)
            state.partial_quality = DataQuality.PARTIAL

        state.last_sequence = tick.sequence_number
        state.last_tick_signature = signature
        state.seen_signatures.add(signature)
        if len(state.seen_signatures) > self.max_seen_signatures:
            state.seen_signatures.pop()

        self._states[tick.instrument_id] = state

    # ── finalization ────────────────────────────────────────────────────────
    def finalize_due(self, now: datetime) -> list[Candle]:
        """Finalize candles whose boundary + grace has passed.

        A candle is complete only when *time* has moved past its minute plus the
        grace period — never because "a later tick arrived" (spec §7.3).
        """
        reference = to_ist(now)
        ready: list[Candle] = []
        for instrument_id, state in list(self._states.items()):
            if state.bucket_start is None or state.tick_count == 0:
                continue
            boundary = state.bucket_start + timedelta(minutes=1) + self.grace
            if reference >= boundary:
                candle = self._finalize_state(instrument_id, state)
                if candle is not None:
                    ready.append(candle)
        return ready

    def finalize_all(self, now: datetime) -> list[Candle]:
        """End-of-session flush: finalize everything still forming."""
        ready: list[Candle] = []
        for instrument_id, state in list(self._states.items()):
            if state.bucket_start is None or state.tick_count == 0:
                continue
            candle = self._finalize_state(instrument_id, state)
            if candle is not None:
                ready.append(candle)
        return ready

    def forming_candle(self, instrument_id: int) -> Candle | None:
        """The candle currently being built, marked ``INCOMPLETE``.

        Used for live display only; the scanner must refuse to evaluate it.
        """
        state = self._states.get(instrument_id)
        if state is None or state.bucket_start is None or state.open is None:
            return None
        return Candle(
            instrument_id=instrument_id,
            trading_date=state.bucket_start.date(),
            candle_start=state.bucket_start,
            open=state.open,
            high=state.high or state.open,
            low=state.low or state.open,
            close=state.close or state.open,
            volume=state.volume,
            is_complete=False,
            data_quality=DataQuality.INCOMPLETE,
            source_timestamp=to_ist(state.last_source_timestamp) if state.last_source_timestamp else None,
            tick_count=state.tick_count,
        )

    def finalized_candles(self, instrument_id: int) -> list[Candle]:
        rows = self._finalized.get(instrument_id, {})
        return [rows[key] for key in sorted(rows)]

    def record_gap(self, instrument_id: int, start: datetime, *, reason: str) -> CandleGap:
        gap = CandleGap(
            instrument_id=instrument_id,
            candle_start=start,
            candle_end=start + timedelta(minutes=1),
            reason=reason,
        )
        self._gaps.append(gap)
        self.stats.gap_count += 1
        return gap

    @property
    def gaps(self) -> list[CandleGap]:
        return list(self._gaps)

    # ── internals ───────────────────────────────────────────────────────────
    def _finalize_state(self, instrument_id: int, state: _InstrumentState) -> Candle | None:
        candle = self._build_candle(state)
        if candle is None:
            self._states.pop(instrument_id, None)
            return None
        candle.is_complete = True
        candle.data_quality = state.partial_quality if state.partial_quality != DataQuality.OK else DataQuality.OK
        bucket = candle.candle_start
        self._finalized.setdefault(instrument_id, {})[bucket] = candle
        # reset the forming bucket, keep the cumulative counter (it is day-scoped)
        state.bucket_start = None
        state.open = state.high = state.low = state.close = None
        state.volume = 0
        state.tick_count = 0
        state.partial_quality = DataQuality.OK
        self.stats.candles_finalized += 1
        return candle

    def _flush_state(self, state: _InstrumentState) -> None:
        """Keep the finished bucket in the finalized map without grace semantics."""
        if state.bucket_start is None or state.open is None or state.tick_count == 0:
            return
        candle = self._build_candle(state, complete=True)
        if candle is not None:
            self._finalized.setdefault(state.instrument_id, {})[state.bucket_start] = candle
            self.stats.candles_finalized += 1

    def _build_candle(self, state: _InstrumentState, *, complete: bool = False) -> Candle | None:
        if state.bucket_start is None or state.open is None:
            return None
        if state.tick_count == 0:
            return None
        if state.high is None or state.low is None or state.close is None:
            return None
        quality = state.partial_quality
        if state.volume <= 0 and quality == DataQuality.OK:
            # ticks existed but no attributable volume
            quality = DataQuality.PARTIAL
        return Candle(
            instrument_id=state.instrument_id,
            trading_date=state.bucket_start.date(),
            candle_start=state.bucket_start,
            open=round(state.open, 4),
            high=round(max(state.high, state.open, state.close), 4),
            low=round(min(state.low, state.open, state.close), 4),
            close=round(state.close, 4),
            volume=int(max(0, state.volume)),
            is_complete=complete,
            data_quality=quality if complete else DataQuality.INCOMPLETE,
            source_timestamp=to_ist(state.last_source_timestamp) if state.last_source_timestamp else None,
            tick_count=state.tick_count,
        )

    def _is_session_start(self, bucket: datetime) -> bool:
        if self.session_open is None:
            return False
        return bucket.date() == self.session_open.date() and bucket < self.session_open + timedelta(minutes=2)

    @staticmethod
    def _missing_starts(previous_start: datetime, new_start: datetime) -> list[datetime]:
        """Bucket starts strictly between the last filled minute and the new one."""
        missing: list[datetime] = []
        cursor = previous_start + timedelta(minutes=1)
        while cursor < new_start:
            missing.append(cursor)
            cursor += timedelta(minutes=1)
        return missing

    def _gaps_list(self) -> list[CandleGap]:
        return self._gaps

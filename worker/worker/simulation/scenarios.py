"""Scripted synthetic price/volume paths for the offline replay (§8, §10, §11).

Each script is a *statement about behaviour*, not decoration: it exists to make
one documented rule fire so the run can prove the rule fired. That is why the
expected watchlist events are declared next to the shape of the data — a script
that fails to produce its stated intent is a finding, not a passing test.

The shapes are deliberately built on the rules the scanner actually applies:

* qualification needs close > session VWAP **and** EMA9 > EMA20 **and**
  relative volume >= the threshold, and relative volume only becomes defined on
  the 21st candle (20-candle baseline + the current one), so every script that
  should enter first spends ~24 rising candles warming up, then raises volume;
* a fade brings the close under session VWAP quickly, but EMA9 only crosses
  below EMA20 after several falling candles, and removal then needs
  ``removal_confirm_candles`` *consecutive* valid candles with both conditions
  failing — so the fade segments are long and steep on purpose;
* volume dominates VWAP: the spike segments carry 6-7x the baseline volume,
  which pulls the session VWAP toward them and is what makes the post-fade
  re-entry need a genuinely large move rather than a mild uptick.

Prices are rounded to 2 decimals like an NSE tick, and jitter is drawn from a
seeded :class:`random.Random`, so a rerun with the same seed replays exactly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import cycle out
    from worker.models import Instrument


#: per-minute volume jitter, kept wide enough to look like a real tape and too
#: narrow to move a 1.5x relative-volume decision.
VOLUME_JITTER = 0.08


@dataclass(frozen=True)
class Segment:
    """One stretch of minutes with a constant drift and baseline volume."""

    minutes: int
    step: float
    """Price change applied to the *open* of each minute (negative = falling)."""

    volume: int
    """Shares traded in each minute of the segment."""

    tail: float = 0.20
    """``close - open`` for these minutes (sign should match ``step``)."""


@dataclass(frozen=True)
class Script:
    """A symbol plus the path its ticks follow through the session."""

    symbol: str
    company_name: str
    previous_close: float
    start_price: float
    intent: str
    segments: tuple[Segment, ...] = ()
    expect: tuple[str, ...] = ()
    """Watchlist ``event_type`` values this script must produce, in order of
    appearance. Empty means the script must produce *no* membership at all."""

    @property
    def minutes(self) -> int:
        return sum(segment.minutes for segment in self.segments)

    def bars(self, rng: random.Random) -> list[tuple[float, float, int]]:
        """Per-minute ``(open, close, volume)`` rows for this script.

        The candle engine derives OHLC from the tick prices it is given, so two
        prints per minute (open and close) means ``high = max(open, close)`` and
        ``low = min(open, close)``. Intra-minute wicks are not simulated: they
        would move the VWAP typical price without testing any rule, and the
        simplification is stated rather than hidden.
        """
        rows: list[tuple[float, float, int]] = []
        price = self.start_price
        for segment in self.segments:
            for _ in range(segment.minutes):
                open_price = round(price, 2)
                close_price = round(price + segment.tail + rng.uniform(-0.03, 0.03), 2)
                if close_price <= 0:
                    # A script must never ask for a non-positive price: the tick
                    # model rejects it, and the replay is not here to test that.
                    close_price = 0.05
                volume = max(1, int(round(segment.volume * (1.0 + rng.uniform(-VOLUME_JITTER, VOLUME_JITTER)))))
                rows.append((open_price, close_price, volume))
                price = open_price + segment.step
        return rows


def _riser_then_more(minute_count: int, *, start: float, step: float, volume: int) -> Segment:
    return Segment(minutes=minute_count, step=step, volume=volume, tail=round(step / 2, 2))


SCRIPTS: tuple[Script, ...] = (
    Script(
        symbol="SIMBREAK",
        company_name="Simulated Breakout Ltd",
        previous_close=100.0,
        start_price=100.4,
        intent="rises on normal volume, then a 6x volume spike qualifies it",
        segments=(
            Segment(minutes=24, step=0.50, volume=1000, tail=0.20),
            Segment(minutes=20, step=0.45, volume=6000, tail=0.22),
        ),
        expect=("ADDED",),
    ),
    Script(
        symbol="SIMQUIET",
        company_name="Simulated Quiet Climber Ltd",
        previous_close=250.0,
        start_price=250.6,
        intent="rises steadily but never on enough volume — the relative-volume gate must refuse it",
        segments=(Segment(minutes=44, step=0.45, volume=1000, tail=0.18),),
        expect=(),
    ),
    Script(
        symbol="SIMFADE",
        company_name="Simulated Fade Away Ltd",
        previous_close=80.0,
        start_price=80.3,
        intent="qualifies on a volume spike, then a long steep fade removes it",
        segments=(
            Segment(minutes=22, step=0.45, volume=900, tail=0.18),
            Segment(minutes=6, step=0.50, volume=5200, tail=0.20),
            Segment(minutes=26, step=-1.60, volume=700, tail=-0.60),
        ),
        expect=("ADDED", "WEAKENING", "REMOVED"),
    ),
    Script(
        symbol="SIMREENTRY",
        company_name="Simulated Second Wind Ltd",
        previous_close=50.0,
        start_price=50.2,
        intent="enters, is removed after a sustained fade, then re-qualifies on a fresh surge",
        segments=(
            Segment(minutes=22, step=0.40, volume=900, tail=0.16),
            _riser_then_more(6, start=59.0, step=0.45, volume=5000),
            Segment(minutes=24, step=-1.50, volume=700, tail=-0.55),
            Segment(minutes=26, step=1.40, volume=7000, tail=0.55),
        ),
        expect=("ADDED", "WEAKENING", "REMOVED", "REQUALIFIED"),
    ),
    Script(
        symbol="SIMDOWN",
        company_name="Simulated No Bid Ltd",
        previous_close=300.0,
        start_price=299.4,
        intent="declines all session; nothing may ever be added for it",
        segments=(Segment(minutes=44, step=-0.60, volume=1100, tail=-0.25),),
        expect=(),
    ),
    Script(
        symbol="SIMSILENT",
        company_name="Simulated Silent Ltd",
        previous_close=120.0,
        start_price=120.0,
        intent="mapped but never prints: must show up as missing data, never as a candle",
        segments=(),
        expect=(),
    ),
)


@dataclass(frozen=True)
class Universe:
    """The instruments plus their per-minute tick plans."""

    instruments: tuple[Instrument, ...]
    plans: dict[int, list[tuple[float, float, int]]] = field(default_factory=dict)

    @property
    def longest_script(self) -> int:
        return max((len(rows) for rows in self.plans.values()), default=0)


def build_universe(*, seed: int, instrument_start_id: int = 1) -> Universe:
    """Materialise the scripts into instruments + tick plans (deterministic)."""
    from worker.models import Instrument

    rng = random.Random(seed)
    instruments: list[Instrument] = []
    plans: dict[int, list[tuple[float, float, int]]] = {}
    for offset, script in enumerate(SCRIPTS):
        instrument_id = instrument_start_id + offset
        instruments.append(
            Instrument(
                id=instrument_id,
                exchange="NSE",
                symbol=script.symbol,
                trading_symbol=script.symbol,
                company_name=script.company_name,
                # Synthetic but token-shaped: 7-digit NSE cash instrument tokens.
                instrument_token=str(9000000 + instrument_id),
                instrument_type="",
                previous_close=script.previous_close,
            )
        )
        plans[instrument_id] = script.bars(rng)
    return Universe(instruments=tuple(instruments), plans=plans)


def script_for(symbol: str) -> Script | None:
    return next((item for item in SCRIPTS if item.symbol == symbol), None)

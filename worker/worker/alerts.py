"""Alert engine: signal construction, idempotency, cooldowns, notification queue.

Spec §12 distilled into four rules this module enforces:

1. **One entry alert per qualifying episode.** A *episode* is a run of
   consecutive qualifying candles that ends when the loss-of-qualification logic
   fires (watchlist removal). The episode id is part of the idempotency key, so
   reprocessing the same candle after a restart cannot mint a second alert.
2. **The database has the final word.** Every signal carries a deterministic
   ``idempotency_key`` backed by a unique constraint; a duplicate insert is
   treated as success-with-no-delivery, never as an error to retry.
3. **Cooldowns are checked before queueing**, and a suppressed alert is still
   recorded (``SUPPRESSED`` delivery) so the dashboard can show why nothing
   arrived.
4. **Delivery is claimed, not assumed.** Nothing here marks a message as
   delivered; only the notifier, after the provider confirms, does that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .analytics.breakouts import BreakoutEvent
from .config import ScannerParams
from .logging_setup import get_logger
from .models import (
    Channel,
    DeliveryStatus,
    NotificationDelivery,
    Signal,
    SignalType,
)
from .persistence.store import ScannerStore, SignalInsertResult

log = get_logger(__name__)

# Signals that describe a stock's setup; the rest are operational messages.
CRITICAL_SIGNAL_TYPES = {
    SignalType.DATA_FEED_INTERRUPTED,
    SignalType.DATA_FEED_RECOVERED,
}

# Alerts worth a Telegram push. Watchlist bookkeeping states are visible in the
# dashboard; only entries and exits ping the phone.
PUSHED_SIGNAL_TYPES = {
    SignalType.BULLISH_ENTRY,
    SignalType.WATCHLIST_ADDED,
    SignalType.WATCHLIST_REMOVED,
    SignalType.WATCHLIST_REQUALIFIED,
    SignalType.DATA_FEED_INTERRUPTED,
    SignalType.DATA_FEED_RECOVERED,
    SignalType.DAILY_SCAN_COMPLETE,
    # RSI transitions (operator rule 2026-10-12, reversed 2026-10-15): below 40 in an
    # up-trend is the green side, an **entry**; above 60 in a down-trend is the red
    # one. Both are raised once per state *change*, never per candle — the nightly
    # two-timeframe pair by :meth:`WorkerRuntime._emit_rsi_transitions`, the
    # intraday five-minute reading by the ``rsi`` detector. The old ``RSI_BULLISH`` /
    # ``RSI_BEARISH`` rows stay in the database as history; nothing raises them any
    # more, and a page that called the oversold side "bullish" is the thing this
    # rename exists to stop.
    SignalType.RSI_ENTRY,
    SignalType.RSI_OVERBOUGHT,
    # Level breaks (operator feature 2026-10-14). This set says the *type* is
    # pushable; whether a given strategy pages the phone is decided per event from
    # ``ops_settings['alert_strategies']`` — see :meth:`AlertEngine.raise_breakout`.
    SignalType.BREAKOUT_UP,
    SignalType.BREAKDOWN,
}

COOLDOWN_APPLIED = {
    SignalType.BULLISH_ENTRY,
    SignalType.WATCHLIST_ADDED,
    SignalType.WATCHLIST_REQUALIFIED,
    # Each strategy brings its own window (``StrategyParams.cooldown_minutes``) and
    # its own discriminator (``strategy:<name>`` in ``reasons``), because three
    # strategies share these two signal types: an opening-range break must not be
    # muted by a VWAP loss raised in the same minute on the same stock.
    SignalType.BREAKOUT_UP,
    SignalType.BREAKDOWN,
    # The ``rsi`` detector is the fourth strategy and has its own window (30 minutes
    # by default: an RSI that hugs 40 in a slow drift is one piece of news). The
    # nightly pair raises the same two types with ``cooldown_minutes=0``, which asks
    # for no window at all — its once-per-session idempotency key is its dedupe.
    SignalType.RSI_ENTRY,
    SignalType.RSI_OVERBOUGHT,
}


def episode_id_for(*, trading_date: date, instrument_id: int, episode: int) -> str:
    """Deterministic episode identity: date + instrument + ordinal."""
    return f"{trading_date.isoformat()}-{instrument_id}-e{episode}"


def idempotency_key(
    *,
    signal_type: SignalType | str,
    instrument_id: int | None,
    episode_id: str | None = None,
    candle_start: datetime | None = None,
    session_id: int | None = None,
) -> str:
    """Stable key for one logical event (spec §12.3).

    Never purely a rounded timestamp: an episode id makes retries idempotent
    even when the same candle is evaluated twice after a restart.
    """
    kind = signal_type.value if isinstance(signal_type, SignalType) else str(signal_type)
    if instrument_id is None:
        # Session-wide events key on session + kind — and on the episode when one
        # exists. A second feed interruption later the same day is a different
        # logical event (§12.4: one alert per outage episode); without the episode
        # its key collides with the first outage and the alert is dropped as a
        # duplicate, so the operator never learns the feed died again.
        if episode_id:
            return f"{kind}:session-{session_id}:{episode_id}"
        return f"{kind}:session-{session_id}"
    if episode_id:
        return f"{kind}:{instrument_id}:{episode_id}"
    if candle_start is not None:
        return f"{kind}:{instrument_id}:{candle_start.strftime('%Y%m%d%H%M')}"
    return f"{kind}:{instrument_id}:{session_id}"


def _subject(signal: Signal) -> str:
    """Who this alert is about.

    Session-wide alerts (daily scan, feed interruption) carry no instrument at
    all, and rendering them as "instrument #None" reads like a bug to the user
    even though the row itself is correct.
    """
    if signal.symbol:
        return signal.symbol
    if signal.instrument_id is None:
        return "session-wide"
    return f"instrument #{signal.instrument_id}"


#: Titles the automatic casing cannot produce, because the acronym *is* the subject:
#: "Rsi Entry" is what ``title()`` makes of a type an operator reads on a phone.
_TYPE_LABELS = {
    SignalType.RSI_ENTRY: "RSI entry",
    SignalType.RSI_OVERBOUGHT: "RSI overbought",
}


def format_message(signal: Signal, *, deep_link: str | None = None) -> str:
    """Human-readable alert text shared by Telegram and the dashboard."""
    label = _TYPE_LABELS.get(signal.signal_type) or signal.signal_type.value.replace("_", " ").title()
    stamp = signal.event_timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"{_emoji(signal.signal_type)} {label} — {_subject(signal)}"]
    if signal.company_name and signal.company_name != signal.symbol:
        lines.append(signal.company_name)
    if signal.price is not None:
        change = (
            f" ({signal.intraday_change_pct:+.2f}% vs prev close)"
            if signal.intraday_change_pct is not None
            else ""
        )
        lines.append(f"Price: ₹{signal.price:,.2f}{change}")
    if signal.vwap is not None and signal.ema_fast is not None and signal.ema_slow is not None:
        lines.append(
            f"VWAP ₹{signal.vwap:,.2f} · EMA9 ₹{signal.ema_fast:,.2f} · EMA20 ₹{signal.ema_slow:,.2f}"
        )
    if signal.relative_volume is not None:
        lines.append(f"Relative volume: {signal.relative_volume:.2f}×")
    if signal.score is not None:
        lines.append(f"Score: {signal.score:.1f}")
    if signal.reasons:
        lines.append("Why: " + ", ".join(_pretty(reason) for reason in signal.reasons))
    if signal.message:
        lines.append(signal.message)
    lines.append(stamp)
    if signal.is_simulated:
        lines.append("SIMULATED — not a live market signal")
    if deep_link:
        lines.append(deep_link)
    return "\n".join(lines)


def _emoji(kind: SignalType) -> str:
    return {
        SignalType.BULLISH_ENTRY: "🟢",
        SignalType.WATCHLIST_ADDED: "➕",
        SignalType.WATCHLIST_WEAKENING: "🟡",
        SignalType.WATCHLIST_REMOVED: "🔻",
        SignalType.WATCHLIST_REQUALIFIED: "♻️",
        SignalType.DATA_FEED_INTERRUPTED: "⚠️",
        SignalType.DATA_FEED_RECOVERED: "✅",
        SignalType.DAILY_SCAN_COMPLETE: "📊",
        # Green is the actionable side and red is the warning, on both horizons:
        # the entry is what the operator acts on, the overbought reading is the one
        # to step back from. 📈/📉 stay mapped for the retired pair so an old row
        # re-rendered from the database does not turn into an information icon.
        SignalType.RSI_ENTRY: "🟢",
        SignalType.RSI_OVERBOUGHT: "🔴",
        SignalType.RSI_BULLISH: "📈",
        SignalType.RSI_BEARISH: "📉",
        SignalType.BREAKOUT_UP: "🔼",
        SignalType.BREAKDOWN: "🔽",
    }.get(kind, "ℹ️")


def _pretty(code: str) -> str:
    return code.replace("_", " ").lower()


@dataclass
class AlertOutcome:
    """What happened when the engine tried to raise one alert."""

    signal: Signal
    created: bool
    duplicate: bool = False
    suppressed_reason: str | None = None
    delivery_channels: tuple[Channel, ...] = ()
    delivery_ids: tuple[int, ...] = ()

    @property
    def notified(self) -> bool:
        return bool(self.delivery_channels) and self.suppressed_reason is None


class AlertEngine:
    """Turns a decision into a durable signal plus queued deliveries."""

    def __init__(
        self,
        *,
        store: ScannerStore,
        params: ScannerParams,
        telegram_enabled: bool = False,
        telegram_chat_id: str = "",
        app_url: str = "",
        simulation: bool = False,
    ) -> None:
        self.store = store
        self.params = params
        self.telegram_enabled = telegram_enabled
        self.telegram_chat_id = telegram_chat_id
        self.app_url = app_url
        self.simulation = simulation

    async def raise_alert(
        self,
        *,
        signal_type: SignalType,
        instrument_id: int | None,
        session_id: int | None,
        event_timestamp: datetime,
        episode_id: str | None = None,
        candle_start: datetime | None = None,
        price: float | None = None,
        vwap: float | None = None,
        ema_fast: float | None = None,
        ema_slow: float | None = None,
        relative_volume: float | None = None,
        intraday_change_pct: float | None = None,
        score: float | None = None,
        reasons: tuple[str, ...] = (),
        message: str | None = None,
        symbol: str | None = None,
        company_name: str | None = None,
        exchange: str = "NSE",
        previous_close: float | None = None,
        config_version: int | None = None,
        cooldown_minutes: int | None = None,
        telegram: bool = True,
    ) -> AlertOutcome:
        """Persist one alert and queue its deliveries.

        ``cooldown_minutes`` overrides :attr:`ScannerParams.alert_cooldown_minutes`
        for a strategy that carries its own window; ``0`` asks for no window at all,
        which is what the nightly RSI pair passes because its idempotency key already
        makes it once-per-session. ``telegram=False`` records the alert — the board
        still shows it — without paging the phone. Both default to the global
        behaviour, so every existing caller is unchanged.
        """
        signal = Signal(
            instrument_id=instrument_id,
            trading_session_id=session_id,
            signal_type=signal_type,
            qualifying_episode_id=episode_id or "",
            idempotency_key=idempotency_key(
                signal_type=signal_type,
                instrument_id=instrument_id,
                episode_id=episode_id,
                candle_start=candle_start,
                session_id=session_id,
            ),
            event_timestamp=event_timestamp,
            candle_start=candle_start,
            price=price,
            vwap=vwap,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            relative_volume=relative_volume,
            intraday_change_pct=intraday_change_pct,
            score=score,
            reasons=tuple(reasons),
            message=message,
            config_version=config_version if config_version is not None else self.params.config_version,
            is_simulated=self.simulation,
            symbol=symbol,
            company_name=company_name,
            exchange=exchange,
            previous_close=previous_close,
        )

        # Cooldown is judged against the alerts that *already* exist, so it has to
        # be read before this signal is written: once the row is in the table the
        # query can return this signal itself, the gap looks like 0 seconds, and
        # every alert is suppressed as a cooldown duplicate.
        #
        # ``cooldown_minutes`` lets a caller use its own window — the breakout
        # strategies each carry one — while the ``strategy:<name>`` reason inside
        # ``reasons`` narrows the comparison to the same strategy. Both default to
        # the global behaviour, so every existing alert path is unchanged.
        window = self.params.alert_cooldown_minutes if cooldown_minutes is None else cooldown_minutes
        cooled = signal.signal_type in COOLDOWN_APPLIED and await self._in_cooldown(
            signal, event_timestamp, minutes=window
        )

        inserted: SignalInsertResult = await self.store.insert_signal(signal)
        if not inserted.created:
            log.info("signal_duplicate", key=signal.idempotency_key, existing_id=inserted.signal_id)
            return AlertOutcome(
                signal=signal,
                created=False,
                duplicate=True,
                suppressed_reason="duplicate",
            )
        # The repository assigned the primary key (or returned the existing one);
        # deliveries must point at that id, not at a locally generated guess.
        signal.id = inserted.signal_id
        stored = signal
        outcome = AlertOutcome(signal=stored, created=True)

        if self.simulation:
            # Simulated signals never touch a provider: they are recorded as
            # SIMULATED deliveries so the dashboard can show them honestly.
            outcome.suppressed_reason = "simulation mode"
            outcome.delivery_ids = await self._queue(stored, Channel.DASHBOARD, DeliveryStatus.SIMULATED)
            outcome.delivery_channels = (Channel.DASHBOARD,)
            return outcome

        if cooled:
            outcome.suppressed_reason = f"cooldown {window}m"
            outcome.delivery_ids = await self._queue(stored, Channel.DASHBOARD, DeliveryStatus.SUPPRESSED)
            outcome.delivery_channels = (Channel.DASHBOARD,)
            log.info("signal_cooldown_suppressed", symbol=stored.symbol, type=stored.signal_type.value)
            return outcome

        channels: list[Channel] = [Channel.DASHBOARD]
        ids: list[int] = []
        ids.extend(await self._queue(stored, Channel.DASHBOARD, DeliveryStatus.DELIVERED))
        # ``telegram`` is the per-strategy switch the operator sets on /alerts: a
        # row is still written and still shows on the board, it just never pages
        # the phone. The global enable has to agree, too.
        if self.telegram_enabled and telegram and signal_type in PUSHED_SIGNAL_TYPES:
            channels.append(Channel.TELEGRAM)
            ids.extend(await self._queue(stored, Channel.TELEGRAM, DeliveryStatus.QUEUED))
        outcome.delivery_channels = tuple(channels)
        outcome.delivery_ids = tuple(ids)
        return outcome

    async def feed_interrupted(
        self, *, session_id: int, at: datetime, reason: str, outage_episode: str
    ) -> AlertOutcome:
        return await self.raise_alert(
            signal_type=SignalType.DATA_FEED_INTERRUPTED,
            instrument_id=None,
            session_id=session_id,
            event_timestamp=at,
            episode_id=outage_episode,
            message=f"Market-data feed interrupted: {reason}",
        )

    async def feed_recovered(
        self, *, session_id: int, at: datetime, outage_episode: str, quiet_seconds: float
    ) -> AlertOutcome:
        return await self.raise_alert(
            signal_type=SignalType.DATA_FEED_RECOVERED,
            instrument_id=None,
            session_id=session_id,
            event_timestamp=at,
            episode_id=outage_episode,
            message=f"Feed restored after {quiet_seconds:.0f}s of silence.",
        )

    async def raise_breakout(
        self,
        event: BreakoutEvent,
        *,
        session_id: int,
        company_name: str | None = None,
        previous_close: float | None = None,
    ) -> AlertOutcome:
        """Turn one detector event into an alert.

        The three level-break strategies share two signal types — the direction is
        what the alert *is*, the strategy is metadata — so the dedupe that keeps a
        grinding tape from paging once per new high is the event's own level-keyed
        :attr:`~worker.analytics.breakouts.BreakoutEvent.episode_id`, and the
        strategy name inside ``reasons`` is what keeps each strategy's cooldown
        separate. Both are decided in the detector, not here; this method only
        carries them into the database unchanged.

        The fourth strategy is the exception, and deliberately so. A day high or a
        VWAP loss is a *move*, so its type names the direction. The RSI rule is not a
        move, it is a *reading against a level*, and its useful direction is the one
        the tape made through 40 or 60: oversold in an up-trend is the break *down*
        through the level and the side worth acting on. Reusing ``BREAKDOWN`` for that
        would put a green, actionable entry on the board under a word that reads like
        a warning — which is the exact confusion the 2026-10-15 reversal was asked to
        end — so it gets its own two types instead.
        """
        change = (
            None
            if not previous_close
            else (event.price / previous_close - 1.0) * 100.0
        )
        if event.strategy == "rsi":
            signal_type = SignalType.RSI_OVERBOUGHT if event.is_up else SignalType.RSI_ENTRY
        else:
            signal_type = SignalType.BREAKOUT_UP if event.is_up else SignalType.BREAKDOWN
        return await self.raise_alert(
            signal_type=signal_type,
            instrument_id=event.instrument_id,
            session_id=session_id,
            event_timestamp=event.candle_start,
            episode_id=event.episode_id,
            candle_start=event.candle_start,
            price=event.price,
            # The level is only the session VWAP for that one strategy; storing it
            # in the vwap column of a day-break row would make the chart lie.
            vwap=event.level if event.strategy == "vwap" else None,
            relative_volume=event.volume_ratio,
            intraday_change_pct=change,
            reasons=event.reasons,
            message=event.describe(),
            symbol=event.symbol,
            company_name=company_name,
            previous_close=previous_close,
            cooldown_minutes=event.cooldown_minutes,
            telegram=event.telegram,
        )

    # ── internals ─────────────────────────────────────────────────────────

    async def _in_cooldown(self, signal: Signal, now: datetime, *, minutes: int) -> bool:
        """Has this (instrument, type) already produced an alert inside the window?

        Deliberately keyed on delivered alerts rather than on signal rows: a
        qualifying instrument emits a signal every minute, so counting suppressed
        rows would push the window forward forever and the second alert of the day
        would never happen.

        For level breaks the window is the strategy's own, and the match is narrowed
        to the same strategy: ``BREAKOUT_UP`` from the opening range and ``BREAKOUT_UP``
        from a VWAP reclaim are different pieces of news even when the stock, the
        minute and the signal type match. The discriminator lives in ``reasons``
        because that is the only column the row has for it.
        """
        if minutes <= 0 or signal.instrument_id is None:
            return False
        reason = next((code for code in signal.reasons if code.startswith("strategy:")), None)
        since = now - timedelta(minutes=minutes)
        last = await self.store.last_pushed_signal_at(
            signal.instrument_id,
            signal.signal_type.value,
            since=since,
            reason=reason,
        )
        if last is None:
            return False
        return (now - last).total_seconds() < minutes * 60.0

    async def _queue(
        self, signal: Signal, channel: Channel, status: DeliveryStatus
    ) -> list[int]:
        if signal.id is None:
            log.error("signal_without_id", key=signal.idempotency_key)
            return []
        if channel is Channel.TELEGRAM and not self.telegram_chat_id:
            log.warning("telegram_destination_missing", signal_id=signal.id)
            return []
        payload: dict[str, Any] = {
            "text": format_message(signal, deep_link=self.deep_link(signal)),
            "signal_type": signal.signal_type.value,
            "symbol": signal.symbol,
            "is_simulated": signal.is_simulated,
        }
        destination = (
            self.telegram_chat_id if channel is Channel.TELEGRAM else signal.symbol or "dashboard"
        )
        delivery = NotificationDelivery(
            id=None,
            signal_id=signal.id,
            channel=channel,
            destination_reference=destination,
            delivery_status=status,
            next_retry_at=signal.event_timestamp if status is DeliveryStatus.QUEUED else None,
            payload=payload,
        )
        return [await self.store.enqueue_notification(delivery)]

    def deep_link(self, signal: Signal) -> str | None:
        if not self.app_url or not signal.symbol:
            return None
        return f"{self.app_url.rstrip('/')}/instrument/{signal.symbol.lower()}"

"""Durable notification dispatcher: drains the queue, records the truth.

Alerts are *queued* by :mod:`worker.alerts` inside the same logical flow that
created the signal, then delivered later by this loop (:class:`Dispatcher`).
That separation is the point: a Telegram outage, a restart, or a rate limit
cannot lose an alert, and cannot duplicate one either, because every state
change is written back to ``notification_deliveries``.

Guarantees this module is responsible for (spec §12.5, §13):

* ``DELIVERED`` is written only when :class:`TelegramResult` carries a
  ``message_id`` returned by Telegram;
* retryable failures go back to ``FAILED`` with a computed ``next_retry_at``
  and are re-claimed later; permanent failures (bad chat id, blocked bot) stop
  immediately instead of burning the rate limiter;
* after ``max_attempts`` dispatches the row becomes ``PERMANENTLY_FAILED`` and
  an operator-visible log line is emitted — a silently dropped alert is the
  failure mode this whole layer exists to prevent.

Retry layering is deliberate: the sender retries *inside* one dispatch (429
``retry_after``, transport errors) while this loop retries *across* minutes.
Worst-case provider calls are ``max_attempts ** 2`` spread over an exponential
schedule, which Telegram's per-minute limits absorb comfortably.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import TelegramSettings
from .logging_setup import get_logger, scrub
from .models import Channel, DeliveryStatus
from .persistence.store import NotificationClaim, ScannerStore
from .telegram import TelegramSender

log = get_logger(__name__)

BACKOFF_CAP_SECONDS = 900.0


@dataclass
class DispatchReport:
    """What one ``run_once`` pass did, for logs and for tests."""

    claimed: int = 0
    delivered: int = 0
    retrying: int = 0
    permanently_failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "delivered": self.delivered,
            "retrying": self.retrying,
            "permanently_failed": self.permanently_failed,
            "skipped": self.skipped,
            "errors": self.errors[:5],
        }


def backoff_seconds(*, attempt: int, base_seconds: int, cap: float = BACKOFF_CAP_SECONDS) -> float:
    """Exponential retry spacing for the *durable* queue (attempt is 1-based)."""
    if base_seconds < 1:
        base_seconds = 1
    return min(base_seconds * (2 ** (attempt - 1)), cap)


class Dispatcher:
    """Single-worker delivery loop for one channel (Telegram)."""

    def __init__(
        self,
        *,
        store: ScannerStore,
        sender: TelegramSender,
        settings: TelegramSettings,
        worker_id: str = "dispatcher",
        batch_size: int = 10,
    ) -> None:
        self.store = store
        self.sender = sender
        self.settings = settings
        self.worker_id = worker_id
        self.batch_size = batch_size

    async def run_once(self, *, now: datetime) -> DispatchReport:
        report = DispatchReport()
        claims = await self.store.claim_due_notifications(
            channel=Channel.TELEGRAM, now=now, limit=self.batch_size
        )
        report.claimed = len(claims)
        for claim in claims:
            await self._deliver(claim, now=now, report=report)
        return report

    async def _deliver(
        self, claim: NotificationClaim, *, now: datetime, report: DispatchReport
    ) -> None:
        delivery = claim.delivery
        attempt = delivery.attempt_count + 1
        text = str(delivery.payload.get("text") or "").strip()
        if not text:
            # A queued row without body text can never be delivered; recording
            # it as permanent is honest and keeps the queue drainable.
            await self.store.update_delivery(
                delivery.id,
                status=DeliveryStatus.PERMANENTLY_FAILED,
                attempt_count=attempt,
                last_attempt_at=now,
                last_error="queued notification has no message text",
            )
            report.permanently_failed += 1
            report.errors.append(f"delivery {delivery.id}: empty payload")
            return

        result = await self.sender.send(text, chat_id=delivery.destination_reference)
        if result.delivered:
            await self.store.update_delivery(
                delivery.id,
                status=DeliveryStatus.DELIVERED,
                attempt_count=attempt,
                last_attempt_at=now,
                delivered_at=now,
                provider_message_id=str(result.message_id),
                last_error=None,
            )
            report.delivered += 1
            log.info(
                "notification_delivered",
                delivery_id=delivery.id,
                signal_id=delivery.signal_id,
                telegram_message_id=result.message_id,
                attempts=attempt,
            )
            return

        exhausted = attempt >= self.settings.max_attempts
        status = (
            DeliveryStatus.PERMANENTLY_FAILED
            if result.permanent or exhausted
            else DeliveryStatus.FAILED
        )
        next_retry_at = (
            None
            if status is DeliveryStatus.PERMANENTLY_FAILED
            else now
            + timedelta(
                seconds=backoff_seconds(
                    attempt=attempt, base_seconds=self.settings.retry_base_seconds
                )
            )
        )
        await self.store.update_delivery(
            delivery.id,
            status=status,
            attempt_count=attempt,
            last_attempt_at=now,
            next_retry_at=next_retry_at,
            last_error=scrub(result.error)[:300] if result.error else "telegram send failed",
        )
        report.errors.append(f"delivery {delivery.id}: {result.error}")
        if status is DeliveryStatus.PERMANENTLY_FAILED:
            report.permanently_failed += 1
            log.error(
                "notification_permanently_failed",
                delivery_id=delivery.id,
                signal_id=delivery.signal_id,
                attempts=attempt,
                permanent=result.permanent,
                error=scrub(result.error),
            )
        else:
            report.retrying += 1
            log.warning(
                "notification_retry_scheduled",
                delivery_id=delivery.id,
                signal_id=delivery.signal_id,
                attempts=attempt,
                retry_in_seconds=None
                if next_retry_at is None
                else int((next_retry_at - now).total_seconds()),
                error=scrub(result.error),
            )

    async def run_forever(
        self, *, stop: asyncio.Event, poll_seconds: float = 5.0, now: Any = None
    ) -> DispatchReport:
        """Drain the queue until ``stop`` is set. Never raises on delivery errors."""
        totals = DispatchReport()
        clock = now or (lambda: datetime.now(tz=self._tz()))
        while not stop.is_set():
            try:
                report = await self.run_once(now=clock())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the queue must survive outages
                log.error("dispatch_pass_failed", error=scrub(f"{type(exc).__name__}: {exc}"))
                report = DispatchReport()
            for key in ("claimed", "delivered", "retrying", "permanently_failed", "skipped"):
                setattr(totals, key, getattr(totals, key) + getattr(report, key))
            totals.errors.extend(report.errors)
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass
        await self.sender.close()
        return totals

    @staticmethod
    def _tz() -> Any:
        from .timeutils import ist_zoneinfo

        return ist_zoneinfo()

"""Durable notification dispatcher tests (spec §12.5, §13).

Every scenario starts from a *real* queued Telegram row: an ``AlertEngine`` on
an ``InMemoryStore`` raises a ``BULLISH_ENTRY`` alert (which enqueues the
``QUEUED`` delivery), then ``Dispatcher.run_once`` drains it with a stub sender
returning canned ``TelegramResult``s. This pins the queue contract: delivery
is recorded only on provider proof, retryable rows come back at exactly the
backoff time, permanent rows stop dead, and a claim is released so the queue
drains without ever double-sending.

NOTE on the cooldown: every queue helper below uses the production default
``alert_cooldown_minutes=15``. ``AlertEngine`` reads the cooldown window
*before* inserting the signal and matches only delivered pushes, so the first
``BULLISH_ENTRY`` of an episode queues a ``QUEUED`` Telegram row normally — the
dispatcher tests exercise that real path rather than a workaround.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import minute
from worker.alerts import AlertEngine
from worker.config import ScannerParams, TelegramSettings
from worker.dispatcher import BACKOFF_CAP_SECONDS, Dispatcher, backoff_seconds
from worker.models import Channel, DeliveryStatus, SignalType
from worker.persistence.memory import InMemoryStore
from worker.telegram import TelegramResult

CHAT = "chat-1"
NOW = minute(6)


class StubSender:
    """TelegramSender stand-in: replays canned results, records calls.

    Result i is used for call i (the last result repeats), so a caller can
    script "fail once, then succeed". ``probe`` runs *during* the send, which
    is how the tests observe the IN_FLIGHT claim.
    """

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.probe = None

    async def send(self, text, *, chat_id=None):
        self.calls.append({"text": text, "chat_id": chat_id})
        if self.probe is not None:
            self.probe()
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]

    async def close(self):
        pass


def delivered_result(message_id=7):
    return TelegramResult(ok=True, message_id=message_id, http_status=200, attempts=1)


def server_error_result():
    return TelegramResult(ok=False, error="telegram http 500", http_status=500, attempts=1)


def permanent_result():
    return TelegramResult(
        ok=False,
        error="telegram rejected the message (400)",
        permanent=True,
        http_status=400,
        attempts=1,
    )


async def queue_entry_alert(**engine_overrides):
    """Drive the real AlertEngine to produce a queued Telegram delivery row."""
    store = InMemoryStore()
    engine = AlertEngine(
        store=store,
        params=ScannerParams(),
        telegram_enabled=True,
        telegram_chat_id=CHAT,
        **engine_overrides,
    )
    outcome = await engine.raise_alert(
        signal_type=SignalType.BULLISH_ENTRY,
        instrument_id=1,
        session_id=1,
        event_timestamp=minute(5),
        price=105.5,
        vwap=104.0,
        ema_fast=103.5,
        ema_slow=102.0,
        relative_volume=2.1,
        symbol="TATAMOTORS",
        company_name="Tata Motors Ltd",
    )
    telegram_ids = [d for d in outcome.delivery_ids if store.deliveries[d].channel is Channel.TELEGRAM]
    assert len(telegram_ids) == 1, "a pushed signal type must queue exactly one Telegram row"
    return store, outcome, telegram_ids[0]


def make_dispatcher(store, sender, **settings_overrides):
    base = {"bot_token": "bot-token", "chat_id": CHAT, "max_attempts": 3, "retry_base_seconds": 10}
    base.update(settings_overrides)
    return Dispatcher(store=store, sender=sender, settings=TelegramSettings(**base))


# ── delivery truth (§12.5) ───────────────────────────────────────────────────


async def test_confirmed_send_marks_row_delivered_with_provider_message_id():
    store, _, delivery_id = await queue_entry_alert()
    sender = StubSender(delivered_result(message_id=7))
    report = await make_dispatcher(store, sender).run_once(now=NOW)
    assert report.claimed == 1
    assert report.delivered == 1, "one confirmed send must be reported as one delivery"
    row = store.deliveries[delivery_id]
    assert row.delivery_status is DeliveryStatus.DELIVERED
    assert row.provider_message_id == "7"
    assert row.delivered_at == NOW
    assert row.attempt_count == 1
    assert row.last_error is None
    assert sender.calls[0]["chat_id"] == CHAT, "the queued destination is the row's, not the settings'"
    assert "TATAMOTORS" in sender.calls[0]["text"]


async def test_delivered_row_is_never_reclaimed_by_a_later_pass():
    store, _, delivery_id = await queue_entry_alert()
    sender = StubSender(delivered_result())
    dispatcher = make_dispatcher(store, sender)
    await dispatcher.run_once(now=NOW)
    second = await dispatcher.run_once(now=NOW + timedelta(minutes=5))
    assert second.claimed == 0, "a DELIVERED row must not be resent — no duplicate alerts"
    assert len(sender.calls) == 1


async def test_permanent_rejection_marks_row_permanently_failed_without_retry():
    store, _, delivery_id = await queue_entry_alert()
    sender = StubSender(permanent_result())
    report = await make_dispatcher(store, sender).run_once(now=NOW)
    assert report.permanently_failed == 1
    assert report.delivered == 0
    row = store.deliveries[delivery_id]
    assert row.delivery_status is DeliveryStatus.PERMANENTLY_FAILED
    assert row.next_retry_at is None, "a bad chat id must not schedule another attempt"
    assert row.attempt_count == 1
    assert len(sender.calls) == 1


# ── retry scheduling and queue draining (§13) ───────────────────────────────


async def test_retryable_failure_backs_off_then_drains_without_double_send():
    store, _, delivery_id = await queue_entry_alert()
    sender = StubSender(server_error_result(), delivered_result(message_id=77))
    dispatcher = make_dispatcher(store, sender)

    first = await dispatcher.run_once(now=NOW)
    assert first.retrying == 1
    assert first.delivered == 0
    row = store.deliveries[delivery_id]
    assert row.delivery_status is DeliveryStatus.FAILED
    assert row.attempt_count == 1
    assert row.next_retry_at == NOW + timedelta(
        seconds=backoff_seconds(attempt=1, base_seconds=10)
    ), "next_retry_at must land exactly on the exponential backoff step"
    assert row.next_retry_at == NOW + timedelta(seconds=10)

    # The row is NOT deliverable before its due time: a same-minute pass claims
    # nothing, so the failing provider is not hammered.
    same_time = await dispatcher.run_once(now=NOW)
    assert same_time.claimed == 0, "a FAILED row must not be reclaimed before next_retry_at"
    assert len(sender.calls) == 1, "no provider call may happen before the row is due"

    # ...and it IS deliverable later: the queue drains and reaches DELIVERED.
    later = NOW + timedelta(seconds=40)
    second = await dispatcher.run_once(now=later)
    assert second.claimed == 1
    assert second.delivered == 1
    row = store.deliveries[delivery_id]
    assert row.delivery_status is DeliveryStatus.DELIVERED
    assert row.attempt_count == 2, "the durable attempt counter accumulates across passes"
    assert row.provider_message_id == "77"
    assert row.delivered_at == later
    assert len(sender.calls) == 2, "exactly one provider call per dispatch attempt"


async def test_attempts_exhausted_across_passes_becomes_permanently_failed():
    store, _, delivery_id = await queue_entry_alert()
    sender = StubSender(server_error_result())
    dispatcher = make_dispatcher(store, sender, max_attempts=2, retry_base_seconds=10)
    await dispatcher.run_once(now=NOW)  # attempt 1 -> FAILED, due at NOW+10s
    report = await dispatcher.run_once(now=NOW + timedelta(seconds=30))  # attempt 2 == max
    assert report.permanently_failed == 1
    assert report.retrying == 0
    row = store.deliveries[delivery_id]
    assert row.delivery_status is DeliveryStatus.PERMANENTLY_FAILED
    assert row.attempt_count == 2
    assert row.next_retry_at is None, "an exhausted row must never be re-queued"
    # and it stays dead: a third pass claims nothing
    third = await dispatcher.run_once(now=NOW + timedelta(minutes=5))
    assert third.claimed == 0


async def test_single_attempt_settings_goes_straight_to_permanently_failed():
    store, _, delivery_id = await queue_entry_alert()
    report = await make_dispatcher(store, StubSender(server_error_result()), max_attempts=1).run_once(now=NOW)
    assert report.permanently_failed == 1, "attempt 1 of 1 failing is terminal, per exhaustion rule"
    assert store.deliveries[delivery_id].delivery_status is DeliveryStatus.PERMANENTLY_FAILED


# ── payload and channel guards ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="no-text-key"),
        pytest.param({"text": ""}, id="empty-text"),
        pytest.param({"text": "   "}, id="whitespace-text"),
    ],
)
async def test_queued_row_without_text_fails_permanently_without_touching_the_sender(payload):
    store, _, delivery_id = await queue_entry_alert()
    store.deliveries[delivery_id].payload = dict(payload)
    sender = StubSender(delivered_result())
    report = await make_dispatcher(store, sender).run_once(now=NOW)
    assert report.permanently_failed == 1
    assert sender.calls == [], "there is nothing to send — the provider must not be called"
    row = store.deliveries[delivery_id]
    assert row.delivery_status is DeliveryStatus.PERMANENTLY_FAILED
    assert row.last_error == "queued notification has no message text"
    assert report.errors, "the operator must see why the row died"


async def test_dashboard_only_rows_are_never_claimed_by_the_telegram_dispatcher():
    # WATCHLIST_WEAKENING is bookkeeping, not a pushed type: the engine queues
    # a dashboard row only, and the Telegram dispatcher must ignore it.
    store = InMemoryStore()
    engine = AlertEngine(store=store, params=ScannerParams(), telegram_enabled=True, telegram_chat_id=CHAT)
    outcome = await engine.raise_alert(
        signal_type=SignalType.WATCHLIST_WEAKENING,
        instrument_id=1,
        session_id=1,
        event_timestamp=minute(5),
        symbol="TATAMOTORS",
    )
    for did in outcome.delivery_ids:
        assert store.deliveries[did].channel is Channel.DASHBOARD
    sender = StubSender(delivered_result())
    report = await make_dispatcher(store, sender).run_once(now=NOW)
    assert report.claimed == 0, "the Telegram dispatcher must claim zero dashboard-only work"
    assert sender.calls == []


# ── claim semantics ──────────────────────────────────────────────────────────


async def test_row_is_in_flight_while_sending_and_released_afterwards():
    store, _, delivery_id = await queue_entry_alert()
    observed = {}

    sender = StubSender(server_error_result())

    def probe():
        row = store.deliveries[delivery_id]
        observed["status_during_send"] = row.delivery_status
        observed["claimed_during_send"] = delivery_id in store.claimed

    sender.probe = probe
    await make_dispatcher(store, sender).run_once(now=NOW)
    assert observed["status_during_send"] is DeliveryStatus.IN_FLIGHT, (
        "a claimed row must be IN_FLIGHT before the provider is called (§13, crash-safe)"
    )
    assert observed["claimed_during_send"] is True
    # update_delivery releases the claim so the row can drain later:
    assert delivery_id not in store.claimed
    assert store.deliveries[delivery_id].delivery_status is DeliveryStatus.FAILED


# ── backoff schedule ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attempt", "base", "expected"),
    [
        pytest.param(1, 10, 10.0, id="first-step-is-the-base"),
        pytest.param(2, 10, 20.0, id="doubles"),
        pytest.param(3, 10, 40.0, id="doubles-again"),
        pytest.param(4, 10, 80.0, id="still-exponential"),
        pytest.param(1, 1, 1.0, id="base-floor-one-second"),
        pytest.param(2, 600, 900.0, id="capped-at-900"),
        pytest.param(12, 30, 900.0, id="deep-attempts-stay-capped"),
    ],
)
def test_backoff_seconds_is_exponential_and_capped(attempt, base, expected):
    value = backoff_seconds(attempt=attempt, base_seconds=base)
    assert value == expected
    assert value <= BACKOFF_CAP_SECONDS == 900.0


def test_backoff_is_monotonically_increasing_until_the_cap():
    steps = [backoff_seconds(attempt=a, base_seconds=7) for a in range(1, 12)]
    assert all(later >= earlier for earlier, later in zip(steps, steps[1:], strict=False))
    assert steps[-1] == 900.0


# ── §12.1 entry alerts must reach Telegram end to end ────────────────────────


async def test_default_cooldown_entry_alert_still_queues_a_telegram_row():
    store = InMemoryStore()
    engine = AlertEngine(store=store, params=ScannerParams(), telegram_enabled=True, telegram_chat_id=CHAT)
    outcome = await engine.raise_alert(
        signal_type=SignalType.BULLISH_ENTRY,
        instrument_id=1,
        session_id=1,
        event_timestamp=minute(5),
        price=105.5,
        symbol="TATAMOTORS",
    )
    telegram_rows = [d for d in store.deliveries.values() if d.channel is Channel.TELEGRAM]
    assert outcome.suppressed_reason is None, "the first entry alert must not self-suppress"
    assert len(telegram_rows) == 1, "an entry alert must queue exactly one Telegram push"
    assert telegram_rows[0].delivery_status is DeliveryStatus.QUEUED

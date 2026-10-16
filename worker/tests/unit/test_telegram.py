"""Telegram client + result classification tests (spec §12.5).

The rule everything hangs on: *do not claim delivery until the API confirms* —
``delivered`` requires an ``ok: true`` body **and** an integer ``message_id``,
and nothing that would hang in a retry loop forever may be marked retryable.
All tests inject a fake httpx-like client and a fake ``sleep`` — no network,
no wall-clock waits — and the bot-token secret guard pins the "URLs are never
echoed back" promise from the module docstring.
"""

from __future__ import annotations

import json

import pytest

from worker.config import TelegramSettings
from worker.telegram import (
    DEFAULT_RETRY_AFTER_SECONDS,
    TRUNCATION_MARKER,
    TelegramSender,
    classify_response,
    retry_seconds,
    truncate,
)

SECRET = "SECRET-TOKEN-123"
TEXT_LIMIT = 4096


class FakeResponse:
    """Tiny httpx.Response stand-in: status_code, content, json()."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    """Async client stand-in: records every post, replays queued responses.

    An ``Exception`` entry in the queue is raised instead of returned, which is
    how transport errors are simulated without touching the network.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSleep:
    """Records the retries the sender would wait for, without sleeping."""

    def __init__(self):
        self.waits = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


def telegram_settings(**overrides) -> TelegramSettings:
    base = {
        "bot_token": "bot-token",
        "chat_id": "chat-1",
        "max_attempts": 5,
        "retry_base_seconds": 10,
    }
    base.update(overrides)
    return TelegramSettings(**base)


def sender_with(responses, **settings_overrides):
    client = FakeClient(responses)
    sleeper = FakeSleep()
    sender = TelegramSender(settings=telegram_settings(**settings_overrides), client=client, sleep=sleeper)
    return sender, client, sleeper


def ok_payload(message_id=7):
    return {"ok": True, "result": {"message_id": message_id}}


# ── classify_response: the delivery lie detector (§12.5) ─────────────────────


def test_confirmed_ok_with_message_id_is_the_only_delivery_proof():
    result = classify_response(200, ok_payload(message_id=7))
    assert result.ok is True
    assert result.message_id == 7, "the Telegram-assigned id must surface verbatim"
    assert result.delivered is True
    assert result.permanent is False
    assert result.http_status == 200


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"ok": True}, id="no-result"),
        pytest.param({"ok": True, "result": {}}, id="empty-result"),
        pytest.param({"ok": True, "result": {"message_id": None}}, id="none-id"),
        pytest.param({"ok": True, "result": {"message_id": "7"}}, id="string-id"),
        pytest.param({"ok": True, "result": {"message_id": 7.0}}, id="float-id"),
        pytest.param({"ok": True, "result": "nonsense"}, id="non-dict-result"),
    ],
)
def test_ok_without_integer_message_id_is_permanent_not_delivered(payload):
    # Spec §12.5: a 200 we cannot read a message_id from is a FAILURE — and it
    # must be permanent, because no retry will make Telegram grow an id.
    result = classify_response(200, payload)
    assert result.ok is False
    assert result.permanent is True, "malformed ok bodies must not be retried forever"
    assert result.delivered is False, "never report delivery without provider proof"
    assert result.message_id is None
    assert result.http_status == 200


def test_200_with_ok_false_is_failure_not_delivery():
    result = classify_response(200, {"ok": False, "description": "Bad Request"})
    assert result.ok is False
    assert result.delivered is False
    assert result.http_status == 200
    assert result.error == "Bad Request"


def test_429_is_retryable_and_honours_advertised_retry_after():
    payload = {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}
    result = classify_response(429, payload)
    assert result.ok is False
    assert result.permanent is False, "429 is the per-chat limiter, not a rejection"
    assert result.retry_after_seconds == 3.0
    assert result.delivered is False


def test_429_without_parameters_falls_back_to_default_retry_after():
    result = classify_response(429, {"ok": False})
    assert result.retry_after_seconds == DEFAULT_RETRY_AFTER_SECONDS == 5.0
    assert result.error == "telegram rate limit"


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_client_errors_are_permanent(code):
    result = classify_response(code, {"ok": False, "description": "chat is inaccessible"})
    assert result.permanent is True, f"{code} must stop the retry loop immediately"
    assert result.ok is False
    assert result.delivered is False
    assert result.http_status == code


@pytest.mark.parametrize("code", [500, 502])
def test_server_errors_are_retryable(code):
    result = classify_response(code, {"ok": False})
    assert result.permanent is False
    assert result.ok is False
    assert result.delivered is False
    assert result.error == f"telegram http {code}"


# ── retry_after parsing: default, malformed, floor ───────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"parameters": {"retry_after": 8}}, 8.0, id="advertised"),
        pytest.param({"parameters": {"retry_after": 1.5}}, 1.5, id="float-passthrough"),
        pytest.param({}, DEFAULT_RETRY_AFTER_SECONDS, id="missing-params"),
        pytest.param({"parameters": {}}, DEFAULT_RETRY_AFTER_SECONDS, id="missing-retry-after"),
        pytest.param({"retry_after": 99}, DEFAULT_RETRY_AFTER_SECONDS, id="wrong-place"),
        pytest.param({"parameters": {"retry_after": "soon"}}, DEFAULT_RETRY_AFTER_SECONDS, id="malformed"),
        pytest.param({"parameters": "oops"}, DEFAULT_RETRY_AFTER_SECONDS, id="params-not-dict"),
        pytest.param({"parameters": {"retry_after": 0}}, 0.25, id="floored-at-quarter-second"),
        pytest.param({"parameters": {"retry_after": -10}}, 0.25, id="negative-floored"),
    ],
)
def test_retry_seconds_parsing(payload, expected):
    assert retry_seconds(payload) == expected


# ── truncate: the 4096 hard limit (§12.5) ────────────────────────────────────


def test_truncate_leaves_exactly_at_limit_untouched():
    text = "a" * TEXT_LIMIT
    assert truncate(text) == text


def test_truncate_over_limit_shortens_and_appends_marker():
    text = "HEADER " + "x" * (TEXT_LIMIT + 500)
    out = truncate(text)
    assert len(out) <= TEXT_LIMIT
    assert out.endswith(TRUNCATION_MARKER)
    assert out.startswith("HEADER "), "the message header must survive truncation"


# ── TelegramSender.send: retry layering without sleeping ─────────────────────


async def test_send_success_makes_exactly_one_call():
    sender, client, sleeper = sender_with([FakeResponse(200, ok_payload(message_id=42))])
    result = await sender.send("Bullish RELIANCE")
    assert result.ok is True
    assert result.message_id == 42
    assert result.delivered is True
    assert result.attempts == 1
    assert len(client.calls) == 1
    assert sleeper.waits == [], "a confirmed send must not sleep"
    assert client.calls[0]["json"]["text"] == "Bullish RELIANCE"
    assert client.calls[0]["json"]["chat_id"] == "chat-1"


async def test_send_retries_429_then_success_honours_advertised_wait():
    responses = [
        FakeResponse(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
        FakeResponse(200, ok_payload(message_id=9)),
    ]
    sender, client, sleeper = sender_with(responses)
    result = await sender.send("hello")
    assert result.delivered is True
    assert result.attempts == 2
    assert len(client.calls) == 2
    assert sleeper.waits == [3.0], "the wait must equal the retry_after Telegram advertised"


async def test_send_permanent_rejection_stops_after_one_call():
    sender, client, sleeper = sender_with([FakeResponse(400, {"ok": False, "description": "Bad Request"})])
    result = await sender.send("hello")
    assert result.ok is False
    assert result.permanent is True
    assert result.attempts == 1
    assert len(client.calls) == 1, "a bad chat id must not burn the rate limiter"
    assert sleeper.waits == []


async def test_send_persistent_server_errors_use_exactly_max_attempts():
    responses = [FakeResponse(500, {"ok": False}) for _ in range(3)]
    sender, client, sleeper = sender_with(responses, max_attempts=3)
    result = await sender.send("hello")
    assert result.ok is False
    assert result.attempts == 3
    assert result.permanent is False
    assert result.delivered is False
    assert len(client.calls) == 3, "one provider call per attempt, no more, no less"
    assert sleeper.waits == [10.0, 20.0], "exponential sender-internal backoff, none after the last try"


async def test_send_transport_error_is_retryable_and_eventually_fails():
    sender, client, sleeper = sender_with(
        [ConnectionError("transport closed unexpectedly"), FakeResponse(500, {"ok": False})],
        max_attempts=2,
    )
    result = await sender.send("hello")
    assert result.ok is False
    assert result.permanent is False, "any transport failure is retryable per §12.5"
    assert result.attempts == 2
    assert len(client.calls) == 2
    # the folded result reports the FINAL attempt's outcome (5xx here); the
    # transport error's retryability is pinned by the extra call + single wait
    assert result.error == "telegram http 500"
    assert sleeper.waits == [10.0]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"bot_token": ""}, id="empty-bot-token"),
        pytest.param({"chat_id": ""}, id="empty-chat-id"),
    ],
)
async def test_unconfigured_sender_fails_permanently_with_zero_client_calls(overrides):
    sender, client, _ = sender_with([FakeResponse(200, ok_payload())], **overrides)
    result = await sender.send("hello")
    assert result.ok is False
    assert result.permanent is True
    assert result.attempts == 0
    assert result.delivered is False
    assert result.error == "telegram is not configured"
    assert client.calls == [], "unconfigured must never touch the network"


async def test_explicit_chat_id_override_makes_an_unconfigured_chat_sendable():
    sender, client, _ = sender_with([FakeResponse(200, ok_payload(message_id=4))], chat_id="")
    result = await sender.send("hello", chat_id="override-chat")
    assert result.delivered is True
    assert client.calls[0]["json"]["chat_id"] == "override-chat"


async def test_send_truncates_the_body_before_calling_the_client():
    sender, client, _ = sender_with([FakeResponse(200, ok_payload())])
    await sender.send("y" * (TEXT_LIMIT + 100))
    sent_text = client.calls[0]["json"]["text"]
    assert len(sent_text) == TEXT_LIMIT
    assert sent_text.endswith(TRUNCATION_MARKER)


def test_configured_property_requires_token_and_chat():
    assert TelegramSender(settings=telegram_settings()).configured is True
    assert TelegramSender(settings=telegram_settings(bot_token="")).configured is False
    assert TelegramSender(settings=telegram_settings(chat_id="")).configured is False


# ── SECRET GUARD: the token lives in the URL and nowhere else ─────────────────


@pytest.mark.parametrize(
    "responses",
    [
        pytest.param([FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})], id="permanent-400"),
        pytest.param(
            [FakeResponse(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 1}})],
            id="429-exhausted",
        ),
        pytest.param([ConnectionError("transport closed unexpectedly")], id="transport-error"),
    ],
)
async def test_bot_token_never_leaks_into_the_result(responses):
    sender, client, _ = sender_with(responses, bot_token=SECRET, max_attempts=1)
    result = await sender.send("hello")
    assert result.ok is False
    assert SECRET not in (result.error or ""), "error text must never carry the bot token"
    assert SECRET not in repr(result), "the whole result object must be safe to log"
    # Sanity: the URL does contain the token (Telegram's API shape), yet the
    # sender must never echo that recorded URL back in anything it returns.
    assert SECRET in client.calls[-1]["url"]
    assert "api.telegram.org" not in repr(result)


# ── multi-channel fan-out: chat_targets and send_all ─────────────────────

def test_chat_targets_orders_dedupes_and_drops_blanks():
    sender, _, _ = sender_with(
        [], chat_id=" chat-1 ", extra_chat_ids=("chat-2", "", "chat-1", "  ")
    )
    assert sender.chat_targets == ["chat-1", "chat-2"]


def test_chat_targets_is_empty_when_nothing_is_configured():
    sender, _, _ = sender_with([], bot_token="", chat_id="", extra_chat_ids=())
    assert sender.chat_targets == []


async def test_send_all_visits_every_channel_and_reports_each_one():
    """Per-channel results, not a single verdict: one dead chat must not hide
    a delivered message in another, and the dispatcher needs both halves."""
    sender, client, _ = sender_with(
        [
            FakeResponse(200, ok_payload(message_id=42)),
            FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"}),
        ],
        chat_id="chat-1",
        extra_chat_ids=("chat-2",),
    )
    results = await sender.send_all("hello")
    assert [chat for chat, _ in results] == ["chat-1", "chat-2"]
    assert results[0][1].delivered is True and results[0][1].message_id == 42
    assert results[1][1].delivered is False and results[1][1].permanent is True
    assert [call["json"]["chat_id"] for call in client.calls] == ["chat-1", "chat-2"]


async def test_send_all_with_no_channel_makes_no_client_call():
    sender, client, _ = sender_with([], chat_id="", extra_chat_ids=())
    assert await sender.send_all("hello") == []
    assert client.calls == []

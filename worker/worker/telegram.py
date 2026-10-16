"""Telegram ``sendMessage`` client with honest delivery semantics (spec §12.5).

The rule this module exists to enforce: **a signal is only reported as
delivered when the Telegram API says so.** ``TelegramResult.ok`` therefore
requires an ``ok: true`` response *and* a ``message_id`` in the result — a 200
with a malformed body counts as a failure, because the alternative would be
telling the user an alert arrived when it may not have.

Retry classification follows Telegram's documented responses:

* ``429`` → retry, honouring ``parameters.retry_after`` (per-chat limiter);
* ``5xx`` and transport errors → retry with exponential backoff;
* ``400``/``401``/``403``/``404`` → permanent. A bad chat id or a blocked bot
  does not fix itself, so retrying would only burn the rate limiter;
* text longer than Telegram's 4096-character limit is truncated here rather
  than sent and rejected on the other end.

The bot token lives inside the request URL, so URLs are never logged and never
embedded in an exception message: only status codes and Telegram's own
``description`` text surface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

import httpx

from .config import TelegramSettings
from .logging_setup import get_logger, scrub

log = get_logger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
TRUNCATION_MARKER = "\n…[truncated]"
MAX_ERROR_CHARS = 300
DEFAULT_RETRY_AFTER_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 900.0
PERMANENT_STATUS_CODES = frozenset({400, 401, 403, 404})


@dataclass(frozen=True)
class TelegramResult:
    """Outcome of one send *sequence* (all internal attempts folded together)."""

    ok: bool
    message_id: int | None = None
    error: str | None = None
    permanent: bool = False
    attempts: int = 0
    http_status: int | None = None
    retry_after_seconds: float | None = None

    @property
    def delivered(self) -> bool:
        """True only when Telegram confirmed receipt and gave us a message id."""
        return self.ok and self.message_id is not None


def truncate(text: str, *, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Keep the message inside Telegram's hard limit without losing the header."""
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def retry_seconds(payload: dict[str, Any]) -> float:
    """``parameters.retry_after`` from a 429 body, defaulting when absent."""
    params = payload.get("parameters")
    raw = params.get("retry_after") if isinstance(params, dict) else None
    try:
        value = float(raw) if raw is not None else DEFAULT_RETRY_AFTER_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_RETRY_AFTER_SECONDS
    return max(0.25, value)


def classify_response(status_code: int, payload: dict[str, Any]) -> TelegramResult:
    """Map one HTTP response onto a result — pure, so tests need no network."""
    description = str(payload.get("description") or "")[:MAX_ERROR_CHARS]
    if status_code == 200:
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if payload.get("ok") is True and isinstance(message_id, int):
            return TelegramResult(ok=True, message_id=message_id, http_status=200)
        # 200 without an id is not proof of delivery — treat it as failure.
        return TelegramResult(
            ok=False,
            error=description or "telegram returned no message_id",
            permanent=True,
            http_status=status_code,
        )
    if status_code == 429:
        return TelegramResult(
            ok=False,
            error=description or "telegram rate limit",
            http_status=429,
            retry_after_seconds=retry_seconds(payload),
        )
    if status_code in PERMANENT_STATUS_CODES:
        return TelegramResult(
            ok=False,
            error=description or f"telegram rejected the message ({status_code})",
            permanent=True,
            http_status=status_code,
        )
    return TelegramResult(
        ok=False,
        error=description or f"telegram http {status_code}",
        http_status=status_code,
    )


class TelegramSender:
    """Thin async client. ``client`` is injectable so tests need no network.

    When the sender creates its own :class:`httpx.AsyncClient` it is reused for
    every message in the process; callers should still ``await close()`` on
    shutdown.
    """

    def __init__(
        self,
        *,
        settings: TelegramSettings,
        client: Any | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.settings.bot_token and self.settings.chat_id)

    @property
    def chat_targets(self) -> list[str]:
        """Every live channel, primary first, de-duplicated.

        Read through ``self.settings`` rather than captured at construction:
        ``_apply_alert_settings`` swaps the settings object when the dashboard
        saves, and the dispatcher must fan out to the channels of *now*, not of
        session open.
        """
        ids = [self.settings.chat_id, *self.settings.extra_chat_ids]
        return [chat for chat in dict.fromkeys(str(i).strip() for i in ids) if chat]

    def method_url(self) -> str:
        """Build the endpoint. Contains the bot token — never log the result."""
        return f"{self.settings.api_url.rstrip('/')}/bot{self.settings.bot_token}/sendMessage"

    async def _get_client(self) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def send(self, text: str, *, chat_id: str | None = None) -> TelegramResult:
        """Send one message, retrying inside the documented limits."""
        destination = chat_id or self.settings.chat_id
        if not self.settings.bot_token or not destination:
            return TelegramResult(
                ok=False, error="telegram is not configured", permanent=True, attempts=0
            )
        body = {
            "chat_id": destination,
            "text": truncate(text),
            "disable_web_page_preview": True,
        }
        client = await self._get_client()
        url = self.method_url()
        last: TelegramResult | None = None
        attempts = 0
        while attempts < self.settings.max_attempts:
            attempts += 1
            try:
                response = await client.post(url, json=body)
            except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
                last = TelegramResult(
                    ok=False,
                    error=f"{type(exc).__name__}: {str(exc)[:MAX_ERROR_CHARS]}",
                    attempts=attempts,
                )
            else:
                outcome = classify_response(response.status_code, _json_body(response))
                last = replace(outcome, attempts=attempts)
                if last.ok or last.permanent:
                    return last
            if attempts < self.settings.max_attempts and last is not None:
                await self._sleep(last.retry_after_seconds or self._backoff(attempts))
        final = replace(last or TelegramResult(ok=False), attempts=attempts)
        if final.error is None:
            final = replace(final, error="telegram attempts exhausted")
        log.warning(
            "telegram_send_failed",
            attempts=final.attempts,
            status=final.http_status,
            error=scrub(final.error),
        )
        return final

    async def send_all(self, text: str) -> list[tuple[str, TelegramResult]]:
        """Send one message to every channel, in order.

        Sequential on purpose: Telegram's limiter is per-chat but the bot token
        is shared, and firing at all channels at once is how a multi-channel
        setup teaches a single-chat burst a 429 lesson. A dead channel costs one
        send sequence and nothing more — its result is reported per chat, and
        the caller decides what the aggregate means.
        """
        return [(chat, await self.send(text, chat_id=chat)) for chat in self.chat_targets]

    def _backoff(self, attempts: int) -> float:
        """Exponential backoff on the configured base, capped so retries settle."""
        return min(self.settings.retry_base_seconds * (2 ** (attempts - 1)), BACKOFF_CAP_SECONDS)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            closer = getattr(self._client, "aclose", None)
            if closer is not None:
                await closer()
            self._client = None


def _json_body(response: Any) -> dict[str, Any]:
    try:
        parsed = response.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

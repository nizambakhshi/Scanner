"""Structured logging with secret redaction (spec §19).

Secrets must never reach stdout, a log shipper, or an exception message, so
redaction happens in the logging pipeline itself rather than relying on every
call site to be careful.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

SECRET_KEY_PATTERN = (
    "token",
    "secret",
    "password",
    "authorization",
    "apikey",
    "api_key",
    "privatekey",
    "totp",
    "refresh",
    "chat_id",
)
#: PIN-style credentials are matched per word, never as a substring: "pin" occurs
#: inside ordinary metric names, and the first live ``sync_instruments --status``
#: run masked ``mapping_pct`` — the one number the operator needed.
PIN_WORDS = frozenset({"pin", "mpin", "swpin", "userpin", "loginpin", "transactionpin"})
REDACTED = "***redacted***"
_MAX_STRING = 400

# Instrument tokens are public: the whole mapping is downloadable from the broker
# as the scrip master (docs/research/smartapi-ws-findings.md:256-267) and it is how
# every retail terminal identifies a listing. The substring rule below would mask
# them, which turns an undiagnosable candle/quote failure into a redacted line.
# Credentials that share the "token" word (``refresh_token``, ``feed_token``,
# ``access_token``) are *not* listed here and stay masked.
PUBLIC_IDENTIFIER_KEYS = frozenset(
    {
        "symboltoken",
        "instrument_token",
        "instrument_tokens",
        "duplicate_tokens",
        "token_count",
        "subscribed_tokens",
    }
)


def is_secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    if lowered in PUBLIC_IDENTIFIER_KEYS:
        return False
    if any(word in PIN_WORDS for word in lowered.split("_")):
        return True
    return any(marker in lowered for marker in SECRET_KEY_PATTERN)


def scrub(value: Any) -> Any:
    """Recursively mask secret-bearing values and oversized strings."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_secret_key(str(k)) else scrub(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str) and len(value) > _MAX_STRING:
        return value[:_MAX_STRING] + "…[truncated]"
    return value


def redact_processor(logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return scrub(event_dict)


def sensitive_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Convenience for call sites that build a payload from user/broker data."""
    return scrub(kwargs)  # type: ignore[return-value]


_configured = False
HANDLER_ATTR = "_scanner_structlog"


def add_logger_name(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """``structlog.stdlib.add_logger_name`` without the crash.

    The shipped processor reads ``logger.name`` unguarded, so with a
    ``PrintLoggerFactory`` (whose ``PrintLogger`` has no name) every single log
    call raised ``AttributeError`` — verified on structlog 26.1.0. This version
    keeps the same ``logger`` key but tolerates a nameless logger object.
    """
    if "logger" not in event_dict:
        record = event_dict.get("_record")
        name = getattr(record, "name", None) if record is not None else getattr(logger, "name", None)
        if name:
            event_dict["logger"] = name
    return event_dict


def _pre_chain() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]


def _renderer(fmt: str) -> Any:
    if fmt == "json":
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())


def configure_logging(level: str = "info", *, fmt: str = "console", force: bool = False) -> None:
    """Wire structlog onto the stdlib root handler so there is exactly one sink.

    Why stdlib instead of a plain ``PrintLogger``: the broker SDK, httpx and
    websockets all log through ``logging``, and routing structlog through the
    same handler means a log shipper sees one stream and one format instead of
    two interleaved ones.
    """
    global _configured
    if _configured and not force:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)
    pre_chain = _pre_chain()

    structlog.configure(
        processors=[*pre_chain, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # structlog-origin events skip ``foreign_pre_chain``; the records that
            # do use it are still redacted, because a third-party logger can print
            # a URL or header we never want to reach a log shipper (§19).
            # ``format_exc_info`` lives here (not only in the pre-chain) so a
            # foreign record's ``exc_info`` renders as a traceback string instead
            # of leaking ``<traceback object at 0x...>`` into the JSON.
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                _renderer(fmt),
            ],
            foreign_pre_chain=[
                add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=False),
                redact_processor,
            ],
        )
    )
    setattr(handler, HANDLER_ATTR, fmt)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, HANDLER_ATTR, None) is not None:
            root.removeHandler(existing)
    if not any(getattr(h, HANDLER_ATTR, None) is not None for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(log_level)
    for noisy in ("websockets", "httpx", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    _configured = True


def get_logger(name: str) -> Any:
    """A bound structlog logger; the wrapper class is chosen by ``configure``."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)

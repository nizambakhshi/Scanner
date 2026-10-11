"""Tests for ``worker.logging_setup`` — the §19 redaction contract, and one crash.

The pipeline previously paired ``PrintLoggerFactory`` with
``structlog.stdlib.add_logger_name``. That processor reads ``logger.name``
unguarded (structlog 26.1.0), a ``PrintLogger`` has no name, and so *every* log
call raised ``AttributeError`` — a crash invisible to the rest of the suite,
because no other test emits a log line. These tests render real output.

Nothing here writes files or touches the network. The fixture restores the root
logger and the structlog configuration so global logging state cannot leak into
another test.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from worker import logging_setup as L

NOISY = ("websockets", "httpx", "asyncio", "urllib3")

CREDENTIALS = {
    "api_key": "ANGEL-API-KEY-XYZ",
    "client_secret": "SECRETLAUNCHVALUE",
    "pin": "1234",
    "totp_secret": "JBSWY3DPEHPK3PXP",
    "refresh_token": "REFRESH-TOKEN-VALUE",
    "feed_token": "FEED-TOKEN-VALUE",
    "access_token": "ACCESS-TOKEN-VALUE",
    "authorization": "Bearer something",
    "chat_id": "99887766",
    "password": "hunter2",
    "privatekey": "PEM-BLOB",
}


class Renderer:
    """Configures the pipeline on demand and returns the stderr it produced."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys

    def configure(self, fmt: str = "console", level: str = "info") -> None:
        L.configure_logging(level, fmt=fmt, force=True)

    def take(self) -> str:
        return self._capsys.readouterr().err

    def emit(self, fmt: str, level: str = "info", **events: object) -> str:
        """``info={"event": "x", "a": 1}`` → one ``log.info(event="x", a=1)`` call."""
        self.configure(fmt, level)
        log = structlog.get_logger("worker.test")
        for kind, payload in events.items():
            arguments = payload if isinstance(payload, dict) else {"event": str(payload)}
            getattr(log, kind)(**arguments)
        return self.take()

    def foreign(self, fmt: str, name: str, message: str, *args: object) -> str:
        """A plain ``logging`` record from a third-party library."""
        self.configure(fmt)
        logging.getLogger(name).warning(message, *args)
        return self.take()


def structlog_handlers() -> list[logging.Handler]:
    return [h for h in logging.root.handlers if getattr(h, L.HANDLER_ATTR, None) is not None]


@pytest.fixture()
def renderer(capsys: pytest.CaptureFixture[str]) -> Iterator[Renderer]:
    """``configure_logging`` binds ``sys.stderr`` at call time, so it must run
    while ``capsys`` has already swapped the stream — hence a factory fixture."""
    saved_handlers = list(logging.root.handlers)
    saved_level = logging.root.level
    saved_config = structlog.get_config()
    saved_noisy = {name: logging.getLogger(name).level for name in NOISY}
    try:
        yield Renderer(capsys)
    finally:
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)
        for name, value in saved_noisy.items():
            logging.getLogger(name).setLevel(value)
        structlog.configure(**saved_config)
        L._configured = False


# ── the crash regression: emitting must simply work ───────────────────────


@pytest.mark.parametrize("fmt", ["console", "json"])
def test_every_log_level_renders(renderer: Renderer, fmt: str) -> None:
    out = renderer.emit(
        fmt,
        info={"event": "candle_finalized", "instrument": "RELIANCE", "count": 3},
        warning={"event": "feed_stale", "stale_seconds": 47},
        error={"event": "dispatch_failed", "attempt": 2},
    )
    assert "candle_finalized" in out
    assert "feed_stale" in out
    assert "dispatch_failed" in out
    assert "worker.test" in out  # the logger name survived the pipeline


def test_debug_is_filtered_below_the_configured_level(renderer: Renderer) -> None:
    out = renderer.emit(
        "console", "info", debug={"event": "tick_received"}, info={"event": "candle_ok"}
    )
    assert "candle_ok" in out
    assert "tick_received" not in out


@pytest.mark.parametrize("fmt", ["console", "json"])
def test_structlog_exception_renders_a_traceback(renderer: Renderer, fmt: str) -> None:
    renderer.configure(fmt)
    log = structlog.get_logger("worker.test")
    try:
        raise ValueError("smartapi envelope missing data")
    except ValueError:
        log.error("parse_failed", exc_info=True)
    out = renderer.take()
    assert "parse_failed" in out
    assert "ValueError" in out
    assert "smartapi envelope missing data" in out
    assert "<traceback object at" not in out


@pytest.mark.parametrize("fmt", ["console", "json"])
def test_foreign_exception_does_not_leak_a_traceback_object(renderer: Renderer, fmt: str) -> None:
    renderer.configure(fmt)
    try:
        raise ZeroDivisionError("division by zero")
    except ZeroDivisionError:
        logging.getLogger("sdk.other").exception("ws handshake failed")
    out = renderer.take()
    assert "ws handshake failed" in out
    assert "ZeroDivisionError" in out
    assert "<traceback object at" not in out


def test_reconfiguring_does_not_stack_handlers(renderer: Renderer) -> None:
    renderer.emit("console", "info", info={"event": "first"})
    renderer.emit("json", "info", info={"event": "second"})
    assert len(structlog_handlers()) == 1


# ── §19: credentials never reach the log stream ───────────────────────────


@pytest.mark.parametrize("fmt", ["console", "json"])
def test_credential_values_are_redacted(renderer: Renderer, fmt: str) -> None:
    out = renderer.emit(fmt, "info", info={"event": "login_attempt", **CREDENTIALS})
    for value in CREDENTIALS.values():
        assert value not in out, f"{value} leaked through {fmt}"
    assert out.count(L.REDACTED) >= len(CREDENTIALS)


def test_redaction_survives_nested_payloads() -> None:
    scrubbed = L.scrub({"data": {"one_time_password": "otp123", "symbol": "TATASTEEL"}})
    assert scrubbed["data"]["one_time_password"] == L.REDACTED
    assert scrubbed["data"]["symbol"] == "TATASTEEL"


def test_oversized_strings_are_truncated() -> None:
    out = L.scrub({"frame": "x" * (L._MAX_STRING + 500)})
    text = out["frame"]
    assert isinstance(text, str)
    assert text.endswith("…[truncated]")
    assert len(text) < 1000


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("refresh_token", True),
        ("feed_token", True),
        ("X-PrivateKey", True),
        ("api_key", True),
        ("apikey", True),
        ("Chat-Id", True),
        ("pin", True),
        ("user_pin", True),
        ("X-Pin", True),
        ("sw_pin", True),
        ("transactionpin", True),
        ("symboltoken", False),
        ("instrument_token", False),
        ("duplicate_tokens", False),
        ("mapping_pct", False),
        ("coverage_pct", False),
        ("open", False),
        ("symbol", False),
        ("stale_seconds", False),
    ],
)
def test_is_secret_key_classification(key: str, secret: bool) -> None:
    assert L.is_secret_key(key) is secret


def test_pin_matching_is_per_word_not_substring() -> None:
    """The regression: "pin" as a substring masked the operator's own metrics.

    The first live ``sync_instruments --status`` run printed
    ``mapping_pct=***redacted***``, so the credential rule now matches whole
    words only, and an ordinary metric whose name merely contains "pin" stays
    readable.
    """
    assert L.is_secret_key("user_pin") is True
    assert L.is_secret_key("mapping_pct") is False
    assert L.is_secret_key("painting_group") is False


def test_operational_metrics_render_as_numbers(renderer: Renderer) -> None:
    out = renderer.emit(
        "json",
        "info",
        info={
            "event": "db_coverage",
            "mapping_pct": 24.15,
            "monitored_pct": 0.0,
            "universe_total": 501,
            "user_pin": "1234",
        },
    )
    assert '"mapping_pct": 24.15' in out
    assert '"universe_total": 501' in out
    assert '"user_pin": ' + L.REDACTED in out or "1234" not in out


def test_public_instrument_tokens_stay_readable(renderer: Renderer) -> None:
    """Instrument tokens are public scrip-master data, not credentials.

    Masking them made candle/quote failures undiagnosable, so the pipeline keeps
    those values while every credential-shaped key stays redacted.
    """
    out = renderer.emit(
        "json",
        "info",
        warning={
            "event": "smartapi_candle_empty",
            "symboltoken": "2885",
            "instrument_token": "1000000",
            "refresh_token": "must-not-appear",
        },
    )
    assert '"symboltoken": "2885"' in out
    assert '"instrument_token": "1000000"' in out
    assert "must-not-appear" not in out


# ── output shape ──────────────────────────────────────────────────────────


def test_json_output_is_one_parsable_object_per_line(renderer: Renderer) -> None:
    out = renderer.emit(
        "json",
        "info",
        info={"event": "heartbeat", "subscribed_instrument_count": 500},
        warning={"event": "reauth", "attempt": 1},
    )
    lines = [line for line in out.strip().splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [row["event"] for row in parsed] == ["heartbeat", "reauth"]
    for row in parsed:
        assert row["logger"] == "worker.test"
        assert "timestamp" in row
        assert "_record" not in row and "_from_structlog" not in row
    assert parsed[0]["level"] == "info"
    assert parsed[1]["level"] == "warning"
    assert parsed[0]["subscribed_instrument_count"] == 500


def test_foreign_records_use_the_same_pipeline(renderer: Renderer) -> None:
    out = renderer.foreign("json", "httpx", "HTTP Request: %s %s", "GET", "https://example/api")
    row = json.loads(out.strip().splitlines()[-1])
    assert row["event"] == "HTTP Request: GET https://example/api"
    assert row["logger"] == "httpx"
    assert row["level"] == "warning"

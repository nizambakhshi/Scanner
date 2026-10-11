"""SmartAPI V2 ``smart-stream`` wire format.

Source of truth (verified 2026-10-11 against the official docs bundle and the
official Python SDK — see ``docs/research/smartapi-ws-findings.md``):

* the response is a flat **little-endian binary struct**, NOT protobuf and NOT
  gzip;
* prices are integers in **paise** → divide by 100 for NSE equities;
* ``exchange_timestamp`` is epoch **milliseconds**;
* ``volume_trade_for_the_day`` is **day-cumulative**, so per-minute volume must
  be computed as a delta (see ``candle_builder``);
* packet sizes: LTP = 51 bytes, QUOTE = 123 bytes, SNAP_QUOTE = 379 bytes;
* control traffic (subscription requests, errors, heartbeat) is plain text
  JSON / ``ping`` / ``pong``.

Parsing is kept pure and side-effect-free so the decoder is unit-testable with
hand-built byte buffers.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, replace
from datetime import UTC, datetime

LTP_PACKET_SIZE = 51
QUOTE_PACKET_SIZE = 123
SNAP_QUOTE_PACKET_SIZE = 379

MODE_LTP = 1
MODE_QUOTE = 2
MODE_SNAP_QUOTE = 3
MODE_DEPTH = 4

EXCHANGE_NSE_CM = 1
EXCHANGE_BSE_CM = 3

MODE_NAMES = {MODE_LTP: "LTP", MODE_QUOTE: "QUOTE", MODE_SNAP_QUOTE: "SNAP_QUOTE", MODE_DEPTH: "DEPTH"}


class FeedDecodeError(ValueError):
    """Raised when a frame cannot be decoded at all (never for a bad price)."""


@dataclass(frozen=True)
class FeedPacket:
    """A decoded market-data frame, still in raw feed units (paise)."""

    subscription_mode: int
    exchange_type: int
    token: str
    sequence_number: int | None
    exchange_timestamp_ms: int | None
    last_traded_price_paise: int
    last_traded_quantity: int | None = None
    average_traded_price_paise: int | None = None
    volume_trade_for_the_day: int | None = None
    total_buy_quantity: float | None = None
    total_sell_quantity: float | None = None
    open_price_paise: int | None = None
    high_price_paise: int | None = None
    low_price_paise: int | None = None
    closed_price_paise: int | None = None
    last_traded_timestamp_ms: int | None = None
    raw_size: int = 0

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.subscription_mode, f"UNKNOWN({self.subscription_mode})")

    @property
    def exchange_timestamp(self) -> datetime:
        if self.exchange_timestamp_ms in (None, 0):
            # A missing exchange stamp must never silently become "now".
            raise FeedDecodeError("packet carries no exchange timestamp")
        return datetime.fromtimestamp(self.exchange_timestamp_ms / 1000.0, tz=UTC)

    @property
    def exchange_type_name(self) -> str:
        return {EXCHANGE_NSE_CM: "NSE", EXCHANGE_BSE_CM: "BSE"}.get(self.exchange_type, f"EXCH_{self.exchange_type}")


def _unpack_int(raw: bytes, offset: int, fmt: str = "q") -> int:
    size = struct.calcsize("<" + fmt)
    if offset + size > len(raw):
        raise FeedDecodeError(f"packet too short: need {offset + size} bytes, have {len(raw)}")
    return struct.unpack("<" + fmt, raw[offset : offset + size])[0]


def _unpack_float(raw: bytes, offset: int, fmt: str = "d") -> float:
    size = struct.calcsize("<" + fmt)
    if offset + size > len(raw):
        raise FeedDecodeError(f"packet too short: need {offset + size} bytes, have {len(raw)}")
    return struct.unpack("<" + fmt, raw[offset : offset + size])[0]


def _parse_token(raw: bytes) -> str:
    """Tokens are a 25-byte NUL-padded UTF-8 field."""
    chunk = raw[2:27]
    end = chunk.find(b"\x00")
    if end != -1:
        chunk = chunk[:end]
    return chunk.decode("utf-8", errors="replace").strip()


def decode_packet(raw: bytes) -> FeedPacket:
    """Decode one binary market-data frame.

    Never fabricates values: absent fields stay ``None`` so the candle engine
    can distinguish "the feed did not say" from "the feed said zero".
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise FeedDecodeError(f"binary frame expected, got {type(raw).__name__}")
    if len(raw) < LTP_PACKET_SIZE:
        raise FeedDecodeError(f"frame shorter than the LTP packet ({len(raw)} bytes)")

    mode = raw[0]
    packet = FeedPacket(
        subscription_mode=mode,
        exchange_type=raw[1],
        token=_parse_token(raw),
        sequence_number=_unpack_int(raw, 27),
        exchange_timestamp_ms=_unpack_int(raw, 35),
        last_traded_price_paise=_unpack_int(raw, 43, "q"),
        raw_size=len(raw),
    )

    if mode in (MODE_QUOTE, MODE_SNAP_QUOTE):
        if len(raw) >= 75:
            packet = replace(
                packet,
                last_traded_quantity=_unpack_int(raw, 51),
                average_traded_price_paise=_unpack_int(raw, 59),
                volume_trade_for_the_day=_unpack_int(raw, 67),
            )
        if len(raw) >= 91:
            packet = replace(
                packet,
                total_buy_quantity=_unpack_float(raw, 75),
                total_sell_quantity=_unpack_float(raw, 83),
            )
        if len(raw) >= QUOTE_PACKET_SIZE:
            packet = replace(
                packet,
                open_price_paise=_unpack_int(raw, 91),
                high_price_paise=_unpack_int(raw, 99),
                low_price_paise=_unpack_int(raw, 107),
                closed_price_paise=_unpack_int(raw, 115),
            )
    if mode == MODE_SNAP_QUOTE and len(raw) >= 131:
        packet = replace(packet, last_traded_timestamp_ms=_unpack_int(raw, 123))

    return packet


def scale_price(value_paise: int | None, price_scale: float) -> float | None:
    """Feed integers → rupees. ``None`` stays ``None`` (documented as unknown)."""
    if value_paise is None:
        return None
    if price_scale <= 0:
        raise ValueError("price scale must be > 0")
    return value_paise / price_scale


def parse_text_frame(raw: str | bytes) -> dict[str, object] | None:
    """Decode the socket's text frames: ``pong``, control messages, JSON errors.

    Returns ``None`` for a bare ``pong``/``ping`` so callers can treat those as
    liveness evidence rather than data.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else (raw or "").strip()
    if not text or text in {"pong", "ping"}:
        return None
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FeedDecodeError(f"unparseable control frame: {text[:120]}") from exc
        if isinstance(payload, dict):
            return {str(k): v for k, v in payload.items()}
        return {"frames": payload}  # type: ignore[return-value]
    return {"raw": text}


def is_subscription_error(payload: dict[str, object]) -> bool:
    """Docs: E1001 invalid payload, E1002 subscription limit exceeded."""
    code = str(payload.get("errorCode", payload.get("errorcode", "")))
    return bool(code) and code.startswith("E")


def error_text(payload: dict[str, object]) -> str:
    return str(payload.get("errorMessage", payload.get("message", "unknown feed error")))


def build_subscribe_message(
    *, correlation_id: str, mode: int, tokens: list[str], exchange_type: int = EXCHANGE_NSE_CM
) -> dict[str, object]:
    """Subscription request frame (spec: action 1 = subscribe)."""
    return {
        "correlationID": correlation_id,
        "action": 1,
        "params": {"mode": mode, "tokenList": [{"exchangeType": exchange_type, "tokens": list(tokens)}]},
    }


def build_unsubscribe_message(
    *, correlation_id: str, mode: int, tokens: list[str], exchange_type: int = EXCHANGE_NSE_CM
) -> dict[str, object]:
    return {
        "correlationID": correlation_id,
        "action": 0,
        "params": {"mode": mode, "tokenList": [{"exchangeType": exchange_type, "tokens": list(tokens)}]},
    }


def encode_subscribe_request(**kwargs: object) -> str:
    if "tokens" not in kwargs:
        raise ValueError("tokens required")
    tokens = list(kwargs["tokens"])  # type: ignore[arg-type]
    mode = int(kwargs["mode"])  # type: ignore[arg-type]
    correlation_id = str(kwargs.get("correlation_id", ""))[:10] or "scanner001"
    exchange_type = int(kwargs.get("exchange_type", EXCHANGE_NSE_CM))
    return json.dumps(
        build_subscribe_message(
            correlation_id=correlation_id, mode=mode, tokens=tokens, exchange_type=exchange_type
        ),
        separators=(",", ":"),
    )

"""Unit tests for the SmartAPI V2 ``smart-stream`` binary decoder.

Ground truth (verified 2026-10-11, see ``docs/research/smartapi-ws-findings.md``):
the feed payload is a flat little-endian struct (NOT protobuf/gzip), prices are
integers in paise, ``exchange_timestamp`` is epoch milliseconds, and a missing
value must never be fabricated. Every assertion below reads an offset that the
documented layout defines, and each buffer is hand-built so the test is the
authority on the wire format rather than the decoder.
"""

from __future__ import annotations

import json
import struct
from datetime import UTC, datetime

import pytest

from worker.marketdata import feed_protocol as fp

# Fixed header values reused across the builders.
NSE_TOKEN = "2885"
SEQ = 12345
EPOCH_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z
LTP_PAISE = 25050  # ₹250.50

# QUOTE-mode sample values.
LTQ = 100
AVG_PAISE = 25000
DAY_VOLUME = 999_999
TOTAL_BUY = 1234.5
TOTAL_SELL = 678.25
OPEN_PAISE = 24000
HIGH_PAISE = 26000
LOW_PAISE = 23000
CLOSE_PAISE = 23500
SNAP_LTS_MS = 1_700_000_000_500


def _header(raw: bytearray, mode: int, exchange: int = fp.EXCHANGE_NSE_CM) -> None:
    raw[0] = mode
    raw[1] = exchange
    raw[2:27] = NSE_TOKEN.encode("utf-8").ljust(25, b"\x00")
    struct.pack_into("<q", raw, 27, SEQ)
    struct.pack_into("<q", raw, 35, EPOCH_MS)
    struct.pack_into("<q", raw, 43, LTP_PAISE)


def ltp_packet(mode: int = fp.MODE_LTP, exchange: int = fp.EXCHANGE_NSE_CM) -> bytes:
    raw = bytearray(fp.LTP_PACKET_SIZE)
    _header(raw, mode, exchange)
    return bytes(raw)


def quote_packet(mode: int = fp.MODE_QUOTE) -> bytes:
    raw = bytearray(fp.QUOTE_PACKET_SIZE)
    _header(raw, mode)
    struct.pack_into("<q", raw, 51, LTQ)
    struct.pack_into("<q", raw, 59, AVG_PAISE)
    struct.pack_into("<q", raw, 67, DAY_VOLUME)
    struct.pack_into("<d", raw, 75, TOTAL_BUY)
    struct.pack_into("<d", raw, 83, TOTAL_SELL)
    struct.pack_into("<q", raw, 91, OPEN_PAISE)
    struct.pack_into("<q", raw, 99, HIGH_PAISE)
    struct.pack_into("<q", raw, 107, LOW_PAISE)
    struct.pack_into("<q", raw, 115, CLOSE_PAISE)
    return bytes(raw)


def snap_packet() -> bytes:
    raw = bytearray(fp.SNAP_QUOTE_PACKET_SIZE)
    raw[: fp.QUOTE_PACKET_SIZE] = quote_packet(fp.MODE_SNAP_QUOTE)
    struct.pack_into("<q", raw, 123, SNAP_LTS_MS)
    return bytes(raw)


# ── module constants ──────────────────────────────────────────────────────


def test_packet_sizes_and_mode_constants_match_documented_layout() -> None:
    assert (fp.LTP_PACKET_SIZE, fp.QUOTE_PACKET_SIZE, fp.SNAP_QUOTE_PACKET_SIZE) == (51, 123, 379)
    assert (fp.MODE_LTP, fp.MODE_QUOTE, fp.MODE_SNAP_QUOTE, fp.MODE_DEPTH) == (1, 2, 3, 4)
    assert fp.MODE_NAMES[fp.MODE_QUOTE] == "QUOTE"
    assert fp.EXCHANGE_NSE_CM == 1 and fp.EXCHANGE_BSE_CM == 3


# ── LTP mode decode ───────────────────────────────────────────────────────


def test_ltp_packet_decodes_header_and_price() -> None:
    packet = fp.decode_packet(ltp_packet())
    assert packet.subscription_mode == fp.MODE_LTP
    assert packet.mode_name == "LTP"
    assert packet.exchange_type == fp.EXCHANGE_NSE_CM
    assert packet.token == NSE_TOKEN
    assert packet.sequence_number == SEQ
    assert packet.exchange_timestamp_ms == EPOCH_MS
    assert packet.last_traded_price_paise == LTP_PAISE
    assert packet.raw_size == 51


def test_ltp_mode_never_fabricates_quote_fields() -> None:
    packet = fp.decode_packet(ltp_packet())
    for field in (
        "last_traded_quantity",
        "average_traded_price_paise",
        "volume_trade_for_the_day",
        "total_buy_quantity",
        "total_sell_quantity",
        "open_price_paise",
        "high_price_paise",
        "low_price_paise",
        "closed_price_paise",
        "last_traded_timestamp_ms",
    ):
        assert getattr(packet, field) is None, f"LTP packets must leave {field} as None"


def test_exchange_timestamp_property_reads_epoch_milliseconds() -> None:
    packet = fp.decode_packet(ltp_packet())
    assert packet.exchange_timestamp == datetime.fromtimestamp(EPOCH_MS / 1000.0, tz=UTC)
    assert packet.exchange_timestamp.tzinfo is not None


def test_exchange_timestamp_property_rejects_missing_stamp() -> None:
    # A missing exchange stamp may never silently become "now".
    for missing in (0, None):
        packet = fp.FeedPacket(
            subscription_mode=fp.MODE_LTP,
            exchange_type=fp.EXCHANGE_NSE_CM,
            token=NSE_TOKEN,
            sequence_number=None,
            exchange_timestamp_ms=missing,
            last_traded_price_paise=LTP_PAISE,
        )
        with pytest.raises(fp.FeedDecodeError):
            _ = packet.exchange_timestamp


@pytest.mark.parametrize(
    ("exchange", "expected"),
    [(fp.EXCHANGE_NSE_CM, "NSE"), (fp.EXCHANGE_BSE_CM, "BSE"), (7, "EXCH_7")],
)
def test_exchange_type_name(exchange: int, expected: str) -> None:
    packet = fp.decode_packet(ltp_packet(exchange=exchange))
    assert packet.exchange_type_name == expected


def test_mode_name_unknown_for_unmapped_mode() -> None:
    packet = fp.decode_packet(ltp_packet(mode=99))
    assert packet.mode_name == "UNKNOWN(99)"


# ── QUOTE mode decode ─────────────────────────────────────────────────────


def test_quote_packet_decodes_quantity_volume_and_book_fields() -> None:
    packet = fp.decode_packet(quote_packet())
    assert packet.mode_name == "QUOTE"
    assert packet.last_traded_quantity == LTQ
    assert packet.average_traded_price_paise == AVG_PAISE
    assert packet.volume_trade_for_the_day == DAY_VOLUME
    assert packet.total_buy_quantity == pytest.approx(TOTAL_BUY)
    assert packet.total_sell_quantity == pytest.approx(TOTAL_SELL)
    assert packet.open_price_paise == OPEN_PAISE
    assert packet.high_price_paise == HIGH_PAISE
    assert packet.low_price_paise == LOW_PAISE
    assert packet.closed_price_paise == CLOSE_PAISE
    assert packet.last_traded_timestamp_ms is None  # QUOTE stops before the snap field


def test_snap_quote_adds_last_traded_timestamp() -> None:
    packet = fp.decode_packet(snap_packet())
    assert packet.mode_name == "SNAP_QUOTE"
    assert packet.raw_size == 379
    assert packet.last_traded_timestamp_ms == SNAP_LTS_MS
    # SnapQuote still carries every QUOTE field.
    assert packet.open_price_paise == OPEN_PAISE
    assert packet.closed_price_paise == CLOSE_PAISE


# ── decode guardrails ─────────────────────────────────────────────────────


def test_decode_rejects_short_and_non_binary_frames() -> None:
    with pytest.raises(fp.FeedDecodeError):
        fp.decode_packet(b"")
    with pytest.raises(fp.FeedDecodeError):
        fp.decode_packet(b"\x01\x01" + b"\x00" * 48)  # 50 bytes < LTP_PACKET_SIZE
    with pytest.raises(fp.FeedDecodeError):
        fp.decode_packet("not bytes")  # type: ignore[arg-type]


def test_feed_decode_error_is_a_value_error() -> None:
    assert issubclass(fp.FeedDecodeError, ValueError)


# ── scale_price ───────────────────────────────────────────────────────────


def test_scale_price_converts_paise_to_rupees_and_keeps_none() -> None:
    assert fp.scale_price(LTP_PAISE, 100.0) == pytest.approx(250.50)
    assert fp.scale_price(None, 100.0) is None
    assert fp.scale_price(0, 100.0) == pytest.approx(0.0)


def test_scale_price_rejects_non_positive_scale() -> None:
    with pytest.raises(ValueError):
        fp.scale_price(100, 0)
    with pytest.raises(ValueError):
        fp.scale_price(100, -1.0)


# ── text frames ───────────────────────────────────────────────────────────


def test_parse_text_frame_returns_none_for_heartbeat_and_empty() -> None:
    assert fp.parse_text_frame("pong") is None
    assert fp.parse_text_frame("ping") is None
    assert fp.parse_text_frame("   ping  ") is None
    assert fp.parse_text_frame(b"pong") is None
    assert fp.parse_text_frame("") is None


def test_parse_text_frame_decodes_json_error_dict() -> None:
    payload = fp.parse_text_frame('{"correlationID":"x","errorCode":"E1002","errorMessage":"limit exceeded"}')
    assert isinstance(payload, dict)
    assert payload["errorCode"] == "E1002"
    assert fp.is_subscription_error(payload) is True
    assert fp.error_text(payload) == "limit exceeded"


def test_parse_text_frame_wraps_json_list() -> None:
    assert fp.parse_text_frame("[1, 2, 3]") == {"frames": [1, 2, 3]}


def test_parse_text_frame_non_json_is_returned_as_raw() -> None:
    assert fp.parse_text_frame("open") == {"raw": "open"}


def test_parse_text_frame_raises_on_unparseable_brace() -> None:
    with pytest.raises(fp.FeedDecodeError):
        fp.parse_text_frame("{not valid json")


def test_is_subscription_error_matches_documented_codes_and_rejects_plain() -> None:
    assert fp.is_subscription_error({"errorCode": "E1001"}) is True
    assert fp.is_subscription_error({"errorcode": "E1002"}) is True
    assert fp.is_subscription_error({"errorCode": ""}) is False
    assert fp.is_subscription_error({}) is False
    assert fp.error_text({}) == "unknown feed error"


# ── subscription frames ───────────────────────────────────────────────────


def test_build_subscribe_message_matches_documented_contract() -> None:
    message = fp.build_subscribe_message(
        correlation_id="abcde12345", mode=fp.MODE_QUOTE, tokens=["2885"], exchange_type=fp.EXCHANGE_NSE_CM
    )
    assert message == {
        "correlationID": "abcde12345",
        "action": 1,
        "params": {"mode": 2, "tokenList": [{"exchangeType": 1, "tokens": ["2885"]}]},
    }


def test_build_unsubscribe_message_uses_action_zero() -> None:
    message = fp.build_unsubscribe_message(correlation_id="x", mode=fp.MODE_QUOTE, tokens=["1"])
    assert message["action"] == 0


def test_encode_subscribe_request_truncates_correlation_id_to_ten_chars() -> None:
    encoded = fp.encode_subscribe_request(
        correlation_id="0123456789abcdefghij", mode=fp.MODE_QUOTE, tokens=["2885"]
    )
    decoded = json.loads(encoded)
    assert decoded["correlationID"] == "0123456789"
    assert len(decoded["correlationID"]) == 10
    # Compact JSON: no spaces around separators.
    assert ", " not in encoded and '": ' not in encoded


def test_encode_subscribe_request_defaults_and_validation() -> None:
    with pytest.raises(ValueError):
        fp.encode_subscribe_request(mode=fp.MODE_LTP)  # tokens required
    default = json.loads(fp.encode_subscribe_request(correlation_id="", mode=fp.MODE_LTP, tokens=["1"]))
    assert default["correlationID"] == "scanner001"
    assert default["params"]["tokenList"][0]["exchangeType"] == fp.EXCHANGE_NSE_CM

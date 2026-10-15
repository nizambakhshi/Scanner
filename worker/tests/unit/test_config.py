"""Unit tests for ``worker.config`` — env loading, coercion and validation.

Every case feeds an explicit mapping (never ``os.environ``) with
``load_dotenv_file=False`` so results are deterministic and independent of any
developer ``.env``. The security-critical assertions prove that secrets never
leak into ``repr(settings)`` or the ``safe_summary`` payload.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from worker import config as C

# Distinctive sentinel secrets: if any of these ever appears in a repr or the
# safe summary the redaction contract is broken.
API_KEY = "SECRET-APIKEY-XYZ"
PIN = "SECRET-PIN-1234"
TOTP = "SECRET-TOTP-ABCD"
REFRESH = "SECRET-REFRESH-TOKEN"
BOT_TOKEN = "SECRET-TELEGRAM-BOT-TOKEN"
DB_URL = "postgresql://user:SUPERSECRET@host:5432/db"


def live_env(**overrides: str) -> Mapping[str, str]:
    base = {
        "SCAN_MODE": "live",
        "SIMULATION_LABEL": "false",
        "DATABASE_URL": DB_URL,
        "ANGEL_API_KEY": API_KEY,
        "ANGEL_CLIENT_CODE": "ZZ12345",
        "ANGEL_PIN": PIN,
        "ANGEL_TOTP_SECRET": TOTP,
        "ANGEL_REFRESH_TOKEN": REFRESH,
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "TELEGRAM_CHAT_ID": "chat-1",
    }
    base.update(overrides)
    return base


def load(env: Mapping[str, str]) -> C.WorkerSettings:
    return C.load_settings(dict(env), load_dotenv_file=False)


# ── mode + label agreement ────────────────────────────────────────────────


def test_simulation_loads_without_any_credentials() -> None:
    settings = load({"SCAN_MODE": "simulation"})
    assert settings.scan_mode == "simulation"
    assert settings.simulation_label is True  # defaults to the scan mode
    assert settings.is_live is False
    assert settings.notifications_enabled is False, "simulation never sends live alerts"


def test_simulation_without_label_is_rejected() -> None:
    with pytest.raises(C.ConfigError, match="SCAN_MODE=simulation requires SIMULATION_LABEL"):
        load({"SCAN_MODE": "simulation", "SIMULATION_LABEL": "false"})


def test_live_with_simulation_label_is_rejected() -> None:
    with pytest.raises(C.ConfigError, match="SCAN_MODE=live requires SIMULATION_LABEL"):
        load(live_env(SIMULATION_LABEL="true"))


def test_unknown_scan_mode_is_rejected() -> None:
    with pytest.raises(C.ConfigError, match="SCAN_MODE must be"):
        load({"SCAN_MODE": "paper"})


# ── required-value checks ─────────────────────────────────────────────────


def test_live_missing_required_keys_names_them() -> None:
    with pytest.raises(C.ConfigError) as excinfo:
        load({"SCAN_MODE": "live", "SIMULATION_LABEL": "false"})
    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "ANGEL_API_KEY" in message


def test_full_live_env_loads() -> None:
    settings = load(live_env())
    assert settings.is_live is True
    assert settings.notifications_enabled is True
    assert settings.angel.configured is True


# ── scanner parameter validation ──────────────────────────────────────────


def test_ranking_weights_must_sum_to_one() -> None:
    with pytest.raises(C.ConfigError, match="sum to 1.0"):
        load({"SCAN_MODE": "simulation", "RANK_WEIGHT_PRICE_STRENGTH": "0.5"})
    with pytest.raises(C.ConfigError, match="sum to 1.0"):
        C.ScannerParams(
            rank_weight_price_strength=0.1,
            rank_weight_relative_volume=0.1,
            rank_weight_vwap_strength=0.1,
            rank_weight_ema_spread=0.1,
        ).validate()


def test_scanner_range_and_type_validation() -> None:
    with pytest.raises(C.ConfigError, match="FAST_EMA_PERIOD must be >= 2"):
        C.ScannerParams(fast_ema_period=1, slow_ema_period=2).validate()
    with pytest.raises(C.ConfigError, match="SLOW_EMA_PERIOD must be greater"):
        C.ScannerParams(fast_ema_period=20, slow_ema_period=20).validate()
    with pytest.raises(C.ConfigError, match="MAX_SUBSCRIBED_INSTRUMENTS"):
        C.ScannerParams(max_subscribed_instruments=5000).validate()


def test_non_integer_and_non_boolean_env_are_rejected() -> None:
    with pytest.raises(C.ConfigError, match="must be an integer"):
        load({"SCAN_MODE": "simulation", "FAST_EMA_PERIOD": "lots"})
    with pytest.raises(C.ConfigError, match="must be a boolean"):
        load({"SCAN_MODE": "simulation", "SCANNER_ENABLED": "sometimes"})


def test_subscription_beyond_session_quota_is_rejected_in_live() -> None:
    with pytest.raises(C.ConfigError, match="exceeds ANGEL_MAX_TOKENS_PER_SESSION"):
        load(live_env(MAX_SUBSCRIBED_INSTRUMENTS="900", ANGEL_MAX_TOKENS_PER_SESSION="500"))


def test_defaults_pass_validation_and_are_coerced() -> None:
    params = C.ScannerParams.from_env({"UNIVERSE": "nifty500", "FAST_EMA_PERIOD": "12"})
    assert params.universe == "NIFTY500"  # upper-cased
    assert params.fast_ema_period == 12
    params.validate()
    assert params.warmup_candles_required == max(params.slow_ema_period, params.volume_lookback)


def test_macd_defaults_parsing_and_validation() -> None:
    defaults = C.ScannerParams.from_env({})
    assert (defaults.macd_fast_period, defaults.macd_slow_period, defaults.macd_signal_period) == (
        12,
        26,
        9,
    )
    parsed = C.ScannerParams.from_env(
        {"MACD_FAST_PERIOD": "8", "MACD_SLOW_PERIOD": "21", "MACD_SIGNAL_PERIOD": "5"}
    )
    assert (parsed.macd_fast_period, parsed.macd_slow_period, parsed.macd_signal_period) == (8, 21, 5)
    parsed.validate()
    with pytest.raises(C.ConfigError, match="MACD_FAST_PERIOD must be >= 2"):
        C.ScannerParams(macd_fast_period=1).validate()
    with pytest.raises(C.ConfigError, match="MACD_SLOW_PERIOD must be greater"):
        C.ScannerParams(macd_fast_period=26, macd_slow_period=26).validate()
    with pytest.raises(C.ConfigError, match="MACD_SIGNAL_PERIOD must be >= 2"):
        C.ScannerParams(macd_signal_period=1).validate()


def test_atr_defaults_parsing_and_validation() -> None:
    defaults = C.ScannerParams.from_env({})
    assert defaults.atr_period == 14
    parsed = C.ScannerParams.from_env({"ATR_PERIOD": "7"})
    assert parsed.atr_period == 7
    parsed.validate()
    with pytest.raises(C.ConfigError, match="ATR_PERIOD must be >= 2"):
        C.ScannerParams(atr_period=1).validate()


def test_scanner_config_public_shape_is_unchanged() -> None:
    # The config snapshot the heartbeat and dashboard consume keeps exactly the
    # keys it had before Phases 2B/2C: the MACD and ATR knobs live on the env
    # side for now and must not leak into the public shape until the DB columns
    # land.
    assert set(C.ScannerParams().to_dict()) == {
        "universe",
        "fast_ema_period",
        "slow_ema_period",
        "volume_lookback",
        "relative_volume_threshold",
        "alert_cooldown_minutes",
        "removal_confirm_candles",
        "feed_staleness_threshold_seconds",
        "candle_grace_seconds",
        "initial_scan_candles",
        "max_subscribed_instruments",
        "rank_weights",
        "is_enabled",
        "config_version",
    }
    assert set(C.ScannerParams().to_dict()["rank_weights"]) == {
        "price_strength",
        "relative_volume",
        "vwap_strength",
        "ema_spread",
    }


# ── angel / telegram / simulation sub-configs ─────────────────────────────


def test_angel_capability_flags_and_validation() -> None:
    login_only = C.AngelSettings(api_key="k", client_code="c", pin="p", totp_secret="t")
    assert login_only.can_login is True
    assert login_only.configured is True
    renew_only = C.AngelSettings(api_key="k", client_code="c", refresh_token="r")
    assert renew_only.can_login is False and renew_only.can_renew is True
    with pytest.raises(C.ConfigError):
        C.AngelSettings(feed_mode=99).validate()
    with pytest.raises(C.ConfigError):
        C.AngelSettings(base_url="http://insecure").validate()
    assert C.base_url_is_https("https://x") and not C.base_url_is_https("http://x")


def test_telegram_and_simulation_validation() -> None:
    with pytest.raises(C.ConfigError):
        C.TelegramSettings(max_attempts=0).validate()
    with pytest.raises(C.ConfigError):
        C.SimulationSettings(replay_speed=0).validate()
    with pytest.raises(C.ConfigError):
        C.SimulationSettings(symbols=10_000).validate()


def test_heartbeat_and_lease_rules() -> None:
    with pytest.raises(C.ConfigError, match="WORKER_HEARTBEAT_SECONDS"):
        load({"SCAN_MODE": "simulation", "WORKER_HEARTBEAT_SECONDS": "1"})
    with pytest.raises(C.ConfigError, match="SESSION_LEASE_SECONDS must exceed"):
        load({"SCAN_MODE": "simulation", "WORKER_HEARTBEAT_SECONDS": "20", "SESSION_LEASE_SECONDS": "20"})


def test_with_scanner_returns_copy_with_new_params() -> None:
    settings = load({"SCAN_MODE": "simulation"})
    updated = settings.with_scanner(C.ScannerParams(fast_ema_period=30, slow_ema_period=50))
    assert updated.scanner.fast_ema_period == 30
    assert settings.scanner.fast_ema_period != 30  # original untouched


# ── secret redaction (security contract) ──────────────────────────────────


@pytest.mark.parametrize(
    "secret",
    [PIN, TOTP, API_KEY, BOT_TOKEN, DB_URL, REFRESH],
    ids=["pin", "totp", "api_key", "telegram", "database", "refresh"],
)
def test_secrets_are_absent_from_repr(secret: str) -> None:
    settings = load(live_env())
    assert secret not in repr(settings)


@pytest.mark.parametrize(
    "secret",
    [PIN, TOTP, API_KEY, BOT_TOKEN, DB_URL, REFRESH],
    ids=["pin", "totp", "api_key", "telegram", "database", "refresh"],
)
def test_secrets_are_absent_from_safe_summary(secret: str) -> None:
    settings = load(live_env())
    summary = json.dumps(C.safe_summary(settings))
    assert secret not in summary


def test_safe_summary_only_exposes_non_secret_shape() -> None:
    summary = C.safe_summary(load(live_env()))
    assert summary["scan_mode"] == "live"
    assert summary["database_configured"] is True
    assert summary["angel_configured"] is True
    assert summary["notifications_enabled"] is True
    assert "DATABASE_URL" not in summary
    assert "scanner" in summary and "rank_weights" in summary["scanner"]

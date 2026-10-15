"""Startup configuration: load, coerce and validate every setting.

Rules enforced here (see master spec §18.4):

* required values are checked at startup and a missing one raises
  :class:`ConfigError` naming exactly which variables are absent — never a
  traceback about an undefined key later in the request path;
* secrets are ``repr=False`` so they cannot leak into logs or exception text;
* ``SCAN_MODE`` and ``SIMULATION_LABEL`` must agree, because a mislabelled
  simulation is indistinguishable from a live signal in the alert stream;
* scanner numeric parameters are range-checked, so a typo cannot silently
  disable the scanner (e.g. a negative threshold that everything passes).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULTS_ENV_KEYS = (
    "FAST_EMA_PERIOD",
    "SLOW_EMA_PERIOD",
    "MACD_FAST_PERIOD",
    "MACD_SLOW_PERIOD",
    "MACD_SIGNAL_PERIOD",
    "ATR_PERIOD",
    "VOLUME_LOOKBACK",
    "RELATIVE_VOLUME_THRESHOLD",
    "ALERT_COOLDOWN_MINUTES",
    "REMOVAL_CONFIRM_CANDLES",
    "FEED_STALENESS_SECONDS",
    "CANDLE_GRACE_SECONDS",
    "INITIAL_SCAN_CANDLES",
    "MAX_SUBSCRIBED_INSTRUMENTS",
    "UNIVERSE",
)


class ConfigError(RuntimeError):
    """Raised when the process environment cannot produce a valid config."""


def _load_dotenv_if_present() -> None:
    """Populate os.environ from the nearest .env for local development.

    Production (Railway) injects real environment variables and has no .env
    file, so this is a no-op there. Existing environment values always win.
    """
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:  # pragma: no cover - optional in prod images
        return
    here = Path(__file__).resolve()
    for candidate in (here.parents[2] / ".env", here.parents[1] / ".env", Path(".env")):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def _get(raw: Mapping[str, str], key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _int(raw: Mapping[str, str], key: str, default: int) -> int:
    text = _get(raw, key) or str(default)
    try:
        return int(text)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {text!r}") from exc


def _float(raw: Mapping[str, str], key: str, default: float) -> float:
    text = _get(raw, key) or str(default)
    try:
        return float(text)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {text!r}") from exc


def _bool(raw: Mapping[str, str], key: str, default: bool) -> bool:
    text = (_get(raw, key) or str(default)).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean, got {text!r}")


def _require(raw: Mapping[str, str], keys: list[str], missing: list[str]) -> str:
    value = _get(raw, keys[0])
    if not value:
        missing.append(" / ".join(keys))
    return value


@dataclass(frozen=True)
class ScannerParams:
    """Tunable scanner strategy parameters (spec §10.4).

    These are the *defaults*. The authoritative values live in the
    ``scanner_configs`` table and are versioned, so historical signals stay
    interpretable after a change.
    """

    universe: str = "NIFTY500"
    fast_ema_period: int = 9
    slow_ema_period: int = 20
    # MACD(12, 26, 9) for the intraday engine, on the same five-minute closes
    # as the RSI-5m. Defaults follow the classic definition. These knobs are
    # env/configurable now; the saved ``scanner_configs`` rows carry no MACD
    # columns yet, so a row adopted from the dashboard supplies the defaults
    # until the schema change that accompanies MACD-based detection lands.
    macd_fast_period: int = 12
    macd_slow_period: int = 26
    macd_signal_period: int = 9
    # Wilder ATR(14) on today's one-minute candles: an input to stop distance
    # and extension measurement (audit D5, Step 2), never a hard-coded
    # threshold. Same env-only status as the MACD knobs until the setup
    # engine's columns land.
    atr_period: int = 14
    volume_lookback: int = 20
    relative_volume_threshold: float = 1.5
    alert_cooldown_minutes: int = 15
    removal_confirm_candles: int = 3
    feed_staleness_seconds: int = 90
    candle_grace_seconds: int = 3
    initial_scan_candles: int = 5
    max_subscribed_instruments: int = 500
    rank_weight_price_strength: float = 0.30
    rank_weight_relative_volume: float = 0.30
    rank_weight_vwap_strength: float = 0.20
    rank_weight_ema_spread: float = 0.20
    is_enabled: bool = True
    config_version: int = 1

    @classmethod
    def from_env(cls, raw: Mapping[str, str]) -> ScannerParams:
        return cls(
            universe=(_get(raw, "UNIVERSE") or "NIFTY500").upper(),
            fast_ema_period=_int(raw, "FAST_EMA_PERIOD", 9),
            slow_ema_period=_int(raw, "SLOW_EMA_PERIOD", 20),
            macd_fast_period=_int(raw, "MACD_FAST_PERIOD", 12),
            macd_slow_period=_int(raw, "MACD_SLOW_PERIOD", 26),
            macd_signal_period=_int(raw, "MACD_SIGNAL_PERIOD", 9),
            atr_period=_int(raw, "ATR_PERIOD", 14),
            volume_lookback=_int(raw, "VOLUME_LOOKBACK", 20),
            relative_volume_threshold=_float(raw, "RELATIVE_VOLUME_THRESHOLD", 1.5),
            alert_cooldown_minutes=_int(raw, "ALERT_COOLDOWN_MINUTES", 15),
            removal_confirm_candles=_int(raw, "REMOVAL_CONFIRM_CANDLES", 3),
            feed_staleness_seconds=_int(raw, "FEED_STALENESS_SECONDS", 90),
            candle_grace_seconds=_int(raw, "CANDLE_GRACE_SECONDS", 3),
            initial_scan_candles=_int(raw, "INITIAL_SCAN_CANDLES", 5),
            max_subscribed_instruments=_int(raw, "MAX_SUBSCRIBED_INSTRUMENTS", 500),
            rank_weight_price_strength=_float(raw, "RANK_WEIGHT_PRICE_STRENGTH", 0.30),
            rank_weight_relative_volume=_float(raw, "RANK_WEIGHT_RELATIVE_VOLUME", 0.30),
            rank_weight_vwap_strength=_float(raw, "RANK_WEIGHT_VWAP_STRENGTH", 0.20),
            rank_weight_ema_spread=_float(raw, "RANK_WEIGHT_EMA_SPREAD", 0.20),
            is_enabled=_bool(raw, "SCANNER_ENABLED", True),
        )

    def validate(self) -> None:
        errors: list[str] = []
        if self.universe not in ("NIFTY500", "FNO"):
            errors.append("UNIVERSE must be NIFTY500 or FNO")
        if self.fast_ema_period < 2:
            errors.append("FAST_EMA_PERIOD must be >= 2")
        if self.slow_ema_period <= self.fast_ema_period:
            errors.append("SLOW_EMA_PERIOD must be greater than FAST_EMA_PERIOD")
        if self.macd_fast_period < 2:
            errors.append("MACD_FAST_PERIOD must be >= 2")
        if self.macd_slow_period <= self.macd_fast_period:
            errors.append("MACD_SLOW_PERIOD must be greater than MACD_FAST_PERIOD")
        if self.macd_signal_period < 2:
            errors.append("MACD_SIGNAL_PERIOD must be >= 2")
        if self.atr_period < 2:
            errors.append("ATR_PERIOD must be >= 2")
        if self.volume_lookback < 2:
            errors.append("VOLUME_LOOKBACK must be >= 2")
        if self.relative_volume_threshold <= 0:
            errors.append("RELATIVE_VOLUME_THRESHOLD must be > 0")
        if self.alert_cooldown_minutes < 0:
            errors.append("ALERT_COOLDOWN_MINUTES must be >= 0")
        if self.removal_confirm_candles < 1:
            errors.append("REMOVAL_CONFIRM_CANDLES must be >= 1")
        if not 5 <= self.feed_staleness_seconds <= 3600:
            errors.append("FEED_STALENESS_SECONDS must be between 5 and 3600")
        if not 0 <= self.candle_grace_seconds <= 30:
            errors.append("CANDLE_GRACE_SECONDS must be between 0 and 30")
        if self.initial_scan_candles < 1:
            errors.append("INITIAL_SCAN_CANDLES must be >= 1")
        if not 1 <= self.max_subscribed_instruments <= 1000:
            errors.append("MAX_SUBSCRIBED_INSTRUMENTS must be between 1 and 1000")
        weights = (
            self.rank_weight_price_strength
            + self.rank_weight_relative_volume
            + self.rank_weight_vwap_strength
            + self.rank_weight_ema_spread
        )
        if abs(weights - 1.0) > 1e-6:
            errors.append(f"ranking weights must sum to 1.0 (got {weights:.4f})")
        if errors:
            raise ConfigError("invalid scanner configuration: " + "; ".join(errors))

    @property
    def warmup_candles_required(self) -> int:
        """Completed candles needed before every condition is evaluable."""
        return max(self.slow_ema_period, self.volume_lookback)

    def to_dict(self) -> dict[str, object]:
        """Public shape stored as the DB config snapshot (no secrets)."""
        return {
            "universe": self.universe,
            "fast_ema_period": self.fast_ema_period,
            "slow_ema_period": self.slow_ema_period,
            "volume_lookback": self.volume_lookback,
            "relative_volume_threshold": self.relative_volume_threshold,
            "alert_cooldown_minutes": self.alert_cooldown_minutes,
            "removal_confirm_candles": self.removal_confirm_candles,
            "feed_staleness_threshold_seconds": self.feed_staleness_seconds,
            "candle_grace_seconds": self.candle_grace_seconds,
            "initial_scan_candles": self.initial_scan_candles,
            "max_subscribed_instruments": self.max_subscribed_instruments,
            "rank_weights": {
                "price_strength": self.rank_weight_price_strength,
                "relative_volume": self.rank_weight_relative_volume,
                "vwap_strength": self.rank_weight_vwap_strength,
                "ema_spread": self.rank_weight_ema_spread,
            },
            "is_enabled": self.is_enabled,
            "config_version": self.config_version,
        }


@dataclass(frozen=True)
class AngelSettings:
    """SmartAPI credentials. Every secret here is ``repr=False``."""

    api_key: str = field(default="", repr=False)
    client_code: str = ""
    pin: str = field(default="", repr=False)
    totp_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    feed_token: str = field(default="", repr=False)
    base_url: str = "https://apiconnect.angelone.in"
    ws_url: str = "wss://smartapisocket.angelone.in/smart-stream"
    feed_mode: int = 2
    price_scale: float = 100.0
    max_tokens_per_message: int = 250
    max_tokens_per_session: int = 1000
    reauth_minutes: int = 210
    request_timeout_seconds: float = 12.0
    # SEBI-mandated identity headers (X-ClientLocalIP / X-ClientPublicIP /
    # X-MACAddress) are required since 2025. They are auto-detected when unset;
    # a fixed public IP can be supplied when the host has one.
    public_ip: str = ""
    local_ip: str = ""
    mac_address: str = ""
    instrument_master_url: str = (
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    )
    historical_warmup_days: int = 3

    @property
    def can_login(self) -> bool:
        return bool(self.api_key and self.client_code and self.pin and self.totp_secret)

    @property
    def can_renew(self) -> bool:
        return bool(self.api_key and self.client_code and self.refresh_token)

    @property
    def configured(self) -> bool:
        return self.can_login or self.can_renew

    def validate(self) -> None:
        if self.feed_mode not in (1, 2, 3):
            raise ConfigError("ANGEL_FEED_MODE must be 1 (LTP), 2 (QUOTE) or 3 (SNAP_QUOTE)")
        if self.price_scale <= 0:
            raise ConfigError("ANGEL_PRICE_SCALE must be > 0")
        if not 1 <= self.max_tokens_per_message <= 500:
            raise ConfigError("ANGEL_MAX_TOKENS_PER_MESSAGE must be between 1 and 500")
        if not 1 <= self.max_tokens_per_session <= 1000:
            raise ConfigError("ANGEL_MAX_TOKENS_PER_SESSION must be between 1 and 1000")
        if not base_url_is_https(self.base_url):
            raise ConfigError("ANGEL_BASE_URL must be an https:// URL")
        if not self.ws_url.startswith("wss://"):
            raise ConfigError("ANGEL_WS_URL must be a wss:// URL")
        if not 2 <= self.request_timeout_seconds <= 120:
            raise ConfigError("ANGEL_REQUEST_TIMEOUT_SECONDS must be between 2 and 120")
        if not 30 <= self.reauth_minutes <= 1380:
            # A SmartAPI JWT lives ~24 h; renewing outside this window would
            # either hammer the token endpoint or connect with an expired JWT.
            raise ConfigError("ANGEL_REAUTH_MINUTES must be between 30 and 1380")


def base_url_is_https(value: str) -> bool:
    return value.startswith("https://")


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str = field(default="", repr=False)
    chat_id: str = ""
    api_url: str = "https://api.telegram.org"
    max_attempts: int = 5
    retry_base_seconds: int = 10

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("TELEGRAM_MAX_ATTEMPTS must be >= 1")
        if self.retry_base_seconds < 1:
            raise ConfigError("TELEGRAM_RETRY_BASE_SECONDS must be >= 1")
        if not self.api_url.startswith("https://"):
            raise ConfigError("TELEGRAM_API_URL must be an https:// URL")


@dataclass(frozen=True)
class SimulationSettings:
    replay_speed: float = 60.0
    symbols: int = 25
    seed: int = 42

    def validate(self) -> None:
        if self.replay_speed <= 0:
            raise ConfigError("SIMULATION_REPLAY_SPEED must be > 0")
        if not 1 <= self.symbols <= 500:
            raise ConfigError("SIMULATION_SYMBOLS must be between 1 and 500")


@dataclass(frozen=True)
class WorkerSettings:
    scan_mode: str  # "live" | "simulation"
    simulation_label: bool
    timezone: str
    app_url: str
    database_url: str = field(default="", repr=False)
    log_level: str = "info"
    log_format: str = "console"
    worker_id: str = "worker-local"
    heartbeat_seconds: int = 20
    session_lease_seconds: int = 300
    # Intraday candles lose their value once the session they describe is
    # finalized — retention bounds the hot tables, the warmup window feeds
    # them. Warmup days (AngelSettings) may not exceed this: fetching history
    # that the next purge immediately deletes is just broker API spend.
    # This is only the fallback: the dashboard's `ops_settings['retention']`
    # wins at purge time (see `_retention_days`), and one complete session
    # (345 bars) already covers `warmup_candles_required`, so three days is a
    # window with slack rather than a shortage waiting to happen.
    retention_days: int = 3
    scanner: ScannerParams = field(default_factory=ScannerParams)
    angel: AngelSettings = field(default_factory=AngelSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    simulation: SimulationSettings = field(default_factory=SimulationSettings)

    @property
    def is_live(self) -> bool:
        return self.scan_mode == "live"

    @property
    def notifications_enabled(self) -> bool:
        """Telegram may only send in live mode with a configured bot."""
        return self.telegram.configured and not self.simulation_label

    def with_scanner(self, params: ScannerParams) -> WorkerSettings:
        """Return a copy using DB-backed scanner settings (versioned at runtime)."""
        return replace(self, scanner=params)


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    load_dotenv_file: bool = True,
) -> WorkerSettings:
    """Build and validate :class:`WorkerSettings` from the environment."""
    if environ is None:
        if load_dotenv_file:
            _load_dotenv_if_present()
        environ = os.environ

    raw = {str(k): str(v) for k, v in environ.items()}
    missing: list[str] = []

    scan_mode = (_get(raw, "SCAN_MODE") or "live").lower()
    if scan_mode not in {"live", "simulation"}:
        raise ConfigError("SCAN_MODE must be 'live' or 'simulation'")

    simulation_label = _bool(raw, "SIMULATION_LABEL", scan_mode == "simulation")
    if scan_mode == "simulation" and not simulation_label:
        raise ConfigError(
            "SCAN_MODE=simulation requires SIMULATION_LABEL=true; simulated signals "
            "must never be indistinguishable from live market signals"
        )
    if scan_mode == "live" and simulation_label:
        raise ConfigError(
            "SCAN_MODE=live requires SIMULATION_LABEL=false; live signals would "
            "otherwise be filed as simulated"
        )

    database_url = _get(raw, "DATABASE_URL")
    if scan_mode == "live" and not database_url:
        missing.append("DATABASE_URL")

    angel = AngelSettings(
        api_key=_get(raw, "ANGEL_API_KEY"),
        client_code=_get(raw, "ANGEL_CLIENT_CODE"),
        pin=_get(raw, "ANGEL_PIN"),
        totp_secret=_get(raw, "ANGEL_TOTP_SECRET"),
        refresh_token=_get(raw, "ANGEL_REFRESH_TOKEN"),
        feed_token=_get(raw, "ANGEL_FEED_TOKEN"),
        base_url=_get(raw, "ANGEL_BASE_URL", AngelSettings.base_url),
        ws_url=_get(raw, "ANGEL_WS_URL", AngelSettings.ws_url),
        feed_mode=_int(raw, "ANGEL_FEED_MODE", 2),
        price_scale=_float(raw, "ANGEL_PRICE_SCALE", 100.0),
        max_tokens_per_message=_int(raw, "ANGEL_MAX_TOKENS_PER_MESSAGE", 250),
        max_tokens_per_session=_int(raw, "ANGEL_MAX_TOKENS_PER_SESSION", 1000),
        reauth_minutes=_int(raw, "ANGEL_REAUTH_MINUTES", 210),
        request_timeout_seconds=_float(raw, "ANGEL_REQUEST_TIMEOUT_SECONDS", 12.0),
        public_ip=_get(raw, "ANGEL_PUBLIC_IP"),
        local_ip=_get(raw, "ANGEL_LOCAL_IP"),
        mac_address=_get(raw, "ANGEL_MAC_ADDRESS"),
        instrument_master_url=_get(raw, "INSTRUMENT_MASTER_URL", AngelSettings.instrument_master_url),
        historical_warmup_days=_int(raw, "HISTORICAL_WARMUP_DAYS", 3),
    )
    if scan_mode == "live" and not angel.configured:
        missing.append("ANGEL_API_KEY + ANGEL_CLIENT_CODE + (ANGEL_PIN + ANGEL_TOTP_SECRET or ANGEL_REFRESH_TOKEN)")

    telegram = TelegramSettings(
        bot_token=_get(raw, "TELEGRAM_BOT_TOKEN"),
        chat_id=_get(raw, "TELEGRAM_CHAT_ID"),
        api_url=_get(raw, "TELEGRAM_API_URL", TelegramSettings.api_url),
        max_attempts=_int(raw, "TELEGRAM_MAX_ATTEMPTS", 5),
        retry_base_seconds=_int(raw, "TELEGRAM_RETRY_BASE_SECONDS", 10),
    )
    if scan_mode == "live" and not telegram.configured:
        # Non-fatal: dashboard notifications still work, but say so loudly.
        raw.setdefault("TELEGRAM_WARN", "missing")

    scanner = ScannerParams.from_env(raw)
    simulation = SimulationSettings(
        replay_speed=_float(raw, "SIMULATION_REPLAY_SPEED", 60.0),
        symbols=_int(raw, "SIMULATION_SYMBOLS", 25),
        seed=_int(raw, "SIMULATION_SEED", 42),
    )

    settings = WorkerSettings(
        scan_mode=scan_mode,
        simulation_label=simulation_label,
        timezone=_get(raw, "TIMEZONE") or "Asia/Kolkata",
        app_url=_get(raw, "APP_URL", "http://localhost:3000"),
        database_url=database_url,
        log_level=(_get(raw, "LOG_LEVEL") or "info").lower(),
        log_format=(_get(raw, "LOG_FORMAT") or "console").lower(),
        worker_id=_get(raw, "WORKER_ID") or "worker-local",
        heartbeat_seconds=_int(raw, "WORKER_HEARTBEAT_SECONDS", 20),
        session_lease_seconds=_int(raw, "SESSION_LEASE_SECONDS", 300),
        retention_days=_int(raw, "RETENTION_DAYS", 3),
        scanner=scanner,
        angel=angel,
        telegram=telegram,
        simulation=simulation,
    )

    if missing:
        raise ConfigError(
            "missing required configuration: "
            + ", ".join(missing)
            + f" (SCAN_MODE={scan_mode}; see .env.example)"
        )

    scanner.validate()
    angel.validate()
    telegram.validate()
    simulation.validate()
    if settings.heartbeat_seconds < 5:
        raise ConfigError("WORKER_HEARTBEAT_SECONDS must be >= 5")
    if settings.session_lease_seconds <= settings.heartbeat_seconds:
        raise ConfigError("SESSION_LEASE_SECONDS must exceed WORKER_HEARTBEAT_SECONDS")
    if not 1 <= settings.retention_days <= 30:
        raise ConfigError("RETENTION_DAYS must be between 1 and 30")
    if scan_mode == "live" and angel.historical_warmup_days > settings.retention_days:
        raise ConfigError(
            "HISTORICAL_WARMUP_DAYS must not exceed RETENTION_DAYS; "
            "the purge would delete the history just fetched"
        )
    if scan_mode == "live" and scanner.max_subscribed_instruments > angel.max_tokens_per_session:
        raise ConfigError(
            "MAX_SUBSCRIBED_INSTRUMENTS exceeds ANGEL_MAX_TOKENS_PER_SESSION"
        )
    return settings


def safe_summary(settings: WorkerSettings) -> dict[str, object]:
    """Non-secret description of the active config, safe for logs and the API."""
    return {
        "scan_mode": settings.scan_mode,
        "simulation_label": settings.simulation_label,
        "timezone": settings.timezone,
        "worker_id": settings.worker_id,
        "database_configured": bool(settings.database_url),
        "angel_configured": settings.angel.configured,
        "telegram_configured": settings.telegram.configured,
        "notifications_enabled": settings.notifications_enabled,
        "feed_mode": settings.angel.feed_mode,
        "universe": settings.scanner.universe,
        "scanner": settings.scanner.to_dict(),
    }

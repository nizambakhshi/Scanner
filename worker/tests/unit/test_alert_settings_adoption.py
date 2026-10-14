"""The /alerts page must change what the worker actually runs.

``ops_settings`` is the dashboard's table and the worker's input: the page writes
``alert_strategies`` and ``telegram``, :meth:`WorkerRuntime._adopt_alert_settings`
reads them, and :meth:`WorkerRuntime._apply_alert_settings` pushes them into the
live pipeline, sender and alert engine without a restart. Before this existed the
equivalent risk was a config page whose values were decorative
(:mod:`tests.unit.test_config_adoption`); here the same promise is pinned for the
level breaks and the Telegram credentials.

The credential half of the promise is negative as well as positive: a bot token
that reaches this table must never reach a log line, which is the §19 redaction
rule re-proved at the one call site most tempted to break it.
"""

from __future__ import annotations

from dataclasses import replace

from worker.analytics.breakouts import DEFAULTS, StrategiesConfig
from worker.config import TelegramSettings, WorkerSettings
from worker.main import WorkerRuntime
from worker.models import Instrument
from worker.persistence.memory import InMemoryStore
from worker.timeutils import load_calendar, now_ist

SECRET = "123456:AA-token-that-must-never-be-printed"
ENV_TOKEN = "777:AA-environment-token"
CHAT = "-100987"


def settings_with(token: str = "", chat: str = "") -> WorkerSettings:
    return WorkerSettings(
        scan_mode="live",
        simulation_label=False,
        timezone="Asia/Kolkata",
        app_url="",
        telegram=TelegramSettings(bot_token=token, chat_id=chat),
    )


def runtime(store: InMemoryStore, *, token: str = "", chat: str = "") -> WorkerRuntime:
    return WorkerRuntime(
        settings=settings_with(token, chat), store=store, calendar=load_calendar()
    )


def document(**changes: object) -> dict:
    """The stored shape with a few knobs changed off the shipped defaults."""
    values = {key: DEFAULTS[key] for key in DEFAULTS}
    for name, patch in changes.items():
        values[name] = (
            replace(values[name], enabled=patch)
            if isinstance(patch, bool)
            else replace(values[name], **(patch or {}))
        )
    return StrategiesConfig(**values).to_dict()  # type: ignore[arg-type]


async def build_session(rt: WorkerRuntime, store: InMemoryStore) -> None:
    """Give the runtime a live pipeline, the way ``_run_session`` would."""
    window = rt.calendar.session(now_ist().date())
    assert window is not None, "the calendar must know today for this to be a session"
    session = await store.ensure_session(window.trading_date, window.open_at, window.close_at)
    instrument = Instrument(
        id=1,
        exchange="NSE",
        symbol="RELIANCE",
        trading_symbol="RELIANCE",
        company_name="Reliance Industries",
        instrument_token="2885",
        previous_close=100.0,
    )
    rt.session = session
    rt.window = window
    rt.pipeline = rt._build_pipeline(window, session, {1: instrument})


# ── the strategies document ─────────────────────────────────────────────────


async def test_no_row_means_the_shipped_defaults_and_the_environment() -> None:
    rt = runtime(InMemoryStore())

    summary = await rt._adopt_alert_settings()

    assert rt.strategies == StrategiesConfig()
    assert rt.strategies.any_enabled is True, "the product ships with all three armed"
    assert summary["strategies"] == "defaults"
    assert summary["telegram"] == "env"
    assert summary["telegram_configured"] is False


async def test_a_saved_document_replaces_the_defaults() -> None:
    store = InMemoryStore()
    store.ops_settings["alert_strategies"] = document(
        orb=False, vwap={"threshold_pct": 0.5, "cooldown_minutes": 30}
    )
    rt = runtime(store)

    summary = await rt._adopt_alert_settings()

    assert summary["strategies"] == "ops_settings"
    assert rt.strategies.orb.enabled is False
    assert rt.strategies.vwap.threshold_pct == 0.5
    assert rt.strategies.vwap.cooldown_minutes == 30
    assert rt.strategies.session_break == DEFAULTS["session_break"], "untouched strategies stay"


async def test_settings_adopted_before_the_pipeline_is_built_are_the_ones_it_runs() -> None:
    store = InMemoryStore()
    store.ops_settings["alert_strategies"] = document(
        session_break={"threshold_pct": 0.75}
    )
    rt = runtime(store)
    await rt._adopt_alert_settings()
    await build_session(rt, store)

    assert rt.pipeline is not None
    assert rt.pipeline.strategies.session_break.threshold_pct == 0.75
    assert rt.pipeline.breakouts.strategies is rt.pipeline.strategies


async def test_a_mid_session_save_reconfigures_the_running_tracker() -> None:
    store = InMemoryStore()
    rt = runtime(store)
    await rt._adopt_alert_settings()
    await build_session(rt, store)
    assert rt.pipeline is not None
    tracker = rt.pipeline.breakouts

    store.ops_settings["alert_strategies"] = document(orb=False)
    summary = await rt._adopt_alert_settings()

    assert summary["strategies"] == "ops_settings"
    assert rt.pipeline.breakouts is tracker, "the pipeline is reconfigured, not replaced"
    assert tracker.strategies.orb.enabled is False, (
        "the live detector must read the new document, not a copy"
    )


async def test_an_unparsable_document_keeps_the_last_good_one_and_says_so(
    caplog,
) -> None:
    store = InMemoryStore()
    rt = runtime(store)
    store.ops_settings["alert_strategies"] = document(vwap={"threshold_pct": 0.4})
    await rt._adopt_alert_settings()
    assert rt.strategies.vwap.threshold_pct == 0.4

    store.ops_settings["alert_strategies"] = {"vwap": {"threshold_pct": "not a number"}}
    caplog.clear()
    with caplog.at_level("INFO"):
        summary = await rt._adopt_alert_settings()

    assert rt.strategies.vwap.threshold_pct == 0.4, "the last obeyable document stays in force"
    assert summary["strategies"] == "ops_settings"
    assert "alert_strategies_invalid" in caplog.text, (
        "a page that reads armed while the detector obeys something else is the "
        "failure this feature cannot have, so the decline is logged, not silent"
    )


async def test_an_unknown_strategy_name_is_reported_without_breaking_the_rest(
    caplog,
) -> None:
    store = InMemoryStore()
    saved = document(session_break={"threshold_pct": 0.2})
    saved["strategies"]["some_future_strategy"] = {"enabled": True}
    store.ops_settings["alert_strategies"] = saved
    rt = runtime(store)

    with caplog.at_level("INFO"):
        await rt._adopt_alert_settings()

    assert rt.strategies.session_break.threshold_pct == 0.2, "known keys still apply"
    assert "alert_strategies_unknown" in caplog.text


# ── the Telegram document ───────────────────────────────────────────────────


async def test_credentials_from_the_database_reach_the_sender_and_the_engine(
    caplog,
) -> None:
    store = InMemoryStore()
    store.ops_settings["telegram"] = {
        "version": 1,
        "enabled": True,
        "bot_token": SECRET,
        "chat_id": CHAT,
    }
    rt = runtime(store, token=ENV_TOKEN, chat="-1ENV")
    await build_session(rt, store)
    alerts, sender = rt._alerts, rt._sender
    assert alerts is not None and sender is not None
    assert sender.settings.bot_token == ENV_TOKEN, "before adoption the env is in charge"

    caplog.clear()
    with caplog.at_level("INFO"):
        summary = await rt._adopt_alert_settings()

    assert summary["telegram"] == "ops_settings"
    assert summary["telegram_configured"] is True
    assert rt.telegram.chat_id == CHAT
    assert sender.settings.bot_token == SECRET, "the live sender was rebound, not rebuilt"
    assert alerts.telegram_enabled is True and alerts.telegram_chat_id == CHAT
    assert "telegram_credentials_applied" in caplog.text
    assert SECRET not in caplog.text, "the token never appears in a log line"
    assert ENV_TOKEN not in caplog.text
    assert CHAT not in caplog.text


async def test_a_saved_token_switched_off_pages_nobody() -> None:
    store = InMemoryStore()
    store.ops_settings["telegram"] = {
        "version": 1,
        "enabled": False,
        "bot_token": SECRET,
        "chat_id": CHAT,
    }
    rt = runtime(store, token=ENV_TOKEN, chat="-1ENV")
    await build_session(rt, store)

    summary = await rt._adopt_alert_settings()

    assert rt.telegram.configured is False, "switched off is switched off, env included"
    assert summary["telegram_configured"] is False
    assert rt._alerts is not None and rt._alerts.telegram_enabled is False


async def test_a_half_filled_row_leaves_the_environment_in_charge() -> None:
    store = InMemoryStore()
    store.ops_settings["telegram"] = {"version": 1, "enabled": True, "bot_token": "", "chat_id": ""}
    rt = runtime(store, token=ENV_TOKEN, chat="-1ENV")

    summary = await rt._adopt_alert_settings()

    assert rt.telegram.bot_token == ENV_TOKEN
    assert summary["telegram"] == "env"


async def test_deleting_the_row_returns_to_the_environment() -> None:
    store = InMemoryStore()
    store.ops_settings["telegram"] = {
        "enabled": True,
        "bot_token": SECRET,
        "chat_id": CHAT,
    }
    rt = runtime(store, token=ENV_TOKEN, chat="-1ENV")
    await rt._adopt_alert_settings()
    assert rt.telegram.bot_token == SECRET

    del store.ops_settings["telegram"]
    summary = await rt._adopt_alert_settings()

    assert rt.telegram.bot_token == ENV_TOKEN, "a deleted row is not a stale credential"
    assert summary["telegram"] == "env"


async def test_simulation_mode_never_enables_telegram_even_with_a_saved_token() -> None:
    store = InMemoryStore()
    store.ops_settings["telegram"] = {"enabled": True, "bot_token": SECRET, "chat_id": CHAT}
    rt = runtime(store)
    rt.settings = replace(rt.settings, scan_mode="simulation", simulation_label=True)
    await build_session(rt, store)

    await rt._adopt_alert_settings()

    assert rt._alerts is not None
    assert rt._alerts.telegram_enabled is False, "§12: a simulated day touches no provider"


async def test_a_broken_settings_read_leaves_the_detectors_running(caplog, monkeypatch) -> None:
    """The refresh loop catches, logs and carries on: the tape outranks a config hiccup.

    A database that will not answer the settings read must not stop the detectors —
    they keep running on the configuration already in force, and the failure is
    visible rather than silent.
    """
    store = InMemoryStore()
    store.ops_settings["alert_strategies"] = document(session_break={"threshold_pct": 0.3})
    rt = runtime(store)
    await rt._adopt_alert_settings()
    assert rt.strategies.session_break.threshold_pct == 0.3

    async def explode(key: str) -> dict:
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(store, "get_ops_setting", explode)
    caplog.clear()
    with caplog.at_level("INFO"):
        summary = await rt._adopt_alert_settings()

    assert rt.strategies.session_break.threshold_pct == 0.3, "the last good document stands"
    assert summary["strategies"] == "ops_settings"
    assert "alert_strategies_read_failed" in caplog.text
    assert "telegram_setting_read_failed" in caplog.text


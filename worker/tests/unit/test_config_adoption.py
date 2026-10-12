"""The session must run on the newest ``scanner_configs`` row, not on env defaults.

The dashboard's config page saves versioned qualification rows; before
``_adopt_scanner_config`` existed the worker ignored that table entirely and
the values on screen were decorative. These tests pin the three behaviours the
dashboard copy promises: a saved row wins, a missing row keeps the env-derived
defaults, and an invalid row is declined loudly instead of crashing the day.
"""

from __future__ import annotations

from worker.config import ScannerParams, WorkerSettings
from worker.main import WorkerRuntime
from worker.persistence.memory import InMemoryStore
from worker.timeutils import load_calendar


def runtime(store: InMemoryStore) -> WorkerRuntime:
    settings = WorkerSettings(
        scan_mode="live",
        simulation_label=False,
        timezone="Asia/Kolkata",
        app_url="",
    )
    return WorkerRuntime(settings=settings, store=store, calendar=load_calendar())


async def test_adopted_config_replaces_env_defaults() -> None:
    store = InMemoryStore()
    saved = await store.save_config(
        ScannerParams(
            universe="FNO",
            relative_volume_threshold=2.5,
            alert_cooldown_minutes=7,
            removal_confirm_candles=4,
            initial_scan_candles=8,
        ),
        created_by="test",
    )
    rt = runtime(store)

    await rt._adopt_scanner_config()

    assert rt.params.relative_volume_threshold == 2.5
    assert rt.params.alert_cooldown_minutes == 7
    assert rt.params.removal_confirm_candles == 4
    assert rt.params.initial_scan_candles == 8
    assert rt.params.universe == "FNO"
    assert rt._config_version == saved.config_version


async def test_missing_row_keeps_env_defaults() -> None:
    rt = runtime(InMemoryStore())

    await rt._adopt_scanner_config()

    assert rt.params is rt.settings.scanner
    assert rt._config_version is None


async def test_invalid_row_is_declined_not_fatal() -> None:
    store = InMemoryStore()
    # save_config writes without validating (validation is the settings layer's
    # job), which is exactly how a bad row could reach the table.
    await store.save_config(ScannerParams(fast_ema_period=20, slow_ema_period=9), created_by="test")
    rt = runtime(store)

    await rt._adopt_scanner_config()  # must not raise

    assert rt.params is rt.settings.scanner
    assert rt._config_version is None

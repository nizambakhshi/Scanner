"""Tests for the offline replay (``scanner-simulate``).

The replay exists to answer one question: *does the production pipeline behave
the way the spec says when no market is feeding it?* These tests are therefore
not about the report's formatting — they assert that the replay's own invariant
list comes back clean and that each mode tells the truth about what it did.
A change that breaks qualification, watchlist transitions, idempotency, outage
handling, simulation labelling or delivery draining surfaces here as a failed
invariant name, which is the point of the harness.
"""

from __future__ import annotations

import json
from typing import Any

from worker.simulation.run import (
    EXIT_OK,
    Simulation,
    build,
    main,
    parse_args,
    render,
    run_simulation,
)
from worker.simulation.scenarios import SCRIPTS

TRADING_DATE = "2026-10-12"  # a confirmed NSE trading day on the bundled calendar


async def replay(*flags: str) -> dict[str, Any]:
    """One full replay, pinned to a fixed trading date so runs are comparable."""
    return await run_simulation(parse_args(["--date", TRADING_DATE, *flags]))


def passed(report: dict[str, Any]) -> list[str]:
    return [row["name"] for row in report["invariants"] if row["status"] == "PASS"]


def failed(report: dict[str, Any]) -> list[str]:
    return [row["name"] for row in report["invariants"] if row["status"] == "FAIL"]


def assert_clean(report: dict[str, Any]) -> None:
    assert failed(report) == [], json.dumps(report["invariants"], indent=2, sort_keys=True)


# ── the replay's own verdict ────────────────────────────────────────────────


async def test_default_replay_violates_no_invariant() -> None:
    report = await replay()
    assert_clean(report)
    # guard against a harness that passes by checking nothing
    assert len(report["invariants"]) >= 13
    assert "every scenario demonstrated its rule" in passed(report)


async def test_replay_is_deterministic_for_a_seed() -> None:
    first = json.dumps(await replay("--seed=11"), sort_keys=True, default=str)
    second = json.dumps(await replay("--seed=11"), sort_keys=True, default=str)
    assert first == second


async def test_the_seed_actually_drives_the_scripts() -> None:
    report = await replay("--seed=7")
    resampled = await replay("--seed=99")
    assert_clean(report)
    assert_clean(resampled)
    assert report["candles"] == resampled["candles"]  # same shape...
    assert report["signals"] != resampled["signals"]  # ...different numbers


async def test_a_shorter_replay_is_reported_rather_than_silently_truncated() -> None:
    report = await replay("--minutes=30")
    assert report["minutes"] == 30
    # SIMREENTRY's re-qualification lives past minute 30: the script cannot
    # demonstrate it, and the harness must say so instead of passing vacuously.
    assert "every scenario demonstrated its rule" in failed(report)


# ── labelling honesty per mode ──────────────────────────────────────────────


async def test_simulated_mode_labels_every_row_and_never_queues_telegram() -> None:
    report = await replay()
    assert report["mode"] == "simulated"
    assert "SIMULATED DATA" in report["banner"]
    assert report["network_used"] is False
    assert report["store"] == "InMemoryStore"
    channels = {key.split(":")[0] for key in report["deliveries"]}
    assert channels == {"DASHBOARD"}
    assert all(row["simulated"] for row in report["memberships"])
    assert all(row["is_simulated"] for row in report["signals"])


async def test_dispatch_mode_uses_the_live_path_and_drains_the_queue() -> None:
    report = await replay("--dispatch")
    assert_clean(report)
    assert report["mode"] == "dispatch"
    assert "DISPATCH MODE" in report["banner"]
    assert report["deliveries"]["TELEGRAM:DELIVERED"] > 0
    assert "TELEGRAM:QUEUED" not in report["deliveries"]
    assert report["provider"]["kind"] == "local recorder"
    assert report["provider"]["messages_recorded"] == report["deliveries"]["TELEGRAM:DELIVERED"]
    assert all(not row["is_simulated"] for row in report["signals"])


async def test_dispatch_retries_a_failed_send_instead_of_losing_it() -> None:
    report = await replay("--dispatch", "--provider-failures=2")
    assert_clean(report)
    assert report["provider"]["provider_failures"] == 2
    assert sum(row["retrying"] for row in report["dispatch_passes"]) >= 1
    assert sum(row["permanently_failed"] for row in report["dispatch_passes"]) == 0
    assert "TELEGRAM:FAILED" not in report["deliveries"]
    assert "TELEGRAM:QUEUED" not in report["deliveries"]


# ── coverage of the documented rules ───────────────────────────────────────


async def test_silent_instrument_is_mapped_but_never_invented() -> None:
    report = await replay()
    assert report["coverage"]["instruments_without_any_candle"] == 1
    silent = next(row for row in report["scenarios"] if row["symbol"] == "SIMSILENT")
    assert silent["candles"] == 0
    assert silent["has_membership"] is False
    assert report["initial_scan"]["missing_data"] == 1


async def test_scripted_outage_freezes_transitions_and_is_counted_once() -> None:
    simulation: Simulation = await build(parse_args(["--date", TRADING_DATE]))
    await simulation.run()
    assert simulation.pipeline.counters.outages == 1
    assert simulation.outage_started_at and simulation.outage_ended_at
    inside = [
        event
        for event in simulation.store.events
        if simulation.outage_started_at <= event.event_timestamp <= simulation.outage_ended_at
    ]
    inside += [
        signal
        for signal in simulation.store.signals.values()
        if simulation.outage_started_at <= signal.event_timestamp <= simulation.outage_ended_at
        and signal.instrument_id is not None
    ]
    assert inside == []
    assert simulation.stale_seconds_recorded() > 0


async def test_no_outage_replay_keeps_the_session_clean() -> None:
    report = await replay("--no-outage")
    assert_clean(report)
    assert report["counters"]["outages"] == 0
    assert report["session"]["stale_data_seconds"] == 0


async def test_initial_scan_ranks_every_qualifier_it_sees() -> None:
    # A cold replay store only becomes warm after ``warmup_candles_required``
    # candles, so the scan is deliberately moved past the first entries to
    # exercise the ranking path (§11.1) the scheduled live scan takes for granted.
    report = await replay("--scan-minute=24")
    assert_clean(report)
    ranked = report["initial_scan_ranked"]
    assert ranked
    assert report["initial_scan"]["qualified"] == len(ranked)
    stored = sorted(row["rank"] for row in report["memberships"] if row["rank"] is not None)
    assert stored == list(range(1, len(ranked) + 1))


async def test_every_scenario_demonstrates_its_documented_rule() -> None:
    report = await replay()
    by_symbol = {row["symbol"]: row for row in report["scenarios"]}
    assert set(by_symbol) == {script.symbol for script in SCRIPTS}
    for script in SCRIPTS:
        row = by_symbol[script.symbol]
        if script.expect:
            assert set(script.expect) <= set(row["observed"]), row
        else:
            assert row["observed"] == [], f"{script.symbol} must never be tracked"
            assert row["has_membership"] is False


async def test_removal_and_requalification_keep_their_event_history() -> None:
    report = await replay()
    reentry = next(row for row in report["scenarios"] if row["symbol"] == "SIMREENTRY")
    for transition in ("ADDED", "WEAKENING", "REMOVED", "REQUALIFIED"):
        assert transition in reentry["observed"]
    row = next(item for item in report["memberships"] if item["symbol"] == "SIMREENTRY")
    assert row["episodes"] == 2  # appended, never overwritten (§11.7)


# ── the CLI surface itself ──────────────────────────────────────────────────


def test_flags_map_onto_their_destinations() -> None:
    """Regression: ``--cooldown``/``--json`` once read the wrong Namespace attrs."""
    parsed = parse_args(["--cooldown", "9", "--json", "--no-outage", "--provider-failures", "3"])
    assert parsed.cooldown_minutes == 9
    assert parsed.output_json is True
    assert parsed.outage_minute is None
    assert parsed.provider_failures == 3


def test_list_scenarios_exits_cleanly() -> None:
    assert main(["--list-scenarios"]) == EXIT_OK


async def test_rendered_report_shows_the_session_window_and_its_verdict() -> None:
    """Regression: slicing the stamp's tail printed ``15:00 -> 30:00``."""
    text = render(await replay())
    assert f"{TRADING_DATE}  09:15 -> 15:30" in text
    assert "invariants" in text and "invariants passed" in text
    assert "[FAIL]" not in text

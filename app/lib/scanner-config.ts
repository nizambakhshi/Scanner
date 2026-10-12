/**
 * Scanner-config writes: the dashboard's one path into `scanner_configs`.
 *
 * The config page used to be strictly read-only ("changing what the scanner
 * looks for should be a deploy, not a click"); the operator has since decided
 * the other way, so this module keeps the guarantees that decision must not
 * lose: a save never overwrites history — it inserts a NEW row with the next
 * `config_version`, copying every non-editable column from the active row, so
 * a signal can always be traced back to the exact thresholds that produced it.
 * The insert and its `audit_logs` journal row (who saved what, from where,
 * with the old→new diff) are one SQL statement and cannot land separately.
 *
 * The worker adopts the newest row when it OPENS its next session — see
 * `_adopt_scanner_config` in the worker. A save therefore changes what the
 * next session qualifies on, not what the running session is using; the
 * editor says so next to the button.
 */

import { query, type Raw } from "./db";

export type ConfigEditValues = {
  fast_ema_period: number;
  slow_ema_period: number;
  volume_lookback: number;
  relative_volume_threshold: number;
  alert_cooldown_minutes: number;
  removal_confirm_candles: number;
  initial_scan_candles: number;
};

export type ParsedEdits = { ok: true; values: ConfigEditValues } | { ok: false; error: string };

const EDITABLE_KEYS = [
  "fast_ema_period",
  "slow_ema_period",
  "volume_lookback",
  "relative_volume_threshold",
  "alert_cooldown_minutes",
  "removal_confirm_candles",
  "initial_scan_candles",
] as const;

const intField = (form: Record<string, string>, key: keyof ConfigEditValues): number | null => {
  const value = Math.trunc(Number(form[key]));
  return Number.isFinite(value) ? value : null;
};

/**
 * The same rules the worker's `ScannerParams.validate` enforces, applied
 * before anything reaches the database — a save that would make the worker
 * decline its own config is a 400, not a broken session.
 */
export function parseConfigEdits(form: Record<string, string>): ParsedEdits {
  const fast = intField(form, "fast_ema_period");
  const slow = intField(form, "slow_ema_period");
  const lookback = intField(form, "volume_lookback");
  const rv = Number(form.relative_volume_threshold);
  const cooldown = intField(form, "alert_cooldown_minutes");
  const removal = intField(form, "removal_confirm_candles");
  const initialScan = intField(form, "initial_scan_candles");
  if (
    fast === null ||
    slow === null ||
    lookback === null ||
    cooldown === null ||
    removal === null ||
    initialScan === null ||
    !Number.isFinite(rv)
  ) {
    return { ok: false, error: "Every value must be a number." };
  }
  if (fast < 2) return { ok: false, error: "Fast EMA period must be at least 2." };
  if (slow <= fast) return { ok: false, error: "Slow EMA period must be greater than the fast EMA period." };
  if (lookback < 2) return { ok: false, error: "Volume lookback must be at least 2." };
  if (rv <= 0) return { ok: false, error: "Relative volume threshold must be greater than 0." };
  if (cooldown < 0) return { ok: false, error: "Alert cooldown cannot be negative." };
  if (removal < 1) return { ok: false, error: "Removal confirmation must be at least 1 candle." };
  if (initialScan < 1) return { ok: false, error: "Initial scan must be at least 1 candle." };
  return {
    ok: true,
    values: {
      fast_ema_period: fast,
      slow_ema_period: slow,
      volume_lookback: lookback,
      relative_volume_threshold: rv,
      alert_cooldown_minutes: cooldown,
      removal_confirm_candles: removal,
      initial_scan_candles: initialScan,
    },
  };
}

export type SaveResult = {
  /** The `config_version` now active in the table. */
  version: number;
  /** Only the fields this save actually moved; empty means nothing changed. */
  changes: Record<string, { from: number; to: number }>;
};

type ActiveRow = Raw & { id: number; name: string; config_version: number };

async function activeRow(): Promise<ActiveRow | null> {
  // Same ordering the config page reads with, so the row this edits is exactly
  // the row the page displayed.
  const rows = await query<Raw>(
    `SELECT id::int AS id, name, config_version::int AS config_version
     FROM scanner_configs
     ORDER BY is_enabled DESC, config_version DESC
     LIMIT 1`,
  );
  const row = rows[0];
  return row ? ({ id: Number(row.id), name: String(row.name), config_version: Number(row.config_version) } as ActiveRow) : null;
}

/**
 * Insert the next version and journal the diff. One statement: the version
 * bump, the row and its audit entry commit together or not at all. Columns the
 * operator does not edit here (universe, feed staleness, candle grace,
 * subscription cap, rank weights) are copied from the active row unchanged.
 */
export async function saveScannerConfig(input: {
  values: ConfigEditValues;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<SaveResult> {
  const current = await activeRow();
  if (!current) {
    throw new Error("no scanner_configs row exists to edit — the worker is running on built-in defaults");
  }

  const currentValues = await query<Raw>(
    `SELECT fast_ema_period::float8 AS fast_ema_period,
            slow_ema_period::float8 AS slow_ema_period,
            volume_lookback::float8 AS volume_lookback,
            relative_volume_threshold::float8 AS relative_volume_threshold,
            alert_cooldown_minutes::float8 AS alert_cooldown_minutes,
            removal_confirm_candles::float8 AS removal_confirm_candles,
            initial_scan_candles::float8 AS initial_scan_candles
     FROM scanner_configs WHERE id = $1::int`,
    [current.id],
  );
  const before = currentValues[0] ?? {};
  const changes: Record<string, { from: number; to: number }> = {};
  for (const key of EDITABLE_KEYS) {
    const from = Number(before[key]);
    const to = input.values[key];
    if (from !== to) changes[key] = { from, to };
  }
  if (Object.keys(changes).length === 0) {
    return { version: current.config_version, changes: {} };
  }

  const saved = await query<Raw>(
    `WITH top AS (
       SELECT COALESCE(MAX(config_version), 0) + 1 AS v
       FROM scanner_configs
       WHERE name = (SELECT name FROM scanner_configs WHERE id = $9::int)
     ), base AS (
       SELECT * FROM scanner_configs WHERE id = $9::int
     ), ins AS (
       INSERT INTO scanner_configs (
         name, universe, fast_ema_period, slow_ema_period, volume_lookback,
         relative_volume_threshold, alert_cooldown_minutes, removal_confirm_candles,
         feed_staleness_threshold_seconds, candle_grace_seconds, initial_scan_candles,
         max_subscribed_instruments, rank_weights, is_enabled, config_version, created_by
       )
       SELECT base.name, base.universe, $1::int, $2::int, $3::int,
              $4::float8, $5::int, $6::int,
              base.feed_staleness_threshold_seconds, base.candle_grace_seconds, $7::int,
              base.max_subscribed_instruments, base.rank_weights, true,
              (SELECT v FROM top), $8::text
       FROM base
       RETURNING config_version::int AS version
     ), journaled AS (
       INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
       SELECT $10::text, $11::text, 'config_update', 'scanner_configs',
              (SELECT name FROM scanner_configs WHERE id = $9::int),
              jsonb_build_object('previous_version', $12::int, 'version',
                                 (SELECT version FROM ins), 'changes', $13::jsonb),
              $14::text
       RETURNING 1
     )
     SELECT version FROM ins`,
    [
      input.values.fast_ema_period,
      input.values.slow_ema_period,
      input.values.volume_lookback,
      input.values.relative_volume_threshold,
      input.values.alert_cooldown_minutes,
      input.values.removal_confirm_candles,
      input.values.initial_scan_candles,
      input.actor,
      current.id,
      input.actor,
      input.actorRole ?? "SUPER_ADMIN",
      current.config_version,
      // Passed as a JS object, not a pre-stringified payload: the driver
      // serializes jsonb parameters itself, and a pre-stringified string
      // arrives double-encoded (a jsonb *string* instead of an object).
      changes,
      input.requestIp ?? null,
    ],
  );
  const version = Number(saved[0]?.version);
  if (!Number.isFinite(version) || version <= 0) {
    throw new Error("the config row was not written — nothing changed in the database");
  }
  return { version, changes };
}

/**
 * Operations writes: the retention knob and the manual purge.
 *
 * This is deliberately the dashboard's *only* data-deleting path, and it is
 * shaped so it cannot run quietly: the caller must be a signed-in operator
 * (`guard()` in the route), every run writes an `audit_logs` row with who ran
 * it and what it deleted, and the deleted-row counts come back from the DELETE
 * statements themselves — never inferred from a row count taken before.
 *
 * The days value saved here (`ops_settings['retention']`) is the same number
 * the worker's automatic purge reads, so the button and the background job can
 * never disagree about what "old" means.
 */

import { query, type Raw } from "./db";
import { toIst } from "./format";

export const RETENTION_KEY = "retention";
export const RETENTION_MIN = 1;
export const RETENTION_MAX = 30;
export const RETENTION_DEFAULT = 7;

export type RetentionState = {
  days: number;
  updatedAt: string | null;
  /** The newest `retention_purge` audit row — what the last button press did. */
  lastPurge: {
    at: string | null;
    actor: string | null;
    days: number | null;
    symbol: string | null;
    deleted: Record<string, number> | null;
  } | null;
};

export type PurgeResult = {
  days: number;
  symbol: string | null;
  deleted: Record<string, number>;
};

const clampDays = (value: unknown): number | null => {
  const days = Math.trunc(Number(value));
  if (!Number.isFinite(days)) return null;
  return Math.min(RETENTION_MAX, Math.max(RETENTION_MIN, days));
};

/** The saved ageing window; the built-in default when nothing was saved yet. */
export async function getRetentionDays(): Promise<{ days: number; updatedAt: string | null }> {
  const rows = await query<Raw>(
    "SELECT value, updated_at FROM ops_settings WHERE key = $1::text",
    [RETENTION_KEY],
  );
  const raw = rows[0]?.value;
  const days = raw && typeof raw === "object" ? clampDays((raw as Raw).days) : null;
  return { days: days ?? RETENTION_DEFAULT, updatedAt: toIst(rows[0]?.updated_at as string | null) };
}

/** Persist the ageing window. Runs inside the purge POST and on its own. */
export async function setRetentionDays(days: number): Promise<void> {
  await query(
    `INSERT INTO ops_settings (key, value) VALUES ($1::text, $2::jsonb)
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
    [RETENTION_KEY, JSON.stringify({ days })],
  );
}

/** Newest `retention_purge` audit row, parsed for display. Missing = never run. */
export async function getLastPurge(): Promise<RetentionState["lastPurge"]> {
  const rows = await query<Raw>(
    `SELECT actor, created_at, new_value
     FROM audit_logs
     WHERE action = 'retention_purge'
     ORDER BY created_at DESC
     LIMIT 1`,
  );
  const row = rows[0];
  if (!row) return null;
  const value = row.new_value && typeof row.new_value === "object" ? (row.new_value as Raw) : {};
  return {
    at: toIst(row.created_at as string | null),
    actor: typeof row.actor === "string" ? row.actor : null,
    days: typeof value.days === "number" ? value.days : null,
    symbol: typeof value.symbol === "string" ? value.symbol : null,
    deleted:
      value.deleted && typeof value.deleted === "object"
        ? (Object.fromEntries(
            Object.entries(value.deleted as Record<string, unknown>).map(([key, count]) => [
              key,
              Number(count) || 0,
            ]),
          ) as Record<string, number>)
        : null,
  };
}

export async function getRetentionState(): Promise<RetentionState> {
  const [retention, lastPurge] = await Promise.all([getRetentionDays(), getLastPurge()]);
  return { days: retention.days, updatedAt: retention.updatedAt, lastPurge };
}

/**
 * Delete candles and indicator snapshots older than `days`, optionally scoped
 * to one instrument. The two DELETEs run as one statement (data-modifying CTE)
 * so a reader can never see candles trimmed without their snapshots, and the
 * counts are the rows this statement itself removed.
 */
export async function runPurge(input: {
  days: number;
  symbol?: string | null;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<PurgeResult> {
  const days = clampDays(input.days);
  if (days === null) {
    throw new Error(`days must be an integer between ${RETENTION_MIN} and ${RETENTION_MAX}`);
  }
  const symbol = input.symbol?.trim() || null;

  let instrumentId: number | null = null;
  if (symbol) {
    const found = await query<Raw>(
      `SELECT id::int AS id FROM instruments
       WHERE upper(symbol) = upper($1::text) OR upper(trading_symbol) = upper($1::text)
       ORDER BY is_active DESC, last_verified_at DESC NULLS LAST
       LIMIT 1`,
      [symbol],
    );
    if (!found[0]) throw new Error(`unknown symbol: ${symbol}`);
    instrumentId = Number(found[0].id);
  }

  // `make_interval` keeps the cutoff on the database clock — the same clock the
  // worker's purge uses — instead of trusting the caller's timestamp.
  const purgeSql = `
    WITH removed_candles AS (
      DELETE FROM candles_1m
      WHERE candle_start_at < now() - make_interval(days => $1::int)
        AND ($2::int IS NULL OR instrument_id = $2::int)
      RETURNING 1
    ),
    removed_snapshots AS (
      DELETE FROM indicator_snapshots
      WHERE candle_start_at < now() - make_interval(days => $1::int)
        AND ($2::int IS NULL OR instrument_id = $2::int)
      RETURNING 1
    )
    SELECT (SELECT count(*) FROM removed_candles)::int AS candles_1m,
           (SELECT count(*) FROM removed_snapshots)::int AS indicator_snapshots`;
  const counts = await query<Raw>(purgeSql, [days, instrumentId]);
  const deleted = {
    candles_1m: Number(counts[0]?.candles_1m ?? 0),
    indicator_snapshots: Number(counts[0]?.indicator_snapshots ?? 0),
  };

  await setRetentionDays(days);
  await query(
    `INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
     VALUES ($1::text, $2::text, 'retention_purge', 'candles_1m', $3::text, $4::jsonb, $5::text)`,
    [
      input.actor,
      input.actorRole ?? "SUPER_ADMIN",
      symbol ?? "ALL",
      JSON.stringify({ days, symbol, deleted }),
      input.requestIp ?? null,
    ],
  );

  return { days, symbol, deleted };
}

/**
 * Operations writes: the retention knob and the manual purge.
 *
 * This is deliberately the dashboard's *only* data-deleting path, and it is
 * shaped so it cannot run quietly: the caller must be a signed-in operator
 * (`guard()` in the route), every purge writes an `audit_logs` row with who ran
 * it and what it deleted, and the deleted-row counts come back from the DELETE
 * statements themselves — never inferred from a row count taken before.
 *
 * The days value saved here (`ops_settings['retention']`) is the same number
 * the worker's automatic purge reads, so the button and the background job can
 * never disagree about what "old" means. `0` is a real value, not an error: it
 * means keep everything, and it is how the staging install on the Mini
 * accumulates history while production stays at three days.
 */

import { query, type Raw } from "./db";
import { storedDocument, toIst } from "./format";

export const RETENTION_KEY = "retention";
export const RETENTION_MIN = 1;
export const RETENTION_MAX = 30;
/** The saved-window value that turns ageing off. A purge must never execute
 *  this value — deleting "everything older than 0 days" would be the whole
 *  table — so the delete path refuses it and only the window save accepts it. */
export const RETENTION_FOREVER = 0;
/** Three days, because this product trades intraday: every read of `candles_1m`
 *  is either the newest session (Board, Watchlist, /strategy ORB, intraday
 *  sparklines) or the newest ~400 bars of one instrument, and the worker's
 *  warm-up needs `max(slow_ema_period, volume_lookback)` completed candles — one
 *  session holds 345 of them. Days 2 and 3 are the slack for a restart on a
 *  Monday and for the broker's historical fallback, not a window anything
 *  renders. `candles_1d`/`candles_1w` are outside the purge by design (§0008)
 *  and are bounded separately. The Mini install overrides this with a saved 0 —
 *  keeping history is its whole point. */
export const RETENTION_DEFAULT = 3;

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

/** Parse a stored `days` the way the worker's `int(raw)` does: a finite
 *  number or a numeric string is a value; `null`, `""` and junk are not.
 *  (Python `int("")` raises, so the worker falls back — the display must
 *  agree with it rather than read `Number(null) === 0` as "keep forever".) */
const asInt = (value: unknown): number | null => {
  if (typeof value === "number") return Number.isFinite(value) ? Math.trunc(value) : null;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return Math.trunc(Number(value));
  }
  return null;
};

/** Normalise a stored window for display. `0` (and anything below it) is
 *  "keep forever" and must survive the read path intact — clamping 0 up to 1
 *  would show a number the worker does not obey. 1..30 pass through capped;
 *  unreadable values return null so the caller falls back to the built-in
 *  default, exactly like the worker's `RETENTION_DAYS` fallback. Pure, so the
 *  read path's promise is testable without a database. */
export const windowForDisplay = (value: unknown): number | null => {
  const days = asInt(value);
  if (days === null) return null;
  if (days <= RETENTION_FOREVER) return RETENTION_FOREVER;
  return Math.min(RETENTION_MAX, days);
};

/** The window a manual purge is allowed to run with: strictly 1..30. Pure.
 *  `0` is refused rather than clamped up — "keep everything" must never be
 *  executed as "delete everything older than a day". */
export const purgeWindow = (value: unknown): number => {
  const days = Math.trunc(Number(value));
  if (!Number.isFinite(days) || days < RETENTION_MIN || days > RETENTION_MAX) {
    throw new Error(
      `days must be an integer between ${RETENTION_MIN} and ${RETENTION_MAX}; 0 means keep forever and deletes nothing`,
    );
  }
  return days;
};

/** The window a *save* is allowed to store: 0 (keep forever) or 1..30. Pure. */
export const savedWindow = (value: unknown): number => {
  const days = Math.trunc(Number(value));
  if (!Number.isFinite(days) || days < RETENTION_FOREVER || days > RETENTION_MAX) {
    throw new Error(`days must be 0 (keep forever) or an integer between ${RETENTION_MIN} and ${RETENTION_MAX}`);
  }
  return days;
};

/** The saved ageing window; the built-in default when nothing was saved yet. */
export async function getRetentionDays(): Promise<{ days: number; updatedAt: string | null }> {
  const rows = await query<Raw>(
    "SELECT value, updated_at FROM ops_settings WHERE key = $1::text",
    [RETENTION_KEY],
  );
  // `storedDocument` rather than a typeof check: this column has held a double-encoded
  // jsonb string since the writer below pre-stringified it, and an object-only reader
  // displayed the default while a different number was saved.
  const stored = storedDocument(rows[0]?.value);
  const days = stored ? windowForDisplay(stored.days) : null;
  return { days: days ?? RETENTION_DEFAULT, updatedAt: toIst(rows[0]?.updated_at as string | null) };
}

/** Persist the ageing window. Runs inside the purge POST and from the
 *  window save. 0 is a legal, meaningful save: keep forever. */
export async function setRetentionDays(days: number): Promise<void> {
  const window = savedWindow(days); // rejects junk before it reaches the column
  await query(
    `INSERT INTO ops_settings (key, value) VALUES ($1::text, $2::jsonb)
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
    // An object, not `JSON.stringify({ days })`: the driver binds a parameter as a
    // value, so the stringified form was stored as a jsonb *string* — which the
    // worker's `get_ops_setting()` read as "no row" and fell back to `RETENTION_DAYS`.
    [RETENTION_KEY, { days: window }],
  );
}

/** Save the window without deleting anything — the path that turns the purge
 *  off (0) or re-arms it (1..30). Journalled to `audit_logs` like a purge, so
 *  the knob has a paper trail of its own: who turned ageing off, and when. */
export async function saveRetentionWindow(input: {
  days: number;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<{ days: number; updatedAt: string | null }> {
  const window = savedWindow(input.days);
  await setRetentionDays(window);
  await query(
    `INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
     VALUES ($1::text, $2::text, 'retention_set', 'ops_settings', $3::text, $4::jsonb, $5::text)`,
    [input.actor, input.actorRole ?? "SUPER_ADMIN", RETENTION_KEY, { days: window }, input.requestIp ?? null],
  );
  return getRetentionDays();
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
  // Tolerant for the same reason as above: every purge row written before this fix is
  // a double-encoded jsonb string, and a strict reader would report "never purged"
  // for a database that has been purged repeatedly.
  const value = storedDocument(row.new_value) ?? {};
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
 * counts are the rows this statement itself removed. Refuses anything outside
 * 1..30: `0` is a saved window ("keep forever"), never a purge parameter.
 */
export async function runPurge(input: {
  days: number;
  symbol?: string | null;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<PurgeResult> {
  const days = purgeWindow(input.days);
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
      { days, symbol, deleted },
      input.requestIp ?? null,
    ],
  );

  return { days, symbol, deleted };
}

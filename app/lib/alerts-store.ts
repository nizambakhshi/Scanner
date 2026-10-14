/**
 * The /alerts page's database half: two `ops_settings` documents and the audit
 * rows that explain every change to them.
 *
 * Shape of the arrangement, stated because the two processes depend on it:
 * **the dashboard writes, the worker reads.** The worker re-reads both keys every
 * `ALERT_SETTINGS_POLL_SECONDS` (60 s) and applies what changed, so a save here
 * takes effect inside the running session — no restart, no redeploy. That is the
 * difference between this page and `/config`, whose versioned `scanner_configs`
 * row is only adopted when the next session opens.
 *
 * What may be written down and what may not:
 *
 * * `alert_strategies` is not secret. The full document goes into
 *   `audit_logs.previous_value` / `new_value`, so an operator can read yesterday's
 *   thresholds back off the audit table.
 * * `telegram` **is** secret. The audit row carries `enabled`, whether each half
 *   was present, and the twelve-hex fingerprint of the pair — never the token,
 *   never the chat id. A journal that records credentials is a leak with a
 *   nicer name.
 * * Both rows are `ON CONFLICT (key) DO UPDATE`: this page owns exactly two keys
 *   and cannot shadow anything else in the table.
 */

import { query, type Raw } from "./db";
import { usesPostgres } from "./env";
import { storedDocument, toIst } from "./format";
import {
  DEFAULT_PARAMS,
  STRATEGIES,
  defaultStrategies,
  parseStrategies,
  strategiesDiff,
  type StrategiesDoc,
  type StrategyKey,
} from "./alert-strategies";
import {
  TELEGRAM_SETTINGS_KEY,
  credentialFingerprint,
  effectiveTelegram,
  telegramView,
  type TelegramRow,
  type TelegramView,
} from "./telegram";

export const ALERT_STRATEGIES_KEY = "alert_strategies";

/** Newest audit row for one of this page's actions. */
export type ChangeRecord = {
  at: string | null;
  actor: string | null;
  summary: string | null;
};

export type StrategiesState = {
  doc: StrategiesDoc;
  /** `saved` — a row the worker will obey; `defaults` — what ships until someone saves. */
  source: "saved" | "defaults";
  /** Set when a row exists but this build cannot obey it — the worker keeps the last good document. */
  problem: string | null;
  updatedAt: string | null;
  lastChange: ChangeRecord | null;
};

type OpsRow = Raw & { value: unknown; updated_at: unknown };

async function readOpsRow(key: string): Promise<OpsRow | null> {
  const rows = await query<OpsRow>("SELECT value, updated_at FROM ops_settings WHERE key = $1::text", [key]);
  return rows[0] ?? null;
}

function asAuditRow(action: string): Promise<ChangeRecord | null> {
  return (async () => {
    const rows = await query<Raw>(
      `SELECT actor, created_at, new_value FROM audit_logs
       WHERE action = $1::text ORDER BY created_at DESC LIMIT 1`,
      [action],
    );
    const row = rows[0];
    if (!row) return null;
    const value = storedDocument(row.new_value) ?? {};
    const changes = value.changes && typeof value.changes === "object" ? (value.changes as Raw) : null;
    const summary = changes
      ? Object.entries(changes)
          .map(([field, move]) => {
            const pair = (move ?? {}) as { from?: unknown; to?: unknown };
            return `${field}: ${fmt(pair.from)} → ${fmt(pair.to)}`;
          })
          .join(" · ")
      : null;
    return { at: toIst(row.created_at as string | null), actor: typeof row.actor === "string" ? row.actor : null, summary };
  })();
}

function fmt(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "on" : "off";
  return String(value);
}

/**
 * The document the worker will read next minute, and whether it came from a save
 * or from the defaults that ship.
 *
 * A row this build cannot parse is reported rather than papered over: the page
 * shows the built-in defaults it is *actually* running, plus the complaint,
 * because a page that displayed an unparsable document would be claiming a
 * strategy is armed when the detector never ran.
 */
export async function readStrategiesState(): Promise<StrategiesState> {
  const [row, lastChange] = await Promise.all([
    readOpsRow(ALERT_STRATEGIES_KEY),
    asAuditRow("alert_strategies_update").catch(() => null),
  ]);
  if (!row) {
    return { doc: defaultStrategies(), source: "defaults", problem: null, updatedAt: null, lastChange };
  }
  const parsed = parseStrategies(storedDocument(row.value) ?? row.value);
  if (!parsed.ok) {
    return {
      doc: defaultStrategies(),
      source: "defaults",
      problem: parsed.error,
      updatedAt: toIst(row.updated_at as string | null),
      lastChange,
    };
  }
  return {
    doc: parsed.doc,
    source: "saved",
    problem: parsed.warnings.length > 0 ? parsed.warnings.join("; ") : null,
    updatedAt: toIst(row.updated_at as string | null),
    lastChange,
  };
}

/**
 * Write the document and journal it in one round trip.
 *
 * `changes` is computed against the row that was there, so the audit entry says
 * what moved rather than restating the whole configuration; an unchanged save
 * writes nothing at all, which keeps the journal a record of decisions.
 */
export async function saveStrategies(input: {
  doc: StrategiesDoc;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<{ changes: ReturnType<typeof strategiesDiff>; warnings: string[] }> {
  const current = await readStrategiesState();
  const warnings = current.problem ? [current.problem] : [];
  const changes = strategiesDiff(current.doc, input.doc);
  if (Object.keys(changes).length === 0) {
    return { changes, warnings };
  }
  const written = await query<Raw>(
    `WITH upserted AS (
       INSERT INTO ops_settings (key, value)
       VALUES ($1::text, $2::jsonb)
       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
       RETURNING 1
     ), journaled AS (
       INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, previous_value, new_value, request_ip)
       VALUES ($3::text, $4::text, 'alert_strategies_update', 'ops_settings', $1::text,
               $5::jsonb, jsonb_build_object('changes', $6::jsonb), $7::text)
       RETURNING 1
     )
     SELECT (SELECT count(*) FROM upserted)::int AS upserted,
            (SELECT count(*) FROM journaled)::int AS journaled`,
    [
      ALERT_STRATEGIES_KEY,
      // An object, never `JSON.stringify(doc)`: the driver binds a parameter as a
      // value, so a pre-stringified document is stored as a jsonb *string* and the
      // worker's reader reports "no row" — a page that says saved while the
      // detectors run defaults. See `storedDocument` in lib/format.ts.
      input.doc,
      input.actor,
      input.actorRole ?? "SUPER_ADMIN",
      // Both payloads are objects for the same reason as the document above.
      current.doc,
      changes,
      input.requestIp ?? null,
    ],
  );
  if (Number(written[0]?.upserted ?? 0) < 1) {
    throw new Error("the alert_strategies row was not written — nothing changed in the database");
  }
  return { changes, warnings };
}

/**
 * The stored Telegram row, with the secrets intact and in memory only.
 *
 * The return type is not a view type: callers must pass it through
 * `effectiveTelegram` → `telegramView` before anything reaches a response.
 */
export async function readTelegramRow(): Promise<TelegramRow | null> {
  const row = await readOpsRow(TELEGRAM_SETTINGS_KEY);
  if (!row) return null;
  const value = storedDocument(row.value) ?? {};
  const enabled = typeof value.enabled === "boolean" ? value.enabled : false;
  return {
    enabled,
    botToken: typeof value.bot_token === "string" ? value.bot_token.trim() : "",
    chatId: typeof value.chat_id === "string" ? value.chat_id.trim() : "",
    updatedAt: toIst(row.updated_at as string | null),
  };
}

/**
 * Replace the credential half of the Telegram row.
 *
 * `merged` must already contain the complete pair to store — the caller decides
 * whether a blank field means "keep the stored value" (`mergeTelegramSave`).
 * The audit row is written from the *merged* pair's fingerprint and two booleans.
 */
export async function saveTelegram(input: {
  enabled: boolean;
  botToken: string;
  chatId: string;
  cleared: boolean;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<TelegramView & { changed: boolean }> {
  const before = await readTelegramRow();
  const changed =
    (before?.botToken ?? "") !== input.botToken ||
    (before?.chatId ?? "") !== input.chatId ||
    (before?.enabled ?? false) !== input.enabled;
  const document = {
    version: 1,
    enabled: input.enabled,
    bot_token: input.botToken,
    chat_id: input.chatId,
  };
  const written = await query<Raw>(
    `WITH upserted AS (
       INSERT INTO ops_settings (key, value) VALUES ($1::text, $2::jsonb)
       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
       RETURNING 1
     ), journaled AS (
       INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
       VALUES ($3::text, $4::text, 'telegram_settings_update', 'ops_settings', $1::text, $5::jsonb, $6::text)
       RETURNING 1
     )
     SELECT (SELECT count(*) FROM upserted)::int AS upserted`,
    [
      TELEGRAM_SETTINGS_KEY,
      document,
      input.actor,
      input.actorRole ?? "SUPER_ADMIN",
      // The journal records that credentials exist and which pair they are, in a
      // form that is worthless to a reader. Never `bot_token`, never `chat_id`.
      {
        enabled: input.enabled,
        has_token: input.botToken !== "",
        has_chat_id: input.chatId !== "",
        cleared: input.cleared,
        fingerprint: input.botToken !== "" && input.chatId !== "" ? credentialFingerprint(input.botToken, input.chatId) : null,
      },
      input.requestIp ?? null,
    ],
  );
  if (Number(written[0]?.upserted ?? 0) < 1) {
    throw new Error("the telegram row was not written");
  }
  const stored: TelegramRow = {
    enabled: input.enabled,
    botToken: input.botToken,
    chatId: input.chatId,
    updatedAt: null,
  };
  return { ...telegramView(effectiveTelegram(stored), stored), changed };
}

/**
 * Telegram as the page must display it: the row, the environment fallback, and
 * nothing secret.
 */
export async function readTelegramState(): Promise<TelegramView & { lastChange: ChangeRecord | null }> {
  const [row, lastChange] = await Promise.all([
    readTelegramRow(),
    asAuditRow("telegram_settings_update").catch(() => null),
  ]);
  return { ...telegramView(effectiveTelegram(row), row), lastChange };
}

/**
 * Journal one Send-test press: which pair it went to, what Telegram said, who
 * asked. The detail string is already scrubbed of the token by the sender, and it
 * is stored truncated — this row is for an operator reading the audit table
 * tomorrow, not a mirror of the API's body.
 */
export async function recordTelegramTest(input: {
  ok: boolean;
  detail: string;
  source: "saved" | "env" | "none";
  fingerprint: string | null;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<void> {
  await query(
    `INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
     VALUES ($1::text, $2::text, 'telegram_test_sent', 'ops_settings', $3::text, $4::jsonb, $5::text)`,
    [
      input.actor,
      input.actorRole ?? "SUPER_ADMIN",
      TELEGRAM_SETTINGS_KEY,
      {
        ok: input.ok,
        source: input.source,
        fingerprint: input.fingerprint,
        detail: input.detail.slice(0, 160),
      },
      input.requestIp ?? null,
    ],
  );
}

export type StrategyActivity = Record<StrategyKey, { up: number; down: number }>;

/**
 * How many level breaks each strategy actually raised today, straight from the
 * rows the detectors produced.
 *
 * This is the evidence that a switch on the page means something: a strategy
 * marked on with a zero count and a full board is a different conversation from a
 * strategy marked on that has been paging all morning. Keyed on the
 * `strategy:<name>` code the worker puts first in `reasons`, which is the only
 * place the discriminator survives into the database.
 *
 * `null` when the app is on the replay sample — there is no session to count.
 */
export async function readActivityToday(): Promise<StrategyActivity | null> {
  if (!usesPostgres()) return null;
  const selects = STRATEGIES.flatMap((key) => [
    `count(*) FILTER (WHERE reasons @> ARRAY['strategy:${key}'] AND signal_type = 'BREAKOUT_UP')::int AS ${key}_up`,
    `count(*) FILTER (WHERE reasons @> ARRAY['strategy:${key}'] AND signal_type = 'BREAKDOWN')::int AS ${key}_down`,
  ]);
  const rows = await query<Raw>(
    `SELECT ${selects.join(", ")}
     FROM signals
     WHERE signal_type IN ('BREAKOUT_UP', 'BREAKDOWN')
       AND event_timestamp >= ((now() AT TIME ZONE 'Asia/Kolkata')::date::timestamp + time '09:15')
                                    AT TIME ZONE 'Asia/Kolkata'`,
  );
  const row = rows[0];
  if (!row) return null;
  const activity = {
    session_break: { up: 0, down: 0 },
    orb: { up: 0, down: 0 },
    vwap: { up: 0, down: 0 },
  } as StrategyActivity;
  for (const key of STRATEGIES) {
    activity[key] = {
      up: Number(row[`${key}_up`] ?? 0) || 0,
      down: Number(row[`${key}_down`] ?? 0) || 0,
    };
  }
  return activity;
}

/** The numbers the shipped defaults will run on, for the page's empty state. */
export const SHIPPED_DEFAULTS = DEFAULT_PARAMS;

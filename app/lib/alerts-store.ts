/**
 * The admin pages' database half: the `ops_settings` documents they own
 * (`alert_strategies`, `telegram`, `days`) and the audit rows that explain every
 * change to them.
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
 * * Both kinds of row are `ON CONFLICT (key) DO UPDATE`: these pages own exactly
 *   three keys and cannot shadow anything else in the table.
 */

import { query, type Raw } from "./db";
import { usesPostgres } from "./env";
import { storedDocument, toIst } from "./format";
import {
  DEFAULT_PARAMS,
  SIGNAL_TYPE_FOR_DIRECTION,
  STRATEGIES,
  defaultStrategies,
  parseStrategies,
  strategiesDiff,
  type StrategiesDoc,
  type StrategyKey,
} from "./alert-strategies";
import {
  TELEGRAM_SETTINGS_KEY,
  chatsFingerprint,
  effectiveTelegram,
  telegramView,
  type TelegramChat,
  type TelegramRow,
  type TelegramView,
} from "./telegram";
import { DAYS_SETTINGS_KEY, daysDiff, defaultDays, parseDays, type DaysDoc, type DaysState } from "./days";

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
 *
 * A `chats` array is read when present; a v1 document (or the placeholder
 * migration 0010 seeds) falls back to its single `chat_id`, so the page shows
 * the stored destination either way.
 */
export async function readTelegramRow(): Promise<TelegramRow | null> {
  const row = await readOpsRow(TELEGRAM_SETTINGS_KEY);
  if (!row) return null;
  const value = storedDocument(row.value) ?? {};
  const enabled = typeof value.enabled === "boolean" ? value.enabled : false;
  const botToken = typeof value.bot_token === "string" ? value.bot_token.trim() : "";
  const listed = Array.isArray(value.chats)
    ? (value.chats as unknown[])
        .map((entry): TelegramChat | null => {
          const chat = (entry ?? {}) as Raw;
          const chatId = typeof chat.chat_id === "string" ? chat.chat_id.trim() : "";
          if (chatId === "") return null;
          return { chatId, label: typeof chat.label === "string" && chat.label.trim() !== "" ? chat.label.trim() : null };
        })
        .filter((chat): chat is TelegramChat => chat !== null)
    : [];
  const legacy = typeof value.chat_id === "string" ? value.chat_id.trim() : "";
  const chats = listed.length > 0 ? listed : legacy !== "" ? [{ chatId: legacy, label: null }] : [];
  return { enabled, botToken, chats, updatedAt: toIst(row.updated_at as string | null) };
}

/**
 * Replace the credential half of the Telegram row.
 *
 * `chats` must already contain the complete channel list to store — the caller
 * decides whether a missing list means "keep the stored channels"
 * (`mergeTelegramSave`). The document is written as version 2 with the first
 * channel **mirrored into `chat_id`**: the live worker reads that field, and a
 * chats-only row would look half-saved to it and fall back to the environment —
 * a silent destination change. The audit row is written from the *stored*
 * set's fingerprint, a count, and two booleans; never a chat id, never a token.
 */
export async function saveTelegram(input: {
  enabled: boolean;
  botToken: string;
  chats: TelegramChat[];
  cleared: boolean;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<TelegramView & { changed: boolean }> {
  const before = await readTelegramRow();
  const changed =
    (before?.botToken ?? "") !== input.botToken ||
    (before?.enabled ?? false) !== input.enabled ||
    before?.chats.map((chat) => chat.chatId).join(",") !== input.chats.map((chat) => chat.chatId).join(",");
  const document = {
    version: 2,
    enabled: input.enabled,
    bot_token: input.botToken,
    chat_id: input.chats[0]?.chatId ?? "",
    chats: input.chats.map((chat) => ({ chat_id: chat.chatId, label: chat.label })),
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
      // The journal records that credentials exist and how many destinations they
      // cover, in a form that is worthless to a reader. Never `bot_token`, never
      // a chat id.
      {
        enabled: input.enabled,
        has_token: input.botToken !== "",
        chat_count: input.chats.length,
        cleared: input.cleared,
        fingerprint: chatsFingerprint(input.botToken, input.chats),
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
    chats: input.chats,
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
 * How many rows each strategy actually raised today, straight from the signals the
 * detectors produced.
 *
 * This is the evidence that a switch on the page means something: a strategy
 * marked on with a zero count and a full board is a different conversation from a
 * strategy marked on that has been paging all morning. Keyed on the
 * `strategy:<name>` code the worker puts first in `reasons`, which is the only
 * place the discriminator survives into the database.
 *
 * Each strategy's two counts are the two signal *types* that strategy writes, read
 * from `SIGNAL_TYPE_FOR_DIRECTION` rather than hardcoded: the RSI rule writes
 * `RSI_ENTRY` / `RSI_OVERBOUGHT`, and a query that only knew `BREAKOUT_UP` would
 * report zero for a rule that paged all morning. The interpolated names are this
 * module's own literals, not request input.
 *
 * `null` when the app is on the replay sample — there is no session to count.
 */
export async function readActivityToday(): Promise<StrategyActivity | null> {
  if (!usesPostgres()) return null;
  const selects = STRATEGIES.flatMap((key) => {
    const types = SIGNAL_TYPE_FOR_DIRECTION[key];
    return [
      `count(*) FILTER (WHERE reasons @> ARRAY['strategy:${key}'] AND signal_type = '${types.UP}')::int AS ${key}_up`,
      `count(*) FILTER (WHERE reasons @> ARRAY['strategy:${key}'] AND signal_type = '${types.DOWN}')::int AS ${key}_down`,
    ];
  });
  const countedTypes = [...new Set(STRATEGIES.flatMap((key) => Object.values(SIGNAL_TYPE_FOR_DIRECTION[key])))];
  const rows = await query<Raw>(
    `SELECT ${selects.join(", ")}
     FROM signals
     WHERE signal_type IN (${countedTypes.map((type) => `'${type}'`).join(", ")})
       AND event_timestamp >= ((now() AT TIME ZONE 'Asia/Kolkata')::date::timestamp + time '09:15')
                                    AT TIME ZONE 'Asia/Kolkata'`,
  );
  const row = rows[0];
  if (!row) return null;
  const activity = Object.fromEntries(
    STRATEGIES.map((key) => [key, { up: 0, down: 0 }]),
  ) as unknown as StrategyActivity;
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

/**
 * The trading-day exceptions the /days page owns.
 *
 * Same arrangement as the other two documents: this page writes, the worker
 * reads. A missing row or one this build cannot parse is the plain weekday
 * rule — and unlike the Telegram row, *that is also what the worker runs*, so
 * the page and the detectors can never disagree about which days are on.
 */
export async function readDaysState(): Promise<DaysState> {
  const [row, lastChange] = await Promise.all([
    readOpsRow(DAYS_SETTINGS_KEY),
    asAuditRow("days_update").catch(() => null),
  ]);
  if (!row) {
    return { doc: defaultDays(), source: "default", updatedAt: null, lastChange };
  }
  const parsed = parseDays(storedDocument(row.value) ?? row.value);
  return {
    // A row the parser refuses falls back to the plain rule *displayed as the
    // default*: the page says "weekday rule", because that is what runs.
    doc: parsed.ok ? parsed.doc : defaultDays(),
    source: parsed.ok ? "saved" : "default",
    updatedAt: toIst(row.updated_at as string | null),
    lastChange,
  };
}

/**
 * Write the exceptions document and journal it in one round trip.
 *
 * `changes` lists the dates that moved, not the whole document, so the audit
 * table reads as a record of decisions ("2026-10-26 added to skip"). An
 * unchanged save writes nothing at all.
 */
export async function saveDays(input: {
  doc: DaysDoc;
  actor: string;
  actorRole?: string | null;
  requestIp?: string | null;
}): Promise<{ changes: ReturnType<typeof daysDiff>; applied: boolean }> {
  const current = await readDaysState();
  const changes = daysDiff(current.doc, input.doc);
  if (Object.keys(changes).length === 0) {
    return { changes, applied: false };
  }
  const written = await query<Raw>(
    `WITH upserted AS (
       INSERT INTO ops_settings (key, value)
       VALUES ($1::text, $2::jsonb)
       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
       RETURNING 1
     ), journaled AS (
       INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, previous_value, new_value, request_ip)
       VALUES ($3::text, $4::text, 'days_update', 'ops_settings', $1::text,
               $5::jsonb, jsonb_build_object('changes', $6::jsonb), $7::text)
       RETURNING 1
     )
     SELECT (SELECT count(*) FROM upserted)::int AS upserted,
            (SELECT count(*) FROM journaled)::int AS journaled`,
    [
      DAYS_SETTINGS_KEY,
      // An object, never `JSON.stringify`: the driver binds it as a jsonb value.
      input.doc,
      input.actor,
      input.actorRole ?? "SUPER_ADMIN",
      current.doc,
      changes,
      input.requestIp ?? null,
    ],
  );
  if (Number(written[0]?.upserted ?? 0) < 1) {
    throw new Error("the days row was not written — nothing changed in the database");
  }
  return { changes, applied: true };
}

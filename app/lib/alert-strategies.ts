/**
 * Alert-strategy configuration: the dashboard's mirror of
 * `worker/worker/analytics/breakouts.py`.
 *
 * The operator decides here what the worker judges every minute. Four
 * strategies, each with the same eight fields, stored as one JSONB document in
 * `ops_settings['alert_strategies']` — the row the worker re-reads every 60 s,
 * so a save takes effect inside the running session without a restart.
 *
 * Why this file is a *mirror* rather than a translation: the worker refuses a
 * document it cannot parse and keeps the one already in force. If the page
 * accepted a value the detector would reject, the operator would see a switch
 * that is on while nothing runs — the exact failure this project treats as
 * dangerous. So the defaults, the ranges and the coercion rules are copied
 * field for field from `StrategyParams.problems()` and `parse_strategies()`,
 * and `tests/alert-strategies.test.ts` pins the numbers against this file's
 * constants. Change one side, change the other.
 *
 * Deliberately pure: no database, no `process.env`. Everything that touches the
 * row lives in `alerts-store.ts`.
 */

import type { SignalType } from "./types";

/** Strategy keys, in display order. Must equal `STRATEGIES` in the worker. */
export const STRATEGIES = ["session_break", "orb", "vwap", "rsi"] as const;

export type StrategyKey = (typeof STRATEGIES)[number];

export type StrategyParams = {
  enabled: boolean;
  threshold_pct: number;
  confirm_candles: number;
  min_volume_ratio: number;
  cooldown_minutes: number;
  telegram: boolean;
  /** The operator's two RSI numbers. They belong to `rsi` alone, but the worker
   *  carries them on the shared shape — one dataclass, one parser, one editor — so
   *  every strategy stores and returns them. See `EDITABLE_FIELDS` for what the page
   *  offers on each card. */
  oversold: number;
  overbought: number;
};

export type StrategiesDoc = {
  version: number;
  strategies: Record<StrategyKey, StrategyParams>;
};

/** The stored document's version marker; the worker ignores it, the page shows it. */
export const DOC_VERSION = 1;

/**
 * The editable fields and the ranges the worker enforces
 * (`StrategyParams.problems`). `step` is what the number input offers; a value
 * outside the range is a form error, never a database write.
 */
export type ParamField = keyof Omit<StrategyParams, "enabled" | "telegram">;

export const FIELD_LIMITS: Record<ParamField, { min: number; max: number; step: string; integer: boolean }> = {
  threshold_pct: { min: 0, max: 5, step: "0.05", integer: false },
  confirm_candles: { min: 1, max: 5, step: "1", integer: true },
  min_volume_ratio: { min: 0, max: 10, step: "0.1", integer: false },
  cooldown_minutes: { min: 0, max: 240, step: "1", integer: true },
  // The worker bounds these only as a pair (`0 < oversold < overbought < 100`), so
  // these limits are the input's guard rails, not the rule. `strategyProblems` owns
  // the rule, in the worker's own words.
  oversold: { min: 0, max: 100, step: "1", integer: false },
  overbought: { min: 0, max: 100, step: "1", integer: false },
};

/** The two RSI levels: checked together, never as two independent ranges. */
export const LEVEL_FIELDS: readonly ParamField[] = ["oversold", "overbought"];

/** Every numeric field the shared shape carries, in the order the worker validates
 *  them. The editor posts, diffs and range-checks this whole list — a field that is
 *  not shown on a card is still stored, and a stored value the worker would refuse is
 *  something the page has to catch before writing it. */
export const PARAM_FIELDS: readonly ParamField[] = [
  "threshold_pct",
  "confirm_candles",
  "min_volume_ratio",
  "cooldown_minutes",
  "oversold",
  "overbought",
];

/**
 * The fields each detector actually reads, and therefore the ones worth putting in
 * front of the operator.
 *
 * `rsi` has no `threshold_pct` — the two RSI levels *are* its thresholds — and the
 * three level detectors never look at `oversold`/`overbought`. The hidden fields stay
 * in the document at their stored values: the worker's shape is shared, so dropping
 * them from a save would reset another strategy's numbers to defaults.
 */
export const EDITABLE_FIELDS: Record<StrategyKey, readonly ParamField[]> = {
  session_break: ["threshold_pct", "confirm_candles", "min_volume_ratio", "cooldown_minutes"],
  orb: ["threshold_pct", "confirm_candles", "min_volume_ratio", "cooldown_minutes"],
  vwap: ["threshold_pct", "confirm_candles", "min_volume_ratio", "cooldown_minutes"],
  rsi: ["oversold", "overbought", "confirm_candles", "min_volume_ratio", "cooldown_minutes"],
};

/**
 * Defaults copied from `DEFAULTS` in the worker (and from `StrategyParams`' own field
 * defaults, which every strategy that ignores a level still carries). They are the
 * numbers the system runs on when no row exists, so the page must show them as the
 * current values — a blank form that silently means 0.10% would be a lie about the
 * running config.
 */
export const DEFAULT_PARAMS: Record<StrategyKey, StrategyParams> = {
  session_break: {
    enabled: true,
    threshold_pct: 0.1,
    confirm_candles: 1,
    min_volume_ratio: 1.0,
    cooldown_minutes: 15,
    telegram: true,
    oversold: 40,
    overbought: 60,
  },
  orb: {
    enabled: true,
    threshold_pct: 0.0,
    confirm_candles: 1,
    min_volume_ratio: 1.5,
    cooldown_minutes: 0,
    telegram: true,
    oversold: 40,
    overbought: 60,
  },
  vwap: {
    enabled: true,
    threshold_pct: 0.1,
    confirm_candles: 2,
    min_volume_ratio: 1.0,
    cooldown_minutes: 10,
    telegram: true,
    oversold: 40,
    overbought: 60,
  },
  // The operator's own two numbers, and the only reason this strategy exists. Two
  // buckets (ten minutes) rather than one, thirty minutes of cooldown, and no volume
  // gate: the rule is about the quiet pullback, not about a crowd.
  rsi: {
    enabled: true,
    threshold_pct: 0.0,
    confirm_candles: 2,
    min_volume_ratio: 0.0,
    cooldown_minutes: 30,
    telegram: true,
    oversold: 40,
    overbought: 60,
  },
};

/**
 * Display copy. `what` restates the detector's rule in the operator's terms; the
 * field hints say what raising or lowering that particular number does, because
 * "threshold_pct" alone does not tell anyone that a day-high break on the high
 * itself is what every new high does anyway. Only the fields a strategy reads are
 * hinted — see `EDITABLE_FIELDS`.
 */
export const STRATEGY_COPY: Record<
  StrategyKey,
  { label: string; what: string; direction: string; fieldHints: Partial<Record<ParamField, string>>; telegramNote: string }
> = {
  session_break: {
    label: "Session high / low break",
    what: "A minute that *closes* above the session high printed before it — or below the session low — by more than the threshold. It disarms after firing and re-arms only when the price falls back through the level it broke, so a stock that grinds upward all day does not page once per new high.",
    direction: "Up = new session high, down = new session low.",
    fieldHints: {
      threshold_pct: "How far past the level the minute must close, in percent. 0.10 keeps a one-tick poke-out from counting.",
      confirm_candles: "Consecutive closing candles beyond the level before the alert. 1 is immediate.",
      min_volume_ratio: "Relative volume the break must carry, against the scanner's own volume mean. 0 turns the gate off.",
      cooldown_minutes: "Minutes before the same strategy may page this stock again. The level itself is also one-alert-per-level.",
    },
    telegramNote: "Needs volume evidence to page: a quiet poke past a high is a wick, not news.",
  },
  orb: {
    label: "Opening range break",
    what: "The opening range is the first five one-minute bars; a break is a five-minute bucket close beyond it. This is the same definition the /strategy board computes in SQL, so a page here and a row there cannot disagree.",
    direction: "Up = above the range high, down = below the range low.",
    fieldHints: {
      threshold_pct: "Extra distance past the range, in percent. 0 matches the board exactly; anything above it pages only the decisive buckets.",
      confirm_candles: "Buckets beyond the range before the alert. Keep at 1 — one bucket is five minutes of evidence.",
      min_volume_ratio: "Bucket volume over the mean of every earlier bucket, the board's own ratio. 1.5 is the board's breakout criterion.",
      cooldown_minutes: "Leave at 0: the range is fixed for the day, so this strategy already alerts once per direction per session.",
    },
    telegramNote: "With cooldown 0 and the board's volume gate, one page per direction per day is the intent.",
  },
  vwap: {
    label: "VWAP reclaim / loss",
    what: "A close back above the session VWAP after being below it, or a loss of it after being above. The threshold is a band around the VWAP rather than a distance to travel: the first determination of the day sets the side and does not alert, because the open always prints on one side of its own average.",
    direction: "Up = reclaimed the VWAP, down = lost it.",
    fieldHints: {
      threshold_pct: "The hysteresis band, in percent either side of the VWAP. Without it a tape that hugs the average fires on every tick.",
      confirm_candles: "Candles that must close inside the band before the side flips. 2 stops a single spike from re-labelling the tape.",
      min_volume_ratio: "Relative volume a cross must carry. A cross without volume evidence is not reported, and the standing side is kept so the same move on better volume later still counts as a fresh cross.",
      cooldown_minutes: "Minutes before this strategy may page this stock again.",
    },
    telegramNote: "Flapping is prevented by the band and the confirmation candles, not by the cooldown.",
  },
  rsi: {
    label: "RSI entry / overbought",
    what: "Wilder RSI-14 on today's five-minute closes, judged against the trend the scanner already measures — close over the session VWAP with the fast EMA over the slow one is up, the reverse is down. Below the entry level while the trend is up is the green side; above the overbought level while the trend is down is the red one. An extreme the trend does not confirm is neutral, and the first verdict of a session is a baseline rather than news, as is falling back into the band. The state is judged at every bucket close whether or not this rule is armed: the switch governs the alert, never the measurement the board shows.",
    direction: "Down = the entry dip (RSI_ENTRY), up = the overbought print (RSI_OVERBOUGHT).",
    fieldHints: {
      oversold:
        "The green line, in RSI points. Below it while the trend is up is the dip the rule calls an entry. The worker needs 0 < oversold < overbought < 100, so this cannot pass the red line.",
      overbought:
        "The red line. Above it while the trend is down is an expensive print; the same number inside an up-trend is strength, and stays neutral.",
      confirm_candles:
        "Consecutive five-minute buckets on the same side before the alert — 2 is ten minutes of evidence, 1 lets a single printed low page.",
      min_volume_ratio:
        "Relative volume the confirming bucket must carry. 0 turns the gate off, and 0 is the default here: a pullback on thin volume is the setup, not a reason to doubt it.",
      cooldown_minutes:
        "Minutes before this strategy may page this stock again. 30 keeps an RSI that hugs the line in a slow drift down to one piece of news.",
    },
    telegramNote: "The volume gate ships off: this rule is about where the pullback landed, not about who was trading it.",
  },
};

/**
 * Which signal type each direction of each strategy writes.
 *
 * The three level detectors report a *level* crossing, and the direction says which
 * way it went — `BREAKOUT_UP` / `BREAKDOWN`. `rsi` reports which side of the
 * operator's band a reading landed on, so its green side is the one that pages on a
 * **down** direction: an entry is a dip, and the worker's own mapping says so.
 */
export const SIGNAL_TYPE_FOR_DIRECTION: Record<StrategyKey, { UP: SignalType; DOWN: SignalType }> = {
  session_break: { UP: "BREAKOUT_UP", DOWN: "BREAKDOWN" },
  orb: { UP: "BREAKOUT_UP", DOWN: "BREAKDOWN" },
  vwap: { UP: "BREAKOUT_UP", DOWN: "BREAKDOWN" },
  rsi: { UP: "RSI_OVERBOUGHT", DOWN: "RSI_ENTRY" },
};

/**
 * The label for a strategy key that arrived as text — a `reasons` entry, a field
 * in a stored document. `null` for a name this build does not know, so the caller
 * decides how to show the unknown rather than a label lookup throwing inside a
 * table row.
 */
export function strategyLabel(key: string): string | null {
  return isStrategyKey(key) ? STRATEGY_COPY[key].label : null;
}

export function defaultStrategies(): StrategiesDoc {
  const strategies = {} as Record<StrategyKey, StrategyParams>;
  for (const key of STRATEGIES) strategies[key] = { ...DEFAULT_PARAMS[key] };
  return { version: DOC_VERSION, strategies };
}

export function isStrategyKey(value: string): value is StrategyKey {
  return (STRATEGIES as readonly string[]).includes(value);
}

/** `StrategyParams.to_dict()` — the whole shared shape, including the two levels a
 *  strategy may not read. */
export function cloneParams(params: StrategyParams): StrategyParams {
  return { ...params };
}

export type Parsed = { ok: true; doc: StrategiesDoc; warnings: string[] } | { ok: false; error: string };

function asBool(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "number") return value !== 0;
  const text = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(text)) return true;
  if (["0", "false", "no", "off"].includes(text)) return false;
  throw new Error("expected a yes/no value");
}

function asNumber(value: unknown, fallback: number): number {
  if (value === null || value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error("expected a number");
  return parsed;
}

function asInt(value: unknown, fallback: number): number {
  if (value === null || value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error("expected a whole number");
  return Math.trunc(parsed);
}

/** `StrategyParams.problems()` for one strategy, phrased for an operator. */
export function strategyProblems(key: StrategyKey, params: StrategyParams): string[] {
  const label = STRATEGY_COPY[key].label;
  const found: string[] = [];
  for (const field of PARAM_FIELDS) {
    // The two RSI levels have no range of their own in the worker; they are checked
    // as an ordered pair below, so a card that hides them cannot be blamed for a
    // value it never offered.
    if (LEVEL_FIELDS.includes(field)) continue;
    const limits = FIELD_LIMITS[field];
    const value = params[field];
    if (!Number.isFinite(value)) {
      found.push(`${label}: ${field} must be a number`);
      continue;
    }
    if (value < limits.min || value > limits.max) {
      found.push(`${label}: ${field} must be between ${limits.min} and ${limits.max}`);
    }
  }
  // `0 < oversold < overbought < 100`, in the worker's own words: a level rule that
  // reads as one sentence is a rule an operator can fix in one go, and a strategy
  // that ignores the levels still has to carry a legal pair.
  const { oversold, overbought } = params;
  if (!(Number.isFinite(oversold) && Number.isFinite(overbought) && 0 < oversold && oversold < overbought && overbought < 100)) {
    found.push(`${label}: oversold/overbought must be 0 < oversold < overbought < 100`);
  }
  return found;
}

/** Strategy names a document mentions that this build does not know. */
export function unknownStrategiesIn(payload: unknown): string[] {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return [];
  const record = payload as Record<string, unknown>;
  const inner = record.strategies;
  const body = inner && typeof inner === "object" && !Array.isArray(inner) ? (inner as Record<string, unknown>) : record;
  return Object.keys(body).filter((key) => !isStrategyKey(key)).sort();
}

function strategiesBody(payload: Record<string, unknown>): Record<string, unknown> {
  const inner = payload.strategies;
  return inner && typeof inner === "object" && !Array.isArray(inner) ? (inner as Record<string, unknown>) : payload;
}

/**
 * `parse_strategies()` — the stored JSON becomes a validated document.
 *
 * Accepts `{strategies: {…}}`, a bare `{session_break: {…}, …}` map, or nothing
 * at all (null / {} → the defaults). A malformed value reports the strategy and
 * the field instead of dropping it: the page must not offer a switch the worker
 * will ignore. Unknown strategy names are a warning, not a refusal — a newer
 * dashboard should not take the detectors down, but the operator has to be told
 * the name is not being obeyed.
 */
export function parseStrategies(payload: unknown): Parsed {
  if (payload === null || payload === undefined) {
    return { ok: true, doc: defaultStrategies(), warnings: [] };
  }
  if (typeof payload !== "object" || Array.isArray(payload)) {
    return { ok: false, error: "alert_strategies must be a JSON object" };
  }
  const body = strategiesBody(payload as Record<string, unknown>);
  const doc = defaultStrategies();
  const warnings: string[] = [];
  const problems: string[] = [];
  for (const key of STRATEGIES) {
    const raw = body[key];
    if (raw === null || raw === undefined) continue;
    if (typeof raw !== "object" || Array.isArray(raw)) {
      problems.push(`${STRATEGY_COPY[key].label}: expected an object`);
      continue;
    }
    const fields = raw as Record<string, unknown>;
    const defaults = DEFAULT_PARAMS[key];
    try {
      doc.strategies[key] = {
        enabled: asBool(fields.enabled, defaults.enabled),
        threshold_pct: asNumber(fields.threshold_pct, defaults.threshold_pct),
        confirm_candles: asInt(fields.confirm_candles, defaults.confirm_candles),
        min_volume_ratio: asNumber(fields.min_volume_ratio, defaults.min_volume_ratio),
        cooldown_minutes: asInt(fields.cooldown_minutes, defaults.cooldown_minutes),
        telegram: asBool(fields.telegram, defaults.telegram),
        oversold: asNumber(fields.oversold, defaults.oversold),
        overbought: asNumber(fields.overbought, defaults.overbought),
      };
    } catch (error) {
      problems.push(`${STRATEGY_COPY[key].label}: ${error instanceof Error ? error.message : String(error)}`);
      continue;
    }
    problems.push(...strategyProblems(key, doc.strategies[key]));
  }
  const unknown = unknownStrategiesIn(payload);
  if (unknown.length > 0) {
    warnings.push(`ignored unknown strategies: ${unknown.join(", ")}`);
  }
  if (problems.length > 0) {
    return { ok: false, error: problems.join("; ") };
  }
  return { ok: true, doc, warnings };
}

/** The dotted form key the editor posts one field under: `session_break.threshold_pct`. */
export function formKey(key: StrategyKey, field: keyof StrategyParams): string {
  return `${key}.${field}`;
}

export type EditSource = Record<string, string>;

/**
 * Validate an editor submission against the document already in force.
 *
 * A field the form does not mention keeps its stored value, so a partial save
 * cannot quietly reset a strategy to the built-in defaults. Booleans arrive as
 * "on"/"true"/"false" depending on how the control was rendered, which the
 * worker's own coercion accepts too.
 */
export function parseStrategyEdits(form: EditSource, base: StrategiesDoc = defaultStrategies()): Parsed {
  const doc: StrategiesDoc = { version: base.version ?? DOC_VERSION, strategies: { ...base.strategies } };
  const problems: string[] = [];
  for (const key of STRATEGIES) {
    const current = doc.strategies[key];
    const next: StrategyParams = { ...current };
    for (const field of ["enabled", "telegram"] as const) {
      const value = form[formKey(key, field)];
      if (value === undefined) continue;
      try {
        next[field] = asBool(value, current[field]);
      } catch {
        problems.push(`${STRATEGY_COPY[key].label}: ${field} must be yes or no`);
      }
    }
    for (const field of PARAM_FIELDS) {
      const value = form[formKey(key, field)];
      if (value === undefined) continue;
      if (String(value).trim() === "") continue;
      try {
        next[field] = FIELD_LIMITS[field].integer ? asInt(value, current[field]) : asNumber(value, current[field]);
      } catch {
        problems.push(`${STRATEGY_COPY[key].label}: ${field.replace(/_/g, " ")} must be a number`);
      }
    }
    doc.strategies[key] = next;
    problems.push(...strategyProblems(key, next));
  }
  if (problems.length > 0) {
    return { ok: false, error: problems.join("; ") };
  }
  // A dotted key that names neither a strategy nor a parameter this build knows
  // is dropped, and a save that answers "nothing changed" to a form that did
  // contain a change is a confusing thing to read. The page cannot produce one —
  // it builds its keys from `STRATEGIES` — so the only way to see this warning is
  // to post by hand, which is exactly when a typo is worth naming.
  const known = new Set<string>();
  for (const key of STRATEGIES) {
    for (const field of [...PARAM_FIELDS, "enabled", "telegram"] as (keyof StrategyParams)[]) known.add(formKey(key, field));
  }
  const unknownFields = Object.keys(form).filter((key) => key.includes(".") && !known.has(key)).sort();
  const warnings =
    unknownFields.length > 0 ? [`ignored unknown form fields: ${unknownFields.join(", ")}`] : [];
  return { ok: true, doc, warnings };
}

/** The field-level diff between two documents — what the audit row records. */
export function strategiesDiff(
  before: StrategiesDoc,
  after: StrategiesDoc,
): Record<string, { from: number | boolean; to: number | boolean }> {
  const changes: Record<string, { from: number | boolean; to: number | boolean }> = {};
  for (const key of STRATEGIES) {
    for (const field of [...PARAM_FIELDS, "enabled", "telegram"] as (keyof StrategyParams)[]) {
      const from = before.strategies[key]?.[field];
      const to = after.strategies[key][field];
      if (from !== to) changes[`${key}.${field}`] = { from: from as number | boolean, to };
    }
  }
  return changes;
}

/** Anything on this page worth warning the operator about. */
export function strategiesAdvisories(doc: StrategiesDoc): string[] {
  const notes: string[] = [];
  if (!STRATEGIES.some((key) => doc.strategies[key].enabled)) {
    notes.push("All four detectors are switched off — the board will keep computing, but no alert will be raised.");
  }
  for (const key of STRATEGIES) {
    const params = doc.strategies[key];
    // Two strategies ship without a volume gate on purpose — ORB keeps the strategy
    // board's own bucket-volume criterion, and the entry rule is *about* the quiet
    // pullback — so for those two an off gate is the setting, not an oversight.
    if (key === "orb" || key === "rsi") continue;
    if (params.enabled && params.telegram && params.min_volume_ratio <= 0) {
      notes.push(
        `${STRATEGY_COPY[key].label}: the volume gate is off, so a thin poke past the level will page your phone.`,
      );
    }
  }
  return notes;
}

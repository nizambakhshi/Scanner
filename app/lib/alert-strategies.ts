/**
 * Alert-strategy configuration: the dashboard's mirror of
 * `worker/worker/analytics/breakouts.py`.
 *
 * The operator decides here what the worker judges every minute. Three
 * strategies, each with the same six tunables, stored as one JSONB document in
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

/** Strategy keys, in display order. Must equal `STRATEGIES` in the worker. */
export const STRATEGIES = ["session_break", "orb", "vwap"] as const;

export type StrategyKey = (typeof STRATEGIES)[number];

export type StrategyParams = {
  enabled: boolean;
  threshold_pct: number;
  confirm_candles: number;
  min_volume_ratio: number;
  cooldown_minutes: number;
  telegram: boolean;
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
};

export const PARAM_FIELDS: readonly ParamField[] = [
  "threshold_pct",
  "confirm_candles",
  "min_volume_ratio",
  "cooldown_minutes",
];

/**
 * Defaults copied from `DEFAULTS` in the worker. They are the numbers the system
 * runs on when no row exists, so the page must show them as the current values —
 * a blank form that silently means 0.10% would be a lie about the running config.
 */
export const DEFAULT_PARAMS: Record<StrategyKey, StrategyParams> = {
  session_break: {
    enabled: true,
    threshold_pct: 0.1,
    confirm_candles: 1,
    min_volume_ratio: 1.0,
    cooldown_minutes: 15,
    telegram: true,
  },
  orb: {
    enabled: true,
    threshold_pct: 0.0,
    confirm_candles: 1,
    min_volume_ratio: 1.5,
    cooldown_minutes: 0,
    telegram: true,
  },
  vwap: {
    enabled: true,
    threshold_pct: 0.1,
    confirm_candles: 2,
    min_volume_ratio: 1.0,
    cooldown_minutes: 10,
    telegram: true,
  },
};

/**
 * Display copy. `what` restates the detector's rule in the operator's terms; the
 * field hints say what raising or lowering that particular number does, because
 * "threshold_pct" alone does not tell anyone that a day-high break on the high
 * itself is what every new high does anyway.
 */
export const STRATEGY_COPY: Record<
  StrategyKey,
  { label: string; what: string; direction: string; fieldHints: Record<ParamField, string>; telegramNote: string }
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
};

/** Which signal type each direction raises, for display next to the switch. */
export const SIGNAL_TYPE_FOR_DIRECTION: Record<"UP" | "DOWN", string> = {
  UP: "BREAKOUT_UP",
  DOWN: "BREAKDOWN",
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
  return {
    version: DOC_VERSION,
    strategies: {
      session_break: { ...DEFAULT_PARAMS.session_break },
      orb: { ...DEFAULT_PARAMS.orb },
      vwap: { ...DEFAULT_PARAMS.vwap },
    },
  };
}

export function isStrategyKey(value: string): value is StrategyKey {
  return (STRATEGIES as readonly string[]).includes(value);
}

/** `StrategiesConfig.to_dict()` — one strategy's six fields. */
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
    notes.push("All three detectors are switched off — the board will keep computing, but no level break will be raised.");
  }
  for (const key of STRATEGIES) {
    const params = doc.strategies[key];
    if (params.enabled && params.telegram && params.min_volume_ratio <= 0 && key !== "orb") {
      notes.push(
        `${STRATEGY_COPY[key].label}: the volume gate is off, so a thin poke past the level will page your phone.`,
      );
    }
  }
  return notes;
}

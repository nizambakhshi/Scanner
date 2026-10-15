import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DEFAULT_PARAMS,
  FIELD_LIMITS,
  STRATEGIES,
  defaultStrategies,
  parseStrategies,
  parseStrategyEdits,
  SIGNAL_TYPE_FOR_DIRECTION,
  strategiesAdvisories,
  strategiesDiff,
  strategyProblems,
  unknownStrategiesIn,
} from "../lib/alert-strategies";

/**
 * The dashboard's half of the alert-strategy contract.
 *
 * These tests exist so that `lib/alert-strategies.ts` cannot quietly drift from
 * `worker/worker/analytics/breakouts.py`: the expected numbers below are copied
 * from the Python by hand, on purpose. If a default or a range changes on one side,
 * a test here fails, which is the point — the page and the detector are only
 * honest while they agree.
 */

const WORKER_DEFAULTS = {
  session_break: { enabled: true, threshold_pct: 0.1, confirm_candles: 1, min_volume_ratio: 1.0, cooldown_minutes: 15, telegram: true, oversold: 40, overbought: 60 },
  orb: { enabled: true, threshold_pct: 0.0, confirm_candles: 1, min_volume_ratio: 1.5, cooldown_minutes: 0, telegram: true, oversold: 40, overbought: 60 },
  vwap: { enabled: true, threshold_pct: 0.1, confirm_candles: 2, min_volume_ratio: 1.0, cooldown_minutes: 10, telegram: true, oversold: 40, overbought: 60 },
  // The fourth card, and the reason the shape carries two levels: the worker's
  // `StrategyParams` has one field list for every detector, so a level rule still
  // stores 40/60 even though it never reads them.
  rsi: { enabled: true, threshold_pct: 0.0, confirm_candles: 2, min_volume_ratio: 0.0, cooldown_minutes: 30, telegram: true, oversold: 40, overbought: 60 },
};

test("the page offers exactly the detectors the worker runs", () => {
  assert.deepEqual([...STRATEGIES].sort(), Object.keys(WORKER_DEFAULTS).sort());
});

test("the defaults the page shows are the defaults the worker ships", () => {
  assert.deepEqual(DEFAULT_PARAMS, WORKER_DEFAULTS);
  assert.deepEqual(defaultStrategies().strategies, WORKER_DEFAULTS);
  assert.equal(defaultStrategies().version, 1);
});

test("an absent document parses to the defaults, in either stored shape", () => {
  for (const payload of [null, undefined, {}]) {
    const parsed = parseStrategies(payload);
    assert.ok(parsed.ok);
    if (parsed.ok) assert.deepEqual(parsed.doc, defaultStrategies());
  }
  // A bare strategy map is accepted as well as the wrapped `{strategies: {…}}`.
  const wrapped = parseStrategies({ version: 1, strategies: { orb: { cooldown_minutes: 7 } } });
  assert.ok(wrapped.ok);
  if (wrapped.ok) {
    assert.equal(wrapped.doc.strategies.orb.cooldown_minutes, 7);
    // Untouched strategies keep their own defaults, not the edited one's.
    assert.deepEqual(wrapped.doc.strategies.vwap, WORKER_DEFAULTS.vwap);
  }
});

test("the document 0010 seeded still parses to the shipped defaults", () => {
  // 0011 deliberately leaves `ops_settings['alert_strategies']` alone, so production
  // can hold the row 0010 wrote: three strategies, six fields each. The parser fills
  // what a row omits from the same defaults the worker ships, which is the only
  // reason a database from before the fourth detector exists still describes the
  // rules actually running. If this ever stops being true, the deployed page would
  // be displaying a configuration the worker is not obeying.
  const seeded = {
    version: 1,
    strategies: {
      session_break: { enabled: true, threshold_pct: 0.1, confirm_candles: 1, min_volume_ratio: 1.0, cooldown_minutes: 15, telegram: true },
      orb: { enabled: true, threshold_pct: 0.0, confirm_candles: 1, min_volume_ratio: 1.5, cooldown_minutes: 0, telegram: true },
      vwap: { enabled: true, threshold_pct: 0.1, confirm_candles: 2, min_volume_ratio: 1.0, cooldown_minutes: 10, telegram: true },
    },
  };
  const parsed = parseStrategies(seeded);
  assert.ok(parsed.ok);
  if (parsed.ok) {
    assert.deepEqual(parsed.doc, defaultStrategies());
    assert.deepEqual(parsed.warnings, []);
    assert.equal(parsed.doc.strategies.rsi.oversold, 40, "the missing strategy arrives with the operator's two levels");
  }
});

test("every range the worker refuses, the page refuses too", () => {
  const cases: [keyof typeof FIELD_LIMITS, number][] = [
    ["threshold_pct", 5.5],
    ["threshold_pct", -0.1],
    ["confirm_candles", 0],
    ["confirm_candles", 6],
    ["min_volume_ratio", 10.5],
    ["cooldown_minutes", 241],
    ["cooldown_minutes", -1],
  ];
  for (const [field, value] of cases) {
    const params = { ...WORKER_DEFAULTS.session_break, [field]: value };
    const problems = strategyProblems("session_break", params);
    assert.equal(problems.length, 1, `${field}=${value} must be rejected`);
    assert.match(problems[0] ?? "", new RegExp(String(field)));
    const parsed = parseStrategies({ strategies: { session_break: params } });
    assert.equal(parsed.ok, false, `${field}=${value} must not parse`);
  }
  // The boundaries themselves are legal — 0 volume gate and 0 cooldown are real settings.
  assert.deepEqual(
    strategyProblems("orb", { ...WORKER_DEFAULTS.orb, threshold_pct: 0, min_volume_ratio: 0, cooldown_minutes: 0 }),
    [],
  );
  assert.deepEqual(
    strategyProblems("vwap", { ...WORKER_DEFAULTS.vwap, threshold_pct: 5, confirm_candles: 5, min_volume_ratio: 10, cooldown_minutes: 240 }),
    [],
  );
  // The pair's own boundaries: 1 and 99 are legal, and so is any ordered pair the
  // operator chooses — the defaults are not the only working setting.
  assert.deepEqual(strategyProblems("rsi", { ...WORKER_DEFAULTS.rsi, oversold: 1, overbought: 99 }), []);
});

test("the two RSI levels are checked as an ordered pair, in the worker's words", () => {
  // `0 < oversold < overbought < 100` — one sentence in `StrategyParams.problems()`,
  // and the page must refuse exactly that sentence's cases. The reason for mirroring
  // the text rather than paraphrasing it: if the page says "must be below 60" while
  // the worker says "0 < oversold < overbought < 100", an operator who reads the
  // worker's log after a refused save has no idea which of the two lied.
  const cases: [number, number][] = [
    [40, 40], // inverted-by-tie: a band with no inside
    [70, 60], // crossed
    [0, 60], // a level at zero is a level that can never be under
    [40, 100], // RSI is 0–100; 100 is unreachable, so the rule would never fire
    [-5, 60],
    [40, 101],
  ];
  for (const [oversold, overbought] of cases) {
    const params = { ...WORKER_DEFAULTS.rsi, oversold, overbought };
    const problems = strategyProblems("rsi", params);
    assert.equal(problems.length, 1, `oversold=${oversold} overbought=${overbought} must be rejected`);
    assert.match(problems[0] ?? "", /oversold\/overbought must be 0 < oversold < overbought < 100/);
    assert.equal(parseStrategies({ strategies: { rsi: params } }).ok, false);
  }
  // A level rule that ignores the two numbers still has to store a legal pair,
  // because the shape is shared and the worker validates the whole dataclass.
  assert.equal(parseStrategies({ strategies: { vwap: { oversold: 80, overbought: 20 } } }).ok, false);
  // The form reaches the same refusal through the editor path, so a card can never
  // show a red error the save would have accepted.
  const edit = parseStrategyEdits({ "rsi.oversold": "55", "rsi.overbought": "50" });
  assert.equal(edit.ok, false);
  if (!edit.ok) assert.match(edit.error, /RSI entry \/ overbought/);
});

test("the RSI card's knobs round-trip without disturbing the other three", () => {
  const parsed = parseStrategies({ strategies: { rsi: { oversold: 35, overbought: 55, confirm_candles: 3, cooldown_minutes: 60 } } });
  assert.ok(parsed.ok);
  if (!parsed.ok) return;
  assert.equal(parsed.doc.strategies.rsi.oversold, 35);
  assert.equal(parsed.doc.strategies.rsi.overbought, 55);
  assert.equal(parsed.doc.strategies.rsi.confirm_candles, 3);
  assert.equal(parsed.doc.strategies.rsi.cooldown_minutes, 60);
  // Not shown on the card, still stored at the shipped value: the detector does not
  // read a percentage threshold, but the shape carries one for the other three.
  assert.equal(parsed.doc.strategies.rsi.threshold_pct, 0);
  assert.equal(parsed.doc.strategies.orb.min_volume_ratio, 1.5);

  const edited = parseStrategyEdits({ "rsi.oversold": "30" }, parsed.doc);
  assert.ok(edited.ok);
  if (!edited.ok) return;
  assert.equal(edited.doc.strategies.rsi.oversold, 30);
  assert.equal(edited.doc.strategies.rsi.overbought, 55, "the pair's other half is not reset by editing one side");
  assert.equal(edited.doc.strategies.rsi.cooldown_minutes, 60);
  assert.deepEqual(strategiesDiff(parsed.doc, edited.doc), { "rsi.oversold": { from: 35, to: 30 } });
});

test("the entry side is the one that pages on a falling RSI", () => {
  // The colour/direction inversion the whole feature rests on: green here means a
  // dip under `oversold`, which the worker reports as direction DOWN / RSI_ENTRY,
  // while the red print is direction UP / RSI_OVERBOUGHT. The /alerts today-counts
  // and each rule card read this mapping instead of guessing from colour, so a
  // rewording cannot silently start counting the wrong half of the band.
  assert.deepEqual(SIGNAL_TYPE_FOR_DIRECTION.rsi, { UP: "RSI_OVERBOUGHT", DOWN: "RSI_ENTRY" });
  for (const key of ["session_break", "orb", "vwap"] as const) {
    assert.deepEqual(SIGNAL_TYPE_FOR_DIRECTION[key], { UP: "BREAKOUT_UP", DOWN: "BREAKDOWN" });
  }
});

test("a value that is not a number is reported by strategy and field", () => {
  const parsed = parseStrategies({ strategies: { vwap: { threshold_pct: "wide" } } });
  assert.equal(parsed.ok, false);
  if (!parsed.ok) {
    assert.match(parsed.error, /VWAP/);
    assert.match(parsed.error, /number/);
  }
  const wrongType = parseStrategies({ strategies: { orb: "on" } });
  assert.equal(wrongType.ok, false);
});

test("an unknown strategy name is a warning the page shows, not a save it blocks", () => {
  const payload = { strategies: { ...WORKER_DEFAULTS, ema_cross: { enabled: true } } };
  assert.deepEqual(unknownStrategiesIn(payload), ["ema_cross"]);
  const parsed = parseStrategies(payload);
  assert.ok(parsed.ok);
  if (parsed.ok) {
    assert.equal(parsed.warnings.length, 1);
    assert.match(parsed.warnings[0] ?? "", /ignored unknown strategies: ema_cross/);
  }
});

test("boolean coercion accepts what a form and a JSONB row both produce", () => {
  for (const truthy of [true, 1, "true", "on", "yes", "1"]) {
    const parsed = parseStrategies({ strategies: { orb: { enabled: truthy, telegram: truthy } } });
    assert.ok(parsed.ok);
    if (parsed.ok) assert.equal(parsed.doc.strategies.orb.enabled, true);
  }
  for (const falsy of [false, 0, "false", "off", "no", "0"]) {
    const parsed = parseStrategies({ strategies: { orb: { enabled: falsy } } });
    assert.ok(parsed.ok);
    if (parsed.ok) assert.equal(parsed.doc.strategies.orb.enabled, false);
  }
  const junk = parseStrategies({ strategies: { orb: { enabled: "maybe" } } });
  assert.equal(junk.ok, false);
});

test("an editor submission keeps any field it does not mention", () => {
  const base = defaultStrategies();
  const edited = parseStrategyEdits({ "orb.threshold_pct": "0.25", "vwap.enabled": "false" }, base);
  assert.ok(edited.ok);
  if (!edited.ok) return;
  assert.equal(edited.doc.strategies.orb.threshold_pct, 0.25);
  assert.equal(edited.doc.strategies.orb.min_volume_ratio, 1.5, "unmentioned field must survive");
  assert.equal(edited.doc.strategies.orb.cooldown_minutes, 0, "cooldown 0 is the ORB default, not a reset");
  assert.equal(edited.doc.strategies.vwap.enabled, false);
  assert.equal(edited.doc.strategies.session_break.enabled, true);
  // A checkbox posts "on" for checked and nothing for unchecked; the form omits the
  // latter, so an unchecked switch must not silently read as checked.
  const switched = parseStrategyEdits({ "orb.enabled": "on", "vwap.enabled": "off" }, base);
  assert.ok(switched.ok);
  if (switched.ok) {
    assert.equal(switched.doc.strategies.orb.enabled, true);
    assert.equal(switched.doc.strategies.vwap.enabled, false);
  }
  // Blank means "unchanged", not 0.
  const blank = parseStrategyEdits({ "orb.threshold_pct": "  " }, base);
  assert.ok(blank.ok);
  if (blank.ok) assert.equal(blank.doc.strategies.orb.threshold_pct, 0);
});

test("a partial submission cannot reset a strategy to the shipped defaults", () => {
  // The failure mode this guards: a form that renders one card and posts the whole
  // document would rewrite the other two from its own copy of the defaults.
  const stored = parseStrategies({ strategies: { session_break: { cooldown_minutes: 45, telegram: false } } });
  assert.ok(stored.ok);
  if (!stored.ok) return;
  const edited = parseStrategyEdits({ "orb.min_volume_ratio": "2" }, stored.doc);
  assert.ok(edited.ok);
  if (!edited.ok) return;
  assert.equal(edited.doc.strategies.session_break.cooldown_minutes, 45);
  assert.equal(edited.doc.strategies.session_break.telegram, false);
});

test("a field this build does not know is named, not swallowed", () => {
  // `session_high` is not a strategy — the worker calls it `session_break`. A save
  // that quietly dropped the key would answer "nothing changed" to an operator who
  // had just changed something, which is the worst possible thing for this page to
  // say. The document still saves; the warning rides along in the response.
  const typo = parseStrategyEdits({ "session_high.enabled": "false", "orb.threshold_pct": "0.2" });
  assert.ok(typo.ok);
  if (!typo.ok) return;
  assert.deepEqual(typo.warnings, ["ignored unknown form fields: session_high.enabled"]);
  assert.equal(typo.doc.strategies.orb.threshold_pct, 0.2, "the known field must still be applied");
  const clean = parseStrategyEdits({ "session_break.enabled": "false" });
  assert.ok(clean.ok);
  if (clean.ok) assert.deepEqual(clean.warnings, [], "the page's own keys must never warn");
});

test("the diff names only what moved, which is what the audit row stores", () => {
  const before = defaultStrategies();
  const after = parseStrategies({
    strategies: { session_break: { cooldown_minutes: 30, enabled: true }, orb: { ...WORKER_DEFAULTS.orb, telegram: false }, vwap: WORKER_DEFAULTS.vwap },
  });
  assert.ok(after.ok);
  if (!after.ok) return;
  after.doc.strategies.session_break.threshold_pct = 0.5;
  const diff = strategiesDiff(before, after.doc);
  assert.deepEqual(Object.keys(diff).sort(), [
    "orb.telegram",
    "session_break.cooldown_minutes",
    "session_break.threshold_pct",
  ]);
  assert.deepEqual(diff["session_break.threshold_pct"], { from: 0.1, to: 0.5 });
  assert.deepEqual(diff["orb.telegram"], { from: true, to: false });
  assert.deepEqual(strategiesDiff(before, before), {});
});

test("the advisories catch the two settings that surprise people", () => {
  const allOff = parseStrategies({
    strategies: Object.fromEntries(STRATEGIES.map((key) => [key, { enabled: false }])),
  });
  assert.ok(allOff.ok);
  if (allOff.ok) {
    const notes = strategiesAdvisories(allOff.doc);
    assert.equal(notes.length, 1);
    assert.match(notes[0] ?? "", /switched off/);
  }
  const noGate = parseStrategies({ strategies: { session_break: { min_volume_ratio: 0, telegram: true } } });
  assert.ok(noGate.ok);
  if (noGate.ok) {
    const notes = strategiesAdvisories(noGate.doc);
    assert.equal(notes.length, 1);
    assert.match(notes[0] ?? "", /Session high \/ low break/);
  }
  // ORB's 1.5× gate is the board's own criterion, so it is not worth warning about;
  // the defaults as shipped produce no advisories at all.
  assert.deepEqual(strategiesAdvisories(defaultStrategies()), []);
});

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  otherTheme,
  resolveTheme,
  themeInitScript,
  themeLabel,
} from "../lib/theme";

/**
 * The theme is the one piece of state this dashboard keeps in the browser, and two
 * failure modes are worth pinning down: a value that is not a palette must not
 * reach the stylesheet (the `data-theme` attribute would then match no token block
 * and the board would render unstyled), and the pre-paint script must not drift
 * from the module that reads storage — a key renamed in one place and not the other
 * silently forgets every operator's choice.
 */

test("only the two named palettes are accepted", () => {
  assert.equal(resolveTheme("dark"), "dark");
  assert.equal(resolveTheme("light"), "light");
});

test("anything else falls back to the night board", () => {
  assert.equal(resolveTheme(null), DEFAULT_THEME);
  assert.equal(resolveTheme(undefined), DEFAULT_THEME);
  assert.equal(resolveTheme(""), DEFAULT_THEME);
  assert.equal(resolveTheme("SYSTEM"), DEFAULT_THEME);
  assert.equal(resolveTheme("constructor"), DEFAULT_THEME);
});

test("the night board is the default, not the operating system's choice", () => {
  assert.equal(DEFAULT_THEME, "dark");
  assert.equal(themeLabel(DEFAULT_THEME), "Dark");
});

test("the two boards toggle, they do not cycle", () => {
  assert.equal(otherTheme("dark"), "light");
  assert.equal(otherTheme("light"), "dark");
});

test("the pre-paint script reads the same key, palettes and default as the module", () => {
  assert.ok(themeInitScript.includes(JSON.stringify(THEME_STORAGE_KEY)));
  assert.ok(themeInitScript.includes(`"dark"`));
  assert.ok(themeInitScript.includes(`"light"`));
  assert.ok(themeInitScript.includes(JSON.stringify(DEFAULT_THEME)));
});

/**
 * The script is a string, so nothing type-checks it and the browser is the only
 * place it has ever really run. Executing it against stubs here is what caught
 * `document.localStorage` — undefined, inside a `try`, so every stored choice fell
 * back to dark without an error anywhere. `document` is deliberately given a
 * poisoned `localStorage` so a script that reaches for the wrong global cannot pass.
 */
function runInitScript(stored: string | null | (() => never)): string {
  const doc: { documentElement: { dataset: { theme?: string } }; localStorage: unknown } = {
    documentElement: { dataset: {} },
    localStorage: { getItem: () => {
      throw new Error("the pre-paint script must read storage from window");
    } },
  };
  const win = { localStorage: { getItem: typeof stored === "function" ? stored : () => stored } };
  new Function("document", "window", themeInitScript)(doc, win);
  return doc.documentElement.dataset.theme ?? "(unset)";
}

test("the pre-paint script applies the stored palette before anything else runs", () => {
  assert.equal(runInitScript("light"), "light");
  assert.equal(runInitScript("dark"), "dark");
});

test("the pre-paint script falls back to the night board, and never throws", () => {
  assert.equal(runInitScript(null), "dark");
  assert.equal(runInitScript("sepia"), "dark");
  // A browser that throws on read (private mode, blocked storage) still gets a board.
  assert.equal(runInitScript(() => {
    throw new Error("SecurityError");
  }), "dark");
});

test("the script cannot throw its way into a blank page", () => {
  // Private browsing can refuse the read as well as the write.
  assert.ok(themeInitScript.includes("try"));
  assert.ok(themeInitScript.includes("catch"));
  // It sets exactly the attribute the stylesheet hangs the light tokens on.
  assert.ok(themeInitScript.includes("documentElement.dataset.theme"));
});

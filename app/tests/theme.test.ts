import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_THEME,
  SYSTEM_LIGHT_QUERY,
  THEME_STORAGE_KEY,
  otherTheme,
  preferredTheme,
  resolveTheme,
  storedTheme,
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

test("the night board is the fallback of last resort", () => {
  // Not the answer to "what does this machine want" — it is the answer when nothing
  // else can be read: no stored choice and no readable operating-system preference.
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
 *
 * `os` is the machine's own preference: "light", "dark", "absent" (no `matchMedia`
 * at all, as in an old browser) and "throws" (a stub that refuses the question).
 */
function runInitScript(
  stored: string | null | (() => never),
  os: "light" | "dark" | "absent" | "throws" = "absent",
): string {
  const doc: { documentElement: { dataset: { theme?: string } }; localStorage: unknown } = {
    documentElement: { dataset: {} },
    localStorage: { getItem: () => {
      throw new Error("the pre-paint script must read storage from window");
    } },
  };
  const matchMedia =
    os === "absent"
      ? undefined
      : os === "throws"
        ? () => {
            throw new Error("SecurityError");
          }
        : () => ({ matches: os === "light" });
  const win = {
    localStorage: { getItem: typeof stored === "function" ? stored : () => stored },
    ...(matchMedia ? { matchMedia } : {}),
  };
  new Function("document", "window", themeInitScript)(doc, win);
  return doc.documentElement.dataset.theme ?? "(unset)";
}

test("the pre-paint script applies the stored palette before anything else runs", () => {
  assert.equal(runInitScript("light", "dark"), "light");
  assert.equal(runInitScript("dark", "light"), "dark");
  // A choice the operator made outranks the machine they are sitting at.
  assert.equal(runInitScript("dark", "light"), "dark");
});

test("with no stored choice the pre-paint script asks the operating system", () => {
  assert.equal(runInitScript(null, "light"), "light");
  assert.equal(runInitScript(null, "dark"), "dark");
  // A hostile or stale value is not a preference; it is the absence of one, and the
  // OS then decides — which is what the operator on a daylight machine expects.
  assert.equal(runInitScript("sepia", "light"), "light");
});

test("the pre-paint script falls back to the night board, and never throws", () => {
  assert.equal(runInitScript(null), "dark");
  assert.equal(runInitScript("sepia"), "dark");
  // A browser that throws on read (private mode, blocked storage) still gets a board.
  assert.equal(runInitScript(() => {
    throw new Error("SecurityError");
  }, "light"), "light");
  // …and so does a browser that throws on the media query.
  assert.equal(runInitScript(null, "throws"), "dark");
  assert.equal(runInitScript("light", "throws"), "light");
});

test("the script cannot throw its way into a blank page", () => {
  // Private browsing can refuse the read as well as the write.
  assert.ok(themeInitScript.includes("try"));
  assert.ok(themeInitScript.includes("catch"));
  // It sets exactly the attribute the stylesheet hangs the light tokens on.
  assert.ok(themeInitScript.includes("documentElement.dataset.theme"));
  // The media query it asks is the one the module documents, not a second copy.
  assert.ok(themeInitScript.includes(JSON.stringify(SYSTEM_LIGHT_QUERY)));
});

/**
 * The same three-step order, on the module side: the helpers `adoptStoredTheme`
 * runs when another tab changes the choice must agree with the pre-paint script,
 * or a second tab ends up on a palette the first tab never showed.
 */
function withWindow(stub: Record<string, unknown>, run: () => unknown): unknown {
  const previous = globalThis.window as unknown;
  Object.defineProperty(globalThis, "window", { value: stub, configurable: true, writable: true });
  try {
    return run();
  } finally {
    Object.defineProperty(globalThis, "window", {
      value: previous,
      configurable: true,
      writable: true,
    });
  }
}

function browserFor(stored: string | null, os: "light" | "dark"): Record<string, unknown> {
  return {
    localStorage: {
      getItem: () => stored,
      setItem: () => {},
    },
    matchMedia: (query: string) => {
      if (query !== SYSTEM_LIGHT_QUERY) throw new Error(`unexpected query ${query}`);
      return { matches: os === "light" };
    },
    dispatchEvent: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
  };
}

test("a stored choice is the stored choice, and its absence is null", () => {
  assert.equal(withWindow(browserFor("light", "dark"), storedTheme), "light");
  assert.equal(withWindow(browserFor("dark", "light"), storedTheme), "dark");
  assert.equal(withWindow(browserFor(null, "light"), storedTheme), null);
  assert.equal(withWindow(browserFor("sepia", "light"), storedTheme), null);
  // Storage that throws on read is an absent choice, not a palette.
  assert.equal(
    withWindow(
      { ...browserFor(null, "light"), localStorage: { getItem: () => {
        throw new Error("SecurityError");
      } } },
      storedTheme,
    ),
    null,
  );
});

test("preferredTheme is stored choice, then operating system, then night board", () => {
  assert.equal(withWindow(browserFor("dark", "light"), preferredTheme), "dark");
  assert.equal(withWindow(browserFor("light", "dark"), preferredTheme), "light");
  assert.equal(withWindow(browserFor(null, "light"), preferredTheme), "light");
  assert.equal(withWindow(browserFor(null, "dark"), preferredTheme), "dark");
  assert.equal(
    withWindow({ ...browserFor(null, "light"), matchMedia: undefined }, preferredTheme),
    DEFAULT_THEME,
  );
  assert.equal(
    withWindow(
      { ...browserFor(null, "light"), matchMedia: () => {
        throw new Error("nope");
      } },
      preferredTheme,
    ),
    DEFAULT_THEME,
  );
});

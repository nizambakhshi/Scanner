/**
 * The interface theme: a preference, not a measurement.
 *
 * Two rules shape this module. The board must never repaint on load — the
 * dashboard is read at a glance during a live session, so the choice is applied
 * by an inline script before the first paint rather than after hydration. And the
 * default is the night board: this is the product's identity, and an operating
 * system in light mode must not silently cost the operator the display they chose
 * once. Only an explicit stored choice overrides it.
 *
 * The state lives in localStorage because it belongs to the browser and the
 * screen, not to the account: two operators on two machines can sit at the same
 * watchlist in different palettes, and a failed read of either storage path falls
 * back to the default instead of blocking the page.
 */

export type Theme = "dark" | "light";

/** The key the choice is stored under, in one place so the pre-paint script and
 *  the button cannot drift apart. */
export const THEME_STORAGE_KEY = "scanner-theme";

/** The night board is the default, and the fallback for any unreadable value. */
export const DEFAULT_THEME: Theme = "dark";

export const THEMES: readonly Theme[] = ["dark", "light"];

/** Anything that is not one of the two palettes — null, a stale key, a value
 *  typed into devtools — resolves to the default rather than to a broken board. */
export function resolveTheme(value: string | null | undefined): Theme {
  return THEMES.includes(value as Theme) ? (value as Theme) : DEFAULT_THEME;
}

/** The next theme a click produces: the boards toggle, they do not cycle. */
export function otherTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark";
}

/** A theme's word, as the operator reads it on the button. */
export function themeLabel(theme: Theme): string {
  return theme === "dark" ? "Dark" : "Light";
}

/**
 * The pre-paint script, rendered inline in the root layout's <head>.
 *
 * It is a string built from the constants above, so the storage key, the two
 * palette names and the default cannot disagree with the module that reads them.
 * It runs synchronously before the body paints and writes `data-theme` on the
 * <html> element, which is the single switch `globals.css` hangs the light tokens
 * on.
 *
 * Note the two arguments: storage is on `window`, the attribute is on
 * `document.documentElement`. Reading `document.localStorage` is undefined, and
 * the guard below would swallow that `TypeError` and quietly hand every operator
 * the dark board whatever they had chosen — which is exactly what an earlier
 * version of this line did. `tests/theme.test.ts` executes this string against
 * stubs that poison `document.localStorage` so the mistake cannot come back.
 *
 * Private-browsing storage throws on read as well as write, hence the guard:
 * losing the preference is acceptable, losing the page is not.
 */
export const themeInitScript = [
  `(function (doc, win) {`,
  `  var stored = null;`,
  `  try { stored = win.localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)}); } catch (e) {}`,
  `  doc.documentElement.dataset.theme =`,
  `    stored === ${JSON.stringify("light")} || stored === ${JSON.stringify("dark")}`,
  `      ? stored : ${JSON.stringify(DEFAULT_THEME)};`,
  `})(document, window);`,
].join("");

/**
 * The applied theme is the `data-theme` attribute, not storage: that attribute is
 * what the pre-paint script and the stylesheet actually agreed on, and it is what
 * the screen is drawn from. Reading it means the button can never claim a palette
 * the board is not wearing.
 */
export function readAppliedTheme(): Theme {
  return resolveTheme(document.documentElement.dataset.theme);
}

/**
 * Switch the board. The attribute changes the pixels, storage remembers the choice
 * for the next visit, and the event lets every open control on this page agree
 * immediately. A browser that refuses storage still gets the switch for the session.
 */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Preference forgotten; nothing else about this page is affected.
  }
  window.dispatchEvent(new Event(THEME_EVENT));
}

/**
 * Adopt the choice another tab just made.
 *
 * A `storage` event fires only in the *other* tabs, and nothing there re-runs the
 * pre-paint script, so this is the one place outside load time where the attribute
 * is written from storage. The event implies working storage, and `resolveTheme`
 * keeps a hostile value out of the attribute.
 */
export function adoptStoredTheme(): Theme {
  let stored: string | null = null;
  try {
    stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    stored = null;
  }
  const theme = resolveTheme(stored);
  document.documentElement.dataset.theme = theme;
  return theme;
}

/** Dispatched by `applyTheme` so open controls re-read the attribute. */
export const THEME_EVENT = "scanner-theme-change";

/**
 * Subscribe every theme control to both ways the applied theme can change: a click
 * in this tab, and a click in another one.
 */
export function subscribeToTheme(onStoreChange: () => void): () => void {
  const onLocal = () => onStoreChange();
  const onCrossTab = (event: StorageEvent) => {
    if (event.key !== null && event.key !== THEME_STORAGE_KEY) return;
    adoptStoredTheme();
    onStoreChange();
  };
  window.addEventListener(THEME_EVENT, onLocal);
  window.addEventListener("storage", onCrossTab);
  return () => {
    window.removeEventListener(THEME_EVENT, onLocal);
    window.removeEventListener("storage", onCrossTab);
  };
}

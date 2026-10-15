/**
 * The interface theme: a preference, not a measurement.
 *
 * Two rules shape this module. The board must never repaint on load — the
 * dashboard is read at a glance during a live session, so the choice is applied
 * by an inline script before the first paint rather than after hydration. And the
 * choice is honoured in one order only, the order the Cadence board uses:
 *
 *   1. an explicit stored choice — dark or light;
 *   2. no stored choice — the operating system's preference;
 *   3. nothing readable (private mode, no `matchMedia`, a hostile value) — the
 *      night board.
 *
 * The night board remains the last word because it is the product's identity, but
 * a first visit on a machine set to daylight now gets daylight instead of being
 * handed a black screen it did not ask for. An operator who prefers the night board
 * on such a machine clicks the button once and the stored choice wins forever
 * after, OS preference notwithstanding.
 *
 * The state lives in localStorage because it belongs to the browser and the
 * screen, not to the account: two operators on two machines can sit at the same
 * watchlist in different palettes, and a failed read of either storage path falls
 * back down that list instead of blocking the page.
 */

export type Theme = "dark" | "light";

/** The key the choice is stored under, in one place so the pre-paint script and
 *  the button cannot drift apart. */
export const THEME_STORAGE_KEY = "scanner-theme";

/** The night board is the fallback of last resort, and the palette the server
 *  HTML is rendered with before the pre-paint script has had its say. */
export const DEFAULT_THEME: Theme = "dark";

export const THEMES: readonly Theme[] = ["dark", "light"];

/** Anything that is not one of the two palettes — null, a stale key, a value
 *  typed into devtools — resolves to the default rather than to a broken board. */
export function resolveTheme(value: string | null | undefined): Theme {
  return THEMES.includes(value as Theme) ? (value as Theme) : DEFAULT_THEME;
}

/** The media query the fallback reads. One constant, so the pre-paint script, the
 *  module and the tests cannot each invent their own. */
export const SYSTEM_LIGHT_QUERY = "(prefers-color-scheme: light)";

/**
 * What the operating system asks for, as far as this browser is willing to say.
 * A `matchMedia` that is missing or throws is not a preference for light; it is no
 * preference at all, so it returns the night board.
 */
export function systemTheme(): Theme {
  try {
    return window.matchMedia?.(SYSTEM_LIGHT_QUERY).matches ? "light" : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** A browser's stored choice, or null when there is none to read. */
export function storedTheme(): Theme | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    // Private mode, blocked storage — treated as "no choice", not as a palette.
    return null;
  }
  return THEMES.includes(raw as Theme) ? (raw as Theme) : null;
}

/** The palette a visitor who has never pressed the button should get: their OS's,
 *  and the night board when the OS has no opinion the browser will share. */
export function preferredTheme(): Theme {
  return storedTheme() ?? systemTheme();
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
 * palette names, the media query and the fallback cannot disagree with the module
 * that reads them. It runs synchronously before the body paints and writes
 * `data-theme` on the <html> element, which is the single switch `globals.css`
 * hangs the light tokens on.
 *
 * The order inside it is the order documented at the top of this file — stored
 * choice, then operating system, then the night board — and it is why this is a
 * script rather than a `useEffect`: an operator whose OS is in daylight must not
 * see the night board flash first.
 *
 * Note the two arguments: storage and `matchMedia` are on `window`, the attribute
 * is on `document.documentElement`. Reading `document.localStorage` is undefined,
 * and the guard below would swallow that `TypeError` and quietly hand every
 * operator the fallback board whatever they had chosen — which is exactly what an
 * earlier version of this line did. `tests/theme.test.ts` executes this string
 * against stubs that poison `document.localStorage` so the mistake cannot come
 * back, and against a `matchMedia` that is absent, agrees, and throws.
 *
 * Private-browsing storage throws on read as well as write, hence the guard:
 * losing the preference is acceptable, losing the page is not.
 */
export const themeInitScript = [
  `(function (doc, win) {`,
  `  var stored = null;`,
  `  try { stored = win.localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)}); } catch (e) {}`,
  `  var theme = stored === ${JSON.stringify("light")} || stored === ${JSON.stringify("dark")}`,
  `    ? stored : null;`,
  `  if (theme === null) {`,
  `    try { theme = win.matchMedia(${JSON.stringify(SYSTEM_LIGHT_QUERY)}).matches`,
  `      ? ${JSON.stringify("light")} : ${JSON.stringify("dark")}; } catch (e) {}`,
  `  }`,
  `  doc.documentElement.dataset.theme = theme === null ? ${JSON.stringify(DEFAULT_THEME)} : theme;`,
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
 * is written from storage. The event implies working storage, but the value may
 * have been *removed* rather than set — a cleared site data, another tab pressing
 * a reset — and then the visitor is back to having no explicit choice, so the same
 * order as load time applies: stored, then operating system, then the night board.
 */
export function adoptStoredTheme(): Theme {
  const theme = preferredTheme();
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

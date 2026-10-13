"use client";

import { useSyncExternalStore } from "react";
import {
  DEFAULT_THEME,
  otherTheme,
  readAppliedTheme,
  applyTheme,
  subscribeToTheme,
  themeLabel,
} from "@/lib/theme";

/**
 * The theme button: night board, daylight board, one click.
 *
 * The visible word and glyph name the board you are **looking at** — on a screen
 * whose chrome is a row of state readouts (session phase, SIM, data quality), a
 * control that reports a state is the native idiom. The tooltip and the accessible
 * name spell out the action: "Switch to the light theme".
 *
 * The state is the `data-theme` attribute itself, read through an external store
 * rather than copied into component state: the pre-paint script may have already
 * applied the stored choice before React mounts, another tab can change it while
 * this one is open, and a `useState` mirror would drift from both. Server renders
 * the default board, then React re-reads the attribute during hydration.
 */
export function ThemeToggle({ className = "" }: { className?: string }) {
  const theme = useSyncExternalStore(subscribeToTheme, readAppliedTheme, () => DEFAULT_THEME);
  const next = otherTheme(theme);

  return (
    <button
      type="button"
      onClick={() => applyTheme(next)}
      aria-label={`Switch to the ${next} theme`}
      title={`${themeLabel(theme)} board — click for the ${themeLabel(next).toLowerCase()} one`}
      className={`flex items-center gap-1.5 rounded-md border border-line px-2 py-1 text-ink-2 transition hover:border-line-2 hover:bg-hover hover:text-ink ${className}`}
    >
      {theme === "dark" ? <MoonIcon /> : <SunIcon />}
      <span className="font-display text-[11px] font-semibold uppercase tracking-[0.12em]">
        {themeLabel(theme)}
      </span>
    </button>
  );
}

/** Both glyphs are 14px strokes on `currentColor`, so they take the palette's ink
 *  without knowing anything about it. */
function SunIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
    >
      <circle cx="12" cy="12" r="4.5" />
      <path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5Z" />
    </svg>
  );
}

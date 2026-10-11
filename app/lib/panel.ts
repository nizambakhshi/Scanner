/**
 * Per-panel reads.
 *
 * A page is a set of independent facts, so a page fetches them independently: one
 * failed view must blank its own panel and say why, not the whole screen. The
 * error text is already truncated and DSN-stripped by `panelError`, which is what
 * makes it safe to render at all (§19).
 */

import { panelError } from "./source";

export type PanelRead<T> = { ok: true; data: T } | { ok: false; error: string };

export async function panel<T>(operation: () => Promise<T>): Promise<PanelRead<T>> {
  try {
    return { ok: true, data: await operation() };
  } catch (error) {
    return { ok: false, error: panelError(error) };
  }
}

/** Type guard for the calling page: `if (ok(rows)) …` narrows to the data branch. */
export function ok<T>(value: PanelRead<T>): value is { ok: true; data: T } {
  return value.ok === true;
}

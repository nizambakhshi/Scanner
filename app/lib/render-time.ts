/**
 * The clock a server component renders against.
 *
 * Components must be pure, and `Date.now()` is not: the same render would print a
 * different "3 min ago" every time React replays it. The read therefore happens
 * once, in the data layer, before JSX — the page then formats every relative
 * timestamp against that one instant, so a panel cannot say "just now" while the
 * one beside it says "4 min ago".
 *
 * On a cache miss this is the request time, which is what an operator means by
 * "now" when they load a page.
 */
export function renderInstant(): Date {
  return new Date();
}

/** Milliseconds since the epoch, for the `ago(...)` helpers in `lib/format.ts`. */
export function renderNow(): number {
  return renderInstant().getTime();
}

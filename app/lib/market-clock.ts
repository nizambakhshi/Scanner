/**
 * The NSE session clock — pure arithmetic, no date library, no locale.
 *
 * India has no DST, so IST is a fixed UTC+05:30 shift and every wall-clock fact
 * falls out of the epoch: IST-time = t + 330 minutes, the IST day number is that
 * value floored to days, and the weekday falls out of epoch day 0 being a
 * Thursday.
 *
 * The schedule modeled here is the one this scanner acts on: 09:00–09:15 IST is
 * the pre-open window (the call auction, when the worker warms up), 09:15–15:30
 * is the session, everything else is closed, Monday–Friday. Exchange holidays
 * are NOT modeled: on a holiday this clock says OPEN while the feed is silent.
 * The Board's feed chip — live heartbeat data — is the ground truth, and the
 * strip that renders this clock labels itself a session window for exactly that
 * reason. Guessing a holiday list would be worse than stating what is modeled.
 */

const DAY_MS = 86_400_000;
export const IST_OFFSET_MS = 330 * 60_000;

const PRE_OPEN_MS = 9 * 3_600_000; // 09:00 IST
const OPEN_MS = 9 * 3_600_000 + 15 * 60_000; // 09:15 IST
const CLOSE_MS = 15 * 3_600_000 + 30 * 60_000; // 15:30 IST

export type SessionPhase = "PRE_OPEN" | "OPEN" | "CLOSED";

export type SessionClock = {
  phase: SessionPhase;
  /** Zero-padded IST wall-clock parts, for display only. */
  hh: string;
  mm: string;
  ss: string;
  /** "closes in 5h 42m" · "opens in 14m" · "opens Mon 09:15" */
  countdown: string;
};

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

/** 0 = Sunday … 6 = Saturday, for an IST-shifted epoch day number. */
function istWeekday(dayNumber: number): number {
  return (((dayNumber + 4) % 7) + 7) % 7; // epoch day 0 (1970-01-01) was a Thursday
}

function duration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

/** The 09:15 IST strictly after `shifted`, weekends skipped. */
function nextOpenAt(shifted: number): number {
  const day = Math.floor(shifted / DAY_MS);
  const msOfDay = shifted - day * DAY_MS;
  for (let ahead = 0; ahead < 8; ahead += 1) {
    const weekday = istWeekday(day + ahead);
    if (weekday === 0 || weekday === 6) continue;
    if (ahead === 0 && msOfDay >= OPEN_MS) continue; // today's open has passed
    return (day + ahead) * DAY_MS + OPEN_MS;
  }
  return (day + 1) * DAY_MS + OPEN_MS; // unreachable within a week
}

export function sessionClock(now: Date): SessionClock {
  const shifted = now.getTime() + IST_OFFSET_MS;
  const day = Math.floor(shifted / DAY_MS);
  const msOfDay = shifted - day * DAY_MS;
  const weekday = istWeekday(day);

  const minutesOfDay = Math.floor(msOfDay / 60_000);
  const hh = String(Math.floor(minutesOfDay / 60)).padStart(2, "0");
  const mm = String(minutesOfDay % 60).padStart(2, "0");
  const ss = String(Math.floor(msOfDay / 1000) % 60).padStart(2, "0");

  const tradingDay = weekday >= 1 && weekday <= 5;
  const phase: SessionPhase = !tradingDay
    ? "CLOSED"
    : msOfDay < PRE_OPEN_MS
      ? "CLOSED"
      : msOfDay < OPEN_MS
        ? "PRE_OPEN"
        : msOfDay < CLOSE_MS
          ? "OPEN"
          : "CLOSED";

  let countdown: string;
  if (phase === "OPEN") {
    countdown = `closes in ${duration(Math.ceil((CLOSE_MS - msOfDay) / 60_000))}`;
  } else {
    const target = nextOpenAt(shifted);
    const sameDay = Math.floor(target / DAY_MS) === day;
    countdown = sameDay
      ? `opens in ${duration(Math.ceil((target - shifted) / 60_000))}`
      : `opens ${WEEKDAYS[istWeekday(Math.floor(target / DAY_MS))]} 09:15`;
  }

  return { phase, hh, mm, ss, countdown };
}

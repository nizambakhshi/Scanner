"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { SignOutButton } from "./SignOutButton";
import { ThemeToggle } from "./ThemeToggle";
import { sessionClock, type SessionPhase } from "@/lib/market-clock";

const LINKS = [
  { href: "/", label: "Board" },
  { href: "/watchlist", label: "Watchlist" },
  { href: "/strategy", label: "Strategy" },
  { href: "/alerts", label: "Alert rules" },
  { href: "/history", label: "Days" },
  { href: "/health", label: "Health" },
  { href: "/config", label: "Config" },
  { href: "/account", label: "Account" },
] as const;

export function Nav({ email }: { email: string }) {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-surface/95 backdrop-blur">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5">
        <Link href="/" className="flex w-full shrink-0 items-baseline gap-2 sm:w-auto">
          <span className="font-serif text-[19px] italic leading-none tracking-tight">Bullish Scanner</span>
          <span className="font-display hidden text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-2 lg:inline">
            NSE · 1m
          </span>
        </Link>

        <nav className="flex flex-1 flex-wrap items-center gap-x-0.5 gap-y-1">
          {LINKS.map((link) => {
            const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-md border px-2 py-1.5 text-[13px] font-medium transition ${
                  active
                    ? "border-line bg-surface-2 text-ink"
                    : "border-transparent text-ink-2 hover:bg-hover hover:text-ink"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <div className="flex w-full flex-wrap items-center gap-x-3 gap-y-2 xl:w-auto">
          <SessionStrip />
          <div className="flex items-center gap-1.5">
            <ThemeToggle />
            <RefreshButton />
          </div>
          <span className="hidden max-w-[180px] truncate text-[12px] text-muted 2xl:inline" title={email}>
            {email}
          </span>
          <SignOutButton />
        </div>
      </div>
    </header>
  );
}

const PHASE_FACE: Record<SessionPhase, { label: string; dot: string; tone: string }> = {
  OPEN: { label: "Open", dot: "bg-positive dot-live", tone: "text-positive" },
  PRE_OPEN: { label: "Pre-open", dot: "bg-amber", tone: "text-amber" },
  CLOSED: { label: "Closed", dot: "bg-muted-2", tone: "text-muted" },
};

/**
 * The session strip: the one live instrument on the chrome.
 *
 * It answers the question every other number on the screen is relative to —
 * where in the trading day are we — with an IST clock ticking in tabular
 * figures. It is deliberately computed from the browser clock and the fixed
 * IST offset, not from the worker: the strip must be right even when the feed
 * is dead, and the Board's feed chip is the thing that reports the feed.
 *
 * Rendered only after mount so the server HTML never disagrees with the
 * client's clock; the placeholder holds the exact footprint.
 */
function SessionStrip() {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const id = window.setInterval(() => setNow(new Date()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  if (!now) {
    return (
      <div
        className="flex items-center gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-1"
        aria-hidden
      >
        <span className="h-1.5 w-1.5 rounded-full bg-muted-2" />
        <span className="font-display text-[11px] font-semibold uppercase tracking-[0.14em] text-muted-2">─────</span>
        <span className="num text-[12px] text-muted-2">--:--:--</span>
        <span className="text-[10px] uppercase tracking-wide text-muted-2">IST</span>
      </div>
    );
  }

  const clock = sessionClock(now);
  const face = PHASE_FACE[clock.phase];
  return (
    <div
      className="flex items-center gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-1"
      title="NSE session window (09:15–15:30 IST, Mon–Fri). Exchange holidays are not modeled here — the feed chip on the Board is the ground truth."
    >
      <span className={`h-1.5 w-1.5 rounded-full ${face.dot}`} aria-hidden />
      <span className={`font-display text-[11px] font-semibold uppercase tracking-[0.14em] ${face.tone}`}>
        {face.label}
      </span>
      <span className="num text-[12px] font-medium text-ink">
        {clock.hh}:{clock.mm}:{clock.ss}
      </span>
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-2">IST</span>
      <span className="hidden text-[11px] text-muted lg:inline">{clock.countdown}</span>
    </div>
  );
}

function RefreshButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        setBusy(true);
        router.refresh();
        window.setTimeout(() => setBusy(false), 600);
      }}
      className="rounded-md border border-line px-2.5 py-1 text-[12px] font-medium text-ink-2 transition hover:border-line-2 hover:bg-hover"
      aria-label="Reload from the data source"
    >
      {busy ? "Reloading…" : "Reload"}
    </button>
  );
}

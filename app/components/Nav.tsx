"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { SignOutButton } from "./SignOutButton";

const LINKS = [
  { href: "/", label: "Board" },
  { href: "/watchlist", label: "Watchlist" },
  { href: "/stocks", label: "Stocks" },
  { href: "/signals", label: "Alerts" },
  { href: "/history", label: "Days" },
  { href: "/health", label: "Health" },
  { href: "/config", label: "Config" },
] as const;

export function Nav({ email }: { email: string }) {
  const pathname = usePathname();
  const [at, setAt] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setAt(new Date()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-surface/95 backdrop-blur">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" className="flex items-baseline gap-2">
          <span className="text-[15px] font-bold tracking-tight">Bullish Scanner</span>
          <span className="hidden text-[11px] uppercase tracking-[0.16em] text-muted sm:inline">NSE · 1m</span>
        </Link>

        <nav className="flex flex-1 flex-wrap items-center gap-1">
          {LINKS.map((link) => {
            const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`rounded-md px-2.5 py-1.5 text-[13px] font-medium transition ${
                  active ? "bg-ink text-white" : "text-ink-2 hover:bg-surface-2"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-3">
          <span className="num text-[12px] text-muted" title="Asia/Kolkata">
            {new Intl.DateTimeFormat("en-GB", {
              timeZone: "Asia/Kolkata",
              hour: "2-digit",
              minute: "2-digit",
            }).format(at)}
            <span className="ml-1 uppercase tracking-wide text-muted-2">IST</span>
          </span>
          <RefreshButton />
          <span className="hidden max-w-[180px] truncate text-[12px] text-muted md:inline" title={email}>
            {email}
          </span>
          <SignOutButton />
        </div>
      </div>
    </header>
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
      className="rounded-md border border-line px-2.5 py-1 text-[12px] font-medium text-ink-2 transition hover:border-line-2 hover:bg-surface-2"
      aria-label="Reload from the data source"
    >
      {busy ? "Reloading…" : "Reload"}
    </button>
  );
}

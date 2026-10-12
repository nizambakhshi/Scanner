import Link from "next/link";
import type { ReactNode } from "react";

export function Panel({
  title,
  hint,
  action,
  children,
  className = "",
}: {
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      <div className="panel-title">
        <div className="flex items-baseline gap-2">
          <h2 className="text-[13px] font-bold tracking-tight">{title}</h2>
          {hint ? <span className="text-[11px] text-muted">{hint}</span> : null}
        </div>
        {action}
      </div>
      <div>{children}</div>
    </section>
  );
}

export function Metric({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "positive" | "negative" | "amber";
}) {
  const colour =
    tone === "positive" ? "text-positive" : tone === "negative" ? "text-negative" : tone === "amber" ? "text-amber" : "text-ink";
  return (
    <div className="card px-4 py-3">
      <p className="eyebrow">{label}</p>
      <p className={`num mt-1 text-[22px] font-semibold leading-none ${colour}`}>{value}</p>
      {hint ? <p className="mt-1.5 text-[12px] leading-snug text-muted">{hint}</p> : null}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-4 py-6 text-[13px] text-muted">{children}</p>;
}

/** A state that is honestly unknown is shown as unknown. Never a dash pretending
 *  to be a zero. */
export function ErrorNote({ title = "Could not read", detail }: { title?: string; detail: string }) {
  return (
    <div className="border-l-2 border-negative bg-negative/5 px-4 py-3">
      <p className="text-[12px] font-semibold uppercase tracking-wide text-negative">{title}</p>
      <p className="num mt-1 break-words text-[12px] text-ink-2">{detail}</p>
    </div>
  );
}

/** Every page opens the same way: what this screen answers, and over what data. */
export function PageHeading({
  title,
  subtitle,
  aside,
}: {
  title: string;
  subtitle?: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 pb-1">
      <div>
        <h1 className="text-[19px] font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="mt-1 max-w-[70ch] text-[13px] leading-snug text-muted">{subtitle}</p> : null}
      </div>
      {aside ? <div className="flex flex-wrap items-center gap-2">{aside}</div> : null}
    </div>
  );
}

/** A tally that is also its filter: the count reports the full set, the click
 *  narrows the table to exactly those rows. The active chip is filled, so the
 *  current cut is unmistakable at a glance. */
export function FilterChip({
  href,
  label,
  value,
  tone = "text-ink-2",
  active,
}: {
  href: string;
  label: string;
  value: number;
  tone?: string;
  active: boolean;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "true" : undefined}
      className={`chip ${
        active
          ? "border-ink bg-ink text-surface"
          : "border-line bg-surface text-muted hover:border-line-2 hover:text-ink-2"
      }`}
    >
      {label} <span className={`num font-bold ${active ? "text-surface" : tone}`}>{value}</span>
    </Link>
  );
}

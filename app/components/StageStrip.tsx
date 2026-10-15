import Link from "next/link";
import type { Stage } from "@/lib/stages";

/**
 * The day as a pipeline: six stages, left to right, in the order the scanner
 * actually moves through them. The numbering is not decoration — the sequence
 * is the strategy's enforced order, and a stage that fires out of turn would be
 * a bug an operator needs to see.
 *
 * Below `xl` the strip stacks (2-up on phones, 3-up on tablets) and the arrows
 * drop, because a chevron that points at nothing is worse than no chevron.
 */
export function StageStrip({ stages }: { stages: Stage[] }) {
  return (
    <section aria-label="Today's sequence" className="card px-2 py-3 sm:px-3">
      <ol className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 xl:grid-cols-6">
        {stages.map((stage) => (
          <li key={stage.n} className="relative">
            <Link
              href={stage.href}
              className="block rounded-md px-2 py-1.5 transition hover:bg-hover"
              title={stage.hint}
            >
              <div className="flex items-baseline gap-1.5">
                <span className="font-display text-[10px] font-semibold tracking-[0.18em] text-muted-2">
                  {String(stage.n).padStart(2, "0")}
                </span>
                <span className="eyebrow">{stage.label}</span>
              </div>
              <p className={`num mt-1.5 text-[20px] font-semibold leading-none ${toneClass(stage.tone)}`}>
                {stage.value}
              </p>
              <p className="mt-1 text-[11px] leading-snug text-muted">{stage.hint}</p>
            </Link>
            {stage.n < stages.length ? (
              <span aria-hidden className="absolute -right-[13px] top-5 hidden text-[13px] text-muted-2 xl:block">
                →
              </span>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}

function toneClass(tone: Stage["tone"]): string {
  switch (tone) {
    case "positive":
      return "text-positive";
    case "negative":
      return "text-negative";
    case "amber":
      return "text-amber";
    default:
      return "text-ink";
  }
}

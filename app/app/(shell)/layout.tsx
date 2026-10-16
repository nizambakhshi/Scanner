import Link from "next/link";
import { requireUserPage } from "@/lib/auth/session";
import { source } from "@/lib/source";
import { Nav } from "@/components/Nav";
import { SimulatedBanner } from "@/components/SimulatedBanner";

/**
 * The authenticated shell.
 *
 * `requireUserPage()` runs before any data read, so a signed-out visitor never
 * causes a database query at all. The simulated-data banner belongs here rather
 * than on each page: a page that forgets it is the exact failure mode this project
 * treats as dangerous, and one render site cannot be forgotten.
 */
export default async function ShellLayout({ children }: { children: React.ReactNode }) {
  const user = await requireUserPage();
  const meta = await source().meta();
  return (
    <div className="min-h-screen">
      <Nav email={user.displayName ? `${user.displayName} · ${user.email}` : user.email} role={user.role} />
      <SimulatedBanner meta={meta} />
      <main className="mx-auto max-w-[1400px] px-4 pb-16 pt-5">{children}</main>
      <footer className="border-t border-line bg-surface">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-2 px-4 py-3 text-[11px] text-muted">
          <p>
            It never places, modifies or cancels an order, and it holds no broker key. It reads this database, writes
            the two alert settings rows on{" "}
            <Link href="/alerts" className="link">
              Alert rules
            </Link>
            , and sends exactly one Telegram message: the test an operator asks for. Every real alert is raised,
            deduped and delivered by the worker.
          </p>
          <p className="num">source: {meta.source}</p>
        </div>
      </footer>
    </div>
  );
}

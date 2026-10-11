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
      <Nav email={user.displayName ? `${user.displayName} · ${user.email}` : user.email} />
      <SimulatedBanner meta={meta} />
      <main className="mx-auto max-w-[1400px] px-4 pb-16 pt-5">{children}</main>
      <footer className="border-t border-line bg-surface">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-2 px-4 py-3 text-[11px] text-muted">
          <p>
            Read-only dashboard. It never places, modifies or cancels an order, and it cannot send a Telegram message —
            the worker process does that, elsewhere, with credentials this app does not hold.
          </p>
          <p className="num">source: {meta.source}</p>
        </div>
      </footer>
    </div>
  );
}

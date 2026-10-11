import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Bullish Scanner — operator dashboard",
  description:
    "Read-only operator dashboard for the NSE intraday bullish scanner: daily watchlist, alert feed, delivery health and worker status.",
  // A tool showing one person's trading state must not be indexed, and must not
  // describe itself to a crawler. `app/robots.ts` enforces the same rule at the path.
  robots: { index: false, follow: false, noarchive: true },
};

/**
 * The shell. Everything that reads as "the dashboard" lives under the `(shell)`
 * route group, which is where the session check happens. The login form is outside
 * it by necessity — a page that demands a session before it can create one is a
 * redirect loop.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}

import type { Metadata } from "next";
import { themeInitScript } from "@/lib/theme";
import "./globals.css";

/**
 * Three faces, one job each — the same set the Cadence board runs on: IBM Plex Sans
 * reads as prose and as the stamp on every label and legend, IBM Plex Mono carries
 * every number with tabular figures so columns align as scales, and Instrument Serif
 * italic is the voice — the wordmark and the page title, nowhere else.
 *
 * They are self-hosted from `public/fonts` as latin woff2 through `@font-face` in
 * `globals.css`, not pulled through `next/font/google`: the build then needs no
 * network, and the board needs no font CDN at view time. The CSP in
 * `next.config.ts` allows `font-src 'self' data:` only, which is exactly what this
 * arrangement satisfies.
 */

export const metadata: Metadata = {
  title: "Intraday Scanner — operator dashboard",
  description:
    "Operator dashboard for the NSE intraday scanner: daily watchlist, alert feed, delivery health and worker status.",
  // A tool showing one person's trading state must not be indexed, and must not
  // describe itself to a crawler. `app/robots.ts` enforces the same rule at the path.
  robots: { index: false, follow: false, noarchive: true },
};

/**
 * The shell. Everything that reads as "the dashboard" lives under the `(shell)`
 * route group, which is where the session check happens. The login form is outside
 * it by necessity — a page that demands a session before it can create one is a
 * redirect loop.
 *
 * `data-theme` is the single switch the stylesheet hangs the palette on, exactly as
 * on the Cadence board: the server HTML carries the night board, the inline script
 * in the head may overwrite it before the first paint from the stored choice or the
 * operating system's preference, and the attribute stays on the HTML so a browser
 * with JS disabled still gets a coherent (default) palette rather than unstyled
 * tokens. An operator reloading mid-session gets the same board back instantly,
 * never a repaint as the page arrives.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        {/*
          A plain inline script, deliberately not `next/script`: with
          `strategy="beforeInteractive"` Next queues the children and runs them with
          its own loader, which is after the first paint — verified in the browser,
          where a stored light choice came back as a dark board on every fresh load.
          Inline in the head is the only position that is certain to run before the
          body paints, which is the whole point: the operator's board must never
          repaint under them as the page arrives.
        */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}

import type { Metadata } from "next";
import { Barlow, Barlow_Semi_Condensed, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";

/**
 * Three faces, one job each: Barlow reads as prose, Barlow Semi Condensed is the
 * stamp on every label and legend, and IBM Plex Mono carries every number with
 * tabular figures so columns align as scales. Self-hosted by next/font, so the
 * board never waits on a font CDN.
 */
const barlow = Barlow({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-barlow",
  display: "swap",
});
const barlowCondensed = Barlow_Semi_Condensed({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-barlow-condensed",
  display: "swap",
});
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-plex-mono",
  display: "swap",
});

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
    <html lang="en" className={`${barlow.variable} ${barlowCondensed.variable} ${plexMono.variable}`}>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}

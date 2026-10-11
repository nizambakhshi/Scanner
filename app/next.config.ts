import path from "node:path";
import type { NextConfig } from "next";

/**
 * Security posture for a tool that shows money-market state to one operator.
 *
 * The page bodies are behind a session cookie, but headers are set for every
 * response including the login form and the JSON API: the dashboard must never
 * be frameable by another origin (clickjacking a "yes, that's my watchlist"
 * prompt), must never leak its URL to a third party through the referrer, and
 * must never be indexed.
 */
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Robots-Tag", value: "noindex, nofollow" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      // Tailwind's client injection and React's hydration state both need it.
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "font-src 'self' data:",
      "connect-src 'self'",
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
    ].join("; "),
  },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Single-app monorepo: keep Turbopack pointed at this directory, not the repo
  // root, so it never walks the Python worker tree.
  turbopack: { root: path.resolve(__dirname) },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;

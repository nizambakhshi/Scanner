import type { MetadataRoute } from "next";

/**
 * Nothing here is for crawlers: the whole app is one operator's trading state.
 * Disallowing every path is the belt; the `X-Robots-Tag` header in
 * `next.config.ts` is the braces, and it applies even to a route that forgets this
 * file exists. No sitemap is published either — a listed URL is an invitation to
 * look at a page that was never meant to be looked at.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: "*", disallow: "/" }],
  };
}

/**
 * The news desk: the newsrooms' own public RSS, read directly, stored nowhere.
 *
 * There is no table, no worker job and no database behind this module by choice:
 * a feed can lose a headline and nobody's trading day changes, so the cost of a
 * table (migration, retention, a writer) buys nothing the page needs. Each
 * request reads a module-level cache, and the cache is refreshed at most once
 * per `CACHE_TTL_MS` per server instance — eight small XML documents every ten
 * minutes, with a single-flight guard so ten simultaneous page loads start one
 * refresh, not ten (FinceptTerminal's RSS subsystem, minus everything that
 * needed a database or a subprocess to run).
 *
 * What it deliberately does not do, and why:
 *  - No NLP subprocess, no named-entity recognition, no geolocation — the
 *    enrichment below is keyword reads of the text, and the page says so.
 *  - No clustering / velocity / triangulation signals — those need a store of
 *    past articles to compare against; this module keeps none.
 *  - No live-updating feed — a page load is the unit of freshness.
 *
 * The parser is hand-rolled on purpose: RSS 2.0 and Atom are line-oriented
 * enough that one regex pass per item extracts everything this page renders,
 * and the app has exactly three dependencies (next, postgres, react) — a new
 * one for eight known feeds would be the larger risk. The traps it guards are
 * the ones FinceptTerminal's C++ parser documents: ids that embed the fetch
 * time (breaking dedupe), `(?<![\w-])` whole-word keyword guards (so
 * "record-breaking" is not a BREAKING alert), a 300-second future-skew clamp
 * (a feed whose clock runs fast must not sort above everything forever), and
 * http(s)-only links (a `javascript:` URL from a compromised feed must land
 * nowhere).
 */

import { createHash } from "node:crypto";
import { toIst } from "./format";

// ── the catalog ───────────────────────────────────────────────────────────────

export type FeedSource = {
  name: string;
  url: string;
  /** 2 = major financial media. The tier only orders nothing right now; it is
   *  kept so a slow blog feed can be demoted rather than deleted. */
  tier: number;
};

/** Probed live on 2026-10-15: every URL answered 200 with real XML. A feed that
 *  dies shows up on the page as a named failure, never as a silent gap. */
export const FEEDS: FeedSource[] = [
  { name: "Economic Times", url: "https://economictimes.indiatimes.com/rssfeedstopstories.cms", tier: 2 },
  { name: "Moneycontrol", url: "https://www.moneycontrol.com/rss/latestnews.xml", tier: 2 },
  { name: "LiveMint Markets", url: "https://www.livemint.com/rss/markets", tier: 2 },
  { name: "The Hindu Business", url: "https://www.thehindu.com/business/feeder/default.rss", tier: 2 },
  { name: "BBC Business", url: "https://feeds.bbci.co.uk/news/business/rss.xml", tier: 2 },
  { name: "CNBC", url: "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", tier: 2 },
  { name: "MarketWatch", url: "https://feeds.content.dowjones.io/public/rss/mw_topstories", tier: 2 },
  { name: "Investing.com", url: "https://www.investing.com/rss/news.rss", tier: 2 },
];

const CACHE_TTL_MS = 10 * 60_000;
const FETCH_TIMEOUT_MS = 8_000;
const PER_FEED_CAP = 40;
const SUMMARY_MAX = 300;
const HEADLINE_MAX = 200;
const FUTURE_SKEW_MS = 300_000;
const SYMBOL_CAP = 5;

/** A browser-shaped user agent: several of these feeds answer a bare client
 *  with 403. This is the page fetching what any reader's browser fetches. */
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36";

// ── shapes ────────────────────────────────────────────────────────────────────

export type NewsPriority = "FLASH" | "URGENT" | "BREAKING" | "ROUTINE";
export type NewsSentiment = "BULLISH" | "BEARISH" | "NEUTRAL";
export type NewsCategory =
  | "EARNINGS"
  | "CRYPTO"
  | "DEFENSE"
  | "ECONOMIC"
  | "MARKETS"
  | "ENERGY"
  | "TECH"
  | "GEOPOLITICS";

export type NewsArticle = {
  /** Stable across fetches (sha1 of the normalised link) so React keys and
   *  dedupe survive a re-read — an id with a timestamp in it would make every
   *  refresh look like brand-new articles. */
  id: string;
  headline: string;
  link: string;
  source: string;
  /** IST ISO-8601, produced here: no component does timezone arithmetic. */
  at: string;
  summary: string;
  priority: NewsPriority;
  sentiment: NewsSentiment;
  category: NewsCategory | null;
};

export type SourceReport = { name: string; count: number; error: string | null };

export type NewsDesk = {
  items: NewsArticle[];
  /** When the feeds were last actually read — the page's "updated X ago". On a
   *  total outage this stays the old value, because that is the truth. */
  fetchedAt: string;
  reports: SourceReport[];
};

// ── text: entities, tags, extraction ─────────────────────────────────────────

const NAMED_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  hellip: "…",
  mdash: "—",
  ndash: "–",
  lsquo: "‘",
  rsquo: "’",
  ldquo: "“",
  rdquo: "”",
  times: "×",
};

/** `&amp;`, `&#8217;`, `&#x20B9;` → their characters. Unknown entities are left
 *  verbatim rather than guessed at. */
export function decodeEntities(text: string): string {
  return text.replace(/&(#[xX]?[0-9a-fA-F]+|[a-zA-Z]+);/g, (match, body: string) => {
    if (body.startsWith("#")) {
      const hex = body[1] === "x" || body[1] === "X";
      const code = Number.parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10);
      return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : match;
    }
    return NAMED_ENTITIES[body] ?? match;
  });
}

/** Tags out first, entities second — `&lt;b&gt;` in a feed is a literal `<b>`
 *  the reader asked to see, not markup to strip. */
export function stripHtml(html: string): string {
  return decodeEntities(html.replace(/<[^>]*>/g, " ")).replace(/\s+/g, " ").trim();
}

function stripCdata(text: string): string {
  const trimmed = text.trim();
  const match = /^<!\[CDATA\[([\s\S]*?)\]\]>$/.exec(trimmed);
  return match ? match[1].trim() : trimmed;
}

/** The text content of the first `<name>` element, CDATA unwrapped. */
function tag(block: string, name: string): string | null {
  const match = new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, "i").exec(block);
  return match ? stripCdata(match[1]) : null;
}

/** RSS items carry `<link>url</link>`; Atom entries carry `<link href="…"/>`
 *  with `rel` likely absent or "alternate". Anything not http(s) is refused
 *  here, at the boundary, so a hostile feed cannot smuggle `javascript:` into
 *  an anchor tag. */
function entryLink(block: string): string | null {
  const text = tag(block, "link");
  if (text && /^https?:\/\//i.test(text)) return decodeEntities(text);
  for (const match of block.matchAll(/<link\b[^>]*>/gi)) {
    const href = /\bhref\s*=\s*"([^"]*)"/i.exec(match[0])?.[1] ?? /\bhref\s*=\s*'([^']*)'/i.exec(match[0])?.[1];
    if (!href) continue;
    const rel = /\brel\s*=\s*["']([^"']*)["']/i.exec(match[0])?.[1];
    if (rel && rel !== "alternate") continue;
    const decoded = decodeEntities(href.trim());
    if (/^https?:\/\//i.test(decoded)) return decoded;
  }
  return null;
}

/** The best body text the entry has, capped at the length the rows render. */
function entrySummary(block: string): string {
  for (const name of ["description", "summary", "content:encoded", "content"]) {
    const raw = tag(block, name);
    if (!raw) continue;
    const text = stripHtml(raw);
    if (text) return text.length > SUMMARY_MAX ? `${text.slice(0, SUMMARY_MAX)}…` : text;
  }
  return "";
}

/** An entry's instant. Unparseable → now (an article with no date is still
 *  news); more than five minutes ahead → now (feeds with fast clocks would
 *  otherwise pin themselves to the top of a newest-first list forever). */
function entryDate(block: string, now: number): string {
  for (const name of ["pubDate", "published", "updated", "dc:date"]) {
    const raw = tag(block, name);
    if (!raw) continue;
    const parsed = Date.parse(raw);
    if (Number.isFinite(parsed)) {
      return toIst(new Date(parsed > now + FUTURE_SKEW_MS ? now : parsed))!;
    }
  }
  return toIst(new Date(now))!;
}

// ── enrichment: keyword reads of the text, nothing more ─────────────────────

const PRIORITY_RULES: Array<[NewsPriority, RegExp]> = [
  ["FLASH", /(?<![\w-])(?:flash|just in)\b/i],
  ["URGENT", /(?<![\w-])urgent\b/i],
  ["BREAKING", /(?<![\w-])(?:breaking|alerts?)\b/i],
];

/** The `(?<![\w-])` guard is the whole point: "record-breaking" and other
 *  compounds must not read as alerts. */
export function classifyPriority(headline: string): NewsPriority {
  for (const [priority, rule] of PRIORITY_RULES) {
    if (rule.test(headline)) return priority;
  }
  return "ROUTINE";
}

// Explicit inflections rather than stemming: a stemmer is a dependency-sized
// thing to hide inside an eight-feed page, and Indian wire copy uses a small,
// knowable vocabulary ("surges", "plunges", "slips"). Uniform weight 1 per hit;
// the net decides. This is a lexicon, not a model, and the page says so.
const POSITIVE_TERMS = [
  "beat", "beats", "surge", "surges", "surged", "surging", "soar", "soars", "soared", "rally", "rallies", "rallied",
  "jump", "jumps", "jumped", "climb", "climbs", "climbed", "gain", "gains", "gained", "rise", "rises", "rising",
  "rose", "strong", "stronger", "strongest", "growth", "profit", "profits", "upgrade", "upgrades", "upgraded",
  "bullish", "boost", "boosts", "boosted", "outperform", "outperforms", "rebound", "rebounds", "rebounded",
  "recover", "recovers", "recovered", "win", "wins", "won", "approval", "approved", "breakthrough", "dividend",
  "buyback", "record", "expand", "expands", "expansion", "higher", "advance", "advances",
];

const NEGATIVE_TERMS = [
  "miss", "misses", "missed", "plunge", "plunges", "plunged", "crash", "crashes", "crashed", "slump", "slumps",
  "slumped", "fall", "falls", "fell", "drop", "drops", "dropped", "sink", "sinks", "sank", "decline", "declines",
  "declined", "loss", "losses", "weak", "weaker", "weakest", "cut", "cuts", "downgrade", "downgrades", "downgraded",
  "bearish", "warn", "warns", "warned", "warning", "concern", "concerns", "fear", "fears", "probe", "probes",
  "fraud", "halt", "halts", "halted", "delay", "delays", "delayed", "default", "defaults", "layoff", "layoffs",
  "bankruptcy", "bankrupt", "recession", "selloff", "sell-off", "tumble", "tumbles", "tumbled", "slide", "slides",
  "slid", "dip", "dips", "dipped", "penalty", "lawsuit", "resign", "resigns", "resigned", "strike", "strikes",
  "ban", "bans", "banned", "crisis", "worries", "lower",
];

const termPattern = (terms: string[]) => new RegExp(`\\b(?:${terms.join("|")})\\b`, "gi");

const POSITIVE_PATTERN = termPattern(POSITIVE_TERMS);
const NEGATIVE_PATTERN = termPattern(NEGATIVE_TERMS);

export function classifySentiment(text: string): NewsSentiment {
  const positive = text.match(POSITIVE_PATTERN)?.length ?? 0;
  const negative = text.match(NEGATIVE_PATTERN)?.length ?? 0;
  const net = positive - negative;
  if (net >= 1) return "BULLISH";
  if (net <= -1) return "BEARISH";
  return "NEUTRAL";
}

/** First match wins, specific before general. Deliberately narrow: a chip on
 *  four headlines in five would say nothing. */
const CATEGORY_RULES: Array<[NewsCategory, RegExp]> = [
  ["EARNINGS", /\b(?:earnings|quarterly results|q[1-4] (?:results|profit|earnings|net)|net profit|net loss|revenue|pat)\b/i],
  ["CRYPTO", /\b(?:bitcoin|crypto(?:currency)?|ethereum|blockchain|btc|eth)\b/i],
  ["DEFENSE", /\b(?:defence|defense|missile|military|army|navy|air ?force|drone|war|ceasefire)\b/i],
  ["ECONOMIC", /\b(?:gdp|inflation|cpi|wpi|rbi|federal reserve|fed|repo rate|interest rate|budget|fiscal|trade deficit|iip|rupee)\b/i],
  ["MARKETS", /\b(?:sensex|nifty|ipo|fii|dii|stock market|bond yield|wall street|dow jones|nasdaq)\b/i],
  ["ENERGY", /\b(?:oil|crude|opec|petrol|diesel|power|electricity|solar|renewables?)\b/i],
  ["TECH", /\b(?:artificial intelligence|semiconductor|chip(?:maker|s)?|software|tech|startup|infosys|tcs|wipro)\b/i],
  ["GEOPOLITICS", /\b(?:tariff|sanction|china|trade war|election|parliament|geopolitic)\b/i],
];

export function classifyCategory(text: string): NewsCategory | null {
  for (const [category, rule] of CATEGORY_RULES) {
    if (rule.test(text)) return category;
  }
  return null;
}

// ── identity ─────────────────────────────────────────────────────────────────

/** sha1 of the normalised link, first 16 hex chars, `n` prefix. Same link with
 *  a `#fragment` or a trailing slash is the same article; two feeds syndicating
 *  one story keep one row. Falls back to source|headline when there is no
 *  usable link (tested, though `parseFeed` drops linkless entries today). */
export function stableId(link: string, source: string, headline: string): string {
  const normalized = decodeEntities(link).trim().split("#")[0].replace(/\/+$/, "");
  const basis = /^https?:\/\//i.test(normalized) ? normalized : `${source.toLowerCase()}|${headline.toLowerCase()}`;
  return `n${createHash("sha1").update(basis).digest("hex").slice(0, 16)}`;
}

// ── symbol tagging ───────────────────────────────────────────────────────────

/** `IDEA` (Vodafone Idea) is also an ordinary English word, so it must appear
 *  in the brand's own casing — `IDEA` or `Idea` — and a lowercase "idea" in
 *  prose is just a word. shortcut: bare ticker codes only, so a story that
 *  says "Infosys" without "INFY" does not tag; upgrade by matching company
 *  names from `instruments`. */
const BRAND_CASE_SYMBOLS = new Set(["IDEA"]);

const escapeRegex = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Which of the scanner's universe symbols a text names, ordered by first
 *  mention, capped at five. Word-boundary lookarounds both sides, so
 *  "RELIANCEX" is somebody else. */
export function tagSymbols(text: string, symbols: string[]): string[] {
  const hits: Array<{ symbol: string; at: number }> = [];
  for (const symbol of symbols) {
    if (!symbol) continue;
    const brand = BRAND_CASE_SYMBOLS.has(symbol);
    const word = brand
      ? `(?:${escapeRegex(symbol)}|${escapeRegex(symbol.charAt(0) + symbol.slice(1).toLowerCase())})`
      : escapeRegex(symbol);
    const rule = new RegExp(`(?<![A-Za-z0-9_-])${word}(?![A-Za-z0-9_-])`, brand ? "" : "i");
    const match = rule.exec(text);
    if (match) hits.push({ symbol, at: match.index });
  }
  return hits
    .sort((a, b) => a.at - b.at)
    .slice(0, SYMBOL_CAP)
    .map((hit) => hit.symbol);
}

// ── the parser ───────────────────────────────────────────────────────────────

/** RSS 2.0 `<item>` and Atom `<entry>` blocks from one document, enriched and
 *  returned in feed order (the desk sorts). Pure: `now` is a parameter so tests
 *  can pin time. Entries without a title or an http(s) link are dropped — a
 *  headline with nowhere to go is not a row, and there is no way to show it
 *  honestly as one. */
export function parseFeed(xml: string, sourceName: string, now: number): NewsArticle[] {
  const blocks = xml.match(/<(item|entry)\b[\s\S]*?<\/\1>/g) ?? [];
  const articles: NewsArticle[] = [];
  for (const block of blocks.slice(0, PER_FEED_CAP)) {
    const headline = stripHtml(tag(block, "title") ?? "").slice(0, HEADLINE_MAX);
    if (!headline) continue;
    const link = entryLink(block);
    if (!link) continue;
    const summary = entrySummary(block);
    const text = `${headline} ${summary}`;
    articles.push({
      id: stableId(link, sourceName, headline),
      headline,
      link,
      source: sourceName,
      at: entryDate(block, now),
      summary,
      priority: classifyPriority(headline),
      sentiment: classifySentiment(text),
      category: classifyCategory(text),
    });
  }
  return articles;
}

// ── fetching, cache, single-flight ───────────────────────────────────────────

async function fetchFeed(feed: FeedSource, now: number): Promise<{ report: SourceReport; items: NewsArticle[] }> {
  try {
    const response = await fetch(feed.url, {
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      headers: {
        "user-agent": USER_AGENT,
        accept: "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
      },
      // The cache below is the only cache. Letting Next's fetch layer keep its
      // own copy would make "updated X ago" describe a cache, not a fetch.
      cache: "no-store",
    });
    if (!response.ok) {
      return { report: { name: feed.name, count: 0, error: `HTTP ${response.status}` }, items: [] };
    }
    const items = parseFeed(await response.text(), feed.name, now);
    return { report: { name: feed.name, count: items.length, error: null }, items };
  } catch (error) {
    const detail =
      error instanceof Error
        ? error.name === "TimeoutError"
          ? `timed out after ${FETCH_TIMEOUT_MS / 1000}s`
          : error.message.slice(0, 120)
        : String(error).slice(0, 120);
    return { report: { name: feed.name, count: 0, error: detail }, items: [] };
  }
}

let cache: { at: number; desk: NewsDesk } | null = null;
let inflight: Promise<NewsDesk> | null = null;

/** One refresh of every feed, then the assembled desk. */
async function refresh(now: number): Promise<NewsDesk> {
  const settled = await Promise.all(FEEDS.map((feed) => fetchFeed(feed, now)));
  const seen = new Set<string>();
  const items: NewsArticle[] = [];
  for (const { items: fromFeed } of settled) {
    for (const article of fromFeed) {
      if (seen.has(article.id)) continue;
      seen.add(article.id);
      items.push(article);
    }
  }
  // Newest first; all `at` values are +05:30, so string order is time order.
  items.sort((a, b) => b.at.localeCompare(a.at));
  const reports = settled.map((result) => result.report);

  if (items.length === 0 && cache) {
    // Every source failed at once. Blanking the page would punish the reader
    // for the newsrooms' bad minute: serve the last desk, whose age the page
    // states, and name the failures. The timestamp advances so a dead network
    // is retried once per TTL, not once per page load.
    cache = { at: now, desk: { ...cache.desk, reports } };
    return cache.desk;
  }

  const desk: NewsDesk = { items, fetchedAt: toIst(new Date(now))!, reports };
  cache = { at: now, desk };
  return desk;
}

/** The desk, from cache when fresh. Concurrent callers share one refresh. */
export async function newsDesk(now: number = Date.now()): Promise<NewsDesk> {
  if (cache && now - cache.at < CACHE_TTL_MS) return cache.desk;
  if (inflight) return inflight;
  inflight = refresh(now).finally(() => {
    inflight = null;
  });
  return inflight;
}

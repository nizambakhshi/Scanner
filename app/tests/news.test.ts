import assert from "node:assert/strict";
import { test } from "node:test";
import { toIst } from "@/lib/format";
import {
  classifyCategory,
  classifyPriority,
  classifySentiment,
  decodeEntities,
  parseFeed,
  stableId,
  stripHtml,
  tagSymbols,
} from "@/lib/news";

/**
 * The news desk's pure half: the parser, the id rule and the keyword reads.
 * The fetching half is IO glue and is deliberately not tested with a live
 * network here — the page's failure line is what reports a dead feed.
 */

const NOW = Date.parse("2026-10-15T10:00:00+05:30");
const CLAMPED = toIst(new Date(NOW))!;

const RSS = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test feed</title>
    <item>
      <title><![CDATA[Reliance Q2 profit surges 40%; beats estimates]]></title>
      <link>https://example.com/news/reliance-q2</link>
      <description><![CDATA[<p>Net profit rose to &#8377;15,000 crore, the company said.</p>]]></description>
      <pubDate>Wed, 15 Oct 2026 09:30:00 +0530</pubDate>
    </item>
    <item>
      <title>Record-breaking rally pushes Sensex to an all-time high</title>
      <link>https://example.com/news/sensex-high/</link>
      <description>Markets ended stronger, brokers said.</description>
      <pubDate>Wed, 15 Oct 2026 09:35:00 +0530</pubDate>
    </item>
    <item>
      <title>No link at all</title>
      <pubDate>Wed, 15 Oct 2026 09:36:00 +0530</pubDate>
    </item>
    <item>
      <title>Hostile entry</title>
      <link>javascript:alert(1)</link>
    </item>
  </channel>
</rss>`;

const ATOM = `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom feed</title>
  <entry>
    <title>Bitcoin slides below $60,000 as panic selling deepens</title>
    <link rel="alternate" type="text/html" href="https://example.com/atom/1"/>
    <link rel="self" type="application/atom+xml" href="https://example.com/atom/1.xml"/>
    <summary>Ethereum also fell; investors fear a wider selloff.</summary>
    <updated>2026-10-15T04:05:00Z</updated>
  </entry>
  <entry>
    <title>Calm session ends flat</title>
    <link href="https://example.com/atom/2"/>
    <updated>not a date at all</updated>
  </entry>
  <entry>
    <title>From next week</title>
    <link href="https://example.com/atom/3"/>
    <updated>2026-10-16T04:05:00Z</updated>
  </entry>
</feed>`;

test("parseFeed reads RSS items: CDATA, entities, dates, and drops the unusable", () => {
  const articles = parseFeed(RSS, "Test Wire", NOW);
  // The linkless item and the javascript: link are dropped — they cannot be rows.
  assert.equal(articles.length, 2);
  const [first, second] = articles;

  assert.equal(first.headline, "Reliance Q2 profit surges 40%; beats estimates");
  assert.equal(first.link, "https://example.com/news/reliance-q2");
  assert.equal(first.source, "Test Wire");
  assert.equal(first.at, toIst("2026-10-15T04:00:00Z"));
  assert.ok(first.summary.includes("₹15,000 crore"), `summary entity not decoded: ${first.summary}`);
  assert.ok(!first.summary.includes("<p>"), "summary kept markup");
  assert.equal(first.priority, "ROUTINE");
  assert.equal(first.sentiment, "BULLISH");
  assert.equal(first.category, "EARNINGS");
  assert.match(first.id, /^n[0-9a-f]{16}$/);

  // "BreakING" inside "record-breaking" is not a BREAKING alert.
  assert.equal(second.priority, "ROUTINE");
  assert.equal(second.category, "MARKETS");
  assert.equal(second.link, "https://example.com/news/sensex-high/");
});

test("article ids are stable across fetches — the id must not embed the read time", () => {
  const first = parseFeed(RSS, "Test Wire", NOW);
  const second = parseFeed(RSS, "Test Wire", NOW + 60_000);
  assert.deepEqual(
    second.map((article) => article.id),
    first.map((article) => article.id),
  );
});

test("parseFeed reads Atom entries: href links, published dates, skew clamps", () => {
  const articles = parseFeed(ATOM, "Atom Wire", NOW);
  assert.equal(articles.length, 3);
  const [first, second, third] = articles;

  // rel="alternate" wins over the rel="self" feed URL.
  assert.equal(first.link, "https://example.com/atom/1");
  assert.equal(first.at, toIst("2026-10-15T04:05:00Z"));
  assert.equal(first.sentiment, "BEARISH");
  assert.equal(first.category, "CRYPTO");
  assert.ok(first.summary.includes("selloff"));

  // An unparseable date is still news: it reads as "now", not as missing.
  assert.equal(second.at, CLAMPED);
  // A feed whose clock runs fast (18 h ahead) must not pin itself to the top.
  assert.equal(third.at, CLAMPED);
});

test("stableId normalises fragments and trailing slashes, refuses to drift", () => {
  const a = stableId("https://x.com/story/", "ET", "Headline");
  const b = stableId("https://x.com/story#section", "ET", "Headline");
  assert.equal(a, b);
  assert.match(a, /^n[0-9a-f]{16}$/);
  assert.notEqual(a, stableId("https://x.com/other", "ET", "Headline"));
});

test("decodeEntities decodes what it knows and leaves the rest verbatim", () => {
  assert.equal(decodeEntities("AT&amp;T"), "AT&T");
  assert.equal(decodeEntities("&#8377;15,000 crore"), "₹15,000 crore");
  assert.equal(decodeEntities("&#x20B9;5"), "₹5");
  assert.equal(decodeEntities("&nbsp;"), " ");
  assert.equal(decodeEntities("&bogus;"), "&bogus;");
});

test("stripHtml removes markup first, then decodes — &lt;b&gt; is text, not markup", () => {
  assert.equal(stripHtml("<p>Net <b>profit</b>   rose</p>"), "Net profit rose");
  assert.equal(stripHtml("&lt;b&gt; stays visible"), "<b> stays visible");
});

test("priority is the headline's own words, whole-word only", () => {
  assert.equal(classifyPriority("Breaking: RBI holds rates"), "BREAKING");
  assert.equal(classifyPriority("Market alert: circuit breaker hit"), "BREAKING");
  assert.equal(classifyPriority("Just in: new tariff order"), "FLASH");
  assert.equal(classifyPriority("Urgent meeting called at SEBI"), "URGENT");
  assert.equal(classifyPriority("Record-breaking rally continues"), "ROUTINE");
  assert.equal(classifyPriority("Sensex hits a fresh high"), "ROUTINE");
});

test("sentiment is the net of a small lexicon, honest about neutral", () => {
  assert.equal(classifySentiment("Company beats estimates, profit rises 20%"), "BULLISH");
  assert.equal(classifySentiment("Net loss widens, stock plunges to 52-week low"), "BEARISH");
  // One positive against two negatives is still the red side of the net.
  assert.equal(classifySentiment("Profit falls as revenue misses street view"), "BEARISH");
  assert.equal(classifySentiment("Board to meet on Friday to consider fund raise"), "NEUTRAL");
});

test("category is the first specific rule that matches, and null when none does", () => {
  assert.equal(classifyCategory("RBI holds repo rate steady"), "ECONOMIC");
  assert.equal(classifyCategory("Gemini files for IPO"), "MARKETS");
  assert.equal(classifyCategory("Crude oil slips as OPEC meets"), "ENERGY");
  assert.equal(classifyCategory("Company wins new order worth Rs 500 crore"), null);
});

test("tagSymbols is word-boundary exact, case-honest for the blocked ones", () => {
  assert.deepEqual(tagSymbols("Reliance Industries and Infosys lead the gain", ["RELIANCE", "INFY"]), ["RELIANCE"]);
  // RELIANCEX is somebody else; 360ONE is a full token.
  assert.deepEqual(tagSymbols("RELIANCEX Corp lists", ["RELIANCE"]), []);
  assert.deepEqual(tagSymbols("360ONE WAM hits a high", ["360ONE"]), ["360ONE"]);
  // IDEA (Vodafone Idea) is also an English word: the code must be capitalised.
  assert.deepEqual(tagSymbols("A new idea for banks", ["IDEA"]), []);
  assert.deepEqual(tagSymbols("Vodafone Idea extends gains", ["IDEA"]), ["IDEA"]);
  // Ordered by first mention, capped at five.
  assert.deepEqual(tagSymbols("TCS and Reliance rise", ["RELIANCE", "TCS"]), ["TCS", "RELIANCE"]);
  assert.deepEqual(
    tagSymbols("FFF EEE DDD CCC BBB AAA", ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]),
    ["FFF", "EEE", "DDD", "CCC", "BBB"],
  );
});

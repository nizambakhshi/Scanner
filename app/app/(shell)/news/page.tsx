import Link from "next/link";
import { Empty, ErrorNote, FilterChip, PageHeading, Panel } from "@/components/ui";
import { ago, dateTime, humanise } from "@/lib/format";
import { newsDesk, tagSymbols, type NewsArticle, type NewsPriority, type NewsSentiment } from "@/lib/news";
import { ok, panel } from "@/lib/panel";
import { renderNow } from "@/lib/render-time";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/** The rows the page renders. The desk can hold a few hundred; eighty is more
 *  than a reader scrolls and keeps the DOM small. */
const RENDER_CAP = 80;

const PRIORITY_STYLE: Record<NewsPriority, string | null> = {
  FLASH: "border-negative/40 bg-negative/10 text-negative",
  URGENT: "border-negative/40 bg-negative/10 text-negative",
  BREAKING: "border-amber/40 bg-amber/10 text-amber",
  ROUTINE: null,
};

const SENTIMENT_STYLE: Record<NewsSentiment, string | null> = {
  BULLISH: "border-positive/40 bg-positive/10 text-positive",
  BEARISH: "border-negative/40 bg-negative/10 text-negative",
  NEUTRAL: null,
};

/**
 * The news desk — FinceptTerminal's news concept, read directly from the
 * newsrooms' public RSS and stored nowhere.
 *
 * Read-only by nature and by charter: the page shows headlines, the words the
 * desk reads off them, and which tracked symbols they name. It is market
 * context for the boards, never a trading signal — the scanner's own detectors
 * raise alerts, and nothing here moves them.
 */
export default async function NewsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const taggedOnly = params.scope === "tagged";

  const [desk, universe] = await Promise.all([panel(() => newsDesk()), panel(() => source().universeSymbols())]);
  const symbols = ok(universe) ? universe.data : [];
  const articles: Array<NewsArticle & { symbols: string[] }> = (ok(desk) ? desk.data.items : []).map((article) => ({
    ...article,
    symbols: tagSymbols(`${article.headline} ${article.summary}`, symbols),
  }));
  const tagged = articles.filter((article) => article.symbols.length > 0);
  const cut = taggedOnly ? tagged : articles;
  const visible = cut.slice(0, RENDER_CAP);
  const reports = ok(desk) ? desk.data.reports : [];
  const answered = reports.filter((report) => report.error === null);
  const failed = reports.filter((report) => report.error !== null);
  // One render instant for every "ago" on the page — the desk-wide timestamp and
  // each headline are measured against the same clock (lib/render-time).
  const now = renderNow();

  return (
    <div className="space-y-4">
      <PageHeading
        title="News"
        subtitle={
          <>
            Headlines from the newsrooms&rsquo; public feeds — Indian wires and global market media — read directly by
            this server and stored nowhere. Priority, sentiment and category are keyword reads of the text, not a model
            and not advice; symbol tags are bare NSE codes matched against the scanner&rsquo;s universe.
          </>
        }
      />

      <div className="flex flex-wrap gap-2 text-[12px]">
        <FilterChip href="/news" label="All headlines" value={articles.length} active={!taggedOnly} />
        <FilterChip
          href="/news?scope=tagged"
          label="Mention tracked names"
          value={tagged.length}
          tone={tagged.length > 0 ? "text-positive" : "text-ink-2"}
          active={taggedOnly}
        />
      </div>

      <Panel
        title="Headlines"
        hint={
          ok(desk) ? (
            <>
              {articles.length} headlines · {answered.length} of {reports.length} sources answered · updated{" "}
              {ago(desk.data.fetchedAt, now)}
              {cut.length > visible.length ? ` · showing first ${visible.length}` : ""}
            </>
          ) : undefined
        }
      >
        {!ok(desk) ? (
          <ErrorNote title="News desk unavailable" detail={desk.error} />
        ) : articles.length === 0 ? (
          <Empty>
            No headlines right now — every source answered empty. The feeds are read again on the next refresh window.
          </Empty>
        ) : visible.length === 0 ? (
          <Empty>No headline in this window names a symbol in the scanner&rsquo;s universe — the All cut is above.</Empty>
        ) : (
          <ul>
            {visible.map((article) => (
              <li key={article.id} className="border-b border-line px-4 py-3 last:border-b-0">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted">
                  <span title={dateTime(article.at)}>{ago(article.at, now)}</span>
                  <span aria-hidden>·</span>
                  <span>{article.source}</span>
                  <PriorityChip priority={article.priority} />
                  <SentimentChip sentiment={article.sentiment} />
                  {article.category ? (
                    <span className="chip border-line-2 bg-surface-2 text-muted">{humanise(article.category)}</span>
                  ) : null}
                </div>
                <a
                  href={article.link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 block text-[13.5px] font-medium leading-snug text-ink hover:text-accent hover:underline"
                >
                  {article.headline}
                  <span className="ml-1 text-[11px] text-muted-2" aria-hidden>
                    ↗
                  </span>
                </a>
                {article.summary ? (
                  <p className="mt-1 line-clamp-2 text-[12px] leading-snug text-ink-2">{article.summary}</p>
                ) : null}
                {article.symbols.length > 0 ? (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {article.symbols.map((symbol) => (
                      <Link
                        key={symbol}
                        href={`/instrument/${encodeURIComponent(symbol)}`}
                        className="chip border-accent/40 bg-accent/10 text-accent"
                      >
                        {symbol}
                      </Link>
                    ))}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {failed.length > 0 ? (
        <p className="px-1 text-[11px] leading-snug text-muted">
          No answer from {failed.map((report) => `${report.name} (${report.error})`).join(", ")} — the headlines above
          are what the other sources published.
        </p>
      ) : null}

      {ok(universe) ? null : (
        <p className="px-1 text-[11px] leading-snug text-muted">
          The scanner&rsquo;s universe could not be read ({universe.error}), so no headline carries symbol tags right
          now.
        </p>
      )}

      <p className="px-1 text-[11px] leading-snug text-muted">
        Feeds are re-read at most once every ten minutes and the page reports when that last happened. A story is cut
        out when its link is not http(s), an article needs a title and a working link to appear, and the same link seen
        twice (with a fragment or a trailing slash) is one row. Ticker tagging matches codes, so a story that says
        &ldquo;Infosys&rdquo; without &ldquo;INFY&rdquo; stays untagged.
      </p>
    </div>
  );
}

function PriorityChip({ priority }: { priority: NewsPriority }) {
  const style = PRIORITY_STYLE[priority];
  if (!style) return null;
  return (
    <span className={`chip ${style}`} title="Read from the headline's own words — FLASH / URGENT / BREAKING">
      {priority.charAt(0) + priority.slice(1).toLowerCase()}
    </span>
  );
}

function SentimentChip({ sentiment }: { sentiment: NewsSentiment }) {
  const style = SENTIMENT_STYLE[sentiment];
  if (!style) return null;
  return (
    <span
      className={`chip ${style}`}
      title="A weighted keyword count of the headline and summary — a lexicon, not a model"
    >
      {sentiment.charAt(0) + sentiment.slice(1).toLowerCase()}
    </span>
  );
}

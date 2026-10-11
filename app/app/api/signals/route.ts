import { guard, json, read } from "@/lib/api";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

const TYPES = new Set([
  "BULLISH_ENTRY",
  "WATCHLIST_ADDED",
  "WATCHLIST_WEAKENING",
  "WATCHLIST_REMOVED",
  "WATCHLIST_REQUALIFIED",
  "DATA_FEED_INTERRUPTED",
  "DATA_FEED_RECOVERED",
  "DAILY_SCAN_COMPLETE",
]);

/**
 * `GET /api/signals?date=&type=&symbol=&limit=`
 *
 * Returns `{ signals, deliveries }`. The delivery rows come from the same source
 * in the same request, because an alert whose Telegram status is stale is the
 * number one thing an operator needs to see next to the alert itself.
 */
export async function GET(request: Request) {
  const { denied } = await guard();
  if (denied) return denied;
  const params = new URL(request.url).searchParams;
  const type = params.get("type");
  if (type && !TYPES.has(type.toUpperCase())) {
    return json({ error: `Unknown signal type. Expected one of: ${[...TYPES].join(", ")}.`, signals: [] }, 400);
  }
  return read(async () => {
    const signals = await source().signals({
      tradingDate: params.get("date"),
      signalType: type ? type.toUpperCase() : null,
      symbol: params.get("symbol"),
      limit: Number(params.get("limit") ?? 100),
    });
    const deliveries = await source().deliveries(signals.map((row) => row.id));
    return { signals, deliveries };
  });
}

import { guard, json, read } from "@/lib/api";
import { SIGNAL_TYPES, parseSignalType } from "@/lib/filters";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * `GET /api/signals?date=&type=&symbol=&limit=`
 *
 * Returns `{ signals, deliveries }`. The delivery rows come from the same source
 * in the same request, because an alert whose Telegram status is stale is the
 * number one thing an operator needs to see next to the alert itself.
 *
 * The types this endpoint will filter on are `/signals`' own list rather than a second
 * copy of it: that page renders its dropdown from the same vocabulary, and an endpoint
 * that knows fewer types than the page offers turns a filter a person can select into
 * a 400.
 */
export async function GET(request: Request) {
  const { denied } = await guard();
  if (denied) return denied;
  const params = new URL(request.url).searchParams;
  const rawType = params.get("type");
  const type = parseSignalType(rawType);
  if (rawType && !type) {
    return json({ error: `Unknown signal type. Expected one of: ${SIGNAL_TYPES.join(", ")}.`, signals: [] }, 400);
  }
  return read(async () => {
    const signals = await source().signals({
      tradingDate: params.get("date"),
      signalType: type,
      symbol: params.get("symbol"),
      limit: Number(params.get("limit") ?? 100),
    });
    const deliveries = await source().deliveries(signals.map((row) => row.id));
    return { signals, deliveries };
  });
}

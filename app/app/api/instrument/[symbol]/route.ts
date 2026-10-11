import { guard, json, read } from "@/lib/api";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * `GET /api/instrument/:symbol` — one instrument's candles, indicator history,
 * watchlist row and state transitions.
 *
 * A symbol the universe does not contain is a 404 with a plain message; the
 * lookup is by `symbol` or `trading_symbol` (case-insensitive), and no query in
 * this path can return a broker `instrument_token`.
 */
export async function GET(_request: Request, context: { params: Promise<{ symbol: string }> }) {
  const { denied } = await guard();
  if (denied) return denied;
  const { symbol } = await context.params;
  const clean = decodeURIComponent(symbol).slice(0, 32);
  if (!/^[\w.-]+$/.test(clean)) return json({ error: "That is not a symbol." }, 400);
  return read(async () => {
    const detail = await source().instrument(clean);
    if (!detail) return json({ error: `${clean.toUpperCase()} is not in the instrument universe.`, symbol: clean }, 404);
    return json(detail);
  });
}

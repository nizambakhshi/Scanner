import { guard, read } from "@/lib/api";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * `GET /api/watchlist?date=YYYY-MM-DD`
 *
 * The daily watchlist exactly as `v_current_watchlist` defines it: qualifiers
 * first, then score. The `date` filter is validated in the data layer, so a
 * malformed value means "no filter" rather than a database error leaking a
 * fragment of SQL into the response body.
 */
export async function GET(request: Request) {
  const { denied } = await guard();
  if (denied) return denied;
  const params = new URL(request.url).searchParams;
  const date = params.get("date");
  return read(() => source().currentWatchlist(date));
}

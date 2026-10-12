import { guard, read } from "@/lib/api";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * `GET /api/sectors`
 *
 * The Nifty sector indices exactly as the `sector_indices`/`sector_quotes`
 * tables hold them. This is the endpoint the board's sector tile polls every
 * few seconds, so it is deliberately the smallest query in the app: one join,
 * thirteen rows, no filters.
 */
export async function GET() {
  const { denied } = await guard();
  if (denied) return denied;
  return read(() => source().sectorQuotes());
}

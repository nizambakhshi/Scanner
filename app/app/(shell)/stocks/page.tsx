import { redirect } from "next/navigation";
import { parseStockBiasFilter } from "@/lib/filters";

export const dynamic = "force-dynamic";

/**
 * /stocks is gone — the universe directory is the last stage of the one
 * Watchlist page now. The redirect carries a bias cut through, so a bookmark
 * like `?bias=ENTRY` lands on the same cut instead of the whole universe.
 */
export default async function StocksPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const bias = parseStockBiasFilter(params.bias);
  redirect(bias === "ALL" ? "/watchlist#universe" : `/watchlist?bias=${bias}#universe`);
}

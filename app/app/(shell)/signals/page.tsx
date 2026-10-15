import { redirect } from "next/navigation";
import { parseSignalType } from "@/lib/filters";

export const dynamic = "force-dynamic";

/**
 * /signals is gone — the day's feed is a section of the one Watchlist page now.
 * The type cut carries through (`?type=BREAKOUT_UP` → the same cut there); a
 * `?date=` from the old page is dropped, because the merged page is today by
 * construction — past days are the Days page's job.
 */
export default async function SignalsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const type = parseSignalType(params.type);
  redirect(type ? `/watchlist?type=${type}#alerts` : "/watchlist#alerts");
}

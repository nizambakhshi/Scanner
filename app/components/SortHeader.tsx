import Link from "next/link";
import type { SortWiring } from "@/lib/sorting";

/**
 * A column header that sorts when the page wired it, and is a plain label when
 * it did not (the Board's compact tables keep the source's own order).
 *
 * The link is built by the page through `hrefFor`, which re-derives every other
 * query parameter — filters, the other table's sort — so a header click never
 * quietly resets the view. `aria-sort` carries the state for assistive tech
 * the same way the arrow carries it for the eye.
 */
export function SortHeader({
  label,
  column,
  sort,
  className,
  title,
}: {
  label: string;
  column: string;
  sort?: SortWiring;
  className?: string;
  title?: string;
}) {
  if (!sort) {
    return (
      <th className={className} title={title}>
        {label}
      </th>
    );
  }
  const active = sort.key === column;
  return (
    <th
      className={className}
      title={title}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
    >
      <Link
        href={sort.hrefFor(column)}
        className={`inline-flex items-center gap-1 whitespace-nowrap underline-offset-4 hover:text-ink hover:underline ${
          active ? "text-ink" : ""
        }`}
      >
        {label}
        <span aria-hidden className={`text-[9px] ${active ? "" : "text-muted"}`}>
          {active ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </Link>
    </th>
  );
}

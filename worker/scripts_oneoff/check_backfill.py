"""Final coverage check for the 2026-10-12 backfill, timezone-correct.

candle_start_at is TIMESTAMPTZ; the completeness threshold must be built as a
timestamp *interpreted as IST* (`... AT TIME ZONE 'Asia/Kolkata'`), not by
adding hours in the session timezone (UTC on Supabase) — the retry script got
this wrong and flagged all 212 instruments as incomplete.
"""

import os
import sys
from datetime import date

import psycopg

DAY = date(2026, 10, 12)
# A F&O session's last 1-min bar is stamped 15:29; broker omits zero-volume
# minutes, so "complete" = last stamp reaches 15:25 (same rule as the retry).
THRESHOLD_SQL = "(%s::date + time '15:25') AT TIME ZONE 'Asia/Kolkata'"


def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.symbol, count(c.*)::int AS candles,
                   max(c.candle_start_at) AT TIME ZONE 'Asia/Kolkata' AS last_ist
            FROM instruments i
            LEFT JOIN candles_1m c
              ON c.instrument_id = i.id AND c.trading_date = %s
            WHERE i.exchange = 'NSE' AND i.is_fno
            GROUP BY i.symbol
            """,
            (DAY,),
        )
        rows = cur.fetchall()

        cur.execute("SELECT count(*)::int FROM candles_1m WHERE trading_date = %s", (DAY,))
        total_rows = cur.fetchone()[0]
        cur.execute(
            "SELECT count(DISTINCT instrument_id)::int FROM candles_1m WHERE trading_date = %s", (DAY,)
        )
        instruments_today = cur.fetchone()[0]

    complete = [(s, n, t) for s, n, t in rows if t is not None and t.strftime("%H:%M") >= "15:25"]
    short = [(s, n, t) for s, n, t in rows if t is None or t.strftime("%H:%M") < "15:25"]
    short.sort(key=lambda r: (r[2] is None, r[2]))

    print(f"rows_today total={total_rows} instruments_today={instruments_today}")
    print(f"fno_universe={len(rows)} complete={len(complete)} short={len(short)}")
    if short:
        print("short of 15:25 last stamp:")
        for symbol, n, t in short:
            stamp = "no candles" if t is None else t.strftime("%H:%M")
            print(f"  {symbol:<12} candles={n or 0:>4} last={stamp}")


if __name__ == "__main__":
    sys.exit(main())

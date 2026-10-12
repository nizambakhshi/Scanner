"""One-off: remove everything that is not F&O-underlying data (2026-10-12).

The morning session ran on the old NIFTY500 universe before dying at 13:24,
so the database held 494-universe artifacts — non-F&O candles/snapshots, a
watchlist of nine non-F&O names, its signals and feed-health events, and the
instruments mappings themselves. Operator instruction: keep only F&O-stock
data, nothing else. The 212 F&O underlyings and their backfilled day are
untouched.

FK-safe order in ONE transaction: deliveries/signals first (signals.instrument_id
is ON DELETE SET NULL, so instruments must not be deleted before them), then
memberships (events cascade), the aborted run's summary and initial-scan
journal, candles/snapshots, and finally the instruments themselves (cascades
any leftovers). An audit_logs row records the counts.
"""

import asyncio
import os
import sys

from psycopg import AsyncConnection
from psycopg.types.json import Json

SCOPE_DATE = "2026-10-12"

# Ordered (title, sql, params-less). Every statement returns its rowcount via
# the caller so the audit row and the report carry observed numbers.
STATEMENTS = [
    ("notification_deliveries",
     """DELETE FROM notification_deliveries
        WHERE signal_id IN (SELECT id FROM signals
                            WHERE instrument_id IS NULL
                               OR instrument_id IN (SELECT id FROM instruments WHERE NOT is_fno))"""),
    ("signals",
     """DELETE FROM signals
        WHERE instrument_id IS NULL
           OR instrument_id IN (SELECT id FROM instruments WHERE NOT is_fno)"""),
    ("daily_watchlists",
     "DELETE FROM daily_watchlists WHERE instrument_id IN (SELECT id FROM instruments WHERE NOT is_fno)"),
    ("watchlist_events_cascaded",
     """SELECT 1 FROM watchlist_events WHERE watchlist_id NOT IN (SELECT id FROM daily_watchlists)"""),
    ("daily_summaries",
     f"DELETE FROM daily_summaries WHERE trading_date = DATE '{SCOPE_DATE}'"),
    ("audit_logs_initial_scan",
     "DELETE FROM audit_logs WHERE action = 'initial_scan'"),
    ("candles_1m",
     "DELETE FROM candles_1m WHERE instrument_id IN (SELECT id FROM instruments WHERE NOT is_fno)"),
    ("indicator_snapshots",
     "DELETE FROM indicator_snapshots WHERE instrument_id IN (SELECT id FROM instruments WHERE NOT is_fno)"),
    ("instruments",
     "DELETE FROM instruments WHERE NOT is_fno"),
]


async def main() -> int:
    async with await AsyncConnection.connect(
        os.environ["DATABASE_URL"].replace(":5432", ":6543"), connect_timeout=10
    ) as conn:
            counts: dict[str, int] = {}
            for title, sql in STATEMENTS:
                if title == "watchlist_events_cascaded":  # cascade side effect — count, don't delete
                    cur = await conn.execute(sql)
                    counts[title] = len(await cur.fetchall())
                    continue
                cur = await conn.execute(sql)
                counts[title] = cur.rowcount if cur.rowcount >= 0 else 0
                print(f"{title}: {counts[title]}", flush=True)

            await conn.execute(
                """INSERT INTO audit_logs
                       (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
                   VALUES ('operator', 'SUPER_ADMIN', 'fno_scope_purge', 'instruments', 'ALL', %s, NULL)""",
                (Json({"trading_date": SCOPE_DATE,
                       "reason": "keep only F&O-underlying data; 494-universe morning run removed",
                       "deleted": counts}),),
            )
            await conn.commit()

            check = await conn.execute(
                """SELECT (SELECT count(*) FROM instruments) AS instruments,
                          (SELECT count(*) FROM instruments WHERE is_fno) AS fno,
                          (SELECT count(*) FROM candles_1m) AS candles,
                          (SELECT count(*) FROM indicator_snapshots) AS snapshots,
                          (SELECT count(*) FROM daily_watchlists) AS memberships,
                          (SELECT count(*) FROM signals) AS signals,
                          (SELECT count(*) FROM notification_deliveries) AS deliveries,
                          (SELECT count(*) FROM daily_summaries) AS summaries"""
            )
            row = await check.fetchone()
            print(f"after purge: {dict(row)}", flush=True)
            return 0 if row["instruments"] == row["fno"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

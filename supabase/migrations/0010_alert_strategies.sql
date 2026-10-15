-- 0010_alert_strategies.sql — level-break alerts, and where their settings live
--
-- The operator asked (2026-10-14) for a watchlist of *level breaks* over the whole
-- F&O universe: the day high/low, the opening range, and the session VWAP, each
-- armed or disarmed from the dashboard, each with its own threshold and cooldown,
-- each with its own Telegram switch — plus the Telegram bot credentials themselves
-- editable from the page instead of only from the worker's environment.
--
-- Four changes, none of them destructive:
--
-- 1. signals.signal_type gains BREAKOUT_UP and BREAKDOWN. The CHECK from 0008 is
--    dropped and re-created with the full list, exactly as 0008 did to 0001's —
--    a row written under the old list still validates under the new one.
--
--    Two types rather than six because the direction is what the alert *is*
--    (a person acts on "this stock broke out"), while which strategy saw it is
--    data: it travels in signals.reasons as 'strategy:<name>', is what the
--    per-strategy cooldown matches on, and keeps a day break and a VWAP reclaim
--    on the same stock in the same minute from muting each other.
--
-- 2. ops_settings['alert_strategies'] — the three strategies and their knobs, as
--    the shape worker/analytics/breakouts.py parses and the /alerts page edits.
-- 3. ops_settings['telegram'] — bot token and chat id, so the dashboard is the
--    credential's home and the worker reads it the way it already reads
--    'retention'. The seeded value is empty by design: the environment remains
--    the fallback, and a database that has never seen the page keeps behaving
--    exactly as it did before this file ran.
-- 4. Privileges: ops_settings already has RLS with no policies and no grants to
--    anon/authenticated (0006), so there is nothing to add — this table is only
--    reachable by the server roles, which is what makes storing a bot token in it
--    no weaker than keeping it in a Vercel environment variable.
--
-- Rollout notes:
-- * both seeds are `ON CONFLICT DO NOTHING`. A database where the operator has
--   already saved the page keeps their document; this file only ever supplies the
--   starting point, never an override.
-- * scanner-migrate records the version row itself, so this file must not.
-- * nothing here rewrites historical rows: no new columns, no backfill.

BEGIN;

ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_signal_type_check;
ALTER TABLE signals ADD CONSTRAINT signals_signal_type_check CHECK (
    signal_type IN ('BULLISH_ENTRY',
                    'WATCHLIST_ADDED',
                    'WATCHLIST_WEAKENING',
                    'WATCHLIST_REMOVED',
                    'WATCHLIST_REQUALIFIED',
                    'DATA_FEED_INTERRUPTED',
                    'DATA_FEED_RECOVERED',
                    'DAILY_SCAN_COMPLETE',
                    'RSI_BULLISH',
                    'RSI_BEARISH',
                    'BREAKOUT_UP',
                    'BREAKDOWN')
);

-- The shipped defaults, byte-for-byte what StrategiesConfig.to_dict() writes.
-- Why these numbers, since they are now editable and will be edited:
-- * session_break: the level moves every minute, so it needs both a margin
--   (0.10 % — a tick past yesterday's print is not news) and a 15-minute window;
--   it re-arms only after a close back under the level it broke.
-- * orb: the /strategy board's rule with no extra margin, because a close across
--   the range is already a decision; cooldown 0 because the level is fixed for the
--   day and the idempotency key already allows one alert per direction. The 1.5x
--   volume gate is the board's own evidence column.
-- * vwap: two consecutive closes beyond the band. A single cross back and forth is
--   the noise this strategy is meant to filter, and 10 minutes is short enough that
--   a reclaim early in a move still reaches the phone.
INSERT INTO ops_settings (key, value) VALUES (
    'alert_strategies',
    '{"version": 1, "strategies": {
        "session_break": {"enabled": true, "threshold_pct": 0.10, "confirm_candles": 1,
                          "min_volume_ratio": 1.0, "cooldown_minutes": 15, "telegram": true},
        "orb":           {"enabled": true, "threshold_pct": 0.0,  "confirm_candles": 1,
                          "min_volume_ratio": 1.5, "cooldown_minutes": 0,  "telegram": true},
        "vwap":          {"enabled": true, "threshold_pct": 0.10, "confirm_candles": 2,
                          "min_volume_ratio": 1.0, "cooldown_minutes": 10, "telegram": true}
    }}'::jsonb
) ON CONFLICT (key) DO NOTHING;

-- Empty, not absent: the page has something to render and validate against on its
-- first visit. `enabled` is the master switch, so a token that is saved and then
-- switched off stops paging without the operator having to delete anything.
-- bot_token is never read back to the browser, never logged, and never written to
-- an audit row — only its presence and a fingerprint are ever shown.
INSERT INTO ops_settings (key, value) VALUES (
    'telegram',
    '{"version": 1, "enabled": false, "bot_token": "", "chat_id": ""}'::jsonb
) ON CONFLICT (key) DO NOTHING;

COMMIT;

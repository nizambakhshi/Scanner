-- 0013_saas_subscription.sql — who is allowed to see what, stored in the database
--
-- ⚠️  DRAFT — NOT YET APPROVED FOR APPLICATION. ⚠️
-- Same gate as 0012: written and chain-verified on a disposable database, and it
-- must NOT be applied to the Mini or to Mumbai until the operator has reviewed
-- this exact file and approved the apply order (Approval gate C; the approved
-- target for this series is Mumbai only). It chains *behind* 0012 — the runner
-- applies files in name order and nothing here depends on 0012's tables, but a
-- database that has not applied 0012 has not applied this either.
--
-- Everything here is additive. No existing row, column, view or setting is
-- touched, and a worker/dashboard on the previous build keeps running unchanged:
-- the app reads `plan` defensively (a missing column behaves as UNLIMITED, which
-- is exactly what every existing account is entitled to until the operator
-- says otherwise), so applying this file cannot lock anyone out of their own
-- watchlist.
--
-- Two changes:
--
-- 1. app_users.plan — the subscription fact. The SaaS has two plans and this
--    column is the only place either is remembered:
--
--      BASIC      the daily watchlist shows at most three symbols (the top
--                 three by score; enforced in the dashboard's read path, not
--                 here — the database stores the entitlement, the code applies it)
--      UNLIMITED  the whole qualified list, as today
--
--    The DEFAULT is 'BASIC' and the UPDATE below then grandfathers every
--    account that exists at apply time to 'UNLIMITED': the operator's own
--    account predates the paywall and must not wake up on day one seeing
--    three rows. A row inserted after this file runs without an explicit plan
--    is BASIC — the conservative direction for a table whose next writer is a
--    subscription flow.
--
--    No paid-provider columns, deliberately: no payment IDs, no billing period
--    state, no card references. "Subscribe" on this product creates an account
--    request and the Super Admin grants it a plan; the moment a real payment
--    provider enters the picture, that provider owns this state and a later
--    migration adds its identifiers rather than this file guessing their shape.
--
-- 2. subscription_requests — what the public Subscribe page leaves behind.
--    There is no email sender anywhere in this deployment (the account page
--    documents why password reset is manual), so a request is an row the Super
--    Admin acts on from the Accounts page, not an automated pipeline. The
--    partial UNIQUE index keeps at most one PENDING request per address —
--    "you already asked, the answer is coming" — while a dismissed or approved
--    address is free to ask again. `note` is the applicant's own words, length
--    capped by the form, and it renders as text everywhere it is read.
--
--    No FK to app_users on purpose: a request is made by someone with no
--    account yet, and approving one creates the account in the same action the
--    Accounts page already performs (create user + grant plan). Tying the two
--    rows with a key would forbid the first half of that flow.
--
-- Rollout notes, stated against the runner the operator actually uses:
-- * `scanner-migrate` strips the BEGIN/COMMIT below and autocommits one
--   statement per round trip, so this file is NOT atomic there; both statements
--   are individually safe intermediate states (a new column with a grandfather
--   pass after it, a new table invisible to old code).
-- * Re-runnable except in the ordinary way: ADD COLUMN IF NOT EXISTS and
--   CREATE TABLE IF NOT EXISTS; the grandfather UPDATE is idempotent (setting
--   UNLIMITED on rows that already are UNLIMITED), and it only ever touches
--   rows that existed when it ran — a plan the operator changed later is never
--   walked back by a re-run, because a fresh ALTER skips and the UPDATE… does
--   re-apply on a second full run, so re-running this file resets every plan to
--   UNLIMITED. It is a one-shot file: apply once per database, like every
--   migration in this series. That is stated rather than engineered around,
--   because the scanner-migrate version row already prevents normal re-runs.
-- * Chain verified on a disposable postgresql@17 both ways: the full 0001..0013
--   chain from empty, and 0012 → 0013 alone. See the verification record.
-- * Rollback: `DROP TABLE subscription_requests;` and
--   `ALTER TABLE app_users DROP COLUMN plan;`. Both are clean at any point
--   before the Accounts page has written a BASIC plan anyone depends on.

BEGIN;

-- 1 ── The entitlement on the account.
ALTER TABLE app_users
    ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'BASIC'
        CHECK (plan IN ('BASIC','UNLIMITED'));

-- Everyone with an account before the paywall keeps the full view.
UPDATE app_users SET plan = 'UNLIMITED';

-- 2 ── The Subscribe page's outbox.
CREATE TABLE IF NOT EXISTS subscription_requests (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email            TEXT NOT NULL,
    plan_requested   TEXT NOT NULL DEFAULT 'UNLIMITED'
                     CHECK (plan_requested IN ('BASIC','UNLIMITED')),
    note             TEXT,
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','APPROVED','DISMISSED')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    handled_at       TIMESTAMPTZ,
    handled_by_email TEXT
);

-- One open ask per address; a decided one does not block asking again.
CREATE UNIQUE INDEX IF NOT EXISTS subscription_requests_pending_key
    ON subscription_requests (lower(email))
    WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS subscription_requests_status_idx
    ON subscription_requests (status, created_at);

DO $$
BEGIN
    ALTER TABLE subscription_requests ENABLE ROW LEVEL SECURITY;
    REVOKE ALL ON TABLE subscription_requests FROM anon;
    REVOKE ALL ON TABLE subscription_requests FROM authenticated;
END
$$;

COMMIT;

#!/usr/bin/env bash
# Bring every sequence in the Mini's Postgres up to the rows that already exist.
#
# Why this exists: the Mini's database is a COPY of the Mumbai project. The copy
# carried the rows, and it carried the sequence objects, but not the sequence
# POSITIONS — so `login_attempts.id` had rows in it while its sequence still
# believed the next value was 3. Any INSERT into such a table dies with
#
#   duplicate key value violates unique constraint "login_attempts_pkey"
#
# which is how the staging dashboard started answering POST /api/session with HTTP
# 500 on 2026-10-14: sign-in always journals an attempt (success or failure), and
# journaling always collided. The same silent trap sits under every serial column in

#
# Scope: the Mini only. Mumbai is a live database whose inserts demonstrably work
# (the operator signs in on production daily), and production is not this script's
# business. Nothing here changes a schema object, a row, or the app.
#
# Idempotent: a sequence already ahead of its table is left alone and reported as ok.
set -uo pipefail

DB_CONTAINER=${DB_CONTAINER:-supabase_db_mohdnizam}

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$" \
  || fail "database container ${DB_CONTAINER} is not running (supabase start)"

# One autocommitted block over stdin: no connection string and no credential ever
# reaches a process list.
docker exec -i "$DB_CONTAINER" psql -U postgres -d postgres -q -tA -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE
  r           RECORD;
  max_id      BIGINT;
  seq_now     BIGINT;
  called      BOOLEAN;
  will_hand   BIGINT;
  item        TEXT;
  behind      TEXT[] := '{}';
  ok_count    INTEGER := 0;
  total       INTEGER := 0;
BEGIN
  -- These tables use `bigint GENERATED ALWAYS AS IDENTITY`, not a serial default, so
  -- information_schema.column_default holds no nextval() to look for. The real marks
  -- are pg_attribute.attidentity ('a'lways / 'd' by default) and, for a plain serial,
  -- an actual nextval() expression. Both shapes are covered here.
  FOR r IN
    SELECT c.relname AS table_name, a.attname AS column_name,
           pg_get_serial_sequence(format('public.%I', c.relname), a.attname) AS seq
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_attribute a ON a.attrelid = c.oid
      LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
     WHERE n.nspname = 'public'
       AND c.relkind = 'r'
       AND a.attnum > 0
       AND NOT a.attisdropped
       AND (a.attidentity <> '' OR pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval%')
     ORDER BY c.relname, a.attnum
  LOOP
    total := total + 1;
    IF r.seq IS NULL THEN
      RAISE NOTICE '  ? %.% looks generated but has no resolvable sequence', r.table_name, r.column_name;
      CONTINUE;
    END IF;

    EXECUTE format('SELECT COALESCE(max(%I), 0) FROM public.%I', r.column_name, r.table_name)
      INTO max_id;
    -- Reading the sequence relation gives its state without consuming a value.
    -- is_called = true means "last_value is spent, the next one is last_value + 1".
    EXECUTE 'SELECT last_value, is_called FROM ' || r.seq INTO seq_now, called;
    will_hand := seq_now + CASE WHEN called THEN 1 ELSE 0 END;

    IF max_id = 0 THEN
      ok_count := ok_count + 1;  -- nothing to collide with yet; leave the gap alone
    ELSIF will_hand > max_id THEN
      ok_count := ok_count + 1;
    ELSE
      -- Aligning exactly: after this, the next value handed out is MAX(id) + 1.
      EXECUTE 'SELECT setval(' || quote_literal(r.seq) || ', ' || max_id || ', true)';
      item := format('%s.%s (next was %s, rows go to %s)', r.table_name, r.column_name, will_hand, max_id);
      behind := array_append(behind, item);
    END IF;
  END LOOP;

  RAISE NOTICE 'checked % generated columns: % already ahead, % were behind',
    total, ok_count, coalesce(array_length(behind, 1), 0);
  FOREACH item IN ARRAY behind LOOP
    RAISE NOTICE '  advanced %', item;
  END LOOP;
END $$;

-- Proof for the column that was actually breaking sign-in: the value the next INSERT
-- will ask for, read straight off the sequence so nothing is consumed.
SELECT 'login_attempts  max(id) = ' || COALESCE(max(id), 0) FROM public.login_attempts;
SELECT 'login_attempts  next insert will get id = ' ||
       (last_value + CASE WHEN is_called THEN 1 ELSE 0 END)
  FROM public.login_attempts_id_seq;
SQL
status=$?

echo
if [ "$status" = 0 ]; then
  echo "A colliding INSERT stops happening on its next attempt, not before — retry the thing that failed."
else
  fail "psql reported an error"
fi
exit "$status"

"""Apply ``supabase/migrations/*.sql`` in order, exactly once each.

Usage::

    python -m worker.scripts.migrate [--database-url URL] [--dir supabase/migrations]
    python -m worker.scripts.migrate --status
    python -m worker.scripts.migrate --dry-run

Rules (spec §18 operations):

* files run in lexicographic order, each inside its own transaction;
* a file is applied only when its name is absent from ``schema_migrations``;
* the recorded sha256 checksum is compared on every run — a file whose
  checksum changed after being applied fails loudly instead of silently
  re-running (that would mean someone edited shipped history);
* ``schema_migrations`` is created by this script when missing, mirroring the
  definition in 0001_init.sql so the very first run works;
* no tracebacks and never the connection string in output.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

DEFAULT_MIGRATIONS_DIR = "supabase/migrations"

# Mirrors the table defined at the top of 0001_init.sql. Creating it here (in
# addition to the migration file) is what makes the first run against an empty
# database possible: the runner must record 0001 itself.
ENSURE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_TRANSACTION_WORDS = {"BEGIN", "COMMIT", "END", "ROLLBACK", "START"}


def _strip_transaction_control(sql: str) -> str:
    """Remove top-level BEGIN/COMMIT lines so the runner owns the transaction.

    Dollar-quoted bodies (``DO $$ ... BEGIN ... END $$;``, trigger function
    bodies) also contain bare ``BEGIN`` lines; those must survive. We toggle
    dollar-quote state on every line with an odd number of ``$$`` occurrences
    and only drop control words outside dollar quotes. Comments are kept —
    Postgres strips them itself.
    """
    kept: list[str] = []
    in_dollar_quote = False
    for line in sql.splitlines():
        word = line.strip().rstrip(";").upper()
        if not in_dollar_quote and word in _TRANSACTION_WORDS:
            if line.count("$$") % 2:  # pathological, but keep the toggle honest
                in_dollar_quote = not in_dollar_quote
            continue
        kept.append(line)
        if line.count("$$") % 2:
            in_dollar_quote = not in_dollar_quote
    return "\n".join(kept)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _first_line(exc: BaseException) -> str:
    return str(exc).splitlines()[0].strip() if str(exc).strip() else type(exc).__name__


def _migration_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.sql"), key=lambda path: path.name)


def run(database_url: str, migrations_dir: str, *, dry_run: bool, status: bool) -> int:
    directory = Path(migrations_dir)
    if not directory.is_dir():
        print(f"error: migrations directory not found: {directory}", file=sys.stderr)
        return 2
    files = _migration_files(directory)
    if not files:
        print(f"error: no *.sql files found in {directory}", file=sys.stderr)
        return 2

    try:
        conn = psycopg.connect(database_url, application_name="scanner-migrate", autocommit=False)
    except psycopg.Error as exc:
        print(f"error: cannot connect to the database: {_first_line(exc)}", file=sys.stderr)
        return 1

    try:
        if status or dry_run:
            # Read-only modes never create anything; a database without the
            # registry table simply has nothing applied yet.
            try:
                rows = conn.execute("SELECT name, checksum FROM schema_migrations").fetchall()
            except psycopg.errors.UndefinedTable:
                conn.rollback()
                rows = []
        else:
            with conn:
                conn.execute(ENSURE_MIGRATIONS_TABLE)
            rows = conn.execute("SELECT name, checksum FROM schema_migrations").fetchall()
        applied = dict(rows)

        pending = 0
        for path in files:
            name = path.name
            checksum = _checksum(path)
            if name in applied:
                if applied[name] != checksum:
                    print(
                        f"ERROR: {name} was already applied but its checksum changed "
                        f"(recorded {applied[name][:12]}…, file now {checksum[:12]}…). "
                        "Shipped migrations are immutable: restore the file or reset the "
                        "database and re-run from scratch.",
                        file=sys.stderr,
                    )
                    return 3
                if status:
                    print(f"applied  {name}")
                else:
                    print(f"skip     {name} (already applied)")
                continue
            if status:
                print(f"pending  {name}")
                pending += 1
            elif dry_run:
                print(f"dry-run  {name} (would apply)")
                pending += 1
            else:
                sql = _strip_transaction_control(path.read_text())
                try:
                    with conn.transaction():
                        conn.execute(sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                            (name, checksum),
                        )
                except psycopg.Error as exc:
                    print(f"FAIL     {name}: {_first_line(exc)}", file=sys.stderr)
                    return 4
                print(f"applied  {name} (checksum {checksum[:12]}…)")

        if status:
            print(f"{len(applied)} applied, {pending} pending")
        elif dry_run:
            print(f"dry-run: {pending} file(s) would be applied")
        else:
            print(f"done: {len(files)} migration file(s) checked against this database")
        return 0
    except psycopg.Error as exc:
        print(f"error: migration failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except psycopg.Error:  # pragma: no cover - closing must not mask results
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m worker.scripts.migrate",
        description="Apply supabase/migrations/*.sql exactly once, tracked by checksum.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL conninfo (defaults to the DATABASE_URL env var)",
    )
    parser.add_argument(
        "--dir",
        dest="migrations_dir",
        default=DEFAULT_MIGRATIONS_DIR,
        help=f"directory holding *.sql migration files (default: {DEFAULT_MIGRATIONS_DIR})",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would apply; write nothing")
    parser.add_argument("--status", action="store_true", help="print applied vs pending; write nothing")
    args = parser.parse_args(argv)

    if not args.database_url:
        print(
            "error: DATABASE_URL is required (set the environment variable or pass "
            "--database-url). Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    return run(args.database_url, args.migrations_dir, dry_run=args.dry_run, status=args.status)


if __name__ == "__main__":
    raise SystemExit(main())

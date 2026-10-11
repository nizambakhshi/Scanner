"""Apply ``supabase/migrations/*.sql`` in order, exactly once each.

Usage::

    python -m worker.scripts.migrate [--database-url URL] [--dir supabase/migrations]
    python -m worker.scripts.migrate --status
    python -m worker.scripts.migrate --dry-run

Rules (spec §18 operations):

* files run in lexicographic order;
* a file is applied only when its name is absent from ``schema_migrations``;
* the recorded sha256 checksum is compared on every run — a file whose
  checksum changed after being applied fails loudly instead of silently
  re-running (that would mean someone edited shipped history);
* ``schema_migrations`` is created by this script when missing, mirroring the
  definition in 0001_init.sql so the very first run works;
* no tracebacks and never the connection string in output.

Execution model: **one statement per round trip, each autocommitted.**
Supabase's proxy drops a connection whose first message is a multi-kilobyte
multi-statement string ("the connection is closed"), so the whole-file
transaction this runner originally shipped can never complete there. Each
statement of a file is sent separately; a connection closed mid-file is
reopened and the remaining statements continue from the next one. A file is
registered in ``schema_migrations`` only after every statement has run, and
re-runs after a mid-file failure skip objects reported as "already exists" —
the shipped files are idempotent (``IF NOT EXISTS``/``OR REPLACE``/
``ON CONFLICT DO NOTHING``) so the resume is exact. The trade-off against the
original design is per-file atomicity; the registry plus idempotency replaces
it.
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


def _without_comment_lines(text: str) -> str:
    return "".join(
        line for line in text.splitlines(keepends=True)
        if not line.lstrip().startswith("--")
    ).strip()


def _split_statements(sql: str) -> list[str]:
    """Cut a migration file into individually executable statements.

    Boundaries are lines ending in ``;`` that sit outside a dollar-quoted body
    and outside a string literal. Full-line ``--`` comments are stripped from
    the front of each statement (a statement glued behind a comment would
    otherwise be dropped by naive "starts with --" checks). Known limits, safe
    for the shipped files: ``$$`` inside string literals and trailing comments
    containing an odd number of apostrophes are not tracked.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    in_string = False
    for line in sql.splitlines(keepends=True):
        # XOR toggle on any odd ``$$`` count: the same expression opens and
        # closes a dollar-quoted body, so it must not be guarded by the state.
        if not line.lstrip().startswith("--") and line.count("$$") % 2:
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and not line.lstrip().startswith("--") and line.count("'") % 2:
            in_string = not in_string
        if not in_dollar and not in_string and line.rstrip().endswith(";"):
            body = _without_comment_lines("".join(buf))
            if body:
                statements.append(body)
            buf = []
    tail = _without_comment_lines("".join(buf))
    if tail:
        statements.append(tail)
    return statements


class _Runner:
    """One autocommitted statement per round trip, surviving pooler severings.

    Managed proxies (Supabase's pgbouncer) close connections that arrive with
    a multi-statement string or an idle explicit transaction, and the app's own
    URL uses the transaction pooler. Reconnecting per failure keeps a partially
    applied file resumable; idempotent DDL plus the "already exists" skip makes
    the resume exact.
    """

    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.conn = psycopg.connect(database_url, application_name="scanner-migrate", autocommit=True)

    def _reset(self) -> None:
        try:
            self.conn.close()
        except psycopg.Error:
            pass
        self.conn = psycopg.connect(self.url, application_name="scanner-migrate", autocommit=True)

    def query(self, sql: str) -> list[tuple]:
        return self.execute(sql, fetch=True) or []

    def execute(self, sql: str, params: tuple | None = None, *, fetch: bool = False):
        for attempt in (1, 2):
            try:
                cur = self.conn.execute(sql, params) if params else self.conn.execute(sql)
                return cur.fetchall() if fetch else None
            except psycopg.OperationalError as exc:
                # Only a severed connection is retryable; a real failure must not
                # mask itself behind a reconnect loop.
                if attempt == 2 or "closed" not in str(exc).lower():
                    raise
                self._reset()
        return None

    def close(self) -> None:
        try:
            self.conn.close()
        except psycopg.Error:
            pass


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
        runner = _Runner(database_url)
    except psycopg.Error as exc:
        print(f"error: cannot connect to the database: {_first_line(exc)}", file=sys.stderr)
        return 1

    try:
        if status or dry_run:
            # Read-only modes never create anything; a database without the
            # registry table simply has nothing applied yet.
            try:
                rows = runner.query("SELECT name, checksum FROM schema_migrations")
            except psycopg.errors.UndefinedTable:
                rows = []
        else:
            runner.execute(ENSURE_MIGRATIONS_TABLE)
            rows = runner.query("SELECT name, checksum FROM schema_migrations")
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
                statements = _split_statements(_strip_transaction_control(path.read_text()))
                skipped = 0
                try:
                    for statement in statements:
                        try:
                            runner.execute(statement)
                        except psycopg.Error as exc:
                            # A re-run after a mid-file failure: an object that
                            # already exists means this statement already did
                            # its job on the previous attempt.
                            if "already exists" not in str(exc):
                                raise
                            skipped += 1
                except psycopg.Error as exc:
                    print(f"FAIL     {name}: {_first_line(exc)}", file=sys.stderr)
                    return 4
                runner.execute(
                    "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                    (name, checksum),
                )
                note = f", {skipped} pre-existing" if skipped else ""
                print(f"applied  {name} ({len(statements)} statements, checksum {checksum[:12]}…{note})")

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
        runner.close()


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

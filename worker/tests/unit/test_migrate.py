"""Statement splitting for the migration runner (no database needed).

The runner must send one statement per round trip — Supabase's proxy drops
connections that receive a whole migration file as a single query — so the
splitter's dollar-quote and string awareness is load-bearing: a mis-split
would silently apply half a migration.
"""

from worker.scripts.migrate import _split_statements, _strip_transaction_control


def test_plain_statements():
    sql = "CREATE TABLE a (id int);\nCREATE TABLE b (id int);\n"
    assert _split_statements(sql) == ["CREATE TABLE a (id int);", "CREATE TABLE b (id int);"]


def test_function_body_is_one_statement():
    sql = (
        "CREATE OR REPLACE FUNCTION f() RETURNS trigger AS $$\n"
        "BEGIN\n"
        "  NEW.updated_at := now();\n"
        "  RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "CREATE TRIGGER t BEFORE UPDATE ON x EXECUTE FUNCTION f();\n"
    )
    parts = _split_statements(sql)
    assert len(parts) == 2
    assert parts[0].startswith("CREATE OR REPLACE FUNCTION")
    assert parts[0].endswith("$$ LANGUAGE plpgsql;")
    assert "BEGIN" in parts[0]  # the body's BEGIN is not a boundary


def test_semicolon_and_apostrophe_inside_string_literal():
    sql = "INSERT INTO t (v) VALUES ('a;b');\nSELECT 1;\n"
    parts = _split_statements(sql)
    assert parts == ["INSERT INTO t (v) VALUES ('a;b');", "SELECT 1;"]


def test_multiline_string_literal():
    sql = "INSERT INTO t (v) VALUES (\n  'it''s; fine'\n);\nSELECT 2;\n"
    parts = _split_statements(sql)
    assert len(parts) == 2
    assert parts[0].count("SELECT") == 0


def test_leading_comment_lines_are_stripped_not_glued():
    sql = "-- explain\nCREATE TABLE c (id int);\n"
    assert _split_statements(sql) == ["CREATE TABLE c (id int);"]


def test_transaction_control_words_carry_bodies():
    sql = "BEGIN;\nCREATE TABLE d (id int);\nCOMMIT;\n"
    stripped = _strip_transaction_control(sql)
    assert "BEGIN" not in stripped and "COMMIT" not in stripped
    assert _split_statements(stripped) == ["CREATE TABLE d (id int);"]


def test_do_block_with_inner_statements():
    sql = (
        "DO $$\n"
        "BEGIN\n"
        "  EXECUTE 'CREATE TABLE e (id int)';\n"
        "END;\n"
        "$$;\n"
        "SELECT 3;\n"
    )
    parts = _split_statements(sql)
    assert len(parts) == 2
    assert parts[0].endswith("$$;")


def test_tail_without_semicolon_is_kept():
    assert _split_statements("SELECT 4") == ["SELECT 4"]

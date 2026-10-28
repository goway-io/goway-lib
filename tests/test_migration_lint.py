import json
import pathlib

from goway_lib import migration_lint


def test_revisions_are_read_from_the_migration_module(tmp_path: pathlib.Path):
    path = tmp_path / "abc_add.py"
    path.write_text('revision: str = "abc"\ndown_revision: str | None = "xyz"\nbranch_labels = None\n')
    assert migration_lint._revisions(path) == ("abc", "xyz")
    path.write_text('revision = "first"\ndown_revision = None\n')
    assert migration_lint._revisions(path) == ("first", None)


def test_outage_rules_fail_and_style_rules_only_note(tmp_path, monkeypatch, capsys):
    path = tmp_path / "abc_add.py"
    path.write_text('revision = "abc"\ndown_revision = "xyz"\n')
    monkeypatch.setattr(migration_lint, "_added_migrations", lambda base, versions: [path])
    monkeypatch.setattr(migration_lint, "_render", lambda revision, down: "ALTER TABLE t ADD COLUMN c int;")
    found = [
        {"rule_name": "prefer-text-field", "message": "varchar"},
        {"rule_name": "adding-required-field", "message": "NOT NULL without default"},
    ]
    monkeypatch.setattr(migration_lint, "_squawk", lambda sql: found)
    assert migration_lint.main(["--base", "main"]) == 1
    out = capsys.readouterr().out
    assert "::notice file=" in out and "prefer-text-field" in out
    assert "::error file=" in out and "adding-required-field" in out

    monkeypatch.setattr(migration_lint, "_squawk", lambda sql: found[:1])
    assert migration_lint.main(["--base", "main"]) == 0


def test_squawk_json_is_parsed(monkeypatch):
    class Done:
        returncode = 0
        stdout = json.dumps([{"rule_name": "ban-drop-column", "message": "drop"}])
        stderr = ""

    monkeypatch.setattr(migration_lint.subprocess, "run", lambda *a, **k: Done())
    assert migration_lint._squawk("ALTER TABLE t DROP COLUMN c;")[0]["rule_name"] == "ban-drop-column"

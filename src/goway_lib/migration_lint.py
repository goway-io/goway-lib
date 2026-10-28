"""Lints the Alembic migrations a change adds with squawk, before they reach a database that has traffic.

    goway-lint-migrations --base <git ref>

Each new file under migrations/versions is rendered to SQL offline (`alembic upgrade <down>:<rev> --sql`) and
checked by squawk. Rules that point at an outage (a rewrite of a big table, a lock held across a long statement,
a constraint validated under an exclusive lock, no lock timeout) fail the build; style rules that do not fit
this codebase are reported as notes. A migration that cannot render offline (it reads data through the
connection) is reported and skipped, never failed.
"""

import argparse
import ast
import json
import os
import pathlib
import shlex
import subprocess  # noqa: S404 - runs git, alembic and squawk with fixed argument lists
import sys
import tempfile

SQUAWK = os.environ.get("SQUAWK", "uvx --from squawk-cli==2.70.0 squawk")

#: Reported, never failing: style rules that do not fit here (UUIDv7 keys, bounded varchar on purpose, Alembic's
#: transactional DDL), and concurrent index builds, which matter once a table is big enough to notice the lock.
ADVISORY = frozenset(
    {
        "prefer-robust-stmts",
        "prefer-big-int",
        "prefer-bigint-over-int",
        "prefer-bigint-over-smallint",
        "prefer-identity",
        "prefer-text-field",
        "require-concurrent-index-creation",
        "require-concurrent-index-deletion",
    }
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607


def _added_migrations(base: str, versions: pathlib.Path) -> list[pathlib.Path]:
    if not base or set(base) == {"0"}:
        base = "HEAD~1"
    try:
        _git("rev-parse", "--verify", f"{base}^{{commit}}")
    except subprocess.CalledProcessError:
        print(f"base {base} is not in this checkout; linting the last commit only")
        base = "HEAD~1"
    names = _git("diff", "--name-only", "--diff-filter=A", base, "HEAD", "--", str(versions)).split()
    return [pathlib.Path(name) for name in names if name.endswith(".py")]


def _revisions(path: pathlib.Path) -> tuple[str, str | None]:
    values: dict[str, object] = {}
    for node in ast.parse(path.read_text()).body:
        target = (
            node.target
            if isinstance(node, ast.AnnAssign)
            else (node.targets[0] if isinstance(node, ast.Assign) else None)
        )
        if isinstance(target, ast.Name) and target.id in ("revision", "down_revision") and node.value is not None:
            values[target.id] = ast.literal_eval(node.value)
    down = values.get("down_revision")
    if isinstance(down, (tuple, list)):
        down = down[0] if down else None
    return str(values["revision"]), (str(down) if down else None)


def _render(revision: str, down: str | None) -> str | None:
    span = f"{down}:{revision}" if down else revision
    result = subprocess.run(["alembic", "upgrade", span, "--sql"], capture_output=True, text=True)  # noqa: S603, S607
    if result.returncode != 0:
        print(f"::notice::{revision}: cannot render offline, skipped ({result.stderr.strip().splitlines()[-1:]})")
        return None
    return result.stdout


def _squawk(sql: str) -> list[dict]:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as handle:
        handle.write(sql)
    try:
        result = subprocess.run(  # noqa: S603
            [*shlex.split(SQUAWK), "--reporter", "json", handle.name], capture_output=True, text=True
        )
    finally:
        os.unlink(handle.name)
    if result.returncode not in (0, 1):
        raise SystemExit(f"squawk failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout or "[]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=os.environ.get("MIGRATION_LINT_BASE", ""), help="git ref to compare with")
    parser.add_argument("--versions", default="migrations/versions", type=pathlib.Path)
    args = parser.parse_args(argv)

    files = _added_migrations(args.base, args.versions)
    if not files:
        print("no new migrations")
        return 0
    failed = False
    for path in files:
        revision, down = _revisions(path)
        sql = _render(revision, down)
        if sql is None:
            continue
        for violation in _squawk(sql):
            rule = violation.get("rule_name") or violation.get("rule") or "?"
            message = violation.get("message", "").strip()
            help_text = violation.get("help", "")
            level = "notice" if rule in ADVISORY else "error"
            failed |= level == "error"
            print(f"::{level} file={path}::{rule}: {message} {help_text}".strip())
        print(f"{path}: checked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

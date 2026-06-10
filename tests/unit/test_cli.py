"""Unit tests for the offline CLI commands via Typer's CliRunner."""

from __future__ import annotations

import os
from contextlib import contextmanager

from typer.testing import CliRunner

from langmigrate.cli.main import app

runner = CliRunner()


@contextmanager
def chdir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def test_init_creates_config_and_dir(tmp_path):
    with chdir(tmp_path):
        result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "langmigrate.toml").is_file()
    assert (tmp_path / "migrations").is_dir()
    # Scaffolded package files.
    assert (tmp_path / "migrations" / "__init__.py").is_file()
    readme = tmp_path / "migrations" / "README.md"
    assert readme.is_file()
    assert "langmigrate revision" in readme.read_text()


def test_init_idempotent_leaves_files_untouched(tmp_path):
    with chdir(tmp_path):
        assert runner.invoke(app, ["init"]).exit_code == 0
        (tmp_path / "migrations" / "README.md").write_text("custom")
        assert runner.invoke(app, ["init"]).exit_code == 0
    # A second init must not clobber an existing README.
    assert (tmp_path / "migrations" / "README.md").read_text() == "custom"


def test_init_with_example_scaffolds_first_revision(tmp_path):
    with chdir(tmp_path):
        result = runner.invoke(app, ["init", "--example"])
        assert result.exit_code == 0
        revisions = list((tmp_path / "migrations").glob("*_initial.py"))
        assert len(revisions) == 1
        # The scaffolded revision is discoverable and forms a clean single head.
        assert runner.invoke(app, ["check"]).exit_code == 0


def test_revision_then_history_and_check(tmp_path):
    with chdir(tmp_path):
        assert runner.invoke(app, ["init"]).exit_code == 0
        r1 = runner.invoke(app, ["revision", "-m", "add context field"])
        assert r1.exit_code == 0, r1.output
        r2 = runner.invoke(app, ["revision", "-m", "rename msgs"])
        assert r2.exit_code == 0, r2.output

        files = sorted(
            f for f in (tmp_path / "migrations").glob("*.py") if not f.name.startswith("_")
        )
        assert len(files) == 2
        assert any("add_context_field" in f.name for f in files)

        hist = runner.invoke(app, ["history"])
        assert hist.exit_code == 0
        assert "(base)" in hist.output

        # Generated stubs return state unchanged in downgrade (reversible) -> check OK.
        chk = runner.invoke(app, ["check"])
        assert chk.exit_code == 0, chk.output
        assert "single head" in chk.output

        cur = runner.invoke(app, ["current"])
        assert cur.exit_code == 0
        assert "code head:" in cur.output


def test_revision_chains_down_revision(tmp_path):
    with chdir(tmp_path):
        runner.invoke(app, ["init"])
        runner.invoke(app, ["revision", "-m", "first"])
        out = runner.invoke(app, ["revision", "-m", "second"]).output
    # second revision should reference the first as its down_revision
    assert "down_revision =" in out or "down_revision" in out


_IRREVERSIBLE_MIG = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "irr1"
    down_revision = None
    def upgrade(self, s): return self.drop_field(s, "x")
    # no downgrade override -> irreversible
"""

_HEAD_A = """
from langmigrate import BaseMigration
class A(BaseMigration):
    revision = "ha"
    down_revision = None
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""

_HEAD_B = """
from langmigrate import BaseMigration
class B(BaseMigration):
    revision = "hb"
    down_revision = None
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""


def test_history_and_check_error_without_migrations_dir(tmp_path):
    # No migrations/ directory at all -> _load_registry exits 1 with guidance.
    with chdir(tmp_path):
        result = runner.invoke(app, ["history"])
    assert result.exit_code == 1
    assert "No migrations directory" in result.output


def test_history_empty_when_dir_has_no_revisions(tmp_path):
    (tmp_path / "migrations").mkdir()
    with chdir(tmp_path):
        result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "(no revisions yet)" in result.output


def test_check_flags_irreversible_migration(tmp_path):
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "irr1_drop.py").write_text(_IRREVERSIBLE_MIG)
    with chdir(tmp_path):
        result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "irreversible" in result.output.lower()


def test_check_flags_multiple_heads(tmp_path):
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "ha.py").write_text(_HEAD_A)
    (migs / "hb.py").write_text(_HEAD_B)
    with chdir(tmp_path):
        result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "Multiple heads" in result.output


def test_revision_blocks_on_multiple_heads(tmp_path):
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "ha.py").write_text(_HEAD_A)
    (migs / "hb.py").write_text(_HEAD_B)
    with chdir(tmp_path):
        result = runner.invoke(app, ["revision", "-m", "next"])
    assert result.exit_code == 1
    assert "multiple heads" in result.output.lower()


_DUP_A = """
from langmigrate import BaseMigration
class A(BaseMigration):
    revision = "dup1"
    down_revision = None
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""

_DUP_B = """
from langmigrate import BaseMigration
class B(BaseMigration):
    revision = "dup1"
    down_revision = None
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""


def test_registry_errors_render_message_not_traceback(tmp_path):
    # Two files declaring the same revision id -> DuplicateRevisionError must
    # surface as a red message + exit 1, not a raw traceback.
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "a.py").write_text(_DUP_A)
    (migs / "b.py").write_text(_DUP_B)
    with chdir(tmp_path):
        for command in (["history"], ["revision", "-m", "next"]):
            result = runner.invoke(app, command)
            assert result.exit_code == 1
            assert "Duplicate revision" in result.output
            assert result.exception is None or isinstance(result.exception, SystemExit)


def test_merge_joins_heads_into_single_head(tmp_path):
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "ha.py").write_text(_HEAD_A)
    (migs / "hb.py").write_text(_HEAD_B)
    with chdir(tmp_path):
        # Two heads -> check fails.
        assert runner.invoke(app, ["check"]).exit_code == 1

        result = runner.invoke(app, ["merge", "-m", "join heads"])
        assert result.exit_code == 0, result.output
        assert "merge revision" in result.output.lower()

        # The DAG now has a single head and check passes.
        chk = runner.invoke(app, ["check"])
        assert chk.exit_code == 0, chk.output

        hist = runner.invoke(app, ["history"])
        assert hist.exit_code == 0
        assert "ha + hb" in hist.output

        # And `revision` chains onto the merge head without complaining.
        assert runner.invoke(app, ["revision", "-m", "next"]).exit_code == 0


def test_merge_with_single_head_exits(tmp_path):
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "ha.py").write_text(_HEAD_A)
    with chdir(tmp_path):
        result = runner.invoke(app, ["merge", "-m", "nothing"])
    assert result.exit_code == 1
    assert "at least two" in result.output


def test_merge_unknown_revision_exits(tmp_path):
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "ha.py").write_text(_HEAD_A)
    (migs / "hb.py").write_text(_HEAD_B)
    with chdir(tmp_path):
        result = runner.invoke(app, ["merge", "ha", "ghost", "-m", "bad"])
    assert result.exit_code == 1
    assert "ghost" in result.output


def test_merge_rejects_ancestor_descendant_pair(tmp_path):
    chained = """
from langmigrate import BaseMigration
class C(BaseMigration):
    revision = "hc"
    down_revision = "ha"
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "ha.py").write_text(_HEAD_A)
    (migs / "hc.py").write_text(chained)
    with chdir(tmp_path):
        result = runner.invoke(app, ["merge", "ha", "hc", "-m", "bad"])
    assert result.exit_code == 1
    assert "descendant" in result.output

"""Unit tests for the DB-backed CLI commands (upgrade/downgrade/stamp).

A fake adapter over an in-memory saver is injected via ``_build_adapter`` so the
CLI logic — warnings, confirmation, error rendering — is exercised without a real
database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from typer.testing import CliRunner

from langmigrate.cli import main as cli_main
from langmigrate.cli.main import app
from langmigrate.config import DEFAULT_CONFIG_TOML
from langmigrate.core.types import REVISION_METADATA_KEY
from langmigrate.core.version import read_revision

runner = CliRunner()


@contextmanager
def chdir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


class FakeAdapter:
    """CheckpointAdapter over InMemorySaver, seeded with one legacy checkpoint."""

    def __init__(self) -> None:
        self.saver = InMemorySaver()
        self.closed = False
        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        chk = empty_checkpoint()
        chk["channel_values"] = {"msgs": ["hi"], "count": 1}
        chk["channel_versions"] = {"msgs": 1, "count": 1}
        self.saver.put(config, chk, {"source": "loop"}, {"msgs": 1, "count": 1})

    def setup(self) -> None:
        pass

    def _all(self):
        return list(self.saver.list(None))

    def count_stale(self, head: str) -> int:
        return sum(1 for t in self._all() if read_revision(t.metadata) != head)

    def iter_stale_configs(self, head: str) -> Iterator[dict]:
        for t in self._all():
            if read_revision(t.metadata) != head:
                yield t.config

    def iter_all_configs(self) -> Iterator[dict]:
        for t in self._all():
            yield t.config

    def stamp_all(self, revision: str) -> int:
        count = 0
        for t in self._all():
            cfg = t.parent_config or {
                "configurable": {
                    "thread_id": t.config["configurable"]["thread_id"],
                    "checkpoint_ns": t.config["configurable"].get("checkpoint_ns", ""),
                }
            }
            meta = {**(t.metadata or {}), REVISION_METADATA_KEY: revision}
            self.saver.put(cfg, t.checkpoint, meta, {})
            count += 1
        return count

    def revision_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self._all():
            key = read_revision(t.metadata) or "<untagged>"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def close(self) -> None:
        self.closed = True


# Two linear migrations: v0 -> a1c0 (add context) -> b2d1 (rename msgs->messages).
_MIG_A = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "a1c0"
    down_revision = None
    def upgrade(self, s): return self.add_field(s, "context", factory=dict)
    def downgrade(self, s): return self.drop_field(s, "context")
"""
_MIG_B = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    def upgrade(self, s): return self.rename_field(s, "msgs", "messages")
    def downgrade(self, s): return self.rename_field(s, "messages", "msgs")
"""


def _project(tmp_path) -> FakeAdapter:
    (tmp_path / "langmigrate.toml").write_text(DEFAULT_CONFIG_TOML)
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "a1c0_add_context.py").write_text(_MIG_A)
    (migs / "b2d1_rename.py").write_text(_MIG_B)
    return FakeAdapter()


def _patch_adapter(monkeypatch, adapter: FakeAdapter) -> None:
    monkeypatch.setattr(cli_main, "_build_adapter", lambda cfg: adapter)


def test_upgrade_command_migrates(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["upgrade", "head"])
    assert result.exit_code == 0, result.output
    assert "migrated 1/1" in result.output
    assert adapter.closed
    (t,) = list(adapter.saver.list(None))
    assert t.metadata[REVISION_METADATA_KEY] == "b2d1"
    assert t.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}


def test_upgrade_dry_run_does_not_write(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["upgrade", "head", "--online-dry-run"])
    assert result.exit_code == 0
    assert "would migrate 1/1" in result.output
    (t,) = list(adapter.saver.list(None))
    assert REVISION_METADATA_KEY not in t.metadata


def test_downgrade_to_higher_revision_shows_clear_error(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        runner.invoke(app, ["upgrade", "a1c0"])  # bring DB to a1c0
        # Now ask to "downgrade" to b2d1, which sits ABOVE a1c0.
        result = runner.invoke(app, ["downgrade", "b2d1"])
    assert result.exit_code == 1
    assert "not an ancestor" in result.output


def test_stamp_requires_confirmation(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        # Decline the confirmation prompt.
        result = runner.invoke(app, ["stamp", "b2d1"], input="n\n")
    assert result.exit_code != 0  # aborted
    assert "WARNING" in result.output
    (t,) = list(adapter.saver.list(None))
    assert REVISION_METADATA_KEY not in t.metadata  # nothing stamped


def test_stamp_yes_skips_prompt_and_tags(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["stamp", "b2d1", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Stamped 1" in result.output
    (t,) = list(adapter.saver.list(None))
    assert t.metadata[REVISION_METADATA_KEY] == "b2d1"
    # Data NOT migrated by stamp — still legacy.
    assert t.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}


def test_stamp_unknown_revision_rejected(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["stamp", "ghost", "--yes"])
    assert result.exit_code != 0


def test_current_db_shows_revision_distribution(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["current", "--db"])
    assert result.exit_code == 0, result.output
    assert "database revisions:" in result.output
    assert "<untagged>" in result.output  # the seeded legacy checkpoint is untagged
    assert adapter.closed


def test_build_adapter_rejects_unknown_backend(tmp_path):
    (tmp_path / "langmigrate.toml").write_text(
        '[langmigrate]\nbackend = "mysql"\nurl = "mysql://x"\n'
    )
    (tmp_path / "migrations").mkdir()
    with chdir(tmp_path):
        result = runner.invoke(app, ["current", "--db"])
    assert result.exit_code == 1
    assert "Unknown backend" in result.output


def test_build_adapter_requires_url(tmp_path):
    (tmp_path / "langmigrate.toml").write_text('[langmigrate]\nbackend = "postgres"\n')
    (tmp_path / "migrations").mkdir()
    with chdir(tmp_path):
        result = runner.invoke(app, ["current", "--db"])
    assert result.exit_code == 1
    assert "No database url" in result.output


def test_upgrade_unknown_target_renders_error(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["upgrade", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
    assert adapter.closed  # adapter still closed in the finally block

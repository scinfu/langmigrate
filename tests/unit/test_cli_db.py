"""Unit tests for the DB-backed CLI commands (upgrade/downgrade/stamp).

A fake adapter over an in-memory saver is injected via ``_build_adapter`` so the
CLI logic — warnings, confirmation, error rendering — is exercised without a real
database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import typer
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from typer.testing import CliRunner

from langmigrate.cli import main as cli_main
from langmigrate.cli.main import app
from langmigrate.config import DEFAULT_CONFIG_TOML, LangMigrateConfig
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
    assert result.exit_code == 1
    # Rendered as a clean message, not a raw traceback (consistent with the rest
    # of the CLI). Typer would have set ``result.exception`` if it had escaped.
    assert "not found" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


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


_MIG_POISON = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "p1"
    down_revision = None
    def upgrade(self, s):
        if s.values.get("count") == 666:
            raise ValueError("poisoned checkpoint")
        return self.add_field(s, "context", factory=dict)
    def downgrade(self, s): return self.drop_field(s, "context")
"""


def _poison_project(tmp_path) -> FakeAdapter:
    (tmp_path / "langmigrate.toml").write_text(DEFAULT_CONFIG_TOML)
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "p1_poison.py").write_text(_MIG_POISON)
    adapter = FakeAdapter()
    # Add a second, poisoned checkpoint alongside the healthy seeded one.
    config = {"configurable": {"thread_id": "bad", "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"count": 666}
    chk["channel_versions"] = {"count": 1}
    adapter.saver.put(config, chk, {"source": "loop"}, {"count": 1})
    return adapter


def test_upgrade_continue_on_error_reports_and_exits_nonzero(tmp_path, monkeypatch):
    adapter = _poison_project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["upgrade", "head", "--continue-on-error"])
    assert result.exit_code == 1
    assert "1 failed" in result.output
    assert "ValueError" in result.output
    assert "poisoned" in result.output
    # The healthy checkpoint was still migrated.
    healed = {
        t.config["configurable"]["thread_id"]: read_revision(t.metadata)
        for t in adapter.saver.list(None)
    }
    assert healed["t1"] == "p1"
    assert healed["bad"] is None


def test_upgrade_without_continue_on_error_aborts(tmp_path, monkeypatch):
    adapter = _poison_project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["upgrade", "head"])
    # The raw exception propagates (no tolerance requested).
    assert result.exit_code != 0


# -- langmigrate store ... -----------------------------------------------------

_STORE_MIG = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "s1"
    down_revision = None
    def upgrade(self, s): return self.add_field(s, "kind", default="memory")
    def downgrade(self, s): return self.drop_field(s, "kind")
"""


class FakeStoreAdapter:
    """StoreAdapter over InMemoryStore, seeded with one legacy item."""

    def __init__(self) -> None:
        from langgraph.store.memory import InMemoryStore

        self.store = InMemoryStore()
        self.closed = False
        self.store.put(("memories", "u1"), "m1", {"text": "hi"})

    def setup(self) -> None:
        pass

    def _all_items(self):
        for namespace, items in self.store._data.items():
            for key, item in items.items():
                yield namespace, key, item

    def iter_stale_items(self, head):
        from langmigrate.core.version import read_value_revision

        for namespace, key, item in self._all_items():
            if read_value_revision(item.value) != head:
                yield namespace, key

    def iter_all_items(self):
        for namespace, key, _ in self._all_items():
            yield namespace, key

    def revision_counts(self):
        from langmigrate.core.version import read_value_revision

        counts = {}
        for _, _, item in self._all_items():
            rev = read_value_revision(item.value) or "<untagged>"
            counts[rev] = counts.get(rev, 0) + 1
        return counts

    def stamp_all(self, revision: str) -> int:
        count = 0
        for namespace, key, item in list(self._all_items()):
            self.store.put(namespace, key, {**item.value, REVISION_METADATA_KEY: revision})
            count += 1
        return count

    def close(self) -> None:
        self.closed = True


def _store_project(tmp_path) -> FakeStoreAdapter:
    (tmp_path / "langmigrate.toml").write_text(DEFAULT_CONFIG_TOML)
    migs = tmp_path / "store_migrations"
    migs.mkdir()
    (migs / "s1_add_kind.py").write_text(_STORE_MIG)
    return FakeStoreAdapter()


def test_store_upgrade_command_migrates(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "upgrade", "head"])
    assert result.exit_code == 0, result.output
    assert "migrated 1/1" in result.output
    assert adapter.closed
    item = adapter.store.get(("memories", "u1"), "m1")
    assert item.value[REVISION_METADATA_KEY] == "s1"
    assert item.value["kind"] == "memory"


def test_store_stamp_unknown_revision_rejected(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "stamp", "ghost", "--yes"])
    assert result.exit_code == 1
    # Clean message, not a raw traceback (consistent with the rest of the CLI).
    assert "not found" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_store_history_and_check(tmp_path):
    _store_project(tmp_path)
    with chdir(tmp_path):
        hist = runner.invoke(app, ["store", "history"])
        assert hist.exit_code == 0, hist.output
        assert "s1" in hist.output
        chk = runner.invoke(app, ["store", "check"])
        assert chk.exit_code == 0, chk.output


def test_store_revision_creates_file(tmp_path):
    _store_project(tmp_path)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "revision", "-m", "add tags"])
    assert result.exit_code == 0, result.output
    files = list((tmp_path / "store_migrations").glob("*_add_tags.py"))
    assert len(files) == 1


def test_store_commands_require_store_dir(tmp_path):
    (tmp_path / "langmigrate.toml").write_text(DEFAULT_CONFIG_TOML)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "history"])
    assert result.exit_code == 1
    assert "init --with-store" in result.output


def test_init_with_store_scaffolds_directory(tmp_path):
    with chdir(tmp_path):
        result = runner.invoke(app, ["init", "--with-store"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "store_migrations").is_dir()
    assert (tmp_path / "store_migrations" / "__init__.py").is_file()


# -- adapter factories (no real connection) ----------------------------------


def test_build_adapter_redis_and_postgres(monkeypatch):
    import langmigrate.adapters.postgres as pg_mod
    import langmigrate.adapters.redis as redis_mod

    redis_sentinel, pg_sentinel = object(), object()
    monkeypatch.setattr(redis_mod.RedisAdapter, "from_conn_string", lambda url: redis_sentinel)
    monkeypatch.setattr(pg_mod.PostgresAdapter, "from_conn_string", lambda url: pg_sentinel)

    redis_cfg = LangMigrateConfig(backend="redis", url="redis://x")
    pg_cfg = LangMigrateConfig(backend="postgres", url="postgres://x")
    assert cli_main._build_adapter(redis_cfg) is redis_sentinel
    assert cli_main._build_adapter(pg_cfg) is pg_sentinel


def test_build_store_adapter_postgres(monkeypatch):
    import langmigrate.adapters.postgres as pg_mod

    sentinel = object()
    monkeypatch.setattr(pg_mod.PostgresStoreAdapter, "from_conn_string", lambda url: sentinel)
    assert (
        cli_main._build_store_adapter(LangMigrateConfig(backend="postgres", url="postgres://x"))
        is sentinel
    )


def test_build_store_adapter_rejects_redis_and_unknown_and_missing_url():
    with pytest.raises(typer.Exit):
        cli_main._build_store_adapter(LangMigrateConfig(backend="redis", url="redis://x"))
    with pytest.raises(typer.Exit):
        cli_main._build_store_adapter(LangMigrateConfig(backend="sqlite", url="x"))
    with pytest.raises(typer.Exit):
        cli_main._build_store_adapter(LangMigrateConfig(backend="postgres", url=None))


# -- history / current with multiple heads -----------------------------------

_MIG_BASE = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "base0"
    down_revision = None
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""
_MIG_HEAD_A = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "heada"
    down_revision = "base0"
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""
_MIG_HEAD_B = """
from langmigrate import BaseMigration
class M(BaseMigration):
    revision = "headb"
    down_revision = "base0"
    def upgrade(self, s): return s
    def downgrade(self, s): return s
"""


def _multihead_project(tmp_path) -> None:
    (tmp_path / "langmigrate.toml").write_text(DEFAULT_CONFIG_TOML)
    migs = tmp_path / "migrations"
    migs.mkdir()
    (migs / "base0.py").write_text(_MIG_BASE)
    (migs / "heada.py").write_text(_MIG_HEAD_A)
    (migs / "headb.py").write_text(_MIG_HEAD_B)


def test_history_dedups_shared_ancestor_across_heads(tmp_path):
    _multihead_project(tmp_path)
    with chdir(tmp_path):
        result = runner.invoke(app, ["history"])
    assert result.exit_code == 0, result.output
    # `base0` is shared by both heads but printed as a row once (de-dup across
    # lineages): it is the only base, so "(base)" appears exactly once.
    assert result.output.count("(base)") == 1
    assert "heada" in result.output and "headb" in result.output


def test_current_reports_multiple_heads(tmp_path):
    _multihead_project(tmp_path)
    with chdir(tmp_path):
        result = runner.invoke(app, ["current"])
    # Multiple heads are surfaced (warning), not a crash.
    assert "Multiple heads" in result.output or "heads" in result.output.lower()


# -- misc helper branches -----------------------------------------------------


def test_upgrade_rejects_invalid_unknown_revision_policy(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["upgrade", "head", "--on-unknown-revision", "bogus"])
    assert result.exit_code == 1
    assert "Invalid --on-unknown-revision" in result.output


def test_report_batch_result_truncates_long_failure_list():
    from langmigrate.runtime.batch import BatchFailure, BatchResult

    failures = [BatchFailure(ref=f"r{i}", error="e", error_type="ValueError") for i in range(25)]
    result = BatchResult(
        target="head", total=25, migrated=0, dry_run=False, failed=25, failures=failures
    )
    with pytest.raises(typer.Exit):
        cli_main._report_batch_result(result)


def test_downgrade_command_succeeds(tmp_path, monkeypatch):
    adapter = _project(tmp_path)
    _patch_adapter(monkeypatch, adapter)
    with chdir(tmp_path):
        runner.invoke(app, ["upgrade", "head"])
        result = runner.invoke(app, ["downgrade", "base"])
    assert result.exit_code == 0, result.output
    assert "migrated" in result.output
    (t,) = list(adapter.saver.list(None))
    assert read_revision(t.metadata) is None  # downgraded past base -> untagged


# -- store DB commands --------------------------------------------------------


def test_store_current_db_shows_distribution(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "current", "--db"])
    assert result.exit_code == 0, result.output
    assert "code head: s1" in result.output
    assert adapter.closed


def test_store_upgrade_unknown_target_errors(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "upgrade", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_store_downgrade_command_reverts(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        runner.invoke(app, ["store", "upgrade", "head"])
        result = runner.invoke(app, ["store", "downgrade", "base"])
    assert result.exit_code == 0, result.output
    assert "migrated" in result.output
    item = adapter.store.get(("memories", "u1"), "m1")
    assert REVISION_METADATA_KEY not in item.value  # tag stripped at base


def test_store_stamp_yes_tags(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "stamp", "s1", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Stamped 1" in result.output
    item = adapter.store.get(("memories", "u1"), "m1")
    assert item.value[REVISION_METADATA_KEY] == "s1"


def test_store_stamp_requires_confirmation(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        # Decline the confirmation prompt -> aborted, nothing stamped.
        result = runner.invoke(app, ["store", "stamp", "s1"], input="n\n")
    assert result.exit_code != 0
    assert "WARNING" in result.output
    item = adapter.store.get(("memories", "u1"), "m1")
    assert REVISION_METADATA_KEY not in item.value


def test_store_downgrade_unknown_target_errors(tmp_path, monkeypatch):
    adapter = _store_project(tmp_path)
    monkeypatch.setattr(cli_main, "_build_store_adapter", lambda cfg: adapter)
    with chdir(tmp_path):
        result = runner.invoke(app, ["store", "downgrade", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output

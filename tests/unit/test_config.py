"""Unit tests for langmigrate.toml loading and env overrides."""

from __future__ import annotations

from pathlib import Path

from langmigrate.config import ENV_URL, LangMigrateConfig


def test_load_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_URL, raising=False)
    cfg = LangMigrateConfig.load(tmp_path / "absent.toml")
    assert cfg.migrations_dir == "migrations"
    assert cfg.backend == "postgres"
    assert cfg.url is None
    assert cfg.migrations_path == Path("migrations")


def test_load_reads_toml(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_URL, raising=False)
    toml = tmp_path / "langmigrate.toml"
    toml.write_text(
        '[langmigrate]\nmigrations_dir = "revs"\nbackend = "redis"\n'
        'url = "redis://localhost:6379"\n'
    )
    cfg = LangMigrateConfig.load(toml)
    assert cfg.migrations_dir == "revs"
    assert cfg.backend == "redis"
    assert cfg.url == "redis://localhost:6379"


def test_env_url_overrides_toml(tmp_path, monkeypatch):
    toml = tmp_path / "langmigrate.toml"
    toml.write_text('[langmigrate]\nurl = "postgresql://from-file/db"\n')
    monkeypatch.setenv(ENV_URL, "postgresql://from-env/db")
    cfg = LangMigrateConfig.load(toml)
    assert cfg.url == "postgresql://from-env/db"

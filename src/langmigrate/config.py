"""Project configuration loaded from ``langmigrate.toml``.

Example ``langmigrate.toml``::

    [langmigrate]
    migrations_dir = "migrations"
    backend = "postgres"
    url = "postgresql://user:pass@localhost:5432/db"

The ``url`` may be overridden by the ``LANGMIGRATE_URL`` environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only (tomllib is stdlib on 3.11+)
    import tomli as tomllib  # type: ignore[no-redef,import-not-found,unused-ignore]

DEFAULT_CONFIG_FILE = "langmigrate.toml"
ENV_URL = "LANGMIGRATE_URL"


@dataclass
class LangMigrateConfig:
    """Resolved configuration for a LangMigrate project."""

    migrations_dir: str = "migrations"
    store_migrations_dir: str = "store_migrations"
    backend: str = "postgres"
    url: str | None = None

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_FILE) -> LangMigrateConfig:
        """Load config from ``path`` (if present), applying env overrides."""
        data: dict = {}
        config_path = Path(path)
        if config_path.is_file():
            with config_path.open("rb") as fh:
                data = tomllib.load(fh).get("langmigrate", {})
        cfg = cls(
            migrations_dir=data.get("migrations_dir", "migrations"),
            store_migrations_dir=data.get("store_migrations_dir", "store_migrations"),
            backend=data.get("backend", "postgres"),
            url=data.get("url"),
        )
        if os.environ.get(ENV_URL):
            cfg.url = os.environ[ENV_URL]
        return cfg

    @property
    def migrations_path(self) -> Path:
        return Path(self.migrations_dir)

    @property
    def store_migrations_path(self) -> Path:
        return Path(self.store_migrations_dir)


DEFAULT_CONFIG_TOML = """\
[langmigrate]
# Directory holding revision scripts.
migrations_dir = "migrations"

# Directory holding store (BaseStore) revision scripts, used by `langmigrate store`.
# store_migrations_dir = "store_migrations"

# Persistence backend: "postgres" or "redis" (both implemented).
backend = "postgres"

# Database connection string. Can be overridden by the LANGMIGRATE_URL env var.
url = "postgresql://langmigrate:langmigrate@localhost:5442/langmigrate"
"""

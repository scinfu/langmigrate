"""LangMigrate — declarative schema migrations for LangGraph state persistence.

See ``CLAUDE.md`` for architecture and conventions.
"""

from .core.engine import HEAD, MigrationEngine
from .core.exceptions import (
    ChannelRemovalUnsupportedError,
    CyclicHistoryError,
    DuplicateRevisionError,
    InvalidMigrationGraphError,
    IrreversibleMigrationError,
    LangMigrateError,
    MissingRequiredFieldError,
    MultipleHeadsError,
    ReservedKeyCollisionError,
    RevisionNotAncestorError,
    RevisionNotFoundError,
    TopologyMismatchError,
    UnsafeMigrationError,
)
from .core.migration import BaseMigration, FunctionMigration, migration
from .core.registry import MigrationRegistry, new_revision_id
from .core.topology import NodeRemap
from .core.types import (
    REVISION_METADATA_KEY,
    OnReservedKeyCollision,
    OnUnknownRevision,
    RevisionMeta,
    StateEnvelope,
)
from .integrations.state import migrate_state_update
from .runtime.batch import (
    BatchFailure,
    BatchResult,
    arun_batch_downgrade,
    arun_batch_upgrade,
    run_batch_downgrade,
    run_batch_upgrade,
    run_store_batch_downgrade,
    run_store_batch_upgrade,
)
from .runtime.factory import setup_langmigrate, setup_langmigrate_store
from .runtime.interceptor import MigrationInterceptor
from .runtime.store import MigrationStore

__version__ = "1.2.0"

__all__ = [
    "__version__",
    "HEAD",
    "BaseMigration",
    "FunctionMigration",
    "migration",
    "MigrationEngine",
    "MigrationRegistry",
    "MigrationInterceptor",
    "MigrationStore",
    "OnUnknownRevision",
    "OnReservedKeyCollision",
    "setup_langmigrate",
    "setup_langmigrate_store",
    "new_revision_id",
    "NodeRemap",
    "StateEnvelope",
    "RevisionMeta",
    "REVISION_METADATA_KEY",
    "BatchFailure",
    "BatchResult",
    "run_batch_upgrade",
    "run_batch_downgrade",
    "arun_batch_upgrade",
    "arun_batch_downgrade",
    "run_store_batch_upgrade",
    "run_store_batch_downgrade",
    "migrate_state_update",
    # exceptions
    "LangMigrateError",
    "UnsafeMigrationError",
    "MissingRequiredFieldError",
    "RevisionNotFoundError",
    "RevisionNotAncestorError",
    "DuplicateRevisionError",
    "CyclicHistoryError",
    "MultipleHeadsError",
    "IrreversibleMigrationError",
    "TopologyMismatchError",
    "ChannelRemovalUnsupportedError",
    "ReservedKeyCollisionError",
    "InvalidMigrationGraphError",
]

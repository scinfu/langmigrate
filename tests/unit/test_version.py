"""Unit tests for reading/writing the revision tag in checkpoint metadata."""

from __future__ import annotations

from langmigrate.core.types import REVISION_METADATA_KEY, StateEnvelope
from langmigrate.core.version import (
    envelope_from_parts,
    metadata_for,
    read_revision,
    stamp_metadata,
)


def test_read_revision_present():
    assert read_revision({REVISION_METADATA_KEY: "abc"}) == "abc"


def test_read_revision_absent_or_empty():
    assert read_revision({}) is None
    assert read_revision(None) is None
    assert read_revision({"source": "loop"}) is None


def test_read_revision_ignores_non_string():
    assert read_revision({REVISION_METADATA_KEY: 123}) is None


def test_stamp_metadata_does_not_mutate_input():
    src = {"source": "loop"}
    out = stamp_metadata(src, "v2")
    assert out == {"source": "loop", REVISION_METADATA_KEY: "v2"}
    assert src == {"source": "loop"}


def test_envelope_from_parts_reads_tag():
    env = envelope_from_parts({"a": 1}, {REVISION_METADATA_KEY: "v1", "source": "loop"})
    assert env.revision == "v1"
    assert env.values == {"a": 1}
    assert env.metadata["source"] == "loop"


def test_metadata_for_stamps_revision():
    env = StateEnvelope(values={}, revision="v3", metadata={"source": "loop"})
    assert metadata_for(env) == {"source": "loop", REVISION_METADATA_KEY: "v3"}


def test_metadata_for_strips_tag_when_unrevisioned():
    env = StateEnvelope(values={}, revision=None, metadata={REVISION_METADATA_KEY: "stale"})
    assert REVISION_METADATA_KEY not in metadata_for(env)


def test_roundtrip():
    env = envelope_from_parts({"a": 1}, {REVISION_METADATA_KEY: "v1"})
    env2 = env.with_revision("v2")
    assert read_revision(metadata_for(env2)) == "v2"

"""Unit test for `langmigrate revision --autogenerate`."""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import contextmanager

from typer.testing import CliRunner

from langmigrate.cli.main import app
from langmigrate.core.registry import MigrationRegistry

runner = CliRunner()


@contextmanager
def chdir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


SCHEMA_V1 = """
from typing import TypedDict

class State(TypedDict):
    messages: list
    count: int
    context: dict
"""


def _write_schema_module(tmp_path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(body)


def test_autogenerate_creates_filled_revision(tmp_path):
    _write_schema_module(tmp_path, "myschema", SCHEMA_V1)
    sys.path.insert(0, str(tmp_path))
    try:
        with chdir(tmp_path):
            assert runner.invoke(app, ["init"]).exit_code == 0
            importlib.invalidate_caches()
            result = runner.invoke(
                app,
                [
                    "revision",
                    "-m",
                    "initial schema",
                    "--autogenerate",
                    "--schema",
                    "myschema:State",
                ],
            )
            assert result.exit_code == 0, result.output

            files = [
                f for f in (tmp_path / "migrations").glob("*.py") if not f.name.startswith("_")
            ]
            assert len(files) == 1
            content = files[0].read_text()
            # Body filled with add_field for each new field; snapshot recorded.
            assert 'add_field(state, "messages"' in content
            assert 'add_field(state, "count"' in content
            assert 'add_field(state, "context"' in content
            assert "fields = {" in content

            # The generated revision loads and its snapshot matches the schema.
            registry = MigrationRegistry.from_path(tmp_path / "migrations")
            head = registry.get(registry.head())
            assert head.fields == {"messages": "list", "count": "int", "context": "dict"}
    finally:
        sys.path.remove(str(tmp_path))


def test_autogenerate_requires_schema(tmp_path):
    with chdir(tmp_path):
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["revision", "-m", "x", "--autogenerate"])
    assert result.exit_code == 1
    assert "--schema" in result.output


def test_autogenerate_reports_unloadable_schema(tmp_path):
    with chdir(tmp_path):
        runner.invoke(app, ["init"])
        result = runner.invoke(
            app,
            ["revision", "-m", "x", "--autogenerate", "--schema", "no.such.module:State"],
        )
    assert result.exit_code == 1
    assert "Could not load schema" in result.output


def test_autogenerate_reports_no_changes_against_head_snapshot(tmp_path):
    _write_schema_module(tmp_path, "myschema", SCHEMA_V1)
    sys.path.insert(0, str(tmp_path))
    try:
        with chdir(tmp_path):
            runner.invoke(app, ["init"])
            importlib.invalidate_caches()
            # First autogenerate records the snapshot.
            runner.invoke(
                app, ["revision", "-m", "first", "--autogenerate", "--schema", "myschema:State"]
            )
            # Re-running against the unchanged schema reports no changes.
            result = runner.invoke(
                app, ["revision", "-m", "again", "--autogenerate", "--schema", "myschema:State"]
            )
            assert result.exit_code == 0, result.output
            assert "No schema changes detected" in result.output
    finally:
        sys.path.remove(str(tmp_path))

"""Docker-free unit coverage for adapter pure logic.

The Postgres/Redis adapters are exercised end-to-end only behind
``@pytest.mark.integration`` (which needs Docker). These tests pin the parts
that are *pure logic* — keyset SQL/param construction, namespace round-trip,
untagged aggregation, and Redis metadata parsing — using fake DB clients, so a
regression in the query shape is caught without a database.
"""

from __future__ import annotations

import json

import pytest

from langmigrate.adapters import postgres as pg
from langmigrate.adapters.postgres import PostgresAdapter, PostgresStoreAdapter
from langmigrate.adapters.redis import RedisAdapter, _first

# -- fake psycopg connection --------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        # Record normalized SQL (collapsed whitespace) + params for assertions,
        # and make the next queued result set current.
        self._conn.executed.append((" ".join(sql.split()), tuple(params)))
        self._conn._current = self._conn._results.pop(0) if self._conn._results else []

    def fetchall(self) -> list:
        return list(self._conn._current)

    def fetchone(self):
        return self._conn._current[0] if self._conn._current else None

    @property
    def rowcount(self) -> int:
        return self._conn.rowcount


class _FakeConn:
    def __init__(self, results: list | None = None, rowcount: int = 0) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self._results: list = list(results or [])
        self._current: list = []
        self.rowcount = rowcount
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def _rows(*threads: str) -> list[dict]:
    return [{"thread_id": t, "checkpoint_ns": "", "checkpoint_id": f"c-{t}"} for t in threads]


# -- Postgres checkpoint adapter ---------------------------------------------


def test_pg_iter_stale_configs_keyset_pagination(monkeypatch):
    monkeypatch.setattr(pg, "_PAGE_SIZE", 2)
    conn = _FakeConn(results=[_rows("t1", "t2"), _rows("t3")])
    adapter = PostgresAdapter(conn, saver=None)  # type: ignore[arg-type]

    out = [c["configurable"]["thread_id"] for c in adapter.iter_stale_configs("v2")]
    assert out == ["t1", "t2", "t3"]  # all rows enumerated, none skipped

    assert len(conn.executed) == 2
    sql1, params1 = conn.executed[0]
    assert "IS DISTINCT FROM %s" in sql1
    assert "> (%s, %s, %s)" not in sql1  # first page has no keyset
    assert params1 == ("v2", 2)

    sql2, params2 = conn.executed[1]
    assert "IS DISTINCT FROM %s" in sql2
    assert "(thread_id, checkpoint_ns, checkpoint_id) > (%s, %s, %s)" in sql2
    # keyset carries the last row of page 1: t2 / "" / c-t2
    assert params2 == ("v2", "t2", "", "c-t2", 2)


def test_pg_iter_all_configs_has_no_stale_filter(monkeypatch):
    monkeypatch.setattr(pg, "_PAGE_SIZE", 2)
    conn = _FakeConn(results=[_rows("t1", "t2"), _rows("t3")])
    adapter = PostgresAdapter(conn, saver=None)  # type: ignore[arg-type]

    out = [c["configurable"]["thread_id"] for c in adapter.iter_all_configs()]
    assert out == ["t1", "t2", "t3"]

    sql1, params1 = conn.executed[0]
    assert "WHERE" not in sql1  # first page of iter_all has no predicate
    assert params1 == (2,)

    sql2, params2 = conn.executed[1]
    assert "WHERE (thread_id, checkpoint_ns, checkpoint_id) > (%s, %s, %s)" in sql2
    assert "IS DISTINCT FROM" not in sql2  # iter_all never filters on revision
    assert params2 == ("t2", "", "c-t2", 2)


def test_pg_iter_stops_when_page_not_full(monkeypatch):
    monkeypatch.setattr(pg, "_PAGE_SIZE", 2)
    conn = _FakeConn(results=[_rows("t1")])  # 1 row < page size -> stop, no 2nd query
    adapter = PostgresAdapter(conn, saver=None)  # type: ignore[arg-type]

    out = list(adapter.iter_stale_configs("v2"))
    assert len(out) == 1
    assert len(conn.executed) == 1


def test_pg_iter_full_page_then_empty(monkeypatch):
    # Exactly PAGE_SIZE rows must trigger a follow-up query (can't tell it was the
    # last page); an empty follow-up then terminates cleanly.
    monkeypatch.setattr(pg, "_PAGE_SIZE", 2)
    conn = _FakeConn(results=[_rows("t1", "t2"), []])
    adapter = PostgresAdapter(conn, saver=None)  # type: ignore[arg-type]

    out = list(adapter.iter_stale_configs("v2"))
    assert len(out) == 2
    assert len(conn.executed) == 2


def test_pg_count_stale():
    conn = _FakeConn(results=[[{"c": 3}]])
    adapter = PostgresAdapter(conn, saver=None)  # type: ignore[arg-type]
    assert adapter.count_stale("v2") == 3
    sql, params = conn.executed[0]
    assert "count(*)" in sql and "IS DISTINCT FROM %s" in sql
    assert params == ("v2",)


def test_pg_revision_counts_maps_null_to_untagged():
    conn = _FakeConn(results=[[{"rev": "v1", "c": 2}, {"rev": None, "c": 1}]])
    adapter = PostgresAdapter(conn, saver=None)  # type: ignore[arg-type]
    assert adapter.revision_counts() == {"v1": 2, "<untagged>": 1}


def test_pg_stamp_all_returns_rowcount_and_guards_null():
    conn = _FakeConn(results=[[]], rowcount=5)
    adapter = PostgresAdapter(conn, saver=None)  # type: ignore[arg-type]
    assert adapter.stamp_all("v2") == 5
    sql, params = conn.executed[0]
    assert "jsonb_set" in sql and "COALESCE" in sql and "to_jsonb(%s::text)" in sql
    assert params == ("v2",)


def test_pg_close_closes_connection():
    conn = _FakeConn()
    PostgresAdapter(conn, saver=None).close()  # type: ignore[arg-type]
    assert conn.closed is True


# -- Postgres store adapter ---------------------------------------------------


def test_pg_store_iter_items_namespace_split_and_keyset(monkeypatch):
    monkeypatch.setattr(pg, "_PAGE_SIZE", 2)
    conn = _FakeConn(
        results=[
            [{"prefix": "a.b", "key": "k1"}, {"prefix": "a.b", "key": "k2"}],
            [{"prefix": "c", "key": "k3"}],
        ]
    )
    adapter = PostgresStoreAdapter(conn, store=None)  # type: ignore[arg-type]

    out = list(adapter.iter_stale_items("v2"))
    assert out == [(("a", "b"), "k1"), (("a", "b"), "k2"), (("c",), "k3")]

    sql2, params2 = conn.executed[1]
    assert "(prefix, key) > (%s, %s)" in sql2
    assert params2 == ("v2", "a.b", "k2", 2)


def test_pg_store_revision_counts_maps_null_to_untagged():
    conn = _FakeConn(results=[[{"rev": "s1", "c": 4}, {"rev": None, "c": 2}]])
    adapter = PostgresStoreAdapter(conn, store=None)  # type: ignore[arg-type]
    assert adapter.revision_counts() == {"s1": 4, "<untagged>": 2}


def test_pg_store_stamp_all():
    conn = _FakeConn(results=[[]], rowcount=7)
    adapter = PostgresStoreAdapter(conn, store=None)  # type: ignore[arg-type]
    assert adapter.stamp_all("s1") == 7
    sql, _ = conn.executed[0]
    assert "UPDATE store SET value" in sql and "jsonb_set" in sql


# -- Redis adapter: pure helpers ---------------------------------------------


def test_redis_revision_from_metadata_variants():
    f = RedisAdapter._revision_from_metadata
    assert f(json.dumps({"langmigrate_rev": "v3", "source": "loop"})) == "v3"  # JSON string
    assert f({"langmigrate_rev": "v3"}) == "v3"  # already a dict
    assert f(json.dumps({"source": "loop"})) is None  # tagless
    assert f("{not valid json") is None  # malformed string -> tolerated
    assert f(None) is None
    assert f(123) is None  # non-dict, non-str


def test_redis_first_helper():
    fields = {"$.thread_id": ["t1"], "$.empty": [], "$.checkpoint_ns": [""]}
    assert _first(fields, "$.thread_id") == "t1"
    assert _first(fields, "$.empty") is None
    assert _first(fields, "$.missing") is None
    assert _first(fields, "$.checkpoint_ns") == ""  # empty-string value preserved


# -- Redis adapter: enumeration with a fake client ---------------------------


class _FakeRedisJson:
    def __init__(self, docs: dict) -> None:
        self._docs = docs

    def get(self, key: str, *paths: str) -> dict:
        # RedisJSON multi-path get returns {path: [value]}.
        doc = self._docs[key]
        return {p: [doc[p]] for p in paths}


class _FakeRedisClient:
    def __init__(self, docs: dict) -> None:
        self._docs = docs

    def scan_iter(self, match=None, count=None):
        return iter(list(self._docs.keys()))

    def json(self) -> _FakeRedisJson:
        return _FakeRedisJson(self._docs)


class _FakeRedisSaver:
    def __init__(self, docs: dict) -> None:
        self._redis = _FakeRedisClient(docs)


def _redis_doc(thread: str, rev: str | None) -> dict:
    meta = {"source": "loop"}
    if rev is not None:
        meta["langmigrate_rev"] = rev
    return {
        "$.thread_id": thread,
        "$.checkpoint_ns": "",
        "$.checkpoint_id": f"c-{thread}",
        "$.metadata": json.dumps(meta),
    }


def test_redis_iter_stale_and_revision_counts():
    pytest.importorskip("langgraph.checkpoint.redis.util")
    docs = {
        "checkpoint:t1::c-t1": _redis_doc("t1", "v1"),
        "checkpoint:t2::c-t2": _redis_doc("t2", "v1"),
        "checkpoint:t3::c-t3": _redis_doc("t3", None),  # untagged
    }
    adapter = RedisAdapter(_FakeRedisSaver(docs))  # type: ignore[arg-type]

    # Stale vs head "v2": all three differ.
    stale = sorted(c["configurable"]["thread_id"] for c in adapter.iter_stale_configs("v2"))
    assert stale == ["t1", "t2", "t3"]
    assert adapter.count_stale("v2") == 3

    # Stale vs head "v1": only the untagged one differs.
    stale_v1 = sorted(c["configurable"]["thread_id"] for c in adapter.iter_stale_configs("v1"))
    assert stale_v1 == ["t3"]
    assert adapter.count_stale("v1") == 1

    # iter_all ignores the tag entirely.
    assert len(list(adapter.iter_all_configs())) == 3

    assert adapter.revision_counts() == {"v1": 2, "<untagged>": 1}


def test_redis_iter_docs_skips_empty_json_get():
    pytest.importorskip("langgraph.checkpoint.redis.util")

    class _EmptyJson(_FakeRedisJson):
        def get(self, key, *paths):
            return {}  # RedisJSON returns falsy when the doc/path is gone

    class _Client(_FakeRedisClient):
        def json(self):
            return _EmptyJson(self._docs)

    class _Saver:
        def __init__(self, docs):
            self._redis = _Client(docs)

    adapter = RedisAdapter(_Saver({"checkpoint:t1::c-t1": _redis_doc("t1", "v1")}))  # type: ignore[arg-type]
    # A doc that returns no fields is skipped, not crashed on.
    assert list(adapter.iter_all_configs()) == []

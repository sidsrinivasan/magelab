"""Unit tests for the Database class.

Tests framework-level tables (run_meta, run_events, run_transcripts) and
connection primitives. Store-specific persistence (agents, tasks, wires)
is tested in the respective store test files.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from magelab.events import EventOutcome
from magelab.state.database import SCHEMA_VERSION, Database


@pytest.fixture
def db(tmp_path):
    """Create a fresh Database backed by a temp file (framework tables only)."""
    db_path = tmp_path / "test.db"
    d = Database(str(db_path))
    yield d
    try:
        d.close()
    except Exception:
        pass


# ── Schema creation ──────────────────────────────────────────────────────────


def test_tables_created(db):
    """Database.__init__ creates only framework-level tables."""
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    table_names = {row["name"] for row in rows}
    expected = {"run_meta", "run_events", "run_transcripts"}
    assert expected.issubset(table_names)
    # Store-specific tables should NOT be created by Database alone
    assert "agent_instances" not in table_names
    assert "task_items" not in table_names
    assert "wire_meta" not in table_names


def test_register_schema(db):
    """register_schema creates tables idempotently."""
    ddl = "CREATE TABLE IF NOT EXISTS foo (id TEXT PRIMARY KEY, val TEXT);"
    db.register_schema(ddl)
    # Table exists
    db.execute("INSERT INTO foo (id, val) VALUES ('a', 'b')")
    row = db.fetchone("SELECT val FROM foo WHERE id = 'a'")
    assert row["val"] == "b"
    # Calling again is idempotent
    db.register_schema(ddl)


def test_register_schema_rejects_unsafe_ddl(db):
    """register_schema rejects DDL without IF NOT EXISTS."""
    with pytest.raises(ValueError, match="IF NOT EXISTS"):
        db.register_schema("CREATE TABLE foo (id TEXT PRIMARY KEY);")


def test_register_schema_rejects_inside_transaction(db):
    """register_schema raises if called inside a transaction."""
    with db.transaction():
        with pytest.raises(RuntimeError, match="must not be called inside a transaction"):
            db.register_schema("CREATE TABLE IF NOT EXISTS foo (id TEXT PRIMARY KEY);")


# ── Connection primitives ───────────────────────────────────────────────────


def test_execute_and_fetch(db):
    db.register_schema("CREATE TABLE IF NOT EXISTS test_t (id TEXT PRIMARY KEY, val TEXT);")
    db.execute("INSERT INTO test_t (id, val) VALUES (?, ?)", ("k1", "v1"))
    db.commit()
    assert db.fetchone("SELECT val FROM test_t WHERE id = ?", ("k1",))["val"] == "v1"
    assert len(db.fetchall("SELECT * FROM test_t")) == 1


# ── run_meta ─────────────────────────────────────────────────────────────────


def test_init_and_load_run_meta(db):
    db.init_run_meta(org_name="test", org_config='{"a":1}')
    meta = db.load_run_meta()
    assert meta["org_name"] == "test"
    assert meta["org_config"] == '{"a":1}'
    assert meta["schema_version"] == SCHEMA_VERSION


def test_multiple_run_segments(db):
    db.init_run_meta(org_name="first", org_config="{}")
    db.init_run_meta(org_name="second", org_config="{}")
    segments = db.load_all_run_segments()
    assert len(segments) == 2
    assert segments[0]["org_name"] == "first"
    assert segments[1]["org_name"] == "second"
    latest = db.load_run_meta()
    assert latest["org_name"] == "second"


def test_get_schema_version(db):
    db.init_run_meta(org_name="test", org_config="{}")
    assert db.get_schema_version() == SCHEMA_VERSION


def test_get_schema_version_empty_db(db):
    assert db.get_schema_version() is None


def test_finalize_run(db):
    db.init_run_meta(org_name="test", org_config="{}")
    now = datetime.now(timezone.utc).isoformat()
    db.finalize_run(
        end_time=now,
        duration_seconds=42.5,
        total_cost_usd=12.5,
        timed_out=False,
        outcome="success",
        sync_rounds=3,
        tasks_succeeded=2,
        tasks_failed=1,
        tasks_open=0,
    )
    meta = db.load_run_meta()
    assert meta["end_time"] == now
    assert meta["duration_seconds"] == 42.5
    assert meta["total_cost_usd"] == 12.5
    assert meta["timed_out"] == 0
    assert meta["outcome"] == "success"
    assert meta["sync_rounds"] == 3
    assert meta["tasks_succeeded"] == 2
    assert meta["tasks_failed"] == 1


# ── compute_run_summary ─────────────────────────────────────────────────────


def test_compute_run_summary_cost_and_errors(db):
    """compute_run_summary aggregates cost and classifies errors from run_events."""
    db.init_run_meta(org_name="test", org_config="{}")
    now = datetime.now(timezone.utc).isoformat()

    # Insert events with various error types and costs
    db.insert_event(
        event_id="e1",
        event_type="TaskAssignedEvent",
        target_agent_id="a",
        source_agent_id=None,
        task_id="t1",
        wire_id=None,
        timestamp=now,
    )
    db.update_event_finished(
        "e1", num_turns=3, cost_usd=1.50, duration_ms=1000, timed_out=False, error=None, finished_at=now
    )

    db.insert_event(
        event_id="e2",
        event_type="TaskAssignedEvent",
        target_agent_id="a",
        source_agent_id=None,
        task_id="t2",
        wire_id=None,
        timestamp=now,
    )
    db.update_event_finished(
        "e2",
        num_turns=1,
        cost_usd=0.50,
        duration_ms=500,
        timed_out=False,
        error="Rate limited (429): rate limit exceeded",
        finished_at=now,
    )

    db.insert_event(
        event_id="e3",
        event_type="TaskAssignedEvent",
        target_agent_id="b",
        source_agent_id=None,
        task_id="t3",
        wire_id=None,
        timestamp=now,
    )
    db.update_event_finished(
        "e3",
        num_turns=1,
        cost_usd=0.25,
        duration_ms=200,
        timed_out=False,
        error="API overloaded (529): api overloaded",
        finished_at=now,
    )

    db.insert_event(
        event_id="e4",
        event_type="TaskAssignedEvent",
        target_agent_id="b",
        source_agent_id=None,
        task_id="t4",
        wire_id=None,
        timestamp=now,
    )
    db.update_event_finished(
        "e4",
        num_turns=1,
        cost_usd=0.10,
        duration_ms=100,
        timed_out=False,
        error="API error (500): connection reset",
        finished_at=now,
    )

    summary = db.compute_run_summary()
    assert summary["total_cost_usd"] == pytest.approx(2.35)
    assert summary["rate_limited_429"] == 1
    assert summary["api_overloaded_529"] == 1
    assert summary["api_error_other"] == 1
    assert summary["start_time"] is not None


# ── run_events ──────────────────────────────────────────────────────────────


def test_event_lifecycle(db):
    now = datetime.now(timezone.utc).isoformat()
    eid = "evt-001"
    db.insert_event(
        event_id=eid,
        event_type="TaskAssignedEvent",
        target_agent_id="alice",
        source_agent_id="pm",
        task_id="t1",
        wire_id=None,
        timestamp=now,
    )

    undelivered = db.load_undelivered_events()
    assert len(undelivered) == 1
    assert undelivered[0]["outcome"] is None

    db.update_event_outcome(eid, EventOutcome.DELIVERED)
    undelivered = db.load_undelivered_events()
    assert len(undelivered) == 0

    finish_time = datetime.now(timezone.utc).isoformat()
    db.update_event_finished(
        eid,
        num_turns=5,
        cost_usd=1.23,
        duration_ms=4500,
        timed_out=False,
        error=None,
        finished_at=finish_time,
    )
    row = db.fetchone("SELECT * FROM run_events WHERE event_id = ?", (eid,))
    assert row["outcome"] == "completed"
    assert row["num_turns"] == 5
    assert row["cost_usd"] == 1.23
    assert row["duration_ms"] == 4500
    assert row["error"] is None
    assert row["finished_at"] == finish_time


def test_update_events_by_outcome(db):
    now = datetime.now(timezone.utc).isoformat()
    db.insert_event(
        event_id="evt-a",
        event_type="TaskAssignedEvent",
        target_agent_id="alice",
        source_agent_id="pm",
        task_id="t1",
        wire_id=None,
        timestamp=now,
    )
    db.insert_event(
        event_id="evt-b",
        event_type="TaskAssignedEvent",
        target_agent_id="bob",
        source_agent_id="pm",
        task_id="t2",
        wire_id=None,
        timestamp=now,
    )
    db.insert_event(
        event_id="evt-c",
        event_type="TaskAssignedEvent",
        target_agent_id="carol",
        source_agent_id="pm",
        task_id="t3",
        wire_id=None,
        timestamp=now,
    )

    # Deliver one event
    db.update_event_outcome("evt-a", EventOutcome.DELIVERED)

    # Batch-update all undelivered (NULL outcome) to dropped
    count = db.update_events_by_outcome(None, EventOutcome.DROPPED_ON_RESTART)
    assert count == 2

    assert len(db.load_undelivered_events()) == 0

    rows = db.fetchall("SELECT * FROM run_events ORDER BY timestamp, event_id")
    outcomes = {r["event_id"]: r["outcome"] for r in rows}
    assert outcomes["evt-a"] == "delivered"
    assert outcomes["evt-b"] == "dropped_on_restart"
    assert outcomes["evt-c"] == "dropped_on_restart"


def test_update_event_outcome(db):
    now = datetime.now(timezone.utc).isoformat()
    eid = "evt-stale"
    db.insert_event(
        event_id=eid,
        event_type="TaskAssignedEvent",
        target_agent_id="alice",
        source_agent_id="pm",
        task_id="t1",
        wire_id=None,
        timestamp=now,
    )
    db.update_event_outcome(eid, EventOutcome.STALE_AT_DELIVERY)
    row = db.fetchone("SELECT outcome FROM run_events WHERE event_id = ?", (eid,))
    assert row["outcome"] == "stale_at_delivery"


# ── run_transcripts ─────────────────────────────────────────────────────────


def test_insert_transcript_entry(db):
    now = datetime.now(timezone.utc).isoformat()
    db.insert_transcript_entry(
        agent_id="alice",
        entry_type="assistant_response",
        content="Hello world",
        timestamp=now,
        turn_number=1,
    )
    rows = db.fetchall("SELECT * FROM run_transcripts")
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "alice"
    assert rows[0]["entry_type"] == "assistant_response"
    assert rows[0]["content"] == "Hello world"
    assert rows[0]["timestamp"] == now
    assert rows[0]["turn_number"] == 1


# ── transaction ──────────────────────────────────────────────────────────────


def test_transaction_commits(db):
    db.register_schema("CREATE TABLE IF NOT EXISTS tx_test (id TEXT PRIMARY KEY);")
    with db.transaction():
        db.execute("INSERT INTO tx_test (id) VALUES ('a')")
        db.execute("INSERT INTO tx_test (id) VALUES ('b')")
    assert len(db.fetchall("SELECT * FROM tx_test")) == 2


def test_transaction_rollback(db):
    db.register_schema("CREATE TABLE IF NOT EXISTS tx_test (id TEXT PRIMARY KEY);")
    try:
        with db.transaction():
            db.execute("INSERT INTO tx_test (id) VALUES ('a')")
            raise ValueError("force rollback")
    except ValueError:
        pass
    assert len(db.fetchall("SELECT * FROM tx_test")) == 0


def test_transaction_not_reentrant(db):
    with db.transaction():
        with pytest.raises(RuntimeError):
            with db.transaction():
                pass


# ── close ────────────────────────────────────────────────────────────────────


def test_close(db):
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        db.fetchall("SELECT * FROM run_meta")

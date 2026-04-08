"""Tests for the hydration/resume logic."""

import json
import logging
from datetime import datetime, timezone

import pytest

from magelab.org_config import OrgConfig, OrgSettings, WireNotifications
from magelab.registry_config import AgentConfig, NetworkConfig, RoleConfig
from magelab.events import (
    MCPEvent,
    ResumeEvent,
    ReviewFinishedEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    TaskFinishedEvent,
    WireMessageEvent,
)
from magelab.state.database import Database
from magelab.state.database_hydration import (
    reconstruct_event,
    reconstruct_org_config_from_db,
    resume_continue,
    resume_fresh,
)
from magelab.state.registry import AGENTS_DDL, NETWORK_DDL, ROLES_DDL, Registry
from magelab.state.registry_schemas import AgentState
from magelab.state.task_schemas import ReviewStatus, Task, TaskStatus
from magelab.state.task_store import TASKS_DDL, TaskStore
from magelab.state.wire_store import WIRES_DDL, WireStore

# SQL for direct agent row insertion in tests (bypasses Registry).
# Includes structural columns so from_db can fully reconstruct agents.
_INSERT_AGENT_SQL = """
    INSERT INTO agent_instances
        (agent_id, role, model, role_prompt, tools, max_turns,
         state, current_task_id, session_id, created_at, last_active_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(agent_id) DO UPDATE SET
        state = excluded.state, current_task_id = excluded.current_task_id,
        session_id = excluded.session_id, last_active_at = excluded.last_active_at
"""


def _seed_role(db, name="worker", role_prompt="Work.", tools=None, model="test", max_turns=10):
    """Seed a role row directly via SQL."""
    tools = (
        tools
        or '["tasks_submit_for_review","tasks_mark_finished","tasks_assign","tasks_create","tasks_list","tasks_get"]'
    )
    db.execute(
        "INSERT OR REPLACE INTO agent_roles (name, role_prompt, tools, model, max_turns) VALUES (?, ?, ?, ?, ?)",
        (name, role_prompt, tools, model, max_turns),
    )
    db.commit()


def _upsert_agent(
    db,
    agent_id,
    role="worker",
    model="test",
    state="idle",
    current_task_id=None,
    session_id=None,
    last_active_at=None,
    role_prompt="Work.",
    tools="[]",
    max_turns=10,
):
    """Helper to insert/update an agent row directly via SQL."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        _INSERT_AGENT_SQL,
        (agent_id, role, model, role_prompt, tools, max_turns, state, current_task_id, session_id, now, last_active_at),
    )
    db.commit()


_MINIMAL_ORG_CONFIG_JSON = json.dumps(
    {
        "org_name": "test",
        "org_description": "",
        "org_prompt": "",
        "org_permission_mode": "acceptEdits",
        "org_timeout_seconds": 3600.0,
        "agent_timeout_seconds": 900.0,
        "wire_notifications": "all",
        "wire_max_unread_per_prompt": 10,
        "mcp_modules": {},
        "sync": False,
    }
)


def _seed_db(db):
    """Seed DB with roles and agents for a typical two-agent test."""
    db.register_schema(ROLES_DDL)
    db.register_schema(AGENTS_DDL)
    db.register_schema(NETWORK_DDL)
    _seed_role(db)
    _upsert_agent(db, "alice")
    _upsert_agent(db, "bob")


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    d.register_schema(ROLES_DDL)
    d.register_schema(AGENTS_DDL)
    d.register_schema(NETWORK_DDL)
    d.register_schema(TASKS_DDL)
    d.register_schema(WIRES_DDL)
    yield d
    try:
        d.close()
    except Exception:
        pass


def _load_stores(db, logger=None):
    """Load all stores from DB (test helper)."""
    _logger = logger or logging.getLogger("test")
    registry = Registry(framework_logger=_logger, db=db)
    registry.load_from_db()
    task_store = TaskStore(framework_logger=_logger, db=db)
    task_store.load_from_db()
    wire_store = WireStore(framework_logger=_logger, db=db)
    wire_store.load_from_db()
    return registry, task_store, wire_store


# ── reconstruct_event ────────────────────────────────────────────────────────


def test_reconstruct_task_assigned():
    row = {
        "event_type": "TaskAssignedEvent",
        "target_agent_id": "alice",
        "source_agent_id": "pm",
        "task_id": "t1",
        "wire_id": None,
        "payload": None,
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": 1,
    }
    event = reconstruct_event(row)
    assert isinstance(event, TaskAssignedEvent)
    assert event.target_id == "alice"
    assert event.task_id == "t1"
    assert event.source_id == "pm"


def test_reconstruct_review_requested():
    row = {
        "event_type": "ReviewRequestedEvent",
        "target_agent_id": "alice",
        "source_agent_id": "pm",
        "task_id": "t1",
        "wire_id": None,
        "payload": '{"request_message": "Please review"}',
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": 2,
    }
    event = reconstruct_event(row)
    assert isinstance(event, ReviewRequestedEvent)
    assert event.request_message == "Please review"


def test_reconstruct_wire_message():
    row = {
        "event_type": "WireMessageEvent",
        "target_agent_id": "alice",
        "source_agent_id": "bob",
        "task_id": None,
        "wire_id": "w1",
        "payload": json.dumps({"message_cursor": 5}),
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": 3,
    }
    event = reconstruct_event(row)
    assert isinstance(event, WireMessageEvent)
    assert event.message_cursor == 5


def test_reconstruct_resume_event():
    row = {
        "event_type": "ResumeEvent",
        "target_agent_id": "alice",
        "source_agent_id": None,
        "task_id": "t1",
        "wire_id": None,
        "payload": json.dumps({"was_reviewing": True}),
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": 4,
    }
    event = reconstruct_event(row)
    assert isinstance(event, ResumeEvent)
    assert event.was_reviewing is True


def test_reconstruct_task_finished():
    row = {
        "event_type": "TaskFinishedEvent",
        "target_agent_id": "pm",
        "source_agent_id": None,
        "task_id": "t1",
        "wire_id": None,
        "payload": json.dumps({"outcome": "succeeded", "details": "All done"}),
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": 5,
    }
    event = reconstruct_event(row)
    assert isinstance(event, TaskFinishedEvent)
    assert event.outcome == TaskStatus.SUCCEEDED
    assert event.details == "All done"


def test_reconstruct_mcp_event():
    row = {
        "event_type": "MCPEvent",
        "target_agent_id": "trader-0",
        "source_agent_id": None,
        "task_id": None,
        "wire_id": None,
        "payload": json.dumps({"server_name": "market", "payload": "Price alert: ACME crossed $50"}),
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": "mcp1",
    }
    event = reconstruct_event(row)
    assert isinstance(event, MCPEvent)
    assert event.server_name == "market"
    assert event.payload == "Price alert: ACME crossed $50"
    assert event.target_id == "trader-0"


def test_reconstruct_review_finished():
    """ReviewFinishedEvent has the most complex payload (nested review_records).
    Verify outcome is restored as TaskStatus and review_records as ReviewRecord objects."""
    row = {
        "event_type": "ReviewFinishedEvent",
        "target_agent_id": "coder-0",
        "source_agent_id": None,
        "task_id": "t1",
        "wire_id": None,
        "payload": json.dumps(
            {
                "outcome": "approved",
                "review_records": [
                    {
                        "reviewer_id": "reviewer-0",
                        "requester_id": "coder-0",
                        "request_message": "Please review",
                        "round_number": 1,
                        "review": {
                            "reviewer_id": "reviewer-0",
                            "decision": "approved",
                            "comment": "Looks good",
                        },
                    }
                ],
            }
        ),
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": 6,
    }
    event = reconstruct_event(row)
    assert isinstance(event, ReviewFinishedEvent)
    assert event.outcome == TaskStatus.APPROVED
    assert event.task_id == "t1"
    assert len(event.review_records) == 1
    record = event.review_records[0]
    assert record.reviewer_id == "reviewer-0"
    assert record.review is not None
    assert record.review.comment == "Looks good"
    assert record.review.decision == ReviewStatus.APPROVED


def test_reconstruct_unknown_returns_none():
    row = {
        "event_type": "SomeUnknownEvent",
        "target_agent_id": "alice",
        "source_agent_id": "pm",
        "task_id": "t1",
        "wire_id": None,
        "payload": None,
        "timestamp": "2024-01-01T00:00:00+00:00",
        "event_id": 7,
    }
    assert reconstruct_event(row) is None


# ── Load stores — empty DB ─────────────────────────────────────────────────


def test_empty_db_loads_empty_stores(db):
    _seed_db(db)
    db.init_run_meta(org_name="test", org_config=_MINIMAL_ORG_CONFIG_JSON)
    registry, task_store, wire_store = _load_stores(db)
    assert len(task_store._tasks) == 0
    assert len(wire_store._wires) == 0
    assert len(registry._agents) == 2  # alice and bob from _seed_db


# ── resume_fresh ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_fresh_fails_open_tasks(db):
    logger = logging.getLogger("test")
    _seed_db(db)
    db.init_run_meta(org_name="test", org_config=_MINIMAL_ORG_CONFIG_JSON)

    t1 = Task(id="t1", title="T1", description="D1", status=TaskStatus.IN_PROGRESS, assignment_history=["alice"])
    t2 = Task(id="t2", title="T2", description="D2", status=TaskStatus.SUCCEEDED, assignment_history=["bob"])
    _ts = TaskStore(framework_logger=logger, db=db)
    _ts._persist_task(t1)
    _ts._persist_task(t2)

    now = datetime.now(timezone.utc).isoformat()
    db.insert_event(
        event_id="evt-fresh-1",
        event_type="TaskAssignedEvent",
        target_agent_id="alice",
        source_agent_id="pm",
        task_id="t1",
        wire_id=None,
        timestamp=now,
    )

    _upsert_agent(db, agent_id="alice", state="working")
    _upsert_agent(db, agent_id="bob", state="idle")

    registry, task_store, wire_store = _load_stores(db, logger)
    await resume_fresh(db, task_store, registry, logger)

    assert task_store._tasks["t1"].status == TaskStatus.FAILED
    assert task_store._tasks["t2"].status == TaskStatus.SUCCEEDED
    assert len(db.load_undelivered_events()) == 0
    assert registry.get_agent_snapshot("alice").state == AgentState.IDLE
    assert registry.get_agent_snapshot("bob").state == AgentState.IDLE


# ── resume_continue ────────────────────────────────────────────────────────


def test_resume_continue_requeues_events(db):
    logger = logging.getLogger("test")
    _seed_db(db)
    db.init_run_meta(org_name="test", org_config=_MINIMAL_ORG_CONFIG_JSON)

    now = datetime.now(timezone.utc).isoformat()
    db.insert_event(
        event_id="evt-cont-1",
        event_type="TaskAssignedEvent",
        target_agent_id="alice",
        source_agent_id="pm",
        task_id="t1",
        wire_id=None,
        timestamp=now,
    )

    registry, task_store, wire_store = _load_stores(db, logger)
    resume_continue(db, registry, logger)

    assert registry._agents["alice"].queue.qsize() > 0


def test_resume_continue_creates_resume_events_for_working_agents(db):
    logger = logging.getLogger("test")
    _seed_db(db)
    db.init_run_meta(org_name="test", org_config=_MINIMAL_ORG_CONFIG_JSON)

    _upsert_agent(db, agent_id="alice", state="working", current_task_id="t1")
    _upsert_agent(db, agent_id="bob", state="idle")

    registry, task_store, wire_store = _load_stores(db, logger)
    resume_continue(db, registry, logger)

    assert registry._agents["alice"].queue.qsize() > 0
    event = registry._agents["alice"].queue.get_nowait()
    assert isinstance(event, ResumeEvent)
    assert event.task_id == "t1"
    assert event.was_reviewing is False
    assert registry._agents["bob"].queue.qsize() == 0


def test_resume_continue_creates_resume_events_for_reviewing_agents(db):
    logger = logging.getLogger("test")
    _seed_db(db)
    db.init_run_meta(org_name="test", org_config=_MINIMAL_ORG_CONFIG_JSON)

    _upsert_agent(db, agent_id="alice", state="reviewing", current_task_id="t1")
    _upsert_agent(db, agent_id="bob", state="idle")

    registry, task_store, wire_store = _load_stores(db, logger)
    resume_continue(db, registry, logger)

    assert registry._agents["alice"].queue.qsize() > 0
    event = registry._agents["alice"].queue.get_nowait()
    assert isinstance(event, ResumeEvent)
    assert event.task_id == "t1"
    assert event.was_reviewing is True


# ── Session ID restoration ──────────────────────────────────────────────────


def test_session_ids_available_from_registry(db):
    """Session IDs are in the DB and accessible via registry.get_session_ids()."""
    _seed_db(db)
    db.init_run_meta(org_name="test", org_config=_MINIMAL_ORG_CONFIG_JSON)
    _upsert_agent(db, agent_id="alice", state="idle", session_id="sess-abc")

    registry, task_store, wire_store = _load_stores(db)

    # Session IDs are loaded by the orchestrator after runner construction,
    # not by load_stores_from_db. Verify they're accessible from the registry.
    session_ids = registry.get_session_ids()
    assert session_ids["alice"] == "sess-abc"


# ── Wire loading ───────────────────────────────────────────────────────────


def test_wires_loaded(db):
    _seed_db(db)
    db.init_run_meta(org_name="test", org_config=_MINIMAL_ORG_CONFIG_JSON)

    now = datetime.now(timezone.utc).isoformat()
    _ws = WireStore(framework_logger=logging.getLogger("test"), db=db)
    _ws._db_insert_wire("w1", ["alice", "bob"], None, now)
    _ws._db_insert_message("w1", "alice", "hello", now)
    _ws._db_upsert_cursor("w1", "alice", 1)
    _ws._db_upsert_cursor("w1", "bob", 0)

    registry, task_store, wire_store = _load_stores(db)

    assert "w1" in wire_store._wires
    wire = wire_store._wires["w1"]
    assert len(wire.messages) == 1
    assert wire.participants == frozenset({"alice", "bob"})
    assert wire.read_cursors["alice"] == 1
    assert wire.read_cursors["bob"] == 0


# ── reconstruct_org_config_from_db ─────────────────────────────────────────


def _seed_and_reconstruct(tmp_path, original: OrgConfig) -> OrgConfig:
    """Helper: seed DB from OrgConfig, reconstruct, return the reconstructed config."""
    db = Database(str(tmp_path / "test.db"))
    try:
        registry = Registry(framework_logger=logging.getLogger("test"), db=db)
        registry.register_config(original.roles, original.agents, original.network)
        org_config_json = json.dumps(original.to_dict(), default=str)
        db.init_run_meta(org_name=original.settings.org_name, org_config=org_config_json)
        return reconstruct_org_config_from_db(db)
    finally:
        db.close()


def _yaml_roundtrip(tmp_path, config: OrgConfig) -> OrgConfig:
    """Helper: serialize to dict and reload via from_dict.

    to_dict() produces flattened (DB-style) output; from_dict handles both
    flattened and nested formats.
    """
    return OrgConfig.from_dict(config.to_dict())


def test_reconstruct_roundtrip_full(tmp_path):
    """Full config: roles, agents with overrides, network, wire, settings."""
    original = OrgConfig(
        roles={
            "coder": RoleConfig(
                name="coder",
                role_prompt="Code stuff",
                tools=["worker", "communication"],
                model="test-model",
                max_turns=50,
            ),
            "reviewer": RoleConfig(
                name="reviewer", role_prompt="Review code", tools=[], model="test-model", max_turns=20
            ),
        },
        agents={
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "coder-1": AgentConfig(
                agent_id="coder-1", role="coder", model_override="fast-model", max_turns_override=10
            ),
            "rev-0": AgentConfig(agent_id="rev-0", role="reviewer"),
        },
        network=NetworkConfig(groups={"team": ["coder-0", "coder-1", "rev-0"]}),
        settings=OrgSettings(
            org_name="full_test",
            wire_notifications=WireNotifications.TOOL,
            org_prompt="Be helpful",
            org_timeout_seconds=1800.0,
            agent_timeout_seconds=300.0,
        ),
    )

    reconstructed = _seed_and_reconstruct(tmp_path, original)

    assert reconstructed.settings.org_name == original.settings.org_name
    assert reconstructed.settings.org_prompt == original.settings.org_prompt
    assert reconstructed.settings.org_timeout_seconds == original.settings.org_timeout_seconds
    assert reconstructed.settings.agent_timeout_seconds == original.settings.agent_timeout_seconds
    assert set(reconstructed.roles.keys()) == set(original.roles.keys())
    assert set(reconstructed.agents.keys()) == set(original.agents.keys())

    # Overrides computed correctly
    assert reconstructed.agents["coder-0"].model_override is None
    assert reconstructed.agents["coder-1"].model_override == "fast-model"
    assert reconstructed.agents["coder-1"].max_turns_override == 10

    # Network
    assert reconstructed.network is not None
    assert "team" in reconstructed.network.groups

    # Wire notifications
    assert reconstructed.settings.wire_notifications == WireNotifications.TOOL

    # YAML round-trip
    reloaded = _yaml_roundtrip(tmp_path, reconstructed)
    assert reloaded.settings.org_name == original.settings.org_name
    assert reloaded.agents["coder-1"].model_override == "fast-model"
    assert reloaded.network is not None


def test_reconstruct_no_overrides(tmp_path):
    """All agents use role defaults — no overrides in reconstructed config."""
    original = OrgConfig(
        roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
        settings=OrgSettings(org_name="no_overrides"),
        agents={
            "a": AgentConfig(agent_id="a", role="w"),
            "b": AgentConfig(agent_id="b", role="w"),
        },
    )

    reconstructed = _seed_and_reconstruct(tmp_path, original)
    assert reconstructed.agents["a"].model_override is None
    assert reconstructed.agents["a"].role_prompt_override is None
    assert reconstructed.agents["a"].tools_override is None
    assert reconstructed.agents["a"].max_turns_override is None

    reloaded = _yaml_roundtrip(tmp_path, reconstructed)
    assert set(reloaded.agents.keys()) == {"a", "b"}


def test_reconstruct_all_overrides(tmp_path):
    """Agent overrides every field — all should appear in reconstructed config."""
    original = OrgConfig(
        roles={"w": RoleConfig(name="w", role_prompt="Default", tools=["worker"], model="default-model", max_turns=10)},
        settings=OrgSettings(org_name="all_overrides"),
        agents={
            "a": AgentConfig(
                agent_id="a",
                role="w",
                model_override="custom-model",
                role_prompt_override="Custom prompt",
                tools_override=[],
                max_turns_override=99,
            ),
        },
    )

    reconstructed = _seed_and_reconstruct(tmp_path, original)
    a = reconstructed.agents["a"]
    assert a.model_override == "custom-model"
    assert a.role_prompt_override == "Custom prompt"
    assert a.tools_override == []
    assert a.max_turns_override == 99

    reloaded = _yaml_roundtrip(tmp_path, reconstructed)
    assert reloaded.agents["a"].model_override == "custom-model"


def test_reconstruct_no_network(tmp_path):
    """No network config — fully connected org."""
    original = OrgConfig(
        roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
        agents={"a": AgentConfig(agent_id="a", role="w")},
        settings=OrgSettings(org_name="no_network"),
    )

    reconstructed = _seed_and_reconstruct(tmp_path, original)
    assert reconstructed.network is None

    reloaded = _yaml_roundtrip(tmp_path, reconstructed)
    assert reloaded.network is None


def test_reconstruct_no_wire(tmp_path):
    """No wire config."""
    original = OrgConfig(
        roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
        agents={"a": AgentConfig(agent_id="a", role="w")},
        settings=OrgSettings(org_name="no_wire"),
    )

    reconstructed = _seed_and_reconstruct(tmp_path, original)
    assert reconstructed.settings.wire_notifications == WireNotifications.ALL


def test_reconstruct_optional_settings_none(tmp_path):
    """Optional settings (sync_max_rounds, round_timeout) are None when not set."""
    original = OrgConfig(
        roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
        agents={"a": AgentConfig(agent_id="a", role="w")},
        settings=OrgSettings(org_name="optional_none"),
    )

    reconstructed = _seed_and_reconstruct(tmp_path, original)
    assert reconstructed.settings.sync_max_rounds is None
    assert reconstructed.settings.sync_round_timeout_seconds is None


def test_reconstruct_sync_mode(tmp_path):
    """Sync mode with sync_max_rounds and round_timeout."""
    original = OrgConfig(
        roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
        agents={"a": AgentConfig(agent_id="a", role="w")},
        settings=OrgSettings(org_name="sync_test", sync=True, sync_max_rounds=5, sync_round_timeout_seconds=30.0),
    )

    reconstructed = _seed_and_reconstruct(tmp_path, original)
    assert reconstructed.settings.sync is True
    assert reconstructed.settings.sync_max_rounds == 5
    assert reconstructed.settings.sync_round_timeout_seconds == 30.0

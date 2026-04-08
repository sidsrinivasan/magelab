"""Tests for magelab.frontend.bridge — FrontendBridge event serialization."""

import json
import logging
import pytest
from magelab.registry_config import AgentConfig, RoleConfig
from magelab.events import TaskAssignedEvent, WireMessageEvent
from magelab.frontend.bridge import FrontendBridge
from magelab.orchestrator import RunOutcome
from magelab.state.registry import Registry
from magelab.state.task_schemas import Task
from magelab.state.task_store import TaskStore
from magelab.state.wire_store import WireStore

_test_logger = logging.getLogger("test")


def _make_bridge() -> tuple[FrontendBridge, TaskStore, Registry, WireStore]:
    roles = {
        "pm": RoleConfig(name="pm", role_prompt="Manage.", tools=["management"], model="test"),
        "coder": RoleConfig(name="coder", role_prompt="Code.", tools=["worker"], model="test"),
    }
    agents = {
        "pm": AgentConfig(agent_id="pm", role="pm"),
        "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
    }
    task_store = TaskStore(framework_logger=_test_logger)
    registry = Registry(framework_logger=_test_logger)
    registry.register_config(roles, agents)
    wire_store = WireStore(framework_logger=_test_logger)
    bridge = FrontendBridge(task_store, registry, wire_store, org_name="test-org")
    return bridge, task_store, registry, wire_store


class TestFrontendBridge:
    @pytest.mark.asyncio
    async def test_build_init_snapshot(self):
        bridge, _, _, _ = _make_bridge()
        snapshot = await bridge.build_init_snapshot()
        data = json.loads(snapshot)
        assert data["type"] == "init"
        assert "agents" in data
        assert "pm" in data["agents"]
        assert "coder-0" in data["agents"]
        assert data["org_name"] == "test-org"

    def test_serialize_task_event(self):
        bridge, _, _, _ = _make_bridge()
        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        msg = bridge.serialize_event(event)
        data = json.loads(msg)
        assert data["type"] == "event_dispatched"
        assert data["event_type"] == "TaskAssignedEvent"
        assert data["payload"]["task_id"] == "t1"

    def test_serialize_wire_event(self):
        bridge, _, _, _ = _make_bridge()
        event = WireMessageEvent(target_id="coder-0", wire_id="w1", source_id="pm", message_cursor=1)
        msg = bridge.serialize_event(event)
        data = json.loads(msg)
        assert data["type"] == "event_dispatched"
        assert data["payload"]["wire_id"] == "w1"

    def test_serialize_transcript_entry(self):
        bridge, _, _, _ = _make_bridge()
        msg = bridge.serialize_transcript("coder-0", "assistant_text", "I'll fix the bug")
        data = json.loads(msg)
        assert data["type"] == "transcript_entry"
        assert data["agent_id"] == "coder-0"
        assert data["content"] == "I'll fix the bug"

    @pytest.mark.asyncio
    async def test_serialize_task_changed(self):
        bridge, task_store, _, _ = _make_bridge()
        task = Task(id="t1", title="Do work", description="...")
        await task_store.create(task, assigned_to="coder-0", assigned_by="pm")
        msg = await bridge.serialize_task("t1")
        data = json.loads(msg)
        assert data["type"] == "task_changed"
        assert data["task_id"] == "t1"
        assert data["task"]["id"] == "t1"
        assert data["task"]["status"] == "assigned"

    @pytest.mark.asyncio
    async def test_serialize_task_not_found(self):
        bridge, _, _, _ = _make_bridge()
        msg = await bridge.serialize_task("nonexistent")
        data = json.loads(msg)
        assert data["type"] == "task_changed"
        assert data["task_id"] == "nonexistent"
        assert data["task"] is None

    def test_event_log_accumulates(self):
        bridge, _, _, _ = _make_bridge()
        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        bridge.serialize_event(event)
        bridge.serialize_transcript("coder-0", "assistant_text", "hello")
        assert len(bridge.event_log) == 2

    # --- serialize_agent_state_change ---

    def test_serialize_agent_state_change_fields(self):
        bridge, _, _, _ = _make_bridge()
        msg = bridge.serialize_agent_state_change("coder-0", "working", "t1")
        data = json.loads(msg)
        assert data["type"] == "agent_state_changed"
        assert data["agent_id"] == "coder-0"
        assert data["state"] == "working"
        assert data["current_task_id"] == "t1"

    def test_serialize_agent_state_change_null_task(self):
        bridge, _, _, _ = _make_bridge()
        msg = bridge.serialize_agent_state_change("pm", "idle", None)
        data = json.loads(msg)
        assert data["current_task_id"] is None

    def test_serialize_agent_state_change_appends_to_event_log(self):
        bridge, _, _, _ = _make_bridge()
        assert len(bridge.event_log) == 0
        bridge.serialize_agent_state_change("coder-0", "working", "t1")
        assert len(bridge.event_log) == 1

    # --- serialize_wire_message ---

    def test_serialize_wire_message_fields(self):
        bridge, _, _, _ = _make_bridge()
        msg = bridge.serialize_wire_message("w1", frozenset({"pm", "coder-0"}), "pm", "hello")
        data = json.loads(msg)
        assert data["type"] == "wire_message"
        assert data["wire_id"] == "w1"
        assert data["sender"] == "pm"
        assert data["body"] == "hello"
        assert data["participants"] == ["coder-0", "pm"]  # sorted
        assert "timestamp" in data

    def test_serialize_wire_message_does_not_append_to_event_log(self):
        bridge, _, _, _ = _make_bridge()
        bridge.serialize_wire_message("w1", frozenset({"pm", "coder-0"}), "pm", "hello")
        assert len(bridge.event_log) == 0

    # --- serialize_run_finished ---

    def test_serialize_run_finished_fields(self):
        bridge, _, _, _ = _make_bridge()
        msg = bridge.serialize_run_finished(RunOutcome.SUCCESS, 42.5, 1.23)
        data = json.loads(msg)
        assert data["type"] == "run_finished"
        assert data["outcome"] == "success"
        assert data["duration_seconds"] == 42.5
        assert data["total_cost_usd"] == 1.23

    def test_serialize_run_finished_appends_to_event_log(self):
        bridge, _, _, _ = _make_bridge()
        assert len(bridge.event_log) == 0
        bridge.serialize_run_finished(RunOutcome.FAILURE, 10.0, 0.5)
        assert len(bridge.event_log) == 1

    # --- serialize_queue_snapshot ---

    def test_serialize_queue_snapshot_empty(self):
        bridge, _, _, _ = _make_bridge()
        result = bridge.serialize_queue_snapshot("pm")
        assert result == []

    def test_serialize_queue_snapshot_with_events(self):
        bridge, _, registry, _ = _make_bridge()
        event = TaskAssignedEvent(task_id="t1", target_id="pm", source_id="coder-0")
        registry.enqueue("pm", event)
        result = bridge.serialize_queue_snapshot("pm")
        assert len(result) == 1
        assert result[0]["event_type"] == "TaskAssignedEvent"
        assert result[0]["event_id"] == event.event_id

    def test_serialize_queue_snapshot_does_not_append_to_event_log(self):
        bridge, _, registry, _ = _make_bridge()
        event = TaskAssignedEvent(task_id="t1", target_id="pm", source_id="coder-0")
        registry.enqueue("pm", event)
        bridge.serialize_queue_snapshot("pm")
        assert len(bridge.event_log) == 0

    # --- serialize_queue_event_added / serialize_queue_event_removed ---

    def test_serialize_queue_event_added_fields(self):
        bridge, _, _, _ = _make_bridge()
        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        msg = bridge.serialize_queue_event_added("coder-0", event)
        data = json.loads(msg)
        assert data["type"] == "queue_event_added"
        assert data["agent_id"] == "coder-0"
        assert data["event"]["event_type"] == "TaskAssignedEvent"
        assert data["event"]["event_id"] == event.event_id

    def test_serialize_queue_event_added_does_not_append_to_event_log(self):
        bridge, _, _, _ = _make_bridge()
        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        bridge.serialize_queue_event_added("coder-0", event)
        assert len(bridge.event_log) == 0

    def test_serialize_queue_event_removed_fields(self):
        bridge, _, _, _ = _make_bridge()
        msg = bridge.serialize_queue_event_removed("coder-0", "evt-123")
        data = json.loads(msg)
        assert data["type"] == "queue_event_removed"
        assert data["agent_id"] == "coder-0"
        assert data["event_id"] == "evt-123"

    def test_serialize_queue_event_removed_does_not_append_to_event_log(self):
        bridge, _, _, _ = _make_bridge()
        bridge.serialize_queue_event_removed("coder-0", "evt-123")
        assert len(bridge.event_log) == 0

    # --- Comprehensive event_log invariant test ---

    @pytest.mark.asyncio
    async def test_event_log_tracks_correct_methods(self):
        """Verify the critical invariant: which methods append to event_log and which don't."""
        bridge, task_store, _, _ = _make_bridge()

        # Methods that SHOULD append to event_log
        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        bridge.serialize_event(event)  # 1
        bridge.serialize_transcript("coder-0", "assistant_text", "hi")  # 2
        bridge.serialize_agent_state_change("coder-0", "working", "t1")  # 3
        bridge.serialize_run_finished(RunOutcome.SUCCESS, 10.0, 0.5)  # 4

        task = Task(id="t1", title="Do work", description="...")
        await task_store.create(task, assigned_to="coder-0", assigned_by="pm")
        await bridge.serialize_task("t1")  # 5

        # Methods that should NOT append to event_log
        bridge.serialize_wire_message("w1", frozenset({"pm", "coder-0"}), "pm", "hey")
        bridge.serialize_queue_snapshot("pm")
        bridge.serialize_queue_event_added("coder-0", event)
        bridge.serialize_queue_event_removed("coder-0", "evt-xyz")

        # Exactly 5 entries from the appending methods
        assert len(bridge.event_log) == 5

        # Verify the types in order
        types = [json.loads(entry)["type"] for entry in bridge.event_log]
        assert types == [
            "event_dispatched",
            "transcript_entry",
            "agent_state_changed",
            "run_finished",
            "task_changed",
        ]

"""Tests for magelab.orchestrator — Orchestrator with mocked AgentRunner.

Uses a MockRunner that returns controlled AgentRunResults to test the
orchestration logic without any Claude API calls.
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from magelab.org_config import OrgConfig
from magelab.registry_config import AgentConfig, RoleConfig
from magelab.events import MCPEvent, TaskAssignedEvent
from magelab.orchestrator import Orchestrator
from magelab.runners.agent_runner import AgentRunResult
from magelab.runners.prompts import default_prompt_formatter
from magelab.state.database import Database
from magelab.state.registry import Registry
from magelab.state.task_schemas import ReviewPolicy, ReviewRecord, ReviewStatus, Task, TaskStatus
from magelab.state.task_store import TaskStore

from .conftest import MockRunner, get_agent_dispatches, get_all_agent_dispatches, get_run_meta
from .helpers import make_orch_org, make_orchestrator

_test_logger = logging.getLogger("test")

# =============================================================================
# Helpers
# =============================================================================


def _make_org(
    roles: dict[str, RoleConfig] | None = None,
    agents: dict[str, AgentConfig] | None = None,
    tmp_dir: Path | None = None,
) -> tuple[TaskStore, Registry, MockRunner, Database]:
    store, registry, db = make_orch_org(roles, agents, tmp_dir)
    runner = MockRunner()
    return store, registry, runner, db


def _make_orchestrator(
    store: TaskStore,
    registry: Registry,
    runner: MockRunner,
    db: Database,
    global_timeout: float = 30.0,
    org_prompt: str = "Test org",
) -> Orchestrator:
    return make_orchestrator(store, registry, runner, db, global_timeout, org_prompt)


def _make_task(
    id: str = "task-1",
    title: str = "Test Task",
    description: str = "Do something",
    review_required: bool = False,
) -> Task:
    return Task(id=id, title=title, description=description, review_required=review_required)


# =============================================================================
# Basic run lifecycle
# =============================================================================


class TestBasicRun:
    @pytest.mark.asyncio
    async def test_run_single_task_completes(self):
        """A single task assigned to coder should complete successfully."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # The mock runner returns success, but the task won't auto-finish
        # because mark_finished is done by tool calls. Let's use a side effect.
        async def finish_task():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish_task]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert orch.outcome == "success"
        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert not orch.timed_out
        dispatches = get_agent_dispatches(db, "coder-0")
        assert len(dispatches) >= 1

    @pytest.mark.asyncio
    async def test_run_no_initial_tasks(self):
        """Run with no initial tasks should complete immediately."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[])
        assert orch.outcome == "no_work"
        assert not orch.timed_out

    @pytest.mark.asyncio
    async def test_run_records_timing(self):
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish():
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish]

        await orch.run(initial_tasks=[(_make_task(id="t1"), "coder-0", "User")])
        meta = get_run_meta(db)
        assert meta["start_time"] is not None
        assert meta["end_time"] is not None
        assert meta["duration_seconds"] >= 0


# =============================================================================
# Global timeout
# =============================================================================


class TestTimeout:
    @pytest.mark.asyncio
    async def test_global_timeout(self):
        """Orchestrator should timeout if an agent is busy past the deadline."""
        store, registry, runner, db = _make_org()
        # Very short timeout; side effect sleeps longer than the timeout
        orch = _make_orchestrator(store, registry, runner, db, global_timeout=1.0)

        async def slow_work():
            await asyncio.sleep(10)

        runner.side_effects["coder-0"] = [slow_work]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])
        assert orch.timed_out
        assert orch.outcome == "timeout"


# =============================================================================
# Agent failure handling
# =============================================================================


class TestAgentFailure:
    @pytest.mark.asyncio
    async def test_worker_failure_marks_task_failed(self):
        """When a worker agent fails, its task should be marked FAILED."""
        store, registry, runner, db = _make_org()
        runner.fail_agents.add("coder-0")
        orch = _make_orchestrator(store, registry, runner, db)

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert get_run_meta(db)["tasks_failed"] == 1
        assert orch.outcome == "failure"
        stored_task = await store.get_task("task-1")
        assert stored_task.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_reviewer_failure_marks_review_failed(self):
        """When a reviewer agent fails, the review is marked failed (not the task).
        The coder gets a ReviewFinishedEvent with REVIEW_FAILED outcome.
        From REVIEW_FAILED, the coder can mark the task as failed."""
        store, registry, runner, db = _make_org()
        runner.fail_agents.add("reviewer-0")
        orch = _make_orchestrator(store, registry, runner, db)

        # Coder submits for review, which triggers reviewer
        async def submit_for_review():
            records = [ReviewRecord(reviewer_id="reviewer-0", requester_id="coder-0", request_message="Review")]
            await store.submit_for_review("task-1", records, ReviewPolicy.ALL_APPROVE)

        # After review failure, task is in REVIEW_FAILED status.
        # Coder marks it failed (can't succeed without approval).
        async def finish_after_review():
            await store.mark_finished("task-1", TaskStatus.FAILED, "review failed, giving up")

        runner.side_effects["coder-0"] = [submit_for_review, finish_after_review]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert get_run_meta(db)["tasks_failed"] == 1
        # Reviewer dispatch should show the error
        reviewer_dispatches = get_agent_dispatches(db, "reviewer-0")
        assert len(reviewer_dispatches) >= 1
        assert reviewer_dispatches[0]["error"] == "Agent crashed"
        # coder-0 should have 2 dispatches — first TaskAssignedEvent, second ReviewFinishedEvent.
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 2
        assert coder_dispatches[1]["event_type"] == "ReviewFinishedEvent"

    @pytest.mark.asyncio
    async def test_reviewer_exception_calls_mark_review_failed(self):
        """When a reviewer's run_agent raises an exception (not just returns an error),
        mark_review_failed is called — the task ends up in REVIEW_FAILED, not FAILED.
        The coder then receives a ReviewFinishedEvent with REVIEW_FAILED outcome."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Coder submits for review, which triggers reviewer
        async def submit_for_review():
            records = [ReviewRecord(reviewer_id="reviewer-0", requester_id="coder-0", request_message="Review")]
            await store.submit_for_review("task-1", records, ReviewPolicy.ALL_APPROVE)

        # After review failure, coder marks task failed
        async def finish_after_review():
            await store.mark_finished("task-1", TaskStatus.FAILED, "review failed, giving up")

        runner.side_effects["coder-0"] = [submit_for_review, finish_after_review]

        # Override run_agent to raise an exception for reviewer-0
        original_run = runner.run_agent

        async def exploding_reviewer(agent_id, system_prompt, prompt):
            if agent_id == "reviewer-0":
                # Still record the call for tracking
                runner.calls.append((agent_id, system_prompt, prompt))
                raise RuntimeError("Reviewer SDK crashed")
            return await original_run(agent_id, system_prompt, prompt)

        runner.run_agent = exploding_reviewer

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert get_run_meta(db)["tasks_failed"] == 1
        # Reviewer dispatch should record the exception
        reviewer_dispatches = get_agent_dispatches(db, "reviewer-0")
        assert len(reviewer_dispatches) >= 1
        assert "Reviewer SDK crashed" in reviewer_dispatches[0]["error"]
        # Coder should have been dispatched twice: once for initial task, once for ReviewFinishedEvent
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 2

    @pytest.mark.asyncio
    async def test_runner_exception_marks_task_failed(self):
        """An exception from the runner should mark the task as failed."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Override run_agent to raise
        original_run = runner.run_agent

        async def exploding_run(agent_id, system_prompt, prompt):
            if agent_id == "coder-0":
                raise RuntimeError("Unexpected SDK error")
            return await original_run(agent_id, system_prompt, prompt)

        runner.run_agent = exploding_run

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])
        assert get_run_meta(db)["tasks_failed"] == 1
        # The dispatch should record the error
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 1
        assert "Unexpected SDK error" in coder_dispatches[0]["error"]


# =============================================================================
# Multi-agent delegation
# =============================================================================


class TestMultiAgent:
    @pytest.mark.asyncio
    async def test_two_tasks_parallel(self):
        """Two tasks assigned to different agents should both complete."""
        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
            ),
        }
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "coder-1": AgentConfig(agent_id="coder-1", role="coder"),
        }
        store, registry, runner, db = _make_org(roles=roles, agents=agents)
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish_t1():
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        async def finish_t2():
            await store.mark_finished("t2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish_t1]
        runner.side_effects["coder-1"] = [finish_t2]

        t1 = _make_task(id="t1", title="Task 1")
        t2 = _make_task(id="t2", title="Task 2")
        await orch.run(initial_tasks=[(t1, "coder-0", "User"), (t2, "coder-1", "User")])
        assert get_run_meta(db)["tasks_succeeded"] == 2
        assert orch.outcome == "success"


# =============================================================================
# Stale event skipping
# =============================================================================


class TestStaleEvents:
    @pytest.mark.asyncio
    async def test_stale_events_tracked_in_db(self):
        """When an event becomes stale (task state moved past it), the event
        should be recorded in the DB with outcome 'stale_at_delivery'.

        Scenario: coder-0 finishes task-1, then a second TaskAssignedEvent for
        the same task is dispatched through _dispatch_event (which logs to DB).
        When coder-0 dequeues it, the staleness check fires and records the
        outcome as stale_at_delivery."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # coder-0 finishes the task on first dispatch, then dispatches a second
        # TaskAssignedEvent through the orchestrator (so it gets DB-logged).
        async def finish_and_dispatch_stale():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")
            stale_event = TaskAssignedEvent(task_id="task-1", target_id="coder-0", source_id="pm")
            orch._dispatch_event(stale_event)

        runner.side_effects["coder-0"] = [finish_and_dispatch_stale]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert get_run_meta(db)["tasks_succeeded"] == 1

        # The second event should be recorded as stale_at_delivery in the DB
        all_dispatches = get_all_agent_dispatches(db, "coder-0")
        stale_dispatches = [d for d in all_dispatches if d["outcome"] == "stale_at_delivery"]
        assert len(stale_dispatches) == 1
        assert stale_dispatches[0]["event_type"] == "TaskAssignedEvent"
        assert stale_dispatches[0]["task_id"] == "task-1"


class TestDroppedAtEnqueue:
    @pytest.mark.asyncio
    async def test_event_targeting_non_agent_dropped(self):
        """Events targeting non-agent IDs (e.g., 'User') are dropped at enqueue
        and recorded in the DB with outcome 'dropped_at_enqueue'."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish_task():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish_task]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        # TaskFinishedEvent targets "User" (the assigner), who is not a registered agent
        all_events = get_all_agent_dispatches(db, "User")
        dropped = [d for d in all_events if d["outcome"] == "dropped_at_enqueue"]
        assert len(dropped) == 1
        assert dropped[0]["event_type"] == "TaskFinishedEvent"


# =============================================================================
# Validation
# =============================================================================


class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_tool_deps_raises(self):
        """get_available_reviewers without tasks_submit_for_review is an error."""
        roles = {
            "bad": RoleConfig(
                name="bad", role_prompt="Bad config", tools=["get_available_reviewers"], model="test", max_turns=10
            ),
        }
        agents = {"bad-0": AgentConfig(agent_id="bad-0", role="bad")}

        tmp_dir = Path(tempfile.mkdtemp())
        db = Database(tmp_dir / "org.db")
        db.init_run_meta(org_name="test", org_config="{}")

        store = TaskStore(framework_logger=_test_logger, db=db)
        registry = Registry(framework_logger=_test_logger, db=db)
        registry.register_config(roles, agents)
        runner = MockRunner()
        orch = _make_orchestrator(store, registry, runner, db)

        with pytest.raises(ValueError, match="Invalid configuration"):
            await orch.run(initial_tasks=[])

    @pytest.mark.asyncio
    async def test_invalid_task_assignment_raises(self):
        """Assigning review-required task to agent without submit capability."""
        roles = {
            "pm": RoleConfig(name="pm", role_prompt="PM", tools=["management"], model="test", max_turns=10),
        }
        agents = {"pm": AgentConfig(agent_id="pm", role="pm")}

        tmp_dir = Path(tempfile.mkdtemp())
        db = Database(tmp_dir / "org.db")
        db.init_run_meta(org_name="test", org_config="{}")

        store = TaskStore(framework_logger=_test_logger, db=db)
        registry = Registry(framework_logger=_test_logger, db=db)
        registry.register_config(roles, agents)
        runner = MockRunner()
        orch = _make_orchestrator(store, registry, runner, db)

        task = _make_task(review_required=True)
        with pytest.raises(ValueError, match="Invalid configuration"):
            await orch.run(initial_tasks=[(task, "pm", "User")])


# =============================================================================
# initial_tasks from OrgConfig
# =============================================================================


class TestLoadInitialTasks:
    def test_load_from_yaml(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "initial_tasks": [
                {"id": "t1", "title": "First", "description": "Do first", "assigned_to": "coder-0"},
                {
                    "id": "t2",
                    "title": "Second",
                    "description": "Do second",
                    "assigned_to": "coder-1",
                    "review_required": True,
                },
            ],
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {
                "coder-0": {"agent_id": "coder-0", "role": "coder"},
                "coder-1": {"agent_id": "coder-1", "role": "coder"},
            },
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        org_config = OrgConfig.from_yaml(str(path))
        assert len(org_config.initial_tasks) == 2
        assert org_config.initial_tasks[0][0].id == "t1"
        assert org_config.initial_tasks[0][1] == "coder-0"
        assert org_config.initial_tasks[0][2] == "User"  # default assigned_by
        assert org_config.initial_tasks[1][0].review_required is True

    def test_load_no_initial_tasks(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        org_config = OrgConfig.from_yaml(str(path))
        assert org_config.initial_tasks == []

    def test_load_custom_assigned_by(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "initial_tasks": [
                {
                    "id": "t1",
                    "title": "First",
                    "description": "Do first",
                    "assigned_to": "coder-0",
                    "assigned_by": "client",
                },
            ],
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        org_config = OrgConfig.from_yaml(str(path))
        assert org_config.initial_tasks[0][2] == "client"


# =============================================================================
# initial_messages from OrgConfig
# =============================================================================


class TestLoadInitialMessages:
    def test_load_from_yaml(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "initial_messages": [
                {"participants": ["coder-0"], "sender": "client", "body": "Hello"},
                {"participants": ["coder-1"], "body": "Hi"},  # sender defaults to "User"
            ],
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {
                "coder-0": {"agent_id": "coder-0", "role": "coder"},
                "coder-1": {"agent_id": "coder-1", "role": "coder"},
            },
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        org_config = OrgConfig.from_yaml(str(path))
        assert len(org_config.initial_messages) == 2
        assert org_config.initial_messages[0]["sender"] == "client"
        assert org_config.initial_messages[0]["participants"] == ["coder-0"]
        assert org_config.initial_messages[0]["body"] == "Hello"
        assert org_config.initial_messages[1]["sender"] == "User"  # default

    def test_load_no_initial_messages(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        org_config = OrgConfig.from_yaml(str(path))
        assert org_config.initial_messages == []

    def test_missing_required_field(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "initial_messages": [{"participants": ["coder-0"], "sender": "client"}],  # missing body
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        with pytest.raises(ValueError, match="missing required fields"):
            OrgConfig.from_yaml(str(path))

    def test_unknown_field_rejected(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "initial_messages": [{"participants": ["coder-0"], "sender": "x", "body": "hi", "bogus": True}],
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        with pytest.raises(ValueError, match="unknown fields"):
            OrgConfig.from_yaml(str(path))

    def test_participant_must_be_registered_agent(self, tmp_path):
        data = {
            "settings": {"org_name": "test"},
            "initial_messages": [{"participants": ["nonexistent"], "sender": "client", "body": "hi"}],
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        with pytest.raises(ValueError, match="not a registered agent"):
            OrgConfig.from_yaml(str(path))

    def test_round_trip(self, tmp_path):
        """to_dict() should preserve initial_messages for re-parsing."""
        data = {
            "settings": {"org_name": "test"},
            "initial_messages": [
                {"participants": ["coder-0"], "sender": "client", "body": "Hello"},
            ],
            "roles": {"coder": {"name": "coder", "role_prompt": "You code.", "tools": [], "model": "test"}},
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)

        org_config = OrgConfig.from_yaml(str(path))
        d = org_config.to_dict()
        assert d["initial_messages"] == [{"participants": ["coder-0"], "sender": "client", "body": "Hello"}]


class TestSendInitialMessages:
    @pytest.mark.asyncio
    async def test_initial_messages_create_wires(self):
        """Initial messages should create wires and emit events to participants."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        messages = [
            {"participants": ["coder-0"], "sender": "client", "body": "Build this"},
        ]
        await orch.run(initial_messages=messages)

        # Wire should exist
        wire = await orch.wire_store.get_wire("wire_0")
        assert wire is not None
        assert "client" in wire.participants
        assert "coder-0" in wire.participants
        assert wire.messages[0].body == "Build this"
        assert wire.messages[0].sender == "client"

    @pytest.mark.asyncio
    async def test_initial_messages_skip_existing_wire(self):
        """Sending the same initial message twice should skip the duplicate."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        messages = [
            {"participants": ["coder-0"], "sender": "client", "body": "Build this", "wire_id": "w1"},
        ]
        # First send creates the wire
        await orch._send_initial_messages(messages)
        wire = await orch.wire_store.get_wire("w1")
        assert wire is not None
        assert len(wire.messages) == 1

        # Second send skips it (no error, no duplicate message)
        await orch._send_initial_messages(messages)
        wire2 = await orch.wire_store.get_wire("w1")
        assert len(wire2.messages) == 1  # still just the one message

    @pytest.mark.asyncio
    async def test_both_tasks_and_messages(self):
        """Tasks and messages can coexist — both are created at startup."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Call the setup methods directly to avoid full run lifecycle
        task = _make_task()
        await orch._validate_and_create_initial_tasks([(task, "coder-0", "User")])
        await orch._send_initial_messages([{"participants": ["coder-0"], "sender": "client", "body": "FYI"}])

        t = await store.get_task("task-1")
        assert t is not None
        wire = await orch.wire_store.get_wire("wire_0")
        assert wire is not None


# =============================================================================
# Shutdown
# =============================================================================


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_interrupts_agents(self):
        """On shutdown, all agents should receive interrupt signals."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db, global_timeout=1.0)

        # Keep the agent busy so the global timeout fires
        async def slow_work():
            await asyncio.sleep(10)

        runner.side_effects["coder-0"] = [slow_work]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])
        # After run completes (via timeout), all agents should have been interrupted
        assert runner._interrupted == {"pm", "coder-0", "reviewer-0"}


# =============================================================================
# Stats capture
# =============================================================================


class TestStatsCapture:
    @pytest.mark.asyncio
    async def test_cost_tracked(self):
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish():
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish]

        await orch.run(initial_tasks=[(_make_task(id="t1"), "coder-0", "User")])
        meta = get_run_meta(db)
        assert meta["total_cost_usd"] > 0


# =============================================================================
# Events dropped (prompt formatter returns None)
# =============================================================================


class TestNonePromptFailsTask:
    @pytest.mark.asyncio
    async def test_none_prompt_fails_task(self):
        """When the prompt formatter returns None, the task should be marked
        FAILED (not silently orphaned). The runner should NOT be called."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Patch the prompt formatter to return None for coder-0 events,
        # simulating an unrecognised event type.
        def dropping_formatter(ctx):
            if ctx.event.target_id == "coder-0":
                return None
            return default_prompt_formatter(ctx)

        with patch("magelab.orchestrator.default_prompt_formatter", side_effect=dropping_formatter):
            task = _make_task()
            await orch.run(initial_tasks=[(task, "coder-0", "User")])

        # The runner should NOT have been called for coder-0
        assert not any(c[0] == "coder-0" for c in runner.calls)

        # The task should be FAILED, not orphaned at ASSIGNED
        stored_task = await store.get_task("task-1")
        assert stored_task.status == TaskStatus.FAILED


# =============================================================================
# mark_in_progress race condition
# =============================================================================


class TestMarkInProgressRace:
    @pytest.mark.asyncio
    async def test_mark_in_progress_race_condition(self):
        """When a TaskAssignedEvent passes the staleness check but the task has
        already moved past ASSIGNED before mark_in_progress is called, the
        orchestrator should log a debug message and return (not crash).

        We simulate this by monkey-patching is_event_stale to let a stale
        TaskAssignedEvent through, then verifying the orchestrator handles the
        ValueError from mark_in_progress gracefully."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # First dispatch: coder-0 finishes the task and manually enqueues a
        # second TaskAssignedEvent for the same task (which is now finished).
        async def finish_and_enqueue_stale():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")
            # Manually enqueue a stale TaskAssignedEvent for the same task
            stale_event = TaskAssignedEvent(task_id="task-1", target_id="coder-0", source_id="pm")
            registry.enqueue("coder-0", stale_event)

        runner.side_effects["coder-0"] = [finish_and_enqueue_stale]

        # Monkey-patch is_event_stale to let TaskAssignedEvents through even if stale.
        # This simulates the race where staleness check passes but task state changed
        # between the check and mark_in_progress.
        original_is_stale = store.is_event_stale

        async def permissive_staleness(event):
            if isinstance(event, TaskAssignedEvent) and event.task_id == "task-1":
                # Let it through on the second call (first call is legitimate)
                if hasattr(permissive_staleness, "_seen_task1"):
                    return False  # Let the stale event through
                permissive_staleness._seen_task1 = True
            return await original_is_stale(event)

        store.is_event_stale = permissive_staleness

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        # The run should complete successfully (task succeeded on first dispatch)
        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert orch.outcome == "success"
        # coder-0 should have exactly 1 completed dispatch (the first successful one).
        # The second (stale) event should have been caught by the mark_in_progress
        # ValueError handler and silently skipped — no completed dispatch recorded for it.
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 1


# =============================================================================
# Full review workflow happy path
# =============================================================================


class TestReviewWorkflowHappyPath:
    @pytest.mark.asyncio
    async def test_review_approve_succeeds(self):
        """Full review workflow: worker submits for review, reviewer approves,
        worker receives ReviewFinishedEvent with APPROVED outcome and marks task
        succeeded. Final task status is SUCCEEDED."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Coder dispatch 1: submits for review
        async def submit_for_review():
            records = [ReviewRecord(reviewer_id="reviewer-0", requester_id="coder-0", request_message="Please review")]
            await store.submit_for_review("task-1", records, ReviewPolicy.ALL_APPROVE)

        # Reviewer dispatch: approves the review
        async def approve_review():
            await store.submit_review("task-1", "reviewer-0", ReviewStatus.APPROVED, "Looks good")

        # Coder dispatch 2: receives APPROVED ReviewFinishedEvent, marks task succeeded
        async def finish_after_approval():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done after approval")

        runner.side_effects["coder-0"] = [submit_for_review, finish_after_approval]
        runner.side_effects["reviewer-0"] = [approve_review]

        task = _make_task(review_required=True)
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert orch.outcome == "success"
        # Coder should have 2 dispatches: TaskAssignedEvent, then ReviewFinishedEvent
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 2
        assert coder_dispatches[0]["event_type"] == "TaskAssignedEvent"
        assert coder_dispatches[1]["event_type"] == "ReviewFinishedEvent"
        # Reviewer should have 1 dispatch: ReviewRequestedEvent
        reviewer_dispatches = get_agent_dispatches(db, "reviewer-0")
        assert len(reviewer_dispatches) == 1
        assert reviewer_dispatches[0]["event_type"] == "ReviewRequestedEvent"
        # Final task status
        stored_task = await store.get_task("task-1")
        assert stored_task.status == TaskStatus.SUCCEEDED


# =============================================================================
# Changes-requested workflow
# =============================================================================


class TestChangesRequestedWorkflow:
    @pytest.mark.asyncio
    async def test_changes_requested_then_approved(self):
        """Reviewer requests changes, worker resubmits, reviewer approves
        on second round. Verify task succeeds."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Coder dispatch 1: submits for review (round 1)
        async def submit_round_1():
            records = [ReviewRecord(reviewer_id="reviewer-0", requester_id="coder-0", request_message="Review round 1")]
            await store.submit_for_review("task-1", records, ReviewPolicy.ALL_APPROVE)

        # Coder dispatch 2: receives CHANGES_REQUESTED, resubmits (round 2)
        async def resubmit_round_2():
            records = [ReviewRecord(reviewer_id="reviewer-0", requester_id="coder-0", request_message="Review round 2")]
            await store.submit_for_review("task-1", records, ReviewPolicy.ALL_APPROVE)

        # Coder dispatch 3: receives APPROVED, marks task succeeded
        async def finish_after_approval():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done after round 2 approval")

        # Reviewer dispatch 1: requests changes
        async def request_changes():
            await store.submit_review("task-1", "reviewer-0", ReviewStatus.CHANGES_REQUESTED, "Needs work")

        # Reviewer dispatch 2: approves
        async def approve_review():
            await store.submit_review("task-1", "reviewer-0", ReviewStatus.APPROVED, "Now looks good")

        runner.side_effects["coder-0"] = [submit_round_1, resubmit_round_2, finish_after_approval]
        runner.side_effects["reviewer-0"] = [request_changes, approve_review]

        task = _make_task(review_required=True)
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert orch.outcome == "success"
        # Coder: 3 dispatches (TaskAssigned, ReviewFinished(CHANGES_REQUESTED), ReviewFinished(APPROVED))
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 3
        assert coder_dispatches[0]["event_type"] == "TaskAssignedEvent"
        assert coder_dispatches[1]["event_type"] == "ReviewFinishedEvent"
        assert coder_dispatches[2]["event_type"] == "ReviewFinishedEvent"
        # Reviewer: 2 dispatches (two ReviewRequestedEvents)
        reviewer_dispatches = get_agent_dispatches(db, "reviewer-0")
        assert len(reviewer_dispatches) == 2
        # Final task
        stored_task = await store.get_task("task-1")
        assert stored_task.status == TaskStatus.SUCCEEDED
        assert stored_task.current_review_round == 2


# =============================================================================
# Mixed success/failure verdict
# =============================================================================


class TestMixedVerdict:
    @pytest.mark.asyncio
    async def test_mixed_success_failure_gives_partial_verdict(self):
        """Two tasks: one succeeds, one fails. Verdict should be 'partial'."""
        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
            ),
        }
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "coder-1": AgentConfig(agent_id="coder-1", role="coder"),
        }
        store, registry, runner, db = _make_org(roles=roles, agents=agents)
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish_t1_success():
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        async def finish_t2_failure():
            await store.mark_finished("t2", TaskStatus.FAILED, "something went wrong")

        runner.side_effects["coder-0"] = [finish_t1_success]
        runner.side_effects["coder-1"] = [finish_t2_failure]

        t1 = _make_task(id="t1", title="Task 1")
        t2 = _make_task(id="t2", title="Task 2")
        await orch.run(initial_tasks=[(t1, "coder-0", "User"), (t2, "coder-1", "User")])

        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert get_run_meta(db)["tasks_failed"] == 1
        assert orch.outcome == "partial"


# =============================================================================
# PM receiving TaskFinishedEvent
# =============================================================================


class TestPMReceivesTaskFinished:
    @pytest.mark.asyncio
    async def test_pm_receives_task_finished_event(self):
        """When a worker finishes a task that was assigned by the PM (via
        initial_tasks where assigned_by=User), the TaskFinishedEvent is sent
        to assigned_by — which for initial tasks is SystemAgent.USER.

        However, if a PM creates and assigns a task, the TaskFinishedEvent
        goes to the PM. We simulate this by having the PM create a subtask
        via side effect and the coder finishing it.

        Verify PM receives a TaskFinishedEvent dispatch."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # PM dispatch 1 (for the initial task): creates a subtask and assigns to coder
        async def pm_creates_subtask():
            subtask = _make_task(id="subtask-1", title="Subtask from PM", description="Do sub work")
            await store.create(subtask, assigned_to="coder-0", assigned_by="pm")
            # PM marks its own task as succeeded
            await store.mark_finished("pm-task", TaskStatus.SUCCEEDED, "delegated work")

        # Coder dispatch (for the subtask): finishes it
        async def coder_finishes_subtask():
            await store.mark_finished("subtask-1", TaskStatus.SUCCEEDED, "sub done")

        # PM dispatch 2 (for TaskFinishedEvent from subtask): no-op (task already done)
        async def pm_noop():
            pass

        runner.side_effects["pm"] = [pm_creates_subtask, pm_noop]
        runner.side_effects["coder-0"] = [coder_finishes_subtask]

        pm_task = _make_task(id="pm-task", title="PM Task", description="Manage work")
        await orch.run(initial_tasks=[(pm_task, "pm", "User")])

        assert get_run_meta(db)["tasks_succeeded"] == 2
        # PM should have 2 dispatches: TaskAssignedEvent for pm-task, TaskFinishedEvent for subtask-1
        pm_dispatches = get_agent_dispatches(db, "pm")
        assert len(pm_dispatches) == 2
        assert pm_dispatches[0]["event_type"] == "TaskAssignedEvent"
        assert pm_dispatches[1]["event_type"] == "TaskFinishedEvent"
        assert pm_dispatches[1]["task_id"] == "subtask-1"
        # Coder should have 1 dispatch: TaskAssignedEvent for subtask-1
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 1
        assert coder_dispatches[0]["event_type"] == "TaskAssignedEvent"


# =============================================================================
# Defensive guard: task not found in _run_agent_for_event
# =============================================================================


class TestTaskNotFound:
    @pytest.mark.asyncio
    async def test_event_for_missing_task_is_skipped(self):
        """When an event references a task_id that doesn't exist in the store,
        the orchestrator logs an error and skips (no dispatch recorded, no crash).

        In _run_agent_for_event, after the staleness check passes and agent is found,
        get_task(event.task_id) returns None, causing an early return.

        We simulate this by having the coder finish and delete the task reference,
        then manually enqueue an event for a non-existent task."""

        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # First dispatch: coder finishes the task, then enqueues an event for a
        # task that was never created (so get_task returns None).
        async def finish_and_enqueue_phantom():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")
            phantom_event = TaskAssignedEvent(task_id="nonexistent-task", target_id="coder-0", source_id="pm")
            registry.enqueue("coder-0", phantom_event)

        runner.side_effects["coder-0"] = [finish_and_enqueue_phantom]

        # Monkey-patch is_event_stale to let events for nonexistent tasks through.
        # Normally is_event_stale returns True for missing tasks, so we override
        # to return False for our phantom task to test the get_task guard.
        original_is_stale = store.is_event_stale

        async def permissive_staleness(event):
            if event.task_id == "nonexistent-task":
                return False  # Let phantom event through
            return await original_is_stale(event)

        store.is_event_stale = permissive_staleness

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        # The run should complete (task-1 succeeded)
        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert orch.outcome == "success"
        # coder-0 should have exactly 1 completed dispatch (the successful one).
        # The phantom event was skipped due to task not found — no dispatch recorded.
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 1


# =============================================================================
# Defensive guard: agent not found
# =============================================================================


class TestAgentNotFound:
    @pytest.mark.asyncio
    async def test_event_for_unknown_agent_is_skipped(self):
        """When an event is processed for an agent_id that doesn't exist in the
        registry (get_agent_snapshot returns None), the orchestrator logs a
        warning and skips. No dispatch recorded, no crash.

        We test this by directly calling _run_agent_for_event with a
        non-existent agent_id. We must also patch is_event_stale to return
        False, since the staleness check would otherwise catch the mismatch
        between assigned_to and target_id before reaching the agent guard."""

        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Create a task in the store so get_task succeeds
        task = _make_task()
        await store.create(task, assigned_to="coder-0", assigned_by="pm")

        # Patch is_event_stale to let the event through so we reach the agent guard
        original_is_stale = store.is_event_stale

        async def permissive_staleness(event):
            if event.target_id == "ghost-agent":
                return False
            return await original_is_stale(event)

        store.is_event_stale = permissive_staleness

        # Call _run_agent_for_event directly with an agent not in the registry
        event = TaskAssignedEvent(task_id="task-1", target_id="ghost-agent", source_id="pm")
        # This should not raise — it logs a warning and returns
        await orch._run_agent_for_event("ghost-agent", event)

        # No dispatches should be recorded for the ghost agent
        ghost_dispatches = get_agent_dispatches(db, "ghost-agent")
        assert len(ghost_dispatches) == 0


# =============================================================================
# CancelledError handling
# =============================================================================


class TestCancelledError:
    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_gracefully(self):
        """When an agent's run_agent raises asyncio.CancelledError (e.g., during
        shutdown), the orchestrator re-raises it (line 368-369), which terminates
        the agent's asyncio task. This is the expected cancellation path.

        We verify that the orchestrator handles this without crashing — the
        agent is force-cancelled and the run completes via quiescence."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db, global_timeout=3.0)

        # Override run_agent to raise CancelledError for coder-0
        original_run = runner.run_agent

        async def cancelling_run(agent_id, system_prompt, prompt):
            if agent_id == "coder-0":
                runner.calls.append((agent_id, system_prompt, prompt))
                raise asyncio.CancelledError()
            return await original_run(agent_id, system_prompt, prompt)

        runner.run_agent = cancelling_run

        task = _make_task()
        # The CancelledError in the agent loop causes the agent task to be
        # cancelled. The remaining agents go quiescent (no events) and the
        # run completes without crashing.
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        # The agent had no session (CancelledError on first call before any
        # ResultMessage), so the CancelledError handler treats it as
        # unresumable and marks the task FAILED.
        stored_task = await store.get_task("task-1")
        assert stored_task.status == TaskStatus.FAILED

        # The run completes via quiescence (no other agents have events).
        assert orch.outcome == "failure"


# =============================================================================
# Synchronized mode
# =============================================================================


class TestSynchronizedMode:
    @pytest.mark.asyncio
    async def test_sync_single_task_completes(self):
        """A single task in sync mode should complete in one round."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish_task():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish_task]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")], sync=True, sync_max_rounds=10)

        assert orch.outcome == "success"
        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert orch.sync_rounds == 1
        assert not orch.timed_out

    @pytest.mark.asyncio
    async def test_sync_multi_round_delegation(self):
        """PM assigns task to coder in round 1, coder finishes in round 2."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        async def pm_delegates():
            subtask = _make_task(id="sub-1", title="Subtask")
            await store.create(subtask, assigned_to="coder-0", assigned_by="pm")
            await store.mark_finished("pm-task", TaskStatus.SUCCEEDED, "delegated")

        async def coder_finishes():
            await store.mark_finished("sub-1", TaskStatus.SUCCEEDED, "done")

        async def pm_noop():
            pass

        runner.side_effects["pm"] = [pm_delegates, pm_noop]
        runner.side_effects["coder-0"] = [coder_finishes]

        pm_task = _make_task(id="pm-task", title="PM Task")
        await orch.run(initial_tasks=[(pm_task, "pm", "User")], sync=True, sync_max_rounds=10)

        assert orch.outcome == "success"
        assert get_run_meta(db)["tasks_succeeded"] == 2
        assert orch.sync_rounds >= 2

    @pytest.mark.asyncio
    async def test_sync_converges_when_no_events(self):
        """Sync mode stops when no events are left to process."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish_task():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish_task]

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")], sync=True, sync_max_rounds=100)

        assert orch.outcome == "success"
        # Task finishes in round 1, round 2 finds no events and exits
        assert orch.sync_rounds <= 2

    @pytest.mark.asyncio
    async def test_sync_max_rounds_reached(self):
        """Sync mode stops at sync_max_rounds even if tasks aren't done."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Agent never finishes the task — just creates events each round
        call_count = 0

        async def create_busywork():
            nonlocal call_count
            call_count += 1
            # Create a new task each time to generate events for next round
            new_task = _make_task(id=f"busy-{call_count}", title=f"Busy {call_count}")
            await store.create(new_task, assigned_to="coder-0", assigned_by="coder-0")

        runner.side_effects["coder-0"] = [create_busywork] * 5

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")], sync=True, sync_max_rounds=3)

        assert orch.sync_rounds == 3
        # Original task was never finished
        assert get_run_meta(db)["tasks_succeeded"] == 0

    @pytest.mark.asyncio
    async def test_sync_no_initial_tasks(self):
        """Sync mode with no initial tasks completes immediately."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        await orch.run(initial_tasks=[], sync=True, sync_max_rounds=10)
        assert orch.outcome == "no_work"
        assert orch.sync_rounds is None  # No rounds executed

    @pytest.mark.asyncio
    async def test_sync_timeout(self):
        """Sync mode respects global timeout."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db, global_timeout=1.0)

        # Agent sleeps longer than the timeout
        original_run = runner.run_agent

        async def slow_run(agent_id, system_prompt, prompt):
            runner.calls.append((agent_id, system_prompt, prompt))
            await asyncio.sleep(10.0)
            return await original_run(agent_id, system_prompt, prompt)

        runner.run_agent = slow_run

        task = _make_task()
        await orch.run(initial_tasks=[(task, "coder-0", "User")], sync=True, sync_max_rounds=10)

        assert orch.timed_out
        assert orch.outcome == "timeout"

    @pytest.mark.asyncio
    async def test_sync_sequential_events_per_agent(self):
        """Multiple events for one agent in a round are processed sequentially."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # PM creates two subtasks for coder-0 in round 1
        async def pm_creates_two():
            t1 = _make_task(id="sub-1", title="Sub 1")
            t2 = _make_task(id="sub-2", title="Sub 2")
            await store.create(t1, assigned_to="coder-0", assigned_by="pm")
            await store.create(t2, assigned_to="coder-0", assigned_by="pm")
            await store.mark_finished("pm-task", TaskStatus.SUCCEEDED, "delegated")

        async def coder_finishes_sub1():
            await store.mark_finished("sub-1", TaskStatus.SUCCEEDED, "done 1")

        async def coder_finishes_sub2():
            await store.mark_finished("sub-2", TaskStatus.SUCCEEDED, "done 2")

        async def pm_noop():
            pass

        runner.side_effects["pm"] = [pm_creates_two, pm_noop, pm_noop]
        runner.side_effects["coder-0"] = [coder_finishes_sub1, coder_finishes_sub2]

        pm_task = _make_task(id="pm-task", title="PM Task")
        await orch.run(initial_tasks=[(pm_task, "pm", "User")], sync=True, sync_max_rounds=10)

        assert get_run_meta(db)["tasks_succeeded"] == 3
        assert orch.outcome == "success"
        # Coder should have 2 dispatches (one per subtask)
        coder_dispatches = get_agent_dispatches(db, "coder-0")
        assert len(coder_dispatches) == 2

    @pytest.mark.asyncio
    async def test_sync_agent_failure_doesnt_block_others(self):
        """One agent failing doesn't prevent other agents from completing."""
        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
            ),
        }
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "coder-1": AgentConfig(agent_id="coder-1", role="coder"),
        }
        store, registry, runner, db = _make_org(roles=roles, agents=agents)
        orch = _make_orchestrator(store, registry, runner, db)

        runner.fail_agents.add("coder-0")

        async def finish_t2():
            await store.mark_finished("t2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-1"] = [finish_t2]

        t1 = _make_task(id="t1", title="Task 1")
        t2 = _make_task(id="t2", title="Task 2")
        await orch.run(initial_tasks=[(t1, "coder-0", "User"), (t2, "coder-1", "User")], sync=True, sync_max_rounds=10)

        assert get_run_meta(db)["tasks_succeeded"] == 1
        assert get_run_meta(db)["tasks_failed"] == 1
        assert orch.outcome == "partial"

    @pytest.mark.asyncio
    async def test_sync_round_timeout_cancels_slow_agent(self):
        """A round timeout should cancel a slow agent and leave its task unfinished."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db, global_timeout=30.0)

        # Agent's side_effect sleeps longer than the round timeout,
        # so it never reaches mark_finished.
        async def slow_then_finish():
            await asyncio.sleep(5)
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [slow_then_finish]

        task = _make_task()
        await orch.run(
            initial_tasks=[(task, "coder-0", "User")],
            sync=True,
            sync_max_rounds=10,
            sync_round_timeout_seconds=0.5,
        )

        # The round timed out, so the agent never finished the task
        t = await store.get_task("task-1")
        assert t.status != TaskStatus.SUCCEEDED

        # Run did complete (not a global timeout)
        assert not orch.timed_out
        # At least one round was executed
        assert orch.sync_rounds >= 1
        # Task was never succeeded
        assert get_run_meta(db)["tasks_succeeded"] == 0

    @pytest.mark.asyncio
    async def test_sync_rounds_none_for_async(self):
        """sync_rounds should be None for async runs."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        async def finish():
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish]

        await orch.run(initial_tasks=[(_make_task(id="t1"), "coder-0", "User")])
        assert orch.sync_rounds is None


# =============================================================================
# Synchronized mode validation
# =============================================================================


class TestEventListenerFanOut:
    @pytest.mark.asyncio
    async def test_event_listener_receives_task_events(self):
        """External event listeners receive task store events."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        received = []
        orch.add_event_listener(lambda e: received.append(e))

        async def finish_task():
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish_task]

        task = Task(id="t1", title="Test", description="Test task")
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        assert len(received) > 0
        assert any(hasattr(e, "task_id") and e.task_id == "t1" for e in received)

    @pytest.mark.asyncio
    async def test_event_listener_does_not_break_enqueue(self):
        """A failing listener must not prevent events from reaching agents."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        def bad_listener(e):
            raise RuntimeError("boom")

        orch.add_event_listener(bad_listener)

        async def finish_task():
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["coder-0"] = [finish_task]

        task = Task(id="t1", title="Test", description="Test task")
        await orch.run(initial_tasks=[(task, "coder-0", "User")])

        # The run should still complete despite the bad listener
        assert orch.outcome == "success"


class TestSyncValidation:
    @pytest.mark.asyncio
    async def test_max_rounds_without_sync_raises(self):
        """sync_max_rounds without sync=True should raise ValueError."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        with pytest.raises(ValueError, match="sync_max_rounds can only be specified when sync=True"):
            await orch.run(initial_tasks=[], sync_max_rounds=10)

    @pytest.mark.asyncio
    async def test_sync_without_max_rounds_raises(self):
        """sync=True without sync_max_rounds should raise ValueError."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        with pytest.raises(ValueError, match="sync_max_rounds is required when sync=True"):
            await orch.run(initial_tasks=[], sync=True)

    @pytest.mark.asyncio
    async def test_round_timeout_without_sync_raises(self):
        """sync_round_timeout_seconds without sync=True should raise ValueError."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        with pytest.raises(ValueError, match="sync_round_timeout_seconds can only be specified when sync=True"):
            await orch.run(initial_tasks=[], sync_round_timeout_seconds=60.0)


# =============================================================================
# MCPEvent dispatch
# =============================================================================


class TestMCPEventDispatch:
    @pytest.mark.asyncio
    async def test_mcp_event_dispatches_to_agent(self):
        """An MCPEvent enqueued to an agent should trigger a run with the payload as prompt."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Enqueue an MCPEvent directly and run a sync round
        mcp_event = MCPEvent(target_id="coder-0", server_name="market", payload="Price alert: ACME at $50")
        orch._dispatch_event(mcp_event)

        await orch.run(sync=True, sync_max_rounds=1)

        # Verify the agent was called with the payload as the prompt
        coder_calls = [c for c in runner.calls if c[0] == "coder-0"]
        assert len(coder_calls) == 1
        assert coder_calls[0][2] == "Price alert: ACME at $50"

    @pytest.mark.asyncio
    async def test_mcp_event_logged_to_db(self):
        """MCPEvent should be recorded in run_events with correct fields."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        mcp_event = MCPEvent(target_id="coder-0", server_name="market", payload="alert")
        orch._dispatch_event(mcp_event)

        await orch.run(sync=True, sync_max_rounds=1)

        dispatches = get_agent_dispatches(db, "coder-0")
        assert len(dispatches) == 1
        assert dispatches[0]["event_type"] == "MCPEvent"
        assert dispatches[0]["task_id"] is None

    @pytest.mark.asyncio
    async def test_mcp_event_error_does_not_fail_task(self):
        """An agent error during MCPEvent processing should not trigger task failure."""
        store, registry, runner, db = _make_org()
        orch = _make_orchestrator(store, registry, runner, db)

        # Create a task and let it succeed normally
        task = _make_task()
        await store.create(task, assigned_to="coder-0", assigned_by="pm")

        # Only fail the second call (the MCPEvent), not the first (TaskAssigned)
        call_count = [0]
        original_run = runner.run_agent

        async def selective_fail(agent_id, system_prompt, prompt):
            call_count[0] += 1
            if agent_id == "coder-0" and call_count[0] > 1:
                return AgentRunResult(error="Agent crashed", num_turns=1, cost_usd=0.01)
            return await original_run(agent_id, system_prompt, prompt)

        runner.run_agent = selective_fail

        # Enqueue MCPEvent — will be processed in round 2 (task event in round 1)
        async def emit_mcp():
            orch._dispatch_event(MCPEvent(target_id="coder-0", server_name="market", payload="alert"))

        runner.side_effects["coder-0"] = [emit_mcp]

        await orch.run(sync=True, sync_max_rounds=3)

        # Task should remain IN_PROGRESS — MCPEvent errors have no task_id,
        # so _handle_task_failure is not called.
        t = await store.get_task("task-1")
        assert t.status == TaskStatus.IN_PROGRESS

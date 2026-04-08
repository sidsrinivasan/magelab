"""Integration tests for the Orchestrator — exercises real paths through
orchestration, DB persistence, and resume.

Uses MockRunner to avoid Claude API calls while exercising the real
TaskStore, Registry, Database, and WireStore code paths.
"""

import asyncio
import logging
from unittest.mock import patch

import pytest
import yaml

from magelab.org_config import OrgConfig, ResumeMode
from magelab.registry_config import AgentConfig, RoleConfig
from magelab.events import TaskAssignedEvent
from magelab.orchestrator import Orchestrator, RunOutcome
from magelab.state.task_schemas import Task, TaskStatus
from magelab.view import RunView

from .conftest import MockRunner, get_agent_dispatches, get_all_agent_dispatches, get_run_meta
from .helpers import make_orch_org, make_orchestrator

_test_logger = logging.getLogger("test")

# =============================================================================
# Helpers
# =============================================================================


def _make_org(roles=None, agents=None, tmp_dir=None):
    store, registry, db = make_orch_org(roles, agents, tmp_dir)
    runner = MockRunner()
    return store, registry, runner, db


def _make_orchestrator(store, registry, runner, db, global_timeout=30.0, org_prompt="Test org"):
    return make_orchestrator(store, registry, runner, db, global_timeout, org_prompt)


def _make_task(id="task-1", title="Test Task", description="Do something", review_required=False):
    return Task(id=id, title=title, description=description, review_required=review_required)


def _finish_task(store, task_id):
    """Return an async side effect that marks a task as succeeded."""

    async def finish():
        await store.mark_finished(task_id, TaskStatus.SUCCEEDED, "done")

    return finish


def write_config(tmp_path, name="test_org", global_timeout=5):
    config = {
        "settings": {
            "org_name": name,
            "org_prompt": "Test org",
            "org_timeout_seconds": global_timeout,
        },
        "roles": {
            "worker": {
                "name": "worker",
                "role_prompt": "Work.",
                "tools": ["worker"],
                "model": "test",
                "max_turns": 10,
            }
        },
        "agents": {
            "worker-0": {"agent_id": "worker-0", "role": "worker"},
        },
        "initial_tasks": [
            {
                "id": "task-1",
                "title": "Test Task",
                "description": "Do something",
                "assigned_to": "worker-0",
            }
        ],
    }
    path = tmp_path / f"{name}.yaml"
    with open(path, "w") as f:
        yaml.dump(config, f)
    return str(path)


# =============================================================================
# TestDBPersistence — Verify data is persisted to DB after a run
# =============================================================================


class TestDBPersistence:
    @pytest.mark.asyncio
    async def test_run_meta_outcome_success(self, tmp_path):
        """run_meta has outcome='success' after a successful run."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])

        meta = get_run_meta(db)
        assert meta["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_run_meta_has_timing(self, tmp_path):
        """run_meta records start_time, end_time, and duration after a run."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])

        meta = get_run_meta(db)
        assert meta["start_time"] is not None
        assert meta["end_time"] is not None
        assert meta["duration_seconds"] >= 0.0

    @pytest.mark.asyncio
    async def test_run_meta_task_counts_single_success(self, tmp_path):
        """run_meta has tasks_succeeded=1, tasks_failed=0 for one successful task."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])

        meta = get_run_meta(db)
        assert meta["tasks_succeeded"] == 1
        assert meta["tasks_failed"] == 0

    @pytest.mark.asyncio
    async def test_run_meta_task_counts_failed_task(self, tmp_path):
        """run_meta shows tasks_failed=1 when an agent fails."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.fail_agents.add("coder-0")
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])

        meta = get_run_meta(db)
        assert meta["tasks_failed"] == 1

    @pytest.mark.asyncio
    async def test_events_table_has_dispatches(self, tmp_path):
        """events table records dispatches for the agent that ran."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])

        dispatches = get_agent_dispatches(db, "coder-0")
        assert len(dispatches) >= 1

    @pytest.mark.asyncio
    async def test_run_meta_multiple_tasks(self, tmp_path):
        """run_meta tracks both tasks when two succeed (one per agent)."""
        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code.", tools=["worker", "claude_basic"], model="test", max_turns=10
            ),
        }
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "coder-1": AgentConfig(agent_id="coder-1", role="coder"),
        }
        store, registry, runner, db = _make_org(roles=roles, agents=agents, tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        runner.side_effects["coder-1"] = [_finish_task(store, "task-2")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(
            initial_tasks=[
                (_make_task(id="task-1", title="Task A"), "coder-0", "User"),
                (_make_task(id="task-2", title="Task B"), "coder-1", "User"),
            ]
        )

        meta = get_run_meta(db)
        assert meta["tasks_succeeded"] == 2


# =============================================================================
# TestResumeFromDB — Verify resume behavior (CONTINUE and FRESH modes)
# =============================================================================


async def _build_and_run_with_hang(config_path, output_dir, runner_instance):
    """Build orchestrator and run with initial_tasks from config. Returns (orch, org_config)."""
    org_config = OrgConfig.from_yaml(config_path)
    with patch("magelab.orchestrator.ClaudeRunner", return_value=runner_instance):
        orch = await Orchestrator.build(org_config, output_dir, resume_mode=None)
    return orch, org_config


class TestResumeFromDB:
    @pytest.mark.asyncio
    async def test_resume_continue_task_still_exists(self, tmp_path):
        """CONTINUE resume: in-progress task from Run 1 is still in the store."""
        config_path = write_config(tmp_path, name="test_org", global_timeout=2)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        # Run 1: agent hangs → timeout with task still in-progress
        runner1 = MockRunner()
        runner1.side_effects["worker-0"] = [lambda: asyncio.sleep(60)]
        orch1, org_config = await _build_and_run_with_hang(config_path, output_dir, runner1)
        await orch1.run(initial_tasks=org_config.initial_tasks)
        assert orch1.outcome == RunOutcome.TIMEOUT
        orch1._db.close()

        # Run 2: resume CONTINUE — agent had no session (hung on first call),
        # so CancelledError handler marked task FAILED (unresumable).
        runner2 = MockRunner()
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner2):
            orch2 = await Orchestrator.build(
                OrgConfig.from_yaml(config_path), output_dir, resume_mode=ResumeMode.CONTINUE
            )
        tasks = await orch2.task_store.list_tasks()
        assert any(t.id == "task-1" for t in tasks)
        task = next(t for t in tasks if t.id == "task-1")
        assert task.status == TaskStatus.FAILED, f"No-session cancel should mark task FAILED, got {task.status}"
        orch2._db.close()

    @pytest.mark.asyncio
    async def test_resume_fresh_fails_in_progress_tasks(self, tmp_path):
        """FRESH resume: in-progress task from Run 1 is marked FAILED."""
        config_path = write_config(tmp_path, name="test_org", global_timeout=2)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        runner1 = MockRunner()
        runner1.side_effects["worker-0"] = [lambda: asyncio.sleep(60)]
        orch1, org_config = await _build_and_run_with_hang(config_path, output_dir, runner1)
        await orch1.run(initial_tasks=org_config.initial_tasks)
        orch1._db.close()

        runner2 = MockRunner()
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner2):
            orch2 = await Orchestrator.build(OrgConfig.from_yaml(config_path), output_dir, resume_mode=ResumeMode.FRESH)
        task = await orch2.task_store.get_task("task-1")
        assert task.status == TaskStatus.FAILED
        orch2._db.close()

    @pytest.mark.asyncio
    async def test_resume_no_db_raises(self, tmp_path):
        """Orchestrator.build with resume_mode but no DB raises RuntimeError."""
        config_path = write_config(tmp_path, name="test_org")
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        runner = MockRunner()
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner):
            with pytest.raises(RuntimeError, match="Cannot resume"):
                await Orchestrator.build(OrgConfig.from_yaml(config_path), output_dir, resume_mode=ResumeMode.CONTINUE)

    @pytest.mark.asyncio
    async def test_resume_continue_can_complete(self, tmp_path):
        """CONTINUE resume: second run can complete successfully.

        The first run's agent has a pre-seeded session (simulating one
        completed turn), then hangs on the second call → timeout. The
        CancelledError handler sees the session and keeps the agent WORKING,
        so resume-continue dispatches a ResumeEvent in Run 2.
        """
        config_path = write_config(tmp_path, name="test_org", global_timeout=2)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        # Run 1: agent has a session (prior turn completed), then hangs → timeout
        runner1 = MockRunner()
        runner1._sessions["worker-0"] = "fake-session-id"
        runner1.side_effects["worker-0"] = [lambda: asyncio.sleep(60)]
        orch1, org_config = await _build_and_run_with_hang(config_path, output_dir, runner1)
        await orch1.run(initial_tasks=org_config.initial_tasks)
        orch1._db.close()

        # Run 2: resume CONTINUE, finish the task this time
        runner2 = MockRunner()
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner2):
            orch2 = await Orchestrator.build(
                OrgConfig.from_yaml(config_path), output_dir, resume_mode=ResumeMode.CONTINUE
            )

        async def finish():
            await orch2.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner2.side_effects["worker-0"] = [finish]
        await orch2.run()
        assert orch2.outcome == RunOutcome.SUCCESS
        orch2._db.close()

    @pytest.mark.asyncio
    async def test_resume_continue_dispatches_resume_event(self, tmp_path):
        """CONTINUE resume dispatches a ResumeEvent that the agent processes.

        Verifies the full resume_continue -> run() path: the ResumeEvent is
        created by hydration, delivered to the agent, and the agent's side
        effect finishes the task leading to RunOutcome.SUCCESS. Asserts on
        the DB event record and the prompt content sent to the runner.
        """
        config_path = write_config(tmp_path, name="test_org", global_timeout=2)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        # Run 1: agent has a session (prior turn), then hangs → timeout
        runner1 = MockRunner()
        runner1._sessions["worker-0"] = "fake-session-id"
        runner1.side_effects["worker-0"] = [lambda: asyncio.sleep(60)]
        orch1, org_config = await _build_and_run_with_hang(config_path, output_dir, runner1)
        await orch1.run(initial_tasks=org_config.initial_tasks)
        assert orch1.outcome == RunOutcome.TIMEOUT
        orch1._db.close()

        # Run 2: resume CONTINUE, then run() — agent finishes the task
        runner2 = MockRunner()
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner2):
            orch2 = await Orchestrator.build(
                OrgConfig.from_yaml(config_path), output_dir, resume_mode=ResumeMode.CONTINUE
            )

        async def finish():
            await orch2.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner2.side_effects["worker-0"] = [finish]
        await orch2.run()

        # The run succeeded because the agent processed the ResumeEvent
        assert orch2.outcome == RunOutcome.SUCCESS

        # The task is SUCCEEDED
        task = await orch2.task_store.get_task("task-1")
        assert task.status == TaskStatus.SUCCEEDED

        # The runner received a call in Run 2 with the resume prompt
        assert len(runner2.calls) >= 1
        _, _, prompt = runner2.calls[0]
        assert "interrupted" in prompt.lower() or "continue" in prompt.lower()

        # The DB records a completed ResumeEvent dispatch for worker-0
        dispatches = get_all_agent_dispatches(orch2._db, "worker-0")
        resume_dispatches = [d for d in dispatches if d["event_type"] == "ResumeEvent"]
        assert len(resume_dispatches) >= 1
        assert resume_dispatches[0]["outcome"] == "completed"

        orch2._db.close()


# =============================================================================
# TestEventListeners — Verify events flow to external listeners
# =============================================================================


class TestEventListeners:
    @pytest.mark.asyncio
    async def test_listener_receives_events(self, tmp_path):
        """A registered listener receives events when the orchestrator runs."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)

        received = []
        orch.add_event_listener(lambda e: received.append(e))
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])
        assert len(received) > 0

    @pytest.mark.asyncio
    async def test_listener_receives_task_assigned(self, tmp_path):
        """A registered listener receives at least one TaskAssignedEvent."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)

        assigned = []
        orch.add_event_listener(lambda e: isinstance(e, TaskAssignedEvent) and assigned.append(e))
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])
        assert len(assigned) >= 1

    @pytest.mark.asyncio
    async def test_multiple_listeners(self, tmp_path):
        """Multiple listeners all receive the same events."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)

        events_a, events_b = [], []
        orch.add_event_listener(lambda e: events_a.append(e))
        orch.add_event_listener(lambda e: events_b.append(e))
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])
        assert len(events_a) > 0
        assert len(events_a) == len(events_b)


# =============================================================================
# TestCostAccumulation — Verify cost tracking
# =============================================================================


class TestCostAccumulation:
    @pytest.mark.asyncio
    async def test_total_cost_positive(self, tmp_path):
        """total_cost_usd > 0 after a run (MockRunner returns 0.05 per dispatch)."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])
        assert orch.total_cost_usd > 0.0

    @pytest.mark.asyncio
    async def test_run_meta_reflects_cost(self, tmp_path):
        """run_meta.total_cost_usd matches orchestrator.total_cost_usd."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])

        meta = get_run_meta(db)
        assert abs(float(meta["total_cost_usd"]) - orch.total_cost_usd) < 1e-6

    @pytest.mark.asyncio
    async def test_cost_at_least_one_dispatch(self, tmp_path):
        """total_cost_usd >= 0.05 for one dispatch (MockRunner default)."""
        store, registry, runner, db = _make_org(tmp_dir=tmp_path)
        runner.side_effects["coder-0"] = [_finish_task(store, "task-1")]
        orch = _make_orchestrator(store, registry, runner, db)
        await orch.run(initial_tasks=[(_make_task(), "coder-0", "User")])
        assert orch.total_cost_usd >= 0.05


# =============================================================================
# TestRestoreRunResults — Verify view-only restore
# =============================================================================


class TestRestoreRunResults:
    @pytest.mark.asyncio
    async def test_restore_outcome(self, tmp_path):
        """RunView: restored outcome matches the persisted run."""
        config_path = write_config(tmp_path, name="test_org", global_timeout=10)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        runner1 = MockRunner()
        org_config = OrgConfig.from_yaml(config_path)
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner1):
            orch1 = await Orchestrator.build(org_config, output_dir, resume_mode=None)

        async def finish():
            await orch1.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner1.side_effects["worker-0"] = [finish]
        await orch1.run(initial_tasks=org_config.initial_tasks)
        original_outcome = orch1.outcome

        db_path = output_dir / "test_org.db"
        view = RunView.from_db(db_path)
        assert view.outcome == original_outcome
        view.close()

    @pytest.mark.asyncio
    async def test_restore_cost(self, tmp_path):
        """RunView: restored cost matches the persisted run."""
        config_path = write_config(tmp_path, name="test_org", global_timeout=10)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        runner1 = MockRunner()
        org_config = OrgConfig.from_yaml(config_path)
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner1):
            orch1 = await Orchestrator.build(org_config, output_dir, resume_mode=None)

        async def finish():
            await orch1.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner1.side_effects["worker-0"] = [finish]
        await orch1.run(initial_tasks=org_config.initial_tasks)
        original_cost = orch1.total_cost_usd

        db_path = output_dir / "test_org.db"
        view = RunView.from_db(db_path)
        assert abs(view.total_cost_usd - original_cost) < 1e-6
        view.close()

    @pytest.mark.asyncio
    async def test_view_does_not_run_agents(self, tmp_path):
        """RunView: from_db does not construct a runner or dispatch agents."""
        config_path = write_config(tmp_path, name="test_org", global_timeout=10)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "workspace").mkdir()

        runner1 = MockRunner()
        org_config = OrgConfig.from_yaml(config_path)
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner1):
            orch1 = await Orchestrator.build(org_config, output_dir, resume_mode=None)

        async def finish():
            await orch1.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner1.side_effects["worker-0"] = [finish]
        await orch1.run(initial_tasks=org_config.initial_tasks)

        # RunView provides read-only access — no runner, no MCP, no sessions
        db_path = output_dir / "test_org.db"
        view = RunView.from_db(db_path)
        try:
            # No runner attribute on RunView (it's a frozen dataclass with known fields)
            assert not hasattr(view, "runner")
            # But stores ARE populated with data from the completed run
            tasks = await view.task_store.list_tasks()
            assert len(tasks) == 1
            assert tasks[0].status == TaskStatus.SUCCEEDED
            agents = view.registry.list_agent_snapshots()
            assert len(agents) > 0
        finally:
            view.close()

    @pytest.mark.asyncio
    async def test_view_no_db_raises(self, tmp_path):
        """RunView.from_db raises RuntimeError when no DB exists."""
        db_path = tmp_path / "nonexistent.db"

        with pytest.raises(RuntimeError, match="Cannot view"):
            RunView.from_db(db_path)

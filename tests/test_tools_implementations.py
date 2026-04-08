"""Tests for magelab.tools.implementations — Tool handler integration tests.

These tests use real TaskStore + Registry to test tool handlers end-to-end.
"""

import json
import logging

import pytest

from magelab.registry_config import AgentConfig, NetworkConfig, RoleConfig
from magelab.state.registry import Registry
from magelab.state.task_schemas import Task, TaskStatus
from magelab.state.task_store import TaskStore
from magelab.tools.implementations import create_tool_implementations
from magelab.tools.specs import FRAMEWORK
from magelab.state.wire_store import WireStore

_test_logger = logging.getLogger("test")


# =============================================================================
# Fixtures
# =============================================================================


def _setup(agent_id: str = "pm") -> tuple[TaskStore, Registry, dict]:
    """Create a TaskStore, Registry, and tool implementations for testing."""
    roles = {
        "pm": RoleConfig(name="pm", role_prompt="Manage", tools=["management"], model="test", max_turns=10),
        "coder": RoleConfig(
            name="coder", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
        ),
        "reviewer": RoleConfig(
            name="reviewer", role_prompt="Review", tools=["claude_reviewer"], model="test", max_turns=5
        ),
    }
    agents = {
        "pm": AgentConfig(agent_id="pm", role="pm"),
        "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
        "reviewer-0": AgentConfig(agent_id="reviewer-0", role="reviewer"),
    }
    store = TaskStore(framework_logger=_test_logger)
    registry = Registry(framework_logger=_test_logger)
    registry.register_config(roles, agents)
    impls = create_tool_implementations(store, registry, agent_id, wire_store=WireStore(framework_logger=_test_logger))
    return store, registry, impls


async def _create_task_via_store(
    store: TaskStore,
    task_id: str = "task-1",
    assigned_to: str = "coder-0",
    assigned_by: str = "pm",
    review_required: bool = False,
) -> Task:
    """Create a task directly via store for setup."""
    task = Task(id=task_id, title=f"Task {task_id}", description="Test task", review_required=review_required)
    return await store.create(task, assigned_to=assigned_to, assigned_by=assigned_by)


# =============================================================================
# tasks_create
# =============================================================================


class TestTasksCreate:
    @pytest.mark.asyncio
    async def test_create_basic(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create"]({"id": "t1", "title": "Test", "description": "Desc"})
        assert not result.is_error
        assert "t1" in result.text

    @pytest.mark.asyncio
    async def test_create_with_assignment(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create"](
            {"id": "t1", "title": "Test", "description": "Desc", "assigned_to": "coder-0"}
        )
        assert not result.is_error
        task = await store.get_task("t1")
        assert task.assigned_to == "coder-0"

    @pytest.mark.asyncio
    async def test_create_assign_to_unknown_agent(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create"](
            {"id": "t1", "title": "Test", "description": "Desc", "assigned_to": "ghost"}
        )
        assert result.is_error
        assert "not found" in result.text

    @pytest.mark.asyncio
    async def test_create_assign_to_terminated_agent(self):
        store, registry, impls = _setup("pm")
        registry.mark_terminated("coder-0")
        result = await impls["tasks_create"](
            {"id": "t1", "title": "Test", "description": "Desc", "assigned_to": "coder-0"}
        )
        assert result.is_error
        assert "terminated" in result.text

    @pytest.mark.asyncio
    async def test_create_missing_field(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create"]({"id": "t1", "title": "Test"})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_create_duplicate(self):
        store, registry, impls = _setup("pm")
        await impls["tasks_create"]({"id": "t1", "title": "Test", "description": "Desc"})
        result = await impls["tasks_create"]({"id": "t1", "title": "Test2", "description": "Desc2"})
        assert result.is_error
        assert "already exists" in result.text

    @pytest.mark.asyncio
    async def test_create_review_required_assignee_lacks_submit(self):
        """Assigning review-required task to agent without tasks_submit_for_review fails."""
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create"](
            {
                "id": "t1",
                "title": "Test",
                "description": "Desc",
                "assigned_to": "pm",
                "review_required": True,
            }
        )
        assert result.is_error
        assert "cannot submit" in result.text


# =============================================================================
# tasks_create_batch
# =============================================================================


class TestTasksCreateBatch:
    @pytest.mark.asyncio
    async def test_batch_basic(self):
        store, registry, impls = _setup("pm")
        tasks = [
            {"id": "t1", "title": "First", "description": "D1", "assigned_to": "coder-0"},
            {"id": "t2", "title": "Second", "description": "D2", "assigned_to": "coder-0"},
        ]
        result = await impls["tasks_create_batch"]({"tasks": tasks})
        assert "Created 2" in result.text
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_batch_json_string(self):
        store, registry, impls = _setup("pm")
        tasks_json = json.dumps([{"id": "t1", "title": "First", "description": "D1"}])
        result = await impls["tasks_create_batch"]({"tasks": tasks_json})
        assert "Created 1" in result.text

    @pytest.mark.asyncio
    async def test_batch_partial_errors(self):
        store, registry, impls = _setup("pm")
        tasks = [
            {"id": "t1", "title": "Good", "description": "D1"},
            {"id": "t2", "title": "Missing desc"},  # missing description
        ]
        result = await impls["tasks_create_batch"]({"tasks": tasks})
        assert "Created 1" in result.text
        assert "1 error" in result.text
        assert result.is_error

    @pytest.mark.asyncio
    async def test_batch_empty(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create_batch"]({"tasks": []})
        assert result.is_error
        assert "empty" in result.text

    @pytest.mark.asyncio
    async def test_batch_invalid_json(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create_batch"]({"tasks": "not valid json"})
        assert result.is_error
        assert "Error parsing" in result.text


# =============================================================================
# tasks_assign
# =============================================================================


class TestTasksAssign:
    @pytest.mark.asyncio
    async def test_assign_basic(self):
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        result = await impls["tasks_assign"]({"task_id": "t1", "to_agent": "reviewer-0"})
        assert not result.is_error
        task = await store.get_task("t1")
        assert task.assigned_to == "reviewer-0"

    @pytest.mark.asyncio
    async def test_assign_nonexistent_task(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_assign"]({"task_id": "ghost", "to_agent": "coder-0"})
        assert result.is_error

    @pytest.mark.asyncio
    async def test_assign_nonexistent_agent(self):
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1")
        result = await impls["tasks_assign"]({"task_id": "t1", "to_agent": "ghost"})
        assert result.is_error


# =============================================================================
# tasks_get
# =============================================================================


class TestTasksGet:
    @pytest.mark.asyncio
    async def test_get_existing(self):
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1")
        result = await impls["tasks_get"]({"task_id": "t1"})
        assert not result.is_error
        data = json.loads(result.text)
        assert data["id"] == "t1"

    @pytest.mark.asyncio
    async def test_get_not_found(self):
        store, registry, impls = _setup("pm")
        result = await impls["tasks_get"]({"task_id": "ghost"})
        assert result.is_error
        assert "not found" in result.text


# =============================================================================
# tasks_list
# =============================================================================


class TestTasksList:
    @pytest.mark.asyncio
    async def test_list_all(self):
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1")
        await _create_task_via_store(store, "t2")
        result = await impls["tasks_list"]({})
        assert not result.is_error
        data = json.loads(result.text)
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_list_filter_assigned_to(self):
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await _create_task_via_store(store, "t2", assigned_to="reviewer-0")
        result = await impls["tasks_list"]({"assigned_to": "coder-0"})
        data = json.loads(result.text)
        assert len(data) == 1
        assert data[0]["id"] == "t1"


# =============================================================================
# tasks_mark_finished
# =============================================================================


class TestTasksMarkFinished:
    @pytest.mark.asyncio
    async def test_mark_succeeded(self):
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        result = await impls["tasks_mark_finished"]({"task_id": "t1", "outcome": "succeeded", "details": "done"})
        assert not result.is_error
        assert "succeeded" in result.text

    @pytest.mark.asyncio
    async def test_mark_failed(self):
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        result = await impls["tasks_mark_finished"]({"task_id": "t1", "outcome": "failed", "details": "broken"})
        assert not result.is_error
        assert "failed" in result.text

    @pytest.mark.asyncio
    async def test_mark_invalid_outcome(self):
        store, registry, impls = _setup("coder-0")
        result = await impls["tasks_mark_finished"]({"task_id": "t1", "outcome": "bogus"})
        assert result.is_error
        assert "Invalid outcome" in result.text

    @pytest.mark.asyncio
    async def test_mark_in_progress_as_outcome(self):
        store, registry, impls = _setup("coder-0")
        result = await impls["tasks_mark_finished"]({"task_id": "t1", "outcome": "in_progress"})
        assert result.is_error
        assert "Invalid outcome" in result.text


# =============================================================================
# tasks_submit_for_review
# =============================================================================


class TestTasksSubmitForReview:
    @pytest.mark.asyncio
    async def test_submit_basic(self):
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-0": "Please review this"})
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        assert not result.is_error
        assert "reviewer-0" in result.text

    @pytest.mark.asyncio
    async def test_submit_invalid_json(self):
        store, registry, impls = _setup("coder-0")
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": "not json"})
        assert result.is_error
        assert "Error parsing" in result.text

    @pytest.mark.asyncio
    async def test_submit_empty_reviewers(self):
        store, registry, impls = _setup("coder-0")
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": "{}"})
        assert result.is_error
        assert "at least one reviewer" in result.text

    @pytest.mark.asyncio
    async def test_submit_nonexistent_reviewer(self):
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"ghost": "Review pls"})
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        assert result.is_error
        assert "not found" in result.text

    @pytest.mark.asyncio
    async def test_submit_reviewer_without_review_tool(self):
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        # PM doesn't have tasks_submit_review
        reviewers = json.dumps({"pm": "Review pls"})
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        assert result.is_error
        assert "not authorized" in result.text

    @pytest.mark.asyncio
    async def test_submit_invalid_policy(self):
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-0": "msg"})
        result = await impls["tasks_submit_for_review"](
            {"task_id": "t1", "reviewers": reviewers, "review_policy": "bogus"}
        )
        assert result.is_error
        assert "Invalid policy" in result.text


# =============================================================================
# tasks_submit_review
# =============================================================================


class TestTasksSubmitReview:
    @pytest.mark.asyncio
    async def test_submit_review_basic(self):
        store, registry, impls_coder = _setup("coder-0")
        impls_reviewer = create_tool_implementations(
            store, registry, "reviewer-0", wire_store=WireStore(framework_logger=_test_logger)
        )

        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        # Submit for review via coder tools
        reviewers = json.dumps({"reviewer-0": "Please check"})
        await impls_coder["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})

        # Submit review via reviewer tools
        result = await impls_reviewer["tasks_submit_review"](
            {"task_id": "t1", "decision": "approved", "comment": "LGTM"}
        )
        assert not result.is_error
        assert "approved" in result.text

    @pytest.mark.asyncio
    async def test_submit_review_invalid_decision(self):
        store, registry, impls = _setup("reviewer-0")
        result = await impls["tasks_submit_review"]({"task_id": "t1", "decision": "bogus"})
        assert result.is_error
        assert "Invalid decision" in result.text


# =============================================================================
# connections_list
# =============================================================================


class TestConnectionsList:
    @pytest.mark.asyncio
    async def test_excludes_caller(self):
        store, registry, impls = _setup("pm")
        result = await impls["connections_list"]({})
        assert not result.is_error
        data = json.loads(result.text)
        agent_ids = [e["agent_id"] for e in data]
        assert "pm" not in agent_ids
        assert "coder-0" in agent_ids
        assert "reviewer-0" in agent_ids


# =============================================================================
# get_available_reviewers
# =============================================================================


class TestGetAvailableReviewers:
    @pytest.mark.asyncio
    async def test_returns_reviewer_candidates(self):
        store, registry, impls = _setup("coder-0")
        result = await impls["get_available_reviewers"]({})
        assert not result.is_error
        data = json.loads(result.text)
        agent_ids = [e["agent_id"] for e in data]
        assert "reviewer-0" in agent_ids
        assert "coder-0" not in agent_ids  # excluded (caller)
        assert "pm" not in agent_ids  # excluded (no submit_review)

    @pytest.mark.asyncio
    async def test_no_reviewers_available(self):
        """When no peer has submit_review."""
        roles = {"solo": RoleConfig(name="solo", role_prompt="Solo", tools=["worker"], model="test", max_turns=10)}
        agents = {"solo-0": AgentConfig(agent_id="solo-0", role="solo")}
        store = TaskStore(framework_logger=_test_logger)
        registry = Registry(framework_logger=_test_logger)
        registry.register_config(roles, agents)
        impls = create_tool_implementations(
            store, registry, "solo-0", wire_store=WireStore(framework_logger=_test_logger)
        )
        result = await impls["get_available_reviewers"]({})
        assert "No reviewers" in result.text

    @pytest.mark.asyncio
    async def test_workload_stats(self):
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="reviewer-0", assigned_by="pm")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        result = await impls["get_available_reviewers"]({})
        data = json.loads(result.text)
        reviewer = [e for e in data if e["agent_id"] == "reviewer-0"][0]
        assert reviewer["completed_work"] == 1


# =============================================================================
# sleep
# =============================================================================


class TestSleep:
    @pytest.mark.asyncio
    async def test_sleep_zero(self):
        store, registry, impls = _setup("pm")
        result = await impls["sleep"]({"duration_seconds": 0})
        assert not result.is_error
        assert "0 seconds" in result.text

    @pytest.mark.asyncio
    async def test_sleep_invalid_duration(self):
        store, registry, impls = _setup("pm")
        result = await impls["sleep"]({"duration_seconds": 61})
        assert result.is_error
        assert "between 0 and 60" in result.text

    @pytest.mark.asyncio
    async def test_sleep_negative(self):
        store, registry, impls = _setup("pm")
        result = await impls["sleep"]({"duration_seconds": -1})
        assert result.is_error


# =============================================================================
# tasks_assign — review_required validation
# =============================================================================


class TestTasksAssignReviewRequired:
    @pytest.mark.asyncio
    async def test_assign_review_required_task(self):
        """Assigning review_required task to agent without submit_for_review fails;
        assigning to coder (who has submit_for_review via worker bundle) succeeds."""
        store, registry, impls = _setup("pm")
        # Create a review_required task (unassigned initially)
        result = await impls["tasks_create"](
            {"id": "t1", "title": "Review task", "description": "Needs review", "review_required": True}
        )
        assert not result.is_error

        # Try assigning to pm — pm has management bundle which lacks tasks_submit_for_review
        result = await impls["tasks_assign"]({"task_id": "t1", "to_agent": "pm"})
        assert result.is_error
        assert "cannot submit" in result.text

        # Assign to coder-0 — coder has worker bundle which includes tasks_submit_for_review
        result = await impls["tasks_assign"]({"task_id": "t1", "to_agent": "coder-0"})
        assert not result.is_error
        task = await store.get_task("t1")
        assert task.assigned_to == "coder-0"


# =============================================================================
# tasks_mark_finished — review gating
# =============================================================================


class TestTasksMarkFinishedReviewGating:
    @pytest.mark.asyncio
    async def test_mark_finished_review_gating(self):
        """A review_required task in IN_PROGRESS cannot be marked succeeded without approval."""
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0", review_required=True)
        await store.mark_in_progress("t1")

        # Try to mark succeeded — should fail because review is required and task is not approved
        result = await impls["tasks_mark_finished"]({"task_id": "t1", "outcome": "succeeded", "details": "done"})
        assert result.is_error
        assert "requires review" in result.text


# =============================================================================
# tasks_submit_for_review — terminated reviewer
# =============================================================================


class TestSubmitForReviewTerminatedReviewer:
    @pytest.mark.asyncio
    async def test_submit_for_review_terminated_reviewer(self):
        """Submitting for review with a terminated reviewer should fail."""
        store, registry, impls = _setup("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")

        # Terminate the reviewer
        registry.mark_terminated("reviewer-0")

        # Try to submit for review — should fail because reviewer is terminated
        reviewers = json.dumps({"reviewer-0": "Please review"})
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        assert result.is_error
        assert "terminated" in result.text


# =============================================================================
# tasks_list — combined filters
# =============================================================================


class TestTasksListCombinedFilters:
    @pytest.mark.asyncio
    async def test_list_combined_assigned_to_and_is_finished(self):
        """Filtering tasks_list by assigned_to and is_finished returns only matching tasks."""
        store, registry, impls = _setup("pm")

        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await _create_task_via_store(store, "t2", assigned_to="coder-0")
        await store.mark_in_progress("t2")
        await store.mark_finished("t2", TaskStatus.SUCCEEDED, "done")

        # Filter: assigned_to=coder-0 AND is_finished=false → only t1
        result = await impls["tasks_list"]({"assigned_to": "coder-0", "is_finished": False})
        assert not result.is_error
        data = json.loads(result.text)
        assert len(data) == 1
        assert data[0]["id"] == "t1"


# =============================================================================
# tasks_create_batch — invalid assignee
# =============================================================================


class TestTasksCreateBatchInvalidAssignee:
    @pytest.mark.asyncio
    async def test_batch_create_invalid_assignee(self):
        """Batch create with one valid and one invalid assignee produces partial failure."""
        store, registry, impls = _setup("pm")
        tasks = [
            {"id": "t1", "title": "Good task", "description": "Valid", "assigned_to": "coder-0"},
            {"id": "t2", "title": "Bad task", "description": "Invalid agent", "assigned_to": "nonexistent-agent"},
        ]
        result = await impls["tasks_create_batch"]({"tasks": tasks})
        assert result.is_error  # partial failure is an error
        assert "Created 1" in result.text
        assert "1 error" in result.text

        # Verify the valid task was created
        task = await store.get_task("t1")
        assert task is not None
        assert task.assigned_to == "coder-0"

        # Verify the invalid task was NOT created
        task = await store.get_task("t2")
        assert task is None


# =============================================================================
# connections_list — field inclusion/exclusion
# =============================================================================


class TestConnectionsListFields:
    @pytest.mark.asyncio
    async def test_connections_list_fields(self):
        """connections_list includes role and state but excludes role_prompt and tools."""
        store, registry, impls = _setup("pm")
        result = await impls["connections_list"]({})
        assert not result.is_error
        data = json.loads(result.text)

        # Should have entries for coder-0 and reviewer-0 (pm excluded as caller)
        assert len(data) == 2

        for entry in data:
            # Expected fields are present
            assert "agent_id" in entry
            assert "role" in entry
            assert "state" in entry

            # Internal fields are excluded
            assert "role_prompt" not in entry
            assert "tools" not in entry
            assert "max_turns" not in entry


# =============================================================================
# tasks_list — assigned_by filter
# =============================================================================


class TestTasksListFilterAssignedBy:
    @pytest.mark.asyncio
    async def test_list_filter_assigned_by(self):
        """Filter tasks_list by assigned_by returns tasks assigned by that agent."""
        store, registry, impls = _setup("pm")
        # Create a task: pm creates and assigns to coder-0, so assigned_by="pm"
        await _create_task_via_store(store, "t1", assigned_to="coder-0", assigned_by="pm")

        # Filter by assigned_by="pm" — should return t1
        result = await impls["tasks_list"]({"assigned_by": "pm"})
        assert not result.is_error
        data = json.loads(result.text)
        assert len(data) == 1
        assert data[0]["id"] == "t1"

        # Filter by assigned_by="nobody" — should return empty
        result = await impls["tasks_list"]({"assigned_by": "nobody"})
        assert not result.is_error
        data = json.loads(result.text)
        assert len(data) == 0


# =============================================================================
# tasks_list — is_finished filter
# =============================================================================


class TestTasksListFilterIsFinished:
    @pytest.mark.asyncio
    async def test_list_filter_is_finished(self):
        """Filter tasks_list by is_finished returns only finished/unfinished tasks."""
        store, registry, impls = _setup("pm")

        # Create two tasks
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await _create_task_via_store(store, "t2", assigned_to="coder-0")

        # Finish t1: mark in progress then mark succeeded
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        # Filter by is_finished=true — should return only t1
        result = await impls["tasks_list"]({"is_finished": True})
        assert not result.is_error
        data = json.loads(result.text)
        assert len(data) == 1
        assert data[0]["id"] == "t1"

        # Filter by is_finished=false — should return only t2
        result = await impls["tasks_list"]({"is_finished": False})
        assert not result.is_error
        data = json.loads(result.text)
        assert len(data) == 1
        assert data[0]["id"] == "t2"


# =============================================================================
# tasks_submit_review — changes_requested
# =============================================================================


class TestTasksSubmitReviewChangesRequested:
    @pytest.mark.asyncio
    async def test_submit_review_changes_requested(self):
        """Submitting a 'changes_requested' review updates task status appropriately."""
        store, registry, impls_coder = _setup("coder-0")
        impls_reviewer = create_tool_implementations(
            store, registry, "reviewer-0", wire_store=WireStore(framework_logger=_test_logger)
        )

        # Set up task submitted for review
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-0": "Please check"})
        await impls_coder["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})

        # Submit review with changes_requested
        result = await impls_reviewer["tasks_submit_review"](
            {"task_id": "t1", "decision": "changes_requested", "comment": "Needs work"}
        )
        assert not result.is_error
        assert "changes_requested" in result.text

        # Verify task status changed to changes_requested
        task = await store.get_task("t1")
        assert task.status == TaskStatus.CHANGES_REQUESTED


# =============================================================================
# tasks_submit_review — task not in review
# =============================================================================


class TestTasksSubmitReviewTaskNotInReview:
    @pytest.mark.asyncio
    async def test_submit_review_task_not_in_review(self):
        """Submitting a review for a task not in review returns an error."""
        store, registry, impls_reviewer = _setup("reviewer-0")

        # Create a task that is in progress (not submitted for review)
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")

        # Try to submit review — should fail because task is not in review
        result = await impls_reviewer["tasks_submit_review"]({"task_id": "t1", "decision": "approved"})
        assert result.is_error
        assert "not in a review round" in result.text


# =============================================================================
# sleep — positive duration
# =============================================================================


class TestSleepPositiveDuration:
    @pytest.mark.asyncio
    async def test_sleep_positive_duration(self):
        """Calling sleep with a positive duration actually sleeps and returns success."""
        store, registry, impls = _setup("pm")
        result = await impls["sleep"]({"duration_seconds": 0.01})
        assert not result.is_error
        assert "Slept" in result.text


# =============================================================================
# get_available_reviewers — workload stats
# =============================================================================


class TestGetAvailableReviewersWorkloadStats:
    @pytest.mark.asyncio
    async def test_get_available_reviewers_workload_stats(self):
        """get_available_reviewers includes correct completed_work and queued_tasks stats."""
        store, registry, impls = _setup("coder-0")

        # Task 1: assigned to reviewer-0, SUCCEEDED (should count as completed_work)
        await _create_task_via_store(store, "t1", assigned_to="reviewer-0", assigned_by="pm")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        # Task 2: assigned to reviewer-0, IN_PROGRESS (should count as assigned_work in queued_tasks)
        await _create_task_via_store(store, "t2", assigned_to="reviewer-0", assigned_by="pm")
        await store.mark_in_progress("t2")

        # Call get_available_reviewers
        result = await impls["get_available_reviewers"]({})
        assert not result.is_error
        data = json.loads(result.text)

        # Find reviewer-0 in results
        reviewer_entries = [e for e in data if e["agent_id"] == "reviewer-0"]
        assert len(reviewer_entries) == 1
        reviewer = reviewer_entries[0]

        # Verify completed_work includes the SUCCEEDED task
        assert reviewer["completed_work"] == 1

        # Verify queued_tasks includes the IN_PROGRESS task as assigned_work
        queued = reviewer["queued_tasks"]
        assert len(queued) == 1
        assert queued[0]["task_id"] == "t2"
        assert queued[0]["type"] == "assigned_work"

        # Verify other stats are present
        assert "completed_reviews" in reviewer
        assert "failed_work" in reviewer


# =============================================================================
# tasks_create_batch — duplicate IDs in same batch
# =============================================================================


class TestTasksCreateBatchDuplicateIds:
    @pytest.mark.asyncio
    async def test_batch_create_duplicate_ids(self):
        """Batch create with duplicate IDs: first succeeds, second errors (already exists)."""
        store, registry, impls = _setup("pm")
        tasks = [
            {"id": "dup-1", "title": "First", "description": "D1"},
            {"id": "dup-1", "title": "Duplicate", "description": "D2"},
        ]
        result = await impls["tasks_create_batch"]({"tasks": tasks})
        # Should have partial success: 1 created, 1 error
        assert result.is_error
        assert "Created 1" in result.text
        assert "1 error" in result.text
        assert "already exists" in result.text

        # Verify only one task exists and it's the first one
        task = await store.get_task("dup-1")
        assert task is not None
        assert task.title == "First"


# =============================================================================
# get_available_reviewers — comprehensive workload stats
# =============================================================================


class TestGetAvailableReviewersDetailedStats:
    @pytest.mark.asyncio
    async def test_failed_work_stat(self):
        """get_available_reviewers correctly counts failed_work for reviewer candidates."""
        store, registry, impls = _setup("coder-0")

        # Create a task assigned to reviewer-0 and mark it FAILED
        await _create_task_via_store(store, "t-fail", assigned_to="reviewer-0", assigned_by="pm")
        await store.mark_in_progress("t-fail")
        await store.mark_finished("t-fail", TaskStatus.FAILED, "broken")

        result = await impls["get_available_reviewers"]({})
        assert not result.is_error
        data = json.loads(result.text)
        reviewer = [e for e in data if e["agent_id"] == "reviewer-0"][0]
        assert reviewer["failed_work"] == 1
        assert reviewer["completed_work"] == 0

    @pytest.mark.asyncio
    async def test_completed_reviews_stat(self):
        """get_available_reviewers correctly counts completed_reviews from review_history."""
        store, registry, impls_coder = _setup("coder-0")
        impls_reviewer = create_tool_implementations(
            store, registry, "reviewer-0", wire_store=WireStore(framework_logger=_test_logger)
        )

        # Create a task, submit for review, have reviewer-0 approve it
        await _create_task_via_store(store, "t1", assigned_to="coder-0", assigned_by="pm")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-0": "Please review"})
        await impls_coder["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        await impls_reviewer["tasks_submit_review"]({"task_id": "t1", "decision": "approved", "comment": "Good"})

        # Now query available reviewers from coder-0's perspective
        result = await impls_coder["get_available_reviewers"]({})
        assert not result.is_error
        data = json.loads(result.text)
        reviewer = [e for e in data if e["agent_id"] == "reviewer-0"][0]
        assert reviewer["completed_reviews"] == 1

    @pytest.mark.asyncio
    async def test_pending_review_in_queued_tasks(self):
        """get_available_reviewers includes pending reviews as assigned_review in queued_tasks."""
        store, registry, impls_coder = _setup("coder-0")

        # Create a task submitted for review to reviewer-0 (but reviewer hasn't responded yet)
        await _create_task_via_store(store, "t1", assigned_to="coder-0", assigned_by="pm")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-0": "Please review"})
        await impls_coder["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})

        result = await impls_coder["get_available_reviewers"]({})
        assert not result.is_error
        data = json.loads(result.text)
        reviewer = [e for e in data if e["agent_id"] == "reviewer-0"][0]

        # Should have an assigned_review in queued_tasks
        review_tasks = [q for q in reviewer["queued_tasks"] if q["type"] == "assigned_review"]
        assert len(review_tasks) == 1
        assert review_tasks[0]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_combined_workload_stats(self):
        """get_available_reviewers returns correct counts across multiple task states."""
        store, registry, impls_coder = _setup("coder-0")
        impls_reviewer = create_tool_implementations(
            store, registry, "reviewer-0", wire_store=WireStore(framework_logger=_test_logger)
        )

        # Task 1: assigned to reviewer-0, SUCCEEDED
        await _create_task_via_store(store, "t1", assigned_to="reviewer-0", assigned_by="pm")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        # Task 2: assigned to reviewer-0, FAILED
        await _create_task_via_store(store, "t2", assigned_to="reviewer-0", assigned_by="pm")
        await store.mark_in_progress("t2")
        await store.mark_finished("t2", TaskStatus.FAILED, "broken")

        # Task 3: assigned to reviewer-0, IN_PROGRESS (queued work)
        await _create_task_via_store(store, "t3", assigned_to="reviewer-0", assigned_by="pm")
        await store.mark_in_progress("t3")

        # Task 4: assigned to coder-0, reviewer-0 reviewed it (completed review)
        await _create_task_via_store(store, "t4", assigned_to="coder-0", assigned_by="pm")
        await store.mark_in_progress("t4")
        reviewers = json.dumps({"reviewer-0": "Review please"})
        await impls_coder["tasks_submit_for_review"]({"task_id": "t4", "reviewers": reviewers})
        await impls_reviewer["tasks_submit_review"]({"task_id": "t4", "decision": "approved", "comment": "OK"})

        # Task 5: assigned to coder-0, pending review from reviewer-0
        await _create_task_via_store(store, "t5", assigned_to="coder-0", assigned_by="pm")
        await store.mark_in_progress("t5")
        reviewers = json.dumps({"reviewer-0": "Review this too"})
        await impls_coder["tasks_submit_for_review"]({"task_id": "t5", "reviewers": reviewers})

        result = await impls_coder["get_available_reviewers"]({})
        assert not result.is_error
        data = json.loads(result.text)
        reviewer = [e for e in data if e["agent_id"] == "reviewer-0"][0]

        assert reviewer["completed_work"] == 1
        assert reviewer["failed_work"] == 1
        assert reviewer["completed_reviews"] == 1

        # Queued tasks should include: t3 (assigned_work) + t5 (assigned_review)
        queued = reviewer["queued_tasks"]
        queued_ids = {q["task_id"] for q in queued}
        assert "t3" in queued_ids  # assigned_work (in progress)
        assert "t5" in queued_ids  # assigned_review (pending)
        assert len(queued) == 2


# =============================================================================
# _handle_errors paths — missing required fields
# =============================================================================


class TestMissingRequiredFields:
    @pytest.mark.asyncio
    async def test_tasks_create_missing_title(self):
        """tasks_create with missing 'title' returns Missing required field error."""
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create"]({"id": "t1", "description": "Desc"})
        assert result.is_error
        assert "Missing required field" in result.text
        assert "'title'" in result.text

    @pytest.mark.asyncio
    async def test_tasks_create_missing_description(self):
        """tasks_create with missing 'description' returns Missing required field error."""
        store, registry, impls = _setup("pm")
        result = await impls["tasks_create"]({"id": "t1", "title": "Test"})
        assert result.is_error
        assert "Missing required field" in result.text
        assert "'description'" in result.text

    @pytest.mark.asyncio
    async def test_tasks_assign_missing_task_id(self):
        """tasks_assign with missing 'task_id' returns Missing required field error."""
        store, registry, impls = _setup("pm")
        result = await impls["tasks_assign"]({"to_agent": "coder-0"})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_assign_missing_to_agent(self):
        """tasks_assign with missing 'to_agent' returns Missing required field error."""
        store, registry, impls = _setup("pm")
        result = await impls["tasks_assign"]({"task_id": "t1"})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_get_missing_task_id(self):
        """tasks_get with missing 'task_id' returns Missing required field error."""
        store, registry, impls = _setup("pm")
        result = await impls["tasks_get"]({})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_submit_for_review_missing_task_id(self):
        """tasks_submit_for_review with missing 'task_id' returns Missing required field error."""
        store, registry, impls = _setup("coder-0")
        reviewers = json.dumps({"reviewer-0": "Please review"})
        result = await impls["tasks_submit_for_review"]({"reviewers": reviewers})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_submit_for_review_missing_reviewers(self):
        """tasks_submit_for_review with missing 'reviewers' returns Missing required field error."""
        store, registry, impls = _setup("coder-0")
        result = await impls["tasks_submit_for_review"]({"task_id": "t1"})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_submit_review_missing_task_id(self):
        """tasks_submit_review with missing 'task_id' returns Missing required field error."""
        store, registry, impls = _setup("reviewer-0")
        result = await impls["tasks_submit_review"]({"decision": "approved"})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_mark_finished_missing_task_id(self):
        """tasks_mark_finished with missing 'task_id' returns Missing required field error.

        Note: 'outcome' is accessed first inside a try/except(ValueError), but KeyError
        from args['outcome'] propagates to _handle_errors. If outcome is provided but
        task_id is missing, KeyError for task_id propagates to _handle_errors.
        """
        store, registry, impls = _setup("coder-0")
        # Provide outcome but not task_id
        result = await impls["tasks_mark_finished"]({"outcome": "succeeded"})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_mark_finished_missing_outcome(self):
        """tasks_mark_finished with missing 'outcome' returns Missing required field error.

        The handler accesses args['outcome'] inside a try/except(ValueError) block,
        but KeyError is NOT a subclass of ValueError, so it propagates to _handle_errors.
        """
        store, registry, impls = _setup("coder-0")
        result = await impls["tasks_mark_finished"]({"task_id": "t1"})
        assert result.is_error
        assert "Missing required field" in result.text

    @pytest.mark.asyncio
    async def test_tasks_submit_review_missing_decision(self):
        """tasks_submit_review with missing 'decision' returns Invalid decision error.

        Note: the handler catches (ValueError, KeyError) together in an inner
        try/except, so missing 'decision' returns 'Invalid decision' rather than
        'Missing required field'.
        """
        store, registry, impls = _setup("reviewer-0")
        result = await impls["tasks_submit_review"]({"task_id": "t1"})
        assert result.is_error
        assert "Invalid decision" in result.text


# =============================================================================
# tasks_assign — to finished task
# =============================================================================


class TestTasksAssignToFinishedTask:
    @pytest.mark.asyncio
    async def test_assign_to_succeeded_task(self):
        """Assigning a task that has already SUCCEEDED returns an error."""
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        result = await impls["tasks_assign"]({"task_id": "t1", "to_agent": "reviewer-0"})
        assert result.is_error
        assert "finished" in result.text.lower()

    @pytest.mark.asyncio
    async def test_assign_to_failed_task(self):
        """Assigning a task that has already FAILED returns an error."""
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.FAILED, "broken")

        result = await impls["tasks_assign"]({"task_id": "t1", "to_agent": "reviewer-0"})
        assert result.is_error
        assert "finished" in result.text.lower()


# =============================================================================
# tasks_assign — to task under review
# =============================================================================


class TestTasksAssignToTaskUnderReview:
    @pytest.mark.asyncio
    async def test_assign_task_under_review(self):
        """Assigning a task that is UNDER_REVIEW returns an error."""
        store, registry, impls_pm = _setup("pm")
        impls_coder = create_tool_implementations(
            store, registry, "coder-0", wire_store=WireStore(framework_logger=_test_logger)
        )

        # Create task, mark in progress, submit for review
        await _create_task_via_store(store, "t1", assigned_to="coder-0")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-0": "Please review"})
        submit_result = await impls_coder["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        assert not submit_result.is_error

        # Verify task is under review
        task = await store.get_task("t1")
        assert task.status == TaskStatus.UNDER_REVIEW

        # Try to reassign — should fail because task is in review
        result = await impls_pm["tasks_assign"]({"task_id": "t1", "to_agent": "reviewer-0"})
        assert result.is_error
        assert "review" in result.text.lower()


# =============================================================================
# Spec/impl sync guard
# =============================================================================


class TestSpecImplSync:
    def test_all_specs_have_implementations(self):
        """create_tool_implementations should cover all FRAMEWORK specs."""
        # If this fails, create_tool_implementations would raise RuntimeError at init
        store, registry, impls = _setup("pm")
        assert set(impls.keys()) == set(FRAMEWORK.keys())


# =============================================================================
# Network topology gating
# =============================================================================


def _setup_with_network(agent_id: str = "pm") -> tuple[TaskStore, Registry, dict]:
    """Create setup with network topology: backend group [pm, coder-0, reviewer-0], frontend group [coder-1, reviewer-1]."""
    roles = {
        "pm": RoleConfig(name="pm", role_prompt="Manage", tools=["management"], model="test", max_turns=10),
        "coder": RoleConfig(
            name="coder", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
        ),
        "reviewer": RoleConfig(
            name="reviewer", role_prompt="Review", tools=["claude_reviewer"], model="test", max_turns=5
        ),
    }
    agents = {
        "pm": AgentConfig(agent_id="pm", role="pm"),
        "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
        "coder-1": AgentConfig(agent_id="coder-1", role="coder"),
        "reviewer-0": AgentConfig(agent_id="reviewer-0", role="reviewer"),
        "reviewer-1": AgentConfig(agent_id="reviewer-1", role="reviewer"),
    }
    network = NetworkConfig(
        **{
            "groups": {
                "backend": ["pm", "coder-0", "reviewer-0"],
                "frontend": ["coder-1", "reviewer-1"],
            },
        }
    )
    store = TaskStore(framework_logger=_test_logger)
    registry = Registry(framework_logger=_test_logger)
    registry.register_config(roles, agents, network)
    impls = create_tool_implementations(store, registry, agent_id, wire_store=WireStore(framework_logger=_test_logger))
    return store, registry, impls


class TestNetworkGatedConnectionsList:
    @pytest.mark.asyncio
    async def test_connections_list_shows_only_connected(self):
        """connections_list for pm shows backend group only, not frontend."""
        store, registry, impls = _setup_with_network("pm")
        result = await impls["connections_list"]({})
        assert not result.is_error
        data = json.loads(result.text)
        agent_ids = {e["agent_id"] for e in data}
        assert agent_ids == {"coder-0", "reviewer-0"}

    @pytest.mark.asyncio
    async def test_connections_list_frontend_agent(self):
        """connections_list for coder-1 shows frontend group only."""
        store, registry, impls = _setup_with_network("coder-1")
        result = await impls["connections_list"]({})
        data = json.loads(result.text)
        agent_ids = {e["agent_id"] for e in data}
        assert agent_ids == {"reviewer-1"}


class TestNetworkGatedGetAvailableReviewers:
    @pytest.mark.asyncio
    async def test_reviewers_scoped_to_connections(self):
        """coder-0 only sees reviewer-0 (backend), not reviewer-1 (frontend)."""
        store, registry, impls = _setup_with_network("coder-0")
        result = await impls["get_available_reviewers"]({})
        assert not result.is_error
        data = json.loads(result.text)
        reviewer_ids = {e["agent_id"] for e in data}
        assert reviewer_ids == {"reviewer-0"}

    @pytest.mark.asyncio
    async def test_no_connected_reviewers(self):
        """pm has no connected agents with submit_review → 'No reviewers'."""
        # pm is in backend with coder-0 and reviewer-0; reviewer-0 has submit_review
        # So pm DOES have a connected reviewer. Let's use a network where pm has none.
        roles = {
            "pm": RoleConfig(name="pm", role_prompt="Manage", tools=["management"], model="test", max_turns=10),
            "coder": RoleConfig(
                name="coder", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
            ),
        }
        agents = {
            "pm": AgentConfig(agent_id="pm", role="pm"),
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
        }
        network = NetworkConfig(**{"connections": {"pm": ["coder-0"]}})
        store = TaskStore(framework_logger=_test_logger)
        registry = Registry(framework_logger=_test_logger)
        registry.register_config(roles, agents, network)
        impls = create_tool_implementations(
            store, registry, "coder-0", wire_store=WireStore(framework_logger=_test_logger)
        )
        result = await impls["get_available_reviewers"]({})
        assert "No reviewers" in result.text


class TestNetworkGatedAssignment:
    @pytest.mark.asyncio
    async def test_assign_to_connected_agent_succeeds(self):
        """pm can assign to coder-0 (same backend group)."""
        store, registry, impls = _setup_with_network("pm")
        result = await impls["tasks_create"](
            {"id": "t1", "title": "Test", "description": "D", "assigned_to": "coder-0"}
        )
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_assign_to_disconnected_agent_fails(self):
        """pm cannot assign to coder-1 (frontend group, not connected)."""
        store, registry, impls = _setup_with_network("pm")
        result = await impls["tasks_create"](
            {"id": "t1", "title": "Test", "description": "D", "assigned_to": "coder-1"}
        )
        assert result.is_error
        assert "not connected" in result.text

    @pytest.mark.asyncio
    async def test_tasks_assign_to_disconnected_agent_fails(self):
        """tasks_assign rejects assignment to disconnected agent."""
        store, registry, impls = _setup_with_network("pm")
        await _create_task_via_store(store, "t1", assigned_to="coder-0", assigned_by="pm")
        result = await impls["tasks_assign"]({"task_id": "t1", "to_agent": "coder-1"})
        assert result.is_error
        assert "not connected" in result.text


class TestNetworkGatedSubmitForReview:
    @pytest.mark.asyncio
    async def test_submit_to_connected_reviewer(self):
        """coder-0 can submit for review to reviewer-0 (same backend group)."""
        store, registry, impls = _setup_with_network("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0", assigned_by="pm")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-0": "Please review"})
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_submit_to_disconnected_reviewer_fails(self):
        """coder-0 cannot submit for review to reviewer-1 (frontend group)."""
        store, registry, impls = _setup_with_network("coder-0")
        await _create_task_via_store(store, "t1", assigned_to="coder-0", assigned_by="pm")
        await store.mark_in_progress("t1")
        reviewers = json.dumps({"reviewer-1": "Please review"})
        result = await impls["tasks_submit_for_review"]({"task_id": "t1", "reviewers": reviewers})
        assert result.is_error
        assert "not connected" in result.text


class TestNetworkGatedTasksList:
    @pytest.mark.asyncio
    async def test_tasks_list_scoped_to_network(self):
        """pm sees tasks assigned to self and backend connections, not frontend."""
        store, registry, impls = _setup_with_network("pm")
        await _create_task_via_store(store, "t1", assigned_to="pm")
        await _create_task_via_store(store, "t2", assigned_to="coder-0")  # connected
        await _create_task_via_store(store, "t3", assigned_to="coder-1")  # NOT connected
        result = await impls["tasks_list"]({})
        assert not result.is_error
        data = json.loads(result.text)
        task_ids = {d["id"] for d in data}
        assert task_ids == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_tasks_list_no_network_sees_all(self):
        """Without network, tasks_list returns all tasks."""
        store, registry, impls = _setup("pm")
        await _create_task_via_store(store, "t1", assigned_to="pm")
        await _create_task_via_store(store, "t2", assigned_to="coder-0")
        await _create_task_via_store(store, "t3", assigned_to="reviewer-0")
        result = await impls["tasks_list"]({})
        data = json.loads(result.text)
        assert len(data) == 3

    @pytest.mark.asyncio
    async def test_tasks_list_with_filter_still_scoped(self):
        """Network scoping applies on top of filters."""
        store, registry, impls = _setup_with_network("pm")
        await _create_task_via_store(store, "t1", assigned_to="coder-0", assigned_by="pm")
        await _create_task_via_store(store, "t2", assigned_to="coder-1", assigned_by="pm")
        # Filter by assigned_by=pm, but coder-1 is not connected → only t1
        result = await impls["tasks_list"]({"assigned_by": "pm"})
        data = json.loads(result.text)
        assert len(data) == 1
        assert data[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_tasks_list_unassigned_visible(self):
        """Unassigned tasks are visible to all agents regardless of network."""
        store, registry, impls = _setup_with_network("pm")
        task = Task(id="t-unassigned", title="Unassigned", description="No owner")
        await store.create(task)  # no assigned_to
        await _create_task_via_store(store, "t-assigned", assigned_to="coder-0")
        result = await impls["tasks_list"]({})
        data = json.loads(result.text)
        task_ids = {d["id"] for d in data}
        assert "t-unassigned" in task_ids
        assert "t-assigned" in task_ids


class TestNetworkGatedTasksGet:
    @pytest.mark.asyncio
    async def test_tasks_get_bypasses_network(self):
        """tasks_get returns any task by ID regardless of network connectivity (by design)."""
        store, registry, impls = _setup_with_network("coder-0")
        # Create a task assigned to a disconnected agent (coder-1 is in frontend, coder-0 in backend)
        await _create_task_via_store(store, "t-frontend", assigned_to="coder-1")
        result = await impls["tasks_get"]({"task_id": "t-frontend"})
        assert not result.is_error
        data = json.loads(result.text)
        assert data["id"] == "t-frontend"


class TestNetworkGatedBatchCreate:
    @pytest.mark.asyncio
    async def test_batch_create_rejects_disconnected_assignees(self):
        """Batch create with a disconnected assignee produces partial failure."""
        store, registry, impls = _setup_with_network("pm")
        tasks = [
            {"id": "t1", "title": "Good", "description": "D", "assigned_to": "coder-0"},
            {"id": "t2", "title": "Bad", "description": "D", "assigned_to": "coder-1"},
        ]
        result = await impls["tasks_create_batch"]({"tasks": tasks})
        assert result.is_error  # partial failure
        assert "not connected" in result.text
        # t1 should have been created
        t1 = await store.get_task("t1")
        assert t1 is not None

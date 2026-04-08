"""Tests for magelab.task_store — TaskStore CRUD, events, review workflow, persistence."""

import pytest

from magelab.events import (
    BaseEvent,
    Event,
    ReviewFinishedEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    TaskFinishedEvent,
)
from magelab.state.task_schemas import (
    ReviewPolicy,
    ReviewRecord,
    ReviewStatus,
    SystemAgent,
    Task,
    TaskStatus,
)
from magelab.state.database import Database
from magelab.state.task_store import TaskStore
from tests.helpers import make_review_record, make_task


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def store(logger) -> TaskStore:
    return TaskStore(framework_logger=logger)


@pytest.fixture
def events() -> list[Event]:
    """Mutable list that collects emitted events."""
    collected: list[Event] = []
    return collected


@pytest.fixture
def store_with_events(store: TaskStore, events: list[Event]) -> TaskStore:
    """TaskStore with event collection enabled."""
    store.add_event_listener(lambda e: events.append(e))
    return store


async def _create_assigned_task(
    store: TaskStore,
    task_id: str = "task-1",
    assigned_to: str = "worker",
    assigned_by: str = "pm",
    review_required: bool = False,
) -> Task:
    """Helper: create a task and assign it."""
    task = make_task(id=task_id, review_required=review_required)
    return await store.create(task, assigned_to=assigned_to, assigned_by=assigned_by)


# =============================================================================
# Create
# =============================================================================


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_unassigned(self, store: TaskStore):
        task = make_task(id="t1")
        result = await store.create(task)
        assert result.id == "t1"
        assert result.status == TaskStatus.CREATED

    @pytest.mark.asyncio
    async def test_create_assigned(self, store_with_events: TaskStore, events: list[Event]):
        task = make_task(id="t1")
        result = await store_with_events.create(task, assigned_to="worker", assigned_by="pm")
        assert result.status == TaskStatus.ASSIGNED
        assert result.assigned_to == "worker"

    @pytest.mark.asyncio
    async def test_create_emits_assigned_event(self, store_with_events: TaskStore, events: list[Event]):
        task = make_task(id="t1")
        await store_with_events.create(task, assigned_to="worker", assigned_by="pm")
        assert len(events) == 1
        assert isinstance(events[0], TaskAssignedEvent)
        assert events[0].target_id == "worker"
        assert events[0].source_id == "pm"

    @pytest.mark.asyncio
    async def test_create_no_event_when_unassigned(self, store_with_events: TaskStore, events: list[Event]):
        task = make_task(id="t1")
        await store_with_events.create(task)
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, store: TaskStore):
        await store.create(make_task(id="t1"))
        with pytest.raises(ValueError, match="already exists"):
            await store.create(make_task(id="t1"))

    @pytest.mark.asyncio
    async def test_create_non_created_status_raises(self, store: TaskStore):
        task = make_task()
        task.update_status(TaskStatus.IN_PROGRESS)
        with pytest.raises(ValueError, match="expected 'created'"):
            await store.create(task)

    @pytest.mark.asyncio
    async def test_create_returns_deep_copy(self, store: TaskStore):
        task = make_task(id="t1")
        result = await store.create(task)
        # Mutating the returned copy shouldn't affect the store
        result.update_status(TaskStatus.IN_PROGRESS)
        stored = await store.get_task("t1")
        assert stored.status == TaskStatus.CREATED  # unchanged

    @pytest.mark.asyncio
    async def test_create_without_assigned_by_defaults_to_user(self, store_with_events: TaskStore, events: list[Event]):
        """When assigned_by is not provided, SystemAgent.USER is used as creator/source."""
        task = make_task(id="t1")
        result = await store_with_events.create(task, assigned_to="worker")
        # assignment_history should be [SystemAgent.USER, "worker"]
        assert result.assignment_history[0] == SystemAgent.USER
        assert result.assigned_to == "worker"
        # Event source should be SystemAgent.USER
        assert len(events) == 1
        assert events[0].source_id == SystemAgent.USER


# =============================================================================
# Get & List
# =============================================================================


class TestGetAndList:
    @pytest.mark.asyncio
    async def test_get_task_returns_copy(self, store: TaskStore):
        await store.create(make_task(id="t1"))
        task = await store.get_task("t1")
        assert task is not None
        assert task.id == "t1"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, store: TaskStore):
        result = await store.get_task("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_task_analytics(self, store: TaskStore):
        await store.create(make_task(id="t1"))
        analytics = await store.get_task_analytics("t1")
        assert analytics is not None
        assert analytics.id == "t1"

    @pytest.mark.asyncio
    async def test_get_task_analytics_not_found(self, store: TaskStore):
        result = await store.get_task_analytics("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_tasks_all(self, store: TaskStore):
        await store.create(make_task(id="t1"))
        await store.create(make_task(id="t2"))
        tasks = await store.list_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_tasks_filter_status(self, store: TaskStore):
        await store.create(make_task(id="t1"))
        await _create_assigned_task(store, task_id="t2")
        tasks = await store.list_tasks(status=TaskStatus.ASSIGNED)
        assert len(tasks) == 1
        assert tasks[0].id == "t2"

    @pytest.mark.asyncio
    async def test_list_tasks_filter_assigned_to(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1", assigned_to="alice")
        await _create_assigned_task(store, task_id="t2", assigned_to="bob")
        tasks = await store.list_tasks(assigned_to="alice")
        assert len(tasks) == 1
        assert tasks[0].id == "t1"

    @pytest.mark.asyncio
    async def test_list_tasks_filter_assigned_by(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1", assigned_by="pm")
        await _create_assigned_task(store, task_id="t2", assigned_by="lead")
        tasks = await store.list_tasks(assigned_by="pm")
        assert len(tasks) == 1
        assert tasks[0].id == "t1"

    @pytest.mark.asyncio
    async def test_list_tasks_pending_reviewer(self, store: TaskStore):
        """list_tasks(pending_reviewer=...) returns only tasks with that reviewer pending."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev-alpha")], ReviewPolicy.ALL_APPROVE)

        await _create_assigned_task(store, task_id="t2")
        await store.mark_in_progress("t2")
        # t2 is in progress but NOT under review

        tasks = await store.list_tasks(pending_reviewer="rev-alpha")
        assert len(tasks) == 1
        assert tasks[0].id == "t1"

        # A different reviewer returns nothing
        tasks_other = await store.list_tasks(pending_reviewer="rev-beta")
        assert len(tasks_other) == 0

    @pytest.mark.asyncio
    async def test_list_tasks_combined_filters(self, store: TaskStore):
        """Multiple filters are AND-ed together."""
        await _create_assigned_task(store, task_id="t1", assigned_to="alice", assigned_by="pm")
        await _create_assigned_task(store, task_id="t2", assigned_to="alice", assigned_by="lead")
        await _create_assigned_task(store, task_id="t3", assigned_to="bob", assigned_by="pm")
        tasks = await store.list_tasks(assigned_to="alice", assigned_by="pm")
        assert len(tasks) == 1
        assert tasks[0].id == "t1"

    @pytest.mark.asyncio
    async def test_list_tasks_filter_is_finished(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await _create_assigned_task(store, task_id="t2")
        await store.mark_in_progress("t2")
        await store.mark_finished("t2", TaskStatus.SUCCEEDED, "done")
        finished = await store.list_tasks(is_finished=True)
        assert len(finished) == 1
        assert finished[0].id == "t2"
        open_tasks = await store.list_tasks(is_finished=False)
        assert len(open_tasks) == 1
        assert open_tasks[0].id == "t1"


# =============================================================================
# Assign
# =============================================================================


class TestAssign:
    @pytest.mark.asyncio
    async def test_assign_basic(self, store_with_events: TaskStore, events: list[Event]):
        await store_with_events.create(make_task(id="t1"), assigned_to="alice", assigned_by="pm")
        events.clear()

        result = await store_with_events.assign("t1", to_agent="bob", by_agent="pm")
        assert result.assigned_to == "bob"
        assert result.status == TaskStatus.ASSIGNED
        assert len(events) == 1
        assert isinstance(events[0], TaskAssignedEvent)
        assert events[0].target_id == "bob"

    @pytest.mark.asyncio
    async def test_assign_not_found(self, store: TaskStore):
        with pytest.raises(ValueError, match="not found"):
            await store.assign("nonexistent", "bob")

    @pytest.mark.asyncio
    async def test_assign_finished_raises(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")
        with pytest.raises(ValueError, match="finished"):
            await store.assign("t1", "bob")

    @pytest.mark.asyncio
    async def test_assign_no_by_agent_no_current_assignee(self, store: TaskStore):
        """assign raises ValueError when no by_agent and task has no current assignee."""
        await store.create(make_task(id="t1"))  # CREATED, no assignee
        with pytest.raises(ValueError, match="no by_agent provided"):
            await store.assign("t1", "worker")

    @pytest.mark.asyncio
    async def test_assign_uses_current_assignee_as_source(self, store_with_events: TaskStore, events: list[Event]):
        """When by_agent is omitted, the current assignee is used as event source."""
        await store_with_events.create(make_task(id="t1"), assigned_to="alice", assigned_by="pm")
        events.clear()

        result = await store_with_events.assign("t1", to_agent="bob")  # no by_agent
        assert result.assigned_to == "bob"
        assert len(events) == 1
        assert events[0].source_id == "alice"  # current assignee used as source

    @pytest.mark.asyncio
    async def test_assign_in_review_raises(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        records = [make_review_record(reviewer_id="rev")]
        await store.submit_for_review("t1", records)
        with pytest.raises(ValueError, match="in review"):
            await store.assign("t1", "bob")


# =============================================================================
# Mark in-progress
# =============================================================================


class TestMarkInProgress:
    @pytest.mark.asyncio
    async def test_mark_in_progress(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        result = await store.mark_in_progress("t1")
        assert result.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_mark_in_progress_wrong_status(self, store: TaskStore):
        await store.create(make_task(id="t1"))  # CREATED, not ASSIGNED
        with pytest.raises(ValueError, match="expected 'assigned'"):
            await store.mark_in_progress("t1")

    @pytest.mark.asyncio
    async def test_mark_in_progress_not_found(self, store: TaskStore):
        with pytest.raises(ValueError, match="not found"):
            await store.mark_in_progress("nonexistent")


# =============================================================================
# Review workflow
# =============================================================================


class TestReviewWorkflow:
    @pytest.mark.asyncio
    async def test_submit_for_review(self, store_with_events: TaskStore, events: list[Event]):
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        events.clear()

        records = [make_review_record(reviewer_id="rev1"), make_review_record(reviewer_id="rev2")]
        result = await store_with_events.submit_for_review("t1", records, ReviewPolicy.ALL_APPROVE)
        assert result.status == TaskStatus.UNDER_REVIEW

        # One event per reviewer
        assert len(events) == 2
        assert all(isinstance(e, ReviewRequestedEvent) for e in events)
        targets = {e.target_id for e in events}
        assert targets == {"rev1", "rev2"}

    @pytest.mark.asyncio
    async def test_submit_for_review_wrong_status(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        # status is ASSIGNED, not IN_PROGRESS
        with pytest.raises(ValueError, match="Cannot submit for review"):
            await store.submit_for_review("t1", [make_review_record()])

    @pytest.mark.asyncio
    async def test_submit_review_completes_round(self, store_with_events: TaskStore, events: list[Event]):
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        await store_with_events.submit_for_review(
            "t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE
        )
        events.clear()

        result = await store_with_events.submit_review("t1", "rev", ReviewStatus.APPROVED, "LGTM")
        assert result.status == TaskStatus.APPROVED

        # ReviewFinishedEvent emitted
        assert len(events) == 1
        assert isinstance(events[0], ReviewFinishedEvent)
        assert events[0].outcome == TaskStatus.APPROVED
        assert events[0].target_id == "worker"

    @pytest.mark.asyncio
    async def test_submit_review_partial_no_event(self, store_with_events: TaskStore, events: list[Event]):
        """When not all reviews are in, no ReviewFinishedEvent yet."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        records = [make_review_record(reviewer_id="rev1"), make_review_record(reviewer_id="rev2")]
        await store_with_events.submit_for_review("t1", records, ReviewPolicy.ALL_APPROVE)
        events.clear()

        await store_with_events.submit_review("t1", "rev1", ReviewStatus.APPROVED)
        assert len(events) == 0  # round not yet complete

    @pytest.mark.asyncio
    async def test_submit_review_not_in_review(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        with pytest.raises(ValueError, match="not in a review round"):
            await store.submit_review("t1", "rev", ReviewStatus.APPROVED)

    @pytest.mark.asyncio
    async def test_mark_review_failed(self, store_with_events: TaskStore, events: list[Event]):
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        await store_with_events.submit_for_review(
            "t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE
        )
        events.clear()

        result = await store_with_events.mark_review_failed("t1", "rev")
        assert result.status == TaskStatus.REVIEW_FAILED
        assert len(events) == 1
        assert isinstance(events[0], ReviewFinishedEvent)
        assert events[0].outcome == TaskStatus.REVIEW_FAILED

    @pytest.mark.asyncio
    async def test_changes_requested_allows_resubmit(self, store: TaskStore):
        """After CHANGES_REQUESTED, worker can resubmit for review."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev", ReviewStatus.CHANGES_REQUESTED, "Fix bugs")

        task = await store.get_task("t1")
        assert task.status == TaskStatus.CHANGES_REQUESTED

        # Resubmit
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        task = await store.get_task("t1")
        assert task.status == TaskStatus.UNDER_REVIEW

    @pytest.mark.asyncio
    async def test_submit_for_review_any_approve_policy(self, store_with_events: TaskStore, events: list[Event]):
        """ANY_APPROVE policy: one approval is enough even with 2 reviewers."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        records = [make_review_record(reviewer_id="rev1"), make_review_record(reviewer_id="rev2")]
        await store_with_events.submit_for_review("t1", records, ReviewPolicy.ANY_APPROVE)
        events.clear()

        # First reviewer approves
        await store_with_events.submit_review("t1", "rev1", ReviewStatus.APPROVED, "LGTM")

        # Round is NOT complete yet — both reviews must be submitted before evaluation
        # But second reviewer can request changes
        await store_with_events.submit_review("t1", "rev2", ReviewStatus.CHANGES_REQUESTED, "Nope")

        # With ANY_APPROVE, one approval is enough so outcome is APPROVED
        task = await store_with_events.get_task("t1")
        assert task.status == TaskStatus.APPROVED

        # ReviewFinishedEvent should reflect approval
        review_events = [e for e in events if isinstance(e, ReviewFinishedEvent)]
        assert len(review_events) == 1
        assert review_events[0].outcome == TaskStatus.APPROVED

    @pytest.mark.asyncio
    async def test_mark_review_failed_not_found(self, store: TaskStore):
        """mark_review_failed raises ValueError when task doesn't exist."""
        with pytest.raises(ValueError, match="not found"):
            await store.mark_review_failed("nonexistent", "reviewer")

    @pytest.mark.asyncio
    async def test_mark_review_failed_not_in_review(self, store: TaskStore):
        """mark_review_failed raises ValueError when task is not in a review round."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        with pytest.raises(ValueError, match="not in a review round"):
            await store.mark_review_failed("t1", "reviewer")

    @pytest.mark.asyncio
    async def test_submit_review_not_found(self, store: TaskStore):
        """submit_review raises ValueError when task doesn't exist."""
        with pytest.raises(ValueError, match="not found"):
            await store.submit_review("nonexistent", "reviewer", ReviewStatus.APPROVED, "ok")

    @pytest.mark.asyncio
    async def test_submit_review_already_finished(self, store: TaskStore):
        """submit_review raises ValueError when task is already finished."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")
        with pytest.raises(ValueError, match="already finished"):
            await store.submit_review("t1", "reviewer", ReviewStatus.APPROVED, "ok")

    @pytest.mark.asyncio
    async def test_submit_for_review_majority_approve(self, store_with_events: TaskStore, events: list[Event]):
        """MAJORITY_APPROVE policy: 2/3 approvals (>50%) results in APPROVED."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        records = [
            make_review_record(reviewer_id="rev1"),
            make_review_record(reviewer_id="rev2"),
            make_review_record(reviewer_id="rev3"),
        ]
        await store_with_events.submit_for_review("t1", records, ReviewPolicy.MAJORITY_APPROVE)
        events.clear()

        await store_with_events.submit_review("t1", "rev1", ReviewStatus.APPROVED, "good")
        await store_with_events.submit_review("t1", "rev2", ReviewStatus.APPROVED, "nice")
        await store_with_events.submit_review("t1", "rev3", ReviewStatus.CHANGES_REQUESTED, "needs work")

        task = await store_with_events.get_task("t1")
        assert task.status == TaskStatus.APPROVED

        review_events = [e for e in events if isinstance(e, ReviewFinishedEvent)]
        assert len(review_events) == 1
        assert review_events[0].outcome == TaskStatus.APPROVED

    @pytest.mark.asyncio
    async def test_mark_review_failed_reviewer_not_in_round(self, store: TaskStore):
        """mark_review_failed raises ValueError when reviewer_id is not in the active review round."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev1")], ReviewPolicy.ALL_APPROVE)
        with pytest.raises(ValueError, match="not in this review round"):
            await store.mark_review_failed("t1", "unknown-reviewer")

    @pytest.mark.asyncio
    async def test_mark_review_failed_already_submitted(self, store: TaskStore):
        """mark_review_failed raises ValueError when reviewer has already submitted."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        records = [make_review_record(reviewer_id="rev1"), make_review_record(reviewer_id="rev2")]
        await store.submit_for_review("t1", records, ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev1", ReviewStatus.APPROVED)
        with pytest.raises(ValueError, match="already submitted"):
            await store.mark_review_failed("t1", "rev1")

    @pytest.mark.asyncio
    async def test_mark_review_failed_mixed_with_approval(self, store_with_events: TaskStore, events: list[Event]):
        """One reviewer approves, one is marked failed. With ALL_APPROVE, non_failed=1 and approvals=1 so APPROVED."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        records = [make_review_record(reviewer_id="rev1"), make_review_record(reviewer_id="rev2")]
        await store_with_events.submit_for_review("t1", records, ReviewPolicy.ALL_APPROVE)
        events.clear()

        await store_with_events.submit_review("t1", "rev1", ReviewStatus.APPROVED, "LGTM")
        # No event yet since rev2 hasn't responded
        assert len(events) == 0

        result = await store_with_events.mark_review_failed("t1", "rev2")
        # ALL_APPROVE: non_failed=1, approvals=1 -> threshold met -> APPROVED
        assert result.status == TaskStatus.APPROVED
        assert len(events) == 1
        assert isinstance(events[0], ReviewFinishedEvent)
        assert events[0].outcome == TaskStatus.APPROVED

    @pytest.mark.asyncio
    async def test_submit_for_review_from_approved_status(self, store: TaskStore):
        """After APPROVED, worker can submit for another review round."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev", ReviewStatus.APPROVED)

        task = await store.get_task("t1")
        assert task.status == TaskStatus.APPROVED

        # Resubmit from APPROVED — allowed by the valid_for_review set
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        task = await store.get_task("t1")
        assert task.status == TaskStatus.UNDER_REVIEW
        assert task.current_review_round == 2

    @pytest.mark.asyncio
    async def test_submit_for_review_from_review_failed_status(self, store: TaskStore):
        """After REVIEW_FAILED (all reviewers crashed), worker can resubmit."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        await store.mark_review_failed("t1", "rev")

        task = await store.get_task("t1")
        assert task.status == TaskStatus.REVIEW_FAILED

        # Resubmit from REVIEW_FAILED — allowed
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev2")], ReviewPolicy.ALL_APPROVE)
        task = await store.get_task("t1")
        assert task.status == TaskStatus.UNDER_REVIEW
        assert task.current_review_round == 2

    @pytest.mark.asyncio
    async def test_submit_for_review_event_source_is_worker(self, store_with_events: TaskStore, events: list[Event]):
        """ReviewRequestedEvent source_id should be the task's current assignee (worker)."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        events.clear()

        await store_with_events.submit_for_review(
            "t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE
        )
        assert len(events) == 1
        assert isinstance(events[0], ReviewRequestedEvent)
        assert events[0].source_id == "worker"

    @pytest.mark.asyncio
    async def test_all_approve_multi_reviewer_changes_requested(self, store: TaskStore):
        """ALL_APPROVE with multiple reviewers: one requests changes => CHANGES_REQUESTED."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        records = [make_review_record(reviewer_id="rev1"), make_review_record(reviewer_id="rev2")]
        await store.submit_for_review("t1", records, ReviewPolicy.ALL_APPROVE)

        await store.submit_review("t1", "rev1", ReviewStatus.APPROVED)
        result = await store.submit_review("t1", "rev2", ReviewStatus.CHANGES_REQUESTED)
        assert result.status == TaskStatus.CHANGES_REQUESTED

    @pytest.mark.asyncio
    async def test_majority_approve_not_enough_approvals(self, store: TaskStore):
        """MAJORITY_APPROVE: 1/3 approve (<50%) results in CHANGES_REQUESTED."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        records = [
            make_review_record(reviewer_id="rev1"),
            make_review_record(reviewer_id="rev2"),
            make_review_record(reviewer_id="rev3"),
        ]
        await store.submit_for_review("t1", records, ReviewPolicy.MAJORITY_APPROVE)

        await store.submit_review("t1", "rev1", ReviewStatus.APPROVED)
        await store.submit_review("t1", "rev2", ReviewStatus.CHANGES_REQUESTED)
        result = await store.submit_review("t1", "rev3", ReviewStatus.CHANGES_REQUESTED)
        assert result.status == TaskStatus.CHANGES_REQUESTED

    @pytest.mark.asyncio
    async def test_review_round_increments_and_history_preserved(self, store: TaskStore):
        """Review round number increments across rounds, and review_history accumulates."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")

        # Round 1
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev", ReviewStatus.CHANGES_REQUESTED, "Fix it")
        task = await store.get_task("t1")
        assert task.current_review_round == 1
        assert len(task.review_history) == 1
        assert task.review_history[0].round_number == 1

        # Round 2
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev", ReviewStatus.APPROVED, "Good now")
        task = await store.get_task("t1")
        assert task.current_review_round == 2
        assert len(task.review_history) == 2
        assert task.review_history[1].round_number == 2

    @pytest.mark.asyncio
    async def test_review_completes_with_no_assignee_logs_warning(self, store: TaskStore):
        """When review round completes but task has no assignee, no event is emitted (logs warning)."""
        # Set up task normally through review
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)

        # Directly manipulate internal state to remove assignee (only way to reach lines 415-416)
        # Trim assignment_history so assigned_to returns None (needs len <= 1)
        store._tasks["t1"].assignment_history = ["pm"]

        # Collect events to verify none are emitted
        emitted: list[Event] = []
        store.add_event_listener(lambda e: emitted.append(e))

        result = await store.submit_review("t1", "rev", ReviewStatus.APPROVED, "LGTM")
        # Review round completed (status updated to APPROVED) but no ReviewFinishedEvent
        assert result.status == TaskStatus.APPROVED
        # No ReviewFinishedEvent because assigned_to was None
        review_events = [e for e in emitted if isinstance(e, ReviewFinishedEvent)]
        assert len(review_events) == 0

    @pytest.mark.asyncio
    async def test_submit_for_review_not_found(self, store: TaskStore):
        with pytest.raises(ValueError, match="not found"):
            await store.submit_for_review("nonexistent", [make_review_record()])


# =============================================================================
# Mark finished
# =============================================================================


class TestMarkFinished:
    @pytest.mark.asyncio
    async def test_mark_succeeded(self, store_with_events: TaskStore, events: list[Event]):
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        events.clear()

        result = await store_with_events.mark_finished("t1", TaskStatus.SUCCEEDED, "All done")
        assert result.status == TaskStatus.SUCCEEDED
        assert result.finished_at is not None

        assert len(events) == 1
        assert isinstance(events[0], TaskFinishedEvent)
        assert events[0].outcome == TaskStatus.SUCCEEDED
        assert events[0].target_id == "pm"

    @pytest.mark.asyncio
    async def test_mark_failed(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        result = await store.mark_finished("t1", TaskStatus.FAILED, "Oops")
        assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_mark_succeeded_requires_approved_when_review_required(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1", review_required=True)
        await store.mark_in_progress("t1")
        with pytest.raises(ValueError, match="requires review"):
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

    @pytest.mark.asyncio
    async def test_mark_succeeded_after_approval(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1", review_required=True)
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev", ReviewStatus.APPROVED)

        result = await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")
        assert result.status == TaskStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_mark_finished_already_finished_raises(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")
        with pytest.raises(ValueError, match="already finished"):
            await store.mark_finished("t1", TaskStatus.FAILED, "oops")

    @pytest.mark.asyncio
    async def test_mark_finished_in_review_raises(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        with pytest.raises(ValueError, match="in review"):
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

    @pytest.mark.asyncio
    async def test_force_fail_in_review(self, store: TaskStore):
        """force=True allows FAILED even during review and cleans up review state."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        result = await store.mark_finished("t1", TaskStatus.FAILED, "agent crashed", force=True)
        assert result.status == TaskStatus.FAILED
        assert result.active_reviews is None
        assert result.review_policy is None

    @pytest.mark.asyncio
    async def test_force_double_fail_is_noop(self, store_with_events: TaskStore, events: list[Event]):
        """force=True on already-failed task is a no-op (same timestamp, no event)."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        first_result = await store_with_events.mark_finished("t1", TaskStatus.FAILED, "first fail")
        first_finished_at = first_result.finished_at
        events.clear()

        result = await store_with_events.mark_finished("t1", TaskStatus.FAILED, "second fail", force=True)
        assert result.status == TaskStatus.FAILED
        assert result.finished_at == first_finished_at
        assert len(events) == 0  # no event emitted on second call

    @pytest.mark.asyncio
    async def test_invalid_outcome_raises(self, store: TaskStore):
        with pytest.raises(ValueError, match="'succeeded' or 'failed'"):
            await store.mark_finished("t1", TaskStatus.IN_PROGRESS, "bad")

    @pytest.mark.asyncio
    async def test_mark_finished_not_found(self, store: TaskStore):
        with pytest.raises(ValueError, match="not found"):
            await store.mark_finished("nonexistent", TaskStatus.SUCCEEDED, "done")

    @pytest.mark.asyncio
    async def test_mark_finished_from_created_status(self, store: TaskStore):
        """Cannot mark succeeded from CREATED — task must be IN_PROGRESS or APPROVED."""
        await store.create(make_task(id="t1"))
        with pytest.raises(ValueError):
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

    @pytest.mark.asyncio
    async def test_mark_finished_failed_from_created(self, store: TaskStore):
        """FAILED has no status restriction — can fail a task even from CREATED."""
        await store.create(make_task(id="t1"))
        result = await store.mark_finished("t1", TaskStatus.FAILED, "abandoned")
        assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_mark_succeeded_from_assigned_raises(self, store: TaskStore):
        """Cannot mark succeeded from ASSIGNED — must be IN_PROGRESS or APPROVED."""
        await _create_assigned_task(store, task_id="t1")
        with pytest.raises(ValueError, match="Cannot mark succeeded"):
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

    @pytest.mark.asyncio
    async def test_mark_failed_emits_event(self, store_with_events: TaskStore, events: list[Event]):
        """mark_finished with FAILED should emit a TaskFinishedEvent."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        events.clear()

        await store_with_events.mark_finished("t1", TaskStatus.FAILED, "error")
        assert len(events) == 1
        assert isinstance(events[0], TaskFinishedEvent)
        assert events[0].outcome == TaskStatus.FAILED
        assert events[0].target_id == "pm"
        assert events[0].details == "error"

    @pytest.mark.asyncio
    async def test_force_fail_on_succeeded_task_is_noop(self, store: TaskStore):
        """force=True with FAILED on a SUCCEEDED task is a no-op (does not re-fail)."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")
        # force+FAILED on any finished task (including SUCCEEDED) is a no-op
        result = await store.mark_finished("t1", TaskStatus.FAILED, "crash", force=True)
        assert result.status == TaskStatus.SUCCEEDED  # unchanged

    @pytest.mark.asyncio
    async def test_force_succeed_in_review_still_raises(self, store: TaskStore):
        """force=True only allows FAILED through review guard, not SUCCEEDED."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        with pytest.raises(ValueError, match="in review"):
            await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done", force=True)


# =============================================================================
# all_finished
# =============================================================================


class TestAllFinished:
    @pytest.mark.asyncio
    async def test_empty_store(self, store: TaskStore):
        assert await store.all_finished()

    @pytest.mark.asyncio
    async def test_not_all_finished(self, store: TaskStore):
        await store.create(make_task(id="t1"))
        assert not await store.all_finished()

    @pytest.mark.asyncio
    async def test_all_finished(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")
        assert await store.all_finished()


# =============================================================================
# Event staleness
# =============================================================================


class TestEventStaleness:
    @pytest.mark.asyncio
    async def test_assigned_event_not_stale(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        event = TaskAssignedEvent(task_id="t1", target_id="worker", source_id="pm")
        assert not await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_assigned_event_stale_after_progress(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        event = TaskAssignedEvent(task_id="t1", target_id="worker", source_id="pm")
        assert await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_assigned_event_stale_wrong_target(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        event = TaskAssignedEvent(task_id="t1", target_id="someone_else", source_id="pm")
        assert await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_review_requested_event_not_stale(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        event = ReviewRequestedEvent(task_id="t1", target_id="rev", source_id="worker")
        assert not await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_review_requested_event_stale_after_completion(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev", ReviewStatus.APPROVED)
        event = ReviewRequestedEvent(task_id="t1", target_id="rev", source_id="worker")
        assert await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_task_finished_event_not_stale(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, "done")
        event = TaskFinishedEvent(task_id="t1", target_id="pm", outcome=TaskStatus.SUCCEEDED, details="done")
        assert not await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_task_finished_event_stale_when_not_finished(self, store: TaskStore):
        await _create_assigned_task(store, task_id="t1")
        event = TaskFinishedEvent(task_id="t1", target_id="pm", outcome=TaskStatus.SUCCEEDED, details="done")
        assert await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_review_finished_event_staleness(self, store_with_events: TaskStore, events: list[Event]):
        """ReviewFinishedEvent becomes stale once the task moves past the post-review status."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        await store_with_events.submit_for_review(
            "t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE
        )
        await store_with_events.submit_review("t1", "rev", ReviewStatus.APPROVED, "LGTM")

        # Grab the ReviewFinishedEvent that was emitted
        review_finished = [e for e in events if isinstance(e, ReviewFinishedEvent)]
        assert len(review_finished) == 1
        event = review_finished[0]

        # Right after review completes, event should NOT be stale (task is APPROVED)
        assert not await store_with_events.is_event_stale(event)

        # Now finish the task — this moves past APPROVED to SUCCEEDED
        await store_with_events.mark_finished("t1", TaskStatus.SUCCEEDED, "done")

        # Now the ReviewFinishedEvent is stale because task.is_finished() is True
        assert await store_with_events.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_event_stale_for_unknown_task(self, store: TaskStore):
        event = TaskAssignedEvent(task_id="nonexistent", target_id="worker", source_id="pm")
        assert await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_event_stale_unknown_event_type_raises(self, store: TaskStore):
        """is_event_stale raises TypeError for an unknown event type."""
        await _create_assigned_task(store, task_id="t1")

        # Create a custom event type not in the known set
        from dataclasses import dataclass

        @dataclass(kw_only=True)
        class FakeEvent(BaseEvent):
            task_id: str

        fake_event = FakeEvent(task_id="t1", target_id="worker")
        with pytest.raises(TypeError, match="Unknown event type"):
            await store.is_event_stale(fake_event)

    @pytest.mark.asyncio
    async def test_review_requested_stale_when_reviewer_not_in_round(self, store: TaskStore):
        """ReviewRequestedEvent is stale if the target reviewer is not in active_reviews."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)

        # Event for a different reviewer than who is actually in the review round
        event = ReviewRequestedEvent(task_id="t1", target_id="different-rev", source_id="worker")
        assert await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_review_finished_stale_when_reassigned(self, store_with_events: TaskStore, events: list[Event]):
        """ReviewFinishedEvent becomes stale if the task has been reassigned to someone else."""
        await _create_assigned_task(store_with_events, task_id="t1")
        await store_with_events.mark_in_progress("t1")
        await store_with_events.submit_for_review(
            "t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE
        )
        await store_with_events.submit_review("t1", "rev", ReviewStatus.CHANGES_REQUESTED)

        # Grab the ReviewFinishedEvent
        review_finished = [e for e in events if isinstance(e, ReviewFinishedEvent)]
        assert len(review_finished) == 1
        event = review_finished[0]
        assert event.target_id == "worker"

        # Now reassign the task to someone else
        await store_with_events.assign("t1", to_agent="new-worker", by_agent="pm")
        # Event should be stale because assigned_to != event.target_id
        assert await store_with_events.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_assigned_event_stale_when_finished(self, store: TaskStore):
        """TaskAssignedEvent is stale if the task has already finished."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.FAILED, "dead")

        event = TaskAssignedEvent(task_id="t1", target_id="worker", source_id="pm")
        assert await store.is_event_stale(event)

    @pytest.mark.asyncio
    async def test_assigned_event_stale_when_in_review(self, store: TaskStore):
        """TaskAssignedEvent is stale if the task is under review."""
        await _create_assigned_task(store, task_id="t1")
        await store.mark_in_progress("t1")
        await store.submit_for_review("t1", [make_review_record(reviewer_id="rev")], ReviewPolicy.ALL_APPROVE)

        event = TaskAssignedEvent(task_id="t1", target_id="worker", source_id="pm")
        assert await store.is_event_stale(event)


# =============================================================================
# DB persistence roundtrip
# =============================================================================


class TestDBPersistence:
    @pytest.fixture
    def db_store(self, tmp_path, logger):
        db = Database(str(tmp_path / "test.db"))
        store = TaskStore(framework_logger=logger, db=db)
        yield store, db, logger
        db.close()

    @pytest.mark.asyncio
    async def test_persist_and_load_roundtrip(self, db_store):
        store, db, _logger = db_store
        task = make_task(id="t1", review_required=True)
        await store.create(task, assigned_to="coder", assigned_by="pm")

        # Load into a fresh store from the same DB
        store2 = TaskStore(framework_logger=_logger, db=db)
        store2.load_from_db()
        assert len(store2._tasks) == 1
        t = store2._tasks["t1"]
        assert t.status == TaskStatus.ASSIGNED
        assert t.review_required is True
        assert t.assignment_history == ["pm", "coder"]

    @pytest.mark.asyncio
    async def test_persist_update(self, db_store):
        store, db, _logger = db_store
        task = make_task(id="t1")
        await store.create(task, assigned_to="coder", assigned_by="pm")
        await store.mark_in_progress("t1")

        store2 = TaskStore(framework_logger=_logger, db=db)
        store2.load_from_db()
        assert store2._tasks["t1"].status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_persist_with_review_history(self, db_store):
        store, db, _logger = db_store
        task = make_task(id="t1", review_required=True)
        await store.create(task, assigned_to="coder", assigned_by="pm")
        await store.mark_in_progress("t1")

        record = ReviewRecord(reviewer_id="rev1", requester_id="coder", round_number=1)
        await store.submit_for_review("t1", reviewers=[record], policy=ReviewPolicy.ALL_APPROVE)
        await store.submit_review("t1", "rev1", decision=ReviewStatus.APPROVED)

        store2 = TaskStore(framework_logger=_logger, db=db)
        store2.load_from_db()
        t = store2._tasks["t1"]
        assert len(t.review_history) == 1
        assert t.review_history[0].reviewer_id == "rev1"

    @pytest.mark.asyncio
    async def test_compute_task_counts(self, db_store):
        store, db, _logger = db_store
        t1 = make_task(id="t1")
        t2 = make_task(id="t2")
        t3 = make_task(id="t3")
        await store.create(t1, assigned_to="a", assigned_by="pm")
        await store.create(t2, assigned_to="b", assigned_by="pm")
        await store.create(t3, assigned_to="c", assigned_by="pm")

        await store.mark_in_progress("t1")
        await store.mark_finished("t1", TaskStatus.SUCCEEDED, details="done")
        await store.mark_in_progress("t2")
        await store.mark_finished("t2", TaskStatus.FAILED, details="error")
        # t3 stays assigned (open)

        counts = store.compute_task_counts()
        assert counts == {"succeeded": 1, "failed": 1, "open": 1}


# =============================================================================
# Cross-store interaction: event emission reaches external listeners
# =============================================================================


class TestCrossStoreEventEmission:
    @pytest.mark.asyncio
    async def test_create_emits_event_to_listener(self, store: TaskStore):
        """TaskStore.create emits a TaskAssignedEvent that external listeners receive."""
        received = []
        store.add_event_listener(lambda e: received.append(e))
        task = make_task()
        await store.create(task, assigned_to="coder-0", assigned_by="pm")
        assert len(received) == 1
        assert isinstance(received[0], TaskAssignedEvent)
        assert received[0].target_id == "coder-0"
        assert received[0].task_id == task.id

    @pytest.mark.asyncio
    async def test_listener_can_reenter_store(self, store: TaskStore):
        """Deadlock canary: a listener that reads from the store during emit must not deadlock.

        This verifies the lock-then-emit pattern — events are emitted AFTER
        releasing the lock, so re-entrant reads succeed."""
        reentrant_result = []

        async def reentrant_listener(event):
            # Re-enter the store during callback
            task = await store.get_task(event.task_id)
            reentrant_result.append(task)

        # Use sync wrapper since event listeners are called synchronously
        import asyncio

        def sync_wrapper(event):
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule the coroutine — it will run after current await yields
                asyncio.ensure_future(reentrant_listener(event))

        store.add_event_listener(sync_wrapper)
        task = make_task()
        await store.create(task, assigned_to="coder-0", assigned_by="pm")

        # Give the scheduled coroutine a chance to run
        await asyncio.sleep(0.01)

        assert len(reentrant_result) == 1
        assert reentrant_result[0].id == task.id

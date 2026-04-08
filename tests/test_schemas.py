"""Tests for magelab.schemas — Task lifecycle, review workflow, events."""

import time

import pytest

from magelab.events import (
    ReviewFinishedEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    TaskFinishedEvent,
)
from magelab.state.task_schemas import (
    ReviewPolicy,
    ReviewStatus,
    SystemAgent,
    Task,
    TaskAnalytics,
    TaskStatus,
)
from tests.helpers import make_review_record, make_task


# =============================================================================
# ReviewRecord
# =============================================================================


class TestReviewRecord:
    def test_new_record_is_pending(self):
        r = make_review_record()
        assert r.is_pending()
        assert r.review is None

    def test_submit_populates_review(self):
        r = make_review_record(reviewer_id="alice")
        r.submit("alice", ReviewStatus.APPROVED, "Looks good")
        assert not r.is_pending()
        assert r.review.decision == ReviewStatus.APPROVED
        assert r.review.comment == "Looks good"
        assert r.review.reviewer_id == "alice"

    def test_submit_wrong_reviewer_raises(self):
        r = make_review_record(reviewer_id="alice")
        with pytest.raises(ValueError, match="cannot submit review"):
            r.submit("bob", ReviewStatus.APPROVED)

    def test_mark_failed(self):
        r = make_review_record(reviewer_id="alice")
        r.mark_failed()
        assert not r.is_pending()
        assert r.review.decision == ReviewStatus.FAILED
        assert r.review.reviewer_id == "alice"
        assert r.review.comment == "Reviewer agent failed"


# =============================================================================
# Task — assignment properties
# =============================================================================


class TestTaskAssignment:
    def test_assigned_to_none_when_only_creator(self):
        t = make_task()
        t.record_assignee("pm")  # creator
        assert t.assigned_to is None
        assert t.assigned_by == "pm"

    def test_assigned_to_returns_last(self):
        t = make_task()
        t.record_assignee("pm")
        t.record_assignee("coder-1")
        assert t.assigned_to == "coder-1"
        assert t.assigned_by == "pm"

    def test_assigned_to_tracks_handoff(self):
        t = make_task()
        t.record_assignee("pm")
        t.record_assignee("coder-1")
        t.record_assignee("coder-2")
        assert t.assigned_to == "coder-2"
        assert t.assigned_by == "coder-1"

    def test_assigned_to_none_with_empty_history(self):
        """assigned_to returns None on a truly empty assignment_history (length 0)."""
        t = make_task()
        assert len(t.assignment_history) == 0
        assert t.assigned_to is None

    def test_assigned_by_raises_when_empty(self):
        t = make_task()
        with pytest.raises(ValueError, match="assignment_history is empty"):
            _ = t.assigned_by


# =============================================================================
# Task — status management
# =============================================================================


class TestTaskStatus:
    def test_initial_status_is_created(self):
        t = make_task()
        assert t.status == TaskStatus.CREATED
        assert t.finished_at is None

    def test_update_status_sets_finished_at_on_terminal(self):
        t = make_task()
        t.update_status(TaskStatus.SUCCEEDED)
        assert t.finished_at is not None
        assert t.is_finished()

    def test_update_status_failed_sets_finished_at(self):
        t = make_task()
        t.update_status(TaskStatus.FAILED)
        assert t.finished_at is not None
        assert t.is_finished()

    def test_non_terminal_status_no_finished_at(self):
        t = make_task()
        t.update_status(TaskStatus.IN_PROGRESS)
        assert t.finished_at is None
        assert not t.is_finished()

    def test_is_in_review(self):
        t = make_task()
        assert not t.is_in_review()
        # Use proper start_review_round to reach in-review state
        t.record_assignee("pm")
        t.record_assignee("worker")
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        assert t.is_in_review()

    def test_updated_at_changes_on_status_update(self):
        """Verify updated_at strictly advances when update_status is called."""
        t = make_task()
        original_updated_at = t.updated_at
        time.sleep(0.001)
        t.update_status(TaskStatus.IN_PROGRESS)
        assert t.updated_at > original_updated_at
        # Verify the field actually got replaced (not the same object reference)
        second_updated_at = t.updated_at
        time.sleep(0.001)
        t.update_status(TaskStatus.SUCCEEDED)
        assert t.updated_at > second_updated_at

    def test_to_analytics(self):
        t = make_task(id="t1", title="Analytics test")
        t.record_assignee("pm")
        t.record_assignee("coder")
        a = t.to_analytics()
        assert a.id == "t1"
        assert a.title == "Analytics test"
        assert a.status == TaskStatus.CREATED
        assert a.assigned_to == "coder"
        assert a.review_required is False
        assert a.review_rounds == 0
        assert a.created_at == t.created_at
        assert a.finished_at is None

    def test_to_analytics_with_review_rounds(self):
        """Verify to_analytics reflects review_required and current_review_round."""
        t = make_task(id="t2", title="Review Analytics", review_required=True)
        t.record_assignee("pm")
        t.record_assignee("worker")
        t.update_status(TaskStatus.IN_PROGRESS)
        t.start_review_round(
            [make_review_record(reviewer_id="rev-1")],
            ReviewPolicy.ALL_APPROVE,
        )
        analytics = t.to_analytics()
        assert analytics.status == TaskStatus.IN_PROGRESS
        assert analytics.review_required is True
        assert analytics.review_rounds == 1  # matches current_review_round after one start_review_round

    def test_update_status_from_terminal_state_raises(self):
        """update_status raises ValueError when transitioning from a terminal state."""
        t = make_task()
        t.update_status(TaskStatus.SUCCEEDED)
        assert t.is_finished()

        with pytest.raises(ValueError, match="Cannot transition from terminal state"):
            t.update_status(TaskStatus.IN_PROGRESS)
        # Status and finished_at unchanged
        assert t.status == TaskStatus.SUCCEEDED
        assert t.finished_at is not None

    def test_update_status_from_failed_also_raises(self):
        """update_status raises for FAILED → non-terminal too."""
        t = make_task()
        t.update_status(TaskStatus.FAILED)
        with pytest.raises(ValueError, match="Cannot transition from terminal state"):
            t.update_status(TaskStatus.CREATED)

    def test_to_analytics_finished_task(self):
        """Verify to_analytics on a finished task has finished_at set."""
        t = make_task(id="t3", title="Finished Analytics")
        t.record_assignee("pm")
        t.record_assignee("coder")
        t.update_status(TaskStatus.SUCCEEDED)
        a = t.to_analytics()
        assert a.status == TaskStatus.SUCCEEDED
        assert a.finished_at is not None
        assert a.finished_at == t.finished_at
        assert a.created_at == t.created_at

    def test_analytics_is_frozen(self):
        """Verify TaskAnalytics from to_analytics() raises on mutation."""
        t = make_task(id="t1", title="Frozen test")
        a = t.to_analytics()
        assert isinstance(a, TaskAnalytics)
        with pytest.raises(AttributeError):
            a.status = TaskStatus.FAILED


# =============================================================================
# Task — review workflow
# =============================================================================


class TestTaskReviewWorkflow:
    def _prepare_for_review(self, t: Task) -> Task:
        """Set up a task ready for review (assigned, in progress)."""
        t.record_assignee("pm")
        t.record_assignee("worker")
        t.update_status(TaskStatus.IN_PROGRESS)
        return t

    def test_start_review_round_basic(self):
        t = self._prepare_for_review(make_task())
        records = [make_review_record(reviewer_id="rev-1"), make_review_record(reviewer_id="rev-2")]
        t.start_review_round(records, ReviewPolicy.ALL_APPROVE)

        assert t.is_in_review()
        assert t.current_review_round == 1
        assert set(t.active_reviews.keys()) == {"rev-1", "rev-2"}
        assert t.review_policy == ReviewPolicy.ALL_APPROVE
        # Verify round_number was updated on the review records
        assert t.active_reviews["rev-1"].round_number == 1
        assert t.active_reviews["rev-2"].round_number == 1

    def test_start_review_on_finished_task_raises(self):
        t = make_task()
        t.update_status(TaskStatus.SUCCEEDED)
        with pytest.raises(ValueError, match="Cannot start review on finished task"):
            t.start_review_round([make_review_record()])

    def test_start_review_already_in_review_raises(self):
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        with pytest.raises(ValueError, match="Already in a review round"):
            t.start_review_round([make_review_record(reviewer_id="rev-2")])

    def test_start_review_empty_reviewers_raises(self):
        t = self._prepare_for_review(make_task())
        with pytest.raises(ValueError, match="Must provide at least one reviewer"):
            t.start_review_round([])

    def test_start_review_worker_as_reviewer_raises(self):
        t = self._prepare_for_review(make_task())
        with pytest.raises(ValueError, match="cannot review their own task"):
            t.start_review_round([make_review_record(reviewer_id="worker")])

    def test_start_review_duplicate_reviewer_raises(self):
        t = self._prepare_for_review(make_task())
        records = [make_review_record(reviewer_id="rev"), make_review_record(reviewer_id="rev")]
        with pytest.raises(ValueError, match="Duplicate reviewer"):
            t.start_review_round(records)

    def test_submit_review_basic(self):
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        t.submit_review("rev-1", ReviewStatus.APPROVED, "LGTM")

        record = t.active_reviews["rev-1"]
        assert not record.is_pending()
        assert record.review.decision == ReviewStatus.APPROVED

    def test_submit_review_not_in_round_raises(self):
        t = self._prepare_for_review(make_task())
        with pytest.raises(ValueError, match="Not in a review round"):
            t.submit_review("rev-1", ReviewStatus.APPROVED)

    def test_submit_review_unknown_reviewer_raises(self):
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        with pytest.raises(ValueError, match="not in this review round"):
            t.submit_review("unknown", ReviewStatus.APPROVED)

    def test_submit_review_already_submitted_raises(self):
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        t.submit_review("rev-1", ReviewStatus.APPROVED)
        with pytest.raises(ValueError, match="already submitted"):
            t.submit_review("rev-1", ReviewStatus.APPROVED)

    def test_submit_review_invalid_decision_raises(self):
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        with pytest.raises(ValueError, match="Invalid decision"):
            t.submit_review("rev-1", ReviewStatus.FAILED)

    def test_submit_review_comment_passthrough(self):
        """Verify that a comment passed to submit_review is stored in the ReviewRecord."""
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        t.submit_review("rev-1", ReviewStatus.APPROVED, "Great work, ship it")

        record = t.active_reviews["rev-1"]
        assert record.review is not None
        assert record.review.comment == "Great work, ship it"
        assert record.review.decision == ReviewStatus.APPROVED
        assert record.review.reviewer_id == "rev-1"

    def test_submit_review_updates_updated_at(self):
        """Verify that submit_review updates the task's updated_at timestamp."""
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1")])
        before_review = t.updated_at
        time.sleep(0.001)
        t.submit_review("rev-1", ReviewStatus.APPROVED)
        assert t.updated_at > before_review

    def test_all_reviews_complete(self):
        t = self._prepare_for_review(make_task())
        t.start_review_round([make_review_record(reviewer_id="rev-1"), make_review_record(reviewer_id="rev-2")])
        assert not t.all_reviews_complete()
        t.submit_review("rev-1", ReviewStatus.APPROVED)
        assert not t.all_reviews_complete()
        t.submit_review("rev-2", ReviewStatus.APPROVED)
        assert t.all_reviews_complete()

    def test_all_reviews_complete_when_not_in_review(self):
        t = make_task()
        assert t.all_reviews_complete()  # vacuously true


# =============================================================================
# Task — complete_review_round (policy evaluation)
# =============================================================================


class TestCompleteReviewRound:
    def _prepare_and_start(self, reviewers: list[str], policy: ReviewPolicy) -> Task:
        t = make_task()
        t.record_assignee("pm")
        t.record_assignee("worker")
        t.update_status(TaskStatus.IN_PROGRESS)
        records = [make_review_record(reviewer_id=r) for r in reviewers]
        t.start_review_round(records, policy)
        return t

    def test_all_approve_all_approved(self):
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.APPROVED)
        result = t.complete_review_round()
        assert result == TaskStatus.APPROVED
        assert not t.is_in_review()
        assert len(t.review_history) == 2

    def test_all_approve_one_requests_changes(self):
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.CHANGES_REQUESTED)
        result = t.complete_review_round()
        assert result == TaskStatus.CHANGES_REQUESTED

    def test_any_approve_first_approves(self):
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ANY_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.CHANGES_REQUESTED)
        result = t.complete_review_round()
        assert result == TaskStatus.APPROVED

    def test_any_approve_none_approve(self):
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ANY_APPROVE)
        t.submit_review("r1", ReviewStatus.CHANGES_REQUESTED)
        t.submit_review("r2", ReviewStatus.CHANGES_REQUESTED)
        result = t.complete_review_round()
        assert result == TaskStatus.CHANGES_REQUESTED

    def test_majority_approve_2_of_3(self):
        t = self._prepare_and_start(["r1", "r2", "r3"], ReviewPolicy.MAJORITY_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.APPROVED)
        t.submit_review("r3", ReviewStatus.CHANGES_REQUESTED)
        result = t.complete_review_round()
        assert result == TaskStatus.APPROVED

    def test_majority_approve_1_of_3(self):
        t = self._prepare_and_start(["r1", "r2", "r3"], ReviewPolicy.MAJORITY_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.CHANGES_REQUESTED)
        t.submit_review("r3", ReviewStatus.CHANGES_REQUESTED)
        result = t.complete_review_round()
        assert result == TaskStatus.CHANGES_REQUESTED

    def test_all_reviewers_failed(self):
        """When all reviewers crash, outcome is REVIEW_FAILED."""
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.active_reviews["r1"].mark_failed()
        t.active_reviews["r2"].mark_failed()
        result = t.complete_review_round()
        assert result == TaskStatus.REVIEW_FAILED

    def test_one_failed_one_approved_all_policy(self):
        """With ALL_APPROVE, one failure + one approval: only non-failed count.
        1 approval out of 1 non-failed = passes."""
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.active_reviews["r2"].mark_failed()
        result = t.complete_review_round()
        assert result == TaskStatus.APPROVED

    def test_complete_not_in_review_raises(self):
        t = make_task()
        with pytest.raises(ValueError, match="Not in a review round"):
            t.complete_review_round()

    def test_complete_reviews_incomplete_raises(self):
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        with pytest.raises(ValueError, match="Not all reviews are complete"):
            t.complete_review_round()

    def test_review_round_increments(self):
        """Two rounds: round numbers increment, history accumulates."""
        t = self._prepare_and_start(["r1"], ReviewPolicy.ALL_APPROVE)
        assert t.current_review_round == 1
        t.submit_review("r1", ReviewStatus.CHANGES_REQUESTED)
        t.complete_review_round()

        # Second round
        t.start_review_round([make_review_record(reviewer_id="r1")], ReviewPolicy.ALL_APPROVE)
        assert t.current_review_round == 2
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.complete_review_round()
        assert len(t.review_history) == 2

    def test_get_latest_review_records(self):
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.APPROVED)
        t.complete_review_round()

        latest = t.get_latest_review_records()
        assert len(latest) == 2
        assert all(r.round_number == 1 for r in latest)

    def test_majority_approve_boundary_1_of_2(self):
        """Test the exact 50% boundary — 1 approval out of 2 reviewers under MAJORITY.
        Should be CHANGES_REQUESTED (strictly greater than 50%)."""
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.MAJORITY_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.CHANGES_REQUESTED)
        result = t.complete_review_round()
        assert result == TaskStatus.CHANGES_REQUESTED

    def test_mixed_failures_majority_policy(self):
        """3 reviewers under MAJORITY, 1 approves, 1 changes_requested, 1 failed.
        non_failed=2, approvals=1, 1 > 1.0 is False -> CHANGES_REQUESTED."""
        t = self._prepare_and_start(["r1", "r2", "r3"], ReviewPolicy.MAJORITY_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.submit_review("r2", ReviewStatus.CHANGES_REQUESTED)
        t.active_reviews["r3"].mark_failed()
        result = t.complete_review_round()
        assert result == TaskStatus.CHANGES_REQUESTED

    def test_mixed_failures_any_policy(self):
        """2 reviewers under ANY, 1 failed, 1 approved. Should be APPROVED."""
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ANY_APPROVE)
        t.active_reviews["r1"].mark_failed()
        t.submit_review("r2", ReviewStatus.APPROVED)
        result = t.complete_review_round()
        assert result == TaskStatus.APPROVED

    def test_get_latest_review_records_after_two_rounds(self):
        """Run two review rounds, verify get_latest_review_records returns only round 2 records."""
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.submit_review("r1", ReviewStatus.CHANGES_REQUESTED)
        t.submit_review("r2", ReviewStatus.APPROVED)
        t.complete_review_round()

        # Second round with different reviewers
        t.start_review_round(
            [make_review_record(reviewer_id="r3"), make_review_record(reviewer_id="r4")],
            ReviewPolicy.ALL_APPROVE,
        )
        t.submit_review("r3", ReviewStatus.APPROVED)
        t.submit_review("r4", ReviewStatus.APPROVED)
        t.complete_review_round()

        latest = t.get_latest_review_records()
        assert len(latest) == 2
        assert all(r.round_number == 2 for r in latest)
        reviewer_ids = {r.reviewer_id for r in latest}
        assert reviewer_ids == {"r3", "r4"}

    def test_complete_review_round_archives_with_review_data(self):
        """After completing a round, verify archived records in review_history
        have populated review fields (decision, comment)."""
        t = self._prepare_and_start(["r1", "r2"], ReviewPolicy.ALL_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED, "Looks good")
        t.submit_review("r2", ReviewStatus.CHANGES_REQUESTED, "Needs work")
        t.complete_review_round()

        assert len(t.review_history) == 2
        for record in t.review_history:
            assert record.review is not None
            assert record.review.decision in (ReviewStatus.APPROVED, ReviewStatus.CHANGES_REQUESTED)
            assert record.review.comment is not None

        # Check specific reviewer data
        r1_record = next(r for r in t.review_history if r.reviewer_id == "r1")
        assert r1_record.review.decision == ReviewStatus.APPROVED
        assert r1_record.review.comment == "Looks good"
        r2_record = next(r for r in t.review_history if r.reviewer_id == "r2")
        assert r2_record.review.decision == ReviewStatus.CHANGES_REQUESTED
        assert r2_record.review.comment == "Needs work"

    def test_complete_review_round_clears_review_policy(self):
        """After completing a review round, review_policy should be reset to None."""
        t = self._prepare_and_start(["r1"], ReviewPolicy.MAJORITY_APPROVE)
        assert t.review_policy == ReviewPolicy.MAJORITY_APPROVE
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.complete_review_round()
        assert t.review_policy is None
        assert t.active_reviews is None

    def test_majority_approve_failures_reduce_denominator(self):
        """Failures reduce the denominator for MAJORITY_APPROVE policy.

        3 reviewers: 1 approves, 0 changes_requested, 2 fail.
        Without failures: 1/3 is not majority (need >1.5).
        With failures: non_failed=1, approvals=1, 1 > 0.5 is True -> APPROVED.
        This proves failures reduce the effective denominator.
        """
        t = self._prepare_and_start(["r1", "r2", "r3"], ReviewPolicy.MAJORITY_APPROVE)
        t.submit_review("r1", ReviewStatus.APPROVED)
        t.active_reviews["r2"].mark_failed()
        t.active_reviews["r3"].mark_failed()
        result = t.complete_review_round()
        assert result == TaskStatus.APPROVED

    def test_get_latest_review_records_empty(self):
        t = make_task()
        assert t.get_latest_review_records() == []


# =============================================================================
# SystemAgent
# =============================================================================


class TestSystemAgent:
    def test_system_agent_user_value(self):
        """Verify SystemAgent.USER == "User" (str mixin behavior)."""
        assert SystemAgent.USER == "User"
        assert SystemAgent.USER.value == "User"
        assert isinstance(SystemAgent.USER, str)


# =============================================================================
# Events — construction
# =============================================================================


class TestEvents:
    def test_task_assigned_event(self):
        e = TaskAssignedEvent(task_id="t1", target_id="coder", source_id="pm")
        assert e.task_id == "t1"
        assert e.target_id == "coder"
        assert e.source_id == "pm"
        assert e.timestamp is not None

    def test_review_requested_event(self):
        e = ReviewRequestedEvent(task_id="t1", target_id="rev", source_id="coder", request_message="Check logic")
        assert e.request_message == "Check logic"

    def test_review_finished_event(self):
        e = ReviewFinishedEvent(task_id="t1", target_id="coder", outcome=TaskStatus.APPROVED, review_records=[])
        assert e.outcome == TaskStatus.APPROVED

    def test_review_finished_event_with_records(self):
        """ReviewFinishedEvent preserves populated ReviewRecord objects."""
        rec1 = make_review_record(reviewer_id="rev-1")
        rec1.submit("rev-1", ReviewStatus.APPROVED, "Looks good")
        rec2 = make_review_record(reviewer_id="rev-2")
        rec2.submit("rev-2", ReviewStatus.CHANGES_REQUESTED, "Needs work")

        e = ReviewFinishedEvent(
            task_id="t1",
            target_id="coder",
            outcome=TaskStatus.CHANGES_REQUESTED,
            review_records=[rec1, rec2],
        )
        assert len(e.review_records) == 2
        assert e.review_records[0].reviewer_id == "rev-1"
        assert e.review_records[0].review.decision == ReviewStatus.APPROVED
        assert e.review_records[0].review.comment == "Looks good"
        assert e.review_records[1].reviewer_id == "rev-2"
        assert e.review_records[1].review.decision == ReviewStatus.CHANGES_REQUESTED
        assert e.review_records[1].review.comment == "Needs work"

    def test_task_finished_event(self):
        e = TaskFinishedEvent(task_id="t1", target_id="pm", outcome=TaskStatus.SUCCEEDED, details="Done")
        assert e.details == "Done"

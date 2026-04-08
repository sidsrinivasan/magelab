"""
Task domain types — Task lifecycle, review workflow, and analytics.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# =============================================================================
# Enums
# =============================================================================


class SystemAgent(str, Enum):
    """Special agent identifiers for non-agent callers."""

    USER = "User"  # External/human caller (e.g., initial tasks, initial messages)


class TaskStatus(str, Enum):
    """Lifecycle status of a task."""

    # Creation & assignment
    CREATED = "created"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"

    # Review workflow
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"  # Review passed, agent can complete or iterate
    CHANGES_REQUESTED = "changes_requested"  # Review failed, agent must iterate

    # Review failure
    REVIEW_FAILED = "review_failed"  # Review round failed (reviewer agent crashed)

    # Terminal states (task is "finished")
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReviewStatus(str, Enum):
    """Decision of a submitted review."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    FAILED = "failed"  # Reviewer agent crashed


class ReviewPolicy(str, Enum):
    """Policy for determining when a review round passes."""

    ANY_APPROVE = "any"  # First required approval completes
    MAJORITY_APPROVE = "majority"  # >50% of required reviewers must approve
    ALL_APPROVE = "all"  # All required reviewers must approve


# =============================================================================
# Review Request & Response
# =============================================================================


class Review(BaseModel):
    """A submitted review (decision + optional comment)."""

    reviewer_id: str
    decision: ReviewStatus
    comment: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewRecord(BaseModel):
    """
    Record of a review request and its review.

    Tracks who was asked to review, when, and what they said.
    Check `is_pending()` to determine if review has been submitted.
    """

    reviewer_id: str
    requester_id: str
    request_message: Optional[str] = None  # Message from requester to reviewer
    round_number: int = 1  # Which review round this belongs to
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Populated when reviewer submits (None = pending)
    review: Optional[Review] = None

    def is_pending(self) -> bool:
        """Check if this review is still pending."""
        return self.review is None

    def submit(self, reviewer_id: str, decision: ReviewStatus, comment: Optional[str] = None) -> None:
        """Submit a review decision. Validates reviewer and creates the Review."""
        if reviewer_id != self.reviewer_id:
            raise ValueError(f"Reviewer {reviewer_id} cannot submit review for {self.reviewer_id}")
        self.review = Review(
            reviewer_id=self.reviewer_id,
            decision=decision,
            comment=comment,
        )

    def mark_failed(self) -> None:
        """Mark this review as failed (reviewer agent crashed)."""
        self.review = Review(
            reviewer_id=self.reviewer_id,
            decision=ReviewStatus.FAILED,
            comment="Reviewer agent failed",
        )


# =============================================================================
# Task Analytics (lightweight read-only view)
# =============================================================================


@dataclass(frozen=True)
class TaskAnalytics:
    """Lightweight read-only view of a task for run analytics."""

    id: str
    title: str
    status: TaskStatus
    assigned_to: Optional[str]
    review_required: bool
    review_rounds: int
    created_at: Optional[datetime]
    finished_at: Optional[datetime]


# =============================================================================
# Task
# =============================================================================


class Task(BaseModel):
    """
    A unit of work in the system.

    Core fields are domain-agnostic. Use `details` for experiment-specific data.

    Assignment model:
    - One worker at a time (assigned_to property)
    - assignment_history is a list of agent IDs; adjacency encodes handoff
      e.g. [PM, Coder1, Coder2] means PM assigned to Coder1, who handed to Coder2
    - Supports multiple concurrent reviewers during review rounds
    - Worker is "blocked" on task during review (can't modify)

    Review model:
    - Worker submits for review with a set of reviewers and approval policy
    - All reviewers must respond before round completes
    - Approval policy determines if task passes (APPROVED) or needs changes (CHANGES_REQUESTED)
    - On APPROVED: worker sees feedback, can iterate or complete
    - On CHANGES_REQUESTED: worker must iterate, cannot complete

    Finish gating:
    - If review_required=False: worker can mark task as SUCCEEDED anytime
    - If review_required=True: worker can only mark SUCCEEDED if status is APPROVED

    Task structure is flat (no hierarchy). Assignment_history tracks delegation chain.

    Design note: Task is a "dumb" data container. All state mutations are driven by
    TaskStore, which owns the status lifecycle. Task methods like complete_review_round()
    perform internal bookkeeping (archiving review data) and return outcomes, but never
    call update_status() themselves — that's always the caller's (TaskStore's) job.
    This keeps the mutation authority in one place and avoids split-brain state updates.
    """

    # Identity
    id: str
    title: str
    description: str

    # Status
    status: TaskStatus = TaskStatus.CREATED

    # Active review round (None if not in review)
    active_reviews: Optional[dict[str, ReviewRecord]] = None  # reviewer_id -> request
    review_policy: Optional[ReviewPolicy] = None
    current_review_round: int = 0  # Incremented each time a review round starts

    # History
    # Assignment history is a list of agent IDs. Adjacency encodes handoff:
    # [A, B, C] means A was assigned first, handed to B, then to C.
    assignment_history: list[str] = Field(default_factory=list)
    review_history: list[ReviewRecord] = Field(default_factory=list)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None

    # Review requirement - if True, task cannot be completed without approval
    review_required: bool = False

    # =========================================================================
    # Assignment properties (derived from assignment_history)
    # =========================================================================

    @property
    def assigned_to(self) -> Optional[str]:
        """Current assignee (last in assignment history). None if only creator is in history."""
        return self.assignment_history[-1] if len(self.assignment_history) > 1 else None

    @property
    def assigned_by(self) -> str:
        """Who assigned the task to current assignee. First entry is always the creator."""
        if not self.assignment_history:
            raise ValueError("assignment_history is empty — Task must be created through TaskStore.create()")
        if len(self.assignment_history) > 1:
            return self.assignment_history[-2]
        return self.assignment_history[0]

    # =========================================================================
    # Status management
    # =========================================================================

    def update_status(self, new_status: TaskStatus) -> None:
        """Update status with automatic timestamp management."""
        if self.is_finished():
            raise ValueError(f"Cannot transition from terminal state {self.status.value!r} to {new_status.value!r}")
        now = datetime.now(timezone.utc)
        self.status = new_status
        self.updated_at = now
        if new_status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED):
            self.finished_at = now

    def is_finished(self) -> bool:
        """Check if task has finished (reached terminal state: SUCCEEDED or FAILED)."""
        return self.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED)

    def is_in_review(self) -> bool:
        """Check if task is currently in a review round."""
        return self.active_reviews is not None

    def to_analytics(self) -> TaskAnalytics:
        """Create a lightweight read-only view for run analytics."""
        return TaskAnalytics(
            id=self.id,
            title=self.title,
            status=self.status,
            assigned_to=self.assigned_to,
            review_required=self.review_required,
            review_rounds=self.current_review_round,
            created_at=self.created_at,
            finished_at=self.finished_at,
        )

    # =========================================================================
    # Assignment management
    # =========================================================================

    def record_assignee(self, agent_id: str) -> None:
        """Record an assignee in the assignment history."""
        self.assignment_history.append(agent_id)

    # =========================================================================
    # Review workflow
    # =========================================================================

    def start_review_round(
        self,
        reviewers: list[ReviewRecord],
        policy: ReviewPolicy = ReviewPolicy.ALL_APPROVE,
    ) -> None:
        """
        Start a review round with the given reviewers and policy.

        Args:
            reviewers: List of ReviewRecord objects (each contains reviewer_id)
            policy: Policy for determining pass/fail (applied to required reviewers only)

        Raises:
            ValueError: If already in a review round, no reviewers provided,
                        or task is in terminal state
        """
        if self.is_finished():
            raise ValueError("Cannot start review on finished task")
        if self.active_reviews is not None:
            raise ValueError("Already in a review round")
        if not reviewers:
            raise ValueError("Must provide at least one reviewer")

        # Compute next round (don't commit until validation passes)
        next_round = self.current_review_round + 1

        # Build dict for efficient lookup, validate along the way
        pending: dict[str, ReviewRecord] = {}
        for record in reviewers:
            if record.reviewer_id == self.assigned_to:
                raise ValueError(f"Worker {record.reviewer_id} cannot review their own task")
            if record.reviewer_id in pending:
                raise ValueError(f"Duplicate reviewer: {record.reviewer_id}")
            record.round_number = next_round
            pending[record.reviewer_id] = record

        # All validation passed - commit review data
        self.current_review_round = next_round
        self.active_reviews = pending
        self.review_policy = policy

    def submit_review(
        self,
        reviewer_id: str,
        decision: ReviewStatus,
        comment: Optional[str] = None,
    ) -> None:
        """
        Submit a review decision.

        Args:
            reviewer_id: The reviewer submitting
            decision: Their decision (APPROVED or CHANGES_REQUESTED)
            comment: Optional comment from reviewer

        Raises:
            ValueError: If not in review, reviewer not in round, already submitted,
                        or invalid decision
        """
        if not self.is_in_review():
            raise ValueError("Not in a review round")
        if decision not in (ReviewStatus.APPROVED, ReviewStatus.CHANGES_REQUESTED):
            raise ValueError(f"Invalid decision: {decision.value}. Must be 'approved' or 'changes_requested'")
        if reviewer_id not in self.active_reviews:
            raise ValueError(f"Reviewer {reviewer_id} not in this review round")

        record = self.active_reviews[reviewer_id]
        if not record.is_pending():
            raise ValueError(f"Reviewer {reviewer_id} has already submitted")

        record.submit(reviewer_id, decision, comment)
        self.updated_at = datetime.now(timezone.utc)

    def all_reviews_complete(self) -> bool:
        """Check if all reviewers have responded. True if not in a review round."""
        if not self.is_in_review():
            return True
        return all(not r.is_pending() for r in self.active_reviews.values())

    def complete_review_round(self) -> TaskStatus:
        """
        Complete the current review round.

        Computes outcome based on review policy and archives review data.
        Caller (TaskStore) is responsible for setting the resulting status.

        Returns:
            The outcome: APPROVED or CHANGES_REQUESTED

        Raises:
            ValueError: If not in a review round or reviews incomplete
        """
        if not self.is_in_review():
            raise ValueError("Not in a review round")
        if not self.all_reviews_complete():
            raise ValueError("Not all reviews are complete")

        reviews = list(self.active_reviews.values())

        # Compute outcome based on approval threshold
        # Fail fast: response should never be None after all_reviews_complete()
        approvals = 0
        failures = 0
        for r in reviews:
            if r.review is None:
                raise RuntimeError(f"Review from {r.reviewer_id} has no response after all_reviews_complete()")
            if r.review.decision == ReviewStatus.APPROVED:
                approvals += 1
            elif r.review.decision == ReviewStatus.FAILED:
                failures += 1
        non_failed = len(reviews) - failures

        # If all reviewers failed, no quorum to evaluate
        if non_failed == 0:
            outcome = TaskStatus.REVIEW_FAILED
        else:
            # Evaluate policy against non-failed reviewers only
            if self.review_policy == ReviewPolicy.ANY_APPROVE:
                threshold_met = approvals >= 1
            elif self.review_policy == ReviewPolicy.MAJORITY_APPROVE:
                threshold_met = approvals > non_failed / 2
            elif self.review_policy == ReviewPolicy.ALL_APPROVE:
                threshold_met = approvals == non_failed
            else:
                raise RuntimeError(f"Unknown review policy: {self.review_policy}")

            if threshold_met:
                outcome = TaskStatus.APPROVED
            else:
                outcome = TaskStatus.CHANGES_REQUESTED

        # Archive review data
        self.review_history.extend(self.active_reviews.values())
        self.active_reviews = None
        self.review_policy = None

        return outcome

    def get_latest_review_records(self) -> list[ReviewRecord]:
        """Get review records from the most recently completed round."""
        if not self.review_history:
            return []
        last_round = self.review_history[-1].round_number
        return [r for r in self.review_history if r.round_number == last_round]

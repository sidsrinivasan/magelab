"""
TaskStore - Source of truth for task state.

The event loop watches for state changes and wakes relevant agents.
Assignment, status changes, review requests are all task state - not messages.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Callable, Optional

from ..events import (
    Event,
    ResumeEvent,
    ReviewFinishedEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    TaskFinishedEvent,
)
from .database import Database
from .task_schemas import (
    ReviewPolicy,
    ReviewRecord,
    ReviewStatus,
    SystemAgent,
    Task,
    TaskAnalytics,
    TaskStatus,
)

TASKS_DDL = """
CREATE TABLE IF NOT EXISTS task_items (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL,
    status               TEXT NOT NULL,
    review_required      INTEGER NOT NULL DEFAULT 0,
    review_policy        TEXT,
    current_review_round INTEGER NOT NULL DEFAULT 0,
    assignment_history   TEXT NOT NULL DEFAULT '[]',
    active_reviews       TEXT,
    review_history       TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    finished_at          TEXT
);
"""


class TaskStore:
    """
    In-memory task store with event emission.

    Thread-safe via asyncio.Lock. All mutations emit events
    that the event loop can use to wake agents.
    """

    def __init__(self, framework_logger: logging.Logger, db: Optional[Database] = None) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._event_listeners: list[Callable[[Event], None]] = []
        self._framework_logger = framework_logger
        self._db = db
        if self._db:
            self._db.register_schema(TASKS_DDL)

    # =========================================================================
    # Event listeners
    # =========================================================================

    def add_event_listener(self, fn: Callable[[Event], None]) -> None:
        """Register a listener that receives every task event."""
        self._event_listeners.append(fn)

    def _notify_event_listeners(self, event: Event) -> None:
        """Notify all registered event listeners."""
        for fn in self._event_listeners:
            try:
                fn(event)
            except Exception:
                self._framework_logger.exception("Error in task event listener")

    # =========================================================================
    # Core CRUD operations
    # =========================================================================

    async def create(
        self,
        task: Task,
        assigned_to: Optional[str] = None,
        assigned_by: Optional[str] = None,
    ) -> Task:
        """
        Create a new task.

        Args:
            task: The Task object to add to the store.
            assigned_to: Optional agent ID to assign immediately.
            assigned_by: Who is creating/assigning (for tracking).
                Defaults to SystemAgent.USER if not provided.

        Returns:
            The created Task.

        Emits:
            - TaskAssignedEvent if assigned_to is set (via assign())
        """
        event_to_emit: Optional[Event] = None

        async with self._lock:
            if task.status != TaskStatus.CREATED:
                raise ValueError(f"Cannot create task: status is '{task.status.value}', expected 'created'")
            if task.id in self._tasks:
                raise ValueError(f"Task '{task.id}' already exists")

            creator = assigned_by or SystemAgent.USER
            task.record_assignee(creator)
            self._tasks[task.id] = task

            if assigned_to:
                task.record_assignee(assigned_to)
                task.update_status(TaskStatus.ASSIGNED)
                event_to_emit = TaskAssignedEvent(
                    task_id=task.id,
                    target_id=assigned_to,
                    source_id=task.assigned_by,
                )

            self._persist_task(task)

            result = task.model_copy(deep=True)

        if event_to_emit:
            self._notify_event_listeners(event_to_emit)

        return result

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get a deep copy of a task by ID. Returns None if not found."""
        async with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    async def get_task_analytics(self, task_id: str) -> Optional[TaskAnalytics]:
        """Get a lightweight read-only analytics view. Returns None if not found."""
        async with self._lock:
            task = self._tasks.get(task_id)
            return task.to_analytics() if task else None

    async def list_tasks(
        self,
        *,
        status: Optional[TaskStatus] = None,
        assigned_to: Optional[str] = None,
        assigned_by: Optional[str] = None,
        pending_reviewer: Optional[str] = None,
        is_finished: Optional[bool] = None,
    ) -> list[Task]:
        """
        List tasks with optional filters.

        Args:
            status: Filter by status.
            assigned_to: Filter by assignee (worker).
            assigned_by: Filter by delegator (who assigned the task).
            pending_reviewer: Filter by pending reviewer (has pending review from this agent).
            is_finished: If True, only finished tasks (SUCCEEDED/FAILED).

        Returns:
            List of matching tasks.
        """
        async with self._lock:
            tasks = [t.model_copy(deep=True) for t in self._tasks.values()]

        return [
            t
            for t in tasks
            if (status is None or t.status == status)
            and (assigned_to is None or t.assigned_to == assigned_to)
            and (assigned_by is None or t.assigned_by == assigned_by)
            and (pending_reviewer is None or (t.active_reviews and pending_reviewer in t.active_reviews))
            and (is_finished is None or t.is_finished() == is_finished)
        ]

    async def assign(
        self,
        task_id: str,
        to_agent: str,
        by_agent: Optional[str] = None,
    ) -> Task:
        """
        Assign a task to an agent. Sets status to ASSIGNED.

        Args:
            task_id: The task to assign.
            to_agent: New assignee.
            by_agent: Who is assigning (defaults to current assignee).

        Returns:
            The updated Task.

        Raises:
            ValueError: If task not found, finished, or in review.

        Emits:
            - TaskAssignedEvent to the new assignee
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f"Task '{task_id}' not found")
            if task.is_finished():
                raise ValueError(f"Cannot assign finished task ('{task.status.value}')")
            if task.is_in_review():
                raise ValueError("Cannot assign task while in review")

            source = by_agent or task.assigned_to
            if not source:
                raise ValueError("Cannot assign: no by_agent provided and task has no current assignee")

            task.record_assignee(to_agent)
            task.update_status(TaskStatus.ASSIGNED)

            self._persist_task(task)

            event_to_emit = TaskAssignedEvent(
                task_id=task.id,
                target_id=to_agent,
                source_id=source,
            )
            result = task.model_copy(deep=True)

        self._notify_event_listeners(event_to_emit)
        return result

    async def mark_in_progress(self, task_id: str) -> Task:
        """
        Mark a task as in-progress (agent has started working).

        Called by the EventLoop when an agent picks up an TaskAssignedEvent.

        Args:
            task_id: The task to mark.

        Returns:
            The updated Task.

        Raises:
            ValueError: If task not found or not in ASSIGNED status.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f"Task '{task_id}' not found")
            if task.status != TaskStatus.ASSIGNED:
                raise ValueError(f"Cannot mark in-progress: task is '{task.status.value}', expected 'assigned'")

            task.update_status(TaskStatus.IN_PROGRESS)

            self._persist_task(task)

            return task.model_copy(deep=True)

    # =========================================================================
    # Review workflow
    # =========================================================================

    async def submit_for_review(
        self,
        task_id: str,
        reviewers: list[ReviewRecord],
        policy: ReviewPolicy = ReviewPolicy.ALL_APPROVE,
    ) -> Task:
        """
        Submit a task for review.

        Args:
            task_id: The task to submit for review.
            reviewers: List of ReviewRecord objects specifying reviewers.
            policy: Policy for determining pass/fail (ANY_APPROVE, MAJORITY_APPROVE, ALL_APPROVE).

        Returns:
            The updated Task.

        Raises:
            ValueError: If task not found, wrong status, already in review, or invalid reviewers.

        Emits:
            - ReviewRequestedEvent for EACH reviewer
        """
        events_to_emit: list[Event] = []

        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f"Task '{task_id}' not found")

            # Must be actively worked on to submit for review
            valid_for_review = {
                TaskStatus.IN_PROGRESS,
                TaskStatus.CHANGES_REQUESTED,
                TaskStatus.APPROVED,
                TaskStatus.REVIEW_FAILED,
            }
            if task.status not in valid_for_review:
                raise ValueError(
                    f"Cannot submit for review: task is '{task.status.value}', "
                    f"expected one of {', '.join(s.value for s in valid_for_review)}"
                )

            # Start the review round (validates and sets up review data)
            task.start_review_round(reviewers, policy)
            task.update_status(TaskStatus.UNDER_REVIEW)

            self._persist_task(task)

            # Queue events for each reviewer
            for record in reviewers:
                events_to_emit.append(
                    ReviewRequestedEvent(
                        task_id=task.id,
                        target_id=record.reviewer_id,
                        source_id=task.assigned_to,
                        request_message=record.request_message,
                    )
                )

            result = task.model_copy(deep=True)

        # Emit events (outside lock)
        for event in events_to_emit:
            self._notify_event_listeners(event)

        return result

    async def submit_review(
        self,
        task_id: str,
        reviewer_id: str,
        decision: ReviewStatus,
        comment: Optional[str] = None,
    ) -> Task:
        """
        Submit a review decision.

        If all reviews are complete after this submission, evaluates the round
        and updates task status accordingly.

        Args:
            task_id: The task being reviewed.
            reviewer_id: Who is reviewing.
            decision: APPROVED or CHANGES_REQUESTED.
            comment: Reviewer's comment.

        Returns:
            The updated Task.

        Raises:
            ValueError: If task not found, not in review, reviewer not in round, or already submitted.

        Emits:
            - ReviewFinishedEvent (if round completes after this submission)
        """
        event_to_emit: Optional[Event] = None

        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f"Task '{task_id}' not found")
            if task.is_finished():
                raise ValueError(f"Cannot submit review: task already finished ('{task.status.value}')")
            if not task.is_in_review():
                raise ValueError("Cannot submit review: task is not in a review round")

            # Submit the review (validates reviewer and records decision)
            task.submit_review(reviewer_id, decision, comment)

            # Complete round if all reviews are in
            event_to_emit = self._try_complete_review_round(task)

            self._persist_task(task)

            result = task.model_copy(deep=True)

        # Emit event (outside lock)
        if event_to_emit:
            self._notify_event_listeners(event_to_emit)

        return result

    async def mark_review_failed(
        self,
        task_id: str,
        reviewer_id: str,
    ) -> Task:
        """
        Mark a reviewer as failed (reviewer agent crashed).

        If all reviews are complete after this, evaluates the round
        and updates task status accordingly.

        Args:
            task_id: The task being reviewed.
            reviewer_id: The reviewer that failed.

        Returns:
            The updated Task.

        Raises:
            ValueError: If task not found, not in review, reviewer not in round, or already submitted.

        Emits:
            - ReviewFinishedEvent (if round completes after this failure)
        """
        event_to_emit: Optional[Event] = None

        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f"Task '{task_id}' not found")
            if not task.is_in_review():
                raise ValueError("Cannot mark review failed: task is not in a review round")
            if reviewer_id not in task.active_reviews:
                raise ValueError(f"Reviewer {reviewer_id} not in this review round")

            record = task.active_reviews[reviewer_id]
            if not record.is_pending():
                raise ValueError(f"Reviewer {reviewer_id} has already submitted")

            record.mark_failed()

            # Complete round if all reviews are in
            event_to_emit = self._try_complete_review_round(task)

            self._persist_task(task)

            result = task.model_copy(deep=True)

        # Emit event (outside lock)
        if event_to_emit:
            self._notify_event_listeners(event_to_emit)

        return result

    def _try_complete_review_round(self, task: Task) -> Optional[Event]:
        """
        Complete the review round if all reviews are in. Called inside lock.

        Returns the event to emit, or None if reviews are still pending.
        """
        if not task.all_reviews_complete():
            return None

        outcome = task.complete_review_round()
        task.update_status(outcome)
        review_records = task.get_latest_review_records()
        target = task.assigned_to
        if not target:
            self._framework_logger.warning(f"Task {task.id} has no assignee for ReviewFinishedEvent")
            return None

        return ReviewFinishedEvent(
            task_id=task.id,
            target_id=target,
            outcome=outcome,
            review_records=review_records,
        )

    # =========================================================================
    # Termination
    # =========================================================================

    async def mark_finished(
        self,
        task_id: str,
        outcome: TaskStatus,
        details: str,
        *,
        force: bool = False,
    ) -> Task:
        """
        Mark a task as finished (succeeded or failed).

        Args:
            task_id: The task to finish.
            outcome: Must be SUCCEEDED or FAILED.
            details: Explanation of why the task finished (e.g., error message on failure).
            force: If True, allows FAILED to override "in review" guard and treats double-fail as a no-op. Used by the framework's failure handler when an agent is dead.

        Returns:
            The updated Task.

        Raises:
            ValueError: If task not found, outcome invalid, task already finished,
                        or review gating fails (SUCCEEDED requires APPROVED
                        when review_required=True).

        Emits:
            - TaskFinishedEvent to the agent that delegated this task
        """
        if outcome not in (TaskStatus.SUCCEEDED, TaskStatus.FAILED):
            raise ValueError(f"Outcome must be 'succeeded' or 'failed', got '{outcome.value}'")

        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f"Task '{task_id}' not found")
            if task.is_finished():
                if force and outcome == TaskStatus.FAILED:
                    self._framework_logger.warning(
                        f"Task '{task_id}' already finished ('{task.status.value}'), skipping double-fail"
                    )
                    return task.model_copy(deep=True)
                raise ValueError(f"Task is already finished ('{task.status.value}')")
            if task.is_in_review():
                if force and outcome == TaskStatus.FAILED:
                    task.active_reviews = None  # cancel moot review round
                    task.review_policy = None
                    self._framework_logger.warning(
                        f"Task '{task_id}' is in review, but forcing failure and cancelling review"
                    )
                else:
                    raise ValueError("Cannot finish task while in review")

            # SUCCEEDED validation
            if outcome == TaskStatus.SUCCEEDED:
                valid_for_success = {TaskStatus.IN_PROGRESS, TaskStatus.APPROVED}
                if task.status not in valid_for_success:
                    raise ValueError(
                        f"Cannot mark succeeded: task is '{task.status.value}', expected 'in_progress' or 'approved'"
                    )
                if task.review_required and task.status != TaskStatus.APPROVED:
                    raise ValueError(
                        f"Cannot mark succeeded: task requires review and is '{task.status.value}', expected 'approved'"
                    )

            task.update_status(outcome)

            self._persist_task(task)

            event_to_emit = TaskFinishedEvent(
                task_id=task.id,
                target_id=task.assigned_by,
                outcome=outcome,
                details=details,
            )
            result = task.model_copy(deep=True)

        self._notify_event_listeners(event_to_emit)
        return result

    async def all_finished(self) -> bool:
        """Check if all tasks have finished (SUCCEEDED or FAILED)."""
        async with self._lock:
            if not self._tasks:
                return True
            return all(t.is_finished() for t in self._tasks.values())

    # =========================================================================
    # Event Staleness check
    # =========================================================================

    async def is_event_stale(self, event: Event) -> bool:
        """Check if an event is stale (task state has moved past it)."""
        async with self._lock:
            task = self._tasks.get(event.task_id)
            if not task:
                return True

            if isinstance(event, TaskAssignedEvent):
                return (
                    task.is_finished()
                    or task.is_in_review()
                    or task.assigned_to != event.target_id
                    or task.status != TaskStatus.ASSIGNED
                )

            if isinstance(event, ReviewRequestedEvent):
                return (
                    not task.is_in_review()
                    or task.active_reviews is None
                    or event.target_id not in task.active_reviews
                    or not task.active_reviews[event.target_id].is_pending()
                )

            if isinstance(event, ReviewFinishedEvent):
                return (
                    task.is_finished()
                    or task.is_in_review()
                    or task.assigned_to != event.target_id
                    or task.status not in (TaskStatus.APPROVED, TaskStatus.CHANGES_REQUESTED, TaskStatus.REVIEW_FAILED)
                )

            if isinstance(event, TaskFinishedEvent):
                return not task.is_finished()

            if isinstance(event, ResumeEvent):
                return task.is_finished()

            raise TypeError(f"Unknown event type: {type(event).__name__}")

    # =========================================================================
    # Persistence
    # =========================================================================

    def _persist_task(self, task: Task) -> None:
        """Write task state to DB if available. Raises on failure to keep DB consistent."""
        if not self._db:
            return

        active_reviews_json = None
        if task.active_reviews is not None:
            active_reviews_json = json.dumps({k: v.model_dump(mode="json") for k, v in task.active_reviews.items()})

        review_history_json = json.dumps([r.model_dump(mode="json") for r in task.review_history])

        self._db.execute(
            """INSERT INTO task_items (id, title, description, status, review_required, review_policy,
                   current_review_round, assignment_history, active_reviews, review_history,
                   created_at, updated_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   title = excluded.title, description = excluded.description,
                   status = excluded.status, review_required = excluded.review_required,
                   review_policy = excluded.review_policy,
                   current_review_round = excluded.current_review_round,
                   assignment_history = excluded.assignment_history,
                   active_reviews = excluded.active_reviews,
                   review_history = excluded.review_history,
                   updated_at = excluded.updated_at, finished_at = excluded.finished_at
            """,
            (
                task.id,
                task.title,
                task.description,
                task.status.value,
                1 if task.review_required else 0,
                task.review_policy.value if task.review_policy else None,
                task.current_review_round,
                json.dumps(task.assignment_history),
                active_reviews_json,
                review_history_json,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
                task.finished_at.isoformat() if task.finished_at else None,
            ),
        )
        self._db.commit()

    def compute_task_counts(self) -> dict:
        """Count tasks by terminal status. Returns {succeeded, failed, open}."""
        if not self._db:
            return {"succeeded": 0, "failed": 0, "open": 0}
        succeeded_val = TaskStatus.SUCCEEDED.value
        failed_val = TaskStatus.FAILED.value
        row = self._db.fetchone(
            """
            SELECT
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS succeeded,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status NOT IN (?, ?) THEN 1 ELSE 0 END) AS open
            FROM task_items
            """,
            (succeeded_val, failed_val, succeeded_val, failed_val),
        )
        return {
            "succeeded": (row["succeeded"] or 0) if row else 0,
            "failed": (row["failed"] or 0) if row else 0,
            "open": (row["open"] or 0) if row else 0,
        }

    def load_from_db(self) -> int:
        """Load all tasks from DB into the in-memory store. Bypasses events.

        Reconstructs full Task domain objects from DB rows, parsing JSON
        columns (assignment_history, active_reviews, review_history).
        Must be called before the event loop starts (no lock needed).

        Returns the number of tasks loaded.
        """
        if not self._db:
            return 0
        for row in self._db.fetchall("SELECT * FROM task_items"):
            assignment_history = json.loads(row["assignment_history"])

            active_reviews = None
            if row["active_reviews"] is not None:
                raw = json.loads(row["active_reviews"])
                active_reviews = {k: ReviewRecord.model_validate(v) for k, v in raw.items()}

            review_history = [ReviewRecord.model_validate(r) for r in json.loads(row["review_history"])]

            task = Task(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                status=TaskStatus(row["status"]),
                review_required=bool(row["review_required"]),
                review_policy=ReviewPolicy(row["review_policy"]) if row["review_policy"] else None,
                current_review_round=row["current_review_round"],
                assignment_history=assignment_history,
                active_reviews=active_reviews,
                review_history=review_history,
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            )
            self._tasks[task.id] = task
        return len(self._tasks)

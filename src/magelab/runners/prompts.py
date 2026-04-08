"""
Prompt templates and formatting for magelab framework.

Contains the framework system prompt, event-driven prompt templates,
and the default prompt formatter that maps events to agent prompts.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

from ..events import (
    Event,
    MCPEvent,
    ResumeEvent,
    ReviewFinishedEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    TaskFinishedEvent,
    WireMessageEvent,
)
from ..state.task_schemas import (
    ReviewRecord,
    Task,
    TaskStatus,
)

# =============================================================================
# Review Formatting
# =============================================================================


def format_reviews(reviews: list[ReviewRecord]) -> str:
    """Format review records as JSON for inclusion in prompts."""
    if not reviews:
        return "No reviews."
    return json.dumps([r.model_dump(mode="json") for r in reviews], indent=4)


def format_review_history(reviews: list[ReviewRecord]) -> str:
    """Format review history with header. Returns empty string if no reviews. For use in prompts that may want to omit the section entirely if no reviews."""
    if not reviews:
        return ""
    return f"# Review History\n{format_reviews(reviews)}"


def _format_open_task_reminder(tasks: list[Task]) -> str:
    """Format a reminder about other in-progress tasks. Returns empty string if none."""
    if not tasks:
        return ""
    task_ids = ", ".join(f"`{t.id}`" for t in tasks)
    return (
        f"<reminder>Please mark as finished when your work on the following tasks is complete: {task_ids}. </reminder>"
    )


# =============================================================================
# Prompt Context
# =============================================================================


@dataclass
class PromptContext:
    """
    Context passed to prompt formatters.

    Built by the event loop from current state. Formatters receive
    pre-fetched data and format it into prompts.
    """

    event: Event
    """The event that triggered this agent run."""

    task: Optional[Task] = None
    """The task associated with the event (if any)."""

    agent_tools: set[str] = field(default_factory=set)
    """Tool names available to the agent. Used to tailor instructions to capabilities."""

    wire_conversations: list[str] = field(default_factory=list)
    """Pre-formatted conversation texts for wire events (resolved by orchestrator via Wire.format_conversation)."""

    other_open_tasks: list[Task] = field(default_factory=list)
    """Other non-finished tasks assigned to this agent (excluding the current one)."""


# =============================================================================
# System Prompt
# =============================================================================


def build_system_prompt(agent_id: str, role_prompt: str, org_prompt: str, working_directory: str) -> str:
    """Assemble the full system prompt: org-level prompt + role-specific prompt.

    org_prompt supports an optional '{agent_id}' placeholder.
    Prepends a notification with the working directory path so agents know where
    they are from turn 1.
    """
    resolved = org_prompt.replace("{agent_id}", agent_id)
    parts = [f"<system-message>\nYour current working directory is: {working_directory}\n</system-message>"]
    parts.append(resolved)
    parts.append(role_prompt)
    return "\n".join(parts)


# =============================================================================
# Event Prompt Templates
# =============================================================================

_YIELD_REMINDER = (
    " You may end your turn once you have no remaining work. You will be reactivated when new events arrive."
)
_YIELD_NUDGE = " If you have no remaining work, you may end your turn. You will be reactivated when new events arrive."
_REVIEW_DECISION_INFO = (
    " Your review decision will only be delivered on a subsequent turn, do not remain active to wait for it."
)

TASK_ASSIGNED_TEMPLATE = """\
<system-instructions>
{instructions}
</system-instructions>

Task ID: {task_id}
Title: '{title}'
Assigned By: {source}

# Task
{description}
{review_history}
{open_task_reminder}"""

REVIEW_REQUESTED_TEMPLATE = """\
<system-instructions>
{instructions}
</system-instructions>

Task ID: {task_id}
Title: '{title}'
Review Requested By: {source}
Review Request Message: {message}

# Task
{description}
{review_history}"""

REVIEW_OUTCOME_TEMPLATE = """\
<system-instructions>
{instructions}
</system-instructions>

Task ID: {task_id}
Title: '{title}'

# Task
{description}

# Reviews
{reviews}"""

TASK_FINISHED_PROMPT = """\
<notification>
This is a notification that a task you assigned has been marked finished. If you have no remaining work, you may end your turn. You will be reactivated when new events arrive.
</notification>

Task ID: {task_id}
Title: '{title}'
Outcome: {outcome}

# Details
{details}"""


def _build_task_assigned_prompt(task: Task, source: str, tools: set[str], other_open_tasks: list[Task]) -> str:
    """Build prompt for a newly assigned task, tailored to agent capabilities."""
    can_request_review = "tasks_submit_for_review" in tools
    can_discover_reviewers = "get_available_reviewers" in tools
    can_finish = "tasks_mark_finished" in tools

    if task.review_required and not can_request_review:
        raise ValueError(f"Task '{task.id}' requires review but assigned agent lacks 'tasks_submit_for_review' tool")

    instructions = "Your task details are given below."
    if task.review_required and can_request_review:
        if can_discover_reviewers:
            instructions += (
                " Once complete, get available reviewers to find reviewers"
                " with relevant expertise, then submit your work to them"
                " for review with a message describing what you did." + _YIELD_REMINDER + _REVIEW_DECISION_INFO
            )
        else:
            instructions += (
                " Once complete, submit your work for review with a message describing what you did."
                + _YIELD_REMINDER
                + _REVIEW_DECISION_INFO
            )
    elif can_finish and can_request_review:
        if can_discover_reviewers:
            instructions += (
                " Once complete, you may mark the task as finished directly,"
                " or optionally request advisory reviews by getting available reviewers"
                " and submitting your work for review." + _YIELD_REMINDER + _REVIEW_DECISION_INFO
            )
        else:
            instructions += (
                " Once complete, you may mark the task as finished directly, or optionally submit your work for review."
                + _YIELD_REMINDER
                + _REVIEW_DECISION_INFO
            )
    elif can_finish:
        instructions += " Once all work on this task is complete, mark the task as finished." + _YIELD_REMINDER
    else:
        instructions += _YIELD_REMINDER

    review_history = format_review_history(task.review_history)
    open_task_reminder = _format_open_task_reminder(other_open_tasks) if can_finish else ""

    return TASK_ASSIGNED_TEMPLATE.format(
        instructions=instructions,
        task_id=task.id,
        title=task.title,
        source=source,
        description=task.description,
        review_history="\n" + review_history if review_history else "",
        open_task_reminder=open_task_reminder,
    )


def _build_review_requested_prompt(task: Task, source: str, message: Optional[str], tools: set[str]) -> str:
    """Build prompt for a review request, tailored to agent capabilities."""
    can_submit_review = "tasks_submit_review" in tools

    instructions = "You have been asked to review another agent's work on their assigned task."
    if can_submit_review:
        instructions += (
            " Please comprehensively review the agent's work and submit your review with a decision and comments."
            " If the agent's work satisfactorily completes the task, approve the review and provide any advisory feedback."
            " If the agent has not satisfactorily completed the task, request changes with detailed feedback."
            + _YIELD_REMINDER
        )
    else:
        instructions += " Please comprehensively review the agent's work on their task." + _YIELD_REMINDER

    review_history = format_review_history(task.review_history)

    return REVIEW_REQUESTED_TEMPLATE.format(
        instructions=instructions,
        task_id=task.id,
        title=task.title,
        source=source,
        message=message or "None",
        description=task.description,
        review_history="\n" + review_history if review_history else "",
    )


def _build_review_approved_prompt(task: Task, reviews: list[ReviewRecord], tools: set[str]) -> str:
    """Build prompt for approved review, tailored to agent capabilities."""
    can_finish = "tasks_mark_finished" in tools
    can_request_review = "tasks_submit_for_review" in tools

    instructions = "Reviewers have approved your work on the task below."
    if can_finish and can_request_review:
        instructions += (
            " You may now either mark it as complete,"
            " or make further changes based on any reviewer comments and resubmit for review." + _YIELD_REMINDER
        )
    elif can_finish:
        instructions += " You may now mark it as complete." + _YIELD_REMINDER
    elif can_request_review:
        instructions += (
            " If you make further changes based on any reviewer comments, you must resubmit the task for review."
            + _YIELD_REMINDER
        )
    else:
        instructions += " No further action is needed from you on this task." + _YIELD_NUDGE

    return REVIEW_OUTCOME_TEMPLATE.format(
        instructions=instructions,
        task_id=task.id,
        title=task.title,
        description=task.description,
        reviews=format_reviews(reviews),
    )


def _build_review_failed_prompt(task: Task, reviews: list[ReviewRecord], tools: set[str]) -> str:
    """Build prompt for failed review, tailored to agent capabilities."""
    can_finish = "tasks_mark_finished" in tools
    can_request_review = "tasks_submit_for_review" in tools

    instructions = "Reviewers were not able to complete their review of your work on the task below."
    actions = []
    if can_request_review:
        actions.append("re-attempt to submit the task for review")
    if can_finish:
        actions.append("mark the task as failed")
    if actions:
        instructions += " You may " + ", or ".join(actions) + "."
    if can_request_review:
        instructions += " You may also choose to make changes to the task before resubmitting for review."
    instructions += _YIELD_REMINDER

    return REVIEW_OUTCOME_TEMPLATE.format(
        instructions=instructions,
        task_id=task.id,
        title=task.title,
        description=task.description,
        reviews=format_reviews(reviews),
    )


def _build_changes_requested_prompt(task: Task, reviews: list[ReviewRecord], tools: set[str]) -> str:
    """Build prompt for changes requested, tailored to agent capabilities."""
    can_request_review = "tasks_submit_for_review" in tools

    instructions = (
        "Reviewers have requested changes on your work for the task below."
        " Please carefully review the feedback provided by the reviewers"
        " and make necessary changes to your work on the task."
    )
    if can_request_review:
        instructions += (
            " Once you have made the necessary changes, resubmit the task for review"
            " with a message detailing the changes you made in response to the reviewer feedback."
            + _REVIEW_DECISION_INFO
        )
    instructions += _YIELD_REMINDER
    return REVIEW_OUTCOME_TEMPLATE.format(
        instructions=instructions,
        task_id=task.id,
        title=task.title,
        description=task.description,
        reviews=format_reviews(reviews),
    )


_REVIEW_FINISHED_BUILDERS = {
    TaskStatus.APPROVED: _build_review_approved_prompt,
    TaskStatus.REVIEW_FAILED: _build_review_failed_prompt,
    TaskStatus.CHANGES_REQUESTED: _build_changes_requested_prompt,
}


# =============================================================================
# Wire Prompt Templates
# =============================================================================

WIRE_TEMPLATE = """\
<notification>
{notification}
</notification>

{conversation}"""


def _build_wire_prompt(conversations: list[str]) -> Optional[str]:
    """Build prompt for wire events — single message or batched."""
    if not conversations:
        return None
    if len(conversations) == 1:
        notification = "You have a new message." + _YIELD_REMINDER
    else:
        notification = "You have new messages." + _YIELD_REMINDER

    return WIRE_TEMPLATE.format(notification=notification, conversation="\n\n".join(conversations))


# =============================================================================
# Default Prompt Formatter
# =============================================================================


def default_prompt_formatter(ctx: PromptContext) -> Optional[str]:
    """
    Default prompt formatter for all event types.

    Returns a prompt string for handled events, or None for unhandled events.
    Task info comes from ctx.task (fetched fresh by event loop), not from the event.
    """
    event = ctx.event
    task = ctx.task
    tools = ctx.agent_tools

    # MCP events — payload rendered verbatim, server controls content
    if isinstance(event, MCPEvent):
        return event.payload

    # Wire events — no task needed, just conversation text
    if isinstance(event, WireMessageEvent):
        return _build_wire_prompt(ctx.wire_conversations)

    if isinstance(event, ResumeEvent):
        return "You were interrupted while working. Please continue where you left off." + _YIELD_REMINDER

    if not task:
        return None

    if isinstance(event, TaskAssignedEvent):
        return _build_task_assigned_prompt(
            task, source=event.source_id, tools=tools, other_open_tasks=ctx.other_open_tasks
        )

    if isinstance(event, ReviewRequestedEvent):
        return _build_review_requested_prompt(task, source=event.source_id, message=event.request_message, tools=tools)

    if isinstance(event, ReviewFinishedEvent):
        builder = _REVIEW_FINISHED_BUILDERS.get(event.outcome)
        if not builder:
            return None
        return builder(task, reviews=event.review_records, tools=tools)

    if isinstance(event, TaskFinishedEvent):
        outcome = "succeeded" if event.outcome == TaskStatus.SUCCEEDED else "failed"
        return TASK_FINISHED_PROMPT.format(task_id=task.id, title=task.title, outcome=outcome, details=event.details)

    return None

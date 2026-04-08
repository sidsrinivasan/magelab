"""
Tool specifications for the magelab framework.

Each ToolSpec defines a tool's name, description, and parameter schema.
FRAMEWORK is the canonical registry of all framework tools.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """Metadata for a single framework tool — runner-agnostic."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolResponse:
    """Return type for framework tool implementations — runner-agnostic."""

    text: str
    is_error: bool = False


# =============================================================================
# Individual framework tools
# =============================================================================

tasks_create = ToolSpec(
    name="tasks_create",
    description=(
        "Create a new task. "
        "Task remains unassigned if assigned_to is not specified. "
        "review_required defaults to false if not specified."
    ),
    parameters={"id": str, "title": str, "description": str, "assigned_to": str, "review_required": bool},
)
tasks_create_batch = ToolSpec(
    name="tasks_create_batch",
    description=(
        "Create multiple tasks at once. Pass a JSON array of task objects. "
        "Each object must have: 'id', 'title', 'description'. "
        "Task remains unassigned if 'assigned_to' is not specified. "
        "'review_required' defaults to false if not specified."
    ),
    parameters={"tasks": list},
)
tasks_assign = ToolSpec(
    name="tasks_assign",
    description="Assign a task to an agent",
    parameters={"task_id": str, "to_agent": str},
)
tasks_submit_for_review = ToolSpec(
    name="tasks_submit_for_review",
    description=(
        "Submit a task for review by one or more reviewers. "
        "'reviewers' is a JSON object mapping reviewer_id to request message: "
        '{"agent_a": "Please check the auth logic", "agent_b": "General review"}. '
        "Policy (for approval) can be 'any', 'majority', or 'all' (default)."
    ),
    parameters={"task_id": str, "reviewers": str, "review_policy": str},
)
tasks_submit_review = ToolSpec(
    name="tasks_submit_review",
    description="Submit a review of the task you are reviewing. Decision must be 'approved' or 'changes_requested'.",
    parameters={"task_id": str, "decision": str, "comment": str},
)
tasks_mark_finished = ToolSpec(
    name="tasks_mark_finished",
    description=(
        "Mark a task as finished. Outcome must be 'succeeded' or 'failed'. "
        "Details should explain why (e.g., what was accomplished, or what went wrong). "
        "If the task requires approval, it must be APPROVED by reviewers before you can mark succeeded."
    ),
    parameters={"task_id": str, "outcome": str, "details": str},
)
tasks_get = ToolSpec(
    name="tasks_get",
    description="Get a task by ID",
    parameters={"task_id": str},
)
tasks_list = ToolSpec(
    name="tasks_list",
    description=(
        "List tasks assigned to you and your connections. "
        "Use filters to narrow results: assigned_to, assigned_by, is_finished."
    ),
    parameters={
        "assigned_to": str,
        "assigned_by": str,
        "is_finished": bool,
    },
)
get_available_reviewers = ToolSpec(
    name="get_available_reviewers",
    description=(
        "List agents connected to you who can review your work. "
        "Returns per agent: agent_id, role, state, queued_tasks "
        "(list of {task_id, title, description, type} for pending work and reviews), "
        "completed_work count, completed_reviews count, and failed_work count. "
    ),
    parameters={},
)
connections_list = ToolSpec(
    name="connections_list",
    description=("Discover agents you can interact with."),
    parameters={},
)
sleep = ToolSpec(
    name="sleep",
    description=(
        "Pause execution for a specified duration (in seconds) before resuming work. "
        "Useful if needed to wait for another agent before you can proceed. "
        "Duration must be between 0 and 60 seconds."
    ),
    parameters={"duration_seconds": int},
)

# Communication tools
send_message = ToolSpec(
    name="send_message",
    description=(
        "Send a message to other agents. "
        "To start a new conversation or message existing participants, provide 'recipients' as a JSON array of agent ID strings — "
        "if a conversation with those exact participants already exists, your message is added to it. "
        "To reply to a specific conversation, provide 'conversation_id' (a string). "
        "Use one and leave the other one empty. "
        "Returns a conversation_id for follow-up messages."
    ),
    parameters={"recipients": list, "conversation_id": str, "body": str},
)
read_messages = ToolSpec(
    name="read_messages",
    description=(
        "Read messages in a conversation. Shows all unread messages plus some prior context. "
        "'num_previous' controls how many messages before the first unread are shown (default 2). "
        "Returns up to 15 messages at a time. You may call again to see any further unread messages."
    ),
    parameters={"conversation_id": str, "num_previous": int},
)
batch_read_messages = ToolSpec(
    name="batch_read_messages",
    description=(
        "Read unread messages from multiple conversations at once. "
        "Returns up to 15 messages per conversation from up to 5 conversations with unread messages, "
        "sorted by most recent. All returned messages are marked as read."
    ),
    parameters={},
)
conversations_list = ToolSpec(
    name="conversations_list",
    description=(
        "List your conversations, sorted by most recently updated. "
        "By default only shows conversations with unread messages. "
        "Set 'unread_only' to false to show all conversations."
    ),
    parameters={"unread_only": bool},
)

# All framework tool specs — single source of truth
FRAMEWORK: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        tasks_create,
        tasks_create_batch,
        tasks_assign,
        tasks_submit_for_review,
        tasks_submit_review,
        tasks_mark_finished,
        tasks_get,
        tasks_list,
        connections_list,
        get_available_reviewers,
        sleep,
        send_message,
        read_messages,
        batch_read_messages,
        conversations_list,
    ]
}

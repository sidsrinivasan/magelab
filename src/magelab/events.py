"""
Event types for magelab framework.

Events are thin dataclasses that carry a target_id and minimal immutable data.
They are emitted by stores (TaskStore, WireStore) and routed to agents by the Orchestrator.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .state.task_schemas import ReviewRecord, TaskStatus


def _short_uuid() -> str:
    return uuid.uuid4().hex[:8]


class EventOutcome(str, Enum):
    """Outcome of an event in the dispatch pipeline."""

    DELIVERED = "delivered"
    COMPLETED = "completed"
    STALE_AT_DELIVERY = "stale_at_delivery"
    ERROR_AT_DELIVERY = "error_at_delivery"
    DROPPED_AT_ENQUEUE = "dropped_at_enqueue"
    DROPPED_ON_RESTART = "dropped_on_restart"


# =============================================================================
# Base
# =============================================================================


@dataclass(kw_only=True)
class BaseEvent:
    """Common fields for all events (task events, wire events, etc.)."""

    event_id: str = field(default_factory=_short_uuid)
    target_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# =============================================================================
# Task events
# =============================================================================


@dataclass(kw_only=True)
class TaskAssignedEvent(BaseEvent):
    """You've been assigned a task."""

    task_id: str
    source_id: str


@dataclass(kw_only=True)
class ReviewRequestedEvent(BaseEvent):
    """Please review this task."""

    task_id: str
    source_id: str
    request_message: Optional[str] = None


@dataclass(kw_only=True)
class ReviewFinishedEvent(BaseEvent):
    """A review round finished (approved, changes requested, or failed)."""

    task_id: str
    outcome: TaskStatus
    review_records: list[ReviewRecord]


@dataclass(kw_only=True)
class TaskFinishedEvent(BaseEvent):
    """A task you delegated has finished."""

    task_id: str
    outcome: TaskStatus
    details: str


# =============================================================================
# Wire events
# =============================================================================


@dataclass(kw_only=True)
class WireMessageEvent(BaseEvent):
    """A new message was posted in a conversation you're in."""

    wire_id: str
    source_id: str
    message_cursor: int  # cursor position after this message; event is stale if agent's read cursor >= this


# =============================================================================
# Resume event
# =============================================================================


@dataclass(kw_only=True)
class ResumeEvent(BaseEvent):
    """Agent was interrupted mid-work and should resume.

    Used in resume-continue mode to nudge agents that were WORKING or REVIEWING
    when the run was stopped. The agent's session should have full conversation
    context via session ID; this event just triggers a continuation prompt.
    """

    task_id: Optional[str] = None
    was_reviewing: bool = False  # True if agent was reviewing, False if working


# =============================================================================
# MCP events
# =============================================================================


@dataclass(kw_only=True)
class MCPEvent(BaseEvent):
    """Event emitted by an in-process MCP server.

    The server controls the prompt content entirely via ``payload``,
    which is rendered verbatim to the target agent.
    """

    server_name: str  # which MCP server emitted this (for logging/debugging)
    payload: str  # rendered verbatim as the agent prompt


# =============================================================================
# Union
# =============================================================================

Event = (
    TaskAssignedEvent
    | ReviewRequestedEvent
    | ReviewFinishedEvent
    | TaskFinishedEvent
    | WireMessageEvent
    | ResumeEvent
    | MCPEvent
)

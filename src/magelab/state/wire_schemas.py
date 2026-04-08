"""
Wire domain types — conversations, messages, and snapshots.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class WireMessage(BaseModel):
    """A single message in a wire conversation."""

    sender: str
    body: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Wire(BaseModel):
    """
    A conversation thread with fixed participants.

    Participants are fixed at creation.
    No join/leave — start a new wire if you need different participants.

    Read state is tracked per-participant via read_cursors.
    Cursor = index of first unread message. Unread count = len(messages) - cursor.
    """

    wire_id: str
    read_cursors: dict[str, int]  # participant_id -> index of first unread message
    messages: list[WireMessage]
    task_id: Optional[str] = None  # optional link to a task for analysis

    @model_validator(mode="after")
    def _require_initial_message(self) -> "Wire":
        if not self.messages:
            raise ValueError("Conversation must have at least one message")
        return self

    @property
    def participants(self) -> frozenset[str]:
        """Immutable set of participant IDs."""
        return frozenset(self.read_cursors.keys())

    @property
    def created_by(self) -> str:
        """Derived from the first message sender. Raises IndexError if messages is empty (violated invariant)."""
        return self.messages[0].sender

    def _require_participant(self, agent_id: str) -> None:
        if agent_id not in self.read_cursors:
            raise ValueError(f"Agent '{agent_id}' is not a participant in conversation '{self.wire_id}'")

    def unread_count(self, agent_id: str) -> int:
        """Number of unread messages for this agent."""
        self._require_participant(agent_id)
        return len(self.messages) - self.read_cursors[agent_id]

    def first_unread_index(self, agent_id: str) -> int:
        """Index of the first unread message for this agent."""
        self._require_participant(agent_id)
        return self.read_cursors[agent_id]

    def format_conversation(self, agent_id: str, num_previous: int = 2, max_messages: int = 15) -> tuple[str, int]:
        """Format conversation for reading, with header, message window, and overflow notice.

        The window starts num_previous messages before the first unread and extends
        up to max_messages total (context + unread share the budget). If more unread
        messages remain beyond the window, an overflow notice is appended.

        Returns (formatted_text, end_index) where end_index is the cursor position for mark_read.
        """
        unread = self.unread_count(agent_id)
        boundary = self.first_unread_index(agent_id)
        start = max(0, boundary - num_previous)
        end = min(len(self.messages), start + max_messages)

        total = len(self.messages)
        header = (
            f"Conversation {self.wire_id} ({unread} unread / {total} messages)\n"
            f"Participants: {', '.join(sorted(self.participants))}\n"
            f"---"
        )

        lines = []
        for i in range(start, end):
            msg = self.messages[i]
            marker = " [new]" if i >= boundary else ""
            if lines:
                lines.append("---")
            lines.append(f"[{i}]{marker} {msg.sender} ({msg.timestamp:%H:%M}): {msg.body}")

        unread_shown = end - boundary
        remaining_unread = unread - unread_shown
        if remaining_unread > 0:
            lines.append(
                f"\nShowing {unread_shown} of {unread} unread messages. {remaining_unread} unread messages remain in this conversation."
            )

        return header + "\n" + "\n".join(lines), end

    def mark_read(self, agent_id: str, up_to: int) -> None:
        """Advance cursor to up_to (only moves forward, never backward)."""
        self._require_participant(agent_id)
        self.read_cursors[agent_id] = max(self.read_cursors[agent_id], up_to)


@dataclass(frozen=True)
class WireSnapshot:
    """Lightweight view of a wire. Agent-specific (unread_count varies per agent)."""

    wire_id: str
    participants: frozenset[str]
    message_count: int
    unread_count: int
    last_message_sender: str
    last_message_preview: str
    last_message_at: datetime

    def format(self, viewer_id: str) -> str:
        """Format as a multi-line summary, excluding viewer from participant list."""
        others = sorted(p for p in self.participants if p != viewer_id)
        return (
            f"{self.wire_id} ({self.unread_count} unread / {self.message_count} messages)\n"
            f"  with: {', '.join(others)}\n"
            f"  last:\n"
            f"      {self.last_message_sender}: {self.last_message_preview}"
        )

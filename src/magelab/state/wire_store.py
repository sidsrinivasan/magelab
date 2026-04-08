"""
WireStore - Source of truth for wire (conversation) state.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Callable, Optional

from ..org_config import WireNotifications
from ..events import BaseEvent, WireMessageEvent
from .database import Database
from .wire_schemas import Wire, WireMessage, WireSnapshot

WIRES_DDL = """
CREATE TABLE IF NOT EXISTS wire_meta (
    wire_id      TEXT PRIMARY KEY,
    participants TEXT NOT NULL,
    task_id      TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wire_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wire_id   TEXT NOT NULL REFERENCES wire_meta(wire_id),
    sender_id TEXT NOT NULL,
    body      TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wire_read_cursors (
    wire_id        TEXT NOT NULL REFERENCES wire_meta(wire_id),
    participant_id TEXT NOT NULL,
    cursor_position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wire_id, participant_id)
);

CREATE INDEX IF NOT EXISTS idx_wire_messages_wire
    ON wire_messages (wire_id);
"""

# =============================================================================
# WireStore
# =============================================================================


class WireStore:
    """
    In-memory wire store with event emission.

    Mirrors TaskStore patterns: asyncio.Lock for thread safety,
    emit events outside the lock via registered event listeners.

    Constraint: exactly one wire per participant set. Sending to the same
    recipients always routes to the same conversation (auto-matching).
    This is intentional — agents don't need topic-specific threads.
    """

    def __init__(
        self,
        framework_logger: logging.Logger,
        db: Optional[Database] = None,
        wire_notifications: WireNotifications = WireNotifications.ALL,
        wire_max_unread_per_prompt: int = 10,
    ) -> None:
        self.wire_notifications = wire_notifications
        self.wire_max_unread_per_prompt = wire_max_unread_per_prompt
        self._wires: dict[str, Wire] = {}
        self._lock = asyncio.Lock()
        self._event_listeners: list[Callable[[BaseEvent], None]] = []
        self._message_listeners: list[Callable[[str, frozenset[str], str, str], None]] = []
        self._framework_logger = framework_logger
        self._db = db
        if self._db:
            self._db.register_schema(WIRES_DDL)

        # Participant index: frozenset of participant IDs -> wire_id (1:1, enforced at creation)
        # Used for auto-matching in send_message
        self._participant_index: dict[frozenset[str], str] = {}

    # =========================================================================
    # Listeners
    # =========================================================================

    def add_event_listener(self, fn: Callable[[BaseEvent], None]) -> None:
        """Register a listener that receives wire events (WireMessageEvent)."""
        self._event_listeners.append(fn)

    def add_message_listener(self, fn: Callable[[str, frozenset[str], str, str], None]) -> None:
        """Register a listener for wire messages. Callback receives (wire_id, participants, sender, body).

        Always invoked regardless of wire_notifications setting (used for transcript logging)."""
        self._message_listeners.append(fn)

    def _notify_event_listeners(self, event: BaseEvent) -> None:
        """Notify event listeners (if event notifications enabled)."""
        if self.wire_notifications in (WireNotifications.ALL, WireNotifications.EVENT):
            for fn in self._event_listeners:
                try:
                    fn(event)
                except Exception:
                    self._framework_logger.exception("Error in wire event listener")

    def _notify_message_listeners(self, wire_id: str, participants: frozenset[str], sender: str, body: str) -> None:
        """Notify all message listeners of a new wire message."""
        for fn in self._message_listeners:
            try:
                fn(wire_id, participants, sender, body)
            except Exception:
                self._framework_logger.exception("Error in wire message listener")

    # =========================================================================
    # Wire CRUD
    # =========================================================================

    async def create_wire(
        self,
        wire_id: str,
        participants: list[str],
        sender: str,
        body: str,
        task_id: Optional[str] = None,
    ) -> Wire:
        """
        Create a new wire with an initial message from a sender.
        Sender is automatically added to the wire as a participant
        if not already present.

        Args:
            wire_id: Unique ID for the wire.
            participants: Participant list.
            sender: Who is sending the first message.
            body: The message body.
            task_id: Optional task link for analysis.

        Returns:
            The created Wire.

        Emits:
            WireMessageEvent to each participant except the sender.
        """
        participant_key = frozenset([*participants, sender])
        message = WireMessage(sender=sender, body=body)

        events_to_emit: list[WireMessageEvent] = []

        async with self._lock:
            if wire_id in self._wires:
                raise ValueError(f"Wire '{wire_id}' already exists")
            if participant_key in self._participant_index:
                existing = self._participant_index[participant_key]
                raise ValueError(f"Wire already exists for participants {sorted(participants)}: '{existing}'")

            # Sender cursor = 1 (seen own message), others = 0 (1 unread)
            cursors = {pid: (1 if pid == sender else 0) for pid in participant_key}

            wire = Wire(
                wire_id=wire_id,
                read_cursors=cursors,
                messages=[message],
                task_id=task_id,
            )
            self._wires[wire_id] = wire
            self._participant_index[participant_key] = wire_id

            if self._db:
                with self._db.transaction():
                    self._db_insert_wire(wire_id, sorted(participant_key), task_id, message.timestamp.isoformat())
                    self._db_insert_message(wire_id, sender, body, message.timestamp.isoformat())
                    for pid, pos in cursors.items():
                        self._db_upsert_cursor(wire_id, pid, pos)

            # Emit events to other participants
            cursor = len(wire.messages)
            for pid in participant_key:
                if pid != sender:
                    events_to_emit.append(
                        WireMessageEvent(
                            target_id=pid,
                            wire_id=wire_id,
                            source_id=sender,
                            message_cursor=cursor,
                        )
                    )

            result = wire.model_copy(deep=True)

        for event in events_to_emit:
            self._notify_event_listeners(event)
        self._notify_message_listeners(wire_id, result.participants, sender, body)

        return result

    async def add_message(
        self,
        wire_id: str,
        sender: str,
        body: str,
    ) -> Wire:
        """
        Add a message to an existing wire.

        Args:
            wire_id: The wire to add to.
            sender: Who is sending.
            body: The message body.

        Returns:
            The updated Wire.

        Raises:
            ValueError: If wire not found or sender not a participant.

        Emits:
            WireMessageEvent to each participant except the sender.
        """
        message = WireMessage(sender=sender, body=body)
        events_to_emit: list[WireMessageEvent] = []

        async with self._lock:
            wire = self._wires.get(wire_id)
            if not wire:
                raise ValueError(f"Wire '{wire_id}' not found")
            if sender not in wire.participants:
                raise ValueError(f"Agent '{sender}' is not a participant in wire '{wire_id}'")

            wire.messages.append(message)

            # Sender has seen their own message
            wire.read_cursors[sender] = len(wire.messages)

            if self._db:
                with self._db.transaction():
                    self._db_insert_message(wire_id, sender, body, message.timestamp.isoformat())
                    self._db_upsert_cursor(wire_id, sender, len(wire.messages))

            # Emit events to other participants (their cursors stay, so unread grows)
            cursor = len(wire.messages)
            for pid in wire.participants:
                if pid != sender:
                    events_to_emit.append(
                        WireMessageEvent(
                            target_id=pid,
                            wire_id=wire_id,
                            source_id=sender,
                            message_cursor=cursor,
                        )
                    )

            result = wire.model_copy(deep=True)

        for event in events_to_emit:
            self._notify_event_listeners(event)
        self._notify_message_listeners(wire_id, result.participants, sender, body)

        return result

    # =========================================================================
    # Lookup
    # =========================================================================

    async def get_wire(self, wire_id: str) -> Optional[Wire]:
        """Get a deep copy of a wire by ID. Returns None if not found."""
        async with self._lock:
            wire = self._wires.get(wire_id)
            return wire.model_copy(deep=True) if wire else None

    async def find_wire_by_participants(self, participants: set[str]) -> Optional[str]:
        """
        Find the wire with exactly this participant set.

        Returns wire_id or None. Only one wire per participant set (enforced at creation).
        """
        async with self._lock:
            return self._participant_index.get(frozenset(participants))

    # =========================================================================
    # Unread tracking
    # =========================================================================

    async def mark_read(self, wire_id: str, agent_id: str, up_to: int) -> None:
        """Advance read cursor for an agent.

        Raises:
            ValueError: If wire not found or agent not a participant.
        """
        async with self._lock:
            wire = self._wires.get(wire_id)
            if not wire:
                raise ValueError(f"Wire '{wire_id}' not found")
            if agent_id not in wire.participants:
                raise ValueError(f"Agent '{agent_id}' is not a participant in wire '{wire_id}'")
            wire.mark_read(agent_id, up_to)

            if self._db:
                self._db_upsert_cursor(wire_id, agent_id, wire.read_cursors[agent_id])

    async def get_unread_count(self, wire_id: str, agent_id: str) -> int:
        """Get unread count for a specific wire and agent.

        Raises:
            ValueError: If wire not found or agent not a participant.
        """
        async with self._lock:
            wire = self._wires.get(wire_id)
            if not wire:
                raise ValueError(f"Wire '{wire_id}' not found")
            if agent_id not in wire.participants:
                raise ValueError(f"Agent '{agent_id}' is not a participant in wire '{wire_id}'")
            return wire.unread_count(agent_id)

    async def list_wires(self, agent_id: str, *, unread_only: bool = False) -> list[WireSnapshot]:
        """
        List wires for an agent, sorted by most recent message (descending).

        Args:
            agent_id: The agent requesting the list.
            unread_only: If True, only return wires with unread messages.

        Returns:
            List of WireSnapshot sorted by recency.
        """
        async with self._lock:
            summaries: list[WireSnapshot] = []
            for wire in self._wires.values():
                if agent_id not in wire.participants:
                    continue

                unread = wire.unread_count(agent_id)
                if unread_only and unread == 0:
                    continue

                last_msg = wire.messages[-1]
                preview = last_msg.body[:100] + ("..." if len(last_msg.body) > 100 else "")

                summaries.append(
                    WireSnapshot(
                        wire_id=wire.wire_id,
                        participants=wire.participants,
                        message_count=len(wire.messages),
                        unread_count=unread,
                        last_message_sender=last_msg.sender,
                        last_message_preview=preview,
                        last_message_at=last_msg.timestamp,
                    )
                )

        summaries.sort(key=lambda s: s.last_message_at, reverse=True)
        return summaries

    def unread_summary(self, agent_id: str, *, limit: int = 25) -> Optional[str]:
        """
        Build notification text for tool response injection.

        Returns None if no unread messages. Shows up to `limit` conversations
        with unread counts and participants. Synchronous — safe because there are
        no await points, so no concurrent mutation can occur under asyncio's
        cooperative scheduling.
        """
        entries: list[tuple[int, str, frozenset[str]]] = []
        for wire in self._wires.values():
            if agent_id in wire.participants:
                unread = wire.unread_count(agent_id)
                if unread > 0:
                    entries.append((unread, wire.wire_id, wire.participants))

        if not entries:
            return None

        # Sort by unread count descending
        entries.sort(key=lambda e: e[0], reverse=True)

        total = sum(e[0] for e in entries)
        wire_count = len(entries)
        header = f"NOTIFICATION: You have {total} unread message{'s' if total > 1 else ''} across {wire_count} conversation{'s' if wire_count > 1 else ''}."

        lines = [header]
        for unread, wire_id, participants in entries[:limit]:
            others = sorted(p for p in participants if p != agent_id)
            lines.append(f"  - {wire_id} ({unread} unread) with: {', '.join(others)}")

        if wire_count > limit:
            lines.append(f"  ... and {wire_count - limit} more conversations")

        lines.append("You may use read_messages or batch_read_messages to read them.")
        return "\n".join(lines)

    async def get_all_unread(self, agent_id: str, *, limit: int = 10) -> list[Wire]:
        """Get deep copies of wires with unread messages for an agent.

        Returns up to `limit` wires, sorted by most recent message (descending).
        Remaining unread wires stay unread and will be picked up by subsequent events.
        """
        async with self._lock:
            result: list[Wire] = []
            for wire in self._wires.values():
                if agent_id in wire.participants and wire.unread_count(agent_id) > 0:
                    result.append(wire.model_copy(deep=True))
            result.sort(key=lambda w: w.messages[-1].timestamp, reverse=True)
            return result[:limit]

    # =========================================================================
    # Event staleness
    # =========================================================================

    async def is_event_stale(self, event: WireMessageEvent, agent_id: str) -> bool:
        """
        Check if a wire event is stale.

        Stale if: wire doesn't exist, agent not a participant, or agent's read
        cursor has already advanced past this event's message.
        """
        async with self._lock:
            wire = self._wires.get(event.wire_id)
            if not wire:
                return True
            if agent_id not in wire.participants:
                return True
            return wire.read_cursors[agent_id] >= event.message_cursor

    # =========================================================================
    # DB persistence
    # =========================================================================

    def _db_insert_wire(self, wire_id: str, participants: list[str], task_id: Optional[str], created_at: str) -> None:
        """Insert a new wire row."""
        if not self._db:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO wire_meta (wire_id, participants, task_id, created_at) VALUES (?, ?, ?, ?)",
            (wire_id, json.dumps(sorted(participants)), task_id, created_at),
        )
        self._db.commit()

    def _db_insert_message(self, wire_id: str, sender_id: str, body: str, timestamp: str) -> None:
        """Insert a wire message row."""
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO wire_messages (wire_id, sender_id, body, timestamp) VALUES (?, ?, ?, ?)",
            (wire_id, sender_id, body, timestamp),
        )
        self._db.commit()

    def _db_upsert_cursor(self, wire_id: str, participant_id: str, cursor_position: int) -> None:
        """Insert or update a read cursor."""
        if not self._db:
            return
        self._db.execute(
            """INSERT INTO wire_read_cursors (wire_id, participant_id, cursor_position)
               VALUES (?, ?, ?)
               ON CONFLICT(wire_id, participant_id) DO UPDATE SET cursor_position = excluded.cursor_position
            """,
            (wire_id, participant_id, cursor_position),
        )
        self._db.commit()

    def load_from_db(self) -> int:
        """Load all wires from DB into the in-memory store. Bypasses events.

        Reconstructs full Wire objects with messages and read cursors.
        Rebuilds the participant index. Must be called before the event
        loop starts (no lock needed).

        Returns the number of wires loaded.
        """
        if not self._db:
            return 0
        for wire_row in self._db.fetchall("SELECT wire_id, task_id FROM wire_meta"):
            wire_id = wire_row["wire_id"]
            msg_rows = self._db.fetchall(
                "SELECT sender_id AS sender, body, timestamp FROM wire_messages WHERE wire_id = ? ORDER BY id",
                (wire_id,),
            )
            cursor_rows = self._db.fetchall(
                "SELECT participant_id, cursor_position FROM wire_read_cursors WHERE wire_id = ?",
                (wire_id,),
            )
            wire = Wire(
                wire_id=wire_id,
                task_id=wire_row["task_id"],
                messages=[
                    WireMessage(sender=r["sender"], body=r["body"], timestamp=datetime.fromisoformat(r["timestamp"]))
                    for r in msg_rows
                ],
                read_cursors={r["participant_id"]: r["cursor_position"] for r in cursor_rows},
            )
            self._wires[wire.wire_id] = wire
            self._participant_index[wire.participants] = wire.wire_id
        return len(self._wires)

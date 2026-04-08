"""FrontendBridge — serializes store state and events into JSON for the WebSocket frontend."""

import dataclasses
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from ..events import Event
from ..orchestrator import RunOutcome
from ..state.registry import Registry
from ..state.task_schemas import ReviewRecord, Task
from ..state.task_store import TaskStore
from ..state.wire_store import WireStore

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """Custom JSON serializer for types not handled by json.dumps."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _serialize(data: dict) -> str:
    """Serialize a dict to JSON with custom default handler."""
    return json.dumps(data, default=_json_default)


def _serialize_review_record(record: "ReviewRecord") -> dict:
    """Serialize a ReviewRecord to a frontend-friendly dict."""
    data: dict[str, Any] = {
        "reviewer_id": record.reviewer_id,
        "requester_id": record.requester_id,
        "request_message": record.request_message,
        "round_number": record.round_number,
        "created_at": record.created_at.isoformat(),
    }
    if record.review is not None:
        data["review"] = {
            "reviewer_id": record.review.reviewer_id,
            "decision": record.review.decision.value,
            "comment": record.review.comment,
            "timestamp": record.review.timestamp.isoformat(),
        }
    else:
        data["review"] = None
    return data


def _event_to_dict(event: "Event") -> dict:
    """Serialize any Event dataclass to a frontend-friendly dict.

    Produces a structure with known base fields (event_id, event_type,
    target_id, timestamp) and a payload dict of all remaining fields.
    Uses 'payload' (not 'details') to avoid collision with TaskFinishedEvent.details.
    """
    base: dict[str, Any] = {
        "event_id": event.event_id,
        "event_type": type(event).__name__,
        "target_id": event.target_id,
        "timestamp": event.timestamp.isoformat(),
    }
    payload: dict[str, Any] = {}
    for f in dataclasses.fields(event):
        if f.name in ("event_id", "target_id", "timestamp"):
            continue
        val = getattr(event, f.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, Enum):
            val = val.value
        elif hasattr(val, "model_dump"):
            val = val.model_dump(mode="json")
        elif isinstance(val, list) and val and hasattr(val[0], "model_dump"):
            val = [v.model_dump(mode="json") for v in val]
        payload[f.name] = val
    base["payload"] = payload
    return base


def _serialize_task(task: "Task") -> dict:
    """Serialize a Task to a frontend-friendly dict with review data."""
    data: dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "assigned_to": task.assigned_to,
        "assigned_by": task.assigned_by,
        "assignment_history": list(task.assignment_history),
        "review_required": task.review_required,
        "current_review_round": task.current_review_round,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "review_history": [_serialize_review_record(r) for r in task.review_history],
        "active_reviews": (
            {k: _serialize_review_record(v) for k, v in task.active_reviews.items()} if task.active_reviews else None
        ),
    }
    return data


class FrontendBridge:
    """Serializes orchestrator state and events into JSON messages for the frontend.

    Maintains an event_log for reconnection replay and a broadcast callback
    that the WebSocket server sets to push messages to all connected clients.
    """

    def __init__(
        self,
        task_store: TaskStore,
        registry: Registry,
        wire_store: WireStore,
        org_name: str = "",
        roles: Optional[dict] = None,
        initial_tasks: Optional[list] = None,
    ) -> None:
        self.task_store = task_store
        self.registry = registry
        self.wire_store = wire_store
        self.org_name = org_name
        self.roles = roles or {}
        self.initial_tasks = initial_tasks or []

        self.event_log: list[str] = []
        self.broadcast: Optional[Callable[[str], Coroutine]] = None

    async def build_init_snapshot(self) -> str:
        """Build a full init snapshot with agents, tasks, wires, and network topology."""
        # Agents
        agents: dict[str, dict] = {}
        for snap in self.registry.list_agent_snapshots():
            agents[snap.agent_id] = {
                "agent_id": snap.agent_id,
                "role": snap.role,
                "model": snap.model,
                "tools": snap.tools,
                "state": snap.state.value,
                "current_task_id": snap.current_task_id,
            }

        # Tasks
        tasks: dict[str, dict] = {}
        for task in await self.task_store.list_tasks():
            tasks[task.id] = _serialize_task(task)

        # Wires
        wires: dict[str, dict] = {}
        for agent_id in [s.agent_id for s in self.registry.list_agent_snapshots()]:
            for wire_snap in await self.wire_store.list_wires(agent_id):
                if wire_snap.wire_id not in wires:
                    wire = await self.wire_store.get_wire(wire_snap.wire_id)
                    if wire:
                        wires[wire_snap.wire_id] = {
                            "wire_id": wire.wire_id,
                            "participants": sorted(wire.participants),
                            "messages": [
                                {
                                    "sender": m.sender,
                                    "body": m.body,
                                    "timestamp": m.timestamp.isoformat(),
                                }
                                for m in wire.messages
                            ],
                        }

        # Network topology: for each agent, list connected agent IDs
        network: dict[str, list[str]] = {}
        for agent_id in [s.agent_id for s in self.registry.list_agent_snapshots()]:
            connected = self.registry.get_connected_ids(agent_id)
            network[agent_id] = sorted(connected)

        # Queues: pending events per agent
        queues: dict[str, list[dict]] = {}
        for snap in self.registry.list_agent_snapshots():
            queues[snap.agent_id] = self.serialize_queue_snapshot(snap.agent_id)

        # Roles
        roles: dict[str, dict] = {}
        for name, role_data in self.roles.items():
            roles[name] = {
                "name": name,
                "role_prompt": role_data.get("role_prompt", ""),
                "tools": role_data.get("tools", []),
                "model": role_data.get("model", ""),
            }

        # Initial tasks
        initial_tasks_data = []
        for task_info in self.initial_tasks:
            initial_tasks_data.append(
                {
                    "task_id": task_info.get("id", ""),
                    "title": task_info.get("title", ""),
                    "description": task_info.get("description", ""),
                    "assigned_to": task_info.get("assigned_to", ""),
                }
            )

        data = {
            "type": "init",
            "org_name": self.org_name,
            "agents": agents,
            "tasks": tasks,
            "wires": wires,
            "network": network,
            "queues": queues,
            "roles": roles,
            "initial_tasks": initial_tasks_data,
        }
        return _serialize(data)

    def serialize_event(self, event: Event) -> str:
        """Convert an Event dataclass to a JSON message."""
        event_data = _event_to_dict(event)
        event_data["type"] = "event_dispatched"
        msg = _serialize(event_data)
        self.event_log.append(msg)
        return msg

    def serialize_transcript(self, agent_id: str, entry_type: str, content: str) -> str:
        """Convert a transcript entry to a JSON message."""
        data = {
            "type": "transcript_entry",
            "agent_id": agent_id,
            "entry_type": entry_type,
            "content": content,
        }
        msg = _serialize(data)
        self.event_log.append(msg)
        return msg

    async def serialize_task(self, task_id: str) -> str:
        """Fetch a task from the store and serialize its full state."""
        task = await self.task_store.get_task(task_id)
        if task is None:
            data = {
                "type": "task_changed",
                "task_id": task_id,
                "task": None,
            }
        else:
            data = {
                "type": "task_changed",
                "task_id": task_id,
                "task": _serialize_task(task),
            }

        msg = _serialize(data)
        self.event_log.append(msg)
        return msg

    def serialize_agent_state_change(self, agent_id: str, state: str, current_task_id: Optional[str]) -> str:
        """Serialize an agent state change into a JSON message for the frontend."""
        data = {
            "type": "agent_state_changed",
            "agent_id": agent_id,
            "state": state,
            "current_task_id": current_task_id,
        }
        msg = _serialize(data)
        self.event_log.append(msg)
        return msg

    def serialize_wire_message(self, wire_id: str, participants: frozenset[str], sender: str, body: str) -> str:
        """Convert a wire message into a JSON message for the frontend."""
        data = {
            "type": "wire_message",
            "wire_id": wire_id,
            "sender": sender,
            "body": body,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "participants": sorted(participants),
        }
        msg = _serialize(data)
        # Don't append wire_message to event_log — the init snapshot already
        # includes the full message history from the wire store, so replaying
        # these on reconnect would cause duplicate messages on the frontend.
        return msg

    def serialize_run_finished(
        self,
        outcome: RunOutcome,
        duration_seconds: float,
        total_cost_usd: float,
    ) -> str:
        """Signal that the run has finished with final summary."""
        data = {
            "type": "run_finished",
            "outcome": outcome,
            "duration_seconds": duration_seconds,
            "total_cost_usd": total_cost_usd,
        }
        msg = _serialize(data)
        self.event_log.append(msg)
        return msg

    def serialize_queue_snapshot(self, agent_id: str) -> list[dict]:
        """Serialize all queued events for an agent."""
        events = self.registry.get_queue_snapshot(agent_id)
        return [_event_to_dict(e) for e in events]

    def serialize_queue_event_added(self, agent_id: str, event: Event) -> str:
        """Serialize a queue_event_added message."""
        data = {
            "type": "queue_event_added",
            "agent_id": agent_id,
            "event": _event_to_dict(event),
        }
        # Not added to event_log — init snapshot provides current state on reconnect
        return _serialize(data)

    def serialize_queue_event_removed(self, agent_id: str, event_id: str) -> str:
        """Serialize a queue_event_removed message."""
        data = {
            "type": "queue_event_removed",
            "agent_id": agent_id,
            "event_id": event_id,
        }
        return _serialize(data)

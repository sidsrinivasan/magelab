"""
Hydration — Reconstruct in-memory state from the SQLite database.

The DB is the sole source of truth for both structure and operations.

Entry points:
- load_settings_from_db() — load org settings from run_meta (returns OrgSettings)
- reconstruct_org_config_from_db() — read DB into a serializable OrgConfig
- resume_fresh() / resume_continue() — apply resume-mode semantics
"""

import json
import logging
from datetime import datetime
from typing import Optional

from ..events import (
    Event,
    EventOutcome,
    MCPEvent,
    ResumeEvent,
    ReviewFinishedEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    TaskFinishedEvent,
    WireMessageEvent,
)
from ..org_config import OrgConfig, OrgSettings, WireNotifications
from .database import Database
from .registry import Registry
from .registry_schemas import AgentState
from .task_schemas import ReviewRecord, TaskStatus
from .task_store import TaskStore

# Map event_type strings to their dataclasses for reconstruction.
_EVENT_CLASSES: dict[str, type] = {
    "TaskAssignedEvent": TaskAssignedEvent,
    "ReviewRequestedEvent": ReviewRequestedEvent,
    "ReviewFinishedEvent": ReviewFinishedEvent,
    "TaskFinishedEvent": TaskFinishedEvent,
    "WireMessageEvent": WireMessageEvent,
    "ResumeEvent": ResumeEvent,
    "MCPEvent": MCPEvent,
}


def load_settings_from_db(db: Database) -> OrgSettings:
    """Load org settings from the run_meta.org_config JSON blob.

    Raises ValueError if run_meta is missing or org_config is not valid JSON.
    """
    meta = db.load_run_meta()
    if not meta:
        raise ValueError("No run_meta in DB — cannot load settings")
    if not meta.get("org_config"):
        raise ValueError("run_meta.org_config is empty — cannot load settings")

    raw = json.loads(meta["org_config"])
    settings_raw = raw.get("settings", {})
    wn = settings_raw.get("wire_notifications")
    if wn and not isinstance(wn, WireNotifications):
        settings_raw["wire_notifications"] = WireNotifications(wn)
    return OrgSettings(**settings_raw)


def reconstruct_org_config_from_db(db: Database, logger: Optional[logging.Logger] = None) -> OrgConfig:
    """Reconstruct a full OrgConfig from DB state.

    Starts from the stored OrgConfig JSON in run_meta, then patches in
    runtime structural changes from the store tables (roles, agents,
    network). Settings and non-structural agent fields (like
    session_config_override) pass through from the stored JSON unchanged.
    """
    meta = db.load_run_meta()
    if not meta:
        raise ValueError("No run_meta in DB — cannot reconstruct org config")
    raw = json.loads(meta["org_config"])

    # Patch structural state from registry (captures runtime mutations)
    registry = Registry(framework_logger=logger or logging.getLogger(__name__), db=db)
    registry.load_from_db()

    roles = registry.get_roles()
    raw["roles"] = roles

    # For each agent in the registry, compute overrides by diffing against role defaults.
    # Start from stored agent dict (preserves non-structural fields like session_config_override),
    # then overlay structural overrides from the registry.
    stored_agents = raw.get("agents", {})
    patched_agents: dict[str, dict] = {}
    for agent_id in registry.list_agent_ids(active_only=False):
        snap = registry.get_agent_snapshot(agent_id)
        role = roles.get(snap.role)

        agent_dict = dict(stored_agents.get(agent_id, {"agent_id": agent_id, "role": snap.role}))
        agent_dict["agent_id"] = agent_id
        agent_dict["role"] = snap.role
        agent_dict["model_override"] = snap.model if (not role or snap.model != role.model) else None
        agent_dict["role_prompt_override"] = (
            snap.role_prompt if (not role or snap.role_prompt != role.role_prompt) else None
        )
        agent_dict["tools_override"] = list(snap.tools) if (not role or list(snap.tools) != role.tools) else None
        agent_dict["max_turns_override"] = snap.max_turns if (not role or snap.max_turns != role.max_turns) else None

        # Strip None overrides (matching to_dict behavior)
        patched_agents[agent_id] = {k: v for k, v in agent_dict.items() if v is not None}

    raw["agents"] = patched_agents
    raw["network"] = registry.get_network_config()

    return OrgConfig.from_dict(raw)


async def resume_fresh(
    db: Database,
    task_store: TaskStore,
    registry: Registry,
    logger: logging.Logger,
) -> None:
    """Resume-fresh: fail in-progress tasks, drop undelivered events, reset agents.

    Uses task_store.mark_finished(force=True) — the store's sanctioned path
    for forcing task failure — rather than directly mutating task fields.

    Note: mark_finished emits TaskFinishedEvents, but no event listeners are
    registered on the task_store at this point (called during Orchestrator.build(),
    before __init__ adds the dispatch listener). The events are silently discarded.
    This is acceptable for resume-fresh: all agents are reset to IDLE and
    start with empty queues, so no agent would act on these events anyway.
    """
    dropped = db.update_events_by_outcome(None, EventOutcome.DROPPED_ON_RESTART)
    logger.info(f"Resume-fresh: marked {dropped} undelivered events as dropped")

    open_tasks = await task_store.list_tasks(is_finished=False)
    for task in open_tasks:
        await task_store.mark_finished(task.id, TaskStatus.FAILED, details="Force-failed on resume-fresh", force=True)
        logger.info(f"Resume-fresh: force-failed task {task.id}")

    for agent_id in registry.list_agent_ids():
        registry.mark_idle(agent_id)


def resume_continue(
    db: Database,
    registry: Registry,
    logger: logging.Logger,
) -> None:
    """Resume-continue: re-enqueue undelivered events, send ResumeEvents to interrupted agents.

    Reads agent state from the registry (already hydrated) to determine which
    agents were mid-work when the run stopped.
    """
    # Enqueue ResumeEvents FIRST for agents that were mid-work.
    # These must be at the front of the queue so the agent continues its
    # interrupted task before processing any pending events.
    for agent_id in registry.list_agent_ids():
        snapshot = registry.get_agent_snapshot(agent_id)
        if snapshot.state in (AgentState.WORKING, AgentState.REVIEWING):
            was_reviewing = snapshot.state == AgentState.REVIEWING
            resume_event = ResumeEvent(
                target_id=agent_id,
                task_id=snapshot.current_task_id,
                was_reviewing=was_reviewing,
            )
            registry.enqueue(agent_id, resume_event)
            # Insert into DB so cost/turns are recorded when the event completes.
            db.insert_event(
                event_id=resume_event.event_id,
                event_type="ResumeEvent",
                target_agent_id=agent_id,
                source_agent_id=None,
                task_id=snapshot.current_task_id,
                wire_id=None,
                timestamp=resume_event.timestamp.isoformat(),
                payload=json.dumps({"was_reviewing": was_reviewing}),
            )
            # Agent stays WORKING/REVIEWING in both memory and DB.
            # When the event loop delivers the ResumeEvent, mark_working is
            # called again (harmless overwrite), and mark_idle runs after the
            # agent finishes normally. Keeping DB state as-is means a
            # double-crash will correctly re-create the ResumeEvent.
            logger.info(
                f"Resume-continue: enqueued ResumeEvent for {agent_id} "
                f"(was_reviewing={was_reviewing} on {snapshot.current_task_id})"
            )

    # Then re-enqueue undelivered events in original order.
    # Reconstructed events preserve their original event_id so the orchestrator
    # updates the existing DB row when the event is delivered/completed/stale.
    # ResumeEvents are skipped — they're always freshly created from agent state above.
    undelivered = db.load_undelivered_events()
    requeued = 0
    dropped = 0
    for row in undelivered:
        event = reconstruct_event(row)
        if event and not isinstance(event, ResumeEvent):
            if registry.enqueue(event.target_id, event):
                requeued += 1
            else:
                db.update_event_outcome(event.event_id, EventOutcome.DROPPED_ON_RESTART)
                dropped += 1
        else:
            db.update_event_outcome(row["event_id"], EventOutcome.DROPPED_ON_RESTART)
            dropped += 1
    logger.info(f"Resume-continue: re-enqueued {requeued}, dropped {dropped} undelivered events")


def reconstruct_event(row: dict) -> Optional[Event]:
    """Reconstruct an Event dataclass from a DB event row.

    Uses the payload JSON column to restore event-specific fields faithfully.
    Returns None for unknown event types.
    """
    event_type = row["event_type"]
    cls = _EVENT_CLASSES.get(event_type)
    if cls is None:
        return None

    # Base fields present as columns
    kwargs: dict = {
        "event_id": row["event_id"],
        "target_id": row["target_agent_id"],
        "timestamp": datetime.fromisoformat(row["timestamp"]),
    }

    # Add column fields that many events share
    if row.get("source_agent_id") is not None:
        kwargs["source_id"] = row["source_agent_id"]
    if row.get("task_id") is not None:
        kwargs["task_id"] = row["task_id"]
    if row.get("wire_id") is not None:
        kwargs["wire_id"] = row["wire_id"]

    # Merge event-specific fields from payload
    payload_str = row.get("payload")
    if payload_str:
        payload = json.loads(payload_str)
        # Restore typed fields
        if "outcome" in payload and event_type in ("TaskFinishedEvent", "ReviewFinishedEvent"):
            payload["outcome"] = TaskStatus(payload["outcome"])
        if "review_records" in payload:
            payload["review_records"] = [ReviewRecord.model_validate(r) for r in payload["review_records"]]
        kwargs.update(payload)

    return cls(**kwargs)

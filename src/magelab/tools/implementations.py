"""
Framework tool implementations.

Provides create_tool_implementations() which returns a dict of
{tool_name: async_handler} closures for a given agent. These are plain
async functions returning ToolResponse — runners wrap them for their SDK.

Handlers validate agent/tool concerns and input formats. Task-state validation
(status, transitions, review rounds) is left to task_store — it holds the lock
and raises ValueError on invalid operations.

The @_handle_errors decorator catches KeyError (missing fields) and ValueError
(from task_store) so handlers can let those propagate. Handlers that need custom
error messages (e.g. enum parsing) catch locally — inner except takes precedence.
"""

import asyncio
import dataclasses
import functools
import json
import uuid
from typing import Any, Awaitable, Callable, Optional

from ..state.registry import Registry
from ..state.registry_schemas import AgentState
from ..state.task_schemas import ReviewPolicy, ReviewRecord, ReviewStatus, Task, TaskStatus
from ..state.task_store import TaskStore
from ..state.wire_store import WireStore
from . import specs
from .specs import FRAMEWORK, ToolResponse
from .validation import validate_review_assignment


def create_tool_implementations(
    task_store: TaskStore,
    registry: Registry,
    agent_id: str,
    wire_store: WireStore,
) -> dict[str, Callable[..., Awaitable[ToolResponse]]]:
    """
    Create framework tool handler functions for a given agent.

    Each handler closes over task_store, registry, and agent_id.
    Returns {tool_name: async_handler} — plain functions returning ToolResponse.
    """

    def _handle_errors(fn: Callable[..., Awaitable[ToolResponse]]) -> Callable[..., Awaitable[ToolResponse]]:
        """Decorator: catches KeyError → 'Missing required field' and ValueError → 'Error: ...'."""

        @functools.wraps(fn)
        async def wrapper(args: dict[str, Any]) -> ToolResponse:
            try:
                return await fn(args)
            except KeyError as e:
                return ToolResponse(f"Missing required field: {e}", is_error=True)
            except ValueError as e:
                return ToolResponse(f"Error: {e}", is_error=True)

        return wrapper

    def _get_connection_tools(target_id: str) -> set[str]:
        """Union of tool names across agents connected to target_id."""
        tools: set[str] = set()
        for aid in registry.get_connected_ids(target_id):
            snap = registry.get_agent_snapshot(aid)
            if snap:
                tools.update(snap.tools)
        return tools

    def _check_assignee(assigned_to: str, review_required: bool) -> Optional[str]:
        """Validate an agent assignment: agent must exist, be active, be connected to the
        calling agent, and if review_required, must be able to submit for review with
        connected agents able to conduct reviews. Returns error message or None.
        """
        snapshot = registry.get_agent_snapshot(assigned_to)
        if snapshot is None:
            return f"agent '{assigned_to}' not found"
        if snapshot.state == AgentState.TERMINATED:
            return f"agent '{assigned_to}' is terminated"
        if not registry.is_connected(agent_id, assigned_to):
            return f"agent '{assigned_to}' is not connected to '{agent_id}'"
        if review_required:
            return validate_review_assignment(assigned_to, set(snapshot.tools), _get_connection_tools(assigned_to))
        return None

    # =========================================================================
    # Task CRUD
    # =========================================================================

    @_handle_errors
    async def tasks_create(args: dict[str, Any]) -> ToolResponse:
        # Build task — KeyError on missing fields propagates to decorator
        task = Task(
            id=args["id"],
            title=args["title"],
            description=args["description"],
            review_required=args.get("review_required", False),
        )

        # Validate assignee if provided
        assigned_to = args.get("assigned_to")
        if assigned_to:
            error = _check_assignee(assigned_to, task.review_required)
            if error:
                return ToolResponse(f"Error: {error}", is_error=True)

        task = await task_store.create(
            task,
            assigned_to=assigned_to,
            assigned_by=agent_id,
        )
        return ToolResponse(f"Successfully created task {task.id}: {task.title}. Assignee: {assigned_to}")

    @_handle_errors
    async def tasks_create_batch(args: dict[str, Any]) -> ToolResponse:
        # Parse tasks array (may arrive as JSON string from LLM)
        tasks_data = args["tasks"]
        if isinstance(tasks_data, str):
            try:
                tasks_data = json.loads(tasks_data)
            except json.JSONDecodeError as e:
                return ToolResponse(f"Error parsing tasks JSON: {e}", is_error=True)
        if not tasks_data:
            return ToolResponse("Error: tasks array is empty", is_error=True)

        # Process each entry independently, accumulating errors
        created = []
        errors = []
        for i, entry in enumerate(tasks_data):
            try:
                assigned_to = entry.get("assigned_to")
                review_required = entry.get("review_required", False)
                if assigned_to:
                    assignee_error = _check_assignee(assigned_to, review_required)
                    if assignee_error:
                        errors.append(f"  task[{i}]: {assignee_error}")
                        continue
                task = Task(
                    id=entry["id"],
                    title=entry["title"],
                    description=entry["description"],
                    review_required=review_required,
                )
                task = await task_store.create(
                    task,
                    assigned_to=assigned_to,
                    assigned_by=agent_id,
                )
                created.append(f"  {task.id}: {task.title} -> {assigned_to}")
            except KeyError as e:
                errors.append(f"  task[{i}]: missing field {e}")
            except Exception as e:
                errors.append(f"  task[{i}]: {e}")

        parts = [f"Created {len(created)} task(s):"]
        parts.extend(created)
        if errors:
            parts.append(f"\n{len(errors)} error(s):")
            parts.extend(errors)
        return ToolResponse("\n".join(parts), is_error=bool(errors))

    @_handle_errors
    async def tasks_assign(args: dict[str, Any]) -> ToolResponse:
        # Extract required fields — KeyError propagates to decorator
        to_agent = args["to_agent"]
        task_id = args["task_id"]

        # Fetch task to check review_required before validating assignee
        task = await task_store.get_task(task_id)
        if not task:
            return ToolResponse(f"Error: task '{task_id}' not found", is_error=True)

        error = _check_assignee(to_agent, task.review_required)
        if error:
            return ToolResponse(f"Error: {error}", is_error=True)

        # ValueError from task_store (e.g. finished, in review) propagates to decorator
        task = await task_store.assign(task_id, to_agent, by_agent=agent_id)
        return ToolResponse(f"Task {task.id} assigned to {to_agent}")

    @_handle_errors
    async def tasks_get(args: dict[str, Any]) -> ToolResponse:
        task = await task_store.get_task(args["task_id"])
        if task:
            return ToolResponse(task.model_dump_json(indent=4))
        return ToolResponse(f"Task '{args['task_id']}' not found", is_error=True)

    @_handle_errors
    async def tasks_list(args: dict[str, Any]) -> ToolResponse:
        kwargs: dict[str, Any] = {}
        if args.get("assigned_to"):
            kwargs["assigned_to"] = args["assigned_to"]
        if args.get("assigned_by"):
            kwargs["assigned_by"] = args["assigned_by"]
        if "is_finished" in args:
            kwargs["is_finished"] = args["is_finished"]

        tasks = await task_store.list_tasks(**kwargs)

        # Scope to network: only unassigned tasks or tasks assigned to self/connections
        visible_ids = set(registry.get_connected_ids(agent_id))
        visible_ids.add(agent_id)
        tasks = [t for t in tasks if t.assigned_to is None or t.assigned_to in visible_ids]

        result = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "assigned_to": t.assigned_to,
                "assigned_by": t.assigned_by,
            }
            for t in tasks
        ]
        return ToolResponse(json.dumps(result, indent=4))

    @_handle_errors
    async def tasks_mark_finished(args: dict[str, Any]) -> ToolResponse:
        # Custom catch: only 'succeeded'/'failed' are valid but TaskStatus has many values,
        # so the generic enum ValueError message would be misleading
        try:
            outcome = TaskStatus(args["outcome"])
        except ValueError:
            return ToolResponse(f"Invalid outcome: '{args['outcome']}'. Use 'succeeded' or 'failed'", is_error=True)

        if outcome not in (TaskStatus.SUCCEEDED, TaskStatus.FAILED):
            return ToolResponse(f"Invalid outcome: '{outcome.value}'. Use 'succeeded' or 'failed'", is_error=True)

        # KeyError (missing task_id) and ValueError (task store guards) propagate to decorator
        task = await task_store.mark_finished(args["task_id"], outcome, args.get("details", ""))
        return ToolResponse(f"Task {task.id} marked as {task.status.value}")

    # =========================================================================
    # Review workflow
    # =========================================================================

    @_handle_errors
    async def tasks_submit_for_review(args: dict[str, Any]) -> ToolResponse:
        task_id = args["task_id"]

        # Parse reviewers JSON: {reviewer_id: message, ...}
        try:
            reviewers_dict: dict[str, str] = json.loads(args["reviewers"])
        except json.JSONDecodeError as e:
            return ToolResponse(f"Error parsing reviewers JSON: {e}", is_error=True)
        if not reviewers_dict:
            return ToolResponse("Error: at least one reviewer required", is_error=True)

        # Validate each reviewer exists, is connected, and has review capability
        for reviewer_id in reviewers_dict:
            snapshot = registry.get_agent_snapshot(reviewer_id)
            if snapshot is None:
                return ToolResponse(f"Error: agent '{reviewer_id}' not found", is_error=True)
            if snapshot.state == AgentState.TERMINATED:
                return ToolResponse(f"Error: agent '{reviewer_id}' is terminated and not available", is_error=True)
            if not registry.is_connected(agent_id, reviewer_id):
                return ToolResponse(f"Error: agent '{reviewer_id}' is not connected to '{agent_id}'", is_error=True)
            if "tasks_submit_review" not in snapshot.tools:
                return ToolResponse(
                    f"Error: agent '{reviewer_id}' is not authorized to conduct reviews.",
                    is_error=True,
                )

        review_records: list[ReviewRecord] = [
            ReviewRecord(
                reviewer_id=reviewer_id,
                requester_id=agent_id,
                request_message=message,
            )
            for reviewer_id, message in reviewers_dict.items()
        ]

        # Custom catch: ReviewPolicy enum has specific valid values
        policy_str = args.get("review_policy", "all")
        try:
            policy = ReviewPolicy(policy_str)
        except ValueError:
            return ToolResponse(f"Invalid policy: '{policy_str}'. Use 'any', 'majority', or 'all'", is_error=True)

        # ValueError from task_store (wrong status, already in review) propagates to decorator
        await task_store.submit_for_review(task_id, review_records, policy)
        reviewer_ids = [r.reviewer_id for r in review_records]
        return ToolResponse(f"Task {task_id} submitted for review. Reviewers: {reviewer_ids}, Policy: {policy.value}")

    @_handle_errors
    async def tasks_submit_review(args: dict[str, Any]) -> ToolResponse:
        task_id = args["task_id"]

        # Custom catch: ReviewStatus enum — need specific valid values in error message
        try:
            decision = ReviewStatus(args["decision"])
        except (ValueError, KeyError):
            return ToolResponse(
                f"Invalid decision: {args.get('decision')}. Use 'approved' or 'changes_requested'",
                is_error=True,
            )

        # ValueError from task_store (not in review, already finished, already submitted) propagates to decorator
        await task_store.submit_review(
            task_id,
            agent_id,
            decision,
            args.get("comment"),
        )
        return ToolResponse(f"Review submitted: {decision.value}")

    # =========================================================================
    # Directory and queries
    # =========================================================================

    @_handle_errors
    async def connections_list(args: dict[str, Any]) -> ToolResponse:
        # Return connected agents, excluding internal fields
        _exclude = {"role_prompt", "tools", "max_turns"}
        connected_ids = registry.get_connected_ids(agent_id)
        entries = []
        for aid in connected_ids:
            snap = registry.get_agent_snapshot(aid)
            if snap:
                d = {k: v for k, v in dataclasses.asdict(snap).items() if k not in _exclude}
                if d.get("current_task_id") is None:
                    d.pop("current_task_id", None)
                entries.append(d)
        return ToolResponse(json.dumps(entries, indent=4, default=str))

    @_handle_errors
    async def get_available_reviewers(args: dict[str, Any]) -> ToolResponse:
        """Return workload info for each peer that can conduct reviews.

        Excludes the calling agent and terminated agents. Returns a list of:
        {
            "agent_id": str,
            "role": str,
            "state": "idle" | "working" | "reviewing",
            "queued_tasks": [
                {"task_id": str, "title": str, "description": str,
                 "type": "assigned_work" | "assigned_review"},
                ...
            ],
            "completed_work": int,   # own tasks succeeded
            "completed_reviews": int, # past reviews submitted
            "failed_work": int,      # own tasks failed
        }
        """
        connected_ids = registry.get_connected_ids(agent_id)
        candidates = []
        for aid in connected_ids:
            snap = registry.get_agent_snapshot(aid)
            if snap and "tasks_submit_review" in snap.tools:
                candidates.append(snap)
        if not candidates:
            return ToolResponse("No reviewers are available.")

        candidate_ids = {a.agent_id for a in candidates}

        def _task_summary(task: Task, entry_type: str) -> dict:
            return {"task_id": task.id, "title": task.title, "description": task.description, "type": entry_type}

        # Single pass over all tasks to compute per-candidate workload
        all_tasks = await task_store.list_tasks()
        stats: dict[str, dict] = {
            aid: {"queued_tasks": [], "completed_work": 0, "completed_reviews": 0, "failed_work": 0}
            for aid in candidate_ids
        }
        for task in all_tasks:
            # Work assigned to a reviewer candidate: count finished, queue in-progress
            if task.assigned_to in candidate_ids:
                s = stats[task.assigned_to]
                if task.status == TaskStatus.SUCCEEDED:
                    s["completed_work"] += 1
                elif task.status == TaskStatus.FAILED:
                    s["failed_work"] += 1
                elif not task.is_finished():
                    s["queued_tasks"].append(_task_summary(task, "assigned_work"))
            # Pending reviews: queue any active review assigned to a candidate
            if task.active_reviews:
                for aid in candidate_ids & task.active_reviews.keys():
                    if task.active_reviews[aid].is_pending():
                        stats[aid]["queued_tasks"].append(_task_summary(task, "assigned_review"))
            # Past reviews: count completed (non-failed) reviews by candidates
            for record in task.review_history:
                if (
                    record.reviewer_id in candidate_ids
                    and record.review
                    and record.review.decision != ReviewStatus.FAILED
                ):
                    stats[record.reviewer_id]["completed_reviews"] += 1

        entries = [
            {"agent_id": a.agent_id, "role": a.role, "state": a.state.value, **stats[a.agent_id]} for a in candidates
        ]
        return ToolResponse(json.dumps(entries, indent=4, default=str))

    # =========================================================================
    # Coordination
    # =========================================================================

    @_handle_errors
    async def sleep(args: dict[str, Any]) -> ToolResponse:
        duration = args.get("duration_seconds", 0)
        if duration < 0 or duration > 60:
            return ToolResponse("duration_seconds must be between 0 and 60", is_error=True)
        if duration > 0:
            await asyncio.sleep(duration)
        return ToolResponse(f"Slept for {duration} seconds")

    # =========================================================================
    # Communication
    # =========================================================================

    @_handle_errors
    async def send_message(args: dict[str, Any]) -> ToolResponse:
        body = args["body"]
        recipients = args.get("recipients")
        conversation_id = args.get("conversation_id")

        # Parse recipients (may arrive as JSON string from LLM)
        if isinstance(recipients, str):
            recipients = recipients.strip()
            if not recipients:
                recipients = None
            else:
                try:
                    recipients = json.loads(recipients)
                    if not isinstance(recipients, list):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    return ToolResponse("'recipients' must be a JSON array of agent ID strings", is_error=True)

        if not recipients and not conversation_id:
            return ToolResponse("Must provide 'recipients' or 'conversation_id'", is_error=True)

        if conversation_id:
            # Reply to existing conversation
            wire = await wire_store.get_wire(conversation_id)
            if wire is None:
                return ToolResponse(f"Conversation '{conversation_id}' not found", is_error=True)
            if agent_id not in wire.participants:
                return ToolResponse(f"You are not a participant in conversation '{conversation_id}'", is_error=True)

            # Validate connectivity to all other participants
            for pid in wire.participants:
                if pid == agent_id:
                    continue
                if not registry.get_agent_snapshot(pid) or not registry.is_connected(agent_id, pid):
                    return ToolResponse(f"Agent '{pid}' is not connected to you", is_error=True)

            # If recipients also provided, validate they match
            if recipients:
                expected = frozenset([*recipients, agent_id])
                if expected != wire.participants:
                    return ToolResponse(
                        f"Recipients {sorted(recipients)} don't match conversation participants {sorted(wire.participants)}",
                        is_error=True,
                    )

            await wire_store.add_message(conversation_id, agent_id, body)
            return ToolResponse(f"Message sent in conversation {conversation_id}")

        # Send to recipients — auto-match or create
        if not isinstance(recipients, list) or not recipients:
            return ToolResponse(
                "You must either provide 'recipients' as a non-empty list of agent IDs or 'conversation_id'",
                is_error=True,
            )

        # Reject self-only messaging
        if set(recipients) == {agent_id}:
            return ToolResponse("Cannot send a message to only yourself", is_error=True)

        # Validate recipients are connected to sender
        for rid in recipients:
            if rid == agent_id:
                continue
            if not registry.is_connected(agent_id, rid):
                return ToolResponse(f"Agent '{rid}' is not connected to you", is_error=True)

        # Validate all-pairs connectivity for group messages
        participant_set = set([*recipients, agent_id])
        if len(participant_set) > 2:
            disconnected_pairs = []
            participants_list = sorted(participant_set)
            for i, pid_a in enumerate(participants_list):
                for pid_b in participants_list[i + 1 :]:
                    if not registry.is_connected(pid_a, pid_b):
                        disconnected_pairs.append(f"'{pid_a}' and '{pid_b}'")
            if disconnected_pairs:
                return ToolResponse(
                    f"Cannot create group conversation, the following pairs are not connected: "
                    f"({', '.join(disconnected_pairs)}). Non-connected pairs cannot participate in a single conversation and must be messaged separately.",
                    is_error=True,
                )
        existing_id = await wire_store.find_wire_by_participants(participant_set)

        if existing_id:
            await wire_store.add_message(existing_id, agent_id, body)
            return ToolResponse(f"Message sent in conversation {existing_id}")

        # Create new wire since no existing conversation matches
        wire_id = uuid.uuid4().hex[:12]
        await wire_store.create_wire(wire_id, list(participant_set), agent_id, body)
        return ToolResponse(f"Started conversation with id: {wire_id}")

    @_handle_errors
    async def read_messages(args: dict[str, Any]) -> ToolResponse:
        conversation_id = args["conversation_id"]
        num_previous = args.get("num_previous", 2)

        wire = await wire_store.get_wire(conversation_id)
        if not wire:
            raise ValueError(f"Conversation '{conversation_id}' not found")
        if agent_id not in wire.participants:
            raise ValueError(f"You are not a participant in conversation '{conversation_id}'")
        text, cursor = wire.format_conversation(agent_id, num_previous=num_previous)
        await wire_store.mark_read(conversation_id, agent_id, cursor)
        return ToolResponse(text)

    @_handle_errors
    async def batch_read_messages(args: dict[str, Any]) -> ToolResponse:
        wires = await wire_store.get_all_unread(agent_id, limit=5)
        if not wires:
            return ToolResponse("No unread messages.")

        sections = []
        for wire in wires:
            text, cursor = wire.format_conversation(agent_id)
            await wire_store.mark_read(wire.wire_id, agent_id, cursor)
            sections.append(text)

        return ToolResponse(("\n\n" + "=" * 40 + "\n\n").join(sections))

    @_handle_errors
    async def conversations_list(args: dict[str, Any]) -> ToolResponse:
        unread_only = args.get("unread_only", True)

        summaries = await wire_store.list_wires(agent_id, unread_only=unread_only)
        if not summaries:
            return ToolResponse("No conversations found.")
        return ToolResponse("\n\n".join(s.format(agent_id) for s in summaries))

    # =========================================================================
    # Registry — maps spec names to implementations
    # =========================================================================

    impls = {
        specs.tasks_create.name: tasks_create,
        specs.tasks_create_batch.name: tasks_create_batch,
        specs.tasks_assign.name: tasks_assign,
        specs.tasks_submit_for_review.name: tasks_submit_for_review,
        specs.tasks_submit_review.name: tasks_submit_review,
        specs.tasks_mark_finished.name: tasks_mark_finished,
        specs.tasks_get.name: tasks_get,
        specs.tasks_list.name: tasks_list,
        specs.connections_list.name: connections_list,
        specs.get_available_reviewers.name: get_available_reviewers,
        specs.sleep.name: sleep,
        specs.send_message.name: send_message,
        specs.read_messages.name: read_messages,
        specs.batch_read_messages.name: batch_read_messages,
        specs.conversations_list.name: conversations_list,
    }

    # Ensure implementations stay in sync with specs
    spec_names = set(FRAMEWORK.keys())
    impl_names = set(impls.keys())
    if spec_names != impl_names:
        missing = spec_names - impl_names
        extra = impl_names - spec_names
        parts = []
        if missing:
            parts.append(f"missing implementations: {missing}")
        if extra:
            parts.append(f"extra implementations: {extra}")
        raise RuntimeError(f"Tool spec/impl mismatch: {', '.join(parts)}")

    return impls

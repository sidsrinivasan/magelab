"""
Tool dependency validation.

Validates that tool configurations are consistent — tools that depend on
other tools (same agent or connected agents) are properly paired. Returns
errors (hard stop) and warnings (logged but non-blocking).

Terminology:
    agent_tools (set[str]):  Tool names available to a single agent.
    agent_to_tools (dict[str, set[str]]):  Mapping of agent_id → tool names.
    connection_tools (set[str]):  Union of tool names across an agent's
        connected agents (excludes the agent itself). When a network
        topology is defined, this is scoped to connected agents only;
        otherwise it's all other agents in the org.
    agent_to_connection_tools (dict[str, set[str]]):  Mapping of agent_id → connection_tools.
"""

from typing import Optional

from ..state.task_schemas import Task

# =============================================================================
# Helpers
# =============================================================================


def _compute_connection_tools(agent_id: str, agent_to_tools: dict[str, set[str]]) -> set[str]:
    """Compute the union of tool names across all agents except agent_id."""
    peer_sets = [t for aid, t in agent_to_tools.items() if aid != agent_id]
    return set().union(*peer_sets) if peer_sets else set()


# =============================================================================
# Tool dependency validation (startup)
#   validate_tool_dependencies()      — per-agent, caller provides connection_tools
#   validate_all_tool_dependencies()  — all agents, computes connection_tools
# =============================================================================

# (trigger_tool, required_tool, required_scope, severity)
# "agent" scope: required must be in this agent's tools
# "connections" scope: required must be in connection_tools (some reachable agent has it)
_TOOL_DEPS: list[tuple[str, str, str, str]] = [
    # #1: can discover reviewers but can't submit for review (useless)
    ("get_available_reviewers", "tasks_submit_for_review", "agent", "error"),
    # #2: can submit for review but can't discover reviewers (must know from context)
    ("tasks_submit_for_review", "get_available_reviewers", "agent", "warning"),
    # #3: can discover reviewers but no one can conduct reviews (list always empty)
    ("get_available_reviewers", "tasks_submit_review", "connections", "warning"),
    # #4: can submit for review but no connected agent can conduct reviews (review rounds will never complete)
    ("tasks_submit_for_review", "tasks_submit_review", "connections", "warning"),
    # #5: can conduct reviews but no connected agent can request reviews (reviewer idles; may resolve with dynamic agents)
    ("tasks_submit_review", "tasks_submit_for_review", "connections", "warning"),
    # #6: can conduct reviews but no connected agent can discover reviewers (won't be found via discovery)
    ("tasks_submit_review", "get_available_reviewers", "connections", "warning"),
]


def validate_tool_dependencies(
    agent_id: str,
    agent_tools: set[str],
    connection_tools: set[str],
) -> tuple[list[str], list[str]]:
    """Validate tool dependencies for a single agent.

    Args:
        agent_id: The agent being validated.
        agent_tools: Tool names this agent has (e.g. {"tasks_submit_for_review", ...}).
        connection_tools: Union of tool names across agents this agent can interact with.

    Returns (errors, warnings) — lists of human-readable messages.

    For batch validation over all agents, use validate_all_tool_dependencies().
    """
    errors: list[str] = []
    warnings: list[str] = []

    for trigger, required, scope, severity in _TOOL_DEPS:
        if trigger not in agent_tools:
            continue
        pool = agent_tools if scope == "agent" else connection_tools
        if required not in pool:
            scope_desc = f"agent '{agent_id}' lacks" if scope == "agent" else "no connected agent has"
            msg = f"Agent '{agent_id}' has '{trigger}' but {scope_desc} '{required}'"

            (errors if severity == "error" else warnings).append(msg)

    # Communication tool dependency checks
    has_send = "send_message" in agent_tools
    has_read = "read_messages" in agent_tools or "batch_read_messages" in agent_tools
    has_connections = "connections_list" in agent_tools
    has_conversations = "conversations_list" in agent_tools

    # conversations_list without read tools is an error (can't read what you list)
    if has_conversations and not has_read:
        errors.append(f"Agent '{agent_id}' has 'conversations_list' but no 'read_messages' or 'batch_read_messages'")

    # send without connections_list is a warning (useful but not required)
    if has_send and not has_connections:
        warnings.append(f"Agent '{agent_id}' has 'send_message' but no 'connections_list'")

    # send without read is a warning (can't read replies)
    if has_send and not has_read:
        warnings.append(f"Agent '{agent_id}' has 'send_message' but no read tools (read_messages/batch_read_messages)")

    # read without send is a warning (can't reply)
    if has_read and not has_send:
        warnings.append(f"Agent '{agent_id}' has read tools but no 'send_message'")

    return errors, warnings


def validate_all_tool_dependencies(
    agent_to_tools: dict[str, set[str]],
    agent_to_connection_tools: Optional[dict[str, set[str]]] = None,
) -> tuple[list[str], list[str]]:
    """Validate tool dependencies for a group of given agents.

    Args:
        agent_to_tools: Mapping of agent_id → set of tool names for every agent.
        agent_to_connection_tools: Mapping of agent_id → union of tool names across
            that agent's connected agents. If None, computed from agent_to_tools
            (fully connected: each agent's connections = all other agents).

    Returns (errors, warnings) — lists of human-readable messages.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for agent_id, tools in agent_to_tools.items():
        if agent_to_connection_tools is not None:
            connection_tools = agent_to_connection_tools.get(agent_id, set())
        else:
            connection_tools = _compute_connection_tools(agent_id, agent_to_tools)
        dep_errors, dep_warnings = validate_tool_dependencies(agent_id, tools, connection_tools)
        errors.extend(dep_errors)
        warnings.extend(dep_warnings)
    return errors, warnings


# =============================================================================
# Review assignment validation (startup + runtime)
#   validate_review_assignment()  — per-agent check, used by:
#     startup: validate_task_assignments() below
#     runtime: _check_assignee() in tools/implementations.py
# =============================================================================


def validate_review_assignment(
    agent_id: str,
    agent_tools: set[str],
    connection_tools: set[str],
) -> Optional[str]:
    """Check that an agent can handle a review-required task.

    Args:
        agent_id: The agent being assigned the task.
        agent_tools: Tool names this agent has.
        connection_tools: Union of tool names across this agent's connected agents.

    Assumes the agent exists (caller should verify). Returns an error message,
    or None if valid. Used by both startup validation (validate_task_assignments)
    and runtime validation (tool implementations via _check_assignee).
    """
    if "tasks_submit_for_review" not in agent_tools:
        return f"agent '{agent_id}' cannot submit tasks for review"
    if "tasks_submit_review" not in connection_tools:
        return f"no connected agent of '{agent_id}' can conduct reviews"
    return None


# =============================================================================
# Initial task assignment validation (startup)
#   validate_task_assignments()  — batch over initial tasks, calls
#                                  validate_review_assignment() per task
# =============================================================================


def validate_task_assignments(
    tasks: list[tuple[Task, str, str]],
    agent_to_tools: dict[str, set[str]],
    agent_to_connection_tools: Optional[dict[str, set[str]]] = None,
) -> tuple[list[str], list[str]]:
    """Validate that initial task assignments are compatible with agent tools.

    Args:
        tasks: List of (task, assigned_agent_id) pairs.
        agent_to_tools: Mapping of agent_id → set of tool names for every agent.
        agent_to_connection_tools: Mapping of agent_id → union of tool names across that
            agent's connected agents. If None, computed from agent_to_tools
            (fully connected: each agent's connections = all other agents).

    Returns (errors, warnings).
    """
    if agent_to_connection_tools is None:
        agent_to_connection_tools = {aid: _compute_connection_tools(aid, agent_to_tools) for aid in agent_to_tools}

    errors: list[str] = []
    warnings: list[str] = []

    for task, agent_id, _ in tasks:
        if agent_id not in agent_to_tools:
            errors.append(f"Task '{task.id}' assigned to unknown agent '{agent_id}'")
            continue

        if not task.review_required:
            continue

        tools = agent_to_tools[agent_id]
        peers = agent_to_connection_tools.get(agent_id, set())

        error = validate_review_assignment(agent_id, tools, peers)
        if error:
            errors.append(f"Task '{task.id}' requires review but {error}")
            continue

        # Review required, can submit, but can't discover reviewers
        if "get_available_reviewers" not in tools:
            warnings.append(
                f"Task '{task.id}' requires review and agent '{agent_id}' can submit for review"
                f" but no tool to discover reviewers"
            )

    return errors, warnings

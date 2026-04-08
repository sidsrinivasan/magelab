"""
Registry domain types — agents, network topology, and lifecycle state.

All types here are pure data containers with query methods. No DB operations.
The Registry (registry.py) owns all persistence.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ..registry_config import NetworkConfig


# =============================================================================
# Agent types
# =============================================================================


class AgentState(str, Enum):
    """Lifecycle state of an agent instance."""

    IDLE = "idle"  # Waiting for work (queue empty)
    WORKING = "working"  # Actively processing an event
    REVIEWING = "reviewing"  # Actively reviewing another agent's task
    TERMINATED = "terminated"  # Stopped, won't process events


@dataclass(frozen=True)
class AgentSnapshot:
    """Lightweight read-only view of an agent. Returned by registry queries."""

    agent_id: str
    role: str
    model: str
    role_prompt: str
    tools: tuple[str, ...]
    max_turns: int
    state: AgentState
    current_task_id: Optional[str]
    created_at: datetime
    last_active_at: Optional[datetime]


@dataclass
class AgentInstance:
    """
    Runtime state of an agent instance.

    Created from AgentConfig, tracks runtime info and current state.
    Each agent has its own event queue for parallel execution.
    """

    agent_id: str
    role: str
    model: str
    role_prompt: str
    tools: list[str]
    max_turns: int

    # Event queue - agent pulls events from here
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    # Runtime state
    state: AgentState = AgentState.IDLE

    # Current work
    current_task_id: Optional[str] = None
    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: Optional[datetime] = None

    def to_snapshot(self) -> AgentSnapshot:
        """Create a lightweight read-only snapshot."""
        return AgentSnapshot(
            agent_id=self.agent_id,
            role=self.role,
            model=self.model,
            role_prompt=self.role_prompt,
            tools=tuple(self.tools),
            max_turns=self.max_turns,
            state=self.state,
            current_task_id=self.current_task_id,
            created_at=self.created_at,
            last_active_at=self.last_active_at,
        )


# =============================================================================
# Network types
# =============================================================================


class NetworkInstance:
    """In-memory agent communication graph.

    Pure data container with query and mutation methods. No DB operations.
    Built from a NetworkConfig DTO. The Registry owns persistence.
    """

    def __init__(self, config: NetworkConfig) -> None:
        # Store groups as sets (deduped from config lists)
        self._groups: dict[str, set[str]] = {k: set(v) for k, v in config.groups.items()}

        # Build reverse index: agent_id → set of group names
        self._agent_to_groups: dict[str, set[str]] = {}
        for group_name, members in self._groups.items():
            for agent_id in members:
                self._agent_to_groups.setdefault(agent_id, set()).add(group_name)

        # Symmetrize explicit connections
        self._connections: dict[str, set[str]] = {}
        for agent_id, conn_list in config.connections.items():
            for other_id in conn_list:
                if other_id == agent_id:
                    continue  # skip self-connections
                self._connections.setdefault(agent_id, set()).add(other_id)
                self._connections.setdefault(other_id, set()).add(agent_id)

    # =========================================================================
    # Queries
    # =========================================================================

    @property
    def all_agents(self) -> set[str]:
        """All agents in the network."""
        result: set[str] = set()
        for members in self._groups.values():
            result.update(members)
        for agent_id, connected in self._connections.items():
            result.add(agent_id)
            result.update(connected)
        return result

    def to_config(self) -> NetworkConfig:
        """Export current topology as a NetworkConfig DTO.

        Connections are deduplicated: each pair appears once under the
        lexicographically smaller agent_id, matching the canonical storage format.
        """
        groups = {name: sorted(members) for name, members in self._groups.items()}
        connections: dict[str, list[str]] = {}
        seen: set[tuple[str, str]] = set()
        for agent_id, peers in self._connections.items():
            for peer in peers:
                key = (min(agent_id, peer), max(agent_id, peer))
                if key not in seen:
                    seen.add(key)
                    connections.setdefault(key[0], []).append(key[1])
        for k in connections:
            connections[k] = sorted(connections[k])
        return NetworkConfig(groups=groups, connections=connections)

    def is_connected(self, agent_id: str, other_id: str) -> bool:
        """Check if two agents are connected (share a group or explicit connection)."""
        if other_id in self._connections.get(agent_id, set()):
            return True
        agent_groups = self._agent_to_groups.get(agent_id, set())
        other_groups = self._agent_to_groups.get(other_id, set())
        return bool(agent_groups & other_groups)

    def get_connected_ids(self, agent_id: str) -> set[str]:
        """Get all agent_ids connected to agent_id (via groups or connections), excluding self."""
        result: set[str] = set()
        for group_name in self._agent_to_groups.get(agent_id, set()):
            result.update(self._groups[group_name])
        result.update(self._connections.get(agent_id, set()))
        result.discard(agent_id)
        return result

    # =========================================================================
    # Mutation (in-memory only, Registry wraps with DB writes)
    # =========================================================================

    def add_to_group(self, agent_id: str, group_name: str) -> None:
        """Add an agent to a group. Creates the group if it doesn't exist. Idempotent."""
        self._groups.setdefault(group_name, set()).add(agent_id)
        self._agent_to_groups.setdefault(agent_id, set()).add(group_name)

    def add_connection(self, agent_id: str, other_id: str) -> None:
        """Add a symmetrized connection between two agents. Self-connections are ignored."""
        if agent_id == other_id:
            return
        self._connections.setdefault(agent_id, set()).add(other_id)
        self._connections.setdefault(other_id, set()).add(agent_id)

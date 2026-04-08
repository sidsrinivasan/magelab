"""
Registry — manages organizational structure and agent lifecycle.

Owns roles, agents, and network topology. Provides agent state tracking
(idle, working, reviewing, terminated), event queues, and connectivity queries.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from ..registry_config import AgentConfig, NetworkConfig, RoleConfig
from ..events import Event
from ..tools.bundles import expand
from .database import Database
from .registry_schemas import AgentInstance, AgentSnapshot, AgentState, NetworkInstance

ROLES_DDL = """
CREATE TABLE IF NOT EXISTS agent_roles (
    name            TEXT PRIMARY KEY,
    role_prompt     TEXT NOT NULL,
    tools           TEXT NOT NULL,
    model           TEXT NOT NULL,
    max_turns       INTEGER NOT NULL,
    session_config  TEXT
);
"""

AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS agent_instances (
    agent_id        TEXT PRIMARY KEY,
    role            TEXT NOT NULL,
    model           TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'idle',
    current_task_id TEXT,
    session_id      TEXT,
    created_at      TEXT NOT NULL,
    last_active_at  TEXT,
    role_prompt     TEXT,
    tools           TEXT,
    max_turns       INTEGER
);
"""

NETWORK_DDL = """
CREATE TABLE IF NOT EXISTS network_edges (
    agent_a TEXT NOT NULL,
    agent_b TEXT NOT NULL,
    PRIMARY KEY (agent_a, agent_b)
);

CREATE TABLE IF NOT EXISTS network_groups (
    group_name TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    PRIMARY KEY (group_name, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_network_groups_agent
    ON network_groups (agent_id);
"""


class Registry:
    """Registry of organizational structure and agent state."""

    def __init__(self, framework_logger: logging.Logger, db: Optional[Database] = None) -> None:
        self._roles: dict[str, RoleConfig] = {}
        self._agents: dict[str, AgentInstance] = {}
        self._network: Optional[NetworkInstance] = None
        self._framework_logger = framework_logger
        self._db = db
        self._state_listeners: list[Callable[[str, "AgentState", Optional[str]], None]] = []
        self._queue_listeners: list[Callable[[str, str, str, Optional[Event]], None]] = []
        if self._db:
            self._db.register_schema(ROLES_DDL)
            self._db.register_schema(AGENTS_DDL)
            self._db.register_schema(NETWORK_DDL)

    # =========================================================================
    # Listeners
    # =========================================================================

    def add_state_listener(self, fn: Callable[[str, AgentState, Optional[str]], None]) -> None:
        """Register a listener for agent state changes. Callback receives (agent_id, state, current_task_id)."""
        self._state_listeners.append(fn)

    def add_queue_listener(self, fn: Callable[[str, str, str, Optional[Event]], None]) -> None:
        """Register a listener for queue changes. Callback receives (agent_id, event_id, action, event)."""
        self._queue_listeners.append(fn)

    def _notify_state_listeners(self, agent_id: str, state: AgentState, task_id: Optional[str]) -> None:
        """Notify all state listeners of an agent state change."""
        for fn in self._state_listeners:
            try:
                fn(agent_id, state, task_id)
            except Exception:
                self._framework_logger.exception("Error in agent state listener")

    def _notify_queue_listeners(self, agent_id: str, event_id: str, action: str, event: Optional[Event] = None) -> None:
        """Notify all queue listeners of a queue change."""
        for fn in self._queue_listeners:
            try:
                fn(agent_id, event_id, action, event)
            except Exception:
                self._framework_logger.exception("Error in agent queue listener")

    # =========================================================================
    # Config and creation
    # =========================================================================

    def register_config(
        self,
        role_configs: dict[str, RoleConfig],
        agent_configs: dict[str, AgentConfig],
        network_config: Optional[NetworkConfig] = None,
    ) -> None:
        """Upsert structural state from config into DB and in-memory state.

        Resolves agent overrides, writes roles/agents/network to DB (upsert),
        and builds in-memory state. On conflict, structural fields are updated
        but operational state (agent lifecycle, session IDs) is preserved in DB.

        Call load_from_db() after this to pick up operational state from DB.
        Without a DB, this just builds in-memory state from the configs.
        """
        self._roles = dict(role_configs)
        self._agents = {}

        # Upsert roles to DB
        for role in self._roles.values():
            self._db_upsert_role(role)

        # Resolve overrides and create agents (create_agent handles in-memory + DB)
        # Network is set after agents — create_agent skips network validation when None.
        for agent_config in agent_configs.values():
            role = self._roles[agent_config.role]
            model = agent_config.model_override if agent_config.model_override is not None else role.model
            role_prompt = (
                agent_config.role_prompt_override if agent_config.role_prompt_override is not None else role.role_prompt
            )
            tools = expand(agent_config.tools_override) if agent_config.tools_override is not None else role.tools
            max_turns = (
                agent_config.max_turns_override if agent_config.max_turns_override is not None else role.max_turns
            )
            self.create_agent(agent_config.agent_id, agent_config.role, model, role_prompt, tools, max_turns)

        # Build and persist network
        if network_config is not None:
            self._network = NetworkInstance(network_config)
            self._db_persist_network(self._network)
        else:
            self._db_persist_network(
                NetworkInstance(NetworkConfig())
            )  # wipe prior network; no-network orgs are fully-connected
            self._network = None

        self._validate_network()

    def create_agent(
        self,
        agent_id: str,
        role: str,
        model: str,
        role_prompt: str,
        tools: list[str],
        max_turns: int,
        *,
        groups: Optional[list[str]] = None,
        connections: Optional[list[str]] = None,
    ) -> None:
        """Create an agent instance.

        If a network exists, at least one of groups or connections must be
        non-empty so the new agent is reachable. Without a network, the
        params are ignored (fully connected).
        """
        self._validate_role(role)
        if agent_id in self._agents:
            raise ValueError(f"Agent '{agent_id}' already exists")

        # Network membership validation
        if self._network is not None:
            has_groups = bool(groups)
            has_connections = bool(connections)
            if not has_groups and not has_connections:
                raise ValueError(
                    f"Agent '{agent_id}' requires network membership (groups or connections) "
                    f"when a network is configured"
                )

        # Validate connection targets exist
        if self._network is not None:
            for other_id in connections or []:
                if other_id not in self._agents:
                    raise ValueError(f"Connection target '{other_id}' does not exist")

        # Create agent instance first, then mutate network
        self._agents[agent_id] = AgentInstance(
            agent_id=agent_id,
            role=role,
            model=model,
            role_prompt=role_prompt,
            tools=tools,
            max_turns=max_turns,
        )

        if self._network is not None:
            for group_name in groups or []:
                self._network.add_to_group(agent_id, group_name)
                self._db_network_add_group(agent_id, group_name)
            for other_id in connections or []:
                self._network.add_connection(agent_id, other_id)
                self._db_network_add_connection(agent_id, other_id)

        agent = self._agents[agent_id]
        self._db_upsert_agent(agent)

    def get_role(self, role_name: str) -> Optional[RoleConfig]:
        """Get a role config by name. Returns None if not found."""
        return self._roles.get(role_name)

    def get_roles(self) -> dict[str, RoleConfig]:
        """Get all registered roles."""
        return dict(self._roles)

    def _validate_role(self, role: str) -> None:
        """Validate that a role is registered."""
        if role not in self._roles:
            raise ValueError(f"Unknown role '{role}'. Valid roles: {sorted(self._roles.keys())}")

    def _validate_network(self) -> None:
        """Validate that network topology matches registered agents."""
        if self._network is None:
            return
        agent_ids = set(self._agents.keys())
        network_agents = self._network.all_agents
        missing_from_network = agent_ids - network_agents
        if missing_from_network:
            raise ValueError(f"Agents not found in network: {sorted(missing_from_network)}")
        unknown_in_network = network_agents - agent_ids
        if unknown_in_network:
            raise ValueError(f"Network references unknown agents: {sorted(unknown_in_network)}")

    # =========================================================================
    # State management
    # =========================================================================

    def mark_working(self, agent_id: str, task_id: Optional[str] = None) -> None:
        """Mark agent as actively working on a task (or wire event)."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent '{agent_id}' not found")
        agent.state = AgentState.WORKING
        agent.current_task_id = task_id
        agent.last_active_at = datetime.now(timezone.utc)
        self._db_update_state(agent_id, AgentState.WORKING, task_id)
        self._notify_state_listeners(agent_id, AgentState.WORKING, task_id)

    def mark_reviewing(self, agent_id: str, task_id: str) -> None:
        """Mark agent as actively reviewing a task."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent '{agent_id}' not found")
        agent.state = AgentState.REVIEWING
        agent.current_task_id = task_id
        agent.last_active_at = datetime.now(timezone.utc)
        self._db_update_state(agent_id, AgentState.REVIEWING, task_id)
        self._notify_state_listeners(agent_id, AgentState.REVIEWING, task_id)

    def mark_idle(self, agent_id: str) -> None:
        """Mark agent as idle and clear current task."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent '{agent_id}' not found")
        agent.state = AgentState.IDLE
        agent.current_task_id = None
        agent.last_active_at = datetime.now(timezone.utc)
        self._db_update_state(agent_id, AgentState.IDLE, None)
        self._notify_state_listeners(agent_id, AgentState.IDLE, None)

    def mark_terminated(self, agent_id: str) -> None:
        """Mark agent as terminated. Keeps in registry for history."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent '{agent_id}' not found")
        agent.state = AgentState.TERMINATED
        agent.current_task_id = None
        agent.last_active_at = datetime.now(timezone.utc)
        self._db_update_state(agent_id, AgentState.TERMINATED, None)
        self._notify_state_listeners(agent_id, AgentState.TERMINATED, None)

    # =========================================================================
    # Queue operations
    # =========================================================================

    def enqueue(self, agent_id: str, event: Event) -> bool:
        """Enqueue an event for an agent. Sync (called from TaskStore callback).

        Silently drops events for unknown or terminated agents.

        Returns True if the event was enqueued, False if dropped.
        """
        agent = self._agents.get(agent_id)
        if not agent or agent.state == AgentState.TERMINATED:
            return False
        agent.queue.put_nowait(event)
        self._notify_queue_listeners(agent_id, event.event_id, "added", event)
        return True

    async def dequeue(self, agent_id: str, timeout: float = 1.0) -> Optional[Event]:
        """Dequeue an event for an agent. Returns None on timeout or if terminated."""
        agent = self._agents.get(agent_id)
        if not agent or agent.state == AgentState.TERMINATED:
            return None
        try:
            event = await asyncio.wait_for(agent.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        self._notify_queue_listeners(agent_id, event.event_id, "removed")
        return event

    def drain_queue(self, agent_id: str) -> list[Event]:
        """Drain all queued events for an agent (non-blocking). Returns empty list if none."""
        agent = self._agents.get(agent_id)
        if not agent or agent.state == AgentState.TERMINATED:
            return []
        events: list[Event] = []
        while not agent.queue.empty():
            event = agent.queue.get_nowait()
            events.append(event)
            self._notify_queue_listeners(agent_id, event.event_id, "removed")
        return events

    def get_queue_snapshot(self, agent_id: str) -> list[Event]:
        """Return a snapshot of the agent's pending event queue.

        Reads the internal deque of asyncio.Queue directly. This is a stable
        implementation detail (collections.deque since Python 3.1).
        """
        agent = self._agents.get(agent_id)
        if not agent:
            return []
        return list(agent.queue._queue)  # type: ignore[attr-defined]

    # =========================================================================
    # Queries
    # =========================================================================

    def get_agent_max_turns(self, agent_id: str) -> int:
        """Get an agent's max_turns setting."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Unknown agent '{agent_id}'")
        return agent.max_turns

    def list_agent_ids(self, *, active_only: bool = True) -> list[str]:
        """Get list of agent IDs."""
        return [a.agent_id for a in self._agents.values() if not active_only or a.state != AgentState.TERMINATED]

    def get_agent_snapshot(self, agent_id: str) -> Optional[AgentSnapshot]:
        """Get a read-only snapshot of an agent. Returns None if not found."""
        agent = self._agents.get(agent_id)
        return agent.to_snapshot() if agent else None

    def list_agent_snapshots(self) -> list[AgentSnapshot]:
        """List snapshots of all active agents."""
        return [a.to_snapshot() for a in self._agents.values() if a.state != AgentState.TERMINATED]

    # =========================================================================
    # Network connectivity
    # =========================================================================

    def get_network_config(self) -> Optional[NetworkConfig]:
        """Get the current network topology as a NetworkConfig, or None if fully connected."""
        if self._network is None:
            return None
        return self._network.to_config()

    def get_connected_ids(self, agent_id: str, *, active_only: bool = True) -> list[str]:
        """Get list of connected agent IDs based on network topology.

        - No network → fully connected (all other agents)
        - Network exists → group members + explicit connections
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Agent '{agent_id}' not found")

        if self._network is None:
            candidate_ids = set(self._agents.keys())
        else:
            candidate_ids = self._network.get_connected_ids(agent_id)

        candidate_ids.discard(agent_id)
        return [aid for aid in candidate_ids if not active_only or self._agents[aid].state != AgentState.TERMINATED]

    def all_quiescent(self) -> bool:
        """Check if all active agents are idle with empty queues."""
        for agent in self._agents.values():
            if agent.state == AgentState.TERMINATED:
                continue
            if agent.state != AgentState.IDLE or not agent.queue.empty():
                return False
        return True

    def compute_connection_tools(self) -> dict[str, set[str]]:
        """Compute agent_id → union of connected agents' tools for all active agents."""
        result: dict[str, set[str]] = {}
        for agent_id in self.list_agent_ids():
            connection_tools: set[str] = set()
            for connected_id in self.get_connected_ids(agent_id):
                snap = self.get_agent_snapshot(connected_id)
                if snap:
                    connection_tools.update(snap.tools)
            result[agent_id] = connection_tools
        return result

    def is_connected(self, agent_id: str, other_id: str) -> bool:
        """Check if two agents are connected based on network topology."""
        if agent_id not in self._agents:
            raise ValueError(f"Agent '{agent_id}' not found")
        if other_id not in self._agents:
            raise ValueError(f"Agent '{other_id}' not found")

        if self._network is None:
            return True
        return self._network.is_connected(agent_id, other_id)

    # =========================================================================
    # DB persistence
    # =========================================================================

    def _db_upsert_agent(self, agent: "AgentInstance") -> None:
        """Insert or update an agent row in DB.

        Writes both structural fields (role_prompt, tools, max_turns) and
        operational fields. On conflict, updates structural and config fields
        but preserves runtime state (state, current_task_id, session_id).
        """
        if not self._db:
            return
        self._db.execute(
            """INSERT INTO agent_instances
                   (agent_id, role, model, role_prompt, tools, max_turns,
                    state, current_task_id, session_id, created_at, last_active_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                   role = excluded.role, model = excluded.model,
                   role_prompt = excluded.role_prompt, tools = excluded.tools,
                   max_turns = excluded.max_turns
            """,
            (
                agent.agent_id,
                agent.role,
                agent.model,
                agent.role_prompt,
                json.dumps(agent.tools),
                agent.max_turns,
                agent.state.value,
                agent.current_task_id,
                None,
                agent.created_at.isoformat(),
                agent.last_active_at.isoformat() if agent.last_active_at else None,
            ),
        )
        self._db.commit()

    def _db_upsert_role(self, role: RoleConfig) -> None:
        """Insert or update a role row in DB."""
        if not self._db:
            return
        self._db.execute(
            """INSERT INTO agent_roles (name, role_prompt, tools, model, max_turns, session_config)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   role_prompt = excluded.role_prompt, tools = excluded.tools,
                   model = excluded.model, max_turns = excluded.max_turns,
                   session_config = excluded.session_config
            """,
            (
                role.name,
                role.role_prompt,
                json.dumps(role.tools),
                role.model,
                role.max_turns,
                role.session_config,
            ),
        )
        self._db.commit()

    def _db_update_state(self, agent_id: str, state: AgentState, task_id: Optional[str]) -> None:
        """Write agent state change to DB if available."""
        if not self._db:
            return
        agent = self._agents[agent_id]
        last_active = agent.last_active_at.isoformat() if agent.last_active_at else None
        self._db.execute(
            "UPDATE agent_instances SET state = ?, current_task_id = ?, last_active_at = ? WHERE agent_id = ?",
            (state.value, task_id, last_active, agent_id),
        )
        self._db.commit()

    def update_session(self, agent_id: str, session_id: str) -> None:
        """Persist a Claude session ID for resume."""
        if not self._db:
            return
        self._db.execute("UPDATE agent_instances SET session_id = ? WHERE agent_id = ?", (session_id, agent_id))
        self._db.commit()

    def load_from_db(self) -> int:
        """Load all state from DB, overwriting in-memory state.

        Loads roles, agents (structural + operational), and network.
        Returns the number of agents loaded.
        """
        self._roles = self._load_roles_from_db()
        self._agents = self._load_agents_from_db()
        self._network = self._load_network_from_db()
        return len(self._agents)

    def _load_roles_from_db(self) -> dict[str, RoleConfig]:
        """Load all roles from DB, returning a dict of name → RoleConfig."""
        if not self._db:
            return {}
        rows = self._db.fetchall("SELECT * FROM agent_roles")
        roles: dict[str, RoleConfig] = {}
        for row in rows:
            roles[row["name"]] = RoleConfig(
                name=row["name"],
                role_prompt=row["role_prompt"],
                tools=json.loads(row["tools"]),
                model=row["model"],
                max_turns=row["max_turns"],
                session_config=row.get("session_config"),
            )
        return roles

    def _load_agents_from_db(self) -> dict[str, AgentInstance]:
        """Load all agents from DB, returning a dict of agent_id → AgentInstance."""
        if not self._db:
            return {}
        rows = self._db.fetchall("SELECT * FROM agent_instances")
        agents: dict[str, AgentInstance] = {}
        for row in rows:
            last_active = row.get("last_active_at")
            agents[row["agent_id"]] = AgentInstance(
                agent_id=row["agent_id"],
                role=row["role"],
                model=row["model"],
                role_prompt=row["role_prompt"] or "",
                tools=json.loads(row["tools"]) if row["tools"] else [],
                max_turns=row["max_turns"] or 100,
                state=AgentState(row["state"]),
                current_task_id=row.get("current_task_id"),
                created_at=datetime.fromisoformat(row["created_at"]),
                last_active_at=datetime.fromisoformat(last_active) if last_active else None,
            )
        return agents

    def get_session_ids(self) -> dict[str, str]:
        """Return agent_id → session_id for all agents with a session."""
        if not self._db:
            return {}
        rows = self._db.fetchall("SELECT agent_id, session_id FROM agent_instances WHERE session_id IS NOT NULL")
        return {row["agent_id"]: row["session_id"] for row in rows}

    def _db_persist_network(self, network: NetworkInstance) -> None:
        """Wipe-and-rewrite all network tables from in-memory state.

        The YAML network config is the complete topology spec. Runtime-added
        connections survive across runs because the pipeline reconstructs the
        config from DB between runs (capturing runtime additions).
        """
        if not self._db:
            return
        config = network.to_config()
        with self._db.transaction():
            self._db.execute("DELETE FROM network_groups")
            self._db.execute("DELETE FROM network_edges")
            for group_name, members in config.groups.items():
                for agent_id in members:
                    self._db.execute(
                        "INSERT INTO network_groups (group_name, agent_id) VALUES (?, ?)",
                        (group_name, agent_id),
                    )
            for agent_id, targets in config.connections.items():
                for other_id in targets:
                    a, b = sorted((agent_id, other_id))
                    self._db.execute(
                        "INSERT OR IGNORE INTO network_edges (agent_a, agent_b) VALUES (?, ?)",
                        (a, b),
                    )

    def _db_network_add_group(self, agent_id: str, group_name: str) -> None:
        """Persist a single group membership addition."""
        if not self._db:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO network_groups (group_name, agent_id) VALUES (?, ?)",
            (group_name, agent_id),
        )
        self._db.commit()

    def _db_network_add_connection(self, agent_id: str, other_id: str) -> None:
        """Persist a single connection addition."""
        if not self._db:
            return
        a, b = sorted((agent_id, other_id))
        self._db.execute(
            "INSERT OR IGNORE INTO network_edges (agent_a, agent_b) VALUES (?, ?)",
            (a, b),
        )
        self._db.commit()

    def _load_network_from_db(self) -> Optional[NetworkInstance]:
        """Reconstruct a NetworkInstance from DB tables.

        Returns None if no network topology exists (fully connected org).
        Builds a NetworkConfig from DB rows, then constructs a NetworkInstance.
        """
        if not self._db:
            return None
        group_rows = self._db.fetchall("SELECT group_name, agent_id FROM network_groups")
        edge_rows = self._db.fetchall("SELECT agent_a, agent_b FROM network_edges")

        if not group_rows and not edge_rows:
            return None

        # Build a NetworkConfig from DB rows
        groups: dict[str, list[str]] = {}
        for row in group_rows:
            groups.setdefault(row["group_name"], []).append(row["agent_id"])

        connections: dict[str, list[str]] = {}
        for row in edge_rows:
            connections.setdefault(row["agent_a"], []).append(row["agent_b"])

        config = NetworkConfig(groups=groups, connections=connections)
        return NetworkInstance(config)

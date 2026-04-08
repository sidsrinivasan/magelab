"""
Structural configuration types for the registry — roles, agents, and network topology.

These DTOs describe what the org looks like. They are consumed by the Registry
to build runtime state. OrgConfig (in org_config.py) composes these into
the complete org specification.
"""

from dataclasses import dataclass, field
from typing import Optional

from .tools.bundles import expand


# =============================================================================
# Role Configuration
# =============================================================================


@dataclass
class RoleConfig:
    """
    Template for a type of agent.

    Defines the capabilities and behavior pattern for agents of this role.
    Multiple agents can share the same role (e.g., multiple "coder" agents).
    Individual agents can override any of these via AgentConfig.*_override fields.
    """

    name: str
    """Unique identifier for this role (e.g., 'pm', 'coder', 'reviewer')."""

    role_prompt: str
    """Role-specific prompt defining the agent's persona and capabilities."""

    tools: list[str]
    """
    Tool names this role has access to.
    Framework resolves these to actual tool implementations.
    Examples: ['tasks_create', 'tasks_mark_finished', 'connections_list']
    """

    model: str
    """Model for agents of this role (e.g., 'claude-sonnet-4-6')."""

    max_turns: int = 100
    """Max LLM turns per agent run."""

    session_config: Optional[str] = None
    """Path to session config directory for agents of this role. Copied into
    each agent's session directory before the run. Can contain settings.json,
    .mcp.json, skills/, agents/, CLAUDE.md, etc. Relative to agent_settings_dir."""

    def __post_init__(self):
        self.tools = expand(self.tools)


# =============================================================================
# Agent Configuration
# =============================================================================


@dataclass
class AgentConfig:
    """
    Configuration for a specific agent instance.

    Links an agent_id to a role. All other fields are optional overrides —
    if None, the role's default is used.
    """

    agent_id: str
    """Unique identifier for this agent (e.g., 'coder_0', 'pm_1')."""

    role: str
    """Name of the role this agent uses (must match a RoleConfig.name)."""

    # Optional overrides (None = use role default)
    role_prompt_override: Optional[str] = None
    tools_override: Optional[list[str]] = None
    model_override: Optional[str] = None
    max_turns_override: Optional[int] = None
    session_config_override: Optional[str] = None


# =============================================================================
# Network Configuration
# =============================================================================


@dataclass
class NetworkConfig:
    """Network topology configuration (pure DTO).

    Stores raw YAML data for groups and connections. Runtime logic
    (symmetrization, queries, mutation) lives in NetworkInstance (registry_schemas.py).
    """

    groups: dict[str, list[str]] = field(default_factory=dict)
    """Group definitions: group_name → list of member agent_ids."""

    connections: dict[str, list[str]] = field(default_factory=dict)
    """Explicit connections: agent_id → list of connected agent_ids (raw from YAML, not symmetrized)."""

    def __post_init__(self) -> None:
        for group_name, members in self.groups.items():
            if not members:
                raise ValueError(f"Network group '{group_name}' is empty")
            if len(members) != len(set(members)):
                raise ValueError(f"Network group '{group_name}' has duplicate members")
        for agent_id, conn_list in self.connections.items():
            if agent_id in conn_list:
                raise ValueError(f"Network connection for '{agent_id}' cannot include self")
            if not conn_list:
                raise ValueError(f"Network agent '{agent_id}' has empty connections list")
            if len(conn_list) != len(set(conn_list)):
                raise ValueError(f"Network connections for '{agent_id}' has duplicates")

    @property
    def all_agents(self) -> set[str]:
        """All agents mentioned in the network config (used by OrgConfig validation)."""
        result: set[str] = set()
        for members in self.groups.values():
            result.update(members)
        for agent_id, conn_list in self.connections.items():
            result.add(agent_id)
            result.update(conn_list)
        return result

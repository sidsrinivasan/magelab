"""
Organization-level configuration — OrgConfig, OrgSettings, and enums.

OrgConfig is the top-level DTO that assembles structural configs (from
registry_config.py) with behavioral settings and run inputs into a
complete org specification.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml

from .registry_config import AgentConfig, NetworkConfig, RoleConfig
from .state.task_schemas import SystemAgent, Task


class ResumeMode(str, Enum):
    """How to resume from a prior run's DB."""

    CONTINUE = "continue"
    FRESH = "fresh"


class WireNotifications(str, Enum):
    """Which notification paths are active for wire messages."""

    ALL = "all"
    """Both tool-response injection and event queuing."""

    TOOL = "tool"
    """Append unread count to every tool response only."""

    EVENT = "event"
    """Queue wire events for idle agents only."""

    NONE = "none"
    """No automatic notifications (agents must poll manually)."""


_INITIAL_MSG_REQUIRED = {"participants", "body"}
_INITIAL_MSG_VALID = _INITIAL_MSG_REQUIRED | {"sender", "wire_id", "task_id"}


def _parse_initial_message(index: int, entry: dict, path: str) -> dict:
    """Validate and return an initial_messages entry.

    Sender defaults to SystemAgent.USER if omitted.
    """

    msg = dict(entry)
    missing = _INITIAL_MSG_REQUIRED - msg.keys()
    if missing:
        raise ValueError(f"initial_messages[{index}] missing required fields {sorted(missing)} in {path}")
    unknown = msg.keys() - _INITIAL_MSG_VALID
    if unknown:
        raise ValueError(f"initial_messages[{index}] has unknown fields {sorted(unknown)} in {path}")
    msg.setdefault("sender", SystemAgent.USER)
    return msg


# =============================================================================
# Organization Settings
# =============================================================================


@dataclass
class OrgSettings:
    """Behavioral settings that control how the org runs.

    Single source of truth for all non-structural, non-input settings.
    OrgConfig composes this as a `settings` field — no duplication.
    """

    org_name: str = "magelab"
    """Human-readable name for this organization config."""

    org_description: str = ""
    """Human-readable description of this config variant."""

    org_prompt: str = ""
    """Org-level prompt prepended to every agent's role prompt. Supports optional '{agent_id}' placeholder."""

    org_permission_mode: str = "acceptEdits"
    """SDK permission mode for tool calls."""

    org_timeout_seconds: float = 3600.0
    """Max time (seconds) for entire run before timeout. Default 1 hour."""

    agent_timeout_seconds: float = 900.0
    """Max time (seconds) for a single agent run before timeout. Default 15 minutes."""

    agent_settings_dir: Optional[str] = None
    """Path to agent settings directory (relative to the config YAML's directory).
    Contains per-role subdirectories with backend extension points (settings,
    MCP servers, plugins, skills, etc.). Resolved to an absolute path by
    from_yaml(). The framework fans out per-role configs into each agent's
    session directory based on role session_config and agent
    session_config_override."""

    wire_notifications: WireNotifications = WireNotifications.ALL
    """Which notification paths are active for wire messages: all, tool, event, or none."""

    wire_max_unread_per_prompt: int = 10
    """Max unread conversations delivered in a single wire event prompt."""

    mcp_modules: dict[str, str] = field(default_factory=dict)
    """In-process MCP servers: name → Python module path. The module must expose
    a ``server`` attribute that is a FastMCP instance. Tools with an ``agent_id``
    parameter get it auto-injected by the framework (hidden from the agent)."""

    sync: bool = False
    """If True, run in synchronized round-based mode (no agent loops)."""

    sync_max_rounds: Optional[int] = None
    """Maximum number of rounds in sync mode. Only valid when sync=True."""

    sync_round_timeout_seconds: Optional[float] = None
    """Max time (seconds) per sync round. Only valid when sync=True. None = no per-round limit."""

    def __post_init__(self) -> None:
        """Validate settings."""
        errors = self._validate()
        if errors:
            raise ValueError(f"Invalid OrgSettings: {'; '.join(errors)}")

    def _validate(self) -> list[str]:
        errors: list[str] = []
        if not self.org_name or not self.org_name.strip():
            errors.append("org_name must be a non-empty string")
        if self.org_timeout_seconds <= 0:
            errors.append(f"org_timeout_seconds must be > 0, got {self.org_timeout_seconds}")
        if self.agent_timeout_seconds <= 0:
            errors.append(f"agent_timeout_seconds must be > 0, got {self.agent_timeout_seconds}")
        if self.sync_max_rounds is not None and not self.sync:
            errors.append("sync_max_rounds can only be specified when sync=True")
        if self.sync and self.sync_max_rounds is None:
            errors.append("sync_max_rounds is required when sync=True")
        if self.sync_max_rounds is not None and self.sync_max_rounds <= 0:
            errors.append(f"sync_max_rounds must be > 0, got {self.sync_max_rounds}")
        if self.sync_round_timeout_seconds is not None and not self.sync:
            errors.append("sync_round_timeout_seconds can only be specified when sync=True")
        if self.sync_round_timeout_seconds is not None and self.sync_round_timeout_seconds <= 0:
            errors.append(f"sync_round_timeout_seconds must be > 0, got {self.sync_round_timeout_seconds}")
        return errors


# =============================================================================
# Organization Configuration
# =============================================================================


@dataclass
class OrgConfig:
    """
    Complete configuration for a multi-agent organization (DTO).

    Fields are grouped into three categories:
    - **Structural**: What the org looks like (roles, agents, network).
    - **Settings**: How the org behaves (composed OrgSettings).
    - **Run inputs**: What to do this run (initial tasks/messages, resume mode).
      Consumed once; effects persist as operational state in stores.
    """

    # ── Structural ──────────────────────────────────────────────────────────

    roles: dict[str, RoleConfig]
    """Role definitions keyed by role name."""

    agents: dict[str, AgentConfig]
    """Agent configurations keyed by agent_id."""

    network: Optional[NetworkConfig] = None
    """Network topology. None = fully connected."""

    # ── Settings ────────────────────────────────────────────────────────────

    settings: OrgSettings = field(default_factory=OrgSettings)
    """Behavioral settings for the org run."""

    # ── Run inputs ──────────────────────────────────────────────────────────

    initial_tasks: list[tuple[Task, str, str]] = field(default_factory=list)
    """Parsed (Task, assigned_to, assigned_by) triples of initial task assignments."""

    initial_messages: list[dict] = field(default_factory=list)
    """Wire messages to send at startup, after initial tasks are created.
    Each dict has required keys: participants (list[str]), body (str).
    Optional keys: sender (str, defaults to SystemAgent.USER), wire_id
    (str), task_id (str)."""

    resume_mode: Optional[ResumeMode] = None
    """Resume mode for pipeline runs. None = fresh run. Set by pipeline steps
    to control how the next org run resumes from the DB."""

    @classmethod
    def from_yaml(cls, path: str) -> "OrgConfig":
        """Load an OrgConfig from a YAML file.

        Expects a nested `settings:` key for behavioral settings.
        """
        with open(path, "r") as f:
            config = yaml.safe_load(f)

        for key in ("roles", "agents"):
            if key not in config:
                raise ValueError(f"OrgConfig YAML missing required key '{key}' in {path}")

        roles = {name: RoleConfig(**d) for name, d in config["roles"].items()}
        agents = {aid: AgentConfig(**d) for aid, d in config["agents"].items()}

        # Build settings from nested key
        settings_raw = dict(config.get("settings", {}))
        if "wire_notifications" in settings_raw:
            settings_raw["wire_notifications"] = WireNotifications(settings_raw["wire_notifications"])
        if settings_raw.get("agent_settings_dir"):
            config_dir = Path(path).parent
            settings_raw["agent_settings_dir"] = str((config_dir / settings_raw["agent_settings_dir"]).resolve())
        settings = OrgSettings(**settings_raw)

        kwargs: dict = dict(
            roles=roles,
            agents=agents,
            settings=settings,
        )

        # Structural
        if config.get("network"):
            kwargs["network"] = NetworkConfig(**config["network"])

        # Run inputs
        initial_tasks = []
        for i, entry in enumerate(config.get("initial_tasks", [])):
            try:
                assigned_to = entry["assigned_to"]
                assigned_by = entry.get("assigned_by", SystemAgent.USER)
                task_fields = {k: v for k, v in entry.items() if k not in ("assigned_to", "assigned_by")}
                initial_tasks.append((Task(**task_fields), assigned_to, assigned_by))
            except KeyError as e:
                raise ValueError(f"initial_tasks[{i}] missing required field {e} in {path}") from e
            except Exception as e:
                raise ValueError(f"initial_tasks[{i}] invalid in {path}: {e}") from e
        kwargs["initial_tasks"] = initial_tasks

        kwargs["initial_messages"] = [
            _parse_initial_message(i, entry, path) for i, entry in enumerate(config.get("initial_messages", []))
        ]

        if config.get("resume_mode"):
            kwargs["resume_mode"] = ResumeMode(config["resume_mode"])

        return cls(**kwargs)

    @classmethod
    def from_dict(cls, raw: dict) -> "OrgConfig":
        """Construct an OrgConfig from a to_dict()-style dict.

        Expects settings nested under a "settings" key (matching to_dict output).
        """
        raw = dict(raw)

        # Structural
        raw["roles"] = {name: r if isinstance(r, RoleConfig) else RoleConfig(**r) for name, r in raw["roles"].items()}
        raw["agents"] = {aid: a if isinstance(a, AgentConfig) else AgentConfig(**a) for aid, a in raw["agents"].items()}
        net = raw.get("network", None)
        if net and not isinstance(net, NetworkConfig):
            raw["network"] = NetworkConfig(**net)

        # Settings
        settings_raw = dict(raw.pop("settings", {}))
        if isinstance(settings_raw, dict):
            wn = settings_raw.get("wire_notifications")
            if wn and not isinstance(wn, WireNotifications):
                settings_raw["wire_notifications"] = WireNotifications(wn)
            raw["settings"] = OrgSettings(**settings_raw)

        # Run inputs
        rm = raw.get("resume_mode")
        if rm and not isinstance(rm, ResumeMode):
            raw["resume_mode"] = ResumeMode(rm)

        raw_tasks = raw.get("initial_tasks", [])
        if raw_tasks and isinstance(raw_tasks[0], dict):
            parsed_tasks = []
            for entry in raw_tasks:
                entry = dict(entry)
                assigned_to = entry.pop("assigned_to")
                assigned_by = entry.pop("assigned_by", SystemAgent.USER)
                parsed_tasks.append((Task(**entry), assigned_to, assigned_by))
            raw["initial_tasks"] = parsed_tasks

        return cls(**raw)

    def __post_init__(self) -> None:
        """Validate structural config and cross-cutting constraints."""
        errors = self._validate()
        if errors:
            raise ValueError(f"Invalid OrgConfig: {'; '.join(errors)}")

    def _validate(self) -> list[str]:
        """Validate structural config. Settings validation is in OrgSettings."""
        errors: list[str] = []

        # Check role keys match role.name
        for name, role in self.roles.items():
            if role.name != name:
                errors.append(f"Role key '{name}' doesn't match role.name '{role.name}'")
            if not role.model:
                errors.append(f"Role '{name}' has empty model")
            if role.max_turns <= 0:
                errors.append(f"Role '{name}' has max_turns={role.max_turns}, must be > 0")
            if not role.role_prompt or not role.role_prompt.strip():
                errors.append(f"Role '{name}' has empty role_prompt")

        # Check agent keys match agent.agent_id
        for name, agent in self.agents.items():
            if agent.agent_id != name:
                errors.append(f"Agent key '{name}' doesn't match agent.agent_id '{agent.agent_id}'")

        # Check all agents reference valid roles and have valid overrides
        role_names = set(self.roles.keys())
        for agent in self.agents.values():
            if agent.role not in role_names:
                errors.append(f"Agent '{agent.agent_id}' references unknown role '{agent.role}'")
            if agent.max_turns_override is not None and agent.max_turns_override <= 0:
                errors.append(
                    f"Agent '{agent.agent_id}' has max_turns_override={agent.max_turns_override}, must be > 0"
                )

        # Check network ↔ agents consistency (both directions)
        if self.network is not None:
            agent_ids = set(self.agents.keys())
            network_agents = self.network.all_agents
            for agent_id in network_agents - agent_ids:
                errors.append(f"Network references unknown agent '{agent_id}'")
            for agent_id in agent_ids - network_agents:
                errors.append(f"Agent '{agent_id}' not mentioned in network")

        # Check initial_tasks reference valid agents and have unique IDs
        if self.initial_tasks:
            agent_ids = set(self.agents.keys())
            task_ids = set()
            for task, assigned_to, _ in self.initial_tasks:
                if assigned_to not in agent_ids:
                    errors.append(f"initial_task '{task.id}' assigned to unknown agent '{assigned_to}'")
                if task.id in task_ids:
                    errors.append(f"initial_task '{task.id}' has duplicate ID")
                task_ids.add(task.id)

        # Check initial_messages: participants must be registered agents.
        if self.initial_messages:
            agent_ids = set(self.agents.keys())
            for i, msg in enumerate(self.initial_messages):
                for pid in msg["participants"]:
                    if pid not in agent_ids:
                        errors.append(f"initial_messages[{i}] participant '{pid}' is not a registered agent")

        return errors

    def to_dict(self) -> dict:
        """Serialize OrgConfig to a JSON-safe dict.

        Settings stay nested under a "settings" key. Enums are converted to
        plain strings. The output is safe for json.dumps() and yaml.dump().
        """
        raw = asdict(self)

        # Convert enums to plain strings in settings
        settings_dict = raw["settings"]
        for key, val in settings_dict.items():
            if isinstance(val, Enum):
                settings_dict[key] = val.value

        # Convert top-level enums (resume_mode)
        if raw.get("resume_mode") and isinstance(raw["resume_mode"], Enum):
            raw["resume_mode"] = raw["resume_mode"].value

        # asdict doesn't know about Pydantic models inside tuples
        raw["initial_tasks"] = [
            {
                **task.model_dump(mode="json"),
                "assigned_to": agent_id,
                "assigned_by": assigned_by.value if isinstance(assigned_by, Enum) else assigned_by,
            }
            for task, agent_id, assigned_by in self.initial_tasks
        ]

        # Convert SystemAgent enum in initial_messages sender
        raw["initial_messages"] = [
            {**msg, "sender": msg["sender"].value if isinstance(msg.get("sender"), Enum) else msg.get("sender")}
            for msg in raw["initial_messages"]
        ]

        # Strip None overrides from agents for cleaner output
        raw["agents"] = {aid: {k: v for k, v in agent.items() if v is not None} for aid, agent in raw["agents"].items()}

        return raw

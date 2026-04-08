"""Tests for magelab.config — RoleConfig, AgentConfig, OrgConfig."""

import dataclasses

import pytest
import yaml

from magelab.org_config import OrgConfig, OrgSettings, WireNotifications
from magelab.registry_config import AgentConfig, NetworkConfig, RoleConfig
from magelab.state.task_schemas import Task
from magelab.tools.bundles import expand
from tests.helpers import make_agent_config, make_org_config, make_role

# =============================================================================
# RoleConfig
# =============================================================================


class TestRoleConfig:
    def test_basic_creation(self):
        r = make_role(name="coder", tools=["worker", "claude_basic"])
        assert r.name == "coder"
        assert r.model == "test-model"
        assert r.max_turns == 10
        assert isinstance(r.tools, list)

    def test_tools_expanded(self):
        """Bundle names in tools list should be expanded to individual tool names."""
        r = make_role(tools=["worker"])
        # "worker" bundle expands to submit_for_review, mark_finished, get_available_reviewers
        assert "tasks_submit_for_review" in r.tools
        assert "tasks_mark_finished" in r.tools
        assert "get_available_reviewers" in r.tools

    def test_tools_deduplication(self):
        """Duplicate tool names across bundles should be deduplicated."""
        r = make_role(tools=["worker", "management"])
        # Both contain tasks_mark_finished
        assert r.tools.count("tasks_mark_finished") == 1

    def test_unknown_tool_raises_in_strict_mode(self):
        """Non-bundle, non-framework tool names raise ValueError in strict mode (default)."""
        with pytest.raises(ValueError, match="Unknown tool or bundle"):
            make_role(tools=["custom_tool"])

    def test_unknown_tool_kept_with_strict_false(self):
        """Non-bundle tool names pass through when strict=False."""

        result = expand(["custom_tool"], strict=False)
        assert result == ["custom_tool"]

    def test_defaults(self):
        """RoleConfig defaults: max_turns=100."""
        r = RoleConfig(name="d", role_prompt="Prompt", tools=[], model="test")
        assert r.max_turns == 100


# =============================================================================
# AgentConfig
# =============================================================================


class TestAgentConfig:
    def test_basic_creation(self):
        a = make_agent_config(agent_id="coder-0", role="coder")
        assert a.agent_id == "coder-0"
        assert a.role == "coder"
        assert a.role_prompt_override is None
        assert a.model_override is None

    def test_overrides(self):
        a = AgentConfig(
            agent_id="special",
            role="worker",
            model_override="claude-3",
            max_turns_override=5,
            role_prompt_override="Custom prompt",
        )
        assert a.model_override == "claude-3"
        assert a.max_turns_override == 5
        assert a.role_prompt_override == "Custom prompt"

    def test_tools_override(self):
        """tools_override field stores a custom tool list."""
        a = AgentConfig(
            agent_id="custom",
            role="worker",
            tools_override=["Read", "Write"],
        )
        assert a.tools_override == ["Read", "Write"]


# =============================================================================
# OrgConfig — construction & validation
# =============================================================================


class TestOrgConfig:
    def test_valid_minimal(self):
        org = make_org_config()
        assert org.settings.org_name == "test-org"
        assert "worker" in org.roles
        assert "worker-0" in org.agents

    def test_invalid_empty_name(self):
        with pytest.raises(ValueError, match="org_name must be a non-empty string"):
            make_org_config(name="")

    def test_invalid_whitespace_name(self):
        with pytest.raises(ValueError, match="org_name must be a non-empty string"):
            make_org_config(name="  ")

    def test_invalid_timeout_zero(self):
        with pytest.raises(ValueError, match="org_timeout_seconds must be > 0"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x", org_timeout_seconds=0),
            )

    def test_invalid_agent_timeout(self):
        with pytest.raises(ValueError, match="agent_timeout_seconds must be > 0"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x", agent_timeout_seconds=-1),
            )

    def test_role_key_mismatch(self):
        """Role dict key must match role.name."""
        with pytest.raises(ValueError, match="doesn't match role.name"):
            OrgConfig(
                roles={"wrong_key": make_role(name="actual_name")},
                agents={},
                settings=OrgSettings(org_name="x"),
            )

    def test_agent_key_mismatch(self):
        """Agent dict key must match agent.agent_id."""
        with pytest.raises(ValueError, match="doesn't match agent.agent_id"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"wrong_key": make_agent_config(agent_id="actual_id", role="w")},
                settings=OrgSettings(org_name="x"),
            )

    def test_agent_references_unknown_role(self):
        with pytest.raises(ValueError, match="unknown role"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="nonexistent")},
                settings=OrgSettings(org_name="x"),
            )

    def test_role_empty_prompt(self):
        with pytest.raises(ValueError, match="empty role_prompt"):
            OrgConfig(
                roles={"w": make_role(name="w", role_prompt="")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x"),
            )

    def test_role_invalid_max_turns(self):
        with pytest.raises(ValueError, match="max_turns=0"):
            OrgConfig(
                roles={"w": make_role(name="w", max_turns=0)},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x"),
            )

    def test_agent_invalid_max_turns_override(self):
        with pytest.raises(ValueError, match="max_turns_override"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": AgentConfig(agent_id="a", role="w", max_turns_override=-1)},
                settings=OrgSettings(org_name="x"),
            )

    def test_multiple_errors_collected(self):
        """Multiple validation errors are collected and reported together.

        org_name validation now happens in OrgSettings, so we test structural
        errors collected by OrgConfig validation separately.
        """
        with pytest.raises(ValueError) as exc_info:
            OrgConfig(
                roles={"w": make_role(name="w", role_prompt="")},
                agents={"a": make_agent_config(agent_id="a", role="nonexistent")},
            )
        msg = str(exc_info.value)
        assert "empty role_prompt" in msg
        assert "unknown role" in msg

    def test_role_empty_model(self):
        """Role with model='' triggers 'empty model' validation error (line 164)."""
        with pytest.raises(ValueError, match="empty model"):
            OrgConfig(
                roles={"w": make_role(name="w", model="")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x"),
            )

    def test_defaults(self):
        """OrgConfig defaults: org_prompt, permission_mode, timeouts."""
        org = make_org_config()
        assert org.settings.org_prompt == ""
        assert org.settings.org_permission_mode == "acceptEdits"
        assert org.settings.org_timeout_seconds == 3600.0
        assert org.settings.agent_timeout_seconds == 900.0

    def test_invalid_agent_timeout_zero(self):
        """agent_timeout_seconds=0 should be rejected like global_timeout_seconds=0."""
        with pytest.raises(ValueError, match="agent_timeout_seconds must be > 0"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x", agent_timeout_seconds=0),
            )

    def test_whitespace_only_role_prompt_rejected(self):
        """Whitespace-only role_prompt is rejected, consistent with name validation."""
        with pytest.raises(ValueError, match="empty role_prompt"):
            OrgConfig(
                roles={"w": make_role(name="w", role_prompt="   ")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x"),
            )

    def test_empty_roles_and_agents(self):
        """OrgConfig with no roles and no agents should construct successfully."""
        org = OrgConfig(roles={}, agents={}, settings=OrgSettings(org_name="empty"))
        assert org.settings.org_name == "empty"
        assert len(org.roles) == 0
        assert len(org.agents) == 0

    def test_round_timeout_without_sync_raises(self):
        with pytest.raises(ValueError, match="sync_round_timeout_seconds can only be specified when sync=True"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x", sync_round_timeout_seconds=60.0),
            )

    def test_round_timeout_negative_raises(self):
        with pytest.raises(ValueError, match="sync_round_timeout_seconds must be > 0"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x", sync=True, sync_max_rounds=10, sync_round_timeout_seconds=-1),
            )

    def test_round_timeout_with_sync_valid(self):
        org = OrgConfig(
            roles={"w": make_role(name="w")},
            agents={"a": make_agent_config(agent_id="a", role="w")},
            settings=OrgSettings(org_name="x", sync=True, sync_max_rounds=10, sync_round_timeout_seconds=120.0),
        )
        assert org.settings.sync_round_timeout_seconds == 120.0

    def test_sync_without_max_rounds_raises(self):
        """sync=True without sync_max_rounds should fail validation."""
        with pytest.raises(ValueError, match="sync_max_rounds is required"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x", sync=True),
            )

    def test_initial_task_unknown_agent_raises(self):
        """initial_task assigned to an unregistered agent should fail validation."""
        task = Task(id="t1", title="Test", description="Do it")
        with pytest.raises(ValueError, match="unknown agent"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x"),
                initial_tasks=[(task, "nonexistent", "User")],
            )

    def test_initial_task_duplicate_id_raises(self):
        """Two initial_tasks with the same ID should fail validation."""
        t1 = Task(id="t1", title="Test", description="Do it")
        t2 = Task(id="t1", title="Test2", description="Do it again")
        with pytest.raises(ValueError, match="duplicate ID"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x"),
                initial_tasks=[(t1, "a", "User"), (t2, "a", "User")],
            )

    def test_initial_message_unknown_participant_raises(self):
        """initial_messages with an unknown participant should fail validation."""
        with pytest.raises(ValueError, match="not a registered agent"):
            OrgConfig(
                roles={"w": make_role(name="w")},
                agents={"a": make_agent_config(agent_id="a", role="w")},
                settings=OrgSettings(org_name="x"),
                initial_messages=[{"participants": ["a", "ghost"], "body": "hello"}],
            )

    def test_multiple_roles_and_agents(self):
        """OrgConfig with 2+ roles and 3+ agents constructs successfully."""
        roles = {
            "coder": make_role(name="coder", role_prompt="You code."),
            "reviewer": make_role(name="reviewer", role_prompt="You review."),
        }
        agents = {
            "coder-0": make_agent_config(agent_id="coder-0", role="coder"),
            "coder-1": make_agent_config(agent_id="coder-1", role="coder"),
            "reviewer-0": make_agent_config(agent_id="reviewer-0", role="reviewer"),
        }
        org = OrgConfig(roles=roles, agents=agents, settings=OrgSettings(org_name="multi"))
        assert len(org.roles) == 2
        assert len(org.agents) == 3
        assert org.agents["coder-1"].role == "coder"
        assert org.agents["reviewer-0"].role == "reviewer"


# =============================================================================
# OrgConfig.from_yaml
# =============================================================================


class TestOrgConfigYaml:
    @staticmethod
    def _write_yaml(data: dict, tmp_path) -> str:
        """Write dict to a YAML file inside tmp_path, return path string."""
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(data))
        return str(path)

    def test_load_valid_yaml(self, tmp_path):
        data = {
            "settings": {"org_name": "from-yaml"},
            "roles": {
                "coder": {
                    "name": "coder",
                    "role_prompt": "Code stuff",
                    "tools": ["worker"],
                    "model": "test",
                }
            },
            "agents": {"coder-0": {"agent_id": "coder-0", "role": "coder"}},
        }
        path = self._write_yaml(data, tmp_path)
        org = OrgConfig.from_yaml(path)
        assert org.settings.org_name == "from-yaml"
        assert "coder" in org.roles
        assert "coder-0" in org.agents

    def test_yaml_missing_required_key(self, tmp_path):
        data = {"settings": {"org_name": "incomplete"}, "roles": {}}
        path = self._write_yaml(data, tmp_path)
        with pytest.raises(ValueError, match="missing required key 'agents'"):
            OrgConfig.from_yaml(path)

    def test_yaml_with_optional_fields(self, tmp_path):
        data = {
            "settings": {
                "org_name": "full",
                "org_prompt": "Global instructions for {agent_id}",
                "org_permission_mode": "bypassPermissions",
                "org_timeout_seconds": 7200,
                "agent_timeout_seconds": 600,
            },
            "roles": {"r": {"name": "r", "role_prompt": "Role prompt", "tools": [], "model": "test"}},
            "agents": {"a": {"agent_id": "a", "role": "r"}},
        }
        path = self._write_yaml(data, tmp_path)
        org = OrgConfig.from_yaml(path)
        assert org.settings.org_prompt == "Global instructions for {agent_id}"
        assert org.settings.org_permission_mode == "bypassPermissions"
        assert org.settings.org_timeout_seconds == 7200
        assert org.settings.agent_timeout_seconds == 600

    def test_yaml_extra_key_in_role_raises(self, tmp_path):
        """YAML with an unknown key in a role dict should raise TypeError."""
        data = {
            "settings": {"org_name": "bad-role"},
            "roles": {
                "r": {
                    "name": "r",
                    "role_prompt": "Prompt",
                    "tools": [],
                    "model": "test",
                    "unknown_field": "oops",
                }
            },
            "agents": {"a": {"agent_id": "a", "role": "r"}},
        }
        path = self._write_yaml(data, tmp_path)
        with pytest.raises(TypeError):
            OrgConfig.from_yaml(path)

    def test_yaml_missing_role_field_raises(self, tmp_path):
        """YAML with a role missing role_prompt should raise TypeError."""
        data = {
            "settings": {"org_name": "missing-field"},
            "roles": {
                "r": {
                    "name": "r",
                    "tools": [],
                    # role_prompt is missing
                }
            },
            "agents": {"a": {"agent_id": "a", "role": "r"}},
        }
        path = self._write_yaml(data, tmp_path)
        with pytest.raises(TypeError):
            OrgConfig.from_yaml(path)

    def test_yaml_defaults_match_dataclass(self, tmp_path):
        """Load YAML with only required keys; verify defaults match dataclass."""
        data = {
            "settings": {"org_name": "defaults"},
            "roles": {"r": {"name": "r", "role_prompt": "Prompt", "tools": [], "model": "test"}},
            "agents": {"a": {"agent_id": "a", "role": "r"}},
        }
        path = self._write_yaml(data, tmp_path)
        org = OrgConfig.from_yaml(path)
        assert org.settings.org_permission_mode == "acceptEdits"
        assert org.settings.org_timeout_seconds == 3600.0
        assert org.settings.agent_timeout_seconds == 900.0

    def test_yaml_with_agent_overrides(self, tmp_path):
        """YAML with agent-level overrides loads correctly."""
        data = {
            "settings": {"org_name": "overrides"},
            "roles": {"r": {"name": "r", "role_prompt": "Prompt", "tools": [], "model": "test"}},
            "agents": {
                "a": {
                    "agent_id": "a",
                    "role": "r",
                    "model_override": "claude-3-haiku",
                    "max_turns_override": 50,
                }
            },
        }
        path = self._write_yaml(data, tmp_path)
        org = OrgConfig.from_yaml(path)
        agent = org.agents["a"]
        assert agent.model_override == "claude-3-haiku"
        assert agent.max_turns_override == 50

    def test_from_yaml_nonexistent_file(self):
        """from_yaml with a nonexistent path should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            OrgConfig.from_yaml("nonexistent.yaml")

    def test_from_yaml_empty_content(self, tmp_path):
        """from_yaml with empty YAML content should raise an error.

        yaml.safe_load('') returns None, so the required-key check
        fails with TypeError (cannot check membership on NoneType).
        """
        path = tmp_path / "empty.yaml"
        path.write_text("---")
        with pytest.raises(TypeError):
            OrgConfig.from_yaml(str(path))

    def test_yaml_semantic_validation_unknown_role(self, tmp_path):
        """YAML that parses correctly but has an agent referencing an unknown role
        should raise ValueError from OrgConfig validation."""
        data = {
            "settings": {"org_name": "bad-ref"},
            "roles": {"coder": {"name": "coder", "role_prompt": "Code stuff", "tools": [], "model": "test"}},
            "agents": {"a": {"agent_id": "a", "role": "nonexistent_role"}},
        }
        path = self._write_yaml(data, tmp_path)
        with pytest.raises(ValueError, match="unknown role"):
            OrgConfig.from_yaml(path)

    def test_yaml_non_dict_roles(self, tmp_path):
        """YAML where roles is a list instead of dict should raise AttributeError
        because list has no .items() method."""
        data = {
            "settings": {"org_name": "bad-roles"},
            "roles": [{"name": "r", "role_prompt": "Prompt", "tools": [], "model": "test"}],
            "agents": {"a": {"agent_id": "a", "role": "r"}},
        }
        path = self._write_yaml(data, tmp_path)
        with pytest.raises(AttributeError):
            OrgConfig.from_yaml(path)

    def test_yaml_network_unknown_agent(self, tmp_path):
        """YAML with network referencing an agent not in agents section raises."""
        data = {
            "settings": {"org_name": "bad-net"},
            "roles": {"r": {"name": "r", "role_prompt": "Prompt", "tools": [], "model": "test"}},
            "agents": {"a": {"agent_id": "a", "role": "r"}},
            "network": {"groups": {"t": ["a", "ghost"]}},
        }
        path = self._write_yaml(data, tmp_path)
        with pytest.raises(ValueError, match="unknown"):
            OrgConfig.from_yaml(path)

    def test_yaml_network_missing_agent(self, tmp_path):
        """YAML with agent not mentioned in network raises."""
        data = {
            "settings": {"org_name": "bad-net"},
            "roles": {"r": {"name": "r", "role_prompt": "Prompt", "tools": [], "model": "test"}},
            "agents": {
                "a": {"agent_id": "a", "role": "r"},
                "b": {"agent_id": "b", "role": "r"},
            },
            "network": {"groups": {"t": ["a"]}},
        }
        path = self._write_yaml(data, tmp_path)
        with pytest.raises(ValueError, match="not mentioned"):
            OrgConfig.from_yaml(path)

    def test_yaml_with_network(self, tmp_path):
        data = {
            "settings": {"org_name": "net-test"},
            "roles": {"r": {"name": "r", "role_prompt": "Prompt", "tools": [], "model": "test"}},
            "agents": {
                "a": {"agent_id": "a", "role": "r"},
                "b": {"agent_id": "b", "role": "r"},
                "c": {"agent_id": "c", "role": "r"},
            },
            "network": {
                "groups": {"t1": ["a", "b"]},
                "connections": {"b": ["c"]},
            },
        }
        path = self._write_yaml(data, tmp_path)
        org = OrgConfig.from_yaml(path)
        assert org.network is not None
        assert org.network.groups == {"t1": ["a", "b"]}
        assert org.network.connections == {"b": ["c"]}
        assert org.network.all_agents == {"a", "b", "c"}


# =============================================================================
# NetworkConfig
# =============================================================================


class TestNetworkConfig:
    """Tests for NetworkConfig as a pure DTO — parsing and validation only.
    Runtime query/mutation tests are in test_network.py."""

    def test_groups_only(self):
        data = {"groups": {"backend": ["a", "b"], "frontend": ["c", "d"]}}
        net = NetworkConfig(**data)
        assert net.groups == {"backend": ["a", "b"], "frontend": ["c", "d"]}
        assert net.connections == {}

    def test_connections_only(self):
        data = {"connections": {"a": ["b", "c"]}}
        net = NetworkConfig(**data)
        # Raw connections stored as-is (not symmetrized — that's Network's job)
        assert net.connections == {"a": ["b", "c"]}
        assert net.groups == {}

    def test_groups_and_connections(self):
        data = {
            "groups": {"backend": ["a", "b"]},
            "connections": {"a": ["c"]},
        }
        net = NetworkConfig(**data)
        assert net.groups == {"backend": ["a", "b"]}
        assert net.connections == {"a": ["c"]}

    def test_all_agents_property(self):
        data = {"groups": {"t": ["a", "b"]}, "connections": {"c": ["a"]}}
        net = NetworkConfig(**data)
        assert net.all_agents == {"a", "b", "c"}

    def test_all_agents_connections_both_sides(self):
        """all_agents includes both sides of a connection."""
        data = {"connections": {"a": ["b", "c"]}}
        net = NetworkConfig(**data)
        assert net.all_agents == {"a", "b", "c"}

    def test_empty_group_raises(self):
        with pytest.raises(ValueError, match="empty"):
            NetworkConfig(**{"groups": {"t": []}})

    def test_empty_sections_valid(self):
        net = NetworkConfig(**{"groups": {}, "connections": {}})
        assert net.groups == {}
        assert net.connections == {}

    def test_single_member_group(self):
        """Single-member group is valid."""
        net = NetworkConfig(**{"groups": {"solo": ["a"]}})
        assert net.groups == {"solo": ["a"]}
        assert net.connections == {}

    def test_self_connection_raises(self):
        with pytest.raises(ValueError, match="cannot include self"):
            NetworkConfig(**{"connections": {"a": ["a", "b"]}})

    def test_empty_connections_list_raises(self):
        with pytest.raises(ValueError, match="empty connections"):
            NetworkConfig(**{"connections": {"a": []}})

    def test_duplicate_members_in_group_raises(self):
        with pytest.raises(ValueError, match="duplicate members"):
            NetworkConfig(**{"groups": {"t": ["a", "a", "b"]}})

    def test_duplicate_connections_raises(self):
        with pytest.raises(ValueError, match="duplicates"):
            NetworkConfig(**{"connections": {"a": ["b", "b"]}})


# =============================================================================
# OrgConfig — network topology
# =============================================================================


class TestOrgConfigNetwork:
    def test_no_network_backward_compat(self):
        org = make_org_config()
        assert org.network is None

    def test_network_validation_unknown_agent(self):
        roles = {"w": make_role(name="w")}
        agents = {"a": make_agent_config(agent_id="a", role="w")}
        net = NetworkConfig(**{"groups": {"t": ["a", "unknown"]}})
        with pytest.raises(ValueError, match="unknown"):
            OrgConfig(roles=roles, agents=agents, network=net, settings=OrgSettings(org_name="x"))

    def test_network_valid(self):
        roles = {"w": make_role(name="w")}
        agents = {
            "a": make_agent_config(agent_id="a", role="w"),
            "b": make_agent_config(agent_id="b", role="w"),
        }
        net = NetworkConfig(**{"connections": {"a": ["b"]}})
        org = OrgConfig(roles=roles, agents=agents, network=net, settings=OrgSettings(org_name="x"))
        assert org.network is not None

    def test_network_agent_missing_from_network(self):
        """Agent exists in agents but not in network → validation error."""
        roles = {"w": make_role(name="w")}
        agents = {
            "a": make_agent_config(agent_id="a", role="w"),
            "b": make_agent_config(agent_id="b", role="w"),
            "c": make_agent_config(agent_id="c", role="w"),
        }
        net = NetworkConfig(**{"connections": {"a": ["b"]}})
        with pytest.raises(ValueError, match="not mentioned in network"):
            OrgConfig(roles=roles, agents=agents, network=net, settings=OrgSettings(org_name="x"))


# =============================================================================
# WireNotifications
# =============================================================================


class TestWireNotifications:
    def test_default(self):
        org = make_org_config()
        assert org.settings.wire_notifications == WireNotifications.ALL

    def test_tool_only(self):
        org = make_org_config()
        org.settings.wire_notifications = WireNotifications.TOOL
        assert org.settings.wire_notifications == WireNotifications.TOOL

    def test_event_only(self):
        org = make_org_config()
        org.settings.wire_notifications = WireNotifications.EVENT
        assert org.settings.wire_notifications == WireNotifications.EVENT

    def test_none(self):
        org = make_org_config()
        org.settings.wire_notifications = WireNotifications.NONE
        assert org.settings.wire_notifications == WireNotifications.NONE


# =============================================================================
# from_dict / to_dict round-trip
# =============================================================================


class TestFromDictRoundTrip:
    """Verify that from_dict(to_dict(config)) produces an equivalent config."""

    def _roundtrip(self, original: OrgConfig) -> OrgConfig:
        return OrgConfig.from_dict(original.to_dict())

    def test_minimal(self):
        original = make_org_config()
        result = self._roundtrip(original)
        assert result.settings.org_name == original.settings.org_name
        assert result.roles.keys() == original.roles.keys()
        assert result.agents.keys() == original.agents.keys()
        assert result.settings.wire_notifications == original.settings.wire_notifications

    def test_full_config(self):
        original = OrgConfig(
            roles={
                "coder": RoleConfig(name="coder", role_prompt="Code", tools=["worker"], model="test", max_turns=50),
                "reviewer": RoleConfig(name="reviewer", role_prompt="Review", tools=[], model="test", max_turns=20),
            },
            agents={
                "c0": AgentConfig(agent_id="c0", role="coder"),
                "c1": AgentConfig(agent_id="c1", role="coder", model_override="fast", max_turns_override=10),
                "r0": AgentConfig(agent_id="r0", role="reviewer"),
            },
            network=NetworkConfig(groups={"team": ["c0", "c1", "r0"]}),
            settings=OrgSettings(
                org_name="full",
                wire_notifications=WireNotifications.EVENT,
                org_prompt="Be helpful",
                org_timeout_seconds=1800.0,
                agent_timeout_seconds=300.0,
                mcp_modules={"voting": "experiments.voting.server"},
                agent_settings_dir="/abs/path/to/settings",
            ),
        )
        result = self._roundtrip(original)
        assert result.settings.org_name == original.settings.org_name
        assert result.settings.org_prompt == original.settings.org_prompt
        assert result.settings.org_timeout_seconds == original.settings.org_timeout_seconds
        assert result.settings.agent_timeout_seconds == original.settings.agent_timeout_seconds
        assert result.settings.wire_notifications == original.settings.wire_notifications
        assert result.settings.mcp_modules == original.settings.mcp_modules
        assert result.settings.agent_settings_dir == original.settings.agent_settings_dir
        assert result.network.groups == original.network.groups
        assert set(result.roles.keys()) == set(original.roles.keys())
        assert set(result.agents.keys()) == set(original.agents.keys())
        assert result.agents["c1"].model_override == "fast"
        assert result.agents["c1"].max_turns_override == 10
        assert result.agents["c0"].model_override is None

    def test_with_session_config_override(self):
        original = OrgConfig(
            roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
            agents={"a": AgentConfig(agent_id="a", role="w", session_config_override="custom/path")},
            settings=OrgSettings(org_name="session"),
        )
        result = self._roundtrip(original)
        assert result.agents["a"].session_config_override == "custom/path"

    def test_with_initial_tasks(self):
        original = OrgConfig(
            roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
            agents={"a": AgentConfig(agent_id="a", role="w")},
            settings=OrgSettings(org_name="tasks"),
            initial_tasks=[(Task(id="t1", title="Do it", description="desc"), "a", "user")],
        )
        result = self._roundtrip(original)
        assert len(result.initial_tasks) == 1
        task, assigned_to, assigned_by = result.initial_tasks[0]
        assert task.id == "t1"
        assert task.title == "Do it"
        assert assigned_to == "a"
        assert assigned_by == "user"

    def test_with_initial_messages(self):
        original = OrgConfig(
            roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
            agents={"a": AgentConfig(agent_id="a", role="w"), "b": AgentConfig(agent_id="b", role="w")},
            settings=OrgSettings(org_name="msgs"),
            initial_messages=[{"participants": ["a", "b"], "body": "hello", "sender": "user"}],
        )
        result = self._roundtrip(original)
        assert len(result.initial_messages) == 1
        assert result.initial_messages[0]["body"] == "hello"

    def test_no_network(self):
        original = make_org_config()
        result = self._roundtrip(original)
        assert result.network is None

    def test_sync_mode(self):
        original = OrgConfig(
            roles={"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="m", max_turns=10)},
            agents={"a": AgentConfig(agent_id="a", role="w")},
            settings=OrgSettings(org_name="sync", sync=True, sync_max_rounds=5, sync_round_timeout_seconds=600.0),
        )
        result = self._roundtrip(original)
        assert result.settings.sync is True
        assert result.settings.sync_max_rounds == 5
        assert result.settings.sync_round_timeout_seconds == 600.0


# =============================================================================
# OrgSettings / OrgConfig field parity
# =============================================================================


def test_org_config_settings_field_is_org_settings():
    """OrgConfig.settings field should be typed as OrgSettings."""
    settings_field = next(f for f in dataclasses.fields(OrgConfig) if f.name == "settings")
    assert settings_field.type == OrgSettings

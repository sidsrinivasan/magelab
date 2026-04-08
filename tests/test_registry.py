"""Tests for magelab.registry — Registry, state management, queue ops."""

import asyncio
import logging
import time

import pytest

from magelab.registry_config import AgentConfig, NetworkConfig, RoleConfig
from magelab.events import TaskAssignedEvent
from magelab.state.database import Database
from magelab.state.registry import Registry
from magelab.state.registry_schemas import AgentSnapshot, AgentState

_test_logger = logging.getLogger("test")


def _make_registry(
    roles: dict[str, RoleConfig] | None = None,
    agents: dict[str, AgentConfig] | None = None,
) -> Registry:
    """Create a minimal Registry for testing."""
    if roles is None:
        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
            ),
            "reviewer": RoleConfig(
                name="reviewer", role_prompt="Review", tools=["claude_reviewer"], model="test", max_turns=5
            ),
        }
    if agents is None:
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "reviewer-0": AgentConfig(agent_id="reviewer-0", role="reviewer"),
        }
    registry = Registry(framework_logger=_test_logger)
    registry.register_config(roles, agents)
    return registry


# =============================================================================
# Construction
# =============================================================================


class TestRegistryConstruction:
    def test_agents_created_from_config(self):
        reg = _make_registry()
        ids = reg.list_agent_ids()
        assert set(ids) == {"coder-0", "reviewer-0"}

    def test_agent_inherits_role_defaults(self):
        reg = _make_registry()
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.model == "test"
        assert snap.role == "coder"
        assert "tasks_submit_for_review" in snap.tools  # expanded from worker bundle

    def test_agent_overrides(self):
        roles = {"r": RoleConfig(name="r", role_prompt="Base prompt", tools=[], model="default", max_turns=10)}
        agents = {
            "a": AgentConfig(
                agent_id="a",
                role="r",
                model_override="override-model",
                max_turns_override=99,
                role_prompt_override="Override prompt",
            )
        }
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, agents)
        snap = reg.get_agent_snapshot("a")
        assert snap.model == "override-model"
        assert snap.role_prompt == "Override prompt"
        assert reg.get_agent_max_turns("a") == 99

    def test_duplicate_agent_raises(self):
        reg = _make_registry()
        with pytest.raises(ValueError, match="already exists"):
            reg.create_agent("coder-0", "coder", "m", "p", [], 10)

    def test_unknown_role_raises(self):
        reg = _make_registry()
        with pytest.raises(ValueError, match="Unknown role"):
            reg.create_agent("new-agent", "nonexistent_role", "m", "p", [], 10)

    def test_create_agent_at_runtime(self):
        reg = _make_registry()
        reg.create_agent("coder-1", "coder", "test-model", "Code stuff", ["claude_bash", "claude_read"], 20)
        assert "coder-1" in reg.list_agent_ids()
        snap = reg.get_agent_snapshot("coder-1")
        assert snap.role == "coder"
        assert snap.state == AgentState.IDLE
        assert snap.tools == ("claude_bash", "claude_read")

    def test_construction_with_no_agents(self):
        """Creating a registry with roles but no agents should work; list operations return empty."""
        roles = {
            "coder": RoleConfig(name="coder", role_prompt="Code", tools=["worker"], model="test", max_turns=10),
        }
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, agent_configs={})
        assert reg.list_agent_ids() == []
        assert reg.list_agent_ids(active_only=False) == []
        assert reg.list_agent_snapshots() == []
        # The role should still be accessible
        assert reg.get_role("coder") is not None

    def test_construction_with_unknown_role_in_agent_raises(self):
        """If an agent config references a role not in role_configs, construction raises KeyError."""
        roles = {
            "coder": RoleConfig(name="coder", role_prompt="Code", tools=[], model="test", max_turns=10),
        }
        agents = {
            "agent-0": AgentConfig(agent_id="agent-0", role="nonexistent_role"),
        }
        with pytest.raises(KeyError, match="nonexistent_role"):
            reg = Registry(framework_logger=_test_logger)
            reg.register_config(roles, agents)

    def test_tools_override_empty_list(self):
        """An empty tools_override=[] should be respected, not fall back to role defaults."""
        roles = {"r": RoleConfig(name="r", role_prompt="prompt", tools=["worker"], model="m", max_turns=10)}
        agents = {"a": AgentConfig(agent_id="a", role="r", tools_override=[])}
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, agents)
        snap = reg.get_agent_snapshot("a")
        # Empty list is a valid override (is not None), so role defaults should NOT apply
        assert snap.tools == ()


# =============================================================================
# State management
# =============================================================================


class TestStateManagement:
    def test_initial_state_is_idle(self):
        reg = _make_registry()
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.state == AgentState.IDLE

    def test_mark_working(self):
        reg = _make_registry()
        reg.mark_working("coder-0", "task-1")
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.state == AgentState.WORKING
        assert snap.current_task_id == "task-1"
        assert snap.last_active_at is not None

    def test_mark_reviewing(self):
        reg = _make_registry()
        reg.mark_reviewing("coder-0", "task-1")
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.state == AgentState.REVIEWING
        assert snap.current_task_id == "task-1"

    def test_mark_idle_clears_task(self):
        reg = _make_registry()
        reg.mark_working("coder-0", "task-1")
        reg.mark_idle("coder-0")
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.state == AgentState.IDLE
        assert snap.current_task_id is None

    def test_mark_terminated(self):
        reg = _make_registry()
        reg.mark_terminated("coder-0")
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.state == AgentState.TERMINATED

    def test_initial_timestamps(self):
        """When an agent is first created, created_at is set and last_active_at is None."""
        reg = _make_registry()
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.created_at is not None
        # last_active_at is None until a state transition occurs
        assert snap.last_active_at is None

    def test_last_active_at_updates_on_all_state_transitions(self):
        """last_active_at should update on every state transition: IDLE->WORKING->REVIEWING->IDLE->TERMINATED."""
        reg = _make_registry()
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.last_active_at is None  # No transitions yet

        reg.mark_working("coder-0", "task-1")
        snap = reg.get_agent_snapshot("coder-0")
        ts_working = snap.last_active_at
        assert ts_working is not None

        time.sleep(0.01)  # Ensure time advances

        reg.mark_reviewing("coder-0", "task-1")
        snap = reg.get_agent_snapshot("coder-0")
        ts_reviewing = snap.last_active_at
        assert ts_reviewing is not None
        assert ts_reviewing >= ts_working

        time.sleep(0.01)

        reg.mark_idle("coder-0")
        snap = reg.get_agent_snapshot("coder-0")
        ts_idle = snap.last_active_at
        assert ts_idle is not None
        assert ts_idle >= ts_reviewing

        time.sleep(0.01)

        reg.mark_terminated("coder-0")
        snap = reg.get_agent_snapshot("coder-0")
        ts_terminated = snap.last_active_at
        assert ts_terminated is not None
        assert ts_terminated >= ts_idle

    def test_mark_unknown_agent_raises(self):
        reg = _make_registry()
        with pytest.raises(ValueError, match="not found"):
            reg.mark_working("nonexistent", "task-1")
        with pytest.raises(ValueError, match="not found"):
            reg.mark_idle("nonexistent")
        with pytest.raises(ValueError, match="not found"):
            reg.mark_terminated("nonexistent")
        with pytest.raises(ValueError, match="not found"):
            reg.mark_reviewing("nonexistent", "task-1")

    def test_terminated_agent_clears_task_id(self):
        """mark_terminated clears current_task_id, consistent with mark_idle."""
        reg = _make_registry()
        reg.mark_working("coder-0", "task-1")
        reg.mark_terminated("coder-0")
        snap = reg.get_agent_snapshot("coder-0")
        assert snap.state == AgentState.TERMINATED
        assert snap.current_task_id is None


# =============================================================================
# Queue operations
# =============================================================================


class TestQueueOperations:
    def test_enqueue_and_dequeue(self):
        reg = _make_registry()
        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        reg.enqueue("coder-0", event)

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(reg.dequeue("coder-0", timeout=1.0))
        loop.close()
        assert result is not None
        assert result.task_id == "t1"

    def test_dequeue_timeout_returns_none(self):
        reg = _make_registry()
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(reg.dequeue("coder-0", timeout=0.01))
        loop.close()
        assert result is None

    def test_enqueue_terminated_agent_silently_drops(self):
        """Enqueue to a terminated agent is silently dropped (queue does not grow)."""
        reg = _make_registry()
        event1 = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        # Enqueue while IDLE — queue should grow to 1
        reg.enqueue("coder-0", event1)
        # Access internal queue for verification purposes
        assert reg._agents["coder-0"].queue.qsize() == 1

        # Terminate the agent, then enqueue another event
        reg.mark_terminated("coder-0")
        event2 = TaskAssignedEvent(task_id="t2", target_id="coder-0", source_id="pm")
        reg.enqueue("coder-0", event2)  # Should not raise

        # Queue size should still be 1 — the second enqueue was dropped
        assert reg._agents["coder-0"].queue.qsize() == 1

    def test_enqueue_unknown_agent_silently_drops(self):
        reg = _make_registry()
        event = TaskAssignedEvent(task_id="t1", target_id="x", source_id="pm")
        reg.enqueue("nonexistent", event)  # Should not raise

    @pytest.mark.asyncio
    async def test_dequeue_unknown_agent(self):
        reg = _make_registry()
        result = await reg.dequeue("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_terminated_with_queued_events(self):
        """Dequeue returns None for a terminated agent even if events are queued."""
        reg = _make_registry()
        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        reg.enqueue("coder-0", event)
        # Verify event is in the queue
        assert reg._agents["coder-0"].queue.qsize() == 1
        # Terminate the agent
        reg.mark_terminated("coder-0")
        # Dequeue should short-circuit to None (line ~209 in registry.py)
        result = await reg.dequeue("coder-0", timeout=1.0)
        assert result is None
        # The event is still in the queue — it was not consumed, just ignored
        assert reg._agents["coder-0"].queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_queue_fifo_ordering(self):
        reg = _make_registry()
        events = [TaskAssignedEvent(task_id=f"t{i}", target_id="coder-0", source_id="pm") for i in range(3)]
        for e in events:
            reg.enqueue("coder-0", e)

        results = []
        for _ in range(3):
            result = await reg.dequeue("coder-0", timeout=1.0)
            results.append(result)

        assert [r.task_id for r in results] == ["t0", "t1", "t2"]


# =============================================================================
# Queries
# =============================================================================


class TestQueries:
    def test_list_agent_ids_active_only(self):
        reg = _make_registry()
        reg.mark_terminated("reviewer-0")
        ids = reg.list_agent_ids(active_only=True)
        assert "coder-0" in ids
        assert "reviewer-0" not in ids

    def test_list_agent_ids_all(self):
        reg = _make_registry()
        reg.mark_terminated("reviewer-0")
        ids = reg.list_agent_ids(active_only=False)
        assert set(ids) == {"coder-0", "reviewer-0"}

    def test_get_agent_snapshot_not_found(self):
        reg = _make_registry()
        assert reg.get_agent_snapshot("nonexistent") is None

    def test_list_agent_snapshots_excludes_terminated(self):
        reg = _make_registry()
        reg.mark_terminated("reviewer-0")
        snaps = reg.list_agent_snapshots()
        assert len(snaps) == 1
        assert snaps[0].agent_id == "coder-0"

    def test_list_snapshots_includes_working_agents(self):
        reg = _make_registry()
        reg.mark_working("coder-0", "task-1")
        snaps = reg.list_agent_snapshots()
        agent_ids = [s.agent_id for s in snaps]
        assert "coder-0" in agent_ids
        working_snap = [s for s in snaps if s.agent_id == "coder-0"][0]
        assert working_snap.state == AgentState.WORKING

    def test_get_agent_max_turns(self):
        reg = _make_registry()
        assert reg.get_agent_max_turns("coder-0") == 10

    def test_get_agent_max_turns_unknown_raises(self):
        reg = _make_registry()
        with pytest.raises(ValueError, match="Unknown agent"):
            reg.get_agent_max_turns("nonexistent")

    def test_get_agent_snapshot_on_terminated_agent(self):
        """get_agent_snapshot still returns data for a terminated agent (not None)."""
        reg = _make_registry()
        reg.mark_working("coder-0", "task-1")
        reg.mark_terminated("coder-0")
        snap = reg.get_agent_snapshot("coder-0")
        assert snap is not None
        assert snap.state == AgentState.TERMINATED
        assert snap.agent_id == "coder-0"
        # Terminated agents still carry their data (role, model, etc.)
        assert snap.role == "coder"
        assert snap.model == "test"

    def test_list_agent_snapshots_empty_when_all_terminated(self):
        """After terminating all agents, list_agent_snapshots returns an empty list."""
        reg = _make_registry()
        reg.mark_terminated("coder-0")
        reg.mark_terminated("reviewer-0")
        snaps = reg.list_agent_snapshots()
        assert snaps == []

    def test_list_agent_ids_all_terminated(self):
        """When all agents are terminated, active_only=True returns an empty list."""
        reg = _make_registry()
        reg.mark_terminated("coder-0")
        reg.mark_terminated("reviewer-0")
        ids = reg.list_agent_ids(active_only=True)
        assert ids == []


# =============================================================================
# Role management
# =============================================================================


class TestRoleManagement:
    def test_get_role_not_found(self):
        reg = _make_registry()
        assert reg.get_role("nonexistent") is None


# =============================================================================
# AgentInstance / AgentSnapshot
# =============================================================================


class TestAgentSnapshot:
    def test_snapshot_is_frozen(self):
        reg = _make_registry()
        snap = reg.get_agent_snapshot("coder-0")
        assert isinstance(snap, AgentSnapshot)
        # frozen dataclass
        with pytest.raises(AttributeError):
            snap.state = AgentState.WORKING


# =============================================================================
# Network peer lookups
# =============================================================================


class TestNetworkConnectivity:
    def _make_registry_with_network(self):
        """backend group [pm, coder-0, rev-0], frontend group [coder-1, rev-1], pm connected to coder-1."""
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {
            "pm": AgentConfig(agent_id="pm", role="w"),
            "coder-0": AgentConfig(agent_id="coder-0", role="w"),
            "coder-1": AgentConfig(agent_id="coder-1", role="w"),
            "rev-0": AgentConfig(agent_id="rev-0", role="w"),
            "rev-1": AgentConfig(agent_id="rev-1", role="w"),
        }
        network = NetworkConfig(
            **{
                "groups": {"backend": ["pm", "coder-0", "rev-0"], "frontend": ["coder-1", "rev-1"]},
                "connections": {"pm": ["coder-1"]},
            }
        )
        registry = Registry(framework_logger=_test_logger)
        registry.register_config(roles, agents, network)
        return registry

    def test_get_connected_ids_group_member(self):
        reg = self._make_registry_with_network()
        connected = set(reg.get_connected_ids("coder-0"))
        assert connected == {"pm", "rev-0"}

    def test_get_connected_ids_cross_group_connection(self):
        reg = self._make_registry_with_network()
        connected = set(reg.get_connected_ids("pm"))
        assert connected == {"coder-0", "rev-0", "coder-1"}

    def test_is_connected_true(self):
        reg = self._make_registry_with_network()
        assert reg.is_connected("pm", "coder-0") is True

    def test_is_connected_false(self):
        reg = self._make_registry_with_network()
        assert reg.is_connected("coder-0", "coder-1") is False

    def test_is_connected_cross_group(self):
        reg = self._make_registry_with_network()
        assert reg.is_connected("pm", "coder-1") is True
        assert reg.is_connected("coder-1", "pm") is True

    def test_no_network_fully_connected(self):
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {"a": AgentConfig(agent_id="a", role="w"), "b": AgentConfig(agent_id="b", role="w")}
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, agents)
        assert set(reg.get_connected_ids("a")) == {"b"}
        assert reg.is_connected("a", "b") is True

    def test_get_connected_ids_excludes_terminated(self):
        reg = self._make_registry_with_network()
        reg.mark_terminated("coder-0")
        assert "coder-0" not in set(reg.get_connected_ids("pm"))

    def test_is_connected_unknown_raises(self):
        reg = self._make_registry_with_network()
        with pytest.raises(ValueError):
            reg.is_connected("pm", "nonexistent")

    def test_get_connected_ids_unknown_raises(self):
        reg = self._make_registry_with_network()
        with pytest.raises(ValueError):
            reg.get_connected_ids("nonexistent")


# =============================================================================
# Runtime agent creation with network membership
# =============================================================================


class TestCreateAgentWithNetwork:
    def _make_registry_with_network(self):
        """Simple network: group 'team' has [a, b]."""
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {
            "a": AgentConfig(agent_id="a", role="w"),
            "b": AgentConfig(agent_id="b", role="w"),
        }
        network = NetworkConfig(**{"groups": {"team": ["a", "b"]}})
        registry = Registry(framework_logger=_test_logger)
        registry.register_config(roles, agents, network)
        return registry

    def test_create_agent_with_groups(self):
        reg = self._make_registry_with_network()
        reg.create_agent("c", "w", "test", "Work", [], 10, groups=["team"])
        assert "c" in reg.list_agent_ids()
        assert reg.is_connected("c", "a")
        assert reg.is_connected("c", "b")

    def test_create_agent_with_connections(self):
        reg = self._make_registry_with_network()
        reg.create_agent("c", "w", "test", "Work", [], 10, connections=["a"])
        assert "c" in reg.list_agent_ids()
        assert reg.is_connected("c", "a")
        assert not reg.is_connected("c", "b")

    def test_create_agent_with_groups_and_connections(self):
        """Both groups and connections can be specified together."""
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {
            "a": AgentConfig(agent_id="a", role="w"),
            "b": AgentConfig(agent_id="b", role="w"),
            "d": AgentConfig(agent_id="d", role="w"),
        }
        network = NetworkConfig(**{"groups": {"team": ["a", "b"]}, "connections": {"a": ["d"]}})
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, agents, network)
        reg.create_agent("c", "w", "test", "Work", [], 10, groups=["team"], connections=["d"])
        assert reg.is_connected("c", "a")  # via group
        assert reg.is_connected("c", "b")  # via group
        assert reg.is_connected("c", "d")  # via connection

    def test_create_agent_network_requires_membership(self):
        """When network exists, at least one of groups or connections must be non-empty."""
        reg = self._make_registry_with_network()
        with pytest.raises(ValueError, match="network membership"):
            reg.create_agent("c", "w", "test", "Work", [], 10)

    def test_create_agent_network_empty_groups_and_connections(self):
        """Explicitly passing empty groups=[] and connections=[] also raises."""
        reg = self._make_registry_with_network()
        with pytest.raises(ValueError, match="network membership"):
            reg.create_agent("c", "w", "test", "Work", [], 10, groups=[], connections=[])

    def test_create_agent_no_network_ignores_params(self):
        """Without network, groups/connections params are ignored (no error)."""
        reg = _make_registry()
        reg.create_agent("new-agent", "coder", "test", "Code", [], 10, groups=["team"], connections=["coder-0"])
        assert "new-agent" in reg.list_agent_ids()

    def test_create_agent_new_group(self):
        """Creating an agent with a new group name should work."""
        reg = self._make_registry_with_network()
        reg.create_agent("c", "w", "test", "Work", [], 10, groups=["new-team"])
        assert "c" in reg.list_agent_ids()
        # Not connected to existing agents (different group)
        assert not reg.is_connected("c", "a")

    def test_init_agent_missing_from_network_raises(self):
        """Agent in config but not in network → ValueError at construction."""
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {
            "a": AgentConfig(agent_id="a", role="w"),
            "b": AgentConfig(agent_id="b", role="w"),
        }
        network = NetworkConfig(**{"groups": {"team": ["a"]}})  # b missing from network
        with pytest.raises(ValueError, match="not found in network"):
            reg = Registry(framework_logger=_test_logger)
            reg.register_config(roles, agents, network)

    def test_init_network_references_unknown_agent_raises(self):
        """Agent in network but not in config → ValueError at construction."""
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {"a": AgentConfig(agent_id="a", role="w")}
        network = NetworkConfig(**{"groups": {"team": ["a", "ghost"]}})  # ghost not in agents
        with pytest.raises(ValueError, match="unknown agents"):
            reg = Registry(framework_logger=_test_logger)
            reg.register_config(roles, agents, network)

    def test_incremental_building(self):
        """Build registry one agent at a time with an empty network."""
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, {}, NetworkConfig())

        reg.create_agent("a", "w", "test", "Work", [], 10, groups=["team"])
        reg.create_agent("b", "w", "test", "Work", [], 10, groups=["team"])
        reg.create_agent("c", "w", "test", "Work", [], 10, connections=["a"])

        assert reg.is_connected("a", "b")  # same group
        assert reg.is_connected("c", "a")  # explicit connection
        assert not reg.is_connected("c", "b")  # no link

    def test_incremental_mixed_groups_and_connections(self):
        """Incrementally add agent with both groups and connections."""
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, {}, NetworkConfig())

        reg.create_agent("a", "w", "test", "Work", [], 10, groups=["team"])
        reg.create_agent("b", "w", "test", "Work", [], 10, groups=["team"])
        reg.create_agent("c", "w", "test", "Work", [], 10, groups=["other"], connections=["a"])

        assert reg.is_connected("a", "b")  # same group
        assert reg.is_connected("c", "a")  # explicit connection
        assert not reg.is_connected("c", "b")  # different group, no connection

    def test_create_agent_nonexistent_connection_target_raises(self):
        """Connecting to an agent that doesn't exist raises ValueError."""
        reg = self._make_registry_with_network()
        with pytest.raises(ValueError, match="does not exist"):
            reg.create_agent("c", "w", "test", "Work", [], 10, connections=["nonexistent"])


# =============================================================================
# compute_connection_tools
# =============================================================================


class TestComputeConnectionTools:
    def test_with_network(self):
        """Each agent sees only connected agents' tools."""
        roles = {
            "mgr": RoleConfig(name="mgr", role_prompt="Manage", tools=["management"], model="test", max_turns=10),
            "dev": RoleConfig(
                name="dev", role_prompt="Code", tools=["worker", "claude_basic"], model="test", max_turns=10
            ),
        }
        agents = {
            "pm": AgentConfig(agent_id="pm", role="mgr"),
            "dev-0": AgentConfig(agent_id="dev-0", role="dev"),
            "dev-1": AgentConfig(agent_id="dev-1", role="dev"),
        }
        network = NetworkConfig(**{"groups": {"team-a": ["pm", "dev-0"]}, "connections": {"dev-1": ["pm"]}})
        reg = Registry(framework_logger=_test_logger)
        reg.register_config(roles, agents, network)
        ct = reg.compute_connection_tools()

        # pm is connected to dev-0 (group) and dev-1 (connection)
        dev0_snap = reg.get_agent_snapshot("dev-0")
        dev1_snap = reg.get_agent_snapshot("dev-1")
        assert ct["pm"] == set(dev0_snap.tools) | set(dev1_snap.tools)

        # dev-0 is only connected to pm (group)
        pm_tools = set(reg.get_agent_snapshot("pm").tools)
        assert ct["dev-0"] == pm_tools

        # dev-1 is only connected to pm (connection)
        assert ct["dev-1"] == pm_tools

    def test_no_network_fully_connected(self):
        """Without network, each agent sees all other agents' tools."""
        reg = _make_registry()
        ct = reg.compute_connection_tools()
        coder_tools = set(reg.get_agent_snapshot("coder-0").tools)
        reviewer_tools = set(reg.get_agent_snapshot("reviewer-0").tools)
        assert ct["coder-0"] == reviewer_tools
        assert ct["reviewer-0"] == coder_tools

    def test_excludes_terminated(self):
        """Terminated agents are excluded from connection tools."""
        reg = _make_registry()
        reg.mark_terminated("reviewer-0")
        ct = reg.compute_connection_tools()
        # coder-0's connections no longer include terminated reviewer-0
        assert ct["coder-0"] == set()

    def test_get_connected_ids_terminated_caller(self):
        """get_connected_ids works for a terminated agent (returns connections)."""
        reg = _make_registry()
        reg.mark_terminated("coder-0")
        # terminated agent can still query connections (though it shouldn't normally)
        connected = reg.get_connected_ids("coder-0", active_only=False)
        assert "reviewer-0" in connected


# =============================================================================
# Listeners
# =============================================================================


class TestListeners:
    def test_state_listener_fires_on_state_change(self):
        reg = _make_registry()
        calls: list[tuple] = []
        reg.add_state_listener(lambda aid, state, tid: calls.append((aid, state, tid)))

        reg.mark_working("coder-0", "t1")
        assert len(calls) == 1
        assert calls[0] == ("coder-0", AgentState.WORKING, "t1")

        reg.mark_idle("coder-0")
        assert len(calls) == 2
        assert calls[1] == ("coder-0", AgentState.IDLE, None)

    def test_multiple_state_listeners(self):
        reg = _make_registry()
        calls_a: list = []
        calls_b: list = []
        reg.add_state_listener(lambda aid, s, t: calls_a.append(aid))
        reg.add_state_listener(lambda aid, s, t: calls_b.append(aid))

        reg.mark_working("coder-0", "t1")
        assert len(calls_a) == 1
        assert len(calls_b) == 1

    def test_broken_state_listener_does_not_block_others(self):
        reg = _make_registry()
        calls: list = []

        def bad_listener(aid, state, tid):
            raise RuntimeError("boom")

        reg.add_state_listener(bad_listener)
        reg.add_state_listener(lambda aid, s, t: calls.append(aid))

        reg.mark_working("coder-0", "t1")
        assert len(calls) == 1  # second listener still called

    def test_queue_listener_fires_on_enqueue(self):
        reg = _make_registry()
        calls: list[tuple] = []
        reg.add_queue_listener(lambda aid, eid, action, evt: calls.append((aid, action)))

        event = TaskAssignedEvent(task_id="t1", target_id="coder-0", source_id="pm")
        reg.enqueue("coder-0", event)
        assert len(calls) == 1
        assert calls[0] == ("coder-0", "added")


# =============================================================================
# DB persistence roundtrip
# =============================================================================


class TestDBPersistence:
    @pytest.fixture
    def db_registry(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        roles = {
            "coder": RoleConfig(name="coder", role_prompt="Code", tools=["worker"], model="test", max_turns=10),
        }
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
        }
        registry = Registry(framework_logger=_test_logger, db=db)
        registry.register_config(roles, agents)
        yield registry, db
        db.close()

    def test_agent_persisted_on_create(self, db_registry):
        registry, db = db_registry
        agents = registry._load_agents_from_db()
        assert len(agents) == 1
        agent = agents["coder-0"]
        assert agent.role == "coder"
        assert agent.model == "test"
        assert agent.state == AgentState.IDLE

    def test_state_change_persisted(self, db_registry):
        registry, db = db_registry
        registry.mark_working("coder-0", task_id="t1")
        agents = registry._load_agents_from_db()
        assert agents["coder-0"].state == AgentState.WORKING
        assert agents["coder-0"].current_task_id == "t1"

    def test_session_id_persisted(self, db_registry):
        registry, db = db_registry
        registry.update_session("coder-0", "sess-abc")
        session_ids = registry.get_session_ids()
        assert session_ids["coder-0"] == "sess-abc"

    def test_structural_fields_persisted(self, db_registry):
        """Agent structural fields (role_prompt, tools, max_turns) are written to DB."""
        registry, db = db_registry
        agents = registry._load_agents_from_db()
        agent = agents["coder-0"]
        assert agent.role_prompt == "Code"
        assert agent.tools == registry._agents["coder-0"].tools
        assert agent.max_turns == 10

    def test_upsert_does_not_clobber_runtime_state(self, db_registry):
        """On conflict, upsert updates structural fields but does not
        touch runtime fields (state, current_task_id, session_id)."""
        registry, db = db_registry
        registry.mark_working("coder-0", task_id="t1")

        # Re-upsert the same agent (simulates re-registration)
        agent = registry._agents["coder-0"]
        registry._db_upsert_agent(agent)

        agents = registry._load_agents_from_db()
        assert agents["coder-0"].role == "coder"
        assert agents["coder-0"].role_prompt == "Code"
        # ON CONFLICT clause only sets structural fields — runtime state is untouched
        assert agents["coder-0"].state == AgentState.WORKING

    def test_roles_persisted_at_construction(self, db_registry):
        """All roles are written to agent_roles at construction."""
        registry, db = db_registry
        roles = registry._load_roles_from_db()
        assert len(roles) == 1
        role = roles["coder"]
        assert role.role_prompt == "Code"
        assert role.tools == registry._roles["coder"].tools
        assert role.model == "test"
        assert role.max_turns == 10

    def test_network_persisted_at_construction(self, tmp_path):
        """Network groups and edges are written to DB at construction."""
        db = Database(str(tmp_path / "net.db"))
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {
            "a": AgentConfig(agent_id="a", role="w"),
            "b": AgentConfig(agent_id="b", role="w"),
            "c": AgentConfig(agent_id="c", role="w"),
        }
        network = NetworkConfig(groups={"team": ["a", "b"]}, connections={"b": ["c"]})
        registry = Registry(framework_logger=_test_logger, db=db)
        registry.register_config(roles, agents, network)

        # Verify groups
        group_rows = db.fetchall("SELECT group_name, agent_id FROM network_groups ORDER BY agent_id")
        assert len(group_rows) == 2
        assert {r["agent_id"] for r in group_rows} == {"a", "b"}

        # Verify edges (symmetrized, stored as sorted pair)
        edge_rows = db.fetchall("SELECT agent_a, agent_b FROM network_edges")
        assert len(edge_rows) == 1
        assert edge_rows[0]["agent_a"] == "b"
        assert edge_rows[0]["agent_b"] == "c"

        db.close()

    def test_runtime_agent_creation_persists_network(self, tmp_path):
        """Creating an agent at runtime persists its group and connection to DB."""
        db = Database(str(tmp_path / "net.db"))
        roles = {"w": RoleConfig(name="w", role_prompt="Work", tools=[], model="test", max_turns=10)}
        agents = {
            "a": AgentConfig(agent_id="a", role="w"),
            "b": AgentConfig(agent_id="b", role="w"),
        }
        network = NetworkConfig(groups={"team": ["a", "b"]})
        registry = Registry(framework_logger=_test_logger, db=db)
        registry.register_config(roles, agents, network)

        # Runtime: add agent c to the team group with a connection to a
        registry.create_agent("c", "w", "test", "Work", [], 10, groups=["team"], connections=["a"])

        # Verify group membership persisted
        group_rows = db.fetchall("SELECT agent_id FROM network_groups WHERE group_name = 'team' ORDER BY agent_id")
        assert {r["agent_id"] for r in group_rows} == {"a", "b", "c"}

        # Verify edge persisted
        edge_rows = db.fetchall("SELECT agent_a, agent_b FROM network_edges")
        assert len(edge_rows) == 1
        assert edge_rows[0]["agent_a"] == "a"
        assert edge_rows[0]["agent_b"] == "c"

        db.close()

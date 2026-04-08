"""Tests for magelab.network — Network runtime class."""

from magelab.registry_config import NetworkConfig
from magelab.state.registry_schemas import NetworkInstance


# =============================================================================
# Construction and queries
# =============================================================================


class TestNetworkQueries:
    def test_from_config_groups(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b", "c"]}})
        net = NetworkInstance(cfg)
        assert net.is_connected("a", "b")
        assert net.is_connected("b", "c")

    def test_from_config_connections(self):
        cfg = NetworkConfig(**{"connections": {"a": ["b"]}})
        net = NetworkInstance(cfg)
        assert net.is_connected("a", "b")
        assert net.is_connected("b", "a")  # symmetrized

    def test_from_config_groups_and_connections(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b"]}, "connections": {"a": ["c"]}})
        net = NetworkInstance(cfg)
        assert net.is_connected("a", "b")  # same group
        assert net.is_connected("a", "c")  # explicit connection
        assert not net.is_connected("b", "c")  # no link

    def test_is_connected_false(self):
        cfg = NetworkConfig(**{"groups": {"t1": ["a", "b"], "t2": ["c", "d"]}})
        net = NetworkInstance(cfg)
        assert not net.is_connected("a", "c")

    def test_is_connected_unknown_agent(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b"]}})
        net = NetworkInstance(cfg)
        assert not net.is_connected("a", "unknown")

    def test_get_connected_ids(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b", "c"]}, "connections": {"a": ["d"]}})
        net = NetworkInstance(cfg)
        assert net.get_connected_ids("a") == {"b", "c", "d"}

    def test_get_connected_ids_no_connections(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a"]}})
        net = NetworkInstance(cfg)
        assert net.get_connected_ids("a") == set()

    def test_get_connected_ids_unknown_agent(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b"]}})
        net = NetworkInstance(cfg)
        assert net.get_connected_ids("unknown") == set()

    def test_all_agents(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b"]}, "connections": {"c": ["a"]}})
        net = NetworkInstance(cfg)
        assert net.all_agents == {"a", "b", "c"}

    def test_overlapping_groups(self):
        cfg = NetworkConfig(**{"groups": {"t1": ["a", "b"], "t2": ["b", "c"]}})
        net = NetworkInstance(cfg)
        assert net.is_connected("a", "b")  # same group t1
        assert net.is_connected("b", "c")  # same group t2
        assert not net.is_connected("a", "c")  # no shared group


# =============================================================================
# Mutation
# =============================================================================


class TestNetworkMutation:
    def test_add_to_group_existing(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b"]}})
        net = NetworkInstance(cfg)
        net.add_to_group("c", "t")
        assert net.is_connected("a", "c")
        assert net.is_connected("c", "b")
        assert "c" in net.all_agents

    def test_add_to_group_new_group(self):
        cfg = NetworkConfig(**{"groups": {"t1": ["a"]}})
        net = NetworkInstance(cfg)
        net.add_to_group("b", "t2")
        assert not net.is_connected("a", "b")  # different groups
        assert "b" in net.all_agents

    def test_add_connection(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a"]}})
        net = NetworkInstance(cfg)
        net.add_connection("a", "b")
        assert net.is_connected("a", "b")
        assert net.is_connected("b", "a")  # symmetrized
        assert "b" in net.all_agents

    def test_add_connection_self_ignored(self):
        cfg = NetworkConfig(**{"groups": {"t": ["a"]}})
        net = NetworkInstance(cfg)
        net.add_connection("a", "a")
        assert "a" not in net.get_connected_ids("a")

    def test_add_to_group_idempotent_connectivity(self):
        """Adding an agent to a group it's already in doesn't break connectivity or duplicate."""
        cfg = NetworkConfig(**{"groups": {"t": ["a", "b"]}})
        net = NetworkInstance(cfg)
        net.add_to_group("a", "t")  # already in group
        assert net.is_connected("a", "b")
        assert net.get_connected_ids("a") == {"b"}
        assert len(net._groups["t"]) == 2  # no duplicate added

    def test_add_connection_idempotent(self):
        """Adding the same connection twice doesn't cause issues."""
        cfg = NetworkConfig(**{"connections": {"a": ["b"]}})
        net = NetworkInstance(cfg)
        net.add_connection("a", "b")  # already connected
        assert net.is_connected("a", "b")
        assert net.get_connected_ids("a") == {"b"}

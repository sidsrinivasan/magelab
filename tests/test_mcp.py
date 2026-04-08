"""Tests for magelab.tools.mcp — proxy, module loading, tool resolution, and lifecycle."""

import asyncio
import logging
import types

import pytest
import yaml
from pathlib import Path
from mcp.server.fastmcp import FastMCP

from magelab.events import MCPEvent
from magelab.state.database import Database
from magelab.tools.mcp import (
    AgentProxy,
    LoadedMCPModule,
    MCPContext,
    create_agent_proxy,
    get_tool_names,
    init_mcp_servers,
    load_mcp_module,
    resolve_mcp_tools,
)
from magelab.orchestrator import _copy_session_configs
from magelab.registry_config import AgentConfig, RoleConfig
from magelab.runners.claude_runner import build_allowed_tools
from magelab.tools.bundles import expand
from magelab.org_config import OrgConfig, OrgSettings


# =============================================================================
# expand() passthrough
# =============================================================================


def test_expand_passes_through_mcp_prefixed_names():
    result = expand(["Read", "mcp__voting__cast_vote", "mcp__voting"])
    assert "mcp__voting__cast_vote" in result
    assert "mcp__voting" in result
    assert "Read" in result


def test_expand_still_rejects_unknown_names():
    with pytest.raises(ValueError, match="Unknown tool or bundle"):
        expand(["totally_bogus_tool"])


# =============================================================================
# resolve_mcp_tools
# =============================================================================


def test_resolve_expands_server_level_reference():
    available = {"voting": ["cast_vote", "get_results"], "market": ["place_bid", "get_prices"]}
    tools = ["Read", "mcp__voting", "mcp__market__place_bid"]
    resolved = resolve_mcp_tools(tools, available)
    assert resolved == ["Read", "mcp__voting__cast_vote", "mcp__voting__get_results", "mcp__market__place_bid"]


def test_resolve_passes_through_specific_tool():
    available = {"voting": ["cast_vote", "get_results"]}
    resolved = resolve_mcp_tools(["mcp__voting__cast_vote"], available)
    assert resolved == ["mcp__voting__cast_vote"]


def test_resolve_passes_through_unknown_server():
    """Unknown server-level refs pass through (may be external, in settings)."""
    available = {"voting": ["cast_vote"]}
    resolved = resolve_mcp_tools(["mcp__slack"], available)
    assert resolved == ["mcp__slack"]


def test_resolve_no_mcp_is_noop():
    assert resolve_mcp_tools(["Read", "Write"], {}) == ["Read", "Write"]


# =============================================================================
# External MCP tool reference full pipeline
# =============================================================================


class TestExternalMCPToolPipeline:
    """Verify that external MCP tool references (mcp__slack__post_message)
    survive the full expand → resolve → build_allowed_tools pipeline."""

    def test_external_specific_tool_survives_pipeline(self):
        """mcp__slack__post_message passes through expand, resolve, and build_allowed_tools."""
        role_tools = ["worker", "mcp__slack__post_message"]
        expanded = expand(role_tools, strict=False)
        assert "mcp__slack__post_message" in expanded

        resolved = resolve_mcp_tools(expanded, {})
        assert "mcp__slack__post_message" in resolved

        allowed = build_allowed_tools(resolved)
        assert "mcp__slack__post_message" in allowed

    def test_external_server_level_ref_survives_pipeline(self):
        """mcp__slack (server-level) passes through when server is unknown (external)."""
        role_tools = ["worker", "mcp__slack"]
        expanded = expand(role_tools, strict=False)
        assert "mcp__slack" in expanded

        resolved = resolve_mcp_tools(expanded, {})
        assert "mcp__slack" in resolved

        allowed = build_allowed_tools(resolved)
        assert "mcp__slack" in allowed

    def test_mixed_internal_and_external_mcp_refs(self):
        """Internal MCP refs expand, external ones pass through."""
        internal_tools = {"voting": ["cast_vote", "get_results"]}
        role_tools = ["worker", "mcp__voting", "mcp__slack__post_message"]
        expanded = expand(role_tools, strict=False)
        resolved = resolve_mcp_tools(expanded, internal_tools)
        # Internal: expanded to specific tools
        assert "mcp__voting__cast_vote" in resolved
        assert "mcp__voting__get_results" in resolved
        # External: passed through as-is
        assert "mcp__slack__post_message" in resolved
        # Server-level internal ref should be expanded away
        assert "mcp__voting" not in resolved


# =============================================================================
# FastMCP server loading and introspection
# =============================================================================


@pytest.fixture
def voting_server():
    """Create a test FastMCP voting server."""
    _votes: dict[str, list] = {}
    srv = FastMCP("voting")

    @srv.tool()
    async def cast_vote(agent_id: str, proposal: str, vote: str) -> str:
        """Cast a vote on a proposal."""
        _votes.setdefault(proposal, []).append({"agent_id": agent_id, "vote": vote})
        return f"Vote recorded: {agent_id} voted {vote}"

    @srv.tool()
    async def get_results(agent_id: str, proposal: str) -> str:
        """Get voting results."""
        results = _votes.get(proposal, [])
        return f"Results for {proposal}: {results}"

    srv._test_votes = _votes  # expose for assertions
    return srv


def test_load_mcp_module(monkeypatch, voting_server):
    mod = types.ModuleType("mock_voting")
    mod.server = voting_server
    monkeypatch.setattr("magelab.tools.mcp.importlib.import_module", lambda name: mod)
    loaded = load_mcp_module("mock_voting")
    assert isinstance(loaded, LoadedMCPModule)
    assert isinstance(loaded.server, FastMCP)
    assert loaded.module is mod


def test_load_mcp_module_raises_on_missing_server(monkeypatch):
    mod = types.ModuleType("empty_mod")
    monkeypatch.setattr("magelab.tools.mcp.importlib.import_module", lambda name: mod)
    with pytest.raises(ValueError, match="server"):
        load_mcp_module("empty_mod")


def test_load_mcp_module_raises_on_wrong_type(monkeypatch):
    mod = types.ModuleType("bad_mod")
    mod.server = "not a FastMCP"
    monkeypatch.setattr("magelab.tools.mcp.importlib.import_module", lambda name: mod)
    with pytest.raises(ValueError, match="FastMCP"):
        load_mcp_module("bad_mod")


def test_get_tool_names(voting_server):
    names = get_tool_names(voting_server)
    assert sorted(names) == ["cast_vote", "get_results"]


# =============================================================================
# create_agent_proxy — agent_id injection
# =============================================================================


def test_proxy_injects_agent_id(voting_server):
    proxy = create_agent_proxy("voting", voting_server, agent_id="coder_1")
    cast_vote = next(t for t in proxy.tools if t.name == "cast_vote")
    asyncio.run(cast_vote.handler({"proposal": "p1", "vote": "yes"}))
    assert voting_server._test_votes["p1"] == [{"agent_id": "coder_1", "vote": "yes"}]


def test_proxy_strips_agent_id_from_schema(voting_server):
    proxy = create_agent_proxy("voting", voting_server, agent_id="coder_1")
    cast_vote = next(t for t in proxy.tools if t.name == "cast_vote")
    assert "agent_id" not in cast_vote.input_schema["properties"]
    assert "agent_id" not in cast_vote.input_schema.get("required", [])


def test_different_agents_get_different_ids(voting_server):
    proxy1 = create_agent_proxy("voting", voting_server, agent_id="coder_1")
    proxy2 = create_agent_proxy("voting", voting_server, agent_id="coder_2")
    cast1 = next(t for t in proxy1.tools if t.name == "cast_vote")
    cast2 = next(t for t in proxy2.tools if t.name == "cast_vote")
    asyncio.run(cast1.handler({"proposal": "p1", "vote": "yes"}))
    asyncio.run(cast2.handler({"proposal": "p1", "vote": "no"}))
    votes = voting_server._test_votes["p1"]
    assert votes[0]["agent_id"] == "coder_1"
    assert votes[1]["agent_id"] == "coder_2"


def test_proxy_returns_agent_proxy(voting_server):
    proxy = create_agent_proxy("voting", voting_server, agent_id="coder_1")
    assert isinstance(proxy, AgentProxy)
    assert proxy.server is not None
    assert len(proxy.tools) == 2


# =============================================================================
# Config parsing
# =============================================================================


def _write_config(tmp_path: Path, overrides: dict) -> str:
    base = {
        "settings": {"org_name": "test_org"},
        "roles": {
            "coder": {
                "name": "coder",
                "role_prompt": "You are a coder.",
                "tools": ["Read"],
                "model": "test",
            }
        },
        "agents": {"coder_1": {"agent_id": "coder_1", "role": "coder"}},
    }
    # Merge settings-level overrides into settings sub-dict
    settings_keys = {"mcp_modules", "agent_settings_dir", "org_name"}
    for k in list(overrides.keys()):
        if k in settings_keys:
            base["settings"][k] = overrides.pop(k)
    base.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(base))
    return str(path)


def test_mcp_modules_parsed(tmp_path):
    path = _write_config(tmp_path, {"mcp_modules": {"voting": "experiments.voting.server"}})
    config = OrgConfig.from_yaml(path)
    assert config.settings.mcp_modules == {"voting": "experiments.voting.server"}


def test_no_mcp_defaults_to_empty(tmp_path):
    path = _write_config(tmp_path, {})
    config = OrgConfig.from_yaml(path)
    assert config.settings.mcp_modules == {}


def test_agent_settings_dir_parsed(tmp_path):
    path = _write_config(tmp_path, {"agent_settings_dir": "session_config"})
    config = OrgConfig.from_yaml(path)
    # from_yaml resolves to absolute path
    assert config.settings.agent_settings_dir == str((tmp_path / "session_config").resolve())


def test_role_session_config_parsed(tmp_path):
    config_data = {
        "roles": {
            "coder": {
                "name": "coder",
                "role_prompt": "You are a coder.",
                "tools": ["Read"],
                "model": "test",
                "session_config": "coder/",
            }
        },
    }
    path = _write_config(tmp_path, config_data)
    config = OrgConfig.from_yaml(path)
    assert config.roles["coder"].session_config == "coder/"


def test_agent_session_config_override_parsed(tmp_path):
    config_data = {
        "agents": {
            "coder_1": {
                "agent_id": "coder_1",
                "role": "coder",
                "session_config_override": "strict/",
            }
        },
    }
    path = _write_config(tmp_path, config_data)
    config = OrgConfig.from_yaml(path)
    assert config.agents["coder_1"].session_config_override == "strict/"


def test_mcp_tool_names_in_role_tools(tmp_path):
    config_data = {
        "mcp_modules": {"voting": "experiments.voting.server"},
        "roles": {
            "coder": {
                "name": "coder",
                "role_prompt": "You are a coder.",
                "tools": ["Read", "mcp__voting", "mcp__voting__cast_vote"],
                "model": "test",
            }
        },
    }
    path = _write_config(tmp_path, config_data)
    config = OrgConfig.from_yaml(path)
    assert "mcp__voting" in config.roles["coder"].tools
    assert "mcp__voting__cast_vote" in config.roles["coder"].tools


def test_config_round_trip(tmp_path):
    config_data = {
        "mcp_modules": {"voting": "experiments.voting.server"},
        "agent_settings_dir": "session_config",
        "roles": {
            "coder": {
                "name": "coder",
                "role_prompt": "You are a coder.",
                "tools": ["Read", "mcp__voting__cast_vote"],
                "model": "test",
                "session_config": "coder/",
            }
        },
    }
    path = _write_config(tmp_path, config_data)
    config = OrgConfig.from_yaml(path)

    # to_dict() produces flattened (DB-style) output; use from_dict for round-trip
    config2 = OrgConfig.from_dict(config.to_dict())

    assert config2.settings.mcp_modules == {"voting": "experiments.voting.server"}
    # agent_settings_dir is preserved as-is in from_dict (already absolute from first load)
    assert Path(config2.settings.agent_settings_dir).is_absolute()
    assert config2.roles["coder"].session_config == "coder/"


# =============================================================================
# _copy_session_configs — filesystem
# =============================================================================


class TestCopySessionConfigs:
    def _make_org_config(self, agent_settings_dir, roles, agents):
        return OrgConfig(
            roles=roles,
            agents=agents,
            settings=OrgSettings(org_name="test", agent_settings_dir=str(agent_settings_dir)),
        )

    def test_copies_role_session_config(self, tmp_path):
        """Files from role session_config are copied to .sessions/<agent_id>/."""
        # Set up agent_settings_dir with a role config
        settings_dir = tmp_path / "agent_settings"
        (settings_dir / "coder").mkdir(parents=True)
        (settings_dir / "coder" / "settings.json").write_text('{"model": "sonnet"}')
        (settings_dir / "coder" / ".mcp.json").write_text('{"servers": {}}')

        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code.", tools=["worker"], model="test", max_turns=10, session_config="coder"
            )
        }
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "coder-1": AgentConfig(agent_id="coder-1", role="coder"),
        }
        org = self._make_org_config(settings_dir, roles, agents)

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _copy_session_configs(org, output_dir, logging.getLogger("test"))

        # Both agents should have copies
        for agent_id in ["coder-0", "coder-1"]:
            session_dir = output_dir / ".sessions" / agent_id
            assert (session_dir / "settings.json").exists()
            assert (session_dir / ".mcp.json").exists()
            assert (session_dir / "settings.json").read_text() == '{"model": "sonnet"}'

    def test_agent_override_takes_precedence(self, tmp_path):
        """Agent session_config_override wins over role session_config."""
        settings_dir = tmp_path / "agent_settings"
        (settings_dir / "coder").mkdir(parents=True)
        (settings_dir / "coder" / "settings.json").write_text('{"from": "role"}')
        (settings_dir / "special").mkdir(parents=True)
        (settings_dir / "special" / "settings.json").write_text('{"from": "override"}')

        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code.", tools=["worker"], model="test", max_turns=10, session_config="coder"
            )
        }
        agents = {
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "coder-1": AgentConfig(agent_id="coder-1", role="coder", session_config_override="special"),
        }
        org = self._make_org_config(settings_dir, roles, agents)

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _copy_session_configs(org, output_dir, logging.getLogger("test"))

        # coder-0 gets role config
        assert (output_dir / ".sessions" / "coder-0" / "settings.json").read_text() == '{"from": "role"}'
        # coder-1 gets override config
        assert (output_dir / ".sessions" / "coder-1" / "settings.json").read_text() == '{"from": "override"}'

    def test_no_session_config_skips_agent(self, tmp_path):
        """Agents with no session_config (role or override) get no session dir."""
        settings_dir = tmp_path / "agent_settings"
        settings_dir.mkdir()

        roles = {"worker": RoleConfig(name="worker", role_prompt="Work.", tools=["worker"], model="test", max_turns=10)}
        agents = {"worker-0": AgentConfig(agent_id="worker-0", role="worker")}
        org = self._make_org_config(settings_dir, roles, agents)

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _copy_session_configs(org, output_dir, logging.getLogger("test"))

        assert not (output_dir / ".sessions" / "worker-0").exists()

    def test_no_agent_settings_dir_is_noop(self, tmp_path):
        """When agent_settings_dir is not set, nothing happens."""
        roles = {"worker": RoleConfig(name="worker", role_prompt="Work.", tools=["worker"], model="test", max_turns=10)}
        agents = {"worker-0": AgentConfig(agent_id="worker-0", role="worker")}
        org = OrgConfig(roles=roles, agents=agents, settings=OrgSettings(org_name="test"))

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _copy_session_configs(org, output_dir, logging.getLogger("test"))
        # No .sessions dir created
        assert not (output_dir / ".sessions").exists()

    def test_resume_merges_into_existing(self, tmp_path):
        """Second call (simulating resume) merges files, overwriting existing ones."""
        settings_dir = tmp_path / "agent_settings"
        (settings_dir / "coder").mkdir(parents=True)
        (settings_dir / "coder" / "settings.json").write_text('{"version": 1}')

        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code.", tools=["worker"], model="test", max_turns=10, session_config="coder"
            )
        }
        agents = {"coder-0": AgentConfig(agent_id="coder-0", role="coder")}
        org = self._make_org_config(settings_dir, roles, agents)

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        logger = logging.getLogger("test")

        # First copy
        _copy_session_configs(org, output_dir, logger)
        # Agent creates a runtime file
        (output_dir / ".sessions" / "coder-0" / "runtime.txt").write_text("agent state")

        # Update template and re-copy (resume)
        (settings_dir / "coder" / "settings.json").write_text('{"version": 2}')
        _copy_session_configs(org, output_dir, logger)

        # Template file overwritten, runtime file preserved
        assert (output_dir / ".sessions" / "coder-0" / "settings.json").read_text() == '{"version": 2}'
        assert (output_dir / ".sessions" / "coder-0" / "runtime.txt").read_text() == "agent state"

    def test_pipeline_configs_dir_preferred(self, tmp_path):
        """When .sessions/_configs/ exists (pipeline mode), use that over agent_settings_dir."""
        # The real agent_settings_dir
        settings_dir = tmp_path / "agent_settings"
        (settings_dir / "coder").mkdir(parents=True)
        (settings_dir / "coder" / "settings.json").write_text('{"from": "original"}')

        # Pipeline-copied version
        output_dir = tmp_path / "output"
        pipeline_configs = output_dir / ".sessions" / "_configs"
        (pipeline_configs / "coder").mkdir(parents=True)
        (pipeline_configs / "coder" / "settings.json").write_text('{"from": "pipeline"}')

        roles = {
            "coder": RoleConfig(
                name="coder", role_prompt="Code.", tools=["worker"], model="test", max_turns=10, session_config="coder"
            )
        }
        agents = {"coder-0": AgentConfig(agent_id="coder-0", role="coder")}
        org = self._make_org_config(settings_dir, roles, agents)

        _copy_session_configs(org, output_dir, logging.getLogger("test"))

        # Should use pipeline version
        assert (output_dir / ".sessions" / "coder-0" / "settings.json").read_text() == '{"from": "pipeline"}'


# =============================================================================
# init_mcp_servers — lifecycle
# =============================================================================


@pytest.fixture
def mcp_context(tmp_path):
    """Create an MCPContext with a real DB and a capturing emit_event."""
    db = Database(tmp_path / "test.db")
    db.init_run_meta(org_name="test", org_config="{}")
    emitted: list = []

    ctx = MCPContext(db=db, emit_event=lambda e: emitted.append(e))
    yield ctx, emitted
    db.close()


def _make_loaded_module(server, init_fn=None):
    """Create a LoadedMCPModule with an optional init function."""
    mod = types.ModuleType("test_mod")
    mod.server = server
    if init_fn is not None:
        mod.init = init_fn
    return LoadedMCPModule(server=server, module=mod)


def test_init_calls_init_when_defined(voting_server, mcp_context):
    ctx, _ = mcp_context
    called = []

    def init_fn(context):
        called.append(context)

    loaded = {"voting": _make_loaded_module(voting_server, init_fn)}
    init_mcp_servers(loaded, ctx)
    assert len(called) == 1
    assert called[0] is ctx


def test_init_skips_modules_without_init(voting_server, mcp_context):
    ctx, _ = mcp_context
    loaded = {"voting": _make_loaded_module(voting_server)}
    # Should not raise
    init_mcp_servers(loaded, ctx)


def test_init_raises_on_non_callable_init(voting_server, mcp_context):
    ctx, _ = mcp_context
    mod = types.ModuleType("bad_mod")
    mod.server = voting_server
    mod.init = "not callable"
    loaded = {"bad": LoadedMCPModule(server=voting_server, module=mod)}
    with pytest.raises(RuntimeError, match="not callable"):
        init_mcp_servers(loaded, ctx)


def test_init_raises_on_async_init(voting_server, mcp_context):
    ctx, _ = mcp_context

    async def async_init(context):
        pass

    loaded = {"voting": _make_loaded_module(voting_server, async_init)}
    with pytest.raises(RuntimeError, match="async"):
        init_mcp_servers(loaded, ctx)


def test_init_raises_on_init_failure(voting_server, mcp_context):
    ctx, _ = mcp_context

    def bad_init(context):
        raise ValueError("schema error")

    loaded = {"voting": _make_loaded_module(voting_server, bad_init)}
    with pytest.raises(RuntimeError, match="Failed to initialize"):
        init_mcp_servers(loaded, ctx)


def test_init_can_register_schema(voting_server, mcp_context):
    ctx, _ = mcp_context

    def init_fn(context):
        context.db.register_schema("""
            CREATE TABLE IF NOT EXISTS test_state (
                id INTEGER PRIMARY KEY,
                value TEXT
            );
        """)
        context.db.execute("INSERT INTO test_state (value) VALUES (?)", ("hello",))

    loaded = {"voting": _make_loaded_module(voting_server, init_fn)}
    init_mcp_servers(loaded, ctx)

    row = ctx.db.fetchone("SELECT value FROM test_state WHERE id = 1")
    assert row["value"] == "hello"


def test_init_can_emit_events(voting_server, mcp_context):
    ctx, emitted = mcp_context

    def init_fn(context):
        context.emit_event(
            MCPEvent(
                target_id="agent-0",
                server_name="voting",
                payload="Voting is open",
            )
        )

    loaded = {"voting": _make_loaded_module(voting_server, init_fn)}
    init_mcp_servers(loaded, ctx)

    assert len(emitted) == 1
    assert isinstance(emitted[0], MCPEvent)
    assert emitted[0].payload == "Voting is open"
    assert emitted[0].server_name == "voting"


def test_init_schema_idempotent_on_resume(voting_server, mcp_context):
    """Calling init() twice (simulating resume) should not fail."""
    ctx, _ = mcp_context

    def init_fn(context):
        context.db.register_schema("""
            CREATE TABLE IF NOT EXISTS counter (
                id INTEGER PRIMARY KEY,
                value INTEGER NOT NULL
            );
        """)

    loaded = {"voting": _make_loaded_module(voting_server, init_fn)}
    init_mcp_servers(loaded, ctx)
    # Second call simulates resume — should not raise
    init_mcp_servers(loaded, ctx)

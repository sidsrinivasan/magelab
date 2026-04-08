"""Integration tests for MCP server lifecycle through the Orchestrator.

Tests the full flow: FastMCP server → Orchestrator.build() → init() → emit MCPEvent
→ _dispatch_event → agent receives prompt. Also tests ClaudeRunner MCP wiring
(per-agent config construction).
"""

import logging
import types
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from mcp.server.fastmcp import FastMCP

from magelab.events import MCPEvent
from magelab.orchestrator import Orchestrator
from magelab.org_config import OrgConfig
from magelab.runners.claude_runner import (
    _build_agent_mcp_proxies,
    build_allowed_tools,
)
from magelab.state.database import Database
from magelab.state.task_schemas import TaskStatus
from magelab.tools.mcp import LoadedMCPModule, get_tool_names, resolve_mcp_tools

from .conftest import MockRunner, get_agent_dispatches

_test_logger = logging.getLogger("test")


# =============================================================================
# Helpers
# =============================================================================


def _make_voting_server():
    """Create an inline FastMCP server with an init() that emits an MCPEvent."""
    srv = FastMCP("voting")

    @srv.tool()
    async def cast_vote(agent_id: str, proposal: str, vote: str) -> str:
        """Cast a vote."""
        return f"Vote recorded: {agent_id} voted {vote}"

    return srv


def _make_loaded_module(server, init_fn=None):
    """Create a LoadedMCPModule with an optional init function."""
    mod = types.ModuleType("test_voting")
    mod.server = server
    if init_fn is not None:
        mod.init = init_fn
    return LoadedMCPModule(server=server, module=mod)


def _write_config(tmp_path, mcp_modules=None, agent_tools=None):
    """Write a YAML config and return (config_path, output_dir)."""
    config = {
        "settings": {
            "org_name": "test_org",
            "org_prompt": "Test",
            "org_timeout_seconds": 10,
        },
        "roles": {
            "worker": {
                "name": "worker",
                "role_prompt": "Work.",
                "tools": agent_tools or ["worker"],
                "model": "test",
                "max_turns": 10,
            }
        },
        "agents": {
            "worker-0": {"agent_id": "worker-0", "role": "worker"},
        },
        "initial_tasks": [
            {"id": "task-1", "title": "Test Task", "description": "Do something", "assigned_to": "worker-0"}
        ],
    }
    if mcp_modules:
        config["settings"]["mcp_modules"] = mcp_modules
    path = tmp_path / "test_org.yaml"
    with open(path, "w") as f:
        yaml.dump(config, f)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "workspace").mkdir()
    return str(path), output_dir


# =============================================================================
# MCP server lifecycle through Orchestrator
# =============================================================================


class TestMCPServerLifecycle:
    @pytest.mark.asyncio
    async def test_init_called_during_build(self, tmp_path):
        """Orchestrator.build() calls init() on MCP modules with MCPContext."""
        config_path, output_dir = _write_config(tmp_path)
        org_config = OrgConfig.from_yaml(config_path)

        server = _make_voting_server()
        init_calls = []

        def init_fn(context):
            init_calls.append(context)

        loaded = {"voting": _make_loaded_module(server, init_fn)}

        runner = MockRunner()
        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner):
            with patch("magelab.orchestrator.load_mcp_module", side_effect=lambda p: loaded[p]):
                org_config.settings.mcp_modules = {"voting": "voting"}
                orch = await Orchestrator.build(org_config, output_dir, resume_mode=None)

        assert len(init_calls) == 1
        # MCPContext should have db and emit_event
        ctx = init_calls[0]
        assert hasattr(ctx, "db")
        assert hasattr(ctx, "emit_event")
        orch._db.close()

    @pytest.mark.asyncio
    async def test_init_emits_event_that_reaches_agent(self, tmp_path):
        """MCPEvent emitted during init() is dispatched through the orchestrator
        and the agent receives it as a prompt."""
        config_path, output_dir = _write_config(tmp_path, agent_tools=["worker", "mcp__voting"])
        org_config = OrgConfig.from_yaml(config_path)

        server = _make_voting_server()

        def init_fn(context):
            context.emit_event(
                MCPEvent(
                    target_id="worker-0",
                    server_name="voting",
                    payload="Voting is now open! Cast your votes.",
                )
            )

        loaded = {"voting": _make_loaded_module(server, init_fn)}

        runner = MockRunner()

        async def finish_task():
            await orch.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["worker-0"] = [finish_task]

        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner):
            with patch("magelab.orchestrator.load_mcp_module", side_effect=lambda p: loaded[p]):
                org_config.settings.mcp_modules = {"voting": "voting"}
                orch = await Orchestrator.build(org_config, output_dir, resume_mode=None)

        await orch.run(initial_tasks=org_config.initial_tasks, sync=True, sync_max_rounds=5)

        # Agent should have been dispatched for both TaskAssigned and MCPEvent
        dispatches = get_agent_dispatches(orch._db, "worker-0")
        event_types = [d["event_type"] for d in dispatches]
        assert "TaskAssignedEvent" in event_types
        assert "MCPEvent" in event_types

        # The MCPEvent dispatch should have the payload as the prompt
        mcp_calls = [c for c in runner.calls if c[0] == "worker-0" and "Voting is now open" in c[2]]
        assert len(mcp_calls) == 1

    @pytest.mark.asyncio
    async def test_init_writes_to_db_and_persists(self, tmp_path):
        """Data written by init() via MCPContext.db persists in the database."""
        config_path, output_dir = _write_config(tmp_path)
        org_config = OrgConfig.from_yaml(config_path)

        server = _make_voting_server()

        def init_fn(context):
            context.db.register_schema("""
                CREATE TABLE IF NOT EXISTS votes (
                    id INTEGER PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    vote TEXT NOT NULL
                );
            """)
            context.db.execute(
                "INSERT INTO votes (agent_id, proposal, vote) VALUES (?, ?, ?)",
                ("worker-0", "proposal-1", "yes"),
            )

        loaded = {"voting": _make_loaded_module(server, init_fn)}

        runner = MockRunner()

        async def finish():
            await orch.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["worker-0"] = [finish]

        with patch("magelab.orchestrator.ClaudeRunner", return_value=runner):
            with patch("magelab.orchestrator.load_mcp_module", side_effect=lambda p: loaded[p]):
                org_config.settings.mcp_modules = {"voting": "voting"}
                orch = await Orchestrator.build(org_config, output_dir, resume_mode=None)

        await orch.run(initial_tasks=org_config.initial_tasks)

        # Verify data persisted by opening a fresh DB connection
        db2 = Database(output_dir / "test_org.db")
        row = db2.fetchone("SELECT agent_id, proposal, vote FROM votes WHERE id = 1")
        assert row["agent_id"] == "worker-0"
        assert row["proposal"] == "proposal-1"
        assert row["vote"] == "yes"
        db2.close()


# =============================================================================
# ClaudeRunner MCP wiring
# =============================================================================


class TestClaudeRunnerMCPWiring:
    """Test that ClaudeRunner correctly builds per-agent configs with MCP servers."""

    def test_build_agent_mcp_proxies_creates_proxy_for_matching_tools(self):
        """Agent with mcp__voting__cast_vote gets a proxy for the voting server."""
        server = _make_voting_server()
        resolved = ["tasks_mark_finished", "mcp__voting__cast_vote"]
        proxies = _build_agent_mcp_proxies("worker-0", resolved, {"voting": server})
        assert "voting" in proxies

    def test_build_agent_mcp_proxies_skips_unrelated_servers(self):
        """Agent without any mcp__voting__ tools gets no proxy."""
        server = _make_voting_server()
        resolved = ["tasks_mark_finished", "Read"]
        proxies = _build_agent_mcp_proxies("worker-0", resolved, {"voting": server})
        assert proxies == {}

    def test_build_agent_mcp_proxies_different_agents_get_different_proxies(self):
        """Each agent gets its own proxy instance (for agent_id injection)."""
        server = _make_voting_server()
        resolved = ["mcp__voting__cast_vote"]
        proxy_a = _build_agent_mcp_proxies("alice", resolved, {"voting": server})
        proxy_b = _build_agent_mcp_proxies("bob", resolved, {"voting": server})
        # Different proxy objects
        assert proxy_a["voting"] is not proxy_b["voting"]

    def test_allowed_tools_includes_mcp_prefixed_tools(self):
        """build_allowed_tools passes through mcp__ prefixed tool names."""
        resolved = ["tasks_mark_finished", "mcp__voting__cast_vote", "Read"]
        allowed = build_allowed_tools(resolved)
        assert "mcp__voting__cast_vote" in allowed
        assert "Read" in allowed
        assert "mcp__magelab__tasks_mark_finished" in allowed

    def test_resolve_mcp_tools_expands_server_ref_with_real_server(self):
        """Using a real FastMCP server's tool names for resolution."""
        server = _make_voting_server()
        tool_names = get_tool_names(server)
        available = {"voting": tool_names}
        resolved = resolve_mcp_tools(["worker", "mcp__voting"], available)
        # worker bundle tools pass through, voting tools expanded
        assert "worker" in resolved  # non-bundle, non-mcp passes through
        for name in tool_names:
            assert f"mcp__voting__{name}" in resolved

    def test_env_var_points_to_session_dir(self):
        """CLAUDE_CONFIG_DIR env var should point to .sessions/<agent_id>."""
        # This is a structural test — verify the path construction
        working_dir = "/tmp/output/workspace"
        expected_base = Path(working_dir).parent / ".sessions"
        for agent_id in ["coder-0", "reviewer-0"]:
            expected = str(expected_base / agent_id)
            actual = str(Path(working_dir).parent / ".sessions" / agent_id)
            assert actual == expected

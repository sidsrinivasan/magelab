"""Tests for magelab.frontend.server — WebSocket server."""

import asyncio
import logging
import pytest
from aiohttp.test_utils import TestClient, TestServer
from magelab.registry_config import AgentConfig, RoleConfig
from magelab.frontend.bridge import FrontendBridge
from magelab.frontend.server import create_app
from magelab.state.registry import Registry
from magelab.state.task_store import TaskStore
from magelab.state.wire_store import WireStore

_test_logger = logging.getLogger("test")


def _make_app():
    roles = {
        "pm": RoleConfig(name="pm", role_prompt="Manage.", tools=["management"], model="test"),
        "coder": RoleConfig(name="coder", role_prompt="Code.", tools=["worker"], model="test"),
    }
    agents = {
        "pm": AgentConfig(agent_id="pm", role="pm"),
        "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
    }
    task_store = TaskStore(framework_logger=_test_logger)
    registry = Registry(framework_logger=_test_logger)
    registry.register_config(roles, agents)
    wire_store = WireStore(framework_logger=_test_logger)
    bridge = FrontendBridge(task_store, registry, wire_store, org_name="test-org")
    app = create_app(bridge)
    return app, bridge


@pytest.mark.asyncio
async def test_ws_receives_init_on_connect():
    app, bridge = _make_app()
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/ws")
        msg = await ws.receive_json()
        assert msg["type"] == "init"
        assert "pm" in msg["agents"]
        await ws.close()


@pytest.mark.asyncio
async def test_ws_receives_broadcast():
    app, bridge = _make_app()
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume init
        await bridge.broadcast(bridge.serialize_transcript("coder-0", "assistant_text", "hello"))
        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg["type"] == "transcript_entry"
        assert msg["agent_id"] == "coder-0"
        await ws.close()


@pytest.mark.asyncio
async def test_reconnection_replays_event_log():
    """A second client receives all event_log entries accumulated before it connects."""
    app, bridge = _make_app()
    async with TestClient(TestServer(app)) as client:
        # First client connects and triggers some events that populate event_log
        ws1 = await client.ws_connect("/ws")
        await ws1.receive_json()  # consume init

        # Populate event_log via serialize_transcript (appends to bridge.event_log)
        bridge.serialize_transcript("pm", "assistant_text", "first message")
        bridge.serialize_transcript("coder-0", "assistant_text", "second message")
        assert len(bridge.event_log) == 2

        # Second client connects — should receive init + both replayed entries
        ws2 = await client.ws_connect("/ws")
        init_msg = await asyncio.wait_for(ws2.receive_json(), timeout=2.0)
        assert init_msg["type"] == "init"

        replay1 = await asyncio.wait_for(ws2.receive_json(), timeout=2.0)
        assert replay1["type"] == "transcript_entry"
        assert replay1["agent_id"] == "pm"
        assert replay1["content"] == "first message"

        replay2 = await asyncio.wait_for(ws2.receive_json(), timeout=2.0)
        assert replay2["type"] == "transcript_entry"
        assert replay2["agent_id"] == "coder-0"
        assert replay2["content"] == "second message"

        await ws1.close()
        await ws2.close()


@pytest.mark.asyncio
async def test_workspace_file_path_traversal_rejected():
    """Requests with ../ path traversal must be rejected with 403."""
    import tempfile
    from pathlib import Path
    from magelab.frontend.server import create_app as _create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        (workspace / "legit.txt").write_text("ok")

        # Create a file outside the workspace that an attacker might target
        secret = Path(tmpdir) / "secret.txt"
        secret.write_text("top secret")

        app, bridge = _make_app()
        # Rebuild app with workspace_dir so the file routes are registered
        app = _create_app(bridge, workspace_dir=workspace)

        async with TestClient(TestServer(app)) as client:
            # Legit request works
            resp = await client.request("GET", "/api/workspace/file", params={"path": "legit.txt"})
            assert resp.status == 200
            body = await resp.json()
            assert body["content"] == "ok"

            # Path traversal is blocked
            resp = await client.request("GET", "/api/workspace/file", params={"path": "../secret.txt"})
            assert resp.status == 403
            body = await resp.json()
            assert "outside" in body["error"].lower()

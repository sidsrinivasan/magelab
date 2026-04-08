"""WebSocket server for the frontend dashboard.

Serves a /ws endpoint that streams orchestrator events to browser clients,
and optionally serves static files from a frontend build directory.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from ..org_config import OrgConfig
from ..events import Event
from ..orchestrator import Orchestrator
from ..state.registry import Registry
from ..state.registry_schemas import AgentState
from ..state.task_store import TaskStore
from ..state.wire_store import WireStore
from ..view import RunView
from .bridge import FrontendBridge

logger = logging.getLogger(__name__)

# Typed app keys (avoids aiohttp NotAppKeyWarning)
_bridge_key: web.AppKey[FrontendBridge] = web.AppKey("bridge")
_ws_clients_key: web.AppKey[set[web.WebSocketResponse]] = web.AppKey("ws_clients")
_workspace_key: web.AppKey[Optional[Path]] = web.AppKey("workspace_dir")


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket handler: send init snapshot, replay event log, then listen."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    bridge: FrontendBridge = request.app[_bridge_key]
    clients: set[web.WebSocketResponse] = request.app[_ws_clients_key]
    clients.add(ws)
    logger.info("WebSocket client connected (%d total)", len(clients))

    try:
        # Send init snapshot
        init_msg = await bridge.build_init_snapshot()
        await ws.send_str(init_msg)

        # Replay event log for reconnection
        for msg in bridge.event_log:
            await ws.send_str(msg)

        # Keep connection alive — listen for client messages (or close)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                # Client messages could be used for future features (e.g., filtering)
                logger.debug("Received client message: %s", msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning("WebSocket error: %s", ws.exception())
    finally:
        clients.discard(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(clients))

    return ws


def _build_file_tree(root: Path) -> list[dict]:
    """Recursively build a file tree structure for the workspace directory."""
    entries = []
    try:
        items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return entries
    for item in items:
        if item.name.startswith("."):
            continue
        if item.is_dir():
            children = _build_file_tree(item)
            entries.append({"name": item.name, "type": "dir", "children": children})
        else:
            entries.append({"name": item.name, "type": "file", "size": item.stat().st_size})
    return entries


async def _workspace_tree_handler(request: web.Request) -> web.Response:
    """Return JSON file tree of the workspace directory."""
    workspace: Optional[Path] = request.app[_workspace_key]
    if not workspace or not workspace.is_dir():
        return web.json_response({"tree": []})
    tree = _build_file_tree(workspace)
    return web.json_response({"tree": tree})


async def _workspace_file_handler(request: web.Request) -> web.Response:
    """Return contents of a file in the workspace directory."""
    workspace: Optional[Path] = request.app[_workspace_key]
    if not workspace:
        return web.json_response({"error": "No workspace configured"}, status=404)

    rel_path = request.query.get("path", "")
    if not rel_path:
        return web.json_response({"error": "Missing path parameter"}, status=400)

    # Resolve and validate path is within workspace (prevent traversal)
    target = (workspace / rel_path).resolve()
    if not target.is_relative_to(workspace.resolve()):
        return web.json_response({"error": "Path outside workspace"}, status=403)

    if not target.is_file():
        return web.json_response({"error": "File not found"}, status=404)

    # Read file (text with fallback for binary)
    try:
        content = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        return web.json_response({"error": "Binary file"}, status=422)

    # Cap at 500KB to avoid sending huge files
    if len(content) > 512_000:
        content = content[:512_000] + "\n\n... (truncated at 500KB)"

    return web.json_response({"path": rel_path, "content": content})


def create_app(bridge: FrontendBridge, workspace_dir: Optional[Path] = None) -> web.Application:
    """Create an aiohttp Application with /ws WebSocket endpoint.

    Also sets bridge.broadcast to send messages to all connected clients.
    Optionally serves static files from frontend/dist/ if the directory exists.
    """
    app = web.Application()
    app[_bridge_key] = bridge
    app[_ws_clients_key] = set()
    app[_workspace_key] = workspace_dir

    app.router.add_get("/ws", _ws_handler)

    # Workspace file browser API
    if workspace_dir:
        app.router.add_get("/api/workspace/tree", _workspace_tree_handler)
        app.router.add_get("/api/workspace/file", _workspace_file_handler)

    # Wire bridge.broadcast to push to all connected WS clients
    async def broadcast(message: str) -> None:
        await _broadcast_to_clients(app, message)

    bridge.broadcast = broadcast

    # Serve static frontend build if available (built by Vite into this directory)
    static_dir = Path(__file__).parent / "dist"
    if static_dir.is_dir():
        # Serve index.html for SPA routing, then static assets
        async def _index_handler(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(static_dir / "index.html")

        app.router.add_get("/", _index_handler)
        app.router.add_static("/assets", static_dir / "assets")
    else:
        logger.warning("No frontend build found. Dashboard will only serve WebSocket at /ws.")

    return app


async def _broadcast_to_clients(app: web.Application, message: str) -> None:
    """Send a message to all connected WebSocket clients."""
    clients: set[web.WebSocketResponse] = app[_ws_clients_key]
    if not clients:
        return

    # Send to all clients, removing any that are closed
    closed: list[web.WebSocketResponse] = []
    for ws in clients:
        if ws.closed:
            closed.append(ws)
            continue
        try:
            await ws.send_str(message)
        except Exception:
            logger.warning("Failed to send to WebSocket client, removing")
            closed.append(ws)

    for ws in closed:
        clients.discard(ws)


def _build_bridge(
    task_store: TaskStore,
    registry: Registry,
    wire_store: WireStore,
    org_name: str,
    initial_tasks: Optional[list[dict]] = None,
) -> FrontendBridge:
    """Build a FrontendBridge from stores."""
    roles_data = {}
    for name, role in registry.get_roles().items():
        roles_data[name] = {
            "role_prompt": role.role_prompt,
            "tools": role.tools,
            "model": role.model,
        }

    return FrontendBridge(
        task_store=task_store,
        registry=registry,
        wire_store=wire_store,
        org_name=org_name,
        roles=roles_data,
        initial_tasks=initial_tasks or [],
    )


async def _start_server(app: web.Application, port: int) -> web.AppRunner:
    """Start the aiohttp server and return the runner for cleanup."""
    host = "0.0.0.0" if Path("/.dockerenv").exists() else "127.0.0.1"
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Frontend server started on http://%s:%d", host, port)
    return runner


async def _shutdown_server(app: web.Application, app_runner: web.AppRunner) -> None:
    """Close all WebSocket connections and clean up the server."""
    for ws in list(app[_ws_clients_key]):
        await ws.close()
    app[_ws_clients_key].clear()
    await app_runner.cleanup()


async def serve_view_frontend(
    view: RunView,
    port: int = 8765,
) -> None:
    """Serve a read-only frontend dashboard for a completed run.

    Unlike run_with_frontend, this does not wire event/transcript listeners
    (nothing generates events in view mode). It replays transcripts from the
    DB and broadcasts run_finished from the stored results.

    Note: The caller is responsible for calling view.close() after this
    function returns or raises, typically via try/finally.
    """
    bridge = _build_bridge(view.task_store, view.registry, view.wire_store, view.org_name)
    workspace_dir = Path(view.working_directory) if view.working_directory else None
    app = create_app(bridge, workspace_dir=workspace_dir)

    # Replay transcripts and run_finished into event_log before starting
    # the server, so all connecting clients see the complete history.
    for entry in view.load_transcript_entries():
        bridge.serialize_transcript(entry["agent_id"], entry["entry_type"], entry["content"])
    bridge.serialize_run_finished(
        outcome=view.outcome,
        duration_seconds=view.duration_seconds or 0.0,
        total_cost_usd=view.total_cost_usd,
    )

    app_runner = await _start_server(app, port)

    try:
        logger.info("View-only dashboard serving at http://0.0.0.0:%d — press Ctrl+C to stop.", port)
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down")
        raise
    finally:
        await _shutdown_server(app, app_runner)


async def run_with_frontend(
    orchestrator: Orchestrator,
    org_config: OrgConfig,
    port: int = 8765,
    keep_alive: bool = True,
) -> None:
    """Run orchestrator with a live WebSocket dashboard.

    1. Creates FrontendBridge with orchestrator's stores
    2. Creates aiohttp app
    3. Wires event listeners to broadcast events
    4. Wires transcript listeners to broadcast transcript entries
    5. Starts the server
    6. Runs orchestrator.run() with the org_config's initial tasks
    7. After run completes, broadcasts run_finished and keeps server alive until Ctrl+C
    """

    # 1. Create bridge
    initial_tasks_data = [
        {"id": task.id, "title": task.title, "description": task.description, "assigned_to": assigned_to}
        for task, assigned_to, _ in org_config.initial_tasks
    ]
    bridge = _build_bridge(
        orchestrator.task_store,
        orchestrator.registry,
        orchestrator.wire_store,
        org_name=org_config.settings.org_name,
        initial_tasks=initial_tasks_data,
    )

    # 2. Create app (also sets bridge.broadcast)
    workspace_dir = Path(orchestrator.working_directory) if orchestrator.working_directory else None
    app = create_app(bridge, workspace_dir=workspace_dir)

    # 3. Wire orchestrator event listener (sync callback -> async broadcast)
    def on_event(event: Event) -> None:
        """Sync callback from orchestrator — schedule async broadcast."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        msg = bridge.serialize_event(event)
        loop.create_task(_broadcast_to_clients(app, msg))

        # Also broadcast updated task state for task events
        task_id = getattr(event, "task_id", None)
        if task_id:

            async def _broadcast_task(tid: str) -> None:
                task_msg = await bridge.serialize_task(tid)
                await _broadcast_to_clients(app, task_msg)

            loop.create_task(_broadcast_task(task_id))

    orchestrator.add_event_listener(on_event)

    # 3b. Wire agent state change listener
    def on_state_change(agent_id: str, state: AgentState, current_task_id: Optional[str]) -> None:
        """Sync callback from registry — schedule async broadcast."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        msg = bridge.serialize_agent_state_change(agent_id, state.value, current_task_id)
        loop.create_task(_broadcast_to_clients(app, msg))

    orchestrator.registry.add_state_listener(on_state_change)

    # 3c. Wire queue change listener
    def on_queue_change(agent_id: str, event_id: str, action: str, event: Optional[Event] = None) -> None:
        """Sync callback from registry — schedule async broadcast."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if action == "added" and event:
            msg = bridge.serialize_queue_event_added(agent_id, event)
            loop.create_task(_broadcast_to_clients(app, msg))
        elif action == "removed":
            msg = bridge.serialize_queue_event_removed(agent_id, event_id)
            loop.create_task(_broadcast_to_clients(app, msg))

    orchestrator.registry.add_queue_listener(on_queue_change)

    # 4. Wire transcript listener (sync callback -> async broadcast)
    def on_transcript(agent_id: str, entry_type: str, content: str) -> None:
        """Sync callback from transcript logger — schedule async broadcast."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        msg = bridge.serialize_transcript(agent_id, entry_type, content)
        loop.create_task(_broadcast_to_clients(app, msg))

    orchestrator.runner.transcript_logger.add_listener(on_transcript)

    # 4b. Wire message listener (sync callback from WireStore -> async broadcast)
    def on_wire_message(wire_id: str, participants: frozenset[str], sender: str, body: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        msg = bridge.serialize_wire_message(wire_id, participants, sender, body)
        loop.create_task(_broadcast_to_clients(app, msg))

    orchestrator.wire_store.add_message_listener(on_wire_message)

    # 5. Pre-populate event_log with transcript entries from prior sessions.
    # On fresh runs this is a no-op (empty list). On resume, it replays old
    # transcripts so connecting clients see the full history.
    for entry in orchestrator.load_transcript_entries():
        bridge.serialize_transcript(entry["agent_id"], entry["entry_type"], entry["content"])

    # 6. Start server
    app_runner = await _start_server(app, port)

    try:
        # 7. Run orchestrator
        await orchestrator.run(
            initial_tasks=org_config.initial_tasks,
            initial_messages=org_config.initial_messages,
            sync=org_config.settings.sync,
            sync_max_rounds=org_config.settings.sync_max_rounds,
            sync_round_timeout_seconds=org_config.settings.sync_round_timeout_seconds,
        )

        # 8. Broadcast run_finished
        finished_msg = bridge.serialize_run_finished(
            outcome=orchestrator.outcome,
            duration_seconds=orchestrator.duration_seconds or 0.0,
            total_cost_usd=orchestrator.total_cost_usd,
        )
        await _broadcast_to_clients(app, finished_msg)

        if keep_alive:
            logger.info("Run complete. Dashboard still serving at http://0.0.0.0:%d — press Ctrl+C to stop.", port)
        else:
            logger.info("Run complete. Shutting down frontend server.")
            return

        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down")
        raise
    finally:
        await _shutdown_server(app, app_runner)

# frontend/

Python backend for the live dashboard — a WebSocket server that streams orchestrator state to a React SPA.

| File | What it does |
|------|-------------|
| `server.py` | aiohttp WebSocket server, static file serving, workspace file browser API |
| `bridge.py` | `FrontendBridge` — serializes store state and events into JSON for the frontend |

---

## Architecture

The frontend has two halves:

- **Python backend** (this package) — an aiohttp server that exposes a `/ws` WebSocket endpoint and optional REST APIs for workspace file browsing. Listens to store events and transcript entries, serializes them via `FrontendBridge`, and broadcasts JSON messages to all connected browser clients.
- **React SPA** (`frontend/` at the repo root) — a TypeScript/React app built with Vite. Connects to `/ws`, renders the dashboard. Built artifacts are served as static files by the Python server.

The Python backend is the glue between the orchestrator's event system and the browser.

## Two modes

- **Live mode** (`run_with_frontend`) — Wires listeners onto all stores (task, wire, registry) and the transcript logger. Events are broadcast to connected clients in real time as the org runs. After the run completes, the server stays alive (configurable via `keep_alive`) so you can inspect the final state.
- **View mode** (`serve_view_frontend`) — Opens a read-only dashboard for a completed run. Replays transcript entries from the DB into the event log before starting the server, so connecting clients see the full history. No live listeners are registered (nothing generates events).

## FrontendBridge

The bridge is responsible for all serialization between Python store objects and the JSON the frontend expects. It maintains an `event_log` — a list of serialized messages that are replayed to new WebSocket clients on connection, so late-joining or reconnecting clients see the full history.

### Message types

| Type | When it's sent |
|------|---------------|
| `init` | On WebSocket connect — full snapshot of agents, tasks, wires, network, queues, roles |
| `event_dispatched` | When the orchestrator dispatches a store event |
| `task_changed` | After a task-related event, with the task's updated state |
| `agent_state_changed` | When an agent's lifecycle state changes (idle → working, etc.) |
| `queue_event_added` / `queue_event_removed` | When events enter or leave an agent's queue |
| `transcript_entry` | Agent conversation log entries (prompts, responses, tool calls) |
| `wire_message` | New wire message (not added to event_log — init snapshot has full history) |
| `run_finished` | Run completed, with outcome, duration, and total cost |

## Workspace file browser

When a `workspace_dir` is provided, the server exposes two REST endpoints:

- `GET /api/workspace/tree` — returns a JSON file tree of the workspace
- `GET /api/workspace/file?path=<relative_path>` — returns file contents (text only, capped at 500KB, with path traversal protection)

## Static file serving

The server looks for a built frontend in two locations:
1. `frontend/dist/` relative to this package (for packaged installs)
2. `frontend/dist/` at the project root (for development)

If found, it serves `index.html` at `/` and static assets at `/assets`. If not found, the server still works — it just serves the WebSocket endpoint without a UI.

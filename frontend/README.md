# magelab frontend

Real-time dashboard for monitoring magelab orchestration runs. Built with React, TypeScript, Zustand, and Tailwind CSS.

## Tabs

- **Agents** — per-agent status cards showing state, current task, tools, and a live transcript of LLM turns
- **Tasks** — task list with status badges, assignment history, and review records
- **Workspace** — file tree browser for the run's working directory
- **Wires** — inter-agent message channels with chat-style UI
- **Network** — interactive graph (React Flow + ELK layout) showing agent connectivity

## Architecture

```
ws.ts            WebSocket client, reconnects on disconnect
  -> store.ts    Zustand store — single source of truth for all UI state
  -> components  React components read from the store via selectors
```

The Python backend pushes state over a single WebSocket connection (`/ws`). The frontend never polls — all updates are server-initiated. On connect, the server sends a full `init` snapshot; subsequent messages are incremental deltas (`agent_state_changed`, `task_changed`, `transcript_entry`, `wire_message`, etc.).

Types in `src/types.ts` mirror the Python schemas in `magelab.state`.

## Development

```bash
npm install
npm run dev
```

The Vite dev server proxies `/ws` to `ws://localhost:8765` (the Python backend's default port). Start the backend first, then the frontend.

## Production build

```bash
npm run build
```

Output goes to `../src/magelab/frontend/dist/` so it's bundled inside the Python package. The Python frontend server serves these static files automatically.

## UI components

Shadcn UI components live in `src/components/ui/`. The theme is dark-only, defined in `src/index.css` with CSS custom properties.

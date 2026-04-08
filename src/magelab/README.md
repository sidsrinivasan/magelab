# magelab

A framework for building and running multi-agent organizations — groups of LLM agents that collaborate on tasks through structured workflows.

## How it works

An organization is defined in a YAML config: roles, agents, their tools, network topology, and initial tasks. The framework parses this into an `OrgConfig`, builds the runtime components, and runs the organization to completion.

```
YAML config → OrgConfig → Orchestrator.build() → Orchestrator.run()
```

At runtime, the system is event-driven:

```
┌─────────────────────────────────────────────────────────┐
│                    Orchestrator                         │
│                                                         │
│   Stores ──emit──→ _dispatch_event ──enqueue──→ Agent   │
│   (TaskStore,         │                        Queues   │
│    WireStore)         ├─ log to DB             (in      │
│       ↑               └─ fan-out to listeners  Registry)│
│       │                                          │      │
│       │                                       dequeue   │
│       │                                          ↓      │
│       │              ┌─────────────────────────────┐    │
│       │              │ _run_agent_for_event        │    │
│       │              │  1. check staleness         │    │
│       │              │  2. resolve prompt          │    │
│       │              │  3. mark agent WORKING      │    │
│       │              │  4. run LLM (via Runner)    │    │
│       │              │  5. handle outcome          │    │
│       │              │  6. mark agent IDLE         │    │
│       │              └─────────────────────────────┘    │
│       │                       │                         │
│       └───── tool calls ──────┘                         │
│              mutate stores                              │
│              → new events                               │
└─────────────────────────────────────────────────────────┘
```

The cycle continues until all agents are quiescent (idle with empty queues) or a timeout is reached.

## Top-level files

| File | What it does |
|------|-------------|
| `orchestrator.py` | Runtime engine — event routing, agent dispatch, completion detection |
| `org_config.py` | `OrgConfig`, `OrgSettings`, `ResumeMode`, `WireNotifications` |
| `registry_config.py` | Structural DTOs: `RoleConfig`, `AgentConfig`, `NetworkConfig` |
| `auth.py` | Authentication resolution — `resolve_sub()`, `resolve_api_key()`, credential staging |
| `events.py` | Event types that flow between stores and agents |
| `view.py` | `RunView` — read-only inspection of completed runs |
| `__main__.py` | CLI entry point (`uv run magelab config.yaml`) |
| `__init__.py` | Public API: `OrgConfig`, `run_pipeline`, `run_pipeline_batch`, `RunOutcome`, `RunView`, etc. |

## Subpackages

| Package | What it covers |
|---------|---------------|
| [`state/`](state/README.md) | Task lifecycle, agent registry, network topology, wire conversations, database, hydration |
| [`tools/`](tools/README.md) | Tool definitions, bundles, implementations, validation, MCP modules |
| [`runners/`](runners/README.md) | LLM integration — AgentRunner ABC, ClaudeRunner, prompt system |
| [`pipeline/`](pipeline/README.md) | Stage-based execution, batch runs, Docker helpers, terminal display |
| [`frontend/`](frontend/README.md) | WebSocket server and event bridge for the React dashboard |

<p align="center"><a href="#orchestrator">Orchestrator</a> | <a href="#configuration">Configuration</a> | <a href="#events">Events</a> | <a href="#runview">RunView</a> | <a href="#cli">CLI</a></p>

---

## Orchestrator

The orchestrator is the runtime core. It wires stores to agent queues, dispatches events, runs agents, and detects completion.

### Construction

`Orchestrator.build()` is an async factory that creates all components in this order:

1. Open (or create) the SQLite database; record a new run segment in `run_meta`
2. Create `Registry`, upsert structural state (roles, agents, network) from config
3. Load settings from DB (`load_settings_from_db`) — needed before constructing WireStore
4. Load stores from DB — `registry.load_from_db()`, `task_store.load_from_db()`, `wire_store.load_from_db()`
5. Wire transcript logging, load MCP modules, copy agent settings into session directories
6. Build the `ClaudeRunner` with per-agent configs
7. If resuming: restore session IDs, apply resume logic (`resume_fresh` or `resume_continue`)
8. Instantiate the `Orchestrator`, which wires store event listeners to `_dispatch_event`
9. Initialize MCP server `init()` hooks (gives them DB access and event emission)

### Async mode (default)

All agents run concurrently as persistent asyncio tasks, each pulling events from their own queue. The org finishes when everyone is idle with nothing left to do.

```
run() → _run_with_lifecycle          # validate, create tasks/messages, timeout wrapper
          └─ _async_work
              ├─ _agent_loop         # one per agent, pulls from queue
              │   └─ _run_agent_for_event   # resolve prompt, run LLM, handle outcome
              └─ _wait_for_completion       # poll for quiescence
```

**Lifecycle wrapper** (`_run_with_lifecycle`). Shared by both modes:
- Validates config (tool dependencies, task assignments)
- Creates initial tasks → `TaskAssignedEvent`s enqueued to agent queues
- Sends initial messages → `WireMessageEvent`s enqueued
- Runs the mode-specific work function under a global timeout (`org_timeout_seconds`)
- On timeout: sets `timed_out = True`. On Ctrl+C: sets `interrupted = True`, re-raises.
- Finally: `_shutdown()` → `_finalize_db()`

Initial tasks and messages are idempotent on resume — if they already exist in the DB, they are skipped.

**Agent loops** (`_agent_loop`). One persistent asyncio.Task per agent, all running concurrently. Each agent blocks on its queue, dequeues one event at a time, processes it via `_run_agent_for_event`, and loops until the orchestrator sets `_running=False`.
- The 1-second dequeue timeout ensures agents check the shutdown flag at least once per second.

**Quiescence** (`_wait_for_completion`). A monitor that polls every 0.5s: are all agents IDLE with empty queues and no events in flight? If so, the org is done.
- An `_events_to_process` counter prevents false quiescence during the window between dequeue (queue now empty) and `mark_working` (agent state updated).

### Sync mode

The orchestrator drives discrete rounds instead of running persistent agent loops. In each round, all agent queues are drained and events are processed — sequential within an agent, concurrent across agents. The org terminates when a full round produces no new events (convergence) or `sync_max_rounds` is reached.

```
run() → _run_with_lifecycle          # same wrapper as async
          └─ _run_sync_rounds        # drive discrete rounds
              └─ per round:
                  drain queues → _run_agent_events_sequential  # one list per agent
                                  └─ _run_agent_for_event      # same as async
```

**Rounds** (`_run_sync_rounds`). Each round drains all agent queues, then processes the events via `asyncio.gather` — all agents run concurrently, but each agent processes its events sequentially. An optional per-round timeout can be set via `sync_round_timeout_seconds`.

### Event dispatch

When a store mutation produces an event (e.g., TaskStore emits `TaskAssignedEvent` after assignment), the orchestrator's `_dispatch_event` listener fires:

1. Log the event to the DB (outcome = NULL)
2. Enqueue to the target agent's queue via `registry.enqueue()` — events for unknown or terminated agents are dropped (recorded as `DROPPED_AT_ENQUEUE`)
3. Fan-out to external listeners (e.g., the frontend WebSocket server)

### _run_agent_for_event

The core function at the bottom of both call stacks. Called for every event an agent processes:

```
_run_agent_for_event(agent_id, event):
 1. Guard — agent must exist in registry
 2. Resolve prompt:
    ├─ MCPEvent        → event.payload (verbatim, no staleness check)
    ├─ WireMessageEvent → _resolve_wire_prompt() — batches ALL unread wires
    └─ Task events     → _resolve_task_prompt() — staleness check + task lookup
 3. If stale (resolver returns None) → mark STALE_AT_DELIVERY, skip (not applicable to MCPEvent)
 4. Mark event DELIVERED
 5. Build system prompt (working dir + org prompt + role prompt)
 6. Mark agent WORKING or REVIEWING
 7. Mark wire messages as read (before LLM call, so tool notifications don't re-report)
 8. Run LLM via runner.run_agent()
 9. Record results (session ID, cost, turns, duration)
10. If error → _handle_task_failure()
11. Mark agent IDLE
```

**Exception paths:**
- **CancelledError** (timeout, Ctrl+C): If a session exists → agent stays WORKING (resumable on next run). If no session → task is failed, agent marked IDLE. Always re-raises.
- **Other exceptions**: Task is failed, agent marked IDLE. Does not re-raise (loop continues).

**Failure asymmetry**: Worker failure is terminal (task force-failed). Reviewer failure is recoverable (only that review is marked failed, round continues with other reviewers).

### Staleness

Events can go stale between queueing and processing (e.g., a task gets reassigned, or wire messages get read via tool notifications). The orchestrator checks staleness before running the agent and skips stale events.

| Event | Stale if |
|-------|----------|
| `TaskAssignedEvent` | Task is finished, in review, reassigned, or not in ASSIGNED status |
| `ReviewRequestedEvent` | Task is not in review, or this reviewer's review is no longer pending |
| `ReviewFinishedEvent` | Task status is not one of the expected post-review states (e.g., already finished, still in review, or reassigned) |
| `TaskFinishedEvent` | Task is not finished |
| `ResumeEvent` | Task (if any) is finished |
| `WireMessageEvent` | Agent's read cursor has advanced past the event's `message_cursor` (≥) |

### Wire prompt batching

When a wire event triggers an agent, the orchestrator doesn't just process that one wire — it fetches *all* unread wires for the agent and batches them into a single prompt. This means the first wire event delivers everything pending, and subsequent queued wire events naturally go stale (nothing left unread). One LLM call serves all unread messages.

### Shutdown and completion

| Trigger | What happens |
|---------|-------------|
| **Quiescence** (async) | All agents idle, queues empty, no events in flight → natural exit |
| **Convergence** (sync) | A full round produces no events → return |
| **`sync_max_rounds`** (sync) | Maximum rounds reached → return |
| **Timeout** | `asyncio.wait_for` raises `TimeoutError` → interrupt all agents immediately |
| **Ctrl+C / SIGTERM** | `CancelledError` → interrupt all agents immediately |

`_shutdown()` sequence:
1. If `interrupt_immediately`: send interrupt signal to all active LLM calls
2. (Async mode) Wait up to 10s for agent loop tasks to exit (they see `_running=False`)
3. Force-cancel anything still pending after the grace period

`_finalize_db()` then aggregates run stats (costs, task counts, error counts), computes the `RunOutcome`, writes everything to `run_meta`, and closes the DB.

**`RunOutcome`** is computed from task counts: `SUCCESS` (all succeeded), `PARTIAL` (some succeeded), `FAILURE` (none succeeded), `TIMEOUT` (timed out), `NO_WORK` (no tasks created). Note: `TIMEOUT` takes priority — even if all tasks succeeded before the timeout fired, the outcome is still `TIMEOUT`.

---

## Configuration

Configuration is split across two files:

- **`registry_config.py`** — Structural DTOs that describe what the org looks like: `RoleConfig`, `AgentConfig`, `NetworkConfig`. These are consumed by the Registry to build runtime state.
- **`org_config.py`** — Composes structural configs with behavioral settings and run inputs into the complete `OrgConfig`.

### OrgConfig

```
OrgConfig
├── roles: dict[str, RoleConfig]       — templates for agent types
├── agents: dict[str, AgentConfig]     — individual agent instances
├── network: Optional[NetworkConfig]   — who can talk to whom
├── settings: OrgSettings              — all behavioral settings (see below)
├── initial_tasks                      — starting work items
├── initial_messages                   — wire messages sent at startup
└── resume_mode                        — how to resume from a prior run
```

### OrgSettings

All non-structural settings live in `OrgSettings`, composed as `settings` within `OrgConfig`:

| Field | Default | Purpose |
|-------|---------|---------|
| `org_name` | `"magelab"` | Run name (used for DB filename, logging) |
| `org_description` | `""` | Human-readable description of this config variant (metadata only) |
| `org_prompt` | `""` | Org-level prompt prepended to every agent's role prompt (supports `{agent_id}` placeholder) |
| `org_permission_mode` | `"acceptEdits"` | SDK permission mode for tool calls |
| `org_timeout_seconds` | `3600.0` | Global run wall-clock timeout |
| `agent_timeout_seconds` | `900.0` | Per-agent run timeout |
| `agent_settings_dir` | `None` | Path to per-role backend settings (relative to config YAML) |
| `wire_notifications` | `"all"` | Wire notification mode: `"all"`, `"tool"`, `"event"`, or `"none"` |
| `wire_max_unread_per_prompt` | `10` | Max unread wires delivered in a single wire event prompt |
| `mcp_modules` | `{}` | In-process MCP servers: `name → module_path` |
| `sync` | `False` | If True, run in synchronized round-based mode |
| `sync_max_rounds` | `None` | Required when `sync=True` |
| `sync_round_timeout_seconds` | `None` | Per-round timeout (sync mode only) |

### Role → Agent override pattern

Roles define defaults (model, prompt, tools, max_turns, session_config). Agents reference a role and can override any field. This keeps configs DRY — 10 coders sharing the same role only need one role definition, with per-agent overrides where needed.

Tool lists in roles support bundle names (e.g., `[management, claude_basic]`), which are expanded at `RoleConfig.__post_init__` time. They also support `mcp__<server>` and `mcp__<server>__<tool>` references for in-process MCP servers.

### Extensibility

Each agent runs as its own Claude Code instance with a scoped `CLAUDE_CONFIG_DIR` (set to `output_dir/.sessions/<agent_id>/`). This means every Claude Code configuration mechanism — [settings, permissions, hooks, MCP servers, plugins, skills, subagents, CLAUDE.md files](https://code.claude.com/docs/en/settings) — can be configured per-agent. Anything you can do with Claude Code, you can give to an agent.

**Agent settings** is the mechanism for delivering these configs. Set `agent_settings_dir` in `OrgSettings` and create a subdirectory per role containing whatever Claude Code config files you want agents of that role to have. The framework copies the appropriate subdirectory into each agent's session directory before the run.

```yaml
agent_settings_dir: "agent_settings"
roles:
  coder:
    session_config: "coder/"
  reviewer:
    session_config: "reviewer/"
agents:
  coder_2:
    session_config_override: "coder_strict/"
```

```
agent_settings/
├── coder/
│   ├── settings.json       # permissions, hooks, env vars
│   ├── .mcp.json           # external MCP servers (Slack, GitHub, etc.)
│   ├── agents/             # custom subagents
│   ├── skills/             # custom skills
│   │   └── safety-review/
│   │       └── SKILL.md
│   └── CLAUDE.md           # agent-scoped instructions
└── reviewer/
    └── settings.json
```

**MCP modules** add shared in-process tools. See [`tools/README.md`](tools/README.md#mcp-modules) for details.

### Validation

`OrgConfig.__post_init__` runs comprehensive validation:
- Role/agent key consistency (keys must match `.name`/`.agent_id`)
- Agent roles reference valid role names
- Network ↔ agent bidirectional consistency
- Initial task IDs are unique and assigned to existing agents
- Initial message participants are registered agents
- Value constraints (timeouts > 0, max_turns > 0, etc.)

---

## Events

Events (`events.py`) are thin dataclasses that carry a `target_id` and minimal immutable data. They are emitted by stores and routed to agents by the orchestrator.

| Event | Emitted by | Target | Key fields |
|-------|-----------|--------|------------|
| `TaskAssignedEvent` | TaskStore | The assignee | `task_id`, `source_id` |
| `ReviewRequestedEvent` | TaskStore | Each reviewer | `task_id`, `source_id`, `request_message` |
| `ReviewFinishedEvent` | TaskStore | The worker | `task_id`, `outcome`, `review_records` |
| `TaskFinishedEvent` | TaskStore | The delegator | `task_id`, `outcome`, `details` |
| `WireMessageEvent` | WireStore | Each participant (except sender) | `wire_id`, `source_id`, `message_cursor` |
| `ResumeEvent` | Hydration | The interrupted agent | `task_id`, `was_reviewing` |
| `MCPEvent` | MCP server | Specified by server | `server_name`, `payload` |

`Event` is a type alias (union) of all event types. `BaseEvent` provides common fields (`event_id`, `target_id`, `timestamp`).

### EventOutcome

Each event's lifecycle is tracked in the `run_events` DB table:

| Outcome | Meaning |
|---------|---------|
| `NULL` | Enqueued, not yet delivered |
| `DELIVERED` | Agent ran for this event |
| `COMPLETED` | Event fully processed (reserved; not currently set by the orchestrator) |
| `STALE_AT_DELIVERY` | Staleness check failed, agent skipped |
| `ERROR_AT_DELIVERY` | Task lookup or prompt resolution failed |
| `DROPPED_AT_ENQUEUE` | Target agent unknown or terminated |
| `DROPPED_ON_RESTART` | Dropped during resume-fresh |

---

## RunView

`RunView` (`view.py`) is a lightweight, read-only container for inspecting completed runs. It constructs stores from the DB without building a runner, MCP servers, or session configs — just enough to display results.

```python
view = RunView.from_db(Path("output/myorg.db"))
try:
    transcripts = view.load_transcript_entries()
    print(f"Outcome: {view.outcome}, Cost: ${view.total_cost_usd:.2f}")
finally:
    view.close()
```

Fields: `task_store`, `wire_store`, `registry`, `db`, `outcome`, `duration_seconds`, `total_cost_usd`, `org_name`, `working_directory`.

Used by `view_run()` / `view_run_batch()` in the pipeline to serve read-only frontend dashboards.

---

## CLI

`__main__.py` provides the entry point (`uv run magelab`). It's a thin layer over `pipeline.run_pipeline` / `run_pipeline_batch` / `view_run` / `view_run_batch` — it parses args and delegates.

Three modes of operation:

- **Fresh run** — Requires a config file. Calls `run_pipeline` (single) or `run_pipeline_batch` (when `--runs > 1`). Output directory defaults to `{name}/{timestamp}/`.
- **Resume** — Requires `--output-dir`. Config is optional — if omitted, reconstructs one from the DB via `reconstruct_org_config_from_db()` and writes it to `configs/{N}_resume.yaml`. Calls `run_pipeline` with `resume_mode`.
- **View** — Read-only frontend. `--view` calls `view_run` with a single DB path. `--view-batch` globs for the DB name in subdirectories of `--output-dir` and calls `view_run_batch`.

Exit code is the maximum `RunOutcome.exit_code` across all runs: 0 (success/no_work), 1 (partial), 2 (timeout), 3 (failure).

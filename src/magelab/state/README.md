# state/

Runtime state for the multi-agent organization.

## Overview

Three stores hold all org state in memory, with every mutation written through to SQLite. Structural state describes what the organization looks like (roles, agents, network topology) and is defined by config. Operational state tracks what is happening during a run (task progress, agent lifecycle, messages).

| Store | Schemas | State |
|-------|---------|-------|
| **Registry** | `registry_schemas.py` | Structural (roles, agents, network) + operational (lifecycle, queues) |
| **TaskStore** | `task_schemas.py` | Operational (task lifecycle, reviews) |
| **WireStore** | `wire_schemas.py` | Operational (conversations, messages, read cursors) |

TaskStore and WireStore are pure state containers — they hold operational state, enforce domain rules, and emit events when things change.

The Registry is different in two ways:
- It also holds **structural state** — the role definitions, agent configurations, and network topology that describe what the organization looks like.
- It owns the **per-agent event queues** — the delivery mechanism for all store events. TaskStore and WireStore don't know how events reach agents; they just emit. The orchestrator listens to the stores and routes events into the Registry's queues.

Each domain follows a **schemas + store** split. The `_schemas.py` file defines pure data types (Pydantic models, dataclasses, enums) with no DB operations. The store file owns all mutations, persistence, and event emission.

**Infrastructure** supports the stores:

| Module | File | Purpose |
|--------|------|---------|
| **Database** | `database.py` | SQLite connection manager and framework tables (`run_meta`, `run_events`, `run_transcripts`) |
| **Hydration** | `database_hydration.py` | Reconstruct in-memory state from the DB; resume logic |
| **TranscriptLogger** | `transcript.py` | Agent conversation logging (file + DB listeners) |

<p align="center"><a href="#registry">Registry</a> | <a href="#tasks">Tasks</a> | <a href="#wires">Wires</a> | <a href="#database">Database</a> | <a href="#shared-patterns">Shared patterns</a></p>

---

## Registry

The Registry (`registry.py`) manages organizational structure and agent lifecycle. It is the bridge between configuration (what the org looks like) and runtime (what agents are doing).

Three config types enter the Registry via `register_config()`, and each is handled differently:

### Roles

**RoleConfig** is stored as-is — the Registry persists it to the `agent_roles` table and serves it back unchanged. Roles are templates (model, prompt, tools, max_turns) that define defaults agents inherit.

### Agents

**AgentConfig** (a DTO with a role reference and optional overrides) is resolved and transformed into an **AgentInstance** — a mutable dataclass that holds both the resolved structural fields and operational state:

- **Structural** (from config): `role`, `model`, `role_prompt`, `tools`, `max_turns` — resolved by applying per-agent overrides on top of the role defaults.
- **Operational** (runtime): `state` (lifecycle), `queue` (asyncio.Queue for events), `current_task_id`, `last_active_at`.

Note: session IDs are persisted in the DB (`agent_instances.session_id`) but are not fields on `AgentInstance` — they are accessed through the Registry's `update_session()` and `get_session_ids()` methods, which query the DB directly.

External code never sees the mutable `AgentInstance`. All queries return a read-only **AgentSnapshot** (frozen dataclass) via `get_agent_snapshot()`.

**Lifecycle.** Agents move through: `IDLE` (waiting) → `WORKING` or `REVIEWING` (processing an event) → back to `IDLE`. `TERMINATED` is permanent. The orchestrator drives these transitions as it dispatches events.

**Event routing.** Each AgentInstance has an `asyncio.Queue`. Events reach these queues through a listener chain:

1. **Stores emit events.** When a store mutation produces an event (e.g., TaskStore emits a `TaskAssignedEvent` after assignment), it notifies all registered listeners.
2. **The orchestrator listens.** During construction, the orchestrator registers itself as a listener on the stores. This is the wiring that connects store mutations to agent queues.
3. **The orchestrator enqueues.** When notified, the orchestrator logs the event to the DB, then places it in the target agent's queue via the Registry. Events for unknown or terminated agents are dropped (recorded in the DB as `DROPPED_AT_ENQUEUE`).

The Registry itself doesn't listen to stores — it just owns the queues. The orchestrator is the glue.

Agents consume from their queues in two modes: async (await with timeout, for concurrent execution) or sync (batch drain, for round-based execution).

**Persistence.** On upsert (`register_config`), structural fields are overwritten from config while operational fields (state, current_task_id, session_id) are preserved in the DB. Agents in the DB but not in the new config survive in the DB (their rows are untouched). Agents in config but not in the DB are created fresh with default operational state. `register_config()` builds in-memory state from config only — operational state from the DB is not available until `load_from_db()` is called afterward. This two-step pattern (register then load) is how structural state from config gets merged with operational state from the DB.

### Network

**NetworkConfig** (a DTO specifying groups and connections) is transformed into a **NetworkInstance** — a mutable object that holds the topology and supports runtime changes.

The network supports two complementary mechanisms:

- **Groups** — Named sets where every member can reach every other. Useful for dense cliques (e.g., a team where everyone collaborates). An agent can belong to multiple groups.
- **Connections** — Explicit pairwise links, always symmetric (A→B implies B→A). Useful for sparser structure (e.g., a PM connected to each team lead, but team leads not connected to each other).

An agent's reachable set is the union of group co-members + explicit connections. This supports flat teams (one big group), hub-and-spoke, layered hierarchies, or any combination. Without a network config, all agents are fully connected.

The NetworkInstance allows runtime mutation via `add_to_group()` and `add_connection()`, with the Registry wrapping these with DB writes. Connectivity queries (`is_connected()`, `get_connected_ids()`) delegate to the NetworkInstance.

**Persistence.** Unlike roles and agents (which use upsert), network uses **wipe-and-rewrite** — `register_config()` deletes all existing edges/groups and writes the config's topology fresh. This is because the YAML network section is the complete topology spec: edges absent from config should not survive a re-register.

---

## Tasks

Tasks are the primary unit of work. A task is created, assigned to an agent, worked on, optionally reviewed, and eventually finished. The **schemas** (`task_schemas.py`) define what a task *is*; the **store** (`task_store.py`) controls how tasks *change*.

### Lifecycle

```
CREATED → ASSIGNED → IN_PROGRESS ──────────────────→ SUCCEEDED
                         │                               ↑
                         ↓                               │
                     UNDER_REVIEW ──→ APPROVED ──────────┘
                         ↑       ├──→ CHANGES_REQUESTED
                         │       └──→ REVIEW_FAILED
                         │               │
                         └───────────────┘
                     (all three can re-submit for review)

                     (any non-terminal state) ──→ FAILED
```

Key transitions:
- `SUCCEEDED` can only be reached from `IN_PROGRESS` or `APPROVED`. If `review_required=True`, the `IN_PROGRESS` path is blocked — the task *must* pass through review.
- After receiving `CHANGES_REQUESTED` or `REVIEW_FAILED`, the worker stays in that post-review state and can re-submit for review directly — it does not revert to `IN_PROGRESS`.
- `SUCCEEDED` and `FAILED` are terminal — no further transitions.

### Assignment

Assignment history is a flat list — `[PM, Coder1, Coder2]` means PM created the task, assigned to Coder1, who handed off to Coder2. `assigned_to` and `assigned_by` are derived from the last two entries, not stored separately.

### Reviews

Reviews work in rounds. A worker submits for review, specifying a set of reviewers and an approval policy (`ANY_APPROVE`, `MAJORITY_APPROVE`, or `ALL_APPROVE`). Each reviewer independently responds with `APPROVED` or `CHANGES_REQUESTED`. Once all have responded, the policy determines the round outcome:
- If the threshold is met → `APPROVED`
- If not → `CHANGES_REQUESTED`
- If all reviewers crashed → `REVIEW_FAILED` (crashed reviewers are excluded from the quorum)

Round completion is automatic: each call to `submit_review()` or `mark_review_failed()` checks if all reviewers have responded, and if so evaluates the policy and emits a `ReviewFinishedEvent` back to the worker.

### Store behavior

The TaskStore enforces the state machine, validates every transition, and emits the appropriate events:

- **Force-fail.** `mark_finished(force=True)` lets the framework's crash handler push a task to `FAILED` even if it's under review, cancelling the moot review round.
- **Staleness checking.** `is_event_stale()` lets the orchestrator skip events that no longer match current state (e.g., a task was reassigned between queueing and processing).

`Task` is intentionally a "dumb" data container — its methods handle internal bookkeeping (archiving review data after a round completes) but never call `update_status()`. Status transitions are always driven by the TaskStore, keeping mutation authority in one place.

---

## Wires

Wires are magelab's inter-agent communication infrastructure — persistent, append-only conversation threads between fixed sets of agents. They provide free-form messaging outside the formal task/review workflow. The **WireStore** (`wire_store.py`) manages wire state.

### Core model

- **One wire per participant set.** Messages between {A, B, C} always route to the same wire. No topic-specific threads — the participant set scopes the context.
- **Fixed participants.** No join/leave on existing wires. Different set of people = new wire.
- **Cursor-based read tracking.** Each participant has a read cursor (index of first unread) that only advances forward. `format_conversation()` produces a bounded window of `max_messages` total (context + unread share the budget), with overflow notices when unread messages extend beyond the window.
- **Wire creation requires at least one message.** You cannot create an empty wire — the `Wire` model enforces this with a validator.

### Notification paths

Two ways agents learn about new messages (controlled by `WireNotifications` in org settings):
- **Event notifications** — Emit `WireMessageEvent` into agent queues, waking idle agents promptly. If the agent already read the messages (e.g., via tool notifications or explicit `read_messages` calls), the event is detected as stale and skipped.
- **Tool notifications** — Append an unread summary to every tool response, so busy agents notice messages mid-task.

Both can be active simultaneously. Setting `WireNotifications.NONE` disables both, meaning agents only see wire messages when they explicitly call `read_messages`.

---

## Database

The **Database** class (`database.py`) is a SQLite connection manager. It owns framework-level tables and exposes primitives that stores use for their own persistence.

### Store integration

Every store registers its own tables at construction time via `register_schema(ddl)`. DDL must use `CREATE TABLE IF NOT EXISTS` for idempotency. The Database validates this and rejects DDL without it.

Once registered, stores use the connection primitives for all reads and writes:

| Method | Purpose |
|--------|---------|
| `execute(sql, params)` | Run a SQL statement, return cursor |
| `fetchone(sql, params)` | Query, return first row as dict (or None) |
| `fetchall(sql, params)` | Query, return all rows as dicts |
| `commit()` | Commit if not inside a `transaction()` block (no-op in autocommit) |
| `transaction()` | Context manager for multi-statement atomicity (`BEGIN`/`COMMIT`/`ROLLBACK`) |

Stores always call `commit()` after mutations so the code works correctly whether autocommit is on or off. In autocommit mode (the default), each statement commits immediately and the explicit `commit()` is a no-op. For multi-statement atomicity (e.g., creating a wire with its initial message and cursors), use `transaction()` — it issues `BEGIN`/`COMMIT` and suppresses individual `commit()` calls inside the block. Note: `transaction()` is not re-entrant — nesting it raises `RuntimeError`.

### Framework tables

The Database owns three tables for framework-level bookkeeping:

- **`run_meta`** — One row per run segment (timing, outcome, costs, full OrgConfig JSON). Each fresh run or resume appends a new row.
- **`run_events`** — Event lifecycle: inserted at enqueue time (outcome=NULL), updated when delivered and completed. Tracks per-event cost, turns, duration, and errors.
- **`run_transcripts`** — Agent conversation logs (prompt, response, tool calls) with per-agent turn numbering.

Store-owned tables: `agent_roles`, `agent_instances` (Registry), `task_items` (TaskStore), `wire_meta`, `wire_messages`, `wire_read_cursors` (WireStore), `network_edges`, `network_groups` (Registry/Network).

### Run finalization

At the end of a run, the orchestrator calls `compute_run_summary()` to aggregate costs and error counts from `run_events`, and `task_store.compute_task_counts()` to count tasks by terminal status. These are combined and written to `run_meta` via `finalize_run()`, then the connection is closed.

### Hydration

**Hydration** (`database_hydration.py`) reconstructs in-memory state from the SQLite database. Called by `Orchestrator.build()` on resume, and by `RunView.from_db()` for read-only access.

The DB is the sole source of truth. On resume, everything is reconstructed from the DB — no YAML config is needed for structure. Each store follows the same pattern: construct with `db`, call `load_from_db()` to populate from DB rows.

**Config reconstruction.** `reconstruct_org_config_from_db()` reads DB state into a serializable OrgConfig (structure from registry tables + settings from run_meta JSON). Used by the pipeline for config snapshots between stages.

**Resume modes** are applied after loading:

- **Resume-fresh** — Clean slate. All undelivered events are dropped. All non-terminal tasks are force-failed. All agents are reset to IDLE with empty queues. (The force-fails emit `TaskFinishedEvent`s, but no listeners are registered yet during `build()` — the events are silently discarded, which is fine since all agents start fresh anyway.)

- **Resume-continue** — Pick up where things left off. Agents that were mid-work (WORKING or REVIEWING) when the run stopped get a `ResumeEvent` enqueued *first*, so their interrupted task is the first thing they process. Then undelivered events from the DB are re-enqueued in original order behind them.

  **Crash safety:** Agents stay WORKING/REVIEWING in the DB deliberately. If the process crashes again during resume, the next resume-continue will correctly re-create the ResumeEvent from the persisted agent state.

**Force-cancel subtleties** (org timeout, Ctrl+C). When an agent is interrupted mid-turn, what happens depends on whether it has an established session:
- **Has a session** (completed at least one prior turn): Agent stays WORKING in the DB, session ID is preserved. On resume-continue, it gets a ResumeEvent and picks up with its prior conversation context intact.
- **No session** (crashed on its very first turn): Work is unresumable. The agent is marked IDLE. If it was a worker, the task is force-failed; if it was a reviewer, only that review is marked failed (same asymmetry as normal failures). No ResumeEvent on next resume.

**Failure asymmetry.** When an agent errors out, the impact depends on its role:
- Worker failure is terminal — the task is force-failed.
- Reviewer failure is recoverable — only that reviewer's contribution is marked failed. The review round continues with other reviewers, and the approval policy evaluates with the remaining quorum.

---

## Shared patterns

Conventions consistent across all stores:

- **Listener-list callbacks.** External observers register via `add_*_listener()` methods. Multiple listeners coexist without chaining. Each listener call is wrapped in try/except so one broken listener doesn't block others.
- **Notify outside the lock.** Stores build events inside `async with self._lock:`, then notify listeners after releasing. This prevents deadlocks from callbacks that re-enter the store.
- **Deep-copy returns.** Public methods return `model_copy(deep=True)` so callers never hold references to internal state.
- **ValueError for domain errors.** Invalid transitions, missing entities, and constraint violations all raise `ValueError`.
- **DB write-through.** Every mutation writes to SQLite inside the lock, before notifying listeners. The DB is optional (`db=None` disables persistence) but always present in production runs.

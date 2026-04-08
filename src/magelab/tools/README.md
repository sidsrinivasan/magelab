# tools/

The tool system that defines what agents can *do*.

The tools package is **runner-agnostic** — it defines tool specs and implementations with no dependency on any specific LLM backend. The runner (e.g., `ClaudeRunner`) is responsible for wrapping these into whatever format its backend expects (MCP servers, SDK tool objects, etc.). A new runner for a different LLM would import `ToolSpec` and `create_tool_implementations` and wire them into its own format.

There are three layers to framework tools:

1. **Specs** (`specs.py`) — Define each tool: name, description, parameter schema. Pure data, no behavior.
2. **Implementations** (`implementations.py`) — Instantiate each spec into a live handler closure, scoped to a specific agent and wired to the stores.
3. **Bundles** (`bundles.py`) — Group tools by role (e.g., `worker`, `management`) so YAML configs don't have to list every tool individually.

**Validation** (`validation.py`) checks that tool configurations are consistent at startup. **MCP modules** (`mcp.py`) are a separate extension point for adding custom domain-specific tools as FastMCP servers.

| File | What it does |
|------|-------------|
| `specs.py` | Tool definitions (name, description, parameter schema) |
| `bundles.py` | Named groups of tools for YAML config convenience |
| `implementations.py` | Runtime handler closures that execute tool calls |
| `validation.py` | Startup and runtime checks for tool configuration consistency |
| `mcp.py` | In-process MCP server loading, per-agent proxies with auto-injected `agent_id` |

<p align="center"><a href="#specs">Specs</a> | <a href="#implementations">Implementations</a> | <a href="#bundles">Bundles</a> | <a href="#validation">Validation</a> | <a href="#mcp-modules">MCP modules</a> | <a href="#import-structure">Import structure</a></p>

---

## Specs

Every framework tool is defined as a `ToolSpec` — a frozen dataclass with a name, description, and parameter schema. A runner can pass a `ToolSpec` directly to an MCP server or SDK tool definition with minimal conversion. The canonical registry of all framework tools is `FRAMEWORK: dict[str, ToolSpec]`.

Tool results use `ToolResponse` (text + optional `is_error` flag). Runners convert these into their SDK's native format (e.g., `ClaudeRunner` wraps them as MCP responses).

---

## Implementations

Given a `TaskStore`, `Registry`, `agent_id`, and `WireStore`, `create_tool_implementations()` returns a dict of `{tool_name: async_handler}` — one handler per framework tool, each closing over the stores and the agent's identity.

Each agent gets its own set of handler closures scoped to its `agent_id`. When `coder_0` calls `tasks_mark_finished`, the handler already knows the caller is `coder_0` without it being passed as a parameter.

### Visibility constraints

Some tools apply hidden scoping that agents don't control:

- **`tasks_list`** filters results to tasks that are unassigned or assigned to the calling agent or its direct connections. Agents cannot see tasks assigned to agents they're not connected to.
- **`connections_list`** strips `role_prompt`, `tools`, and `max_turns` from agent snapshots — agents cannot discover what tools their peers have.
- **`send_message` to groups** validates all-pairs connectivity, not just sender-to-each-recipient. If agents A and B are both connected to sender C but not to each other, the group message is rejected.

### Error handling

The `_handle_errors` decorator wraps every handler:
- `KeyError` (missing required field) → `ToolResponse("Missing required field: ...", is_error=True)`
- `ValueError` (from store validation) → `ToolResponse("Error: ...", is_error=True)`

Handlers that need custom error messages (e.g., parsing enum values where the generic ValueError would be misleading) catch locally — the inner except takes precedence over the decorator.

### Assignment validation

`_check_assignee()` runs before any task assignment or creation with `assigned_to`. It validates that the target agent:
- Exists and is not terminated
- Is connected to the calling agent (respects network topology)
- Can handle review-required tasks (has `tasks_submit_for_review`, and connected agents can conduct reviews)

### Spec/impl sync guard

At the end of `create_tool_implementations()`, a runtime check verifies that every spec in `FRAMEWORK` has a matching implementation and vice versa. This fails fast if someone adds a spec without an implementation or removes one without the other.

---

## Bundles

Bundles are named groups of tools that map to common agent roles, so YAML configs can say `tools: [management, worker, claude_reviewer]` instead of listing individual tool names.

| Bundle | Purpose | Includes |
|--------|---------|----------|
| `worker` | Agents that work on tasks | task submit for review, task mark finished, discover reviewers |
| `management` | Agents that delegate tasks | task batch create, assign, get, list, mark finished, discover connections |
| `management_nobatch` | Same but single-create | like management but `tasks_create` instead of `tasks_create_batch` |
| `claude_reviewer` | Review with code access | submit review, Read, Grep, Glob, Bash |
| `passive_claude_reviewer` | Review without execution | submit review, Read, Grep, Glob (no Bash) |
| `coordination` | Timing | sleep |
| `communication` | Inter-agent wire messaging | connections list, send/read/batch-read messages, conversations list |
| `claude_basic` | Common Claude Code tools for agents | Agent, Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch, NotebookEdit, TodoWrite |
| `claude` | **Disallow-list sentinel** (not for agent use) | Every known Claude Code built-in tool — used by the runner to block tools a role doesn't include |

The `expand()` function resolves a mixed list of bundle names and individual tool names into a flat, deduplicated tool list. This runs at `RoleConfig.__post_init__` time, so by the time the orchestrator sees a role's tools, bundles have already been expanded.

Strict mode (default) rejects names that aren't a known bundle or known individual tool (including bare Claude Code tool names like `"Read"` or `"Bash"`, which are valid because they appear in bundle contents). This catches typos early.

### `mcp__` naming convention

Names starting with `mcp__` pass through `expand()` unconditionally — these reference tools from external or in-process MCP servers. Two forms:

- `mcp__<server>` — server-level reference, expanded to all tools from that server at runner init time
- `mcp__<server>__<tool>` — specific tool reference, passes through as-is

---

## Validation

Checks that tool configurations are internally consistent — tools that depend on other tools (on the same agent or connected agents) are properly paired. Runs at startup and returns errors (hard stop) and warnings (logged).

Examples of what it catches:
- Agent has `get_available_reviewers` but not `tasks_submit_for_review` → **error** (useless tool)
- Agent has `tasks_submit_for_review` but no connected agent has `tasks_submit_review` → **warning** (reviews will never complete)
- Agent has `conversations_list` without read tools → **error**
- Agent has `send_message` without `read_messages` (or vice versa) → **warning**

### Review assignment validation

`validate_review_assignment()` checks that an agent can handle a `review_required` task: the agent must have `tasks_submit_for_review`, and at least one connected agent must have `tasks_submit_review`. Used both at startup (`validate_task_assignments()` over initial tasks) and at runtime (`_check_assignee()` in implementations).

---

## MCP modules

MCP modules let you add **custom domain-specific tools** — voting systems, markets, auctions, or any shared service — without modifying the framework. Each module is a standard FastMCP server that agents call like any other tool.

Three key capabilities:

- **Custom tools** — Define arbitrary tools as a FastMCP server. Agents call them through the standard `mcp__<server>__<tool>` naming convention.
- **Database persistence** — Register tables in the run's SQLite database, read and write state that survives across resume.
- **Event emission** — Push events into agent queues, triggering agent runs just like framework events do.

### Defining a module

A module is a Python file that exposes a `server` attribute (a `FastMCP` instance):

```yaml
mcp_modules:
  voting: path.to.voting_module
  market: path.to.market_module
```

During `Orchestrator.build()`, the framework loads each module, introspects its tools, and creates **per-agent proxies**. Each proxy wraps the shared FastMCP server so that tools declaring an `agent_id` parameter get it auto-injected — the agent never sees or fills in this parameter. This lets a single shared server track which agent is calling without trusting agents to self-identify.

Agents reference MCP tools in their role's tool list:
- `mcp__voting` — all tools from the voting server
- `mcp__voting__cast_vote` — just the `cast_vote` tool

Multiple agents call the same FastMCP server concurrently, so tool handlers must be async-safe.

**Implementation note:** The proxy mechanism and tool introspection use `fastmcp_server._tool_manager.list_tools()` — a private FastMCP API. This needs re-verification after FastMCP upgrades.

### Initialization

MCP modules can optionally define an `init(context: MCPContext)` function alongside their `server`. If present, the framework calls it during `Orchestrator.build()` after the database and stores are initialized.

`MCPContext` provides:

| Field | Purpose |
|-------|---------|
| `db` | The run's `Database` instance — register tables, read/write state |
| `emit_event` | `Callable[[BaseEvent], None]` — push events into the orchestrator's dispatch pipeline |

On resume, `init()` is called again with the same DB — tables already exist, so the server reads its persisted state. Modules without `init()` are pure stateless tool providers.

**Important:** `init()` must be synchronous — the framework rejects async `init()` functions at startup. This is a common gotcha since FastMCP tool handlers are typically async.

```python
from magelab.events import MCPEvent
from magelab.tools.mcp import MCPContext

def init(context: MCPContext) -> None:
    context.db.register_schema("""
        CREATE TABLE IF NOT EXISTS market_orders (
            id INTEGER PRIMARY KEY, agent_id TEXT, price REAL
        );
    """)
    # Read persisted state on resume
    orders = context.db.fetchall("SELECT * FROM market_orders")
    # ... hydrate in-memory state

    # Emit events to notify agents
    context.emit_event(MCPEvent(
        target_id="trader-0",
        server_name="market",
        payload="Price alert: ACME crossed $50",
    ))
```

---

## Import structure

`create_tool_implementations` is intentionally **not** re-exported from `tools/__init__.py`. Re-exporting it would create a circular import: `config → tools → implementations → registry → config`. Instead, `claude_runner.py` imports it directly from `tools.implementations`. The `__init__.py` only exports the safe, dependency-free pieces: `ToolSpec`, `ToolResponse`, `FRAMEWORK`, `Bundle`, `BUNDLES`, `expand`.

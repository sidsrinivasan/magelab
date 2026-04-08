# runners/

The LLM backend for agents — how agent runs are executed, configured, and prompted. Currently the only implementation is `ClaudeRunner`, which uses the Claude Agent SDK.

| File | What it does |
|------|-------------|
| `agent_runner.py` | Abstract base class defining the runner interface + `AgentRunResult` dataclass |
| `claude_runner.py` | Implementation using the Claude Agent SDK |
| `prompts.py` | Prompt construction — system prompts, event-to-prompt formatting, `PromptContext` |

<p align="center"><a href="#runner-interface">Runner interface</a> | <a href="#claude-runner">Claude runner</a> | <a href="#prompt-system">Prompt system</a></p>

---

## Runner interface

`AgentRunner` (`agent_runner.py`) is an abstract base class with five methods:

| Method | Purpose |
|--------|---------|
| `run_agent(agent_id, system_prompt, prompt)` | Run a full agent turn and return `AgentRunResult` |
| `interrupt_agent(agent_id)` | Cancel an in-flight LLM call (safe to call even if not running) |
| `get_session(agent_id)` | Return the current session ID (used to persist state on force-cancel) |
| `restore_session(agent_id, session_id)` | Restore a persisted session ID for resume |
| `shutdown()` | Clean up resources (transcript loggers, file handles) |

`AgentRunResult` is a dataclass with: `error`, `num_turns`, `cost_usd`, `duration_ms`, `timed_out`, and `session_id`. The runner reports the session ID on result; the orchestrator decides whether to persist it. `get_session()` is distinct — it retrieves the session ID when an agent is force-cancelled before `run_agent` completes, so the orchestrator can persist it for resume.

Note: the error category prefix strings (`ERROR_RATE_LIMITED`, `ERROR_API_OVERLOADED`, `ERROR_API_ERROR`) are a shared contract with `database.compute_run_summary()`, which uses `str.startswith()` matching to classify errors. Changing these strings will silently break run summary counts.

The orchestrator only interacts with this interface, so the LLM backend is swappable.

### Post-tool hooks

Runners support **post-tool hooks** — callables that run after every tool call and can inject additional text into the conversation. The signature is `(agent_id) -> Optional[str]`; if the hook returns text, it's surfaced to the agent as additional context on the tool response.

Currently used for wire (inter-agent messaging) unread notifications: after every tool call, a hook checks if the agent has unread messages and injects a summary. This ensures agents notice new messages even while busy working.

---

## Claude runner

`ClaudeRunner` (`claude_runner.py`) implements `AgentRunner` using the Claude Agent SDK (`claude_agent_sdk`). Each agent gets its own Claude Code subprocess with persistent session state.

### Per-agent setup

At construction, the runner iterates all agents in the registry and builds a frozen `_AgentRunConfig` for each. These configs are not updated afterward — if agents are added to the registry after the runner is constructed, they will not have configs and `run_agent()` will fail. The runner must be constructed after the registry is fully populated.

Per-agent config includes:

- **Framework MCP server** — magelab exposes its task management, wire messaging, and agent discovery tools to each agent via an in-process MCP server named `"magelab"`. This is how agents interact with the framework — creating tasks, submitting reviews, sending messages, listing connections, etc. Only tools the agent's role has access to are registered, so the LLM never sees tool definitions it can't use.
- **In-process MCP proxies** — If the org config declares `mcp_modules`, the runner creates per-agent proxy servers. Each proxy wraps a shared FastMCP server instance, injecting `agent_id` into tools that declare it (hidden from the agent). Only proxies for servers the agent has tools from are created.
- **Tool resolution** — `mcp__<server>` references in the role's tools list are expanded to concrete tool names (e.g., `mcp__voting__cast_vote`) by querying the FastMCP server's tool registry at construction time.
- **Allowed/disallowed tools** — Framework tools are prefixed as `mcp__magelab__{name}`. Built-in Claude Code tools (Read, Write, Bash, etc.) pass through directly. MCP module tools use `mcp__<server>__<tool>` format. Built-in tools not in the role's list are explicitly disallowed.
- **Model, max_turns, hooks** — Resolved from the role config (with agent overrides applied).

### Session directory

Each agent gets its own `CLAUDE_CONFIG_DIR` set to `<parent_of_working_directory>/.sessions/<agent_id>/`. Since `working_directory` is `output_dir/workspace/`, this resolves to `output_dir/.sessions/<agent_id>/`. The LLM backend writes session files there. Since the output directory is mounted in Docker, sessions persist across container restarts.

The orchestrator copies per-role session config into each agent's session directory before the first run. This is how per-role settings, external MCP server configs, plugins, skills, and other backend extension points are delivered to individual agents.

### Running an agent

Each `run_agent()` call:
1. Opens a `ClaudeSDKClient` session (resuming from `session_id` if one exists, maintaining conversation history)
2. Sends the prompt via `client.query()`
3. Streams the response, logging assistant text, tool calls, and tool results to the transcript logger
4. Returns an `AgentRunResult` with cost, turn count, duration, session ID, and any error

### Error handling

The runner catches SDK-level errors and translates them into `AgentRunResult.error`:

| Error | Category | Behavior |
|-------|----------|----------|
| `RateLimitError` | `Rate limited (429)` | Logged and reported |
| `InternalServerError` | `API overloaded ({status_code})` | Logged and reported |
| `APIStatusError` | `API error ({status_code})` | Catch-all for other API errors |
| `asyncio.TimeoutError` | Timeout | Agent marked timed out |
| `asyncio.CancelledError` | Cancellation | Re-raised for orchestrator shutdown |
| Other exceptions | Generic | Caught, logged, reported |

None of these are retried — the orchestrator handles failure at the task level (force-fail for workers, mark-review-failed for reviewers).

### Interruption

`interrupt_agent()` sends an interrupt signal to the active `ClaudeSDKClient`, cancelling the in-flight LLM call. Used during shutdown (especially on global timeout) to avoid waiting for long-running agents to finish naturally.

### Shutdown

`shutdown()` closes the transcript logger's file handles. Called by the orchestrator at the end of a run to prevent file descriptor leaks in batch execution.

### Authentication

The runner receives a `ResolvedAuth` object from the orchestrator. For API key auth, the key is forwarded to each agent subprocess via `ANTHROPIC_API_KEY` in the env dict. For subscription auth, the credentials file is already in the agent's session directory (symlinked there by `_copy_session_configs()`), so the SDK discovers it via `CLAUDE_CONFIG_DIR` — the runner doesn't need to do anything extra.

`CLAUDE_CODE_STREAM_CLOSE_TIMEOUT` is set to 1 hour in two places: at module import time (in the parent process via `os.environ`) and in each agent subprocess's env dict. Both are needed — the SDK reads from `os.environ` in the parent process for its own timeouts, while the child subprocess needs its own copy.

---

## Prompt system

`prompts.py` is responsible for turning framework events into natural language messages for agents. When the orchestrator dequeues an event from an agent's queue, it builds a `PromptContext` from current state and passes it to the prompt formatter, which produces the text the agent actually sees. The prompt system has two parts: the **system prompt** (sent once, sets identity) and **event prompts** (sent per event, deliver work).

### System prompt

Built by `build_system_prompt()`, assembled from three parts:
1. **Working directory** — Injected as a `<system-message>` so agents know their cwd from turn 1
2. **Org prompt** — Organization-level instructions shared by all agents (supports `{agent_id}` placeholder)
3. **Role prompt** — Role-specific persona and capabilities

The system prompt is sent only on the first call for each agent; subsequent calls reuse the session and skip it.

### Event prompts

Each time an agent is dispatched for an event, the orchestrator builds a `PromptContext` from current state and passes it to `default_prompt_formatter()`, which converts it into a prompt string (or `None` to skip the dispatch if the event is stale).

**PromptContext** fields:

| Field | Purpose |
|-------|---------|
| `event` | The event that triggered this agent run |
| `task` | The task associated with the event (if any, fetched fresh from the store) |
| `agent_tools` | Tool names available to the agent — used to tailor instructions |
| `wire_conversations` | Pre-formatted conversation texts for wire events |
| `other_open_tasks` | Other non-finished tasks assigned to this agent |

The key design principle: **prompts are tool-aware**. The formatter checks which tools the agent has and tailors instructions accordingly. An agent with `tasks_submit_for_review` gets instructions about submitting for review; one without that tool doesn't. An agent with `get_available_reviewers` is told to discover reviewers first; one without it is told to submit directly. This prevents the LLM from attempting actions it can't perform.

| Event | What the agent sees |
|-------|-------------------|
| `TaskAssignedEvent` | Task details + instructions based on review/finish capability |
| `ReviewRequestedEvent` | Task details + review instructions |
| `ReviewFinishedEvent` | Review outcome + feedback (three variants: approved, changes requested, review failed) |
| `TaskFinishedEvent` | Notification that a delegated task completed |
| `WireMessageEvent` | Unread messages from one or more conversations |
| `ResumeEvent` | "You were interrupted while working. Please continue where you left off." |
| `MCPEvent` | Payload rendered verbatim — the MCP server controls the content entirely |

Review-related prompts include the full review history so agents have context from prior rounds.

Wire prompts can contain one or multiple conversations. When a wire event triggers an agent, the orchestrator fetches all unread wires for the agent and batches them into a single prompt. The first wire event delivers everything pending; subsequent queued wire events naturally go stale (nothing left unread) and get skipped.

Task-assigned prompts include an **open task reminder** if the agent has `tasks_mark_finished` and has other non-finished tasks — a nudge to mark completed work as done.

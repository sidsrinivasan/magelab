# 🧙 magelab

**M**ulti-**Age**nt **Lab** — an orchestration and experimentation framework for multi-agent LLM systems.

Define a multi-agent system in YAML — roles, tools, network topology — and magelab will run it. We call this an **organization** (**org** for short) — though it doesn't have to be cooperative; adversarial and competitive setups work too. The framework handles orchestration, task and review workflows, inter-agent messaging, full-state persistence to SQLite, and a live browser dashboard. Currently built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) via the [Claude Agent SDK](https://github.com/anthropics/claude-code-sdk-python).

---

🚀 **Getting started:** [Quick start](#quick-start) · [Authentication](#authentication)

⚙️ **Configuration:** [Org config](#org-config) · [Tools](#tools) · [Extension points](#extension-points)

🔧 **Usage:** [CLI](#cli) · [Python API](#python-api) · [Pipeline](#pipeline) · [Frontend dashboard](#frontend-dashboard) · [Docker](#docker)

📖 **How it works:** [Features](#features) · [Events](#events) · [Execution modes](#execution-modes) · [Output directory](#output-directory) · [Persistence and resume](#persistence-and-resume) · [Prompt tips](#prompt-tips) · [Architecture](#architecture)

---

**At a glance:**
- Single YAML config defines the entire system: agents, roles, tools, network, tasks
- Structured task management with optional multi-round code review
- Asynchronous inter-agent messaging with configurable notification paths
- Pipeline stages with callbacks for experiment setup and evaluation
- Docker isolation with automatic environment matching
- SQLite persistence — crash recovery, run viewing, and post-hoc analysis
- Live browser dashboard with agent transcripts, task state, and workspace browsing
- Extensible via in-process MCP servers and Claude Code's native config system

---

# 🚀 Getting started

## Quick start

### Install

```bash
# Clone and install with uv
git clone https://github.com/sidsrinivasan/magelab.git
cd magelab
uv sync
```

<!--
Once published to PyPI:
```bash
uv pip install magelab
```
-->

Or add as a git submodule:

```bash
git submodule add https://github.com/sidsrinivasan/magelab.git magelab
uv pip install -e magelab/
```

Requires Python 3.12+.

### Configure

Create a `config.yaml`:

```yaml
settings:
  org_name: "my_org"
  org_permission_mode: "acceptEdits"

roles:
  coder:
    name: "coder"
    model: "claude-opus-4-6"
    max_turns: 50
    role_prompt: |
      You are a software engineer. Implement the assigned task,
      run tests, and mark it finished when done.
    tools: [worker, claude_basic]

agents:
  coder_0:
    agent_id: "coder_0"
    role: coder

initial_tasks:
  - id: "task_1"
    title: "Build a calculator"
    description: "Implement a basic calculator with add, subtract, multiply, divide."
    assigned_to: "coder_0"
    review_required: false
```

### Run

**CLI:**

```bash
uv run magelab config.yaml --sub                    # Claude subscription auth
uv run magelab config.yaml --api-key                 # API key auth
uv run magelab config.yaml --sub -o ./my_run         # custom output directory
uv run magelab config.yaml --sub --docker            # run inside a Docker container
```

**Python:**

```python
import asyncio
from magelab import run_pipeline, resolve_sub

auth = resolve_sub()
asyncio.run(run_pipeline("config.yaml", output_dir="./my_run", auth=auth))
```

Output goes to `{org_name}/{timestamp}/` by default. Use `-o` / `--output-dir` to specify a custom path.

### Observe

If you're developing locally, build the frontend first:

```bash
cd frontend && npm install && npm run build
```

The live browser dashboard is enabled by default. Open `http://localhost:8765` while a run is in progress to watch agent activity, task state, transcripts, and wire conversations in real time.


After a run completes, you can reopen the dashboard in read-only mode:

```bash
uv run magelab --view my_org.db -o ./my_run
```

---

## Authentication

Authentication is always required and always explicit — magelab never auto-discovers credentials silently. There are two modes.

### `--sub` — Subscription (OAuth)

Uses a `.credentials.json` file containing OAuth tokens. The SDK needs this file in each agent's session directory to refresh access tokens as they expire.

```bash
uv run magelab config.yaml --sub                              # auto-detect
uv run magelab config.yaml --sub /path/to/.credentials.json   # explicit file
```

Auto-detection searches:
1. `$CLAUDE_CONFIG_DIR/.credentials.json` (if set)
2. `~/.claude/.credentials.json` (Linux/Windows default after `claude login`)

On macOS, Claude stores credentials in the Keychain rather than as a file. macOS users should export their credentials (from the Keychain) to a file and pass the file path explicitly.

### `--api-key` — API key

Uses an `ANTHROPIC_API_KEY` environment variable. The key is forwarded to each agent subprocess — nothing written to disk.

```bash
uv run magelab config.yaml --api-key                          # read from environment
uv run magelab config.yaml --api-key /path/to/.env            # load from .env file
```

Auto-detection searches:
1. `ANTHROPIC_API_KEY` in the current environment
2. `.env` file found by searching from the working directory upward

### Programmatic

```python
from magelab import resolve_sub, resolve_api_key

auth = resolve_sub()                              # auto-detect subscription credentials
auth = resolve_sub(Path("credentials.json"))      # explicit path

auth = resolve_api_key()                          # from environment
auth = resolve_api_key(Path(".env"))              # from .env file
```

Auth credentials are forwarded to Docker containers automatically — no extra flags needed.

### Credential handling

For `--sub`, the credentials file is copied into the output directory (`output_dir/.sessions/_credentials/.credentials.json`) and symlinked into each agent's session directory. All copies and symlinks are set to `0600` permissions (owner read/write only). For `--api-key`, the key is passed via environment variables only — nothing is written to disk in the output directory.

---

# ⚙️ Configuration

## Org config

All configuration lives in a single YAML file. This section is a complete reference.

### Settings

All behavioral settings live under the `settings:` key.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `org_name` | string | `"magelab"` | Organization name. Used in output directory and database filename. |
| `org_description` | string | `""` | Human-readable description of this config variant. Stored in the database for reference. |
| `org_prompt` | string | `""` | Shared prompt prepended to every agent's system prompt. Supports `{agent_id}` placeholder. |
| `org_permission_mode` | string | `"acceptEdits"` | Claude Code permission mode. Use `"acceptEdits"` or `"bypassPermissions"`. |
| `org_timeout_seconds` | float | `3600` | Max time for the entire run (default: 1 hour). |
| `agent_timeout_seconds` | float | `900` | Max time for a single agent dispatch (default: 15 min). |
| `sync` | bool | `false` | Use synchronized round-based execution. See [Execution modes](#execution-modes). |
| `sync_max_rounds` | int | — | Required when `sync: true`. |
| `sync_round_timeout_seconds` | float | — | Per-round timeout. Only valid when `sync: true`. |
| `wire_notifications` | string | `"all"` | Wire notification mode: `"all"`, `"tool"`, `"event"`, or `"none"`. See [Wire notifications](#wire-notifications). |
| `wire_max_unread_per_prompt` | int | `10` | Max unread conversations delivered in a single wire event prompt. |
| `agent_settings_dir` | string | — | Path to per-role settings (relative to config file). See [Extension points](#extension-points). |
| `mcp_modules` | dict | `{}` | In-process MCP servers. See [Extension points](#extension-points). |

### Roles

Roles are templates that define a type of agent. Multiple agents can share the same role.

```yaml
roles:
  pm:
    name: "pm"
    model: "claude-opus-4-6"
    max_turns: 100
    role_prompt: |
      You are a project manager. Break down the project into tasks
      and assign them to your development team.
    tools: [management]

  coder:
    name: "coder"
    model: "claude-opus-4-6"
    max_turns: 100
    role_prompt: |
      You are a software engineer. Implement tasks, write tests,
      and submit your work for review.
    tools: [worker, claude_reviewer, claude_basic]
    session_config: "coder"  # loads from agent_settings_dir/coder/
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | **required** | Unique role identifier. |
| `model` | string | **required** | Anthropic model ID (e.g. `"claude-opus-4-6"`). |
| `max_turns` | int | `100` | Max LLM turns per agent dispatch. |
| `role_prompt` | string | **required** | Prompt defining the agent's persona, instructions, and capabilities. |
| `tools` | list | **required** | Tool names and/or [bundle names](#tools). |
| `session_config` | string | — | Subdirectory of `agent_settings_dir` to copy into each agent's session dir. See [Extension points](#extension-points). |

### Agents

Agents are instances of roles. Each agent has a unique ID and references a role. Any role field can be overridden per agent.

```yaml
agents:
  pm_0:
    agent_id: "pm_0"
    role: pm

  coder_1:
    agent_id: "coder_1"
    role: coder

  coder_2:
    agent_id: "coder_2"
    role: coder
    model_override: "claude-sonnet-4-6"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent_id` | string | **required** | Unique agent identifier. |
| `role` | string | **required** | References a role name. |
| `role_prompt_override` | string | — | Replaces the role's prompt. |
| `tools_override` | list | — | Replaces the role's tool list. |
| `model_override` | string | — | Replaces the role's model. |
| `max_turns_override` | int | — | Replaces the role's max_turns. |
| `session_config_override` | string | — | Replaces the role's session_config. |

### Network

Controls which agents can see and interact with each other. If omitted, all agents are fully connected.

```yaml
network:
  groups:
    dev_team: [coder_1, coder_2, coder_3]
  connections:
    pm_0: [coder_1, coder_2, coder_3]
```

- **Groups** create cliques — full connectivity among all members. Useful for dense subgraphs (e.g., a team where everyone can talk to everyone).
- **Connections** are explicit edges, automatically symmetrized. Useful for sparse topologies (e.g., a PM connected to each coder, but coders not connected to each other).
- An agent's connections are the union of all its groups and explicit connections.
- Every agent must appear somewhere in the network config.
- Network topology governs task assignment, review requests, wire messaging, and agent discovery.

### Wire notifications

Configures how inter-agent messages are delivered. Only active if at least one agent has the `communication` tool bundle. Set via `wire_notifications` in settings:

```yaml
settings:
  wire_notifications: "all"   # "all" (default), "tool", "event", or "none"
```

| Value | Behavior |
|-------|----------|
| `all` | Both tool-response injection and event queuing (default) |
| `tool` | Append unread count to every tool response only |
| `event` | Queue wire events for idle agents only |
| `none` | No automatic notifications (agents must poll) |

`tool` notifications ensure agents notice new messages while busy working. `event` notifications wake idle agents when messages arrive.

### Initial tasks

Tasks that exist at the start of the run, assigned immediately.

```yaml
initial_tasks:
  - id: "project_setup"
    title: "Set up the project"
    description: |
      Create the project structure, install dependencies,
      and implement the core module.
    assigned_to: "pm_0"
    review_required: false
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | **required** | Unique task identifier. |
| `title` | string | **required** | Short task title. |
| `description` | string | **required** | Full task description. |
| `assigned_to` | string | **required** | Agent ID to assign to. |
| `assigned_by` | string | `"User"` | Who assigned the task. |
| `review_required` | bool | `false` | If true, must be approved by reviewers before it can succeed. |

Agents can also create tasks at runtime using `tasks_create` or `tasks_create_batch`.

### Initial messages

Wire messages sent at startup — an alternative to initial tasks for delivering context.

```yaml
initial_messages:
  - participants: [analyst_0, comms_director_0]
    sender: "client"
    body: "Please develop a proposal for our new initiative."
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `participants` | list | **required** | Agent IDs in the conversation. |
| `body` | string | **required** | Message content. |
| `sender` | string | `"User"` | Who sent the message. |
| `wire_id` | string | — | Explicit wire ID. Auto-generated if omitted. |
| `task_id` | string | — | Associate with a task. |

---

## Tools

Agents interact with the framework through tools exposed via an in-process MCP server. Each agent only sees tools that its role's `tools` list includes. Tools can be specified individually or as **bundles** — named groups that expand into related tools.

### Task bundles

| Bundle | Purpose | Tools |
|--------|---------|-------|
| `worker` | Work on assigned tasks | submit for review, mark finished, discover reviewers |
| `management` | Delegate and track tasks | batch create, assign, get, list, mark finished, discover connections |
| `management_nobatch` | Same but single-task creation only | create, assign, get, list, mark finished, discover connections |

### Review bundles

| Bundle | Purpose | Tools |
|--------|---------|-------|
| `claude_reviewer` | Review with code access + execution | submit review, Read, Grep, Glob, Bash |
| `passive_claude_reviewer` | Review with code access (read-only) | submit review, Read, Grep, Glob |

### Claude Code bundles

| Bundle | Purpose | Tools |
|--------|---------|-------|
| `claude_basic` | Core working tools | Agent, Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch, NotebookEdit, TodoWrite |
| `claude` | All Claude Code built-ins | Everything in `claude_basic` plus AskUserQuestion, Cron, LSP, Skill, Task, Plan/Worktree tools, etc. Used internally for the disallow list — tools in `claude` not granted to an agent are explicitly blocked. |

### Other bundles

| Bundle | Purpose | Tools |
|--------|---------|-------|
| `communication` | Wire messaging | connections list, send/read/batch-read messages, conversations list |
| `coordination` | Timing | sleep (0–60 seconds) |

Mix bundles and individual tool names freely:

```yaml
tools: [worker, claude_reviewer, claude_basic]
```

All names are validated at config parse time — typos are caught immediately.

### Framework tools

Bundles expand into individual framework tools. These are the tools magelab exposes to agents via an in-process MCP server — each agent only sees tools its role includes.

**Task tools:**

| Tool | Purpose |
|------|---------|
| `tasks_create` | Create a single task (optionally assigned) |
| `tasks_create_batch` | Create multiple tasks at once |
| `tasks_assign` | Assign a task to an agent |
| `tasks_get` | Get a task by ID |
| `tasks_list` | List tasks (filterable by assignee, creator, status) |
| `tasks_submit_for_review` | Submit work for review, specifying reviewers and approval policy (`any`, `majority`, `all`) |
| `tasks_submit_review` | Submit a review decision (`approved` or `changes_requested`) |
| `tasks_mark_finished` | Mark a task as `succeeded` or `failed` |

**Communication tools:**

| Tool | Purpose |
|------|---------|
| `send_message` | Send a message to agents (by recipients or conversation ID) |
| `read_messages` | Read messages in a conversation (unread + context) |
| `batch_read_messages` | Read unread messages across up to 5 conversations at once |
| `conversations_list` | List conversations (optionally filtered to unread only) |

**Discovery and coordination tools:**

| Tool | Purpose |
|------|---------|
| `connections_list` | Discover agents you can interact with |
| `get_available_reviewers` | List connected agents who can review, with workload info |
| `sleep` | Pause execution (0–60 seconds), useful for waiting on other agents |

### Visibility

Some tools apply hidden scoping that agents don't control:

- **`tasks_list`** only shows unassigned tasks and tasks assigned to the calling agent or its connections.
- **`connections_list`** strips internal details (tools, prompts, max_turns) from agent snapshots.
- **`send_message`** to groups validates all-pairs connectivity, not just sender-to-each.

For full tool specs, implementation details, and validation rules, see [`src/magelab/tools/README.md`](src/magelab/tools/README.md).

---

## Extension points

magelab provides two mechanisms for extending what agents can do beyond the built-in tools.

### In-process MCP servers

Declare custom domain-specific tools as Python [FastMCP](https://github.com/modelcontextprotocol/python-sdk) servers. The framework loads them at startup, creates per-agent proxies, and manages the lifecycle.

```yaml
settings:
  mcp_modules:
    voting: "my_experiment.voting_server"
    scoring: "my_experiment.scoring_server"
```

Reference MCP tools in a role's tool list with the `mcp__<server>__<tool>` prefix:

```yaml
roles:
  analyst:
    tools: [worker, claude_basic, mcp__voting__cast_vote, mcp__scoring__submit_score]
```

The module must expose a `server` attribute that is a `FastMCP` instance:

```python
from mcp.server.fastmcp import FastMCP

server = FastMCP("voting")

@server.tool()
async def cast_vote(agent_id: str, proposal_id: str, vote: str) -> str:
    """Cast a vote on a proposal."""
    # agent_id is auto-injected by the framework, hidden from the agent
    ...
```

Tools that declare an `agent_id` parameter get it auto-injected — the agent never sees or provides it. This lets your server know which agent is calling without exposing that to the LLM. Multiple agents call the same server concurrently, so handlers must be async-safe.

Modules may also expose an optional `init(context)` function (must be synchronous). If present, the framework calls it at startup with an `MCPContext` providing access to the run's SQLite database (for persistence) and an event emitter for proactively notifying agents. The event emitter dispatches `MCPEvent`s — each carries a `payload` string that is rendered verbatim as the target agent's prompt, giving your server full control over what the agent sees. Modules without `init` are pure stateless tool providers. See [`src/magelab/tools/README.md`](src/magelab/tools/README.md) for the full MCP module lifecycle, proxy creation, and tool resolution details.

### Agent settings directory

Each agent runs as a Claude Code subprocess with its own isolated config directory. The `agent_settings_dir` and `session_config` fields deliver per-role configuration into each agent's session — giving access to all of Claude Code's native extension points.

```
agent_settings/
├── coder/
│   ├── settings.json        # model preferences, permission overrides
│   ├── .mcp.json            # external MCP servers (Slack, GitHub, databases)
│   ├── CLAUDE.md            # project-level instructions
│   ├── skills/              # custom slash commands
│   └── agents/              # custom agent definitions
└── pm/
    ├── settings.json
    └── CLAUDE.md
```

```yaml
settings:
  agent_settings_dir: "./agent_settings"   # relative to config file

roles:
  coder:
    session_config: "coder"    # copies agent_settings/coder/ into each coder's session dir
  pm:
    session_config: "pm"

agents:
  coder_2:
    session_config_override: "coder_special"  # override for a specific agent
```

Anything Claude Code supports in its config directory works here. External MCP servers, custom skills, plugins, and future Claude Code features are available to agents without magelab needing explicit support for each one. See [`src/magelab/runners/README.md`](src/magelab/runners/README.md) for details on per-agent session setup and how configs are fanned out.

---

# 🔧 Usage

## CLI

```bash
# Fresh run
uv run magelab config.yaml --sub
uv run magelab config.yaml --api-key
uv run magelab config.yaml --sub -o ./my_run             # custom output directory
uv run magelab config.yaml --sub --no-frontend            # disable dashboard

# Docker
uv run magelab config.yaml --sub --docker                 # run in container
uv run magelab config.yaml --sub --docker-build           # force rebuild image first

# Batch
uv run magelab config.yaml --sub --runs 5 --max-concurrent 2

# Resume
uv run magelab --sub -o ./my_run --resume continue        # pick up where agents left off
uv run magelab --sub -o ./my_run --resume fresh           # fail in-progress tasks, restart

# View completed runs (read-only dashboard, no auth needed)
uv run magelab --view myorg.db -o ./my_run
uv run magelab --view-batch myorg.db -o ./batch_dir
```

| Flag | Description |
|------|-------------|
| `--sub [path]` | Subscription auth. Optional: path to `.credentials.json`. |
| `--api-key [path]` | API key auth. Optional: path to `.env` file. |
| `-o, --output-dir` | Output directory (default: `{org_name}/{timestamp}/`). |
| `-d, --docker` | Run org phases in a Docker container. |
| `-D, --docker-build` | Force rebuild Docker image, then run in Docker. |
| `--no-frontend` | Disable the live browser dashboard. |
| `--frontend-port` | Dashboard port (default: 8765). Base port for batch runs (increments per run). |
| `--runs N` | Total number of runs (default: 1). |
| `--max-concurrent N` | Max simultaneous runs (default: 1). |
| `--resume continue` | Resume a stopped run where agents left off. |
| `--resume fresh` | Resume with in-progress tasks failed. |
| `--view DB_NAME` | Read-only dashboard for a completed run. |
| `--view-batch DB_NAME` | Dashboards for all runs in a batch directory. |

---

## Python API

The pipeline module is the primary programmatic interface.

### `run_pipeline`

```python
import asyncio
from magelab import run_pipeline, resolve_sub

auth = resolve_sub()

outcomes = asyncio.run(run_pipeline(
    config_path="config.yaml",
    output_dir="./my_run",
    auth=auth,
    frontend_port=8765,        # None to disable dashboard
))
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_path` | `str` | **required** | Path to the YAML config file. |
| `output_dir` | `str \| Path` | **required** | Directory for all output. |
| `auth` | `ResolvedAuth` | **required** | From `resolve_sub()` or `resolve_api_key()`. |
| `stages` | `list[StageFn \| None]` | `None` | Stage callbacks. `None` = single run with no callbacks. |
| `frontend_port` | `int \| None` | `None` | Dashboard port. `None` = no dashboard. |
| `abort_on` | `frozenset[RunOutcome]` | `{RunOutcome.FAILURE}` | Outcomes that stop the pipeline early. |
| `docker` | `"run" \| "build" \| None` | `None` | `"run"` = use Docker, `"build"` = rebuild image first. |
| `resume_mode` | `ResumeMode \| None` | `None` | `"continue"` or `"fresh"`. `None` = fresh run. |

Returns `list[RunOutcome]` — one outcome per org run. Possible values:

| `RunOutcome` | Meaning | Exit code |
|-------------|---------|-----------|
| `SUCCESS` | All tasks completed successfully | 0 |
| `NO_WORK` | No tasks were created or assigned | 0 |
| `PARTIAL` | Some tasks succeeded, some failed | 1 |
| `TIMEOUT` | Global timeout reached | 2 |
| `FAILURE` | All tasks failed or a fatal error occurred | 3 |

### `run_pipeline_batch`

```python
from magelab import run_pipeline_batch, resolve_api_key

auth = resolve_api_key()

all_outcomes = asyncio.run(run_pipeline_batch(
    config_path="config.yaml",
    output_dirs=["run_1", "run_2", "run_3"],
    auth=auth,
    max_concurrent=2,
    base_frontend_port=8765,   # run_1 :8765, run_2 :8766, ...
))
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_path` | `str` | **required** | Path to the YAML config file (same for all runs). |
| `output_dirs` | `list[str \| Path]` | **required** | One output directory per run. |
| `auth` | `ResolvedAuth` | **required** | From `resolve_sub()` or `resolve_api_key()`. |
| `stages` | `list[StageFn \| None]` | `None` | Stage callbacks (same for all runs). `None` = single run with no callbacks. |
| `max_concurrent` | `int` | `1` | Max runs executing simultaneously. |
| `base_frontend_port` | `int \| None` | `None` | Starting port. Ports are pooled and recycled across runs (pool size = `max_concurrent`). |
| `abort_on` | `frozenset[RunOutcome]` | `{RunOutcome.FAILURE}` | Outcomes that stop each pipeline early. |
| `docker` | `"run" \| "build" \| None` | `None` | `"run"` = use Docker, `"build"` = rebuild image first. |
| `resume_mode` | `ResumeMode \| None` | `None` | `"continue"` or `"fresh"`. `None` = fresh run. |

Returns `list[list[RunOutcome]]` — one outcome list per run.

### Viewing completed runs

```python
from magelab import view_run, view_run_batch

view_run("./my_run/myorg.db", frontend_port=8765)
view_run_batch(["run_1/myorg.db", "run_2/myorg.db"], base_frontend_port=8765)
```

**`view_run`** — open a read-only dashboard for a single completed run. Blocks until Ctrl+C.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `db_path` | `str \| Path` | **required** | Path to the SQLite database file. |
| `frontend_port` | `int` | `8765` | Dashboard port. |

**`view_run_batch`** — open dashboards for multiple runs, each on its own port.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `db_paths` | `list[str \| Path]` | **required** | Paths to SQLite database files. |
| `base_frontend_port` | `int` | `8765` | Starting port (run *i* gets port base + *i*). |

---

## Pipeline

The pipeline is a list of **stage callbacks** with an org run between each consecutive pair. Stages run on the host; org runs execute locally or in Docker. This is how you add experiment setup, evaluation, or multi-phase workflows.

```
[setup, evaluate]         →  setup()  → run org → evaluate()
[setup, shock, evaluate]  →  setup()  → run org → shock() → run org → evaluate()
[None, evaluate]          →  (skip)   → run org → evaluate()
```

N stages produce N-1 org runs. Use `None` for stages where no callback is needed. With no `stages` argument, `run_pipeline` runs a single org with no callbacks — equivalent to the CLI. Each org run uses either async or sync execution depending on the config — see [Execution modes](#execution-modes).

### Stage callbacks (`StageFn`)

A stage callback has the following signature (importable as `StageFn` from `magelab`):

```python
def my_stage(output_dir: Path, logger: logging.Logger, config: OrgConfig) -> OrgConfig | None
```

| Argument | Type | Description |
|----------|------|-------------|
| `output_dir` | `Path` | The pipeline output directory. Write seed files to `output_dir / "workspace"`. |
| `logger` | `logging.Logger` | Framework logger for this pipeline run. Writes to `logs/framework.log`. |
| `config` | `OrgConfig` | The current org config, including any runtime mutations from the previous org run. |
| **return** | `OrgConfig \| None` | Return `None` to reuse the current config. Return a new `OrgConfig` to override the config for the next org run. |

Stages can be sync or async — async stages are awaited automatically.

```python
from pathlib import Path
from magelab import OrgConfig, run_in_workspace

def setup(output_dir: Path, logger, config: OrgConfig) -> OrgConfig | None:
    shutil.copy("data/train.json", output_dir / "workspace" / "train.json")
    return None

def evaluate(output_dir: Path, logger, config: OrgConfig) -> OrgConfig | None:
    result = run_in_workspace(["python", "predict.py"], output_dir, auth=auth)
    logger.info("stdout: %s", result.stdout)
    return None
```

### `run_in_workspace`

Run a command in the workspace directory. If the pipeline used Docker, the command automatically runs inside a container with the workspace mounted. Otherwise it runs locally.

```python
from magelab import run_in_workspace

result = run_in_workspace(
    cmd=["python", "src/predict.py", "data/test.json"],
    output_dir=output_dir,
    auth=auth,           # API key forwarded to container if needed
    timeout=120,
)
print(result.stdout)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cmd` | `list[str]` | **required** | Command to run (e.g. `["python", "predict.py"]`). |
| `output_dir` | `Path` | **required** | Pipeline output directory (parent of `workspace/`). |
| `env` | `dict \| None` | `None` | Extra environment variables (local mode only). |
| `auth` | `ResolvedAuth \| None` | `None` | API key forwarded to container if in Docker mode. |
| `timeout` | `float \| None` | `None` | Timeout in seconds. |

Returns `subprocess.CompletedProcess` with stdout/stderr captured as text. Especially useful for evaluating code the agents themselves wrote — for example, running an agent-built `predict.py` against a held-out test set. It ensures your evaluation runs in the same environment (local or Docker) as the agents.

### Abort on failure

By default, the pipeline aborts if any org run fails. Control this with `abort_on`:

```python
from magelab import run_pipeline, RunOutcome

outcomes = asyncio.run(run_pipeline(
    config_path="config.yaml",
    output_dir="./run",
    auth=auth,
    abort_on=frozenset({RunOutcome.FAILURE}),  # default
    # abort_on=frozenset(),                    # never abort
))
```

For the full pipeline API (stage signatures, resume logic, config snapshots), see [`src/magelab/pipeline/README.md`](src/magelab/pipeline/README.md).

---

## Frontend dashboard

magelab includes a live browser dashboard that streams orchestrator state over WebSocket. Enabled by default on port 8765.

Open `http://localhost:8765` while a run is in progress to see:

- Agent states (idle, working, reviewing) and event queues
- Task lifecycle (created, assigned, in review, succeeded, failed)
- Wire conversations between agents
- Full agent transcripts (LLM responses, tool calls, tool results)
- Workspace file browser
- Run outcome, duration, and cost

The dashboard stays alive after the run completes — press Ctrl+C to stop. For batch runs, each run gets its own port starting from `--frontend-port`.

### Viewing completed runs

Use `--view` to open a read-only dashboard from the SQLite database without re-running anything:

```bash
uv run magelab --view myorg.db -o ./my_run
uv run magelab --view-batch myorg.db -o ./batch_dir
```

---

## Docker

The `--docker` flag runs org phases inside containers. Stage callbacks always run on the host.

```bash
uv run magelab config.yaml --sub --docker          # builds image if needed
uv run magelab config.yaml --sub --docker-build    # force rebuild
```

How it works:
1. The `magelab:latest` image is built from the repo's Dockerfile on first use (or when `--docker-build` is passed)
2. The host's output directory is mounted at `/app` inside the container
3. Forwards auth credentials (see [Authentication](#authentication))
4. Runs as the host user to avoid permission issues on mounted files

Agents' working directory is `/app/workspace/` — this is where all their file operations happen. The rest of the output directory (`/app/.sessions/`, `/app/logs/`, the database, etc.) is technically visible to agents but they have no reason to access it.

Memory usage is roughly 5 GB per container for 10 agents. For batch runs, the image is built once before the batch starts.

**Programmatic usage:** Pass `docker="run"` to `run_pipeline` or `run_pipeline_batch` to run org phases in Docker. Use `docker="build"` to force rebuild the image first. Stage callbacks using `run_in_workspace` automatically run inside a container when the pipeline used Docker — no extra flags needed.

**Note:** Auto-building the Docker image requires the repo Dockerfile, so `--docker-build` / `docker="build"` only works when running from the cloned repo or a git submodule.

---

# 📖 How it works

## Features

magelab's features are modular — use any combination depending on what your organization needs.

### Task management

Give agents task tools (`worker`, `management` bundles) to enable structured task workflows. Without these, agents run independently with whatever Claude Code tools they have.

- **Creation** — Define initial tasks in the config. Agents with management tools can create additional tasks at runtime, individually or in batch.
- **Assignment** — Tasks are assigned to specific agents. The network topology controls who can assign to whom.
- **Completion** — Agents mark tasks as `succeeded` or `failed`. The organization terminates when all agents are quiescent (idle with empty event queues).

### Task review

Set `review_required: true` on a task and give agents the appropriate bundles (`worker` for submitting, `claude_reviewer` or `passive_claude_reviewer` for reviewing).

1. The worker submits the task for review, specifying reviewers and an approval policy (`any`, `majority`, or `all`)
2. Each reviewer is prompted with the task details and the worker's submission
3. Reviewers approve or request changes
4. The worker is notified of the outcome:
   - **Approved** — worker can mark the task as succeeded
   - **Changes requested** — worker iterates on the feedback and resubmits
5. Multiple review rounds are supported — the full review history is included in each prompt

The framework enforces that `review_required` tasks cannot be marked succeeded without approval. What reviewers look for is up to you — provide review criteria and instructions in the reviewer's role prompt.

### Wire messaging

**Wires** are magelab's inter-agent communication primitive — asynchronous message threads between agents. Give agents the `communication` bundle to enable them. Each unique set of participants shares a single wire (conversation thread).

Messages are delivered through two notification paths:
- **Tool notifications** — unread count appended to every tool response, so agents notice messages while working
- **Event notifications** — wire events queued for idle agents, waking them up

Both paths are enabled by default. Configure via `wire_notifications` in [settings](#wire-notifications).

For details on the task lifecycle, review state machine, network topology, and wire model, see [`src/magelab/state/README.md`](src/magelab/state/README.md).

---

## Events

magelab is event-driven. When something happens in the system — a task is assigned, a review completes, a message arrives — an **event** is emitted and queued to the target agent. The orchestrator dequeues events one at a time and dispatches the agent with a prompt built from the event.

### Event types

| Event | Trigger | Target |
|-------|---------|--------|
| `TaskAssignedEvent` | A task is assigned to an agent | The assignee |
| `ReviewRequestedEvent` | A worker submits a task for review | Each reviewer |
| `ReviewFinishedEvent` | All reviewers have responded | The worker who submitted |
| `TaskFinishedEvent` | A delegated task is marked succeeded or failed | The agent who most recently assigned it |
| `WireMessageEvent` | A message is posted in a conversation | Each participant |
| `ResumeEvent` | A run resumes via `--resume continue` | Agents that were mid-work |
| `MCPEvent` | An in-process MCP server emits an event | Whatever agent the server targets |

### Delivery timing

Each event represents something an agent should react to — a task assignment, a review result, an incoming message. **Events are only delivered when an agent ends its current turn.** The model is one event per turn: the orchestrator delivers an event, the agent reacts to it, and when the agent ends its turn the orchestrator delivers the next one. While an agent is running, new events queue up and wait.

Because of this, it can be helpful to guide agents in your role prompts on when to end their turn — for example, "End your turn after submitting a review request and wait for the result." The framework already nudges agents internally, but explicit guidance in prompts gives you more control. See [`src/magelab/runners/prompts.py`](src/magelab/runners/prompts.py) for how events are formatted into agent prompts.

### Staleness

Events can become stale if the system state moves past them before they're delivered. For example, if a task is already finished by the time a `TaskAssignedEvent` is dequeued, the event is dropped rather than confusing the agent with outdated information. Wire events work similarly — if the agent has already read the messages via a tool call, the queued `WireMessageEvent` is stale and skipped. Stale events are recorded in the database but never dispatched.

### Notifications vs events

`TaskFinishedEvent` notifies the agent who *most recently assigned* the task, not the agent who finished it. This is how managers learn that delegated work is done. The agent who finishes the task doesn't receive an event for their own completion — they already know, since they're the one who called `tasks_mark_finished`.

For per-event staleness conditions, wire prompt batching, and the full dispatch lifecycle, see [`src/magelab/README.md`](src/magelab/README.md).

---

## Execution modes

### Async (default)

Each agent runs as a concurrent loop: pull an event from its queue, process it, return to waiting. All agents run in parallel. The organization terminates when all agents are **quiescent** — idle with empty queues and no events in flight — or on global timeout.

### Sync

The orchestrator drives discrete rounds. Each round:
1. Drains all agent queues
2. Processes those events (sequential per agent, concurrent across agents)
3. New events generated during the round are queued for the *next* round

This creates clean round boundaries — everything in round N is based only on events from round N-1. Terminates when no events remain, or `sync_max_rounds` is reached.

```yaml
settings:
  sync: true
  sync_max_rounds: 20
  sync_round_timeout_seconds: 300  # 5 min per round
```

---

## Output directory

Each run produces an output directory with this structure:

```
output_dir/
├── {org_name}.db          # SQLite database — all state, persisted during run
├── workspace/             # Shared working directory visible to agents
├── .sessions/             # Per-agent Claude Code session directories
│   ├── coder_0/
│   ├── coder_1/
│   ├── _configs/           # Staged agent_settings_dir contents
│   └── _credentials/      # Staged auth credentials (--sub mode)
├── configs/
│   ├── 000_start.yaml     # Config snapshot before each org run
│   └── 000_end.yaml       # Config snapshot after (captures runtime mutations)
└── logs/
    ├── framework.log      # Framework-level log
    ├── transcripts/
    │   ├── coder_0.txt    # Full conversation transcript per agent
    │   └── pm_0.txt
    └── wires/
        └── {wire_id}.txt  # Conversation log per wire
```

- **`workspace/`** — The shared working directory for all agents. Every agent sees the same files — there is no per-agent isolation within the workspace. Stage callbacks that seed files should write into `output_dir / "workspace"`.
- **`.sessions/`** — Each agent's `CLAUDE_CONFIG_DIR`. Session files persist across restarts and enable conversation resume.
- **`{org_name}.db`** — The SQLite database. See [Persistence and resume](#persistence-and-resume) for details.
- **`configs/`** — YAML snapshots before and after each org run for auditability.
- **`logs/`** — Framework log and human-readable transcripts. Transcripts are also in the DB, but the text files are convenient for quick inspection.

---

## Persistence and resume

The SQLite database (`{org_name}.db`) captures all state as the run progresses. During a run, the database is write-only — all operational state lives in memory, and the database serves as a persistent record. It is only read from on resume (to reconstruct state) and for post-run analysis. You can query it directly with any SQLite client.

### Database tables

| Table | Contents |
|-------|----------|
| `run_meta` | One row per org run: outcome, duration, total cost, task counts (succeeded/failed/open), error counts (rate limited, overloaded, other API errors), whether it timed out, and a snapshot of the full org config as JSON |
| `run_events` | Every event dispatched: type, target/source agent, associated task/wire, outcome (delivered, stale, error), cost, duration, turn count |
| `run_transcripts` | Full conversation transcripts per agent: assistant text, tool calls, tool results, with turn numbers |
| `task_items` | All tasks: status, title, description, assignee, review records, outcome, timestamps |
| `agent_roles` / `agent_instances` | Role definitions and per-agent state: model, tools, prompt, current status (idle/working/reviewing), current task, session ID |
| `network_edges` / `network_groups` | Network topology |
| `wire_meta` / `wire_messages` / `wire_read_cursors` | Wire conversations, messages, and per-agent read positions |

MCP modules can register additional tables via `init(context)` for domain-specific persistence. Cost tracking is automatic — the database and dashboard both report per-agent and total USD spend.

### `--resume continue`

Pick up where agents left off. In-progress tasks remain in progress. Each agent's Claude Code session is restored with full conversation history. Works for crash recovery, after `docker stop`, or as part of a multi-phase pipeline where successive org runs build on previous state.

### `--resume fresh`

Fail all in-progress tasks and start clean, preserving historical data (completed tasks, transcripts, costs). Useful when agents got stuck and you want to retry — provide fresh initial tasks or messages in the config to give agents new work to pick up.

### Graceful shutdown

SIGTERM is converted to SIGINT so `docker stop` triggers proper cleanup — session IDs, costs, and outcomes are persisted before exit. If an agent is mid-turn when shutdown starts, its session ID is captured so `--resume continue` can restore it.

---

## Prompt tips

Agents are driven by their role prompts and org prompt. The suggestions below are things you can include in your prompts to make agent behavior more explicit — none are required, but they help reduce ambiguity.

### Completion gates

Without explicit gates, agents may mark tasks finished at their discretion.

- [ ] Require tests to pass before marking a task complete
- [ ] Require all delegated tasks to be completed before a delegator marks its own task complete
- [ ] Tell agents to explore the codebase before starting implementation

### Review workflow

- [ ] Specify how many reviewers to request ("Request ONE other agent to review your work")
- [ ] Provide criteria for reviewer selection
- [ ] Give reviewing agents explicit review criteria

### Wire messaging

- [ ] Guide agents on when to proactively message others
- [ ] Distinguish when to use messaging vs the task/review system

### Workspace discipline

- [ ] "Do not read, write, or modify files outside your working directory"

---

## Architecture

magelab is built on **Claude Code** via the [Claude Agent SDK](https://github.com/anthropics/claude-code-sdk-python). Each agent runs as a Claude Code subprocess with persistent session state. The runner interface is abstract — the orchestrator interacts only with this interface, and the tool system has no dependency on any specific LLM backend.

For implementation details, see the internal documentation:

| Document | Scope |
|----------|-------|
| [`src/magelab/README.md`](src/magelab/README.md) | Package overview — orchestrator, config, events, internals |
| [`src/magelab/pipeline/README.md`](src/magelab/pipeline/README.md) | Pipeline execution — stages, batch runs, Docker |
| [`src/magelab/state/README.md`](src/magelab/state/README.md) | Task lifecycle, agent registry, network topology, wire model |
| [`src/magelab/tools/README.md`](src/magelab/tools/README.md) | Tool specs, bundles, implementations, MCP modules |
| [`src/magelab/runners/README.md`](src/magelab/runners/README.md) | Runner interface, Claude runner, prompt system |

# pipeline/

Runs multi-stage org pipelines — locally or in Docker, one run or many in parallel.

| File | What it does |
|------|-------------|
| `execution.py` | `run_pipeline()`, `run_pipeline_batch()`, `view_run()`, `view_run_batch()`, `StageFn` type |
| `docker.py` | Image management (`ensure_image`), containerized execution (`run_in_docker`), workspace subprocess helper (`run_in_workspace`) |
| `display.py` | `StatusDisplay` — live-updating terminal progress for concurrent runs |
| `__init__.py` | Public API: `run_pipeline`, `run_pipeline_batch`, `run_in_workspace`, `view_run`, `view_run_batch`, `StageFn`, `RunOutcome` |

<p align="center"><a href="#stage-based-pipeline">Stage-based pipeline</a> | <a href="#run_pipeline">run_pipeline</a> | <a href="#run_pipeline_batch">run_pipeline_batch</a> | <a href="#docker-execution">Docker execution</a> | <a href="#output-directory-structure">Output directory structure</a> | <a href="#viewing-runs">Viewing runs</a> | <a href="#statusdisplay">StatusDisplay</a></p>

---

## Stage-based pipeline

A pipeline is a list of **stage callbacks** separated by org runs. N stages produce N-1 org runs.

```
[setup, evaluate]        → setup() → run org → evaluate()
[setup, shock, evaluate] → setup() → run org → shock() → run org → evaluate()
[None, None]             → (skip)  → run org → (skip)
```

The last example is what the CLI uses — `stages=None` defaults to `[None, None]`, producing a single org run with no callbacks.

**`StageFn`** is the callback type:

```python
StageFn = Union[
    Callable[[Path, logging.Logger, OrgConfig], Optional[OrgConfig]],
    Callable[[Path, logging.Logger, OrgConfig], Awaitable[Optional[OrgConfig]]],
]
```

Stages receive `output_dir` (an absolute `Path`), a logger, and the current `OrgConfig`. For the first stage, this is the config parsed from YAML. For subsequent stages, it's the config reconstructed from the DB after the previous run (capturing any runtime mutations). Return `None` to reuse the current config, or return a new `OrgConfig` to change the config for the next org run. Both sync and async callbacks are supported.

### What happens when a stage returns a new OrgConfig

The returned OrgConfig **fully replaces** the previous one — it is not merged. On the next org run, `Orchestrator.build()` applies it via `register_config()`. Here's what that means for each part:

- **Roles** — Upsert. Roles in the new config are created or updated. Roles in the DB but absent from the new config **survive** — they are not deleted. After `register_config()` + `load_from_db()`, all roles from the DB (including ones not in the new config) are loaded into memory.
- **Agents** — Upsert, same as roles. Structural fields (role, model, prompt, tools, max_turns) are overwritten from the new config. **Operational state** (lifecycle state, session IDs, current task) is preserved via the upsert — so an agent that was mid-work keeps its progress across phases. Agents in the DB but absent from the new config survive, same as roles.
- **Network** — Fully replaced (wipe-and-rewrite). Unlike roles and agents, the new config's network section is the complete topology spec. Edges and groups absent from the new config are deleted. If the new config has `network=None`, all prior network state is wiped and agents become fully connected.
- **Settings** — Fully overwritten. The new config's settings (timeouts, sync mode, wire notifications, etc.) take effect for the next run (stored as the full OrgConfig JSON in `run_meta`).
- **Initial tasks and messages** — Consumed fresh from the new config. The pipeline passes them to `orchestrator.run()` for each org run.

This preserves operational continuity (task history, sessions, wire state) across structural changes — you can restructure the org between runs while agents keep their progress. Combined with the stage callback's access to `output_dir`, this enables interventions like modifying workspace files, swapping prompts, adding agents, reconfiguring the network, or assigning new tasks between org runs.


## run_pipeline

`run_pipeline` runs a single pipeline end-to-end. Stages run on the host; org runs execute locally or in Docker containers.

### Directory setup

On first run, the pipeline creates:

```
output_dir/
├── workspace/           # agent working directory
│   └── .trash/
├── logs/
├── configs/
└── .docker_image        # Docker mode only — stores the image name
```

### Config snapshots

Before each org run, the pipeline saves a YAML snapshot of the config as `configs/{run_number:03d}_start.yaml`. After the run, it saves `configs/{run_number:03d}_end.yaml` (reconstructed from DB, capturing runtime mutations). The start snapshot is read back from disk before the org run to ensure YAML round-trip parity (catching any fields that don't survive serialization).

### Agent settings

If the org config specifies `agent_settings_dir`, the pipeline copies it into `.sessions/_configs/` as a staging area. The orchestrator then fans out per-role settings into each agent's session directory. Put your `agent_settings_dir` next to your config YAML — it's resolved relative to the config file's directory.

Note: this copy happens once at pipeline start from the initial config. If a stage callback returns a new OrgConfig with a different `agent_settings_dir`, the new directory is not copied — the staging area is frozen after pipeline start.

### Resume behavior

Resume mode is resolved by priority — the first match wins:

**First org run:**
1. `resume_mode` parameter (the programmatic/CLI override)
2. `resume_mode` in the OrgConfig
3. Fresh (no resume)

**Subsequent org runs:**
1. `resume_mode` in the OrgConfig
2. `CONTINUE`

Subsequent runs default to CONTINUE so that multi-phase pipelines (setup → run → shock → run → evaluate) build on the DB state left by the previous phase.

### Abort behavior

`abort_on` controls early termination. If an org run's `RunOutcome` is in the set, remaining stages are skipped and the outcomes collected so far are returned.

- Default: `frozenset({RunOutcome.FAILURE})` — stops on failure, proceeds on success or partial.
- Experiments typically pass `frozenset()` so the evaluate stage always runs regardless of org outcome.

### Phase reporting

The `on_phase` callback is invoked with a string describing the current pipeline phase — `"setup"`, `"stage 1/2"` (during callbacks), `"running 1/2"` (during org runs), and finally an outcome string like `"S"` or `"SSF"` (see [StatusDisplay](#statusdisplay) for the encoding). Used by `run_pipeline_batch` to feed a shared `StatusDisplay`. When called directly (not from batch), if stdout is a TTY, `run_pipeline` creates its own single-run `StatusDisplay` automatically.

---

## run_pipeline_batch

`run_pipeline_batch` runs the same pipeline across multiple output directories concurrently. Each run gets its own output directory, logger, and frontend port. Runs are otherwise fully independent.

### Concurrency

- **Semaphore** — `max_concurrent` limits how many pipelines run simultaneously.
- **Port pool** — A shared pool of `max_concurrent` frontend ports starting at `base_frontend_port`, recycled as runs finish.
- **StatusDisplay** — A shared live terminal display showing status across all runs.
- **Docker image** — Built once before any runs start (not per-run). Individual runs receive `docker="run"` so they skip the image check.

---

## Docker execution

Docker mode runs the entire org inside an isolated container. This is useful when agents have access to tools like Bash and Write — the container provides a sandboxed environment where agents can install packages, run arbitrary code, and modify files without affecting the host machine. Each container gets its own filesystem; the only host state visible inside the container is the output directory, which is mounted at `/app`. Agents work inside `/app/workspace/` by default, but can access anything under `/app` (logs, configs, the DB). They cannot access anything on the host that isn't part of the output directory.

Stage callbacks (setup, evaluate, etc.) always run on the host, not inside containers. This means callbacks have full access to the host filesystem for things like copying data into the workspace or reading results out.

`ensure_image` and `run_in_docker` are internal to the pipeline. `run_in_workspace` is public.

### ensure_image

Builds the Docker image from the repo's Dockerfile if it doesn't already exist. `force=True` rebuilds unconditionally. Only works when running from the magelab repo (not from a pip install) — the Dockerfile must be present at the repo root. The default image name is `magelab:latest`.

### run_in_docker

Runs a single org phase inside a Docker container:

- Mounts `output_dir` at `/app` — agents see `workspace/`, configs, and the DB
- Requires the config YAML to be inside `output_dir` (it's always a snapshot in `configs/`)
- For API key auth: passes `ANTHROPIC_API_KEY` via `-e` flag
- For subscription auth: passes `--sub` pointing to staged credentials in the mounted volume
- Maps the frontend port through to the host (if specified)
- Runs as the host user on Linux (so output files aren't root-owned)
- Returns a `RunOutcome` derived from the container's exit code
- On Ctrl+C, sends `docker stop` (not `docker kill`) for graceful DB finalization. Note: the container's stop timeout is 1 second, so finalization must be fast or it will be killed.

### run_in_workspace

Runs a subprocess in the workspace, automatically choosing local or Docker execution based on how the pipeline was started. Checks for a `.docker_image` marker in the output directory — if present, runs the command inside a Docker container with the workspace mounted. Otherwise runs locally.

```python
result = run_in_workspace(["python", "src/predict.py", "data/test.json"], output_dir)
```

The working directory is `output_dir/workspace/`, so paths in `cmd` should be relative to the workspace root.

Important: in Docker mode, `run_in_workspace` mounts only `workspace/` (not the full `output_dir`). Scripts run this way cannot access logs, configs, or the DB — only workspace contents. This differs from `run_in_docker`, which mounts the full output directory.

Additional parameters: `env` (environment variables; local mode only), `auth` (resolved authentication credentials; API key is passed via `-e` flag in Docker mode), `timeout` (seconds).

**Authentication** is handled by the `auth` parameter, not environment files. For API key auth, the key is forwarded as an env var to the subprocess (local) or via `-e` flag (Docker). For subscription auth, credentials are already in agent session directories — `run_in_workspace` doesn't need to handle them.

---

## Output directory structure

```
output_dir/
├── .docker_image               # marker: Docker image name (only present in Docker mode)
├── .sessions/                  # per-agent session directories
│   ├── _configs/               # staging area: agent_settings_dir copied here by pipeline
│   ├── coder_1/                # agent session dir (settings + backend state)
│   └── coder_2/
├── configs/
│   ├── 000_start.yaml          # config snapshot before org run 0
│   └── 000_end.yaml            # config reconstructed from DB after org run 0
├── logs/
│   ├── framework.log           # pipeline and orchestrator logging
│   ├── transcripts/            # per-agent conversation logs
│   └── wires/                  # per-wire message logs
├── {name}.db                   # SQLite database (all persisted state)
└── workspace/                  # shared working directory visible to agents
    └── .trash/
```

The `workspace/` directory is the agent-facing working directory (their cwd). The `.sessions/` directory holds per-agent backend config and session state — each agent's subdirectory is set as its config home. Everything else in `output_dir` is framework output not exposed to agents.

---

## Viewing runs

`view_run` and `view_run_batch` open read-only frontend dashboards for previously completed runs.

```python
view_run(db_path="path/to/myorg.db", frontend_port=8765)
view_run_batch(db_paths=["run1/myorg.db", "run2/myorg.db"], base_frontend_port=8765)
```

Both take paths to SQLite database files (not config paths or output directories). Each builds a lightweight `RunView` from the DB, re-emits all transcript entries over the frontend websocket so connecting clients see the full history, and blocks until Ctrl+C. For batch viewing, each run gets its own port starting from `base_frontend_port`.

---

## StatusDisplay

`StatusDisplay` is a live-updating terminal display for pipeline runs. It renders one line per run showing phase, elapsed time, and optional label (e.g., port assignment).

### Phase icons

| Icon | Phase |
|------|-------|
| ○ | waiting |
| ◐ | setup |
| ● | running |
| ✓ | success |
| ◑ | partial |
| ✗ | failure |
| ⧖ | timeout |
| ⊘ | no_work |

When an org run completes, the outcome is encoded as a character (`S`uccess, `P`artial, `F`ailure, `T`imeout, `N`o_work). Multi-phase pipelines produce outcome strings like `"SSF"`. The icon and color are determined by the worst outcome in the string.

`abort_chars` controls which outcomes display as "aborted" vs "completed" in the summary line. This is derived from the `abort_on` parameter — if a run's outcome string contains any abort character, it counts as aborted.

---

## Integration with experiments

Experiment scripts are thin wrappers around `run_pipeline_batch`:

```python
from magelab import OrgConfig, run_pipeline_batch

stages = [setup, evaluate]
asyncio.run(run_pipeline_batch(config_path, output_dirs, stages=stages, ...))
```

The `setup` stage copies domain-specific data into `workspace/`. The `evaluate` stage calls `run_in_workspace()` to execute agent-written scripts, then computes metrics. Both stages are defined in the experiment module, not in the pipeline package.

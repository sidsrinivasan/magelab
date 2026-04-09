"""
Pipeline execution — run, batch, and view.

The core abstraction: a list of stage callbacks with org runs between them.
Stages return OrgConfig (for next run's config) or None (reuse previous config).

Multi-phase pipelines use SQLite resume — each org run is a
build→run→finalize cycle, and the next phase rebuilds from the DB
with resume_mode=CONTINUE.
"""

import asyncio
import logging
import shutil
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional, Union

import yaml

from ..auth import ResolvedAuth, stage_credentials
from ..frontend.server import run_with_frontend, serve_view_frontend
from ..orchestrator import Orchestrator, RunOutcome
from ..org_config import OrgConfig, ResumeMode
from ..state.database import Database
from ..state.database_hydration import reconstruct_org_config_from_db
from ..view import RunView
from .display import StatusDisplay
from .docker import cleanup_container, ensure_image, run_in_docker, start_container

# Stage callback: receives output dir, logger, and the current OrgConfig (which
# includes runtime mutations from the previous run). Returns OrgConfig (configure
# the next run) or None (no-op). May be sync or async — async stages are awaited.
StageFn = Union[
    Callable[[Path, logging.Logger, OrgConfig], Optional[OrgConfig]],
    Callable[[Path, logging.Logger, OrgConfig], Awaitable[Optional[OrgConfig]]],
]


def _setup_logging(output_dir: Path) -> logging.Logger:
    """Create an isolated logger for this pipeline run."""
    framework_logger = logging.getLogger(f"magelab.run.{output_dir.parent.name}")
    framework_logger.setLevel(logging.INFO)
    framework_logger.propagate = False
    if not framework_logger.handlers:
        handler = logging.FileHandler(output_dir / "framework.log", mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        framework_logger.addHandler(handler)
    return framework_logger


def _outcome_string(outcomes: list[RunOutcome]) -> str:
    """Build a compact outcome string like 'SSF' from a list of RunOutcome values."""
    return "".join(o.value[0].upper() for o in outcomes)


def _save_config_snapshot(output_dir: Path, run_count: int, label: str, config: OrgConfig) -> Path:
    """Save a YAML snapshot of the org config for auditability. Returns the path."""
    path = output_dir / "configs" / f"{run_count:03d}_{label}.yaml"
    with open(path, "w") as f:
        yaml.dump(config.to_dict(), f, default_flow_style=False, sort_keys=False, width=120, allow_unicode=True)
    return path


def _install_sigterm_handler() -> None:
    """Convert SIGTERM to KeyboardInterrupt for graceful shutdown (e.g. docker stop)."""
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: signal.raise_signal(signal.SIGINT))


async def run_pipeline(
    config_path: str,
    output_dir: Union[str, Path],
    stages: Optional[list[Optional[StageFn]]] = None,
    frontend_port: Optional[int] = None,
    abort_on: frozenset[RunOutcome] = frozenset({RunOutcome.FAILURE}),
    docker: Optional[Literal["run", "build"]] = None,
    auth: Optional[ResolvedAuth] = None,
    resume_mode: Optional[ResumeMode] = None,
    on_phase: Optional[Callable[[str], None]] = None,
) -> list[RunOutcome]:
    """Run an org with lifecycle stages.

    The pipeline is a list of stages (callbacks) with an org run between each
    consecutive pair. Stages run on the host; org runs execute locally or in
    Docker containers.

    Examples:
        [setup, evaluate]           → setup() → run org → evaluate()
        [setup, shock, evaluate]    → setup() → run org → shock() → run org → evaluate()
        [None, evaluate]            → (skip) → run org → evaluate()
        [setup, None]               → setup() → run org → (skip)

    N stages produce N-1 org runs. Requires at least 2 stages. Use None for
    stages where no callback is needed.

    Args:
        config_path: Path to the initial YAML config file.
        stages: List of stage callbacks (or None for a single no-op run).
            Must have at least 2 entries when provided as a list.
        output_dir: Directory for all output (workspace, DB, logs, results).
        frontend_port: Port for frontend dashboard (None = no frontend).
        abort_on: Set of RunOutcome values that should stop the pipeline early.
            If an org run produces an outcome in this set, remaining stages are
            skipped and the outcomes collected so far are returned. Default
            aborts on FAILURE. Pass frozenset() to never abort.
        docker: Docker execution mode. None = run locally. "run" = run org
            phases in Docker containers (auto-builds image if missing). "build" =
            force rebuild the image, then run in Docker. Stages always run on
            the host.
        auth: Resolved authentication credentials. Required for run/resume.
            Use resolve_sub() or resolve_api_key() from magelab.auth.
        resume_mode: Resume mode for the first org run (e.g. ResumeMode.CONTINUE
            to resume a previous run). None = fresh run. Subsequent runs always
            use CONTINUE unless the stage's OrgConfig overrides it.
        on_phase: Optional callback invoked with a string describing the current
            pipeline phase, e.g. "setup", "stage 1/2", "running 1/2", "S".
            Used by run_pipeline_batch to feed a shared StatusDisplay. If None and
            stdout is a TTY, a single-run StatusDisplay is shown automatically.
            If None and not a TTY, phases are silently ignored.

    Returns:
        List of RunOutcome values, one per org run. If the pipeline completes
        normally, this has N-1 entries for N stages. If aborted early, it has
        fewer entries.
    """
    if auth is None:
        raise ValueError("auth is required — use resolve_sub() or resolve_api_key() from magelab.auth")
    if stages is None:
        stages = [None, None]
    if len(stages) < 2:
        raise ValueError("stages must have at least 2 entries (use None for no-op slots)")

    _install_sigterm_handler()
    config_path_resolved = str(Path(config_path).resolve())
    output_dir = Path(output_dir).resolve()

    # Create directory structure
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "workspace").mkdir(exist_ok=True)
    (output_dir / "workspace" / ".trash").mkdir(exist_ok=True)
    (output_dir / "logs").mkdir(exist_ok=True)
    (output_dir / "configs").mkdir(exist_ok=True)

    # Setup logging
    framework_logger = _setup_logging(output_dir / "logs")

    # Stage credentials for orchestrator fan-out (SUB mode copies .credentials.json;
    # API_KEY mode is a no-op — key is forwarded via env vars)
    stage_credentials(auth, output_dir, logger=framework_logger)

    # Phase reporting to update display
    own_display = None
    if on_phase is None:
        if sys.stdout.isatty():
            abort_chars = {o.value[0].upper() for o in abort_on}
            own_display = StatusDisplay(num_runs=1, abort_chars=abort_chars)

            def on_phase(p: str) -> None:
                own_display.update(0, p)

            await own_display.start()
        else:

            def on_phase(p: str) -> None:
                pass

    try:
        # Docker setup: ensure image exists, then start long-lived container
        if docker is not None:
            await ensure_image(force=(docker == "build"))
            await start_container(output_dir, frontend_port, auth=auth)

        # Parse initial config (stages can override by returning a new OrgConfig)
        current_org_config = OrgConfig.from_yaml(config_path_resolved)
        num_org_runs = len(stages) - 1
        outcomes: list[RunOutcome] = []
        db_path = output_dir / f"{current_org_config.settings.org_name}.db"

        # Copy agent_settings_dir into .sessions/_configs/ for Docker portability
        # and as staging area for per-agent fan-out by the orchestrator.
        # agent_settings_dir is resolved to absolute by from_yaml().
        if current_org_config.settings.agent_settings_dir:
            src_config = Path(current_org_config.settings.agent_settings_dir)
            if not src_config.is_dir():
                raise FileNotFoundError(f"agent_settings_dir not found: {src_config} (referenced in config)")
            dest_config = output_dir / ".sessions" / "_configs"
            shutil.copytree(src_config, dest_config, dirs_exist_ok=True)
            framework_logger.info(f"Copied agent_settings_dir {src_config} → {dest_config}")

        on_phase("setup")

        for stage_idx, stage in enumerate(stages):
            # Call the stage callback (if not None). Supports both sync and async stages.
            stage_result = None
            if stage is not None:
                on_phase(f"stage {stage_idx + 1}/{len(stages)}")
                stage_result = stage(output_dir, framework_logger, current_org_config)
                if asyncio.iscoroutine(stage_result):
                    stage_result = await stage_result

            # Stage callback can override the config for subsequent runs
            if stage_result is not None:
                current_org_config = stage_result
                framework_logger.info(f"Stage {stage_idx + 1} returned new OrgConfig")

            # Run the org between stages (not after the last stage)
            if stage_idx < num_org_runs:
                # Get run number from DB (0 if DB is new/empty)
                with Database(db_path) as _db:
                    run_number = _db.run_count()

                # Snapshot config, read it back for YAML round-trip parity
                start_snapshot = _save_config_snapshot(output_dir, run_number, "start", current_org_config)
                current_org_config = OrgConfig.from_yaml(str(start_snapshot))
                on_phase(f"running {run_number + 1}/{num_org_runs}")

                # First org run in this pipeline invocation: CLI override > config > fresh.
                # Subsequent org runs: config > CONTINUE.
                if stage_idx == 0:
                    run_resume = resume_mode or current_org_config.resume_mode
                else:
                    run_resume = current_org_config.resume_mode or ResumeMode.CONTINUE

                if docker is not None:
                    outcome = await run_in_docker(
                        config_path=str(start_snapshot),
                        output_dir=output_dir,
                        frontend_port=frontend_port,
                        resume_mode=run_resume,
                        auth=auth,
                        logger=framework_logger,
                    )
                else:
                    orchestrator = await Orchestrator.build(
                        current_org_config,
                        output_dir,
                        logger=framework_logger,
                        resume_mode=run_resume,
                        auth=auth,
                    )
                    if frontend_port is not None:
                        await run_with_frontend(orchestrator, current_org_config, port=frontend_port, keep_alive=False)
                    else:
                        await orchestrator.run(
                            initial_tasks=current_org_config.initial_tasks,
                            initial_messages=current_org_config.initial_messages,
                            sync=current_org_config.settings.sync,
                            sync_max_rounds=current_org_config.settings.sync_max_rounds,
                            sync_round_timeout_seconds=current_org_config.settings.sync_round_timeout_seconds,
                        )
                    outcome = orchestrator.outcome

                # Snapshot end config and carry forward (captures runtime mutations)
                with Database(db_path) as db:
                    current_org_config = reconstruct_org_config_from_db(db)
                    _save_config_snapshot(output_dir, run_number, "end", current_org_config)

                outcomes.append(outcome)

                # Abort early if this outcome is in the abort set
                if outcome in abort_on:
                    framework_logger.info(
                        f"Aborting pipeline: run {run_number + 1} outcome {outcome.value} is in abort_on"
                    )
                    on_phase(_outcome_string(outcomes))
                    return outcomes

        on_phase(_outcome_string(outcomes))
        return outcomes

    finally:
        if docker is not None:
            await cleanup_container(output_dir)
        if own_display:
            await own_display.stop()


async def run_pipeline_batch(
    config_path: str,
    output_dirs: list[Union[str, Path]],
    stages: Optional[list[Optional[StageFn]]] = None,
    max_concurrent: int = 1,
    base_frontend_port: Optional[int] = None,
    abort_on: frozenset[RunOutcome] = frozenset({RunOutcome.FAILURE}),
    docker: Optional[Literal["run", "build"]] = None,
    auth: Optional[ResolvedAuth] = None,
    resume_mode: Optional[ResumeMode] = None,
) -> list[list[RunOutcome]]:
    """Run the same pipeline across multiple output directories concurrently.

    Args:
        config_path: Path to the YAML config file (same for all runs).
        stages: List of stage callbacks (same for all runs).
        output_dirs: One output directory per run.
        max_concurrent: Max number of concurrent pipeline runs.
        base_frontend_port: Starting port for frontend port pool. Ports are
            recycled across runs. None = no frontend.
        abort_on: Set of RunOutcome values that should stop each pipeline early.
            Passed through to each run_pipeline call. Default aborts on FAILURE.
        docker: Docker execution mode (None, "run", or "build"). See run_pipeline.
        auth: Resolved authentication credentials. See run_pipeline.
        resume_mode: Resume mode passed to each run_pipeline call. See run_pipeline.

    Returns:
        List of RunOutcome lists (one list per pipeline run, in order).
    """
    num_runs = len(output_dirs)
    semaphore = asyncio.Semaphore(max_concurrent)
    abort_chars = {o.value[0].upper() for o in abort_on}
    display = StatusDisplay(num_runs=num_runs, abort_chars=abort_chars)

    # Ensure image once before batch, not per-run
    if docker is not None:
        await ensure_image(force=(docker == "build"))

    # Port pool: allocate max_concurrent ports, recycle as runs finish
    port_pool: Optional[asyncio.Queue[int]] = None
    if base_frontend_port is not None:
        port_pool = asyncio.Queue()
        for i in range(max_concurrent):
            port_pool.put_nowait(base_frontend_port + i)

    async def _run_one(index: int, output_dir: Union[str, Path]) -> list[RunOutcome]:
        async with semaphore:
            fe_port = None
            try:
                if port_pool is not None:
                    fe_port = await port_pool.get()
                    display.set_label(index, f":{fe_port}")

                def on_phase(p: str) -> None:
                    display.update(index, p)

                # Pass "run" not "build" — image already ensured above.
                return await run_pipeline(
                    config_path=config_path,
                    stages=stages,
                    output_dir=output_dir,
                    frontend_port=fe_port,
                    abort_on=abort_on,
                    docker="run" if docker is not None else None,
                    auth=auth,
                    resume_mode=resume_mode,
                    on_phase=on_phase,
                )
            except Exception:
                logging.getLogger(__name__).exception("Pipeline run failed for %s", output_dir)
                display.update(index, RunOutcome.FAILURE.value[0].upper())
                return []
            finally:
                if port_pool is not None and fe_port is not None:
                    port_pool.put_nowait(fe_port)

    await display.start()
    try:
        outcomes = await asyncio.gather(*[_run_one(i, d) for i, d in enumerate(output_dirs)])
    finally:
        await display.stop()

    return list(outcomes)


async def _serve_view(
    db_path: Path,
    frontend_port: int,
) -> None:
    """Serve a read-only frontend for one run. Blocks until interrupted."""
    framework_logger = logging.getLogger(f"magelab.view.{db_path.stem}")
    framework_logger.setLevel(logging.INFO)

    view = RunView.from_db(db_path, logger=framework_logger)
    try:
        await serve_view_frontend(view, port=frontend_port)
    finally:
        view.close()


def view_run(
    db_path: Union[str, Path],
    frontend_port: int = 8765,
) -> None:
    """Open a read-only frontend dashboard for a previous run.

    Args:
        db_path: Path to the SQLite database file.
        frontend_port: Port for the frontend dashboard.
    """

    async def _run() -> None:
        _install_sigterm_handler()
        await _serve_view(Path(db_path).resolve(), frontend_port)

    asyncio.run(_run())


def view_run_batch(
    db_paths: list[Union[str, Path]],
    base_frontend_port: int = 8765,
) -> None:
    """Open read-only frontend dashboards for multiple previous runs.

    Each run gets its own port starting from base_frontend_port.

    Args:
        db_paths: Paths to SQLite database files.
        base_frontend_port: Starting port. Run i gets port base_frontend_port + i.
    """

    async def _run() -> None:
        _install_sigterm_handler()
        async with asyncio.TaskGroup() as tg:
            for i, db_path in enumerate(db_paths):
                tg.create_task(_serve_view(Path(db_path).resolve(), base_frontend_port + i))

    asyncio.run(_run())

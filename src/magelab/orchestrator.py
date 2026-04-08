"""
Orchestrator - Runs the multi-agent organization.

Responsibilities:
- Wire TaskStore events to agent queues via registry.enqueue()
- Spawn and manage parallel agent loops (async) or drive rounds (sync)
- Guard against stale events before running agents
- Detect completion
"""

import asyncio
import dataclasses
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .auth import ResolvedAuth
from .events import (
    Event,
    EventOutcome,
    MCPEvent,
    ResumeEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    WireMessageEvent,
)
from .org_config import OrgConfig, ResumeMode, WireNotifications
from .runners.agent_runner import AgentRunner, AgentRunResult
from .runners.claude_runner import ClaudeRunner
from .runners.prompts import (
    PromptContext,
    build_system_prompt,
    default_prompt_formatter,
)
from .state.database import Database
from .state.database_hydration import (
    load_settings_from_db,
    resume_continue,
    resume_fresh,
)
from .state.registry import Registry
from .state.task_schemas import (
    Task,
    TaskStatus,
)
from .state.task_store import TaskStore
from .state.transcript import TranscriptLogger
from .state.wire_store import WireStore
from .tools.mcp import LoadedMCPModule, MCPContext, init_mcp_servers, load_mcp_module
from .tools.validation import validate_all_tool_dependencies, validate_task_assignments

logger = logging.getLogger(__name__)


class RunOutcome(str, Enum):
    """Overall outcome of an orchestrator run.

    Each outcome has a stable exit code used by the CLI so that Docker
    containers can communicate outcomes via process exit code.
    """

    NO_WORK = "no_work"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    TIMEOUT = "timeout"

    @property
    def exit_code(self) -> int:
        return _EXIT_CODE_MAP[self]

    @classmethod
    def from_exit_code(cls, code: int) -> "RunOutcome":
        """Map an exit code back to a RunOutcome. Unknown codes → FAILURE."""
        return next((o for o, c in _EXIT_CODE_MAP.items() if c == code), cls.FAILURE)


# Ordered by severity: 0 = clean exit, higher = worse.
_EXIT_CODE_MAP = {
    RunOutcome.SUCCESS: 0,
    RunOutcome.NO_WORK: 0,
    RunOutcome.PARTIAL: 1,
    RunOutcome.TIMEOUT: 2,
    RunOutcome.FAILURE: 3,
}


# =============================================================================
# Helpers
# =============================================================================


def _copy_session_configs(
    org_config: OrgConfig,
    output_dir: Path,
    logger: logging.Logger,
) -> None:
    """Copy per-agent session config and credentials into .sessions/<agent_id>/.

    Source configs live in .sessions/_configs/ (copied there by the pipeline
    from the user's agent_settings_dir). This function fans them out into
    each agent's session directory, where the LLM backend discovers
    extension points (settings, MCP servers, plugins, skills, etc.).

    If staged credentials exist at .sessions/_credentials/.credentials.json
    (placed there by stage_credentials() for SUB auth), they are symlinked into
    every agent's session directory. All agents share the same file so that
    token refresh (if the SDK does it) updates once for everyone.

    Resolution order for configs: agent session_config_override > role session_config > skip.
    On resume, session config files are merged into existing agent directories
    (new files added, existing files overwritten with the template).
    """
    # --- Fan out credentials to all agents ---
    staged_creds = output_dir / ".sessions" / "_credentials" / ".credentials.json"
    if staged_creds.is_file():
        for agent_id in org_config.agents:
            session_dir = output_dir / ".sessions" / agent_id
            session_dir.mkdir(parents=True, exist_ok=True)
            dest = session_dir / ".credentials.json"
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            os.symlink(staged_creds.resolve(), dest)
        logger.info(f"Symlinked credentials into {len(org_config.agents)} agent session dirs")

    # --- Fan out session configs ---
    if not org_config.settings.agent_settings_dir:
        return

    # Pipeline copies agent_settings_dir into .sessions/_configs/ for Docker.
    # Direct build() callers may not use the pipeline — fall back to the
    # absolute path resolved by from_yaml().
    configs_src = output_dir / ".sessions" / "_configs"
    if not configs_src.is_dir():
        configs_src = Path(org_config.settings.agent_settings_dir)
    if not configs_src.is_dir():
        logger.warning(f"Session config dir not found: {org_config.settings.agent_settings_dir}")
        return

    for agent_id, agent_cfg in org_config.agents.items():
        # Identify session config precedence
        config_rel = None
        if agent_cfg.session_config_override:
            config_rel = agent_cfg.session_config_override
        else:
            role_cfg = org_config.roles.get(agent_cfg.role)
            if role_cfg and role_cfg.session_config:
                config_rel = role_cfg.session_config

        if not config_rel:
            continue

        # Copy session config to agent's session directory
        src = configs_src / config_rel
        session_dir = output_dir / ".sessions" / agent_id
        if src.is_dir():
            shutil.copytree(src, session_dir, dirs_exist_ok=True)
            logger.info(f"Copied session config {src} → {session_dir}")
        else:
            logger.warning(f"Session config directory not found: {src} (agent {agent_id})")


# =============================================================================
# Orchestrator
# =============================================================================


class Orchestrator:
    """
    Runs the multi-agent organization.

    Supports two execution modes:
      - Async (default): agents run as persistent asyncio tasks, pulling from queues.
      - Sync: orchestrator drives discrete rounds, draining queues each round.

    Usage:
        orch = Orchestrator(task_store, registry, runner)
        # initial_tasks: list of (Task, assigned_to, assigned_by) triples
        await orch.run(initial_tasks=[(task, "coder-0", "User")])
        await orch.run(initial_tasks=[(task, "coder-0", "User")], sync=True, sync_max_rounds=10)

    Or via YAML config:
        org_config = OrgConfig.from_yaml("config.yaml")
        orch = await Orchestrator.build(org_config, output_dir, logger)
        await orch.run(initial_tasks=org_config.initial_tasks,
                       initial_messages=org_config.initial_messages,
                       sync=org_config.settings.sync, sync_max_rounds=org_config.settings.sync_max_rounds,
                       sync_round_timeout_seconds=org_config.settings.sync_round_timeout_seconds)
    """

    # =========================================================================
    # Construction
    # =========================================================================

    def __init__(
        self,
        task_store: TaskStore,
        registry: Registry,
        runner: AgentRunner,
        wire_store: WireStore,
        db: Database,
        org_timeout_seconds: float,
        org_prompt: str,
        working_directory: str,
        framework_logger: Optional[logging.Logger] = None,
    ) -> None:
        self.task_store = task_store
        self.registry = registry
        self.runner = runner
        self.wire_store = wire_store
        self._org_timeout_seconds = org_timeout_seconds
        self._org_prompt = org_prompt
        self.working_directory = working_directory
        self._framework_logger = framework_logger or logging.getLogger(__name__)
        self._db = db

        self._running = False
        self._interrupted = False
        self._agent_tasks: dict[str, asyncio.Task] = {}  # agent_id -> asyncio task
        self._event_listeners: list[Callable[[Event], None]] = []
        self._events_to_process: int = 0

        # Run results — set during the run or at finalization
        self.timed_out: bool = False
        self.sync_rounds: Optional[int] = None
        self.outcome: RunOutcome = RunOutcome.NO_WORK
        self.duration_seconds: Optional[float] = None
        self.total_cost_usd: float = 0.0

        # Wire store events to agent queues (+ fan-out to external listeners)
        self.task_store.add_event_listener(self._dispatch_event)
        if self.wire_store.wire_notifications in (WireNotifications.ALL, WireNotifications.EVENT):
            self.wire_store.add_event_listener(self._dispatch_event)

    @classmethod
    async def build(
        cls,
        org_config: OrgConfig,
        output_dir: Path,
        logger: Optional[logging.Logger] = None,
        resume_mode: Optional[ResumeMode] = None,
        auth: Optional[ResolvedAuth] = None,
    ) -> "Orchestrator":
        """Build a fully wired Orchestrator from config.

        Flow:
        1. Record run segment and upsert structural state
        2. Load stores from DB (registry, task_store, wire_store, settings)
        3. Wire up transcript logging, MCP, session configs, credentials
        4. Build runner
        5. On resume: restore session IDs, apply resume logic

        Args:
            org_config: Parsed organization config (structural + settings + run inputs).
            output_dir: Root directory for this run.
            logger: Optional logger for framework-level logging.
            resume_mode: None for fresh run, ResumeMode.CONTINUE to resume
                where agents left off, ResumeMode.FRESH to fail in-progress
                tasks and start clean.
            auth: Resolved authentication credentials. Passed through to the runner.
        """
        logger = logger or logging.getLogger(__name__)

        db_path = output_dir / f"{org_config.settings.org_name}.db"
        if resume_mode is not None and not db_path.exists():
            raise RuntimeError(
                f"Cannot resume: no database found at {db_path}. "
                f"Check that --output-dir points to a previous run and the config name matches."
            )
        db = Database(db_path)
        try:
            # 1. Record run segment and upsert structural state
            org_config_json = json.dumps(org_config.to_dict())
            db.init_run_meta(org_name=org_config.settings.org_name, org_config=org_config_json, resume_mode=resume_mode)

            registry = Registry(framework_logger=logger, db=db)
            registry.register_config(org_config.roles, org_config.agents, org_config.network)

            # 2. Load stores from DB
            settings = load_settings_from_db(db)
            registry.load_from_db()

            task_store = TaskStore(framework_logger=logger, db=db)
            task_store.load_from_db()

            wire_store = WireStore(
                framework_logger=logger,
                db=db,
                wire_notifications=settings.wire_notifications,
                wire_max_unread_per_prompt=settings.wire_max_unread_per_prompt,
            )
            wire_store.load_from_db()

            # 3. Transcript logging, MCP, session configs
            logs_dir = output_dir / "logs"
            logs_dir.mkdir(exist_ok=True)
            transcript_logger = TranscriptLogger(logs_dir)
            transcript_logger.add_listener(db.create_transcript_listener())
            wire_store.add_message_listener(transcript_logger.log_wire_message)

            mcp_modules: dict[str, LoadedMCPModule] = {}
            for server_name, module_path in settings.mcp_modules.items():
                try:
                    mcp_modules[server_name] = load_mcp_module(module_path)
                    logger.info(f"Loaded MCP module '{server_name}' from {module_path}")
                except Exception as e:
                    raise RuntimeError(f"Failed to load MCP module '{server_name}' ({module_path}): {e}") from e
            mcp_servers = {name: loaded.server for name, loaded in mcp_modules.items()}

            _copy_session_configs(org_config, output_dir, logger)

            # 4. Runner
            post_tool_hooks = (
                [lambda agent_id: f"\n\n-----\n[{s}]" if (s := wire_store.unread_summary(agent_id)) else None]
                if settings.wire_notifications in (WireNotifications.ALL, WireNotifications.TOOL)
                else None
            )
            runner = ClaudeRunner(
                task_store=task_store,
                registry=registry,
                permission_mode=settings.org_permission_mode,
                working_directory=str(output_dir / "workspace"),
                agent_timeout_seconds=settings.agent_timeout_seconds,
                wire_store=wire_store,
                mcp_servers=mcp_servers,
                transcript_logger=transcript_logger,
                framework_logger=logger,
                post_tool_hooks=post_tool_hooks,
                auth=auth,
            )

            # 5. Restore session IDs and apply resume logic
            if resume_mode is not None:
                for agent_id, session_id in registry.get_session_ids().items():
                    runner.restore_session(agent_id, session_id)
                if resume_mode == ResumeMode.FRESH:
                    await resume_fresh(db, task_store, registry, logger)
                elif resume_mode == ResumeMode.CONTINUE:
                    resume_continue(db, registry, logger)

        except Exception:
            db.close()
            raise

        orch = cls(
            task_store=task_store,
            registry=registry,
            runner=runner,
            wire_store=wire_store,
            db=db,
            org_timeout_seconds=settings.org_timeout_seconds,
            org_prompt=settings.org_prompt,
            working_directory=str(output_dir / "workspace"),
            framework_logger=logger,
        )

        # Initialize MCP servers that define init() — gives them DB access
        # and event emission. Done after construction so _dispatch_event is bound.
        try:
            if mcp_modules:
                mcp_context = MCPContext(db=db, emit_event=orch._dispatch_event)
                init_mcp_servers(mcp_modules, mcp_context, logger)
        except Exception:
            db.close()
            raise

        return orch

    # =========================================================================
    # Public API
    # =========================================================================

    def load_transcript_entries(self) -> list[dict]:
        """Load all transcript entries from the database.

        Returns a list of dicts with keys: agent_id, entry_type, content.
        Used by the frontend to replay prior transcripts on resume.
        """
        return self._db.load_transcript_entries()

    def add_event_listener(self, fn: Callable[[Event], None]) -> None:
        """Register an external listener that receives every task/wire event.

        Listeners are called synchronously after the event is enqueued to the
        target agent. They must not raise; exceptions are logged and swallowed.
        """
        self._event_listeners.append(fn)

    async def run(
        self,
        initial_tasks: Optional[list[tuple[Task, str, str]]] = None,
        initial_messages: Optional[list[dict]] = None,
        sync: bool = False,
        sync_max_rounds: Optional[int] = None,
        sync_round_timeout_seconds: Optional[float] = None,
    ) -> None:
        """
        Run the multi-agent organization.

        Args:
            initial_tasks: List of (Task, assigned_to, assigned_by) triples to create and assign.
            initial_messages: Wire messages to send at startup, after initial tasks
                are created. Each dict has keys matching wire_store.create_wire().
            sync: If True, run in synchronized round-based mode (no agent loops).
            sync_max_rounds: Maximum number of rounds. Required when sync=True.
            sync_round_timeout_seconds: Max time per sync round. None = no per-round limit.
        """
        if sync_max_rounds is not None and not sync:
            raise ValueError("sync_max_rounds can only be specified when sync=True")
        if sync and sync_max_rounds is None:
            raise ValueError("sync_max_rounds is required when sync=True")
        if sync_round_timeout_seconds is not None and not sync:
            raise ValueError("sync_round_timeout_seconds can only be specified when sync=True")
        if sync:
            await self._run_with_lifecycle(
                initial_tasks,
                initial_messages,
                lambda: self._run_sync_rounds(sync_max_rounds, sync_round_timeout_seconds),
            )
        else:

            async def _async_work() -> None:
                for agent_id in self.registry.list_agent_ids():
                    self._agent_tasks[agent_id] = asyncio.create_task(self._agent_loop(agent_id))
                await self._wait_for_completion()

            await self._run_with_lifecycle(initial_tasks, initial_messages, _async_work)

    # =========================================================================
    # Event dispatch and recording
    # =========================================================================

    def _dispatch_event(self, event: Event) -> None:
        """Dispatch a store event: log to DB, enqueue to agent, fan-out to listeners."""
        self._log_event_to_db(event)
        enqueued = self.registry.enqueue(event.target_id, event)
        if not enqueued:
            self._db.update_event_outcome(event.event_id, EventOutcome.DROPPED_AT_ENQUEUE)
        for fn in self._event_listeners:
            try:
                fn(event)
            except Exception:
                logger.exception("Error in event listener")

    def _log_event_to_db(self, event: Event) -> None:
        """Insert event into DB at enqueue time (outcome=NULL)."""
        source_id = getattr(event, "source_id", None)
        task_id = getattr(event, "task_id", None)
        wire_id = getattr(event, "wire_id", None)

        # Serialize event-specific fields (everything beyond BaseEvent) as JSON payload.
        payload = {}
        base_fields = {"event_id", "target_id", "timestamp"}
        # Also exclude fields stored as columns
        column_fields = {"source_id", "task_id", "wire_id"}
        for f in dataclasses.fields(event):
            if f.name not in base_fields and f.name not in column_fields:
                val = getattr(event, f.name)
                # Convert enums and other non-JSON types to strings
                if hasattr(val, "value"):
                    val = val.value
                elif isinstance(val, list):
                    val = [v.model_dump(mode="json") if hasattr(v, "model_dump") else v for v in val]
                payload[f.name] = val

        self._db.insert_event(
            event_id=event.event_id,
            event_type=type(event).__name__,
            target_agent_id=event.target_id,
            source_agent_id=source_id,
            task_id=task_id,
            wire_id=wire_id,
            timestamp=event.timestamp.isoformat(),
            payload=json.dumps(payload) if payload else None,
        )

    def _record_dispatch(self, event: Event, result: AgentRunResult) -> None:
        """Record dispatch results to DB."""
        if result.session_id:
            self.registry.update_session(event.target_id, result.session_id)

        self._db.update_event_finished(
            event.event_id,
            num_turns=result.num_turns,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            error=result.error,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    # =========================================================================
    # Run lifecycle
    # =========================================================================

    async def _run_with_lifecycle(
        self,
        initial_tasks: Optional[list[tuple[Task, str, str]]],
        initial_messages: Optional[list[dict]],
        work_fn: Callable[[], Awaitable[None]],
    ) -> None:
        """Run lifecycle wrapper shared by async and sync modes.

        Handles the common structure around the mode-specific work:
        1. Create initial tasks
        2. Send initial messages
        3. Run ``work_fn()`` with global timeout — in async mode this is the agent
           event loops, in sync mode this is the round loop (``_run_sync_rounds``)
        4. On completion or timeout: shutdown agents, capture stats, finalize DB

        work_fn is a zero-arg callable that returns an awaitable. It is called
        AFTER validation so that coroutines are not leaked if validation fails.
        """
        self._running = True
        self._interrupted = False
        self.timed_out = False
        self.sync_rounds = None
        self.outcome = RunOutcome.NO_WORK

        try:
            if not initial_tasks and not initial_messages:
                self._framework_logger.warning("No initial tasks or messages — agents will start with empty queues")
            await self._validate_and_create_initial_tasks(initial_tasks)
            await self._send_initial_messages(initial_messages)

            try:
                await asyncio.wait_for(work_fn(), timeout=self._org_timeout_seconds)
            except asyncio.TimeoutError:
                self._framework_logger.warning(f"Global timeout reached after {self._org_timeout_seconds}s")
                self.timed_out = True

        except asyncio.CancelledError:
            self._interrupted = True
            raise

        finally:
            self._running = False
            await self._shutdown(interrupt_immediately=self.timed_out or self._interrupted)
            self._finalize_db()

    async def _shutdown(self, grace_period: float = 10.0, interrupt_immediately: bool = False) -> None:
        """Graceful shutdown: interrupt agents if needed, then cancel persistent tasks.

        Works for both async and sync modes. The interrupt step kills in-flight
        LLM calls/subprocesses. The task cancellation step (async-only) cleans
        up persistent agent loop tasks.

        Args:
            grace_period: Seconds to wait for agent tasks to exit before force-cancelling.
            interrupt_immediately: If True (e.g. timeout), interrupt agents right away
                instead of waiting for them to finish their current turn.
        """
        if interrupt_immediately:
            await asyncio.gather(
                *(self.runner.interrupt_agent(agent_id) for agent_id in self.registry.list_agent_ids()),
                return_exceptions=True,
            )

        # Cancel persistent agent loop tasks (async mode only)
        if not self._agent_tasks:
            return

        # Wait for agents to exit (they see self._running = False and stop after current turn)
        _, pending = await asyncio.wait(
            self._agent_tasks.values(),
            timeout=grace_period,
        )

        # Force-cancel anything that didn't stop
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        self._agent_tasks.clear()

    @staticmethod
    def _compute_outcome(timed_out: bool, tasks_succeeded: int, tasks_failed: int, tasks_open: int) -> RunOutcome:
        """Compute the overall run outcome from task counts."""
        if timed_out:
            return RunOutcome.TIMEOUT
        total = tasks_succeeded + tasks_failed + tasks_open
        if total == 0:
            return RunOutcome.NO_WORK
        if tasks_succeeded == total:
            return RunOutcome.SUCCESS
        if tasks_succeeded > 0:
            return RunOutcome.PARTIAL
        return RunOutcome.FAILURE

    def _finalize_db(self) -> None:
        """Aggregate run stats from DB, compute outcome, write to run_meta, and close."""
        try:
            end_time = datetime.now(timezone.utc)
            summary = self._db.compute_run_summary()
            task_counts = self.task_store.compute_task_counts()

            # Compute duration from DB start_time
            start_time_str = summary["start_time"]
            duration_seconds = None
            if start_time_str:
                start_time = datetime.fromisoformat(start_time_str)
                duration_seconds = (end_time - start_time).total_seconds()

            self.outcome = self._compute_outcome(
                self.timed_out,
                task_counts["succeeded"],
                task_counts["failed"],
                task_counts["open"],
            )
            self.duration_seconds = duration_seconds
            self.total_cost_usd = summary["total_cost_usd"]

            self._db.finalize_run(
                end_time=end_time.isoformat(),
                duration_seconds=duration_seconds,
                timed_out=self.timed_out,
                outcome=self.outcome,
                sync_rounds=self.sync_rounds,
                total_cost_usd=summary["total_cost_usd"],
                tasks_succeeded=task_counts["succeeded"],
                tasks_failed=task_counts["failed"],
                tasks_open=task_counts["open"],
                rate_limited_429=summary["rate_limited_429"],
                api_overloaded_529=summary["api_overloaded_529"],
                api_error_other=summary["api_error_other"],
            )
            self.runner.shutdown()
        finally:
            self._db.close()

    async def _wait_for_completion(self) -> None:
        """Wait until all agents are quiescent (idle with empty queues, no events in flight)."""
        while self._running:
            if self._events_to_process == 0 and self.registry.all_quiescent():
                self._framework_logger.info("All agents quiescent, stopping event loop")
                break
            await asyncio.sleep(0.5)

    async def _validate_and_create_initial_tasks(self, initial_tasks: Optional[list[tuple[Task, str, str]]]) -> None:
        """Validate config, create initial tasks, and emit events."""
        agent_to_tools = {s.agent_id: set(s.tools) for s in self.registry.list_agent_snapshots()}
        agent_to_connection_tools = self.registry.compute_connection_tools()
        errors: list[str] = []

        dep_errors, dep_warnings = validate_all_tool_dependencies(agent_to_tools, agent_to_connection_tools)
        errors.extend(dep_errors)
        for w in dep_warnings:
            self._framework_logger.warning(w)

        if initial_tasks:
            task_errors, task_warnings = validate_task_assignments(
                initial_tasks, agent_to_tools, agent_to_connection_tools
            )
            errors.extend(task_errors)
            for w in task_warnings:
                self._framework_logger.warning(w)

        if errors:
            raise ValueError(f"Invalid configuration: {'; '.join(errors)}")

        if initial_tasks:
            for task, agent_id, assigned_by in initial_tasks:
                existing = await self.task_store.get_task(task.id)
                if existing:
                    self._framework_logger.info(f"Task '{task.id}' already exists (from prior run), skipping creation")
                    continue
                await self.task_store.create(task, assigned_to=agent_id, assigned_by=assigned_by)

    async def _send_initial_messages(self, initial_messages: Optional[list[dict]]) -> None:
        """Send initial wire messages at startup.

        Messages are sent after initial tasks so that an agent with both
        a task and a message queued processes the task first (FIFO).
        """
        if not initial_messages:
            return

        for i, msg in enumerate(initial_messages):
            wire_id = msg.get("wire_id") or f"wire_{i}"
            participants = msg["participants"]
            sender = msg["sender"]
            body = msg["body"]
            task_id = msg.get("task_id")

            existing = await self.wire_store.get_wire(wire_id)
            if existing:
                self._framework_logger.info(f"Wire '{wire_id}' already exists (from prior run), skipping creation")
                continue

            await self.wire_store.create_wire(
                wire_id=wire_id,
                participants=participants,
                sender=sender,
                body=body,
                task_id=task_id,
            )
            self._framework_logger.info(f"Sent initial message on wire '{wire_id}' from {sender}")

    # =========================================================================
    # Agent execution
    # =========================================================================

    async def _agent_loop(self, agent_id: str) -> None:
        """Main loop for a single agent. Runs as an asyncio task.

        Pulls events from queue, checks staleness, then processes.
        """
        self._framework_logger.info(f"Agent {agent_id} loop started")

        while self._running:
            event = await self.registry.dequeue(agent_id, timeout=1.0)
            if event is None:
                continue
            # Race condition guard: between dequeue (queue now empty) and
            # mark_working inside _run_agent_for_event, the agent appears
            # idle with an empty queue. _wait_for_completion would see false
            # quiescence. The counter stays > 0 for the entire span.
            # If you add another call site for _run_agent_for_event in async
            # mode, it must also manage this counter.
            self._events_to_process += 1
            try:
                await self._run_agent_for_event(agent_id, event)
            finally:
                self._events_to_process -= 1

        self._framework_logger.info(f"Agent {agent_id} loop stopped")

    async def _run_sync_rounds(self, sync_max_rounds: int, sync_round_timeout_seconds: Optional[float] = None) -> None:
        """Execute sync rounds until convergence, all tasks done, or sync_max_rounds reached."""
        for round_num in range(1, sync_max_rounds + 1):
            # Drain all agent queues
            agent_events: dict[str, list[Event]] = {}
            for agent_id in self.registry.list_agent_ids():
                events = self.registry.drain_queue(agent_id)
                if events:
                    agent_events[agent_id] = events

            if not agent_events:
                self._framework_logger.info(f"No events after round {round_num - 1} — converged")
                return

            self._framework_logger.info(
                f"Round {round_num}: dispatching events to {len(agent_events)} agents "
                f"({sum(len(v) for v in agent_events.values())} events total)"
            )

            # Run all agents concurrently — sequential per agent, concurrent across agents
            round_coro = asyncio.gather(
                *(self._run_agent_events_sequential(agent_id, events) for agent_id, events in agent_events.items()),
                return_exceptions=True,
            )
            if sync_round_timeout_seconds is not None:
                try:
                    await asyncio.wait_for(round_coro, timeout=sync_round_timeout_seconds)
                except asyncio.TimeoutError:
                    self._framework_logger.warning(f"Round {round_num} timed out after {sync_round_timeout_seconds}s")
            else:
                await round_coro

            self.sync_rounds = round_num

        self._framework_logger.warning(f"Max rounds ({sync_max_rounds}) reached")

    async def _run_agent_events_sequential(self, agent_id: str, events: list[Event]) -> None:
        """Process an agent's events sequentially within a sync round.

        Each event gets its own call. Wire events naturally deduplicate: the first
        one fetches all unread wires, marks them read, and subsequent wire events
        go stale.
        """
        for event in events:
            await self._run_agent_for_event(agent_id, event)

    async def _run_agent_for_event(self, agent_id: str, event: Event) -> None:
        """Run an agent in response to an event.

        Steps: guard → resolve prompt → mark agent state → mark read → run LLM → handle outcome → mark idle.
        """
        # 1. Guard: agent must exist
        agent = self.registry.get_agent_snapshot(agent_id)
        if not agent:
            self._framework_logger.warning(f"Agent {agent_id} not found in registry, skipping event")
            return

        # 2. Resolve prompt (MCP, wire, or task path)
        task_id: Optional[str] = None
        wire_cursors: list[tuple[str, int]] = []

        if isinstance(event, MCPEvent):
            prompt = event.payload
        elif isinstance(event, WireMessageEvent):
            resolved = await self._resolve_wire_prompt(agent_id, event, set(agent.tools))
            if not resolved:
                self._db.update_event_outcome(event.event_id, EventOutcome.STALE_AT_DELIVERY)
                return
            prompt, wire_cursors = resolved
        else:
            try:
                resolved = await self._resolve_task_prompt(agent_id, event, set(agent.tools))
            except RuntimeError as e:
                self._framework_logger.error(str(e))
                self._db.update_event_outcome(event.event_id, EventOutcome.ERROR_AT_DELIVERY)
                is_review = isinstance(event, ReviewRequestedEvent)
                await self._handle_task_failure(agent_id, is_review, event.task_id, details=str(e))
                return
            if not resolved:
                self._db.update_event_outcome(event.event_id, EventOutcome.STALE_AT_DELIVERY)
                return
            prompt, task_id = resolved
        self._db.update_event_outcome(event.event_id, EventOutcome.DELIVERED)

        # 3. Mark agent state, mark read, run agent, handle outcome, mark idle
        system_prompt = build_system_prompt(agent_id, agent.role_prompt, self._org_prompt, self.working_directory)
        is_review = isinstance(event, ReviewRequestedEvent) or (isinstance(event, ResumeEvent) and event.was_reviewing)
        try:
            if is_review:
                self.registry.mark_reviewing(agent_id, event.task_id)
            else:
                self.registry.mark_working(agent_id, task_id)

            # Mark wire messages as read before running, so tool notifications
            # during the run don't re-report messages already in the prompt
            for wid, cursor in wire_cursors:
                await self.wire_store.mark_read(wid, agent_id, cursor)
            run_result = await self.runner.run_agent(agent_id, system_prompt, prompt)
            self._record_dispatch(event, run_result)
            if run_result.error:
                self._framework_logger.error(f"Agent {agent_id} failed: {run_result.error}")
                if task_id is not None:
                    await self._handle_task_failure(agent_id, is_review, task_id, details=run_result.error)
            self.registry.mark_idle(agent_id)

        except asyncio.CancelledError:
            # Agent was force-cancelled (org timeout, Ctrl+C, etc.).
            self._framework_logger.info(f"Agent {agent_id} force-cancelled")
            session_id = self.runner.get_session(agent_id)
            if session_id:
                # Session exists from a prior completed turn — agent can resume.
                # Stay WORKING so resume_continue sends a ResumeEvent.
                self.registry.update_session(agent_id, session_id)
            else:
                # No session — work is unresumable. Treat as a failure:
                # finalize the event, fail the task, mark idle.
                error_result = AgentRunResult(error="Agent force-cancelled (no session)")
                self._record_dispatch(event, error_result)
                if task_id is not None:
                    await self._handle_task_failure(
                        agent_id, is_review, task_id, details="Force-cancelled before session established"
                    )
                self.registry.mark_idle(agent_id)
            raise

        except Exception as e:
            self._framework_logger.exception(f"Error running agent {agent_id}: {e}")
            error_result = AgentRunResult(error=str(e))
            self._record_dispatch(event, error_result)
            if task_id is not None:
                await self._handle_task_failure(agent_id, is_review, task_id, details=str(e))
            self.registry.mark_idle(agent_id)

    async def _resolve_wire_prompt(
        self,
        agent_id: str,
        event: WireMessageEvent,
        agent_tools: set[str],
    ) -> Optional[tuple[str, list[tuple[str, int]]]]:
        """Resolve a wire event into a prompt by fetching ALL unread wires for the agent.

        Rather than processing just the triggering event's wire, this fetches every
        wire with unread messages. This way a single wire event delivers all pending
        messages, and subsequent wire events naturally go stale (0 unread).

        Returns:
            None if the triggering event is stale (caller should skip the LLM call).
            Otherwise a 2-tuple:
            - prompt: the formatted prompt string to send to the LLM
            - cursors: (wire_id, message_index) pairs for the caller to mark_read
              before running the agent (so tool notifications don't re-report them)
        """
        # Quick stale check on the triggering event
        if await self.wire_store.is_event_stale(event, agent_id):
            self._framework_logger.debug(f"Skipping stale WireMessageEvent for {agent_id}")
            return None

        # Fetch ALL unread wires for this agent (not just the triggering one)
        limit = self.wire_store.wire_max_unread_per_prompt
        unread_wires = await self.wire_store.get_all_unread(agent_id, limit=limit)
        conversations: list[str] = []
        cursors: list[tuple[str, int]] = []

        for wire in unread_wires:
            text, cursor = wire.format_conversation(agent_id)
            conversations.append(text)
            cursors.append((wire.wire_id, cursor))

        if not conversations:
            return None

        prompt = default_prompt_formatter(
            PromptContext(event=event, agent_tools=agent_tools, wire_conversations=conversations)
        )
        return prompt, cursors

    async def _resolve_task_prompt(
        self,
        agent_id: str,
        event: Event,
        agent_tools: set[str],
    ) -> Optional[tuple[str, Optional[str]]]:
        """Resolve a task event into a prompt.

        Returns (prompt, task_id) or None if the event should be skipped.
        Handles staleness, task lookup, prompt formatting, and ASSIGNED→IN_PROGRESS.
        """
        # Staleness and task lookup (skipped for taskless events like ResumeEvent without a task)
        task = None
        other_open_tasks: list = []
        if event.task_id:
            if await self.task_store.is_event_stale(event):
                self._framework_logger.debug(f"Skipping stale {type(event).__name__} for {agent_id}")
                return None
            # Task lookup
            task = await self.task_store.get_task(event.task_id)
            if not task:
                raise RuntimeError(f"{type(event).__name__} for task {event.task_id} but task not found")
            # Gather other open tasks for context
            all_open = await self.task_store.list_tasks(assigned_to=agent_id, is_finished=False)
            other_open_tasks = [t for t in all_open if t.id != event.task_id and not t.is_in_review()]

        # Prompt formatting
        prompt = default_prompt_formatter(
            PromptContext(event=event, task=task, agent_tools=agent_tools, other_open_tasks=other_open_tasks)
        )
        if prompt is None:
            raise RuntimeError(f"Prompt formatter returned None for {type(event).__name__} targeting {agent_id}")

        # Transition ASSIGNED → IN_PROGRESS
        if isinstance(event, TaskAssignedEvent):
            try:
                await self.task_store.mark_in_progress(event.task_id)
            except ValueError:
                self._framework_logger.debug(f"Task {event.task_id} already moved past ASSIGNED, skipping")
                return None

        return prompt, event.task_id

    async def _handle_task_failure(
        self,
        agent_id: str,
        is_review: bool,
        task_id: Optional[str],
        details: str,
    ) -> None:
        """
        Handle task failure when an agent errors out.

        Reviewer failure is recoverable: only that review is marked failed,
        the review round continues with other reviewers.
        Worker failure is terminal: the task is marked FAILED.
        """
        if task_id is None:
            return
        try:
            if is_review:
                await self.task_store.mark_review_failed(task_id, agent_id)
            else:
                await self.task_store.mark_finished(task_id, TaskStatus.FAILED, details, force=True)
        except ValueError as e:
            self._framework_logger.warning(f"Could not mark failure for {agent_id} on task {task_id}: {e}")

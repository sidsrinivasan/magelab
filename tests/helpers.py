"""Shared test factories for magelab tests."""

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from magelab.orchestrator import Orchestrator
from magelab.org_config import OrgConfig, OrgSettings
from magelab.registry_config import AgentConfig, RoleConfig
from magelab.state.database import Database
from magelab.state.registry import Registry
from magelab.state.task_schemas import ReviewRecord, Task, TaskStatus
from magelab.state.task_store import TaskStore
from magelab.state.wire_store import WireStore

if TYPE_CHECKING:
    from .conftest import MockRunner

_test_logger = logging.getLogger("test")


# =============================================================================
# Minimal Task factory
# =============================================================================


def make_task(
    id: str = "task-1",
    title: str = "Test Task",
    description: str = "A test task",
    review_required: bool = False,
    status: TaskStatus = TaskStatus.CREATED,
) -> Task:
    """Create a minimal Task for testing."""
    t = Task(id=id, title=title, description=description, review_required=review_required)
    if status != TaskStatus.CREATED:
        t.update_status(status)
    return t


def make_review_record(
    reviewer_id: str = "reviewer-1",
    requester_id: str = "worker-1",
    request_message: str = "Please review",
    round_number: int = 1,
) -> ReviewRecord:
    """Create a ReviewRecord for testing."""
    return ReviewRecord(
        reviewer_id=reviewer_id,
        requester_id=requester_id,
        request_message=request_message,
        round_number=round_number,
    )


# =============================================================================
# Minimal OrgConfig fixtures
# =============================================================================


def make_role(
    name: str = "worker",
    role_prompt: str = "You are a worker.",
    tools: list[str] | None = None,
    model: str = "test-model",
    max_turns: int = 10,
) -> RoleConfig:
    """Create a minimal RoleConfig for testing."""
    return RoleConfig(
        name=name,
        role_prompt=role_prompt,
        tools=tools if tools is not None else ["worker", "claude_basic"],
        model=model,
        max_turns=max_turns,
    )


def make_agent_config(
    agent_id: str = "worker-0",
    role: str = "worker",
) -> AgentConfig:
    """Create a minimal AgentConfig for testing."""
    return AgentConfig(agent_id=agent_id, role=role)


def make_org_config(
    name: str = "test-org",
    roles: dict[str, RoleConfig] | None = None,
    agents: dict[str, AgentConfig] | None = None,
) -> OrgConfig:
    """Create a minimal valid OrgConfig for testing."""
    if roles is None:
        roles = {"worker": make_role()}
    if agents is None:
        agents = {"worker-0": make_agent_config()}
    return OrgConfig(roles=roles, agents=agents, settings=OrgSettings(org_name=name))


# =============================================================================
# Orchestrator test setup (shared across test_orchestrator*.py files)
# =============================================================================


def make_orch_org(
    roles: dict[str, RoleConfig] | None = None,
    agents: dict[str, AgentConfig] | None = None,
    tmp_dir: Path | None = None,
) -> tuple[TaskStore, Registry, Database]:
    """Create a standard org setup for orchestrator testing, including a temp DB.

    Returns (store, registry, db). Caller provides their own runner.
    """

    if roles is None:
        roles = {
            "pm": RoleConfig(
                name="pm", role_prompt="You manage tasks.", tools=["management"], model="test", max_turns=10
            ),
            "coder": RoleConfig(
                name="coder",
                role_prompt="You write code.",
                tools=["worker", "claude_basic"],
                model="test",
                max_turns=10,
            ),
            "reviewer": RoleConfig(
                name="reviewer", role_prompt="You review code.", tools=["claude_reviewer"], model="test", max_turns=5
            ),
        }
    if agents is None:
        agents = {
            "pm": AgentConfig(agent_id="pm", role="pm"),
            "coder-0": AgentConfig(agent_id="coder-0", role="coder"),
            "reviewer-0": AgentConfig(agent_id="reviewer-0", role="reviewer"),
        }

    if tmp_dir is None:
        tmp_dir = Path(tempfile.mkdtemp())
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_dir / "org.db")
    db.init_run_meta(org_name="test", org_config="{}")

    store = TaskStore(framework_logger=_test_logger, db=db)
    registry = Registry(framework_logger=_test_logger, db=db)
    registry.register_config(roles, agents)
    return store, registry, db


def make_orchestrator(
    store: TaskStore,
    registry: Registry,
    runner: "MockRunner",
    db: Database,
    global_timeout: float = 30.0,
    org_prompt: str = "Test org",
) -> Orchestrator:
    """Create an Orchestrator with test defaults."""
    return Orchestrator(
        store,
        registry,
        runner,
        WireStore(framework_logger=_test_logger, db=db),
        db,
        global_timeout,
        org_prompt,
        "/test/workspace",
    )

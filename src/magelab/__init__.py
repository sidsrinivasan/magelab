"""
magelab — An orchestration and experimentation framework for multi-agent LLMs.

Usage:
    from magelab import OrgConfig, run_pipeline, run_pipeline_batch

    config = OrgConfig.from_yaml("config.yaml")
    asyncio.run(run_pipeline(config_path="config.yaml", stages=[setup, evaluate], output_dir=output))

All other types (events, schemas, stores, runners, tools) are available
via submodule imports, e.g. ``from magelab.state.task_schemas import Task``.
"""

from .auth import ResolvedAuth, resolve_api_key, resolve_sub
from .orchestrator import RunOutcome
from .org_config import OrgConfig, OrgSettings, ResumeMode
from .pipeline import run_in_workspace, run_pipeline, run_pipeline_batch, view_run, view_run_batch
from .pipeline.execution import StageFn
from .view import RunView

__all__ = [
    "ResolvedAuth",
    "resolve_api_key",
    "resolve_sub",
    "OrgConfig",
    "OrgSettings",
    "ResumeMode",
    "run_pipeline",
    "run_pipeline_batch",
    "run_in_workspace",
    "view_run",
    "view_run_batch",
    "RunOutcome",
    "RunView",
    "StageFn",
]

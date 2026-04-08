"""
magelab.pipeline — Pipeline execution, batching, and viewing.

Provides the stage-based pipeline model for running organizations with
setup/intervention/eval hooks, concurrent batch execution, and viewing
previous runs.
"""

from ..orchestrator import RunOutcome
from .docker import run_in_workspace
from .execution import StageFn, run_pipeline, run_pipeline_batch, view_run, view_run_batch

__all__ = [
    "run_pipeline",
    "run_pipeline_batch",
    "run_in_workspace",
    "view_run",
    "view_run_batch",
    "StageFn",
    "RunOutcome",
]

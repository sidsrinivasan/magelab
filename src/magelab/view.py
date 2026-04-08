"""RunView — read-only access to a completed run.

Lightweight container that holds only what the frontend dashboard needs
to display a finished run: stores for state, DB for transcripts, and
run results. Does not construct a ClaudeRunner, MCP servers, or session
configs. Requires only the DB path — no YAML config needed.
"""

import dataclasses
import logging
from pathlib import Path
from typing import Optional

from .orchestrator import RunOutcome
from .state.database import Database
from .state.database_hydration import load_settings_from_db
from .state.registry import Registry
from .state.task_store import TaskStore
from .state.wire_store import WireStore


@dataclasses.dataclass(frozen=True)
class RunView:
    """Lightweight read-only view of a completed run.

    Usage:
        view = RunView.from_db(Path("run_output/myorg.db"))
        try:
            transcripts = view.load_transcript_entries()
            ...
        finally:
            view.close()
    """

    task_store: TaskStore
    wire_store: WireStore
    registry: Registry
    db: Database
    working_directory: Optional[str]
    org_name: str = "unknown"
    outcome: RunOutcome = RunOutcome.NO_WORK
    duration_seconds: Optional[float] = None
    total_cost_usd: float = 0.0

    @classmethod
    def from_db(
        cls,
        db_path: Path,
        logger: Optional[logging.Logger] = None,
    ) -> "RunView":
        """Build a RunView from a DB file. No YAML config needed.

        Args:
            db_path: Path to the SQLite database file.
            logger: Optional framework logger.

        Returns:
            A RunView populated with stores and run metadata from the database.

        Raises:
            RuntimeError: If no database file is found at the given path.
        """
        logger = logger or logging.getLogger(__name__)

        if not db_path.exists():
            raise RuntimeError(f"Cannot view: no database found at {db_path}")
        db = Database(db_path)

        try:
            settings = load_settings_from_db(db)

            registry = Registry(framework_logger=logger, db=db)
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

            # Read run results from the finalized run_meta row
            meta = db.load_run_meta()
            if not meta:
                raise RuntimeError(f"Cannot view: database at {db_path} has no run metadata")
            outcome = RunOutcome(meta["outcome"]) if meta["outcome"] else RunOutcome.NO_WORK
            duration_seconds = meta["duration_seconds"]
            total_cost_usd = meta["total_cost_usd"] or 0.0

            working_directory = str(db_path.parent / "workspace")
            org_name = meta["org_name"]

        except Exception:
            db.close()
            raise

        return cls(
            task_store=task_store,
            wire_store=wire_store,
            registry=registry,
            db=db,
            working_directory=working_directory,
            org_name=org_name,
            outcome=outcome,
            duration_seconds=duration_seconds,
            total_cost_usd=total_cost_usd,
        )

    def load_transcript_entries(self) -> list[dict]:
        """Load all transcript entries from the database.

        Returns a list of dicts with keys: agent_id, entry_type, content.
        """
        return self.db.load_transcript_entries()

    def close(self) -> None:
        """Close the underlying database connection."""
        self.db.close()

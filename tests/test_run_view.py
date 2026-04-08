"""Tests for RunView — read-only access to a completed run.

Tests RunView.from_db against a real seeded SQLite database, verifying
constructor logic, transcript loading, close behavior, and error paths.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from magelab.orchestrator import Orchestrator, RunOutcome
from magelab.org_config import OrgConfig
from magelab.state.database import Database
from magelab.state.task_schemas import TaskStatus
from magelab.view import RunView

from .conftest import MockRunner

import logging

_test_logger = logging.getLogger("test")


# =============================================================================
# Helpers — seed a real DB the way the orchestrator does
# =============================================================================


def _seed_db(tmp_path: Path) -> Path:
    """Create a DB with a completed run: one task succeeded, one agent, transcripts."""
    import yaml

    config = {
        "settings": {"org_name": "test_org", "org_prompt": "Test", "org_timeout_seconds": 10},
        "roles": {
            "worker": {"name": "worker", "role_prompt": "Work.", "tools": ["worker"], "model": "test", "max_turns": 10}
        },
        "agents": {"worker-0": {"agent_id": "worker-0", "role": "worker"}},
        "initial_tasks": [
            {"id": "task-1", "title": "Test Task", "description": "Do something", "assigned_to": "worker-0"}
        ],
    }
    config_path = tmp_path / "test_org.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "workspace").mkdir()

    return config_path, output_dir


async def _run_and_close(config_path, output_dir, insert_transcripts: bool = True) -> Path:
    """Run a real orchestrator with MockRunner, return the DB path."""
    org_config = OrgConfig.from_yaml(str(config_path))
    runner = MockRunner()

    with patch("magelab.orchestrator.ClaudeRunner", return_value=runner):
        orch = await Orchestrator.build(org_config, output_dir, resume_mode=None)

    async def finish():
        await orch.task_store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

    runner.side_effects["worker-0"] = [finish]
    await orch.run(initial_tasks=org_config.initial_tasks)

    db_path = output_dir / "test_org.db"

    # Insert transcript entries after the run, using a fresh connection
    # (the orchestrator's finalize_run_meta closes its connection).
    if insert_transcripts:
        db = Database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        db.insert_transcript_entry("worker-0", "message", "Hello world", now, turn_number=1)
        db.insert_transcript_entry("worker-0", "tool_call", '{"name": "read"}', now, turn_number=2)
        db.close()

    return db_path


# =============================================================================
# RunView.from_db
# =============================================================================


class TestFromDB:
    @pytest.mark.asyncio
    async def test_loads_outcome(self, tmp_path):
        """from_db correctly loads the run outcome from DB."""
        config_path, output_dir = _seed_db(tmp_path)
        db_path = await _run_and_close(config_path, output_dir)

        view = RunView.from_db(db_path)
        try:
            assert view.outcome == RunOutcome.SUCCESS
        finally:
            view.close()

    @pytest.mark.asyncio
    async def test_loads_cost(self, tmp_path):
        """from_db loads cost > 0 from a completed run."""
        config_path, output_dir = _seed_db(tmp_path)
        db_path = await _run_and_close(config_path, output_dir)

        view = RunView.from_db(db_path)
        try:
            assert view.total_cost_usd > 0.0
        finally:
            view.close()

    @pytest.mark.asyncio
    async def test_loads_stores(self, tmp_path):
        """from_db populates task_store and registry from the DB."""
        config_path, output_dir = _seed_db(tmp_path)
        db_path = await _run_and_close(config_path, output_dir)

        view = RunView.from_db(db_path)
        try:
            tasks = await view.task_store.list_tasks()
            assert len(tasks) == 1
            assert tasks[0].id == "task-1"
            assert tasks[0].status == TaskStatus.SUCCEEDED

            agents = view.registry.list_agent_snapshots()
            assert len(agents) > 0
        finally:
            view.close()

    @pytest.mark.asyncio
    async def test_loads_org_name(self, tmp_path):
        """from_db sets org_name from DB metadata."""
        config_path, output_dir = _seed_db(tmp_path)
        db_path = await _run_and_close(config_path, output_dir)

        view = RunView.from_db(db_path)
        try:
            assert view.org_name == "test_org"
        finally:
            view.close()

    def test_no_db_raises(self, tmp_path):
        """from_db raises RuntimeError when no DB file exists."""
        with pytest.raises(RuntimeError, match="Cannot view"):
            RunView.from_db(tmp_path / "nonexistent.db")


# =============================================================================
# RunView.load_transcript_entries
# =============================================================================


class TestLoadTranscripts:
    @pytest.mark.asyncio
    async def test_loads_real_transcripts(self, tmp_path):
        """load_transcript_entries returns entries inserted into the DB."""
        config_path, output_dir = _seed_db(tmp_path)
        db_path = await _run_and_close(config_path, output_dir)

        view = RunView.from_db(db_path)
        try:
            entries = view.load_transcript_entries()
            assert len(entries) == 2
            assert entries[0]["agent_id"] == "worker-0"
            assert entries[0]["entry_type"] == "message"
            assert entries[0]["content"] == "Hello world"
            assert entries[1]["entry_type"] == "tool_call"
        finally:
            view.close()

    @pytest.mark.asyncio
    async def test_empty_when_no_transcripts(self, tmp_path):
        """load_transcript_entries returns [] when no transcripts were logged."""
        config_path, output_dir = _seed_db(tmp_path)
        db_path = await _run_and_close(config_path, output_dir, insert_transcripts=False)

        view = RunView.from_db(db_path)
        try:
            assert view.load_transcript_entries() == []
        finally:
            view.close()


# =============================================================================
# RunView.close
# =============================================================================


class TestClose:
    @pytest.mark.asyncio
    async def test_close_prevents_further_queries(self, tmp_path):
        """After close(), DB queries should fail."""
        config_path, output_dir = _seed_db(tmp_path)
        db_path = await _run_and_close(config_path, output_dir)

        view = RunView.from_db(db_path)
        view.close()

        # Attempting to load transcripts after close should raise
        with pytest.raises(Exception):
            view.load_transcript_entries()

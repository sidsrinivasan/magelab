"""Pytest configuration and shared fixtures for magelab tests."""

import logging
import sqlite3

import pytest

from magelab.runners.agent_runner import AgentRunner, AgentRunResult
from magelab.state.database import Database


@pytest.fixture
def logger():
    """Shared test logger for store constructors."""
    return logging.getLogger("test")


def open_db_for_query(db: Database):
    """Open a fresh read-only connection to the DB file for test assertions."""
    conn = sqlite3.connect(str(db._path))
    conn.row_factory = sqlite3.Row
    return conn


def get_agent_dispatches(db: Database, agent_id: str) -> list[dict]:
    """Query the DB for completed dispatches for a given agent."""
    conn = open_db_for_query(db)
    rows = conn.execute(
        "SELECT event_type, task_id, wire_id, error FROM run_events WHERE target_agent_id = ? AND outcome = 'completed' ORDER BY timestamp",
        (agent_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run_meta(db: Database) -> dict:
    """Query the DB for the latest run_meta row."""
    conn = open_db_for_query(db)
    row = conn.execute("SELECT * FROM run_meta ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else {}


def get_all_agent_dispatches(db: Database, agent_id: str) -> list[dict]:
    """Query the DB for all dispatches (any outcome) for a given agent."""
    conn = open_db_for_query(db)
    rows = conn.execute(
        "SELECT event_type, task_id, wire_id, error, outcome FROM run_events WHERE target_agent_id = ? ORDER BY timestamp",
        (agent_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class MockRunner(AgentRunner):
    """Mock AgentRunner that records calls and returns controlled results.

    By default returns a successful result. Set `fail_agents` to simulate failures,
    or `side_effects` to run custom async logic (e.g., create tasks via tools).
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, str]] = []  # (agent_id, system_prompt, prompt)
        self.fail_agents: set[str] = set()
        self.error_message: str = "Agent crashed"
        self.side_effects: dict[str, list] = {}  # agent_id -> [callable, callable, ...]
        self._interrupted: set[str] = set()
        self._sessions: dict[str, str] = {}  # agent_id -> session_id

    async def run_agent(self, agent_id: str, system_prompt: str, prompt: str) -> AgentRunResult:
        self.calls.append((agent_id, system_prompt, prompt))

        # Run side effect if registered
        effects = self.side_effects.get(agent_id, [])
        if effects:
            effect = effects.pop(0)
            await effect()

        if agent_id in self.fail_agents:
            return AgentRunResult(error=self.error_message, num_turns=1, cost_usd=0.01)

        # Simulate SDK session creation on successful completion
        import uuid

        self._sessions[agent_id] = str(uuid.uuid4())
        return AgentRunResult(num_turns=3, cost_usd=0.05)

    async def interrupt_agent(self, agent_id: str) -> None:
        self._interrupted.add(agent_id)

    def get_session(self, agent_id: str):
        return self._sessions.get(agent_id)

    def restore_session(self, agent_id: str, session_id: str) -> None:
        self._sessions[agent_id] = session_id

    def shutdown(self) -> None:
        pass

"""Tests for AgentRunResult dataclass.

RunOutcome tests are in test_run_outcome.py.
"""

from magelab.runners.agent_runner import AgentRunResult


class TestAgentRunResult:
    def test_successful_result(self):
        """Constructing with real values stores them correctly."""
        r = AgentRunResult(num_turns=5, cost_usd=0.12, duration_ms=1500)
        assert r.num_turns == 5
        assert r.cost_usd == 0.12
        assert r.duration_ms == 1500
        assert r.error is None
        assert not r.timed_out

    def test_error_result(self):
        """Error result carries the error message and can have timed_out."""
        r = AgentRunResult(error="Rate limited", num_turns=1, cost_usd=0.01, timed_out=True)
        assert r.error == "Rate limited"
        assert r.timed_out is True
        assert r.num_turns == 1

    def test_session_id(self):
        """Session ID is stored when provided."""
        r = AgentRunResult(session_id="sess-abc", num_turns=3, cost_usd=0.05)
        assert r.session_id == "sess-abc"

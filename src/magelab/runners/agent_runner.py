"""
AgentRunner - Abstract base class for running agent turns.

Separated from orchestrator.py to break the circular import between
orchestrator and claude_runner.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


# Error category prefixes. Used by runners to format errors and by
# database.compute_run_summary() to classify them. Keep in sync.
ERROR_RATE_LIMITED = "Rate limited"
ERROR_API_OVERLOADED = "API overloaded"
ERROR_API_ERROR = "API error"


@dataclass
class AgentRunResult:
    """Result of a single agent run (one event dispatch)."""

    error: Optional[str] = None
    num_turns: int = 0
    cost_usd: Optional[float] = None
    duration_ms: int = 0
    timed_out: bool = False
    session_id: Optional[str] = None


# Signature: (agent_id) -> optional text to inject after every tool call.
PostToolHook = Callable[[str], Optional[str]]


class AgentRunner(ABC):
    """
    Base class for running agent turns.

    Subclasses implement run_agent() and interrupt_agent() to integrate with an LLM backend.
    Setup and configuration is implementation-specific.
    """

    def __init__(self, post_tool_hooks: Optional[list[PostToolHook]] = None) -> None:
        self.post_tool_hooks = post_tool_hooks

    @abstractmethod
    async def run_agent(
        self,
        agent_id: str,
        system_prompt: str,
        prompt: str,
    ) -> AgentRunResult:
        """
        Run one turn of an agent.

        Args:
            agent_id: The agent's identifier.
            system_prompt: The fully resolved system prompt (framework + role/override).
            prompt: The resolved prompt string to send to the LLM.

        Returns:
            AgentRunResult with error (if any), cost, turns, and timing.
        """
        ...

    @abstractmethod
    async def interrupt_agent(self, agent_id: str) -> None:
        """
        Interrupt a running agent, cancelling the active LLM call.

        Must be safe to call even if the agent is not currently running.
        """
        ...

    @abstractmethod
    def get_session(self, agent_id: str) -> Optional[str]:
        """Return the current session ID for an agent, or None.

        Used to persist session state when an agent is force-cancelled,
        so resume can continue the conversation from prior turns.
        """
        ...

    @abstractmethod
    def restore_session(self, agent_id: str, session_id: str) -> None:
        """Restore a session ID for an agent (used during resume)."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Clean up resources (transcript loggers, file handles, etc.).

        Called by the orchestrator at the end of a run.
        """
        ...

"""
TranscriptLogger - Per-agent conversation logging.

Logs prompts, responses, and tool calls to per-agent files using Python's logging.
"""

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Protocol


class TranscriptLoggerProtocol(Protocol):
    """Interface for transcript loggers. Ensures TranscriptLogger and NoOpTranscriptLogger stay in sync."""

    def log_system_prompt(self, agent_id: str, system_prompt: str) -> None: ...
    def log_prompt(self, agent_id: str, prompt: str) -> None: ...
    def log_assistant_text(self, agent_id: str, text: str) -> None: ...
    def log_tool_call(self, agent_id: str, tool_name: str, tool_input: dict[str, Any]) -> None: ...
    def log_tool_result(self, agent_id: str, result: str, is_error: bool = False) -> None: ...
    def log_hook_output(self, agent_id: str, text: str) -> None: ...
    def log_wire_message(self, wire_id: str, participants: frozenset[str], sender: str, body: str) -> None: ...
    def log_run_complete(
        self, agent_id: str, num_turns: int, cost_usd: Optional[float] = None, error: Optional[str] = None
    ) -> None: ...

    def add_listener(self, fn: Callable[[str, str, str], None]) -> None: ...
    def close(self) -> None: ...


class TranscriptLogger:
    """
    Logs agent conversations to per-agent transcript files.

    Each agent gets its own file: {output_dir}/transcripts/{agent_id}.txt
    Uses Python's logging.FileHandler for proper buffering and integration.
    """

    _instance_counter: int = 0

    def __init__(self, output_dir: Path) -> None:
        """
        Initialize transcript logger.

        Args:
            output_dir: Base output directory. Transcripts are written to
                        {output_dir}/transcripts/{agent_id}.txt.
                        Wire messages go to {output_dir}/wires/{wire_id}.txt.
        """
        self.output_dir = output_dir / "transcripts"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._wires_dir = output_dir / "wires"
        self._wires_dir.mkdir(parents=True, exist_ok=True)
        # Use a monotonic counter for logger namespace to avoid handler accumulation
        # when id(self) is reused after GC
        self._namespace = f"transcript.{TranscriptLogger._instance_counter}"
        TranscriptLogger._instance_counter += 1
        self._loggers: dict[str, logging.Logger] = {}
        self._wire_loggers: dict[str, logging.Logger] = {}
        self._listeners: list[Callable[[str, str, str], None]] = []

    def _get_logger(self, agent_id: str) -> logging.Logger:
        """Get or create logger with file handler for agent."""
        if not agent_id or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        if agent_id not in self._loggers:
            logger = logging.getLogger(f"{self._namespace}.{agent_id}")
            logger.setLevel(logging.INFO)
            logger.propagate = False  # Don't propagate to root logger

            handler = logging.FileHandler(self.output_dir / f"{agent_id}.txt", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(handler)

            self._loggers[agent_id] = logger
        return self._loggers[agent_id]

    def _log(self, agent_id: str, message: str) -> None:
        """Write message to agent's transcript file."""
        self._get_logger(agent_id).info(message)

    def add_listener(self, fn: Callable[[str, str, str], None]) -> None:
        """Register a listener for transcript entries. Callback receives (agent_id, entry_type, content)."""
        self._listeners.append(fn)

    def _notify(self, agent_id: str, entry_type: str, content: str) -> None:
        """Notify all listeners of a transcript entry."""
        stripped = content.strip()
        for fn in self._listeners:
            fn(agent_id, entry_type, stripped)

    def log_system_prompt(self, agent_id: str, system_prompt: str) -> None:
        """Log the system prompt for an agent run."""
        self._log(agent_id, f"\n{'=' * 60}")
        self._log(agent_id, f"SYSTEM PROMPT:\n{system_prompt}")
        self._log(agent_id, "=" * 60)
        self._notify(agent_id, "system_prompt", system_prompt)

    def log_prompt(self, agent_id: str, prompt: str) -> None:
        """Log a prompt sent to the agent."""
        self._log(agent_id, f"\n{'=' * 60}")
        self._log(agent_id, f"PROMPT:\n{prompt}")
        self._log(agent_id, "=" * 60)
        self._notify(agent_id, "prompt", prompt)

    def log_assistant_text(self, agent_id: str, text: str) -> None:
        """Log assistant text response."""
        self._log(agent_id, "=" * 60)
        self._log(agent_id, f"💭 ASSISTANT:\n{text}")
        self._log(agent_id, "=" * 60)
        self._notify(agent_id, "assistant_text", text)

    def log_tool_call(
        self,
        agent_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Log a tool call."""
        input_str = json.dumps(tool_input, indent=4, default=str)
        # Truncate large inputs
        if len(input_str) > 5000:
            input_str = input_str[:5000] + "\n...[truncated]"
        self._log(agent_id, "=" * 60)
        self._log(agent_id, f"🔧 TOOL CALL: {tool_name}\n{input_str}")
        self._log(agent_id, "=" * 60)
        self._notify(agent_id, "tool_call", f"{tool_name}: {input_str}")

    def log_tool_result(
        self,
        agent_id: str,
        result: str,
        is_error: bool = False,
    ) -> None:
        """Log a tool result."""
        emoji = "❌" if is_error else "✅"
        # Truncate very long results
        if len(result) > 5000:
            result = result[:5000] + f"\n...[truncated, {len(result)} chars total]"
        self._log(agent_id, f"{emoji} TOOL RESULT:\n{result}")
        self._log(agent_id, "=" * 60)
        self._notify(agent_id, "tool_result", result)

    def log_hook_output(self, agent_id: str, text: str) -> None:
        """Log PostToolUse hook output (e.g. unread notifications)."""
        self._log(agent_id, f"🔔 HOOK: {text}")
        self._log(agent_id, "=" * 60)
        self._notify(agent_id, "hook_output", text)

    def _get_wire_logger(self, wire_id: str) -> logging.Logger:
        """Get or create logger with file handler for a wire."""
        if wire_id not in self._wire_loggers:
            logger = logging.getLogger(f"{self._namespace}.wire.{wire_id}")
            logger.setLevel(logging.INFO)
            logger.propagate = False  # Don't propagate to root logger

            handler = logging.FileHandler(self._wires_dir / f"{wire_id}.txt", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(handler)

            self._wire_loggers[wire_id] = logger
        return self._wire_loggers[wire_id]

    def log_wire_message(self, wire_id: str, participants: frozenset[str], sender: str, body: str) -> None:
        """Log a wire message to the per-wire transcript file.

        On first message to a wire, writes a header with participants.
        """
        is_new = wire_id not in self._wire_loggers
        logger = self._get_wire_logger(wire_id)
        if is_new:
            logger.info(f"Conversation {wire_id}")
            logger.info(f"Participants: {', '.join(sorted(participants))}")
            logger.info("=" * 60)
        logger.info(f"[{sender}]")
        logger.info(body)
        logger.info("-" * 40)

    def log_run_complete(
        self,
        agent_id: str,
        num_turns: int,
        cost_usd: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log completion of an agent run."""
        cost_str = f"${cost_usd:.4f}" if cost_usd is not None else "N/A"
        if error:
            self._log(agent_id, f"❌ RUN FAILED: {error}")
        else:
            self._log(agent_id, f"✅ RUN COMPLETE: turns={num_turns}, cost={cost_str}")
        self._log(agent_id, "=" * 60)
        self._log(agent_id, "-" * 60)
        self._log(agent_id, "=" * 60)
        self._notify(agent_id, "run_complete", cost_str if not error else f"ERROR: {error}")

    def close(self) -> None:
        """Close all file handlers and remove loggers from the global registry.

        Must be called at the end of a pipeline run to prevent file descriptor
        and memory leaks in batch execution.
        """
        for logger in list(self._loggers.values()):
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
        for logger in list(self._wire_loggers.values()):
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
        self._loggers.clear()
        self._wire_loggers.clear()


class NoOpTranscriptLogger:
    """No-op transcript logger for when logging is disabled."""

    def add_listener(self, fn: Callable[[str, str, str], None]) -> None:
        pass

    def log_system_prompt(self, agent_id: str, system_prompt: str) -> None:
        pass

    def log_prompt(self, agent_id: str, prompt: str) -> None:
        pass

    def log_assistant_text(self, agent_id: str, text: str) -> None:
        pass

    def log_tool_call(
        self,
        agent_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        pass

    def log_tool_result(
        self,
        agent_id: str,
        result: str,
        is_error: bool = False,
    ) -> None:
        pass

    def log_hook_output(self, agent_id: str, text: str) -> None:
        pass

    def log_wire_message(self, wire_id: str, participants: frozenset[str], sender: str, body: str) -> None:
        pass

    def log_run_complete(
        self,
        agent_id: str,
        num_turns: int,
        cost_usd: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        pass

    def close(self) -> None:
        pass

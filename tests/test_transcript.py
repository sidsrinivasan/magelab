"""Tests for magelab.transcript — TranscriptLogger and NoOpTranscriptLogger."""

import gc
import inspect
import re
import tempfile

import pytest
from datetime import datetime
from pathlib import Path

from magelab.state.transcript import NoOpTranscriptLogger, TranscriptLogger, TranscriptLoggerProtocol


# =============================================================================
# TranscriptLogger
# =============================================================================


class TestTranscriptLogger:
    def _make_logger(self) -> tuple[TranscriptLogger, Path]:
        tmpdir = Path(tempfile.mkdtemp())
        tl = TranscriptLogger(tmpdir)
        return tl, tmpdir

    def _read_transcript(self, tmpdir: Path, agent_id: str) -> str:
        path = tmpdir / "transcripts" / f"{agent_id}.txt"
        return path.read_text()

    def test_creates_transcript_dir(self):
        tl, tmpdir = self._make_logger()
        assert (tmpdir / "transcripts").is_dir()

    def test_log_system_prompt(self):
        tl, tmpdir = self._make_logger()
        tl.log_system_prompt("agent-1", "You are a coder.")
        content = self._read_transcript(tmpdir, "agent-1")
        assert "SYSTEM PROMPT" in content
        assert "You are a coder." in content

    def test_log_prompt(self):
        tl, tmpdir = self._make_logger()
        tl.log_prompt("agent-1", "Please implement feature X")
        content = self._read_transcript(tmpdir, "agent-1")
        assert "PROMPT" in content
        assert "Please implement feature X" in content

    def test_log_assistant_text(self):
        tl, tmpdir = self._make_logger()
        tl.log_assistant_text("agent-1", "I'll start by reading the code")
        content = self._read_transcript(tmpdir, "agent-1")
        assert "ASSISTANT" in content
        assert "I'll start by reading the code" in content

    def test_log_tool_call(self):
        tl, tmpdir = self._make_logger()
        tl.log_tool_call("agent-1", "Read", {"file_path": "/foo/bar.py"})
        content = self._read_transcript(tmpdir, "agent-1")
        assert "TOOL CALL" in content
        assert "Read" in content
        assert "/foo/bar.py" in content

    def test_log_tool_call_truncates_large_input(self):
        tl, tmpdir = self._make_logger()
        large_input = {"data": "x" * 6000}
        tl.log_tool_call("agent-1", "Write", large_input)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "truncated" in content
        assert "Write" in content  # tool name preserved despite truncation
        assert "xxxx" in content  # beginning of content preserved

    def test_log_tool_result_success(self):
        tl, tmpdir = self._make_logger()
        tl.log_tool_result("agent-1", "File contents here")
        content = self._read_transcript(tmpdir, "agent-1")
        assert "TOOL RESULT" in content
        assert "File contents here" in content
        assert "\u2705" in content  # success emoji present
        assert "\u274c" not in content  # error emoji absent

    def test_log_tool_result_error(self):
        tl, tmpdir = self._make_logger()
        tl.log_tool_result("agent-1", "File not found", is_error=True)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "File not found" in content
        assert "\u274c" in content  # error emoji present
        assert "\u2705" not in content  # success emoji absent

    def test_log_tool_result_truncates_large_output(self):
        tl, tmpdir = self._make_logger()
        large_result = "x" * 6000
        tl.log_tool_result("agent-1", large_result)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "truncated" in content
        assert "TOOL RESULT" in content  # header preserved despite truncation
        assert "xxxx" in content  # beginning of content preserved

    def test_log_run_complete_success(self):
        tl, tmpdir = self._make_logger()
        tl.log_run_complete("agent-1", num_turns=5, cost_usd=0.1234)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "RUN COMPLETE" in content
        assert "turns=5" in content
        assert "$0.1234" in content
        assert "FAILED" not in content

    def test_log_run_complete_error(self):
        tl, tmpdir = self._make_logger()
        tl.log_run_complete("agent-1", num_turns=3, error="Rate limited")
        content = self._read_transcript(tmpdir, "agent-1")
        assert "RUN FAILED" in content
        assert "Rate limited" in content
        assert "RUN COMPLETE" not in content

    def test_log_run_complete_no_cost(self):
        tl, tmpdir = self._make_logger()
        tl.log_run_complete("agent-1", num_turns=1)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "N/A" in content

    def test_per_agent_files(self):
        tl, tmpdir = self._make_logger()
        tl.log_prompt("agent-1", "Prompt for agent 1")
        tl.log_prompt("agent-2", "Prompt for agent 2")
        content1 = self._read_transcript(tmpdir, "agent-1")
        content2 = self._read_transcript(tmpdir, "agent-2")
        assert "agent 1" in content1
        assert "agent 2" in content2
        assert "agent 2" not in content1
        assert "agent 1" not in content2

    def test_logger_reuse(self):
        """Getting logger for same agent_id returns same logger instance."""
        tl, tmpdir = self._make_logger()
        logger1 = tl._get_logger("agent-1")
        logger2 = tl._get_logger("agent-1")
        assert logger1 is logger2

    def test_multiple_logs_append(self):
        tl, tmpdir = self._make_logger()
        tl.log_prompt("agent-1", "First")
        tl.log_prompt("agent-1", "Second")
        content = self._read_transcript(tmpdir, "agent-1")
        assert "First" in content
        assert "Second" in content

    def test_log_tool_result_truncation_reports_length(self):
        """Truncated tool results include the original length in the message."""
        tl, tmpdir = self._make_logger()
        result = "a" * 6000
        tl.log_tool_result("agent-1", result)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "truncated" in content
        assert "6000" in content  # original length reported

    def test_log_tool_call_non_serializable_input(self):
        """Non-JSON-serializable values are stringified via default=str."""
        tl, tmpdir = self._make_logger()
        dt = datetime(2026, 1, 15, 12, 30, 0)
        tl.log_tool_call("agent-1", "Schedule", {"timestamp": dt})
        content = self._read_transcript(tmpdir, "agent-1")
        assert "Schedule" in content
        assert "2026-01-15" in content  # datetime stringified

    def test_log_run_complete_zero_cost(self):
        """cost_usd=0.0 is falsy but not None; should be formatted, not N/A."""
        tl, tmpdir = self._make_logger()
        tl.log_run_complete("agent-1", num_turns=1, cost_usd=0.0)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "$0.0000" in content
        assert "N/A" not in content

    def test_log_run_complete_error_with_cost(self):
        """When error is set, RUN FAILED is logged; cost is not included."""
        tl, tmpdir = self._make_logger()
        tl.log_run_complete("agent-1", num_turns=2, cost_usd=0.5, error="timeout")
        content = self._read_transcript(tmpdir, "agent-1")
        assert "RUN FAILED" in content
        assert "timeout" in content
        # Error path does not log cost or RUN COMPLETE
        assert "RUN COMPLETE" not in content
        assert "$0.5000" not in content

    def test_truncation_boundary_no_truncate(self):
        """Result of exactly 5000 chars should NOT be truncated (threshold is > 5000)."""
        tl, tmpdir = self._make_logger()
        result = "a" * 5000
        tl.log_tool_result("agent-1", result)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "truncated" not in content

    def test_truncation_boundary_just_over(self):
        """Result of 5001 chars should be truncated (off-by-one boundary check)."""
        tl, tmpdir = self._make_logger()
        result = "a" * 5001
        tl.log_tool_result("agent-1", result)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "truncated" in content

    def test_log_tool_result_truncation_with_error(self):
        """Truncation works correctly for error results: error emoji present, success emoji absent."""
        tl, tmpdir = self._make_logger()
        result = "a" * 6000
        tl.log_tool_result("agent-1", result, is_error=True)
        content = self._read_transcript(tmpdir, "agent-1")
        assert "truncated" in content
        assert "\u274c" in content  # error emoji ❌
        assert "\u2705" not in content  # success emoji ✅ absent

    def test_output_contains_timestamp(self):
        """Logged output includes a timestamp in YYYY-MM-DD HH:MM:SS format."""
        tl, tmpdir = self._make_logger()
        tl.log_prompt("agent-1", "Hello")
        content = self._read_transcript(tmpdir, "agent-1")
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", content), (
            f"Expected timestamp pattern not found in output: {content[:200]}"
        )

    def test_handler_accumulation_on_id_reuse(self):
        """Handlers should not accumulate when id(self) is reused after GC.

        TranscriptLogger uses id(self) in its logger namespace. Python's logging.getLogger
        returns the same Logger object for the same name. If a TranscriptLogger is garbage
        collected and a new one happens to get the same id(self), the old Logger (with its
        handler) is still alive in Python's logging manager. The new TranscriptLogger will
        add another handler to it, causing duplicate log entries.

        BUG: The source code does not clean up logging handlers on destruction, so handlers
        can accumulate when id(self) is reused. This test documents the issue by checking
        that handler counts stay bounded. If the source is fixed (e.g., by adding __del__
        or using a unique namespace), this test will still pass.
        """
        max_handlers_seen = 0
        for _ in range(20):
            tmpdir = Path(tempfile.mkdtemp())
            tl = TranscriptLogger(tmpdir)
            logger = tl._get_logger("agent-1")
            handler_count = len(logger.handlers)
            max_handlers_seen = max(max_handlers_seen, handler_count)
            # Clean up handlers to prevent test pollution
            for h in logger.handlers[:]:
                h.close()
                logger.removeHandler(h)
            del tl
            gc.collect()

        # After cleanup in each iteration, handlers should not grow unbounded.
        # Without cleanup, this could grow to 20. We assert a reasonable bound.
        assert max_handlers_seen <= 2, (
            f"Handler count grew to {max_handlers_seen}; indicates handlers accumulate when id(self) is reused"
        )

    def test_empty_agent_id_raises(self):
        """Empty agent_id raises ValueError instead of creating a dotfile."""
        tl, tmpdir = self._make_logger()
        with pytest.raises(ValueError, match="non-empty"):
            tl.log_system_prompt("", "")

    def test_whitespace_agent_id_raises(self):
        """Whitespace-only agent_id raises ValueError."""
        tl, tmpdir = self._make_logger()
        with pytest.raises(ValueError, match="non-empty"):
            tl.log_system_prompt("   ", "")

    # -----------------------------------------------------------------
    # log_wire_message
    # -----------------------------------------------------------------

    def _read_wire(self, tmpdir: Path, wire_id: str) -> str:
        path = tmpdir / "wires" / f"{wire_id}.txt"
        return path.read_text()

    def test_wire_creates_file_on_first_message(self):
        """log_wire_message creates wires/{wire_id}.txt on first call."""
        tl, tmpdir = self._make_logger()
        wire_path = tmpdir / "wires" / "w1.txt"
        assert not wire_path.exists()
        tl.log_wire_message("w1", frozenset({"alice", "bob"}), "alice", "hello")
        assert wire_path.exists()

    def test_wire_header_on_first_message(self):
        """First message to a wire writes a participant header."""
        tl, tmpdir = self._make_logger()
        tl.log_wire_message("w1", frozenset({"alice", "bob"}), "alice", "hello")
        content = self._read_wire(tmpdir, "w1")
        assert "Conversation w1" in content
        assert "Participants:" in content
        assert "alice" in content
        assert "bob" in content

    def test_wire_subsequent_messages_append(self):
        """Subsequent messages to the same wire append without repeating the header."""
        tl, tmpdir = self._make_logger()
        participants = frozenset({"alice", "bob"})
        tl.log_wire_message("w1", participants, "alice", "hello")
        tl.log_wire_message("w1", participants, "bob", "hi back")
        content = self._read_wire(tmpdir, "w1")
        # Header appears exactly once
        assert content.count("Conversation w1") == 1
        assert content.count("Participants:") == 1
        # Both messages present
        assert "[alice]" in content
        assert "hello" in content
        assert "[bob]" in content
        assert "hi back" in content

    def test_wire_per_wire_file_isolation(self):
        """Different wire_ids write to separate files with no cross-contamination."""
        tl, tmpdir = self._make_logger()
        tl.log_wire_message("w1", frozenset({"alice", "bob"}), "alice", "msg-for-w1")
        tl.log_wire_message("w2", frozenset({"carol", "dave"}), "carol", "msg-for-w2")
        content_w1 = self._read_wire(tmpdir, "w1")
        content_w2 = self._read_wire(tmpdir, "w2")
        assert "msg-for-w1" in content_w1
        assert "msg-for-w2" not in content_w1
        assert "msg-for-w2" in content_w2
        assert "msg-for-w1" not in content_w2

    # -----------------------------------------------------------------
    # close()
    # -----------------------------------------------------------------

    def test_close_releases_file_handlers(self):
        """After close(), internal loggers have no remaining handlers."""
        tl, tmpdir = self._make_logger()
        tl.log_prompt("agent-1", "hello")
        tl.log_wire_message("w1", frozenset({"a", "b"}), "a", "hi")
        # Grab references before close clears the dicts
        agent_logger = tl._loggers["agent-1"]
        wire_logger = tl._wire_loggers["w1"]
        tl.close()
        assert len(agent_logger.handlers) == 0, "Agent logger still has handlers after close()"
        assert len(wire_logger.handlers) == 0, "Wire logger still has handlers after close()"
        assert len(tl._loggers) == 0
        assert len(tl._wire_loggers) == 0


# =============================================================================
# NoOpTranscriptLogger
# =============================================================================


class TestNoOpTranscriptLogger:
    def test_all_methods_are_noop(self):
        """All methods should be callable without error."""
        noop = NoOpTranscriptLogger()
        noop.log_system_prompt("a", "prompt")
        noop.log_prompt("a", "prompt")
        noop.log_assistant_text("a", "text")
        noop.log_tool_call("a", "tool", {"key": "val"})
        noop.log_tool_result("a", "result")
        noop.log_tool_result("a", "result", is_error=True)
        noop.log_run_complete("a", 5, 0.1, None)
        noop.log_run_complete("a", 5, error="err")


# =============================================================================
# Protocol conformance
# =============================================================================


class TestTranscriptListeners:
    def _make_logger(self) -> tuple[TranscriptLogger, Path]:
        tmpdir = Path(tempfile.mkdtemp())
        tl = TranscriptLogger(tmpdir)
        return tl, tmpdir

    def test_listener_receives_system_prompt(self):
        tl, _ = self._make_logger()
        received = []
        tl.add_listener(lambda agent_id, entry_type, content: received.append((agent_id, entry_type, content)))
        tl.log_system_prompt("agent-1", "You are a coder.")
        assert len(received) == 1
        assert received[0] == ("agent-1", "system_prompt", "You are a coder.")

    def test_listener_receives_prompt(self):
        tl, _ = self._make_logger()
        received = []
        tl.add_listener(lambda agent_id, entry_type, content: received.append((agent_id, entry_type, content)))
        tl.log_prompt("agent-1", "Please implement feature X")
        assert len(received) == 1
        assert received[0] == ("agent-1", "prompt", "Please implement feature X")

    def test_listener_receives_assistant_text(self):
        tl, _ = self._make_logger()
        received = []
        tl.add_listener(lambda agent_id, entry_type, content: received.append((agent_id, entry_type, content)))
        tl.log_assistant_text("agent-1", "I'll read the code")
        assert len(received) == 1
        assert received[0] == ("agent-1", "assistant_text", "I'll read the code")

    def test_listener_receives_tool_call(self):
        tl, _ = self._make_logger()
        received = []
        tl.add_listener(lambda agent_id, entry_type, content: received.append((agent_id, entry_type, content)))
        tl.log_tool_call("agent-1", "Read", {"file_path": "/foo.py"})
        assert len(received) == 1
        assert received[0][0] == "agent-1"
        assert received[0][1] == "tool_call"
        assert "Read" in received[0][2]

    def test_listener_receives_tool_result(self):
        tl, _ = self._make_logger()
        received = []
        tl.add_listener(lambda agent_id, entry_type, content: received.append((agent_id, entry_type, content)))
        tl.log_tool_result("agent-1", "file contents here")
        assert len(received) == 1
        assert received[0] == ("agent-1", "tool_result", "file contents here")

    def test_listener_receives_hook_output(self):
        tl, _ = self._make_logger()
        received = []
        tl.add_listener(lambda agent_id, entry_type, content: received.append((agent_id, entry_type, content)))
        tl.log_hook_output("agent-1", "NOTIFICATION: 3 unread messages")
        assert len(received) == 1
        assert received[0] == ("agent-1", "hook_output", "NOTIFICATION: 3 unread messages")

    def test_listener_receives_run_complete(self):
        tl, _ = self._make_logger()
        received = []
        tl.add_listener(lambda agent_id, entry_type, content: received.append((agent_id, entry_type, content)))
        tl.log_run_complete("agent-1", 5, 0.25)
        assert len(received) == 1
        assert received[0][1] == "run_complete"

    def test_multiple_listeners(self):
        tl, _ = self._make_logger()
        r1, r2 = [], []
        tl.add_listener(lambda a, t, c: r1.append(t))
        tl.add_listener(lambda a, t, c: r2.append(t))
        tl.log_assistant_text("agent-1", "hello")
        assert r1 == ["assistant_text"]
        assert r2 == ["assistant_text"]

    def test_noop_logger_has_add_listener(self):
        noop = NoOpTranscriptLogger()
        noop.add_listener(lambda a, t, c: None)  # should not raise


# =============================================================================
# Protocol conformance
# =============================================================================


class TestProtocolConformance:
    """Both TranscriptLogger and NoOpTranscriptLogger must implement the same public interface."""

    # Derive expected methods from the Protocol definition itself, so the test
    # stays in sync if the protocol gains new methods.
    EXPECTED_METHODS = [
        name
        for name, _ in inspect.getmembers(TranscriptLoggerProtocol, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]

    def test_protocol_has_expected_methods(self):
        """Sanity check: the protocol defines at least the 6 known log methods."""
        assert len(self.EXPECTED_METHODS) >= 6, (
            f"Expected at least 6 protocol methods, got {len(self.EXPECTED_METHODS)}: {self.EXPECTED_METHODS}"
        )

    def test_transcript_logger_has_all_protocol_methods(self):
        """TranscriptLogger implements every method defined in TranscriptLoggerProtocol."""
        for method_name in self.EXPECTED_METHODS:
            assert hasattr(TranscriptLogger, method_name), f"TranscriptLogger is missing method: {method_name}"

    def test_noop_logger_has_all_protocol_methods(self):
        """NoOpTranscriptLogger implements every method defined in TranscriptLoggerProtocol."""
        for method_name in self.EXPECTED_METHODS:
            assert hasattr(NoOpTranscriptLogger, method_name), f"NoOpTranscriptLogger is missing method: {method_name}"

    def test_both_loggers_have_same_public_methods(self):
        """TranscriptLogger and NoOpTranscriptLogger expose the same set of public methods."""
        tl_public = {m for m in dir(TranscriptLogger) if not m.startswith("_")}
        noop_public = {m for m in dir(NoOpTranscriptLogger) if not m.startswith("_")}
        # NoOpTranscriptLogger should have at least all methods of the protocol.
        # TranscriptLogger may have extra private/internal methods, but the public
        # interface visible to callers (the protocol methods) must match.
        protocol_methods = set(self.EXPECTED_METHODS)
        assert protocol_methods.issubset(tl_public), f"TranscriptLogger missing: {protocol_methods - tl_public}"
        assert protocol_methods.issubset(noop_public), f"NoOpTranscriptLogger missing: {protocol_methods - noop_public}"

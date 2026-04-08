"""Tests for magelab.claude_runner — utility functions and construction.

Does NOT test run_agent (requires live Claude SDK). Tests pure utility
functions and the build_allowed_tools / build_disallowed_tools logic.
"""

import logging

import pytest

from magelab.runners.claude_runner import (
    _extract_tool_result_text,
    _to_mcp_response,
    build_allowed_tools,
    build_disallowed_tools,
)
from magelab.tools.bundles import BUNDLES, Bundle
from magelab.tools.specs import FRAMEWORK, ToolResponse


# =============================================================================
# _to_mcp_response
# =============================================================================


class TestToMcpResponse:
    def test_success_response(self):
        result = ToolResponse(text="Task created", is_error=False)
        mcp = _to_mcp_response(result)
        assert mcp["content"] == [{"type": "text", "text": "Task created"}]
        assert "is_error" not in mcp

    def test_error_response(self):
        result = ToolResponse(text="Not found", is_error=True)
        mcp = _to_mcp_response(result)
        assert mcp["content"] == [{"type": "text", "text": "Not found"}]
        assert mcp["is_error"] is True

    def test_to_mcp_response_empty_text(self):
        """Empty text should produce valid MCP response with empty text content."""
        result = ToolResponse(text="", is_error=False)
        mcp = _to_mcp_response(result)
        assert mcp["content"] == [{"type": "text", "text": ""}]
        assert "is_error" not in mcp


# =============================================================================
# _extract_tool_result_text
# =============================================================================


class TestExtractToolResultText:
    def test_string_content(self):
        assert _extract_tool_result_text("hello") == "hello"

    def test_list_of_dicts(self):
        content = [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]
        result = _extract_tool_result_text(content)
        assert result == "line1\nline2"

    def test_list_of_mixed(self):
        content = [{"type": "text", "text": "dict"}, "raw string"]
        result = _extract_tool_result_text(content)
        assert result == "dict\nraw string"

    def test_none_content(self):
        assert _extract_tool_result_text(None) == ""

    def test_other_type(self):
        assert _extract_tool_result_text(42) == "42"

    def test_extract_empty_list(self):
        """Empty list should return empty string (join of empty list)."""
        assert _extract_tool_result_text([]) == ""

    def test_extract_list_dict_missing_text_key(self):
        """Dict items without 'text' key should fall back to empty string via .get('text', '')."""
        content = [{"type": "image", "url": "http://example.com"}]
        assert _extract_tool_result_text(content) == ""

    def test_extract_falsy_non_none_value(self):
        """Falsy non-None values like 0 and False are stringified, not discarded."""
        assert _extract_tool_result_text(0) == "0"
        assert _extract_tool_result_text(False) == "False"

    def test_extract_list_with_none_items(self):
        """None items in a list are filtered out; other items are kept."""
        result = _extract_tool_result_text([None, "text"])
        assert result == "text"

    def test_extract_list_all_none(self):
        """List of all None items produces empty string."""
        assert _extract_tool_result_text([None, None]) == ""


# =============================================================================
# build_allowed_tools
# =============================================================================


class TestBuildAllowedTools:
    def test_framework_tools_get_mcp_prefix(self):
        """Framework tools should be prefixed with mcp__magelab__."""
        tools = build_allowed_tools(["tasks_create", "tasks_assign"])
        assert "mcp__magelab__tasks_create" in tools
        assert "mcp__magelab__tasks_assign" in tools

    def test_claude_native_tools_pass_through(self):
        """Claude SDK native tools (Read, Write, etc.) pass through unchanged."""
        tools = build_allowed_tools(["Read", "Write", "Bash"])
        assert "Read" in tools
        assert "Write" in tools
        assert "Bash" in tools

    def test_custom_mcp_tools_pass_through(self):
        """Custom MCP tools (mcp__*) pass through unchanged."""
        tools = build_allowed_tools(["mcp__custom__my_tool"])
        assert "mcp__custom__my_tool" in tools

    def test_unknown_tool_warns(self, caplog):
        """Unknown tools should log a warning (not included in output)."""
        test_logger = logging.getLogger("test_build_allowed")
        with caplog.at_level(logging.WARNING, logger="test_build_allowed"):
            tools = build_allowed_tools(["totally_unknown_tool"], test_logger)
        assert "totally_unknown_tool" not in tools
        assert len(tools) == 0
        assert any("totally_unknown_tool" in record.message for record in caplog.records)

    def test_mixed_tools(self):
        """Mixed framework + native + MCP tools."""
        tools = build_allowed_tools(
            [
                "tasks_create",  # framework
                "Read",  # native
                "mcp__ext__fetch",  # custom MCP
            ]
        )
        assert tools == [
            "mcp__magelab__tasks_create",
            "Read",
            "mcp__ext__fetch",
        ]

    def test_empty_input(self):
        assert build_allowed_tools([]) == []

    @pytest.mark.parametrize("tool_name", list(FRAMEWORK.keys()), ids=list(FRAMEWORK.keys()))
    def test_every_framework_tool_gets_prefix(self, tool_name):
        """Every tool in the FRAMEWORK registry gets the mcp__magelab__ prefix."""
        allowed = build_allowed_tools([tool_name])
        assert len(allowed) == 1
        assert allowed[0] == f"mcp__magelab__{tool_name}"


# =============================================================================
# build_disallowed_tools
# =============================================================================


class TestBuildDisallowedTools:
    def test_disallows_unused_claude_tools(self):
        """Should return Claude tools NOT in the role's tool list."""
        role_tools = ["Read", "Grep", "Glob"]
        disallowed = build_disallowed_tools(role_tools)
        # Should disallow Write, Edit, Bash, etc. — everything in CLAUDE bundle not in role_tools
        claude_tools = set(BUNDLES[Bundle.CLAUDE])
        expected_disallowed = claude_tools - set(role_tools)
        assert set(disallowed) == expected_disallowed

    def test_all_claude_tools_means_nothing_disallowed(self):
        """If role has all Claude tools, nothing should be disallowed."""
        role_tools = list(BUNDLES[Bundle.CLAUDE])
        disallowed = build_disallowed_tools(role_tools)
        assert disallowed == []

    def test_no_claude_tools_means_all_disallowed(self):
        """If role has no Claude tools, all should be disallowed."""
        disallowed = build_disallowed_tools([])
        assert set(disallowed) == set(BUNDLES[Bundle.CLAUDE])

    def test_framework_tools_ignored(self):
        """Framework tools should not appear in disallowed list."""
        role_tools = ["tasks_create", "tasks_assign"]
        disallowed = build_disallowed_tools(role_tools)
        # Framework tools are not in CLAUDE bundle, so they don't affect output
        assert "tasks_create" not in disallowed
        assert "tasks_assign" not in disallowed
        # All Claude tools should be disallowed since role has none of them
        assert set(disallowed) == set(BUNDLES[Bundle.CLAUDE])


# =============================================================================
# Cross-function invariant: allowed and disallowed are complementary
# =============================================================================


class TestAllowedDisallowedInvariant:
    """Verify that build_allowed_tools and build_disallowed_tools are complementary
    for Claude native tools: every Claude tool appears in exactly one of the two lists."""

    def test_claude_tools_partitioned_with_subset(self):
        """With a subset of Claude tools, each Claude tool is in allowed XOR disallowed."""
        role_tools = ["Read", "Grep", "Glob", "tasks_create"]
        allowed = set(build_allowed_tools(role_tools))
        disallowed = set(build_disallowed_tools(role_tools))

        claude_tools = set(BUNDLES[Bundle.CLAUDE])
        for tool in claude_tools:
            in_allowed = tool in allowed
            in_disallowed = tool in disallowed
            assert in_allowed != in_disallowed, (
                f"Claude tool '{tool}' should be in exactly one of allowed or disallowed, "
                f"but in_allowed={in_allowed}, in_disallowed={in_disallowed}"
            )

    def test_claude_tools_partitioned_with_all(self):
        """With all Claude tools in role, none should be disallowed."""
        role_tools = list(BUNDLES[Bundle.CLAUDE])
        allowed = set(build_allowed_tools(role_tools))
        disallowed = set(build_disallowed_tools(role_tools))

        assert len(disallowed) == 0
        for tool in BUNDLES[Bundle.CLAUDE]:
            assert tool in allowed

    def test_claude_tools_partitioned_with_none(self):
        """With no Claude tools in role, all should be disallowed and none allowed."""
        role_tools: list[str] = []
        allowed = set(build_allowed_tools(role_tools))
        disallowed = set(build_disallowed_tools(role_tools))

        for tool in BUNDLES[Bundle.CLAUDE]:
            assert tool not in allowed
            assert tool in disallowed

    def test_no_overlap_between_allowed_and_disallowed(self):
        """Allowed and disallowed should never share any tools, regardless of input mix."""
        role_tools = ["Read", "Write", "Bash", "tasks_create", "tasks_assign", "mcp__ext__fetch"]
        allowed = set(build_allowed_tools(role_tools))
        disallowed = set(build_disallowed_tools(role_tools))

        overlap = allowed & disallowed
        assert overlap == set(), f"Tools should not appear in both allowed and disallowed: {overlap}"

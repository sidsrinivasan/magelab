"""Tests for magelab.tools.bundles — Bundle expansion."""

import pytest

from magelab.tools.bundles import BUNDLES, Bundle, expand


class TestBundle:
    def test_bundle_values_match_keys(self):
        """Each Bundle enum value should be a key in BUNDLES."""
        for b in Bundle:
            assert b.value in BUNDLES

    def test_bundles_have_tools(self):
        """Every bundle should map to at least one tool."""
        for name, tools in BUNDLES.items():
            assert len(tools) > 0, f"Bundle '{name}' is empty"

    def test_bundles_keys_match_enum(self):
        """BUNDLES keys and Bundle enum members are exactly the same set."""
        assert set(BUNDLES.keys()) == set(Bundle)


class TestExpand:
    def test_expand_single_bundle(self):
        result = expand(["worker"])
        assert "tasks_submit_for_review" in result
        assert "tasks_mark_finished" in result
        assert "get_available_reviewers" in result

    def test_expand_multiple_bundles(self):
        result = expand(["worker", "management"])
        assert "tasks_submit_for_review" in result
        assert "tasks_create_batch" in result
        assert "tasks_assign" in result

    def test_deduplication(self):
        """Shared tools across bundles appear only once."""
        result = expand(["worker", "management"])
        assert result.count("tasks_mark_finished") == 1

    def test_preserves_order(self):
        """First occurrence order is preserved."""
        result = expand(["worker", "coordination"])
        # Worker tools come first, then coordination
        worker_idx = result.index("tasks_submit_for_review")
        coord_idx = result.index("sleep")
        assert worker_idx < coord_idx

    def test_unknown_name_raises_in_strict_mode(self):
        """Non-bundle, non-framework tool names raise ValueError in strict mode (default)."""
        with pytest.raises(ValueError, match="Unknown tool or bundle"):
            expand(["custom_tool"])

    def test_unknown_name_kept_verbatim_non_strict(self):
        """Non-bundle names pass through when strict=False."""
        result = expand(["custom_tool"], strict=False)
        assert result == ["custom_tool"]

    def test_mixed_bundles_and_names_non_strict(self):
        """Can mix bundle names and individual tool names in non-strict mode."""
        result = expand(["worker", "my_special_tool"], strict=False)
        assert "tasks_submit_for_review" in result
        assert "my_special_tool" in result

    def test_mixed_bundles_and_known_tools(self):
        """Can mix bundle names and known individual tool names in strict mode."""
        result = expand(["worker", "Read"])
        assert "tasks_submit_for_review" in result
        assert "Read" in result

    def test_empty_input(self):
        assert expand([]) == []

    def test_claude_basic_bundle_has_sdk_tools(self):
        result = expand(["claude_basic"])
        assert "Read" in result
        assert "Write" in result
        assert "Edit" in result
        assert "Bash" in result

    def test_claude_bundle_is_superset_of_basic(self):
        basic = set(expand(["claude_basic"]))
        full = set(expand(["claude"]))
        assert basic.issubset(full)

    def test_reviewer_bundles(self):
        active = expand(["claude_reviewer"])
        passive = expand(["passive_claude_reviewer"])
        # Both have submit_review and Read/Grep/Glob
        assert "tasks_submit_review" in active
        assert "tasks_submit_review" in passive
        assert "Read" in active
        assert "Read" in passive
        # Only active has Bash
        assert "Bash" in active
        assert "Bash" not in passive

    def test_management_nobatch_bundle(self):
        """management_nobatch has tasks_create (not batch); management has tasks_create_batch (not single)."""
        nobatch = expand(["management_nobatch"])
        batch = expand(["management"])
        assert "tasks_create" in nobatch
        assert "tasks_create_batch" not in nobatch
        assert "tasks_create_batch" in batch
        assert "tasks_create" not in batch

    def test_worker_bundle_exact_contents(self):
        """Worker bundle expands to exactly these tools in this order."""
        assert expand(["worker"]) == ["tasks_submit_for_review", "tasks_mark_finished", "get_available_reviewers"]

    def test_claude_basic_bundle_exact_contents(self):
        """claude_basic bundle expands to exactly these SDK tools in this order."""
        assert expand(["claude_basic"]) == [
            "Agent",
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
            "NotebookEdit",
            "TodoWrite",
        ]

    def test_claude_bundle_has_all_native_tools(self):
        """claude bundle contains the full set of Claude Code built-in tools for disallow list."""
        result = expand(["claude"])
        for tool in [
            "Agent",
            "AskUserQuestion",
            "EnterPlanMode",
            "ExitPlanMode",
            "Skill",
            "TaskCreate",
            "TaskUpdate",
            "ToolSearch",
            "Read",
            "Write",
            "Bash",
        ]:
            assert tool in result, f"{tool} missing from claude bundle"

    def test_cross_type_dedup(self):
        """Individual tool name + bundle containing it: tool appears only once, first occurrence wins."""
        result = expand(["Read", "claude_basic"])
        assert result.count("Read") == 1
        assert result[0] == "Read"

    def test_duplicate_bundle_input(self):
        """Same bundle listed twice: tools still appear only once."""
        result = expand(["worker", "worker"])
        assert result.count("tasks_submit_for_review") == 1
        assert result.count("tasks_mark_finished") == 1
        assert result.count("get_available_reviewers") == 1

    def test_communication_bundle_exact_contents(self):
        """Communication bundle expands to exactly these tools in this order."""
        assert expand(["communication"]) == [
            "connections_list",
            "send_message",
            "read_messages",
            "batch_read_messages",
            "conversations_list",
        ]

    def test_management_vs_nobatch_difference(self):
        """The only difference between management and management_nobatch is the create tool."""
        mgmt = set(expand(["management"]))
        nobatch = set(expand(["management_nobatch"]))
        only_in_mgmt = mgmt - nobatch
        only_in_nobatch = nobatch - mgmt
        assert only_in_mgmt == {"tasks_create_batch"}
        assert only_in_nobatch == {"tasks_create"}

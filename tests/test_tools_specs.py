"""Tests for magelab.tools.specs — ToolSpec, FRAMEWORK registry."""

import pytest

from magelab.tools import specs as specs_module
from magelab.tools.specs import FRAMEWORK, ToolResponse, ToolSpec


class TestToolSpec:
    def test_frozen(self):
        """ToolSpec is immutable."""
        s = ToolSpec(name="test", description="A test tool", parameters={"x": str})
        with pytest.raises(AttributeError):
            s.name = "changed"

    def test_fields(self):
        s = ToolSpec(name="foo", description="Bar", parameters={"id": str, "val": int})
        assert s.name == "foo"
        assert s.description == "Bar"
        assert s.parameters == {"id": str, "val": int}


class TestToolResponse:
    def test_defaults(self):
        r = ToolResponse(text="ok")
        assert r.text == "ok"
        assert r.is_error is False

    def test_error_response(self):
        r = ToolResponse(text="Bad input", is_error=True)
        assert r.is_error is True

    def test_frozen(self):
        """ToolResponse is immutable."""
        r = ToolResponse(text="ok")
        with pytest.raises(AttributeError):
            r.text = "changed"


class TestFrameworkRegistry:
    EXPECTED_TOOLS = {
        "tasks_create",
        "tasks_create_batch",
        "tasks_assign",
        "tasks_submit_for_review",
        "tasks_submit_review",
        "tasks_mark_finished",
        "tasks_get",
        "tasks_list",
        "connections_list",
        "get_available_reviewers",
        "sleep",
        "send_message",
        "read_messages",
        "batch_read_messages",
        "conversations_list",
    }

    def test_all_tools_present(self):
        assert set(FRAMEWORK.keys()) == self.EXPECTED_TOOLS

    def test_all_entries_are_toolspecs(self):
        for name, spec in FRAMEWORK.items():
            assert isinstance(spec, ToolSpec)
            assert spec.name == name

    def test_all_have_descriptions(self):
        for spec in FRAMEWORK.values():
            assert spec.description, f"{spec.name} has empty description"

    def test_all_have_parameters(self):
        for spec in FRAMEWORK.values():
            assert isinstance(spec.parameters, dict), f"{spec.name} parameters is not a dict"

    def test_tasks_create_parameters(self):
        assert set(FRAMEWORK["tasks_create"].parameters.keys()) == {
            "id",
            "title",
            "description",
            "assigned_to",
            "review_required",
        }

    def test_tasks_list_parameters(self):
        assert set(FRAMEWORK["tasks_list"].parameters.keys()) == {
            "assigned_to",
            "assigned_by",
            "is_finished",
        }

    def test_sleep_parameters(self):
        assert set(FRAMEWORK["sleep"].parameters.keys()) == {"duration_seconds"}

    def test_tasks_mark_finished_parameters(self):
        assert set(FRAMEWORK["tasks_mark_finished"].parameters.keys()) == {
            "task_id",
            "outcome",
            "details",
        }

    def test_tasks_create_batch_parameters(self):
        assert set(FRAMEWORK["tasks_create_batch"].parameters.keys()) == {"tasks"}

    def test_tasks_assign_parameters(self):
        assert set(FRAMEWORK["tasks_assign"].parameters.keys()) == {"task_id", "to_agent"}

    def test_tasks_submit_for_review_parameters(self):
        assert set(FRAMEWORK["tasks_submit_for_review"].parameters.keys()) == {
            "task_id",
            "reviewers",
            "review_policy",
        }

    def test_tasks_submit_review_parameters(self):
        assert set(FRAMEWORK["tasks_submit_review"].parameters.keys()) == {
            "task_id",
            "decision",
            "comment",
        }

    def test_tasks_get_parameters(self):
        assert set(FRAMEWORK["tasks_get"].parameters.keys()) == {"task_id"}

    def test_connections_list_parameters(self):
        assert set(FRAMEWORK["connections_list"].parameters.keys()) == set()

    def test_get_available_reviewers_parameters(self):
        assert set(FRAMEWORK["get_available_reviewers"].parameters.keys()) == set()

    def test_no_duplicate_names(self):
        """FRAMEWORK has exactly 15 entries, confirming no silent overwrites."""
        assert len(FRAMEWORK) == 15

    def test_parameter_values_are_types(self):
        """All parameter values must be Python type objects (str, int, bool, list)."""
        for spec in FRAMEWORK.values():
            for key, value in spec.parameters.items():
                assert isinstance(value, type), f"{spec.name}.parameters[{key!r}] = {value!r} is not a type object"

    def test_send_message_parameters(self):
        assert set(FRAMEWORK["send_message"].parameters.keys()) == {
            "recipients",
            "conversation_id",
            "body",
        }

    def test_read_messages_parameters(self):
        assert set(FRAMEWORK["read_messages"].parameters.keys()) == {
            "conversation_id",
            "num_previous",
        }

    def test_conversations_list_parameters(self):
        assert set(FRAMEWORK["conversations_list"].parameters.keys()) == {"unread_only"}

    def test_module_level_vars_match_framework_entries(self):
        """Module-level ToolSpec variables must be the same objects as FRAMEWORK entries."""
        expected_vars = {
            "tasks_create",
            "tasks_create_batch",
            "tasks_assign",
            "tasks_submit_for_review",
            "tasks_submit_review",
            "tasks_mark_finished",
            "tasks_get",
            "tasks_list",
            "connections_list",
            "get_available_reviewers",
            "sleep",
            "send_message",
            "read_messages",
            "batch_read_messages",
            "conversations_list",
        }
        for var_name in expected_vars:
            module_var = getattr(specs_module, var_name)
            assert module_var is FRAMEWORK[var_name], (
                f"specs.{var_name} is not the same object as FRAMEWORK[{var_name!r}]"
            )

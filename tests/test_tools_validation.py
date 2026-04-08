"""Tests for magelab.tools.validation — Tool dependency and assignment validation."""

from magelab.tools.validation import (
    _compute_connection_tools,
    validate_all_tool_dependencies,
    validate_review_assignment,
    validate_task_assignments,
    validate_tool_dependencies,
)
from tests.helpers import make_task


# =============================================================================
# _compute_connection_tools
# =============================================================================


class TestComputeConnectionTools:
    def test_excludes_self(self):
        agent_to_tools = {
            "a": {"tool1", "tool2"},
            "b": {"tool2", "tool3"},
            "c": {"tool4"},
        }
        connections = _compute_connection_tools("a", agent_to_tools)
        assert connections == {"tool2", "tool3", "tool4"}

    def test_single_agent_empty_connections(self):
        agent_to_tools = {"a": {"tool1"}}
        connections = _compute_connection_tools("a", agent_to_tools)
        assert connections == set()

    def test_agent_not_in_mapping(self):
        """When agent_id is not in the mapping, all agents are connections — returns union of all tools."""
        agent_to_tools = {"a": {"t1"}, "b": {"t2"}}
        connections = _compute_connection_tools("nonexistent", agent_to_tools)
        assert connections == {"t1", "t2"}

    def test_empty_dict(self):
        """Empty agent_to_tools returns empty set."""
        connections = _compute_connection_tools("a", {})
        assert connections == set()


# =============================================================================
# validate_tool_dependencies
# =============================================================================


class TestValidateToolDependencies:
    def test_no_issues_when_properly_configured(self):
        """Worker with submit_for_review + get_available_reviewers, connections have submit_review."""
        agent_tools = {"tasks_submit_for_review", "get_available_reviewers"}
        connection_tools = {"tasks_submit_review", "get_available_reviewers"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, connection_tools)
        assert errors == []
        assert warnings == []

    def test_error_reviewers_without_submit(self):
        """get_available_reviewers without tasks_submit_for_review is an error."""
        agent_tools = {"get_available_reviewers"}
        connection_tools = {"tasks_submit_review"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, connection_tools)
        assert len(errors) == 1
        assert "tasks_submit_for_review" in errors[0]

    def test_warning_submit_without_discover(self):
        """tasks_submit_for_review without get_available_reviewers is a warning."""
        agent_tools = {"tasks_submit_for_review"}
        connection_tools = {"tasks_submit_review"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, connection_tools)
        assert errors == []
        assert len(warnings) == 1
        assert "get_available_reviewers" in warnings[0]

    def test_warning_no_peer_reviewers(self):
        """Both get_available_reviewers and tasks_submit_for_review warn when no peer has submit_review (rules #3 and #4)."""
        agent_tools = {"tasks_submit_for_review", "get_available_reviewers"}
        connection_tools = set()  # no connections have submit_review
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, connection_tools)
        assert errors == []
        assert len(warnings) == 2
        assert any("tasks_submit_review" in w for w in warnings)

    def test_no_trigger_tools_no_issues(self):
        """Agent with unrelated tools produces no errors/warnings."""
        agent_tools = {"tasks_create", "tasks_assign"}
        connection_tools = {"tasks_create"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, connection_tools)
        assert errors == []
        assert warnings == []

    def test_reviewer_side_warnings(self):
        """Agent with submit_review but empty connection_tools produces 2 warnings (rules #5 and #6)."""
        agent_tools = {"tasks_submit_review"}
        connection_tools = set()
        errors, warnings = validate_tool_dependencies("reviewer-1", agent_tools, connection_tools)
        assert errors == []
        assert len(warnings) == 2
        assert any("tasks_submit_for_review" in w for w in warnings)
        assert any("get_available_reviewers" in w for w in warnings)

    def test_conversations_without_read_is_error(self):
        """conversations_list without read_messages or batch_read_messages is an error."""
        agent_tools = {"conversations_list"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, set())
        assert len(errors) == 1
        assert "conversations_list" in errors[0]
        assert "read_messages" in errors[0]

    def test_send_without_connections_is_warning(self):
        """send_message without connections_list is a warning."""
        agent_tools = {"send_message", "read_messages"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, set())
        assert errors == []
        assert any("connections_list" in w for w in warnings)

    def test_send_without_read_is_warning(self):
        """send_message without read tools is a warning."""
        agent_tools = {"send_message", "connections_list"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, set())
        assert errors == []
        assert any("read tools" in w for w in warnings)

    def test_read_without_send_is_warning(self):
        """read_messages without send_message is a warning."""
        agent_tools = {"read_messages"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, set())
        assert errors == []
        assert any("send_message" in w for w in warnings)

    def test_full_communication_bundle_no_issues(self):
        """All communication tools together produce no errors or warnings."""
        agent_tools = {"connections_list", "send_message", "read_messages", "batch_read_messages", "conversations_list"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, set())
        assert errors == []
        assert warnings == []

    def test_send_alone_produces_two_warnings(self):
        """send_message alone: missing connections_list + missing read tools = 2 warnings."""
        agent_tools = {"send_message"}
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, set())
        assert errors == []
        assert len(warnings) == 2

    def test_simultaneous_error_and_warning(self):
        """Agent with get_available_reviewers only and empty connection_tools gets 1 error (rule #1) and 1 warning (rule #3)."""
        agent_tools = {"get_available_reviewers"}
        connection_tools = set()
        errors, warnings = validate_tool_dependencies("agent-1", agent_tools, connection_tools)
        assert len(errors) == 1
        assert "tasks_submit_for_review" in errors[0]
        assert len(warnings) == 1
        assert "tasks_submit_review" in warnings[0]


# =============================================================================
# validate_all_tool_dependencies
# =============================================================================


class TestValidateAllToolDependencies:
    def test_multi_agent_valid(self):
        agent_to_tools = {
            "pm": {"tasks_create_batch", "tasks_assign", "tasks_mark_finished"},
            "coder": {"tasks_submit_for_review", "get_available_reviewers", "tasks_mark_finished"},
            "reviewer": {"tasks_submit_review"},
        }
        errors, warnings = validate_all_tool_dependencies(agent_to_tools)
        assert errors == []
        assert warnings == []

    def test_multi_agent_missing_reviewer(self):
        """Organization without any reviewer agent produces warnings."""
        agent_to_tools = {
            "pm": {"tasks_create_batch", "tasks_assign"},
            "coder": {"tasks_submit_for_review", "get_available_reviewers"},
        }
        errors, warnings = validate_all_tool_dependencies(agent_to_tools)
        assert errors == []
        assert any("tasks_submit_review" in w for w in warnings)

    def test_single_agent_org(self):
        """Single agent with review tools — connections are empty, so peer-scope rules fire."""
        agent_to_tools = {
            "solo": {"tasks_submit_for_review", "get_available_reviewers", "tasks_submit_review"},
        }
        errors, warnings = validate_all_tool_dependencies(agent_to_tools)
        # No agent-scope errors (solo has all three tools on itself).
        assert errors == []
        # Peer-scope warnings fire because connection_tools is empty for a single-agent org:
        #   Rule #3: get_available_reviewers needs tasks_submit_review in connections -> warning
        #   Rule #4: tasks_submit_for_review needs tasks_submit_review in connections -> warning
        #   Rule #5: tasks_submit_review needs tasks_submit_for_review in connections -> warning
        #   Rule #6: tasks_submit_review needs get_available_reviewers in connections -> warning
        assert len(warnings) == 4
        assert all("solo" in w for w in warnings)

    def test_empty_dict(self):
        """Empty agent_to_tools produces no errors or warnings."""
        errors, warnings = validate_all_tool_dependencies({})
        assert errors == []
        assert warnings == []

    def test_explicit_connection_tools_restricts_scope(self):
        """When agent_to_connection_tools is provided, only those tools are checked (not all agents)."""
        agent_to_tools = {
            "coder": {"tasks_submit_for_review", "get_available_reviewers"},
            "reviewer": {"tasks_submit_review"},
        }
        # Without explicit connection_tools: fully connected, no warnings
        errors, warnings = validate_all_tool_dependencies(agent_to_tools)
        assert errors == []
        assert warnings == []

        # With explicit connection_tools: coder has no connections with submit_review
        connection_tools = {
            "coder": set(),  # isolated — no connection tools
            "reviewer": set(),
        }
        errors, warnings = validate_all_tool_dependencies(agent_to_tools, connection_tools)
        assert errors == []
        # coder's rules #3 and #4 fire (no connected agent has tasks_submit_review)
        assert any("tasks_submit_review" in w for w in warnings)

    def test_explicit_connection_tools_missing_key_defaults_empty(self):
        """Agent missing from agent_to_connection_tools gets empty set via .get()."""
        agent_to_tools = {
            "coder": {"tasks_submit_for_review", "get_available_reviewers"},
        }
        # coder not in connection_tools mapping at all
        errors, warnings = validate_all_tool_dependencies(agent_to_tools, {})
        assert errors == []
        # Rules #3 and #4 fire
        assert any("tasks_submit_review" in w for w in warnings)


# =============================================================================
# validate_review_assignment
# =============================================================================


class TestValidateReviewAssignment:
    def test_valid(self):
        agent_tools = {"tasks_submit_for_review"}
        connection_tools = {"tasks_submit_review"}
        result = validate_review_assignment("coder", agent_tools, connection_tools)
        assert result is None

    def test_no_submit_for_review(self):
        agent_tools = {"tasks_mark_finished"}
        connection_tools = {"tasks_submit_review"}
        result = validate_review_assignment("coder", agent_tools, connection_tools)
        assert "cannot submit" in result

    def test_no_peer_reviewers(self):
        agent_tools = {"tasks_submit_for_review"}
        connection_tools = {"tasks_mark_finished"}
        result = validate_review_assignment("coder", agent_tools, connection_tools)
        assert "no connected" in result

    def test_both_conditions_fail_returns_first_error(self):
        """When agent lacks submit_for_review AND connections lack submit_review, only the first error is returned (short-circuit)."""
        agent_tools = {"tasks_mark_finished"}
        connection_tools = {"tasks_mark_finished"}  # also lacks tasks_submit_review
        result = validate_review_assignment("coder", agent_tools, connection_tools)
        # Function checks submit_for_review first and returns immediately
        assert result is not None
        assert "cannot submit" in result
        # The second condition (no peer reviewers) is never reached
        assert "no connected" not in result


# =============================================================================
# validate_task_assignments
# =============================================================================


class TestValidateTaskAssignments:
    def test_valid_non_review_task(self):
        task = make_task(review_required=False)
        agent_to_tools = {"coder": {"tasks_mark_finished"}}
        errors, warnings = validate_task_assignments([(task, "coder", "User")], agent_to_tools)
        assert errors == []
        assert warnings == []

    def test_unknown_agent(self):
        task = make_task()
        agent_to_tools = {"coder": {"tasks_mark_finished"}}
        errors, warnings = validate_task_assignments([(task, "unknown", "User")], agent_to_tools)
        assert len(errors) == 1
        assert "unknown agent" in errors[0]

    def test_review_required_no_submit_tool(self):
        task = make_task(review_required=True)
        agent_to_tools = {"coder": {"tasks_mark_finished"}, "reviewer": {"tasks_submit_review"}}
        errors, warnings = validate_task_assignments([(task, "coder", "User")], agent_to_tools)
        assert len(errors) == 1
        assert "cannot submit" in errors[0]

    def test_review_required_no_peer_reviewer(self):
        task = make_task(review_required=True)
        agent_to_tools = {"coder": {"tasks_submit_for_review"}}
        errors, warnings = validate_task_assignments([(task, "coder", "User")], agent_to_tools)
        assert len(errors) == 1
        assert "no connected" in errors[0]

    def test_review_required_warning_no_discover(self):
        """Agent can submit for review but can't discover reviewers → warning."""
        task = make_task(review_required=True)
        agent_to_tools = {
            "coder": {"tasks_submit_for_review"},
            "reviewer": {"tasks_submit_review"},
        }
        errors, warnings = validate_task_assignments([(task, "coder", "User")], agent_to_tools)
        assert errors == []
        assert len(warnings) == 1
        assert "discover reviewers" in warnings[0]

    def test_review_required_fully_valid(self):
        task = make_task(review_required=True)
        agent_to_tools = {
            "coder": {"tasks_submit_for_review", "get_available_reviewers"},
            "reviewer": {"tasks_submit_review"},
        }
        errors, warnings = validate_task_assignments([(task, "coder", "User")], agent_to_tools)
        assert errors == []
        assert warnings == []

    def test_multiple_tasks_accumulate_errors(self):
        """Two review-required tasks: one validly assigned, one to an agent that can't submit — exactly 1 error."""
        task_ok = make_task(id="task-ok", review_required=True)
        task_bad = make_task(id="task-bad", review_required=True)
        agent_to_tools = {
            "coder": {"tasks_submit_for_review", "get_available_reviewers"},
            "helper": {"tasks_mark_finished"},  # lacks submit_for_review
            "reviewer": {"tasks_submit_review"},
        }
        errors, warnings = validate_task_assignments(
            [(task_ok, "coder", "User"), (task_bad, "helper", "User")],
            agent_to_tools,
        )
        assert len(errors) == 1
        assert "task-bad" in errors[0]
        assert "cannot submit" in errors[0]
        assert warnings == []

    def test_empty_task_list(self):
        """Empty task list produces no errors or warnings."""
        errors, warnings = validate_task_assignments([], {"coder": {"tasks_mark_finished"}})
        assert errors == []
        assert warnings == []

    def test_connection_tools_missing_agent_key(self):
        """When agent_to_connection_tools lacks the assigned agent's key, .get() fallback returns empty set, triggering 'no peer' error."""
        task = make_task(review_required=True)
        agent_to_tools = {
            "coder": {"tasks_submit_for_review", "get_available_reviewers"},
            "other": {"tasks_submit_review"},
        }
        # coder's key is missing from connection_tools mapping — .get(agent_id, set()) returns empty set
        errors, warnings = validate_task_assignments(
            [(task, "coder", "User")],
            agent_to_tools,
            agent_to_connection_tools={"other": {"tasks_submit_review"}},
        )
        assert len(errors) == 1
        assert "no connected" in errors[0]

    def test_explicit_connection_tools_parameter(self):
        """Pass a custom agent_to_connection_tools dict instead of relying on auto-computation."""
        task = make_task(review_required=True)
        agent_to_tools = {
            "coder": {"tasks_submit_for_review", "get_available_reviewers"},
            "reviewer": {"tasks_submit_review"},
        }
        # Override connection_tools: pretend coder has no connections with submit_review
        custom_connection_tools = {
            "coder": set(),  # empty — no peer tools
            "reviewer": {"tasks_submit_for_review", "get_available_reviewers"},
        }
        errors, warnings = validate_task_assignments(
            [(task, "coder", "User")],
            agent_to_tools,
            agent_to_connection_tools=custom_connection_tools,
        )
        # With empty connection_tools for coder, review validation fails
        assert len(errors) == 1
        assert "no connected" in errors[0]

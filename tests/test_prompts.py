"""Tests for magelab.prompts — Prompt formatting for all event types."""

import json
from dataclasses import dataclass

import pytest

from magelab.runners.prompts import (
    PromptContext,
    build_system_prompt,
    default_prompt_formatter,
    format_review_history,
    format_reviews,
)
from magelab.events import (
    BaseEvent,
    MCPEvent,
    ResumeEvent,
    ReviewFinishedEvent,
    ReviewRequestedEvent,
    TaskAssignedEvent,
    TaskFinishedEvent,
    WireMessageEvent,
)
from magelab.state.task_schemas import (
    ReviewStatus,
    Task,
    TaskStatus,
)
from tests.helpers import make_review_record, make_task


def _make_assigned_task(review_required: bool = False) -> Task:
    """Create a task with assignment history set up."""
    t = make_task(review_required=review_required)
    t.record_assignee("pm")
    t.record_assignee("worker")
    t.update_status(TaskStatus.IN_PROGRESS)
    return t


# =============================================================================
# Review formatting helpers
# =============================================================================


class TestFormatReviews:
    def test_empty(self):
        assert format_reviews([]) == "No reviews."

    def test_with_records(self):
        r = make_review_record()
        result = format_reviews([r])
        assert "reviewer-1" in result
        assert "worker-1" in result

    def test_with_multiple_records(self):
        """format_reviews with 2+ ReviewRecords includes all records in the output."""
        r1 = make_review_record(reviewer_id="rev-A", requester_id="worker-1")
        r1.submit("rev-A", ReviewStatus.APPROVED, "Looks good")
        r2 = make_review_record(reviewer_id="rev-B", requester_id="worker-1")
        r2.submit("rev-B", ReviewStatus.CHANGES_REQUESTED, "Needs work")
        result = format_reviews([r1, r2])
        # Result should be valid JSON
        parsed = json.loads(result)
        assert len(parsed) == 2
        # Both reviewer IDs present
        assert "rev-A" in result
        assert "rev-B" in result
        # Both comments present
        assert "Looks good" in result
        assert "Needs work" in result

    def test_format_review_history_empty(self):
        assert format_review_history([]) == ""

    def test_format_review_history_with_records(self):
        r = make_review_record()
        result = format_review_history([r])
        assert "# Review History" in result
        assert "reviewer-1" in result


# =============================================================================
# build_system_prompt
# =============================================================================


class TestBuildSystemPrompt:
    def test_basic(self):
        result = build_system_prompt("agent-1", "You are a coder.", "Org instructions", "/app/workspace")
        assert "Org instructions" in result
        assert "You are a coder." in result

    def test_agent_id_placeholder(self):
        result = build_system_prompt("coder-0", "Role prompt", "Hello {agent_id}", "/app/workspace")
        assert "Hello coder-0" in result

    def test_no_placeholder(self):
        result = build_system_prompt("coder-0", "Role prompt", "No placeholder here", "/app/workspace")
        assert "No placeholder here" in result

    def test_org_prompt_before_role_prompt(self):
        """Verify that org_prompt appears before role_prompt in the output.
        The source builds: system-message, then org_prompt, then role_prompt."""
        org = "ORG_MARKER_FIRST"
        role = "ROLE_MARKER_SECOND"
        result = build_system_prompt("agent-1", role, org, "/app/workspace")
        org_pos = result.index(org)
        role_pos = result.index(role)
        assert org_pos < role_pos, "org_prompt must appear before role_prompt"

    def test_working_directory_in_output(self):
        result = build_system_prompt("agent-1", "Role", "Org", "/app/workspace")
        assert "/app/workspace" in result
        assert "<system-message>" in result


# =============================================================================
# TaskAssigned prompt
# =============================================================================


class TestTaskAssignedPrompt:
    def _format(self, task: Task, tools: set[str] | None = None) -> str:
        if tools is None:
            tools = {"tasks_mark_finished", "tasks_submit_for_review", "get_available_reviewers"}
        event = TaskAssignedEvent(task_id=task.id, target_id="worker", source_id="pm")
        ctx = PromptContext(event=event, task=task, agent_tools=tools)
        return default_prompt_formatter(ctx)

    def test_basic_prompt(self):
        task = _make_assigned_task()
        prompt = self._format(task)
        assert task.id in prompt
        assert task.title in prompt
        assert task.description in prompt
        assert "pm" in prompt  # source

    def test_review_required_with_discover(self):
        task = _make_assigned_task(review_required=True)
        tools = {"tasks_submit_for_review", "get_available_reviewers"}
        prompt = self._format(task, tools)
        assert "get available reviewers" in prompt
        assert "submit your work" in prompt

    def test_review_required_without_discover(self):
        task = _make_assigned_task(review_required=True)
        tools = {"tasks_submit_for_review"}
        prompt = self._format(task, tools)
        assert "submit your work for review" in prompt
        assert "get available reviewers" not in prompt

    def test_review_required_without_submit_raises(self):
        task = _make_assigned_task(review_required=True)
        tools = {"tasks_mark_finished"}
        with pytest.raises(ValueError, match="lacks 'tasks_submit_for_review'"):
            self._format(task, tools)

    def test_optional_review_with_finish_and_discover(self):
        task = _make_assigned_task(review_required=False)
        tools = {"tasks_mark_finished", "tasks_submit_for_review", "get_available_reviewers"}
        prompt = self._format(task, tools)
        assert "mark the task as finished" in prompt
        assert "optionally" in prompt

    def test_assigned_optional_review_without_discover(self):
        task = _make_assigned_task(review_required=False)
        tools = {"tasks_mark_finished", "tasks_submit_for_review"}
        prompt = self._format(task, tools)
        assert "mark the task as finished" in prompt
        assert "optionally" in prompt
        assert "getting available reviewers" not in prompt

    def test_no_review_can_only_finish(self):
        task = _make_assigned_task(review_required=False)
        tools = {"tasks_mark_finished"}
        prompt = self._format(task, tools)
        assert "mark the task as finished" in prompt

    def test_assigned_no_tools_no_review(self):
        task = _make_assigned_task(review_required=False)
        prompt = self._format(task, tools=set())
        assert task.description in prompt
        assert "mark the task as finished" not in prompt
        assert "submit" not in prompt
        assert "review" not in prompt.lower()

    def test_assigned_with_review_history(self):
        task = _make_assigned_task(review_required=False)
        record = make_review_record(reviewer_id="rev-A", requester_id="worker")
        record.submit("rev-A", ReviewStatus.CHANGES_REQUESTED, "Needs more tests")
        task.review_history.append(record)
        prompt = self._format(task, {"tasks_mark_finished"})
        assert "# Review History" in prompt
        assert "rev-A" in prompt
        assert "Needs more tests" in prompt

    def test_submit_only_no_finish_no_review_tool(self):
        """Agent has submit_for_review but NOT mark_finished and NOT submit_review.
        With review_required=False, no conditional branch matches so the prompt
        contains only the base instruction with no action guidance."""
        task = _make_assigned_task(review_required=False)
        tools = {"tasks_submit_for_review"}
        prompt = self._format(task, tools)
        # The base instruction is always present
        assert "Your task details are given below." in prompt
        # No branch matches: can_finish is False, review_required is False
        # so there's no instruction about finishing or submitting for review
        assert "mark the task as finished" not in prompt
        assert task.description in prompt


# =============================================================================
# ReviewRequested prompt
# =============================================================================


class TestReviewRequestedPrompt:
    def _format(self, task: Task, tools: set[str] | None = None, message: str = "Please review") -> str:
        if tools is None:
            tools = {"tasks_submit_review"}
        event = ReviewRequestedEvent(task_id=task.id, target_id="reviewer", source_id="worker", request_message=message)
        ctx = PromptContext(event=event, task=task, agent_tools=tools)
        return default_prompt_formatter(ctx)

    def test_basic_prompt(self):
        task = _make_assigned_task()
        prompt = self._format(task)
        assert "review" in prompt.lower()
        assert task.description in prompt

    def test_with_submit_review_tool(self):
        task = _make_assigned_task()
        prompt = self._format(task, tools={"tasks_submit_review"})
        assert "submit your review" in prompt

    def test_without_submit_review_tool(self):
        task = _make_assigned_task()
        prompt = self._format(task, tools=set())
        assert "submit your review" not in prompt
        assert "review" in prompt.lower()

    def test_request_message_included(self):
        task = _make_assigned_task()
        prompt = self._format(task, message="Check auth logic")
        assert "Check auth logic" in prompt

    def test_review_requested_with_none_message(self):
        task = _make_assigned_task()
        event = ReviewRequestedEvent(task_id=task.id, target_id="reviewer", source_id="worker", request_message=None)
        ctx = PromptContext(event=event, task=task, agent_tools={"tasks_submit_review"})
        prompt = default_prompt_formatter(ctx)
        assert "Review Request Message: None" in prompt

    def test_review_requested_with_review_history(self):
        task = _make_assigned_task()
        record = make_review_record(reviewer_id="rev-B", requester_id="worker")
        record.submit("rev-B", ReviewStatus.CHANGES_REQUESTED, "Fix the edge case")
        task.review_history.append(record)
        prompt = self._format(task)
        assert "# Review History" in prompt
        assert "rev-B" in prompt
        assert "Fix the edge case" in prompt


# =============================================================================
# ReviewFinished prompts
# =============================================================================


class TestReviewFinishedPrompt:
    def _format(self, task: Task, outcome: TaskStatus, tools: set[str] | None = None) -> str:
        if tools is None:
            tools = {"tasks_mark_finished", "tasks_submit_for_review"}
        records = [make_review_record()]
        event = ReviewFinishedEvent(task_id=task.id, target_id="worker", outcome=outcome, review_records=records)
        ctx = PromptContext(event=event, task=task, agent_tools=tools)
        return default_prompt_formatter(ctx)

    def test_approved_prompt(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.APPROVED)
        assert "approved" in prompt.lower()
        assert "mark it as complete" in prompt

    def test_approved_finish_only(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.APPROVED, {"tasks_mark_finished"})
        assert "mark it as complete" in prompt
        assert "resubmit" not in prompt

    def test_changes_requested_prompt(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.CHANGES_REQUESTED)
        assert "changes" in prompt.lower()
        assert "resubmit" in prompt

    def test_review_failed_prompt(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.REVIEW_FAILED)
        assert "not able to complete" in prompt
        assert "re-attempt to submit the task for review" in prompt
        assert "mark the task as failed" in prompt

    def test_review_failed_submit_only(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.REVIEW_FAILED, {"tasks_submit_for_review"})
        assert "re-attempt to submit the task for review" in prompt
        assert "mark the task as failed" not in prompt
        assert "make changes to the task before resubmitting" in prompt

    def test_review_failed_finish_only(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.REVIEW_FAILED, {"tasks_mark_finished"})
        assert "mark the task as failed" in prompt
        assert "re-attempt to submit" not in prompt
        assert "make changes" not in prompt

    def test_approved_request_review_only(self):
        """When agent has submit_for_review but not mark_finished, approved mentions conditional resubmit."""
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.APPROVED, {"tasks_submit_for_review"})
        assert "resubmit the task for review" in prompt
        assert "mark it as complete" not in prompt

    def test_approved_no_actions(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.APPROVED, set())
        assert "No further action" in prompt

    def test_review_failed_no_actions(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.REVIEW_FAILED, set())
        assert "not able to complete" in prompt
        assert "re-attempt to submit" not in prompt
        assert "mark the task as failed" not in prompt

    def test_changes_requested_without_resubmit(self):
        task = _make_assigned_task()
        prompt = self._format(task, TaskStatus.CHANGES_REQUESTED, {"tasks_mark_finished"})
        assert "resubmit" not in prompt

    def test_unknown_outcome_returns_none(self):
        task = _make_assigned_task()
        tools = {"tasks_mark_finished"}
        event = ReviewFinishedEvent(
            task_id=task.id, target_id="worker", outcome=TaskStatus.IN_PROGRESS, review_records=[]
        )
        ctx = PromptContext(event=event, task=task, agent_tools=tools)
        assert default_prompt_formatter(ctx) is None


# =============================================================================
# TaskFinished prompt
# =============================================================================


class TestTaskFinishedPrompt:
    def test_succeeded(self):
        task = _make_assigned_task()
        event = TaskFinishedEvent(task_id=task.id, target_id="pm", outcome=TaskStatus.SUCCEEDED, details="All done")
        ctx = PromptContext(event=event, task=task)
        prompt = default_prompt_formatter(ctx)
        assert "succeeded" in prompt
        assert "All done" in prompt

    def test_failed(self):
        task = _make_assigned_task()
        event = TaskFinishedEvent(task_id=task.id, target_id="pm", outcome=TaskStatus.FAILED, details="Error")
        ctx = PromptContext(event=event, task=task)
        prompt = default_prompt_formatter(ctx)
        assert "failed" in prompt
        assert "Error" in prompt


# =============================================================================
# Edge cases
# =============================================================================


class TestMCPEvent:
    def test_mcp_event_renders_payload_verbatim(self):
        event = MCPEvent(target_id="agent-0", server_name="market", payload="Price alert: ACME crossed $50")
        ctx = PromptContext(event=event)
        result = default_prompt_formatter(ctx)
        assert result == "Price alert: ACME crossed $50"

    def test_mcp_event_no_task_needed(self):
        """MCPEvent should render even without a task in context."""
        event = MCPEvent(target_id="agent-0", server_name="market", payload="Update")
        ctx = PromptContext(event=event, task=None)
        result = default_prompt_formatter(ctx)
        assert result == "Update"

    def test_mcp_event_multiline_payload(self):
        payload = "## Market Update\n\n- ACME: $50.25\n- BETA: $12.00"
        event = MCPEvent(target_id="agent-0", server_name="market", payload=payload)
        ctx = PromptContext(event=event)
        assert default_prompt_formatter(ctx) == payload


class TestEdgeCases:
    def test_no_task_returns_none(self):
        event = TaskAssignedEvent(task_id="t1", target_id="w", source_id="pm")
        ctx = PromptContext(event=event, task=None)
        assert default_prompt_formatter(ctx) is None

    def test_unknown_event_type_returns_none(self):
        """An event type not matching any isinstance check falls through to return None (line 329)."""

        @dataclass(kw_only=True)
        class UnknownEvent(BaseEvent):
            """A custom event type not handled by default_prompt_formatter."""

            task_id: str
            extra: str = "surprise"

        task = _make_assigned_task()
        event = UnknownEvent(task_id=task.id, target_id="worker", extra="test")
        ctx = PromptContext(event=event, task=task, agent_tools={"tasks_mark_finished"})
        result = default_prompt_formatter(ctx)
        assert result is None


# =============================================================================
# Template structure — task title and ID present for all event types
# =============================================================================


class TestTemplateStructure:
    """Verify that prompts for each event type contain the task title and task ID."""

    def test_task_assigned_contains_title_and_id(self):
        task = _make_assigned_task()
        event = TaskAssignedEvent(task_id=task.id, target_id="worker", source_id="pm")
        ctx = PromptContext(event=event, task=task, agent_tools={"tasks_mark_finished"})
        prompt = default_prompt_formatter(ctx)
        assert task.id in prompt
        assert task.title in prompt

    def test_review_requested_contains_title_and_id(self):
        task = _make_assigned_task()
        event = ReviewRequestedEvent(
            task_id=task.id, target_id="reviewer", source_id="worker", request_message="Please review"
        )
        ctx = PromptContext(event=event, task=task, agent_tools={"tasks_submit_review"})
        prompt = default_prompt_formatter(ctx)
        assert task.id in prompt
        assert task.title in prompt

    def test_review_finished_approved_contains_title_and_id(self):
        task = _make_assigned_task()
        records = [make_review_record()]
        event = ReviewFinishedEvent(
            task_id=task.id, target_id="worker", outcome=TaskStatus.APPROVED, review_records=records
        )
        ctx = PromptContext(event=event, task=task, agent_tools={"tasks_mark_finished"})
        prompt = default_prompt_formatter(ctx)
        assert task.id in prompt
        assert task.title in prompt

    def test_review_finished_changes_requested_contains_title_and_id(self):
        task = _make_assigned_task()
        records = [make_review_record()]
        event = ReviewFinishedEvent(
            task_id=task.id, target_id="worker", outcome=TaskStatus.CHANGES_REQUESTED, review_records=records
        )
        ctx = PromptContext(event=event, task=task, agent_tools={"tasks_submit_for_review"})
        prompt = default_prompt_formatter(ctx)
        assert task.id in prompt
        assert task.title in prompt

    def test_review_finished_failed_contains_title_and_id(self):
        task = _make_assigned_task()
        records = [make_review_record()]
        event = ReviewFinishedEvent(
            task_id=task.id, target_id="worker", outcome=TaskStatus.REVIEW_FAILED, review_records=records
        )
        ctx = PromptContext(event=event, task=task, agent_tools={"tasks_mark_finished", "tasks_submit_for_review"})
        prompt = default_prompt_formatter(ctx)
        assert task.id in prompt
        assert task.title in prompt

    def test_task_finished_contains_title_and_id(self):
        task = _make_assigned_task()
        event = TaskFinishedEvent(task_id=task.id, target_id="pm", outcome=TaskStatus.SUCCEEDED, details="Complete")
        ctx = PromptContext(event=event, task=task)
        prompt = default_prompt_formatter(ctx)
        assert task.id in prompt
        assert task.title in prompt


# =============================================================================
# WireMessageEvent prompt
# =============================================================================


class TestWireMessagePrompt:
    def test_single_conversation(self):
        """Single wire conversation uses singular template."""
        event = WireMessageEvent(target_id="bob", wire_id="conv-1", source_id="alice", message_cursor=1)
        ctx = PromptContext(event=event, wire_conversations=["alice: Hello bob!"])
        prompt = default_prompt_formatter(ctx)
        assert "You have a new message." in prompt
        assert "alice: Hello bob!" in prompt

    def test_multiple_conversations_batched(self):
        """Multiple wire conversations use plural template."""
        event = WireMessageEvent(target_id="bob", wire_id="conv-1", source_id="alice", message_cursor=1)
        conversations = ["alice: First message", "carol: Second message"]
        ctx = PromptContext(event=event, wire_conversations=conversations)
        prompt = default_prompt_formatter(ctx)
        assert "You have new messages." in prompt
        assert "First message" in prompt
        assert "Second message" in prompt

    def test_empty_conversations_returns_none(self):
        """No conversations (all stale) returns None."""
        event = WireMessageEvent(target_id="bob", wire_id="conv-1", source_id="alice", message_cursor=1)
        ctx = PromptContext(event=event, wire_conversations=[])
        assert default_prompt_formatter(ctx) is None

    def test_no_task_needed(self):
        """Wire prompts work without a task in context."""
        event = WireMessageEvent(target_id="bob", wire_id="conv-1", source_id="alice", message_cursor=1)
        ctx = PromptContext(event=event, task=None, wire_conversations=["alice: Hey!"])
        prompt = default_prompt_formatter(ctx)
        assert prompt is not None
        assert "alice: Hey!" in prompt


# =============================================================================
# ResumeEvent prompt
# =============================================================================


class TestResumePrompt:
    def test_resume_prompt(self):
        """ResumeEvent produces a prompt telling the agent to continue."""
        task = _make_assigned_task()
        event = ResumeEvent(task_id=task.id, target_id="worker", was_reviewing=False)
        ctx = PromptContext(event=event, task=task)
        prompt = default_prompt_formatter(ctx)
        assert "interrupted" in prompt
        assert "continue" in prompt

    def test_resume_reviewing_same_prompt(self):
        """ResumeEvent with was_reviewing=True produces the same prompt text."""
        task = _make_assigned_task()
        event = ResumeEvent(task_id=task.id, target_id="worker", was_reviewing=True)
        ctx = PromptContext(event=event, task=task)
        prompt = default_prompt_formatter(ctx)
        assert "interrupted" in prompt
        assert "continue" in prompt

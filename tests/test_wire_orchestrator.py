"""Tests for wire event handling in the Orchestrator.

Uses the same MockRunner pattern as test_orchestrator.py to test wire event
routing, staleness, notification dedup, and sync mode delivery.

NOTE: In sync mode, wire events created in round N are processed in round N+1.
The sync loop stops when all tasks finish OR no events remain. Tests must ensure
tasks stay open long enough for wire events to be processed.
"""

import logging
import tempfile
from pathlib import Path

import pytest

from magelab.org_config import WireNotifications
from magelab.registry_config import AgentConfig, RoleConfig
from magelab.orchestrator import Orchestrator
from magelab.state.database import Database
from magelab.state.registry import Registry
from magelab.state.task_schemas import Task, TaskStatus
from magelab.state.task_store import TaskStore
from magelab.state.wire_store import WireStore

from .conftest import MockRunner, get_agent_dispatches

_test_logger = logging.getLogger("test")

# =============================================================================
# Helpers
# =============================================================================


def _make_wire_org(
    wire_notifications: WireNotifications = WireNotifications.ALL,
    tmp_dir: Path | None = None,
) -> tuple[TaskStore, Registry, MockRunner, WireStore, Database]:
    """Create a standard org with wire support.

    Pass tmp_dir (e.g. from pytest tmp_path) for automatic cleanup.
    Falls back to tempfile.mkdtemp() if not provided.
    """
    roles = {
        "worker": RoleConfig(
            name="worker",
            role_prompt="You work on tasks.",
            tools=["worker", "claude_basic", "communication"],
            model="test",
            max_turns=10,
        ),
    }
    agents = {
        "alice": AgentConfig(agent_id="alice", role="worker"),
        "bob": AgentConfig(agent_id="bob", role="worker"),
        "carol": AgentConfig(agent_id="carol", role="worker"),
    }

    if tmp_dir is None:
        tmp_dir = Path(tempfile.mkdtemp())
    db = Database(tmp_dir / "org.db")
    db.init_run_meta(org_name="test", org_config="{}")

    store = TaskStore(framework_logger=_test_logger, db=db)
    registry = Registry(framework_logger=_test_logger, db=db)
    registry.register_config(roles, agents)
    runner = MockRunner()
    wire_store = WireStore(framework_logger=_test_logger, db=db, wire_notifications=wire_notifications)
    return store, registry, runner, wire_store, db


def _make_orchestrator(
    store: TaskStore,
    registry: Registry,
    runner: MockRunner,
    wire_store: WireStore,
    db: Database,
    global_timeout: float = 30.0,
) -> Orchestrator:
    return Orchestrator(store, registry, runner, wire_store, db, global_timeout, "Test org", "/test/workspace")


def _make_task(id: str = "task-1", title: str = "Test Task", description: str = "Do work") -> Task:
    return Task(id=id, title=title, description=description)


# =============================================================================
# Wire event routing (sync mode)
# =============================================================================


class TestWireEventRouting:
    @pytest.mark.asyncio
    async def test_wire_message_dispatches_to_recipient(self):
        """When alice sends a message to bob, bob should receive a WireMessageEvent
        and be dispatched to handle it.

        Uses two tasks: alice and bob each get one. Alice sends a wire in round 1,
        bob processes the wire event alongside his task event in round 1 (or round 2).
        """
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        # alice: sends wire, does NOT finish her task yet
        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey bob!")

        # alice: finishes her task in her second dispatch (round 3, triggered by no-op)
        # Actually, we'll just let it time out — the test is about wire routing, not task completion.
        # Instead, alice finishes in a second dispatch.
        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1", title="Alice Task")
        t2 = _make_task(id="task-2", title="Bob Task")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # bob should have been dispatched for the WireMessageEvent
        bob_dispatches = get_agent_dispatches(db, "bob")
        assert any(d["event_type"] == "WireMessageEvent" for d in bob_dispatches)
        # The wire dispatch should have wire_id set
        wire_dispatch = next(d for d in bob_dispatches if d["event_type"] == "WireMessageEvent")
        assert wire_dispatch["wire_id"] == "conv-1"
        assert wire_dispatch["task_id"] is None

    @pytest.mark.asyncio
    async def test_wire_message_prompt_contains_conversation(self):
        """The prompt sent to the agent for a wire event should contain the conversation."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Important message!")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # Find the prompt sent to bob for the wire event (second call to bob)
        bob_calls = [c for c in runner.calls if c[0] == "bob"]
        assert len(bob_calls) >= 2  # TaskAssigned + WireMessage
        wire_prompt = bob_calls[1][2]  # (agent_id, system_prompt, prompt)
        assert "Important message!" in wire_prompt
        assert "conv-1" in wire_prompt

    @pytest.mark.asyncio
    async def test_wire_sender_not_notified(self):
        """The sender of a wire message should NOT receive a WireMessageEvent for it."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey!")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # alice should not have any WireMessageEvent dispatches
        alice_dispatches = get_agent_dispatches(db, "alice")
        alice_events = [d["event_type"] for d in alice_dispatches]
        assert "WireMessageEvent" not in alice_events

    @pytest.mark.asyncio
    async def test_wire_multi_participant(self):
        """Message to 3-person wire sends events to all non-sender participants."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob", "carol"], sender="alice", body="Team update")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        async def carol_finishes():
            await store.mark_finished("task-3", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]
        runner.side_effects["carol"] = [carol_finishes]

        t1 = _make_task(id="task-1", title="Alice Task")
        t2 = _make_task(id="task-2", title="Bob Task")
        t3 = _make_task(id="task-3", title="Carol Task")
        await orch.run(
            initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User"), (t3, "carol", "User")],
            sync=True,
            sync_max_rounds=5,
        )

        # Both bob and carol should have wire dispatches
        for agent in ["bob", "carol"]:
            agent_dispatches = get_agent_dispatches(db, agent)
            wire_dispatches = [d for d in agent_dispatches if d["event_type"] == "WireMessageEvent"]
            assert len(wire_dispatches) == 1


# =============================================================================
# Wire event staleness
# =============================================================================


class TestWireEventStaleness:
    @pytest.mark.asyncio
    async def test_stale_wire_event_skipped(self):
        """If bob reads messages (advancing cursor) before the queued event is
        processed, the event becomes stale and is skipped."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        # alice sends and immediately advances bob's cursor (simulating bob reading via tool)
        async def alice_sends_and_bob_reads():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey!")
            await wire_store.mark_read("conv-1", "bob", 1)

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends_and_bob_reads, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # bob should NOT have a WireMessageEvent dispatch
        bob_dispatches = get_agent_dispatches(db, "bob")
        bob_events = [d["event_type"] for d in bob_dispatches]
        assert "WireMessageEvent" not in bob_events


# =============================================================================
# Wire event failure handling
# =============================================================================


class TestWireEventFailure:
    @pytest.mark.asyncio
    async def test_wire_event_failure_does_not_affect_tasks(self):
        """When an agent fails handling a wire event, the agent's task should NOT
        be marked failed.

        Both alice and bob have tasks. Alice sends a wire to bob. Bob succeeds
        on his task dispatch but fails on the wire dispatch. Bob's task should
        remain SUCCEEDED — the wire failure must not retroactively fail it.
        """
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        original_run = runner.run_agent
        call_count = {"bob": 0}

        async def bob_fails_on_wire(agent_id, system_prompt, prompt):
            """Bob succeeds on task dispatch but fails on wire dispatch."""
            if agent_id == "bob":
                call_count["bob"] += 1
                if call_count["bob"] > 1:
                    # Second call (wire event) — return error
                    runner.calls.append((agent_id, system_prompt, prompt))
                    from magelab.runners.agent_runner import AgentRunResult

                    return AgentRunResult(error="Wire handling failed", num_turns=1, cost_usd=0.01)
            return await original_run(agent_id, system_prompt, prompt)

        runner.run_agent = bob_fails_on_wire

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey!")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1", title="Alice Task")
        t2 = _make_task(id="task-2", title="Bob Task")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # bob's TASK should be SUCCEEDED — wire failure must not affect it
        stored_t2 = await store.get_task("task-2")
        assert stored_t2.status == TaskStatus.SUCCEEDED
        # bob's wire dispatch should record the error
        bob_dispatches = get_agent_dispatches(db, "bob")
        wire_dispatch = next(d for d in bob_dispatches if d["event_type"] == "WireMessageEvent")
        assert wire_dispatch["error"] is not None
        assert wire_dispatch["wire_id"] == "conv-1"

    @pytest.mark.asyncio
    async def test_wire_event_exception_does_not_affect_tasks(self):
        """When run_agent raises an exception for a wire event, no task is affected.

        Bob successfully finishes his task in round 1, then gets a wire event in
        round 2. The exception on the wire event should not fail any task.
        """
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        original_run = runner.run_agent
        call_count = {"bob": 0}

        async def exploding_bob_on_wire(agent_id, system_prompt, prompt):
            if agent_id == "bob":
                call_count["bob"] += 1
                if call_count["bob"] > 1:
                    # Second call (wire event) — explode
                    runner.calls.append((agent_id, system_prompt, prompt))
                    raise RuntimeError("Bob crashed on wire event")
            return await original_run(agent_id, system_prompt, prompt)

        runner.run_agent = exploding_bob_on_wire

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey!")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # task-2 succeeded (bob finished it); wire exception didn't affect it
        stored_t2 = await store.get_task("task-2")
        assert stored_t2.status == TaskStatus.SUCCEEDED
        # bob should have a dispatch with the error for the wire event
        bob_dispatches = get_agent_dispatches(db, "bob")
        wire_dispatch = next((d for d in bob_dispatches if d["event_type"] == "WireMessageEvent"), None)
        assert wire_dispatch is not None
        assert "Bob crashed on wire event" in wire_dispatch["error"]


# =============================================================================
# Config: event_notifications disabled
# =============================================================================


class TestEventNotificationsDisabled:
    @pytest.mark.asyncio
    async def test_no_events_when_disabled(self):
        """When event_notifications=False, wire events should NOT be queued."""
        store, registry, runner, wire_store, db = _make_wire_org(wire_notifications=WireNotifications.TOOL)
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey!")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # bob should only have a TaskAssignedEvent, no WireMessageEvent
        bob_dispatches = get_agent_dispatches(db, "bob")
        bob_events = [d["event_type"] for d in bob_dispatches]
        assert "WireMessageEvent" not in bob_events


# =============================================================================
# Wire + task events mixed
# =============================================================================


class TestMixedWireAndTask:
    @pytest.mark.asyncio
    async def test_wire_and_task_events_coexist(self):
        """Wire events and task events should both be processed in the same run.

        alice sends a wire and keeps task open -> bob gets both task + wire events.
        """
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="FYI")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1", title="Alice task")
        t2 = _make_task(id="task-2", title="Bob task")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # bob should have 2 dispatches: TaskAssigned + WireMessage
        bob_dispatches = get_agent_dispatches(db, "bob")
        bob_events = [d["event_type"] for d in bob_dispatches]
        assert "TaskAssignedEvent" in bob_events
        assert "WireMessageEvent" in bob_events


# =============================================================================
# Sync mode batching
# =============================================================================


class TestWireEventBatching:
    @pytest.mark.asyncio
    async def test_first_wire_event_fetches_all_unread(self):
        """When multiple wire events are queued, the first one fetches all unread
        wires for the agent. Subsequent wire events go stale."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        # alice and carol each send a message to bob (different participant sets -> different wires)
        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="First message")

        async def carol_sends():
            await wire_store.create_wire("conv-2", ["carol", "bob"], sender="carol", body="Second message")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        async def carol_finishes():
            await store.mark_finished("task-3", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["carol"] = [carol_sends, carol_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        t3 = _make_task(id="task-3", title="Carol Task")
        await orch.run(
            initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User"), (t3, "carol", "User")],
            sync=True,
            sync_max_rounds=5,
        )

        # bob gets ONE wire dispatch (first event fetches all unread, second goes stale)
        bob_dispatches = get_agent_dispatches(db, "bob")
        bob_wire = [d for d in bob_dispatches if d["event_type"] == "WireMessageEvent"]
        assert len(bob_wire) == 1

        # bob should only have been called for wire events ONCE
        bob_calls = [c for c in runner.calls if c[0] == "bob"]
        # 1 call for TaskAssigned + 1 call for wire event = 2 total
        assert len(bob_calls) == 2
        # The wire prompt should contain BOTH messages (fetched all unread)
        wire_prompt = bob_calls[1][2]
        assert "First message" in wire_prompt
        assert "Second message" in wire_prompt

    @pytest.mark.asyncio
    async def test_wire_batch_uses_batch_template(self):
        """When multiple wire events are batched, the prompt should use the batch template
        ('You have new messages.') not the single template."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Msg 1")

        async def carol_sends():
            await wire_store.create_wire("conv-2", ["carol", "bob"], sender="carol", body="Msg 2")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        async def carol_finishes():
            await store.mark_finished("task-3", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["carol"] = [carol_sends, carol_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        t3 = _make_task(id="task-3", title="Carol Task")
        await orch.run(
            initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User"), (t3, "carol", "User")],
            sync=True,
            sync_max_rounds=5,
        )

        bob_calls = [c for c in runner.calls if c[0] == "bob"]
        wire_prompt = bob_calls[1][2]
        assert "You have new messages." in wire_prompt

    @pytest.mark.asyncio
    async def test_single_wire_event_uses_single_template(self):
        """When only one wire event is queued, the prompt should use the single template."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hello!")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        bob_calls = [c for c in runner.calls if c[0] == "bob"]
        wire_prompt = bob_calls[1][2]
        assert "You have a new message." in wire_prompt
        assert "new messages" not in wire_prompt  # not the batch template

    @pytest.mark.asyncio
    async def test_wire_batch_stale_extras_skipped(self):
        """In a batch, stale extra events should be skipped while live ones are included."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends_and_bob_reads_one():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Msg 1")

        async def carol_sends():
            await wire_store.create_wire("conv-2", ["carol", "bob"], sender="carol", body="Msg 2")
            # Bob reads conv-1 (simulating tool notification read), making that event stale
            await wire_store.mark_read("conv-1", "bob", 1)

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        async def carol_finishes():
            await store.mark_finished("task-3", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends_and_bob_reads_one, alice_finishes]
        runner.side_effects["carol"] = [carol_sends, carol_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        t3 = _make_task(id="task-3", title="Carol Task")
        await orch.run(
            initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User"), (t3, "carol", "User")],
            sync=True,
            sync_max_rounds=5,
        )

        # Only conv-2 should have a wire dispatch (conv-1 was stale)
        bob_dispatches = get_agent_dispatches(db, "bob")
        bob_wire = [d for d in bob_dispatches if d["event_type"] == "WireMessageEvent"]
        assert len(bob_wire) == 1
        assert bob_wire[0]["wire_id"] == "conv-2"

    @pytest.mark.asyncio
    async def test_wire_between_task_events_not_batched_with_tasks(self):
        """Wire events should be batched together but not with task events.
        Task events before or after wires get their own dispatch."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        # Set up: bob gets task-2 assigned, then wire events, then task-3 assigned
        async def alice_sends_and_creates_task():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey")
            await store.create(
                Task(id="task-3", title="Extra Task", description="More work"),
                assigned_to="bob",
                assigned_by="alice",
            )

        async def bob_finishes_t2():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes_t3():
            await store.mark_finished("task-3", TaskStatus.SUCCEEDED, "done")

        async def alice_finishes():
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends_and_creates_task, alice_finishes]
        runner.side_effects["bob"] = [bob_finishes_t2, bob_finishes_t3]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")], sync=True, sync_max_rounds=5)

        # bob should have separate dispatches for task and wire events
        bob_dispatches = get_agent_dispatches(db, "bob")
        assert len(bob_dispatches) >= 2, f"Expected at least 2 dispatches, got {len(bob_dispatches)}"
        task_types = [d["event_type"] for d in bob_dispatches]
        assert "TaskAssignedEvent" in task_types
        assert "WireMessageEvent" in task_types
        # Verify separation: no single dispatch has both a task_id AND a wire_id
        for d in bob_dispatches:
            assert not (d["task_id"] is not None and d["wire_id"] is not None), (
                f"Dispatch has both task_id and wire_id — events were batched together: {d}"
            )


# =============================================================================
# Async mode wire event routing
# =============================================================================


class TestAsyncModeWireEvents:
    @pytest.mark.asyncio
    async def test_async_wire_message_dispatches_to_recipient(self):
        """In async mode, wire events are delivered via queue and processed concurrently.

        Unlike sync mode, agents only run when events arrive. So side effects must
        complete all work within the dispatch that triggers them.
        """
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends_and_finishes():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Async hello!")
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends_and_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")])

        assert orch.outcome == "success"
        # bob should have a WireMessageEvent dispatch
        bob_dispatches = get_agent_dispatches(db, "bob")
        assert any(d["event_type"] == "WireMessageEvent" for d in bob_dispatches)
        wire_dispatch = next(d for d in bob_dispatches if d["event_type"] == "WireMessageEvent")
        assert wire_dispatch["wire_id"] == "conv-1"

    @pytest.mark.asyncio
    async def test_async_wire_stale_event_skipped(self):
        """In async mode, stale wire events are skipped (same as sync)."""
        store, registry, runner, wire_store, db = _make_wire_org()
        orch = _make_orchestrator(store, registry, runner, wire_store=wire_store, db=db)

        async def alice_sends_bob_reads_and_finishes():
            await wire_store.create_wire("conv-1", ["alice", "bob"], sender="alice", body="Hey!")
            await wire_store.mark_read("conv-1", "bob", 1)
            await store.mark_finished("task-1", TaskStatus.SUCCEEDED, "done")

        async def bob_finishes():
            await store.mark_finished("task-2", TaskStatus.SUCCEEDED, "done")

        runner.side_effects["alice"] = [alice_sends_bob_reads_and_finishes]
        runner.side_effects["bob"] = [bob_finishes]

        t1 = _make_task(id="task-1")
        t2 = _make_task(id="task-2")
        await orch.run(initial_tasks=[(t1, "alice", "User"), (t2, "bob", "User")])

        assert orch.outcome == "success"
        bob_dispatches = get_agent_dispatches(db, "bob")
        bob_events = [d["event_type"] for d in bob_dispatches]
        assert "WireMessageEvent" not in bob_events

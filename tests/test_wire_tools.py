"""Tests for wire communication tool implementations (send_message, read_messages, conversations_list).

Uses real WireStore + Registry to test tool handlers end-to-end.
"""

import logging

import pytest

from magelab.org_config import WireNotifications
from magelab.registry_config import AgentConfig, NetworkConfig, RoleConfig
from magelab.state.registry import Registry
from magelab.state.task_store import TaskStore
from magelab.tools.implementations import create_tool_implementations
from magelab.state.wire_store import WireStore

_test_logger = logging.getLogger("test")


# =============================================================================
# Fixtures
# =============================================================================


def _setup(agent_id: str = "alice") -> tuple[WireStore, Registry, TaskStore, dict]:
    """Create a WireStore, Registry, TaskStore, and tool implementations for testing.

    Network: alice <-> bob <-> charlie (alice and charlie not directly connected).
    """
    roles = {
        "worker": RoleConfig(
            name="worker",
            role_prompt="Work",
            tools=["communication", "worker", "claude_basic"],
            model="test",
            max_turns=10,
        ),
    }
    agents = {
        "alice": AgentConfig(agent_id="alice", role="worker"),
        "bob": AgentConfig(agent_id="bob", role="worker"),
        "charlie": AgentConfig(agent_id="charlie", role="worker"),
    }
    network = NetworkConfig(connections={"alice": ["bob"], "bob": ["charlie"]})
    store = TaskStore(framework_logger=_test_logger)
    wire_store = WireStore(framework_logger=_test_logger)
    registry = Registry(framework_logger=_test_logger)
    registry.register_config(roles, agents, network)
    impls = create_tool_implementations(store, registry, agent_id, wire_store=wire_store)
    return wire_store, registry, store, impls


def _setup_pair(agent_id: str, wire_store: WireStore, store: TaskStore, registry: Registry) -> dict:
    """Create impls for a second agent sharing the same wire_store, task_store, and registry."""
    return create_tool_implementations(store, registry, agent_id, wire_store=wire_store)


# =============================================================================
# send_message
# =============================================================================


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_to_recipients_creates_wire(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["send_message"]({"recipients": ["bob"], "body": "Hello bob"})
        assert not result.is_error
        assert "conversation" in result.text.lower() or "started" in result.text.lower()

    @pytest.mark.asyncio
    async def test_send_returns_conversation_id(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        assert not result.is_error
        # Should contain a wire_id (the hex id)
        wires = await ws.list_wires("alice")
        assert len(wires) == 1

    @pytest.mark.asyncio
    async def test_send_to_same_recipients_reuses_wire(self):
        ws, registry, store, impls = _setup("alice")
        r1 = await impls["send_message"]({"recipients": ["bob"], "body": "First"})
        r2 = await impls["send_message"]({"recipients": ["bob"], "body": "Second"})
        assert not r1.is_error
        assert not r2.is_error
        wires = await ws.list_wires("alice")
        assert len(wires) == 1
        wire = await ws.get_wire(wires[0].wire_id)
        assert len(wire.messages) == 2

    @pytest.mark.asyncio
    async def test_send_to_unconnected_agent_fails(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["send_message"]({"recipients": ["charlie"], "body": "Hello"})
        assert result.is_error
        assert "not connected" in result.text

    @pytest.mark.asyncio
    async def test_send_with_conversation_id(self):
        ws, registry, store, impls = _setup("alice")
        # Create a wire first
        await impls["send_message"]({"recipients": ["bob"], "body": "First"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        # Reply by conversation_id
        r2 = await impls["send_message"]({"conversation_id": wire_id, "body": "Reply"})
        assert not r2.is_error
        wire = await ws.get_wire(wire_id)
        assert len(wire.messages) == 2

    @pytest.mark.asyncio
    async def test_send_with_conversation_id_not_participant(self):
        ws, registry, store, impls = _setup("alice")
        # Alice creates wire with bob
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        # Charlie tries to reply — not a participant
        charlie_impls = _setup_pair("charlie", ws, store, registry)
        result = await charlie_impls["send_message"]({"conversation_id": wire_id, "body": "Hi"})
        assert result.is_error
        assert "not a participant" in result.text

    @pytest.mark.asyncio
    async def test_send_with_conversation_id_and_matching_recipients(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        result = await impls["send_message"]({"conversation_id": wire_id, "recipients": ["bob"], "body": "Again"})
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_send_with_conversation_id_and_mismatched_recipients(self):
        ws, registry, store, impls = _setup("bob")
        await impls["send_message"]({"recipients": ["alice"], "body": "Hello"})
        wires = await ws.list_wires("bob")
        wire_id = wires[0].wire_id

        # Bob tries to reply with charlie as recipient — mismatch
        result = await impls["send_message"]({"conversation_id": wire_id, "recipients": ["charlie"], "body": "Hi"})
        assert result.is_error
        assert "don't match" in result.text.lower()

    @pytest.mark.asyncio
    async def test_send_with_no_recipients_or_conversation_id(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["send_message"]({"body": "Hello?"})
        assert result.is_error

    @pytest.mark.asyncio
    async def test_send_with_empty_recipients(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["send_message"]({"recipients": [], "body": "Hello?"})
        assert result.is_error

    @pytest.mark.asyncio
    async def test_send_nonexistent_conversation_id(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["send_message"]({"conversation_id": "nonexistent", "body": "Hello"})
        assert result.is_error
        assert "not found" in result.text.lower()

    @pytest.mark.asyncio
    async def test_send_to_self_only_fails(self):
        """Sending to only yourself (recipients=["alice"] as alice) is rejected."""
        ws, registry, store, impls = _setup("alice")
        result = await impls["send_message"]({"recipients": ["alice"], "body": "Talking to myself"})
        assert result.is_error

    @pytest.mark.asyncio
    async def test_sender_included_in_participants(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        wires = await ws.list_wires("alice")
        wire = await ws.get_wire(wires[0].wire_id)
        assert "alice" in wire.participants
        assert "bob" in wire.participants


# =============================================================================
# read_messages
# =============================================================================


class TestReadMessages:
    @pytest.mark.asyncio
    async def test_read_shows_messages(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello bob"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        result = await impls["read_messages"]({"conversation_id": wire_id})
        assert not result.is_error
        assert "Hello bob" in result.text
        assert "alice" in result.text

    @pytest.mark.asyncio
    async def test_read_marks_as_read(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        wires = await ws.list_wires("bob")
        wire_id = wires[0].wire_id

        # Bob has 1 unread
        assert await ws.get_unread_count(wire_id, "bob") == 1

        bob_impls = _setup_pair("bob", ws, store, registry)
        await bob_impls["read_messages"]({"conversation_id": wire_id})

        # Now 0 unread
        assert await ws.get_unread_count(wire_id, "bob") == 0

    @pytest.mark.asyncio
    async def test_read_nonexistent_conversation(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["read_messages"]({"conversation_id": "nonexistent"})
        assert result.is_error
        assert "not found" in result.text.lower()

    @pytest.mark.asyncio
    async def test_read_not_participant(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        charlie_impls = _setup_pair("charlie", ws, store, registry)
        result = await charlie_impls["read_messages"]({"conversation_id": wire_id})
        assert result.is_error
        assert "not a participant" in result.text

    @pytest.mark.asyncio
    async def test_read_header_format(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        bob_impls = _setup_pair("bob", ws, store, registry)
        result = await bob_impls["read_messages"]({"conversation_id": wire_id})
        assert f"Conversation {wire_id}" in result.text
        assert "Participants:" in result.text
        assert "1 unread" in result.text
        assert "---" in result.text

    @pytest.mark.asyncio
    async def test_read_marks_new_messages(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})
        wires = await ws.list_wires("bob")
        wire_id = wires[0].wire_id

        bob_impls = _setup_pair("bob", ws, store, registry)
        result = await bob_impls["read_messages"]({"conversation_id": wire_id})
        assert "[new]" in result.text

    @pytest.mark.asyncio
    async def test_read_with_num_previous(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Message 1"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        # Add more messages from bob
        bob_impls = _setup_pair("bob", ws, store, registry)
        for i in range(2, 6):
            await bob_impls["send_message"]({"conversation_id": wire_id, "body": f"Message {i}"})

        # Alice has 4 unread (messages 2-5). Request 0 previous context.
        result = await impls["read_messages"]({"conversation_id": wire_id, "num_previous": 0})
        assert not result.is_error
        # Should not include Message 1 (it's context, and we asked for 0 previous)
        assert "Message 1" not in result.text
        assert "Message 2" in result.text

    @pytest.mark.asyncio
    async def test_read_overflow_message(self):
        """When more than 30 unread messages, shows overflow notice."""
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "First"})
        wires = await ws.list_wires("alice")
        wire_id = wires[0].wire_id

        bob_impls = _setup_pair("bob", ws, store, registry)
        for i in range(35):
            await bob_impls["send_message"]({"conversation_id": wire_id, "body": f"Msg {i}"})

        # Alice has 35 unread, max_messages=30 so overflow
        result = await impls["read_messages"]({"conversation_id": wire_id})
        assert "unread messages remain" in result.text


# =============================================================================
# conversations_list
# =============================================================================


class TestConversationsList:
    @pytest.mark.asyncio
    async def test_no_conversations(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["conversations_list"]({"unread_only": False})
        assert not result.is_error
        assert "No conversations found" in result.text

    @pytest.mark.asyncio
    async def test_list_shows_conversations(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello bob"})

        result = await impls["conversations_list"]({"unread_only": False})
        assert not result.is_error
        assert "bob" in result.text

    @pytest.mark.asyncio
    async def test_list_unread_only_default(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})

        # Alice sent the message, so her cursor is at the end — 0 unread
        # Default unread_only=True should show nothing for alice
        result = await impls["conversations_list"]({})
        assert "No conversations found" in result.text

    @pytest.mark.asyncio
    async def test_list_unread_only_false_shows_all(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})

        # unread_only=False shows even conversations with 0 unread
        result = await impls["conversations_list"]({"unread_only": False})
        assert not result.is_error
        assert "bob" in result.text

    @pytest.mark.asyncio
    async def test_list_shows_unread_count(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Hello"})

        bob_impls = _setup_pair("bob", ws, store, registry)
        result = await bob_impls["conversations_list"]({})
        assert "1 unread" in result.text

    @pytest.mark.asyncio
    async def test_list_shows_last_message_preview(self):
        ws, registry, store, impls = _setup("alice")
        await impls["send_message"]({"recipients": ["bob"], "body": "Check this out please"})

        result = await impls["conversations_list"]({"unread_only": False})
        assert "Check this out" in result.text

    @pytest.mark.asyncio
    async def test_list_preview_truncation(self):
        ws, registry, store, impls = _setup("alice")
        long_msg = "x" * 200
        await impls["send_message"]({"recipients": ["bob"], "body": long_msg})

        result = await impls["conversations_list"]({"unread_only": False})
        assert "..." in result.text

    @pytest.mark.asyncio
    async def test_list_sorted_by_recency(self):
        ws, registry, store, impls = _setup("bob")
        # Bob creates two conversations
        await impls["send_message"]({"recipients": ["alice"], "body": "Older"})
        await impls["send_message"]({"recipients": ["charlie"], "body": "Newer"})

        result = await impls["conversations_list"]({"unread_only": False})
        # Charlie's conversation should come first (more recent)
        charlie_pos = result.text.find("charlie")
        alice_pos = result.text.find("alice")
        assert charlie_pos < alice_pos

    @pytest.mark.asyncio
    async def test_conversations_only_shows_accessible_wires(self):
        """Alice can only see conversations she participates in — not bob-charlie wires.

        Network: alice <-> bob <-> charlie. Bob creates a wire with charlie.
        Alice's conversations_list should NOT include the bob-charlie wire."""
        ws, registry, store, alice_impls = _setup("alice")
        bob_impls = _setup_pair("bob", ws, store, registry)

        # Bob sends to alice (alice can see this)
        await bob_impls["send_message"]({"recipients": ["alice"], "body": "Hi Alice"})
        # Bob sends to charlie (alice should NOT see this)
        await bob_impls["send_message"]({"recipients": ["charlie"], "body": "Hi Charlie"})

        result = await alice_impls["conversations_list"]({"unread_only": False})
        assert not result.is_error
        # Alice should see the bob-alice conversation but NOT the bob-charlie one
        assert "bob" in result.text.lower()
        assert "Hi Charlie" not in result.text


# =============================================================================
# batch_read_messages
# =============================================================================


class TestBatchReadMessages:
    @pytest.mark.asyncio
    async def test_no_unread(self):
        ws, registry, store, impls = _setup("alice")
        result = await impls["batch_read_messages"]({})
        assert not result.is_error
        assert "No unread messages" in result.text

    @pytest.mark.asyncio
    async def test_reads_multiple_conversations(self):
        ws, registry, store, impls = _setup("bob")
        alice_impls = _setup_pair("alice", ws, store, registry)
        charlie_impls = _setup_pair("charlie", ws, store, registry)

        # Alice and Charlie each send Bob a message
        await alice_impls["send_message"]({"recipients": ["bob"], "body": "From Alice"})
        await charlie_impls["send_message"]({"recipients": ["bob"], "body": "From Charlie"})

        result = await impls["batch_read_messages"]({})
        assert not result.is_error
        assert "From Alice" in result.text
        assert "From Charlie" in result.text

    @pytest.mark.asyncio
    async def test_marks_as_read(self):
        ws, registry, store, impls = _setup("bob")
        alice_impls = _setup_pair("alice", ws, store, registry)
        await alice_impls["send_message"]({"recipients": ["bob"], "body": "Hello"})

        await impls["batch_read_messages"]({})

        # Calling again should show no unread
        result = await impls["batch_read_messages"]({})
        assert "No unread messages" in result.text

    @pytest.mark.asyncio
    async def test_separator_between_conversations(self):
        ws, registry, store, impls = _setup("bob")
        alice_impls = _setup_pair("alice", ws, store, registry)
        charlie_impls = _setup_pair("charlie", ws, store, registry)

        await alice_impls["send_message"]({"recipients": ["bob"], "body": "From Alice"})
        await charlie_impls["send_message"]({"recipients": ["bob"], "body": "From Charlie"})

        result = await impls["batch_read_messages"]({})
        assert "=" * 40 in result.text


# =============================================================================
# Tool notifications
# =============================================================================


class TestEventNotifications:
    @pytest.mark.asyncio
    async def test_events_emitted_when_enabled(self):
        ws = WireStore(framework_logger=_test_logger, wire_notifications=WireNotifications.EVENT)
        emitted = []
        ws.add_event_listener(lambda e: emitted.append(e))
        await ws.create_wire("w1", ["alice", "bob"], "alice", "Hello")
        assert len(emitted) > 0

    @pytest.mark.asyncio
    async def test_events_suppressed_when_disabled(self):
        ws = WireStore(framework_logger=_test_logger, wire_notifications=WireNotifications.TOOL)
        emitted = []
        ws.add_event_listener(lambda e: emitted.append(e))
        await ws.create_wire("w1", ["alice", "bob"], "alice", "Hello")
        assert len(emitted) == 0

    @pytest.mark.asyncio
    async def test_events_suppressed_on_add_message(self):
        ws = WireStore(framework_logger=_test_logger, wire_notifications=WireNotifications.TOOL)
        await ws.create_wire("w1", ["alice", "bob"], "alice", "Hello")
        emitted = []
        ws.add_event_listener(lambda e: emitted.append(e))
        await ws.add_message("w1", "bob", "Reply")
        assert len(emitted) == 0

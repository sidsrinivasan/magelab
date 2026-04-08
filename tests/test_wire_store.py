"""Tests for WireStore."""

import sqlite3

import pytest


from magelab.events import WireMessageEvent
from magelab.org_config import WireNotifications
from magelab.state.database import Database
from magelab.state.wire_schemas import Wire, WireMessage, WireSnapshot
from magelab.state.wire_store import WireStore


# =============================================================================
# Wire model tests
# =============================================================================


class TestWireModel:
    def test_wire_requires_message(self):
        with pytest.raises(ValueError, match="must have at least one message"):
            Wire(wire_id="w1", read_cursors={"a": 0, "b": 0}, messages=[])

    def test_wire_created_by(self):
        wire = Wire(
            wire_id="w1",
            read_cursors={"a": 1, "b": 0},
            messages=[WireMessage(sender="a", body="hello")],
        )
        assert wire.created_by == "a"

    def test_wire_participants_sorted(self):
        wire = Wire(
            wire_id="w1",
            read_cursors={"charlie": 0, "alice": 1, "bob": 0},
            messages=[WireMessage(sender="alice", body="hi")],
        )
        assert wire.participants == frozenset({"alice", "bob", "charlie"})

    def test_wire_unread_count(self):
        wire = Wire(
            wire_id="w1",
            read_cursors={"a": 1, "b": 0},
            messages=[WireMessage(sender="a", body="hi")],
        )
        assert wire.unread_count("a") == 0
        assert wire.unread_count("b") == 1

    def test_wire_mark_read(self):
        wire = Wire(
            wire_id="w1",
            read_cursors={"a": 1, "b": 0},
            messages=[WireMessage(sender="a", body="hi")],
        )
        wire.mark_read("b", 1)
        assert wire.unread_count("b") == 0

    def test_wire_mark_read_only_forward(self):
        wire = Wire(
            wire_id="w1",
            read_cursors={"a": 1, "b": 1},
            messages=[WireMessage(sender="a", body="hi")],
        )
        wire.mark_read("b", 0)  # try to go backward
        assert wire.read_cursors["b"] == 1  # stays at 1

    def test_wire_task_id(self):
        wire = Wire(
            wire_id="w1",
            read_cursors={"a": 1, "b": 0},
            messages=[WireMessage(sender="a", body="hi")],
            task_id="t1",
        )
        assert wire.task_id == "t1"


# =============================================================================
# WireStore tests
# =============================================================================


@pytest.fixture
def store(logger) -> WireStore:
    return WireStore(framework_logger=logger)


class TestCreateWire:
    @pytest.mark.asyncio
    async def test_create_basic(self, store: WireStore):
        wire = await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        assert wire.wire_id == "w1"
        assert wire.participants == frozenset({"alice", "bob"})
        assert len(wire.messages) == 1
        assert wire.messages[0].sender == "alice"
        assert wire.messages[0].body == "hello"
        assert wire.created_by == "alice"

    @pytest.mark.asyncio
    async def test_create_sorts_participants(self, store: WireStore):
        wire = await store.create_wire("w1", ["charlie", "alice", "bob"], "charlie", "hi")
        assert wire.participants == frozenset({"alice", "bob", "charlie"})

    @pytest.mark.asyncio
    async def test_create_duplicate_id_raises(self, store: WireStore):
        await store.create_wire("w1", ["a", "b"], "a", "hi")
        with pytest.raises(ValueError, match="already exists"):
            await store.create_wire("w1", ["a", "b"], "a", "hi again")

    @pytest.mark.asyncio
    async def test_create_duplicate_participants_raises(self, store: WireStore):
        await store.create_wire("w1", ["a", "b"], "a", "hi")
        with pytest.raises(ValueError, match="Wire already exists for participants"):
            await store.create_wire("w2", ["b", "a"], "b", "hello")

    @pytest.mark.asyncio
    async def test_create_with_task_id(self, store: WireStore):
        wire = await store.create_wire("w1", ["a", "b"], "a", "hi", task_id="t1")
        assert wire.task_id == "t1"

    @pytest.mark.asyncio
    async def test_create_emits_events_to_others(self, store: WireStore):
        events: list = []
        store.add_event_listener(lambda e: events.append(e))

        await store.create_wire("w1", ["alice", "bob", "charlie"], "alice", "hello")

        assert len(events) == 2
        targets = {e.target_id for e in events}
        assert targets == {"bob", "charlie"}
        for e in events:
            assert isinstance(e, WireMessageEvent)
            assert e.wire_id == "w1"
            assert e.source_id == "alice"
            assert e.message_cursor == 1

    @pytest.mark.asyncio
    async def test_create_no_event_to_sender(self, store: WireStore):
        events: list = []
        store.add_event_listener(lambda e: events.append(e))

        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")

        assert len(events) == 1
        assert events[0].target_id == "bob"

    @pytest.mark.asyncio
    async def test_create_sets_unread_for_others(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        assert await store.get_unread_count("w1", "bob") == 1
        assert await store.get_unread_count("w1", "alice") == 0


class TestAddMessage:
    @pytest.mark.asyncio
    async def test_add_message(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        wire = await store.add_message("w1", "bob", "hi back")
        assert len(wire.messages) == 2
        assert wire.messages[1].sender == "bob"
        assert wire.messages[1].body == "hi back"

    @pytest.mark.asyncio
    async def test_add_to_nonexistent_raises(self, store: WireStore):
        with pytest.raises(ValueError, match="not found"):
            await store.add_message("nope", "alice", "hello")

    @pytest.mark.asyncio
    async def test_add_from_non_participant_raises(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        with pytest.raises(ValueError, match="not a participant"):
            await store.add_message("w1", "charlie", "intruder")

    @pytest.mark.asyncio
    async def test_add_increments_unread(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        assert await store.get_unread_count("w1", "bob") == 1

        await store.add_message("w1", "alice", "another")
        assert await store.get_unread_count("w1", "bob") == 2
        assert await store.get_unread_count("w1", "alice") == 0

    @pytest.mark.asyncio
    async def test_add_emits_events(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob", "charlie"], "alice", "hello")

        events: list = []
        store.add_event_listener(lambda e: events.append(e))

        await store.add_message("w1", "bob", "reply")

        assert len(events) == 2
        targets = {e.target_id for e in events}
        assert targets == {"alice", "charlie"}
        for e in events:
            assert e.source_id == "bob"
            assert e.message_cursor == 2  # 2nd message in wire


class TestGetWire:
    @pytest.mark.asyncio
    async def test_get_returns_copy(self, store: WireStore):
        await store.create_wire("w1", ["a", "b"], "a", "hi")
        wire = await store.get_wire("w1")
        assert wire is not None
        assert wire.wire_id == "w1"

        # Mutating the copy shouldn't affect the store
        wire.messages.append(WireMessage(sender="a", body="sneaky"))
        original = await store.get_wire("w1")
        assert len(original.messages) == 1

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, store: WireStore):
        assert await store.get_wire("nope") is None


class TestFindByParticipants:
    @pytest.mark.asyncio
    async def test_find_existing(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        found = await store.find_wire_by_participants({"alice", "bob"})
        assert found == "w1"

    @pytest.mark.asyncio
    async def test_find_no_match(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        found = await store.find_wire_by_participants({"alice", "charlie"})
        assert found is None

    @pytest.mark.asyncio
    async def test_find_stable_after_messages(self, store: WireStore):
        """Index doesn't change when messages are added."""
        await store.create_wire("w1", ["alice", "bob"], "alice", "first")
        await store.add_message("w1", "bob", "reply")
        found = await store.find_wire_by_participants({"alice", "bob"})
        assert found == "w1"


class TestMarkRead:
    @pytest.mark.asyncio
    async def test_mark_read_clears_unread(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        await store.add_message("w1", "alice", "another")
        assert await store.get_unread_count("w1", "bob") == 2

        await store.mark_read("w1", "bob", up_to=2)
        assert await store.get_unread_count("w1", "bob") == 0

    @pytest.mark.asyncio
    async def test_mark_read_partial(self, store: WireStore):
        """Reading only some messages leaves the rest unread."""
        await store.create_wire("w1", ["alice", "bob"], "alice", "msg1")
        await store.add_message("w1", "alice", "msg2")
        await store.add_message("w1", "alice", "msg3")
        assert await store.get_unread_count("w1", "bob") == 3

        await store.mark_read("w1", "bob", up_to=2)
        assert await store.get_unread_count("w1", "bob") == 1

    @pytest.mark.asyncio
    async def test_mark_read_unknown_wire_raises(self, store: WireStore):
        with pytest.raises(ValueError, match="not found"):
            await store.mark_read("nope", "alice", up_to=1)

    @pytest.mark.asyncio
    async def test_mark_read_non_participant_raises(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        with pytest.raises(ValueError, match="not a participant"):
            await store.mark_read("w1", "charlie", up_to=1)


class TestListWires:
    @pytest.mark.asyncio
    async def test_list_basic(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        await store.create_wire("w2", ["alice", "charlie"], "alice", "hey")

        summaries = await store.list_wires("alice")
        assert len(summaries) == 2
        assert all(isinstance(s, WireSnapshot) for s in summaries)

    @pytest.mark.asyncio
    async def test_list_sorted_by_recency(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "first")
        await store.create_wire("w2", ["alice", "charlie"], "alice", "second")
        await store.add_message("w1", "bob", "reply")  # w1 now most recent

        summaries = await store.list_wires("alice")
        assert summaries[0].wire_id == "w1"
        assert summaries[1].wire_id == "w2"

    @pytest.mark.asyncio
    async def test_list_unread_only(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "bob", "hello")
        await store.create_wire("w2", ["alice", "charlie"], "charlie", "hey")
        await store.mark_read("w1", "alice", up_to=1)

        summaries = await store.list_wires("alice", unread_only=True)
        assert len(summaries) == 1
        assert summaries[0].wire_id == "w2"

    @pytest.mark.asyncio
    async def test_list_excludes_non_participant(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello")
        summaries = await store.list_wires("charlie")
        assert len(summaries) == 0

    @pytest.mark.asyncio
    async def test_summary_fields(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hello world")
        summaries = await store.list_wires("bob")
        assert len(summaries) == 1
        s = summaries[0]
        assert s.wire_id == "w1"
        assert s.participants == frozenset({"alice", "bob"})
        assert s.message_count == 1
        assert s.unread_count == 1
        assert s.last_message_sender == "alice"
        assert s.last_message_preview == "hello world"


class TestFormatConversation:
    def test_separator_between_messages(self):
        """Messages should be separated by --- but not before the first one."""
        wire = Wire(
            wire_id="w1",
            read_cursors={"alice": 0, "bob": 3},
            messages=[
                WireMessage(sender="bob", body="msg1"),
                WireMessage(sender="bob", body="msg2"),
                WireMessage(sender="bob", body="msg3"),
            ],
        )
        text, _ = wire.format_conversation("alice", num_previous=0)
        # Split after header (header ends with "---\n")
        header_end = text.index("---\n") + 4
        body = text[header_end:]
        lines = [line for line in body.split("\n") if line]
        # Pattern: msg, ---, msg, ---, msg
        assert len(lines) == 5
        assert lines[0].startswith("[0]")
        assert lines[1] == "---"
        assert lines[2].startswith("[1]")
        assert lines[3] == "---"
        assert lines[4].startswith("[2]")

    def test_no_separator_for_single_message(self):
        wire = Wire(
            wire_id="w1",
            read_cursors={"alice": 0, "bob": 1},
            messages=[WireMessage(sender="bob", body="only one")],
        )
        text, _ = wire.format_conversation("alice")
        assert "---" not in text.split("\n", 3)[-1]  # after header


class TestUnreadSummary:
    @pytest.mark.asyncio
    async def test_no_unread(self, store: WireStore):
        assert store.unread_summary("alice") is None

    @pytest.mark.asyncio
    async def test_single_wire_single_message(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        summary = store.unread_summary("bob")
        assert summary is not None
        lines = summary.split("\n")
        assert lines[0] == "NOTIFICATION: You have 1 unread message across 1 conversation."
        assert "  - w1 (1 unread) with: alice" in lines
        assert lines[-1] == "You may use read_messages or batch_read_messages to read them."

    @pytest.mark.asyncio
    async def test_single_wire_multiple_messages(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        await store.add_message("w1", "alice", "again")
        summary = store.unread_summary("bob")
        assert summary is not None
        lines = summary.split("\n")
        assert lines[0] == "NOTIFICATION: You have 2 unread messages across 1 conversation."
        assert "  - w1 (2 unread) with: alice" in lines

    @pytest.mark.asyncio
    async def test_multiple_wires(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        await store.create_wire("w2", ["bob", "charlie"], "charlie", "hey")
        summary = store.unread_summary("bob")
        assert summary is not None
        lines = summary.split("\n")
        assert lines[0] == "NOTIFICATION: You have 2 unread messages across 2 conversations."
        assert "  - w1 (1 unread) with: alice" in lines
        assert "  - w2 (1 unread) with: charlie" in lines

    @pytest.mark.asyncio
    async def test_after_mark_read(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        await store.mark_read("w1", "bob", up_to=1)
        assert store.unread_summary("bob") is None

    @pytest.mark.asyncio
    async def test_limit_truncates(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        await store.create_wire("w2", ["bob", "charlie"], "charlie", "hey")
        await store.create_wire("w3", ["alice", "bob", "charlie"], "alice", "all")
        summary = store.unread_summary("bob", limit=2)
        assert summary is not None
        assert "... and 1 more conversation" in summary


class TestStaleness:
    @pytest.mark.asyncio
    async def test_stale_when_wire_gone(self, store: WireStore):
        event = WireMessageEvent(target_id="bob", wire_id="nope", source_id="alice", message_cursor=1)
        assert await store.is_event_stale(event, "bob") is True

    @pytest.mark.asyncio
    async def test_stale_when_not_participant(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        event = WireMessageEvent(target_id="charlie", wire_id="w1", source_id="alice", message_cursor=1)
        assert await store.is_event_stale(event, "charlie") is True

    @pytest.mark.asyncio
    async def test_stale_when_message_read(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        await store.mark_read("w1", "bob", up_to=1)
        event = WireMessageEvent(target_id="bob", wire_id="w1", source_id="alice", message_cursor=1)
        assert await store.is_event_stale(event, "bob") is True

    @pytest.mark.asyncio
    async def test_not_stale_when_message_unread(self, store: WireStore):
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        event = WireMessageEvent(target_id="bob", wire_id="w1", source_id="alice", message_cursor=1)
        assert await store.is_event_stale(event, "bob") is False

    @pytest.mark.asyncio
    async def test_reading_makes_event_stale(self, store: WireStore):
        """Staleness dedup: reading via tool hook makes queued event stale."""
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        event = WireMessageEvent(target_id="bob", wire_id="w1", source_id="alice", message_cursor=1)

        assert await store.is_event_stale(event, "bob") is False

        await store.mark_read("w1", "bob", up_to=1)

        assert await store.is_event_stale(event, "bob") is True

    @pytest.mark.asyncio
    async def test_later_event_survives_earlier_read(self, store: WireStore):
        """Reading msg 1 makes event1 stale but event2 (for msg 2) stays fresh."""
        await store.create_wire("w1", ["alice", "bob"], "alice", "hi")
        await store.add_message("w1", "alice", "follow up")

        event1 = WireMessageEvent(target_id="bob", wire_id="w1", source_id="alice", message_cursor=1)
        event2 = WireMessageEvent(target_id="bob", wire_id="w1", source_id="alice", message_cursor=2)

        await store.mark_read("w1", "bob", up_to=1)

        assert await store.is_event_stale(event1, "bob") is True
        assert await store.is_event_stale(event2, "bob") is False


# =============================================================================
# DB persistence roundtrip
# =============================================================================


class TestDBPersistence:
    @pytest.fixture
    def db_store(self, tmp_path, logger):
        db = Database(str(tmp_path / "test.db"))
        store = WireStore(framework_logger=logger, db=db)
        yield store, db, logger
        db.close()

    @pytest.mark.asyncio
    async def test_wire_roundtrip(self, db_store):
        store, db, _logger = db_store
        await store.create_wire(wire_id="w1", participants=["alice", "bob"], sender="alice", body="hello")

        # Load into a fresh store from the same DB
        store2 = WireStore(framework_logger=_logger, db=db)
        store2.load_from_db()
        assert len(store2._wires) == 1
        wire = store2._wires["w1"]
        assert len(wire.messages) == 1
        assert wire.messages[0].sender == "alice"
        assert wire.messages[0].body == "hello"
        assert wire.read_cursors["alice"] == 1
        assert wire.read_cursors["bob"] == 0

    @pytest.mark.asyncio
    async def test_add_message_persisted(self, db_store):
        store, db, _logger = db_store
        await store.create_wire(wire_id="w1", participants=["alice", "bob"], sender="alice", body="hello")
        await store.add_message("w1", sender="bob", body="hi back")

        store2 = WireStore(framework_logger=_logger, db=db)
        store2.load_from_db()
        wire = store2._wires["w1"]
        assert len(wire.messages) == 2
        assert wire.read_cursors["bob"] == 2

    @pytest.mark.asyncio
    async def test_mark_read_persisted(self, db_store):
        store, db, _logger = db_store
        await store.create_wire(wire_id="w1", participants=["alice", "bob"], sender="alice", body="hello")
        await store.mark_read("w1", "bob", up_to=1)

        store2 = WireStore(framework_logger=_logger, db=db)
        store2.load_from_db()
        assert store2._wires["w1"].read_cursors["bob"] == 1

    @pytest.mark.asyncio
    async def test_wire_fk_constraint(self, db_store):
        """Inserting a message for a non-existent wire should fail."""
        store, db, _logger = db_store
        with pytest.raises(sqlite3.IntegrityError):
            store._db_insert_message("nonexistent", "alice", "hello", "2024-01-01T00:00:00")


# =============================================================================
# WireNotifications settings
# =============================================================================


class TestWireNotifications:
    """Test that WireNotifications settings correctly gate event emission."""

    @pytest.mark.asyncio
    async def test_all_emits_events(self, logger):
        """WireNotifications.ALL emits WireMessageEvents to event listeners."""
        ws = WireStore(framework_logger=logger, wire_notifications=WireNotifications.ALL)
        events = []
        ws.add_event_listener(lambda e: events.append(e))
        await ws.create_wire("w1", ["alice", "bob"], "alice", "hello")
        assert len(events) == 1
        assert isinstance(events[0], WireMessageEvent)

    @pytest.mark.asyncio
    async def test_event_emits_events(self, logger):
        """WireNotifications.EVENT emits WireMessageEvents to event listeners."""
        ws = WireStore(framework_logger=logger, wire_notifications=WireNotifications.EVENT)
        events = []
        ws.add_event_listener(lambda e: events.append(e))
        await ws.create_wire("w1", ["alice", "bob"], "alice", "hello")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_tool_suppresses_events(self, logger):
        """WireNotifications.TOOL suppresses WireMessageEvents from event listeners."""
        ws = WireStore(framework_logger=logger, wire_notifications=WireNotifications.TOOL)
        events = []
        ws.add_event_listener(lambda e: events.append(e))
        await ws.create_wire("w1", ["alice", "bob"], "alice", "hello")
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_none_suppresses_events(self, logger):
        """WireNotifications.NONE suppresses WireMessageEvents from event listeners."""
        ws = WireStore(framework_logger=logger, wire_notifications=WireNotifications.NONE)
        events = []
        ws.add_event_listener(lambda e: events.append(e))
        await ws.create_wire("w1", ["alice", "bob"], "alice", "hello")
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_message_listeners_always_fire(self, logger):
        """Message listeners fire regardless of wire_notifications setting."""
        for mode in WireNotifications:
            ws = WireStore(framework_logger=logger, wire_notifications=mode)
            messages = []
            ws.add_message_listener(lambda wid, parts, sender, body: messages.append(body))
            await ws.create_wire(f"w-{mode.value}", ["alice", "bob"], "alice", "hello")
            assert len(messages) == 1, f"Message listener should fire for {mode.value}"

    @pytest.mark.asyncio
    async def test_add_message_also_gated(self, logger):
        """Event emission on add_message follows the same gating as create_wire."""
        ws = WireStore(framework_logger=logger, wire_notifications=WireNotifications.TOOL)
        events = []
        ws.add_event_listener(lambda e: events.append(e))
        await ws.create_wire("w1", ["alice", "bob"], "alice", "hello")
        await ws.add_message("w1", "bob", "reply")
        assert len(events) == 0

        ws2 = WireStore(framework_logger=logger, wire_notifications=WireNotifications.ALL)
        events2 = []
        ws2.add_event_listener(lambda e: events2.append(e))
        await ws2.create_wire("w1", ["alice", "bob"], "alice", "hello")
        await ws2.add_message("w1", "bob", "reply")
        # create_wire emits for bob (not alice), add_message emits for alice (not bob)
        assert len(events2) == 2

"""Archive/settings persistence with temporary files only; no model or device operations."""
import copy
from contextlib import closing
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from backend.chat_archive import ChatArchive, ConversationListChanged
from backend.chat_requests import ChatRequests, ReceiptError
from backend.mobile_preferences import MobilePreferences
from backend.safety import SafetyManager


PERSONA = "测试角色:角色扮演:zh"


def safety_fixture():
    return SafetyManager({
        "safety": {"channels": {"A": {"max_strength": 150}, "B": {"max_strength": 120}},
                   "max_temp_duration_s": 10, "max_strength_step": 8, "overheat_reduce_to": 10},
        "playback": {"max_duration_s": 10, "min_duration_s": 0.1},
        "presets": {"测试波形": {}}, "app": {"dry_run": True},
    })


class ArchivePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "chat.sqlite3"
        self.archive = ChatArchive(self.path)
        self.conversation = self.archive.active_id

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def append(self, index, *, conversation=None, result=None, history=None, persona=PERSONA):
        return self.archive.append_turn(conversation or self.conversation, f"test-turn-{index}", f"用户消息 {index}",
            result if result is not None else {"line": f"角色回复 {index}", "executed": [], "dropped": []},
            history=history if history is not None else [{"role": "user", "content": f"用户消息 {index}"}], persona=persona)

    def reopen(self):
        self.archive.close()
        self.archive = ChatArchive(self.path)

    def test_restart_preserves_active_conversation_messages_and_stable_ids(self):
        first = self.append(1)
        second_id = self.archive.create()["conversation_id"]
        second = self.append(2, conversation=second_id)
        self.archive.select(self.conversation)
        self.reopen()
        self.assertEqual(self.archive.active_id, self.conversation)
        self.assertEqual(self.archive.messages(self.conversation)["messages"], first)
        self.assertEqual(self.archive.messages(second_id)["messages"], second)
        self.assertEqual(len({item["id"] for item in first + second}), 4)
        self.assertEqual(self.archive.title(self.conversation), "用户消息 1")

    def test_message_pages_over_100_have_no_loss_duplication_or_cross_chat_cursor(self):
        expected = []
        for index in range(133):
            expected.extend(self.append(index))
        other = self.archive.create()["conversation_id"]
        foreign = self.append("other", conversation=other)
        combined, before = [], None
        while True:
            page = self.archive.messages(self.conversation, before=before, limit=100)
            self.assertLessEqual(len(page["messages"]), 100)
            combined = page["messages"] + combined
            if not page["has_more"]:
                break
            self.assertTrue(page["messages"])
            before = page["messages"][0]["id"]
        self.assertEqual(combined, expected)
        self.assertEqual(len({item["id"] for item in combined}), 266)
        with self.assertRaises(KeyError):
            self.archive.messages(self.conversation, before=foreign[0]["id"])

    def test_conversation_pages_over_100_with_equal_timestamps_are_complete(self):
        with patch("backend.chat_archive.time.time_ns", return_value=1_000_000_000):
            for _ in range(124):
                self.archive.create()
        expected = {row[0] for row in self.archive.db.execute("SELECT id FROM conversations")}
        ids, before = [], None
        while True:
            page = self.archive.list(before=before, limit=37)
            ids.extend(item["id"] for item in page["conversations"])
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                break
            self.assertTrue(page["next_cursor"])
            before = page["next_cursor"]
        self.assertEqual(set(ids), expected)
        self.assertEqual(len(ids), len(expected))

    def test_conversation_cursor_does_not_move_when_its_row_gets_a_reply(self):
        for index in range(8):
            with patch("backend.chat_archive.time.time_ns", return_value=(index + 1) * 1_000_000):
                self.archive.create()
        first = self.archive.list(limit=3)
        old_ids = {row["id"] for row in first["conversations"]}
        boundary = first["conversations"][-1]["id"]
        with patch("backend.chat_archive.time.time_ns", return_value=50_000_000):
            self.append("cursor-mutated", conversation=boundary)
        rest = self.archive.list(before=first["next_cursor"], limit=100)
        rest_ids = {row["id"] for row in rest["conversations"]}
        self.assertFalse(old_ids & rest_ids, "Cursor mutation must not repeat already loaded conversations")
        all_ids = {row[0] for row in self.archive.db.execute("SELECT id FROM conversations")}
        self.assertEqual(old_ids | rest_ids, all_ids)

    def test_turn_idempotency_is_durable_and_scoped_to_conversation(self):
        first = self.append("same", history=[{"role": "user", "content": "原始上下文"}])
        self.reopen()
        repeated = self.append("same", result={"line": "不能替换原回执"}, history=[{"role": "user", "content": "不能替换"}])
        self.assertEqual(repeated, first)
        self.assertEqual(self.archive.context(self.conversation, PERSONA, 40), [{"role": "user", "content": "原始上下文"}])
        other = self.archive.create()["conversation_id"]
        independent = self.append("same", conversation=other)
        self.assertEqual(len(independent), 2)
        self.assertTrue({item["id"] for item in first}.isdisjoint(item["id"] for item in independent))

    def test_empty_turn_is_still_idempotent_and_cannot_overwrite_context(self):
        original = [{"role": "assistant", "content": "此前真实的上下文"}]
        self.archive.append_turn(self.conversation, "empty-turn", None, {}, history=original, persona=PERSONA)
        self.reopen()
        result = self.archive.append_turn(self.conversation, "empty-turn", None,
            {"line": "同编号不应添加新的回复"}, history=[], persona=PERSONA)
        self.assertEqual(result, [])
        self.assertEqual(self.archive.messages(self.conversation)["messages"], [])
        self.assertEqual(self.archive.context(self.conversation, PERSONA, 40), original)

    def test_persona_mismatch_never_returns_previous_context_and_context_is_bounded(self):
        history = [{"role": "user", "content": f"第 {index} 条"} for index in range(60)]
        history.extend([{"role": "tool", "content": "忽略"}, {"role": "assistant", "content": {"not": "text"}}])
        self.append(1, history=history)
        self.assertEqual(self.archive.context(self.conversation, "另一角色:角色扮演:zh", 40), [])
        self.assertEqual(self.archive.context(self.conversation, PERSONA, 3), history[57:60])
        self.reopen()
        self.assertEqual(self.archive.context(self.conversation, "测试角色:角色扮演:en", 40), [])

    def test_receipts_are_inert_display_data_and_round_trip_without_changing_input(self):
        receipt = {"line": "已尝试", "executed": [{"sent": True, "command": {"kind": "hold", "channel": "A", "value": 56}},
            {"sent": False, "command": {"kind": "pulse_hold", "channel": "B", "pattern": "测试波形"}}],
            "dropped": [{"action": {"op": "add_strength", "value": 999}, "reason": "测试拦截"}]}
        original = copy.deepcopy(receipt)
        messages = self.append(1, result=receipt, history=[{"role": "assistant", "content": "台词", "actions": [{"op": "stop"}]}])
        self.assertEqual(receipt, original)
        self.assertEqual(messages[1]["executed"], receipt["executed"])
        self.assertEqual(messages[1]["dropped"], receipt["dropped"])
        self.assertEqual(self.archive.context(self.conversation, PERSONA, 40), [{"role": "assistant", "content": "台词"}])
        self.reopen()
        self.assertEqual(self.archive.messages(self.conversation)["messages"], messages)

    def test_context_serialization_failure_rolls_back_messages_title_and_context(self):
        before = self.append(1)
        history = self.archive.context(self.conversation, PERSONA, 40)
        with self.assertRaises(ValueError):
            self.append(2, history=[{"role": "user", "content": float("nan")}])
        self.assertEqual(self.archive.messages(self.conversation)["messages"], before)
        self.assertEqual(self.archive.title(self.conversation), "用户消息 1")
        self.assertEqual(self.archive.context(self.conversation, PERSONA, 40), history)
        self.reopen()
        self.assertEqual(self.archive.messages(self.conversation)["messages"], before)
        self.assertEqual(len(self.append(2)), 2, "A rolled-back turn remains appendable")

    def test_database_insert_failure_cannot_leave_half_a_turn(self):
        with self.archive.db:
            self.archive.db.execute("CREATE TRIGGER reject_ai BEFORE INSERT ON messages WHEN NEW.role='ai' BEGIN SELECT RAISE(ABORT,'fixture disk error'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.append(1)
        self.assertEqual(self.archive.messages(self.conversation)["messages"], [])
        self.assertEqual(self.archive.title(self.conversation), "新聊天")
        self.assertEqual(self.archive.context(self.conversation, PERSONA, 40), [])

    def test_active_selection_write_failure_preserves_persisted_and_live_selection(self):
        other = self.archive.create()["conversation_id"]
        self.archive.select(self.conversation)
        with self.archive.db:
            self.archive.db.execute("CREATE TRIGGER reject_active BEFORE INSERT ON archive_meta WHEN NEW.key='active' BEGIN SELECT RAISE(ABORT,'fixture disk error'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.archive.select(other)
        self.assertEqual(self.archive.active_id, self.conversation)
        self.reopen()
        self.assertEqual(self.archive.active_id, self.conversation)

    def test_old_database_migrates_without_losing_active_messages_or_context(self):
        old_path = self.path.with_name("old.sqlite3")
        old_id = "a" * 32
        message = {"id": "b" * 32, "conversation_id": old_id, "role": "user", "text": "已保存的旧消息"}
        context = {"persona": PERSONA, "history": [{"role": "user", "content": message["text"]}]}
        with closing(sqlite3.connect(old_path)) as db, db:
            db.executescript("""
                CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,context TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL,turn_id TEXT NOT NULL,role TEXT NOT NULL,body TEXT NOT NULL,
                    created_at INTEGER NOT NULL,FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    UNIQUE(conversation_id,turn_id,role));
                CREATE TABLE archive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            db.execute("INSERT INTO conversations VALUES(?,?,?,?,?)", (old_id, "原聊天", 1, 2, json.dumps(context)))
            db.execute("INSERT INTO messages(id,conversation_id,turn_id,role,body,created_at) VALUES(?,?,?,?,?,?)",
                       (message["id"], old_id, "old-turn", "user", json.dumps(message), 2))
            db.execute("INSERT INTO archive_meta VALUES('active',?)", (old_id,))
        restored = ChatArchive(old_path)
        try:
            self.assertEqual(restored.active_id, old_id)
            self.assertEqual(restored.messages(old_id)["messages"], [message])
            self.assertEqual(restored.context(old_id, PERSONA, 40), context["history"])
            self.assertIs(restored.list()["conversations"][0]["pinned"], False)
            self.assertEqual(restored.append_turn(old_id, "old-turn", "不能重复", {"line": "不能添加"}, history=[], persona=PERSONA), [message])
            restored.set_pinned(old_id, True)
        finally:
            restored.close()
        restored = ChatArchive(old_path)
        try:
            self.assertIs(restored.list()["conversations"][0]["pinned"], True)
            self.assertEqual(restored.messages(old_id)["messages"], [message])
        finally:
            restored.close()

    def test_pin_persists_and_reorders_without_editing_chat_or_active_selection(self):
        original = self.append(1)
        with patch("backend.chat_archive.time.time_ns", return_value=9_000_000_000_000_000_000):
            other = self.archive.create()["conversation_id"]
        before = next(row for row in self.archive.list()["conversations"] if row["id"] == self.conversation)
        self.archive.set_pinned(self.conversation, True)
        self.reopen()
        rows = self.archive.list()["conversations"]
        self.assertEqual(rows[0], {**before, "pinned": True})
        self.assertEqual(self.archive.active_id, other)
        self.assertEqual(self.archive.messages(self.conversation)["messages"], original)
        self.archive.set_pinned(self.conversation, False)
        self.assertEqual(self.archive.list()["conversations"][0]["id"], other)
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.archive.set_pinned(self.conversation, value)

    def test_pinned_and_regular_chat_pagination_is_complete_and_pin_changes_invalidate_cursor(self):
        with patch("backend.chat_archive.time.time_ns", return_value=1_000_000):
            for index in range(25):
                identity = self.archive.create()["conversation_id"]
                if index % 2 == 0:
                    self.archive.set_pinned(identity, True)
        ids, pin_states, cursor = [], [], None
        first = self.archive.list(limit=7)
        while True:
            page = self.archive.list(before=cursor, limit=7)
            ids.extend(row["id"] for row in page["conversations"])
            pin_states.extend(row["pinned"] for row in page["conversations"])
            if not page["has_more"]:
                break
            cursor = page["next_before"]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 26)
        self.assertEqual(pin_states, sorted(pin_states, reverse=True))
        boundary = first["conversations"][-1]
        self.archive.set_pinned(boundary["id"], boundary["pinned"])
        self.assertTrue(self.archive.list(before=first["next_cursor"])["conversations"], "Idempotent pin does not expire a cursor")
        self.archive.set_pinned(boundary["id"], not boundary["pinned"])
        with self.assertRaises(ConversationListChanged):
            self.archive.list(before=first["next_cursor"])

    def test_delete_inactive_keeps_active_context_and_delete_active_creates_empty_chat(self):
        self.append("old")
        self.archive.set_pinned(self.conversation, True)
        other = self.archive.create()["conversation_id"]
        active_messages = self.append("active", conversation=other)
        inactive = self.archive.delete(self.conversation)
        self.assertEqual(inactive, {"deleted_id": self.conversation, "active_id": other, "active_changed": False, "conversation": None})
        self.assertEqual(self.archive.messages(other)["messages"], active_messages)
        current = self.archive.delete(other)
        fresh = current["active_id"]
        self.assertTrue(current["active_changed"])
        self.assertNotIn(fresh, (other, self.conversation))
        self.assertEqual(current["conversation"]["messages"], [])
        self.assertEqual(self.archive.context(fresh, PERSONA, 40), [])
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM archived_turns").fetchone()[0], 0)
        self.reopen()
        self.assertEqual(self.archive.active_id, fresh)
        self.assertEqual([row["id"] for row in self.archive.list()["conversations"]], [fresh])
        with self.assertRaises(KeyError):
            self.archive.delete(other)

    def test_delete_transaction_failure_restores_messages_markers_meta_and_selection(self):
        expected = self.append("retain")
        context = self.archive.context(self.conversation, PERSONA, 40)
        triggers = (
            "CREATE TRIGGER reject_mutation BEFORE DELETE ON conversations BEGIN SELECT RAISE(ABORT,'fixture delete failure'); END",
            "CREATE TRIGGER reject_mutation BEFORE INSERT ON archive_meta WHEN NEW.key='active' BEGIN SELECT RAISE(ABORT,'fixture replacement failure'); END",
        )
        for trigger in triggers:
            with self.subTest(trigger=trigger):
                with self.archive.db:
                    self.archive.db.execute(trigger)
                with self.assertRaises(sqlite3.DatabaseError):
                    self.archive.delete(self.conversation)
                self.assertEqual(self.archive.active_id, self.conversation)
                self.assertEqual(self.archive.messages(self.conversation)["messages"], expected)
                self.assertEqual(self.archive.context(self.conversation, PERSONA, 40), context)
                self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM archived_turns").fetchone()[0], 1)
                self.reopen()
                self.assertEqual(self.archive.active_id, self.conversation)
                with self.archive.db:
                    self.archive.db.execute("DROP TRIGGER reject_mutation")


class PreferencePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config" / "preferences.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_restores_caps_all_six_levels_ui_and_model_judgment_only(self):
        preferences = MobilePreferences(self.path)
        for level in SafetyManager.INTENSITY_LEVELS:
            preferences.save({"user_caps": {"A": 48, "B": 61}, "intensity_level": level,
                "intensity_device_link": True, "model_judgment": True,
                "device_preferences": {"focus_channel": "B", "manual_link": True,
                                       "last_presets": {"A": "测试波形", "B": "不存在"}},
                "current": {"A": 145, "B": 119}, "autopilot": True, "estop_active": False,
                "pulse_until": {"A": 123456789}, "microphone": True})
            loaded = MobilePreferences(self.path)
            safety = safety_fixture()
            safety.estop_active = True
            loaded.apply(safety)
            self.assertEqual(safety.user_caps, {"A": 48, "B": 61})
            self.assertEqual(safety.intensity_level, level)
            self.assertEqual(safety.scale, {"A": SafetyManager.INTENSITY_LEVELS[level], "B": SafetyManager.INTENSITY_LEVELS[level]})
            self.assertEqual(safety.current, {"A": 0, "B": 0})
            self.assertEqual(safety.requested, {"A": None, "B": None})
            self.assertEqual(safety.pulse_until, {"A": 0.0, "B": 0.0})
            self.assertTrue(safety.estop_active)
            self.assertFalse(hasattr(safety, "autopilot"))
            self.assertFalse(hasattr(safety, "microphone"))
            self.assertIs(loaded.values["model_judgment"], True)
            self.assertEqual(loaded.device(safety.presets), {"focus_channel": "B", "manual_link": True,
                                                          "last_presets": {"A": "测试波形", "B": None}})

    def test_caps_are_validated_and_live_state_is_not_overwritten(self):
        preferences = MobilePreferences(self.path)
        preferences.save({"user_caps": {"A": 9999, "B": -99}, "intensity_level": "炼狱", "intensity_device_link": False})
        safety = safety_fixture()
        safety.current = {"A": 5, "B": 7}
        preferences.apply(safety)
        self.assertEqual(safety.user_caps, {"A": 150, "B": 1})
        self.assertEqual(safety.current, {"A": 5, "B": 7})
        self.assertEqual(safety.scale, {"A": 1.0, "B": 1.0})
        preferences.save({"user_caps": {"A": True, "B": "90"}, "device_preferences": {"focus_channel": [], "manual_link": "true", "last_presets": []}})
        clean = safety_fixture()
        preferences.apply(clean)
        self.assertEqual(clean.user_caps, {"A": 100, "B": 100})
        self.assertEqual(preferences.device(clean.presets), {"focus_channel": "A", "manual_link": False, "last_presets": {"A": None, "B": None}})

    def test_replace_failure_preserves_file_and_memory_and_removes_partial(self):
        preferences = MobilePreferences(self.path)
        preferences.save({"user_caps": {"A": 35}, "model_judgment": False})
        previous_bytes, previous_values = self.path.read_bytes(), copy.deepcopy(preferences.values)
        with patch("backend.mobile_preferences.os.replace", side_effect=OSError("fixture full disk")):
            with self.assertRaises(OSError):
                preferences.save({"user_caps": {"A": 90}, "model_judgment": True})
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.assertEqual(preferences.values, previous_values)
        self.assertEqual(MobilePreferences(self.path).values, previous_values)
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_serialization_failure_preserves_file_and_memory(self):
        preferences = MobilePreferences(self.path)
        preferences.save({"intensity_level": "低"})
        with self.assertRaises(ValueError):
            preferences.save({"fixture": float("nan")})
        self.assertEqual(preferences.values, {"intensity_level": "低"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), preferences.values)

    def test_corrupt_settings_fail_without_overwriting_original(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{invalid fixture", encoding="utf-8")
        with self.assertRaises(ValueError):
            MobilePreferences(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{invalid fixture")


class ArchiveReceiptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.archive = ChatArchive(self.path)
        self.receipts = ChatRequests(max_results=1)
        self.calls = 0

    async def asyncTearDown(self):
        await self.receipts.close()
        self.archive.close()
        self.temp.cleanup()

    def request_id(self):
        return f"{self.receipts.session_id}:{uuid.uuid4()}"

    async def complete(self):
        async def wait():
            while self.receipts.has_pending:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 1)

    def operation(self, conversation, request):
        async def run():
            self.calls += 1  # Counts fake operations; there is deliberately no device/LLM object.
            result = {"line": "假回复", "executed": [{"sent": True, "command": {"kind": "hold", "channel": "A", "value": 20}}]}
            result["messages"] = self.archive.append_turn(conversation, request, "假请求", result, history=[], persona=PERSONA)
            return result
        return run

    async def test_duplicate_and_expired_receipts_do_not_repeat_operation(self):
        conversation = self.archive.active_id
        request = self.request_id()
        payload = {"message": "假请求", "conversation_id": conversation}
        self.receipts.submit(request, payload, self.operation(conversation, request))
        self.receipts.submit(request, payload, self.operation(conversation, request))
        await self.complete()
        first = self.receipts.get(request)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.receipts.submit(request, payload, self.operation(conversation, request)), first)
        other = self.archive.create()["conversation_id"]
        with self.assertRaises(ReceiptError) as changed:
            self.receipts.submit(request, {**payload, "conversation_id": other}, self.operation(other, request))
        self.assertEqual(changed.exception.status, 409)
        second = self.request_id()
        self.receipts.submit(second, {**payload, "conversation_id": other}, self.operation(other, second))
        await self.complete()
        with self.assertRaises(ReceiptError) as expired:
            self.receipts.submit(request, payload, self.operation(conversation, request))
        self.assertEqual(expired.exception.status, 410)
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(self.archive.messages(conversation)["messages"]), 2)
        self.assertEqual(len(self.archive.messages(other)["messages"]), 2)

    async def test_restart_reads_messages_but_rejects_old_operation_session(self):
        conversation = self.archive.active_id
        request = self.request_id()
        payload = {"message": "假请求", "conversation_id": conversation}
        self.receipts.submit(request, payload, self.operation(conversation, request))
        await self.complete()
        expected = self.archive.messages(conversation)["messages"]
        await self.receipts.close()
        self.archive.close()
        self.archive = ChatArchive(self.path)
        self.receipts = ChatRequests()
        self.assertEqual(self.archive.messages(conversation)["messages"], expected)
        with self.assertRaises(ReceiptError) as restarted:
            self.receipts.submit(request, payload, self.operation(conversation, request))
        self.assertEqual(restarted.exception.status, 409)
        await asyncio.sleep(0)
        self.assertEqual(self.calls, 1)
        self.assertFalse(self.receipts.has_pending)


if __name__ == "__main__":
    unittest.main()

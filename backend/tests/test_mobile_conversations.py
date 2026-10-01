"""Temporary app-private history/settings only; no real model, relay, microphone or device."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
import yaml

from backend import config, main
from backend.tests.test_role_features import isolated_logging


class MobileConversationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(isolated_logging())
        self.root = Path(folder)
        cfgdir = self.root / "config"
        cfgdir.mkdir()
        (cfgdir / "config.yaml").write_text(yaml.safe_dump({"app": {"dry_run": True, "check_update": False},
            "llm": {"api_key": "", "base_url": "http://invalid.local/v1"}}), encoding="utf-8")
        (self.root / "basic.md").write_text("You are a neutral test character.", encoding="utf-8")
        (cfgdir / "character.yaml").write_text(yaml.safe_dump({"player_nick": "玩家", "role": "base", "profile": "default",
            "roles": {"base": {"name": "Base", "profiles": {"default": {"prompt_file": "basic.md"}}}}}), encoding="utf-8")
        for module, name, value in ((config, "PROJECT_ROOT", self.root), (config, "CONFIG_DIR", cfgdir),
                (config, "CHARACTER_RUNTIME_FILE", cfgdir / "character_runtime.yaml"),
                (config, "DEVICE_CHANNELS_FILE", cfgdir / "device_channels.yaml"), (main, "PROJECT_ROOT", self.root)):
            self.stack.enter_context(patch.object(module, name, value))
        def mobile_config():
            cfg = config.load_config()
            cfg["_mobile"] = True
            return cfg
        self.stack.enter_context(patch.object(main, "load_config", side_effect=mobile_config))
        self.token = "x" * 64
        self.headers = {"Origin": "http://127.0.0.1:12345", "Cookie": f"coyote_session={self.token}"}
        self.apps = []
        self.addCleanup(self.close_apps)
        self.app, self.runtime, self.client = self.make_app()

    def make_app(self):
        app = main.make_app(mobile_token=self.token, mobile_port=12345)
        runtime = app.state.runtime
        runtime.llm.chat = AsyncMock(return_value=("你好，继续对话。", []))
        client = TestClient(app, base_url="http://127.0.0.1:12345", headers=self.headers)
        self.apps.append((runtime, client))
        return app, runtime, client

    def close_apps(self):
        for runtime, client in self.apps:
            client.close()
            if runtime.archive:
                runtime.archive.close()
                runtime.archive = None
            asyncio.run(runtime.llm.client.aclose())

    def add_other_role(self):
        """Install only a temporary role, including one unavailable profile."""
        path = self.root / "config" / "character.yaml"
        character = yaml.safe_load(path.read_text(encoding="utf-8"))
        character["roles"]["other"] = {"name": "Other", "profiles": {
            "default": {"prompt_file": "other.md"},
            "missing": {"prompt_file": "not-installed.md"},
        }}
        (self.root / "other.md").write_text("A different neutral test character.", encoding="utf-8")
        path.write_text(yaml.safe_dump(character), encoding="utf-8")
        config.reload_character(self.runtime.cfg)

    def test_role_transition_stops_output_but_invalid_or_unchanged_selection_does_not(self):
        self.add_other_role()
        original_id = self.runtime.archive.active_id
        self.runtime.loop.history = [{"role": "user", "content": "previous character context"}]
        self.runtime.loop.notes = ["previous character observation"]
        self.runtime.loop.autopilot = True  # Idle cadence; never start a real automatic worker.
        self.runtime.safety.resume()
        self.runtime.safety.current = {"A": 20, "B": 15}
        with patch.object(self.runtime.loop, "estop", wraps=self.runtime.loop.estop) as stop:
            for payload in ({"role": "unknown", "profile": "default"},
                            {"role": "other", "profile": "unknown"},
                            {"role": "other", "profile": "missing"}):
                with self.subTest(payload=payload):
                    self.assertEqual(self.client.post("/api/character/profile", json=payload).status_code, 400)
                    self.assertEqual(self.runtime.archive.active_id, original_id)
                    self.assertTrue(self.runtime.loop.autopilot)
                    self.assertFalse(self.runtime.safety.estop_active)
            unchanged = self.client.post("/api/character/profile", json={"role": "base", "profile": "default"})
            self.assertEqual(unchanged.status_code, 200)
            stop.assert_not_awaited()
            selected = self.client.post("/api/character/profile", json={"role": "other", "profile": "default"})
            self.assertEqual(selected.status_code, 200)
            stop.assert_awaited_once()
        self.assertEqual(self.runtime.cfg["character"]["role"], "other")
        self.assertNotEqual(self.runtime.archive.active_id, original_id)
        self.assertEqual(self.runtime.loop.history, [])
        self.assertEqual(self.runtime.loop.notes, [])
        self.assertFalse(self.runtime.loop.autopilot)
        self.assertTrue(self.runtime.safety.estop_active)
        self.assertEqual(self.runtime.safety.current, {"A": 0, "B": 0})

    def test_pending_conversation_transition_rejects_output_but_allows_stop_controls(self):
        async def scenario(select_existing):
            entered, release = asyncio.Event(), asyncio.Event()
            stop_original = self.runtime.loop.estop
            first_stop = True
            async def gated_stop():
                nonlocal first_stop
                is_first = first_stop
                first_stop = False
                result = await stop_original()
                if is_first:
                    entered.set()
                    await release.wait()
                return result
            self.runtime.loop.autopilot = True
            self.runtime.safety.resume()
            path = (f"/api/conversations/{self.runtime.archive.active_id}/select"
                    if select_existing else "/api/conversations")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                    base_url="http://127.0.0.1:12345", headers=self.headers) as client:
                with patch.object(self.runtime.loop, "estop", side_effect=gated_stop), \
                        patch.object(self.runtime.loop, "execute_actions", wraps=self.runtime.loop.execute_actions) as execute:
                    transition = asyncio.create_task(client.post(path, json={}))
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        self.assertTrue(self.runtime._conversation_changing)
                        current_id = self.runtime.archive.active_id
                        deletion = await client.delete(f"/api/conversations/{current_id}")
                        self.assertEqual(deletion.status_code, 409)
                        self.assertEqual(self.runtime.archive.active_id, current_id)
                        for endpoint, payload in (("/api/resume", {}),
                                ("/api/autopilot", {"enabled": True}),
                                ("/api/manual", {"op": "hold_strength", "channel": "A", "value": 25})):
                            response = await client.post(endpoint, json=payload)
                            self.assertEqual(response.status_code, 409, (endpoint, response.text))
                        execute.assert_not_awaited()
                        off = await client.post("/api/autopilot", json={"enabled": False})
                        self.assertEqual(off.status_code, 200)
                        self.assertFalse(off.json()["autopilot"])
                        for operation in ("clear", "stop"):
                            response = await client.post("/api/manual", json={"op": operation, "channel": "A"})
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(response.json()["dropped"], [])
                            self.assertEqual(len(response.json()["executed"]), 1)
                        urgent = await client.post("/api/estop", json={})
                        self.assertEqual(urgent.status_code, 200)
                        self.assertTrue(urgent.json()["estop"])
                        # Stop controls bypass ordinary action gating while output is latched off.
                        execute.assert_not_awaited()
                    finally:
                        release.set()
                        response = await asyncio.wait_for(transition, 2)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(self.runtime.safety.estop_active)
                    self.assertFalse(self.runtime.loop.autopilot)
                    self.assertFalse(self.runtime._conversation_changing)
        for select_existing in (False, True):
            with self.subTest(select_existing=select_existing):
                asyncio.run(scenario(select_existing))

    def test_language_cannot_change_while_reply_is_pending(self):
        (self.root / "basic-EN.md").write_text("An English test prompt.", encoding="utf-8")
        config.reload_character(self.runtime.cfg)
        self.assertTrue(self.runtime.cfg["character"]["en_available"])
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def model(*args, **kwargs):
                entered.set()
                await release.wait()
                return "原语言回复", []
            self.runtime.llm.chat = model
            cid = self.runtime.archive.active_id
            persona = self.runtime.persona_key()
            request_id = self.runtime.chat_requests.session_id + ":" + str(uuid.uuid4())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                    base_url="http://127.0.0.1:12345", headers=self.headers) as client:
                pending = await client.post("/api/chat", json={"message": "保持当前语言", "request_id": request_id})
                self.assertEqual(pending.status_code, 202)
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    with patch.object(self.runtime.loop, "estop", wraps=self.runtime.loop.estop) as stop:
                        response = await client.post("/api/character/lang", json={"lang": "en"})
                        self.assertEqual(response.status_code, 409)
                        stop.assert_not_awaited()
                    self.assertEqual(self.runtime.cfg["character"]["lang"], "zh")
                    self.assertEqual(self.runtime.archive.active_id, cid)
                finally:
                    release.set()
                for _ in range(100):
                    receipt = (await client.get("/api/chat/result/" + request_id)).json()
                    if receipt["status"] == "completed":
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(receipt["result"]["line"], "原语言回复")
                self.assertEqual(self.runtime.archive.persona(cid), persona)
        asyncio.run(scenario())

    def test_successful_language_change_stops_and_starts_clean_conversation(self):
        (self.root / "basic-EN.md").write_text("An English test prompt.", encoding="utf-8")
        self.runtime.loop.history = [{"role": "user", "content": "previous language context"}]
        self.runtime.loop.notes = ["previous language observation"]
        self.runtime.loop.autopilot = True
        self.runtime.safety.resume()
        cid = self.runtime.archive.active_id
        with patch.object(self.runtime.loop, "estop", wraps=self.runtime.loop.estop) as stop:
            invalid = self.client.post("/api/character/lang", json={"lang": "invalid"})
            self.assertEqual(invalid.status_code, 400)
            stop.assert_not_awaited()
            changed = self.client.post("/api/character/lang", json={"lang": "en"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["lang"], "en")
            stop.assert_awaited_once()
        self.assertNotEqual(self.runtime.archive.active_id, cid)
        self.assertEqual(self.runtime.loop.history, [])
        self.assertEqual(self.runtime.loop.notes, [])
        self.assertFalse(self.runtime.loop.autopilot)
        self.assertTrue(self.runtime.safety.estop_active)

    def test_archive_select_failure_keeps_persona_and_clears_untrusted_live_context(self):
        self.add_other_role()
        archive = self.runtime.archive
        original_id = archive.active_id
        target_id = archive.create()["conversation_id"]
        target_history = [{"role": "user", "content": "target role private history"}]
        archive.append_turn(target_id, "target-turn", target_history[0]["content"],
                            {"line": "target role reply"}, history=target_history,
                            persona=json.dumps(["other", "default", "zh"], ensure_ascii=False))
        archive.select(original_id)
        self.runtime.loop.history = [{"role": "user", "content": "old live context"}]
        self.runtime.loop.notes = ["old live observation"]
        self.runtime.loop.autopilot = True
        self.runtime.safety.resume()
        with patch.object(archive, "select", side_effect=sqlite3.OperationalError("fixture storage unavailable")), \
                patch.object(main, "save_character_runtime", wraps=main.save_character_runtime) as save, \
                patch.object(self.runtime, "broadcast", wraps=self.runtime.broadcast) as broadcast:
            response = self.client.post(f"/api/conversations/{target_id}/select", json={})
            self.assertEqual(response.status_code, 503, response.text)
            save.assert_not_called()
            broadcast.assert_awaited()
        self.assertEqual(archive.active_id, original_id)
        self.assertEqual(self.runtime.cfg["character"]["role"], "base")
        self.assertEqual(self.runtime.loop.history, [])
        self.assertEqual(self.runtime.loop.notes, [])
        self.assertFalse(self.runtime.loop.autopilot)
        self.assertTrue(self.runtime.safety.estop_active)
        self.assertFalse(self.runtime._conversation_changing)
        self.assertEqual(archive.context(target_id, json.dumps(["other", "default", "zh"], ensure_ascii=False), 10), target_history)
        result = self.client.post("/api/chat", json={"message": "storage failure follow-up"})
        self.assertEqual(result.status_code, 200)
        sent_history = self.runtime.llm.chat.call_args.args[1]
        self.assertFalse(any(item["content"] in ("old live context", "target role private history") for item in sent_history))

    def test_chat_ids_history_new_select_and_restart_without_replaying_actions(self):
        initial = self.client.get("/api/state").json()
        conversation = initial["conversation_id"]
        self.assertFalse(initial["model_judgment"])
        result = self.client.post("/api/chat", json={"message": "第一条消息", "conversation_id": conversation}).json()
        self.assertEqual(result["conversation_id"], conversation)
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "ai"])
        stored = self.client.get(f"/api/conversations/{conversation}/messages").json()
        self.assertEqual(result["messages"], stored["messages"])
        created = self.client.post("/api/conversations", json={}).json()
        self.assertNotEqual(conversation, created["conversation_id"])
        self.assertEqual(created["messages"], [])
        with patch.object(self.runtime.loop, "execute_actions", new_callable=AsyncMock) as execute:
            selected = self.client.post(f"/api/conversations/{conversation}/select", json={}).json()
            self.assertEqual(selected["messages"], result["messages"])
            execute.assert_not_called()
        self.assertEqual(self.runtime.loop.history[-2]["content"], "第一条消息")
        self.runtime.archive.close()
        self.runtime.archive = None
        _, restored, client = self.make_app()
        state = client.get("/api/state").json()
        self.assertEqual(state["conversation_id"], conversation)
        self.assertEqual(client.get(f"/api/conversations/{conversation}/messages").json()["messages"], stored["messages"])
        self.assertEqual(restored.loop.history[-2]["content"], "第一条消息")
        self.assertTrue(state["estop"])
        self.assertEqual(state["current"], {"A": 0, "B": 0})
        self.assertFalse(state["autopilot"])

    def test_pending_request_blocks_switch_and_wrong_conversation_never_runs(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def model(*args, **kwargs):
                entered.set()
                await release.wait()
                return "回复一次", []
            self.runtime.llm.chat = model
            cid = self.runtime.archive.active_id
            request_id = self.runtime.chat_requests.session_id + ":" + str(uuid.uuid4())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:12345", headers=self.headers) as client:
                payload = {"message": "稍等", "request_id": request_id, "conversation_id": cid}
                self.assertEqual((await client.post("/api/chat", json=payload)).status_code, 202)
                await asyncio.wait_for(entered.wait(), 2)
                with patch.object(self.runtime.loop, "estop", new_callable=AsyncMock) as stop:
                    self.assertEqual((await client.post("/api/conversations", json={})).status_code, 409)
                    stop.assert_not_called()
                self.assertEqual((await client.post("/api/chat", json={"message": "旧页面", "conversation_id": "a" * 32})).status_code, 409)
                release.set()
                for _ in range(100):
                    receipt = (await client.get("/api/chat/result/" + request_id)).json()
                    if receipt["status"] == "completed":
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(receipt["status"], "completed")
                repeated = (await client.post("/api/chat", json=payload)).json()
                self.assertEqual(receipt["result"]["messages"], repeated["result"]["messages"])
                self.assertEqual(len(self.runtime.archive.messages(cid)["messages"]), 2)
        asyncio.run(scenario())

    def test_model_judgment_requires_server_wait_and_persists_with_device_preferences(self):
        endpoint = "/api/character/model-judgment"
        self.assertEqual(self.client.post(endpoint, json={"enabled": True}).status_code, 409)
        self.assertEqual(self.client.post(endpoint, json={"enabled": "true"}).status_code, 400)
        prepared = self.client.post(endpoint + "/prepare", json={}).json()
        payload = {"enabled": True, "confirmation_token": prepared["confirmation_token"]}
        self.assertEqual(self.client.post(endpoint, json=payload).status_code, 409)
        later = time.monotonic() + 6
        with patch.object(main.time, "monotonic", return_value=later):
            self.assertEqual(self.client.post(endpoint, json=payload).status_code, 200)
            self.assertEqual(self.client.post(endpoint, json=payload).status_code, 409)
        self.client.post("/api/device/channels/cap", json={"channel": "A", "value": 56}).raise_for_status()
        self.client.post("/api/intensity", json={"level": "极高", "sync_to_device": True}).raise_for_status()
        self.client.post("/api/device/preferences", json={"focus_channel": "B", "manual_link": True}).raise_for_status()
        self.client.post("/api/character/nick", json={"nick": "旅人"}).raise_for_status()
        self.runtime.archive.close()
        self.runtime.archive = None
        _, restored, client = self.make_app()
        state = client.get("/api/state").json()
        self.assertTrue(state["model_judgment"])
        self.assertEqual(state["user_caps"]["A"], 56)
        self.assertEqual(state["intensity_level"], "极高")
        self.assertEqual(state["device_preferences"]["focus_channel"], "B")
        self.assertTrue(state["device_preferences"]["manual_link"])
        self.assertEqual(state["config_info"]["player_nick"], "旅人")
        self.assertEqual(state["current"], {"A": 0, "B": 0})
        self.assertEqual(client.post(endpoint, json={"enabled": False}).status_code, 200)
        self.assertFalse(restored.cfg["interaction"]["model_judgment"])

    def test_archive_write_failure_returns_reply_without_reexecuting_and_keeps_database(self):
        with patch.object(self.runtime.archive, "append_turn", side_effect=OSError("private path")):
            result = self.client.post("/api/chat", json={"message": "只说一次"}).json()
        self.assertEqual(result["line"], "你好，继续对话。")
        self.assertIn("persistence_error", result)
        self.runtime.llm.chat.assert_awaited_once()
        self.assertTrue((self.root / "data" / "chat_history.sqlite3").is_file())

    def test_pin_api_validation_persistence_and_list_cursor_invalidation(self):
        archive = self.runtime.archive
        old = archive.active_id
        other = archive.create()["conversation_id"]
        page = self.client.get("/api/conversations?limit=1").json()
        for value in (1, "true", None):
            self.assertEqual(self.client.post(f"/api/conversations/{old}/pin", json={"pinned": value}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/conversations/{'f' * 32}/pin", json={"pinned": True}).status_code, 404)
        with patch.object(self.runtime.loop, "estop", new_callable=AsyncMock) as stop:
            result = self.client.post(f"/api/conversations/{old}/pin", json={"pinned": True})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {"conversation_id": old, "pinned": True, "active_id": other})
            stop.assert_not_awaited()
        expired = self.client.get("/api/conversations", params={"before": page["next_cursor"]})
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(expired.json()["code"], "conversation_list_changed")
        first = self.client.get("/api/conversations").json()["conversations"][0]
        self.assertEqual(first["id"], old)
        self.assertIs(first["pinned"], True)
        with archive.db:
            archive.db.execute("CREATE TRIGGER reject_pin_revision BEFORE INSERT ON archive_meta WHEN NEW.key='list_revision' BEGIN SELECT RAISE(ABORT,'fixture storage error'); END")
        failed = self.client.post(f"/api/conversations/{old}/pin", json={"pinned": False})
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(self.client.get("/api/conversations").json()["conversations"][0], first)
        archive.close()
        self.runtime.archive = None
        _, _, client = self.make_app()
        self.assertEqual(client.get("/api/conversations").json()["conversations"][0], first)

    def test_delete_active_stops_and_returns_new_empty_chat_without_replaying_actions(self):
        archive = self.runtime.archive
        current = archive.active_id
        other = archive.create()["conversation_id"]
        archive.select(current)
        self.client.post("/api/chat", json={"message": "即将删除的消息"}).raise_for_status()
        self.runtime.loop.notes = ["旧观察"]
        self.runtime.loop.autopilot = True
        self.runtime.safety.resume()
        self.runtime.safety.current = {"A": 20, "B": 10}
        with patch.object(self.runtime.loop, "estop", wraps=self.runtime.loop.estop) as stop, \
                patch.object(self.runtime.loop, "execute_actions", new_callable=AsyncMock) as execute:
            response = self.client.delete(f"/api/conversations/{current}")
            self.assertEqual(response.status_code, 200, response.text)
            stop.assert_awaited_once()
            execute.assert_not_awaited()
        result = response.json()
        self.assertTrue(result["active_changed"])
        self.assertEqual(result["deleted_id"], current)
        self.assertNotIn(result["active_id"], (current, other))
        self.assertEqual(result["conversation"]["conversation_id"], result["active_id"])
        self.assertEqual(result["conversation"]["messages"], [])
        self.assertEqual(self.runtime.loop.history, [])
        self.assertEqual(self.runtime.loop.notes, [])
        self.assertFalse(self.runtime.loop.autopilot)
        self.assertTrue(self.runtime.safety.estop_active)
        self.assertEqual(self.runtime.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.client.get(f"/api/conversations/{current}/messages").status_code, 404)
        self.assertEqual(self.client.get(f"/api/conversations/{other}/messages").status_code, 200)
        archive.close()
        self.runtime.archive = None
        _, restored, client = self.make_app()
        self.assertEqual(client.get("/api/state").json()["conversation_id"], result["active_id"])
        self.assertEqual(restored.loop.history, [])

    def test_pending_reply_allows_inactive_delete_and_pin_but_rejects_active_delete(self):
        async def scenario():
            archive = self.runtime.archive
            old = archive.active_id
            current = archive.create()["conversation_id"]
            entered, release = asyncio.Event(), asyncio.Event()
            async def model(*args, **kwargs):
                entered.set()
                await release.wait()
                return "仅一次回复", []
            self.runtime.llm.chat = AsyncMock(side_effect=model)
            request_id = self.runtime.chat_requests.session_id + ":" + str(uuid.uuid4())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:12345", headers=self.headers) as client:
                pending = await client.post("/api/chat", json={"message": "正在处理", "request_id": request_id, "conversation_id": current})
                self.assertEqual(pending.status_code, 202)
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    with patch.object(self.runtime.loop, "estop", new_callable=AsyncMock) as stop:
                        response = await client.delete(f"/api/conversations/{current}")
                        self.assertEqual(response.status_code, 409)
                        pinned = await client.post(f"/api/conversations/{current}/pin", json={"pinned": True})
                        self.assertEqual(pinned.status_code, 200)
                        deleted = await client.delete(f"/api/conversations/{old}")
                        self.assertEqual(deleted.status_code, 200)
                        self.assertEqual(deleted.json(), {"deleted_id": old, "active_id": current, "active_changed": False, "conversation": None})
                        stop.assert_not_awaited()
                    self.assertEqual(archive.active_id, current)
                finally:
                    release.set()
                for _ in range(100):
                    receipt = (await client.get("/api/chat/result/" + request_id)).json()
                    if receipt["status"] == "completed":
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(len(archive.messages(current)["messages"]), 2)
                self.runtime.llm.chat.assert_awaited_once()
        asyncio.run(scenario())

    def test_delete_active_stop_failure_preserves_chat_and_selection(self):
        archive = self.runtime.archive
        current = archive.active_id
        messages = archive.append_turn(current, "keep", "保留消息", {"line": "保留回复"}, history=[], persona=self.runtime.persona_key())
        async def unconfirmed_stop():
            self.runtime.safety.estop()
            return {"sent": False, "status": "unconfirmed"}
        # Only the stop result is simulated. No relay or real command is used.
        with patch.object(self.runtime.backend, "ready", return_value=True), \
                patch.object(self.runtime.safety, "dry_run", False), \
                patch.object(self.runtime.loop, "estop", new_callable=AsyncMock, side_effect=unconfirmed_stop) as stop:
            response = self.client.delete(f"/api/conversations/{current}")
            self.assertEqual(response.status_code, 503)
            stop.assert_awaited_once()
        self.assertEqual(archive.active_id, current)
        self.assertEqual(archive.messages(current)["messages"], messages)
        self.assertTrue(self.runtime.safety.estop_active)
        self.assertFalse(self.runtime._conversation_changing)

    def test_delete_storage_failure_rolls_back_selection_messages_and_live_context(self):
        archive = self.runtime.archive
        current = archive.active_id
        self.client.post("/api/chat", json={"message": "不能丢失"}).raise_for_status()
        messages = archive.messages(current)["messages"]
        history = list(self.runtime.loop.history)
        self.runtime.loop.notes = ["保留上下文"]
        self.runtime.loop.autopilot = True
        self.runtime.safety.resume()
        with archive.db:
            archive.db.execute("CREATE TRIGGER reject_replacement BEFORE INSERT ON archive_meta WHEN NEW.key='active' BEGIN SELECT RAISE(ABORT,'fixture storage error'); END")
        response = self.client.delete(f"/api/conversations/{current}")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(archive.active_id, current)
        self.assertEqual(archive.messages(current)["messages"], messages)
        self.assertEqual(self.runtime.loop.history, history)
        self.assertEqual(self.runtime.loop.notes, ["保留上下文"])
        self.assertTrue(self.runtime.safety.estop_active)
        self.assertFalse(self.runtime.loop.autopilot)
        self.assertFalse(self.runtime._conversation_changing)
        self.assertEqual(len(archive.list()["conversations"]), 1)


if __name__ == "__main__":
    unittest.main()

"""Role/channel editing uses isolated files and fake model calls; no real device or network."""
import asyncio
import base64
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import AsyncMock, patch
import zlib

import httpx
import yaml
from fastapi.testclient import TestClient

from backend import config
from backend.chat_archive import ChatArchive
from backend.llm import build_system_prompt
from backend.role_edits import avatar_bytes, load_role_edits
from backend.role_library import load_custom_roles, save_user_role
from backend.tests.test_role_management import isolated_roles_app, SOURCE


def png_avatar(width=1, height=1, color=b"\xff\x00\x00"):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"\0" + color)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(data).decode()


@contextmanager
def edited_app():
    with isolated_roles_app() as (root, app, first, second):
        state = app.state.runtime
        client = TestClient(app)
        try:
            yield root, app, state, client, first, second
        finally:
            client.close()
            if state.archive:
                state.archive.close()
                state.archive = None
            asyncio.run(state.llm.client.aclose())


class RoleEditingTests(unittest.TestCase):
    def test_jpeg_webp_and_png_signatures_reject_mismatch_truncation_and_oversized_dimensions(self):
        # Tiny, generated RGB fixtures. No Pillow dependency is needed by Android.
        jpeg = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDx2iiiu04z/9k="
        webp = "UklGRjAAAABXRUJQVlA4ICQAAABQAQCdASoBAAEAAUAmJQBOgC6gAP77LkvF3YjjJ4dVU9ffoAA="
        self.assertEqual(avatar_bytes("data:image/jpeg;base64," + jpeg)[0], "image/jpeg")
        self.assertEqual(avatar_bytes("data:image/webp;base64," + webp)[0], "image/webp")
        self.assertEqual(avatar_bytes(png_avatar())[0], "image/png")
        corrupted_png = bytearray(avatar_bytes(png_avatar())[1])
        corrupted_png[-1] ^= 1
        wrong_webp = bytearray(base64.b64decode(webp))
        wrong_webp[26:28] = (2048).to_bytes(2, "little")
        for value in ("data:image/png;base64," + jpeg, "data:image/jpeg;base64," + jpeg[:-4],
                      "data:image/webp;base64," + base64.b64encode(wrong_webp).decode(),
                      "data:image/png;base64," + base64.b64encode(corrupted_png).decode()):
            with self.subTest(value=value[:25]), self.assertRaises(ValueError):
                avatar_bytes(value)

    def test_builtin_edit_preserves_prompt_id_chat_context_and_output_permissions(self):
        with edited_app() as (root, app, state, client, first, second):
            state.archive = ChatArchive(root / "data" / "history.sqlite3")
            cid = state.archive.active_id
            history = [{"role": "user", "content": "已存在的上下文"}]
            state.loop.history = list(history)
            state.cfg.setdefault("interaction", {})["model_judgment"] = True
            state.safety.estop()
            original = (root / "base.md").read_bytes()
            details = client.get("/api/character/edit", params={"role": "assistant"}).json()
            self.assertEqual(details["kind"], "builtin")
            with patch.object(state.loop, "estop", new_callable=AsyncMock) as stop, \
                    patch.object(state.loop, "execute_actions", new_callable=AsyncMock) as execute:
                response = client.put("/api/character/edit", json={"role": "assistant", "name": "自定义助手",
                    "personality": "冷静，表达简洁", "background": "喜欢安静的图书室", "voiceId": "melo-zh"})
                self.assertEqual(response.status_code, 200, response.text)
                stop.assert_not_awaited()
                execute.assert_not_awaited()
            self.assertEqual(state.cfg["character"]["role"], "assistant")
            self.assertEqual(state.cfg["character"]["name"], "自定义助手")
            self.assertIn("A neutral assistant.", state.cfg["character"]["prompt"])
            self.assertIn("冷静，表达简洁", state.cfg["character"]["prompt"])
            self.assertEqual((root / "base.md").read_bytes(), original)
            self.assertEqual(state.loop.history, history)
            self.assertEqual(state.archive.active_id, cid)
            self.assertTrue(state.safety.estop_active)
            self.assertTrue(state.cfg["interaction"]["model_judgment"])
            restored = config.load_config()["character"]
            self.assertEqual(restored["name"], "自定义助手")
            self.assertEqual(next(r for r in restored["roles"] if r["name"] == "assistant")["voiceId"], "melo-zh")

    def test_legacy_manual_json_prefills_and_edit_keeps_other_fields_and_original_file(self):
        with edited_app() as (root, app, state, client, first, second):
            role = save_user_role(root, "旧旅人", "耐心，独立", "来自山间")
            registry = root / "config" / "custom_roles.yaml"
            raw = yaml.safe_load(registry.read_text(encoding="utf-8"))
            raw["roles"][role].pop("personality")
            raw["roles"][role].pop("background")
            registry.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            config.reload_character(state.cfg)
            original = (root / "content" / "custom-roles" / f"{role}.md").read_bytes()
            details = client.get("/api/character/edit", params={"role": role}).json()
            self.assertEqual((details["kind"], details["personality"], details["background"]), ("manual", "耐心，独立", "来自山间"))
            result = client.put("/api/character/edit", json={"role": role, "name": "新旅人", "personality": "更加果断"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["character"]["background"], "来自山间")
            self.assertEqual((root / "content" / "custom-roles" / f"{role}.md").read_bytes(), original)
            self.assertEqual(state.cfg["character"]["role"], "assistant", "Editing another role never selects it")
            client.post("/api/character/profile", json={"role": role, "profile": "角色扮演"}).raise_for_status()
            self.assertIn("更加果断", state.cfg["character"]["prompt"])
            self.assertIn("来自山间", state.cfg["character"]["prompt"])

    def test_legacy_search_reference_and_note_survive_edit(self):
        with edited_app() as (root, app, state, client, first, second):
            registry = root / "config" / "custom_roles.yaml"
            raw = yaml.safe_load(registry.read_text(encoding="utf-8"))
            raw["roles"][first].pop("creation_type")
            raw["roles"][first].pop("note")
            registry.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            old = (root / "content" / "custom-roles" / f"{first}.md").read_bytes()
            details = client.get("/api/character/edit", params={"role": first}).json()
            self.assertEqual(details["kind"], "search")
            self.assertEqual(details["note"], "")
            response = client.put("/api/character/edit", json={"role": first, "name": "搜索角色重命名", "note": "更偏爱简短回答"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["character"]["sources"][0]["url"], SOURCE["url"])
            self.assertEqual((root / "content" / "custom-roles" / f"{first}.md").read_bytes(), old)
            self.assertEqual(load_custom_roles(root)[first]["sources"][0]["url"], SOURCE["url"])

    def test_created_avatar_replace_omit_delete_and_never_broadcast_or_prompt_image_data(self):
        with edited_app() as (root, app, state, client, first, second):
            avatar = png_avatar()
            created = client.post("/api/character/custom", json={"name": "有头像角色", "personality": "平静", "avatar_data": avatar})
            self.assertEqual(created.status_code, 200, created.text)
            role = created.json()["role"]
            details = client.get("/api/character/edit", params={"role": role}).json()
            url = details["avatar_url"]
            self.assertTrue(url.startswith("/api/character/avatar?"))
            image = client.get(url)
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.headers["content-type"], "image/png")
            self.assertEqual(image.content, avatar_bytes(avatar)[1])
            self.assertEqual(image.headers["x-content-type-options"], "nosniff")
            body = client.get("/api/state").json()
            self.assertNotIn("data:image", json.dumps(body))
            self.assertNotIn(avatar, state.cfg["character"]["prompt"])
            changed = client.put("/api/character/edit", json={"role": role, "name": "只改名"}).json()
            self.assertEqual(changed["character"]["avatar_url"], url)
            replacement = client.put("/api/character/edit", json={"role": role, "avatar_data": png_avatar(color=b"\x00\x00\xff")}).json()
            self.assertNotEqual(replacement["character"]["avatar_url"], url)
            removed = client.put("/api/character/edit", json={"role": role, "avatar_data": None})
            self.assertIsNone(removed.json()["character"]["avatar_url"])
            self.assertEqual(client.get(url).status_code, 404)
            config.reload_character(state.cfg)
            self.assertIsNone(next(item for item in state.cfg["character"]["roles"] if item["name"] == role)["avatar_url"])

    def test_avatar_and_identity_validation_happens_before_persisting(self):
        with edited_app() as (root, app, state, client, first, second):
            invalid = ("https://example.invalid/a.jpg", "file:///private.jpg", "data:image/svg+xml;base64,PHN2Zz4=",
                       "data:image/jpeg;base64," + base64.b64encode(b"not an image").decode(), png_avatar(width=1025),
                       png_avatar()[:-4], "data:image/png;base64," + "A" * 700_000)
            for avatar in invalid:
                with self.subTest(avatar=avatar[:30]):
                    response = client.put("/api/character/edit", json={"role": "assistant", "name": "不能保存", "avatar_data": avatar})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertFalse((root / "config" / "role_edits.yaml").exists())
                    self.assertEqual(state.cfg["character"]["name"], "情景助手")
            for payload in ({"role": "assistant", "name": ""}, {"role": "assistant", "name": "a" * 61},
                            {"role": "assistant", "personality": "a" * 3001}, {"role": "assistant", "voiceId": "remote"},
                            {"role": "assistant", "model_judgment": True}, {"role": "assistant", "prompt_file": "../../secret"}):
                self.assertEqual(client.put("/api/character/edit", json=payload).status_code, 400)
            self.assertEqual(client.put("/api/character/edit", json={"role": "../../secret", "name": "name"}).status_code, 404)
            self.assertEqual(client.get("/api/character/edit", params={"role": "../../secret"}).status_code, 404)
            self.assertEqual(client.get("/api/character/avatar", params={"role": "../../secret"}).status_code, 404)
            before = set(load_custom_roles(root))
            created = client.post("/api/character/custom", json={"name": "不创建", "personality": "冷静", "avatar_data": invalid[0]})
            self.assertEqual(created.status_code, 400)
            self.assertEqual(set(load_custom_roles(root)), before)

    def test_edit_replace_failure_preserves_file_memory_and_original_avatar(self):
        with edited_app() as (root, app, state, client, first, second):
            good = {"role": "assistant", "name": "原名称", "avatar_data": png_avatar()}
            client.put("/api/character/edit", json=good).raise_for_status()
            target = root / "config" / "role_edits.yaml"
            prior = target.read_bytes()
            character = copy.deepcopy(state.cfg["character"])
            with patch("backend.role_edits.os.replace", side_effect=OSError("fixture disk full")):
                response = client.put("/api/character/edit", json={"role": "assistant", "name": "不能保存", "avatar_data": None})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(target.read_bytes(), prior)
            self.assertEqual(state.cfg["character"], character)
            self.assertEqual(list(target.parent.glob(".role_edits-*.tmp")), [])

    def test_deleting_custom_role_removes_its_private_edits_without_touching_other_roles(self):
        with edited_app() as (root, app, state, client, first, second):
            for role in (first, second):
                client.put("/api/character/edit", json={"role": role, "avatar_data": png_avatar(), "note": "保存设定"}).raise_for_status()
            second_before = copy.deepcopy(load_role_edits(root)[second])
            client.post("/api/character/delete", json={"role": first}).raise_for_status()
            self.assertNotIn(first, load_role_edits(root))
            self.assertEqual(load_role_edits(root)[second], second_before)
            self.assertEqual(client.get("/api/character/avatar", params={"role": first}).status_code, 404)

    def test_edit_waits_for_active_turn_and_applies_to_next_turn_without_new_chat(self):
        with edited_app() as (root, app, state, client, first, second):
            async def scenario():
                entered, release = asyncio.Event(), asyncio.Event()
                async def model(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return "完成原回合", []
                state.llm.chat = model
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as api:
                    turn = asyncio.create_task(api.post("/api/chat", json={"message": "旧回合"}))
                    await asyncio.wait_for(entered.wait(), 2)
                    editing = asyncio.create_task(api.put("/api/character/edit", json={"role": "assistant", "name": "新名字", "personality": "使用短句"}))
                    try:
                        for _ in range(100):
                            if state.loop.pending_user_turns:
                                break
                            await asyncio.sleep(.001)
                        self.assertFalse(editing.done())
                        self.assertEqual(state.cfg["character"]["name"], "情景助手")
                    finally:
                        release.set()
                    self.assertEqual((await asyncio.wait_for(turn, 2)).status_code, 200)
                    self.assertEqual((await asyncio.wait_for(editing, 2)).status_code, 200)
                self.assertEqual(state.cfg["character"]["name"], "新名字")
                self.assertTrue(any(item["content"] == "旧回合" for item in state.loop.history))
                self.assertIn("使用短句", state.cfg["character"]["prompt"])
            asyncio.run(scenario())


class ChannelNameEditingTests(unittest.TestCase):
    def test_names_persist_preserve_other_fields_and_reach_custom_and_default_model_context(self):
        with edited_app() as (root, app, state, client, first, second):
            before = copy.deepcopy(state.cfg["device_channels"])
            caps = copy.deepcopy(state.safety.user_caps)
            response = client.post("/api/device/channel-names", json={"A": "左侧", "B": "右侧"})
            self.assertEqual(response.status_code, 200, response.text)
            for channel, name in (("A", "左侧"), ("B", "右侧")):
                self.assertEqual(state.cfg["device_channels"][channel], {**before[channel], "name": name})
                self.assertEqual(config.load_config()["device_channels"][channel]["name"], name)
            self.assertEqual(state.safety.user_caps, caps)
            for role in ("assistant", first):
                if role != "assistant":
                    client.post("/api/character/profile", json={"role": role, "profile": "角色扮演"}).raise_for_status()
                # Android's default guide follows the same custom-device branch.
                character = {**state.cfg["character"], "is_custom": True}
                prompt = build_system_prompt(character, state.loop.build_state())
                self.assertIn('"channel_names": {"A": "左侧", "B": "右侧"}', prompt)
                self.assertIn("A或B", prompt)
            reset = client.post("/api/device/channel-names", json={"A": " "})
            self.assertEqual(reset.json()["device_channels"]["A"]["name"], "A 通道")
            self.assertEqual(reset.json()["device_channels"]["B"]["name"], "右侧")

    def test_validation_and_atomic_failure_leave_file_and_memory_unchanged(self):
        with edited_app() as (root, app, state, client, first, second):
            client.post("/api/device/channel-names", json={"A": "原名称"}).raise_for_status()
            target = root / "config" / "device_channels.yaml"
            previous, values = target.read_bytes(), copy.deepcopy(state.cfg["device_channels"])
            for body in ({"A": "x" * 21}, {"A": None}, {"C": "名称"}, {"A": "两\n行"}, {"A": {"enabled": False}}, {}):
                self.assertEqual(client.post("/api/device/channel-names", json=body).status_code, 400)
            with patch("backend.config.os.replace", side_effect=OSError("fixture disk full")):
                response = client.post("/api/device/channel-names", json={"A": "新名称"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(state.cfg["device_channels"], values)
            self.assertEqual(list(target.parent.glob(".device_channels-*.tmp")), [])
            old = client.post("/api/device/channels", json={"B": {"name": "只改旧接口名称"}})
            self.assertEqual(old.status_code, 200)
            self.assertEqual(old.json()["device_channels"]["B"]["location"], values["B"]["location"])


if __name__ == "__main__":
    unittest.main()

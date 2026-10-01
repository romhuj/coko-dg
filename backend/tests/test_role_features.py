"""API integration in a temporary workspace; never starts a relay or real LLM."""
import copy
import asyncio
import logging
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
import httpx
from fastapi.testclient import TestClient

from backend import config
from backend.safety import SafetyManager


@contextmanager
def isolated_logging():
    logger = logging.getLogger("ai-for-coyote")
    original_handlers = set(logger.handlers)
    original_level = logger.level
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(original_level)


class RoleFeatureTests(unittest.TestCase):
    def test_api_roles_chat_intensity_and_restart(self):
        with tempfile.TemporaryDirectory() as folder, isolated_logging():
            root = Path(folder)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "config.yaml").write_text(yaml.safe_dump({
                "app": {"dry_run": True, "check_update": False},
                "llm": {"api_key": "", "base_url": "http://invalid.local/v1"},
            }), encoding="utf-8")
            (root / "basic.md").write_text("You are a neutral test character.", encoding="utf-8")
            (config_dir / "character.yaml").write_text(yaml.safe_dump({
                "roles": {"base": {"name": "Base", "profiles": {"default": {"prompt_file": "basic.md"}}}},
                "role": "base", "profile": "default",
            }), encoding="utf-8")
            with patch.object(config, "PROJECT_ROOT", root), patch.object(config, "CONFIG_DIR", config_dir), \
                    patch.object(config, "CHARACTER_RUNTIME_FILE", config_dir / "character_runtime.yaml"), \
                    patch.object(config, "DEVICE_CHANNELS_FILE", config_dir / "device_channels.yaml"):
                from backend import main
                with patch.object(main, "PROJECT_ROOT", root):
                    app = main.make_app()
                    runtime = app.state.runtime
                    runtime.llm.chat = AsyncMock(return_value=("Hello", [{"op": "hold_strength", "channel": "A", "value": 99}]))
                    client = TestClient(app)  # no context manager: deliberately skip startup/lifespan
                    with patch.object(runtime.loop, "execute_actions", new_callable=AsyncMock) as execute:
                        result = client.post("/api/chat", json={"message": "Hello", "mode": "text"})
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual(result.json()["executed"], [])
                        execute.assert_not_called()
                    with patch.object(runtime.loop, "handle_user_message", new_callable=AsyncMock,
                                      return_value={"line": "OK", "executed": [], "dropped": [], "error": None}) as handle:
                        self.assertEqual(client.post("/api/chat", json={"message": "继续场景"}).status_code, 200)
                        handle.assert_awaited_once_with("继续场景", control_device=None, preferred_pattern=None,
                                                       accepted_action_epoch=runtime.loop._action_epoch)
                    for bad in ({"message": ""}, {"message": "x", "mode": "invalid"}, {"message": "x", "preferred_pattern": "missing"}):
                        self.assertEqual(client.post("/api/chat", json=bad).status_code, 400)
                    for level, scale in SafetyManager.INTENSITY_LEVELS.items():
                        result = client.post("/api/intensity", json={"level": level})
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual(result.json()["strength_scale"], {"A": scale, "B": scale})
                        self.assertTrue(result.json()["intensity_device_link"])
                    self.assertEqual(client.post("/api/intensity", json={"level": "unknown"}).status_code, 400)
                    self.assertEqual(client.post("/api/intensity", json={"level": "中", "sync_to_device": "false"}).status_code, 400)
                    # Lowering a cap must actually send the decrease, not pre-clamp the tracked current.
                    runtime.safety.current["A"] = 80
                    with patch.object(runtime.loop, "execute_actions", new_callable=AsyncMock, return_value=([], [])) as execute:
                        client.post("/api/device/channels/cap", json={"channel": "A", "value": 25})
                        execute.assert_awaited_once_with([{"op": "hold_strength", "channel": "A", "value": 25}])
                        self.assertEqual(runtime.safety.current["A"], 80)
                    source = {"title": "Sherlock Holmes", "url": "https://en.wikipedia.org/?curid=123", "summary": "A fictional detective.", "provider": "Wikipedia", "language": "en"}
                    with patch.object(main, "search_character", new_callable=AsyncMock, return_value={"query": "Holmes", "sources": [source], "warnings": []}):
                        search = client.post("/api/character/search", json={"query": "Holmes"}).json()
                    body = {"search_id": search["search_id"], "source_index": 0, "name": "福尔摩斯", "note": "注重推理"}
                    created = client.post("/api/character/create", json=body)
                    self.assertEqual(created.status_code, 200, created.text)
                    role = created.json()["role"]
                    state = client.get("/api/state").json()
                    self.assertEqual(state["role"], role)
                    self.assertEqual(state["character"], "福尔摩斯")
                    self.assertEqual(len(state["roles"]), 2)
                    self.assertTrue(state["roles"][-1]["is_custom"])
                    self.assertTrue(state["roles"][-1]["profiles"][0]["available"])
                    self.assertEqual(state["roles"][-1]["sources"], [source])
                    self.assertEqual(runtime.loop.history, [])
                    self.assertEqual(client.post("/api/character/create", json=body).status_code, 400)
                    reloaded = config.load_config()
                    self.assertEqual(reloaded["character"]["role"], role)
                    self.assertTrue(reloaded["character"]["is_custom"])
                    self.assertIn("A fictional detective", reloaded["character"]["prompt"])
                    self.assertEqual(client.post("/api/character/profile", json={"role": "base", "profile": "default"}).status_code, 200)
                    self.assertEqual(client.post("/api/character/profile", json={"role": role, "profile": "角色扮演"}).status_code, 200)
                    async def switch_during_auto_turn():
                        entered, release = asyncio.Event(), asyncio.Event()
                        async def delayed_model(*args, **kwargs):
                            entered.set()
                            await release.wait()
                            return "旧角色的本轮回复", []
                        runtime.llm.chat = delayed_model
                        auto_task = asyncio.create_task(runtime.loop._autopilot_turn())
                        await asyncio.wait_for(entered.wait(), 2)
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as queued_client:
                            switch_task = asyncio.create_task(queued_client.post("/api/character/profile", json={"role": "base", "profile": "default"}))
                            for _ in range(20):
                                if runtime.loop.build_state()["pending_chat"]:
                                    break
                                await asyncio.sleep(.01)
                            self.assertFalse(switch_task.done())
                            release.set()
                            await asyncio.wait_for(auto_task, 2)
                            switched = await asyncio.wait_for(switch_task, 2)
                            self.assertEqual(switched.status_code, 200)
                        self.assertEqual(runtime.cfg["character"]["role"], "base")
                        self.assertEqual(runtime.loop.history, [])
                        self.assertFalse(runtime.loop.turn_busy)
                        self.assertEqual(runtime.loop.build_state()["pending_chat"], 0)
                    asyncio.run(switch_during_auto_turn())
                    client.close()

    def test_app_channel_caps_and_zero_are_enforced(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        safety = SafetyManager(cfg)
        safety.update_device_state({}, {"channelA": {"intensityMax": 24, "comfortLimit": {"comfortMax": 31, "absoluteMax": 80}}, "channelB": {"comfortLimit": {"comfortMax": 0}}})
        self.assertEqual(safety.cap_for("A"), 24)
        self.assertEqual(safety.cap_for("B"), 0)
        for channel in ("A", "B"):
            for op in ("hold_strength", "temp_strength", "add_strength"):
                valid, _, cmd = safety.validate({"op": op, "channel": channel, "value": 999, "delta": 999})
                self.assertTrue(valid)
                self.assertLessEqual(cmd["value"], safety.cap_for(channel))


if __name__ == "__main__":
    unittest.main()

"""Offline chat/device routing tests. No configured LLM or device is contacted."""
import copy
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend.config import Config, DEFAULTS, _load_waveforms
from backend.game_loop import GameLoop
from backend.device.dglab_relay import DGLabRelayBackend
from backend.delivery import Delivery
from backend.llm import LLM, build_system_prompt
from backend.safety import SafetyManager


def config():
    cfg = Config(copy.deepcopy(DEFAULTS))
    cfg["character"] = {"name": "测试角色", "player_nick": "测试用户", "prompt": "简洁回答。"}
    _load_waveforms(cfg)
    return cfg


def fake_backend(*, ready=False):
    backend = Mock()
    backend.ready.return_value = ready
    backend.to_state.return_value = {"status": "paired" if ready else "disconnected", "controller_id": None}
    backend.loops_active.return_value = {"A": False, "B": False}
    backend.apply = AsyncMock(return_value=False)
    backend.start_pulse_hold = AsyncMock(return_value=False)
    return backend


class ChatModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpaired_device_requests_do_not_call_model(self):
        cfg = config()
        cfg["app"]["dry_run"] = False
        model = Mock(chat=AsyncMock())
        loop = GameLoop(cfg, model, SafetyManager(cfg), fake_backend())
        result = await loop.handle_user_message("你好", control_device=True)
        self.assertEqual(result["error"], "device_not_connected")
        loop.turn_busy = True
        self.assertEqual((await loop.auto_open())["error"], "turn_unavailable")
        self.assertIsNone(await loop._auto_observe_turn())
        self.assertIsNone(await loop._autopilot_turn())
        model.chat.assert_not_awaited()
        self.assertEqual(loop.history, [])

    async def test_chat_waits_for_autopilot_and_pending_messages_have_priority(self):
        cfg = config()
        entered, release = asyncio.Event(), asyncio.Event()
        calls, active = [], 0

        async def chat(character, history, state, **kwargs):
            nonlocal active
            active += 1
            self.assertEqual(active, 1)
            calls.append(copy.deepcopy(history))
            if len(calls) == 1:
                entered.set()
                await release.wait()
            active -= 1
            return f"回复{len(calls)}", []

        loop = GameLoop(cfg, Mock(chat=AsyncMock(side_effect=chat)), SafetyManager(cfg), fake_backend(ready=True))
        loop.autopilot = True
        loop.on_ai_turn = AsyncMock()
        loop.on_state_change = AsyncMock()
        with patch("backend.game_loop.reload_character"):
            automatic = asyncio.create_task(loop._autopilot_turn())
            await asyncio.wait_for(entered.wait(), 1)
            first = asyncio.create_task(loop.handle_user_message("第一条", control_device=False))
            second = asyncio.create_task(loop.handle_user_message("第二条", control_device=False))
            await asyncio.sleep(0)
            self.assertEqual(loop.build_state()["pending_chat"], 2)
            self.assertTrue(loop.build_state()["turn_busy"])
            self.assertIsNone(await loop._autopilot_turn())
            self.assertIsNone(await loop._auto_observe_turn())
            release.set()
            results = await asyncio.wait_for(asyncio.gather(automatic, first, second), 1)
        self.assertEqual([r["line"] for r in results], ["回复1", "回复2", "回复3"])
        self.assertEqual([m["content"] for m in calls[1][-2:]], ["回复1", "第一条"])
        self.assertEqual([m["content"] for m in calls[2][-2:]], ["回复2", "第二条"])
        self.assertEqual(loop.pending_user_turns, 0)
        self.assertFalse(loop.turn_busy)
        self.assertTrue(loop.autopilot)
        loop.on_ai_turn.assert_awaited_once()  # HTTP user replies are not broadcast twice.
        self.assertGreaterEqual(loop.on_state_change.await_count, 3)

    async def test_cancelled_queue_entry_does_not_block_later_chat_or_role_edits(self):
        cfg = config()
        model = Mock(chat=AsyncMock(return_value=("可以继续", [])))
        loop = GameLoop(cfg, model, SafetyManager(cfg), fake_backend())
        with patch("backend.game_loop.reload_character"):
            async with loop.conversation_edit():
                cancelled = asyncio.create_task(loop.handle_user_message("取消", control_device=False))
                await asyncio.sleep(0)
                self.assertEqual(loop.pending_user_turns, 1)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                self.assertEqual(loop.pending_user_turns, 0)
                self.assertTrue(loop.turn_busy)
                loop.clear_history()
            result = await asyncio.wait_for(loop.handle_user_message("继续", control_device=False), 1)
        self.assertEqual(result["line"], "可以继续")
        self.assertFalse(loop.turn_busy)
        model.chat.assert_awaited_once()

    async def test_automatic_messages_publish_once_before_queued_chat_can_finish(self):
        cfg = config()
        for entrypoint in ("auto_open", "_auto_observe_turn", "_autopilot_turn"):
            with self.subTest(entrypoint=entrypoint):
                publishing, release = asyncio.Event(), asyncio.Event()
                order = []

                async def chat(character, history, state, **kwargs):
                    if history[-1]["content"] == "排队消息":
                        order.append("user-model")
                        return "用户回复", []
                    return "主动回复", []

                async def publish(result):
                    publishing.set()
                    await release.wait()
                    order.append("automatic-published")

                model = Mock(chat=AsyncMock(side_effect=chat))
                loop = GameLoop(cfg, model, SafetyManager(cfg), fake_backend(ready=True))
                loop.on_ai_turn = AsyncMock(side_effect=publish)
                with patch("backend.game_loop.reload_character"):
                    automatic = asyncio.create_task(getattr(loop, entrypoint)())
                    await asyncio.wait_for(publishing.wait(), 1)
                    queued = asyncio.create_task(loop.handle_user_message("排队消息", control_device=False))
                    await asyncio.sleep(0)
                    self.assertFalse(queued.done())
                    self.assertEqual(model.chat.await_count, 1)
                    self.assertTrue(loop.turn_busy)
                    release.set()
                    results = await asyncio.wait_for(asyncio.gather(automatic, queued), 1)
                self.assertEqual(order, ["automatic-published", "user-model"])
                self.assertEqual([r["line"] for r in results], ["主动回复", "用户回复"])
                loop.on_ai_turn.assert_awaited_once()

    async def test_auto_mode_resolves_connection_and_estop_after_waiting(self):
        cfg = config()
        cfg["app"]["dry_run"] = False
        backend = fake_backend(ready=True)
        backend.apply.return_value = True
        backend.start_pulse_hold.return_value = True
        model = Mock(chat=AsyncMock(return_value=("文字仍可用", [{"op": "hold_strength", "channel": "A", "value": 20}])))
        loop = GameLoop(cfg, model, SafetyManager(cfg), backend)
        with patch("backend.game_loop.reload_character"):
            async with loop.conversation_edit():
                queued = asyncio.create_task(loop.handle_user_message("继续聊天", control_device=None))
                await asyncio.sleep(0)
                backend.ready.return_value = False
                loop.on_client_disconnected()
            result = await asyncio.wait_for(queued, 1)
            self.assertEqual(result["executed"], [])
            self.assertFalse(model.chat.call_args.args[2]["control_device"])
            backend.ready.return_value = True
            await loop.estop()
            result = await loop.handle_user_message("急停期间继续聊", control_device=None)
            self.assertEqual(result["executed"], [])
            self.assertFalse(model.chat.call_args.args[2]["control_device"])
            await loop.resume()
            result = await loop.handle_user_message("恢复后的新互动", control_device=None)
            self.assertTrue(model.chat.call_args.args[2]["control_device"])
            self.assertEqual(len(result["executed"]), 2)

    async def test_estop_invalidates_inflight_and_queued_actions_even_after_resume(self):
        cfg = config()
        entered, release = asyncio.Event(), asyncio.Event()

        async def chat(*args, **kwargs):
            entered.set()
            await release.wait()
            return "模型迟到的动作", [{"op": "hold_strength", "channel": "A", "value": 20}]

        backend = fake_backend(ready=True)
        loop = GameLoop(cfg, Mock(chat=AsyncMock(side_effect=chat)), SafetyManager(cfg), backend)
        with patch("backend.game_loop.reload_character"):
            automatic = asyncio.create_task(loop._autopilot_turn())
            await asyncio.wait_for(entered.wait(), 1)
            queued = asyncio.create_task(loop.handle_user_message("排队的设备消息", control_device=True))
            await asyncio.sleep(0)
            stopped = await asyncio.wait_for(loop.handle_user_message("急停", control_device=False), 1)
            self.assertIsNone(stopped["error"])
            await loop.resume()
            release.set()
            results = await asyncio.wait_for(asyncio.gather(automatic, queued), 1)
        self.assertIsNone(results[0])  # The model itself was preempted before execution.
        self.assertEqual(results[1]["executed"], [])
        self.assertEqual(results[1]["error"], "turn_cancelled")
        self.assertEqual(loop.safety.current, {"A": 0, "B": 0})
        self.assertEqual(loop.patterns, {"A": None, "B": None})
        backend.apply.assert_not_awaited()
        backend.start_pulse_hold.assert_not_awaited()

    async def test_cap_and_reported_strength_are_rechecked_after_waveform_await(self):
        cfg = config()
        cfg["app"]["dry_run"] = False
        safety = SafetyManager(cfg)
        safety.set_intensity_level("炼狱", sync_to_device=True)
        backend = fake_backend(ready=True)
        loop = GameLoop(cfg, Mock(), safety, backend)

        async def lower_cap(*args, **kwargs):
            await asyncio.sleep(0)
            safety.app_caps["A"] = 9
            safety.current["A"] = 4
            return loop._receipt({"op": "pulse_hold", "channel": "A", "pattern": "呼吸"},
                                 {"kind": "pulse_hold", "channel": "A", "pattern": "呼吸"}, Delivery("sent", True))

        loop._ensure_default_wave = AsyncMock(side_effect=lower_cap)
        for op in ("hold_strength", "add_strength", "temp_strength"):
            safety.app_caps["A"] = 100
            safety.current["A"] = 10
            action = {"op": op, "channel": "A", "value": 30, "delta": 20, "duration_s": 3}
            executed, dropped = await loop.execute_actions([action], apply_scale=True)
            self.assertEqual(len(executed), 2)
            self.assertEqual(dropped, [])
            sent = backend.apply.call_args.args[0]
            self.assertEqual(sent["value"], 9)
            if op == "add_strength":
                self.assertEqual(sent["delta"], 5)

    async def test_exact_stop_works_even_in_busy_text_chat(self):
        cfg = config()
        model = Mock(chat=AsyncMock())
        safety = SafetyManager(cfg)
        loop = GameLoop(cfg, model, safety, fake_backend())
        loop.turn_busy = True
        for text in ("急停", "停止设备", "stop", "ESTOP"):
            safety.resume()
            safety.current["A"] = 15
            result = await loop.handle_user_message(text, control_device=False)
            self.assertIsNone(result["error"])
            self.assertTrue(safety.estop_active)
            self.assertEqual(safety.current["A"], 0)
        model.chat.assert_not_awaited()

    async def test_model_failure_cannot_trigger_floor_or_actions(self):
        cfg = config()
        model = Mock(chat=AsyncMock(side_effect=RuntimeError("offline failure")))
        backend = fake_backend(ready=True)
        loop = GameLoop(cfg, model, SafetyManager(cfg), backend)
        loop.turn_count = 4
        loop.execute_actions = AsyncMock(side_effect=AssertionError("No execution on model failure"))
        with patch("backend.game_loop.reload_character"), self.assertLogs("ai-for-coyote.game", level="ERROR"):
            result = await loop.handle_user_message("你好", control_device=True)
            opened = await loop.auto_open()
        self.assertEqual(result["error"], "offline failure")
        self.assertEqual(opened["executed"], [])
        self.assertEqual(loop.turn_count, 4)
        loop.execute_actions.assert_not_awaited()

    async def test_all_automatic_turns_are_neutral_and_do_not_force_actions(self):
        cfg = config()
        model = Mock(chat=AsyncMock(return_value=("你好。", [])))
        backend = fake_backend(ready=True)
        loop = GameLoop(cfg, model, SafetyManager(cfg), backend)
        with patch("backend.game_loop.reload_character"):
            for custom in (False, True):
                cfg["character"]["is_custom"] = custom
                await loop.auto_open()
                await loop._auto_observe_turn()
                await loop._autopilot_turn()
        prompts = [m["content"] for m in loop.history if m["role"] == "user"]
        self.assertEqual(len(prompts), 6)
        for content in prompts:
            self.assertIn("用户", content)
            self.assertNotIn("触手", content)
            self.assertNotIn("至少一个波形", content)
        self.assertEqual(loop.safety.current, {"A": 0, "B": 0})
        self.assertEqual(loop.patterns, {"A": None, "B": None})
        backend.apply.assert_not_awaited()
        backend.start_pulse_hold.assert_not_awaited()

    async def test_unpaired_text_turn_discards_actions_and_never_captures_image(self):
        cfg = config()
        cfg["app"]["dry_run"] = False
        safety = SafetyManager(cfg)
        backend = fake_backend()
        model = Mock()
        model.chat = AsyncMock(return_value=("你好。", [{"op": "hold_strength", "channel": "A", "value": 20}]))
        camera = Mock(enabled=True)
        camera.to_state.return_value = {}
        camera.has_frame.side_effect = AssertionError("Text chat must not request a frame")
        loop = GameLoop(cfg, model, safety, backend, camera=camera)
        loop.turn_count = 4
        with patch("backend.game_loop.reload_character"):
            result = await loop.handle_user_message("你好", control_device=False)
        self.assertEqual(result["line"], "你好。")
        self.assertEqual((result["executed"], result["dropped"]), ([], []))
        self.assertEqual(safety.current, {"A": 0, "B": 0})
        self.assertEqual(loop.turn_count, 4)
        self.assertFalse(model.chat.call_args.args[2]["control_device"])
        self.assertIsNone(model.chat.call_args.kwargs["image_b64"])
        backend.apply.assert_not_awaited()
        backend.start_pulse_hold.assert_not_awaited()

    async def test_selected_imported_waveform_reaches_the_existing_safety_pipeline(self):
        cfg = config()
        name, meta = next((n, p) for n, p in cfg["presets"].items() if p["waveform"].startswith("import_pulse_"))
        safety = SafetyManager(cfg)
        backend = fake_backend(ready=True)
        model = Mock()
        model.chat = AsyncMock(return_value=("已选择该波形。", [{"op": "pulse_hold", "channel": "A", "pattern": name}]))
        loop = GameLoop(cfg, model, safety, backend)
        with patch("backend.game_loop.reload_character"):
            result = await loop.handle_user_message("使用选中的波形", control_device=True, preferred_pattern=name)
        self.assertEqual(len(result["executed"]), 2)
        self.assertEqual(result["dropped"], [])
        self.assertEqual(loop.patterns["A"], name)
        sent_state = model.chat.call_args.args[2]
        self.assertEqual(sent_state["preferred_pattern"], name)
        self.assertTrue(sent_state["control_device"])
        self.assertNotIn("preferred_pattern", cfg)
        self.assertEqual(meta["frames"], safety.presets[name]["frames"])
        # dry-run still uses the real validation/recording route without sending.
        backend.start_pulse_hold.assert_not_awaited()

    async def test_llm_text_boundary_removes_unexpected_actions_and_images(self):
        cfg = config()
        cfg["llm"].update(base_url="http://offline.invalid/v1", api_key="", model="offline-test")
        model = LLM(cfg)
        await model.client.aclose()
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"line":"你好。","actions":[{"op":"stop"}]}'}}]})

        model.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            line, actions = await model.chat(cfg["character"], [{"role": "user", "content": "你好"}],
                                             {"control_device": False}, image_b64="must-not-attach")
        finally:
            await model.client.aclose()
        self.assertEqual((line, actions), ("你好。", []))
        self.assertNotIn(b"must-not-attach", requests[0].content)


class WaveformPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config()
        cls.safety = SafetyManager(cls.cfg)

    def test_all_imported_waveforms_work_with_external_and_custom_roles(self):
        imported = {n: p for n, p in self.cfg["presets"].items() if p["waveform"].startswith("import_pulse_")}
        self.assertEqual(len(imported), 212)
        state = self.safety.to_state()
        selected = next(reversed(imported))
        state.update(control_device=True, preferred_pattern=selected)
        for lang in ("zh", "en"):
            for custom in (False, True):
                with self.subTest(lang=lang, custom=custom):
                    character = dict(self.cfg["character"], prompt_file="neutral-role.md", lang=lang, is_custom=custom)
                    prompt = build_system_prompt(character, state)
                    for name in imported:
                        self.assertIn(name, prompt)
                    for op in ("hold_strength", "add_strength", "pulse", "pulse_hold", "temp_strength", "clear", "stop"):
                        self.assertIn(op, prompt)
                    self.assertGreaterEqual(prompt.count(selected), 2)
        for name in imported:
            ok, reason, cmd = self.safety.validate({"op": "pulse_hold", "channel": "A", "pattern": name})
            self.assertTrue(ok, reason)
            self.assertEqual(cmd["frames"], imported[name]["frames"])

    def test_text_prompt_keeps_role_identity_and_includes_six_styles(self):
        character = dict(self.cfg["character"], prompt="ROLE_IDENTITY_REFERENCE", prompt_file="old.md")
        for level in ("低", "中", "高", "极高", "最高", "炼狱"):
            state = self.safety.to_state()
            state.update(control_device=False, intensity_level=level, intensity_device_link=True)
            prompt = build_system_prompt(character, state)
            self.assertIn(level, prompt)
            self.assertIn("actions 必须为 []", prompt)
            self.assertIn("ROLE_IDENTITY_REFERENCE", prompt)
            self.assertNotIn("pulse_hold", prompt)
            self.assertNotIn("当前完整波形库", prompt)

    def test_all_roles_receive_current_patterns_and_contextual_action_policy(self):
        state = self.safety.to_state()
        state.update(patterns={"A": "正在播放的波形", "B": None}, autopilot=True,
                     notes=["用户希望保持当前节奏"], control_device=True, rage_rounds=20)
        for lang in ("zh", "en"):
            for custom in (False, True):
                character = dict(self.cfg["character"], is_custom=custom, lang=lang)
                prompt = build_system_prompt(character, state)
                self.assertIn("正在播放的波形", prompt)
                self.assertIn("用户希望保持当前节奏", prompt)
                self.assertIn('"autopilot": true', prompt)
                self.assertIn("actions: []", prompt)
                self.assertNotIn("每轮小幅上调", prompt)
                self.assertNotIn("必须把 A 也带上", prompt)
                self.assertNotIn("强度逐步加码", prompt)

    def test_saved_personality_and_independent_judgment_apply_in_every_chat_mode(self):
        persona = "性格谨慎，重视证据，先比较方案再作决定。"
        for custom in (False, True):
            for control_device in (False, True):
                for lang in ("zh", "en"):
                    with self.subTest(custom=custom, control_device=control_device, lang=lang):
                        character = dict(self.cfg["character"], prompt=persona, prompt_file="saved-role.md",
                                         is_custom=custom, lang=lang)
                        state = self.safety.to_state()
                        state["control_device"] = control_device
                        prompt = build_system_prompt(character, state)
                        self.assertIn(persona, prompt)
                        if lang == "zh":
                            self.assertIn("接受、拒绝、暂缓", prompt)
                            self.assertIn("不是必须照办的执行命令", prompt)
                            self.assertIn("停止、减弱的要求", prompt)
                            self.assertIn("不要编造为原作设定", prompt)
                            self.assertIn("身份、动机和核心语气优先", prompt)
                        else:
                            self.assertIn("accept, refuse, defer", prompt)
                            self.assertIn("not an automatic order to execute", prompt)
                            self.assertIn("Stop or ease-off requests", prompt)
                            self.assertIn("Do not invent canonical traits", prompt)
                            self.assertIn("identity, motives and core voice take precedence", prompt)

    def test_custom_role_reference_survives_text_mode(self):
        character = dict(self.cfg["character"], is_custom=True, prompt="Neutral explorer who likes maps.")
        prompt = build_system_prompt(character, {"control_device": False})
        self.assertIn(character["prompt"], prompt)
        self.assertIn("actions must be []", prompt)


class ScaledActionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.safety = SafetyManager(self.cfg)
        self.loop = GameLoop(self.cfg, Mock(), self.safety, fake_backend())

    def test_all_six_levels_scale_both_channels_and_sync_can_be_disabled(self):
        for level, multiplier in (("低", .7), ("中", 1), ("高", 1.3), ("极高", 1.6), ("最高", 2), ("炼狱", 2.5)):
            self.safety.set_intensity_level(level, sync_to_device=True)
            for channel in ("A", "B"):
                cmd = {"kind": "hold", "channel": channel, "value": 20}
                self.loop._scale_cmd(cmd)
                self.assertEqual(cmd["value"], round(20 * multiplier))
            self.safety.set_intensity_level(level, sync_to_device=False)
            cmd = {"kind": "hold", "channel": "A", "value": 20}
            self.loop._scale_cmd(cmd)
            self.assertEqual(cmd["value"], 20)

    def test_scaled_delta_matches_recorded_target_and_respects_step_and_app_cap(self):
        self.safety.set_intensity_level("炼狱", sync_to_device=True)
        self.safety.current["A"] = 10
        self.safety.app_caps["A"] = 35
        cmd = {"kind": "add", "channel": "A", "delta": 30, "value": 40}
        self.loop._scale_cmd(cmd)
        self.assertEqual((cmd["delta"], cmd["value"]), (25, 35))
        self.safety.app_caps["A"] = 100
        cmd = {"kind": "add", "channel": "A", "delta": 30, "value": 40}
        self.loop._scale_cmd(cmd)
        self.assertEqual((cmd["delta"], cmd["value"]), (40, 50))

    def test_unit_scale_still_caps_and_reduction_never_increases_strength(self):
        self.safety.app_caps["A"] = 20
        cmd = {"kind": "hold", "channel": "A", "value": 80}
        self.loop._scale_cmd(cmd)
        self.assertEqual(cmd["value"], 20)
        self.safety.current["A"] = 90
        cmd = {"kind": "add", "channel": "A", "delta": -5}
        self.loop._scale_cmd(cmd)
        self.assertEqual((cmd["delta"], cmd["value"]), (-70, 20))

    def test_wire_targets_respect_independent_zero_and_low_caps_at_every_level(self):
        # Only compile RPC dictionaries. Never start the relay or call apply/send.
        relay = DGLabRelayBackend(self.cfg, self.safety)
        for cap_a, cap_b in ((18, 7), (0, 11), (16, 0)):
            self.safety.app_caps.update(A=cap_a, B=cap_b)
            for level in self.safety.INTENSITY_LEVELS:
                self.safety.set_intensity_level(level, sync_to_device=True)
                for ch, cap in (("A", cap_a), ("B", cap_b)):
                    initial = min(5, cap)
                    for op in ("hold_strength", "temp_strength", "add_strength"):
                        with self.subTest(caps=(cap_a, cap_b), level=level, channel=ch, op=op):
                            self.safety.current[ch] = initial
                            action = {"op": op, "channel": ch, "value": 95, "delta": 40, "duration_s": 3}
                            ok, reason, cmd = self.safety.validate(action)
                            self.assertTrue(ok, reason)
                            self.loop._scale_cmd(cmd)
                            frames = relay._build_frames(cmd, "fake-client", "fake-slot")
                            deltas = [f["data"]["data"]["v"] for f in frames]
                            self.assertEqual(initial + sum(deltas), cmd["value"])
                            self.assertGreaterEqual(cmd["value"], 0)
                            self.assertLessEqual(cmd["value"], cap)
                            self.assertTrue(all(abs(delta) <= self.safety.max_step for delta in deltas))
                            self.safety.record(cmd)
                            self.assertEqual(self.safety.current[ch], cmd["value"])


if __name__ == "__main__":
    unittest.main()

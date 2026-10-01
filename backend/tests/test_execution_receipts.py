"""Public synthetic frames only: no configured model, socket or device is used.

Protocol fixtures: https://github.com/dungeonlab-open/dglab-kit#v4-协议参考
device.op completes later; device.op.clear returns {}. A timeout is never retried.
"""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.config import Config, DEFAULTS
from backend.delivery import Delivery
from backend.device.dglab_relay import DGLabRelayBackend
from backend.game_loop import GameLoop
from backend.llm import build_system_prompt
from backend.safety import SafetyManager
from backend.safety_intent import stop_intent


def configuration():
    cfg = Config(copy.deepcopy(DEFAULTS))
    cfg["app"]["dry_run"] = False
    cfg["character"] = {"name": "Fixture", "prompt": "保持冷静，依据上下文判断。", "is_custom": True}
    cfg["presets"] = {"挤压": {"waveform": "fixture", "label": "挤压", "frames": ["0A0A0A0A00000000"],
                              "default_duration_s": 3, "max_duration_s": 10}}
    cfg["ui"]["default_wave"] = "挤压"
    return cfg


class SocketFixture:
    def __init__(self, relay):
        self.relay, self.frames = relay, []
        self.fail = False
        self.respond = True
        self.error = None
        self.wave_gate = None
        self.entered = asyncio.Event()

    async def send(self, raw):
        frame = json.loads(raw)
        self.frames.append(frame)
        inner = frame["data"]
        waveform = inner["m"] == "device.op" and inner.get("data", {}).get("t") == 0
        self.entered.set()
        if waveform and self.wave_gate:
            await self.wave_gate.wait()
        if self.fail:
            raise OSError("synthetic transport failure")
        if self.respond and not waveform:
            self.reply(frame, error=self.error)

    def reply(self, frame, *, error=None, client=None, req_id=None, reason="completed"):
        inner = frame["data"]
        result = {} if inner["m"] == "device.op.clear" else {"reason": reason, "type": inner.get("data", {}).get("t")}
        response = {"t": "resp", "reqId": req_id or inner["reqId"]}
        response["error" if error else "result"] = error or result
        self.relay._handle_message({"type": "message", "clientId": client or "fixture-app", "data": response})


class ExecutionReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = configuration()
        self.safety = SafetyManager(self.cfg)
        self.backend = DGLabRelayBackend(self.cfg, self.safety)
        self.backend.rpc_timeout_s = .04
        self.relay = self.backend.relay
        self.relay.clients["fixture-app"] = {"devices": [{"slotId": "fixture-slot"}], "props": {}, "slotState": {}}
        self.relay.clients["other-app"] = {"devices": [], "props": {}, "slotState": {}}
        self.ws = self.relay.ws = SocketFixture(self.relay)
        self.model = Mock(chat=AsyncMock(return_value=("我先保持原来的状态。", [])))
        self.loop = GameLoop(self.cfg, self.model, self.safety, self.backend)

    async def asyncTearDown(self):
        await self.backend.stop()
        self.relay._fail_pending("fixture cleanup")
        await asyncio.sleep(0)

    def wave(self):
        return {"kind": "pulse_hold", "channel": "A", "pattern": "挤压", "frames": ["0A0A0A0A00000000"]}

    async def test_default_wave_and_strength_have_two_truthful_receipts(self):
        executed, dropped = await self.loop.execute_actions([{"op": "hold_strength", "channel": "A", "value": 56}])
        self.assertEqual(dropped, [])
        self.assertEqual([item["command"] for item in executed], [
            {"kind": "pulse_hold", "channel": "A", "pattern": "挤压"},
            {"kind": "hold", "channel": "A", "value": 56}])
        self.assertEqual([item["status"] for item in executed], ["sent", "confirmed"])
        self.assertTrue(all(item["sent"] for item in executed))
        self.assertEqual(executed[0]["source"], "default_wave")
        self.assertEqual(self.safety.current["A"], 56)

    async def test_pulse_only_derives_nonzero_baseline_once_and_preserves_live_strength(self):
        self.safety.set_intensity_level("最高", True)
        for current, expected in ((0, 30), (7, 7)):
            self.safety.current["A"] = current
            executed, dropped = await self.loop.execute_actions(
                [{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}], apply_scale=True)
            self.assertEqual(dropped, [])
            self.assertEqual(len(executed), 2)
            self.assertEqual(executed[1]["command"]["value"], expected)
            self.assertEqual(executed[1]["status"], "confirmed")
            self.assertEqual(executed[1]["source"], "waveform_strength")

    async def test_explicit_pair_is_not_duplicated_even_with_strength_first_and_keeps_channels_separate(self):
        self.safety.set_intensity_level("最高", True)
        self.safety.user_caps.update(A=56, B=7)
        executed, dropped = await self.loop.execute_actions([
            {"op": "hold_strength", "channel": "A", "value": 40},
            {"op": "pulse_hold", "channel": "A", "pattern": "挤压"},
            {"op": "pulse_hold", "channel": "B", "pattern": "挤压"},
            {"op": "hold_strength", "channel": "B", "value": 3},
        ], apply_scale=True)
        self.assertEqual(dropped, [])
        self.assertEqual(len(executed), 4)
        self.assertEqual([item["command"]["channel"] for item in executed], ["A", "A", "B", "B"])
        self.assertEqual([item["command"]["value"] for item in executed if item["command"]["kind"] == "hold"], [56, 6])

    async def test_zero_cap_blocks_derived_pair_and_explicit_zero_never_becomes_an_increase(self):
        self.safety.app_caps["A"] = 0
        executed, dropped = await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 2)
        self.assertEqual(self.ws.frames, [])
        self.safety.app_caps["A"] = 100
        self.safety.current["A"] = 20
        executed, dropped = await self.loop.execute_actions([
            {"op": "pulse_hold", "channel": "A", "pattern": "挤压"},
            {"op": "hold_strength", "channel": "A", "value": 0}])
        self.assertEqual(len(dropped), 1)
        self.assertEqual([item["command"]["kind"] for item in executed], ["hold"])
        self.assertEqual(executed[0]["command"]["value"], 0)
        self.assertEqual(self.safety.current["A"], 0)
        self.assertFalse(any(frame["data"].get("data", {}).get("t") == 0 for frame in self.ws.frames))

    async def test_pair_strength_failure_clears_already_sent_wave_without_retrying_strength(self):
        original = self.ws.reply
        def reply(frame, **kwargs):
            if frame["data"].get("data", {}).get("t") == 3 and frame["data"].get("data", {}).get("v", 0) > 0:
                kwargs["error"] = "invalid_operate"
            original(frame, **kwargs)
        self.ws.reply = reply
        executed, _ = await self.loop.execute_actions([
            {"op": "pulse_hold", "channel": "A", "pattern": "挤压"},
            {"op": "hold_strength", "channel": "A", "value": 56}])
        self.assertEqual([item["status"] for item in executed], ["sent", "failed", "confirmed"])
        self.assertEqual(executed[-1]["source"], "failed_pair_cleanup")
        self.assertFalse(self.backend.loops_active()["A"])
        self.assertEqual(self.safety.current["A"], 0)
        self.assertEqual(sum(frame["data"].get("data", {}).get("v") == 56 for frame in self.ws.frames), 1)

    async def test_invalid_paired_wave_never_falls_back_to_default_for_positive_strength(self):
        executed, dropped = await self.loop.execute_actions([
            {"op": "pulse_hold", "channel": "A", "pattern": "不存在的波形"},
            {"op": "hold_strength", "channel": "A", "value": 56}])
        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 2)
        self.assertEqual(self.ws.frames, [])
        self.assertEqual(self.safety.current["A"], 0)
        self.safety.current["A"] = 20
        executed, dropped = await self.loop.execute_actions([
            {"op": "pulse_hold", "channel": "A", "pattern": "不存在的波形"},
            {"op": "hold_strength", "channel": "A", "value": 0}])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(executed[0]["command"], {"kind": "hold", "channel": "A", "value": 0})
        self.assertFalse(any(frame["data"].get("data", {}).get("t") == 0 for frame in self.ws.frames))

    async def test_first_wave_write_failure_never_changes_state_or_sends_strength(self):
        self.ws.fail = True
        executed, dropped = await self.loop.execute_actions([{"op": "hold_strength", "channel": "A", "value": 56}])
        self.assertEqual(executed[0]["status"], "failed")
        self.assertFalse(executed[0]["sent"])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(self.safety.current["A"], 0)
        self.assertIsNone(self.loop.patterns["A"])
        self.assertFalse(self.backend.loops_active()["A"])
        self.assertEqual(len(self.ws.frames), 1)

    async def test_first_write_is_a_barrier_and_stop_during_it_prevents_worker(self):
        self.ws.wave_gate = asyncio.Event()
        start = asyncio.create_task(self.backend.start_pulse_hold("A", self.wave()))
        await self.ws.entered.wait()
        self.assertFalse(start.done())
        self.assertFalse(self.backend.loops_active()["A"])
        self.backend.stop_pulse_hold("A")
        self.ws.wave_gate.set()
        receipt = await start
        self.assertFalse(receipt)
        self.assertEqual(receipt.status, "unconfirmed")
        self.assertFalse(self.backend.loops_active()["A"])

    async def test_response_must_match_both_client_and_request_and_late_reply_cannot_revive(self):
        self.ws.respond = False
        frame = self.backend.ops.add_strength("fixture-app", "fixture-slot", 0, 12)
        task = asyncio.create_task(self.relay.send_rpc(frame, timeout_s=.03))
        await self.ws.entered.wait()
        self.ws.reply(frame, client="other-app")
        self.ws.reply(frame, req_id="different-request")
        self.assertFalse(task.done())
        receipt = await task
        self.assertEqual(receipt.status, "unconfirmed")
        self.assertTrue(receipt.sent)
        self.ws.reply(frame)
        self.assertEqual(len(self.ws.frames), 1)
        self.assertEqual(self.relay._pending_rpc, {})

    async def test_uncertain_strength_blocks_next_turn_until_fresh_matching_device_state(self):
        await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        self.ws.respond = False
        executed, _ = await self.loop.execute_actions([{"op": "hold_strength", "channel": "A", "value": 56}])
        self.assertEqual(executed[0]["status"], "unconfirmed")
        self.assertTrue(self.safety.uncertain_channels["A"])
        self.assertEqual(self.safety.current["A"], 15)
        frames = len(self.ws.frames)
        executed, dropped = await self.loop.execute_actions([{"op": "add_strength", "channel": "A", "delta": 5}])
        self.assertEqual(executed, [])
        self.assertTrue(dropped)
        self.assertEqual(len(self.ws.frames), frames)
        self.safety.update_device_state({"intensityA": 15}, {})
        self.assertTrue(self.safety.uncertain_channels["A"])
        self.ws.reply(self.ws.frames[-1])
        self.assertTrue(self.safety.uncertain_channels["A"])
        await self.backend._forward_event("slots_patch", {"_client_id": "other-app", "slots": [
            {"slotId": "fixture-slot", "props": {"intensityA": 56}}]})
        self.assertTrue(self.safety.uncertain_channels["A"])
        await self.backend._forward_event("slots_patch", {"_client_id": "fixture-app", "slots": [
            {"slotId": "fixture-slot", "props": {"intensityA": 56}}]})
        self.assertFalse(self.safety.uncertain_channels["A"])
        self.assertEqual(self.safety.current["A"], 56)
        self.ws.respond = True
        executed, dropped = await self.loop.execute_actions([{"op": "add_strength", "channel": "A", "delta": 5}])
        self.assertFalse(dropped)
        self.assertEqual(executed[-1]["command"]["value"], 61)

    async def test_device_report_before_add_ack_does_not_apply_delta_twice(self):
        await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        original = self.ws.reply
        def reply(frame, **kwargs):
            operation = frame["data"].get("data", {})
            if operation.get("t") == 3:
                # A completed operation can be reported before its RPC reply.
                self.safety.current["A"] = 20
            original(frame, **kwargs)
        self.ws.reply = reply
        executed, dropped = await self.loop.execute_actions([{"op": "add_strength", "channel": "A", "delta": 5}])
        self.assertEqual(dropped, [])
        self.assertEqual(executed[-1]["command"]["value"], 20)
        self.assertEqual(self.safety.current["A"], 20)

    async def test_late_ack_after_stop_and_resume_cannot_restore_old_strength(self):
        await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        self.ws.respond = False
        self.ws.entered.clear()
        pending = asyncio.create_task(self.loop.execute_actions([{"op": "hold_strength", "channel": "A", "value": 56}]))
        await self.ws.entered.wait()
        frame = self.ws.frames[-1]
        self.ws.respond = True
        await self.loop.estop()
        await self.loop.resume()
        self.ws.reply(frame)
        executed, _ = await pending
        self.assertEqual(executed[0]["status"], "unconfirmed")
        self.assertEqual(self.safety.current["A"], 0)
        self.assertFalse(self.safety.uncertain_channels["A"])

    async def test_concurrent_strength_is_not_issued_against_old_value_and_late_temp_cannot_rearm(self):
        self.ws.respond = False
        pending = asyncio.create_task(self.backend.apply({"kind": "temp", "channel": "A", "value": 56, "duration_s": 5}))
        await self.ws.entered.wait()
        frame = self.ws.frames[-1]
        newer = await self.backend.apply({"kind": "add", "channel": "A", "value": 5, "delta": 5})
        self.assertEqual(newer.status, "failed")
        self.assertFalse(newer.sent)
        self.assertEqual(len(self.ws.frames), 1)
        self.ws.respond = True
        await self.loop.estop()
        await self.loop.resume()
        await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        self.ws.reply(frame)
        self.assertTrue((await pending).superseded)
        self.assertNotIn("A", self.backend.revert_tasks)
        self.assertEqual(self.safety.current["A"], 15)
        self.assertTrue(self.backend.loops_active()["A"])

    async def test_disconnect_cancels_existing_temp_timer_before_reconnect_and_resume(self):
        result = await self.backend.apply({"kind": "temp", "channel": "A", "value": 20, "duration_s": .03})
        self.assertTrue(result)
        self.assertIn("A", self.backend.revert_tasks)
        self.loop.on_client_disconnected()
        await self.loop.resume()
        self.ws.frames.clear()
        await asyncio.sleep(.05)
        self.assertEqual(self.ws.frames, [])
        self.assertNotIn("A", self.backend.revert_tasks)

    async def test_lowering_cap_during_pending_strength_can_clear_without_raising_old_zero(self):
        await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        self.ws.respond = False
        self.ws.entered.clear()
        pending = asyncio.create_task(self.loop.execute_actions([{"op": "hold_strength", "channel": "A", "value": 80}]))
        await self.ws.entered.wait()
        frame = self.ws.frames[-1]
        self.assertTrue(self.backend.strength_pending("A"))
        self.safety.set_user_cap("A", 40)
        self.ws.respond = True
        await self.loop.clear_channel("A")
        self.ws.reply(frame)
        executed, _ = await pending
        self.assertEqual(executed[0]["status"], "unconfirmed")
        self.assertTrue(self.safety.enabled["A"])
        self.assertEqual(self.safety.current["A"], 0)

    async def test_explicit_rpc_failure_does_not_predict_new_strength(self):
        await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        self.ws.error = "invalid_operate"
        executed, _ = await self.loop.execute_actions([{"op": "hold_strength", "channel": "A", "value": 56}])
        self.assertEqual(executed[0]["status"], "failed")
        self.assertTrue(executed[0]["sent"])
        self.assertEqual(self.safety.current["A"], 15)
        self.assertFalse(self.backend.loops_active()["A"])

    async def test_each_wave_batch_uses_unique_request_and_late_error_stops_loop(self):
        self.cfg["playback"].update(loop_batch_s=.1, loop_overlap_s=0)
        await self.loop.execute_actions([{"op": "pulse_hold", "channel": "A", "pattern": "挤压"}])
        await asyncio.sleep(.23)
        ids = [frame["data"]["reqId"] for frame in self.ws.frames]
        self.assertGreaterEqual(len(ids), 2)
        self.assertEqual(len(ids), len(set(ids)))
        self.ws.reply(self.ws.frames[-1], error="slot_not_found")
        await asyncio.sleep(0)
        self.assertFalse(self.backend.loops_active()["A"])
        self.assertIsNone(self.loop.patterns["A"])
        self.assertEqual(self.loop.build_state()["execution_feedback"]["outcome"], "failed")

    async def test_disabled_channel_is_cleared_on_wire_and_negative_delta_uses_snapshot(self):
        self.safety.current["A"] = 56
        result = await self.loop.disable_channel("A")
        self.assertFalse(self.safety.enabled["A"])
        self.assertEqual(self.safety.current["A"], 0)
        self.assertEqual(result["executed"][0]["status"], "confirmed")
        self.assertTrue(any(frame["data"].get("data", {}).get("v") == -56 for frame in self.ws.frames))
        self.assertEqual(self.ws.frames[0]["data"]["m"], "device.op.clear")

    async def test_clear_all_remains_available_while_stopped_without_changing_enabled_channels(self):
        self.safety.current.update(A=12, B=9)
        self.safety.estop_active = True
        self.safety.enabled["B"] = False
        self.loop.patterns.update(A="挤压", B="挤压")
        result = await self.loop.clear_channel(None)
        self.assertEqual(result["executed"][0]["status"], "confirmed")
        self.assertEqual(result["executed"][0]["command"], {"kind": "clear", "channel": None})
        self.assertEqual(self.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.loop.patterns, {"A": None, "B": None})
        self.assertTrue(self.safety.estop_active)
        self.assertEqual(self.safety.enabled, {"A": True, "B": False})
        self.assertEqual(len(self.ws.frames), 5)

    async def test_clear_batch_timeout_is_shared_and_never_retries(self):
        self.ws.respond = False
        self.safety.current.update(A=12, B=9)
        started = asyncio.get_running_loop().time()
        result = await self.loop.estop()
        self.assertLess(asyncio.get_running_loop().time() - started, .15)
        self.assertEqual(result["status"], "unconfirmed")
        self.assertFalse(result["sent"])
        self.assertEqual(len(self.ws.frames), 5)
        self.assertTrue(self.safety.estop_active)

    async def test_stop_preempts_model_and_hook_sees_mutable_final_reply_under_lock(self):
        entered = asyncio.Event()
        async def model(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.model.chat.side_effect = model
        events = []
        async def save(event):
            self.assertTrue(self.loop._conversation_lock.locked())
            event["result"]["conversation_id"] = "fixture-conversation"
            events.append(event)
        self.loop.on_turn = save
        with patch("backend.game_loop.reload_character"):
            old = asyncio.create_task(self.loop.handle_user_message("继续交流", control_device=False))
            await asyncio.wait_for(entered.wait(), 1)
            stopped = await asyncio.wait_for(self.loop.handle_user_message("急停。", control_device=False), 1)
            self.assertEqual((await old)["error"], "turn_cancelled")
        self.assertEqual(stopped["conversation_id"], "fixture-conversation")
        self.assertEqual(events[-1]["user_text"], "急停。")
        self.assertEqual(events[-1]["result"]["safety_intent"], "emergency")

    async def test_empty_actions_keep_output_and_include_prior_objective_feedback(self):
        await self.loop.execute_actions([{"op": "hold_strength", "channel": "A", "value": 12}])
        before = len(self.ws.frames)
        with patch("backend.game_loop.reload_character"):
            result = await self.loop.handle_user_message("再想一想。", control_device=True)
        self.assertEqual(result["executed"], [])
        self.assertEqual(result["dropped"], [])
        self.assertEqual(result["execution_outcome"], "unchanged")
        self.assertEqual(len(self.ws.frames), before)
        self.assertEqual(self.safety.current["A"], 12)

    async def test_judgment_switch_changes_ordinary_stop_but_never_emergency(self):
        with patch("backend.game_loop.reload_character"):
            stopped = await self.loop.handle_user_message("我不舒服。", control_device=True)
            self.assertEqual(stopped["safety_intent"], "discomfort")
            self.model.chat.assert_not_awaited()
            await self.loop.resume()
            self.cfg["interaction"] = {"model_judgment": True}
            result = await self.loop.handle_user_message("停一下。", control_device=True)
            self.assertNotIn("safety_intent", result)
            self.assertEqual(self.model.chat.await_count, 1)
            result = await self.loop.handle_user_message("停止设备！", control_device=True)
            self.assertEqual(result["safety_intent"], "emergency")
            self.assertEqual(self.model.chat.await_count, 1)

    async def test_automatic_prompt_uses_the_same_judgment_switch(self):
        with patch("backend.game_loop.reload_character"):
            for enabled in (False, True):
                self.cfg["interaction"] = {"model_judgment": enabled}
                await self.loop._autopilot_turn()
                prompt = self.model.chat.call_args.args[1][-1]["content"]
                self.assertEqual("停止与减弱要求仍须优先遵守" in prompt, not enabled)
                if enabled:
                    self.assertIn("普通情景中的犹豫和拒绝措辞", prompt)
                    self.assertIn("明确急停与真实安全撤回始终优先", prompt)


class StopPolicyTests(unittest.TestCase):
    def test_exact_emergency_is_always_active_and_quotes_negation_are_not_commands(self):
        for mode in (False, True):
            for text in ("急停！", "停止设备。", "STOP!", "estop"):
                self.assertEqual(stop_intent(text, model_judgment=mode), "emergency")
        for text in ("不要停止", "她说‘停止’", "如果我说停止会怎样", "我没有不舒服", "停不停由你决定", "可能该停一下？"):
            self.assertIsNone(stop_intent(text))
        self.assertEqual(stop_intent("请停一下。"), "stop")
        self.assertIsNone(stop_intent("请停一下。", model_judgment=True))

    def test_every_role_gets_execution_evidence_and_judgment_policy(self):
        for custom in (False, True):
            for mode in (False, True):
                state = {"model_judgment": mode, "execution_feedback": {"outcome": "failed"}, "control_device": True}
                prompt = build_system_prompt({"name": "Fixture", "prompt_file": "fixture.md", "is_custom": custom}, state)
                self.assertIn("模型自判断：开启" if mode else "模型自判断：关闭", prompt)
                self.assertIn("历史角色台词不是执行证据", prompt)
                self.assertIn('"outcome": "failed"', prompt)
                self.assertIn("真实安全撤回", prompt)
                if mode:
                    self.assertNotIn("停止、减弱的要求", prompt)


if __name__ == "__main__":
    unittest.main()

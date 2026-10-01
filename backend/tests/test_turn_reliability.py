"""Turn latency, model parsing and cancellation tests: in-memory HTTP/device only."""
import asyncio
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend.game_loop import GameLoop
from backend.llm import LLM, ModelResponseError, parse_llm_json
from backend.safety import SafetyManager
from backend.tests.test_chat_modes import config, fake_backend


def response(content, **extra):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, **extra}]})


class CompletionReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def model(self, handler, *, official=False, json_mode=True):
        cfg = config()
        cfg["llm"].update(base_url="https://api.deepseek.com" if official else "https://offline.invalid/v1",
                          api_key="", model="deepseek-flash", json_mode=json_mode)
        model = LLM(cfg)
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(model.client.aclose)
        return model

    async def chat(self, model):
        return await model.chat({"name": "测试"}, [{"role": "user", "content": "继续"}], {"control_device": True})

    def test_decoder_handles_quoted_braces_and_rejects_ambiguous_objects(self):
        self.assertEqual(parse_llm_json('```json\n{"line":"括号 } 与 {","actions":[]}\n```')["line"], "括号 } 与 {")
        self.assertEqual(parse_llm_json('{"line":"one"} {"line":"two","actions":[{"op":"stop"}]}'), {})
        self.assertEqual(parse_llm_json('{"line":"truncated","actions":[{"op":"stop"}]'), {})
        self.assertEqual(parse_llm_json('{"line":"outer","actions":[{"line":"nested","actions":[{"op":"stop"}]}]'), {})
        self.assertEqual(parse_llm_json('解释：{"line":"不能从说明里摘动作","actions":[{"op":"stop"}]}'), {})

    async def test_official_flash_disables_thinking_custom_provider_untouched(self):
        for official in (False, True):
            requests = []
            def handle(request):
                requests.append(json.loads(request.content))
                return response('{"line":"回复","actions":[]}')
            model = await self.model(handle, official=official)
            self.assertEqual(await self.chat(model), ("回复", []))
            self.assertEqual(requests[0].get("thinking"), {"type": "disabled"} if official else None)

    async def test_reasoning_is_never_used_as_words_or_device_commands(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "", "reasoning_content": '{"line":"秘密推理","actions":[{"op":"hold_strength","channel":"A","value":80}]}'}}]})
        model = await self.model(handle)
        with self.assertRaisesRegex(RuntimeError, "最终回复"):
            await self.chat(model)
        self.assertEqual(len(calls), 2)

    async def test_empty_line_repair_cannot_reuse_rejected_actions(self):
        contents = iter(['{"line":"","actions":[{"op":"stop"}]}', '{"line":"改正的回复","actions":[]}'])
        model = await self.model(lambda request: response(next(contents)))
        self.assertEqual(await self.chat(model), ("改正的回复", []))

    async def test_empty_response_gets_one_model_only_retry_then_recovers(self):
        contents = iter([None, '{"line":"恢复回复","actions":[]}'])
        calls = []
        def handle(request):
            calls.append(request)
            return response(next(contents), finish_reason="stop")
        model = await self.model(handle)
        self.assertEqual(await self.chat(model), ("恢复回复", []))
        self.assertEqual(len(calls), 2)

    async def test_repeated_empty_reports_safe_diagnostics(self):
        calls = []
        def handle(request):
            calls.append(request)
            return response(None, finish_reason="stop")
        model = await self.model(handle)
        with self.assertRaisesRegex(ModelResponseError, "content 与 reasoning_content 均为空") as caught:
            await self.chat(model)
        self.assertEqual(caught.exception.diagnostic, {"code": "empty_content", "attempts": 2, "finish_reason": "stop"})
        self.assertEqual(len(calls), 2)

    async def test_structured_final_content_blocks_are_supported(self):
        model = await self.model(lambda request: response([
            {"type": "text", "text": '{"line":"完成",'}, {"type": "text", "text": '"actions":[]}'}]))
        self.assertEqual(await self.chat(model), ("完成", []))

    async def test_known_unsupported_json_format_gets_one_compatibility_retry(self):
        requests = []
        def handle(request):
            payload = json.loads(request.content); requests.append(payload)
            if len(requests) == 1:
                return httpx.Response(400, json={"error": {"message": "response_format json_object is not supported"}})
            return response('{"line":"兼容回复","actions":[]}')
        model = await self.model(handle)
        self.assertEqual(await self.chat(model), ("兼容回复", []))
        self.assertEqual(len(requests), 2)
        self.assertNotIn("response_format", requests[1])

    async def test_model_error_and_network_timeout_are_not_retried(self):
        for status in (400, 401, 429, 503, "timeout"):
            calls = []
            def handle(request):
                calls.append(request)
                if status == "timeout":
                    raise httpx.ReadTimeout("slow", request=request)
                return httpx.Response(status, json={"error": {"message": "model unavailable"}})
            model = await self.model(handle)
            with self.assertRaises((httpx.HTTPStatusError, httpx.ReadTimeout)):
                await self.chat(model)
            self.assertEqual(len(calls), 1)

    async def test_one_wall_clock_deadline_covers_repair(self):
        calls = []
        async def handle(request):
            calls.append(request)
            await asyncio.sleep(.01 if len(calls) == 1 else 1)
            return response("not json")
        model = await self.model(handle)
        model.timeout_s = .12
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await self.chat(model)
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(len(calls), 2)

    async def test_truncation_does_not_execute_even_parseable_json(self):
        calls = []
        def handle(request):
            calls.append(request)
            return response('{"line":"回复","actions":[{"op":"stop"}]}', finish_reason="length")
        model = await self.model(handle)
        with self.assertRaisesRegex(RuntimeError, "截断"):
            await self.chat(model)
        self.assertEqual(len(calls), 1)


class TurnReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def loop(self, chat, *, real_sends=False):
        cfg = config()
        cfg["app"]["dry_run"] = not real_sends
        backend = fake_backend(ready=True)
        backend.apply = AsyncMock(return_value=True)
        backend.loops_active.return_value = {"A": True, "B": True}
        loop = GameLoop(cfg, Mock(chat=AsyncMock(side_effect=chat)), SafetyManager(cfg), backend)
        loop.auto_yield_delay_s = .015
        self.patch = patch("backend.game_loop.reload_character")
        self.patch.start(); self.addCleanup(self.patch.stop)
        return loop

    async def stop_auto(self, loop):
        task = loop.autopilot_task
        loop.set_autopilot(False)
        if task:
            await asyncio.gather(task, return_exceptions=True)

    async def test_interval_change_wakes_existing_wait_and_recalculates_start_deadline(self):
        waiting, started = asyncio.Event(), asyncio.Event()
        async def chat(*args, **kwargs):
            started.set(); return "回复", []
        loop = self.loop(chat)
        loop.autopilot_interval = 30
        async def notify():
            if loop.build_state()["turn_status"]["next_auto_at_ms"]:
                waiting.set()
        loop.on_state_change = notify
        clock = [0.0]
        clock_api = SimpleNamespace(monotonic=lambda: time.monotonic()+clock[0], time=time.time)
        with patch("backend.game_loop.time", clock_api):
            loop.set_autopilot(True)
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                clock[0] = 6  # Six seconds elapsed since enable; original wait was thirty.
                self.assertEqual(loop.set_autopilot_interval(5), 5)
                await asyncio.wait_for(started.wait(), .5)
            finally:
                await self.stop_auto(loop)

    async def test_slow_turn_does_not_add_another_full_interval_after_completion(self):
        first, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        async def chat(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first.set(); await release.wait()
            else:
                second.set()
            return "回复", []
        loop = self.loop(chat)
        loop.autopilot_interval = .01  # Reach the first test turn promptly.
        clock = [0.0]
        clock_api = SimpleNamespace(monotonic=lambda: time.monotonic()+clock[0], time=time.time)
        with patch("backend.game_loop.time", clock_api):
            loop.set_autopilot(True)
            try:
                await asyncio.wait_for(first.wait(), 1)
                loop.set_autopilot_interval(5)
                clock[0] = 8  # The first model call took eight seconds of scheduler time.
                release.set()
                await asyncio.wait_for(second.wait(), .5)
                self.assertEqual(calls, 2)
            finally:
                await self.stop_auto(loop)

    async def test_scheduled_auto_continues_after_yielding_a_slow_model_to_chat(self):
        entered, next_auto = asyncio.Event(), asyncio.Event()
        auto_calls = 0
        async def chat(character, history, state, **kwargs):
            nonlocal auto_calls
            if state.get("turn_source") == "autopilot":
                auto_calls += 1
                if auto_calls == 1:
                    entered.set(); await asyncio.Event().wait()
            return "继续", []
        loop = self.loop(chat)
        loop.autopilot_interval = .03
        async def publish(result):
            next_auto.set()
        loop.on_ai_turn = publish
        loop.set_autopilot(True)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            result = await asyncio.wait_for(loop.handle_user_message("先回复我", control_device=False), 1)
            self.assertEqual(result["line"], "继续")
            await asyncio.wait_for(next_auto.wait(), 1)
            self.assertTrue(loop.autopilot)
            self.assertEqual(auto_calls, 2)
        finally:
            await self.stop_auto(loop)

    async def test_auto_errors_are_published_without_killing_schedule_and_wait_state_is_visible(self):
        async def chat(*args, **kwargs):
            raise TimeoutError()
        loop = self.loop(chat)
        loop.autopilot_interval = .03
        errors, states, twice = [], [], asyncio.Event()
        async def publish(result):
            errors.append(result)
            if len(errors) == 2:
                twice.set()
        async def state_changed():
            states.append(copy.deepcopy(loop.build_state()))
        loop.on_ai_turn = publish
        loop.on_state_change = state_changed
        loop.set_autopilot(True)
        try:
            with self.assertLogs("ai-for-coyote.game", level="ERROR"):
                await asyncio.wait_for(twice.wait(), 1)
            self.assertTrue(all(result["error"] == "TimeoutError" for result in errors))
            self.assertTrue(any(state["turn_status"]["next_auto_at_ms"] for state in states))
            self.assertEqual(loop.history, [])
        finally:
            await self.stop_auto(loop)

    async def test_slow_automatic_generation_yields_to_user_without_old_history_or_actions(self):
        entered = asyncio.Event()
        active = 0
        async def chat(character, history, state, **kwargs):
            nonlocal active
            active += 1
            self.assertEqual(active, 1)
            try:
                if state.get("turn_source") == "autopilot":
                    entered.set()
                    await asyncio.Event().wait()
                return "及时回复", []
            finally:
                active -= 1
        loop = self.loop(chat)
        automatic = asyncio.create_task(loop._autopilot_turn())
        await asyncio.wait_for(entered.wait(), 1)
        user = asyncio.create_task(loop.handle_user_message("用户新消息", control_device=False))
        auto_result, user_result = await asyncio.wait_for(asyncio.gather(automatic, user), 1)
        self.assertIsNone(auto_result)
        self.assertEqual(user_result["line"], "及时回复")
        self.assertEqual([m["content"] for m in loop.history], ["用户新消息", "及时回复"])
        self.assertEqual(loop.pending_user_turns, 0)
        self.assertFalse(loop.turn_busy)
        loop.backend.apply.assert_not_awaited()

    async def test_cancellation_ignoring_model_cannot_execute_superseded_actions(self):
        entered = asyncio.Event()
        async def chat(character, history, state, **kwargs):
            if state.get("turn_source") == "autopilot":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return "过期回复", [{"op": "hold_strength", "channel": "A", "value": 40}]
            return "用户回复", []
        loop = self.loop(chat, real_sends=True)
        automatic = asyncio.create_task(loop._autopilot_turn())
        await asyncio.wait_for(entered.wait(), 1)
        user = await asyncio.wait_for(loop.handle_user_message("先回答", control_device=False), 1)
        self.assertEqual(user["line"], "用户回复")
        self.assertIsNone(await automatic)
        loop.backend.apply.assert_not_awaited()

    async def test_user_priority_never_interrupts_or_retries_device_execution(self):
        executing, finish = asyncio.Event(), asyncio.Event()
        async def chat(character, history, state, **kwargs):
            return "回复", [{"op": "hold_strength", "channel": "A", "value": 8}] if state.get("turn_source") == "autopilot" else []
        loop = self.loop(chat, real_sends=True)
        async def apply(cmd):
            executing.set(); await finish.wait(); return True
        loop.backend.apply = AsyncMock(side_effect=apply)
        automatic = asyncio.create_task(loop._autopilot_turn())
        await asyncio.wait_for(executing.wait(), 1)
        user = asyncio.create_task(loop.handle_user_message("排队", control_device=False))
        await asyncio.sleep(.04)
        self.assertFalse(user.done())
        self.assertEqual(loop.build_state()["turn_status"]["phase"], "executing")
        finish.set()
        auto_result, _ = await asyncio.wait_for(asyncio.gather(automatic, user), 1)
        self.assertTrue(auto_result["executed"][0]["sent"])
        self.assertEqual(loop.backend.apply.await_count, 1)

    async def test_automatic_failure_is_visible_and_does_not_pollute_context(self):
        async def chat(*args, **kwargs):
            raise TimeoutError()
        loop = self.loop(chat)
        loop.history = [{"role": "user", "content": "之前的话"}]
        loop.on_ai_turn = AsyncMock()
        with self.assertLogs("ai-for-coyote.game", level="ERROR"):
            result = await loop._autopilot_turn()
        self.assertIn("超时", result["line"])
        loop.on_ai_turn.assert_awaited_once_with(result)
        self.assertEqual(loop.history, [{"role": "user", "content": "之前的话"}])
        status = loop.build_state()["turn_status"]
        self.assertEqual(status["phase"], "idle")
        self.assertIn("超时", status["last_error"])
        self.assertEqual(result["executed"], [])

    async def test_phase_broadcasts_and_executed_metadata_use_actual_capped_target(self):
        async def chat(*args, **kwargs):
            return "调整", [{"op": "hold_strength", "channel": "A", "value": 95}]
        loop = self.loop(chat, real_sends=True)
        loop.safety.set_user_cap("A", 9)
        loop.safety.scale["A"] = 2.5
        states = []
        async def state_changed():
            states.append(copy.deepcopy(loop.build_state()))
        loop.on_state_change = state_changed
        result = await loop.handle_user_message("根据情况决定", control_device=True)
        executed = result["executed"][0]
        self.assertEqual(executed["command"], {"kind": "hold", "channel": "A", "value": 9})
        self.assertEqual(executed["action"]["value"], 95)
        self.assertEqual(loop.backend.apply.call_args.args[0]["value"], 9)
        phases = [s["turn_status"]["phase"] for s in states]
        self.assertIn("thinking", phases); self.assertIn("executing", phases)
        self.assertEqual(phases[-1], "idle")
        thinking = next(s["turn_status"] for s in states if s["turn_status"]["phase"] == "thinking")
        self.assertEqual(thinking["source"], "user")
        self.assertIsInstance(thinking["started_at_ms"], int)


if __name__ == "__main__":
    unittest.main()

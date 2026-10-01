"""DeepSeek response compatibility: fake HTTP only, no model or device calls."""
import asyncio
import copy
import json
import time
import unittest

import httpx

from backend.llm import LLM, ModelResponseError
from backend.tests.test_chat_modes import config


def response(content=None, *, finish_reason="stop", reasoning=None, usage=None):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish_reason, "message": {
            "content": content, "reasoning_content": reasoning,
        }}],
        "usage": usage if usage is not None else {
            "completion_tokens": 0, "completion_tokens_details": {"reasoning_tokens": 0},
        },
    })


VALID = '{"line":"已恢复","actions":[{"op":"hold_strength","channel":"A","value":3}]}'


class DeepSeekEmptyResponseTests(unittest.IsolatedAsyncioTestCase):
    async def model(self, handler, *, host="https://api.deepseek.com", name="deepseek-chat", json_mode=True):
        cfg = config()
        cfg["llm"].update(base_url=host, api_key="unit-test-secret", model=name, json_mode=json_mode)
        model = LLM(cfg)
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(model.client.aclose)
        return model

    async def chat(self, model):
        return await model.chat({"name": "测试"}, [{"role": "user", "content": "继续"}], {"control_device": True})

    async def test_empty_official_response_retries_without_format_and_keeps_strict_mode(self):
        for name in ("deepseek-chat", "deepseek-flash"):
            with self.subTest(model=name):
                requests = []

                def handle(request):
                    requests.append(json.loads(request.content))
                    return response(None if len(requests) == 1 else VALID)

                model = await self.model(handle, name=name)
                line, actions = await self.chat(model)
                self.assertEqual(line, "已恢复")
                self.assertEqual(actions, [{"op": "hold_strength", "channel": "A", "value": 3}])
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0]["response_format"], {"type": "json_object"})
                self.assertNotIn("response_format", requests[1])
                self.assertIs(model.json_mode, True)
                self.assertEqual(requests[1]["model"], name)
                self.assertEqual(requests[1].get("thinking"), {"type": "disabled"} if name == "deepseek-flash" else None)
                # A fallback belongs to this turn, not a persistent setting change.
                await self.chat(model)
                self.assertIn("response_format", requests[2])

    async def test_fallback_does_not_accept_plain_text_or_malformed_json(self):
        for invalid in ("a plain final answer", '{"line":"partial","actions":[{"op":"stop"}]',
                        '{"line":"","actions":[{"op":"stop"}]}',
                        'prefix {"line":"hidden","actions":[{"op":"stop"}]}'):
            with self.subTest(response=invalid):
                requests = []

                def handle(request):
                    requests.append(json.loads(request.content))
                    return response(None if len(requests) == 1 else invalid)

                model = await self.model(handle)
                with self.assertRaises(ModelResponseError) as caught:
                    await self.chat(model)
                self.assertEqual(caught.exception.diagnostic["code"], "invalid_response")
                self.assertEqual(len(requests), 2)
                self.assertNotIn("response_format", requests[1])
                self.assertTrue(model.json_mode)

    async def test_repeated_empty_keeps_both_safe_response_metadata_records(self):
        calls = []

        def handle(request):
            calls.append(request)
            return response(None, usage={
                "completion_tokens": len(calls),
                "completion_tokens_details": {"reasoning_tokens": 0},
                "private_payload": "provider-secret",
            })

        model = await self.model(handle)
        with self.assertLogs("ai-for-coyote.llm", level="INFO") as logs:
            with self.assertRaises(ModelResponseError) as caught:
                await self.chat(model)
        self.assertEqual(caught.exception.diagnostic, {
            "code": "empty_content", "attempts": 2, "finish_reason": "stop",
            "response_attempts": [
                {"attempt": 1, "finish_reason": "stop", "completion_tokens": 1,
                 "reasoning_tokens": 0, "empty_content": True, "json_mode": True},
                {"attempt": 2, "finish_reason": "stop", "completion_tokens": 2,
                 "reasoning_tokens": 0, "empty_content": True, "json_mode": False},
            ],
        })
        self.assertEqual(len(calls), 2)
        for output in (str(caught.exception.diagnostic), "\n".join(logs.output)):
            self.assertNotIn("unit-test-secret", output)
            self.assertNotIn("provider-secret", output)
            self.assertNotIn("继续", output)

    async def test_response_metadata_rejects_provider_text_and_invalid_token_counts(self):
        model = await self.model(lambda request: response(
            None, finish_reason="private-provider-payload", usage={
                "completion_tokens": "private-provider-payload",
                "completion_tokens_details": {"reasoning_tokens": True},
            },
        ))
        with self.assertRaises(ModelResponseError) as caught:
            await self.chat(model)
        for item in caught.exception.diagnostic["response_attempts"]:
            self.assertEqual(item["finish_reason"], "unknown")
            self.assertIsNone(item["completion_tokens"])
            self.assertIsNone(item["reasoning_tokens"])
        self.assertNotIn("private-provider-payload", str(caught.exception.diagnostic))

    async def test_custom_hosts_keep_the_existing_json_retry(self):
        for host in ("https://offline.invalid/v1", "https://api.deepseek.com.attacker.invalid",
                     "https://api.deepseek.com@offline.invalid"):
            requests = []

            def handle(request):
                requests.append(json.loads(request.content))
                return response(None if len(requests) == 1 else VALID)

            model = await self.model(handle, host=host, name="deepseek-flash")
            self.assertEqual((await self.chat(model))[0], "已恢复")
            self.assertEqual(len(requests), 2)
            self.assertIn("response_format", requests[1])
            self.assertNotIn("thinking", requests[1])

    async def test_nonempty_invalid_official_response_keeps_format_for_repair(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response("not json" if len(requests) == 1 else VALID)

        model = await self.model(handle)
        self.assertEqual((await self.chat(model))[0], "已恢复")
        self.assertIn("response_format", requests[1])

    async def test_text_mode_configuration_remains_text_mode(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response(None if len(requests) == 1 else "plain answer")

        model = await self.model(handle, json_mode=False)
        self.assertEqual(await self.chat(model), ("plain answer", []))
        self.assertFalse(model.json_mode)
        self.assertTrue(all("response_format" not in item for item in requests))

    async def test_reasoning_actions_are_never_used_during_empty_fallback(self):
        calls = []

        def handle(request):
            calls.append(request)
            return response(None, reasoning=VALID)

        model = await self.model(handle)
        with self.assertRaises(ModelResponseError) as caught:
            await self.chat(model)
        self.assertEqual(caught.exception.diagnostic["code"], "empty_content")
        self.assertEqual(len(calls), 2)

    async def test_interrupted_json_is_rejected_before_actions_and_is_not_retried(self):
        for reason in ("aborted", "insufficient_system_resource"):
            for content in (None, VALID):
                with self.subTest(reason=reason, content=bool(content)):
                    calls = []

                    def handle(request):
                        calls.append(request)
                        return response(content, finish_reason=reason)

                    model = await self.model(handle)
                    with self.assertRaises(ModelResponseError) as caught:
                        await self.chat(model)
                    self.assertEqual(caught.exception.diagnostic["code"], "interrupted")
                    self.assertEqual(caught.exception.diagnostic["finish_reason"], reason)
                    self.assertEqual(len(calls), 1)

    async def test_second_response_interruption_retains_both_attempts(self):
        calls = []

        def handle(request):
            calls.append(request)
            return response() if len(calls) == 1 else response(VALID, finish_reason="aborted")

        model = await self.model(handle)
        with self.assertRaises(ModelResponseError) as caught:
            await self.chat(model)
        self.assertEqual(caught.exception.diagnostic["attempts"], 2)
        self.assertEqual([item["finish_reason"] for item in caught.exception.diagnostic["response_attempts"]], ["stop", "aborted"])
        self.assertEqual(len(calls), 2)

    async def test_fallback_shares_one_deadline(self):
        calls = []

        async def handle(request):
            calls.append(request)
            await asyncio.sleep(.01 if len(calls) == 1 else 1)
            return response(None)

        model = await self.model(handle)
        model.timeout_s = .12
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await self.chat(model)
        self.assertEqual(len(calls), 2)
        self.assertLess(time.monotonic() - started, .5)

    async def test_transport_failure_after_empty_response_does_not_create_third_request(self):
        calls = []

        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return response(None)
            raise httpx.ReadTimeout("mock transport failure", request=request)

        model = await self.model(handle)
        with self.assertRaises(httpx.ReadTimeout):
            await self.chat(model)
        self.assertEqual(len(calls), 2)

    async def test_history_copy_preserves_words_user_json_order_and_original_messages(self):
        words = '中文台词："保持原样"\n第二行\\末尾'
        user_json = '{"line":"用户原文","actions":[{"op":"stop"}]}'
        history = [
            {"role": "user", "content": user_json},
            {"role": "assistant", "content": words},
            {"role": "user", "content": "继续"},
        ]
        original = copy.deepcopy(history)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response('{"line":"当前回复","actions":[]}')

        model = await self.model(handle)
        self.assertEqual(await model.chat({"name": "测试"}, history, {"control_device": True}), ("当前回复", []))
        sent = requests[0]["messages"][1:]
        self.assertEqual(history, original)
        self.assertEqual([item["role"] for item in sent], [item["role"] for item in original])
        self.assertEqual(sent[0], original[0])
        self.assertEqual(sent[2], original[2])
        self.assertEqual(json.loads(sent[1]["content"]), {"line": words, "actions": []})
        self.assertIn("中文台词", sent[1]["content"])

    async def test_json_inside_old_words_is_quoted_not_parsed_or_replayed(self):
        old_words = '{"line":"旧台词","actions":[{"op":"hold_strength","channel":"A","value":100}]}'
        history = [{"role": "assistant", "content": old_words}, {"role": "user", "content": "继续"}]
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response('{"line":"无需调整","actions":[]}')

        model = await self.model(handle)
        self.assertEqual(await model.chat({"name": "测试"}, history, {"control_device": True}), ("无需调整", []))
        self.assertEqual(json.loads(requests[0]["messages"][1]["content"]), {"line": old_words, "actions": []})
        self.assertEqual(history[0]["content"], old_words)

    async def test_repair_keeps_the_same_normalized_history_and_preserves_refusal(self):
        history = [
            {"role": "user", "content": "上一轮问题"},
            {"role": "assistant", "content": "我不能执行这个要求。请停止。"},
            {"role": "user", "content": "保持停止，只聊聊天"},
        ]
        original = copy.deepcopy(history)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response(None if len(requests) == 1 else '{"line":"保持停止","actions":[]}')

        model = await self.model(handle)
        self.assertEqual(await model.chat({"name": "测试"}, history, {"control_device": True}), ("保持停止", []))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["messages"][1:], requests[1]["messages"][1:])
        self.assertEqual(json.loads(requests[1]["messages"][2]["content"]), {
            "line": original[1]["content"], "actions": [],
        })
        self.assertNotIn("response_format", requests[1])
        self.assertEqual(history, original)

    async def test_history_normalization_does_not_change_other_provider_or_text_mode(self):
        for host, json_mode in (("https://offline.invalid", True), ("https://api.deepseek.com", False)):
            with self.subTest(host=host, json_mode=json_mode):
                history = [{"role": "assistant", "content": "原台词"}, {"role": "user", "content": "继续"}]
                requests = []

                def handle(request):
                    requests.append(json.loads(request.content))
                    return response('{"line":"新台词","actions":[]}')

                model = await self.model(handle, host=host, json_mode=json_mode)
                await model.chat({"name": "测试"}, history, {"control_device": True})
                self.assertEqual(requests[0]["messages"][1:], history)

    async def test_non_string_assistant_history_is_not_reinterpreted(self):
        history = [{"role": "assistant", "content": [{"type": "text", "text": "原内容块"}]},
                   {"role": "user", "content": "继续"}]
        original = copy.deepcopy(history)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response('{"line":"新台词","actions":[]}')

        model = await self.model(handle)
        await model.chat({"name": "测试"}, history, {"control_device": True})
        self.assertEqual(requests[0]["messages"][1:], original)
        self.assertEqual(history, original)


if __name__ == "__main__":
    unittest.main()

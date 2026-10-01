"""Detached chat acceptance and role-deletion edge cases, never physical I/O."""
import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock

import httpx

from backend.tests.test_chat_modes import fake_backend
from backend.tests.test_role_management import isolated_roles_app


class ReceiptEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def completed(self, client, request_id):
        async def poll():
            while True:
                response = await client.get(f"/api/chat/result/{request_id}")
                response.raise_for_status()
                if response.json()["status"] == "completed":
                    return response.json()["result"]
                await asyncio.sleep(.005)
        return await asyncio.wait_for(poll(), 2)

    def request(self, runtime, message):
        return {"request_id": f"{runtime.chat_requests.session_id}:{uuid.uuid4()}", "message": message, "mode": "auto"}

    async def test_rapid_distinct_requests_preserve_order_and_duplicate_id_never_overlaps_model(self):
        with isolated_roles_app() as (_, app, _, _):
            runtime = app.state.runtime
            entered, release = asyncio.Event(), asyncio.Event()
            active = peak = 0
            calls = []
            async def chat(character, history, state, **kwargs):
                nonlocal active, peak
                active += 1; peak = max(peak, active)
                calls.append(history[-1]["content"])
                try:
                    if len(calls) == 1:
                        entered.set(); await release.wait()
                    return f"回复{len(calls)}", []
                finally:
                    active -= 1
            runtime.llm.chat = chat
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
                    payloads = [self.request(runtime, message) for message in ("第一条", "第二条", "第三条")]
                    self.assertEqual((await client.post("/api/chat", json=payloads[0])).status_code, 202)
                    await asyncio.wait_for(entered.wait(), 2)
                    for payload in (payloads[1], payloads[2], payloads[0], payloads[1]):
                        self.assertEqual((await client.post("/api/chat", json=payload)).status_code, 202)
                    self.assertEqual(calls, ["第一条"])
                    release.set()
                    results = [await self.completed(client, payload["request_id"]) for payload in payloads]
                    self.assertEqual([result["line"] for result in results], ["回复1", "回复2", "回复3"])
                    self.assertEqual(calls, ["第一条", "第二条", "第三条"])
                    self.assertEqual(peak, 1)
            finally:
                release.set()
                await runtime.chat_requests.close()
                await runtime.llm.client.aclose()

    async def test_accepted_receipt_delayed_past_estop_and_resume_cannot_send_old_actions(self):
        with isolated_roles_app() as (_, app, _, _):
            runtime = app.state.runtime
            backend = fake_backend(ready=True)
            backend.apply = AsyncMock(return_value=True)
            backend.start_pulse_hold = AsyncMock(return_value=True)
            backend.loops_active.return_value = {"A": True, "B": True}
            runtime._attach_backend(backend)
            runtime.safety.dry_run = False
            runtime.llm.chat = AsyncMock(return_value=("旧消息只回复文字", [{"op": "hold_strength", "channel": "A", "value": 9}]))
            gate = asyncio.Event()
            original_run = runtime.chat_requests._run
            async def delayed_run(request_id, operation):
                await gate.wait()
                return await original_run(request_id, operation)
            runtime.chat_requests._run = delayed_run
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
                    payload = self.request(runtime, "在急停前提交的消息")
                    self.assertEqual((await client.post("/api/chat", json=payload)).status_code, 202)
                    self.assertEqual((await client.post("/api/estop")).status_code, 200)
                    self.assertEqual((await client.post("/api/resume")).status_code, 200)
                    backend.apply.reset_mock()
                    backend.start_pulse_hold.reset_mock()
                    gate.set()
                    result = await self.completed(client, payload["request_id"])
                    self.assertEqual(result["executed"], [])
                    backend.apply.assert_not_awaited()
                    backend.start_pulse_hold.assert_not_awaited()
                    self.assertEqual(runtime.safety.current, {"A": 0, "B": 0})
            finally:
                gate.set()
                await runtime.chat_requests.close()
                await runtime.llm.client.aclose()

    async def test_active_role_delete_keeps_role_when_physical_stop_not_confirmed(self):
        with isolated_roles_app() as (root, app, role, _):
            runtime = app.state.runtime
            backend = fake_backend(ready=True)
            backend.apply = AsyncMock(return_value=False)
            runtime._attach_backend(backend)
            runtime.safety.dry_run = False
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
                    switched = await client.post("/api/character/profile", json={"role": role, "profile": "角色扮演"})
                    self.assertEqual(switched.status_code, 200)
                    registry = root / "config" / "custom_roles.yaml"
                    before = registry.read_bytes()
                    response = await client.post("/api/character/delete", json={"role": role})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("未能确认设备停止", response.json()["error"])
                    self.assertEqual(runtime.cfg["character"]["role"], role)
                    self.assertEqual(registry.read_bytes(), before)
                    self.assertTrue(runtime.safety.estop_active)
                    self.assertFalse(runtime.loop.autopilot)
            finally:
                await runtime.chat_requests.close()
                await runtime.llm.client.aclose()


if __name__ == "__main__":
    unittest.main()

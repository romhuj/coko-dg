"""Exercise uncertain delivery without a model or physical device."""
import asyncio
import unittest
import uuid

from backend.chat_requests import ChatRequests, ReceiptError


class ChatReceiptTests(unittest.IsolatedAsyncioTestCase):
    def new_id(self, ledger):
        return f"{ledger.session_id}:{uuid.uuid4()}"

    async def test_lost_response_retry_does_not_repeat_operation(self):
        ledger = ChatRequests()
        request_id = self.new_id(ledger)
        gate, calls = asyncio.Event(), []
        async def operation():
            calls.append("device action")
            await gate.wait()
            return {"line": "done", "executed": [{"sent": True}]}
        self.assertEqual(ledger.submit(request_id, {"message": "hello"}, operation)["status"], "pending")
        await asyncio.sleep(0)
        self.assertEqual(ledger.submit(request_id, {"message": "hello"}, operation)["status"], "pending")
        gate.set()
        await asyncio.sleep(0)
        result = ledger.submit(request_id, {"message": "hello"}, operation)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["line"], "done")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ReceiptError) as error:
            ledger.submit(request_id, {"message": "changed"}, operation)
        self.assertEqual(error.exception.status, 409)
        await ledger.close()


class ChatReceiptApiTests(unittest.IsolatedAsyncioTestCase):
    def new_id(self, ledger):
        return f"{ledger.session_id}:{uuid.uuid4()}"

    async def test_http_accepted_poll_and_retry_survive_detached_request(self):
        import httpx
        from backend.tests.test_role_management import isolated_roles_app
        with isolated_roles_app() as (_, app, _, _):
            runtime = app.state.runtime
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def delayed_model(*args, **kwargs):
                calls.append(True)
                entered.set()
                await release.wait()
                return "收到", []
            runtime.llm.chat = delayed_model
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
                session = (await client.get("/api/state")).json()["chat_session_id"]
                request_id = f"{session}:{uuid.uuid4()}"
                payload = {"message": "你好", "mode": "text", "request_id": request_id}
                response = await client.post("/api/chat", json=payload)
                self.assertEqual(response.status_code, 202)
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual((await client.post("/api/chat", json=payload)).status_code, 202)
                self.assertEqual((await client.post("/api/chat", json={**payload, "message": "不同"})).status_code, 409)
                release.set()
                for _ in range(100):
                    response = await client.get(f"/api/chat/result/{request_id}")
                    if response.json()["status"] == "completed": break
                    await asyncio.sleep(.01)
                self.assertEqual(response.json()["result"]["line"], "收到")
                self.assertEqual((await client.post("/api/chat", json=payload)).status_code, 200)
                self.assertEqual(len(calls), 1)
            await runtime.chat_requests.close()
            await runtime.llm.client.aclose()

    async def test_expired_or_previous_session_ids_never_execute_again(self):
        ledger = ChatRequests(max_results=1)
        calls = []
        async def operation():
            calls.append(True)
            return {"line": "ok"}
        first = self.new_id(ledger)
        for request_id in (first, self.new_id(ledger)):
            ledger.submit(request_id, {}, operation)
            await asyncio.sleep(0)
        with self.assertRaises(ReceiptError) as expired:
            ledger.submit(first, {}, operation)
        self.assertEqual(expired.exception.status, 410)
        restarted = ChatRequests()
        with self.assertRaises(ReceiptError) as foreign:
            restarted.submit(first, {}, operation)
        self.assertEqual(foreign.exception.status, 409)
        self.assertEqual(len(calls), 2)
        await ledger.close()

    async def test_capacity_shutdown_and_exception_are_definitive(self):
        ledger = ChatRequests(max_active=1, max_seen=2)
        gate = asyncio.Event()
        request_id = self.new_id(ledger)
        ledger.submit(request_id, {}, gate.wait)
        await asyncio.sleep(0)
        with self.assertRaises(ReceiptError) as full:
            ledger.submit(self.new_id(ledger), {}, gate.wait)
        self.assertEqual(full.exception.status, 429)
        await ledger.close()
        self.assertFalse(ledger.get(request_id)["result"]["retryable"])
        ledger = ChatRequests()
        async def partial_failure():
            raise RuntimeError("PRIVATE_ERROR")
        request_id = self.new_id(ledger)
        ledger.submit(request_id, {}, partial_failure)
        await asyncio.sleep(0)
        result = ledger.get(request_id)["result"]
        self.assertFalse(result["retryable"])
        self.assertNotIn("PRIVATE_ERROR", result["error"])
        await ledger.close()

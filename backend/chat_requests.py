"""Session-scoped receipts: a lost HTTP response must never repeat device actions."""
import asyncio
from collections import OrderedDict
import hashlib
import json
import re
import uuid


class ReceiptError(ValueError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class ChatRequests:
    def __init__(self, *, max_active=8, max_results=128, max_seen=4096):
        self.session_id = uuid.uuid4().hex
        self.max_active, self.max_results, self.max_seen = max_active, max_results, max_seen
        self._seen = {}
        self._tasks = {}
        self._results = OrderedDict()
        self._closed = False

    @property
    def has_pending(self):
        return bool(self._tasks)

    def _validate(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}:[a-f0-9-]{36}", request_id):
            raise ReceiptError("请求编号无效，请重新输入消息", 400)
        if request_id.split(":", 1)[0] != self.session_id:
            raise ReceiptError("应用已重新启动，无法确认上次结果；请先检查聊天和设备状态", 409)

    def get(self, request_id):
        self._validate(request_id)
        if request_id in self._results:
            return {"status": "completed", "request_id": request_id, "result": self._results[request_id]}
        if request_id in self._tasks:
            return {"status": "pending", "request_id": request_id}
        if request_id in self._seen:
            raise ReceiptError("上次结果已过期，请先检查聊天和设备状态；此请求不会重复执行", 410)
        raise ReceiptError("尚未收到此请求", 404)

    def submit(self, request_id, payload, operation):
        self._validate(request_id)
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if request_id in self._seen:
            if self._seen[request_id] != fingerprint:
                raise ReceiptError("此请求编号已用于另一条消息", 409)
            return self.get(request_id)
        if self._closed or len(self._tasks) >= self.max_active or len(self._seen) >= self.max_seen:
            raise ReceiptError("当前请求较多，请稍后再发送", 429)
        self._seen[request_id] = fingerprint
        self._tasks[request_id] = asyncio.create_task(self._run(request_id, operation))
        return self.get(request_id)

    async def _run(self, request_id, operation):
        result = {"line": "", "executed": [], "dropped": [], "error": "请求已中断，请检查设备状态", "retryable": False}
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            result = {"line": "", "executed": [], "dropped": [], "error": "处理失败，部分操作可能已执行，请检查设备状态", "retryable": False}
        finally:
            self._results[request_id] = result
            self._tasks.pop(request_id, None)
            while len(self._results) > self.max_results:
                self._results.popitem(last=False)

    async def close(self):
        self._closed = True
        pending = list(self._tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # A task cancelled before its first step cannot enter _run's finally.
        for request_id in list(self._tasks):
            self._results[request_id] = {"line": "", "executed": [], "dropped": [], "error": "请求已中断，请检查设备状态", "retryable": False}
            self._tasks.pop(request_id, None)
        while len(self._results) > self.max_results:
            self._results.popitem(last=False)

# -*- coding: utf-8 -*-
"""中继客户端：连接 dglab-websocket-server v4，维护配对与设备状态，自动重连。

协议要点（已核对源码）：
- 控制方连 ws://host:9998，收 {"type":"hello","clientId":...}；
- 被控方（DG-LAB 4 APP）用 ?tid=控制方clientId 接入，控制方收 client_attached；
- 控制方下发 {"type":"message","clientId":被控方ID,"data":{RPC}}；
- APP 上行：devices.snapshot（设备列表）、slots.patch（强度/状态）、custom.action（反馈按钮）。
"""
import asyncio
import json
import logging
import time

import websockets

from .delivery import Delivery

logger = logging.getLogger("ai-for-coyote.relay")


def _deep_merge(base: dict, patch: dict) -> dict:
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class RelayClient:
    def __init__(
        self,
        url: str,
        reconnect_delay_s: float = 3,
        on_event=None,   # async fn(event: str, payload: dict)
        on_action=None,  # async fn(action: int, client_id: str)
    ) -> None:
        self.url = url
        self.reconnect_delay_s = reconnect_delay_s
        self.on_event = on_event
        self.on_action = on_action

        self.ws = None
        self.status = "disconnected"      # disconnected/connecting/waiting/paired
        self.controller_id: str | None = None
        self.clients: dict[str, dict] = {}  # clientId -> {devices, props, slotState}
        self.last_error: str = ""
        self._pending_rpc = {}  # (clientId, reqId) -> future/timer/callback metadata

    # ---------- 状态 ----------
    def get_slot_id(self, client_id: str | None = None) -> str | None:
        """取设备 slotId（默认优先选有真实设备的被控方）。"""
        if client_id is None:
            cid = self.first_client_id()
        else:
            cid = client_id
        client = self.clients.get(cid or "")
        if not client:
            return None
        devices = client.get("devices") or []
        return devices[0]["slotId"] if devices else None

    def first_client_id(self) -> str | None:
        """取第一个被控方 ID；优先选择暴露了设备（有 slotId）的。"""
        for cid, client in self.clients.items():
            if client.get("devices"):
                return cid
        return next(iter(self.clients), None)

    def to_state(self) -> dict:
        paired = []
        for cid, client in self.clients.items():
            paired.append(
                {
                    "clientId": cid,
                    "slotId": self.get_slot_id(cid),
                    "devices": [
                        {"slotId": d.get("slotId"), "name": d.get("name"),
                         "type": d.get("type")}
                        for d in (client.get("devices") or [])
                    ],
                    "props": client.get("props", {}),
                    "slotState": client.get("slotState", {}),
                }
            )
        return {
            "status": self.status,
            "controller_id": self.controller_id,
            "url": self.url,
            "clients": paired,
            "last_error": self.last_error,
        }

    # ---------- 连接与收发 ----------
    async def run(self) -> None:
        """常驻任务：连接 + 自动重连。"""
        while True:
            try:
                self.status = "connecting"
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, open_timeout=10
                ) as ws:
                    self.ws = ws
                    logger.info("已连接中继服务器 %s", self.url)
                    await self._recv_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.warning("中继连接失败/断开: %s，%ss 后重连", exc, self.reconnect_delay_s)
            finally:
                self._fail_pending("中继连接已断开，设备结果未确认")
                self.ws = None
                self.controller_id = None
                self.status = "disconnected"
                self.clients.clear()
                await self._emit("disconnected", {"error": self.last_error})
            await asyncio.sleep(self.reconnect_delay_s)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._handle_frame(frame)

    def _handle_frame(self, frame: dict) -> None:
        ftype = frame.get("type")
        if ftype == "hello":
            self.controller_id = frame.get("clientId")
            self.last_error = ""
            self.status = "waiting"
            logger.info("拿到控制方 ID: %s", self.controller_id)
            asyncio.create_task(self._emit("hello", frame))
        elif ftype == "client_attached":
            cid = frame.get("clientId")
            self.clients.setdefault(cid, {"devices": [], "props": {}, "slotState": {}})
            self.status = "paired"
            logger.info("APP 被控方接入: %s", cid)
            asyncio.create_task(self._emit("client_attached", frame))
        elif ftype == "client_disconnected":
            cid = frame.get("clientId")
            self._fail_pending("设备连接已断开，执行结果未确认", cid)
            self.clients.pop(cid, None)
            if not self.clients:
                self.status = "waiting"
            logger.info("APP 被控方断开: %s", cid)
            asyncio.create_task(self._emit("client_disconnected", frame))
        elif ftype == "error":
            self.last_error = frame.get("code", "") + " " + str(frame.get("message", ""))
            logger.warning("服务器错误帧: %s", self.last_error)
            asyncio.create_task(self._emit("error", frame))
        elif ftype == "message":
            self._handle_message(frame)
        # heartbeat / pong / idle_timeout 忽略或已由上层处理

    def _handle_message(self, frame: dict) -> None:
        cid = frame.get("clientId") or self.first_client_id()
        data = frame.get("data")
        if not isinstance(data, dict) or cid is None:
            return
        # 只处理已知被控方的消息，避免产生幽灵客户端
        client = self.clients.get(cid)
        if client is None:
            return

        if data.get("t") == "ev":
            ev = data.get("ev")
            if ev == "devices.snapshot":
                client["devices"] = list(data.get("devices") or [])
                logger.info("设备快照: %s", [d.get("name") for d in client["devices"]])
                asyncio.create_task(self._emit("devices_snapshot", data))
            elif ev == "devices.patch":
                added = data.get("added") or []
                removed = set(data.get("removed") or [])
                client["devices"] = [
                    d for d in client["devices"] if d.get("slotId") not in removed
                ] + list(added)
                asyncio.create_task(self._emit("devices_patch", data))
            elif ev == "slots.patch":
                for slot in data.get("slots") or []:
                    sid = slot.get("slotId")
                    if not sid:
                        continue
                    props = _deep_merge(client["props"], slot.get("props") or {})
                    slot_state = _deep_merge(client["slotState"], slot.get("slotState") or {})
                    client["props"], client["slotState"] = props, slot_state
                    # 设备当前是这台 slot 时，直接给 props 打上 slotId 方便安全层读取
                    props.setdefault("slotId", sid)
                asyncio.create_task(self._emit("slots_patch", {**data, "_client_id": cid, "_received_at": time.monotonic()}))
            elif ev == "custom.action":
                action = data.get("action")
                logger.info("收到 APP 反馈按钮: %s (来自 %s)", action, cid)
                asyncio.create_task(self._emit("custom_action", data))
                if self.on_action:
                    asyncio.create_task(self.on_action(action, cid))
        elif data.get("t") == "resp":
            result = data.get("result")
            err = data.get("error")
            if err:
                logger.warning("RPC %s 失败: %s", data.get("reqId"), err)
            else:
                logger.debug("RPC %s 完成: %s", data.get("reqId"), result)
            key = (cid, data.get("reqId"))
            pending = self._pending_rpc.get(key)
            if pending:
                if "error" in data and data["error"] is not None:
                    receipt = Delivery("failed", True, "设备拒绝命令：" + str(data["error"])[:160])
                elif "result" not in data:
                    receipt = Delivery("unconfirmed", True, "设备返回了不完整的执行回执")
                elif pending["method"] == "device.op" and (not isinstance(result, dict) or result.get("reason") != "completed"):
                    reason = result.get("reason", "unknown") if isinstance(result, dict) else "unknown"
                    receipt = Delivery("failed", True, "设备任务未完成：" + str(reason)[:60])
                else:
                    receipt = Delivery("confirmed", True, "设备已确认")
                self._resolve_rpc(key, receipt, data)

    def _resolve_rpc(self, key, receipt, response=None):
        pending = self._pending_rpc.pop(key, None)
        if pending is None:
            return  # Late/duplicate replies never revive an expired operation.
        pending["timer"].cancel()
        if not pending["future"].done():
            pending["future"].set_result(receipt)
        callback = pending["callback"]
        if callback:
            try:
                callback(receipt, response)
            except Exception:
                logger.exception("设备回执回调失败")

    def _fail_pending(self, reason, client_id=None):
        for key, pending in list(self._pending_rpc.items()):
            if client_id is None or key[0] == client_id:
                self._resolve_rpc(key, Delivery("unconfirmed", pending["written"], reason))

    async def send_rpc(self, frame, *, timeout_s=2.0, wait=True, on_result=None) -> Delivery:
        """Send once; match the APP response, never retry an uncertain command.

        Official protocol: https://github.com/dungeonlab-open/dglab-kit#v4-协议参考
        device.op responses arrive on task *completion*, not queue acceptance.
        Long waveform tasks therefore return `sent` after the write and resolve
        their callback later. Short one-shot commands wait for their response.
        """
        inner = frame.get("data", {})
        key = (frame.get("clientId"), inner.get("reqId"))
        if self.ws is None:
            return Delivery("failed", False, "中继未连接，命令未发送")
        if not all(isinstance(part, str) and part for part in key) or key in self._pending_rpc:
            return Delivery("failed", False, "RPC 请求编号无效或重复")
        if len(self._pending_rpc) >= 256:
            return Delivery("failed", False, "待确认的设备请求过多")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending = {"future": future, "callback": on_result, "written": False, "method": inner.get("m")}
        pending["timer"] = loop.call_later(max(.001, timeout_s), lambda: self._resolve_rpc(
            key, Delivery("unconfirmed", pending["written"], "设备回执超时，未重复发送")))
        self._pending_rpc[key] = pending
        try:
            # A stalled socket must not keep a safety operation waiting forever.
            wrote = await asyncio.wait_for(self.send_frame(frame), timeout=min(2.0, max(.001, timeout_s)))
            pending["written"] = bool(wrote)
            if not wrote:
                self._resolve_rpc(key, Delivery("failed", False, "设备命令发送失败"))
            if future.done():
                return future.result()
            if not wait:
                return Delivery("sent", True, "波形请求已发送，运行结束后才有完成回执")
            return await asyncio.shield(future)
        except asyncio.TimeoutError:
            self._resolve_rpc(key, Delivery("unconfirmed", pending["written"], "发送超时，结果未确认；未重复发送"))
            return future.result()
        except asyncio.CancelledError:
            pending = self._pending_rpc.pop(key, None)
            if pending:
                pending["timer"].cancel()
                pending["future"].cancel()
            raise

    async def send_frame(self, frame: dict) -> bool:
        """发送服务器帧；未连接返回 False。"""
        if self.ws is None:
            return False
        try:
            await self.ws.send(json.dumps(frame, ensure_ascii=False))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送失败: %s", exc)
            return False

    async def _emit(self, event: str, payload: dict) -> None:
        if self.on_event:
            try:
                await self.on_event(event, payload)
            except Exception:  # noqa: BLE001
                logger.exception("事件回调异常: %s", event)

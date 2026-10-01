# -*- coding: utf-8 -*-
"""现网 dglab-websocket-server v4 桥 adapter（T043 §B adapter 一）。

行为 = 改造前 game_loop.py 的发送/循环/归零逻辑 + RelayClient，零行为变化：
- 一次性命令（temp/hold/add/pulse/clear/stop）→ V4 RPC 帧发送；
- pulse_hold 循环波形 → 本机分批重发（不依赖 App 的 d=0，实测不可靠）；
- temp 归零定时器登记在本 adapter（stop()/clear/estop 路径可取消）。
"""
from __future__ import annotations

import asyncio
import logging

from ..device_ops import CHANNEL, DeviceOps
from ..relay_client import RelayClient
from ..safety import SafetyManager
from ..delivery import Delivery, combine
from .base import (
    STATUS_CONNECTING,
    STATUS_DISCONNECTED,
    STATUS_PAIRED,
    STATUS_READY,
    STATUS_WAITING,
    DeviceBackend,
)

logger = logging.getLogger("ai-for-coyote.device.dglab")


class DGLabRelayBackend(DeviceBackend):
    """dglab-websocket-server v4 桥。持有 RelayClient 并转发其中继事件。"""

    name = "dglab_relay"

    def __init__(self, cfg, safety: SafetyManager,
                 on_event=None, on_action=None) -> None:
        super().__init__()
        self.cfg = cfg
        self.safety = safety
        self.on_event = on_event      # async (event, payload)，原样转发中继事件
        self.on_action = on_action    # async (action, client_id)

        self.relay = RelayClient(
            str(cfg["relay"]["url"]),
            reconnect_delay_s=float(cfg["relay"].get("reconnect_delay_s", 3)),
            on_event=self._forward_event,
            on_action=self.on_action,
        )
        self.ops = DeviceOps()

        self._relay_task: asyncio.Task | None = None
        # 循环波形：channel -> (task, stop_event)
        self.loop_tasks: dict[str, asyncio.Task] = {}
        self.loop_events: dict[str, asyncio.Event] = {}
        # temp 归零任务：channel -> task
        self.revert_tasks: dict[str, asyncio.Task] = {}
        self.on_command_failure = None
        self.rpc_timeout_s = 2.0
        self._strength_pending = {}

    # ---------- 事件转发（附断开通知） ----------
    async def _forward_event(self, event: str, payload: dict) -> None:
        if event == "disconnected":
            self._notify_disconnect(payload)
        if event == "slots_patch" and payload.get("_client_id") == self.relay.first_client_id():
            slot_id = self.relay.get_slot_id()
            for slot in payload.get("slots", []):
                if slot.get("slotId") == slot_id:
                    self.safety.update_device_state(slot.get("props"), slot.get("slotState"),
                                                    fresh=True, received_at=payload.get("_received_at"))
        if self.on_event:
            await self.on_event(event, payload)

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self._relay_task is None or self._relay_task.done():
            self._relay_task = asyncio.create_task(self.relay.run())
            logger.info("dglab 中继后端启动：%s", self.relay.url)

    async def stop(self) -> None:
        self.stop_pulse_hold(None)
        self._cancel_revert(None)
        if self._relay_task:
            self._relay_task.cancel()
            self._relay_task = None

    # ---------- 一次性命令 ----------
    async def apply(self, cmd: dict) -> Delivery:
        client_id = self.relay.first_client_id()
        slot_id = self.relay.get_slot_id()
        if client_id is None or slot_id is None:
            return Delivery("failed", False, "设备未连接，命令未发送")
        kind = cmd.get("kind")
        ch = cmd.get("channel")
        channels = ("A", "B") if ch is None else (ch,)
        token = object()
        strength = kind in ("hold", "add", "temp")
        stopping = kind in ("clear", "stop")
        if (strength or kind == "pulse") and ch in self._strength_pending:
            return Delivery("failed", False, "该通道上一条强度命令仍在确认，未叠加发送")
        if strength or stopping:
            for channel in channels:
                self._strength_pending[channel] = token
        if kind in ("temp", "hold", "add", "clear", "stop"):
            self._cancel_revert(cmd.get("channel"))
        try:
            frames = self._build_frames(cmd, client_id, slot_id)
            receipt = combine(await self._send_all(frames))
            if (strength or stopping) and any(self._strength_pending.get(channel) is not token for channel in channels):
                return Delivery("unconfirmed", receipt.sent, "命令已被较新的停止操作取代", superseded=True)
            if kind == "temp" and receipt and not self.safety.estop_active:
                self._schedule_temp_revert(client_id, slot_id, cmd)
            return receipt
        finally:
            for channel in channels:
                if self._strength_pending.get(channel) is token:
                    self._strength_pending.pop(channel, None)

    async def _send_all(self, frames: list[dict]) -> list[Delivery]:
        # Frames are dispatched in order, and their response deadlines overlap:
        # clearing two channels must not cost N times the RPC timeout.
        return await asyncio.gather(*(self._send(frame) for frame in frames))

    async def _send(self, frame, on_result=None) -> Delivery:
        data = frame.get("data", {}).get("data", {})
        waveform = frame.get("data", {}).get("m") == "device.op" and data.get("t") == 0
        timeout = max(self.rpc_timeout_s, float(data.get("d", 0)) / 1000 + 5) if waveform else self.rpc_timeout_s
        return await self.relay.send_rpc(frame, timeout_s=timeout, wait=not waveform, on_result=on_result)

    def _build_frames(self, cmd: dict, client_id: str | None, slot_id: str | None) -> list[dict]:
        """内部命令 -> V4 服务器帧列表（复刻原 game_loop 逻辑）。"""
        frames: list[dict] = []
        kind = cmd["kind"]
        if client_id is None or slot_id is None:
            return frames
        ch = CHANNEL.get(cmd.get("channel"))
        ch_name = cmd.get("channel")
        current = cmd.get("_current", self.safety.current)
        if kind == "temp":
            # 爆发：加差值到目标，到时自动归零（归零由 _schedule_temp_revert 负责）
            delta = cmd["value"] - self.safety.current[ch_name]
            if delta:
                frames.append(self.ops.add_strength(client_id, slot_id, ch, delta))
        elif kind == "hold":
            # 持续强度：加差值到目标（AddIntensity 是实测可靠的原语）
            delta = cmd["value"] - self.safety.current[ch_name]
            if delta or cmd.get("_force_send"):
                frames.append(self.ops.add_strength(client_id, slot_id, ch, delta))
        elif kind == "add":
            frames.append(self.ops.add_strength(client_id, slot_id, ch, cmd["delta"]))
        elif kind == "pulse":
            # 波形按帧消费（每帧 100ms），帧播完即停；tiling 补齐到请求的时长
            base = cmd["frames"]
            total = max(1, int(round(cmd["duration_s"] * 10)))
            tiled = (base * (total // len(base) + 1))[:total] if base else []
            frames.append(
                self.ops.pulse(
                    client_id, slot_id, ch, tiled,
                    int(cmd["duration_s"] * 1000), immediate=True,
                )
            )
        elif kind == "clear":
            frames.append(
                self.ops.clear(client_id, slot_id, None if cmd["channel"] is None else ch)
            )
            # 用可靠的 AddIntensity 负值归零，另发 reset 兜底
            if cmd["channel"] is None:
                for c in ("A", "B"):
                    if current[c]:
                        frames.append(
                            self.ops.add_strength(
                                client_id, slot_id, CHANNEL[c], -current[c]
                            )
                        )
                    frames.append(self.ops.reset_intensity(client_id, slot_id, CHANNEL[c]))
            else:
                if current[ch_name]:
                    frames.append(
                        self.ops.add_strength(
                            client_id, slot_id, ch, -current[ch_name]
                        )
                    )
                frames.append(self.ops.reset_intensity(client_id, slot_id, ch))
        elif kind == "stop":
            frames.append(self.ops.clear(client_id, slot_id))
            for c in ("A", "B"):
                if current[c]:
                    frames.append(
                        self.ops.add_strength(
                            client_id, slot_id, CHANNEL[c], -current[c]
                        )
                    )
                frames.append(self.ops.reset_intensity(client_id, slot_id, CHANNEL[c]))
        return frames

    # ---------- temp 归零 ----------
    def _cancel_revert(self, ch_name: str | None) -> None:
        names = [ch_name] if ch_name else list(self.revert_tasks)
        for name in names:
            task = self.revert_tasks.pop(name, None)
            if task:
                task.cancel()

    def _schedule_temp_revert(
        self, client_id: str, slot_id: str, cmd: dict
    ) -> None:
        """爆发时长结束后自动归零（AddIntensity 负值 + reset 兜底）。"""
        ch = CHANNEL.get(cmd["channel"])
        ch_name = cmd["channel"]
        duration_s = float(cmd["duration_s"])

        async def revert() -> None:
            owner = asyncio.current_task()
            try:
                await asyncio.sleep(duration_s)
            except asyncio.CancelledError:
                return
            def current():
                return (self.revert_tasks.get(ch_name) is owner and not self.safety.estop_active
                        and self.relay.first_client_id() == client_id and self.relay.get_slot_id() == slot_id)
            if not current():
                return
            value = self.safety.current[ch_name]
            frames = []
            if value:
                frames.append(self.ops.add_strength(client_id, slot_id, ch, -value))
            frames.append(self.ops.reset_intensity(client_id, slot_id, ch))
            receipt = combine(await self._send_all(frames))
            if not current():
                return
            if receipt:
                self.safety.record({"kind": "zero", "channel": ch_name})
                logger.info("爆发结束，%s 通道自动归零", ch_name)
            else:
                self.safety.mark_uncertain(ch_name)
                self.stop_pulse_hold(ch_name)
                if self.on_command_failure:
                    self.on_command_failure({"kind": "zero", "channel": ch_name}, receipt)
            if self.revert_tasks.get(ch_name) is owner:
                self.revert_tasks.pop(ch_name, None)

        self.revert_tasks[ch_name] = asyncio.create_task(revert())

    # ---------- 循环波形 ----------
    async def start_pulse_hold(self, ch_name: str, cmd: dict) -> Delivery:
        client_id = self.relay.first_client_id()
        slot_id = self.relay.get_slot_id()
        if client_id is None or slot_id is None:
            return Delivery("failed", False, "设备未连接，波形未发送")
        if ch_name in self._strength_pending:
            return Delivery("failed", False, "该通道强度仍在确认，波形未发送")
        self.stop_pulse_hold(ch_name)
        ch = CHANNEL.get(ch_name)
        base = cmd["frames"]
        playback = self.cfg["playback"]
        frame_s = float(playback["frame_ms"]) / 1000.0
        natural = max(len(base) * frame_s, 0.1)
        batch_s = max(float(playback["loop_batch_s"]), natural)
        mult = max(1, round(batch_s / natural))
        batch_s = natural * mult
        overlap = min(float(playback["loop_overlap_s"]), batch_s * 0.5)
        total = max(1, int(round(batch_s * 10)))
        tiled = (base * (total // len(base) + 1))[:total] if base else []
        wait_s = max(0.1, batch_s - overlap)

        stop_event = asyncio.Event()
        self.loop_events[ch_name] = stop_event

        def active():
            return self.loop_events.get(ch_name) is stop_event and not stop_event.is_set() and not self.safety.estop_active

        def failed(receipt, response=None):
            result = (response or {}).get("result")
            # Our next unique batch intentionally replaces the previous batch.
            if isinstance(result, dict) and result.get("reason") == "replaced":
                return
            if not receipt and active():
                self.stop_pulse_hold(ch_name)
                if self.on_command_failure:
                    self.on_command_failure(cmd, receipt)

        async def send_batch():
            # Reusing reqId while the prior batch is running is a protocol error.
            frame = self.ops.pulse(client_id, slot_id, ch, tiled, int(batch_s * 1000), immediate=True)
            return await self._send(frame, failed)

        # This await is the first-write barrier, not a 30-second completion wait.
        try:
            first = await send_batch()
        except asyncio.CancelledError:
            if self.loop_events.get(ch_name) is stop_event:
                self.stop_pulse_hold(ch_name)
            raise
        if not first or not active():
            if self.loop_events.get(ch_name) is stop_event:
                self.stop_pulse_hold(ch_name)
            return first if not first else Delivery("unconfirmed", first.sent, "波形启动已被停止取消")

        async def worker() -> None:
            try:
                while active():
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
                        break
                    except asyncio.TimeoutError:
                        pass
                    if not active():
                        break
                    receipt = await send_batch()
                    if not receipt:
                        failed(receipt)
                        break
            finally:
                if self.loop_events.get(ch_name) is stop_event:
                    self.loop_events.pop(ch_name, None)
                    self.loop_tasks.pop(ch_name, None)
                logger.info("%s 通道循环波形结束", ch_name)

        self.loop_tasks[ch_name] = asyncio.create_task(worker())
        return first

    def stop_pulse_hold(self, ch_name: str | None = None) -> None:
        # Disconnect and ready=false emergency paths also enter here, without
        # an apply(clear). Their old temp timers must never survive a reconnect.
        self._cancel_revert(ch_name)
        names = [ch_name] if ch_name else list(self.loop_events)
        for name in names:
            event = self.loop_events.pop(name, None)
            if event:
                event.set()
            task = self.loop_tasks.pop(name, None)
            if task:
                task.cancel()
        if ch_name and ch_name in self.safety.pulse_until:
            self.safety.pulse_until[ch_name] = 0.0

    def loops_active(self) -> dict:
        return {ch: ch in self.loop_tasks and not self.loop_tasks[ch].done() for ch in ("A", "B")}

    def strength_pending(self, channel: str) -> bool:
        return channel in self._strength_pending

    # ---------- 状态 ----------
    def ready(self) -> bool:
        return bool(self.relay.first_client_id() and self.relay.get_slot_id())

    def controller_id(self) -> str | None:
        return self.relay.controller_id

    def client_state(self) -> dict | None:
        cid = self.relay.first_client_id()
        return self.relay.clients.get(cid or "") if cid else None

    def _map_status(self, s: str) -> str:
        # 现网 relay 状态对齐 DeviceBackend 语义；paired ≈ 可发
        if s == "paired":
            return STATUS_PAIRED
        if s in ("waiting",):
            return STATUS_WAITING
        if s == "connecting":
            return STATUS_CONNECTING
        return STATUS_DISCONNECTED

    def to_state(self) -> dict:
        st = self.relay.to_state()
        st["status"] = self._map_status(st.get("status", STATUS_DISCONNECTED))
        st["backend"] = self.name
        st["scanned"] = []
        # ready 语义：dglab 下 paired 即视为可发（前端仍读 status/controller_id）
        if st["status"] == STATUS_PAIRED:
            st["status"] = STATUS_PAIRED
        return st

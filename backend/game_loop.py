# -*- coding: utf-8 -*-
"""闭环决策：用户消息 -> 模型台词+指令 -> 安全校验 -> 执行 -> 记录反馈。

这是阶段 1 的核心闭环；阶段 3 的摄像头观察循环也复用同一执行通道。

强度模型（实测结论）：DG-LAB 4 App 对 SetTempIntensity(d=0) 支持不可靠，
AddIntensity（相对增减）是最可靠的原语。因此所有强度命令都换算成
「AddIntensity(目标 - 当前)」实现绝对控制，设备上报值即最终强度。
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

import httpx

from .config import reload_character
from .safety import SafetyManager
from .ui_en import describe_en, reason_en
from .delivery import Delivery, delivery
from .safety_intent import stop_intent

logger = logging.getLogger("ai-for-coyote.game")


def _friendly_llm_error(exc: Exception) -> str:
    """把模型接口错误翻译成人话（中转站/配置常见坑）。"""
    err = str(exc)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "模型响应超时，本轮未执行新的设备动作。请稍后重试，或检查网络与模型设置。"
    if isinstance(exc, httpx.NetworkError):
        return "模型网络连接失败，本轮未执行新的设备动作。请检查网络后重试。"
    if "401" in err:
        return (
            "API Key 无效或未填：官方与中转站的密钥不通用，请确认 Base URL 与密钥配套；"
            "可在设置页点「测试连接」验证。"
        )
    if "400" in err:
        return "模型接口返回 400（参数不被支持，中转站常见）：请核对模型名，或关闭 JSON 模式后重试。"
    return f"模型调用失败：{err}。请检查 API 配置与网络。"


class _AutomaticTurnYielded(Exception):
    """A pending user turn superseded model generation before any actions."""


class GameLoop:
    def __init__(self, cfg, llm, safety: SafetyManager, backend, camera=None, audio=None) -> None:
        """backend: backend.device 的 DeviceBackend（默认 dglab_relay，零行为变化）。"""
        self.cfg = cfg
        self.llm = llm
        self.safety = safety
        self.backend = backend
        self.camera = camera
        self.audio = audio

        self.history: list[dict] = []          # [{"role","content"}]
        self.notes: list[str] = []             # 反馈按钮等系统备注，注入下一轮
        self.keep = int(cfg["log"]["history_keep"])

        # 当前播放的波形（按通道，供页面显示与 AI 上下文）
        # 循环波形的物理任务由 backend 维护；此处只保留展示/上下文用 patterns
        self.patterns: dict[str, str | None] = {"A": None, "B": None}

        # 自动观察循环（阶段 3 摄像头闭环）
        self.observe_task: asyncio.Task | None = None
        self.observe_stop = asyncio.Event()
        self.turn_busy = False
        self._conversation_lock = asyncio.Lock()
        self.pending_user_turns = 0
        self.on_state_change = None
        self.on_turn = None  # async ({source, user_text, result}); may annotate result in place
        self._execution_feedback = {"outcome": "unchanged", "commands": []}
        if hasattr(type(backend), "start_pulse_hold"):
            backend.on_command_failure = self._command_failed_later
        self._action_epoch = 0                # 急停/断线后丢弃尚未执行的旧回合动作
        self._pending_safety_replies = 0
        self._turn_source = None
        self._turn_phase = "idle"
        self._turn_started = None
        self._turn_started_monotonic = None
        self._last_turn_error = None
        self._next_auto_at = None
        self._active_model_task = None
        self._preempted_models = set()
        self.auto_yield_delay_s = 1.0

        # 自动运行（AI 自主回合：观察→描写→动作→发言，玩家不用打字）
        self.autopilot = bool(cfg.get("autopilot", {}).get("enabled", False))
        self.autopilot_interval = float(cfg.get("autopilot", {}).get("interval_s", 12))
        self.autopilot_task: asyncio.Task | None = None
        self.autopilot_stop = asyncio.Event()
        self._autopilot_schedule_changed = asyncio.Event()
        self._autopilot_reference = None
        self._autopilot_retry_at = 0.0
        self.on_ai_turn = None                 # 由 AppState 注入：把 AI 主动回合推送到页面

        # 轮次计数与每通道最近一次调整，仅记录已选择的动作
        self.turn_count = 0
        self.last_strength = {"A": 0, "B": 0}
        self.last_wave = {"A": 0, "B": 0}

        # 怒气值检测：画面持续黑暗 / 麦克风持续无声 → 触手怒气逐轮上升
        self.rage_rounds = 0
        self.rage_triggered = False

    # ---------- 状态 ----------
    def _sensor_rage(self) -> bool:
        """画面持续黑暗 或 麦克风持续无声 → 怒气积累。"""
        dark = False
        if self.camera and self.camera.enabled:
            cs = self.camera.to_state()
            dark = bool(cs.get("has_frame")) and bool(cs.get("dark"))
        silent = False
        if self.audio and self.audio.enabled:
            ast = self.audio.to_state()
            # 麦克风开关关闭（未在监听）时不计无声，避免用户主动关麦却触发怒气
            silent = bool(ast.get("running")) and bool(ast.get("silent"))
        return dark or silent

    def _note_rage(self) -> None:
        """每轮开始前更新怒气值轮数。"""
        self.rage_triggered = self._sensor_rage()
        if self.rage_triggered:
            self.rage_rounds += 1
        else:
            self.rage_rounds = 0

    def build_state(self) -> dict:
        backend_state = self.backend.to_state()
        state = self.safety.to_state()
        state["relay_status"] = backend_state["status"]
        state["controller_id"] = backend_state["controller_id"]
        state["connected"] = backend_state["status"] in ("paired", "ready")
        state["notes"] = list(self.notes)
        state["camera_enabled"] = bool(self.camera and self.camera.enabled)
        state["camera"] = self.camera.to_state() if self.camera else {}
        # 通道配件与工作状态（台词描写只落在设备位置 / 只写工作通道）
        state["device_channels"] = {
            ch: dict(self.cfg["device_channels"].get(ch) or {})
            for ch in ("A", "B")
        }
        pulse = self.safety.pulse_active()
        loops = self.backend.loops_active()
        state["active_channels"] = {
            ch: bool(self.safety.current.get(ch))
            or bool(pulse.get(ch))
            or bool(loops.get(ch))
            for ch in ("A", "B")
        }
        state["patterns"] = dict(self.patterns)
        state["model_judgment"] = bool(self.cfg.get("interaction", {}).get("model_judgment", False))
        state["execution_feedback"] = self._execution_feedback
        # 强度基准：跟随配件走（敏感配件基准低，如贴片15/肛塞5）
        state["baseline_strength"] = {}
        for ch in ("A", "B"):
            d = self.cfg["device_channels"].get(ch) or {}
            try:
                value = int(d.get("baseline", 15 if ch == "A" else 5))
            except (TypeError, ValueError):
                value = 15 if ch == "A" else 5
            state["baseline_strength"][ch] = max(0, min(100, value))
        state["rage_rounds"] = self.rage_rounds + int(self.cfg["character"].get("rage_baseline") or 0)
        state["rage_triggered"] = self.rage_triggered
        # 角色与风格版本（多角色两级：角色 → 风格档），页面切换用
        state["role"] = str(self.cfg["character"].get("role") or "触手")
        state["role_title"] = str(self.cfg["character"].get("role_title") or "主人")
        state["roles"] = list(self.cfg["character"].get("roles") or [])
        state["profile"] = str(self.cfg["character"].get("profile") or "纯爱")
        state["profiles"] = list(self.cfg["character"].get("profiles") or ["纯爱"])
        state["profile_available"] = dict(self.cfg["character"].get("profile_available") or {})
        state["profile_level"] = str(self.cfg["character"].get("profile_level") or "中")
        state["autopilot"] = bool(self.autopilot)
        state["autopilot_interval_s"] = self.autopilot_interval
        state["turn_busy"] = self.turn_busy
        state["pending_chat"] = self.pending_user_turns
        state["turn_status"] = {
            "phase": self._turn_phase, "source": self._turn_source,
            "started_at_ms": self._turn_started,
            "elapsed_s": round(max(0, time.monotonic() - self._turn_started_monotonic), 1)
            if self._turn_started_monotonic is not None else 0,
            "last_error": self._last_turn_error,
            "next_auto_at_ms": self._next_auto_at if self.autopilot else None,
        }
        return state

    async def _phase(self, phase: str, source: str | None = None) -> None:
        self._turn_phase = phase
        if source is not None:
            self._turn_source = source
        if phase == "thinking":
            self._turn_started = round(time.time() * 1000)
            self._turn_started_monotonic = time.monotonic()
            self._last_turn_error = None
        elif phase == "idle":
            self._turn_source = None
            self._turn_started = self._turn_started_monotonic = None
        await self._notify_state_change()

    async def _yield_automatic_model(self, task) -> None:
        await asyncio.sleep(self.auto_yield_delay_s)
        if (self.pending_user_turns and task is self._active_model_task and not task.done()
                and self._turn_source in {"autopilot", "observe", "open"}
                and self._turn_phase == "thinking"):
            self._preempted_models.add(task)
            task.cancel()

    async def _model_reply(self, messages, state, image_b64=None):
        task = asyncio.create_task(self.llm.chat(self.cfg["character"], messages, state, image_b64=image_b64))
        self._active_model_task = task
        yielding = asyncio.create_task(self._yield_automatic_model(task)) if self.pending_user_turns else None
        try:
            try:
                result = await task
            except asyncio.CancelledError:
                if task in self._preempted_models and not asyncio.current_task().cancelling():
                    raise _AutomaticTurnYielded() from None
                raise
            if task in self._preempted_models:
                raise _AutomaticTurnYielded()
            return result
        finally:
            if yielding:
                yielding.cancel()
            if self._active_model_task is task:
                self._active_model_task = None
            self._preempted_models.discard(task)

    async def _notify_state_change(self) -> None:
        if self.on_state_change:
            try:
                await self.on_state_change()
            except Exception:  # noqa: BLE001
                logger.exception("对话队列状态推送失败")

    @asynccontextmanager
    async def conversation_edit(self):
        """按到达顺序处理用户消息/角色编辑，并让自动回合让路。"""
        self.pending_user_turns += 1
        model = self._active_model_task
        yielding = asyncio.create_task(self._yield_automatic_model(model)) if model is not None else None
        asyncio.create_task(self._notify_state_change())
        acquired = False
        try:
            # Acquire before notifying: awaits in the callback must not reorder
            # messages arriving at an otherwise idle conversation.
            await self._conversation_lock.acquire()
            acquired = True
            self.pending_user_turns -= 1
            self.turn_busy = True
            await self._notify_state_change()
            yield
        finally:
            if acquired:
                self.turn_busy = False
                self._conversation_lock.release()
            else:
                self.pending_user_turns -= 1
            if yielding:
                yielding.cancel()
            if acquired:
                await self._phase("idle")
            else:
                await self._notify_state_change()

    @asynccontextmanager
    async def _automatic_turn_slot(self):
        """自动回合只使用空闲时隙，不排在待处理用户消息前面。"""
        if self.turn_busy or self.pending_user_turns or self.safety.estop_active:
            yield False
            return
        await self._conversation_lock.acquire()
        self.turn_busy = True
        try:
            await self._notify_state_change()
            yield True
        finally:
            self.turn_busy = False
            self._conversation_lock.release()
            await self._phase("idle")

    # ---------- 用户回合 ----------
    def clear_history(self) -> None:
        """清空对话历史（模型上下文；页面消息记录由前端同步清）。"""
        self.history.clear()
        self._execution_feedback = {"outcome": "unchanged", "commands": []}
        logger.info("对话历史已清空")

    async def handle_user_message(
        self, text: str, *, control_device: bool | None = True,
        preferred_pattern: str | None = None,
        accepted_action_epoch: int | None = None,
    ) -> dict:
        """排队处理消息；None 自动判断连接，False 始终只进行文字交流。"""
        text = (text or "").strip()
        if not text:
            return {"line": "", "executed": [], "dropped": []}
        intent = stop_intent(text, model_judgment=bool(self.cfg.get("interaction", {}).get("model_judgment", False)))
        if intent:
            self._pending_safety_replies += 1
            try:
                stopped = await self.estop()  # Safety takes effect before any conversation lock.
                entry = self._receipt({"op": "stop"}, {"kind": "stop"}, Delivery(stopped["status"], stopped["written"], stopped["reason"]))
                line = "已停止输出并锁定设备。" if stopped["sent"] else "已锁定输出并停止循环；设备清零尚未确认，请检查设备。"
                if self.safety.dry_run:
                    line = "已停止模拟输出并锁定设备。"
                result = {"line": line, "executed": [entry], "dropped": [], "error": None, "safety_intent": intent}
                async with self.conversation_edit():
                    self.history = (self.history + [{"role": "user", "content": text}, {"role": "assistant", "content": line}])[-self.keep:]
                    self._remember_execution(result)
                    await self._publish_turn("user", text, result)
                return result
            finally:
                self._pending_safety_replies -= 1
        # Capturing before waiting also invalidates queued actions on an estop.
        action_epoch = self._action_epoch if accepted_action_epoch is None else accepted_action_epoch
        async with self.conversation_edit():
            if self._pending_safety_replies:
                result = {"line": "本轮已因停止请求取消，请确认设备状态后重新交流。",
                          "executed": [], "dropped": [], "error": "turn_cancelled"}
                await self._publish_turn("user", text, result)
                return result
            if control_device is None:
                control_device = (
                    (self.backend.ready() or self.safety.dry_run)
                    and not self.safety.estop_active
                    and action_epoch == self._action_epoch
                )
            return await self._user_turn(text, control_device, preferred_pattern, action_epoch)

    async def _user_turn(self, text, control_device, preferred_pattern, action_epoch) -> dict:
        if control_device and not (self.backend.ready() or self.safety.dry_run):
            result = {"line": "设备尚未连接，可以先切换到纯文字聊天。", "executed": [], "dropped": [], "error": "device_not_connected"}
            await self._publish_turn("user", text, result)
            return result
        if control_device and preferred_pattern and preferred_pattern not in self.safety.presets:
            raise ValueError("所选波形不在当前波形库中")
        reload_character(self.cfg)  # 角色设定热加载：改完保存，下一条消息生效

        draft = (self.history + [{"role": "user", "content": text}])[-self.keep:]

        if control_device:
            self._note_rage()
        state = self.build_state()
        state["control_device"] = bool(control_device)
        state["chat_mode"] = "device" if control_device else "text"
        if control_device and preferred_pattern:
            state["preferred_pattern"] = preferred_pattern
        error = None
        model_error = None
        await self._phase("thinking", "user")
        try:
            line, actions = await self._model_reply(draft, state, self._latest_image() if control_device else None)
        except _AutomaticTurnYielded:
            line, actions, error = "本轮已被停止请求取消。", [], "turn_cancelled"
        except Exception as exc:  # noqa: BLE001
            logger.exception("模型调用失败")
            error = str(exc) or type(exc).__name__
            if "思维链泄漏" in error or "思维链" in error:
                line = "（模型走神了：连续输出思考过程已被拦截。把刚才的话再发一次就好。）"
            else:
                line = f"（{_friendly_llm_error(exc)}）"
            actions = []
            self._last_turn_error = _friendly_llm_error(exc)
            model_error = getattr(exc, "diagnostic", None)
        if control_device and error is None:
            await self._phase("executing")
            self.turn_count += 1
            executed, dropped = await self.execute_actions(
                actions, apply_scale=True, action_epoch=action_epoch,
            )
        else:
            # The execution boundary discards model actions independently of its prompt.
            executed, dropped = [], []

        if error is None:
            self.history = draft + [{"role": "assistant", "content": line}]
        result = {"line": line, "executed": executed, "dropped": dropped, "error": error, "model_error": model_error}
        self._remember_execution(result)
        await self._publish_turn("user", text, result)
        return result

    async def _publish_turn(self, source, user_text, result):
        if self.on_turn:
            try:
                await self.on_turn({"source": source, "user_text": user_text, "result": result})
            except Exception:
                # Actions may have run already. Persistence failure is never a
                # reason to fail/retry the physical operation.
                logger.exception("聊天回执保存失败")
                result["persistence_error"] = "本轮已处理，但聊天记录保存失败"

    def _remember_execution(self, result):
        entries, dropped = result.get("executed", []), result.get("dropped", [])
        failures = any(item.get("status") in {"failed", "unconfirmed"} for item in entries)
        outcome = "failed" if failures else "blocked" if dropped else "processed" if entries else "unchanged"
        result["execution_outcome"] = outcome
        self._execution_feedback = {"outcome": outcome, "commands": [
            {"command": item.get("command"), "status": item.get("status"), "reason": item.get("reason")}
            for item in entries[-16:]], "blocked": [item.get("reason") for item in dropped[-8:]]}

    def _command_failed_later(self, cmd, receipt):
        ch = cmd.get("channel")
        if ch in ("A", "B"):
            self.patterns[ch] = None
            self.safety.pulse_until[ch] = 0.0
        self._execution_feedback = {"outcome": "failed", "commands": [
            {"command": self._public_command(cmd), "status": receipt.status, "reason": receipt.reason}]}
        asyncio.create_task(self._notify_state_change())

    # ---------- 画面辅助 ----------
    def _latest_image(self) -> str | None:
        """取摄像头最新帧（启用时）。"""
        if self.camera and self.camera.enabled and self.camera.has_frame():
            return self.camera.base64()
        return None

    # ---------- 主动开场（配对成功后 AI 自动开口） ----------
    async def auto_open(self) -> dict:
        result = await self._run_automatic_turn("open")
        return result or {"line": "", "executed": [], "dropped": [], "error": "turn_unavailable"}

    async def _run_automatic_turn(self, kind: str) -> dict | None:
        async with self._automatic_turn_slot() as acquired:
            if not acquired or not (self.backend.ready() or self.safety.dry_run):
                return None
            action_epoch = self._action_epoch
            reload_character(self.cfg)
            self._note_rage()
            state = self.build_state()
            state.update(control_device=True, chat_mode="device", turn_source=kind)
            prompts = {
                "open": "请按照当前角色的身份和语气向用户打招呼，自然承接已有对话。",
                "observe": "请结合当前角色与对话观察最新画面，只引用能够确认的内容，自然回应用户。",
                "autopilot": "这是用户已开启的自动回合。请结合当前角色与最近对话主动延续情景，自然回应用户，不重复上一轮内容。",
            }
            prompt = prompts[kind] + (
                "依据角色已有性格与动机、对话情景、用户反馈与当前设备状态，自主判断保持现状、调整强度或切换波形。"
                "可以接受、拒绝、暂缓或提出替代回应，不要把用户的话直接当成设备命令。"
                + ("模型自判断已开启：普通情景中的犹豫和拒绝措辞结合角色与上下文判断；明确急停与真实安全撤回始终优先。"
                   if state.get("model_judgment") else "停止与减弱要求仍须优先遵守，真实不适按保守含义处理。")
                +
                "需要调整时使用当前完整波形库；无需调整则 actions 为 []。"
                "不必每轮调整，不必同时操作两个通道，没有画面或声音也不是加大强度的理由。"
            )
            draft = (self.history + [{"role": "user", "content": prompt}])[-self.keep:]
            error = None
            model_error = None
            await self._phase("thinking", kind)
            try:
                line, actions = await self._model_reply(draft, state, self._latest_image())
            except _AutomaticTurnYielded:
                return None
            except Exception as exc:  # noqa: BLE001
                logger.exception("自动回合模型调用失败: %s", exc)
                error = str(exc) or type(exc).__name__
                line = f"（{_friendly_llm_error(exc)}）"
                actions = []
                self._last_turn_error = _friendly_llm_error(exc)
                model_error = getattr(exc, "diagnostic", None)
            if error is None:
                await self._phase("executing")
                self.turn_count += 1
                executed, dropped = await self.execute_actions(
                    actions, apply_scale=True, action_epoch=action_epoch,
                )
            else:
                executed, dropped = [], []
            if error is None:
                self.history = draft + [{"role": "assistant", "content": line}]
            result = {"line": line, "executed": executed, "dropped": dropped, "error": error, "model_error": model_error, "source": kind}
            self._remember_execution(result)
            await self._publish_turn(kind, None, result)
            # Publish while this turn still owns the conversation. Otherwise a
            # queued HTTP reply can overtake an earlier automatic WS message.
            if self.on_ai_turn:
                try:
                    await self._phase("publishing")
                    await self.on_ai_turn(result)
                except Exception:  # noqa: BLE001
                    logger.exception("自动回合推送失败")
            return result

    # ---------- 自动观察循环（阶段 3：AI 看图 → 调整策略） ----------
    def start_observe_loop(self) -> None:
        cfg = self.cfg["camera"]
        if (
            self.observe_task
            or not self.camera
            or not self.camera.enabled
            or not bool(cfg.get("auto_observe", True))
        ):
            return
        interval = float(cfg.get("observe_interval_s", 10))
        self.observe_stop = asyncio.Event()
        self.observe_task = asyncio.create_task(self._observe_loop(interval))
        logger.info("自动观察循环启动：每 %ss 看一次画面", interval)

    def stop_observe_loop(self) -> None:
        self.observe_stop.set()
        if self.observe_task:
            self.observe_task.cancel()
            self.observe_task = None

    async def _observe_loop(self, interval: float) -> None:
        while not self.observe_stop.is_set():
            try:
                await asyncio.wait_for(self.observe_stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            if self.safety.estop_active or self.turn_busy or self.pending_user_turns:
                continue
            if self.backend.to_state()["status"] not in ("paired", "ready"):
                continue
            if not (self.camera and self.camera.has_frame()):
                continue
            await self._auto_observe_turn()

    async def _auto_observe_turn(self) -> dict | None:
        """共享对话中观察最新画面，并在释放回合前广播结果。"""
        return await self._run_automatic_turn("observe")

    # ---------- 自动运行（玩家不输入，AI 自主回合） ----------
    def set_autopilot_interval(self, interval_s: float) -> float:
        """更新最小开轮间隔，并立即唤醒等待中的计时器重新计算。"""
        self.autopilot_interval = max(5.0, min(30.0, float(interval_s)))
        self._autopilot_schedule_changed.set()
        return self.autopilot_interval

    def set_autopilot(self, enabled: bool) -> None:
        """两次自动回合开始至少相隔 interval_s 秒，始终不并发开轮。"""
        self.autopilot = bool(enabled)
        if self.autopilot and (self.autopilot_task is None or self.autopilot_task.done()):
            self.autopilot_stop = asyncio.Event()
            self._autopilot_schedule_changed = asyncio.Event()
            self._autopilot_reference = time.monotonic()
            self._autopilot_retry_at = 0.0
            self.autopilot_task = asyncio.create_task(self._autopilot_loop())
            logger.info("自动运行已开启：每 %.1fs 一个自主回合", self.autopilot_interval)
        elif not self.autopilot:
            self._next_auto_at = None
            self.autopilot_stop.set()
            self._autopilot_schedule_changed.set()
            if self.autopilot_task:
                self.autopilot_task.cancel()
                self.autopilot_task = None
            logger.info("自动运行已停止")

    async def _autopilot_loop(self) -> None:
        while not self.autopilot_stop.is_set():
            try:
                reference = self._autopilot_reference
                if reference is None:
                    reference = self._autopilot_reference = time.monotonic()
                deadline = max(reference + self.autopilot_interval, self._autopilot_retry_at)
                delay = max(.05, deadline - time.monotonic())
                self._next_auto_at = round((time.time() + delay) * 1000)
                await self._notify_state_change()
                try:
                    await asyncio.wait_for(self._autopilot_schedule_changed.wait(), timeout=max(.05, deadline - time.monotonic()))
                    self._autopilot_schedule_changed.clear()
                    continue
                except asyncio.TimeoutError:
                    pass
                self._next_auto_at = None
                if self.safety.estop_active or self.turn_busy or self.pending_user_turns:
                    self._autopilot_retry_at = time.monotonic() + 1
                    continue
                if not (self.backend.ready() or self.safety.dry_run):
                    self._autopilot_retry_at = time.monotonic() + 1
                    continue  # 设备未连接不自动运行（用户设定：连接设备后才开始）
                self._autopilot_reference = time.monotonic()
                self._autopilot_retry_at = 0.0
                await self._autopilot_turn()
            except Exception:  # noqa: BLE001
                # 任何异常都不能杀死循环任务（曾因此静默死亡导致 AI 一直不说话）
                logger.exception("自动运行循环异常，跳过本轮继续")

    async def _autopilot_turn(self) -> dict | None:
        return await self._run_automatic_turn("autopilot")

    # ---------- 动作执行（AI 与手动共用） ----------
    async def _ensure_default_wave(
        self, ch_name: str, ready: bool, dry_run: bool, *, action_epoch: int | None = None,
    ) -> dict | None:
        """给通道挂默认持续波形（强度没有波形承载时设备无输出）。"""
        pattern = str(self.cfg["ui"].get("default_wave", "呼吸") or "呼吸")
        meta = self.safety.presets.get(pattern)
        if not meta and self.safety.presets:
            pattern = next(iter(self.safety.presets))
            meta = self.safety.presets[pattern]
        if not meta:
            return
        cmd = {
            "kind": "pulse_hold", "channel": ch_name, "pattern": pattern,
            "wave_key": meta["waveform"], "frames": meta["frames"],
        }
        receipt = await self._perform(cmd, action_epoch)
        entry = self._receipt({"op": "pulse_hold", "channel": ch_name, "pattern": pattern}, cmd, receipt)
        entry["source"] = "default_wave"
        return entry

    def _scale_cmd(self, cmd: dict) -> None:
        """按强度档倍率修正强度动作（最终强度 = AI 设定值 × 档位倍率；手动操作不乘）。

        只作用于 hold/add/temp 三类强度动作；任何倍率下结果都钳到该通道上限。
        """
        ch = cmd.get("channel")
        if ch not in ("A", "B") or cmd.get("kind") not in ("hold", "add", "temp"):
            return
        scale = self.safety.scale.get(ch, 1.0)
        cap = self.safety.cap_for(ch)
        try:
            if cmd["kind"] == "add":
                delta = round(int(cmd.get("delta", 0)) * scale)
                delta = max(-self.safety.max_step, min(delta, self.safety.max_step))
                current = self.safety.current.get(ch, 0)
                cmd["value"] = max(0, min(cap, current + delta))
                # The relay sends delta while the state tracker records value;
                # both must describe the same capped target. Reducing an already
                # excessive current down to the cap takes priority over max_step.
                cmd["delta"] = cmd["value"] - current
            else:
                cmd["value"] = max(0, min(cap, round(int(cmd.get("value", 0)) * scale)))
        except (TypeError, ValueError):
            return

    def _actions_invalidated(self, action_epoch: int | None) -> bool:
        return action_epoch is not None and (
            action_epoch != self._action_epoch or self.safety.estop_active
        )

    def _paired_actions(self, actions):
        """Pair each waveform with one explicit strength, without replaying it.

        Waveform-first ordering lets a failed carrier block its strength change.
        This is sequential dispatch with independent receipts, not a transaction.
        """
        waves, strengths = {"pulse", "pulse_hold"}, {"hold_strength", "add_strength", "temp_strength"}
        consumed, planned = set(), []

        def channel(action):
            try:
                return self.safety.norm_channel(action.get("channel")) if isinstance(action, dict) else None
            except ValueError:
                return None
            except Exception:
                return None

        for index, action in enumerate(actions):
            if index in consumed:
                continue
            consumed.add(index)
            op = action.get("op") if isinstance(action, dict) else None
            ch = channel(action)
            match = None
            if ch and op in waves | strengths:
                for later in range(index + 1, len(actions)):
                    candidate = actions[later]
                    if later in consumed or not isinstance(candidate, dict):
                        continue
                    candidate_op = candidate.get("op")
                    if candidate_op in {"stop", "clear"}:
                        break
                    if channel(candidate) != ch:
                        continue
                    if candidate_op in (strengths if op in waves else waves):
                        match = later
                    # Do not cross a second command of the same family.
                    if candidate_op in waves | strengths:
                        break
            if op in strengths and match is None or op not in waves | strengths or not ch:
                planned.append({"action": action})
                continue
            if match is not None:
                consumed.add(match)
                wave, strength = (action, actions[match]) if op in waves else (actions[match], action)
                derived = False
            else:
                wave, strength = action, {"op": "hold_strength", "channel": ch, "value": 0}
                derived = True
            pair = {"strength": strength, "derived": derived}
            planned.append({"action": wave, "pair": pair})
            planned.append({"action": strength, "strength_pair": pair})
        return planned

    def _derived_wave_strength(self, ch):
        cap = self.safety.cap_for(ch)
        current = max(0, min(cap, self.safety.current[ch]))
        if current > 0:
            return current  # A preserved effective value must never be scaled again.
        try:
            baseline = int(self.cfg.get("device_channels", {}).get(ch, {}).get("baseline", 15 if ch == "A" else 5))
        except (TypeError, ValueError):
            baseline = 15 if ch == "A" else 5
        return min(cap, max(1, round(max(0, min(100, baseline)) * self.safety.scale.get(ch, 1.0))))

    async def execute_actions(
        self, actions: list, apply_scale: bool = False, *, action_epoch: int | None = None,
    ) -> tuple[list, list]:
        """校验并执行动作列表，返回 (已执行说明列表, 被拒绝说明列表)。

        apply_scale=True 时（AI 回合），强度动作按玩家选择的强度档倍率（轻/中/重）修正；
        手动操作（apply_scale=False）保持原值。
        """
        executed, dropped = [], []
        if not isinstance(actions, list):
            return executed, dropped

        # EN 模式：把发给页面的执行说明/拒绝原因换成英文（T051 §1.5/§4.2）
        en = str((self.cfg.get("character") or {}).get("lang") or "zh") == "en"

        action_epoch = self._action_epoch if action_epoch is None else action_epoch
        deadline = time.monotonic() + 6.0
        blocked_channels = set()

        for planned in self._paired_actions(actions):
            action = planned["action"]
            if self._actions_invalidated(action_epoch):
                dropped.append({"action": action, "reason": "回合已被急停或断线取消"})
                continue
            if not isinstance(action, dict):
                dropped.append(
                    {
                        "action": action,
                        "reason": (
                            reason_en("动作必须是 JSON 对象") if en else "动作必须是 JSON 对象"
                        ),
                    }
                )
                continue
            try:
                ok, reason, cmd = self.safety.validate(action)
            except (ValueError, TypeError, OverflowError):
                ok, reason, cmd = False, "动作数值格式无效", None
            if not ok:
                if en:
                    reason = reason_en(reason)
                dropped.append({"action": action, "reason": reason})
                logger.warning("动作被安全层拒绝: %s -> %s", action, reason)
                continue
            strength_pair = planned.get("strength_pair")
            if strength_pair and not strength_pair.get("wave_started"):
                try:
                    explicit_zero = (not strength_pair["derived"] and action.get("op") in {"hold_strength", "temp_strength"}
                                     and float(action.get("value")) == 0)
                except (ValueError, TypeError, OverflowError):
                    explicit_zero = False
                if not explicit_zero:
                    dropped.append({"action": action, "reason": "配套波形未成功发送，配套强度未执行"})
                    continue

            if time.monotonic() >= deadline:
                dropped.append({"action": action, "reason": "本轮设备确认等待已达上限，剩余命令未发送"})
                continue
            if cmd.get("channel") in blocked_channels and cmd["kind"] not in ("stop", "clear"):
                dropped.append({"action": action, "reason": "该通道上一条命令失败或未确认，后续动作未发送"})
                continue

            ready, dry_run = self.backend.ready(), self.safety.dry_run
            if not ready and not dry_run:
                dropped.append(
                    {
                        "action": action,
                        "reason": (
                            reason_en("设备未连接（无 clientId/slotId）")
                            if en
                            else "设备未连接（无 clientId/slotId）"
                        ),
                    }
                )
                continue

            # AI 回合：强度动作按玩家强度档倍率修正（手动操作已在调用处传 apply_scale=False）
            if apply_scale and not (strength_pair and strength_pair["derived"]):
                self._scale_cmd(cmd)
            if strength_pair:
                cmd["_force_send"] = True  # Pair receipts describe an actual write, including a zero delta.

            pair = planned.get("pair")
            if pair:
                ch = cmd["channel"]
                if pair["derived"]:
                    pair["strength"]["value"] = self._derived_wave_strength(ch)
                try:
                    valid, _, target = self.safety.validate(pair["strength"])
                    if valid and apply_scale and not pair["derived"]:
                        self._scale_cmd(target)
                except (ValueError, TypeError, OverflowError):
                    valid, target = False, None
                if not valid or target["value"] <= 0:
                    dropped.append({"action": action, "reason": "波形未发送：同通道强度为零、上限为零或配套强度无效"})
                    if pair["derived"]:
                        blocked_channels.add(ch)
                    continue

            # 强度类动作必须有波形承载才有输出（DG-LAB 特性）：通道无波形时自动挂默认波形
            if cmd["kind"] in ("hold", "add", "temp") and cmd.get("channel") in ("A", "B"):
                ch_name = cmd["channel"]
                if cmd["value"] > 0 and not self.backend.loops_active().get(ch_name) and not self.safety.pulse_active().get(ch_name):
                    entry = await self._ensure_default_wave(ch_name, ready, dry_run, action_epoch=action_epoch)
                    if entry:
                        executed.append(entry)
                    if not entry or entry["status"] not in ("confirmed", "sent", "simulated", "unchanged"):
                        blocked_channels.add(ch_name)
                        dropped.append({"action": action, "reason": "承载波形未能启动，强度命令未发送"})
                        continue
                    if self._actions_invalidated(action_epoch):
                        self.backend.stop_pulse_hold(ch_name)
                        self.patterns[ch_name] = None
                        dropped.append({"action": action, "reason": "回合已被急停或断线取消"})
                        continue

            # A default waveform can yield while caps, reported strength or the
            # connection change. Re-clamp the already scaled target immediately
            # before sending; never apply the selected multiplier a second time.
            ready, dry_run = self.backend.ready(), self.safety.dry_run
            if self.safety.estop_active and cmd["kind"] not in ("stop", "clear"):
                dropped.append({"action": action, "reason": "当前处于急停状态"})
                continue
            if not ready and not dry_run:
                dropped.append({"action": action, "reason": "设备未连接（无 clientId/slotId）"})
                continue
            if cmd["kind"] in ("hold", "add", "temp"):
                ch_name = cmd["channel"]
                cap = self.safety.cap_for(ch_name)
                target = max(0, min(cap, int(cmd["value"])))
                if cmd["kind"] == "add":
                    current = self.safety.current.get(ch_name, 0)
                    delta = max(-self.safety.max_step, min(target - current, self.safety.max_step))
                    target = max(0, min(cap, current + delta))
                    cmd["delta"] = target - current
                cmd["value"] = target

            try:
                receipt = await asyncio.wait_for(self._perform(cmd, action_epoch), max(.001, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                receipt = Delivery("unconfirmed", False, "本轮设备确认超时，未重复发送")
                if not self._actions_invalidated(action_epoch) and cmd["kind"] in ("hold", "add", "temp"):
                    self.safety.mark_uncertain(cmd["channel"])
            entry = self._receipt(action, cmd, receipt)
            if strength_pair and strength_pair["derived"]:
                entry["source"] = "waveform_strength"
            executed.append(entry)
            if pair:
                pair["wave_started"] = bool(receipt) or receipt.status == "simulated"
            if not receipt and receipt.status != "simulated":
                if cmd.get("channel") in ("A", "B"):
                    blocked_channels.add(cmd["channel"])
                if receipt.superseded or (receipt.status == "failed" and not receipt.sent):
                    continue
                self.backend.stop_pulse_hold(cmd.get("channel"))
                if cmd.get("channel") in ("A", "B"):
                    ch = cmd["channel"]
                    self.patterns[ch] = None
                    self.safety.pulse_until[ch] = 0.0
                    if strength_pair and not self._actions_invalidated(action_epoch):
                        # The carrier may already be running. Clear it instead
                        # of leaving a partial pair or retrying an uncertain add.
                        cleanup = {"kind": "clear", "channel": ch, "_current": dict(self.safety.current)}
                        stopped = await self._send_stop(cleanup)
                        if stopped:
                            self.safety.record(cleanup)
                        item = self._receipt({"op": "clear", "channel": ch}, cleanup, stopped)
                        item["source"] = "failed_pair_cleanup"
                        executed.append(item)
        return executed, dropped

    def _receipt(self, action, cmd, receipt):
        receipt = delivery(receipt)
        en = str(self.cfg.get("character", {}).get("lang")) == "en"
        return {"action": action, "command": self._public_command(cmd), "reason": receipt.reason,
                "sent": receipt.sent, "status": receipt.status,
                "label": describe_en(cmd) if en else self._describe(cmd)}

    async def _perform(self, cmd, action_epoch):
        kind, ch = cmd["kind"], cmd.get("channel")
        if kind in ("stop", "clear"):
            self.backend.stop_pulse_hold(ch)
            for channel in ("A", "B") if ch is None else (ch,):
                self.patterns[channel] = None
        if self._actions_invalidated(action_epoch):
            return Delivery("failed", False, "回合已被急停或断线取消")
        if self.safety.dry_run:
            result = Delivery("simulated", False, "模拟操作，未发送到设备")
        else:
            try:
                value = await (self.backend.start_pulse_hold(ch, cmd) if kind == "pulse_hold" else self.backend.apply(cmd))
                result = delivery(value)
            except Exception:
                logger.exception("设备操作失败")
                result = Delivery("unconfirmed", False, "设备操作中断，结果未确认；未重复发送")
        if result.superseded:
            return result
        if self._actions_invalidated(action_epoch) or (ch in ("A", "B") and not self.safety.enabled[ch] and kind not in ("stop", "clear")):
            if kind == "pulse_hold":
                self.backend.stop_pulse_hold(ch)
            return Delivery("unconfirmed", result.sent, "本轮命令已被停止或断线取消，请以当前设备状态为准", superseded=True)
        if result or result.status == "simulated":
            self.safety.record(cmd)
            if ch in ("A", "B"):
                if kind in ("hold", "add", "temp"):
                    self.last_strength[ch] = self.turn_count
                if kind in ("pulse", "pulse_hold"):
                    self.patterns[ch] = cmd.get("pattern")
                    self.last_wave[ch] = self.turn_count
        elif result.status == "unconfirmed" and kind in ("hold", "add", "temp", "clear", "stop"):
            for channel in ("A", "B") if ch is None else (ch,):
                self.safety.mark_uncertain(channel)
        return result

    @staticmethod
    def _public_command(cmd: dict) -> dict:
        """Expose actual normalized targets, never waveform frames or raw AI values."""
        return {key: cmd[key] for key in ("kind", "channel", "value", "delta", "pattern", "duration_s") if key in cmd}

    @staticmethod
    def _describe(cmd: dict) -> str:
        kind = cmd["kind"]
        ch = cmd.get("channel")
        if kind == "temp":
            return f"{ch} 爆发 {cmd['value']} × {cmd['duration_s']:.1f}s（结束归零）"
        if kind == "hold":
            return f"{ch} 持续强度 {cmd['value']}（保持）"
        if kind == "add":
            return f"{ch} 增减 {cmd['delta']:+d}"
        if kind == "pulse":
            return f"{ch} 波形「{cmd['pattern']}」× {cmd['duration_s']:.1f}s"
        if kind == "pulse_hold":
            return f"{ch} 持续波形「{cmd['pattern']}」（循环）"
        if kind == "clear":
            return "清除全部" if ch is None else f"清除 {ch} 通道"
        return "急停清零"

    # ---------- 急停 / 恢复 ----------
    async def estop(self) -> dict:
        self._action_epoch += 1
        snapshot = dict(self.safety.current)
        self.backend.stop_pulse_hold(None)  # 停掉所有循环波形
        self.patterns = {"A": None, "B": None}
        self.safety.estop()  # Latch before the first await.
        self.set_autopilot(False)
        self.stop_observe_loop()
        model = self._active_model_task
        if model is not None and not model.done():
            self._preempted_models.add(model)
            model.cancel()
        receipt = await self._send_stop({"kind": "stop", "_current": snapshot})
        if receipt or receipt.status == "simulated":
            self.safety.record({"kind": "stop"})
        return {"estop": True, "sent": bool(receipt) and receipt.sent,
                "written": receipt.sent, "status": receipt.status, "reason": receipt.reason}

    async def _send_stop(self, cmd):
        if self.safety.dry_run:
            return Delivery("simulated", False, "模拟输出已清零")
        if not self.backend.ready():
            for channel in ("A", "B") if cmd.get("channel") is None else (cmd["channel"],):
                self.safety.mark_uncertain(channel)
            return Delivery("failed", False, "设备未连接，清零未确认")
        try:
            receipt = delivery(await asyncio.wait_for(self.backend.apply(cmd), 2.5))
        except Exception:
            receipt = Delivery("unconfirmed", False, "设备清零未确认，请立即检查设备")
        if not receipt and not receipt.superseded:
            for channel in ("A", "B") if cmd.get("channel") is None else (cmd["channel"],):
                self.safety.mark_uncertain(channel)
        return receipt

    async def disable_channel(self, ch):
        """Disable immediately, invalidate in-flight turns, then clear the wire."""
        return await self.clear_channel(ch, disable=True)

    async def clear_channel(self, ch, *, disable=False):
        """Clear one or both channels even while an emergency/session latch is set."""
        ch = self.safety.norm_channel(ch) if ch is not None else None
        snapshot = dict(self.safety.current)
        self._action_epoch += 1
        if disable and ch is not None:
            self.safety.enabled[ch] = False
        self.backend.stop_pulse_hold(ch)
        for channel in ("A", "B") if ch is None else (ch,):
            self.patterns[channel] = None
        cmd = {"kind": "clear", "channel": ch, "_current": snapshot}
        receipt = await self._send_stop(cmd)
        if receipt or receipt.status == "simulated":
            self.safety.record(cmd)
        result = {"executed": [self._receipt({"op": "clear", "channel": ch}, cmd, receipt)], "dropped": []}
        self._remember_execution(result)
        return result

    async def resume(self) -> dict:
        self.safety.resume()
        return {"estop": False}

    def on_client_disconnected(self) -> None:
        """设备断开：停止所有循环波形并清零跟踪。"""
        self._action_epoch += 1
        self.backend.stop_pulse_hold(None)
        self.patterns = {"A": None, "B": None}
        self.safety.record({"kind": "stop"})
        if not self.safety.dry_run:
            for ch in ("A", "B"):
                self.safety.mark_uncertain(ch)

    # ---------- 设备反馈 ----------
    async def handle_feedback(self, action: int, client_id: str) -> None:
        """APP 反馈按钮（custom.action 0-9）。"""
        self.add_note(f"玩家按下了反馈按钮 {action}")

    def add_note(self, note: str) -> None:
        """实时信号（反馈按钮/麦克风转写等）注入下一轮 AI 上下文。"""
        self.notes.append(note)
        self.notes = self.notes[-5:]

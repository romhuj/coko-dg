# -*- coding: utf-8 -*-
"""AI 郊狼驯服师 —— 主程序入口（FastAPI）。

启动后：
- 后台连接 dglab-websocket-server v4 中继；
- Web 页面：聊天、手动控制、实时状态、急停、配对二维码、日志；
- 所有设备命令统一走 safety -> device_ops -> relay 链路。
"""
import asyncio
import contextlib
from contextvars import ContextVar
import io
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
import qrcode
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .audio import AudioManager
from .camera import Camera
from .update_check import UpdateChecker
from .config import (
    load_config,
    reload_character,
    save_autopilot_interval,
    save_character_runtime,
    save_device_channels,
)
from .game_loop import GameLoop
from .llm import LLM
from .logging_utils import setup_logging
from .pairing import build_pair_url, pairing_relay_url
from .device.factory import build_backend
from .safety import SafetyError, SafetyManager
from .content_install import ContentInstallError, install_zip_bytes
from .character_search import CharacterSearchError, search_character
from .role_library import save_custom_role, save_user_role, set_custom_role_pinned, delete_custom_role, validate_custom_role_deletion
from .chat_requests import ChatRequests, ReceiptError
from .chat_archive import ChatArchive, ConversationListChanged
from .mobile_preferences import MobilePreferences
from .dungeon_v2 import DungeonRuntime

# 打包（PyInstaller）后以 exe 所在目录为项目根；开发时以仓库根
if os.environ.get("DGLAB_DATA_DIR"):
    PROJECT_ROOT = Path(os.environ["DGLAB_DATA_DIR"]).resolve()
elif getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = Path(os.environ.get("DGLAB_SEED_DIR", str(PROJECT_ROOT))).resolve() if os.environ.get("DGLAB_DATA_DIR") else PROJECT_ROOT
FRONTEND_DIST = BUNDLE_ROOT / "frontend" / "dist"
CHAT_REQUEST_CONTEXT = ContextVar("coyote_chat_request", default=None)


def _app_version() -> str:
    """版本号：打包时写入 version.txt；源码运行时显示 dev。"""
    f = BUNDLE_ROOT / "version.txt"
    if f.exists():
        v = f.read_text(encoding="utf-8").strip().lstrip("\ufeff")
        if v:
            return v
    return "dev"


def get_lan_ip() -> str:
    """探测本机局域网 IP（用于配对二维码）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def get_local_ips() -> list[str]:
    """列出本机所有可用 IPv4（含自动探测结果），供网络自检。"""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except OSError:
        pass
    ips.add(get_lan_ip())
    return sorted(ips)


class AppState:
    """共享运行对象 + Web 广播。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.mobile = bool(cfg.get("_mobile"))
        self._shutdown_started = False
        self.chat_requests = ChatRequests()
        self.logger = setup_logging(cfg["log_dir"], cfg["log"]["level"])
        self.archive = None
        self.archive_error = ""
        self.preferences = None
        self._conversation_changing = False
        cfg.setdefault("interaction", {})["model_judgment"] = False
        if self.mobile:
            try:
                self.preferences = MobilePreferences(PROJECT_ROOT / "config" / "mobile_preferences.json")
                cfg["interaction"]["model_judgment"] = self.preferences.values.get("model_judgment") is True
            except (OSError, ValueError):
                self.logger.error("无法读取已保存的设备设置，原文件保留")

        self.safety = SafetyManager(cfg)
        if self.mobile:
            self.safety.estop_active = True
        if self.preferences:
            self.preferences.apply(self.safety)
        self.backend = build_backend(
            cfg, self.safety, on_event=self.on_relay_event, on_action=self.on_relay_action
        )
        self.backend.on_disconnect(self._on_backend_disconnect)
        # 测试模式（不连郊狼）：保存真实后端，切换时换回
        self._real_backend = self.backend
        self._sim_backend = None
        self._test_mode = False
        self._test_mode_lock = asyncio.Lock()
        self.llm = LLM(cfg)
        self.camera = Camera(cfg)
        self.audio = AudioManager(
            cfg, on_text=self.on_audio_text, on_moan=self.on_audio_moan
        )
        self.loop = GameLoop(cfg, self.llm, self.safety, self.backend, self.camera, self.audio)
        self.loop.on_ai_turn = self.broadcast_chat  # AI 主动回合推送到页面聊天区
        self.loop.on_state_change = self.broadcast
        if self.mobile:
            try:
                self.archive = ChatArchive(PROJECT_ROOT / "data" / "chat_history.sqlite3")
                self.loop.history = self.archive.context(self.archive.active_id, self.persona_key(), self.loop.keep)
                self.loop.on_turn = self.archive_turn
            except Exception:
                # Preserve the existing database on storage failure; never silently replace it.
                if self.archive:
                    self.archive.close()
                self.archive = None
                self.archive_error = "聊天记录暂时无法读取，请检查存储空间后重启"
                self.logger.error("无法打开聊天存档，原文件保留")
        self.dungeon = DungeonRuntime(cfg, self.llm, self.safety, PROJECT_ROOT)
        # 地牢反馈真机发送接入同一后端（M3 遗留缺口：FeedbackExecutor 此前无 send 回调）
        if getattr(self.dungeon, "executor", None) is not None:
            self.dungeon.executor.send = self.backend.apply

        self.ws_clients: set[WebSocket] = set()
        self.tasks: list[asyncio.Task] = []
        self.auto_opened = self.mobile  # Mobile pairing never starts an unsolicited device turn.
        self.sensors_on = False
        self.sensor_watch_task: asyncio.Task | None = None
        self.layout: dict = {}  # 前端上报的三栏布局（监测/调试用）
        self.update = UpdateChecker(
            enabled=bool(self.cfg["app"].get("check_update", True)),
            url=self.cfg["app"].get("update_url", ""),
        )
        # 传感器运行时开关（不持久化；初始跟随 config.enabled）
        self.sensor_switches: dict[str, bool] = {
            "camera": bool(self.cfg["camera"].get("enabled", False)),
            "audio": bool(self.cfg["audio"].get("enabled", False)),
        }

    # ---------- 麦克风转写回调 ----------
    async def on_audio_text(self, text: str) -> None:
        self.loop.add_note(f"麦克风检测到玩家说：「{text}」")
        self.logger.info("麦克风信号已注入：%s", text)
        await self.broadcast()

    # ---------- 麦克风呻吟回调（无文字片段按电平分级） ----------
    async def on_audio_moan(self, kind: str, level: float) -> None:
        if kind == "high":
            self.loop.add_note(
                "麦克风检测到玩家发出较大的呻吟/惨叫（音量高）：应降低强度、安抚并关心，不要继续加码。"
            )
        else:
            self.loop.add_note(
                "麦克风检测到玩家发出普通呻吟/呜呜声（音量中等）：挑逗等级可逐渐增加，小幅加码。"
            )
        self.logger.info("麦克风呻吟信号注入：%s（%.3f）", kind, level)
        await self.broadcast()

    # ---------- 中继事件 ----------
    async def on_relay_event(self, event: str, payload: dict) -> None:
        if event == "slots_patch":
            # 取第一台设备的 props/slotState 同步给安全层
            client = self.backend.client_state()
            if client:
                self.safety.update_device_state(
                    client.get("props"), client.get("slotState")
                )
        elif event == "client_attached" and not self.auto_opened:
            # 首次配对成功：AI 主动开场（挑逗 + 第一个轻微试探）
            self.auto_opened = True
            asyncio.create_task(self._auto_open_and_broadcast())
        if event == "client_disconnected" and self.cfg["safety"]["auto_clear_on_disconnect"]:
            self.loop.on_client_disconnected()
            self.logger.warning("APP 断开，自动清零并停止循环波形")
        await self.broadcast()

    async def _on_backend_disconnect(self, payload: dict) -> None:
        """设备/链路断开（BLE notify 超时、中继断开等）：按配置自动清零。"""
        if self.cfg["safety"]["auto_clear_on_disconnect"]:
            self.loop.on_client_disconnected()
        self.logger.warning("设备后端断开，已按配置处理: %s", payload)
        await self.broadcast()

    async def _auto_open_and_broadcast(self) -> None:
        try:
            # 配对成功后缓 3 秒再开场，给玩家反应时间
            await asyncio.sleep(3)
            # GameLoop publishes automatic turns while holding the shared
            # conversation slot so queued user replies cannot overtake them.
            await self.loop.auto_open()
            self.logger.info("AI 主动开场完成")
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("主动开场失败: %s", exc)

    async def on_relay_action(self, action: int, client_id: str) -> None:
        await self.loop.handle_feedback(action, client_id)
        await self.broadcast()

    # ---------- 广播 ----------
    async def broadcast(self) -> None:
        await self._broadcast_message({"type": "state", "data": self.build_state()})

    async def _broadcast_message(self, message: dict) -> None:
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=1.0)
            except Exception:
                self.ws_clients.discard(ws)
        await asyncio.gather(*(send(ws) for ws in list(self.ws_clients)))

    async def broadcast_chat(self, result: dict) -> None:
        """把 AI 主动生成的台词推送到页面聊天区。"""
        await self._broadcast_message({"type": "chat", **result})

    def persona_key(self) -> str:
        character = self.cfg.get("character", {})
        return json.dumps([str(character.get(key) or "") for key in ("role", "profile", "lang")], ensure_ascii=False)

    async def archive_turn(self, event: dict) -> None:
        if not self.archive:
            return
        result = event["result"]
        conversation = self.archive.active_id
        request = CHAT_REQUEST_CONTEXT.get() if event.get("source") in ("user", "safety") else None
        if request and request[0] != conversation:
            result["persistence_error"] = "本条回复未写入当前聊天，请保留当前页面"
            return
        turn_id = request[1] if request else uuid.uuid4().hex
        try:
            result["messages"] = self.archive.append_turn(conversation, turn_id, event.get("user_text"), result,
                history=self.loop.history[-self.loop.keep:], persona=self.persona_key())
            result["conversation_id"] = conversation
            self.archive_error = ""
        except Exception:
            self.archive_error = "聊天记录保存失败，请检查存储空间；本条消息仍保留在当前页面"
            result["persistence_error"] = self.archive_error
            self.logger.error("聊天存档写入失败；不会重试模型或设备操作")

    def save_preferences(self, changes: dict) -> None:
        if not self.mobile:
            return
        if self.preferences is None:
            raise HTTPException(503, "已保存的设置暂不可读取，请先检查存储后重启")
        try:
            self.preferences.save(changes)
        except (OSError, ValueError):
            raise HTTPException(503, "设置保存失败，请检查存储空间后重试") from None

    def build_state(self) -> dict:
        state = self.loop.build_state()
        state["chat_session_id"] = self.chat_requests.session_id
        state["model_judgment"] = self.cfg.get("interaction", {}).get("model_judgment") is True
        if self.archive:
            state["conversation_id"] = self.archive.active_id
            state["conversation_title"] = self.archive.title(self.archive.active_id)
        if self.mobile:
            state["archive_error"] = self.archive_error
            state["device_preferences"] = self.preferences.device(self.safety.presets) if self.preferences else {
                "focus_channel": "A", "manual_link": False, "last_presets": {"A": None, "B": None}}
        state["sensors_on"] = self.sensors_on
        state["sensors"] = dict(self.sensor_switches)
        state["relay"] = self.backend.to_state()
        state["device_backend"] = self.backend.name
        state["test_mode"] = self._test_mode
        state["audio"] = self.audio.to_state()
        state["layout"] = dict(self.layout)
        state["update"] = self.update.to_state(_app_version())
        state["character"] = self.cfg["character"]["name"]
        state["lang"] = str(self.cfg["character"].get("lang") or "zh")
        state["en_available"] = bool(self.cfg["character"].get("en_available"))
        state["dungeon"] = self.dungeon.to_state()
        if self.mobile:
            state["platform"] = "android"
            state["capabilities"] = {
                "camera": False, "audio": False, "dungeon": False, "ble": False,
                "chat": True, "autopilot": True, "relay": True, "waveforms": True,
            }
        state["config_info"] = {
            "model": self.cfg["llm"]["model"],
            "character_file": str(self.cfg["character_file"]),
            "waveforms_file": str(PROJECT_ROOT / "config" / "waveforms.yaml"),
            "title": str(self.cfg["app"].get("title", "郊狼 · AI 驯服师")),
            "profile": str(self.cfg["character"].get("profile") or "调教"),
            "player_nick": str(self.cfg["character"].get("player_nick") or "小柳"),
            "version": _app_version(),
        }
        return state

    # ---------- 测试模式（不连郊狼，模拟设备全流程试跑） ----------
    def _sim_backend_instance(self):
        """懒创建模拟后端（测试模式用）。"""
        if self._sim_backend is None:
            from backend.device.sim_backend import SimulatedBackend

            self._sim_backend = SimulatedBackend()
        return self._sim_backend

    def _attach_backend(self, backend) -> None:
        """把某个后端挂到所有消费点上（loop / 地牢执行器 / 本对象状态）。"""
        self.backend = backend
        self.loop.backend = backend
        if getattr(self.dungeon, "executor", None) is not None:
            self.dungeon.executor.send = backend.apply

    async def set_test_mode(self, on: bool) -> dict:
        """开/关测试模式：开 = 换模拟后端 + 强制 dry-run + 假装配对成功；关 = 恢复真实中继。"""
        on = bool(on)
        # 串行化开关：连点/前后两次请求交叠时，避免 stop/start 与 backend 换绑交错执行
        async with self._test_mode_lock:
            if on == self._test_mode:
                return {"ok": True, "test_mode": self._test_mode}
            if on:
                # 前置 dry-run：切换窗口（stop 真实中继期间）进来的动作一律按模拟兜底，
                # 避免 race 报「设备未连接（无 clientId/slotId）」（T058 P2.1）
                self.safety.dry_run = True               # 全体标「模拟」+ AI 被告知 dry-run
                await self._real_backend.stop()          # 停真实中继重连任务
                self._attach_backend(self._sim_backend_instance())
                self._test_mode = True
                self.logger.info("已进入测试模式：模拟设备已配对，不发送真实命令")
                # 与真实配对一致：缓 3 秒让 AI 主动开场
                if not self.auto_opened:
                    self.auto_opened = True
                    asyncio.create_task(self._auto_open_and_broadcast())
            else:
                sim = self._sim_backend
                if sim is not None:
                    await sim.stop()
                self._attach_backend(self._real_backend)
                self.safety.dry_run = bool(self.cfg["app"].get("dry_run", True))
                self._test_mode = False
                # 清掉模拟态残留的强度/波形，避免退出后 UI 显示模拟数值（真实设备 slots_patch 覆盖前）
                self.loop.on_client_disconnected()
                self.logger.info("已退出测试模式，恢复真实设备配对")
                asyncio.create_task(self._real_backend.start())
            await self.broadcast()
            return {"ok": True, "test_mode": self._test_mode}

    # ---------- 传感器开关（跟随自动运行；浏览器断开超时自动关） ----------
    async def set_sensors(self, on: bool) -> None:
        """自动运行开启时启动「开关为开」的传感器，关闭时全部停止（config.enabled 为初始默认）。"""
        if self.mobile:
            self.sensors_on = False
            return
        self.sensors_on = bool(on)
        if on:
            if self.sensor_switches.get("camera"):
                await self.camera.start()
            else:
                await self.camera.stop()
            if self.sensor_switches.get("audio"):
                await self.audio.start()
            else:
                await self.audio.stop()
        else:
            await self.camera.stop()
            await self.audio.stop()

    def _on_ws_clients_change(self) -> None:
        """有浏览器接入：重启传感器（自动运行开着时）；全断开：延迟关传感器。"""
        if self.mobile:
            return
        if self.ws_clients:
            if self.sensor_watch_task:
                self.sensor_watch_task.cancel()
                self.sensor_watch_task = None
            if self.loop.autopilot and not self.sensors_on:
                asyncio.create_task(self.set_sensors(True))
        elif self.sensor_watch_task is None:
            self.sensor_watch_task = asyncio.create_task(self._watch_sensors_idle())

    async def _watch_sensors_idle(self) -> None:
        timeout = float(self.cfg["app"].get("sensor_idle_timeout_s", 30))
        try:
            await asyncio.sleep(timeout)
        finally:
            self.sensor_watch_task = None
        if not self.ws_clients:
            self.logger.info("浏览器已断开 %.0fs，自动关闭摄像头/麦克风", timeout)
            await self.set_sensors(False)
            await self.broadcast()

    async def _sensor_watchdog(self) -> None:
        """看门狗：开关开着但传感器没在跑且有错误时，每 15s 自动重试启动（拔插设备自恢复）。"""
        while True:
            await asyncio.sleep(15)
            try:
                if not self.sensors_on:
                    continue
                cam_bad = (
                    self.sensor_switches.get("camera")
                    and not self.camera.has_frame()
                    and bool(self.camera.error)
                )
                mic_bad = (
                    self.sensor_switches.get("audio")
                    and not self.audio.to_state().get("running")
                )
                if cam_bad or mic_bad:
                    self.logger.info("传感器看门狗重试启动（摄像头=%s 麦克风=%s）", cam_bad, mic_bad)
                    await self.set_sensors(True)
                    await self.broadcast()
            except Exception:  # noqa: BLE001
                self.logger.exception("传感器看门狗异常")

    # ---------- 更新检测 ----------
    async def _update_loop(self) -> None:
        """启动查一次 + 每 6 小时静默复查；结果写进状态供顶栏徽章展示。"""
        while True:
            await self.update.check()
            await self.broadcast()
            await asyncio.sleep(6 * 3600)

    # ---------- 生命周期 ----------
    async def start_background(self) -> None:
        self.tasks.append(asyncio.create_task(self.backend.start()))
        if self.mobile:
            return
        self.tasks.append(asyncio.create_task(self._sensor_watchdog()))
        self.tasks.append(asyncio.create_task(self._update_loop()))
        # 配置里自动运行开着时，启动真正的循环任务（此前只置状态、不启动任务，
        # 导致重启后「假开真停」：AI 一直不说话）
        if self.loop.autopilot:
            self.loop.set_autopilot(True)
        # 自动运行开着也只在「已有浏览器接入」时才启动传感器；
        # 无浏览器时不占摄像头/麦克风，等页面连上后由 _on_ws_clients_change 再启动
        if self.loop.autopilot and self.ws_clients:
            await self.set_sensors(True)
        self.loop.start_observe_loop()

    async def shutdown(self, *, stop_devices: bool = True) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        if self.mobile:
            await self._shutdown_mobile(stop_devices=stop_devices)
            return
        # 退出时急停（若配置了断开自动清零）
        self.loop.stop_observe_loop()
        self.loop.set_autopilot(False)
        await self.camera.stop()
        await self.audio.stop()
        if self.cfg["safety"]["auto_clear_on_disconnect"]:
            with contextlib.suppress(Exception):
                await self.loop.estop()
        await self.chat_requests.close()
        with contextlib.suppress(Exception):
            await self.backend.stop()
        for task in self.tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await self.llm.client.aclose()

    async def _shutdown_mobile(self, *, stop_devices: bool) -> None:
        """Clean up all mobile resources and retain any stop/cleanup failure."""
        failed = False
        self.loop.stop_observe_loop()
        self.loop.set_autopilot(False)
        for operation in (self.camera.stop, self.audio.stop):
            try:
                await operation()
            except Exception:
                failed = True
        if stop_devices:
            must_send = self.backend.ready() and not self.safety.dry_run
            try:
                result = await self.loop.estop()
                failed = failed or (must_send and (not result.get("sent") or result.get("status") in ("failed", "unconfirmed")))
            except Exception:
                failed = True
        await self.chat_requests.close()
        try:
            await self.backend.stop()
        except Exception:
            failed = True
        for task in self.tasks:
            task.cancel()
        try:
            await self.llm.client.aclose()
        except Exception:
            failed = True
        if self.archive:
            self.archive.close()
            self.archive = None
        if failed:
            raise RuntimeError("Android shutdown could not confirm device stop or resource cleanup")


def make_app(*, mobile_token: str | None = None, mobile_port: int | None = None) -> FastAPI:
    cfg = load_config()
    if cfg.get("_mobile"):
        if not mobile_token or not mobile_port:
            raise RuntimeError("Android server requires a native session")
        cfg["app"]["port"] = mobile_port
    state = AppState(cfg)
    app = FastAPI(title="AI Coyote Tamer")
    if state.mobile:
        from .mobile_auth import MobileSessionMiddleware
        app.add_middleware(MobileSessionMiddleware, token=mobile_token, port=mobile_port)
    app.state.runtime = state
    character_searches: dict[str, tuple[float, dict]] = {}
    judgment_confirmations: dict[str, float] = {}

    def require_archive():
        if not state.archive:
            raise HTTPException(503, state.archive_error or "当前版本不提供长期聊天记录")
        return state.archive

    def conversation_busy() -> bool:
        return state._conversation_changing or state.loop.turn_busy or state.loop.pending_user_turns > 0 or state.chat_requests.has_pending

    @contextlib.asynccontextmanager
    async def edit_character():
        if state.mobile and conversation_busy():
            raise HTTPException(409, "请等待当前消息处理完成后再更换角色")
        state._conversation_changing = True
        try:
            async with state.loop.conversation_edit():
                yield
        finally:
            state._conversation_changing = False

    def begin_role_conversation():
        state.loop.clear_history()
        state.loop.notes.clear()
        if state.archive:
            state.archive.create()

    async def stop_for_conversation_change():
        state.loop.set_autopilot(False)
        state.loop.stop_observe_loop()
        must_send = state.backend.ready() and not state.safety.dry_run
        stopped = await state.loop.estop()
        if must_send and (not stopped.get("sent") or stopped.get("status") in ("failed", "unconfirmed")):
            await state.broadcast()
            raise HTTPException(503, "未能确认设备停止，已锁定输出；请检查设备连接后再切换聊天或角色")

    @app.on_event("startup")
    async def startup() -> None:
        await state.start_background()
        state.logger.info(
            "启动完成。Web: http://%s:%s  dry_run=%s",
            cfg["app"]["host"], cfg["app"]["port"], cfg["app"]["dry_run"],
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await state.shutdown()

    # ---------- 页面 ----------
    @app.get("/")
    async def index() -> Response:
        # 托管 React 前端构建产物；未构建时给出一句构建提示
        if (FRONTEND_DIST / "index.html").exists():
            return FileResponse(FRONTEND_DIST / "index.html")
        return Response(
            "前端尚未构建：请在 frontend\\ 目录执行 npm install && npm run build",
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/index.html")
    async def index_html() -> RedirectResponse:
        """收藏夹/手输带 index.html 的地址时别 404，重定向回首页。"""
        return RedirectResponse("/")

    # React 构建产物的静态资源（存在时才挂载）
    if (FRONTEND_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        return JSONResponse(state.build_state())

    @app.get("/api/qrcode.png")
    async def api_qrcode() -> Response:
        controller_id = state.backend.controller_id()
        if not controller_id:
            return Response(
                "设备未就绪：dglab 后端需中继已连接；coyote2_ble 后端无二维码（走 BLE 扫描）",
                status_code=503,
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        lan_ip = cfg["relay"]["lan_ip"]
        if lan_ip == "auto":
            lan_ip = get_lan_ip()
        url = build_pair_url(cfg, lan_ip, controller_id)
        if state.mobile:
            from qrcode.image.pure import PyPNGImage
            img = qrcode.make(url, image_factory=PyPNGImage)
        else:
            img = qrcode.make(url)
        buf = io.BytesIO()
        if state.mobile:
            img.save(buf)
        else:
            img.save(buf, format="PNG")
        return Response(
            content=buf.getvalue(), media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/pair_url")
    async def api_pair_url() -> JSONResponse:
        controller_id = state.backend.controller_id()
        if not controller_id:
            return JSONResponse(
                {"error": "设备未就绪（dglab 中继未连接；coyote2_ble 无二维码）"},
                status_code=503, headers={"Cache-Control": "no-store"},
            )
        lan_ip = cfg["relay"]["lan_ip"]
        if lan_ip == "auto":
            lan_ip = get_lan_ip()
        return JSONResponse(
            {"url": build_pair_url(cfg, lan_ip, controller_id)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/network")
    async def api_network() -> JSONResponse:
        """网络自检：本机 IP 列表 + 当前配对地址（dglab 后端用）。"""
        controller_id = state.backend.controller_id()
        lan_ip = cfg["relay"]["lan_ip"]
        if lan_ip == "auto":
            lan_ip = get_lan_ip()
        pair_url = (
            build_pair_url(cfg, lan_ip, controller_id) if controller_id else None
        )
        relay_address = urllib.parse.urlsplit(pairing_relay_url(cfg, lan_ip))
        relay_port = relay_address.port or (443 if relay_address.scheme == "wss" else 80)
        probe_url = urllib.parse.urlunsplit(relay_address._replace(
            scheme="https" if relay_address.scheme == "wss" else "http",
        ))
        return JSONResponse(
            {
                "lan_ip": lan_ip,
                "all_ips": get_local_ips(),
                "pair_url": pair_url,
                "public_url": str(cfg["relay"].get("public_url") or ""),
                "relay_port": relay_port,
                "hint": (
                    f"手机浏览器打开 {probe_url} 若立即显示 "
                    "'WebSocket upgrade required' 说明链路通；超时说明被防火墙/热点隔离拦截。"
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    # ---------- 控制 ----------
    @app.post("/api/chat")
    async def api_chat(body: dict) -> JSONResponse:
        message = body.get("message", "")
        mode = body.get("mode", "auto")
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            return JSONResponse({"error": "消息须为 1–8000 字"}, status_code=400)
        if mode not in ("auto", "text", "device"):
            return JSONResponse({"error": "mode 只能是 auto/text/device"}, status_code=400)
        pattern = body.get("preferred_pattern") or None
        if pattern is not None and (not isinstance(pattern, str) or pattern not in state.safety.presets):
            return JSONResponse({"error": "所选波形不存在，请重新选择"}, status_code=400)
        conversation = state.archive.active_id if state.archive else None
        requested_conversation = body.get("conversation_id")
        if state._conversation_changing or (requested_conversation is not None and requested_conversation != conversation):
            return JSONResponse({"error": "聊天已切换，请确认当前聊天后再发送", "retryable": False}, status_code=409)
        accepted_action_epoch = state.loop._action_epoch
        async def run():
            if state.archive and state.archive.active_id != conversation:
                return {"line": "", "executed": [], "dropped": [], "error": "聊天已切换，此消息未执行", "retryable": False}
            token = CHAT_REQUEST_CONTEXT.set((conversation, body.get("request_id") or uuid.uuid4().hex))
            try:
                result = await state.loop.handle_user_message(
                    message, control_device=None if mode == "auto" else mode == "device", preferred_pattern=pattern,
                    accepted_action_epoch=accepted_action_epoch,
                )
                await state.broadcast()
                return result
            finally:
                CHAT_REQUEST_CONTEXT.reset(token)
        if "request_id" not in body:
            return JSONResponse(await run())
        try:
            receipt = state.chat_requests.submit(body["request_id"], {"message": message, "mode": mode, "pattern": pattern, "conversation_id": conversation}, run)
            return JSONResponse(receipt, status_code=202 if receipt["status"] == "pending" else 200)
        except ReceiptError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.get("/api/chat/result/{request_id}")
    async def api_chat_result(request_id: str) -> JSONResponse:
        try:
            return JSONResponse(state.chat_requests.get(request_id), headers={"Cache-Control": "no-store"})
        except ReceiptError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.get("/api/conversations")
    async def api_conversations(before: str | None = None, limit: int = 100) -> JSONResponse:
        try:
            return JSONResponse(require_archive().list(before=before, limit=limit), headers={"Cache-Control": "no-store"})
        except ConversationListChanged as exc:
            return JSONResponse({"error": str(exc), "code": "conversation_list_changed"}, status_code=409)
        except KeyError:
            raise HTTPException(404, "聊天不存在") from None

    @app.get("/api/conversations/{conversation_id}/messages")
    async def api_conversation_messages(conversation_id: str, before: str | None = None, limit: int = 100) -> JSONResponse:
        try:
            return JSONResponse(require_archive().messages(conversation_id, before=before, limit=limit), headers={"Cache-Control": "no-store"})
        except KeyError:
            raise HTTPException(404, "聊天或消息不存在") from None

    async def change_conversation(conversation_id: str | None) -> JSONResponse:
        archive = require_archive()
        if conversation_busy():
            return JSONResponse({"error": "请等待当前消息处理完成后再切换聊天"}, status_code=409)
        if conversation_id is not None:
            try:
                archive.title(conversation_id)
            except KeyError:
                raise HTTPException(404, "聊天不存在") from None
        state._conversation_changing = True
        try:
            async with state.loop.conversation_edit():
                await stop_for_conversation_change()
                selected = archive.create() if conversation_id is None else archive.select(conversation_id)
                # Once the active record has changed, never keep the previous
                # role's in-memory context if a later settings write fails.
                state.loop.clear_history()
                state.loop.notes.clear()
                if conversation_id is not None:
                    saved_persona = archive.persona(conversation_id)
                    if saved_persona:
                        try:
                            saved_role, saved_profile, saved_language = json.loads(saved_persona)
                            available = any(r.get("name") == saved_role and any(p.get("name") == saved_profile and p.get("available")
                                for p in r.get("profiles", [])) for r in cfg["character"].get("roles", []))
                            if available:
                                save_character_runtime(cfg, role=saved_role, profile=saved_profile, lang=saved_language or "zh")
                        except (ValueError, TypeError):
                            pass
                state.loop.history = archive.context(archive.active_id, state.persona_key(), state.loop.keep)
            await state.broadcast()
            return JSONResponse(selected)
        except (OSError, sqlite3.Error):
            state.loop.clear_history()
            state.loop.notes.clear()
            state.archive_error = "聊天切换未能保存，请检查存储空间后重试"
            await state.broadcast()
            raise HTTPException(503, state.archive_error) from None
        finally:
            state._conversation_changing = False

    @app.post("/api/conversations")
    async def api_conversation_create() -> JSONResponse:
        return await change_conversation(None)

    @app.post("/api/conversations/{conversation_id}/select")
    async def api_conversation_select(conversation_id: str) -> JSONResponse:
        return await change_conversation(conversation_id)

    @app.post("/api/conversations/{conversation_id}/pin")
    async def api_conversation_pin(conversation_id: str, body: dict) -> JSONResponse:
        pinned = body.get("pinned")
        if type(pinned) is not bool:
            raise HTTPException(400, "pinned 必须是布尔值")
        try:
            return JSONResponse(require_archive().set_pinned(conversation_id, pinned))
        except KeyError:
            raise HTTPException(404, "聊天不存在") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "置顶状态未能保存，请检查存储空间后重试") from None

    @app.delete("/api/conversations/{conversation_id}")
    async def api_conversation_delete(conversation_id: str) -> JSONResponse:
        archive = require_archive()
        try:
            archive.title(conversation_id)
        except KeyError:
            raise HTTPException(404, "聊天不存在") from None
        deleting_active = conversation_id == archive.active_id
        if state._conversation_changing or (deleting_active and conversation_busy()):
            return JSONResponse({"error": "请等待当前消息或聊天切换处理完成后再删除"}, status_code=409)
        if not deleting_active:
            try:
                return JSONResponse(archive.delete(conversation_id))
            except (OSError, sqlite3.Error):
                raise HTTPException(503, "聊天删除未能保存，请检查存储空间后重试") from None
        state._conversation_changing = True
        try:
            async with state.loop.conversation_edit():
                await stop_for_conversation_change()
                result = archive.delete(conversation_id)
                state.loop.clear_history()
                state.loop.notes.clear()
            await state.broadcast()
            return JSONResponse(result)
        except (OSError, sqlite3.Error):
            # SQLite rolls back the whole deletion. Keep that chat selected and its
            # context intact, while the stop latch remains in effect.
            await state.broadcast()
            raise HTTPException(503, "聊天删除未能保存，请检查存储空间后重试") from None
        finally:
            state._conversation_changing = False

    @app.post("/api/character/model-judgment/prepare")
    async def api_model_judgment_prepare() -> JSONResponse:
        if conversation_busy():
            return JSONResponse({"error": "请等待当前消息处理完成"}, status_code=409)
        now = time.monotonic()
        for key, created in list(judgment_confirmations.items()):
            if now - created > 300:
                judgment_confirmations.pop(key, None)
        # One pending confirmation: reopening always starts a fresh five-second wait.
        judgment_confirmations.clear()
        token = uuid.uuid4().hex
        judgment_confirmations[token] = now
        return JSONResponse({"confirmation_token": token, "wait_seconds": 5})

    @app.post("/api/character/model-judgment")
    async def api_model_judgment(body: dict) -> JSONResponse:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return JSONResponse({"error": "enabled 必须是布尔值"}, status_code=400)
        if enabled and conversation_busy():
            return JSONResponse({"error": "请等待当前消息处理完成"}, status_code=409)
        if not enabled:
            state.save_preferences({"model_judgment": False})
            cfg.setdefault("interaction", {})["model_judgment"] = False
            # Returning to conservative interpretation also invalidates any pending
            # device decision made under the previously enabled mode.
            state.loop._action_epoch += 1
            judgment_confirmations.clear()
            await state.broadcast()
            return JSONResponse({"ok": True, "model_judgment": False})
        if enabled:
            token = body.get("confirmation_token")
            created = judgment_confirmations.get(token) if isinstance(token, str) else None
            age = time.monotonic() - created if created is not None else -1
            if age < 5 or age > 300:
                return JSONResponse({"error": "请阅读提示并等待 5 秒后确认；过期请重新开启"}, status_code=409)
        async with state.loop.conversation_edit():
            state.save_preferences({"model_judgment": enabled})
            cfg.setdefault("interaction", {})["model_judgment"] = enabled
            judgment_confirmations.clear()
        await state.broadcast()
        return JSONResponse({"ok": True, "model_judgment": enabled})

    @app.post("/api/estop")
    async def api_estop() -> JSONResponse:
        if state.mobile:
            state.loop.set_autopilot(False)
            state.loop.stop_observe_loop()
        result = await state.loop.estop()
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/resume")
    async def api_resume() -> JSONResponse:
        if state.mobile and state._conversation_changing:
            raise HTTPException(409, "正在切换聊天或角色，请稍后解除急停")
        if state.mobile:
            # Unlocking is one request and never restarts a paused automatic loop.
            state.loop.set_autopilot(False)
            state.loop.stop_observe_loop()
        result = await state.loop.resume()
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/manual")
    async def api_manual(body: dict) -> JSONResponse:
        """手动控制：与 AI 指令走完全相同的安全链路。"""
        if not isinstance(body, dict) or "op" not in body:
            return JSONResponse({"error": "缺少 op"}, status_code=400)
        if state.mobile and state._conversation_changing and body["op"] not in ("clear", "stop"):
            raise HTTPException(409, "正在切换聊天或角色，请稍后调整设备")
        if body["op"] == "clear":
            try:
                channel = state.safety.norm_channel(body["channel"]) if body.get("channel") is not None else None
            except SafetyError as exc:
                raise HTTPException(400, str(exc)) from None
            result = await state.loop.clear_channel(channel)
            executed, dropped = result["executed"], result["dropped"]
        elif body["op"] == "stop":
            stopped = await state.loop.estop()
            executed, dropped = [{"action": body, "command": {"kind": "stop"},
                "label": "A/B 停止并清零", "sent": stopped["sent"],
                "status": stopped["status"], "reason": stopped["reason"]}], []
        else:
            executed, dropped = await state.loop.execute_actions([body])
        await state.broadcast()
        return JSONResponse({"executed": executed, "dropped": dropped})

    @app.post("/api/device/channels")
    async def api_device_channels(body: dict) -> JSONResponse:
        """通道配件设置：{A:{name,location}, B:{...}}，保存到 device_channels.yaml。"""
        try:
            save_device_channels(cfg, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await state.broadcast()
        return JSONResponse({"ok": True, "device_channels": cfg["device_channels"]})

    @app.post("/api/device/channels/enabled")
    async def api_device_channel_enabled(body: dict) -> JSONResponse:
        """手动开关通道：{channel:"A", enabled:false}。关闭的通道拒绝一切动作并清零。"""
        ch = str(body.get("channel") or "").strip().upper()
        if ch not in ("A", "B"):
            return JSONResponse({"error": "channel 只能是 A 或 B"}, status_code=400)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return JSONResponse({"error": "enabled 必须是布尔值"}, status_code=400)
        try:
            save_device_channels(cfg, {ch: {"enabled": enabled}})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        result = {"executed": [], "dropped": []}
        if enabled:
            state.safety.set_channel_enabled(ch, True)
        else:
            result = await state.loop.disable_channel(ch)
        await state.broadcast()
        return JSONResponse({"ok": True, "enabled_channels": state.safety.enabled, **result})

    @app.post("/api/sensors")
    async def api_sensors(body: dict) -> JSONResponse:
        """运行时单独开关摄像头/麦克风：{camera: bool, audio: bool}（可只传一项；不持久化）。"""
        if state.mobile and any(body.get(key) for key in ("camera", "audio")):
            return JSONResponse({"error": "Android 版暂不支持摄像头和麦克风"}, status_code=501)
        changed = False
        for key in ("camera", "audio"):
            if key in body and isinstance(body[key], bool):
                if state.sensor_switches.get(key) != body[key]:
                    state.sensor_switches[key] = body[key]
                    changed = True
        if changed:
            # 传感器正在运行时按最新开关重新对齐（关掉的立即停）
            if state.sensors_on:
                await state.set_sensors(True)
            await state.broadcast()
        return JSONResponse({"ok": True, "sensors": state.sensor_switches})

    @app.post("/api/history/clear")
    async def api_history_clear(body: dict) -> JSONResponse:
        """清空对话历史（模型上下文 + 页面记录由前端同步清）。"""
        async with state.loop.conversation_edit():
            state.loop.clear_history()
            if state.archive:
                state.archive.clear(state.archive.active_id)
        return JSONResponse({"ok": True})

    @app.post("/api/layout")
    async def api_layout(body: dict) -> JSONResponse:
        """前端上报三栏布局（调试/监测用）：{sidebar_w, control_w, inner_width, zoom}。"""
        try:
            state.layout = {
                "sidebar_w": float(body.get("sidebar_w", 0)),
                "control_w": float(body.get("control_w", 0)),
                "inner_width": float(body.get("inner_width", 0)),
                "zoom": float(body.get("zoom", 1.0)),
            }
        except (TypeError, ValueError):
            return JSONResponse({"error": "数值格式错误"}, status_code=400)
        return JSONResponse({"ok": True, "layout": state.layout})

    @app.post("/api/device/channels/cap")
    async def api_device_channel_cap(body: dict) -> JSONResponse:
        """设置并保存通道上限；重启只恢复设置，不恢复输出。"""
        ch = str(body.get("channel") or "").strip().upper()
        if ch not in ("A", "B"):
            return JSONResponse({"error": "channel 只能是 A 或 B"}, status_code=400)
        try:
            value = int(body.get("value", 100))
        except (TypeError, ValueError):
            return JSONResponse({"error": "value 必须是整数"}, status_code=400)
        v = max(1, min(state.safety.caps[ch], value))
        previous_cap = state.safety.cap_for(ch)
        state.save_preferences({"user_caps": {**state.safety.user_caps, ch: v}})
        state.safety.set_user_cap(ch, v)
        # 上限低于当前强度时，立即把设备强度降下来
        effective = state.safety.cap_for(ch)
        if effective < previous_cap and state.backend.strength_pending(ch):
            # An in-flight target may exceed the new cap even while current
            # still reports zero. Invalidate it and clear, never raise to cap.
            await state.loop.clear_channel(ch)
        elif state.safety.current[ch] > effective:
            await state.loop.execute_actions(
                [{"op": "hold_strength", "channel": ch, "value": effective}]
            )
        await state.broadcast()
        return JSONResponse(
            {
                "ok": True,
                "user_caps": state.safety.user_caps,
                "effective_caps": {c: state.safety.cap_for(c) for c in ("A", "B")},
            }
        )

    @app.post("/api/intensity")
    async def api_intensity(body: dict) -> JSONResponse:
        """六档强度默认联动 A/B；不改变通道上限，不在切档瞬间主动输出。"""
        level = str(body.get("level") or "").strip()
        if level not in (*SafetyManager.INTENSITY_LEVELS, "轻", "重"):
            return JSONResponse({"error": "level 只能是 低/中/高/极高/最高/炼狱"}, status_code=400)
        sync = body.get("sync_to_device")
        if sync is not None and not isinstance(sync, bool):
            return JSONResponse({"error": "sync_to_device 必须是布尔值"}, status_code=400)
        state.save_preferences({"intensity_level": {"轻": "低", "重": "高"}.get(level, level),
                                "intensity_device_link": state.safety.intensity_device_link if sync is None else sync})
        state.safety.set_intensity_level(level, sync)
        await state.broadcast()
        return JSONResponse(
            {
                "ok": True,
                "intensity_level": state.safety.intensity_level,
                "intensity_device_link": state.safety.intensity_device_link,
                "strength_scale": state.safety.scale,
                "effective_caps": {c: state.safety.cap_for(c) for c in ("A", "B")},
            }
        )

    @app.post("/api/device/preferences")
    async def api_device_preferences(body: dict) -> JSONResponse:
        if not state.mobile or not state.preferences:
            raise HTTPException(503, "当前版本无法保存设备偏好")
        if set(body) - {"focus_channel", "manual_link", "last_presets"}:
            raise HTTPException(400, "设备偏好字段无效")
        current = state.preferences.device(state.safety.presets)
        if "focus_channel" in body:
            if body["focus_channel"] not in ("A", "B"):
                raise HTTPException(400, "通道只能是 A 或 B")
            current["focus_channel"] = body["focus_channel"]
        if "manual_link" in body:
            if not isinstance(body["manual_link"], bool):
                raise HTTPException(400, "联动状态必须是布尔值")
            current["manual_link"] = body["manual_link"]
        if "last_presets" in body:
            presets = body["last_presets"]
            if not isinstance(presets, dict) or set(presets) - {"A", "B"}:
                raise HTTPException(400, "波形选择格式无效")
            for channel, pattern in presets.items():
                if pattern is not None and (not isinstance(pattern, str) or pattern not in state.safety.presets):
                    raise HTTPException(400, "所选波形不存在")
                current["last_presets"][channel] = pattern
        state.save_preferences({"device_preferences": current})
        await state.broadcast()
        return JSONResponse({"ok": True, "device_preferences": current})

    @app.post("/api/character/search")
    async def api_character_search(body: dict) -> JSONResponse:
        try:
            result = await search_character(body.get("query", ""))
        except CharacterSearchError as exc:
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status_code)
        now = time.monotonic()
        for token, (created, _) in list(character_searches.items()):
            if now - created > 900:
                character_searches.pop(token, None)
        while len(character_searches) >= 30:
            character_searches.pop(next(iter(character_searches)))
        token = uuid.uuid4().hex
        character_searches[token] = (now, result)
        return JSONResponse({**result, "search_id": token})

    async def create_character_response(body: dict) -> JSONResponse:
        token = str(body.get("search_id") or "")
        saved = character_searches.get(token)
        if not saved or time.monotonic() - saved[0] > 900:
            return JSONResponse({"error": "搜索结果已过期，请重新搜索"}, status_code=400)
        index = body.get("source_index")
        sources = saved[1].get("sources", [])
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(sources):
            return JSONResponse({"error": "请选择一条搜索结果"}, status_code=400)
        try:
            role = save_custom_role(PROJECT_ROOT, str(body.get("name") or sources[index]["title"]), sources[index], str(body.get("note") or ""), body.get("voiceId", "system-default"))
            if state.mobile:
                await stop_for_conversation_change()
            save_character_runtime(cfg, role=role, profile="角色扮演")
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": f"角色保存失败：{exc}"}, status_code=400)
        character_searches.pop(token, None)
        begin_role_conversation()
        return JSONResponse({"ok": True, "role": role, "profile": "角色扮演"})

    @app.post("/api/character/create")
    async def api_character_create(body: dict) -> JSONResponse:
        async with edit_character():
            response = await create_character_response(body)
        await state.broadcast()
        return response

    @app.post("/api/character/pin")
    async def api_character_pin(body: dict) -> JSONResponse:
        if not isinstance(body.get("pinned"), bool):
            return JSONResponse({"error": "置顶状态无效"}, status_code=400)
        role = body.get("role")
        try:
            async with state.loop.conversation_edit():
                pinned = set_custom_role_pinned(PROJECT_ROOT, role, body["pinned"])
                reload_character(cfg)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": f"置顶失败：{exc}"}, status_code=400)
        await state.broadcast()
        return JSONResponse({"ok": True, "role": role, "pinned": pinned})

    @app.post("/api/character/custom")
    async def api_character_custom(body: dict) -> JSONResponse:
        try:
            async with edit_character():
                role = save_user_role(PROJECT_ROOT, body.get("name"), body.get("personality"), body.get("background", ""), body.get("voiceId", "system-default"))
                if state.mobile:
                    await stop_for_conversation_change()
                save_character_runtime(cfg, role=role, profile="角色扮演")
                begin_role_conversation()
        except (ValueError, OSError) as exc:
            await state.broadcast()
            return JSONResponse({"error": f"角色保存失败：{exc}"}, status_code=400)
        await state.broadcast()
        return JSONResponse({"ok": True, "role": role, "profile": "角色扮演"})

    @app.post("/api/character/delete")
    async def api_character_delete(body: dict) -> JSONResponse:
        role = body.get("role")
        try:
            async with edit_character():
                validate_custom_role_deletion(PROJECT_ROOT, role)
                reload_character(cfg)
                if cfg["character"].get("role") == role:
                    fallback = next(((r["name"], p["name"]) for r in cfg["character"]["roles"]
                                     if r["name"] != role and not r.get("manageable")
                                     for p in r["profiles"] if p.get("available")), None)
                    if not fallback:
                        raise ValueError("没有可用的默认角色，请先安装默认角色")
                    state.loop.set_autopilot(False)
                    state.loop.stop_observe_loop()
                    must_send = state.backend.ready() and not state.safety.dry_run
                    try:
                        stopped = await state.loop.estop()
                    except Exception:
                        state.logger.exception("删除当前角色时未能确认设备停止")
                        raise ValueError("未能确认设备停止，已锁定输出；请检查设备连接后重试删除") from None
                    if must_send and (not stopped.get("sent") or stopped.get("status") in ("failed", "unconfirmed")):
                        raise ValueError("未能确认设备停止，已锁定输出；请检查设备连接后重试删除")
                    save_character_runtime(cfg, role=fallback[0], profile=fallback[1])
                    begin_role_conversation()
                deleted = delete_custom_role(PROJECT_ROOT, role)
                reload_character(cfg)
        except (ValueError, OSError) as exc:
            await state.broadcast()
            return JSONResponse({"error": f"删除失败：{exc}"}, status_code=400)
        await state.broadcast()
        return JSONResponse({"ok": True, "deleted_role": role, "role": cfg["character"]["role"],
                             "profile": cfg["character"]["profile"], "prompt_cleanup_pending": deleted.get("prompt_cleanup_pending", False)})

    async def character_profile_response(body: dict) -> JSONResponse:
        """切换角色/风格：{role: "触手", profile: "调教"}，保存并热加载；目标 DLC 未安装时拒绝。"""
        role = str(body.get("role") or "").strip()
        profile = str(body.get("profile") or "").strip()
        # 以文件当前状态为准：先热加载再校验，避免陈旧内存配置放行未安装的 DLC
        reload_character(cfg)
        roles = {r["name"]: r for r in (cfg["character"].get("roles") or [])}
        if not role:
            role = str(cfg["character"].get("role") or "")
        if role not in roles:
            return JSONResponse({"error": f"未知角色，可用：{list(roles)}"}, status_code=400)
        rmeta = roles[role]
        avail = {p["name"]: p["available"] for p in rmeta["profiles"]}
        if not profile:
            profile = avail and next(iter(avail))
        if profile not in avail:
            return JSONResponse({"error": f"未知风格版本，可用：{list(avail)}"}, status_code=400)
        if not avail.get(profile, True):
            return JSONResponse(
                {
                    "error": f"「{rmeta['label']}·{profile}」暂不可用",
                },
                status_code=400,
            )
        changed = (role, profile) != (cfg["character"]["role"], cfg["character"]["profile"])
        if changed and state.mobile:
            await stop_for_conversation_change()
        save_character_runtime(cfg, role=role, profile=profile)
        if changed:
            begin_role_conversation()
        return JSONResponse(
            {"ok": True, "role": cfg["character"]["role"], "profile": cfg["character"]["profile"]}
        )

    @app.post("/api/character/profile")
    async def api_character_profile(body: dict) -> JSONResponse:
        async with edit_character():
            response = await character_profile_response(body)
        await state.broadcast()
        return response

    @app.post("/api/character/nick")
    async def api_character_nick(body: dict) -> JSONResponse:
        """改玩家昵称：{nick: "..."}，保存并热加载。"""
        nick = str(body.get("nick") or "").strip()
        if not nick or len(nick) > 20:
            return JSONResponse({"error": "昵称不能为空且不超过 20 字"}, status_code=400)
        save_character_runtime(cfg, player_nick=nick)
        await state.broadcast()
        return JSONResponse({"ok": True, "player_nick": cfg["character"]["player_nick"]})

    @app.post("/api/character/lang")
    async def api_character_lang(body: dict) -> JSONResponse:
        """中英内容切换：{lang: "zh"|"en"}，保存并热加载；英文稿缺失时保持中文。"""
        lang = str(body.get("lang") or "").strip()
        if lang not in ("zh", "en"):
            return JSONResponse({"error": "lang 只能是 zh/en"}, status_code=400)
        async with edit_character():
            reload_character(cfg)
            # A missing English prompt resolves to Chinese before any transition.
            if lang == "en" and not cfg["character"].get("en_available"):
                lang = "zh"
            changed = lang != str(cfg["character"].get("lang") or "zh")
            if changed and state.mobile:
                await stop_for_conversation_change()
            save_character_runtime(cfg, lang=lang)
            if changed and state.mobile:
                begin_role_conversation()
        await state.broadcast()
        return JSONResponse(
            {
                "ok": True,
                "lang": str(cfg["character"].get("lang") or "zh"),
                "en_available": bool(cfg["character"].get("en_available")),
            }
        )

    # ---------- 内容/语言包安装（zh/en 大包 zip，程序内入口） ----------
    @app.post("/api/content/install")
    async def api_content_install(request: Request) -> JSONResponse:
        """接收内容包 zip 的原始字节，校验后合并进 content/，热加载角色与地牢主题包。"""
        data = await request.body()
        try:
            res = install_zip_bytes(data, PROJECT_ROOT / "content")
        except ContentInstallError as exc:
            en = str(cfg["character"].get("lang") or "zh") == "en"
            return JSONResponse({"error": exc.en if en else exc.zh}, status_code=400)
        # 热加载：角色清单/角色稿（EN 并列稿也在重载范围内）+ 地牢主题包
        try:
            reload_character(cfg)
        except Exception:  # noqa: BLE001
            state.logger.exception("内容包安装后角色热加载失败")
        try:
            state.dungeon._reload()
        except Exception:  # noqa: BLE001
            state.logger.exception("内容包安装后地牢主题包重载失败")
        await state.broadcast()
        return JSONResponse({"ok": True, **res})

    # ---------- 地牢（紫金地牢，M4） ----------
    @app.get("/api/dungeon/state")
    async def api_dungeon_state() -> JSONResponse:
        return JSONResponse(state.dungeon.to_state())

    @app.get("/api/dungeon/render")
    async def api_dungeon_render() -> JSONResponse:
        """上一帧 render（D11 E1）：前端刷新后恢复进行中视图；只读，不触发设备动作。"""
        try:
            return JSONResponse(state.dungeon.render())
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/dungeon/start")
    async def api_dungeon_start(body: dict) -> JSONResponse:
        try:
            result = await state.dungeon.start(
                active_themes=body.get("active_themes"),
                mix_policy=str(body.get("mix_policy") or "mixed_pool"),
                floors=int(body.get("floors") or 3),
                seed=body.get("seed"),
                map_mode=bool(body.get("map_mode", False)),
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/dungeon/advance")
    async def api_dungeon_advance(body: dict) -> JSONResponse:
        try:
            result = await state.dungeon.advance(
                choice_id=body.get("choice_id"),
                text=body.get("text"),
                map_target=body.get("map_target"),
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/dungeon/move")
    async def api_dungeon_move(body: dict) -> JSONResponse:
        """map 选路（D25）：{node_id} → render；仅 awaiting_move 且 reachable。"""
        try:
            result = await state.dungeon.move(node_id=body.get("node_id"))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/dungeon/save")
    async def api_dungeon_save(body: dict) -> JSONResponse:
        try:
            path = state.dungeon.save(str(body.get("slot") or "autosave"))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "path": path})

    @app.post("/api/dungeon/load")
    async def api_dungeon_load(body: dict) -> JSONResponse:
        try:
            result = await state.dungeon.load(str(body.get("slot") or "autosave"))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/dungeon/restart")
    async def api_dungeon_restart(body: dict) -> JSONResponse:
        state.dungeon.restart()
        await state.broadcast()
        return JSONResponse({"ok": True})

    @app.post("/api/autopilot")
    async def api_autopilot(body: dict) -> JSONResponse:
        """自动运行开关：{enabled: true/false}。AI 自主观察、调整设备并发言；摄像头/麦克风跟随启停。"""
        enabled = bool(body.get("enabled"))
        if state.mobile and enabled and state._conversation_changing:
            raise HTTPException(409, "正在切换聊天或角色，请稍后启用自动运行")
        if state.mobile and enabled and state.safety.estop_active:
            return JSONResponse({"error": "请先解除急停，再启用自动运行"}, status_code=409)
        state.loop.set_autopilot(enabled)
        await state.set_sensors(enabled)
        await state.broadcast()
        return JSONResponse({"ok": True, "autopilot": bool(state.loop.autopilot)})

    @app.post("/api/autopilot/interval")
    async def api_autopilot_interval(body: dict) -> JSONResponse:
        """自动运行间隔：{interval_s: 5~30}。改内存值并持久化到 config.yaml 的 autopilot.interval_s。"""
        try:
            value = float(body.get("interval_s", 12))
        except (TypeError, ValueError):
            return JSONResponse({"error": "间隔必须是 5–30 秒"}, status_code=400)
        value = state.loop.set_autopilot_interval(value)
        try:
            save_autopilot_interval(cfg, value)
        except Exception:  # noqa: BLE001
            state.logger.exception("自动运行间隔保存失败，保留内存值")
        await state.broadcast()
        return JSONResponse({"ok": True, "interval_s": value})

    @app.post("/api/test_mode")
    async def api_test_mode(body: dict) -> JSONResponse:
        """测试模式开关：{enabled: true/false}。不连郊狼，用模拟设备试跑全流程（不会真电击）。"""
        result = await state.set_test_mode(bool(body.get("enabled", False)))
        await state.broadcast()
        return JSONResponse(result)

    # ---------- AI 模型配置（设置页填写，保存即生效） ----------
    @app.get("/api/settings/llm")
    async def api_settings_llm() -> JSONResponse:
        llm = cfg.get("llm", {})
        key = str(llm.get("api_key") or "")
        masked = (
            key[:4] + "*" * (len(key) - 8) + key[-4:]
            if len(key) > 12
            else ("*" * len(key) or "")
        )
        return JSONResponse(
            {
                "base_url": str(llm.get("base_url", "")),
                "model": str(llm.get("model", "")),
                "api_key_masked": masked,
                "has_key": bool(key),
                "saved": (PROJECT_ROOT / "config" / "config.yaml").exists(),
                "json_mode": bool(llm.get("json_mode", True)),
            }
        )

    def _patch_llm_text(
        text: str, api_key: str, base_url: str, model: str, json_mode: bool | None
    ) -> str:
        """文本级更新 config.yaml 的 llm 小节（仅 2 空格缩进键），保留其余注释与内容。"""
        lines = text.splitlines()
        out: list[str] = []
        in_llm = False
        for ln in lines:
            if ln and not ln.startswith(" "):
                in_llm = ln.startswith("llm:")
                out.append(ln)
                continue
            if in_llm and ln.startswith("  ") and not ln.startswith("    "):
                key = ln.lstrip().split(":", 1)[0]
                if key == "api_key":
                    esc = (
                        api_key.replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", "\\n")
                        .replace("\r", "")
                    )
                    out.append(f'  api_key: "{esc}"')
                    continue
                if key == "base_url":
                    out.append(f'  base_url: "{base_url}"')
                    continue
                if key == "model":
                    out.append(f'  model: "{model}"')
                    continue
                if key == "json_mode" and json_mode is not None:
                    out.append(f"  json_mode: {str(bool(json_mode)).lower()}")
                    continue
            out.append(ln)
        return "\n".join(out) + "\n"

    def _llm_form_key(body: dict, base_url: str) -> tuple[str, str | None]:
        """Reuse a mobile saved key only on an explicit request to its saved URL."""
        api_key = str(body.get("api_key") or "").strip()
        if state.mobile and body.get("keep_api_key") is True and not api_key:
            saved = cfg.get("llm", {})
            if base_url.rstrip("/") != str(saved.get("base_url") or "").strip().rstrip("/"):
                return "", "服务地址已更改，请重新填写 API Key"
            api_key = str(saved.get("api_key") or "").strip()
            if not api_key:
                return "", "未找到已保存的 API Key，请重新填写"
        return api_key, None

    @app.post("/api/settings/llm")
    async def api_settings_llm_save(body: dict) -> JSONResponse:
        """保存 AI 配置；移动端可用 keep_api_key 显式保留同地址的已存密钥。"""
        base_url = str(body.get("base_url") or "").strip()
        model = str(body.get("model") or "").strip()
        json_mode = body.get("json_mode") if isinstance(body.get("json_mode"), bool) else None
        if not base_url or not model:
            return JSONResponse({"error": "地址与模型名不能为空"}, status_code=400)
        api_key, key_error = _llm_form_key(body, base_url)
        if key_error:
            return JSONResponse({"error": key_error}, status_code=400)
        cfg_path = PROJECT_ROOT / "config" / "config.yaml"
        example = PROJECT_ROOT / "config" / "config.example.yaml"
        if not cfg_path.exists():
            cfg_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        cfg_path.write_text(
            _patch_llm_text(
                cfg_path.read_text(encoding="utf-8"), api_key, base_url, model, json_mode
            ),
            encoding="utf-8",
        )
        # 同步内存并热加载（key 留空时回退环境变量 DGLAB_LLM_API_KEY）
        llm = cfg.setdefault("llm", {})
        llm["api_key"] = api_key or os.environ.get("DGLAB_LLM_API_KEY", "")
        llm["base_url"] = base_url
        llm["model"] = model
        if json_mode is not None:
            llm["json_mode"] = json_mode
        old = state.llm
        state.llm = LLM(cfg)
        state.loop.llm = state.llm
        with contextlib.suppress(Exception):
            await old.client.aclose()
        await state.broadcast()
        return JSONResponse({"ok": True, "model": model})

    @app.post("/api/settings/llm/test")
    async def api_settings_llm_test(body: dict) -> JSONResponse:
        """测试连接：用表单值发一条最小请求（不保存）。"""
        base = str(body.get("base_url") or "").strip()
        model = str(body.get("model") or "").strip()
        api_key, key_error = _llm_form_key(body, base)
        if key_error:
            return JSONResponse({"ok": False, "error": key_error}, status_code=400)
        # 安全：环境变量密钥只允许用于「已保存的 Base URL」（与请求体地址一致时），
        # 防止调用者指定任意地址让后端把环境密钥外发（SSRF + 凭据外传）
        if not api_key and base.rstrip("/") == str(cfg["llm"].get("base_url", "")).rstrip("/"):
            api_key = os.environ.get("DGLAB_LLM_API_KEY", "")
        if not api_key or not base or not model:
            return JSONResponse({"ok": False, "error": "请先填写 API Key、地址与模型名"})
        try:
            async with httpx.AsyncClient(
                timeout=20, trust_env=bool(cfg["llm"].get("trust_env", False))
            ) as client:
                r = await client.post(
                    f"{base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                )
            if r.status_code == 200:
                return JSONResponse({"ok": True, "detail": "连接成功，模型可用"})
            if r.status_code == 401:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "API Key 无效或未填（官方与中转站的密钥不通用，请确认 Base URL 与密钥配套）",
                    }
                )
            if r.status_code == 400:
                return JSONResponse(
                    {"ok": False, "error": "请求参数不被支持（中转站常见）：请核对模型名，或关闭 JSON 模式"}
                )
            return JSONResponse({"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:300]})

    def _patch_app_text(text: str, check_update: bool) -> str:
        """文本级更新 config.yaml 的 app 小节（仅 2 空格缩进键），缺失时插入。"""
        lines = text.splitlines()
        out: list[str] = []
        in_app = False
        patched = False
        for ln in lines:
            if ln and not ln.startswith(" "):
                in_app = ln.startswith("app:")
                out.append(ln)
                continue
            if in_app and ln.startswith("  ") and not ln.startswith("    "):
                key = ln.lstrip().split(":", 1)[0]
                if key == "check_update":
                    out.append(f"  check_update: {str(bool(check_update)).lower()}")
                    patched = True
                    continue
            out.append(ln)
        if not patched:
            idx = next((i for i, l in enumerate(out) if l.strip() == "app:"), -1)
            if idx >= 0:
                out.insert(idx + 1, f"  check_update: {str(bool(check_update)).lower()}")
        return "\n".join(out) + "\n"

    # ---------- 更新检测 ----------
    @app.get("/api/update")
    async def api_update() -> JSONResponse:
        await state.update.check()
        await state.broadcast()
        return JSONResponse(state.update.to_state(_app_version()))

    @app.post("/api/update")
    async def api_update_set(body: dict) -> JSONResponse:
        """开关自动检查更新（持久化到 config.yaml app.check_update）。"""
        if isinstance(body.get("enabled"), bool):
            state.update.enabled = body["enabled"]
            cfg["app"]["check_update"] = state.update.enabled
            cfg_path = PROJECT_ROOT / "config" / "config.yaml"
            example = PROJECT_ROOT / "config" / "config.example.yaml"
            if not cfg_path.exists():
                cfg_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            cfg_path.write_text(
                _patch_app_text(cfg_path.read_text(encoding="utf-8"), state.update.enabled),
                encoding="utf-8",
            )
            if state.update.enabled:
                await state.update.check()
            await state.broadcast()
        return JSONResponse({"ok": True, **state.update.to_state(_app_version())})

    # ---------- 实时推送 ----------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        state.ws_clients.add(ws)
        state._on_ws_clients_change()
        await ws.send_json({"type": "state", "data": state.build_state()})
        try:
            while True:
                await ws.receive_text()  # 客户端心跳/忽略
        except WebSocketDisconnect:
            pass
        finally:
            state.ws_clients.discard(ws)
            state._on_ws_clients_change()

    # 兜底：任意非 API/静态资源路径都回首页（用户输错地址/旧收藏夹不再 404）。
    # 必须在所有 /api 路由与 /assets 挂载之后注册，API 优先命中。
    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> Response:
        if full_path.startswith(("api/", "assets/")):
            raise HTTPException(status_code=404, detail="Not Found")
        if (FRONTEND_DIST / "index.html").exists():
            return FileResponse(FRONTEND_DIST / "index.html")
        return Response(
            "前端尚未构建：请在 frontend\\ 目录执行 npm install && npm run build",
            media_type="text/plain; charset=utf-8",
        )

    return app


app = None if os.environ.get("DGLAB_DATA_DIR") else make_app()


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        app,
        host=cfg["app"]["host"],
        port=int(cfg["app"]["port"]),
        log_level="info",
    )

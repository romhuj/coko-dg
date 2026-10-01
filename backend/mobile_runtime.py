"""Chaquopy entry points. Importing this module never starts a device or server."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import threading
import time


_lifecycle_lock = threading.RLock()
_runtime = None
_START_TIMEOUT_S = 20.0


class _Runtime:
    def __init__(self, data_root, seed_root, token):
        self.data_root = data_root
        self.seed_root = seed_root
        self.token = token
        self.port = 0
        self.socket = None
        self.thread = None
        self.loop = None
        self.server = None
        self.app = None
        self.failure = None
        self.finished = threading.Event()


def _prepare_data(data_root: Path, seed_root: Path) -> None:
    if data_root == seed_root or data_root.is_relative_to(seed_root) or seed_root.is_relative_to(data_root):
        raise ValueError("Android data and seed directories must be separate")
    for name in ("config/config.yaml", "config/character.yaml", "config/waveforms.yaml", "frontend/dist/index.html"):
        if not (seed_root / name).is_file():
            raise FileNotFoundError(f"Missing packaged asset: {name}")
    data_root.mkdir(parents=True, exist_ok=True)
    for directory in ("config", "content"):
        source = seed_root / directory
        if not source.exists():
            continue
        for entry in source.rglob("*"):
            if entry.is_symlink():
                raise ValueError("Packaged assets must not contain symbolic links")
            if not entry.is_file():
                continue
            destination = data_root / entry.relative_to(seed_root)
            if destination.exists():
                continue  # User settings, saved roles and imported data always win.
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".seed-tmp")
            shutil.copyfile(entry, temporary)
            temporary.replace(destination)


def _configure_paths(runtime: _Runtime) -> None:
    os.environ["DGLAB_DATA_DIR"] = str(runtime.data_root)
    os.environ["DGLAB_SEED_DIR"] = str(runtime.seed_root)
    # Python stays alive when the Android service restarts. Refresh already
    # imported module paths as well as setting the environment before imports.
    from . import config
    config.PROJECT_ROOT = runtime.data_root
    config.CONFIG_DIR = runtime.data_root / "config"
    config.DEVICE_CHANNELS_FILE = config.CONFIG_DIR / "device_channels.yaml"
    config.CHARACTER_RUNTIME_FILE = config.CONFIG_DIR / "character_runtime.yaml"


def _make_application(runtime: _Runtime):
    from . import main
    main.PROJECT_ROOT = runtime.data_root
    main.BUNDLE_ROOT = runtime.seed_root
    main.FRONTEND_DIST = runtime.seed_root / "frontend" / "dist"
    return main.make_app(mobile_token=runtime.token, mobile_port=runtime.port)


def _serve(runtime: _Runtime) -> None:
    logger = logging.getLogger("ai-for-coyote")
    prior_handlers = set(logger.handlers)
    async def run():
        import uvicorn
        runtime.loop = asyncio.get_running_loop()
        runtime.app = _make_application(runtime)
        config = uvicorn.Config(
            runtime.app, host="127.0.0.1", port=runtime.port,
            loop="asyncio", http="h11", ws="websockets", lifespan="on",
            access_log=False, log_level="warning", log_config=None,
            proxy_headers=False, server_header=False, timeout_graceful_shutdown=3,
        )
        runtime.server = uvicorn.Server(config)
        await runtime.server.serve(sockets=[runtime.socket])

    try:
        asyncio.run(run())
    except BaseException as exc:
        runtime.failure = exc
    finally:
        if runtime.socket:
            runtime.socket.close()
        for handler in list(logger.handlers):
            if handler not in prior_handlers:
                logger.removeHandler(handler)
                handler.close()
        runtime.finished.set()


def start(data_root: str, seed_root: str, token: str) -> int:
    """Start one authenticated loopback server and return its ephemeral port."""
    global _runtime
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("A random URL-safe native session token is required")
    data, seed = Path(data_root).resolve(), Path(seed_root).resolve()
    with _lifecycle_lock:
        if _runtime and not _runtime.finished.is_set():
            if (_runtime.data_root, _runtime.seed_root, _runtime.token) == (data, seed, token):
                return _runtime.port
            raise RuntimeError("Stop the existing Android session before starting another")
        _prepare_data(data, seed)
        runtime = _Runtime(data, seed, token)
        _configure_paths(runtime)
        runtime.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        runtime.socket.bind(("127.0.0.1", 0))
        runtime.socket.listen(128)
        runtime.socket.setblocking(False)
        runtime.port = runtime.socket.getsockname()[1]
        runtime.thread = threading.Thread(target=_serve, args=(runtime,), name="CoyoteBackend", daemon=True)
        _runtime = runtime
        runtime.thread.start()
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if runtime.finished.wait(.02):
                _runtime = None
                raise RuntimeError("Android backend failed to start") from runtime.failure
            if runtime.server and runtime.server.started:
                return runtime.port
        if runtime.server:
            runtime.server.should_exit = True
        runtime.thread.join(timeout=5)
        if runtime.finished.is_set():
            _runtime = None
        raise RuntimeError("Android backend startup timed out")


async def _emergency_stop(runtime: _Runtime) -> None:
    if runtime.app is None:
        return
    state = runtime.app.state.runtime
    state.loop.set_autopilot(False)
    state.loop.stop_observe_loop()
    must_send = state.backend.ready() and not state.safety.dry_run
    # estop latches and invalidates pending model actions before its first await.
    result = await state.loop.estop()
    await state.broadcast()
    if must_send and (not result.get("sent") or result.get("status") in ("failed", "unconfirmed")):
        raise RuntimeError("Device stop transmission was not confirmed")


def estop() -> None:
    """Queue immediate stop on the server loop; callable from Java without HTTP."""
    runtime = _runtime
    if runtime and runtime.loop and runtime.loop.is_running() and not runtime.finished.is_set():
        future = asyncio.run_coroutine_threadsafe(_emergency_stop(runtime), runtime.loop)
        # Consume exceptions without logging session data or blocking Android's UI.
        def consume(done):
            with contextlib.suppress(Exception):
                done.result()
        future.add_done_callback(consume)


def stop() -> None:
    """Latch stop, close devices, then stop uvicorn and join its worker thread."""
    global _runtime
    with _lifecycle_lock:
        runtime = _runtime
        if runtime is None:
            return
        stop_failed = False
        if runtime.loop and runtime.loop.is_running() and runtime.app:
            async def shutdown():
                failed = False
                try:
                    await _emergency_stop(runtime)
                except Exception:
                    failed = True
                try:
                    # Always clean up sockets/tasks, even if the stop frame
                    # failed. Do not resend or restart a waveform while exiting.
                    await runtime.app.state.runtime.shutdown(stop_devices=False)
                except Exception:
                    failed = True
                if failed:
                    raise RuntimeError("Android shutdown could not confirm device stop")
            future = asyncio.run_coroutine_threadsafe(shutdown(), runtime.loop)
            try:
                future.result(timeout=5)
            except Exception:
                stop_failed = True
        if runtime.server:
            runtime.server.should_exit = True
        if runtime.thread and runtime.thread is not threading.current_thread():
            runtime.thread.join(timeout=8)
        if not runtime.finished.is_set():
            raise RuntimeError("Android backend did not stop; keep the session locked")
        _runtime = None
        if stop_failed:
            raise RuntimeError("Local service stopped, but device stop was not confirmed. Check DG-LAB.")

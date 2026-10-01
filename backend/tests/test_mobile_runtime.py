"""Mobile runtime isolation: fake hardware/model, ephemeral localhost HTTP/WS only."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from backend.mobile_auth import MobileSessionMiddleware


class MobileAuthTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, *, kind="http", token="x" * 64, origin="http://127.0.0.1:12345", host="127.0.0.1:12345", extra=(), path="/api/state"):
        messages = []
        endpoint = AsyncMock()
        middleware = MobileSessionMiddleware(endpoint, token="x" * 64, port=12345)
        headers = [(b"host", host.encode())]
        if token is not None:
            headers.append((b"cookie", f"coyote_session={token}".encode()))
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        headers.extend(extra)
        async def send(message):
            messages.append(message)
        await middleware({"type": kind, "path": path, "headers": headers}, AsyncMock(), send)
        return endpoint.await_count, messages

    async def test_http_and_assets_require_cookie_and_exact_origin_host(self):
        for path in ("/api/state", "/api/manual", "/", "/assets/app.js"):
            for invalid in ({"token": None}, {"token": "wrong"}, {"origin": "https://outside.invalid"},
                            {"origin": "null"}, {"host": "localhost:12345"},
                            {"host": "127.0.0.1:12346"},
                            {"extra": [(b"origin", b"http://127.0.0.1:12345")]},
                            {"extra": [(b"sec-fetch-site", b"cross-site")]}):
                count, messages = await self.request(path=path, **invalid)
                self.assertEqual(count, 0)
                self.assertEqual(messages[0]["status"], 403)
        self.assertEqual((await self.request())[0], 1)
        self.assertEqual((await self.request(origin=None, path="/"))[0], 1)

    async def test_websocket_requires_cookie_and_origin(self):
        self.assertEqual((await self.request(kind="websocket"))[0], 1)
        for invalid in ({"token": None}, {"token": "wrong"}, {"origin": None}, {"origin": "https://outside.invalid"}):
            count, messages = await self.request(kind="websocket", **invalid)
            self.assertEqual(count, 0)
            self.assertEqual(messages, [{"type": "websocket.close", "code": 1008}])


class MobileRuntimeTests(unittest.TestCase):
    def test_start_auth_restart_data_retention_and_native_estop_in_isolated_process(self):
        environment = dict(os.environ)
        for key in ("DGLAB_DATA_DIR", "DGLAB_SEED_DIR", "DGLAB_LLM_API_KEY", "DGLAB_DRY_RUN"):
            environment.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-m", "backend.tests.test_mobile_runtime", "--scenario"],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=55,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MOBILE_SCENARIO_OK", result.stdout)


def _scenario():
    import httpx
    import yaml
    from backend import mobile_runtime

    class FakeBackend:
        name = "dglab_relay"
        def __init__(self):
            self.commands, self.loops = [], {}
            self.started, self.stopped = False, False
            self.connected, self.send_result = True, True
            self.send_error = self.cleanup_error = False
        async def start(self): self.started = True
        async def stop(self):
            self.stopped = True
            if self.cleanup_error:
                raise RuntimeError("PRIVATE_CLEANUP_SENTINEL")
        def on_disconnect(self, callback): pass
        def ready(self): return self.connected
        def controller_id(self): return "isolated-controller"
        def client_state(self): return None
        def to_state(self): return {"status": "paired", "controller_id": self.controller_id(), "clients": []}
        def loops_active(self): return dict(self.loops)
        async def apply(self, cmd):
            self.commands.append(dict(cmd))
            if self.send_error:
                raise RuntimeError("PRIVATE_SEND_SENTINEL")
            return self.send_result
        async def start_pulse_hold(self, channel, cmd):
            self.commands.append(dict(cmd))
            self.loops[channel] = True
            return True
        def stop_pulse_hold(self, channel=None):
            if channel: self.loops.pop(channel, None)
            else: self.loops.clear()

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        seed, data = base / "seed", base / "private"
        (seed / "config").mkdir(parents=True)
        (seed / "content").mkdir()
        (seed / "frontend" / "dist" / "assets").mkdir(parents=True)
        (seed / "frontend" / "dist" / "index.html").write_text("mobile-first-build", encoding="utf-8")
        (seed / "frontend" / "dist" / "assets" / "app.js").write_text("/* mobile */", encoding="utf-8")
        (seed / "content" / "mobile-guide.md").write_text("A neutral, thoughtful test character.", encoding="utf-8")
        (seed / "config" / "config.yaml").write_text(yaml.safe_dump({
            "app": {"dry_run": False, "check_update": False},
            "autopilot": {"enabled": True, "interval_s": 12},
            "camera": {"enabled": True}, "audio": {"enabled": True},
            "device": {"backend": "coyote2_ble"},
            "llm": {"api_key": "", "base_url": "https://offline.invalid/v1", "model": "offline"},
        }), encoding="utf-8")
        (seed / "config" / "character.yaml").write_text(yaml.safe_dump({
            "name": "Mobile test role", "prompt_file": "content/mobile-guide.md",
        }), encoding="utf-8")
        # This data file has no personal configuration or credentials.
        source_waves = Path(__file__).resolve().parents[2] / "config" / "waveforms.yaml"
        (seed / "config" / "waveforms.yaml").write_bytes(source_waves.read_bytes())

        original_make = mobile_runtime._make_application
        instances = []
        def make_isolated(runtime):
            from backend import main
            backend = FakeBackend()
            with patch.object(main, "build_backend", return_value=backend):
                app = original_make(runtime)
            app.state.runtime.llm.chat = AsyncMock(return_value=("离线测试回复", []))
            instances.append(app.state.runtime)
            return app

        token = "x" * 64
        with patch.object(mobile_runtime, "_make_application", side_effect=make_isolated):
            try:
                port = mobile_runtime.start(str(data), str(seed), token)
                assert port > 0
                assert mobile_runtime.start(str(data), str(seed), token) == port
                origin = f"http://127.0.0.1:{port}"
                headers = {"Origin": origin, "Cookie": f"coyote_session={token}"}
                with httpx.Client(base_url=origin, headers=headers, trust_env=False, timeout=3) as client:
                    assert client.get("/").text == "mobile-first-build"
                    assert client.get("/api/state", headers={"Cookie": ""}).status_code == 403
                    assert client.get("/api/state", headers={"Origin": "https://outside.invalid"}).status_code == 403
                    state = client.get("/api/state").json()
                    assert state["platform"] == "android" and state["estop"] and not state["autopilot"]
                    assert not any(state["capabilities"][key] for key in ("camera", "audio", "dungeon", "ble"))
                    assert len(state["presets"]) == 236
                    assert instances[-1].cfg["relay"]["url"] == "wss://trex.dungeon-lab.cn/v4"
                    assert not instances[-1].backend.commands
                    assert client.post("/api/autopilot", json={"enabled": True}).status_code == 409
                    assert client.post("/api/sensors", json={"camera": True}).status_code == 501
                    assert client.post("/api/dungeon/start", json={}).status_code == 501
                    assert client.post("/api/chat", json={"message": "Hello", "mode": "auto"}).json()["line"] == "离线测试回复"
                    blocked = client.post("/api/manual", json={"op": "hold_strength", "channel": "A", "value": 10}).json()
                    assert not blocked["executed"]
                    assert client.get("/api/qrcode.png").content.startswith(b"\x89PNG\r\n\x1a\n")
                    asyncio.run(_websocket_checks(port, token))
                    assert client.post("/api/resume").status_code == 200
                    action = client.post("/api/manual", json={"op": "hold_strength", "channel": "A", "value": 10}).json()
                    assert action["executed"] and instances[-1].backend.commands
                    assert client.post("/api/autopilot", json={"enabled": True}).status_code == 200
                    assert client.get("/api/state").json()["autopilot"]
                    stopped = client.post("/api/estop", json={}).json()
                    assert stopped["estop"] and stopped["sent"]
                    stopped_state = client.get("/api/state").json()
                    assert stopped_state["estop"] and not stopped_state["autopilot"]
                    assert instances[-1].loop.autopilot_task is None
                    assert instances[-1].loop.observe_task is None
                    assert client.post("/api/resume", json={}).status_code == 200
                    resumed_state = client.get("/api/state").json()
                    assert not resumed_state["estop"] and not resumed_state["autopilot"]
                    # Resume owns the auto-off transition itself, in one request.
                    assert client.post("/api/autopilot", json={"enabled": True}).status_code == 200
                    assert client.get("/api/state").json()["autopilot"]
                    assert client.post("/api/resume", json={}).status_code == 200
                    assert not client.get("/api/state").json()["autopilot"]
                    assert instances[-1].loop.autopilot_task is None
                    assert client.post("/api/settings/llm", json={"api_key": "saved-test-key", "base_url": "https://offline.invalid/v1", "model": "saved-model"}).status_code == 200
                    _mobile_settings_checks(client, instances[-1], data)
                    mobile_runtime.estop()
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and not client.get("/api/state").json()["estop"]:
                        time.sleep(.01)
                    assert client.get("/api/state").json()["estop"]
                    assert not client.get("/api/state").json()["autopilot"]
                saved = (data / "config" / "config.yaml").read_bytes()
                (data / "content" / "mobile-guide.md").write_text("Personal role edits", encoding="utf-8")
                mobile_runtime.stop()
                assert instances[-1].backend.stopped
                assert mobile_runtime._runtime is None
                (seed / "frontend" / "dist" / "index.html").write_text("mobile-updated-build", encoding="utf-8")
                (seed / "content" / "mobile-guide.md").write_text("New seed must not overwrite edits", encoding="utf-8")
                new_token = "y" * 64
                port2 = mobile_runtime.start(str(data), str(seed), new_token)
                origin2 = f"http://127.0.0.1:{port2}"
                with httpx.Client(base_url=origin2, headers={"Origin": origin2, "Cookie": f"coyote_session={new_token}"}, trust_env=False) as client:
                    state = client.get("/api/state").json()
                    assert state["estop"] and not state["autopilot"] and state["current"] == {"A": 0, "B": 0}
                    assert not instances[-1].backend.commands
                    assert client.get("/").text == "mobile-updated-build"
                    assert client.get("/api/state", headers={"Cookie": f"coyote_session={token}"}).status_code == 403
                    assert client.get("/api/settings/llm").json()["model"] == "saved-model"
                assert (data / "config" / "config.yaml").read_bytes() == saved
                assert (data / "content" / "mobile-guide.md").read_text(encoding="utf-8") == "Personal role edits"
            finally:
                mobile_runtime.stop()
            assert instances[-1].backend.stopped
            # A failed stop must remain observable to Java after the local
            # thread exits. A disconnected or simulated device needs no send.
            for case in ("send_false", "send_exception", "cleanup_exception", "disconnected", "dry_run"):
                mobile_runtime.start(str(data), str(seed), token)
                runtime = mobile_runtime._runtime
                state, backend = instances[-1], instances[-1].backend
                backend.send_result = case != "send_false"
                backend.send_error = case == "send_exception"
                backend.cleanup_error = case == "cleanup_exception"
                backend.connected = case != "disconnected"
                state.safety.dry_run = case == "dry_run"
                failure = None
                try:
                    mobile_runtime.stop()
                except RuntimeError as exc:
                    failure = str(exc)
                expected_failure = case in ("send_false", "send_exception", "cleanup_exception")
                assert bool(failure) == expected_failure, (case, failure)
                assert not failure or "PRIVATE_" not in failure
                assert runtime.finished.is_set() and not runtime.thread.is_alive(), case
                assert mobile_runtime._runtime is None and backend.stopped, case
                assert state.safety.estop_active and not state.loop.autopilot, case
                # One stop command owns the complete clear/zero batch in the
                # adapter. A failed or uncertain delivery must not be retried.
                expected_commands = [] if case in ("disconnected", "dry_run") else ["stop"]
                actual_commands = [command["kind"] for command in backend.commands]
                assert actual_commands == expected_commands, (case, actual_commands)
                assert not backend.loops
        print("MOBILE_SCENARIO_OK")


def _mobile_settings_checks(client, state, data):
    """Exercise key preservation with all outbound model HTTP mocked."""
    import httpx
    import yaml

    saved_url = "https://offline.invalid/v1"
    form = {"api_key": "", "keep_api_key": True, "base_url": saved_url + "/", "model": "saved-model"}
    assert client.post("/api/settings/llm", json=form).status_code == 200
    assert state.cfg["llm"]["api_key"] == "saved-test-key"
    persisted = yaml.safe_load((data / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert persisted["llm"]["api_key"] == "saved-test-key"
    with patch("backend.main.httpx.AsyncClient") as constructor:
        fake = constructor.return_value.__aenter__.return_value
        fake.post = AsyncMock(return_value=httpx.Response(200, json={"ok": True}))
        tested = client.post("/api/settings/llm/test", json={**form, "base_url": saved_url})
        assert tested.status_code == 200 and tested.json()["ok"]
        assert fake.post.await_count == 1
        args, kwargs = fake.post.call_args
        assert args[0] == saved_url + "/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer saved-test-key"
    # Neither saving nor testing a different URL may reuse the private key.
    with patch("backend.main.httpx.AsyncClient") as constructor:
        for endpoint in ("/api/settings/llm", "/api/settings/llm/test"):
            for changed in ("https://elsewhere.invalid/v1", saved_url + "/other"):
                rejected = client.post(endpoint, json={**form, "base_url": changed})
                assert rejected.status_code == 400
                assert "API Key" in rejected.json()["error"]
                assert "saved-test-key" not in rejected.text
        constructor.assert_not_called()
    assert state.cfg["llm"]["api_key"] == "saved-test-key"
    assert state.cfg["llm"]["base_url"] == saved_url + "/"
    # An explicit blank with no keep flag still clears, matching desktop semantics.
    assert client.post("/api/settings/llm", json={"api_key": "", "base_url": saved_url, "model": "saved-model"}).status_code == 200
    assert not client.get("/api/settings/llm").json()["has_key"]
    assert client.post("/api/settings/llm", json=form).status_code == 400
    # Restore the exact expected saved model/key for the restart-retention test.
    assert client.post("/api/settings/llm", json={"api_key": "saved-test-key", "base_url": saved_url, "model": "saved-model"}).status_code == 200


async def _websocket_checks(port, token):
    import websockets
    origin = f"http://127.0.0.1:{port}"
    # legacy client keeps compatibility with the minimum websockets >= 12 requirement.
    from websockets.legacy.client import connect
    async with connect(f"ws://127.0.0.1:{port}/ws", origin=origin,
                       extra_headers={"Cookie": f"coyote_session={token}"}) as websocket:
        frame = json.loads(await asyncio.wait_for(websocket.recv(), 3))
        assert frame["type"] == "state" and frame["data"]["platform"] == "android"
    for kwargs in ({"origin": origin}, {"origin": "https://outside.invalid", "extra_headers": {"Cookie": f"coyote_session={token}"}}):
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws", **kwargs):
                raise AssertionError("Unauthorized WebSocket accepted")
        except websockets.exceptions.InvalidStatusCode as exc:
            assert exc.status_code == 403


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        _scenario()
    else:
        unittest.main()

"""Stage an allowlisted mobile bundle without copying personal configuration.

Run after `npm ci && npm run build` in frontend/. No existing user installation
is read. Generated files live exclusively beneath android/app/build/.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "android" / "app" / "build" / "generated" / "mobile"

MOBILE_CONFIG = """app:
  host: 127.0.0.1
  port: 0
  title: coko DG
  dry_run: false
  check_update: false
relay:
  url: wss://trex.dungeon-lab.cn/v4
  reconnect_delay_s: 3
  lan_ip: auto
  public_url: ''
device:
  backend: dglab_relay
llm:
  base_url: https://api.deepseek.com
  api_key: ''
  model: deepseek-flash
  temperature: 1.0
  max_tokens: 1500
  timeout_s: 60
  json_mode: true
  trust_env: false
character_file: config/character.yaml
camera:
  enabled: false
audio:
  enabled: false
autopilot:
  enabled: false
  interval_s: 12
device_channels:
  A:
    name: A 通道
    location: A 通道
    baseline: 0
  B:
    name: B 通道
    location: B 通道
    baseline: 0
safety:
  channels:
    A:
      max_strength: 200
    B:
      max_strength: 200
  max_temp_duration_s: 10
  max_strength_step: 40
  auto_clear_on_disconnect: true
  overheat_reduce_to: 20
log:
  dir: logs
  level: INFO
  history_keep: 40
"""

MOBILE_CHARACTER = """player_nick: 玩家
role: 情景助手
profile: 角色扮演
roles:
  情景助手:
    name: 情景助手
    title: 助手
    device_narrative: 设备反馈
    is_custom: true
    profiles:
      角色扮演:
        level: 中
        note: 冷静、理性、有主见，结合情景交流。
        prompt_file: content/mobile-guide.md
        examples: []
"""

MOBILE_CHANNELS = """A:
  name: A 通道
  location: A 通道
  baseline: 0
B:
  name: B 通道
  location: B 通道
  baseline: 0
"""

MOBILE_PROMPT = """你是情景助手，性格冷静、理性、有主见，表达简洁友好。
结合当前角色、对话历史与情景决定回应，可以接受、拒绝、追问或暂缓。
玩家的一般要求不是必须执行的设备命令。只有情景确有需要时才提出动作；
选择不调整时保持现状。普通情景台词按系统提供的模型自判断模式解释；
该模式关闭时停止、减弱和不适反馈优先，开启时结合角色性格与完整情景判断。
遵守系统提供的输出格式、设备权限、各通道上限与急停状态。
这份设定不赋予设备权限，不改变强度限制，也不要求每轮调整设备。
"""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def prepare() -> dict:
    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise SystemExit("Build the frontend first: cd frontend && npm ci && npm run build")
    backend = ROOT / "backend"
    if not (backend / "mobile_runtime.py").is_file():
        raise SystemExit("backend/mobile_runtime.py is missing")
    # Remove only this generated staging directory, never runtime or source data.
    generated = GENERATED.resolve()
    boundary = (ROOT / "android" / "app" / "build").resolve()
    if not generated.is_relative_to(boundary) or generated == boundary:
        raise RuntimeError("Generated directory escaped the Android build folder")
    if generated.exists():
        shutil.rmtree(generated)
    python_root = generated / "python"
    seed = generated / "assets" / "runtime_seed"
    count = 0
    for source in sorted(backend.rglob("*.py")):
        relative = source.relative_to(backend)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        destination = python_root / "backend" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        count += 1
    write_text(seed / "config" / "config.yaml", MOBILE_CONFIG)
    write_text(seed / "config" / "config.example.yaml", MOBILE_CONFIG)
    write_text(seed / "config" / "character.yaml", MOBILE_CHARACTER)
    write_text(seed / "config" / "character.example.yaml", MOBILE_CHARACTER)
    write_text(seed / "config" / "device_channels.yaml", MOBILE_CHANNELS)
    write_text(seed / "content" / "mobile-guide.md", MOBILE_PROMPT)
    shutil.copyfile(ROOT / "config" / "waveforms.yaml", seed / "config" / "waveforms.yaml")
    shutil.copytree(frontend, seed / "frontend" / "dist")
    shutil.copyfile(ROOT / "LICENSE", seed / "LICENSE")
    version = re.search(r"versionName\s+'([0-9A-Za-z._-]+)'", (ROOT / "android" / "app" / "build.gradle").read_text(encoding="utf-8"))
    if not version:
        raise SystemExit("Android versionName is missing")
    write_text(seed / "version.txt", version.group(1) + "\n")
    manifest = {
        p.relative_to(seed).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(seed.rglob("*")) if p.is_file()
    }
    write_text(seed / "bundle-manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    result = {"python_modules": count, "seed_files": len(manifest), "generated": str(generated)}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    prepare()

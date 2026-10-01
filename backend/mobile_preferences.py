"""Persist only user settings, never live strengths, pairing sessions or automatic execution."""
import json
import os
from pathlib import Path


class MobilePreferences:
    def __init__(self, path: Path):
        self.path = path
        self.values = {}
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("设备设置文件格式无效")
            self.values = value

    def save(self, changes: dict):
        values = {**self.values, **changes}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(values, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.values = values

    def apply(self, safety):
        caps = self.values.get("user_caps", {})
        if isinstance(caps, dict):
            for channel in ("A", "B"):
                value = caps.get(channel)
                if isinstance(value, int) and not isinstance(value, bool):
                    safety.set_user_cap(channel, value)
        level = self.values.get("intensity_level", "中")
        link = self.values.get("intensity_device_link", True)
        safety.set_intensity_level(level if isinstance(level, str) else "中", link if isinstance(link, bool) else True)

    def device(self, presets: dict) -> dict:
        raw = self.values.get("device_preferences", {})
        if not isinstance(raw, dict):
            raw = {}
        last = raw.get("last_presets", {})
        if not isinstance(last, dict):
            last = {}
        return {"focus_channel": raw.get("focus_channel") if raw.get("focus_channel") in ("A", "B") else "A",
                "manual_link": raw.get("manual_link") is True,
                "last_presets": {ch: last.get(ch) if isinstance(last.get(ch), str) and last[ch] in presets else None for ch in ("A", "B")}}

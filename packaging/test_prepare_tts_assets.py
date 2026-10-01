"""Small, offline regressions for the APK resource packer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("prepare_tts_assets", Path(__file__).with_name("prepare_tts_assets.py"))
tts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tts
SPEC.loader.exec_module(tts)


class ResourcePackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.local = self.root / "local"
        self.contents = {"tokens.txt": b"known tokens", "phonemes/Mr serious": "中文空格".encode(),
                         "model.int8.onnx": b"excluded model", "voices.bin": b"excluded voices"}
        self.manifest = self.root / "tiny-pack.json"
        self.raw = {"schema": 1, "id": "tiny-pack", "repository": "official/model",
                    "revision": "a" * 40, "total_bytes": sum(map(len, self.contents.values())),
                    "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                              for name, data in self.contents.items()]}
        self.write_manifest()
        for name, data in self.contents.items():
            source = self.local / "tiny-pack" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)

    def tearDown(self):
        self.temporary.cleanup()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.raw), encoding="utf-8")

    def test_pack_is_repeatable_excludes_weights_and_works_without_local_inputs(self):
        pack = tts.load_manifest(self.manifest)
        with patch.object(tts, "download", side_effect=AssertionError("No network allowed")):
            first = tts.build_pack(pack, self.root / "cache", self.root / "one", self.local).read_bytes()
            second = tts.build_pack(pack, self.root / "cache", self.root / "two").read_bytes()
        self.assertEqual(first, second)
        with zipfile.ZipFile(self.root / "one/tiny-pack.zip") as archive:
            self.assertEqual(["phonemes/Mr serious", "tokens.txt"], archive.namelist())
            for name in archive.namelist():
                self.assertEqual(self.contents[name], archive.read(name))
                self.assertEqual((1980, 1, 1, 0, 0, 0), archive.getinfo(name).date_time)

    def test_corrupt_local_or_cached_file_cannot_be_packed(self):
        pack = tts.load_manifest(self.manifest)
        entry = pack.bundled[0]
        (self.local / pack.id / entry.path).write_bytes(b"not the pinned data")
        cache = self.root / "cache"
        cache.mkdir()
        (cache / entry.sha256).write_bytes(b"also invalid")
        with patch.object(tts, "download", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                tts.build_pack(pack, cache, self.root / "out", self.local)
        self.assertFalse((self.root / "out/tiny-pack.zip").exists())

    def test_paths_revision_and_totals_are_strict(self):
        for bad in ("../escape", "/absolute", "C:/absolute", "a\\b", "a/./b", "a//b"):
            with self.subTest(path=bad):
                self.raw["files"][0]["path"] = bad
                self.write_manifest()
                with self.assertRaises(ValueError):
                    tts.load_manifest(self.manifest)
        self.raw["files"][0]["path"] = "tokens.txt"
        self.raw["revision"] = "main"
        self.write_manifest()
        with self.assertRaises(ValueError):
            tts.load_manifest(self.manifest)
        self.raw["revision"] = "a" * 40
        self.raw["total_bytes"] += 1
        self.write_manifest()
        with self.assertRaises(ValueError):
            tts.load_manifest(self.manifest)

    def test_spaces_are_encoded_in_fixed_commit_download_url(self):
        pack = tts.load_manifest(self.manifest)
        entry = pack.bundled[0]
        with patch.object(tts, "download", return_value=self.root / "result") as download:
            tts.obtain(pack, entry, self.root / "cache")
        self.assertEqual("https://huggingface.co/official/model/resolve/" + "a" * 40 + "/phonemes/Mr%20serious",
                         download.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

"""Bundle pinned small TTS resources; model weights remain runtime downloads.

Only Python's standard library is required. Sources and hashes come from the
checked-in manifest, never an unpinned repository head. --local-dir is optional:
it can seed the build cache from existing verified files but is not required.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

LARGE_FILES = frozenset({"model.int8.onnx", "voices.bin"})
CHUNK = 128 * 1024


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Pack:
    id: str
    repository: str
    revision: str
    entries: tuple[Entry, ...]

    @property
    def bundled(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.path not in LARGE_FILES)


def safe_relative(path: object) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith("/")
            and not path.endswith("/") and "\\" not in path and ":" not in path
            and "\x00" not in path and all(part not in ("", ".", "..") for part in path.split("/")))


def load_manifest(path: Path) -> Pack:
    raw = json.loads(path.read_text(encoding="utf-8"))
    pack_id, repo, revision = raw.get("id"), raw.get("repository"), raw.get("revision")
    if raw.get("schema") != 1 or not isinstance(pack_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", pack_id):
        raise ValueError(f"Invalid TTS manifest identity: {path.name}")
    if path.stem != pack_id or not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"Invalid TTS manifest repository/name: {path.name}")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError(f"TTS source revision must be a fixed commit: {path.name}")
    files = raw.get("files")
    if not isinstance(files, list) or not files or len(files) > 10000:
        raise ValueError(f"Invalid TTS manifest file list: {path.name}")
    entries, seen = [], set()
    for item in files:
        relative, size, digest = item.get("path"), item.get("bytes"), item.get("sha256")
        if not safe_relative(relative) or relative in seen:
            raise ValueError(f"Unsafe or duplicate TTS resource path in {path.name}")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError(f"TTS resource lacks exact size/SHA256: {path.name}: {relative}")
        seen.add(relative)
        entries.append(Entry(relative, size, digest))
    if raw.get("total_bytes") != sum(entry.size for entry in entries):
        raise ValueError(f"TTS manifest total size does not match its entries: {path.name}")
    return Pack(pack_id, repo, revision, tuple(sorted(entries, key=lambda entry: entry.path)))


def verified(path: Path, entry: Entry) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != entry.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(CHUNK):
                digest.update(chunk)
        return digest.hexdigest() == entry.sha256
    except OSError:
        return False


class HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme.lower() != "https":
            raise OSError("TTS downloads cannot redirect away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(entry: Entry, url: str, cache: Path) -> Path:
    target = cache / entry.sha256
    temporary = None
    try:
        request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "Coyote-TTS-assets/1"})
        with build_opener(HttpsRedirects()).open(request, timeout=30) as source:
            if source.status != 200:
                raise OSError(f"TTS asset HTTP status {source.status}")
            with tempfile.NamedTemporaryFile(dir=cache, prefix=entry.sha256 + ".", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                digest, size = hashlib.sha256(), 0
                while chunk := source.read(CHUNK):
                    size += len(chunk)
                    if size > entry.size:
                        raise OSError("TTS asset exceeded its pinned size")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
        if size != entry.size or digest.hexdigest() != entry.sha256:
            raise OSError("TTS asset size/SHA256 mismatch")
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def obtain(pack: Pack, entry: Entry, cache: Path, local_dir: Path | None = None) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / entry.sha256
    if verified(target, entry):
        return target
    if local_dir is not None:
        source = local_dir / pack.id / Path(*PurePosixPath(entry.path).parts)
        if verified(source, entry):
            with tempfile.NamedTemporaryFile(dir=cache, suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                try:
                    with source.open("rb") as input_file:
                        shutil.copyfileobj(input_file, output, CHUNK)
                except BaseException:
                    output.close()
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                if not verified(temporary, entry):
                    raise OSError("Local TTS source changed while copying")
                os.replace(temporary, target)
                return target
            finally:
                temporary.unlink(missing_ok=True)
    suffix = f"/{pack.repository}/resolve/{pack.revision}/{quote(entry.path, safe='/')}"
    last_error = None
    for host in ("https://huggingface.co", "https://hf-mirror.com"):
        try:
            return download(entry, host + suffix, cache)
        except OSError as error:
            last_error = error
    raise OSError(f"Could not prepare pinned TTS asset: {pack.id}/{entry.path}") from last_error


def build_pack(pack: Pack, cache: Path, output_dir: Path, local_dir: Path | None = None, workers: int = 4) -> Path:
    # Fetch each content hash once; resources common to multiple paths share the cache.
    unique = {entry.sha256: entry for entry in pack.bundled}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda entry: obtain(pack, entry, cache, local_dir), unique.values()))
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (pack.id + ".zip")
    with tempfile.NamedTemporaryFile(dir=output_dir, prefix=pack.id + ".", suffix=".part", delete=False) as temporary_file:
        temporary = Path(temporary_file.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for entry in pack.bundled:
                source = cache / entry.sha256
                # Also validate bytes used for packing, independently of download/cache provenance.
                data = source.read_bytes()
                if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise OSError(f"TTS cached resource changed before packing: {pack.id}/{entry.path}")
                info = zipfile.ZipInfo(entry.path, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data, compresslevel=9)
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--local-dir", type=Path)
    args = parser.parse_args()
    manifests = sorted(args.manifest_dir.glob("*.json"))
    if not manifests:
        raise SystemExit("No checked-in TTS manifests found; refusing to create an empty asset bundle")
    for manifest in manifests:
        pack = load_manifest(manifest)
        output = build_pack(pack, args.cache_dir, args.output_dir, args.local_dir)
        print(f"{pack.id}: {len(pack.bundled)} verified bundled files, {output.stat().st_size} ZIP bytes", flush=True)


if __name__ == "__main__":
    main()

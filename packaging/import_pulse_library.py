"""Offline .pulse compatibility import; never connects to or operates a device.

Field tables: Fangs' first-party format analysis (DG-LAB App 3.4.4):
https://forum.dg-bbs.com/d/18-344-xin-ban-bo-xing-shu-ju-ge-shi-jie-xi
Frequency-index interpolation follows the author's https://dg-bbs.com/share preview.
V3 byte encoding: https://github.com/dungeonlab-open/dglab-bluetooth-protocol

This is a compatibility conversion, not a claim of identical App playback.
The third global field is retained as metadata and is not applied to the device.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import shutil

import yaml

PERIOD_MS = (
    list(range(10, 50)) + list(range(50, 80, 2)) + list(range(80, 100, 5))
    + list(range(100, 200, 10)) + [200, 233, 266, 300, 333, 366]
    + list(range(400, 600, 50)) + list(range(600, 1001, 100))
)
DURATION_TICKS = (
    list(range(1, 50)) + list(range(50, 80, 2)) + [80, 85, 90, 95]
    + list(range(100, 200, 10)) + [200, 234, 266, 300, 334, 366]
    + [400, 450, 500, 550] + [600, 700, 800, 900]
    + [1000, 1200, 1400, 1600, 1800] + [2000, 2500, 3000]
)


def frequency_byte(period: int) -> int:
    if period <= 100:
        return period
    if period <= 600:
        return 100 + (period - 100) // 5
    return 200 + (period - 600) // 10


def compile_pulse(text: str) -> tuple[list[str], dict]:
    text = text.lstrip('\ufeff').strip()
    prefix = 'Dungeonlab+pulse:'
    if not text.startswith(prefix):
        raise ValueError('不是 Dungeonlab+pulse 格式')
    payload = text[len(prefix):]
    rest, speed, parameter3 = 0, 1, None
    legacy = '=' not in payload
    if not legacy:
        settings, payload = payload.split('=', 1)
        values = [int(x) for x in settings.split(',')]
        if len(values) != 3:
            raise ValueError('全局参数必须为三个整数')
        rest, speed, parameter3 = values
        if not 0 <= rest <= 100 or speed not in (1, 2, 4):
            raise ValueError('不支持的休息时长或速度倍率')

    sections = payload.split('+section+')
    if not 1 <= len(sections) <= 10:
        raise ValueError('小节数量不在 1–10 范围')
    frequencies, strengths = [], []
    enabled_sections = 0
    for section_number, section in enumerate(sections, 1):
        header, shape = section.split('/', 1)
        values = [int(x) for x in header.split(',')]
        if len(values) != 5:
            raise ValueError(f'第 {section_number} 小节头必须为五个整数')
        start, end, duration_index, mode, enabled = values
        if enabled not in (0, 1):
            raise ValueError(f'第 {section_number} 小节开关为 {enabled}，应为 0 或 1')
        if not (0 <= start < 84 and 0 <= end < 84 and 0 <= duration_index < 100 and mode in (1, 2, 3, 4)):
            raise ValueError(f'第 {section_number} 小节参数超出格式范围')
        points = []
        for item in shape.split(','):
            match = re.fullmatch(r'(\d+(?:\.\d+)?)-([01])', item.strip())
            if not match:
                raise ValueError(f'第 {section_number} 小节包含非法曲线点')
            value = float(match.group(1))
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError('相对幅度超出 0–100 范围')
            points.append(math.floor(value + 0.5))
        if len(points) < 2:
            raise ValueError('脉冲元至少需要两个点')
        if not enabled:
            continue
        enabled_sections += 1
        count = len(points)
        repeats = max(1, (DURATION_TICKS[duration_index] + count - 1) // count)
        for cycle in range(repeats):
            for position, strength in enumerate(points):
                if mode == 1:
                    index = start
                elif mode == 2:
                    index = start + (end - start) * (cycle * count + position) // (repeats * count)
                elif mode == 3:
                    index = start + (end - start) * position // count
                else:
                    index = start + (end - start) * cycle // repeats
                frequencies.extend([frequency_byte(PERIOD_MS[index])] * (4 // speed))
                strengths.extend([strength] * (4 // speed))
    if not enabled_sections:
        raise ValueError('没有启用的小节')
    # The end rest is independent of playback speed. One tick is 100 ms.
    frequencies.extend([10] * (rest * 4))
    strengths.extend([0] * (rest * 4))
    padding = (-len(frequencies)) % 4
    frequencies.extend([10] * padding)
    strengths.extend([0] * padding)
    frames = [
        bytes(frequencies[n:n + 4] + strengths[n:n + 4]).hex().upper()
        for n in range(0, len(frequencies), 4)
    ]
    return frames, {
        'format': 'Dungeonlab+pulse', 'legacy_without_global_header': legacy,
        'rest_s': rest / 10, 'speed': speed, 'global_parameter_3': parameter3,
        'global_parameter_3_applied': False, 'enabled_sections': enabled_sections,
        'cycle_s': len(frames) / 10, 'padding_ms': padding * 25,
        'conversion': 'community-compatible-v1',
    }


def import_library(manifest_path: Path, config_paths: list[Path]) -> dict:
    base = manifest_path.resolve().parent
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    compiled, rejected, duplicates = [], [], []
    for record in manifest['files']:
        if record['duplicate_of']:
            duplicates.append(record)
            continue
        original = (base / record['relative_path']).resolve()
        if not original.is_relative_to(base):
            raise ValueError('原文件路径超出导入目录')
        raw = original.read_bytes()
        digest = hashlib.sha256(raw.strip()).hexdigest()
        if digest != record['sha256']:
            raise ValueError('原文件内容与清单不一致')
        try:
            frames, metadata = compile_pulse(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            rejected.append({'source': record['source'], 'reason': str(exc)})
            continue
        group = original.parent.name.removeprefix('pulse').lstrip('-')
        title = re.sub(r'^\d+-', '', original.stem.removeprefix('pulse-'))
        compiled.append({
            'source': record['source'], 'key': 'import_pulse_' + digest[:20],
            'title': title, 'category': '导入·' + group,
            'frames': frames, 'metadata': metadata,
        })

    results = []
    for path in config_paths:
        original_config = yaml.safe_load(path.read_text(encoding='utf-8'))
        presets = original_config.setdefault('presets', {})
        custom = original_config.setdefault('custom', {})
        added, names = 0, {}
        for item in compiled:
            key = item['key']
            previous = next((name for name, p in presets.items() if isinstance(p, dict) and p.get('waveform') == key), None)
            if previous:
                names[key] = previous
                continue
            name = item['title']
            if name in presets:
                name += '（' + item['category'].removeprefix('导入·') + '）'
            stem, number = name, 2
            while name in presets:
                name = f'{stem}·{number}'
                number += 1
            maximum = float(original_config['playback']['max_duration_s'])
            default = min(maximum, max(float(original_config['playback']['min_duration_s']), min(6, item['metadata']['cycle_s'])))
            presets[name] = {'waveform': key, 'default_duration_s': default,
                             'max_duration_s': maximum, 'category': item['category']}
            custom[key] = {'label': name, 'frames': item['frames'],
                           'source_file': item['source'], 'import_metadata': item['metadata']}
            names[key] = name
            added += 1
        encoded = yaml.safe_dump(original_config, allow_unicode=True, sort_keys=False, width=120)
        assert yaml.safe_load(encoded) == original_config
        backup = path.with_name(path.name + '.before-pulse-import-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.bak')
        if added:
            if backup.exists():
                raise FileExistsError(backup)
            shutil.copy2(path, backup)
            path.write_text(encoded, encoding='utf-8')
        results.append({'config': str(path), 'added': added, 'total_presets': len(presets),
                        'backup': str(backup) if added else None, 'names': names})

    report = {'originals': manifest['file_count'], 'unique_contents': manifest['unique_content_count'],
              'converted': len(compiled), 'duplicate_files': len(duplicates), 'rejected': rejected,
              'total_frames': sum(len(x['frames']) for x in compiled), 'configs': results,
              'notes': ['兼容转换，未声称与 DG-LAB App 的播放效果完全一致。',
                        '保留整段循环、小节频率模式、速度和末尾休息；幅度量化为整数字节。',
                        '第三个全局参数仅保留为元数据，不修改设备设置。',
                        '缺全局头的旧文件按无末尾休息、1 倍速处理。'],
              'items': [{k: v for k, v in item.items() if k != 'frames'} for item in compiled]}
    (base / '导入结果.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('config', type=Path, nargs='+')
    args = parser.parse_args()
    report = import_library(args.manifest, args.config)
    print(json.dumps({k: v for k, v in report.items() if k not in {'items', 'configs'}}, ensure_ascii=False, indent=2))
    for result in report['configs']:
        print(f"{result['config']}: added={result['added']}, total={result['total_presets']}")

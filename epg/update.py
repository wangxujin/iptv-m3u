#!/usr/bin/env python3
"""按配置顺序合并当天及跨天 XMLTV：高优先级节目优先，后续来源补齐空缺时段。"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from copy import deepcopy
from datetime import date, datetime, time as day_time, timedelta, timezone
import gzip
from http.client import HTTPException
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_XML = 128 * 1024 * 1024


def name_key(name: str) -> str:
    # 只统一全角、空白和大小写；保留 HD、4K、地区等有意义的名称差异。
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', name)).casefold()


def parse_time(value: str) -> datetime:
    match = re.fullmatch(r'(\d{12}(?:\d{2})?)(?:\s+([+-]\d{4}|UTC|GMT|Z))?', value.strip())
    if not match:
        raise ValueError('节目时间须精确到分钟，时区须为数字偏移或 UTC')
    stamp, offset = match.groups()
    zone = timezone.utc
    if offset and offset not in ('UTC', 'GMT', 'Z'):
        hours, minutes = int(offset[1:3]), int(offset[3:5])
        if hours > 23 or minutes > 59:
            raise ValueError('节目时区偏移无效')
        zone = timezone(timedelta(hours=hours, minutes=minutes) * (1 if offset[0] == '+' else -1))
    stamp += '00' if len(stamp) == 12 else ''
    return datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=zone).astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S %z')


def parse_source(raw: bytes) -> ET.Element:
    if raw.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_XML + 1)
    if len(raw) > MAX_XML:
        raise ValueError('EPG 解压后的 XML 超过大小限制')
    # XMLTV 的外部 DTD 声明可保留；不接受输入自定义实体。
    if b'<!ENTITY' in raw.upper():
        raise ValueError('EPG 包含自定义 XML 实体')
    root = ET.fromstring(raw)
    if root.tag != 'tv' or not root.findall('channel') or not root.findall('programme'):
        raise ValueError('来源不是包含频道和节目的 XMLTV 文件')
    return root


def download(url: str, timeout: float, attempts: int) -> bytes:
    request = Request(url, headers={'User-Agent': 'IPTV-M3U-EPG/1.0', 'Accept': 'application/gzip,application/xml,text/xml,*/*'})
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_DOWNLOAD + 1)
            if len(raw) > MAX_DOWNLOAD:
                raise ValueError('EPG 下载超过大小限制')
            return raw
        except (URLError, OSError, HTTPException) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ValueError('EPG HTTPS 证书校验失败；请配置可信证书') from exc
            if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == attempts:
                raise
            print(f'下载失败，重试 {attempt + 1}/{attempts}：{type(exc).__name__}', flush=True)
            time.sleep(min(5 * 2 ** (attempt - 1), 20))
    raise AssertionError('unreachable')


class Timeline:
    """始终保持不重叠；低优先级节目只填充已有时间轴的空缺。"""
    def __init__(self):
        self.starts: list[datetime] = []
        self.items: list[tuple[datetime, datetime, ET.Element]] = []

    def add(self, start: datetime, stop: datetime, programme: ET.Element,
            day_start: datetime, day_end: datetime) -> list[tuple[datetime, datetime]]:
        index = bisect_left(self.starts, start)
        if index and self.items[index - 1][1] > start:
            index -= 1
        cursor, gaps = start, []
        for occupied_start, occupied_stop, _ in self.items[index:]:
            if occupied_start >= stop:
                break
            if occupied_start > cursor:
                gaps.append((cursor, occupied_start))
            cursor = max(cursor, occupied_stop)
            if cursor >= stop:
                break
        if cursor < stop:
            gaps.append((cursor, stop))
        # 跨天节目被高优先级节目切开后，片段本身也必须与当天相交。
        gaps = [(left, right) for left, right in gaps if right > day_start and left < day_end]
        for left, right in gaps:
            record = deepcopy(programme)
            record.set('start', format_time(left))
            record.set('stop', format_time(right))
            position = bisect_left(self.starts, left)
            self.starts.insert(position, left)
            self.items.insert(position, (left, right, record))
        return gaps


def merge(sources: list[tuple[str, ET.Element]], target_date: date | None = None) -> tuple[ET.Element, dict]:
    target_date = target_date or datetime.now(SHANGHAI).date()
    day_start = datetime.combine(target_date, day_time.min, tzinfo=SHANGHAI)
    day_end = day_start + timedelta(days=1)
    channels: dict[str, ET.Element] = {}
    names: dict[str, str] = {}
    timelines: dict[str, Timeline] = {}
    summary = {'date': target_date.isoformat(), 'timezone': 'Asia/Shanghai',
               'priority': [name for name, _ in sources], 'sources': []}
    for source_id, root in sources:
        stats = {'id': source_id, 'input_channels': len(root.findall('channel')),
                 'input_programmes': len(root.findall('programme')), 'invalid_programmes': 0,
                 'outside_day_programmes': 0,
                 'shadowed_programmes': 0, 'trimmed_programmes': 0, 'added_segments': 0,
                 'ambiguous_alias_groups': 0}
        # 同一源的重复 ID 表示同一个节目引用，合并其 display-name 别名。
        groups: dict[str, list[ET.Element]] = {}
        for channel in root.findall('channel'):
            original_id = channel.get('id', '').strip()
            if not original_id or not any(name_key(node.text or '') for node in channel.findall('display-name')):
                raise ValueError(f'源 {source_id} 的频道缺少 ID 或名称')
            groups.setdefault(original_id, []).append(channel)
        remap = {}
        for original_id, definitions in groups.items():
            aliases = [node for channel in definitions for node in channel.findall('display-name')
                       if name_key(node.text or '')]
            matches = list(dict.fromkeys(names[name_key(node.text)] for node in aliases if name_key(node.text) in names))
            # 别名对应多个已有频道时，不用低优先级源强行合并它们；按名称顺序选择。
            stats['ambiguous_alias_groups'] += len(matches) > 1
            canonical = matches[0] if matches else unicodedata.normalize('NFKC', aliases[0].text).strip()
            if canonical not in channels:
                channel = deepcopy(definitions[0])
                channel.set('id', canonical)
                for node in list(channel.findall('display-name')):
                    channel.remove(node)
                channels[canonical] = channel
                timelines[canonical] = Timeline()
            channel = channels[canonical]
            known = {name_key(node.text or '') for node in channel.findall('display-name')}
            for alias in aliases:
                key = name_key(alias.text)
                if key in names and names[key] != canonical:
                    continue
                names[key] = canonical
                if key not in known:
                    channel.insert(len(channel.findall('display-name')), deepcopy(alias))
                    known.add(key)
            remap[original_id] = canonical
        valid = 0
        for programme in root.findall('programme'):
            canonical = remap.get(programme.get('channel', '').strip())
            try:
                start = parse_time(programme.get('start', ''))
                stop = parse_time(programme.get('stop', ''))
                if not canonical or stop <= start or not (programme.findtext('title') or '').strip():
                    raise ValueError('节目缺少频道、标题或有效起止时间')
            except ValueError:
                stats['invalid_programmes'] += 1
                continue
            valid += 1
            # 按北京时间与当天求交集；保留相交的整条跨天节目，不截断到零点。
            if stop <= day_start or start >= day_end:
                stats['outside_day_programmes'] += 1
                continue
            record = deepcopy(programme)
            record.set('channel', canonical)
            for previous in record.findall('previously-shown'):
                if previous.get('channel') in remap:
                    previous.set('channel', remap[previous.get('channel')])
            gaps = timelines[canonical].add(start, stop, record, day_start, day_end)
            stats['added_segments'] += len(gaps)
            if not gaps:
                stats['shadowed_programmes'] += 1
            elif gaps != [(start, stop)]:
                stats['trimmed_programmes'] += 1
        if not valid:
            raise ValueError(f'源 {source_id} 没有有效节目，保留旧 EPG')
        summary['sources'].append(stats)
    result = ET.Element('tv', {'generator-info-name': 'iptv-m3u EPG merger'})
    active = sorted(name for name, line in timelines.items() if line.items)
    if not active:
        raise ValueError('指定北京时间日期没有有效节目，保留旧 EPG')
    for canonical in active:
        result.append(channels[canonical])
    for canonical in active:
        result.extend(record for _, _, record in timelines[canonical].items)
    summary.update(channels=len(active), programmes=sum(len(line.items) for line in timelines.values()))
    return result, summary


def validate(root: ET.Element, target_date: date | None = None) -> dict:
    if root.tag != 'tv':
        raise ValueError('EPG 根节点必须为 tv')
    channels = root.findall('channel')
    ids = [channel.get('id') for channel in channels]
    if not ids or len(ids) != len(set(ids)) or any(not value for value in ids):
        raise ValueError('EPG 频道 ID 缺失或重复')
    if any(not any(name_key(node.text or '') for node in channel.findall('display-name')) for channel in channels):
        raise ValueError('EPG 频道名称缺失')
    known, last_stop, count = set(ids), {}, 0
    first, last = None, None
    for programme in root.findall('programme'):
        channel = programme.get('channel')
        start, stop = parse_time(programme.get('start', '')), parse_time(programme.get('stop', ''))
        if channel not in known or stop <= start or not (programme.findtext('title') or '').strip():
            raise ValueError('EPG 节目引用、时间或标题无效')
        if target_date is not None:
            day_start = datetime.combine(target_date, day_time.min, tzinfo=SHANGHAI)
            if stop <= day_start or start >= day_start + timedelta(days=1):
                raise ValueError('EPG 含与指定日期不相交的节目')
        if channel in last_stop and start < last_stop[channel]:
            raise ValueError('EPG 含重复、重叠或顺序错误的节目')
        last_stop[channel] = stop
        first, last = min(first, start) if first else start, max(last, stop) if last else stop
        count += 1
    if not count:
        raise ValueError('EPG 没有有效节目')
    return {'channels': len(ids), 'programmes': count, 'start': format_time(first), 'stop': format_time(last)}


def write_atomic(root: ET.Element, output: Path, target_date: date | None = None) -> dict:
    stats = validate(root, target_date)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.epg-', suffix='.tmp', dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            ET.indent(root, space='  ')
            ET.ElementTree(root).write(stream, encoding='utf-8', xml_declaration=True)
            stream.write(b'\n')
        validate(ET.parse(temporary).getroot(), target_date)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return stats


def run(config_path: Path, output: Path, source_dir: Path | None = None, target_date: date | None = None) -> dict:
    # 整轮采用开始时的北京时间日期，避免下载过程中跨零点导致窗口不一致。
    target_date = target_date or datetime.now(SHANGHAI).date()
    config = json.loads(config_path.read_text(encoding='utf-8'))
    timeout, attempts = float(config.get('timeout_seconds', 60)), int(config.get('max_attempts', 3))
    if not math.isfinite(timeout) or timeout <= 0 or not 1 <= attempts <= 5:
        raise ValueError('超时须大于零，重试次数须为 1～5')
    sources, seen = [], set()
    for source in config['sources']:
        source_id, url = source['id'], source['url']
        parts = urlsplit(url)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', source_id) or source_id in seen:
            raise ValueError('EPG 来源 ID 无效或重复')
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('EPG 来源必须为不带账号的 HTTPS 地址')
        seen.add(source_id)
        print(f'[EPG 源 {source_id}] {"读取本地文件" if source_dir else "下载"} {url}', flush=True)
        raw = ((source_dir / f'source-{source_id}.xml.gz').read_bytes() if source_dir
               else download(url, timeout, attempts))
        sources.append((source_id, parse_source(raw)))
    if not sources:
        raise ValueError('EPG 来源列表为空')
    merged, summary = merge(sources, target_date)
    summary.update(write_atomic(merged, output, target_date))
    summary['output'] = str(output.resolve())
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/epg.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'epg.xml')
    parser.add_argument('--date', type=date.fromisoformat, help='目标北京时间日期 YYYY-MM-DD，默认当天')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true', help='只校验现有 epg.xml，不联网')
    modes.add_argument('--source-dir', type=Path, help='从 source-<id>.xml.gz 离线读取三个来源')
    args = parser.parse_args()
    try:
        stats = (validate(ET.parse(args.output).getroot(), args.date) if args.check
                 else run(args.config, args.output, args.source_dir, args.date))
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, HTTPException, EOFError) as exc:
        print(f'EPG 更新失败，保留旧文件：{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

"""下载并验证咪咕列表；生成、缓存及发布复用 b.py。"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
import time

from m3u import ATTRIBUTE, parse_playlist, valid_http

MAX_BYTES = 32 * 1024 * 1024


def fetch_migu(config: dict, session=None) -> dict:
    import requests

    sources = config.get('sources')
    timeout, attempts = float(config.get('timeout_seconds', 30)), int(config.get('max_attempts', 3))
    if (not isinstance(sources, list) or not sources or not math.isfinite(timeout) or timeout <= 0
            or not 1 <= attempts <= 5):
        raise ValueError('咪咕 sources 必须非空，超时为正数，重试次数为1～5')
    for item in sources:
        if (not isinstance(item, dict) or not isinstance(item.get('url'), str) or not valid_http(item['url'])
                or not isinstance(item.get('group', ''), str)
                or not isinstance(item.get('repair_name_separator', False), bool)):
            raise ValueError('咪咕源 URL、分组或格式修正配置无效')
    owned = session is None
    session = session or requests.Session()
    records, snapshots = [], []
    try:
        for number, item in enumerate(sources, 1):
            for attempt in range(1, attempts + 1):
                print(f'[咪咕 源{number}] 下载，尝试 {attempt}/{attempts}', flush=True)
                pause = min(2 ** attempt, 30)
                try:
                    with session.get(item['url'], timeout=(15, timeout), stream=True) as response:
                        if response.status_code in (408, 429, 500, 502, 503, 504):
                            value = response.headers.get('Retry-After', '0')
                            try:
                                retry_after = float(value) if value.isdigit() else max(0, parsedate_to_datetime(value).timestamp() - time.time())
                            except (ValueError, TypeError, OverflowError): retry_after = 0
                            if retry_after > 120:
                                raise ValueError('咪咕源要求长时间冷却，本轮保留旧文件')
                            pause = max(pause, retry_after, 60 if response.status_code == 429 else 0)
                            raise requests.ConnectionError(f'HTTP {response.status_code}')
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > MAX_BYTES: raise ValueError('咪咕源响应超过32 MiB')
                            chunks.append(chunk)
                        raw = b''.join(chunks)
                    text = raw.decode('utf-8-sig')
                    rows = parse_playlist(text, item.get('repair_name_separator', False))
                    if not rows or any(not valid_http(row['url']) for row in rows):
                        raise ValueError('咪咕源没有频道或含无效 HTTP(S) 地址')
                    header = {match[1].lower(): match[2] if match[2] is not None else match[3]
                              for match in ATTRIBUTE.finditer(text.splitlines()[0])}
                    for row in rows:
                        for key in ('catchup', 'catchup-source', 'catchup-days', 'catchup-correction'):
                            if key in header: row['attrs'].setdefault(key, header[key])
                    if item.get('group'):
                        for row in rows: row['attrs']['group-title'] = item['group']
                    records.extend(rows)
                    snapshots.append(dict(item, sha256=hashlib.sha256(raw).hexdigest(), channels=len(rows), bytes=len(raw)))
                    print(f'[咪咕 源{number}] 有效频道 {len(rows)} 条', flush=True)
                    break
                except requests.exceptions.SSLError as exc:
                    raise ValueError(f'咪咕源{number} HTTPS 校验失败') from exc
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as exc:
                    if attempt == attempts: raise ValueError(f'咪咕源{number} 下载失败：{type(exc).__name__}') from exc
                    print(f'[咪咕 源{number}] {type(exc).__name__}，等待 {pause:g}s 重试', flush=True)
                    time.sleep(pause)
                except requests.exceptions.RequestException as exc:
                    raise ValueError(f'咪咕源{number} 请求失败：{type(exc).__name__}') from exc
                except ValueError as exc:
                    raise ValueError(f'咪咕源{number} 内容校验失败：{exc}') from exc
        return {'records': records, 'source': {'kind': 'migu', 'sources': snapshots,
                'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}}
    finally:
        if owned: session.close()

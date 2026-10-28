"""仅采集酒店和组播：按省前 5 页，无有效频道时延伸至第 8 页。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
from html.parser import HTMLParser
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from m3u import OPERATORS, PROVINCES, parse_playlist, source_timestamp, valid_http

SOURCE_TYPES = ('hotel', 'multicast')


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    providers: dict
    scope: tuple[str, ...]
    source: dict


def usable_status(value):
    return re.fullmatch(r'新上线|存活\s*[0-9]+\s*天', value.strip()) is not None


def operator_name(value):
    matches = [op for op in OPERATORS if op in (value or '')]
    return matches[0] if len(matches) == 1 else None


def update_time(value):
    try: datetime.fromisoformat(value or '')
    except ValueError: return None
    return value


def same_site(url, base):
    a, b = urlsplit(url), urlsplit(base)
    return valid_http(url) and (a.scheme, a.netloc) == (b.scheme, b.netloc)


class Page(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        if raw.lstrip().startswith('{'):
            data = json.loads(raw)
            if data.get('status') != 'success' or not isinstance(data.get('html'), str):
                raise SourceError('源站接口未成功返回 HTML')
            raw = data['html']
        if not raw.strip() or any(s in raw for s in ('安全验证中', 'Access denied', 'captcha-container', '域名不在安全策略')):
            raise SourceError('源站返回验证页面、访问拦截或空响应')
        self.rows, self.links, self.scripts, self.provinces, self.text = [], [], [], {}, []
        self.row = self.cell = self.option = self.link = None
        self.region, self.hidden, self.tables = False, 0, 0
        self.has_table = False
        self.feed(raw)
        if self.tables or self.row is not None or self.cell is not None:
            raise SourceError('源站表格响应不完整')
        self.empty = bool(re.search(r'暂无(?:相关)?(?:数据|记录|结果)|暂无(?:可用)?\s*IP|没有找到(?:匹配|符合条件)的\s*IP', '\n'.join(self.text), re.I))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'): self.hidden += 1
        if tag == 'script' and a.get('src'): self.scripts.append(a['src'])
        if tag == 'table': self.tables += 1; self.has_table = True
        if tag == 'tr':
            if self.row is not None: raise SourceError('源站表格行未结束')
            self.row = {'cells': [], 'token': None}
        if tag == 'td' and self.row is not None:
            if self.cell is not None: raise SourceError('源站表格单元格未结束')
            self.cell = []
        if tag == 'select': self.region = a.get('name') == 'province'
        if tag == 'option' and self.region: self.option = [a.get('value', ''), []]
        if tag == 'a':
            self.link = [a.get('href', ''), []]
            match = re.fullmatch(r"\s*gotoIP\(['\"]([^'\"]+)['\"],\s*['\"]([a-z]+)['\"]\)\s*;?\s*", a.get('onclick', ''))
            if match and self.row is not None: self.row.update(token=match[1], type=match[2])

    def handle_data(self, text):
        if self.hidden: return
        if text.strip(): self.text.append(text.strip())
        for target in (self.cell, self.option[1] if self.option else None, self.link[1] if self.link else None):
            if target is not None: target.append(text)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.hidden -= 1
        if tag == 'table': self.tables -= 1
        if tag == 'td' and self.cell is not None and self.row is not None:
            self.row['cells'].append(''.join(self.cell).strip()); self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row); self.row = self.cell = None
        if tag == 'option' and self.option:
            name = ''.join(self.option[1]).strip()
            if name in PROVINCES and self.option[0]: self.provinces[name] = self.option[0]
            self.option = None
        if tag == 'select': self.region = False
        if tag == 'a' and self.link:
            self.links.append((self.link[0], ''.join(self.link[1]).strip())); self.link = None

    def field(self, label):
        match = re.search(r'(?:^|\n)\s*' + re.escape(label) + r'\s*[:：]\s*([^\n]+)', '\n'.join(self.text))
        return match[1].strip() if match else None


def signing_key(script):
    # 只解析公开网页参数，不执行下载的 JavaScript。
    match = re.search(r'var\s+constantPool\s*=\s*(\[.*?\]);', script, re.S)
    try: key = json.loads(match[1])[0] if match else None
    except (ValueError, IndexError, TypeError) as exc: raise SourceError('源站签名脚本结构变化') from exc
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9]{32,128}', key) or 'generatePaerToken' not in script:
        raise SourceError('源站签名脚本结构变化')
    return key.encode('ascii')


class Client:
    def __init__(self, config):
        try: import requests
        except ImportError as exc: raise SourceError('在线采集需安装 requests==2.34.2') from exc
        self.requests, self.base = requests, config['url']
        self.delay = float(config.get('request_interval_seconds', 3))
        self.timeout = float(config.get('timeout_seconds', 45))
        self.attempts = int(config.get('max_attempts', 5))
        if (not valid_http(self.base) or urlsplit(self.base).query or urlsplit(self.base).path != '/index.php'
                or not math.isfinite(self.delay) or self.delay < 0 or not math.isfinite(self.timeout)
                or self.timeout <= 0 or not 1 <= self.attempts <= 5):
            raise SourceError('源站地址或网络参数无效')
        self.lock, self.local, self.stop = threading.Lock(), threading.local(), threading.Event()
        self.sessions, self.key, self.next_request = [], None, 0
        self.stats = {'requests': 0, 'retries': 0}

    def session(self):
        if not hasattr(self.local, 'session'):
            session = self.requests.Session()
            session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; IPTV-M3U/1.0)', 'Referer': self.base})
            if hasattr(self, 'cookies'): session.cookies.update(self.cookies)
            with self.lock: self.sessions.append(session)
            self.local.session = session
        return self.local.session

    def wait(self):
        while True:
            with self.lock:
                pause = max(0, self.next_request - time.monotonic())
                if not pause and not self.stop.is_set():
                    self.next_request = time.monotonic() + self.delay
                    return
            if self.stop.wait(pause): raise SourceError('其他请求失败，停止剩余采集')

    def get(self, url, label='首页', signed=True):
        url = urljoin(self.base, url)
        if not same_site(url, self.base): raise SourceError('拒绝请求源站以外的地址')
        for attempt in range(1, self.attempts + 1):
            self.wait()
            headers, retry_after = {}, 0
            if signed:
                if not self.key: raise SourceError('请求协议尚未初始化')
                message = f'{int(time.time())}|{secrets.token_hex(10)}'
                headers = {'X-Requested-With': 'XMLHttpRequest',
                           'X-CSRF-TOKEN': message + '|' + hmac.new(self.key, message.encode(), hashlib.sha256).hexdigest()}
            with self.lock: self.stats['requests'] += 1
            print(f'[{label}] GET {urlsplit(url).path}，尝试 {attempt}/{self.attempts}', flush=True)
            try:
                with self.session().get(url, headers=headers, timeout=(15, self.timeout), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if status == 200:
                        chunks, size = [], 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > 32 * 1024 * 1024: raise SourceError(f'[{label}] 响应超过 32 MiB')
                            chunks.append(chunk)
                        return b''.join(chunks).decode('utf-8-sig')
                    if status not in (408, 429, 500, 502, 503, 504): raise SourceError(f'[{label}] HTTP {status}，停止更新')
                    reason = f'HTTP {status}'
                    value = response.headers.get('Retry-After', '0')
                    try:
                        retry_after = float(value) if value.isdigit() else max(0, parsedate_to_datetime(value).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError): pass
                    if retry_after > 120: raise SourceError(f'[{label}] 服务端要求长时间冷却，停止本轮')
            except self.requests.exceptions.SSLError as exc:
                raise SourceError(f'[{label}] HTTPS 证书或 TLS 校验失败') from exc
            except (self.requests.exceptions.Timeout, self.requests.exceptions.ConnectionError,
                    self.requests.exceptions.ChunkedEncodingError, self.requests.exceptions.ContentDecodingError) as exc:
                reason = type(exc).__name__
            except self.requests.exceptions.RequestException as exc:
                raise SourceError(f'[{label}] 请求失败：{type(exc).__name__}') from exc
            if attempt == self.attempts: raise SourceError(f'[{label}] {reason}，尝试次数已用完')
            pause = max(retry_after, 60 if reason == 'HTTP 429' else min(2 ** attempt, 30))
            with self.lock:
                self.stats['retries'] += 1
                if reason == 'HTTP 429': self.delay = min(max(5, self.delay * 2), 15)
                self.next_request = max(self.next_request, time.monotonic() + pause)
            print(f'[{label}] {reason}，全局冷却 {pause:g}s 后重试', flush=True)

    def bootstrap(self):
        home = Page(self.get(self.base, signed=False))
        scripts = [urljoin(self.base, p) for p in home.scripts if re.fullmatch(r'pae\d+\.js', urlsplit(p).path.rsplit('/', 1)[-1])]
        if len(scripts) != 1 or not same_site(scripts[0], self.base) or not home.provinces:
            raise SourceError('源站首页缺少省份选项或唯一的签名脚本')
        self.key = signing_key(self.get(scripts[0], '初始化请求协议', signed=False))
        self.cookies = self.session().cookies.copy()
        return home

    def close(self):
        for session in self.sessions: session.close()


def servers(page, province, skip):
    if not page.provinces or not page.has_table and not page.empty: raise SourceError(f'{province} 列表页结构异常')
    result, matching, conflicting = [], 0, 0
    for row in page.rows:
        cells = row['cells']
        if not cells: continue
        if not row['token']:
            if not page.empty: raise SourceError(f'{province} 列表缺少服务器入口')
            continue
        if row.get('type') not in SOURCE_TYPES: continue
        if len(cells) < 6: raise SourceError(f'{province} 服务器列数不足：{len(cells)}/6')
        if not usable_status(cells[5]): continue
        operator = operator_name(cells[2])
        if operator is None:
            print(f'[{province}] 列表运营商无法识别，跳过该服务器', flush=True)
            continue
        provider = province + operator
        if province not in cells[2]:
            conflicting += 1
            skip(provider, '列表省份归属不符')
            continue
        matching += 1
        count = int(cells[1]) if re.fullmatch(r'[0-9]+', cells[1]) else None
        if count == 0: continue
        if count is None: print(f'[{provider}] 列表频道数量无效，按实际下载内容处理', flush=True)
        updated = update_time(cells[4])
        if updated is None: print(f'[{provider}] 列表更新时间无效，尝试详情时间', flush=True)
        result.append(dict(row, provider=provider, count=count, updated=updated))
    if conflicting and not matching: raise SourceError(f'{province} 列表可用服务器全部跨省')
    if not any(row['token'] for row in page.rows) and not page.empty: raise SourceError(f'{province} 列表缺少明确记录或空结果')
    return result


def next_page(page, current, base):
    links = {urljoin(current, href) for href, label in page.links if label == '下一页'}
    if not links: return None
    if len(links) != 1: raise SourceError('下一页链接冲突')
    target = links.pop()
    old, new = parse_qs(urlsplit(current).query), parse_qs(urlsplit(target).query)
    if (not same_site(target, base) or urlsplit(target).path != urlsplit(base).path
            or any(old.get(k) != new.get(k) for k in ('t', 'province', 'limit'))
            or new.get('page') != [str(int(old['page'][0]) + 1)]):
        raise SourceError('源站分页跳过、换组或离开源站')
    return target


def download_url(page, base, kind):
    links = {urljoin(base, href) for href, _ in page.links if parse_qs(urlsplit(href).query).get('s')}
    if len(links) != 1: raise SourceError('详情页缺少唯一频道入口')
    parts = urlsplit(links.pop()); params = parse_qs(parts.query)
    if not same_site(urlunsplit(parts), base) or parts.path != urlsplit(base).path or params.get('t') != [kind]:
        raise SourceError('频道入口不属于本次源站或类型')
    params.update(channels=['1'], download=['m3u'])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params, doseq=True), ''))


def fetch_source(config, provinces=None, client=None):
    normal, fallback = int(config.get('pages_per_province', 5)), int(config.get('fallback_pages', 8))
    workers, maximum = int(config.get('max_workers', 2)), int(config.get('max_servers_per_operator', 5))
    if not 1 <= normal <= fallback <= 8 or not 1 <= workers <= 4 or maximum < 1:
        raise SourceError('页数须为 1～8，并发为 1～4，每运营商来源上限须大于零')
    owned, started = client is None, time.monotonic()
    client = client or Client(config)
    try:
        home = client.bootstrap()
        selected = list(dict.fromkeys(provinces or [p for p in PROVINCES if p in home.provinces]))
        if not selected or any(p not in home.provinces for p in selected): raise SourceError('所选省份不在当前源站列表中')
        source = {'kind': 'website', 'url': config['url'], 'protocol': 'index',
                  'source_types': list(SOURCE_TYPES),
                  'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'checked_provinces': selected,
                  'page_policy': {'size': 10, 'normal': normal, 'fallback_if_empty': fallback}}

        def collect(province):
            url = config['url'] + '?' + urlencode(dict(t='all', province=home.provinces[province], limit=10, page=1))
            seen, candidates, groups, skipped = set(), [], {}, []

            def skip(provider, reason):
                skipped.append({'provider': provider, 'reason': reason})
                print(f'[{provider}] {reason}，跳过该服务器', flush=True)

            for number in range(1, fallback + 1):
                page = Page(client.get(url, f'{province} 列表第 {number} 页'))
                candidates.extend(row for row in servers(page, province, skip) if (row['type'], row['token']) not in seen)
                tokens = {(r.get('type'), r['token']) for r in page.rows if r['token']}
                if tokens and not tokens - seen: raise SourceError(f'{province} 分页内容重复')
                seen.update(tokens)
                ended = page.empty and not tokens or not any(label == '下一页' for _, label in page.links)
                if number >= normal or ended:
                    candidates.sort(key=lambda r: (source_timestamp(r['updated']), r['type'] == 'multicast'), reverse=True)
                    for server in candidates:
                        provider = server['provider']
                        group = groups.setdefault(provider, {'records': [], 'retained': False, 'source': {'servers': []}})
                        if len(group['source']['servers']) >= maximum: continue
                        detail = Page(client.get(config['url']+'?'+urlencode(dict(p=server['token'], t=server['type'])), f'{provider} 详情'))
                        status = detail.field('状态')
                        if status is None:
                            skip(provider, '详情状态缺失')
                            continue
                        if not usable_status(status):
                            if '失效' not in status: skip(provider, '详情状态无法识别')
                            continue
                        owner = detail.field('类型')
                        operator = operator_name(owner)
                        if operator is None:
                            skip(provider, '详情运营商无法识别')
                            continue
                        if province not in owner or province + operator != provider:
                            skip(provider, '详情省份或运营商归属不符')
                            continue
                        updated = update_time(detail.field('更新时间'))
                        times = [value for value in (server['updated'], updated) if value is not None]
                        if not times:
                            skip(provider, '列表与详情均无有效更新时间')
                            continue
                        if updated is None: print(f'[{provider}] 详情更新时间无效，使用列表时间', flush=True)
                        updated = max(times, key=source_timestamp)
                        try: target = download_url(detail, config['url'], server['type'])
                        except ValueError as exc:
                            skip(provider, str(exc))
                            continue
                        # 网络异常继续向外抛出，保持整轮失败保护。
                        try: raw = client.get(target, f'{provider} 完整 M3U')
                        except UnicodeDecodeError:
                            skip(provider, 'M3U 不是有效 UTF-8 文本')
                            continue
                        try: rows = parse_playlist(raw)
                        except ValueError as exc:
                            skip(provider, f'M3U 格式异常：{exc}')
                            continue
                        downloaded = len(rows)
                        if server['count'] is not None and downloaded != server['count']:
                            print(f'[{provider}] 列表标注 {server["count"]} 条，下载 {downloaded} 条，按实际内容处理', flush=True)
                        rows = [row for row in rows if valid_http(row['url'])]
                        if len(rows) < downloaded:
                            print(f'[{provider}] 下载 {downloaded} 条，跳过 {downloaded - len(rows)} 条无效或非 HTTP 地址，保留 {len(rows)} 条', flush=True)
                        if not rows: continue
                        for row in rows: row.update(source_type=server['type'], source_status=status, source_updated_at=updated)
                        group['records'].extend(rows)
                        group['source']['servers'].append({'type': server['type'], 'channels': len(rows), 'updated_at': updated,
                                                           'sha256': hashlib.sha256(raw.encode()).hexdigest()})
                    candidates.clear()
                    if any(g['records'] for g in groups.values()) or ended or number == fallback: break
                url = next_page(page, url, config['url'])
            result = {name: g for name, g in groups.items() if g['records']}
            print(f'[{province}] 完成：{number} 页，{len(result)} 个运营商组合', flush=True)
            return result, number, datetime.now(timezone.utc).isoformat(timespec='seconds'), skipped

        groups, pages, checked, skipped = {}, {}, {}, {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            tasks = {pool.submit(collect, province): province for province in selected}
            try:
                for task in as_completed(tasks):
                    data, count, checked_at, issues = task.result()
                    province = tasks[task]
                    groups.update(data); pages[province] = count; checked[province] = checked_at; skipped[province] = issues
            except Exception:
                client.stop.set()
                for task in tasks: task.cancel()
                raise
        source['pages'] = {p: pages[p] for p in selected}
        source['skipped_servers'] = [issue for p in selected for issue in skipped[p]]
        source['protected_providers'] = sorted({issue['provider'] for issue in source['skipped_servers']} - set(groups))
        source['unavailable_provinces'] = [
            {'province': p, 'pages': 8, 'checked_at': checked[p],
             'url': config['url'] + '?' + urlencode(dict(t='all', province=home.provinces[p], limit=10, page=8))}
            for p in selected if pages[p] == 8 and not any(name.startswith(p) for name in groups)
            and not any(name.startswith(p) for name in source['protected_providers'])]
        source['metrics'] = dict(client.stats, elapsed_seconds=round(time.monotonic() - started, 2))
        for group in groups.values():
            group['source'].update({k: v for k, v in source.items()
                                    if k not in ('unavailable_provinces', 'protected_providers', 'skipped_servers')})
        print(json.dumps({'crawler': source['metrics'], 'pages': source['pages']}, ensure_ascii=False), flush=True)
        return SourceSnapshot(groups, tuple(provinces or PROVINCES), source)
    finally:
        if owned: client.close()

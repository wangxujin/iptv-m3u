"""读取酒店和组播的公开 AJAX 接口，按省份和运营商合并完整频道。"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
import json
import math
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from m3u import OPERATORS, PROVINCES, source_timestamp, valid_http


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    """完整采集的结果；空 providers 也可表示已确认没有可用来源。"""
    providers: dict
    scope: tuple[str, ...]
    source: dict


def usable_status(value: str) -> bool:
    return re.fullmatch(r'新上线|存活\s*[0-9]+\s*天', value.strip()) is not None


def same_site(url: str, base: str) -> bool:
    left, right = urlsplit(url), urlsplit(base)
    return valid_http(url) and (left.scheme, left.netloc) == (right.scheme, right.netloc)


class SiteRedirects(HTTPRedirectHandler):
    def __init__(self, base: str):
        self.base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not same_site(newurl, self.base):
            target = urlsplit(newurl)
            raise SourceError(f'源站重定向到其他域名或协议：{target.scheme}://{target.netloc}{target.path}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, config: dict):
        self.base = config['url']
        if not valid_http(self.base):
            raise SourceError('source.url 必须是 HTTP(S) 地址')
        self.delay = float(config['request_interval_seconds'])
        self.timeout = float(config['timeout_seconds'])
        self.attempts = int(config.get('max_attempts', 3))
        if (not math.isfinite(self.delay) or not math.isfinite(self.timeout)
                or self.delay < 0 or self.timeout <= 0 or not 1 <= self.attempts <= 5):
            raise SourceError('请求间隔须非负，超时须大于零，尝试次数须为 1～5')
        self.last_request = None
        self.opener = build_opener(SiteRedirects(self.base), HTTPCookieProcessor(CookieJar()))

    def get(self, path: str, label: str = '首页') -> str:
        return self.request(path, label=label)

    def post(self, path: str, fields: dict, label: str) -> str:
        return self.request(path, fields, label)

    def request(self, path: str, fields: dict | None = None, label: str = '源站') -> str:
        url = urljoin(self.base, path)
        if not same_site(url, self.base):
            raise SourceError('拒绝请求源站以外的地址')
        parts = urlsplit(url)
        # 只打印请求路径与业务阶段，不记录 nonce、token、Cookie 或查询参数。
        target = f'{parts.scheme}://{parts.netloc}{parts.path}'
        data = urlencode(fields).encode() if fields is not None else None
        request = Request(url, data=data, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; IPTV-M3U/1.0)',
            'Accept': 'text/html,application/json,*/*', 'Referer': self.base})
        for attempt in range(1, self.attempts + 1):
            spacing = (max(0, self.delay - (time.monotonic() - self.last_request))
                       if self.last_request is not None else 0)
            pause = max(spacing, min(5 * 2 ** (attempt - 2), 30) if attempt > 1 else 0)
            if pause:
                time.sleep(pause)
            started = self.last_request = time.monotonic()
            phase = '连接／等待响应头'
            print(f'[{label}] {request.get_method()} {target}，尝试 {attempt}/{self.attempts}，'
                  f'超时 {self.timeout:g}s', flush=True)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    phase = '读取响应正文'
                    payload = response.read(32 * 1024 * 1024 + 1)
                if len(payload) > 32 * 1024 * 1024:
                    raise SourceError(f'[{label}] 源站响应超过 32 MiB')
                return payload.decode('utf-8-sig')
            except SourceError as exc:
                raise SourceError(f'[{label}] {target}：{exc}') from exc
            except HTTPError as exc:
                code = exc.code
                exc.close()
                retryable = code in (408, 500, 502, 503, 504)
                reason = f'HTTP {code}'
                if code in (401, 403):
                    reason += '：访问受限或需要浏览器验证'
                elif code == 429:
                    reason += '：请求被限流，停止本轮更新'
                error = exc
            except (URLError, OSError, HTTPException, UnicodeError) as exc:
                cause = exc.reason if isinstance(exc, URLError) else exc
                retryable = isinstance(cause, (OSError, HTTPException)) and not isinstance(cause, ssl.SSLError)
                reason = type(cause).__name__
                if isinstance(cause, ssl.SSLCertVerificationError):
                    reason += '：证书校验失败'
                elif str(cause):
                    reason += ': ' + str(cause)[:160]
                error = exc
            message = (f'[{label}] {target}，{phase}失败，耗时 {time.monotonic() - started:.1f}s，'
                       f'尝试 {attempt}/{self.attempts}：{reason}')
            if not retryable or attempt == self.attempts:
                raise SourceError(message) from error
            print(message + '；稍后重试', flush=True)
        raise AssertionError('unreachable')


class Page(HTMLParser):
    """读取省份、服务器和频道表，以及 data-page / data-p / data-s。"""
    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        if not content.strip() or any(word in content for word in (
                '安全验证中', '正在确认您的浏览器', 'captcha-container', '域名不在安全策略')):
            raise SourceError('源站返回空页面、浏览器验证或网络拦截页面')
        self.rows, self.provinces, self.pages, self.channel_tokens = [], {}, set(), set()
        self.row = self.cell = self.option = None
        self.region_select = False
        self.has_table = False
        self.table_depth = 0
        self.hidden_depth = 0
        self.text_parts = []
        self.feed(content)
        if self.table_depth or self.row is not None or self.cell is not None:
            raise SourceError('源站表格响应不完整')
        visible = '\n'.join(self.text_parts)
        self.empty_list = bool(re.search(r'暂无(?:数据|记录|结果|组播源|酒店源)|未找到(?:相关)?(?:数据|记录|组播源|酒店源)|没有(?:可用的?)?(?:组播源|酒店源)', visible))
        match = re.search(r'(?:^|\n)\s*状态\s*[:：]\s*([^\n]+)', visible)
        self.status = match.group(1).strip() if match else None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.has_table = True
            self.table_depth += 1
        if tag in ('script', 'style'):
            self.hidden_depth += 1
        if tag == 'tr':
            if self.row is not None:
                raise SourceError('源站表格行未完整结束')
            self.row = {'cells': [], 'token': None}
        elif tag == 'td' and self.row is not None:
            if self.cell is not None:
                raise SourceError('源站表格单元格未完整结束')
            self.cell = []
        elif tag == 'select':
            self.region_select = attrs.get('name') == 'region'
        elif tag == 'option' and self.region_select:
            self.option = [attrs.get('value', ''), []]
        if attrs.get('data-p') and self.row is not None:
            self.row['token'] = attrs['data-p']
        if attrs.get('data-s'):
            self.channel_tokens.add(attrs['data-s'])
        if 'page-link' in attrs.get('class', '').split():
            try:
                number = int(attrs['data-page'])
                if number < 1:
                    raise ValueError('invalid page')
                self.pages.add(number)
            except (KeyError, ValueError) as exc:
                raise SourceError('源站分页格式变化') from exc

    def handle_data(self, data):
        if self.hidden_depth:
            return
        if data.strip():
            self.text_parts.append(data.strip())
        if self.cell is not None:
            self.cell.append(data)
        if self.option is not None:
            self.option[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'table':
            self.table_depth -= 1
        if tag in ('script', 'style'):
            self.hidden_depth -= 1
        if tag == 'td' and self.cell is not None and self.row is not None:
            self.row['cells'].append(''.join(self.cell).strip())
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = self.cell = None
        elif tag == 'option' and self.option is not None:
            name = ''.join(self.option[1]).strip()
            if name in PROVINCES and self.option[0]:
                self.provinces[name] = self.option[0]
            self.option = None
        elif tag == 'select':
            self.region_select = False


def encrypt_token(token: str) -> str:
    # 与公开网页客户端的 AES-ECB / PKCS7 编码一致；每次使用页面实时返回的令牌。
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except ImportError as exc:
        raise SourceError('在线采集需要依赖：python -m pip install pycryptodome==3.23.0') from exc
    cipher = AES.new(b'cQshuShu88888888', AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(token.encode('utf-8'), AES.block_size))).decode('ascii')


class AjaxSite:
    def __init__(self, client: Client, url: str, kind: str = 'multicast'):
        protocols = {'multicast': ('multicastIptvAjax', 'multicast_iptv_ajax'),
                     'hotel': ('hotelIptvAjax', 'hotel_iptv_ajax')}
        if kind not in protocols:
            raise SourceError('不支持的 IPTV 来源类型')
        variable, self.action = protocols[kind]
        self.kind = kind
        self.client = client
        content = client.get(url)
        self.home = Page(content)
        match = re.search(r'var\s+' + variable + r'\s*=\s*(\{.*?\});', content, re.DOTALL)
        if not match:
            raise SourceError(f'源站首页缺少 {variable} 配置；请核对来源类型和 URL')
        try:
            config = json.loads(match.group(1))
        except ValueError as exc:
            raise SourceError('源站 AJAX 配置不是有效 JSON') from exc
        if not isinstance(config, dict) or any(not isinstance(config.get(k), str) or not config[k]
                                               for k in ('ajaxUrl', 'nonce', 'token')):
            raise SourceError('源站 AJAX 配置字段缺失')
        self.url = urljoin(url, config['ajaxUrl'])
        if not same_site(self.url, url):
            raise SourceError('源站 AJAX 地址指向其他域名或协议')
        self.nonce, self.token = config['nonce'], config['token']

    def call(self, action: str, label: str, **fields) -> str:
        token = encrypt_token(self.token) if self.kind == 'multicast' else self.token
        content = self.client.post(self.url, dict(fields, action=self.action,
            action_type=action, nonce=self.nonce, token=token), label)
        try:
            response = json.loads(content)
        except ValueError as exc:
            raise SourceError(f'[{label}] 源站 AJAX 没有返回 JSON，可能为网络拦截或验证页面') from exc
        if not isinstance(response, dict) or response.get('success') is not True:
            detail = response.get('data') if isinstance(response, dict) else None
            message = detail.get('message', '') if isinstance(detail, dict) else ''
            message = re.sub(r'<[^>]+>', '', str(message))
            for secret in (self.nonce, self.token, token):
                message = message.replace(secret, '[redacted]')
            message = ' '.join(message.split())[:160]
            raise SourceError(f'[{label}] 源站 AJAX 返回失败：{message or "未提供原因"}')
        data = response.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('html'), str):
            raise SourceError(f'[{label}] 源站 AJAX 缺少 HTML 内容')
        if data.get('new_token'):
            if not isinstance(data['new_token'], str):
                raise SourceError(f'[{label}] 源站新令牌格式变化')
            self.token = data['new_token']
        return data['html']


def server_rows(page: Page, province: str) -> list[dict]:
    result = []
    for row in page.rows:
        cells = row['cells']
        if not row['token']:
            if len(cells) >= 6 or cells and not page.empty_list:
                raise SourceError('源站服务器表缺少 data-p')
            continue
        if len(cells) < 6:
            raise SourceError('源站服务器表结构变化')
        if province not in cells[2]:
            raise SourceError('源站返回了其他省份的数据')
        if not usable_status(cells[5]):
            continue
        operators = [operator for operator in OPERATORS if operator in cells[2]]
        if len(operators) != 1:
            raise SourceError(f'{province} 存在无法确定运营商的可用服务器')
        if not cells[1].isdigit() or int(cells[1]) < 1:
            raise SourceError(f'{province} 可用服务器缺少有效频道数量')
        try:
            datetime.fromisoformat(cells[4])
        except ValueError as exc:
            raise SourceError(f'{province} 可用服务器的更新时间缺失或无效') from exc
        result.append({'token': row['token'], 'provider': province + operators[0],
                       'host': cells[0], 'channels': int(cells[1]),
                       'status': cells[5], 'updated_at': cells[4]})
    if not any(row['token'] for row in page.rows) and not page.empty_list:
        raise SourceError('服务器列表缺少记录或明确的空结果标记')
    return result


def channel_records(content: str) -> list[dict]:
    page = Page(content)
    if not page.has_table:
        raise SourceError('源站频道表缺失')
    records = []
    for row in page.rows:
        cells = row['cells']
        if not cells:
            continue  # 表头使用 th，不是频道。
        if len(cells) < 3 or not cells[0].isdigit() or not cells[1] or not valid_http(cells[2]):
            raise SourceError('源站频道行不完整或播放地址不是完整 HTTP(S) URL')
        # 直接保留频道页中的完整地址；列表页显示的服务器地址可能与播放地址不同。
        records.append({'name': cells[1], 'url': cells[2], 'attrs': {}, 'options': []})
    if not records:
        raise SourceError('源站频道列表为空')
    return records


def _fetch_site(config: dict, provinces: list[str] | None = None, client=None,
                allow_absent_provinces: bool = False) -> SourceSnapshot:
    """所有选定省份成功后才返回；请求或解析失败不会交付部分更新。"""
    max_pages = int(config['max_pages_per_province'])
    max_servers = int(config['max_servers_per_operator'])
    if max_pages < 1 or max_servers < 0:
        raise SourceError('分页上限须大于零，每运营商来源上限须非负')
    kind = config.get('type', 'multicast')
    label = '酒店' if kind == 'hotel' else '组播'
    site = AjaxSite(client or Client(config), config['url'], kind)
    if not site.home.provinces:
        raise SourceError('源站没有可识别的省份筛选项')
    selected = list(dict.fromkeys(provinces or [name for name in PROVINCES if name in site.home.provinces]))
    if not allow_absent_provinces and any(name not in site.home.provinces for name in selected):
        raise SourceError('所选省份未出现在源站当前筛选项中')
    selected = [name for name in selected if name in site.home.provinces]
    groups = {}
    fetched_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    source = {'kind': 'website', 'url': config['url'], 'fetched_at': fetched_at,
              'source_type': kind, 'checked_provinces': selected, 'max_servers_per_operator': max_servers}
    for province in selected:
        servers, seen = [], set()
        for number in range(1, max_pages + 1):
            page = Page(site.call('list', f'{label}/{province} 列表第 {number} 页', page_num=number,
                limit=20, region=site.home.provinces[province], search=''))
            if not page.has_table or province not in page.provinces:
                raise SourceError(f'{province} 列表页结构变化或内容不完整')
            tokens = {row['token'] for row in page.rows if row['token']}
            following = sorted(p for p in page.pages if p > number)
            if (following or number > 1 and tokens) and not tokens - seen:
                raise SourceError(f'{province} 分页内容重复或缺失')
            servers.extend(row for row in server_rows(page, province) if row['token'] not in seen)
            seen.update(tokens)
            if not following:
                break
            if following[0] != number + 1:
                raise SourceError(f'{province} 分页跳过了下一页')
            if number == max_pages:
                raise SourceError(f'{province} 达到分页上限仍有下一页，停止更新以免截断数据')
        servers.sort(key=lambda row: source_timestamp(row['updated_at']), reverse=True)
        counts = {}
        for server in servers:
            provider = server['provider']
            if max_servers and counts.get(provider, 0) >= max_servers:
                continue
            detail = Page(site.call('detail', f'{label}/{provider} 服务器详情', p=server['token']))
            if detail.status is None:
                raise SourceError(f'{provider} 详情页缺少状态字段')
            if not usable_status(detail.status):
                print(f'[{provider}] 详情状态不符合条件，跳过该服务器', flush=True)
                continue
            if len(detail.channel_tokens) != 1:
                raise SourceError(f'{provider} 详情页缺少唯一的频道入口')
            content = site.call('channels', f'{label}/{provider} 频道列表', s=next(iter(detail.channel_tokens)))
            records = channel_records(content)
            if len(records) < server['channels']:
                raise SourceError(f'{provider} 频道数量少于源站声明值：{len(records)}/{server["channels"]}')
            for record in records:
                record.update(source_type=kind, source_status=detail.status, source_updated_at=server['updated_at'])
            group = groups.setdefault(provider, {'records': [], 'retained': False,
                'source': dict(source, servers=[])})
            group['records'].extend(records)
            group['source']['servers'].append({'host': server['host'], 'channels': len(records), 'status': detail.status,
                'source_type': kind, 'source_url': config['url'], 'updated_at': server['updated_at'],
                'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
            counts[provider] = counts.get(provider, 0) + 1
        print(f'[{label}/{province}] 完成：{sum(counts.values())} 个服务器，{len(counts)} 个运营商组合', flush=True)
    return SourceSnapshot(groups, tuple(provinces or PROVINCES), source)


def fetch_source(config: dict, provinces: list[str] | None = None, client=None) -> SourceSnapshot:
    """全部来源成功才返回合并结果；任何一侧失败都不交付部分更新。"""
    if 'sources' not in config:
        return _fetch_site(config, provinces, client)
    definitions = config['sources']
    if not isinstance(definitions, list) or not definitions:
        raise SourceError('sources 必须为非空来源列表')
    seen = set()
    for definition in definitions:
        if not isinstance(definition, dict) or definition.get('type') not in ('multicast', 'hotel'):
            raise SourceError('每个来源须指定 multicast 或 hotel 类型')
        key = definition.get('type'), definition.get('url')
        if not isinstance(key[1], str) or not valid_http(key[1]) or key in seen:
            raise SourceError('来源 URL 无效或重复')
        seen.add(key)
    snapshots = []
    for index, definition in enumerate(definitions):
        if index:
            time.sleep(float(config['request_interval_seconds']))
        settings = dict(config, **definition)
        snapshots.append(_fetch_site(settings, provinces, client, allow_absent_provinces=True))
    checked = list(dict.fromkeys(provinces or [name for item in snapshots for name in item.source['checked_provinces']]))
    source = {'kind': 'website', 'fetched_at': snapshots[0].source['fetched_at'],
              'checked_provinces': checked, 'sources': [item.source for item in snapshots]}
    groups = {}
    for snapshot in snapshots:
        for provider, data in snapshot.providers.items():
            group = groups.setdefault(provider, {'records': [], 'retained': False,
                                                 'source': dict(source, servers=[])})
            group['records'].extend(data['records'])
            group['source']['servers'].extend(data['source']['servers'])
    return SourceSnapshot(groups, tuple(provinces or PROVINCES), source)

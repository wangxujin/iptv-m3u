"""直接读取源站公开列表和 M3U 下载入口；不依赖其他 GitHub 仓库。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from m3u import OPERATORS, PROVINCES, parse_playlist, valid_http


class SourceError(ValueError):
    pass


def same_site(url: str, base: str) -> bool:
    left, right = urlsplit(url), urlsplit(base)
    return (valid_http(url) and left.scheme == right.scheme
            and left.netloc == right.netloc)


class SiteRedirects(HTTPRedirectHandler):
    def __init__(self, base: str):
        self.base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not same_site(newurl, self.base):
            raise SourceError('源站重定向到其他域名或协议，已停止请求')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, config: dict):
        self.base = config['url'].rstrip('/') + '/'
        if not valid_http(self.base):
            raise SourceError('source.url 必须是 HTTP(S) 地址')
        self.delay = float(config['request_interval_seconds'])
        self.timeout = float(config['timeout_seconds'])
        if self.delay < 0 or self.timeout <= 0:
            raise SourceError('请求间隔必须非负，超时必须大于零')
        self.last_request = None
        self.opener = build_opener(SiteRedirects(self.base), HTTPCookieProcessor(CookieJar()))

    def get(self, path: str) -> str:
        url = urljoin(self.base, path)
        if not same_site(url, self.base):
            raise SourceError('拒绝请求源站以外的地址')
        if self.last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; IPTV-M3U/1.0)',
                                       'Accept': 'text/html,application/json,application/vnd.apple.mpegurl,*/*'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(32 * 1024 * 1024 + 1)
                if len(payload) > 32 * 1024 * 1024:
                    raise SourceError('源站响应超过 32 MiB，停止更新')
                return payload.decode('utf-8-sig')
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403):
                raise SourceError(f'源站 HTTP {code}：访问受限或需要浏览器验证；已有输出保持不变') from exc
            if code == 429:
                raise SourceError('源站 HTTP 429：请求被限流，停止本轮更新；请稍后重试') from exc
            raise SourceError(f'源站 HTTP {code}，停止本轮更新') from exc
        except (URLError, TimeoutError, UnicodeError) as exc:
            raise SourceError(f'源站连接或响应异常：{type(exc).__name__}') from exc


class Page(HTMLParser):
    """只读取列表单元格、公开链接和省份选项，不执行网页脚本。"""
    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        self.rows, self.links, self.provinces = [], [], {}
        self.row = self.cell = self.link = self.option = None
        self.province_select = False
        self.has_table = False
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.has_table = True
        if tag == 'tr':
            self.row = {'cells': [], 'token': None}
        elif tag == 'td' and self.row is not None:
            self.cell = []
        elif tag == 'select':
            self.province_select = attrs.get('name') == 'province'
        elif tag == 'option' and self.province_select:
            self.option = [attrs.get('value', ''), []]
        if tag == 'a':
            self.link = [attrs.get('href', ''), []]
            match = re.search(r"gotoIP\(['\"]([^'\"]+)['\"],\s*['\"]multicast['\"]\)", attrs.get('onclick', ''))
            if match and self.row is not None:
                self.row['token'] = match.group(1)

    def handle_data(self, data):
        for parts in (self.cell, self.link[1] if self.link else None,
                      self.option[1] if self.option else None):
            if parts is not None:
                parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None and self.row is not None:
            self.row['cells'].append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = self.cell = None
        elif tag == 'a' and self.link is not None:
            self.links.append((self.link[0], ''.join(self.link[1]).strip()))
            self.link = None
        elif tag == 'option' and self.option is not None:
            name = ''.join(self.option[1]).strip()
            if name in PROVINCES and self.option[0]:
                self.provinces[name] = self.option[0]
            self.option = None
        elif tag == 'select':
            self.province_select = False


def html_page(content: str) -> Page:
    if content.lstrip().startswith('{'):
        data = json.loads(content)
        if data.get('status') != 'success' or not isinstance(data.get('html'), str):
            raise SourceError('源站接口返回失败，停止更新')
        content = data['html']
    if not content.strip() or any(word in content for word in ('安全验证中', '正在确认您的浏览器', 'captcha-container')):
        raise SourceError('源站返回空页面或浏览器验证页面，停止更新')
    return Page(content)


def server_rows(page: Page, province: str) -> list[dict]:
    result = []
    for row in page.rows:
        if not row['token']:
            continue
        cells = row['cells']
        if len(cells) < 6:
            raise SourceError('源站服务器表结构变化')
        if province not in cells[2]:
            raise SourceError('源站返回了其他省份的数据')
        operators = [operator for operator in OPERATORS if operator in cells[2]]
        if not any(status in cells[5] for status in ('新上线', '存活')):
            continue
        if len(operators) != 1:
            raise SourceError(f'{province} 存在无法确定运营商的可用服务器')
        count = re.fullmatch(r'\d+', cells[1])
        result.append({'token': row['token'], 'provider': province + operators[0],
                       'host': cells[0], 'channels': int(count.group()) if count else None,
                       'status': cells[5], 'updated_at': cells[4]})
    return result


def next_page(page: Page, current: str, base: str) -> str | None:
    here = urlsplit(urljoin(base, current))
    values = parse_qs(here.query)
    for href, label in page.links:
        if label != '下一页':
            continue
        target = urljoin(urljoin(base, current), href)
        there = urlsplit(target)
        params = parse_qs(there.query)
        if (not same_site(target, base) or there.path != here.path
                or any(params.get(key) != values.get(key) for key in ('t', 'province'))):
            raise SourceError('源站下一页指向其他站点或省份')
        try:
            if int(params.get('page', ['0'])[0]) <= int(values.get('page', ['1'])[0]):
                raise ValueError('non-increasing page')
        except ValueError as exc:
            raise SourceError('源站分页未向前推进') from exc
        return target
    return None


def channel_link(page: Page, base: str) -> str:
    for href, _ in page.links:
        url = urljoin(base, href)
        values = parse_qs(urlsplit(url).query)
        if same_site(url, base) and values.get('s') and values.get('t') == ['multicast']:
            return url
    raise SourceError('源站详情页缺少频道列表链接')


def download_link(page: Page, base: str) -> str:
    for href, _ in page.links:
        url = urljoin(base, href)
        if same_site(url, base) and parse_qs(urlsplit(url).query).get('download') == ['m3u']:
            return url
    raise SourceError('源站频道页缺少 M3U 下载入口')


def fetch_source(config: dict, provinces: list[str] | None = None, client=None) -> dict:
    """全部选定省份成功后才返回；任一请求失败不会向调用方交付部分更新。"""
    client = client or Client(config)
    base = config['url'].rstrip('/') + '/'
    max_pages = int(config['max_pages_per_province'])
    max_servers = int(config['max_servers_per_operator'])
    if max_pages < 1 or max_servers < 0:
        raise SourceError('分页上限必须大于零，每运营商来源上限必须非负')
    home = html_page(client.get('index.php?t=multicast'))
    if not home.provinces:
        raise SourceError('源站没有可识别的省份筛选项，可能需要浏览器验证')
    selected = provinces or [name for name in PROVINCES if name in home.provinces]
    if any(name not in home.provinces for name in selected):
        raise SourceError('所选省份未出现在源站当前筛选项中')
    groups = {}
    fetched_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for province in selected:
        path = 'index.php?' + urlencode({'t': 'multicast', 'province': home.provinces[province], 'limit': 10, 'page': 1})
        servers, seen = [], set()
        for index in range(max_pages):
            page = html_page(client.get(path))
            if not page.provinces or not page.has_table:
                raise SourceError(f'{province} 列表页结构变化或内容不完整')
            tokens = {row['token'] for row in page.rows if row['token']}
            following = next_page(page, path, base)
            if (following or (index > 0 and tokens)) and not tokens - seen:
                raise SourceError(f'{province} 分页内容重复或缺失')
            servers.extend(row for row in server_rows(page, province) if row['token'] not in seen)
            seen.update(tokens)
            if not following:
                break
            if index + 1 == max_pages:
                raise SourceError(f'{province} 达到分页上限仍有下一页，停止更新以免截断数据')
            path = following
        # 按新上线优先、更新时间降序取来源；0 表示不限制服务器数量。
        servers.sort(key=lambda row: (row['status'].find('新上线') >= 0, row['updated_at']), reverse=True)
        counts = {}
        for server in servers:
            provider = server['provider']
            if max_servers and counts.get(provider, 0) >= max_servers:
                continue
            detail_path = 'index.php?' + urlencode({'p': server['token'], 't': 'multicast'})
            channels_url = channel_link(html_page(client.get(detail_path)), urljoin(base, detail_path))
            m3u_url = download_link(html_page(client.get(channels_url)), channels_url)
            content = client.get(m3u_url)
            records = parse_playlist(content)
            if not records or (server['channels'] and len(records) < server['channels']):
                raise SourceError(f'{provider} 下载结果为空或少于源站声明的频道数量')
            group = groups.setdefault(provider, {'records': [], 'retained': False, 'source': {
                'kind': 'website', 'url': base, 'fetched_at': fetched_at,
                'max_servers_per_operator': max_servers, 'servers': []}})
            group['records'].extend(records)
            group['source']['servers'].append({'host': server['host'], 'channels': len(records),
                'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
            counts[provider] = counts.get(provider, 0) + 1
        print(f'[{province}] 完成：下载 {sum(counts.values())} 个服务器的完整 M3U', flush=True)
    if not groups:
        raise SourceError('源站没有可用的频道列表，保留已有文件')
    return groups

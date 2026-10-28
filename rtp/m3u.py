"""M3U 解析、频道分类与单文件导出。只依赖 Python 标准库。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

PROVINCES = '北京 天津 河北 山西 内蒙古 辽宁 吉林 黑龙江 上海 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 广西 海南 重庆 四川 贵州 云南 西藏 陕西 甘肃 青海 宁夏 新疆'.split()
OPERATORS = ('电信', '联通', '移动', '广电')
PROVIDER = re.compile('^(' + '|'.join(PROVINCES) + ')(' + '|'.join(OPERATORS) + r')(?:-?\d+)?$')
ATTRIBUTE = re.compile(r'''\s*,?\s*([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')''')


def provider_name(stem: str) -> str:
    match = PROVIDER.fullmatch(stem)
    if not match:
        raise ValueError(f'无法识别省份和运营商：{stem}')
    return match.group(1) + match.group(2)


def parse_extinf(line: str) -> tuple[str, dict]:
    head = re.match(r'#EXTINF:-?\d+(?:\.\d+)?', line)
    if not head:
        raise ValueError('EXTINF 时长格式无效')
    position, attributes = head.end(), {}
    while match := ATTRIBUTE.match(line, position):
        attributes[match.group(1).lower()] = match.group(2) if match.group(2) is not None else match.group(3)
        position = match.end()
    tail = line[position:].lstrip()
    if not tail.startswith(',') or not tail[1:].strip():
        raise ValueError('EXTINF 缺少频道名称或分隔符')
    return tail[1:].strip(), attributes


def parse_playlist(content: str) -> list[dict]:
    lines = content.lstrip('\ufeff').splitlines()
    if not lines or not re.fullmatch(r'#EXTM3U(?:\s.*)?', lines[0].strip()):
        raise ValueError('缺少 #EXTM3U 文件头')
    records, pending, options = [], None, []
    for raw in lines[1:]:
        line = raw.strip()
        if line.startswith(('#EXT-X-TARGETDURATION', '#EXT-X-STREAM-INF', '#EXT-X-MEDIA-SEQUENCE')):
            raise ValueError('HLS 媒体清单不能作为频道目录')
        if line.startswith('#EXTINF:'):
            if pending is not None:
                raise ValueError('频道缺少播放地址')
            pending = parse_extinf(line)
            options = []
        elif line.startswith(('#EXTVLCOPT:', '#KODIPROP:')) and pending:
            options.append(line)
        elif line and not line.startswith('#'):
            if pending is None:
                raise ValueError('播放地址缺少 EXTINF')
            name, attrs = pending
            records.append({'name': name, 'url': line, 'attrs': attrs, 'options': options})
            pending, options = None, []
    if pending is not None:
        raise ValueError('文件末尾频道缺少播放地址')
    return records


def valid_http(url: str) -> bool:
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or re.search(r'%(?![0-9a-fA-F]{2})', url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None and not parsed.fragment
                and (parsed.port is None or 0 < parsed.port <= 65535))
    except ValueError:
        return False


def normalized_name(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).upper().strip()
    value = re.sub(r'\s+', '', value).replace('PLUS', '+').replace('＋', '+')
    value = re.sub(r'^CCTV[-_]', 'CCTV', value)
    # 清晰度不决定节目类别；保留 CCTV4K / CCTV8K 这种频道本名。
    value = re.sub(r'(?:[（(]?(?:超高清|高清|标清|超清|FHD|UHD|HD|SD)[）)]?)+$', '', value)
    return value


def normalize_group(name: str, g: str = '') -> str:
    """按用户给出的顺序规范 OK影视 / TVBox 菜单，英文忽略大小写。"""
    s = unicodedata.normalize('NFKC', f'{name} {g}')
    for pattern, group in [
        (r'4K|2160|UHD|超高清|8K', '4K超清'),
        (r'CCTV|CGTN|央视|中央', '央视频道')]:
        if re.search(pattern, s, re.I):
            return group
    if re.search(r'卫视', name):
        return '卫视频道'
    for pattern, group in [
        (r'体育|运动|NBA|足球|篮球|赛事|奥运', '体育频道'),
        (r'港|澳|台|HK|Macau|Taiwan|Hong', '港澳台'),
        (r'少儿|动画|卡通|亲子', '少儿频道'),
        (r'电影|影视|剧场|综艺', '影视频道'),
        (r'纪录|探索|Discovery|科教|知识', '纪实频道'),
        (r'新闻|财经|法治|军事', '新闻频道')]:
        if re.search(pattern, s, re.I):
            return group
    if re.search(r'频道', g) and not re.search(r'高清|备用|Undefined|General', g, re.I):
        return '地方频道'
    if re.search(r'高清|卫视|央视|备用', g, re.I):
        return g
    return '其他'


class Classifier:
    def __init__(self, config: dict):
        self.order = config['group_order']
        if not self.order or len(set(self.order)) != len(self.order):
            raise ValueError('group_order 必须非空且不能重复')
        self.aliases = {normalized_name(k): normalized_name(v) for k, v in config.get('aliases', {}).items()}

    def classify(self, name: str, g: str = '') -> str:
        return normalize_group(name, g)


def channel_key(name: str, classifier: Classifier) -> str:
    key = normalized_name(name)
    suffix = r'(?:[-_（(\[]?(?:4K|8K|2160[PI]?|4320[PI]?|1080[PI]?|720[PI]?|超高清|超清|高清|标清|蓝光|FHD|UHD|HD|SD)[）)\]]?)+$'
    while key not in ('CCTV4K', 'CCTV8K'):
        base = re.sub(suffix, '', key).strip('-_ ')
        if base == key or not base:
            break
        key = base
    key = classifier.aliases.get(key, key)
    descriptions = {'1': '综合', '2': '财经', '3': '综艺', '4': '中文国际', '5': '体育',
                    '5+': '体育赛事', '6': '电影', '7': '国防军事', '8': '电视剧', '9': '纪录',
                    '10': '科教', '11': '戏曲', '12': '社会与法', '13': '新闻', '14': '少儿',
                    '15': '音乐', '16': '奥林匹克', '17': '农业农村'}
    for number, description in descriptions.items():
        if key in (f'CCTV{number}', f'CCTV{number}{description}', f'CCTV{number}-{description}'):
            return f'CCTV{number}'
    return key


def quality_rank(record: dict) -> int:
    attrs = record.get('attrs', {})
    text = ' '.join(str(value) for value in (record['name'], attrs.get('group-title', ''),
                    attrs.get('tvg-name', ''), record.get('resolution', '')))
    text = unicodedata.normalize('NFKC', text)
    if re.search(r'8K|4320', text, re.I):
        return 4
    if re.search(r'4K|2160|UHD|超高清', text, re.I):
        return 3
    if re.search(r'HD|1080|720|高清|蓝光', text, re.I):
        return 2
    return 1


def source_timestamp(value: str) -> float:
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=timezone(timedelta(hours=8))) if stamp.tzinfo is None else stamp
        return stamp.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0


def route_rank(record: dict) -> tuple:
    return (source_timestamp(record.get('source_updated_at', '')),
            record.get('source_type') == 'multicast', quality_rank(record))


def better_route(record: dict, previous: dict) -> bool:
    rank, old_rank = route_rank(record), route_rank(previous)
    return rank > old_rank or rank == old_rank and (record['url'], record['name']) < (previous['url'], previous['name'])


def clean_attribute(value: str) -> str:
    return re.sub(r'[\x00-\x1f\x7f"]', ' ', value).strip()


def natural_key(value: str) -> tuple:
    return tuple((0, int(piece)) if piece.isdigit() else (1, piece.casefold()) for piece in re.split(r'(\d+)', value))


def playlist_header(epg_url: str | None = None) -> str:
    if epg_url is None:
        return '#EXTM3U'
    if not valid_http(epg_url) or '"' in epg_url:
        raise ValueError('EPG 地址必须是有效的 HTTP(S) URL')
    return f'#EXTM3U x-tvg-url="{epg_url}"'


def build_provider(records: list[dict], classifier: Classifier, epg_url: str | None = None) -> tuple[str, dict]:
    """一个省份＋运营商始终只生成一个 M3U，不按来源、数量或字节数分卷。"""
    by_url = {}
    invalid = duplicate = 0
    for record in records:
        url = record['url']
        if not valid_http(url):
            invalid += 1
        elif url in by_url:
            duplicate += 1
            if better_route(record, by_url[url]):
                by_url[url] = record
        else:
            by_url[url] = record
    selected = {}
    for record in by_url.values():
        key = channel_key(record['name'], classifier), quality_rank(record) >= 3
        if key not in selected or better_route(record, selected[key]):
            selected[key] = record
    channel_duplicates = len(by_url) - len(selected)
    accepted = [(classifier.classify(record['name'], record.get('attrs', {}).get('group-title', '')), record)
                for record in selected.values()]
    rank = {group: index for index, group in enumerate(classifier.order)}
    accepted.sort(key=lambda item: (rank.get(item[0], len(rank)), item[0],
                  natural_key(channel_key(item[1]['name'], classifier)), -quality_rank(item[1]), item[1]['url']))
    m3u = [playlist_header(epg_url)]
    for group, record in accepted:
        attrs = dict(record.get('attrs', {}))
        attrs.setdefault('tvg-id', record['name'])
        attrs['group-title'] = group
        keys = [key for key in ('tvg-id', 'tvg-name', 'tvg-logo', 'group-title') if key in attrs]
        keys += sorted(set(attrs) - set(keys))
        metadata = ' '.join(f'{key}="{clean_attribute(str(attrs[key]))}"' for key in keys)
        name = re.sub(r'[\x00-\x1f\x7f]', ' ', record['name']).strip()
        m3u.append(f'#EXTINF:-1 {metadata},{name}')
        m3u.extend(record.get('options', []))
        m3u.append(record['url'])
    content = '\n'.join(m3u) + '\n'
    stats = {'input_channels': len(records), 'channels': len(accepted),
             'duplicates_removed': duplicate + channel_duplicates, 'exact_url_duplicates_removed': duplicate,
             'channel_duplicates_removed': channel_duplicates,
             'selection_policy': 'one_uhd_and_one_other_newest_update_multicast_tie', 'invalid_or_non_http_removed': invalid,
             'groups': dict(Counter(group for group, _ in accepted)), 'bytes': len(content.encode('utf-8')),
             'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()}
    return content, stats


def load_classifier(path: Path) -> Classifier:
    return Classifier(json.loads(path.read_text(encoding='utf-8')))

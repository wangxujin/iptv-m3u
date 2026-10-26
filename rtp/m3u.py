"""M3U 解析、频道分类与单文件导出。只依赖 Python 标准库。"""
from __future__ import annotations

from collections import Counter
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


class Classifier:
    def __init__(self, config: dict):
        self.order = config['group_order']
        if not self.order or len(set(self.order)) != len(self.order):
            raise ValueError('group_order 必须非空且不能重复')
        self.default = config['default_group']
        self.exact = {}
        for group, names in config['exact_channels'].items():
            if group not in self.order:
                raise ValueError(f'未声明的分组：{group}')
            for name in names:
                key = normalized_name(name)
                if key in self.exact and self.exact[key] != group:
                    raise ValueError(f'频道映射冲突：{name}')
                self.exact[key] = group
        self.aliases = {normalized_name(k): normalized_name(v) for k, v in config.get('aliases', {}).items()}
        self.rules = [(re.compile(row['pattern'], re.I), row['group']) for row in config['rules']]
        if self.default not in self.order or any(group not in self.order for _, group in self.rules):
            raise ValueError('规则使用了未声明的分组')

    def classify(self, name: str) -> str:
        key = normalized_name(name)
        key = self.aliases.get(key, key)
        if key in self.exact:
            return self.exact[key]
        return next((group for pattern, group in self.rules if pattern.search(key)), self.default)


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
    accepted, seen = [], set()
    invalid = duplicate = 0
    for record in records:
        url = record['url']
        if not valid_http(url):
            invalid += 1
        elif url in seen:
            duplicate += 1
        else:
            seen.add(url)
            accepted.append((classifier.classify(record['name']), record))
    rank = {group: index for index, group in enumerate(classifier.order)}
    accepted.sort(key=lambda item: (rank[item[0]], natural_key(item[1]['name']), item[1]['url']))
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
             'duplicates_removed': duplicate, 'invalid_or_non_http_removed': invalid,
             'groups': dict(Counter(group for group, _ in accepted)), 'bytes': len(content.encode('utf-8')),
             'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()}
    return content, stats


def load_classifier(path: Path) -> Classifier:
    return Classifier(json.loads(path.read_text(encoding='utf-8')))

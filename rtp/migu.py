"""按 ioptu/migu_video 的请求协议直采咪咕，生成与发布接入现有 b.py。"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import random
import re
import time

from m3u import clean_attribute, playlist_header, valid_http


CATALOGUE_URL = 'https://program-sc.miguvideo.com/live/v2/tv-data/'
PLAY_URL = 'https://play.miguvideo.com/playurl/v1/play/playurl'
PROXY_URL = 'https://workspace.apipost.net/proxy/v2/http'
APP_VERSION = '2600034600'
CATALOGUE_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Origin': 'https://m.miguvideo.com',
    'Pragma': 'no-cache',
    'Referer': 'https://m.miguvideo.com/',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
    'Support-Pendant': '1',
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0'),
    'appCode': 'miguvideo_default_h5',
    'appId': 'miguvideo',
    'channel': 'H5',
    'sec-ch-ua': '"Chromium";v="150", "Microsoft Edge";v="150", "Not.A/Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'terminalId': 'h5',
}
PROXY_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': CATALOGUE_HEADERS['Accept-Language'],
    'apipost-language': 'zh-cn',
    'apipost-machine': 'bd2d54b7c2002',
    'apipost-platform': 'Win',
    'apipost-terminal': 'web',
    'apipost-version': '8.2.7',
    'cache-control': 'no-cache',
    'content-type': 'application/json',
    'pragma': 'no-cache',
    'priority': 'u=1, i',
    'sec-ch-ua': '"Google Chrome";v="150", "Chromium";v="150", "Not A(Brand";v="24"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-origin',
    'Referer': 'https://workspace.apipost.net/guest/apis',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
}
CATEGORIES = {
    '央视': '1ff892f2b5ab4a79be6e25b69d2f5d05',
    '卫视': '0847b3f6c08a4ca28f85ba5701268424',
    '地方': '855e9adc91b04ea18ef3f2dbd43f495b',
    '体育': '7538163cdac044398cb292ecf75db4e0',
    '影视': '10b0d04cb23d4ac5945c4bc77c7ac44e',
    '少儿': 'fc2f5b8fd7db43ff88c4243e731ecede',
    '新闻': 'c584f67ad63f4bc983c31de3a9be977c',
    '教育': 'af72267483d94275995a4498b2799ecd',
    '纪实': 'e1165138bdaa44b9a3138d74af6c6673',
}
EXCLUDED = {
    'CHC动作电影', 'CHC家庭影院', '海南广播电视总台自贸频道', '海南广播电视总台社会与法频道',
    '海南广播电视总台新闻频道', '海南广播电视总台文旅频道', '海南广播电视总台少儿频道',
}


CATCHUP_SOURCE = '&playbackbegin=${(b)yyyyMMddHHmmss}&playbackend=${(e)yyyyMMddHHmmss}'


@dataclass(frozen=True)
class Channel:
    name: str
    pid: str
    category: str
    logo: str


def md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def getSaltAndSign(pid):
    timestamp = str(int(time.time() * 1000))
    salt = f'{random.randint(0, 999999):06d}25'
    app_text = timestamp + pid + APP_VERSION[:8]
    suffix = '2cac4f2c6c3346a5b34e085725ef7e33migu' + salt[:4]
    return {'salt': salt, 'sign': md5(md5(app_text) + suffix), 'timestamp': timestamp}


def proxy_parameter(key, value, identity, header=False):
    parameter = {'param_id': identity, 'field_type': 'string', 'is_checked': 1,
                 'key': key, 'value': value, 'not_None': 1, 'description': ''}
    if header:
        parameter['schema'] = {'type': 'string'}
    return parameter


def get_content(pid, http=None, timeout=15):
    if http is None:
        import requests
        http = requests
    client_id = md5(str(int(time.time() * 1000)))
    signed = getSaltAndSign(pid)
    query_values = {
        'sign': signed['sign'],
        'rateType': '2' if pid == '608831231' else '3',
        'contId': pid,
        'timestamp': signed['timestamp'],
        'salt': signed['salt'],
        'flvEnable': 'true',
        'super4k': 'true',
    }
    url = PLAY_URL + '?' + '&'.join(f'{key}={value}' for key, value in query_values.items())
    header_parameters = [
        proxy_parameter('AppVersion', APP_VERSION, '3c60653273e0b3', header=True),
        proxy_parameter('TerminalId', 'android', '3c6075c1f3e0e1', header=True),
        proxy_parameter('X-UP-CLIENT-CHANNEL-ID', APP_VERSION + '-99000-201600010010028',
                        '3c60858bb3e10c', header=True),
        proxy_parameter('ClientId', client_id, 'clientid_new', header=True),
    ]
    if pid not in ('641886683', '641886773'):
        header_parameters.append(proxy_parameter('appCode', 'miguvideo_default_android',
                                                  'appcode_new', header=True))
    header = {'parameter': header_parameters}
    query = {'parameter': [proxy_parameter(key, value, f'qp_{index}')
                            for index, (key, value) in enumerate(query_values.items())]}
    target_id = '3c5fd6a9786002'
    default_server = {'server_id': '1', 'name': '默认服务', 'sort': 1000, 'uri': ''}
    payload = {
        'option': {
            'scene': 'http_request', 'lang': 'zh-cn', 'globals': {},
            'project': {
                'request': {
                    'header': header, 'query': query, 'body': {'parameter': []},
                    'cookie': {'parameter': []}, 'auth': {'type': 'noauth'},
                    'pre_tasks': [], 'post_tasks': [],
                },
            },
            'env': {
                'env_id': '1', 'env_name': '默认环境', 'env_pre_url': '',
                'env_pre_urls': {'1': default_server, 'default': default_server},
                'environment': {},
            },
            'cookies': {'switch': 1, 'data': []},
            'system_configs': {
                'send_timeout': 0, 'auto_redirect': -1, 'max_redirect_time': 5,
                'auto_gen_mock_url': -1, 'request_param_auto_json': -1,
                'proxy': {
                    'type': 2, 'envfirst': 1, 'bypass': [], 'protocols': ['http'],
                    'auth': {'authenticate': -1, 'host': '', 'username': '', 'password': ''},
                },
                'ca_cert': {'open': -1, 'path': '', 'base64': ''}, 'client_cert': {},
            },
            'custom_functions': {},
            'collection': [{
                'target_id': target_id, 'target_type': 'api', 'parent_id': '0', 'name': 'MIGU',
                'request': {
                    'auth': {'type': 'inherit'},
                    'body': {
                        'mode': 'None', 'parameter': [], 'raw': '', 'raw_parameter': [],
                        'raw_schema': {'type': 'object'}, 'binary': None,
                    },
                    'pre_tasks': [], 'post_tasks': [], 'header': header,
                    'query': dict(query, query_add_equal=1),
                    'cookie': {'parameter': [], 'cookie_encode': 1},
                    'restful': {'parameter': []}, 'tabs_default_active_key': 'query',
                },
                'parents': [], 'method': 'GET', 'protocol': 'http/1.1', 'url': url, 'pre_url': '',
            }],
            'database_configs': {},
        },
        'test_events': [{
            'type': 'api',
            'data': {'target_id': target_id, 'project_id': '57a21612a051000',
                     'parent_id': '0', 'target_type': 'api'},
        }],
    }
    # ApiPost 的外层响应中嵌套了咪咕返回的 JSON 字符串。
    with http.post(PROXY_URL, headers=PROXY_HEADERS,
                       data=json.dumps(payload, separators=(',', ':')), timeout=timeout) as response:
        response.raise_for_status()
        result = response.json()
    return json.loads(result['data']['data']['response']['body'])


def getddCalcu720p(url, pid):
    pu_data = url.split('&puData=', 1)[1]
    keys = 'cdabyzwxkl'
    characters = []
    for index in range(len(pu_data) // 2):
        characters.extend((pu_data[-index - 1], pu_data[index]))
        if index == 1:
            characters.append('v')
        if index == 2:
            characters.append(keys[int(datetime.now().strftime('%Y%m%d')[2])])
        if index == 3:
            characters.append(keys[int(pid[6])])
        if index == 4:
            characters.append('a')
    return url + '&ddCalcu=' + ''.join(characters) + '&sv=10004&ct=android'


def playback_address(channel, http, timeout, attempts):
    for attempt in range(1, attempts + 1):
        try:
            result = get_content(channel.pid, http, timeout)
            if not isinstance(result, dict):
                raise ValueError('咪咕响应格式异常，预期为 JSON 对象')
            body = result.get('body')
            info = body.get('urlInfo') if isinstance(body, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get('url'), str) or not info['url']:
                code = result.get('code', '未知')
                message = result.get('message') or result.get('msg') or '接口未提供原因'
                message = ' '.join(str(message).split())[:240]
                raise ValueError(f'未返回播放地址（业务码 {code}，{message}）')
            content = body.get('content')
            actual_pid = (content.get('contId') if isinstance(content, dict) else None) or channel.pid
            url = getddCalcu720p(info['url'], str(actual_pid))
            if not valid_http(url):
                raise ValueError('播放地址格式异常')
            print(f'{channel.name}：取址成功，rateType={info.get("rateType", "未知")}', flush=True)
            return url, ''
        # 每个频道自行重试和返回失败原因，异常不会传播到其他线程的频道任务。
        except Exception as exc:
            if getattr(exc, 'response', None) is not None:
                reason = f'代理请求 HTTP {exc.response.status_code}'
            elif isinstance(exc, ValueError):
                reason = str(exc) or type(exc).__name__
            else:
                reason = type(exc).__name__
            if attempt < attempts:
                pause = 2 ** attempt + random.uniform(0.5, 1.5)
                print(f'{channel.name}：第 {attempt}/{attempts} 次失败（{reason}），{pause:.2f}s 后重试', flush=True)
                time.sleep(pause)
    return None, reason


def get_group_priority_and_key(extinf):
    """央视按 CCTV 数字、CGTN、欧洲/美洲、其余排列；卫视和其它组内按原名排列。"""
    name_match = re.search(r',(.+?)$', extinf)
    name = name_match[1].strip() if name_match else ''
    if '央视' in extinf or 'CCTV' in name.upper() or 'CGTN' in name.upper():
        group = '央视'
        number = re.search(r'CCTV(\d+)', name)
        if number:
            return (0, 0, int(number[1]), name), group
        if 'CGTN' in name:
            return (0, 1, 0, name), group
        if '欧洲' in name or '美洲' in name:
            return (0, 2, 0, name), group
        return (0, 3, 0, name), group
    if '卫视' in extinf or '卫视' in name:
        return (1, 0, 0, name), '卫视'
    return (2, 0, 0, name), '其它'


def channel_metadata(channel):
    name = ' '.join(channel.name.splitlines())
    attr_name = name.replace('"', "'")
    logo = ''.join(channel.logo.splitlines()).replace('"', '')
    return (f'#EXTINF:-1 tvg-id="{attr_name}" tvg-name="{attr_name}" '
            f'tvg-logo="{logo}" group-title="{channel.category}",{name}')


def catalogue(http, timeout, attempts):
    channels = []
    seen = set()
    failed_categories = []
    for category, group_id in CATEGORIES.items():
        for attempt in range(1, attempts + 1):
            try:
                with http.get(CATALOGUE_URL + group_id, headers=CATALOGUE_HEADERS,
                              timeout=(5, timeout)) as response:
                    response.raise_for_status()
                    rows = response.json()['body']['dataList']
                if not isinstance(rows, list):
                    raise ValueError('目录格式异常')
                break
            except Exception as exc:
                if attempt == attempts:
                    failed_categories.append({'category': category, 'reason': type(exc).__name__})
                    print(f'[咪咕] {category}目录失败（{type(exc).__name__}），继续其他分类', flush=True)
                    rows = []
                else:
                    time.sleep(2 ** (attempt - 1))
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                print(f'[咪咕] {category}跳过格式异常记录', flush=True)
                continue
            name = row['name'].strip()
            pid = str(row.get('pID') or '')
            if not name or not pid.isdigit() or name in EXCLUDED or (name, pid) in seen:
                continue
            seen.add((name, pid))
            pictures = row.get('pics')
            logo = pictures.get('highResolutionH') if isinstance(pictures, dict) else ''
            channels.append(Channel(name, pid, category, logo if isinstance(logo, str) else ''))
    return channels, failed_categories


def fetch_migu(config: dict, session=None) -> dict:
    """一个分类或频道失败只记录原因；全部取址失败时由 b.py 保留旧产物。"""
    if not isinstance(config, dict) or 'sources' in config:
        raise ValueError('请使用新版 config/migu.json 的直采配置')
    try:
        timeout = float(config.get('timeout_seconds', 15))
    except (TypeError, ValueError) as exc:
        raise ValueError('咪咕超时必须为正数') from exc
    attempts = config.get('max_attempts', 3)
    workers = config.get('max_workers', 5)
    if (not math.isfinite(timeout) or timeout <= 0
            or type(attempts) is not int or not 1 <= attempts <= 5
            or type(workers) is not int or not 1 <= workers <= 5):
        raise ValueError('咪咕超时必须为正数，重试次数和并发数必须为1～5的整数')
    if session is None:
        import requests
        session = requests
    channels, failed_categories = catalogue(session, timeout, attempts)
    records, failures = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda channel: playback_address(channel, session, timeout, attempts), channels)
        for channel, (url, reason) in zip(channels, results):
            if not url:
                failures.append({'name': channel.name, 'pid': channel.pid, 'reason': reason})
                print(f'[咪咕] {channel.name}：{reason}', flush=True)
                continue
            records.append({'name': channel.name, 'url': url, 'options': [], 'attrs': {
                'tvg-id': channel.name, 'tvg-name': channel.name, 'tvg-logo': channel.logo,
                'group-title': channel.category, 'catchup': 'append', 'catchup-source': CATCHUP_SOURCE,
            }})
    if not records:
        raise ValueError('咪咕全部取址失败，保留旧文件和上次数据快照')
    return {'records': records, 'source': {
        'kind': 'migu', 'mode': 'direct', 'url': CATALOGUE_URL, 'proxy_url': PROXY_URL,
        'categories': list(CATEGORIES), 'catalogue_channels': len(channels),
        'fetched_channels': len(records), 'failed_channels': failures, 'failed_categories': failed_categories,
        'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }}


def build_playlist(records: list[dict], epg_url: str | None):
    """按上游主列表的名称去重和排序，保持频道名与返回的播放地址。"""
    selected = {}
    invalid = duplicate = 0
    for record in records:
        if not valid_http(record['url']):
            invalid += 1
            continue
        attrs = record.get('attrs', {})
        key = attrs.get('tvg-id') or attrs.get('tvg-name') or record['name']
        if key in selected:
            duplicate += 1
            continue
        selected[key] = record
    entries = []
    for record in selected.values():
        attrs = record.get('attrs', {})
        metadata = channel_metadata(Channel(record['name'], '', attrs.get('group-title', ''), attrs.get('tvg-logo', '')))
        key, group = get_group_priority_and_key(metadata)
        entries.append((key, group, record))
    entries.sort(key=lambda entry: entry[0])
    lines = [playlist_header(epg_url)]
    for _, group, record in entries:
        attrs = dict(record.get('attrs', {}))
        attrs.setdefault('tvg-id', record['name'])
        attrs['group-title'] = group
        keys = [key for key in ('tvg-id', 'tvg-name', 'tvg-logo', 'group-title') if key in attrs]
        keys += sorted(set(attrs) - set(keys))
        metadata = ' '.join(f'{key}="{clean_attribute(str(attrs[key]))}"' for key in keys)
        name = re.sub(r'[\x00-\x1f\x7f]', ' ', record['name']).strip()
        lines.append(f'#EXTINF:-1 {metadata},{name}')
        lines.extend(record.get('options', []))
        lines.append(record['url'])
    content = '\n'.join(lines) + '\n'
    return content, {
        'input_channels': len(records), 'channels': len(entries), 'duplicates_removed': duplicate,
        'exact_url_duplicates_removed': 0, 'channel_duplicates_removed': duplicate,
        'selection_policy': 'first_success_by_name_upstream_order', 'invalid_or_non_http_removed': invalid,
        'groups': dict(Counter(group for _, group, _ in entries)), 'bytes': len(content.encode('utf-8')),
        'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
    }

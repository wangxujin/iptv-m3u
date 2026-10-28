#!/usr/bin/env python3
"""直接读取源站，按省份＋运营商合并，按顶层菜单规则分组并优选每档线路。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote
from zoneinfo import ZoneInfo

from m3u import Classifier, PROVINCES, OPERATORS, build_provider, parse_playlist, playlist_header, provider_name
from source import SourceSnapshot, fetch_source

ROOT = Path(__file__).resolve().parents[1]
ONLINE_POLICY = 'replace_confirmed_scope_remove_unavailable'
DEFAULT_README = """# IPTV M3U

{{SUMMARY}}

仅采集酒店和组播源；每个省份与运营商组合只输出一个 M3U。
同一频道超高清与其他画质各保留一条，各档先按更新时间从新到旧选择，更新时间相同优先组播，再比较清晰度。
分组按 4K超清、央视频道、卫视频道、体育频道、港澳台、少儿频道、影视频道、纪实频道、新闻频道等顺序规范。
频道分类见 [config/groups.json](config/groups.json)，源站设置见 [config/source.json](config/source.json)，
仓库、分支与 EPG 设置见 [config/project.json](config/project.json)。

## 订阅列表

{{FILES}}

{{UNAVAILABLE}}

来源时间不代表播放可用性已验证；历史来源表示保留的缓存。

## 本地使用

需要 Python 3.10+。在线采集依赖 Requests，Actions 会自动安装。

```sh
python3 -m pip install requests==2.34.2
python3 rtp/b.py           # 从源站采集并生成
python3 rtp/b.py --offline # 使用已有缓存重建
python3 rtp/b.py --check   # 校验生成结果
python3 epg/update.py     # 按 1 → 3 → 2 合并 EPG
python3 epg/update.py --check # 校验 epg.xml.gz
```

EPG 每天与频道列表一起在北京时间 00:00、12:00 更新；EPG 先独立发布。
GitHub schedule 显式设置 timezone: Asia/Shanghai；平台负载仍可能造成触发延迟。
来源顺序见 config/epg.json，同名频道合并，节目冲突优先保留前面的来源，后面的来源补齐空缺时段。
仅输出北京时间当天及与当天相交的跨天节目，保留跨天节目的完整起止时间。
M3U 文件头引用项目配置中的 EPG 地址。

首次成功采集后自动创建 data/、m3u/、manifest.json、epg.xml.gz 和本文件。采集失败不会清空已有结果。
在线采集成功后自动删除本轮范围内确认没有可用来源的 M3U；仅接收新上线或存活 N 天，并复查详情状态。
列表或详情无法唯一识别运营商时，记录日志并跳过该服务器。
完整采集确认零可用来源时允许清空该范围；异常情况按以下规则处理。
列表数量仅供参考，数量无效仍下载，下载数量不一致只提示；更新时间由列表、详情互补，两处均无效则跳过。
单台归属冲突、缺少状态或频道入口、M3U 格式损坏时跳过，不占来源名额。
单台数据异常导致某组合无有效新结果时保留旧文件，并在“来源异常待核查”中记录。
任一网络请求、列表或分页失败仍保留本轮全部旧文件。
按省份采集，每页 10 条，先采前 5 页；没有有效频道才延伸至第 6～8 页，找到即停。
两个省份并行，线程共用限速和 429 冷却；每轮直接下载新地址的完整 M3U。
可用 --fresh 从空数据开始验证指定省份。
"""


def git(folder: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(folder), *args], check=True, capture_output=True,
                            text=True, timeout=180, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    return result.stdout.strip()


def read_source(folder: Path) -> dict:
    """可选导入本地 M3U 目录，不需要 Git 仓库，也不执行任何外部脚本。"""
    imported_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    groups = {}
    directory = folder / 'm3u' if (folder / 'm3u').is_dir() else folder
    for path in sorted(directory.glob('*.m3u')):
        provider = provider_name(path.stem)
        raw = path.read_bytes()
        records = parse_playlist(raw.decode('utf-8-sig'))
        if not records:
            raise ValueError(f'导入列表为空：{path.name}')
        data = groups.setdefault(provider, {'records': [], 'source': {
            'kind': 'local_import', 'imported_at': imported_at, 'files': []},
            'retained': False})
        data['records'].extend(records)
        data['source']['files'].append({'name': path.name, 'sha256': hashlib.sha256(raw).hexdigest()})
    if not groups:
        raise ValueError('本地目录没有有效 M3U；保留原有文件')
    return groups


def merge_source(state: dict, incoming: dict) -> dict:
    result = json.loads(json.dumps(state))
    for data in result['providers'].values():
        data['retained'] = True
    result['providers'].update(incoming)
    result['last_sync'] = next(iter(incoming.values()))['source'].copy()
    result['last_sync'].pop('files', None)
    result['last_sync'].pop('servers', None)
    return result


def apply_snapshot(state: dict, snapshot: SourceSnapshot) -> dict:
    """只在完整采集成功后替换已确认范围；局部更新保留其他省份。"""
    if not isinstance(snapshot, SourceSnapshot) or not snapshot.scope or not snapshot.source.get('checked_provinces'):
        raise ValueError('在线结果缺少已完成的采集范围')
    if not set(snapshot.scope) <= set(PROVINCES) or snapshot.source.get('kind') != 'website':
        raise ValueError('在线结果的采集范围或来源类型无效')
    for provider, data in snapshot.providers.items():
        provider_name(provider)
        if not any(provider.startswith(province) for province in snapshot.scope) or data.get('retained') is not False:
            raise ValueError('在线结果超出已确认范围或混入历史来源')
    protected = set(snapshot.source.get('protected_providers', []))
    for provider in protected:
        provider_name(provider)
        if not any(provider.startswith(province) for province in snapshot.scope):
            raise ValueError('异常保留组合超出采集范围')
    result = json.loads(json.dumps(state))
    result['providers'] = {name: dict(data, retained=True) for name, data in result['providers'].items()
                           if name in protected or not any(name.startswith(province) for province in snapshot.scope)}
    result['providers'].update(snapshot.providers)
    result['last_sync'] = snapshot.source.copy()
    result['source_policy'] = ONLINE_POLICY
    return result


def repository_name(explicit: str | None, root: Path) -> str | None:
    value = explicit or os.getenv('GITHUB_REPOSITORY')
    if not value:
        try:
            remote = git(root, 'remote', 'get-url', 'origin')
            match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$', remote)
            value = match.group(1) if match else None
        except (subprocess.SubprocessError, OSError):
            pass
    if not value:
        config_path = root / 'config/project.json'
        if config_path.exists():
            value = json.loads(config_path.read_text(encoding='utf-8')).get('repository')
    if value and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError('仓库名称应为 owner/repository')
    return value


def build_readme(template: str, manifest: dict, repository: str | None, branch: str) -> str:
    rows = ['| 省份／运营商 | M3U | 频道线路 | 数据快照（北京时间） | 来源状态 |', '|---|---|---:|---|---|']
    for provider, item in manifest['providers'].items():
        source = item['source']
        timestamp = source.get('fetched_at') or source.get('imported_at') or source['committed_at']
        date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        date = date.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
        relative = f'm3u/{provider}.m3u'
        url = (f'https://raw.githubusercontent.com/{repository}/refs/heads/{quote(branch, safe="")}/' + quote(relative)
               if repository else quote(relative))
        status = ('保留的历史来源' if item['retained'] else
                  '源站直接采集' if source.get('kind') == 'website' else '本地导入')
        rows.append(f'| {provider} | [M3U]({url}) | {item["channels"]} | {date} | {status} |')
    summary = (f'当前输出 **{manifest["province_count"]} 个省份、{manifest["file_count"]} 份 M3U、'
               f'{manifest["total_channels"]:,} 条频道线路**。其中 {manifest["current_provider_count"]} 份来自最近成功采集或导入，'
               f'{manifest["retained_provider_count"]} 份保留历史来源。每个省份与运营商的组合只有一份文件。')
    scopes = {tuple(item['source'].get('checked_provinces', [])) for item in manifest['providers'].values()}
    if len(scopes) == 1 and next(iter(scopes)):
        summary += '\n\n本轮采集范围：' + '、'.join(next(iter(scopes))) + '；只代表配置的分页范围。'
    coverage = ['| 省份 | 有输出的运营商 | 无输出的运营商 |', '|---|---|---|']
    for province in PROVINCES:
        present = [operator for operator in OPERATORS if province + operator in manifest['providers']]
        missing = [operator for operator in OPERATORS if operator not in present]
        coverage.append(f'| {province} | {"、".join(present) or "—"} | {"、".join(missing) or "—"} |')
    unavailable = ['## 第 8 页仍无可用来源', '']
    failed = manifest.get('unavailable_provinces', [])
    if failed:
        unavailable += ['以下省份已成功处理到第 8 页，仍没有通过状态及详情复查的有效频道，可按链接人工对照。', '',
                        '| 省份 | 已采页数 | 检查时间（北京时间） | 对照查询 |', '|---|---:|---|---|']
        for item in failed:
            checked = datetime.fromisoformat(item['checked_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai'))
            unavailable.append(f'| {item["province"]} | {item["pages"]} | {checked:%Y-%m-%d %H:%M:%S} | [查看第 8 页]({item["url"]}) |')
    else:
        unavailable.append('本轮没有“已采到第 8 页仍无可用来源”的记录。')
    protected = manifest.get('protected_providers', [])
    if protected:
        unavailable += ['', '## 来源异常待核查', '',
                        '以下组合因单台来源异常未取得有效新结果；已有旧文件保留，不按无来源删除。', '',
                        '| 省份／运营商 | 处理 | 原因 |', '|---|---|---|']
        for provider in protected:
            status = '保留旧文件' if provider in manifest['providers'] else '无旧文件'
            reasons = sorted({item['reason'] for item in manifest.get('skipped_servers', []) if item['provider'] == provider})
            unavailable.append(f'| {provider} | {status} | {"；".join(reasons)} |')
    if '{{UNAVAILABLE}}' not in template:
        template += '\n\n{{UNAVAILABLE}}\n'
    return (template.replace('{{SUMMARY}}', summary).replace('{{FILES}}', '\n'.join(rows))
            .replace('{{COVERAGE}}', '\n'.join(coverage)).replace('{{UNAVAILABLE}}', '\n'.join(unavailable)))


def generate(state: dict, config: dict, output: Path, template: str, repository: str | None, branch: str,
             epg_url: str | None = None) -> dict:
    classifier = Classifier(config)
    playlist_header(epg_url)
    for provider in state['providers']:
        if provider_name(provider) != provider:
            raise ValueError('数据缓存包含编号来源而非合并运营商：' + provider)
    manifest = {'schema_version': 1, 'source_policy': state.get('source_policy', 'direct_website_with_explicit_retention'),
                'split_files': False,
                'mapping_sha256': hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                'epg_url': epg_url, 'playback_tested': False, 'providers': {}}
    manifest['unavailable_provinces'] = state.get('last_sync', {}).get('unavailable_provinces', [])
    manifest['protected_providers'] = state.get('last_sync', {}).get('protected_providers', [])
    manifest['skipped_servers'] = state.get('last_sync', {}).get('skipped_servers', [])
    files = {}
    for provider in sorted(state['providers'], key=lambda p: (PROVINCES.index(next(v for v in PROVINCES if p.startswith(v))), p)):
        data = state['providers'][provider]
        content, stats = build_provider(data['records'], classifier, epg_url)
        if not stats['channels']:
            raise ValueError(f'{provider} 没有有效 HTTP(S) 地址，停止更新并保留旧输出')
        files[f'm3u/{provider}.m3u'] = content
        manifest['providers'][provider] = dict(stats, source=data['source'], retained=data['retained'])
    confirmed_empty = (state.get('source_policy') == ONLINE_POLICY
                       and state.get('last_sync', {}).get('kind') == 'website'
                       and state.get('last_sync', {}).get('checked_provinces'))
    if not files and not confirmed_empty:
        raise ValueError('没有可输出的数据，保留旧文件')
    manifest.update(file_count=len(manifest['providers']),
                    province_count=len({next(p for p in PROVINCES if name.startswith(p)) for name in manifest['providers']}),
                    total_channels=sum(row['channels'] for row in manifest['providers'].values()),
                    current_provider_count=sum(not row['retained'] for row in manifest['providers'].values()),
                    retained_provider_count=sum(row['retained'] for row in manifest['providers'].values()))
    files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    files['data/sources.json'] = json.dumps(state, ensure_ascii=False, indent=2) + '\n'
    files['README.md'] = build_readme(template, manifest, repository, branch)
    # 所有输入先解析、分类、校验成功，再替换输出；请求和解析异常不会清空旧结果。
    output.mkdir(parents=True, exist_ok=True)
    (output / 'm3u').mkdir(exist_ok=True)
    previous = output / 'manifest.json'
    old_providers = json.loads(previous.read_text(encoding='utf-8')).get('providers', {}) if previous.exists() else {}
    for provider in old_providers:
        if provider_name(provider) != provider:
            raise ValueError('旧清单含非法文件名')
    with tempfile.TemporaryDirectory(prefix='.iptv-build-', dir=output) as temporary:
        staging = Path(temporary)
        for name, content in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        for name in files:
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / name, destination)
    # 只清理本工具上次清单中管理的文件，绝不扫描删除用户的其他文件。
    for provider in set(old_providers) - set(manifest['providers']):
        (output / 'm3u' / f'{provider}.m3u').unlink(missing_ok=True)
    return manifest


def validate_outputs(output: Path, config: dict, epg_url: str | None = None) -> dict:
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    state = json.loads((output / 'data/sources.json').read_text(encoding='utf-8'))
    if set(state['providers']) != set(manifest['providers']):
        raise ValueError('缓存与清单中的运营商组合不一致')
    header = playlist_header(epg_url)
    if manifest.get('epg_url') != epg_url:
        raise ValueError('清单中的 EPG 地址与项目配置不一致')
    expected = {name + '.m3u' for name in manifest['providers']}
    actual = {p.name for p in (output / 'm3u').glob('*.m3u')}
    if expected != actual:
        raise ValueError('输出有缺失文件或多余编号分卷')
    total = 0
    classifier = Classifier(config)
    for provider, item in manifest['providers'].items():
        if provider_name(provider) != provider:
            raise ValueError('输出文件名不是唯一省份＋运营商')
        raw = (output / 'm3u' / f'{provider}.m3u').read_bytes()
        if raw.decode('utf-8-sig').splitlines()[:1] != [header]:
            raise ValueError('M3U 文件头与 EPG 配置不一致：' + provider)
        records = parse_playlist(raw.decode('utf-8-sig'))
        if len(records) != item['channels'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('文件内容与清单不一致：' + provider)
        if len({record['url'] for record in records}) != len(records):
            raise ValueError('存在重复 URL：' + provider)
        expected_content, _ = build_provider(state['providers'][provider]['records'], classifier, epg_url)
        if raw != expected_content.encode('utf-8'):
            raise ValueError('频道分组或线路选择与缓存不一致：' + provider)
        total += len(records)
    if total != manifest['total_channels']:
        raise ValueError('频道总数不一致')
    return {'files': len(actual), 'channels': total, 'split_files': False, 'playback_tested': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--offline', action='store_true', help='使用随仓库保存的输入，完全离线重建')
    modes.add_argument('--source-dir', type=Path, help='导入本地 M3U 目录，无需 Git 仓库')
    modes.add_argument('--check', action='store_true', help='只校验输出文件、数量、哈希和分组')
    parser.add_argument('--output', type=Path, default=ROOT)
    parser.add_argument('--repository', help='用于订阅链接的 owner/repository；Actions 中自动识别')
    parser.add_argument('--branch', help='输出订阅地址的分支，默认使用 Actions 分支或项目配置')
    parser.add_argument('--provinces', nargs='+', choices=PROVINCES, help='仅从源站更新指定省份，其他省份保留')
    parser.add_argument('--drop-retained', action='store_true', help='离线或导入时移除历史组合；在线已自动清理采集范围')
    parser.add_argument('--fresh', action='store_true', help='在线采集从空数据开始，只生成本轮来源')
    args = parser.parse_args()
    try:
        config = json.loads((ROOT / 'config/groups.json').read_text(encoding='utf-8'))
        project = json.loads((ROOT / 'config/project.json').read_text(encoding='utf-8'))
        branch = args.branch or os.getenv('GITHUB_REF_NAME') or project['branch']
        if args.provinces and (args.offline or args.source_dir or args.check):
            raise ValueError('--provinces 只能用于在线源站采集')
        if args.fresh and (args.offline or args.source_dir or args.check):
            raise ValueError('--fresh 只能用于在线源站采集')
        if args.check:
            print(json.dumps(validate_outputs(args.output, config, project['epg_url']), ensure_ascii=False))
            return 0
        state_root = ROOT if args.offline or args.source_dir else args.output.resolve()
        state_path = state_root / 'data/sources.json'
        if state_path.exists() and not args.fresh:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        elif args.offline:
            raise ValueError('尚无数据缓存；请先在线采集或使用 --source-dir 导入 M3U')
        else:
            state = {'schema_version': 1, 'providers': {}}
        if args.source_dir:
            state = merge_source(state, read_source(args.source_dir))
        elif not args.offline:
            source_config = json.loads((ROOT / 'config/source.json').read_text(encoding='utf-8'))
            state = apply_snapshot(state, fetch_source(source_config, args.provinces))
        if args.drop_retained and (args.offline or args.source_dir):
            state['providers'] = {name: data for name, data in state['providers'].items() if not data['retained']}
        template_path = ROOT / 'docs/README.template.md'
        template = template_path.read_text(encoding='utf-8') if template_path.exists() else DEFAULT_README
        manifest = generate(state, config, args.output.resolve(), template,
                            repository_name(args.repository, ROOT), branch, project['epg_url'])
        checked = validate_outputs(args.output.resolve(), config, project['epg_url'])
        print(json.dumps(dict(checked, current=manifest['current_provider_count'],
                              retained=manifest['retained_provider_count'], output=str(args.output.resolve())), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'更新失败，保留旧列表：{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

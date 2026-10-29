# IPTV M3U

当前输出 **4 个省份、6 份 M3U、1,041 条频道线路**。其中 6 份来自最近成功采集或导入，0 份保留历史来源。每个省份与运营商的组合只有一份文件。

本轮采集范围：北京、浙江、湖南、河北；只代表配置的分页范围。

仅采集酒店和组播源；每个省份与运营商组合只输出一个 M3U。
同一频道超高清与其他画质各保留一条，各档先按更新时间从新到旧选择，更新时间相同优先组播，再比较清晰度。
分组按 超清体验、央视频道、卫视频道、体育频道、港澳台、少儿频道、影视频道、纪实频道、新闻频道等顺序规范。
频道分类见 [config/groups.json](config/groups.json)，源站设置见 [config/source.json](config/source.json)，
仓库、分支与 EPG 设置见 [config/project.json](config/project.json)。

## 订阅列表

| 省份／运营商 | M3U | 频道线路 | 数据快照（北京时间） | 来源状态 |
|---|---|---:|---|---|
| 北京移动 | [M3U](https://raw.githubusercontent.com/wangxujin/iptv-m3u/refs/heads/main/m3u/%E5%8C%97%E4%BA%AC%E7%A7%BB%E5%8A%A8.m3u) | 140 | 2026-10-30 04:47 | 源站直接采集 |
| 北京联通 | [M3U](https://raw.githubusercontent.com/wangxujin/iptv-m3u/refs/heads/main/m3u/%E5%8C%97%E4%BA%AC%E8%81%94%E9%80%9A.m3u) | 135 | 2026-10-30 04:47 | 源站直接采集 |
| 河北电信 | [M3U](https://raw.githubusercontent.com/wangxujin/iptv-m3u/refs/heads/main/m3u/%E6%B2%B3%E5%8C%97%E7%94%B5%E4%BF%A1.m3u) | 290 | 2026-10-30 04:47 | 源站直接采集 |
| 河北联通 | [M3U](https://raw.githubusercontent.com/wangxujin/iptv-m3u/refs/heads/main/m3u/%E6%B2%B3%E5%8C%97%E8%81%94%E9%80%9A.m3u) | 294 | 2026-10-30 04:47 | 源站直接采集 |
| 浙江电信 | [M3U](https://raw.githubusercontent.com/wangxujin/iptv-m3u/refs/heads/main/m3u/%E6%B5%99%E6%B1%9F%E7%94%B5%E4%BF%A1.m3u) | 37 | 2026-10-30 04:47 | 源站直接采集 |
| 湖南电信 | [M3U](https://raw.githubusercontent.com/wangxujin/iptv-m3u/refs/heads/main/m3u/%E6%B9%96%E5%8D%97%E7%94%B5%E4%BF%A1.m3u) | 145 | 2026-10-30 04:47 | 源站直接采集 |

## 第 8 页仍无可用来源

本轮没有“已采到第 8 页仍无可用来源”的记录。

## 来源异常待核查

以下组合因单台来源异常未取得有效新结果；已有旧文件保留，不按无来源删除。

| 省份／运营商 | 处理 | 原因 |
|---|---|---|
| 浙江联通 | 无旧文件 | 详情运营商无法识别 |

来源时间不代表播放可用性已验证；历史来源表示保留的缓存。

## 本地使用

需要 Python 3.10+。在线采集依赖 Requests，Actions 会自动安装。

```sh
python3 -m pip install requests==2.34.2
python3 rtp/b.py           # 按 config/source.json 的 provinces 采集并生成
python3 rtp/b.py --provinces 北京 河北 # 临时覆盖配置的省份
python3 rtp/b.py --offline # 使用已有缓存重建
python3 rtp/b.py --check   # 校验生成结果
python3 epg/update.py     # 按 1 → 3 → 2 合并 EPG
python3 epg/update.py --check # 校验 epg.xml.gz
```

频道列表计划在北京时间每两小时整点更新（00:00、02:00……22:00）；EPG 仅在 00:00、12:00 的定时任务中更新。
EPG 按计划时段判断，不受实际启动时间延迟影响；同轮需更新 EPG 时先独立发布，再采集频道。
手动触发或推送触发工作流时，两项都更新。
GitHub schedule 显式设置 timezone: Asia/Shanghai，计划时间不等于实际启动时间，平台可能延迟。
严格控制执行时间需使用自行管理的定时器及执行环境，单改 cron 或时区不能保证准点。
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
config/source.json 的 provinces 自定义采集范围，当前配置为北京、浙江、湖南、河北。
--provinces 优先覆盖配置；省略 provinces 字段或设为 [] 时采集全部省份。指定范围外的旧文件保留。
按省份采集，每页 10 条，先采前 5 页；没有有效频道才延伸至第 6～8 页，找到即停。
每省份＋运营商仅取一台成功来源，先组播后酒店，同类型按更新时间选择；成功后跳过后续同组服务器。
默认两个省份并行，复用各线程连接，全局请求间隔 5 秒；429 全局冷却至少 60 秒。
429 降速后，连续 20 个成功且稳定至少 120 秒，才逐级恢复到配置间隔；失败和空 M3U 重新计数。
恢复不缩短已安排的请求间隔及冷却，每次恢复后重新累计成功次数和稳定时间。
下载为空或格式异常时冷却 30 秒，使用网页公开 M3U 接口复核一次；仍无频道记为异常并保护旧文件。
日志记录来源编号、响应字节数及解析数量，不记录原始令牌或 Cookie。
成功和失败轮次均输出 crawler 统计：分阶段请求耗时、429、双接口结果、按原因累计的线程等待时间。
线程等待和请求耗时可能并行重叠，不可相加作为整轮耗时；整轮耗时见 elapsed_seconds。
可用 --fresh 从空数据开始验证指定省份。

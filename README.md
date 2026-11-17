# astrbot_plugin_game_activity

游戏活动日历插件：按游戏持久化活动快照，同步 6 个第三方活动源，跨源合并去重，提供 `/活动` 指令、Dashboard 视图与活动结束前提醒。

- 已验证的 AstrBot 稳定版：**4.29.4**（`astrbot_version: ">=4.29.4,<4.30"`）。其他版本未验证，不宣称兼容。
- 零第三方运行时依赖：仅使用标准库与 AstrBot 自带 `aiohttp`，因此**没有** `requirements.txt`。

## 功能

- **6 个内置源**：原神、崩坏：星穹铁道、绝区零、明日方舟、明日方舟：终末地、鸣潮（数据来自 `starrailassistant.top`，详见〈数据来源与版权〉）。
- **增量同步**：ETag / If-Modified-Since 条件请求，304 跳过解析；无校验头时退化为全量拉取并由内容哈希判定变更。
- **稳定身份**：上游无稳定 id，插件以规范化名称生成 `a`+16 位十六进制 `raw_id`，落盘后永不重算；改名通过相似度识别并保留原 id。
- **跨源合并**：同游戏内相似名称 + 开始时间相差 ≤24h 的活动合并展示，按优先级选择主源。
- **下架追踪**：活动消失后保留 tombstone（默认 7 天），重新出现自动复活。
- **结束前提醒**：按 `remind_days_before` 与 `remind_time` 生成提醒任务，主动推送到订阅会话；支持补发窗口、失败退避与逐目标状态机。所有目标统一经 `Context.send_message` 投递，不按平台前缀跳过（含 `qq_official` / `qq_official_webhook`，详见〈提醒投递与 QQ 官方机器人〉）。
- **Dashboard 视图**：总览、活动、数据源、提醒四个分区（`views/activity`）。
- **7 条 Web API**：`health` / `overview` / `games` / `activities` / `sources` / `sync` / `reminders`，均需 Dashboard 登录态。

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/活动` | 所有人 | 查看指令帮助 |
| `/活动列表 <游戏>` | 所有人 | 按开始时间列出进行中/即将开始/即将结束的活动（游戏可选） |
| `/活动今日 <游戏>` | 所有人 | 今日进行中与 24 小时内开始的活动（游戏可选） |
| `/活动详情 <名称或ID>` | 所有人 | 名称模糊匹配（多条时列出候选） |
| `/活动状态` | 所有人 | 同步状态、最后同步时间、各源健康与错误 |
| `/活动同步 <游戏>` | 管理员 | 后台单飞同步；30 秒内重复触发会被限流（游戏可选） |
| `/活动订阅 <游戏>` | 所有人 | 订阅/查看当前会话的结束提醒（游戏可选） |
| `/活动取消订阅 <游戏>` | 所有人 | 取消当前会话订阅（游戏可选） |

英文别名：`activity`、`activity_list`、`activity_today`、`activity_detail`、`activity_status`、`activity_sync`、`activity_subscribe`、`activity_unsubscribe`。游戏参数支持 game_id 或中文名，省略时覆盖全部游戏。

指令名为单 token（不含空格），适配 WebUI 指令管理；旧 `/活动 列表` 语法已弃用，会回退到帮助。

## 配置

配置项位于 WebUI 插件配置页（`_conf_schema.json`），分组如下：

| 分组 | 关键项 | 默认 |
| --- | --- | --- |
| `general` | `enabled`、`sync_interval_minutes`、`request_timeout_seconds`、`max_response_kb`、`use_t2i` | 启用、30 分钟、20 秒、4096 KB、关闭 |
| `sources` | `template_list`：内置源预设或自定义 JSON 源 | 留空 = 启用 6 个内置源 |
| `reminder` | `enabled`、`remind_days_before`、`remind_time`、`max_grace_hours`、`target_umos`、`max_sends_per_tick` | 启用、`3,1`、`10:00`、48 小时、空、5 |
| `view` | `default_game`、`poll_seconds`、`show_covers`、`show_timezone` | 空、30 秒、开启、关闭 |
| `advanced` | `retention_days`、`allow_insecure_http`、`http_user_agent` | 7 天、关闭、插件 UA |

- 自定义 JSON 源使用点号路径映射（`map_activities` / `map_name` / `map_start` / `map_end` 必填），不支持数组下标。
- 非法配置只会禁用对应源并记录原因，不会导致插件崩溃。
- 视图时间默认只显示本地时间、不附带时区（如 `GMT+8`）；`view.show_timezone=true` 后，最后同步、统计更新时间、活动起止、数据源同步时间、提醒触发/发送时间与缓存时间文案会统一附带时区。
- 配置更新后需重载插件生效（含 `reminder.target_umos`；重载后新增目标会在下一轮自动追加到已有作业，已发送的目标不会重发）。`general.enabled=false` 可关闭自动同步，仅用手动同步。

## 数据目录

运行数据位于 `AstrBot/data/plugin_data/astrbot_plugin_game_activity/`：

```text
snapshots/<game_id>.json    # 每游戏一个活动快照
state/http_cache.json       # 每源 ETag/Last-Modified/状态
state/reminders.json        # 提醒任务与逐目标状态
state/subscriptions.json    # 会话订阅
```

所有写入使用临时文件 + `os.replace` 原子落盘；损坏的 JSON 会被重命名为 `*.corrupt-<epoch>` 并告警。插件目录内不写入业务数据。

## Dashboard 视图

插件详情页 →「游戏活动同步」：总览（同步状态、源健康、活动计数、下一提醒）、活动列表与筛选、数据源诊断与重试、提醒任务。视图运行在受限 iframe 中，仅通过 `window.AstrBotPluginView` bridge 访问上述 7 条 API。时间默认不显示时区，在插件配置 `view.show_timezone`（默认关闭）打开后统一显示。提醒状态 `sent` 展示为「已提交」，表示已交给平台适配器而非送达；晚于计划时间 1 分钟以上的发送会标「补发」，任务详情需点击活动名旁的「详情」展开。

## 开发与验证

在 AstrBot 仓库根目录使用其虚拟环境运行测试；测试不联网，也不写入插件目录：

```bash
# 在插件目录下（Windows 下 ruff 缓存目录需可写）
ruff format --no-cache .
ruff check --no-cache .

# 在 AstrBot 仓库根目录运行（使用 AstrBot 的 venv）
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/Scripts/python.exe -m pytest data/plugins/astrbot_plugin_game_activity/tests -q -p no:cacheprovider
```

说明：

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 用于避免 AstrBot 环境中的 pytest 插件自动加载；本插件测试全部为同步测试，不需要 `pytest-asyncio`。
- 测试临时目录使用工作区根目录下的 `.dsh-test-tmp/`（`PLUGIN_TEST_TMP` 可覆盖），避免 Windows 受限环境对系统临时目录的写入限制。
- `ruff` 使用与 AstrBot 仓库一致的版本（0.15.22）、默认 88 列宽。

## 提醒投递与 QQ 官方机器人

- **不再按平台前缀跳过**：所有提醒目标（含 `qq_official` / `qq_official_webhook` 的私聊与群聊）统一交给 `Context.send_message`，发送失败由既有失败状态机（`failed` / 退避重试）处理。旧实现依据 [CORE-103]「不得对 `qq_official` 主动发送」直接跳过，但 AstrBot 4.29.4 的 `qq_official` / `qq_official_webhook` 适配器已声明 `support_proactive_message=True` 并实现 `send_by_session` 主动发送（`context.py` 的旧 docstring 已过时）；此处以 AstrBot 源码为准。
- **冷群限制**：QQ 官方机器人的群主动消息依赖 AstrBot **进程内**群场景缓存（`_session_scene` / `_session_last_message_id`），仅当适配器**收到**过该群消息后才写入，且每次 AstrBot 重启后清空。冷群发送时核心只打印 `[QQOfficial] No cached msg_id for session: …` 并静默跳过，而 `Context.send_message` 仍返回 `True`；因此插件的 `sent` 只表示「已交给平台适配器」，**不是平台投递回执**。规避方式：AstrBot 启动后先在该目标群发任意消息，或让群成员执行一次 `/活动订阅` 预热，再等待提醒。插件会在每次向 QQ 官方群目标发送前打印一条对应 warning（不含消息正文）。
- **固定 `target_umos` 与群内 `/活动订阅` 的取舍**：`target_umos` 适合管理员指定、跨重启稳定的推送目标，但修改配置后需重载插件；`/活动订阅` 由群内成员自助登记、无需改配置，但同样受上述冷群缓存限制。两者最终走同一条投递路径。
- **状态迁移**：历史状态中因旧前缀规则产生的 `skipped` / `platform_unsupported` 目标，会在升级后首次加载时一次性重置为 `pending` 并重试（提醒状态 schema v1 → v2）；`grace_expired`、`activity_ended` 等其它 skipped 目标保持不变，不会被重置。

## 兼容性与已知限制

- 仅在 AstrBot **4.29.4** 稳定版上开发与验证；`astrbot_version` 只声明 `>=4.29.4,<4.30`。
- `qq_official` / `qq_official_webhook` 的真实群投递未在本仓库实机验证：测试只覆盖插件侧调用与状态机；冷群可能被 AstrBot 核心静默跳过，`sent` 不是平台投递回执（见〈提醒投递与 QQ 官方机器人〉）。
- Windows 若缺少 IANA 时区数据库（`tzdata` 未安装），`Asia/Shanghai` 系列回退为固定 `+08:00`；其他时区会使对应源禁用并记录 `unknown_timezone`。
- 默认配置 `admins_id: ["astrbot"]` 使 WebChat 的 Dashboard 用户成为管理员，因此 `/活动同步` 的**管理员正向路径**已在实机验证；非管理员拒绝路径未单独实测（权限过滤器已按 `PermissionType.ADMIN` 注册）。
- 视图 `app.js` 约 84 KB，略高于设计建议的 80 KB 软预算（功能完整且无构建步骤）；总资源仍低于 150 KB。
- 上游 `starrailassistant.top` 的 ETag/Last-Modified 是否始终存在取决于对方服务；插件已实现无校验头的全量降级路径。
- 上游返回空活动列表时（且本地快照非空）不会清空快照，而是标记源为 `stale` 并保留旧数据。

## 安全

- 所有网络请求使用 `aiohttp`（异步），不使用 `requests`/`urllib`。
- 源 URL 仅允许 `https`（`allow_insecure_http=true` 时才允许 `http`），禁止用户信息、fragment 与非 80/443 端口；DNS 解析结果经私有/回环/保留地址校验后固定连接，禁止重定向。
- 响应体有大小上限并流式读取，严格 UTF-8 解码；错误只记录摘要，不记录响应体全文。
- 视图仅使用 `textContent` 渲染上游文本，无 `innerHTML`、无原生 `EventSource`、无 localStorage/cookie；封面仅允许 http(s) URL。
- 主动消息不按平台前缀跳过：`qq_official` / `qq_official_webhook` 目标同样交给 `Context.send_message`，其群聊冷启动限制见〈提醒投递与 QQ 官方机器人〉。

## 数据来源与版权

活动数据来自以下第三方接口（2026-11-16 采样）：

- 原神 `https://starrailassistant.top/api/v1/activity/ys.json`
- 崩坏：星穹铁道 `https://starrailassistant.top/api/v1/activity/sr.json`
- 绝区零 `https://starrailassistant.top/api/v1/activity/zzz.json`
- 明日方舟 `https://starrailassistant.top/api/v1/activity/ak.json`
- 明日方舟：终末地 `https://starrailassistant.top/api/v1/activity/end.json`
- 鸣潮 `https://starrailassistant.top/api/v1/activity/ww.json`

> 数据版权归各游戏与数据提供方所有；本插件仅做时间线整理与提醒，与上述站点无隶属或授权关系。请遵守数据提供方的使用条款；如需商用请自行确认授权。

## 来源与许可

- 本仓库初始模板来自 AstrBot 官方插件模板（`Soulter/helloworld`），保留其 `LICENSE`（GNU AGPL-3.0）。
- 插件业务代码为本项目原创，未复制 AstrBot 源码或其他插件代码。
- 设计依据：`docs/game-activity-sync/plugin-design.md`、`docs/game-activity-sync/view-ui-design.md`、`docs/astrbot-plugin-dev-spec.md`。

## 免责声明

本插件按“现状”提供，不保证第三方数据源的可用性、准确性或时效性；因数据源变更、平台限流或配置错误造成的提醒缺失，请以游戏内公告为准。

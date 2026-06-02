# PolyCrawl 实现文档

最后更新：2026-05-30

## 1. 当前已实现内容

### 1.1 项目结构与基础文件

已创建目录与基础工程文件：

- `apps/api`
- `services/worker`
- `packages/core/config`
- `packages/core/providers`
- `packages/providers/douyin`
- `deploy`
- `config/sites`

已创建关键文件：

- `pyproject.toml`
- `README.md`
- `.gitignore`
- `deploy/docker-compose.yml`
- `config/base.jsonc`
- `config/creators.jsonc`
- `config/sites/douyin.jsonc`

### 1.2 配置系统（文件驱动）

已实现 JSONC 配置解析与统一加载：

- `packages/core/config/jsonc.py`
- `packages/core/config/models.py`
- `packages/core/config/loader.py`

能力包括：

- 读取 `config/base.jsonc`、`config/creators.jsonc`、`config/sites/*.jsonc`
- JSONC 注释剥离后解析
- Pydantic 模型校验
- 站点配置批量加载
- creators 文件原子写回（临时文件替换）
**JSONC 写回机制（`update_jsonc_key`）**：

所有配置落盘操作均使用 `update_jsonc_key(file_path, key, new_value)` 函数，实现原子替换且完整保留注释：

1. `_find_jsonc_value_span()` 通过正则定位顶层 key，按首字符（`{`/`[`/`"`/数字）走不同的深度追踪逻辑找到完整值的 `[start, end)` span（处理嵌套括号、字符串转义）。
2. 用 `json.dumps(new_value, ensure_ascii=False, indent=2)` 序列化新値。
3. `dump_json()` 将 `UrlEntry`（`{"url": ..., "enabled": ...}`）压缩为单行，避免展开成多行。
4. 文本替换：`raw[:start] + '"key": new_json' + raw[end:]`。
5. **原子写入**：写入 `.jsonc.tmp` 临时文件→ `tmp.replace(file_path)`（操作系统级原子换名）。
6. 所有 `//` 和 `/* */` 注释在替换目标 key 外完全保留。
### 1.3 creator_key 规则（按最新确认）

已严格按你确认的规则实现：

- 仅当 `creator_key` 缺失时才自动生成随机值
- 若配置中出现重复 `creator_key`，立即报错并中止加载（fail fast）
- 自动生成格式：`creator_<8位小写字母数字>`
- 自动生成后会写回 `config/creators.jsonc`

对应实现文件：

- `packages/core/config/creator_keys.py`
- `packages/core/config/loader.py`

### 1.4 API 服务（已全量实现）

实现文件：

- `apps/api/main.py`
- `apps/api/schemas.py`

**完整 API 端点列表：**

```
# 基础
GET    /health
GET    /platforms                               # 已实现 Provider 列表

# Creator 管理
GET    /creators
POST   /creators
PATCH  /creators/{creator_key}/tags             # 更新标签，写回 creators.jsonc
POST   /creators/{creator_key}/links            # 添加账号链接，写回 creators.jsonc + DB 同步
DELETE /creators/{creator_key}/links?account_url=...
PUT    /creators/reorder                        # 重排 creators.jsonc 数组顺序
GET    /creators/summary                        # 含 files_count, works_count, total_bytes

# 配置
GET    /config/effective

# 账号
GET    /accounts
PATCH  /accounts/{account_id}/scheduled         # 启用/禁用账号调度，同步写回 creators.jsonc + DB

# 任务
GET    /tasks
POST   /tasks
POST   /tasks/{task_id}/retry

# 直播
GET    /live/status
POST   /live/records/{account_id}/stop

# 调度配置
GET    /schedules
PUT    /schedules                               # 写回 base.jsonc schedules[]

# 日志
GET    /logs?limit=&level=
DELETE /logs

# Cookie 管理
POST   /login/cookies                           # 写入 platform cookies + 清 Redis 验证态
GET    /login/status                            # Cookie 状态 + Redis 验证结果
POST   /login/verify                            # 主动测试 cookies

# WebSocket
WS     /ws/logs
WS     /ws/events

# 静态资源
GET    /                                        # 服务 index.html
GET    /assets/*
```

说明：

- `POST /creators` 请求体仅需 `display_name`
- 不允许前端提交 creator_key 作为最终值
- 写回 `creators.jsonc` 使用 `update_jsonc_key` 原子替换，保留注释
- `POST /tasks` 已实现：`tasks` 入库、推入 Redis List、首条 `task_runs` 记录创建（queued）
- `GET /tasks` 返回的 `TaskResponse` 包含 `error_message` 字段，前端支持展开展示

### 1.5 Worker 架构（自研 asyncio 实现）

Worker 采用自研的 asyncio 单进程双协程模型：

**入口**：`services/worker/run.py`，`asyncio.run(main())` 同时启动 Scheduler + Consumer。

**核心文件**：

- `services/worker/scheduler.py`：事件驱动 + 定时器驱动的双轨调度器（详见 §1.14）
- `services/worker/consumer.py`：Redis BLPOP 消费器（详见下方）
- `services/worker/runtime.py`：共享的 session_factory、Redis 连接初始化
- `services/worker/executors/content_executor.py`：内容抓取执行器
- `services/worker/executors/live_executor.py`：直播录制执行器
- `services/worker/executors/deep_executor.py`：深度扫描执行器

**Consumer 工作原理**：

```python
# 任务队列命同规则：task_{idx} 对应 base.jsonc schedules[idx]
# BLPOP 同时监听所有队列，超时 2s、再循环
keys = [f"task_{i}" for i in range(len(schedules))]
list_key, raw = await redis.blpop(keys, timeout=2)
task_data = json.loads(raw)
# 根据 task_type 路由到对应执行器
if task_data["task_type"] == "content_fetch":
    await content_executor.run(task_data)
elif task_data["task_type"] == "live_record":
    await live_executor.run(task_data)
    notify_live_done(task_data["account_id"])  # 通知 Scheduler 重新分配 tier
```

**任务执行状态回写**：

- 消费开始：`tasks.status = running`，`task_runs` 新边 running 行
- 成功完成：`tasks.status = success`，`task_runs.status = success`，写入 items_fetched/downloaded/bytes
- 失败：`tasks.status = failed`，`tasks.error_message` 写入错误信息，`task_runs.status = failed`

**Provider 注册与加载**：

- `packages/core/providers/base.py`：`BaseProvider` 抽象基类
- `packages/core/providers/registry.py`：`ProviderRegistry` 惰性加载与缓存
- `packages/providers/douyin/__init__.py`：完整实现（真实 API 调用 + 签名）
- `packages/providers/weibo/__init__.py`：完整实现（HTML 抓取）
- `packages/providers/xiaohongshu/__init__.py`：完整实现（自研签名 + API）
- `BaseProvider` 统一接口：包含 `platform`、`account_types`（声明支持的账号类型）和抽象方法组
- `GET /platforms` API：自动发现 provider 包，返回平台名及 account_types，前端动态渲染

### 1.6 部署骨架

已提供本地 compose 启动模板：

- `deploy/docker-compose.yml`

当前服务：

- `api`
- `content-worker`
- `live-worker`
- `postgres`
- `redis`

### 1.7 数据库层与迁移基础（已完成首版）

已完成 SQLAlchemy 与 Alembic 基础搭建：

- `packages/core/db/base.py`
- `packages/core/db/session.py`
- `packages/core/db/models.py`
- `alembic.ini`
- `alembic/env.py`
- `alembic/versions/0001_initial_schema.py`

首版迁移已覆盖 8 张核心表：

- creators
- accounts
- tasks
- task_runs
- artifacts
- live_statuses
- live_sessions
- config_versions

#### 1.7.1 数据库结构实现明细（用于审阅）

迁移版本：

- `alembic/versions/0001_initial_schema.py`

已落地的约束与外键行为：

1. creators
  - `creator_key` 唯一

2. accounts
  - FK: `creator_id -> creators.id`（on delete cascade）
  - Unique: `(platform, platform_account_id)`

3. tasks
  - FK: `account_id -> accounts.id`（on delete set null）
  - `params` 使用 JSONB

4. task_runs
  - FK: `task_id -> tasks.id`（on delete cascade）
  - Unique: `(task_id, run_number)`
  - `error_detail` 使用 JSONB

5. artifacts
  - FK: `account_id -> accounts.id`（on delete set null）
  - FK: `task_id -> tasks.id`（on delete set null）
  - Unique: `(account_id, platform, content_id, media_kind)`
  - 现行跳过规则：`status=completed` 且 `file_size>0`

6. live_statuses
  - FK: `account_id -> accounts.id`（on delete cascade）
  - Unique: `account_id`（每账号单行快照）

7. live_sessions
  - FK: `account_id -> accounts.id`（on delete set null）

8. config_versions
  - `config_content` 使用 JSONB

已落地索引（摘要）：

- creators: `creator_key`
- accounts: `creator_id`, `platform+account_type`, `account_url`, `platform+platform_account_id`
- tasks: `account_id`, `status`, `task_type`, `created_at desc`, `queue_key`
- task_runs: `task_id`, `status`, `completed_at desc`
- artifacts: `account_id`, `platform+content_id`, `download_date desc`, `status`, `file_path`
- live_statuses: `account_id`, `status`
- live_sessions: `account_id`, `started_at desc`
- config_versions: `version_number desc`, `changed_at desc`

当前未下沉到数据库的约束（应用层控制）：

- 各表 `status` 值枚举约束（当前由服务层写入逻辑保证）
- JSONB 字段的结构约束（当前未加 schema 级校验）

上线前审阅建议：

1. 是否为 status 字段补 CHECK 约束。
2. 是否为 JSONB 热查询路径补 GIN 索引。
3. 是否在 V1 即启用 `config_versions` 的完整审计写入。
4. 是否补充数据库备份恢复演练（pg_dump/pg_restore）。

### 1.8 配置到数据库同步（已实现首版）

已实现 `creators.jsonc -> creators/accounts` 的幂等同步服务：

- `packages/core/sync/config_sync.py`

当前行为：

- 按 `creator_key` upsert `creators`
- 按 `(creator_id, platform, account_type, account_url)` upsert `accounts`
- 删除配置中已不存在的 creator/account（保持数据库与配置一致）
- API 启动时自动执行一次同步
- 新增 creator 成功写配置后立即触发同步

### 1.9 任务与直播接口进展（本轮完成）

- `/tasks`：真实入库、入队、首条 run 记录创建
- worker：执行时回写任务状态 `running -> success/failed`
- `content_fetch`：已接入首版真实执行器，支持 `artifacts` 去重写入与下载统计回填
- `/live/status`：从 `live_statuses` 表读取当前快照
- `/live/records/{account_id}/stop`：对活跃 `live_record` 任务执行幂等取消，并同步写回任务状态

### 1.10 运行脚本与基础测试（本轮完成）

新增脚本：

- `scripts/migrate.ps1`
- `scripts/run-api.ps1`
- `scripts/run-worker-content.ps1`
- `scripts/run-worker-live.ps1`

新增测试：

- `tests/config/test_creator_key_policy.py`
   - 用例1：仅在缺失时生成 `creator_key`
   - 用例2：重复 `creator_key` 立即报错

测试状态：

- `tests/config/test_creator_key_policy.py` 已执行通过（2 passed）

### 1.11 任务重试机制（本轮完成）

- 新增接口：`POST /tasks/{task_id}/retry`
- 行为：
   - 检查 `max_retries` 上限
   - `retry_count` 递增
   - 新增一条 `task_runs` 记录，`run_number` 自动递增
   - LPUSH 入 Redis List `task_{idx}:{platform}`
   - 主任务状态回到 `queued`

### 1.12 直播状态机执行器（本轮完成）

新增：

- `services/worker/executors/live_executor.py`

能力：

- `live_monitor` 执行时写入 `probing -> offline/recording` 状态流转
- `live_record` 执行时写入 `recording -> offline`，并创建 `live_sessions` 记录
- 异常时写入 `error` 状态

测试：

- `tests/worker/test_live_executor.py` 已执行通过（2 passed）

### 1.13 Provider 可插拔执行层（本轮完成）

本轮将 content/live 执行器从“直接读 task.params”改为“通过 provider 协议调用”，实现平台逻辑解耦。

新增/更新：

- `packages/core/providers/base.py`
  - 新增 `fetch_content_items` / `detect_live_status` / `build_live_session_payload` 抽象方法
- `packages/providers/douyin/__init__.py`
  - 实现上述 provider 协议方法（当前为联调桩实现）
- `services/worker/executors/content_executor.py`
  - 改为通过 `ProviderRegistry().get(account.platform)` 获取 provider 并拉取内容项
- `services/worker/executors/live_executor.py`
  - 改为通过 provider 检测开播与构建录制会话参数
- `tests/providers/test_douyin_provider.py`
  - provider 方法单测

测试状态：

- `tests/providers/test_douyin_provider.py`、`tests/worker/test_live_executor.py`、`tests/config/test_creator_key_policy.py` 已执行通过（6 passed）

### 1.14 任务调度与事件驱动架构（已完整实现）

实现文件：`services/worker/scheduler.py`

#### 整体架构

```
Worker 进程 (services/worker/run.py asyncio main())
├── Scheduler.start()                ← 调度器（事件驱动 + 定时器）
│   ├── _register_all()              ← content_fetch 定时器注册
│   ├── _init_live_tiers()           ← live tier 分级初始化
│   ├── _handle_live_events()        ← 事件循环（阶塞在 Queue.get()）
│   └── _handle_config_events()      ← 配置热加载监听
│
└── Consumer.start()                 ← 队列消费器（BLPOP 循环）
    ├── content_executor             ← 内容抓取执行器
    └── live_executor                ← 直播录制执行器
```

Scheduler 与 Consumer 通过两个解耦通道交互：
1. **Redis List `task_{idx}`**：Scheduler 推任务 → Consumer 消费（单向）
2. **`_live_event_queue` asyncio.Queue**：Consumer 完成后通知 Scheduler（反向事件回调）

#### Content Fetch — 定时器驱动

```python
# _register_all() 逐一处理 base.jsonc schedules[]
# 对每个 enabled 的非 live_record 条目：
def _schedule_next(idx, entry, state):
    interval_secs = _parse_interval(entry.interval)
    h = loop.call_later(
        interval_secs,
        lambda: asyncio.create_task(_dispatch(idx, entry, state))
    )
    self._timers[idx] = h
    # _dispatch() 完成后再次调用 _schedule_next()，形成自持续定时器链
```

`_dispatch()` 查询 `scheduled=True` 的 accounts，对每个 account 创建 Task 记录并 LPUSH 入 `task_{idx}`。

#### Live Record — Tier-driven 事件架构

**核心思路**：以账号维度的“闲置时长”分级，闲置越久检测间隔越长。无轮询，无 pub/sub，所有行为由事件队列驱动。

**Tier 匹配逻辑**：

```python
# base.jsonc > strategy.live.adaptive.tiers[]
# 按 after 升序匹配，匹配最高满足 after <= idle_seconds 的 tier
def _tier_for_idle(idle_seconds, adapt_cfg) -> str:
    best_tier = adapt_cfg.tiers[0].interval
    for t in adapt_cfg.tiers:
        if idle_seconds >= parse_duration_to_seconds(t.after):
            best_tier = t.interval
    return best_tier
```

**初始化流程**（启动或配置热加载时）：

```
_init_live_tiers(state):
  1. DB 查询所有 account_type == "live" 的账号 ID
  2. Redis MGET polycrawl:live:last_live:{aid} 批量读取最后直播时间戳
  3. 对于 Redis miss 的账号，fallback DB live_sessions 表最近一条已完成会话
  4. 按 idle_seconds = now - last_live_ts 调用 _tier_for_idle() 分级
  5. _start_live_timer(aid, first_delay):
     - 新账号（无历史）：first_delay = 0，立即检测
     - 有历史：first_delay = tier_interval
     loop.call_later(delay, lambda: _live_event_queue.put_nowait(("timer", aid)))
```

**事件循环** (`_handle_live_events()`):

```
永久阶塞于 _live_event_queue.get()
  批量清空积压事件

  ("timer", aid):
    if aid not in _live_recording:
      DB 创建 Task + LPUSH task_{live_idx}  ← 派发 live_record
      _live_recording.add(aid)              ← 防重复

  ("done", aid):
    _live_recording.discard(aid)
    Redis 读取 polycrawl:live:last_live:{aid} → 重新分级
    _start_live_timer(aid, new_tier_interval)  ← 重启定时器
```

**录制完成通知链**：

```python
# Consumer._execute() 完成 live_record 任务后：
notify_live_done(account_id)
  # 即 _live_event_queue.put_nowait(("done", account_id))
```

#### 配置热加载的调度器重建

配置文件变更 → `_reload()` 完整重建调度器：

```
1. 取消所有 content_fetch timer
2. 取消所有 live 账号 timer
3. 清空 _live_tier_groups、_live_recording
4. 重新加载配置 + DB 同步
5. _register_all(new_state)、_cache_live_config()、_init_live_tiers()
```

#### 9p 文件系统兼容

- `_fs_supports_inotify()` 读取 `/proc/mounts`，检测 config 目录是否挂载在 9p 文件系统上。
- 9p = Docker Desktop for Windows/Mac，不支持 inotify。
- 9p 环境自动切换为 5 秒轮询检测，生产璯境（NAS Linux）使用 inotify 事件驱动。

### 1.15 数据库联调测试骨架（本轮完成）

新增：

- `tests/integration/test_content_pipeline_integration.py`

能力：

- 提供 content pipeline 的端到端数据库联调测试（创建表 -> seed -> 执行 `content_fetch` -> 校验 task/task_run/artifacts）
- 使用 `POLYCRAWL_TEST_DATABASE_URL` 作为测试数据库连接
- 未设置该环境变量时自动 skip，不影响日常单测

测试状态：

- `tests/integration/test_content_pipeline_integration.py` 在当前环境自动 skip（1 skipped）

### 1.15 live_record stop/recover_window 行为细化（本轮完成）

新增行为：

- provider payload 支持：
  - `stop_requested`
  - `simulate_disconnect`
  - `recover_window_seconds`
  - `fast_reconnect_seconds`
- `live_record` 执行时：
  - `stop_requested=true` -> `live_sessions.status=interrupted`，并记录中断原因
  - `simulate_disconnect=true` -> 在 `recover_window_seconds` 内按 `fast_reconnect_seconds` 计算快速重连次数
  - 超出恢复窗口且无有效重连 -> 抛错并走任务失败 + `live_status=error`
- 任务返回中新增 `reconnect_attempts`

测试状态：

- `tests/providers/test_douyin_provider.py`、`tests/worker/test_live_executor.py`、`tests/config/test_creator_key_policy.py` 已执行通过（6 passed）

### 1.16 配置热加载监听（本轮完成）

新增：

- `apps/api/main.py` 启动时创建后台 watcher 任务（2 秒轮询）

能力：

- 监听 `base.jsonc`、`creators.jsonc`、`sites/*.jsonc` 的 mtime 变化
- 检测到变化后执行 `loader.load_all()` 校验与重载
- 校验成功时同步更新数据库中的 creators/accounts
- 校验失败时保持上一版状态（不覆盖、不崩溃）
- 服务 shutdown 时优雅取消 watcher 任务

验证状态：已验证

---

### 1.17 Douyin provider 真实分页抓取（本轮完成）

新增文件：

- `packages/providers/douyin/api_client.py`：异步 httpx 客户端
- `packages/providers/douyin/signing.py`：X-Bogus 纯 Python 签名模块
- `packages/providers/douyin/__init__.py`：改写为真实抓取路径

关键能力：

- `DouyinAPIClient`：async context manager，rate limit（`fetch_rps`），cookie auth，所有 GET 请求自动附加 `X-Bogus`
- `fetch_post_page(sec_uid, cursor)`：返回 `(items, next_cursor, has_more)`，调用 `/aweme/v1/web/aweme/post/`
- `check_live_status(web_rid)`：调用 `/webcast/room/web/enter/`，status=2 判为直播中
- `fetch_all_posts(sec_uid, cookies, ...)`: 分页遍历，自动 dedup（known_ids）、max_count 限制、look_ahead_pages 防止遇到已下载页面时过早停止
- `_normalise_item(raw)`: 标准化 aweme 字段 → `{content_id, media_kind, title, author, file_size, download_url, music_url, cover_url, create_time}`
- `compute_xbogus(payload)` / `sign_query(payload)`：从参考项目重构出的纯函数签名 API

配置读取：

- cookies 来自 `config/sites/douyin.jsonc` → `platform.cookies`
- rate_control.fetch_requests_per_second 驱动 DouyinAPIClient 速率限制
- 另外通过 provider 类级 `_rate_limit(tick)` 确保所有 API 调用遵循 `task.params["tick"]` 配置的最小间隔（sync 模式，`threading.Lock` + `time.sleep`）
- 未配置 cookies 时自动降级为 stub 模式（保证 dev/test 环境可跑通）

单测覆盖（共 14 passed）：

- `test_extract_sec_uid` — URL 解析
- `test_normalise_item_minimal` — 字段映射
- `test_douyin_provider_content_items_stub_when_no_cookies` — stub 降级
- `test_douyin_provider_content_items_non_profile_url` — 非 profile URL 返回空
- `test_douyin_provider_content_items_real_path` — mock fetch_all_posts 调用链路
- `test_douyin_provider_live_helpers` — 无 cookies 时 fallback 到 task_params
- `test_xbogus_length_and_chars` — 签名长度与字符集
- `test_sign_query_appends_xbogus` — query string 附加签名
- `test_xbogus_determinism_within_same_second` — 同秒内结果稳定
- `test_xbogus_differs_for_different_payloads` — 不同 payload 结果不同

验证状态：

- `tests/providers/test_douyin_provider.py`
- `tests/providers/test_signing.py`
- `tests/worker/test_live_executor.py`
- `tests/config/test_creator_key_policy.py`
- 本地真实 profile 抓取验证通过：配置中的 Douyin 账号可抓取到网页可见的 6 条视频，分页结果与网页一致
- 完整业务流（配置同步 -> 建任务 -> worker 执行 -> DB 落库 -> 去重二次执行）尚未完成本地验证：需要先启动 Docker daemon 后执行联调

### 1.18 asyncio Scheduler 定时调度（本轮完成）

**已完全弃用**：调度逻辑全部内化到 `services/worker/scheduler.py`。

修改文件清单：

- `services/worker/scheduler.py`：新建，全量调度逻辑（内容护取定时器 + 直播 tier 事件循环）
- `services/worker/run.py`：入口，`asyncio.run(main())` 同时启动 Scheduler + Consumer
- `packages/core/config/watcher.py`：提供 `watch_config_dir()`，返回事件队列
- `deploy/docker-compose.yml`：**删除** `beat` 服务，当前只有 `api` + `worker` + `postgres` + `redis`

**已废弃内容**：`dispatcher.py`、`--scan-on-start`、API 中的 `_schedule_loop()`、`_dispatch_schedule()`。

详细调度器实现请参考 §1.14。

修改文件：

- `packages/core/config/models.py`：新增 `ScheduleEntry`、`SchedulesConfig` 模型；`AccountConfig` 新增 `scheduled: bool = True`
- `packages/core/db/models.py`：`Account` 新增 `scheduled` 列
- `packages/core/sync/config_sync.py`：同步 `scheduled` 字段到 DB
- `config/base.jsonc`：新增 `schedules` 配置段（content_fetch / live_monitor 各自独立启用）
- `apps/api/schemas.py`：新增 `ScheduleEntryResponse`、`SchedulesResponse`、`ScheduleUpdateRequest`、`AccountScheduledRequest`
- `apps/web/index.html`：新增"调度"面板
- `apps/web/app.js`：新增调度页渲染逻辑与事件处理
- `apps/web/styles.css`：新增调度面板样式

关键能力：

- **Dispatcher**：beats 每 30s 触发 `dispatch_scheduled_tasks`，读取 `base.jsonc` 中 schedules 配置，按 interval 表达式触发，Redis SETNX 去重，遍历 `scheduled=True` 的 accounts 创建 Task 并入队
- **配置按类型独立**：`content_fetch` 和 `live_monitor` 各自独立 `enabled` 开关和 `interval` 表达式
- **Per-account 开关**：`accounts.scheduled` 字段控制是否参与自动调度，默认 `true`
- **Web 管理**：调度面板可查看/修改 schedules 配置（写回 base.jsonc），每个 account 可独立切换调度开关（写回 creators.jsonc + DB）
- **启动扫描**：设置 `POLYCRAWL_SCAN_ON_START=1` 环境变量，worker 就绪后立即触发一轮全量 dispatch

### 1.19 PostgreSQL 本地配置说明（本轮补充）

推荐方案：使用 Docker Compose 启动 PostgreSQL（无需本机单独安装 PostgreSQL）。

- `deploy/docker-compose.yml` 已新增 `postgres` 服务（`postgres:16-alpine`）
- 默认连接：`postgresql+asyncpg://polycrawl:password@postgres:5432/polycrawl_db`（容器内）
- 数据持久化卷：`pgdata`
- 运行时统一从 `config/base.jsonc` 读取 `storage.database_url` / `storage.redis_url`，不再依赖环境变量覆盖

启动步骤：

1. 启动 Docker Desktop（确保 daemon 处于 running）
2. 在项目根目录执行：`docker compose -f deploy/docker-compose.yml up -d postgres redis`
3. 执行迁移：`./scripts/migrate.ps1`
4. 启动全部服务：`docker compose -f deploy/docker-compose.yml up -d`

若你不使用 Docker，也可以本机安装 PostgreSQL，并将 `config/base.jsonc` 中 `storage.database_url` 改为本机地址（如 `localhost:5432`）。

### 1.19 历史下载去重迁移脚本（本轮完成）

新增：

- `tools/migration/import_legacy_douyin_artifacts.py`
- `packages/core/migration/legacy_douyin.py`
- `tests/migration/test_legacy_douyin_helpers.py`

目标：

- 将旧 Douyin 下载历史（SQLite + 磁盘文件）导入当前项目 `artifacts`，用于后续任务去重。
- 支持多个旧来源（本地 `Y:\douyin-downloader` 与 NAS `\\mxm43ds1821\docker\douyin-video-downloader`）。

导入规则：

- 旧库读取：`t_user_post(sec_uid, aweme_id, rawdata)`
- 账号映射：从当前库 `accounts` 中提取 `douyin/profile` 的 `sec_uid -> account_id`
- 内容 ID：`content_id = aweme_id`
- 媒体类型：按旧 `rawdata` 推断 `video/image`
- 文件定位：按旧命名规则 `create_time + desc(清洗)` 匹配 `downloads/*{sec_uid}*/post/*`
- 幂等写入：`ON CONFLICT DO NOTHING`（唯一键 `account_id + platform + content_id + media_kind`）

执行方式：

1. 先 dry-run（默认不落库）：
  - `python tools/migration/import_legacy_douyin_artifacts.py`
2. 确认统计后再 apply：
  - `python tools/migration/import_legacy_douyin_artifacts.py --apply`
3. 若只导入指定来源：
  - `python tools/migration/import_legacy_douyin_artifacts.py --legacy-source "Y:\douyin-downloader" --legacy-source "\\mxm43ds1821\docker\douyin-video-downloader" --apply`

注意：

- 脚本数据库连接来自 `config/base.jsonc`（不依赖环境变量覆盖）。
- 需要先完成一次 `creators.jsonc -> accounts` 同步，确保待迁移账号已存在于当前数据库。

### 1.20 当前项目继续联调结果（本轮完成）

本轮重点：不调试迁移工具，继续验证当前项目 API/Worker 主链路。

验证项与结果：

1. API 健康检查
   - `GET /health` 返回：`status=ok`、`config=ok`、`database=ok`
2. content_fetch 去重链路（同账号重复执行）
   - 第一次执行：`items_fetched=1, items_downloaded=1, items_skipped=0`
   - 第二次执行：`items_fetched=1, items_downloaded=0, items_skipped=1`
   - 结论：去重生效
3. retry 链路
   - 调用 `POST /tasks/{task_id}/retry` 后任务重新入队成功
   - `task_runs` 验证：`run_number` 从 1 递增到 2，均执行成功
4. live stop 链路
   - 创建 `live_record` 任务后调用 `POST /live/records/{account_id}/stop`
   - 返回 `stopped=true`，任务状态更新为 `canceled`

本轮排障记录（Windows 本地开发环境）：
## 2. 当前状态结论

当前代码已进入“**主链路全贯通、多平台可运行**”阶段。

**已完成**：

- API / DB / Worker 全链路打通（请求 → Redis 队列 → Consumer → 执行器 → DB 状态回写）
- Douyin / Weibo / Xiaohongshu Provider 完整实现
- 事件驱动调度器（content_fetch 定时 + live_record tier-driven）
- Cookie 管理系统（存储、验证、自动失效）
- WebSocket 实时推送（日志流 + 事件流）
- Vanilla JS Web 管理台（8 个 Tab）
- 配置热加载 + DB 同步 + 调度器重建
- 任务失败错误可视化（前后端均已实现）
- Worker Startup Recovery
- 内容去重链路（同账号重复执行跳过已下载）
- 历史下载迁移工具（legacy Douyin SQLite 导入）

**尚未完成**：

- content executor 实际文件下载落盘（当前仅写 artifacts 记录，未真实下载文件）
- deep_scan 执行器（`deep_executor.py` 已存在，实际调度集成未完成）
- UA 轮换、代理、Circuit Breaker
- `.tmp` 残留文件定期清理
- `config_versions` 表完整审计写入
- Twitter/X Provider
- 移动端应用

## 3. 下一步实现计划

### 3.1 内容下载落盘（首要任务）

目标：将 content executor 从“仅写 artifacts 记录”扩展为“实际下载文件 + 原子落盘”。

- 下载流写入 `<dest>.tmp`，同步计算 SHA-256
- 下载完成后 `tmp.rename(dest)`，正式文件名出现即代表完整
- 补充内容 DB content_id 去重查询（fetch 阶段已将 `artifacts` `content_id` 字段存在，尚未用于跳过）
- 启动时/任务前扫描并清理孤立 `.tmp` 文件

### 3.2 Deep Scan 执行器集成

`deep_executor.py` 已存在，需要接入 Scheduler 的 `content_fetch` 调度条目并实现 30s/作品节奔。

### 3.3 集成测试接入 CI 流程

将 `tests/integration/test_content_pipeline_integration.py` 接入本地 compose CI（自动起 PostgreSQL + Redis）。

### 3.4 安全加固（对外网版备用）

- 任务/直播接口 JWT 鲉权开关与审计日志埋点
- `.tmp` 残留文件定期清理任务

目标：把文档中 8 张核心表落地，形成稳定数据层。

已完成：

1. 引入 SQLAlchemy 2 + Alembic
2. 建立 8 张核心表的模型与初始迁移
3. 落地主要唯一约束与索引

待继续：

1. 增加 DB 初始化与迁移运行脚本
2. 补充任务链路集成测试

交付物：

- `packages/core/db/*`
- `alembic.ini`
- `alembic/versions/*`

### 3.2 阶段 B：配置与数据同步

目标：将 `creators.jsonc` 与 `creators/accounts` 表建立幂等同步链路。

任务：

1. 实现配置同步服务（upsert）
2. 幂等键：`(creator_key)`、`(creator_id, platform, account_type, normalized_account_url)`
3. URL 规范化工具
4. 同步失败回滚与错误日志

交付物：

- `packages/core/sync/config_sync.py`
- `packages/core/normalize/url_normalizer.py`

### 3.3 阶段 C：任务执行主链路

目标：API -> DB -> Redis 队列 -> Worker 执行 -> TaskRun/Artifacts 全链路打通。

任务：

1. 抓取阶段接入 content_id 去重查询
2. 下载阶段保留临时文件原子落盘策略
3. 任务重试与 run_number 递增策略
4. 任务日志与 metrics 打点

交付物：

- `apps/api/routes/tasks.py`
- `services/worker/executors/content_executor.py`

### 3.4 阶段 D：直播监控主链路

目标：`live_monitor` 与 `live_record` 形成闭环。

任务：

1. 直播状态表写入（offline/probing/recording/error）
2. 重连策略实现（快速窗口 + 慢速退避）
3. `/live/status` 查询真实状态
4. `/live/records/{account_id}/stop` 幂等停止

交付物：

- `services/worker/executors/live_executor.py`
- `apps/api/routes/live.py`

### 3.5 阶段 E：Web 管理台 MVP

目标：可进行任务管理、创作者管理、直播状态查看、配置热更新。

任务：

1. React 工程初始化
2. `/tasks`、`/creators`、`/live`、`/config` 四页
3. WebSocket 直播状态推送接入
4. 创作者新增仅输入 `display_name`

交付物：

- `apps/web/*`

## 4. 关键实现约束（必须持续遵守）

1. 配置文件是唯一权威源（single source of truth）
2. creator_key 规则：
   - 缺失才生成
   - 重复立即报错
3. 所有配置变更必须落盘，不允许仅改内存
4. 所有配置写入使用 `update_jsonc_key` 原子替换，保留注释
5. 任务队列就是 Redis List，Consumer 就是 BLPOP 循环
6. Cookie 验证状态独立存储于 Redis，不写回配置文件

## 5. 立即执行顺序（下一步）

1. ~~将 Douyin provider 栅逻辑替换为真实抓取实现（分页抓取 + 去重 + 下载）~~ **已完成**
2. ~~在可用 PostgreSQL 环境下完成一次完整业务流验证~~ **已完成**
3. 将 content executor 从“仅写 artifacts”扩展为“实际下载文件 + 原子落盘”
4. 实现 DB content_id 层去重（fetch 阶段已包含 content_id，尚未用于跳过）
5. 将 integration 测试接入本地 compose CI 流程
6. deep_scan 执行器集成
7. 对外网版鲉权开关与审计日志埋点

---

## 1.21 Weibo Provider 实现

实现文件：`packages/providers/weibo/__init__.py`

**抹取方式**：HTML 抓取 `weibo.cn`（移动版，较少风控）。

**关键实现点**：

- `fetch_content_items(task_params, account_url)` — 分页抓取 `weibo.cn/u/{uid}` 并返回标准化了的内容项列表。
- `_extract_uid(account_url)` — 从多种 URL 格式解析 UID（支持 `weibo.com/u/{uid}`、`weibo.com/{name}` 展开查询）。
- `_to_large(url)` — 将图片 CDN URL 从缩略图升级为大图（`thumbnail` → `large`）。
- `_fetch_page_json(page_url)` — 请求单页，返回 JSON 数据；当 `ok != 1` 且含 `login/auth/cookie/token` 关键词时抛出 `RuntimeError`（认证失败信号）。
- **速率控制**：Provider 类级 `_rate_limit(tick)` 确保每次 API 调用（包括翻页）遵循 `task.params["tick"]` 的最小间隔。使用 `threading.Lock` + `time.sleep`（sync 模式，因运行在 `asyncio.to_thread` 线程池中）。

**Provider 属性**：

```python
platform = "weibo"
account_types = ["profile"]  # 暂无直播支持
```

## 1.22 Xiaohongshu Provider 实现

实现文件：

- `packages/providers/xiaohongshu/__init__.py` — Provider 主入口
- `packages/providers/xiaohongshu/xhs_signer.py` — `XHSignatureSigner`，封装签名层
- `packages/providers/xiaohongshu/xs_signer.py` — 本地 mnsv2 签名实现
- `packages/providers/xiaohongshu/xs_crypto.py`、`xs_encoder.py`、`xs_fingerprint.py`、`xs_config.py` — 辅助加密模块
- `scripts/xhs_login.py`、`scripts/xhs_qr_login.py` — Cookie 获取辅助脚本

**抹取方式**：调用 `edith.xiaohongshu.com/api/sns/web/v1/user_posted` API，自研签名得到 `X-s`、`X-t` 请求头。

**关键实现点**：

- `XHSignatureSigner.sign(url, data)` — 本地计算 `X-s`、`X-t`，无需外部签名服务。
- `fetch_notes(user_id, cursor, cookies)` — 分页拉取账号发布的笔记，返回 `{notes, has_more, cursor}`。
- `fetch_note_detail(note_id, xsec_token, cookies)` — 调用 Feed API 获取笔记详情（多图 URL、视频 URL、创建时间）。
- 认证失败检测：`success == false` 且含 `verify/login/auth/expired/461/471` 关键词 → 抛出 `RuntimeError` → Worker 自动 `invalidate("xiaohongshu")`。
- **速率控制**：Provider 类级 `_rate_limit(tick)` 确保每次 API 调用（列表 + Feed 详情）遵循 `task.params["tick"]` 的最小间隔。使用 `threading.Lock` + `time.sleep`（sync 模式）。

**Provider 属性**：

```python
platform = "xiaohongshu"
account_types = ["profile"]  # 无直播支持
```

## 1.23 Cookie 管理 API 实现

实现文件：`packages/core/config/cookie_verify.py`

**Redis 状态管理**：

```python
_KEY_PREFIX = "polycrawl:cookies:verify:"
_DEFAULT_TTL = 604800  # 7 天

# 四个函数
def get_verify_state(platform) -> dict        # 返回 {verified_ok, verified_at}
def set_verified(platform, ok: bool)          # 写入并设置 TTL
def invalidate(platform)                      # Worker 自动失效时调用
def clear(platform)                           # 删除 key（粘贴新 Cookie 时调用）
```

**`POST /login/cookies` 流程**：

```
1. 解析请求 body {platform, cookies}
2. update_jsonc_key("config/sites/{platform}.jsonc", "platform.cookies", cookies)
3. update_jsonc_key(..., "platform.saved_at", now_iso8601)
4. cookie_verify.clear(platform)   ← 清除 Redis 验证态
```

**`GET /login/status` 返回格式**：

```json
{
  "platforms": {
    "douyin": {
      "has_cookies": true,
      "saved_at": "2026-05-29T10:00:00Z",
      "verified_ok": true,
      "verified_at": 1748512800,
      "critical": false
    }
  }
}
```

**`POST /login/verify` 各平台验证逻辑**：

| 平台 | 验证方式 | 成功条件 |
|---|---|---|
| xiaohongshu | 请求 `/api/sns/web/v2/user/me`（自研签名） | `data.guest == false` |
| douyin | 请求 Douyin 主页 | HTTP 200 且正文长度 > 1KB |
| weibo | 请求 `m.weibo.cn/api/config` | 返回 JSON 含 `uid` 字段 |

验证结果通过 `cookie_verify.set_verified(platform, ok)` 写入 Redis。

## 1.24 WebSocket 实时推送

实现文件：`apps/api/main.py`

**两个 WebSocket 端点**：

| 端点 | 内容 | 前端使用 |
|---|---|---|
| `WS /ws/logs` | 结构化日志流，格式 `{level, message, timestamp, logger}` | 日志页实时显示 |
| `WS /ws/events` | 任务状态变更、直播状态变更事件，格式 `{type, payload}` | 其他页实时刺新 |

前端建立双 WebSocket 连接。日志页订阅 `/ws/logs`，其他页面通过 `/ws/events` 接收实时更新。
日志页初始加载通过 `GET /logs` REST 接口补充历史记录，再建立 

WS 接收增量。

## 1.25 Worker Startup Recovery

实现文件：`services/worker/run.py` 启动阶段

Worker 启动时不预设上次是正常退出，自动修复异常退出郳留的状态：

```python
# 1. 将 DB 中 status=pending/running 的任务全部标为 failed
#    error_message = "Worker restarted unexpectedly"
UPDATE tasks SET status='failed', error_message='...' WHERE status IN ('pending','running')

# 2. 清空 Redis 中所有 task_{idx} 队列残留消息
for i in range(len(schedules)):
    redis.delete(f"task_{i}")

# 3. 将 live_statuses 中 recording/probing 状态重置为 offline
UPDATE live_statuses SET status='offline' WHERE status IN ('recording','probing')

# 4. 将 live_sessions 中 status=active 的会话标为 interrupted
UPDATE live_sessions SET status='interrupted' WHERE status='active'
```

Recovery 完成后再启动 Scheduler 和 Consumer，保证启动后状态干净。

## 1.26 Provider 认证失败统一处理

三个 Provider 均已实现认证失败自动 invalidate：

**Douyin** (`packages/providers/douyin/api_client.py`)：

```python
status_code = data.get("status_code", 0)
if status_code == 6:
    raise RuntimeError(f"Douyin API error: status_code=6 — cookies may be invalid")
# Worker 捕获 RuntimeError 后调用 cookie_verify.invalidate("douyin")
```

**Weibo** (`packages/providers/weibo/__init__.py`)：

```python
if data.get("ok") != 1:
    msg = str(data.get("msg") or "")
    if data.get("ok", 1) < 0 or any(kw in msg.lower() for kw in ("login","auth","cookie","token")):
        raise RuntimeError(f"Weibo API auth failure: ok={data.get('ok')}, msg={msg}")
```

**Xiaohongshu** (`packages/providers/xiaohongshu/xhs_signer.py`)：

```python
if not data.get("success"):
    msg = str(data.get("msg", "unknown error"))
    if any(kw in msg.lower() for kw in ("verify","login","auth","expired","461","471")):
        raise RuntimeError(f"Xiaohongshu API auth failure: {msg}")
```

Worker Consumer 捕获 `RuntimeError`，若错误信息包含认证关键词，调用 `cookie_verify.invalidate(platform)`。

## 1.27 前端改进记录（本轮完成）

### 任务错误详情可视化

- `apps/api/schemas.py` `TaskResponse` 新增 `error_message: str | None = None` 字段。
- `apps/web/tasks.js` ：失败任务行点击展开 `.task-error-detail` 详情行，显示 `error_message`。
- `apps/web/overview.js` ：`renderRecentFailures()` 显示最近 10 条失败任务及错误摘要。

### 日志级别层级过滤

`apps/web/logs.js` 将日志级别过滤从精确匹配改为层级比较：

```javascript
const LEVELS = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
const minLevel = LEVELS[(filterValue || "").toUpperCase()] ?? -1;
// 选 INFO = 显示 INFO + WARNING + ERROR
const filtered = minLevel >= 0
    ? entries.filter(e => (LEVELS[(e.level||"").toUpperCase()] ?? 0) >= minLevel)
    : entries;
```

### Web 管理台完整 Tab 列表

详见 `docs/design.md §3.12`。当前 8 个 Tab 均已实现：创作者、概览、任务、直播、调度、日志、登录管理、文档。

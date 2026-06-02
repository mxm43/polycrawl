# PolyCrawl 设计文档（评审版）

本文档用于当前阶段设计评审。目标是先确认架构与边界，再进入下一步开发与联调。

## 1. 目标与范围

### 1.1 项目目标

- 整合多平台爬虫（Douyin、Twitter/X、Weibo、Xiaohongshu）到统一中台。
- 统一配置、任务管理、下载流程、反爬策略。
- 在统一账号体系下支持直播下载（录制）。
- 提供统一 Web 管理台，并为未来 iOS/Android App 复用 API 能力。
- 支持 Docker 化部署，优先适配群晖 DSM。

### 1.2 当前范围（V1）

- 已落地：核心框架、Douyin / Weibo / Xiaohongshu Provider 完整实现、FastAPI（含 WebSocket 实时推送）、自研 asyncio Worker（事件驱动调度器 + Redis 队列消费）、Vanilla JS Web 管理台（8 个功能页）、Cookie 管理系统、Synology 部署模板。
- 规划中：Twitter/X Provider 实现、移动端应用。
- 架构路线：自研 asyncio Scheduler + Consumer 调度执行，Vanilla JS 多文件 Web 前端。

### 1.3 非目标（当前不做）

- 不做分布式多机任务编排（仅单实例/小规模扩展）。
- 不做复杂 BI 报表系统。
- 不做自动化绕过验证码能力。

## 2. 总体架构

## 2.1 分层

- apps/api：控制面，提供任务、配置、鉴权、健康检查 API。
- services/worker：执行面，消费队列并执行抓取与下载。
- packages/core：共享核心（配置模型、任务模型、反爬策略、下载引擎、Provider 抽象）。
- packages/providers/*：平台适配层（当前已完成 douyin）。
- apps/web：管理台（任务/配置/运行态可视化）。
- deploy/*：容器部署与群晖适配。

## 2.2 关键基础设施

- PostgreSQL：任务、运行记录、产物元数据、配置版本的持久化存储（权威数据源）。
- Redis：任务队列（`task_{idx}:{platform}` Redis List，LPUSH 写入 / BLPOP 消费）、Cookie 验证状态（key `polycrawl:cookies:verify:{platform}`，TTL 7d）、直播 `last_live` 时间戳缓存（key `polycrawl:live:last_live:{account_id}`）。
- asyncio Scheduler（Worker 进程内）：事件驱动 + 定时器驱动的双轨调度器，在 Worker 单进程内运行。
- Watchdog / 轮询（Worker 进程内）：监听配置文件变更，触发配置重载 + DB 同步 + 调度器完整重建。在 Docker Desktop（9p 文件系统）环境下自动切换为 5 秒轮询。

## 3. 核心设计

## 3.1 配置系统

- 配置来源：仅配置文件（File-only），不读取任何系统环境变量。
- 配置结构：通用基础配置文件 + creators 独立配置文件 + 各站点独立配置文件。
- 配置优先级：default < base config < site config < creators config。
- 配置权威源：配置文件（single source of truth），运行时以内存有效配置为准；一旦文件变更并通过校验，立即替换生效。
- 主配置模型：`AppConfig`（Pydantic v2）。
- 启动硬约束：若配置文件缺失或字段不合法，服务启动失败（fail fast）。
- 配置写回规则：所有配置变更（`PUT /schedules`、`POST /login/cookies`、`PATCH /accounts/{id}/scheduled`、`PATCH /creators/{key}/tags` 等）必须原子落盘到对应配置文件，禁止仅改内存。
- 配置热加载机制（已实现，`services/worker/scheduler.py`）：
  - Worker 进程内 Watchdog 监听 `base.jsonc`、`creators.jsonc`、`sites/*.jsonc` 的 mtime 变化（inotify 事件驱动；9p 文件系统自动降级为 5s 轮询）。
  - 变化触发：全量重新加载 → Pydantic 校验 → 原子替换内存配置 → DB 同步（config_sync）→ 完整重建调度器（取消所有 timer，重新 `_register_all` + `_init_live_tiers`）。
  - 校验失败则保留上一版有效配置，不中断服务，记录错误日志。
  - schedules、strategy、live tier、cookie 等所有字段变更均可热加载实时生效，无需重启进程。

### 3.1.1 配置文件格式规范（拟定 v1）

- 文件格式：JSONC（UTF-8，支持 `//` 与 `/* ... */` 注释）。
- 配置拆分：
  - 通用基础配置：`./config/base.jsonc`
  - 创作者配置：`./config/creators.jsonc`
  - 站点独立配置：`./config/sites/<site>.jsonc`
- 支持站点：`douyin`、`twitter`、`weibo`、`xiaohongshu`
- 合并规则：深度合并（deep merge），同路径字段站点配置覆盖基础配置。
- 限速归属：统一使用 `rate_control`，可在基础配置中定义默认值，也可在站点配置中覆盖。
- 站点标签规则：站点配置文件内不使用 `douyin/twitter` 等站点标签键，当前站点由文件名决定。
- 版本字段：`config_version: 1`
- 要求：所有敏感字段也在配置文件声明，不通过环境变量注入。

示例（拟定）：

`./config/base.jsonc`

```json
{
  // 配置版本
  "config_version": 1,

  "global": {
    "debug": false,
    "log_level": "INFO",
    "data_dir": "./data",
    "trace_enabled": true
  },

  "storage": {
    "database_url": "postgresql+asyncpg://polycrawl:password@postgres:5432/polycrawl_db",
    "redis_url": "redis://redis:6379/0",
    "media_base_path": "./downloads"
  },

  "auth": {
    "secret_key": "CHANGE_ME_TO_RANDOM_32B",
    "algorithm": "HS256",
    "access_token_expire_minutes": 30,
    "refresh_token_expire_days": 7
  },

  "download": {
    "base_path": "./downloads",
    "thread_count": 5,
    "retry_times": 3,
    "timeout_seconds": 30,
    "chunk_size": 1048576,
    "naming_template": "{platform}/{author}/{date}_{title}"
  },

  "rate_control": {
    "fetch_requests_per_second": 2.0,
    "download_requests_per_second": 3.0,
    "burst": 5
  },

  "increase": {
    "enabled": true,
    "look_ahead_pages": 8               // 停止点后继续抓 N 页再判断，覆盖近期隐藏/解锁
  },

  "anti_bot": {
    "proxy": null,
    "proxy_pool": [],
    "user_agent_rotation": true,
    "user_agents": [],
    "retry_policy": {
      "max_retries": 3,
      "backoff_base": 2.0,
      "backoff_max": 60.0,
      "jitter": true
    },
    "circuit_breaker": {
      "enabled": true,
      "failure_threshold": 5,
      "timeout_seconds": 300
    }
  }
}
```

`./config/sites/douyin.jsonc`

```json
{
  // 平台认证
  "platform": {
    "enabled": true,

    // Cookie（键值对形式，从浏览器 DevTools → Application → Cookies 获取）
    "cookies": {
      "msToken": "",
      "ttwid": "",
      "odin_tt": "",
      "passport_csrf_token": "",
      "passport_csrf_token_default": "",  // 与 passport_csrf_token 保持一致
      "passport_assist_user": "",
      "sid_guard": ""
    }
  },

  // 下载类型（可多选）
  // post: 发布作品   like: 点赞作品   mix: 合集作品
  "mode": ["post"],

  // 下载内容开关（覆盖 base.jsonc 中的 download 通用开关）
  "download": {
    "music": false,
    "cover": false,
    "avatar": false,
    "save_metadata_json": false,
    // true = 每个用户创建独立子文件夹；false = 按 naming_template 平铺
    "folder_style": false
  },

  // 站点限速（覆盖 base.jsonc 中的 rate_control 默认值）
  "rate_control": {
    "fetch_requests_per_second": 1.5,
    "download_requests_per_second": 2.5
  },

  // 直播默认策略（平台级，供 creators 中 live 账号继承）
  "live": {
    "enabled": true,
    "check_interval_seconds": 90,
    "record": {
      "container": "ts",
      "segment_seconds": 6,
      "max_duration_minutes": 0,
      "auto_merge": true,
      "fast_reconnect_seconds": [1, 2, 3, 5, 8],
      "recover_window_seconds": 120
    }
  }
}
```

字段约束（拟定）：

- `config_version`：必填，当前固定为 `1`。
- `storage.database_url` / `storage.redis_url`：必填。
- `auth.secret_key`：必填，长度不小于 32。
- `rate_control` 可定义在 `base.jsonc`（默认值）和 `sites/<site>.jsonc`（覆盖值）。
- `rate_control.fetch_requests_per_second`：大于 0。
- `rate_control.download_requests_per_second`：大于等于 0（0 表示不限速）。
- `rate_control.burst`：大于等于 1。
- `sites/<site>.jsonc.live`：直播平台默认策略。
- `sites/<site>.jsonc.live.check_interval_seconds`：大于等于 30，建议不小于 60。
- `sites/<site>.jsonc.live.record.segment_seconds`：大于 0。
- `platform.enabled=false` 时，可允许凭证为空。

### 3.1.2 创作者归类配置

为支持"同一博主多账号"统一管理，配置层新增创作者实体，但不改变物理文件存储结构。

- 归类原则：抓取按账号执行，聚合按创作者展示。
- 目录原则：不按 creator 复制/硬链接生成镜像目录，所有文件仅保留一份真实路径。
- 关联原则：creator 与 account 的关系由数据库维护，配置文件仅描述静态绑定与默认策略。
- 继承原则：`enabled`、`mode`、`increase` 等执行策略统一由站点配置提供默认值，账号条目默认不重复声明。
- 标识原则：账号配置统一显式声明 `type` 区分 URL 类型，避免同平台不同 URL 语义混淆。
- 平台原则：每个账号必须显式声明 `platform`，避免同一 creator 下多平台账号出现歧义。

示例（拟定）：

`./config/creators.jsonc`

```jsonc
{
  "creators": [
    {
      // creator_key 系统自动生成（如 creator_a3k2m9x1），用户不需要填写或修改
      "creator_key": "creator_a3k2m9x1",
      "display_name": "李明",
      "accounts": [
        {
          "platform": "douyin",
          "type": "profile",
          "account_url": "https://www.douyin.com/user/EXAMPLE_SEC_UID",
          "account_alias": "main"
        },
        {
          "platform": "douyin",
          "type": "live",
          "account_url": "https://live.douyin.com/1234567890",
          "account_alias": "live_main"
        },
        {
          "platform": "douyin",
          "type": "live",
          "account_url": "https://live.douyin.com/9876543210",
          "account_alias": "live_low_freq",
          "live": {
            // 可选覆盖：仅在该直播间需要差异化时填写
            "enabled": true,
            "check_interval_seconds": 180,
            "record": {
              "max_duration_minutes": 240
            }
          }
        }
      ]
    }
  ]
}
```

说明：创作者归类统一放在 `config/creators.jsonc`（跨平台聚合），账号项显式声明 `platform`；站点策略仍放在 `config/sites/<site>.jsonc`。

字段约束（拟定）：

- `creators` 根数组定义在 `config/creators.jsonc`。
- `creator_key`：必填、全局唯一、系统自动生成，用户**无法修改**。
  - **生成规则**：系统生成随机字符串，格式为 `creator_<随机8位字母数字>`（如 `creator_a3k2m9x1`），不依赖任何用户输入字段。
  - **唯一性保障**：无需冲突检测，生成时即保证全局唯一性（数据库 UNIQUE 约束）。
  - **示例**：无论输入什么 display_name，系统都会生成诸如 `creator_a3k2m9x1`、`creator_b7j1q5c9` 这样的随机 key。
  - **配置写回**：创建 creator 时，系统自动生成 `creator_key` 并写入 `creators.jsonc`，用户无需关心。
- `accounts[].platform`：必填，取值为 `douyin` / `twitter` / `weibo` / `xiaohongshu`。
- `accounts[].type`：必填，取值为 `profile` / `live`。
- `accounts[].account_url`：必填，必须同时与 `platform + type` 对应（如 Douyin `profile` 为 `/user/<sec_user_id>`，Douyin `live` 为 `live.douyin.com/<room_id>`）。
- `accounts[]` 唯一性：建议以 `(platform, type, account_url)` 去重；程序启动时解析 URL 得到标准化标识，并在首次抓取后可回填平台主键到数据库。
- `accounts[].account_alias`：可选，用于区分同创作者下多个账号。
- `enabled` / `mode` / `increase`：统一使用站点级配置；如未来需要账号特例，再引入 `account_overrides` 显式覆盖。
- `accounts[].live`：可选，仅用于覆盖 `sites/<site>.jsonc.live` 的平台默认策略。
- `accounts[].live.check_interval_seconds`：若声明则需大于等于 30。
- `accounts[].live.record.max_duration_minutes`：若声明则需大于等于 0。

### 3.1.3 直播配置归属与账号类型（已确认）

- 直播目标（直播间列表）放在 `config/creators.jsonc`，直播默认策略放在 `config/sites/<site>.jsonc.live`。
- `accounts[].live` 为可选覆盖层，不要求每个直播间都单独配置。
- `accounts[].type` 作为 URL 语义判定的唯一显式字段：
  - `profile`：内容抓取账号（作品/图文等）。
  - `live`：直播间录制目标。

### 3.1.4 配置热加载与一致性策略（已确认）

- 文件变更检测：监听 `base.jsonc`、`creators.jsonc`、`sites/<site>.jsonc` 的修改时间或文件事件。
- 重新加载流程：检测到变更 -> 读取并合并 -> Pydantic 校验 -> 原子替换内存配置。
- 失败回退：新配置校验失败时，保留上一版有效配置并记录错误日志，不中断服务。
- 生效时机：校验通过后立即生效（秒级），无需重启。
- 幂等键（用于导入/对齐账号）：`(creator_key, platform, type, normalized_account_url)`。
- `normalized_account_url` 规则：去 query/fragment、统一 host 大小写、按平台提取稳定主键（如 Douyin live 的 room_id）。
- 配置落盘映射：
  - 全局与通用字段（如 `rate_control.*`、`anti_bot.*`）写入 `base.jsonc`。
  - 平台字段写入 `sites/<site>.jsonc`。
  - 创作者与账号字段写入 `creators.jsonc`。
- 并发控制：配置写入采用“文件锁 + 临时文件 + 原子替换”，避免并发写导致半文件状态。

## 3.2 任务模型与状态机

- 任务状态：pending / queued / running / success / failed / canceled / retrying。
- 一条 Task 可对应多条 TaskRun（重试场景）。
- Artifact 存储每个下载产物元数据（路径、校验和、大小、类型、去重标记）。
- 任务类型：`content_fetch`（内容抓取）、`live_record`（直播录制）。`live_monitor` 任务类型已移除——开播检测已内化为 Scheduler 的 tier-driven 事件派发，不生成独立的可见任务，也不占用 DB task 记录。

### 3.2.1 数据库结构设计（上线前审阅）

当前数据库结构以 Alembic 首版迁移为准，目标是确保任务可追踪、下载可去重、直播可回放、配置可审计。

主关系：

- creators 1 -> N accounts
- accounts 1 -> N tasks
- tasks 1 -> N task_runs
- accounts 1 -> N artifacts
- accounts 1 -> 1 live_statuses
- accounts 1 -> N live_sessions

核心表设计：

1. creators
  - 字段：id, creator_key, display_name, created_at, updated_at
  - 约束：creator_key 全局唯一

2. accounts
  - 字段：id, creator_id, platform, account_type, account_url, platform_account_id, account_alias, created_at, updated_at
  - 约束：uq_accounts_platform_platform_account_id (platform, platform_account_id)

3. tasks
  - 字段：id(UUID), account_id, task_type, status, params(JSONB), retry_count, max_retries, created_at, started_at, completed_at, queue_key（存储 Redis 队列标识如 `task_0:douyin`）, error_message

4. task_runs
  - 字段：id, task_id, run_number, status, started_at, completed_at, duration_seconds, items_fetched, items_downloaded, items_failed, items_skipped, bytes_downloaded, error_type, error_message, error_detail(JSONB), log_entry_id
  - 约束：uq_task_runs_task_id_run_number (task_id, run_number)

5. artifacts
  - 字段：id, account_id, task_id, platform, content_id, media_kind, file_path, file_size, sha256, title, author, publish_date, download_date, status, created_at
  - 约束：uq_artifacts_account_platform_content_media (account_id, platform, content_id, media_kind)
  - 运行规则：仅 status=completed 且 file_size>0 时参与下载跳过判定

6. live_statuses
  - 字段：id, account_id, status, status_since, current_recording_session_id, recorded_seconds, recorded_bytes, error_message, error_time, updated_at
  - 约束：account_id 唯一（每个账号一条实时状态）

7. live_sessions
  - 字段：id(UUID), account_id, started_at, ended_at, output_file_path, total_duration_seconds, total_bytes, segment_count, status, error_message

8. config_versions
  - 字段：id, version_number, config_content(JSONB), changed_by, change_reason, changed_at, is_active

外键删除策略：

- creators -> accounts: CASCADE
- tasks -> task_runs: CASCADE
- accounts -> tasks/artifacts/live_sessions: SET NULL（保留历史轨迹）
- accounts -> live_statuses: CASCADE（状态快照随账号删除）

索引重点：

- tasks: account_id, status, task_type, created_at desc, queue_key
- task_runs: task_id, status, completed_at desc
- artifacts: account_id, platform+content_id, download_date desc, status, file_path
- live_statuses: account_id, status
- live_sessions: account_id, started_at desc
- creators/accounts: creator_key, creator_id, platform+account_type, account_url, platform+platform_account_id

状态值约定（当前应用层约束）：

- tasks.status: queued, running, success, failed, canceled
- task_runs.status: queued, running, success, failed
- artifacts.status: pending, completed, download_failed, no_url
- live_statuses.status: probing, recording, offline, error
- live_sessions.status: completed, interrupted

上线前审阅清单：

1. 是否将状态值下沉为数据库 CHECK 约束（当前未加）。
2. artifacts 是否需要补强哈希唯一策略（当前 sha256 字段已预留，未约束）。
3. JSONB 字段（tasks.params, task_runs.error_detail）是否需要 GIN 索引。
4. config_versions 是否在 V1 启用完整写入闭环。

### 3.3 Creator 列表交互补充（本轮确认）

- 不再强调“涉及平台数”统计；在 creator 名称后直接展示平台标签（如 `douyin`、`twitter`）。
- 支持用户自定义标签，并支持基于标签过滤 creator 列表。
- 列表支持按展示信息排序（最近更新、作品数、容量、名称）。
- 链接展示按“平台 + 账号类型（type）”分组。
- 组内链接一行一个，链接文案不使用 `profile 1` 这类占位命名，优先使用别名，否则显示可读 URL 标识。

状态流转：

- Scheduler 定时触发 / API 手动创建 → tasks.status=queued → LPUSH 入 Redis List `task_{idx}`。
- Worker Consumer BLPOP 消费 → tasks.status=running → 执行完成 → success / failed（写入 error_message）。
- 每次执行记录写入 task_runs（含 items_fetched、items_downloaded、bytes_downloaded 等统计）；成功产物写入 artifacts。
- 任务失败的 `error_message` 通过 `GET /tasks` 暴露给前端，支持点击展开详情。

## 3.3 Provider 插件机制

### 3.3.1 BaseProvider 统一接口

每个 Provider 必须继承 `BaseProvider`（`packages/core/providers/base.py`）并实现以下接口：

```python
class BaseProvider(ABC):
    platform: str                                          # 平台标识，如 "douyin"
    account_types: list[str] = ["profile", "live"]         # 支持的账号类型列表，用于前端动态渲染

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_content_items(self, task_params, account_url) -> list[dict[str, Any]]: ...
    @abstractmethod
    def detect_live_status(self, task_params, account_url) -> bool: ...
    @abstractmethod
    def build_live_session_payload(self, task_params, account_url) -> dict[str, Any]: ...

    # 可选覆写
    def build_account_dir(self, account) -> str: ...
    def build_content_file_path(self, item, *, creator_dir, account_dir, account_url) -> str: ...
    def build_download_request(self, account_url) -> dict[str, Any]: ...
    def refresh_content_item_for_download(self, *, content_id, media_kind, account_url) -> dict | None: ...
    def resolve_live_stream(self, task_params, account_url) -> dict | None: ...
    def build_live_download_request(self, account_url) -> dict[str, Any]: ...
```

### 3.3.2 平台发现

- `GET /platforms` API：自动扫描 `packages/providers/` 目录，对每个有效包调用 `build_provider()` 工厂函数获取 Provider 实例，返回其 `platform` 和 `account_types`。
- 前端添加链接时根据此 API 动态渲染平台和类型下拉框，无需硬编码。
- 新增平台只需在 `packages/providers/<name>/__init__.py` 中实现 `build_provider()` 工厂函数，前端自动感知。

### 3.3.3 注册与加载

- `ProviderRegistry` 按平台惰性加载与缓存。
- 通过 `importlib.import_module(f"packages.providers.{platform}")` 动态导入。
- 调用 `build_provider()` 工厂函数获取实例后注册。
- 已加载的 Provider 缓存在内存中，避免重复加载。

### 3.3.4 错误处理

- 配置中声明的平台对应 Provider 不存在时，启动时记录警告，不中断启动。
- 执行任务时若 Provider 不可用，返回 `ProviderNotFoundError`，任务标记为失败。

## 3.4 反爬与下载协同

### 3.4.0 速率配置口径与生效优先级（已收敛）

为避免限速配置分散，V1 统一以 `rate_control` 为唯一入口：

- 抓取通道（API 请求）
  - 读取 `rate_control.fetch_requests_per_second`（站点配置优先，缺省回退 base）。

- 下载通道（CDN 资源请求）
  - 读取 `rate_control.download_requests_per_second`（站点配置优先，缺省回退 base）。

- 突发控制
  - `rate_control.burst` 统一用于令牌桶突发容量（站点未显式设置时继承 base）。

- 兜底规则
  - `fetch_requests_per_second` 必须大于 0。
  - `download_requests_per_second = 0` 表示下载通道不限速。

当前策略链（API 抓取阶段）：

- 失败信号识别（429/403/timeout 等）。✅ 已实现（HTTP 状态码 + JSON 业务错误码双层检测）
- Provider 认证失败检测 → 自动 invalidate Cookie 验证状态（三平台各自识别关键词）。✅ 已实现
- 指数退避 + jitter。⚠️ Provider 层内有简单 sleep，无统一退避框架
- UA 轮换、代理切换。❌ 未实现
- 熔断器（Circuit Breaker）。❌ 未实现

下载阶段（CDN 资源拉取）：

- 并发受 `download.thread_count` 限制。✅ 配置字段已存在，执行层已接入
- 下载速率由 `rate_control.download_requests_per_second` 控制。⚠️ 配置字段已存在，令牌桶未实现（当前为简单 sleep）
- 对 URL 过期（典型 403/410）预留“刷新 URL 再下载一次”的扩展位。⚠️ 架构已预留 `refresh_content_item_for_download`，尚未实际触发

### 3.4.1 时效链接与频率冲突的设计原则

- 原则1：抓取与下载限速分离。
  - API 抓取限速：`rate_control.fetch_requests_per_second`
  - CDN 下载限速：`rate_control.download_requests_per_second`
- 原则2：优先“及时下载”，避免先抓大量链接后排队过久。
- 原则3：检测 URL 过期时，走“刷新 URL -> 单次重试”而非盲目多次重试。
- 原则4：任务规模大时优先分批抓取分批下载（分页窗口化）。
### 3.4.2 已下载资源的去重与增量机制

#### content_id 的稳定性

平台 URL（CDN 签名链接）随时会失效或变更，**不能作为去重依据**。
`content_id` 使用平台原生内容 ID（Douyin: `aweme_id`），与 URL 无关，具备稳定性，是去重的唯一键。

#### 下载完整性保障

下载引擎采用"临时文件 → 原子重命名"模式：

1. 下载流写入 `<dest>.tmp`，同步计算 SHA-256。
2. 下载完成后 `tmp.rename(dest)`，正式文件名出现即代表完整。
3. 进程意外崩溃只会留下 `.tmp` 残留，不会影响正式文件的存在性判断。

⚠️ 当前漏洞：`.tmp` 残留文件不会被自动清理，长期运行会积累磁盘占用。待实现：启动时或任务开始前扫描并清理孤立 `.tmp`。

#### 三层去重机制

| 层级 | 位置 | 当前状态 | 说明 |
|---|---|---|---|
| 文件存在性 | 下载阶段 | ✅ 已实现 | 正式文件名存在则跳过（`.tmp` 不干扰判断） |
| 数据库 content_id | 抓取阶段 | ⚠️ 字段存在，未用于跳过 | `artifacts` 表有 `content_id`，fetch 阶段未查询 |
| 增量翻页停止 | 翻页阶段 | ❌ 未实现，且有平台限制 | 见下方说明 |

#### 增量翻页停止的限制

⚠️ **不能简单地"遇到已知 content_id 就停翻页"**，原因：

- 平台支持作者**隐藏再解锁**作品，解锁后该内容会重新出现在时间线中，但位置可能在已知内容之后（旧时间戳），导致漏抓。
- 平台内容排序不保证严格时间倒序（置顶、活动内容等会插队）。

## 3.9 多维扫描策略体系

### 3.9.1 三层架构

```
第1层：任务调度层 (ScheduleEntry)    ← 谁来扫、什么时候扫、扫哪些 tag
第2层：账号自适应层 (Adaptive)       ← 每个账号的动态频率调整
第3层：内容策略层 (Fetch Strategy)   ← 扫到什么程度停
```

三层相互独立、可组合使用。

### 3.9.2 策略配置层 （`config/base.jsonc > strategy`）

策略配置与 `tasks[]` 同级，定义各策略的默认参数和自适应规则。

```jsonc
"strategy": {
    "incremental": {
      "adaptive": true
    },
    "deep": {
      "adaptive": false
    },
    "adaptive": {
      "tiers": [
        { "after": "14d", "interval": "1d" },
        { "after": "30d", "interval": "3d" },
        { "after": "60d", "interval": "7d" },
        { "after": "180d", "interval": "30d" }
      ]
    }
  }
```

| 字段 | 说明 |
|------|------|
| `incremental.adaptive` | 增量策略是否启用自适应降频 |
| `deep.adaptive` | 深度策略是否启用自适应降频 |
| `adaptive.tiers[]` | 自适应阶梯曲线定义 |

> **注意**：`tick` 和 `jitter` 不再属于策略配置，已移到 `config/sites/{platform}.jsonc → request`（平台级）和 `tasks[].request`（任务级覆写）。
>
> ```jsonc
> // config/sites/xiaohongshu.jsonc
> "request": {
>     "tick": "2s",
>     "jitter": ["1s", "2s"]
> }
> ```
>
> 解析优先级（高→低）：
> 1. `tasks[].request.tick` — 任务级覆写
> 2. `sites/{platform}.jsonc → request.tick` — 平台默认值
> 3. `strategy.{use}.tick` — 策略全局默认值
>
> 调度器将 tick/jitter 通过 `task.params` 传递给 Provider，Provider 在每个 API 调用前通过 `_rate_limit(tick)` 确保间隔。
> 当前所有平台（Douyin、Xiaohongshu、Weibo）均已实现此机制。

### 3.9.3 任务调度层 （`config/base.jsonc > tasks[]`）

```jsonc
// 完整配置示例
"tasks": [
  {
    "type": "content_fetch",
    "enabled": true,
    "strategy": { "use": "incremental" },
    "start_at": "02:00",
    "interval": "12h"
  },
  {
    "type": "content_fetch",
    "enabled": true,
    "strategy": { "use": "deep" },
    "interval": "15d"
  }
]
```

#### 参数字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 任务类型: content_fetch / live_monitor / live_record |
| `enabled` | bool | 否 | 默认为 true |
| `strategy.use` | string | 是 | 引用的策略名: `incremental` 或 `deep` |
| `strategy.adaptive` | bool | 否 | 覆盖策略的 adaptive 开关 |
| `request.tick` | string | 否 | 覆盖 API 请求间隔，如 `3s`（同时覆盖 site config 和 strategy 默认值） |
| `request.jitter` | [string,string] | 否 | 覆盖请求间隔随机抖动范围，如 `["2s","4s"]` |
| `tag_filter` | string[] | 否 | 标签白名单，只处理匹配的 creator；不填则处理所有 |
| `start_at` | string | 否 | 首次执行时间 HH:MM，24h 格式 |
| `interval` | string | 否 | 窗口/周期: Ns/Nm/Nh/Nd，如 30s, 12h, 1d, 15d |
| `look_ahead_pages` | int | 否 | 遇到已知内容后继续翻页数，默认继承站点配置 |

### 3.9.4 内容策略层 (Fetch Strategy)

#### incremental（增量模式）

日常增量翻页，适用于绝大多数场景。

```
执行流程：
  第1页 → 第2页 → ... → 遇到已知 content_id
    ↓ 不立即停止，继续抓 look_ahead_pages 页 (默认1页)
  [全是已知内容] → 停止 ✅
  [发现未知 content_id] → 重置计数器，继续向下翻 ✅ 捕获解锁内容
```

- 请求量 = 新内容页数 + look_ahead_pages
- 速率 = 平台正常抓取速率（`rate_control.fetch_requests_per_second`）

#### deep（深扫模式）

极低速率全量扫描所有 creator 的全部历史作品，用于捕获深层历史内容或漏掉的内容。目标是**每个作品约 30 秒**的扫描节奏，远低于正常抓取速率，极大降低风控风险。

```
速率计算（假设每页 20 个作品）:
  1 作品 = 30 秒
  1 页   = 20 × 30 = 600 秒 = 10 分钟
  1 个 creator (100 页) = 100 × 10 分钟 ≈ 16 小时
  全部 creator (156 个) = 156 × 16 小时 ≈ 104 天
```

#### 执行模型

deep **没有长运行任务**，由 Beat 调度器驱动。Beat 每 30 秒 tick 一次，每次派发一个工作单元。

```
调度器配置（base.jsonc）:
{
  "type": "content_fetch",
  "enabled": true,
  "strategy": { "use": "deep" },
  "interval": "15d"
}
```

Beat 的频率本身就是节拍——不需要额外的延迟检查，也不用在 Redis 里记 `last_work_at`。每次 tick 必然派发一个工作单元。

**粒度：每个工作单元只做一次平台 API 请求。**

```
工作单元类型            请求                     后续
────────────────────────────────────────────────────────
扫描 (page)            fetch_post_page()        发现新 content_id → 入队待下载
下载 (download)        fetch_post_detail()      新鲜 URL → CDN 下载
```

Beat 调度逻辑：

```
每个 Beat tick (每30秒):
  ├─ 读取 Redis: polycrawl:deep:discovered
  │     ├─ 不为空 → 弹出一个 content_id → 派发下载工作单元
  │     └─ 为空 → 读取 state 进度 → 派发页面扫描工作单元
  │
  └─ LPUSH `task_{deep_idx}` (Redis List)
```

工作单元执行流程：

```
deep_work 任务:
  acquire 平台锁 (polycrawl:ratelimit:douyin:content_fetch)

  if type == "page":
    请求第 N 页
    遍历该页: 新 content_id → 写入 Redis 待下载集合
    更新 state (creator_idx / page)

  if type == "download":
    fetch_post_detail(aweme_id) → 新鲜 download_url
    CDN 下载文件
    写入 Artifact(status="completed")

  release 平台锁
  ✅ 结束（几秒）
```

**deep 只拿 aweme_id，不拿列表页的 download_url。** 下载时通过 `fetch_post_detail()` 获取新鲜签名链接。

为什么不用列表页的 download_url——实测 CDN URL TTL：

| CDN 域名 | URL 有效时长 | 说明 |
|---------|-------------|------|
| `v26-web.douyinvod.com` | ~3 小时 | 主力 CDN |
| `video-web-cn.douyin.com` | ~1 小时 | 备用 CDN |

列表页返回的 download_url 在 deep 的蜗牛速率下必然过期。每次下载前调用 `fetch_post_detail()` 确保使用新签名，不会遇到 403。

#### Redis 数据丢失的韧性

Redis 不做持久化，但 deep 不会因 Redis 丢失而漏扫。

**去重权威源是 PostgreSQL `artifacts` 表**，Redis 仅作为临时工作队列：

| Redis Key | 丢失后果 | 恢复行为 |
|-----------|---------|---------|
| `polycrawl:deep:state` (进度) | 从头开始扫 | 重新遍历所有 creator，每页重新比对 artifacts |
| `polycrawl:deep:discovered` (待下载) | 待下载列表丢失 | 下次扫到同一页时重新发现，重新入队 |

所有 Redis key 丢失的最终结果都是**多做一次扫描 + 重新比对 artifacts**，不会导致漏下载。下载阶段的文件级去重和 DB 唯一约束提供双重保障。

```
最坏情况推演:
  Redis 崩溃
    ↓
  state 丢失 → 扫过的页面重新扫一遍
    ↓
  原页面上的 content_id 已在 artifacts → 跳过 ✅
  原页面上未下载的 content_id → 重新发现 → 正常下载 ✅
    ↓
  无漏扫，无重复文件
```

#### 任务量估算

每次 Beat tick (30s) 检查一次，大约每 20 个 tick 才派发一次。DB 无压力。

#### 与同平台并发任务的关系

deep 的每个工作单元和增量任务共享 `polycrawl:ratelimit:douyin:content_fetch` 平台锁。由于每个工作单元秒级完成：

```
时间线示意（tick=30s）:
  ▸ 每30秒派发一个工作单元（扫描一页 或 下载一个作品）

  deep 工作:     [获取锁] 1次API请求(几秒) [释放锁] ✅结束
  增量任务:                                  [获取锁] 执行完毕 [释放锁]
                                             ↑ 几乎不受影响
```

- 每个工作单元只做一次平台 API 请求（页面 fetch 或单作品 detail fetch），不长期持锁
- 增量任务最多等几秒，远小于其重试超时（30s）
- 工作单元崩溃 → 下一个 Beat tick 从 Redis 进度继续

### 3.9.5 链接自适应层 (Adaptive)

每个链接（URL/account）的更新频率不同。长期无新内容的链接应阶梯式降频，避免无效扫描。

#### 阶梯降频曲线

| 闲置时间 | 扫描频率 | 间隔 | 说明 |
|---------|---------|------|------|
| < 14 天 | 2 次/天 | 12h | 正常活跃期 |
| 14 ~ 30 天 | 1 次/天 | 24h | 两周无更新 |
| 30 ~ 60 天 | 1 次/3 天 | 72h | 一个月无更新 |
| 60 ~ 180 天 | 1 次/周 | 7d | 两个月无更新 |
| ≥ 180 天 | 1 次/月 | 30d | 半年无更新，到此封顶 |

降频曲线示意（闲置天数 → 扫描间隔）：
```
间隔
 30d │                          ─────
     │                        ╱
  7d │                   ─────
     │                 ╱
  3d │            ─────
     │          ╱
  1d │     ─────
     │   ╱
 12h │──
     │
     └──────────────────────────────→ 闲置天数
       0   14   30   60          180
```

#### 配置

```jsonc
// config/base.jsonc > strategy.adaptive
"adaptive": {
  "tiers": [
    { "after": "14d", "interval": "1d" },
    { "after": "30d", "interval": "3d" },
    { "after": "60d", "interval": "7d" },
    { "after": "180d", "interval": "30d" }
  ]
}
```

- `tiers[]`：阶梯数组，按 `after` 升序排列
  - `after`：自上次有新内容以来的闲置天数（calendar days，不是扫描次数）
  - `interval`：进入该阶梯后的复查间隔
- 自动按 `after` 升序匹配，匹配第一个满足条件的阶梯，不叠加
- 最后一级即为封顶值，不再继续降频

#### 执行逻辑

自适应跟踪的粒度是**每条链接（account）**，不按 creator 聚合。同一 creator 下的不同链接可能处于不同的闲置阶梯。

```
每次执行（对单个 account 完成抓取后）:
  新增作品数 > 0 →
    Redis 记录该链接的最后活跃时间:
      polycrawl:adaptive:active:<account_id> = now
    TTL 延长（每次有更新重置 TTL）
    清除闲置标记（如有）
    下次该链接按正常 interval 执行（由所属 task 定义）

  新增作品数 = 0 →
    从 Redis 读取该链接最后活跃时间
    计算闲置天数 = now - last_active_time
    查找匹配的 tier:
      idle_days < 14  → 不用降频，继续正常 interval
      14 ≤ idle_days < 30 → next_scan = 1d
      30 ≤ idle_days < 60 → next_scan = 3d
      60 ≤ idle_days < 180 → next_scan = 7d
      idle_days ≥ 180 → next_scan = 30d
    写入 Redis: polycrawl:adaptive:idle:<account_id> = now + next_scan
    下次调度器检查此链接时跳过，直到当前时间 ≥ next_scan 时间戳

恢复活跃：
  任何一次执行发现该链接新增作品 > 0 →
    清除 idle 标记，恢复正常频率
    无需手动干预
```

#### 与任务调度层的关系

- 自适应层在**调度器分发时**生效，按 account 粒度检查
- 调度器遍历任务匹配的 accounts 时，检查每个 account 的 idle 标记：
  - 有 idle 标记且未到复查时间 → 跳过该 account
  - 无 idle 标记或已到复查时间 → 正常创建任务
- 同一 creator 下不同 account 各自独立降频

#### 组合示例

| 任务配置 | 自适应效果 |
|---------|-----------|
| incremental, 每6h, 全部creator | 活跃链接 6h/次，闲置链接逐步降到 30d/次 |
| incremental, 每1h, tag_filter=[vip] | VIP 活跃链接 1h/次，VIP 闲置链接降到 1d/次→30d/次 |
| deep, 连续运行 | 自动跳过闲置链接，节省深扫资源 |

### 3.9.6 执行优先级与合并规则

1. **tag_filter 匹配**：任务只处理满足标签条件的 creator
2. **闲置过滤**：被标记为 idle 的 account 跳过，直到 next_recheck（account 粒度）
3. **锁共享**：deep 工作单元与增量任务共享同一个 `content_fetch` 平台锁。由于每个工作单元秒级完成，不会导致增量任务饿死
4. **任务合并**：同一 account 可能匹配多个任务（如 6h 增量 + 深扫），各自独立执行
5. **幂等**：两次执行间隔短于内容更新周期时，第二次会因 content_id 已存在而快速停止（增量模式）或跳过下载（深扫模式）

#### 去重优先级（不论方案）

数据库 content_id 命中 → 跳过下载请求 > 本地文件存在 → 跳过写盘 > 正常下载

### 3.4.3 直播检测、反爬与重连策略（评审结论）

#### 进程还是线程

结论：单独进程优于单独线程。

- 原因1：录制任务为长生命周期 I/O，异常隔离需求高，进程级隔离可避免拖垮抓取任务。
- 原因2：直播拉流为长阻塞 I/O，放在线程池会占满线程资源且异常难以隔离，独立 asyncio Task 可优雅取消与错误隔离。
- 原因3：Worker 进程内通过 `_live_recording` 集合追踪录制中的账号，避免同一账号重复录制；Consumer 在任务完成后通过 `notify_live_done()` 事件通知 Scheduler 重新分配 tier。

#### 如何获取开播状态

- 主路径：`check_live_status` 轮询（平台公开接口或页面轻量探测）。
- 辅路径：若平台支持 webhook/订阅回调，优先事件驱动，轮询作为兜底。
- 状态机建议：offline -> probing -> recording -> offline/error。
- 说明：`ended` 不作为持久状态，仅作为一次性事件（`event_type=ended`）通知前端“本场已结束”，随后状态回落为 `offline`。

#### 多主播轮询与反爬风险

结论：必须重点考虑，且默认按"分片时间轮 + 抖动"执行。

- 调度策略：
  - 采用最小堆/时间轮按下次探测时间调度，避免整点齐发。
  - `check_interval_seconds` 引入随机抖动（例如 ±20%）。
  - **未开播（offline）账号维持固定间隔轮询，不做指数回退**——因为直播通常每天只持续数小时，"未开播"是正常状态而非异常，回退会显著推迟发现开播时间。
  - 指数回退仅适用于**连续探测请求失败**（网络错误、HTTP 5xx、被限速）：如 30s -> 60s -> 120s，封顶 10min；失败消除后立即恢复 `check_interval_seconds` 固定间隔。
- 限速策略：轮询请求走 `rate_control.fetch_requests_per_second` 统一节流。
- 分组策略：按平台与账号哈希分桶，分批探测，避免单 IP 突发峰值。

#### 直播中断快速重连

结论：采用"快速重试窗口 + 慢速退避"两阶段。

- 阶段1（快速恢复）：在 `recover_window_seconds` 内按 `fast_reconnect_seconds` 序列快速重连（1/2/3/5/8s）。
- 阶段2（退避恢复）：超出窗口后切换指数退避并触发 `refresh_live_stream` 重签 URL。
- 连续失败处理：达到阈值后任务标记为 failed，Scheduler 的 tier-driven timer 在下一个检测间隔重新派发 `live_record` 任务。
- 文件一致性：继续沿用"分片写 tmp -> 原子重命名"，避免中断污染正式文件。
## 3.5 鉴权与权限

- V1（内网版）不启用鉴权，默认受信网络访问。
- 鉴权（JWT + RBAC）延后到外网版本再引入。

## 3.6 关闭策略（Graceful Shutdown）

### API 服务关闭
- 收到 SIGTERM 信号后，立即停止接收新请求（如 uvicorn 的 graceful shutdown）。
- 已处理的请求在 30 秒内完成，超时则强制关闭。
- 关闭前记录当前状态到日志。

### asyncio Worker 关闭
- 收到 SIGTERM 信号后，`Scheduler.stop()` 取消所有 timer 与 live asyncio Task，Consumer 停止 BLPOP 循环。
- 当前正在执行的 asyncio Task（content_fetch / live_record）被 cancel；长阻塞的 I/O 等待会在下一次 checkpoint 处退出。
- 设置超时上限（300 秒）；超时后强制杀死 worker 进程。
- 正在执行的任务状态会在下次启动时通过 Startup Recovery 从 `running/pending` 自动恢复为 `failed`，避免状态悬挂。

### 直播录制任务中断处理
- 当 `live_record` 任务被 SIGTERM 中断时，调用 `recorder.stop()` 执行：
  - 关闭拉流进程（如 ffmpeg/streamlink）。
  - 最后一个 `.tmp` 分片不重命名，保留为残留（后续由 cleanup 任务清理）。
  - 记录中断事件到日志（包含账号、时间、已录秒数）。
- 下次 Scheduler 的 tier-driven timer 触发时，如果直播仍在进行，会重新派发 `live_record` 任务从头开始。
- 配置 docker-compose 的 `stop_grace_period` 为 350 秒，给 worker 充足时间完成中断处理。

## 3.7 .tmp 孤立文件定期清理

### 清理机制
- 后台定期扫描任务：启动一个独立 asyncio 后台任务定期扫描 `downloads` 目录。
- 清理周期：10 天（可配置，`cleanup.tmp_cleanup_days` = 10）。
- 清理规则：
  - 扫描所有 `*.tmp` 文件。
  - 若文件修改时间 > 10 天，删除该文件并记录日志。
  - 若文件修改时间 <= 10 天，跳过（可能还有任务在处理）。
- 触发时机：
  - 启动时首次扫描（异步，不阻塞 API 启动）。
  - 之后每天定时运行一次（如 03:00 UTC）。

### 实现细节
- 清理任务本身应该健壮，不因某个文件删除失败而中止整个流程。
- 记录清理摘要到日志：删除文件数、清理空间大小。
- 可在 `/config` 中暴露 `cleanup.tmp_cleanup_days` 给管理员调整（支持热更新）。

## 3.8 Provider 发现与加载机制

### 机制设计
- 配置文件驱动：根据 `creators.jsonc` 中声明的 `platform` 字段（`douyin` / `twitter` / `weibo` / `xiaohongshu`）动态加载对应 Provider。
- 运行时加载：不在启动时必须全量发现所有 Provider，而是根据实际配置使用的平台按需加载。
- 缓存机制：已加载的 Provider 缓存在内存中，避免重复加载。

### 实现流程
1. 系统启动时，检查 `packages/providers/` 目录下的可用 Provider 列表。
2. 解析配置文件中所有账号的 `platform` 字段。
3. 对每个平台，动态 import 对应的 `providers/<platform>/__init__.py`，调用其提供的工厂函数获取 Provider 实例。
4. 将 Provider 注册到 `ProviderRegistry` 中，按平台键 (e.g., `"douyin"`) 存储。
5. 当执行任务时，根据任务指定的 `platform` 查询 Registry，获取对应 Provider。
6. 若查询到未初始化的 Platform，则在查询时动态加载（延迟加载）。

### 错误处理
- 若配置文件中声明的 Platform 对应的 Provider 不存在，启动时记录警告，但不中断启动。
- 执行任务时，若 Provider 不可用，返回 `ProviderNotFoundError`，任务标记为失败。

## 3.9 监控指标设计

### 系统级指标

| 指标 | 类型 | 说明 | 采集点 |
|---|---|---|---|
| `spider_tasks_total` | Counter | 任务总数（按状态分组：pending/queued/running/success/failed/canceled） | 任务状态转移时 |
| `spider_tasks_duration_seconds` | Histogram | 任务执行时长分布（秒） | 任务完成时 |
| `spider_task_retries` | Counter | 重试次数（按任务类型分组） | 重试触发时 |
| `spider_queue_depth` | Gauge | 每个队列的待处理任务数（content/live） | 定期采样（5s） |
| `spider_worker_count` | Gauge | 在线 Worker 数量（按队列分组） | Worker 心跳时更新 |
| `spider_worker_task_processing` | Gauge | 每个 Worker 正在处理的任务数 | Worker 状态报告 |

### 下载与反爬指标

| 指标 | 类型 | 说明 | 采集点 |
|---|---|---|---|
| `spider_download_bytes` | Counter | 下载字节总数（按平台分组） | 下载完成时 |
| `spider_download_files` | Counter | 下载文件总数（按平台、媒体类型分组） | 下载完成时 |
| `spider_download_duration_seconds` | Histogram | 单文件下载耗时分布 | 下载完成时 |
| `spider_http_requests_total` | Counter | HTTP 请求总数（按 platform、endpoint、status_code 分组） | HTTP 请求时 |
| `spider_http_errors` | Counter | HTTP 错误数（按 status_code、error_type 分组：timeout/429/403/500 等） | HTTP 错误时 |
| `spider_url_expired_total` | Counter | URL 过期检测数（按平台分组） | URL 刷新时 |
| `spider_circuit_breaker_trips` | Counter | 熔断器触发次数（按平台分组） | 熔断触发时 |

### 直播指标

| 指标 | 类型 | 说明 | 采集点 |
|---|---|---|---|
| `spider_live_rooms_online` | Gauge | 当前在线直播间数量 | 检测完成时 |
| `spider_live_recording_active` | Gauge | 当前正在录制的直播数量 | 状态转移时 |
| `spider_live_recording_duration_seconds` | Histogram | 单场直播录制时长分布 | 录制结束时 |
| `spider_live_check_interval_seconds` | Histogram | 开播检测间隔耗时分布 | 检测完成时 |
| `spider_live_reconnect_attempts` | Counter | 直播中断重连尝试次数 | 重连时 |

### 存储与数据库指标

| 指标 | 类型 | 说明 | 采集点 |
|---|---|---|---|
| `spider_artifacts_total` | Counter | 入库产物总数（按平台、媒体类型分组） | 产物落盘时 |
| `spider_artifacts_deduplicated` | Counter | 去重跳过的产物数 | 去重触发时 |
| `polycrawl_db_query_duration_seconds` | Histogram | 数据库查询耗时（按操作分组：select/insert/update） | 查询完成时 |
| `polycrawl_db_connection_pool_size` | Gauge | 数据库连接池当前大小 | 连接变化时 |
| `spider_redis_operations_total` | Counter | Redis 操作数（按操作类型分组：get/set/lpush 等） | 操作完成时 |
| `spider_storage_cleanup_files_deleted` | Counter | .tmp 清理删除文件数 | 清理任务完成时 |

### 日志采集

**结构化日志字段标准：**
```json
{
  "timestamp": "ISO8601",
  "level": "INFO/WARN/ERROR",
  "logger_name": "spider.module.path",
  "event": "event_type",
  "task_id": "uuid",
  "account_id": "platform:account_identifier",
  "platform": "douyin/twitter/weibo/xiaohongshu",
  "content_id": "platform_native_id",
  "duration_ms": 123,
  "status": "success/failed/retrying",
  "error_type": "ProviderError/NetworkError/...",
  "error_message": "human readable message",
  "metadata": {
    "page_num": 1,
    "batch_size": 20,
    "retry_count": 2,
    "status_code": 429
  }
}
```

**日志事件类型：**
- `task_created` / `task_started` / `task_completed` / `task_failed`
- `fetch_started` / `fetch_completed` / `fetch_failed`
- `download_started` / `download_completed` / `download_failed`
- `live_check_started` / `live_status_changed`
- `live_record_started` / `live_record_stopped`
- `config_loaded` / `config_validation_failed`
- `provider_loaded` / `provider_error`
- `db_connection_error` / `redis_connection_error`

### 指标暴露
- 使用 `prometheus_client` 库暴露所有指标。
- 在 FastAPI 中注册 `/metrics` 端点，返回 Prometheus 格式数据。
- 在 asyncio Worker 中集成指标收集（通过自定义 Hook 函数）。
- 监控仪表板（未来 V2）：Grafana 接入 Prometheus 数据源展示。

## 4. 数据设计

核心表：

- creators：创作者实体（逻辑聚合层，镜像自配置文件）。
- accounts：平台账号实体（执行边界，镜像自配置文件）。
- tasks：任务定义与生命周期状态。
- task_runs：每次执行尝试与错误/指标。
- artifacts：下载产物记录。
- users：预留给外网版鉴权（V1 内网版可不创建）。

归类与存储策略：

- 物理存储：单层真实文件存储，不创建 creator 镜像目录，不依赖硬链接/符号链接/复制。
- 逻辑归类：通过 `accounts.creator_id` 与 `artifacts.account_id` 在数据库中完成 creator 聚合。
- 产物唯一性：建议使用 `(platform, content_id, media_kind)` 唯一约束，`file_path` 指向唯一真实文件。
- 可选完整性字段：`sha256` 用于校验与后续修复，不作为主去重键。
- 文件到数据库同步：`creators.jsonc` 热加载成功后，以幂等键进行 upsert，同步 `creators/accounts` 表，保证查询与执行口径一致。

设计意图：

- Redis 用于短期消息与异步调度；PostgreSQL 作为权威历史数据源。

### 4.1 详细数据库 Schema

#### 表 1: creators（创作者）

```sql
CREATE TABLE creators (
  id SERIAL PRIMARY KEY,
  creator_key VARCHAR(255) NOT NULL UNIQUE,  -- 全局唯一标识符，配置驱动
  display_name VARCHAR(255) NOT NULL,         -- 显示名称
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX idx_creators_creator_key ON creators(creator_key);
```

**说明：**
- `creator_key` 是从 `config/creators.jsonc` 同步来的唯一标识，全局不重复。
- 创意者是逻辑聚合层，仅用于 Web 管理和数据查询聚合，不直接驱动执行。

---

#### 表 2: accounts（平台账号）

```sql
CREATE TABLE accounts (
  id SERIAL PRIMARY KEY,
  creator_id INTEGER NOT NULL,
  platform VARCHAR(50) NOT NULL,             -- douyin/twitter/weibo/xiaohongshu
  account_type VARCHAR(50) NOT NULL,         -- profile/live
  account_url VARCHAR(500) NOT NULL,         -- 完整 URL（用于查询验证）
  platform_account_id VARCHAR(255),          -- 平台原生 ID（如抖音 sec_user_id，首次抓取后回填）
  account_alias VARCHAR(255),                -- 账号别名（可选）
  
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  FOREIGN KEY (creator_id) REFERENCES creators(id) ON DELETE CASCADE,
  UNIQUE(platform, platform_account_id)     -- 同平台相同账号唯一
);

-- 索引
CREATE INDEX idx_accounts_creator_id ON accounts(creator_id);
CREATE INDEX idx_accounts_platform_type ON accounts(platform, account_type);
CREATE INDEX idx_accounts_url ON accounts(account_url);
CREATE INDEX idx_accounts_platform_id ON accounts(platform, platform_account_id);
```

**说明：**
- `account_type` 区分 `profile`（内容账号）和 `live`（直播账号）。
- `platform_account_id` 初始为 NULL，首次执行时由 Provider 解析 URL 得到平台主键并回填。
- 幂等键：`(platform, account_type, platform_account_id)` 或规范化后的 `account_url`。

---

#### 表 3: tasks（任务定义）

```sql
CREATE TABLE tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  
  account_id INTEGER NOT NULL,
  task_type VARCHAR(50) NOT NULL,            -- content_fetch/live_monitor/live_record
  status VARCHAR(50) NOT NULL,               -- pending/queued/running/success/failed/canceled/retrying
  
  -- 执行参数
  params JSONB NOT NULL,                     -- { "mode": ["post"], "number": 0, ... }
  
  -- 控制参数
  retry_count INTEGER DEFAULT 0,
  max_retries INTEGER DEFAULT 3,
  
  -- 时间戳
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  
  -- 追踪信息
  queue_key VARCHAR(255),               -- 任务队列标识（如 task_0:douyin）
  error_message TEXT,
  
  FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
);

-- 索引
CREATE INDEX idx_tasks_account_id ON tasks(account_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_task_type ON tasks(task_type);
CREATE INDEX idx_tasks_created_at ON tasks(created_at DESC);
CREATE INDEX idx_tasks_queue_key ON tasks(queue_key);
```

**说明：**
- `status` 流转：`pending` → `queued` → `running` → `success`/`failed` 或 `canceled`/`retrying`。
- `params` 存储任务参数（如限速、下载范围等），JSON 格式便于灵活扩展。
- `queue_key` 存储 Redis 队列标识（如 `task_0:douyin`），用于追踪任务执行队列。

---

#### 表 4: task_runs（任务执行记录）

```sql
CREATE TABLE task_runs (
  id SERIAL PRIMARY KEY,
  
  task_id UUID NOT NULL,
  run_number INTEGER NOT NULL,               -- 执行次数序号（第 1 次、第 2 次等）
  
  status VARCHAR(50) NOT NULL,               -- success/failed/timeout/aborted
  
  -- 执行时间
  started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP,
  duration_seconds NUMERIC,
  
  -- 结果指标
  items_fetched INTEGER,                     -- 抓取内容数
  items_downloaded INTEGER,                  -- 成功下载数
  items_failed INTEGER,                      -- 下载失败数
  items_skipped INTEGER,                     -- 跳过数（已存在或重复）
  bytes_downloaded BIGINT,                   -- 下载字节数
  
  -- 错误信息
  error_type VARCHAR(100),                   -- ProviderError/NetworkError/StorageError/...
  error_message TEXT,
  error_detail JSONB,                        -- 详细错误信息（堆栈、状态码等）
  
  -- 日志追踪
  log_entry_id VARCHAR(255),                 -- 关联的日志 ID（便于追踪）
  
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  UNIQUE(task_id, run_number)
);

-- 索引
CREATE INDEX idx_task_runs_task_id ON task_runs(task_id);
CREATE INDEX idx_task_runs_status ON task_runs(status);
CREATE INDEX idx_task_runs_completed_at ON task_runs(completed_at DESC);
```

**说明：**
- 一条 Task 对应多条 TaskRun（重试时产生新 run 记录）。
- 记录详细执行指标，便于统计与调试。

---

#### 表 5: artifacts（下载产物）

```sql
CREATE TABLE artifacts (
  id SERIAL PRIMARY KEY,
  
  account_id INTEGER NOT NULL,
  task_id UUID,
  
  -- 内容标识
  platform VARCHAR(50) NOT NULL,
  content_id VARCHAR(255) NOT NULL,         -- 平台原生内容 ID（如抖音 aweme_id）
  media_kind VARCHAR(50) NOT NULL,          -- video/image/audio/metadata
  
  -- 文件信息
  file_path VARCHAR(500) NOT NULL,          -- 相对于 MEDIA_BASE_PATH 的路径
  file_size BIGINT,
  sha256 VARCHAR(64),                       -- 文件哈希（校验与去重）
  
  -- 元数据
  title VARCHAR(255),
  author VARCHAR(255),
  publish_date TIMESTAMP,
  download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  -- 状态
  status VARCHAR(50) DEFAULT 'completed',   -- completed/failed/partial
  
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
  
  -- 唯一约束：同一账号、同一内容、同一媒体类型只有一条记录
  UNIQUE(account_id, platform, content_id, media_kind)
);

-- 索引
CREATE INDEX idx_artifacts_account_id ON artifacts(account_id);
CREATE INDEX idx_artifacts_platform_content_id ON artifacts(platform, content_id);
CREATE INDEX idx_artifacts_download_date ON artifacts(download_date DESC);
CREATE INDEX idx_artifacts_status ON artifacts(status);
CREATE INDEX idx_artifacts_file_path ON artifacts(file_path);
```

**说明：**
- 去重键：`(account_id, platform, content_id, media_kind)` 确保同一内容不重复下载。
- `file_path` 指向物理存储位置（相对路径）。
- `sha256` 用于完整性校验和冲突检测，但不是主去重键。
- 同一 `content_id` 可能有多条记录（不同 `media_kind`：视频、封面、音乐等）。

---

#### 表 6: live_statuses（直播状态快照）

```sql
CREATE TABLE live_statuses (
  id SERIAL PRIMARY KEY,
  
  account_id INTEGER NOT NULL,
  
  -- 当前状态
  status VARCHAR(50) NOT NULL,               -- offline/probing/recording/error
  status_since TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  -- 录制进度（仅当 status=recording 时有效）
  current_recording_session_id UUID,        -- 关联的 live_record 任务 ID
  recorded_seconds INTEGER,                 -- 已录秒数
  recorded_bytes BIGINT,                    -- 已录字节数
  
  -- 错误信息（仅当 status=error 时有效）
  error_message TEXT,
  error_time TIMESTAMP,
  
  -- 时间戳
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
  UNIQUE(account_id)
);

-- 索引
CREATE INDEX idx_live_statuses_account_id ON live_statuses(account_id);
CREATE INDEX idx_live_statuses_status ON live_statuses(status);
```

**说明：**
- 记录每个直播账号的当前状态，用于 `/live/status` 快速查询。
- 作为状态快照表，在状态转移时更新；不保存完整的状态历史（历史由日志记录）。

---

#### 表 7: live_sessions（直播录制会话）

```sql
CREATE TABLE live_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  
  account_id INTEGER NOT NULL,
  
  -- 会话时间
  started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  ended_at TIMESTAMP,
  
  -- 录制产物
  output_file_path VARCHAR(500),            -- 最终合并后的文件路径（可选）
  total_duration_seconds INTEGER,
  total_bytes BIGINT,
  segment_count INTEGER,                    -- 录制分片数量
  
  -- 状态
  status VARCHAR(50),                       -- completed/interrupted/failed
  error_message TEXT,
  
  FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
);

-- 索引
CREATE INDEX idx_live_sessions_account_id ON live_sessions(account_id);
CREATE INDEX idx_live_sessions_started_at ON live_sessions(started_at DESC);
```

**说明：**
- 记录每场直播的录制会话，便于查看录制历史和产物管理。
- 与 `artifacts` 的区别：`live_sessions` 记录录制过程，`artifacts` 记录最终产物。

---

#### 表 8: config_versions（配置版本历史）（可选，用于审计）

```sql
CREATE TABLE config_versions (
  id SERIAL PRIMARY KEY,
  
  version_number INTEGER NOT NULL,
  config_content JSONB NOT NULL,            -- 完整合并后的配置
  
  changed_by VARCHAR(100),                  -- 修改者（V1 内网版可为 NULL）
  change_reason TEXT,                       -- 修改原因
  
  changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  is_active BOOLEAN DEFAULT TRUE
);

-- 索引
CREATE INDEX idx_config_versions_version_number ON config_versions(version_number DESC);
CREATE INDEX idx_config_versions_changed_at ON config_versions(changed_at DESC);
```

**说明：**
- 可选表，用于记录配置变更历史（V1 可不创建，V2 审计时引入）。

---

### 4.2 数据同步策略

#### 配置文件与数据库的幂等同步

当 `config/creators.jsonc` 热加载成功后：

1. 解析所有 creator 和 account 条目。
2. 以幂等键 `(creator_key)` 对 `creators` 表执行 `UPSERT`。
3. 以幂等键 `(creator_id, platform, account_type, normalized_account_url)` 对 `accounts` 表执行 `UPSERT`。
4. 若 `platform_account_id` 为空（新增账号），暂时保留为 NULL，首次任务执行时由 Provider 回填。
5. 删除配置文件中不存在但数据库中存在的记录（逻辑删除或硬删除由业务决定）。

**事务性：** 整个同步过程在一个数据库事务中完成，确保一致性。

#### 任务执行与 TaskRun/Artifacts 的流程

1. API 创建 Task → `tasks.status = 'pending'`。
2. Redis 队列入队 → `tasks.status = 'queued'`。
3. Worker 执行 → 新增 TaskRun 记录 → `tasks.status = 'running'`。
4. 每次下载完成 → 新增 Artifact 记录。
5. 任务完成 → 更新 TaskRun 指标 → `tasks.status = 'success'`。
6. 任务失败 → `tasks.status = 'failed'` → 如果设置了重试，生成新 TaskRun 并重试。

---

## 5. API 设计（V1）

- Tasks
  - POST /tasks
  - GET /tasks
  - GET /tasks/{id}
  - GET /tasks/{id}/logs
  - GET /tasks/{id}/artifacts
  - POST /tasks/{id}/cancel
- Creators & Accounts
  - GET /creators
  - POST /creators
  - PUT /creators/{creator_key}
  - DELETE /creators/{creator_key}
  - POST /creators/{creator_key}/accounts
  - PUT /creators/{creator_key}/accounts/{account_id}
  - DELETE /creators/{creator_key}/accounts/{account_id}
- Live
  - GET /live/status（所有 live 账号当前状态快照）
  - WS /live/ws（WebSocket，推送状态变更事件）
  - POST /live/records/{account_id}/stop（停止指定账号当前录制，幂等）
- Config
  - GET /config/effective
  - PUT /config
- Health
  - GET /health

说明：

- Creators & Accounts 的 CRUD 操作直接修改 `creators.jsonc`（配置文件为权威源），服务监听并热加载。
- `/live/ws` 推送采用“先快照、后增量”协议：
  - 首帧 `snapshot`：返回当前所有 live 账号状态。
  - 增量帧 `event`：仅推送状态变化。
  - 统一字段：`event_type`、`sequence`、`event_time`、`account_id`、`status`、`since`。
  - `status` 枚举统一为：`offline` / `probing` / `recording` / `error`。
  - `ended` 作为事件类型存在（`event_type=ended`），不进入 `status` 枚举。
  - 心跳帧：`heartbeat`（30s），前端断线重连后先拉 `GET /live/status` 再续收事件。
- `POST /live/records/{account_id}/stop` 语义：若无在录任务返回成功（幂等）；若存在则取消该账号当前 `live_record` 任务并返回停止结果。
- `PUT /config` 语义：仅允许白名单字段；写回对应配置文件并触发热加载，成功后返回新 `config_version` 与生效时间戳。

## 6. Web 管理台

### 6.0 页面需求评审结论（2026-05-24，第一轮）

以下为已确认结论，后续页面细化需遵守：

1. 第一优先细化页面：账号页面（Creators/Accounts 管理）。
2. 信息架构：单页 Tab 形态（在一个管理台内分栏切换）。
3. 接口文档策略：中英文双语文档（需支持语言切换，不仅限 Swagger 默认英文 UI）。
4. 页面密度：平衡型（卡片 + 表格混合），兼顾可读性与效率。

说明：本节为“需求评审结论记录”，每轮确认后持续追加，并同步更新实现文档。

### 6.0.1 页面需求评审结论（2026-05-24，第二轮：账号页面）

账号页面已确认结论：

1. 主布局：单表格混合展示 Creator 与 Account。
2. 首批必须操作：
  - 新增 Creator
  - 编辑 Creator 显示名
  - 新增 Account
  - 编辑 Account
  - 删除 Account
  - 删除 Creator（带二次确认）
3. Account 列表默认字段：`account_id`、`creator_display_name`、`platform/type`、`account_url`、`updated_at`。
4. 首批筛选条件：Creator 名搜索、平台筛选、类型筛选、URL 模糊搜索、状态筛选（启用/停用）。
5. 编辑交互：行内编辑。

未决项：

1. 字段语义定义（用户要求先明确当前显示字段含义，再进入页面交互细节）。

### 6.0.2 字段语义确认（2026-05-24）

针对“账号页面字段”已确认如下定义：

1. `creator_key`：配置文件主标识（来自 `config/creators.jsonc`），用于配置层稳定识别 Creator。
2. `creator_id`：数据库内部主键（`creators.id`，自增），由同步入库时生成，不存在于配置文件。
3. `account_id`：数据库内部主键（`accounts.id`，自增），由同步入库时生成，不存在于配置文件。
4. `account_url`：配置层账号 URL 字段，和 `platform/type` 一起用于账号匹配与同步。

界面展示约束：

1. `display_name` 是账号卡片首要视觉信息。
2. `creator_id/account_id` 仅作为运维排障辅助信息，视觉层级必须低于 `display_name`。

终端用户展示原则（新增确认）：

1. 管理台账号页面默认不展示 `creator_key`、`creator_id`、`account_id` 等技术字段。
2. 管理台账号页面默认不展示原始完整 URL 文本。
3. 账号访问通过操作按钮提供（例如“访问主页”），而非直接暴露完整链接字符串。

### 6.0.3 页面需求评审结论（2026-05-24，第三轮：Creator 聚合视图）

已确认结论：

1. 主界面默认展示 Creator 列表（非账号平铺列表）。
2. Creator 行为：点击/展开后显示其在各平台下的多个账号链接。
3. 未展开时展示聚合统计信息，包括：
  - 涉及平台（platform 列表）
  - 已下载作品总数
  - 作品总容量
  - 最近更新作品时间
4. 链接展示策略：不直接暴露原始 URL 文本，仅提供“访问链接”按钮。

接口契约新增：

1. `GET /creators/summary`
2. 返回维度：
  - `display_name`
  - `platforms[]`
  - `downloads_count`
  - `total_bytes`
  - `last_updated_at`
  - `platform_groups[]`（每个平台下的多个链接项）

补充展示规则（本轮确认）：

1. Creator 列表采用“一行一个 Creator”的简洁视图，不使用大卡片块状排布。
2. 平台（如 douyin）属于链接归属信息，不与 Creator 名称同级突出展示。
3. 平台信息下沉到展开详情中，以“按平台分组的链接列表”呈现。

交互与时间语义补充（本轮确认）：

1. Creator 展开状态在自动刷新时应保持，不得因轮询刷新自动折叠。
2. `最近更新` 语义统一为“最近作品发布时间”（`publish_date`），不使用页面抓取时间。
3. 展开后每个链接项需展示该链接的最近作品更新时间（同样基于 `publish_date`）。

### 6.1 页面清单（MVP 范围）

| 页面 | 路由 | 核心功能 |
|---|---|---|
| 任务页 | `/tasks` | 创建任务、筛选列表、取消、查看详情与日志 |
| 创作者页 | `/creators` | 管理 creator + account，支持新增/编辑/删除 |
| 直播监控页 | `/live` | 所有直播间实时状态，WebSocket 推送 |
| 配置页 | `/config` | 查看生效配置、白名单字段热更新 |

健康态：顶部全局状态栏（轮询 `/health`），显示 Worker 在线/离线。

非 MVP（V2 迭代）：总览 Dashboard、产物浏览页、系统详情页（队列深度/Worker 心跳）。

### 6.2 各页面功能详述（MVP）

#### 任务页
- 列表：类型筛选（content_fetch / live_monitor / live_record）、状态筛选、时间范围。
- 创建任务：选平台 → 选账号 → 填参数（表单）。
- 任务详情抽屉：运行日志、产物列表、重试历史。
- 操作：取消、手动重试。

#### 创作者页
- 创作者列表，展开显示旗下账号（platform / type / alias / 状态）。
- 新增 creator：
  - 输入 `display_name`（昵称）。
  - 系统自动生成 `creator_key`（随机字符串，如 `creator_a3k2m9x1`）并立即写入配置文件，用户无需输入或修改。
  - 示例：输入"李明" → 系统生成 `creator_key: creator_a3k2m9x1` 并保存。
- 新增 account：选 platform、type（profile / live）、填 URL、alias；type=live 可填可选覆盖字段（`check_interval_seconds` / `max_duration_minutes`）。
- 编辑 / 删除 account：行内操作，删除前需确认。
- 编辑 creator 名称（display_name）：修改后立即写入 `creators.jsonc`。
- 说明：所有编辑直接写入 `creators.jsonc`，保存成功后由配置热加载机制立即生效。

#### 直播监控页
- 状态卡片表格：房间别名、平台、当前状态（offline / probing / recording / error）、状态持续时长。
- 实时更新：WebSocket 连接 `/live/ws`，状态变更时推送，无需手动刷新。
- 操作：手动触发检测（发 `live_monitor` 任务）、停止录制（调用 `POST /live/records/{account_id}/stop`）。

#### 配置页
- 展示合并后的完整生效配置（只读 JSON 树）。
- 白名单字段行内编辑 + 提交（调用 `PUT /config`）：
  - `rate_control.fetch_requests_per_second`
  - `rate_control.download_requests_per_second`
  - `anti_bot.retry_policy.max_retries`
  - `download.thread_count`
  - `download.retry_times`
  - `anti_bot.proxy`
  - `anti_bot.proxy_pool`

### 6.3 V2 页面规划

#### 总览页（Dashboard）`/dashboard`
- 数据卡片：当前录制中直播数、今日成功任务数、今日失败任务数、队列积压量。
- 最近失败任务列表（快速跳转到任务详情）。
- Worker 在线状态（content-worker / live-worker 各自心跳时间）。

#### 产物浏览页（Artifacts）`/artifacts`
- 按创作者 / 平台 / 日期多维筛选。
- 列表展示：文件名、平台、创作者、文件大小、下载时间、sha256。
- 支持按 `content_id` 或文件名搜索。
- 不提供在线预览（V1/V2 均不做）。

#### 系统详情页（System）`/system`
- 任务队列深度：`content` 队列 / `live` 队列各自积压数。
- Worker 心跳列表：每个 worker 实例的最后活跃时间、处理中任务数。
- DB / Redis 健康检查状态与响应延迟。
- 日志级别临时调整（运行时热更新，不持久化）。

## 7. 部署设计

### 7.1 Docker Compose Services

| Service | 镜像 | 职责 |
|---|---|---|
| `nginx` | nginx:alpine | 静态资源托管（React SPA）+ 反向代理 `/api` 到 `api` |
| `api` | app/api | FastAPI 服务，对外暴露 REST 接口 |
| `content-worker` | app/worker | asyncio Worker，消费 `content` 队列，执行抓取与下载任务 |
| `live-worker` | app/worker | asyncio Worker，消费 `live` 队列，执行开播检测与录制任务；录制时在进程内直接拉流写文件（HLS 分片），无外部依赖 |
| `redis` | redis:7-alpine | 任务队列（Redis List）+ Cookie 验证状态缓存 + 直播状态缓存 |
| `postgres` | postgres:15-alpine | 权威历史数据存储 |

说明：

- `content-worker` 与 `live-worker` 使用同一镜像，通过启动参数（`--queues content` / `--queues live`）分别绑定队列，实现资源隔离而不增加镜像维护成本。
- `live-worker` 容器重启会中断所有正在进行的拉流录制；`live_monitor` 在下次检测周期会重新发现并拉起录制。
- 存储卷：`content-worker` 与 `live-worker` 挂载同一个 NAS 存储卷（`/data`），确保产物路径一致。

### 7.2 环境变体

- 本地开发：docker-compose + dev override（热重载，api/worker 挂载源码）。
- 群晖：deploy/synology 专用 compose 与环境模板（卷路径映射到 DSM 共享文件夹）。
- Web 通过 Nginx 处理 SPA fallback，并代理 `/api` 到 `api`。

## 8. 可靠性与可观测性

- 结构化日志：structlog。
- 健康检查：DB + Redis。
- 重试策略：Task 级重试计数 + Provider 策略引擎重试。
- 失败隔离：熔断器避免短时大面积封禁。

## 9. 决策落地说明

- 执行模式采用“分页抓取 + 分页下载”，避免一次性积压大量时效链接。
- 下载速率调整为“按平台独立配置”，与抓取速率分离管理。
- URL 过期（403/410）采用“刷新 URL 后单次重试”，并记录过期率指标。
- 直播能力采用"creator 配置驱动 + 独立 live 进程队列"模式，不入库（直播数据实时短效，无持久化价值）。
- 任务粒度采用“时间窗口切片子任务”，用于长周期用户抓取与失败重跑。
- 代理策略维持“固定代理/无代理”，暂不引入代理池健康评分。
- 同一博主多账号采用“creator/account 双层模型”，仅在数据库做逻辑归类，物理文件保持单份存储。

## 10. 下一阶段实施建议（确认后执行）

- Step 0：新增 `accounts[].type` 与 `accounts[].live` 配置结构，并完成 URL 解析校验（`profile/live`）。
- Step 0.1：落地 live 专用队列与 worker 进程（与内容抓取 worker 解耦）。
- Step 0.2：实现开播检测调度器（时间轮 + 抖动 + 失败退避（非 offline））。
- Step 1：将 Douyin 改为"分页抓取 + 分页下载"流式执行（对应 1-B）。
  - 1a：`_fetch_user_posts` 改为逐页 yield，每页 fetch 完立即触发该页的 `download_all`。
  - 1b：fetch 阶段对每个 content_id 先查 DB，已存在则直接跳过，不发下载请求（数据库级去重）。
  - 1c：启动时/任务前扫描清理孤立 `.tmp` 文件。
  - 1d：实现 look-ahead 翻页窗口（`look_ahead_pages`）与 deep 单页调度（`tick`）。
- Step 2：实现 URL 过期自动刷新回调，并打点统计过期率（对应 3-A）。
- Step 3：落地平台级独立限速配置（抓取与下载双通道，默认值按平台维护）（对应 2-C）。
- Step 4：引入“时间窗口切片子任务”模型与调度逻辑（对应 4-B）。
- Step 5：维持固定代理策略并补齐 Alembic/回归测试（对应 5-A）。
- Step 6：新增 creator/account 数据模型与管理接口，任务执行仍按 account 维度，列表与查询支持 creator 聚合视图。

---

## 3.10 Cookie 管理系统（已实现）

平台 Cookie 是内容抓取和直播录制的唯一认证方式，Cookie 管理系统负责存储、验证、失效三个闭环。

### 3.10.1 存储设计

- Cookie 以键值对形式明文存储于 `config/sites/{platform}.jsonc` 的 `platform.cookies` 对象。
- 保存时同时写入 `platform.saved_at` 时间戳（ISO8601）。
- 写入使用 `update_jsonc_key()` 函数，原子替换，保留文件内所有注释。

### 3.10.2 验证状态设计

Cookie 验证状态**独立存储于 Redis**，与配置文件解耦：

```
key:   polycrawl:cookies:verify:{platform}
value: {"verified_ok": bool, "verified_at": int (Unix timestamp)}
TTL:   604800 秒 (7 天)
```

解耦的理由：
- 粘贴新 Cookie 时不会覆盖上一次的验证结果（通过 API 写配置 + 清 Redis 验证态两步分离）。
- Worker 执行器可以在认证失败时独立调用 `invalidate(platform)` 标记失效，不需要写配置文件。
- 验证态过期（TTL）自动清除，不需要手动维护。

### 3.10.3 API 接口

| 接口 | 行为 |
|---|---|
| `POST /login/cookies` | 写入 JSONC (`update_jsonc_key`) + 清除 Redis 验证态 (`clear(platform)`) |
| `GET /login/status` | 扫描 `sites/*.jsonc`，合并 Redis 验证态，返回 `{has_cookies, saved_at, verified_ok, verified_at, critical}` |
| `POST /login/verify` | 主动测试 Cookie 是否有效，结果写 Redis (`set_verified`) |

### 3.10.4 各平台验证逻辑

- **Xiaohongshu**：调用 `XHSignatureSigner` 签名后请求 `/api/sns/web/v2/user/me`，判断返回 `data.guest == false`。
- **Douyin**：请求 Douyin 主页，200 且正文长度 > 1 KB 视为有效。
- **Weibo**：请求 `m.weibo.cn/api/config`，JSON 中含 `uid` 字段视为有效。

### 3.10.5 Worker 自动失效

三个 Provider 在认证失败时均自动调用 `invalidate(platform)`：

- **Douyin**：`fetch_post_page()` 检测 `status_code == 6`（Cookie 过期信号）。
- **Weibo**：`_fetch_page_json()` 检测 `ok < 0` 或含 `login/auth/cookie/token` 关键词。
- **Xiaohongshu**：`fetch_notes()` 检测 `success == false` 且含 `verify/login/auth/expired/461/471` 关键词。

---

## 3.11 事件驱动的调度架构（已实现）

调度系统是整个项目的核心。整体设计为**事件驱动 + 定时器驱动双轨模型**，在单一 asyncio Worker 进程内运行，无外部调度进程。

### 3.11.1 整体架构

```
Worker 进程 (services/worker/run.py asyncio main())
├── Scheduler.start()                ← 调度器（事件驱动 + 定时器）
│   ├── _register_all()              ← content_fetch 定时器注册
│   ├── _init_live_tiers()           ← live_record tier 分级初始化
│   ├── _handle_live_events()        ← 事件循环（阻塞在 Queue.get()）
│   └── _handle_config_events()      ← 配置热加载监听
│
└── Consumer.start()                 ← 队列消费器（BLPOP 循环）
    ├── content_executor             ← 内容抓取执行器
    └── live_executor                ← 直播录制执行器
```

Scheduler 与 Consumer 通过两个解耦通道交互：
1. **Redis List `task_{idx}`**：Scheduler 推任务 → Consumer 消费（单向）
2. **`_live_event_queue` asyncio.Queue**：Consumer 完成后通知 Scheduler（反向事件回调）

### 3.11.2 Content Fetch — 定时器驱动

```
base.jsonc schedules[] 中每个 enabled 的 content_fetch 条目
  └── _schedule_next(idx, entry, state)
        └── loop.call_later(interval_secs, lambda: create_task(_dispatch()))
              └── _dispatch()
                    ├── 查询 scheduled=True 的 accounts
                    ├── 对每个 account 创建 Task 记录 (tasks 表)
                    └── LPUSH task_{idx} {task_id, account_id, task_type, ...}
```

- 每次触发后立即重新调用 `_schedule_next()` 注册下一次，形成自持续定时器链。
- `tasks.queue_key` 存储 Redis 队列标识（如 `task_0:douyin`）。

### 3.11.3 Live Record — Tier-driven 事件架构

**核心思路**：以账号维度的"闲置时长"分级，闲置越久检测间隔越长。无轮询，无 pub/sub，所有行为由事件队列驱动。

**初始化流程**（启动或配置热加载时）：
1. 从 DB 查询所有 `account_type == "live"` 的账号 ID。
2. 批量从 Redis MGET `polycrawl:live:last_live:{aid}` 时间戳（miss 则 fallback 到 `live_sessions` 表最近一条）。
3. 按 `idle_seconds = now - last_live_ts` 调用 `_tier_for_idle()` 匹配 `base.jsonc → strategy.live.adaptive.tiers[]`。
4. 为每账号调用 `_start_live_timer(aid, delay)`，设置 `asyncio.call_later`，届时向 `_live_event_queue` 推 `("timer", aid)`。

**事件循环** (`_handle_live_events()`)：
```
永久阻塞于 _live_event_queue.get()
  批量排空积压事件
  ("timer", aid):
    if aid not in _live_recording:
      LPUSH task_{live_idx} {task_id, account_id, "live_record"}
      _live_recording.add(aid)          ← 防重复
  ("done", aid):
    _live_recording.discard(aid)
    读 Redis last_live → 重新计算 tier → _start_live_timer(aid, tier_interval)
```

**录制完成通知链**：
```
Consumer._execute() 完成 live_record 任务
  └── notify_live_done(account_id)
        └── _live_event_queue.put_nowait(("done", account_id))
```

**Tier 配置示例**：
```jsonc
"strategy": {
  "live": {
    "adaptive": {
      "enabled": true,
      "tiers": [
        { "after": "0d",   "interval": "30s" },   // 刚播完/首次，30 秒后再检测
        { "after": "2h",   "interval": "5m"  },   // 2 小时未播，每 5 分钟检测
        { "after": "12h",  "interval": "30m" },   // 12 小时未播，每 30 分钟检测
        { "after": "3d",   "interval": "4h"  }    // 3 天未播，每 4 小时检测
      ]
    }
  }
}
```

### 3.11.4 配置热加载的调度器重建

配置文件变更 → Watchdog 触发 `_reload()` → 完整重建调度器：

```
_reload():
  1. 取消所有 content_fetch timer
  2. 取消所有 live 账号 timer
  3. 清空 _live_tier_groups、_live_recording
  4. 重新加载配置 + DB 同步
  5. _register_all(new_state)       ← 重新注册 content_fetch timer
  6. _cache_live_config(new_state)  ← 重新缓存 live 配置
  7. _init_live_tiers(new_state)    ← 重新分级所有账号
```

热加载后，所有调度状态完全重建，等同于重启但无进程重启开销。

### 3.11.5 Startup Recovery

Worker 启动时执行清理，防止异常退出后状态悬挂：

```python
# 将 DB 中 pending/running 的任务全部标为 failed
# 清空 Redis 中所有 task_{idx} 队列残留
# 将 live_statuses 中 recording/probing 重置为 offline
# 将 live_sessions 中 status=active 的会话标为 interrupted
```

---

## 3.12 Web 管理台（已实现）

管理台基于 Vanilla JS（无框架），通过 REST API + WebSocket 双通道与后端交互。

### 3.12.1 功能页面

| Tab | 文件 | 核心功能 |
|---|---|---|
| 创作者 | `creators.js` | Creator 卡片列表；自定义标签（Excel 下拉过滤）；多选批量操作；拖拽排序（`PUT /creators/reorder`）；per-platform 链接增删 |
| 概览 | `overview.js` | 系统健康状态（config/database）；Cookie 过期警告；近期失败任务（含错误消息）；汇总统计 |
| 任务 | `tasks.js` | 任务列表（100 条）；状态 badge；失败任务点击展开 error_message；手动创建任务；一键重试 |
| 直播 | `live.js` | 直播状态列表；recording/offline/probing 状态；一键停止录制 |
| 调度 | `schedules.js` | 编辑 `base.jsonc schedules[]`（enabled/interval/start_at）；`PUT /schedules` 写回 |
| 日志 | `logs.js` | 实时日志流（`/ws/logs` WS 初始加载 + REST 补全）；Level 层级过滤（选 INFO = 显示 INFO+WARNING+ERROR） |
| 登录管理 | `login.js` | per-platform Cookie 粘贴；各平台 Cookie 字段模板；主动验证（`POST /login/verify`）；过期状态 badge |
| 文档 | (内嵌) | 静态使用说明 |

### 3.12.2 WebSocket 接口

| 端点 | 方向 | 内容 |
|---|---|---|
| `/ws/logs` | 服务器 → 客户端 | 结构化日志流（`{level, message, timestamp, logger}`） |
| `/ws/events` | 服务器 → 客户端 | 任务状态变更事件、直播状态变更事件（`{type, payload}`） |

前端建立双 WebSocket 连接，日志页订阅 `/ws/logs`，其余页面通过 `/ws/events` 接收实时更新。

### 3.12.3 完整 API 端点列表

```
# 基础
GET    /health
GET    /platforms                               # 已实现 Provider 列表

# Creator 管理
GET    /creators
POST   /creators
PATCH  /creators/{creator_key}/tags             # 更新标签（写回 creators.jsonc）
POST   /creators/{creator_key}/links            # 添加账号链接
DELETE /creators/{creator_key}/links?account_url=...
PUT    /creators/reorder                        # 重排 creators.jsonc 数组顺序
GET    /creators/summary                        # 含 files_count, works_count, total_bytes

# 配置
GET    /config/effective

# 账号
GET    /accounts
PATCH  /accounts/{account_id}/scheduled         # 启用/禁用账号调度（写回 creators.jsonc + DB）

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
POST   /login/cookies                           # 写入 platform cookies + 清验证态
GET    /login/status                            # Cookie 状态 + Redis 验证结果
POST   /login/verify                            # 主动测试 cookies

# WebSocket
WS     /ws/logs
WS     /ws/events

# 静态资源
GET    /                                        # 服务 index.html
GET    /assets/*
```


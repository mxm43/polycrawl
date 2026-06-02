# PolyCrawl — NAS Docker 部署指南

> 适用环境：Synology DSM、QNAP QTS/QuTS hero、Unraid、TrueNAS Scale 等
> 及任何 Linux 主机（Ubuntu / Debian / CentOS）

## 目录

1. [前提条件](#1-前提条件)
2. [快速部署](#2-快速部署)
3. [首次启动与验证](#3-首次启动与验证)
4. [NAS 共享文件夹配置（可选）](#4-nas-共享文件夹配置可选)
5. [常用运维操作](#5-常用运维操作)
6. [故障排查](#6-故障排查)
7. [参考架构](#7-参考架构)

---

## 1. 前提条件

| 组件 | 要求 | 说明 |
|------|------|------|
| **Docker** | v24+ | NAS 套件中心安装 Container Manager（Synology）或 Container Station（QNAP） |
| **Docker Compose** | v2.20+ | 一般随 Container Manager 自带，或 `sudo apt install docker-compose-plugin` |
| **内存** | ≥ 4 GB | PostgreSQL + Redis + Worker + API 约需 2 GB，剩余用于爬虫任务 |
| **磁盘** | ≥ 20 GB | 系统 5 GB + PostgreSQL 5 GB + 媒体文件根据需要增长 |
| **网络** | 可访问外网 | 爬虫需要访问抖音/小红书/微博 API |

### 1.1 NAS 上启用 SSH

Synology DSM：
```
控制面板 → 终端机和 SNMP → 启用 SSH 服务
```

QNAP QTS：
```
控制台 → 网络 & 文件服务 → Telnet / SSH → 允许 SSH 连接
```

---

## 2. 快速部署

### 2.1 复制项目到 NAS

**方式 A：Git Clone（推荐）**
```bash
# 在 NAS 共享文件夹中创建项目目录
cd /volume1/docker    # Synology 典型路径
mkdir spider
cd spider

# 克隆项目（需先在 GitHub 上配置好 SSH key 或使用 token）
git clone https://github.com/your-org/polycrawl.git .
```

**方式 B：File Station 上传**
通过 NAS Web 管理界面将项目文件夹上传到 `/volume1/docker/spider/`。

### 2.2 配置环境变量

```bash
cd /volume1/docker/spider
cp deploy/.env.example .env
# 编辑 .env，修改 POSTGRES_PASSWORD 等敏感字段
nano .env
```

**最小配置**只需修改密码：
```ini
POSTGRES_PASSWORD=YourStrongPassword123
```

### 2.3 构建镜像

```bash
docker compose -f deploy/docker-compose.yml build
```

构建时间约 2-5 分钟（具体取决于 NAS CPU 性能）。

### 2.4 启动所有服务

```bash
docker compose -f deploy/docker-compose.yml up -d
```

此命令会按依赖顺序启动：`postgres` → `redis` → `api` + `worker`。

首次启动时，API 容器会自动执行数据库迁移（`alembic upgrade head`）。

### 2.5 验证服务状态

```bash
# 查看所有容器状态
docker compose -f deploy/docker-compose.yml ps

# 查看实时日志
docker compose -f deploy/docker-compose.yml logs -f

# 单独查看 API 日志
docker compose -f deploy/docker-compose.yml logs -f api

# 健康检查
curl http://localhost:8000/health
```

预期返回：
```json
{"status":"ok","database":"ok","redis":"ok"}
```

---

## 3. 首次启动与验证

### 3.1 访问 Web 管理界面

打开浏览器访问：`http://<NAS_IP>:8000`

如果 NAS 端口 8000 被占用，修改 `.env` 中的 `API_PORT=8001` 后重启：
```bash
docker compose -f deploy/docker-compose.yml up -d
```

### 3.2 上传 Cookie

Web 界面 → 「登录管理」Tab → 依次为抖音/小红书/微博 上传 Cookie。

> Cookie 格式：JSON 键值对，如 `{"sessionid": "abc123", "uid": "12345"}`

### 3.3 配置创作者

编辑 `config/creators.jsonc`，添加要跟踪的创作者账号。

修改后无需重启 — 配置文件变更会被自动检测并热加载（约 10 秒内生效）。

### 3.4 手动触发一次抓取

```bash
# 通过 API 创建任务
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_type": "content_fetch", "account_id": 1}'
```

或在 Web 界面任务 Tab 中点击「创建任务」。

---

## 4. NAS 共享文件夹配置（可选）

默认配置下，下载的媒体文件存储在 Docker 卷 `media-data` 中。
如果要让这些文件可以通过 NAS 文件管理器直接访问，挂载共享文件夹：

### 4.1 修改 docker-compose.yml

在 `api` 和 `worker` 服务的 `volumes` 段中替换：
```yaml
# 替换：
- media-data:/app/downloads

# 为（Synology 示例）：
- /volume1/media/polycrawl-downloads:/app/downloads
```

### 4.2 创建共享文件夹

**Synology：**
```
控制面板 → 共享文件夹 → 新增 → 名称: polycrawl-downloads
```

**QNAP：**
```
控制台 → 共享文件夹 → 创建 → 名称: polycrawl-downloads
```

### 4.3 重启服务

```bash
docker compose -f deploy/docker-compose.yml up -d
```

---

## 5. 常用运维操作

### 5.1 停止/启动

```bash
# 停止所有服务（数据不丢失）
docker compose -f deploy/docker-compose.yml down

# 停止并删除数据卷（⚠️ 清空数据库 + 已下载文件）
docker compose -f deploy/docker-compose.yml down -v

# 重启某个服务
docker compose -f deploy/docker-compose.yml restart worker
```

### 5.2 查看日志

```bash
# 实时跟踪
docker compose logs -f

# 最近 100 行
docker compose logs --tail=100

# 按服务过滤
docker compose logs -f api
docker compose logs -f worker
```

### 5.3 数据库操作

```bash
# 手动执行迁移
docker compose exec api alembic upgrade head

# 查看迁移历史
docker compose exec api alembic history

# 回退一步
docker compose exec api alembic downgrade -1
```

### 5.4 更新项目

```bash
# 拉取最新代码
git pull

# 重新构建（注意：config/ 目录可能被挂载覆盖，确认有最新配置）
docker compose -f deploy/docker-compose.yml build --no-cache

# 重启
docker compose -f deploy/docker-compose.yml up -d
```

### 5.5 资源限制（NAS 内存有限时）

修改 `deploy/docker-compose.yml`，在服务中添加：

```yaml
worker:
  deploy:
    resources:
      limits:
        memory: 512M
      reservations:
        memory: 256M
```

---

## 6. 故障排查

### 6.1 数据库连接失败

```
Error: connection refused to postgres:5432
```

**原因：** PostgreSQL 尚未就绪，API/Worker 启动太快。

**解决：** 已配置 healthcheck，docker compose 会自动等待。如仍有问题：
```bash
# 检查 postgres 日志
docker compose logs postgres

# 手动测试连接
docker compose exec postgres psql -U polycrawl -d polycrawl_db -c "SELECT 1"
```

### 6.2 配置文件热加载失效（9p 文件系统）

**症状：** 修改 `config/*.jsonc` 后不生效，必须重启容器。

**原因：** Docker Desktop 或某些 NAS 使用 9p 文件系统，不支持 inotify。

**解决：** 代码已包含 polling fallback，会每 5 秒检查文件变更。
如仍不生效，手动触发重载：
```bash
docker compose restart api
docker compose restart worker
```

### 6.3 容器频繁重启

```
polycrawl-worker  exited with code 1 (restarting)
```

**排查步骤：**
```bash
# 查看退出时的完整日志
docker compose logs worker --tail=50

# 常见原因：
# 1. 数据库连接配置错误 → 检查 .env 中的密码
# 2. Redis 连接不上 → docker compose logs redis
# 3. Python 导入错误 → 检查是否构建了最新镜像
```

### 6.4 端口冲突

**症状：** `Error: address already in use`

**解决：** 修改 `.env` 中的端口映射：
```ini
API_PORT=8001
POSTGRES_PORT=5433
REDIS_PORT=6380
```
然后重新创建容器：
```bash
docker compose -f deploy/docker-compose.yml down
docker compose -f deploy/docker-compose.yml up -d
```

### 6.5 Cookie 频繁失效

**可能原因：**
- 请求频率过高被风控 → 调整 `config/sites/*.jsonc` 中的 `request.tick`（增大间隔）
- 同账号多容器并行 → 确保一个账号的 Cookie 只在一个 worker 中使用
- IP 被限 → 考虑配置代理

---

## 7. 参考架构

```
┌────────── NAS ─────────────────────────────────────┐
│                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │  api     │    │  worker  │    │  postgres│      │
│  │ :8000    │    │ (爬虫执行) │    │  :5432   │      │
│  │ FastAPI  │    │ Scheduler│    │  polycrawl_db│      │
│  │ Web UI   │    │ Consumer │    │          │      │
│  └────┬─────┘    └────┬─────┘    └──────────┘      │
│       │               │               │             │
│       └───────────────┼───────────────┘             │
│                       │                             │
│                  ┌────┴─────┐                       │
│                  │  redis   │                       │
│                  │  :6379   │                       │
│                  │ 任务队列  │                       │
│                  └──────────┘                       │
│                                                     │
│  共享文件夹: /volume1/media/polycrawl-downloads/        │
│  配置文件:   /volume1/docker/spider/config/          │
└─────────────────────────────────────────────────────┘
```

---

> **文件清单**
>
> | 文件 | 作用 |
> |------|------|
> | `Dockerfile` | 项目容器镜像构建 |
> | `.dockerignore` | 构建上下文排除项 |
> | `deploy/docker-compose.yml` | 编排所有服务 |
> | `deploy/.env.example` | 环境变量模板 → 复制为 `.env` |
> | 本文档 | 部署操作手册 |

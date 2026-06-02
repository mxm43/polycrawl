# PolyCrawl — 项目文件清单

> 路径: `D:\maxim\Documents\coding\polycrawl`
> 记录时间: 2026-05-31

```
polycrawl/
├── .dockerignore
├── .gitignore
├── Dockerfile
├── pyproject.toml
├── alembic.ini
│
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_initial_schema.py
│       ├── 0002_accounts_scheduled.py
│       └── 0003_artifact_sequence.py
│
├── apps/
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py          # FastAPI 入口
│   │   └── schemas.py       # Pydantic 响应模型
│   └── web/
│       ├── index.html
│       ├── styles.css
│       ├── core.js
│       ├── creators.js
│       ├── live.js
│       ├── login.js
│       ├── logs.js
│       ├── overview.js
│       ├── schedules.js
│       ├── tasks.js
│       ├── translations.js
│       └── utils.js
│
├── config/
│   ├── base.jsonc               # 基础配置（数据库/策略/任务）
│   ├── creators.jsonc           # 创作者列表（示例）
│   └── sites/
│       ├── douyin.jsonc         # 抖音站点配置（Cookie 仅示例）       
│       ├── twitter.jsonc        # Twitter 站点配置（Cookie 仅示例）
│       ├── weibo.jsonc          # 微博站点配置（Cookie 仅示例）
│       └── xiaohongshu.jsonc    # 小红书站点配置（Cookie 仅示例）
│
├── deploy/
│   ├── .env.example             # 环境变量模板
│   ├── docker-compose.yml       # Docker 编排
│   └── DEPLOY.md                # NAS 部署手册
│
├── docs/
│   ├── design.md
│   ├── implementation.md
│   └── xiaohongshu_auth_analysis.md
│
├── packages/
│   ├── core/
│   │   ├── __init__.py
│   │   ├── events.py
│   │   ├── config/
│   │   │   ├── __init__.py
│   │   │   ├── cookie_verify.py
│   │   │   ├── creator_keys.py
│   │   │   ├── jsonc.py
│   │   │   ├── loader.py
│   │   │   ├── models.py
│   │   │   └── watcher.py
│   │   ├── db/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── health.py
│   │   │   ├── models.py
│   │   │   └── session.py
│   │   ├── logging/
│   │   │   └── __init__.py
│   │   ├── migration/
│   │   │   ├── __init__.py
│   │   │   └── legacy_douyin.py
│   │   ├── providers/
│   │   │   ├── __init__.py
│   │   │   ├── base.py          # BaseProvider + SyncRateLimiter
│   │   │   └── registry.py
│   │   ├── sync/
│   │   │   ├── __init__.py
│   │   │   └── config_sync.py
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── dates.py
│   │       └── filesystem.py
│   └── provider_impls/
│       ├── __init__.py
│       ├── douyin/
│       │   ├── __init__.py
│       │   ├── api_client.py
│       │   └── signing.py        ├── twitter/
        │   └── __init__.py│       ├── weibo/
│       │   └── __init__.py
│       └── xiaohongshu/
│           ├── __init__.py
│           ├── xhs_rap.js
│           ├── xhs_rap.py
│           ├── xhs_signer.py
│           ├── xs_config.py
│           ├── xs_crypto.py
│           ├── xs_encoder.py
│           ├── xs_fingerprint.py
│           └── xs_signer.py
│
├── scripts/
│   ├── migrate.ps1
│   ├── run-api.ps1
│   ├── run-worker-content.ps1
│   ├── run-worker-live.ps1
│   ├── xhs_login.py
│   ├── xhs_qr_login.py
│   └── xhs_xs_common_probe.py
│
├── services/
│   ├── __init__.py
│   └── worker/
│       ├── __init__.py
│       ├── consumer.py
│       ├── run.py
│       ├── runtime.py
│       ├── scheduler.py
│       └── executors/
│           ├── __init__.py
│           ├── content_executor.py
│           └── live_executor.py
│
└── tests/
    ├── config/
    │   └── test_creator_key_policy.py
    ├── integration/
    │   └── test_content_pipeline_integration.py
    ├── providers/
    │   ├── test_douyin_provider.py
    │   └── test_signing.py
    └── worker/
        └── test_live_executor.py
```

**统计**: 101 文件，37 目录（不含 `__pycache__`）

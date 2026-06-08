from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


_INTERVAL_RE = re.compile(r"^(\d+)([smhd])$")
# Supports negative and decimal, e.g. "-0.5s", "10s", "1.5m"
_JITTER_RE = re.compile(r"^(-?\d+(?:\.\d+)?)([smhd])$")


def parse_duration_to_seconds(value: str) -> float:
    """Parse a duration string like ``30s``, ``1m``, ``-0.5s`` into float seconds."""
    m = _JITTER_RE.match(value)
    if not m:
        raise ValueError(f"Invalid duration: {value!r} (expected like 30s, 1m, -0.5s)")
    amount = float(m.group(1))
    unit = m.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


class UrlEntry(BaseModel):
    url: str = Field(min_length=1)
    enabled: bool = True


class RequestConfig(BaseModel):
    """API 请求速率配置。可定义在 site config 或 task entry 中。"""
    tick: Optional[str] = Field(default=None, description="API 调用最小间隔，如 1s、2s、30s")
    jitter: Optional[tuple[str, str]] = Field(default=None, description="请求间隔随机抖动范围，如 ['1s', '2s']")
    platforms: dict[str, RequestConfig] = Field(
        default_factory=dict,
        description="按平台覆写 tick/jitter，key 为平台名，如 {\"douyin\": {\"tick\": \"3s\"}}",
    )

    @field_validator("tick")
    @classmethod
    def _validate_tick(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _INTERVAL_RE.match(value):
            raise ValueError(f"tick must be like 30s, 1m, 5m, got: {value!r}")
        return value


class SiteRequestConfig(BaseModel):
    """Platform site-level request config — tick is required."""
    tick: str = Field(description="API 调用最小间隔，如 10s、30s（必填）")
    jitter: tuple[str, str] = Field(default=("0s", "0s"), description="请求间隔随机抖动范围，如 ['-1s', '4s']")

    @field_validator("tick")
    @classmethod
    def _validate_tick(cls, value: str) -> str:
        if not _INTERVAL_RE.match(value):
            raise ValueError(f"tick must be like 30s, 1m, 5m, got: {value!r}")
        return value


class SiteConfig(BaseModel):
    """Per-platform site config (sites/*.jsonc). Validated at startup."""
    enabled: bool = True
    cookies: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    request: SiteRequestConfig = Field(description="请求配置（tick 为必填）")
    content_fetch: dict[str, Any] = Field(default_factory=dict)
    download: dict[str, Any] = Field(default_factory=dict)
    proxy: str | None = None


class StrategyOverride(BaseModel):
    """Task 级策略配置，可覆盖 strategy.<use> 中的同名字段。"""
    use: str = Field(description="引用的策略名：incremental / deep / live")
    adaptive: AdaptiveConfig | None = Field(default=None, description="覆盖 strategy.<use>.adaptive，如 {\"enabled\": false}")


class ScheduleEntry(BaseModel):
    type: str = Field(min_length=1, description="任务类型，如 content_fetch、live_record")
    enabled: bool = True
    strategy: StrategyOverride | None = Field(
        default=None,
        description="策略引用与覆写。不填则根据 type 自动推断。",
    )
    request: RequestConfig | None = Field(
        default=None,
        description="API 请求速率覆写，覆盖 site config 的 request 配置。",
    )
    tag_filter: list[str] | None = Field(default=None, description="标签白名单，只处理匹配的 creator")
    start_at: str | None = None
    interval: str | None = None
    @field_validator("start_at")
    @classmethod
    def _validate_start_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", value)
        if not m:
            raise ValueError(f"start_at must be HH:MM (24h), got: {value!r}")
        return value

    @field_validator("interval")
    @classmethod
    def _validate_interval(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _INTERVAL_RE.match(str(value)):
            raise ValueError(f"interval must be like 30s, 30m, 6h, 1d, got: {value!r}")
        return value


class SchedulesConfig(BaseModel):
    tasks: list[ScheduleEntry] = Field(default_factory=list)


class AccountConfig(BaseModel):
    platform: Literal["douyin", "twitter", "weibo", "xiaohongshu"]
    type: Literal["profile", "live"]
    account_url: str | list[str] | list[UrlEntry]
    account_alias: str | None = None
    live: dict[str, Any] | None = None

    @field_validator("account_url")
    @classmethod
    def _validate_account_url(cls, value: str | list[str] | list[UrlEntry]) -> str | list[str] | list[UrlEntry]:
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise ValueError("account_url must not be empty")
            return cleaned

        if not value:
            raise ValueError("account_url list must not be empty")

        # Accept both list[str] and list[UrlEntry]
        return value

    def normalized_urls(self) -> list[UrlEntry]:
        """Return a list of UrlEntry regardless of input format."""
        raw = self.account_url
        if isinstance(raw, str):
            return [UrlEntry(url=raw)]

        result: list[UrlEntry] = []
        for item in raw:
            if isinstance(item, str):
                result.append(UrlEntry(url=item))
            elif isinstance(item, dict):
                result.append(UrlEntry(**item))
            else:
                result.append(item)
        return result


class CreatorConfig(BaseModel):
    creator_key: str | None = None
    display_name: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    accounts: list[AccountConfig] = Field(default_factory=list)


class CreatorsFile(BaseModel):
    creators: list[CreatorConfig] = Field(default_factory=list)


class BaseStorageConfig(BaseModel):
    media_base_path: str


class BaseConfig(BaseModel):
    config_version: int = 1
    storage: BaseStorageConfig
    global_config: dict[str, Any] = Field(default_factory=dict, alias="global")
    schedules: list[ScheduleEntry] = Field(default_factory=list, alias="tasks")
    strategy: StrategyConfig = Field(alias="strategy")
    download: DownloadConfig

    model_config = {
        "populate_by_name": True,
    }


class ConfigState(BaseModel):
    base: BaseConfig
    creators: CreatorsFile
    sites: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AdaptiveTier(BaseModel):
    after: str = Field(description="闲置时间阈值，如 14d、30d、60d、180d")
    interval: str = Field(description="阶梯间隔，如 1d、3d、7d、30d")

    @field_validator("after")
    @classmethod
    def _validate_after(cls, value: str) -> str:
        if not _INTERVAL_RE.match(value):
            raise ValueError(f"after must be like 14d, 30d, 60d, 180d, got: {value!r}")
        return value

    @field_validator("interval")
    @classmethod
    def _validate_tier_interval(cls, value: str) -> str:
        if not _INTERVAL_RE.match(value):
            raise ValueError(f"tier interval must be like 30s, 1m, 5m, 1d, got: {value!r}")
        return value


class AdaptiveConfig(BaseModel):
    enabled: bool = True
    tiers: list[AdaptiveTier] = Field(default_factory=list)


class StrategyItemConfig(BaseModel):
    """策略定义（仅保留 adaptive，tick/jitter 已移到 request 配置）。"""
    adaptive: AdaptiveConfig = Field(default_factory=lambda: AdaptiveConfig(enabled=False))


class StrategyConfig(BaseModel):
    incremental: StrategyItemConfig
    deep: StrategyItemConfig
    live: StrategyItemConfig


class DownloadConfig(BaseModel):
    """下载相关配置。"""
    download_requests_per_second: float = Field(description="CDN 下载限速，0 表示不限速")

from __future__ import annotations

from importlib import import_module

from .base import BaseProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.platform] = provider

    def get(self, platform: str) -> BaseProvider:
        if platform not in self._providers:
            self._lazy_load(platform)
        try:
            return self._providers[platform]
        except KeyError as exc:
            raise KeyError(f"Provider not found: {platform}") from exc

    def _lazy_load(self, platform: str) -> None:
        module = import_module(f"packages.provider_impls.{platform}")
        factory = getattr(module, "build_provider", None)
        if factory is None:
            return
        provider = factory()
        self.register(provider)

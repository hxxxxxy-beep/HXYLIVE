from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from .base import BaseProvider
from .builtin import ChaturbateProvider
from .bilibili import BilibiliProvider
from .sessions import ProviderSessionStore
from .stripchat import StripchatProvider
from .twitch import TwitchProvider


class ProviderRegistry:
    def __init__(self, providers: Optional[Iterable[BaseProvider]] = None):
        self._providers: dict[str, BaseProvider] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.source_type] = provider

    def get(self, source_type: str) -> BaseProvider:
        key = (source_type or "").strip().lower()
        if key not in self._providers:
            raise KeyError(key)
        return self._providers[key]

    def has(self, source_type: str) -> bool:
        return (source_type or "").strip().lower() in self._providers

    def source_types(self) -> set[str]:
        return set(self._providers.keys())

    def all(self) -> list[BaseProvider]:
        return list(self._providers.values())

    def metadata(self) -> list[dict]:
        return [provider.metadata() for provider in self.all()]


def create_provider_registry(
    db,
    chaturbate_api=None,
    chaturbate_auth=None,
    output_dir: Optional[Path] = None,
) -> ProviderRegistry:
    output_dir = Path(output_dir or "data")
    store = ProviderSessionStore(db)
    registry = ProviderRegistry()

    registry.register(ChaturbateProvider(chaturbate_api, chaturbate_auth, store))

    # Twitch support: use yt-dlp Twitch extractor (HLS/m3u8)
    # Kept as a normal provider so existing recording pipeline is unchanged.
    registry.register(
        TwitchProvider(
            "twitch",
            "Twitch",
            "https://www.twitch.tv/{username}",
            ("twitch.tv", "www.twitch.tv"),
            store,
        )
    )
    registry.register(
        BilibiliProvider(
            "bilibili",
            "Bilibili",
            "https://live.bilibili.com/{username}",
            ("live.bilibili.com", "bilibili.com", "www.bilibili.com"),
            store,
        )
    )
    registry.register(
        StripchatProvider(
            "stripchat",
            "Stripchat",
            "https://stripchat.com/{username}",
            ("stripchat.com",),
            store,
        )
    )
    return registry

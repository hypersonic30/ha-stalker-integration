"""Cached portal data and server-side resolution of ids into stream URLs."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import CACHE_TTL, M3U_MAX_BYTES
from .m3u import Entry, Episode, Series, group_series, parse_m3u
from .stalker import StalkerClient, StalkerError, host_of

_LOGGER = logging.getLogger(__name__)
M3U_TIMEOUT = aiohttp.ClientTimeout(total=90)


class NotFound(Exception):
    """Unknown id."""


def series_id(title: str) -> str:
    return hashlib.sha1(title.encode()).hexdigest()[:12]


@dataclass
class _Cache:
    value: Any = None
    at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StalkerData:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        client: StalkerClient,
        vod_url: str | None,
        series_url: str | None,
    ) -> None:
        self.client = client
        self._session = session
        self._vod_url = vod_url or None
        self._series_url = series_url or None
        self._live, self._vod, self._series = _Cache(), _Cache(), _Cache()
        # Streams may only come from hosts the user configured themselves.
        self.allowed_hosts = {client.host}
        for u in (self._vod_url, self._series_url):
            if u:
                self.allowed_hosts.add(host_of(u))

    @property
    def has_vod(self) -> bool:
        return self._vod_url is not None

    @property
    def has_series(self) -> bool:
        return self._series_url is not None

    # -- loading -----------------------------------------------------------
    async def _cached(self, cache: _Cache, loader):
        async with cache.lock:
            if cache.value is None or time.monotonic() - cache.at > CACHE_TTL:
                try:
                    cache.value = await loader()
                    cache.at = time.monotonic()
                except (StalkerError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                    if cache.value is None:
                        raise
                    _LOGGER.warning("Refresh failed, using stale data: %s", type(err).__name__)
            return cache.value

    async def _fetch_m3u(self, url: str, keep_attrs: bool) -> list[Entry]:
        try:
            async with self._session.get(url, timeout=M3U_TIMEOUT) as r:
                r.raise_for_status()
                body = await r.content.read(M3U_MAX_BYTES + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise StalkerError("playlist_unreachable") from None  # no URL in the message
        if len(body) > M3U_MAX_BYTES:
            raise StalkerError("playlist_too_large")
        return parse_m3u(body.decode("utf-8", "replace"), keep_attrs)

    async def live(self) -> dict[str, Any]:
        async def load():
            return {
                "genres": await self.client.genres(),
                "channels": {c["id"]: c for c in await self.client.channels()},
            }

        return await self._cached(self._live, load)

    async def vod(self) -> dict[str, Entry]:
        if not self._vod_url:
            return {}

        async def load():
            return {e.stream_id or str(i): e for i, e in enumerate(
                await self._fetch_m3u(self._vod_url, False))}  # fmt: skip

        return await self._cached(self._vod, load)

    async def series(self) -> dict[str, Series]:
        if not self._series_url:
            return {}

        async def load():
            grouped = group_series(await self._fetch_m3u(self._series_url, True))
            return {series_id(t): s for t, s in grouped.items()}

        return await self._cached(self._series, load)

    # -- resolving -----------------------------------------------------------
    async def source_for(self, kind: str, item_id: str) -> str:
        """Stream URL for an id. For live TV this opens a fresh one-shot link."""
        if kind == "live":
            ch = (await self.live())["channels"].get(item_id)
            if not ch:
                raise NotFound
            url = await self.client.create_link(ch["cmd"])
        elif kind == "vod":
            e = (await self.vod()).get(item_id)
            if not e:
                raise NotFound
            url = e.url
        elif kind == "episode":
            url = self._episode(await self.series(), item_id).url
        else:
            raise NotFound
        if host_of(url) not in self.allowed_hosts:
            raise StalkerError("foreign_host")
        return url

    @staticmethod
    def _episode(series: dict[str, Series], item_id: str) -> Episode:
        for s in series.values():
            for eps in s.seasons.values():
                for ep in eps:
                    if ep.stream_id == item_id:
                        return ep
        raise NotFound

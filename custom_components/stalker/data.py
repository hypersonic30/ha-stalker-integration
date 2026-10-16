"""Cached source data and server-side resolution of ids into stream URLs."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import CACHE_TTL, EPG_TTL
from .m3u import Entry, Episode, Series, SeriesInfo
from .stalker import StalkerError, host_of

_LOGGER = logging.getLogger(__name__)
NETWORK_ERRORS = (StalkerError, aiohttp.ClientError, asyncio.TimeoutError, ValueError)


class NotFound(Exception):
    """Unknown id."""


@dataclass
class _Cache:
    value: Any = None
    at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StalkerData:
    def __init__(self, live, vod=None, series=None) -> None:
        self._live_src, self._vod_src, self._series_src = live, vod, series
        self._live, self._vod, self._series, self._epg = _Cache(), _Cache(), _Cache(), _Cache()
        self._details: dict[str, _Cache] = {}

    @property
    def has_vod(self) -> bool:
        return self._vod_src is not None

    @property
    def has_series(self) -> bool:
        return self._series_src is not None

    @property
    def has_epg(self) -> bool:
        return bool(self._live_src.has_epg)

    @property
    def allowed_hosts(self) -> set[str]:
        """Streams may only come from servers the user configured (or that are in their own list)."""
        hosts = set(self._live_src.allowed_hosts)
        for src in (self._vod_src, self._series_src):
            if src is not None:
                hosts |= src.allowed_hosts
        return hosts

    # -- loading -----------------------------------------------------------
    @staticmethod
    async def _cached(cache: _Cache, loader, ttl: float = CACHE_TTL):
        async with cache.lock:
            if cache.value is None or time.monotonic() - cache.at > ttl:
                try:
                    cache.value = await loader()
                    cache.at = time.monotonic()
                except NETWORK_ERRORS as err:
                    if cache.value is None:
                        raise
                    _LOGGER.warning("Refresh failed, using stale data: %s", type(err).__name__)
            return cache.value

    async def live(self) -> dict[str, Any]:
        async def load():
            genres, channels = await self._live_src.live_listing()
            return {"genres": genres, "channels": {c["id"]: c for c in channels}}

        return await self._cached(self._live, load)

    async def vod(self) -> dict[str, Entry]:
        if self._vod_src is None:
            return {}
        return await self._cached(self._vod, self._vod_src.entries)

    async def series(self) -> dict[str, SeriesInfo]:
        if self._series_src is None:
            return {}
        return await self._cached(self._series, self._series_src.listing)

    async def series_detail(self, sid: str) -> Series | None:
        if self._series_src is None or sid not in await self.series():
            return None
        cache = self._details.setdefault(sid, _Cache())
        if len(self._details) > 200:  # series opened long ago
            for k in sorted(self._details, key=lambda k: self._details[k].at)[:50]:
                self._details.pop(k, None)
        return await self._cached(cache, lambda: self._series_src.detail(sid))

    async def guide(self) -> dict[str, list[dict[str, Any]]]:
        """Programme guide of all channels; empty (never an error) when there is none."""
        if not self.has_epg:
            return {}
        await self.live()  # EPG ids map onto the channel list
        try:
            return await self._cached(self._epg, self._live_src.epg_all, EPG_TTL) or {}
        except NETWORK_ERRORS as err:
            _LOGGER.warning("Programme guide unavailable: %s", type(err).__name__)
            return {}

    async def short_epg(self, channel_id: str) -> list[dict[str, Any]]:
        return await self._live_src.epg(channel_id)

    # -- resolving -----------------------------------------------------------
    async def _find_episode(self, item_id: str, sid: str | None) -> tuple[str, Series, Episode]:
        if sid:
            sids = [sid]
        elif getattr(self._series_src, "lazy", False):
            raise NotFound  # one series at a time: the player has to say which
        else:
            sids = list(await self.series())
        for s_id in sids:
            s = await self.series_detail(s_id)
            for eps in (s.seasons.values() if s else ()):
                for ep in eps:
                    if ep.stream_id == item_id:
                        return s_id, s, ep
        raise NotFound

    async def source_for(self, kind: str, item_id: str, sid: str | None = None) -> str:
        """Stream URL for an id. For a Stalker portal this opens a fresh one-shot link."""
        if kind == "live":
            ch = (await self.live())["channels"].get(item_id)
            if not ch:
                raise NotFound
            url = await self._live_src.create_link(ch["cmd"])
        elif kind == "vod":
            e = (await self.vod()).get(item_id)
            if not e:
                raise NotFound
            url = e.url
        elif kind == "episode":
            url = (await self._find_episode(item_id, sid))[2].url
        else:
            raise NotFound
        if host_of(url) not in self.allowed_hosts:
            raise StalkerError("foreign_host")
        return url

    async def describe(self, kind: str, item_id: str, sid: str | None = None) -> dict[str, Any]:
        """Server-side metadata stored with a progress entry (the client only sends ids)."""
        if kind == "vod":
            e = (await self.vod()).get(item_id)
            if not e:
                raise NotFound
            return {"title": e.title, "logo": e.logo}
        if kind == "episode":
            s_id, s, ep = await self._find_episode(item_id, sid)
            logo = s.logo or (await self.series())[s_id].logo
            return {"title": s.title, "logo": logo, "sid": s_id, "season": ep.season, "episode": ep.episode}
        raise NotFound

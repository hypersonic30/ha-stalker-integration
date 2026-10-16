"""Async client for a Stalker/Ministra portal (live TV, genres, EPG, links)."""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlencode

import aiohttp

UA = (
    "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
    "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
)
ENDPOINTS = ("/portal.php", "/stalker_portal/server/load.php", "/server/load.php")
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
TIMEOUT = aiohttp.ClientTimeout(total=20)
BULK_TIMEOUT = aiohttp.ClientTimeout(total=90)  # the whole programme guide is ~5 MB
DESCR_MAX = 300


class StalkerError(Exception):
    """Any portal problem; the message never contains the MAC or tokens."""


class StalkerAuthError(StalkerError):
    """Handshake refused or token rejected."""


def host_of(url: str) -> str:
    m = re.match(r"(https?://[^/]+)", url.strip())
    if not m:
        raise StalkerError("invalid_url")
    return m.group(1)


class StalkerClient:
    def __init__(self, session: aiohttp.ClientSession, portal_url: str, mac: str) -> None:
        if not MAC_RE.match(mac):
            raise StalkerError("invalid_mac")
        self._session = session
        self._host = host_of(portal_url)
        self._mac = mac
        self._endpoint: str | None = None
        self._token: str | None = None
        self._lock = asyncio.Lock()

    @property
    def host(self) -> str:
        return self._host

    async def _raw(
        self, params: dict[str, str], token: str | None, timeout: aiohttp.ClientTimeout = TIMEOUT
    ) -> Any:
        headers = {
            "User-Agent": UA,
            "X-User-Agent": "Model: MAG250; Link: WiFi",
            "Cookie": f"mac={self._mac}; stb_lang=en; timezone=Europe/Berlin",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self._host}{self._endpoint}?{urlencode({**params, 'JsHttpRequest': '1-xml'})}"
        try:
            async with self._session.get(url, headers=headers, timeout=timeout) as r:
                if r.status in (401, 403):
                    raise StalkerAuthError("auth")
                if r.status >= 400:
                    raise StalkerError(f"http_{r.status}")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            # Never include the exception text: aiohttp prints the full URL.
            raise StalkerError(type(err).__name__) from None
        if not isinstance(data, dict) or "js" not in data:
            raise StalkerAuthError("bad_reply")
        return data["js"]

    async def _handshake(self) -> None:
        candidates = (self._endpoint,) if self._endpoint else ENDPOINTS
        last: StalkerError = StalkerError("no_endpoint")
        for ep in candidates:
            self._endpoint = ep
            try:
                js = await self._raw({"type": "stb", "action": "handshake", "token": ""}, None)
            except StalkerAuthError as err:
                if str(err) == "auth":  # 401/403: the portal is here and refuses this MAC
                    self._endpoint = None
                    raise
                last = err
                continue
            except StalkerError as err:
                last = err
                continue
            token = js.get("token") if isinstance(js, dict) else None
            if not token:
                last = StalkerAuthError("no_token")
                continue
            await self._raw({"type": "stb", "action": "get_profile"}, token)
            self._token = token
            return
        self._endpoint = None
        raise last

    async def call(
        self, params: dict[str, str], timeout: aiohttp.ClientTimeout = TIMEOUT
    ) -> Any:
        """Call with automatic handshake and one re-handshake on auth failure."""
        async with self._lock:
            for attempt in (0, 1):
                if not self._token:
                    await self._handshake()
                try:
                    return await self._raw(params, self._token, timeout)
                except StalkerAuthError:
                    self._token = None
                    if attempt:
                        raise
        raise StalkerAuthError("auth")  # pragma: no cover

    async def genres(self) -> list[dict[str, str]]:
        js = await self.call({"type": "itv", "action": "get_genres"})
        return [{"id": str(g["id"]), "title": g["title"]} for g in js if g.get("id")]

    async def channels(self) -> list[dict[str, Any]]:
        js = await self.call({"type": "itv", "action": "get_all_channels"})
        data = js["data"] if isinstance(js, dict) else js
        return [
            {
                "id": str(c["id"]),
                "name": c.get("name", ""),
                "number": c.get("number"),
                "genre_id": str(c.get("tv_genre_id") or c.get("genre_id") or ""),
                "logo": c.get("logo") or "",
                "has_epg": bool(c.get("xmltv_id")),
                "cmd": c.get("cmd", ""),
                "xmltv_id": c.get("xmltv_id") or "",
            }
            for c in data
        ]

    async def epg(self, channel_id: str, size: int = 3) -> list[dict[str, Any]]:
        js = await self.call(
            {"type": "itv", "action": "get_short_epg", "ch_id": channel_id, "size": str(size)}
        )
        if not isinstance(js, list):
            return []
        return [
            {
                "title": e.get("name", ""),
                "descr": e.get("descr", ""),
                "start": int(e.get("start_timestamp") or 0),
                "stop": int(e.get("stop_timestamp") or 0),
            }
            for e in js
        ]

    async def epg_all(self) -> dict[str, list[dict[str, Any]]]:
        """Programme guide of every channel in one call: {channel id: [programme, ...]} by start."""
        js = await self.call({"type": "itv", "action": "get_epg_info", "period": "6"}, BULK_TIMEOUT)
        data = js.get("data") if isinstance(js, dict) else None
        out: dict[str, list[dict[str, Any]]] = {}
        for ch_id, items in (data.items() if isinstance(data, dict) else ()):
            progs = []
            for e in items if isinstance(items, list) else ():
                try:
                    start, stop = int(e["start_timestamp"]), int(e["stop_timestamp"])
                except (KeyError, TypeError, ValueError):
                    continue
                if stop > start:
                    progs.append({
                        "title": str(e.get("name") or ""),
                        "descr": str(e.get("descr") or "")[:DESCR_MAX],
                        "start": start, "stop": stop,
                    })  # fmt: skip
            if progs:
                out[str(ch_id)] = sorted(progs, key=lambda p: p["start"])
        return out

    async def create_link(self, cmd: str) -> str:
        """Resolve a channel's cmd into a one-shot stream URL (call right before playing)."""
        js = await self.call({"type": "itv", "action": "create_link", "cmd": cmd})
        url = (js.get("cmd") or "") if isinstance(js, dict) else ""
        url = url.removeprefix("ffmpeg").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise StalkerError("no_link")
        return url

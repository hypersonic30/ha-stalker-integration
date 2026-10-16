"""Content sources: Stalker portal, plain M3U (+ XMLTV) and Xtream Codes. Same shapes for all three.

Live backends offer  live_listing() -> (genres, channels), epg_all() -> {channel id: programmes},
create_link(cmd) -> url.  VOD backends offer entries();  series backends offer listing() and detail(sid).
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .const import (
    CONF_LIVE_M3U,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_PORTAL_URL,
    CONF_SERIES_M3U,
    CONF_SERVER_URL,
    CONF_SOURCE,
    CONF_USERNAME,
    CONF_VOD_M3U,
    CONF_XMLTV,
    M3U_MAX_BYTES,
    SOURCE_M3U,
    SOURCE_XTREAM,
)
from .data import StalkerData
from .m3u import Entry, Episode, Series, SeriesInfo, group_series, parse_m3u
from .stalker import StalkerAuthError, StalkerClient, StalkerError, host_of
from .xmltv import XmltvError, norm, parse_xmltv

TIMEOUT = aiohttp.ClientTimeout(total=90)
XMLTV_MAX_BYTES = 120 * 1024 * 1024
API_MAX_BYTES = 80 * 1024 * 1024


async def fetch(session: aiohttp.ClientSession, url: str, limit: int = M3U_MAX_BYTES) -> bytes:
    """GET with a size cap. Errors never contain the URL (it may hold a password)."""
    try:
        async with session.get(url, timeout=TIMEOUT) as r:
            if r.status in (401, 403):
                raise StalkerAuthError("auth")
            if r.status >= 400:
                raise StalkerError(f"http_{r.status}")
            chunks, size = [], 0
            async for chunk in r.content.iter_chunked(65536):
                size += len(chunk)
                if size > limit:
                    raise StalkerError("too_large")
                chunks.append(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise StalkerError(type(err).__name__) from None
    return b"".join(chunks)


def _hid(*parts: str, n: int = 10) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:n]


def is_private_host(host: str) -> bool:
    h = re.sub(r"^https?://", "", host.lower()).split("@")[-1].rsplit(":", 1)[0].strip("[]")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _guide_for(channels: list[dict[str, Any]], progs: dict[str, list], names: dict[str, str]) -> dict[str, list]:
    """Map XMLTV channel ids onto our channels: by id first, then by (normalised) name."""
    out: dict[str, list] = {}
    for c in channels:
        key = c.get("xmltv_id") or ""
        if key not in progs:
            key = next((k for k in (names.get(norm(c["name"])), names.get(norm(c.get("tvg_name") or ""))) if k in progs), "")
        if key in progs:
            out[c["id"]] = progs[key]
    return out


async def _load_xmltv(session, url: str) -> tuple[dict[str, list], dict[str, str]]:
    raw = await fetch(session, url, XMLTV_MAX_BYTES)
    try:
        return await asyncio.get_running_loop().run_in_executor(None, parse_xmltv, raw)
    except XmltvError as err:
        raise StalkerError(str(err)) from None


# ── Stalker portal ───────────────────────────────────────────────────────
class StalkerLive:
    def __init__(self, client: StalkerClient) -> None:
        self.client = client
        self.allowed_hosts = {client.host}
        self.has_epg = True

    async def live_listing(self):
        return await self.client.genres(), await self.client.channels()

    async def epg_all(self):
        return await self.client.epg_all()

    async def epg(self, channel_id: str):
        return await self.client.epg(channel_id)

    async def create_link(self, cmd: str) -> str:
        return await self.client.create_link(cmd)


# ── Plain M3U (+ XMLTV) ──────────────────────────────────────────────────
class M3uLive:
    def __init__(self, session, url: str, xmltv_url: str | None = None) -> None:
        self._session, self._url, self._xmltv = session, url, xmltv_url or None
        self.allowed_hosts = {host_of(url)} | ({host_of(xmltv_url)} if xmltv_url else set())
        self.has_epg = bool(self._xmltv)
        self._channels: list[dict[str, Any]] = []

    async def live_listing(self):
        entries = parse_m3u((await fetch(self._session, self._url)).decode("utf-8", "replace"), True)
        genres: dict[str, str] = {}
        seen: dict[str, int] = {}
        channels = []
        for i, e in enumerate(entries, 1):
            group = e.group or "Sonstige"
            gid = _hid(group, n=8)
            genres.setdefault(gid, group)
            tvg_id = e.attrs.get("tvg-id", "")
            base = _hid(tvg_id, e.title)
            seen[base] = seen.get(base, 0) + 1
            num = e.attrs.get("tvg-chno", "")
            channels.append({
                "id": base if seen[base] == 1 else f"{base}{seen[base]}",
                "name": e.title or tvg_id or f"Kanal {i}", "number": num if num.isdigit() else str(i),
                "genre_id": gid, "logo": e.logo, "has_epg": bool(tvg_id and self._xmltv),
                "cmd": e.url, "xmltv_id": tvg_id, "tvg_name": e.attrs.get("tvg-name", ""),
            })  # fmt: skip
        if not channels:
            raise StalkerError("no_channels")
        self._channels = channels
        self.allowed_hosts |= {host_of(c["cmd"]) for c in channels if not is_private_host(host_of(c["cmd"]))}
        return [{"id": k, "title": v} for k, v in genres.items()], channels

    async def epg_all(self):
        if not self._xmltv:
            return {}
        if not self._channels:
            await self.live_listing()
        progs, names = await _load_xmltv(self._session, self._xmltv)
        return _guide_for(self._channels, progs, names)

    async def epg(self, channel_id: str):
        return []

    async def create_link(self, cmd: str) -> str:
        return cmd


class M3uVod:
    lazy = False

    def __init__(self, session, url: str) -> None:
        self._session, self._url = session, url
        self.allowed_hosts = {host_of(url)}

    async def entries(self) -> dict[str, Entry]:
        raw = await fetch(self._session, self._url)
        return {e.stream_id or str(i): e for i, e in enumerate(parse_m3u(raw.decode("utf-8", "replace"), False))}


class M3uSeries:
    lazy = False

    def __init__(self, session, url: str) -> None:
        self._session, self._url = session, url
        self.allowed_hosts = {host_of(url)}
        self._all: dict[str, Series] = {}

    async def listing(self) -> dict[str, SeriesInfo]:
        raw = await fetch(self._session, self._url)
        grouped = group_series(parse_m3u(raw.decode("utf-8", "replace"), True))
        self._all = {_hid(t, n=12): s for t, s in grouped.items()}
        return {sid: SeriesInfo(s.title, s.logo, s.group, sum(len(v) for v in s.seasons.values())) for sid, s in self._all.items()}

    async def detail(self, sid: str) -> Series | None:
        if not self._all:
            await self.listing()
        return self._all.get(sid)


# ── Xtream Codes ─────────────────────────────────────────────────────────
def xtream_base(url: str) -> str:
    base = url.strip().rstrip("/")
    for tail in ("/player_api.php", "/get.php", "/xmltv.php", "/panel_api.php"):
        if base.lower().endswith(tail):
            base = base[: -len(tail)]
    host_of(base)  # raises StalkerError("invalid_url")
    return base


class XtreamApi:
    def __init__(self, session, url: str, username: str, password: str) -> None:
        self._session, self.base, self._u, self._p = session, xtream_base(url), username, password
        self.allowed_hosts = {host_of(self.base)}

    async def call(self, action: str | None = None, **params: str) -> Any:
        q = {"username": self._u, "password": self._p, **({"action": action} if action else {}), **params}
        raw = await fetch(self._session, f"{self.base}/player_api.php?{urlencode(q)}", API_MAX_BYTES)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise StalkerError("bad_reply") from None

    async def login(self) -> None:
        reply = await self.call()
        info = reply.get("user_info") if isinstance(reply, dict) else None
        if not isinstance(info, dict) or str(info.get("auth")) != "1":
            raise StalkerAuthError("auth")

    def url(self, kind: str, item_id: str, ext: str) -> str:
        return f"{self.base}/{kind}/{self._u}/{self._p}/{item_id}.{ext}"

    def xmltv_url(self) -> str:
        return f"{self.base}/xmltv.php?{urlencode({'username': self._u, 'password': self._p})}"

    async def categories(self, action: str) -> dict[str, str]:
        cats = await self.call(action)
        return {str(c.get("category_id")): str(c.get("category_name") or "") for c in cats if isinstance(c, dict)} if isinstance(cats, list) else {}


class XtreamLive:
    def __init__(self, api: XtreamApi) -> None:
        self._api = api
        self.allowed_hosts = api.allowed_hosts
        self.has_epg = True
        self._channels: list[dict[str, Any]] = []

    async def live_listing(self):
        cats = await self._api.categories("get_live_categories")
        streams = await self._api.call("get_live_streams")
        if not isinstance(streams, list):
            raise StalkerError("bad_reply")
        channels = []
        for i, st in enumerate(streams, 1):
            if not isinstance(st, dict) or not st.get("stream_id"):
                continue
            sid = str(st["stream_id"])
            channels.append({
                "id": sid, "name": str(st.get("name") or sid), "number": str(st.get("num") or i),
                "genre_id": str(st.get("category_id") or ""), "logo": str(st.get("stream_icon") or ""),
                "has_epg": bool(st.get("epg_channel_id")), "cmd": self._api.url("live", sid, "ts"),
                "xmltv_id": str(st.get("epg_channel_id") or ""),
            })  # fmt: skip
        if not channels:
            raise StalkerError("no_channels")
        self._channels = channels
        return [{"id": k, "title": v} for k, v in cats.items()], channels

    async def epg_all(self):
        if not self._channels:
            await self.live_listing()
        progs, names = await _load_xmltv(self._api._session, self._api.xmltv_url())
        return _guide_for(self._channels, progs, names)

    async def epg(self, channel_id: str):
        return []

    async def create_link(self, cmd: str) -> str:
        return cmd


class XtreamVod:
    lazy = False

    def __init__(self, api: XtreamApi) -> None:
        self._api = api
        self.allowed_hosts = api.allowed_hosts

    async def entries(self) -> dict[str, Entry]:
        cats = await self._api.categories("get_vod_categories")
        items = await self._api.call("get_vod_streams")
        out: dict[str, Entry] = {}
        for it in items if isinstance(items, list) else ():
            if not isinstance(it, dict) or not it.get("stream_id"):
                continue
            sid = str(it["stream_id"])
            out[sid] = Entry(
                title=str(it.get("name") or sid), url=self._api.url("movie", sid, str(it.get("container_extension") or "mp4")),
                logo=str(it.get("stream_icon") or ""), group=cats.get(str(it.get("category_id")), ""), stream_id=sid,
            )  # fmt: skip
        return out


class XtreamSeries:
    lazy = True  # episodes are fetched per series when it is opened

    def __init__(self, api: XtreamApi) -> None:
        self._api = api
        self.allowed_hosts = api.allowed_hosts

    async def listing(self) -> dict[str, SeriesInfo]:
        cats = await self._api.categories("get_series_categories")
        items = await self._api.call("get_series")
        return {
            str(it["series_id"]): SeriesInfo(
                str(it.get("name") or it["series_id"]), str(it.get("cover") or ""), cats.get(str(it.get("category_id")), ""))
            for it in (items if isinstance(items, list) else ()) if isinstance(it, dict) and it.get("series_id")
        }  # fmt: skip

    async def detail(self, sid: str) -> Series | None:
        info = await self._api.call("get_series_info", series_id=sid)
        if not isinstance(info, dict) or not info.get("episodes"):
            return None
        meta = info.get("info") or {}
        s = Series(str(meta.get("name") or sid), str(meta.get("cover") or ""), "")
        eps = info["episodes"]
        groups = eps.items() if isinstance(eps, dict) else enumerate(eps, 1)
        for season, items in groups:
            for ep in items if isinstance(items, list) else ():
                if not isinstance(ep, dict) or not ep.get("id"):
                    continue
                n = int(str(ep.get("season") or season)) if str(ep.get("season") or season).isdigit() else 1
                num = int(ep["episode_num"]) if str(ep.get("episode_num", "")).isdigit() else 0
                eid = str(ep["id"])
                s.seasons.setdefault(n, []).append(Episode(
                    str(ep.get("title") or f"Folge {num}"), n, num, self._api.url("series", eid, str(ep.get("container_extension") or "mp4")), eid))  # fmt: skip
        for eps_ in s.seasons.values():
            eps_.sort(key=lambda x: x.episode)
        return s if s.seasons else None


def build_data(session, conf: dict[str, Any]):
    """Backends for a config entry's data (entries from before 0.3.0 have no `source`: Stalker)."""
    source = conf.get(CONF_SOURCE)
    if source == SOURCE_M3U:
        return StalkerData(
            M3uLive(session, conf[CONF_LIVE_M3U], conf.get(CONF_XMLTV)),
            M3uVod(session, conf[CONF_VOD_M3U]) if conf.get(CONF_VOD_M3U) else None,
            M3uSeries(session, conf[CONF_SERIES_M3U]) if conf.get(CONF_SERIES_M3U) else None,
        )  # fmt: skip
    if source == SOURCE_XTREAM:
        api = XtreamApi(session, conf[CONF_SERVER_URL], conf[CONF_USERNAME], conf[CONF_PASSWORD])
        return StalkerData(XtreamLive(api), XtreamVod(api), XtreamSeries(api))
    client = StalkerClient(session, conf[CONF_PORTAL_URL], conf[CONF_MAC])
    return StalkerData(
        StalkerLive(client),
        M3uVod(session, conf[CONF_VOD_M3U]) if conf.get(CONF_VOD_M3U) else None,
        M3uSeries(session, conf[CONF_SERIES_M3U]) if conf.get(CONF_SERIES_M3U) else None,
    )  # fmt: skip

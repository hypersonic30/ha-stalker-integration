"""HTTP API for the stalker card. The card only ever sees ids, never portal URLs or the MAC."""
from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .data import NotFound, StalkerData
from .progress import KINDS, Progress, resume_list
from .stalker import StalkerAuthError, StalkerError
from .streams import SAFE_NAME, StreamError, StreamManager

_LOGGER = logging.getLogger(__name__)
PAGE_MAX = 100
GUIDE_MAX_CHANNELS = 60
GUIDE_MAX_SPAN = 36 * 3600
MAX_DURATION = 24 * 3600
HLS_PREFIX = "/api/stalker/hls"
CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
    ".m4s": "video/iso.segment", ".mp4": "video/mp4", ".vtt": "text/vtt",
}


def _err(code: str, status: int) -> web.Response:
    return web.json_response({"code": code}, status=status)


def _state(hass: HomeAssistant, portal: str | None) -> tuple[StalkerData, StreamManager] | None:
    """Data of the asked portal (none given: the first one) and the shared stream manager."""
    d = hass.data.get(DOMAIN)
    if not d or "portals" not in d:
        return None
    portals = d["portals"]
    p = portals.get(portal) if portal else next(iter(portals.values()), None)
    return (p.data, d["streams"]) if p else None


class _Base(HomeAssistantView):
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def _run(self, fn, portal: str | None = None):
        st = _state(self.hass, portal)
        if st is None:
            if (self.hass.data.get(DOMAIN) or {}).get("portals") is not None:
                return _err("portal_not_found", 404)
            return _err("not_loaded", 503)
        try:
            return web.json_response(await fn(*st))
        except NotFound:
            return _err("not_found", 404)
        except StalkerAuthError:
            return _err("portal_auth", 502)
        except StalkerError as err:
            if str(err) == "foreign_host":
                return _err("foreign_host", 403)
            _LOGGER.warning("Portal problem: %s", err)
            return _err("portal_unreachable", 502)


class InfoView(_Base):
    url = "/api/stalker/info"
    name = "api:stalker:info"

    async def get(self, request: web.Request) -> web.Response:
        async def fn(data: StalkerData, streams: StreamManager):
            return {"vod": data.has_vod, "series": data.has_series, "epg": data.has_epg, "max_streams": streams.max_streams}

        return await self._run(fn, _portal_of(request))


class PortalsView(_Base):
    url = "/api/stalker/portals"
    name = "api:stalker:portals"

    async def get(self, request: web.Request) -> web.Response:
        d = self.hass.data.get(DOMAIN)
        if not d or "portals" not in d:
            return _err("not_loaded", 503)
        return web.json_response({"items": [
            {"id": p.id, "title": p.title, "source": p.source} for p in d["portals"].values()
        ]})


class LiveView(_Base):
    url = "/api/stalker/live"
    name = "api:stalker:live"

    async def get(self, request: web.Request) -> web.Response:
        async def fn(data: StalkerData, streams: StreamManager):
            live = await data.live()
            chans = [
                {k: c[k] for k in ("id", "name", "number", "genre_id", "logo", "has_epg")}
                for c in live["channels"].values()
            ]
            return {"genres": live["genres"], "channels": chans}

        return await self._run(fn, _portal_of(request))


class EpgView(_Base):
    url = "/api/stalker/epg/{channel_id}"
    name = "api:stalker:epg"

    async def get(self, request: web.Request, channel_id: str) -> web.Response:
        async def fn(data: StalkerData, streams: StreamManager):
            if channel_id not in (await data.live())["channels"]:
                raise NotFound
            now = time.time()
            items = [p for p in (await data.guide()).get(channel_id, []) if p["stop"] > now][:3]
            return {"items": items or await data.short_epg(channel_id)}

        return await self._run(fn, _portal_of(request))


class NowPlayingView(_Base):
    """What is on right now (and next) for every channel that has a guide: one small call."""

    url = "/api/stalker/nowplaying"
    name = "api:stalker:nowplaying"

    async def get(self, request: web.Request) -> web.Response:
        async def fn(data: StalkerData, streams: StreamManager):
            now, out = time.time(), {}
            for ch_id, progs in (await data.guide()).items():
                for i, p in enumerate(progs):
                    if p["start"] <= now < p["stop"]:
                        item = {"t": p["title"], "s": p["start"], "e": p["stop"]}
                        if i + 1 < len(progs):
                            item["n"], item["ns"] = progs[i + 1]["title"], progs[i + 1]["start"]
                        out[ch_id] = item
                        break
            return {"items": out}

        return await self._run(fn, _portal_of(request))


class GuideView(_Base):
    """Programmes of some channels in a time window (the TV guide grid)."""

    url = "/api/stalker/guide"
    name = "api:stalker:guide"

    async def get(self, request: web.Request) -> web.Response:
        try:
            ids = [i for i in request.query.get("ids", "").split(",") if i][:GUIDE_MAX_CHANNELS]
            start = float(request.query["from"])
            stop = float(request.query["to"])
        except (ValueError, KeyError):
            return _err("bad_request", 400)
        stop = min(stop, start + GUIDE_MAX_SPAN)

        async def fn(data: StalkerData, streams: StreamManager):
            guide = await data.guide()
            return {"items": {
                i: [{"t": p["title"], "d": p["descr"], "s": p["start"], "e": p["stop"]}
                    for p in guide.get(i, []) if p["stop"] > start and p["start"] < stop]
                for i in ids
            }}  # fmt: skip

        return await self._run(fn, _portal_of(request))


class VodView(_Base):
    url = "/api/stalker/vod"
    name = "api:stalker:vod"

    async def get(self, request: web.Request) -> web.Response:
        group = request.query.get("group", "")
        q = request.query.get("q", "").strip().lower()
        try:
            page = max(1, int(request.query.get("page", "1")))
            size = min(PAGE_MAX, max(1, int(request.query.get("size", "60"))))
        except ValueError:
            return _err("bad_request", 400)

        async def fn(data: StalkerData, streams: StreamManager):
            entries = await data.vod()
            groups = sorted({e.group for e in entries.values()})
            hits = [
                (i, e) for i, e in entries.items()
                if (not group or e.group == group) and (not q or q in e.title.lower())
            ]  # fmt: skip
            part = hits[(page - 1) * size : page * size]
            return {
                "groups": groups, "total": len(hits), "page": page,
                "items": [{"id": i, "title": e.title, "logo": e.logo, "group": e.group} for i, e in part],
            }  # fmt: skip

        return await self._run(fn, _portal_of(request))


class SeriesListView(_Base):
    url = "/api/stalker/series"
    name = "api:stalker:series"

    async def get(self, request: web.Request) -> web.Response:
        q = request.query.get("q", "").strip().lower()

        async def fn(data: StalkerData, streams: StreamManager):
            return {"items": [
                {"id": sid, "title": s.title, "logo": s.logo, "group": s.group, "episodes": s.episodes}
                for sid, s in (await data.series()).items() if not q or q in s.title.lower()
            ]}  # fmt: skip

        return await self._run(fn, _portal_of(request))


class SeriesView(_Base):
    url = "/api/stalker/series/{sid}"
    name = "api:stalker:series_detail"

    async def get(self, request: web.Request, sid: str) -> web.Response:
        async def fn(data: StalkerData, streams: StreamManager):
            s = await data.series_detail(sid)
            if not s:
                raise NotFound
            return {
                "id": sid, "title": s.title, "logo": s.logo or (await data.series())[sid].logo,
                "seasons": [
                    {"season": n, "episodes": [
                        {"id": e.stream_id, "episode": e.episode, "title": e.title} for e in eps]}
                    for n, eps in sorted(s.seasons.items())
                ],
            }  # fmt: skip

        return await self._run(fn, _portal_of(request))


def _pid(hass: HomeAssistant, portal: str | None) -> str | None:
    """The id of the asked portal (none given: the first one)."""
    portals = (hass.data.get(DOMAIN) or {}).get("portals") or {}
    return portal if portal in portals else (None if portal else next(iter(portals), None))


def _portal_of(request: web.Request) -> str | None:
    return request.query.get("portal") or None


def _user(request: web.Request) -> str:
    return request["hass_user"].id


class FavoritesView(_Base):
    """Favourite channels of the signed-in user in one portal."""

    url = "/api/stalker/favorites"
    name = "api:stalker:favorites"

    async def get(self, request: web.Request) -> web.Response:
        user, portal = _user(request), _pid(self.hass, _portal_of(request))

        async def fn(data: StalkerData, streams: StreamManager):
            return {"ids": self.hass.data[DOMAIN]["favorites"].ids(user, portal)}

        return await self._run(fn, _portal_of(request))

    async def post(self, request: web.Request) -> web.Response:
        try:
            body: dict[str, Any] = await request.json()
            portal_arg = str(body["portal"]) if body.get("portal") else None
            order = body.get("order")
            if order is not None:
                if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
                    return _err("bad_request", 400)
            else:
                channel_id, fav = str(body["id"]), body["fav"]
        except (ValueError, KeyError, TypeError):
            return _err("bad_request", 400)
        if order is None and not isinstance(fav, bool):
            return _err("bad_request", 400)
        user, portal = _user(request), _pid(self.hass, portal_arg)

        if order is not None:
            async def reorder(data: StalkerData, streams: StreamManager):
                favorites = self.hass.data[DOMAIN]["favorites"]
                if not favorites.reorder(user, portal, order):
                    raise _BadOrder
                return {"ids": favorites.ids(user, portal)}

            try:
                return await self._run(reorder, portal_arg)
            except _BadOrder:
                return _err("bad_request", 400)

        async def fn(data: StalkerData, streams: StreamManager):
            favorites = self.hass.data[DOMAIN]["favorites"]
            if fav and channel_id not in (await data.live())["channels"]:
                raise NotFound
            if not favorites.set(user, portal, channel_id, fav):
                raise _TooMany
            return {"ids": favorites.ids(user, portal)}

        try:
            return await self._run(fn, portal_arg)
        except _TooMany:
            return _err("too_many_favorites", 409)


class _TooMany(Exception):
    pass


class _BadOrder(Exception):
    pass


class ProgressView(_Base):
    url = "/api/stalker/progress"
    name = "api:stalker:progress"

    async def get(self, request: web.Request) -> web.Response:
        user, portal = _user(request), _pid(self.hass, _portal_of(request))

        async def fn(data: StalkerData, streams: StreamManager):
            entries = self.hass.data[DOMAIN]["progress"].entries(user, portal)
            series = {}
            for sid in {e.get("sid") for e in entries.values() if e.get("sid")}:
                if s := await data.series_detail(sid):
                    series[sid] = s
            return {
                "items": {k: {f: e.get(f) for f in ("pos", "dur", "done", "t")} for k, e in entries.items()},
                "resume": resume_list(entries, series),
            }  # fmt: skip

        return await self._run(fn, _portal_of(request))

    async def post(self, request: web.Request) -> web.Response:
        """Save a position, or (done given) mark an item as watched / not watched."""
        try:
            body: dict[str, Any] = await request.json()
            kind, item_id = str(body["kind"]), str(body["id"])
            done = body.get("done")
            pos, dur = float(body.get("pos", 0)), float(body.get("dur", 0))
            sid = str(body["sid"]) if body.get("sid") is not None else None
            portal_arg = str(body["portal"]) if body.get("portal") else None
        except (ValueError, KeyError, TypeError):
            return _err("bad_request", 400)
        if kind not in KINDS or (done is not None and not isinstance(done, bool)) or dur != dur or pos != pos:
            return _err("bad_request", 400)
        user, portal = _user(request), _pid(self.hass, portal_arg)

        async def fn(data: StalkerData, streams: StreamManager):
            progress: Progress = self.hass.data[DOMAIN]["progress"]
            meta = await data.describe(kind, item_id, sid)
            if done is None:
                progress.save(user, portal, kind, item_id, pos, min(dur, MAX_DURATION), meta)
            else:
                progress.mark(user, portal, kind, item_id, done, meta)
            return {"ok": True}

        return await self._run(fn, portal_arg)


class PlayView(_Base):
    url = "/api/stalker/play"
    name = "api:stalker:play"

    async def post(self, request: web.Request) -> web.Response:
        try:
            body: dict[str, Any] = await request.json()
            kind, item_id = str(body["kind"]), str(body["id"])
            start = max(0.0, float(body.get("start", 0)))
            hevc_ok = body.get("hevc") is True  # the player can decode HEVC itself
            audio = body.get("audio")
            audio = audio if isinstance(audio, int) and not isinstance(audio, bool) else None
            sid = str(body["sid"]) if body.get("sid") is not None else None
            portal_arg = str(body["portal"]) if body.get("portal") else None
        except (ValueError, KeyError, TypeError):
            return _err("bad_request", 400)
        if kind not in ("live", "vod", "episode"):
            return _err("bad_request", 400)

        async def fn(data: StalkerData, streams: StreamManager):
            await streams.make_room()  # old stream must be gone before the portal sees a new one
            source = await data.source_for(kind, item_id, sid)
            try:
                s = await streams.start(
                    source, live=kind == "live", start=start, hevc_ok=hevc_ok, audio=audio,
                    relink=lambda: data.source_for(kind, item_id, sid),
                )
            except StreamError as err:
                code, _, detail = str(err).partition(":")
                _LOGGER.warning("Stream failed: %s %s", code, detail)
                raise _PlayFailed(code) from None
            return {
                "session": s.id, "live": s.live, "duration": s.duration, "start": s.start_offset,
                "transcoding": s.transcoding, "hevc": s.fmp4,
                "audio": [{"index": i, "lang": lang, "codec": codec} for i, lang, codec in s.audios],
                "audio_index": s.audio_index,
                "multi_audio": s.multi,
                "subs": [{"index": i, "lang": lang} for i, lang, _ in s.subs],
                "url": f"{HLS_PREFIX}/{s.id}/index.m3u8",
            }  # fmt: skip

        try:
            return await self._run(fn, portal_arg)
        except _PlayFailed as err:
            return _err(err.code, 502)


class _PlayFailed(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class StopView(_Base):
    url = "/api/stalker/stop"
    name = "api:stalker:stop"

    async def post(self, request: web.Request) -> web.Response:
        try:
            sid = str((await request.json())["session"])
        except (ValueError, KeyError, TypeError):
            return _err("bad_request", 400)

        streams: StreamManager | None = (self.hass.data.get(DOMAIN) or {}).get("streams")
        if streams is None:
            return _err("not_loaded", 503)
        await streams.stop(sid)
        return web.json_response({"ok": True})


class HlsView(HomeAssistantView):
    """Playlist and segments. No login: <video> in the companion app cannot send headers, so the
    128-bit random session id from the authenticated /play call is the credential."""

    requires_auth = False
    url = "/api/stalker/hls/{sid}/{name}"
    name = "api:stalker:hls"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, sid: str, name: str) -> web.Response:
        streams: StreamManager | None = (self.hass.data.get(DOMAIN) or {}).get("streams")
        if streams is None or not SAFE_NAME.match(name):
            return web.Response(status=404)
        path = streams.file_for(sid, name)
        if path is None:
            return web.Response(status=404)
        try:
            body = path.read_bytes()
        except OSError:  # segment rotated away between check and read
            return web.Response(status=404)
        headers = {"Cache-Control": "no-store" if name.endswith((".m3u8", ".vtt")) else "max-age=60"}
        return web.Response(body=body, content_type=CONTENT_TYPES[path.suffix], headers=headers)


def register_views(hass: HomeAssistant) -> None:
    for view in (PortalsView, InfoView, FavoritesView, LiveView, EpgView, NowPlayingView, GuideView, ProgressView, VodView, SeriesListView, SeriesView, PlayView,
                 StopView, HlsView):  # fmt: skip
        hass.http.register_view(view(hass))

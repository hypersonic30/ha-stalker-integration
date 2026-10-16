"""Fake M3U / XMLTV / Xtream Codes servers for tests (all on one aiohttp app; media comes from the media dir)."""
import gzip
import time
from datetime import datetime, timezone

from aiohttp import web

USER, PASSWORD = "alice", "s3cret"


def xmltv_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def xmltv(now: int | None = None) -> str:
    now = now or int(time.time())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
 <channel id="one.de"><display-name>Eins HD</display-name></channel>
 <channel id="two.de"><display-name>Zwei</display-name></channel>
 <programme start="{xmltv_time(now - 600)}" stop="{xmltv_time(now + 600)}" channel="one.de"><title>Nachrichten</title><desc>{'d' * 500}</desc></programme>
 <programme start="{xmltv_time(now + 600)}" stop="{xmltv_time(now + 4200)}" channel="one.de"><title>Film &lt;b&gt;</title></programme>
 <programme start="{xmltv_time(now - 90000)}" stop="{xmltv_time(now - 86400)}" channel="one.de"><title>Gestern</title></programme>
 <programme start="{xmltv_time(now - 600)}" stop="{xmltv_time(now + 3000)}" channel="two.de"><title>Zwei-Show</title></programme>
 <programme start="{xmltv_time(now - 600)}" stop="{xmltv_time(now + 3000)}" channel="other"><title>Fremd</title></programme>
</tv>"""


def add_routes(app: web.Application, media_dir=None) -> None:
    """Routes for the fake M3U/XMLTV/Xtream server; stream URLs point back at the server itself."""

    async def live_m3u(r):
        base = str(r.url.origin())
        return web.Response(text=f"""#EXTM3U
#EXTINF:-1 tvg-id="one.de" tvg-chno="5" tvg-logo="http://l/one.png" group-title="News",Eins HD
{base}/media/live.ts
#EXTINF:-1 tvg-id="" group-title="News",Zwei
{base}/media/live.ts
#EXTINF:-1 group-title="Sport",Intern
http://10.9.8.7/secret.ts
#EXTINF:-1 group-title="Sport",Extern
http://cdn.example.com/y.ts
""")

    async def xml(r):
        return web.Response(text=xmltv(), content_type="application/xml")

    async def xml_gz(r):
        return web.Response(body=gzip.compress(xmltv().encode()), content_type="application/gzip")

    def authed(q):
        return q.get("username") == USER and q.get("password") == PASSWORD

    async def player_api(r):
        q = r.query
        if not authed(q):
            return web.json_response({"user_info": {"auth": 0}})
        act = q.get("action")
        if act is None:
            return web.json_response({"user_info": {"auth": 1, "status": "Active"}})
        data = {
            "get_live_categories": [{"category_id": "1", "category_name": "News"}],
            "get_live_streams": [
                {"num": 1, "name": "Eins HD", "stream_id": 101, "stream_icon": "i.png", "epg_channel_id": "one.de", "category_id": "1"},
                {"num": 2, "name": "Zwei", "stream_id": 102, "stream_icon": "", "epg_channel_id": "", "category_id": "1"},
            ],
            "get_vod_categories": [{"category_id": "7", "category_name": "Action"}],
            "get_vod_streams": [{"name": "Xt Movie", "stream_id": 201, "stream_icon": "m.jpg", "category_id": "7", "container_extension": "mkv"}],
            "get_series_categories": [{"category_id": "9", "category_name": "Anime"}],
            "get_series": [{"series_id": 301, "name": "Xt Show", "cover": "c.jpg", "category_id": "9"}],
        }.get(act)
        if act == "get_series_info":
            if q.get("series_id") != "301":
                return web.json_response({"episodes": []})
            data = {"info": {"name": "Xt Show", "cover": "c.jpg"}, "episodes": {
                "1": [{"id": "401", "episode_num": 1, "title": "Pilot", "container_extension": "mkv", "season": 1},
                      {"id": "402", "episode_num": 2, "title": "Zwei", "container_extension": "mkv", "season": 1}],
                "2": [{"id": "403", "episode_num": 1, "title": "Neu", "container_extension": "mkv", "season": 2}]}}
        if data is None:
            return web.Response(status=400)
        return web.json_response(data)

    app.router.add_get("/live.m3u", live_m3u)
    app.router.add_get("/guide.xml", xml)
    app.router.add_get("/guide.xml.gz", xml_gz)
    app.router.add_get("/player_api.php", player_api)

    async def xmltv_php(r):
        return await xml(r) if authed(r.query) else web.Response(status=401)

    app.router.add_get("/xmltv.php", xmltv_php)
    # Xtream stream URLs all serve the same media files
    if media_dir:
        for kind in ("live", "movie", "series"):
            app.router.add_get(
                f"/{kind}/{USER}/{PASSWORD}/{{name}}",
                lambda r, kind=kind: web.FileResponse(media_dir / ("live.ts" if kind == "live" else "movie.mkv")),
            )

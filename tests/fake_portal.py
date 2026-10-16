"""Minimal Stalker portal + media server for tests."""
import time

from aiohttp import web

MAC = "00:1A:79:AA:BB:CC"


class FakePortal:
    def __init__(self, media_dir=None):
        self.token = "tok-1"
        self.handshakes = 0
        self.calls: list[dict] = []
        self.media_dir = media_dir
        self.app = web.Application()
        self.app.router.add_get("/portal.php", self.portal)
        if media_dir:
            self.app.router.add_static("/media", media_dir, show_index=False)

    def expire_token(self):
        self.token = f"tok-{self.handshakes + 1}"

    async def portal(self, request: web.Request):
        q = request.query
        if f"mac={MAC}" not in request.headers.get("Cookie", ""):
            return web.Response(status=403)
        act = q.get("action")
        if act == "handshake":
            self.handshakes += 1
            self.token = f"tok-{self.handshakes}"
            return web.json_response({"js": {"token": self.token}})
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        self.calls.append(dict(q))
        now = int(time.time())
        js = {
            "get_profile": {"id": "1"},
            "get_genres": [{"id": "1", "title": "News"}, {"id": "*", "title": "All"}],
            "get_all_channels": {"data": [
                {"id": "10", "name": "Ch One", "number": "1", "tv_genre_id": "1",
                 "logo": "x.png", "cmd": "ffmpeg http://localhost/ch/10", "xmltv_id": "one"},
                {"id": "11", "name": "Ch Two", "number": "2", "tv_genre_id": "1",
                 "cmd": "ffmpeg http://localhost/ch/11"},
            ]},
            "get_short_epg": [
                {"name": "Now", "descr": "d", "start_timestamp": "100", "stop_timestamp": "200"}
            ],
            "get_epg_info": {"data": {
                "10": [
                    {"name": "Past", "descr": "old", "start_timestamp": str(now - 7200), "stop_timestamp": str(now - 3600)},
                    {"name": "Now", "descr": "x" * 500, "start_timestamp": str(now - 600), "stop_timestamp": str(now + 600)},
                    {"name": "Next", "descr": "n", "start_timestamp": str(now + 600), "stop_timestamp": str(now + 4200)},
                    {"name": "Broken", "descr": "b", "start_timestamp": "x", "stop_timestamp": "y"},
                ],
                "99": [{"name": "Unknown channel", "start_timestamp": str(now), "stop_timestamp": str(now + 60)}],
            }},
            "create_link": {"cmd": f"ffmpeg http://127.0.0.1:{request.url.port}/media/live.ts"},
        }.get(act)
        if js is None:
            return web.Response(status=400)
        return web.json_response({"js": js})

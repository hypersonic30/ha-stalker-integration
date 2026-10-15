"""Whole integration inside a real Home Assistant: config flow, views, real ffmpeg."""
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stalker.const import DOMAIN

from .fake_portal import MAC, FakePortal

SERIES_M3U = """#EXTM3U
#EXTINF:-1 tvg-logo="http://l/x.jpg" portal-stream-id="e2" serie-title="Show" tvg-season="1" tvg-episode="2" group-title="G",Show S01E02
{base}/media/movie.mkv
#EXTINF:-1 tvg-logo="http://l/x.jpg" portal-stream-id="e1" serie-title="Show" tvg-season="1" tvg-episode="1" group-title="G",Show S01E01
{base}/media/movie.mkv
"""
VOD_M3U = """#EXTM3U
#EXTINF:-1 portal-stream-id="m1" group-title="Action",Alpha Movie - 2020
{base}/media/movie.mkv
#EXTINF:-1 portal-stream-id="m2" group-title="Drama",Beta Movie - 2021
{base}/media/hevc.mkv
#EXTINF:-1 portal-stream-id="m3" group-title="Drama",Evil Movie
http://evil.example/steal.mkv
"""


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def portal(media):
    p = FakePortal(media)
    p.app.router.add_get("/vod.m3u", lambda r: web.Response(text=VOD_M3U.format(base=f"http://127.0.0.1:{r.url.port}")))
    p.app.router.add_get("/series.m3u", lambda r: web.Response(text=SERIES_M3U.format(base=f"http://127.0.0.1:{r.url.port}")))
    srv = TestServer(p.app)
    await srv.start_server()
    p.base = f"http://127.0.0.1:{srv.port}"
    yield p
    await srv.close()


@pytest.fixture
async def entry(hass, portal, ffmpeg):
    e = MockConfigEntry(domain=DOMAIN, data={
        "portal_url": portal.base + "/c/", "mac": MAC, "vod_m3u": portal.base + "/vod.m3u",
        "series_m3u": portal.base + "/series.m3u", "max_streams": 1,
    })  # fmt: skip
    e.add_to_hass(hass)
    mgr = MagicMock(binary=ffmpeg)
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True), patch(
        "custom_components.stalker.get_ffmpeg_manager", return_value=mgr
    ):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
        yield e
        await hass.config_entries.async_unload(e.entry_id)


async def test_config_flow_validates_with_real_handshake(hass, portal):
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        bad = await hass.config_entries.flow.async_configure(r["flow_id"], {
            "portal_url": portal.base + "/c/", "mac": "00:1A:79:00:00:00", "vod_m3u": "",
            "series_m3u": "", "max_streams": 1})  # fmt: skip
        assert bad["errors"] == {"base": "auth"}
        junk = await hass.config_entries.flow.async_configure(r["flow_id"], {
            "portal_url": portal.base, "mac": "nonsense", "vod_m3u": "", "series_m3u": "", "max_streams": 1})  # fmt: skip
        assert junk["errors"] == {"base": "invalid_mac"}
        ok = await hass.config_entries.flow.async_configure(r["flow_id"], {
            "portal_url": portal.base + "/c/", "mac": MAC.lower(), "vod_m3u": "", "series_m3u": "", "max_streams": 2})  # fmt: skip
    assert ok["type"] == "create_entry" and ok["data"]["mac"] == MAC and ok["data"]["max_streams"] == 2


async def test_requires_login(hass, entry, hass_client_no_auth):
    c = await hass_client_no_auth()
    for path in ("/api/stalker/info", "/api/stalker/live", "/api/stalker/vod", "/api/stalker/series"):
        assert (await c.get(path)).status == 401
    assert (await c.post("/api/stalker/play", json={"kind": "live", "id": "10"})).status == 401


async def test_live_list_hides_portal_secrets(hass, entry, hass_client):
    c = await hass_client()
    j = await (await c.get("/api/stalker/live")).json()
    assert [x["name"] for x in j["channels"]] == ["Ch One", "Ch Two"]
    assert j["channels"][0]["has_epg"] is True
    blob = str(j)
    assert "cmd" not in blob and "localhost/ch" not in blob and MAC not in blob


async def test_epg_and_unknown_channel(hass, entry, hass_client):
    c = await hass_client()
    assert (await (await c.get("/api/stalker/epg/10")).json())["items"][0]["title"] == "Now"
    assert (await c.get("/api/stalker/epg/999")).status == 404


async def test_vod_search_groups_and_paging(hass, entry, hass_client):
    c = await hass_client()
    j = await (await c.get("/api/stalker/vod?q=beta")).json()
    assert [i["id"] for i in j["items"]] == ["m2"] and j["total"] == 1
    j = await (await c.get("/api/stalker/vod?group=Drama&size=1&page=2")).json()
    assert j["total"] == 2 and len(j["items"]) == 1 and "Action" in j["groups"]
    assert (await c.get("/api/stalker/vod?page=x")).status == 400


async def test_series_tree(hass, entry, hass_client):
    c = await hass_client()
    lst = (await (await c.get("/api/stalker/series")).json())["items"]
    assert lst[0]["title"] == "Show" and lst[0]["episodes"] == 2
    d = await (await c.get(f"/api/stalker/series/{lst[0]['id']}")).json()
    assert [e["episode"] for e in d["seasons"][0]["episodes"]] == [1, 2]
    assert (await c.get("/api/stalker/series/nope")).status == 404


async def test_play_live_then_fetch_hls_without_login_then_stop(hass, entry, hass_client, hass_client_no_auth):
    c = await hass_client()
    r = await c.post("/api/stalker/play", json={"kind": "live", "id": "10"})
    assert r.status == 200
    j = await r.json()
    assert j["live"] is True and j["url"].startswith("/api/stalker/hls/")
    anon = await hass_client_no_auth()  # what <video> in the companion app does
    pl = await anon.get(j["url"])
    assert pl.status == 200 and "mpegurl" in pl.content_type and "#EXTINF" in await pl.text()
    seg = next(l for l in (await pl.text()).splitlines() if l.endswith(".ts"))
    s = await anon.get(j["url"].rsplit("/", 1)[0] + "/" + seg)
    assert s.status == 200 and s.content_type == "video/mp2t" and (await s.read())[:1] == b"G"
    assert (await anon.get(j["url"].replace(j["session"], "0" * 32))).status == 404
    assert (await anon.get(j["url"].rsplit("/", 1)[0] + "/..%2Fx")).status in (404, 400)
    await c.post("/api/stalker/stop", json={"session": j["session"]})
    assert (await anon.get(j["url"])).status == 404


async def test_play_vod_and_episode(hass, entry, hass_client):
    c = await hass_client()
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m1"})).json()
    assert j["live"] is False and 11 < j["duration"] < 13
    j = await (await c.post("/api/stalker/play", json={"kind": "episode", "id": "e1", "start": 2})).json()
    assert j["start"] == 2.0


async def test_play_refuses_bad_input_and_foreign_hosts(hass, entry, hass_client):
    c = await hass_client()
    p = lambda body: c.post("/api/stalker/play", json=body)  # noqa: E731
    r = await p({"kind": "vod", "id": "m3"})  # playlist points at a host the user never configured
    assert r.status == 403 and (await r.json())["code"] == "foreign_host"
    assert (await p({"kind": "vod", "id": "nope"})).status == 404
    assert (await p({"kind": "file", "id": "x"})).status == 400
    assert (await p({"kind": "vod"})).status == 400
    r = await p({"kind": "vod", "id": "m2"})  # hevc
    assert r.status == 502 and (await r.json())["code"] == "video_codec_unsupported"


async def test_new_play_stops_old_stream_before_asking_portal(hass, entry, hass_client, portal):
    c = await hass_client()
    a = await (await c.post("/api/stalker/play", json={"kind": "live", "id": "10"})).json()
    order = []
    mgr = hass.data[DOMAIN]["streams"]
    real_stop = mgr.stop

    async def spy(sid):
        order.append(sum(1 for x in portal.calls if x["action"] == "create_link"))
        await real_stop(sid)

    mgr.stop = spy
    n = sum(1 for x in portal.calls if x["action"] == "create_link")
    await c.post("/api/stalker/play", json={"kind": "live", "id": "11"})
    assert order == [n]  # the old stream was stopped BEFORE the portal got the new create_link
    assert sum(1 for x in portal.calls if x["action"] == "create_link") == n + 1
    assert a["session"] not in mgr.sessions


async def test_unload_stops_ffmpeg(hass, entry, hass_client):
    c = await hass_client()
    j = await (await c.post("/api/stalker/play", json={"kind": "live", "id": "10"})).json()
    s = hass.data[DOMAIN]["streams"].sessions[j["session"]]
    await hass.config_entries.async_unload(entry.entry_id)
    assert s.proc.returncode is not None

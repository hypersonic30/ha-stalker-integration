"""Whole integration inside a real Home Assistant: config flow, views, real ffmpeg."""
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stalker.const import DOMAIN

from .conftest import first_portal, portal_entry
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
    e = portal_entry({
        "source": "stalker", "portal_url": portal.base + "/c/", "mac": MAC, "vod_m3u": portal.base + "/vod.m3u",
        "series_m3u": portal.base + "/series.m3u",
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


async def test_transcode_option_is_on_by_default_and_reaches_the_stream_manager(hass, entry):
    assert hass.data[DOMAIN]["streams"].transcode is True


async def test_transcode_option_off_in_entry_is_applied(hass, portal, ffmpeg):
    e = portal_entry({"source": "stalker", "portal_url": portal.base + "/c/", "mac": MAC, "vod_m3u": "",
                      "series_m3u": ""}, transcode=False)  # fmt: skip
    e.add_to_hass(hass)
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True), patch(
        "custom_components.stalker.get_ffmpeg_manager", return_value=MagicMock(binary=ffmpeg)
    ):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
        assert hass.data[DOMAIN]["streams"].transcode is False
        await hass.config_entries.async_unload(e.entry_id)


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


async def test_play_hevc_is_transcoded_and_reported(hass, entry, hass_client):
    c = await hass_client()
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m2"})).json()
    assert j["transcoding"] is True
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m1"})).json()
    assert j["transcoding"] is False


async def test_play_hevc_is_copied_for_capable_players_and_served_as_fmp4(hass, entry, hass_client, hass_client_no_auth):
    c = await hass_client()
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m2", "hevc": True})).json()
    assert j["hevc"] is True and j["transcoding"] is False
    anon = await hass_client_no_auth()
    base = j["url"].rsplit("/", 1)[0]
    init = await anon.get(base + "/init.mp4")
    assert init.status == 200 and init.content_type == "video/mp4"
    pl = await (await anon.get(j["url"])).text()
    seg = next(line for line in pl.splitlines() if line.endswith(".m4s"))
    assert (await anon.get(base + "/" + seg)).content_type == "video/iso.segment"
    j2 = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m2", "hevc": "yes"})).json()
    assert j2["hevc"] is False and j2["transcoding"] is True  # only a real true counts


async def test_play_hevc_is_refused_when_transcoding_is_off(hass, entry, hass_client):
    hass.data[DOMAIN]["streams"].transcode = False
    c = await hass_client()
    r = await c.post("/api/stalker/play", json={"kind": "vod", "id": "m2"})
    assert r.status == 502 and (await r.json())["code"] == "transcode_disabled"


async def test_play_refuses_bad_input_and_foreign_hosts(hass, entry, hass_client):
    c = await hass_client()
    p = lambda body: c.post("/api/stalker/play", json=body)  # noqa: E731
    r = await p({"kind": "vod", "id": "m3"})  # playlist points at a host the user never configured
    assert r.status == 403 and (await r.json())["code"] == "foreign_host"
    assert (await p({"kind": "vod", "id": "nope"})).status == 404
    assert (await p({"kind": "file", "id": "x"})).status == 400
    assert (await p({"kind": "vod"})).status == 400


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


# -- programme guide ---------------------------------------------------------
async def test_nowplaying_and_guide_come_from_one_bulk_call(hass, entry, hass_client, portal):
    c = await hass_client()
    np = (await (await c.get("/api/stalker/nowplaying")).json())["items"]
    assert np["10"]["t"] == "Now" and np["10"]["n"] == "Next" and "11" not in np
    now = time.time()
    g = (await (await c.get(f"/api/stalker/guide?ids=10,11&from={now - 100}&to={now + 5000}")).json())["items"]
    assert [p["t"] for p in g["10"]] == ["Now", "Next"] and g["11"] == []  # Past is out of the window
    assert len(g["10"][0]["d"]) == 300  # descriptions are trimmed
    assert sum(1 for x in portal.calls if x["action"] == "get_epg_info") == 1  # cached


async def test_guide_rejects_bad_input_and_caps_channels(hass, entry, hass_client):
    c = await hass_client()
    assert (await c.get("/api/stalker/guide?ids=10&from=x&to=1")).status == 400
    assert (await c.get("/api/stalker/guide?ids=10")).status == 400
    ids = ",".join(str(i) for i in range(500))
    r = await (await c.get(f"/api/stalker/guide?ids={ids}&from=0&to=10")).json()
    assert len(r["items"]) == 60


async def test_epg_view_uses_the_guide_and_survives_a_broken_portal_guide(hass, entry, hass_client, portal):
    c = await hass_client()
    items = (await (await c.get("/api/stalker/epg/10")).json())["items"]
    assert [i["title"] for i in items] == ["Now", "Next"]
    data = first_portal(hass).data
    data._epg.value, data._epg.at = None, 0.0

    async def boom():
        from custom_components.stalker.stalker import StalkerError
        raise StalkerError("x")

    data._live_src.client.epg_all = boom
    assert (await (await c.get("/api/stalker/nowplaying")).json()) == {"items": {}}  # no error
    assert (await c.get("/api/stalker/epg/10")).status == 200  # falls back to the short EPG


# -- watch progress ------------------------------------------------------------
async def _second_user(hass, hass_client):
    u = await hass.auth.async_create_user("Zweite", group_ids=["system-users"])
    t = await hass.auth.async_create_refresh_token(u, "https://stalker.test/")
    return await hass_client(hass.auth.async_create_access_token(t))


async def test_progress_is_saved_per_user_and_never_shared(hass, entry, hass_client):
    a = await hass_client()
    b = await _second_user(hass, hass_client)
    r = await a.post("/api/stalker/progress", json={"kind": "vod", "id": "m1", "pos": 100, "dur": 1000})
    assert r.status == 200
    ja = await (await a.get("/api/stalker/progress")).json()
    assert {k: ja["items"]["vod:m1"][k] for k in ("pos", "dur", "done")} == {"pos": 100, "dur": 1000, "done": False}
    assert ja["items"]["vod:m1"]["t"] > 0
    assert ja["resume"][0]["id"] == "m1" and ja["resume"][0]["title"] == "Alpha Movie - 2020"
    jb = await (await b.get("/api/stalker/progress")).json()
    assert jb == {"items": {}, "resume": []}


async def test_progress_done_threshold_and_resume_rules(hass, entry, hass_client):
    c = await hass_client()
    post = lambda **k: c.post("/api/stalker/progress", json=k)  # noqa: E731
    await post(kind="vod", id="m1", pos=10, dur=1000)  # just opened: not offered
    j = await (await c.get("/api/stalker/progress")).json()
    assert j["resume"] == [] and j["items"]["vod:m1"]["done"] is False
    await post(kind="vod", id="m1", pos=930, dur=1000)  # 93 % = watched
    j = await (await c.get("/api/stalker/progress")).json()
    assert j["items"]["vod:m1"]["done"] is True and j["resume"] == []


async def test_series_resume_offers_the_next_episode_after_a_finished_one(hass, entry, hass_client):
    c = await hass_client()
    await c.post("/api/stalker/progress", json={"kind": "episode", "id": "e1", "pos": 95, "dur": 100})
    r = (await (await c.get("/api/stalker/progress")).json())["resume"]
    assert [(x["id"], x["label"]) for x in r] == [("e2", "Staffel 1 · Folge 2 · als Nächstes")]
    await c.post("/api/stalker/progress", json={"kind": "episode", "id": "e2", "pos": 50, "dur": 100})
    r = (await (await c.get("/api/stalker/progress")).json())["resume"]
    assert [(x["id"], x["pos"]) for x in r] == [("e2", 50)]  # unfinished episode wins
    await c.post("/api/stalker/progress", json={"kind": "episode", "id": "e2", "pos": 99, "dur": 100})
    assert (await (await c.get("/api/stalker/progress")).json())["resume"] == []  # series finished


async def test_progress_mark_watched_and_unwatched(hass, entry, hass_client):
    c = await hass_client()
    await c.post("/api/stalker/progress", json={"kind": "episode", "id": "e1", "done": True})
    j = await (await c.get("/api/stalker/progress")).json()
    assert j["items"]["episode:e1"]["done"] is True
    await c.post("/api/stalker/progress", json={"kind": "episode", "id": "e1", "done": False})
    assert (await (await c.get("/api/stalker/progress")).json())["items"] == {}


async def test_progress_rejects_bad_input_and_unknown_items(hass, entry, hass_client):
    c = await hass_client()
    post = lambda body: c.post("/api/stalker/progress", json=body)  # noqa: E731
    assert (await post({"kind": "live", "id": "10", "pos": 1, "dur": 2})).status == 400
    assert (await post({"kind": "vod", "id": "m1", "pos": "x"})).status == 400
    assert (await post({"kind": "vod", "id": "m1", "done": "yes"})).status == 400
    assert (await post({"kind": "vod", "pos": 1})).status == 400
    assert (await post({"kind": "vod", "id": "nope", "pos": 100, "dur": 1000})).status == 404
    assert (await post({"kind": "vod", "id": "m1", "pos": float("nan")})).status in (400, 200)
    anon = await (await c.get("/api/stalker/progress")).json()
    assert "vod:nope" not in anon["items"]


async def test_progress_survives_reload_and_needs_login(hass, entry, hass_client, hass_client_no_auth):
    c = await hass_client()
    await c.post("/api/stalker/progress", json={"kind": "vod", "id": "m1", "pos": 100, "dur": 1000})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert "vod:m1" in (await (await c.get("/api/stalker/progress")).json())["items"]
    anon = await hass_client_no_auth()
    assert (await anon.get("/api/stalker/progress")).status == 401
    assert (await anon.post("/api/stalker/progress", json={})).status == 401
    assert (await anon.get("/api/stalker/nowplaying")).status == 401
    assert (await anon.get("/api/stalker/guide?ids=1&from=0&to=1")).status == 401


async def test_play_reports_tracks_and_honours_the_audio_pick(hass, entry, hass_client):
    c = await hass_client()
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m1"})).json()
    assert [a["lang"] for a in j["audio"]] == ["eng", "ger"] and j["audio_index"] == 2 and j["subs"] == []
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m1", "audio": 1})).json()
    assert j["audio_index"] == 1
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "m1", "audio": "1"})).json()
    assert j["audio_index"] == 2  # not an integer: ignored

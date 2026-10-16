"""M3U + XMLTV and Xtream Codes sources inside a real Home Assistant (config flow, views, real ffmpeg)."""
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stalker.const import DOMAIN

from . import fake_sources as fs
from .conftest import portal_entry


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def srv(media):
    app = web.Application()
    fs.add_routes(app, media)
    app.router.add_static("/media", media)
    server = TestServer(app)
    await server.start_server()
    server.base = f"http://127.0.0.1:{server.port}"
    yield server
    await server.close()


async def _setup(hass, ffmpeg, data):
    e = portal_entry(data)
    e.add_to_hass(hass)
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True), patch(
        "custom_components.stalker.get_ffmpeg_manager", return_value=MagicMock(binary=ffmpeg)
    ):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


@pytest.fixture
async def m3u_entry(hass, srv, ffmpeg):
    e = await _setup(hass, ffmpeg, {"source": "m3u", "live_m3u": srv.base + "/live.m3u", "xmltv": srv.base + "/guide.xml.gz"})
    yield e
    await hass.config_entries.async_unload(e.entry_id)


@pytest.fixture
async def xtream_entry(hass, srv, ffmpeg):
    e = await _setup(hass, ffmpeg, {"source": "xtream", "server_url": srv.base, "username": fs.USER, "password": fs.PASSWORD})
    yield e
    await hass.config_entries.async_unload(e.entry_id)


# -- M3U + XMLTV ----------------------------------------------------------------------
async def test_m3u_live_list_guide_and_play(hass, m3u_entry, hass_client):
    c = await hass_client()
    assert (await (await c.get("/api/stalker/info")).json()) == {"vod": False, "series": False, "epg": True, "max_streams": 1}
    live = await (await c.get("/api/stalker/live")).json()
    chans = {x["name"]: x for x in live["channels"]}
    assert set(chans) == {"Eins HD", "Zwei", "Intern", "Extern"} and "cmd" not in chans["Eins HD"]
    np = (await (await c.get("/api/stalker/nowplaying")).json())["items"]
    assert np[chans["Eins HD"]["id"]]["t"] == "Nachrichten" and np[chans["Eins HD"]["id"]]["n"] == "Film <b>"
    assert np[chans["Zwei"]["id"]]["t"] == "Zwei-Show"  # matched by name
    j = await (await c.post("/api/stalker/play", json={"kind": "live", "id": chans["Eins HD"]["id"]})).json()
    assert j["live"] is True and j["url"].startswith("/api/stalker/hls/")
    r = await c.post("/api/stalker/play", json={"kind": "live", "id": chans["Intern"]["id"]})
    assert r.status == 403 and (await r.json())["code"] == "foreign_host"  # a LAN address only the list names


async def test_m3u_without_xmltv_reports_no_guide(hass, srv, ffmpeg, hass_client):
    e = await _setup(hass, ffmpeg, {"source": "m3u", "live_m3u": srv.base + "/live.m3u"})
    c = await hass_client()
    assert (await (await c.get("/api/stalker/info")).json())["epg"] is False
    assert (await (await c.get("/api/stalker/nowplaying")).json()) == {"items": {}}
    await hass.config_entries.async_unload(e.entry_id)


# -- Xtream Codes -------------------------------------------------------------------------
async def test_xtream_everything_is_listed_and_plays(hass, xtream_entry, hass_client):
    c = await hass_client()
    info = await (await c.get("/api/stalker/info")).json()
    assert info["vod"] and info["series"] and info["epg"]
    vod = (await (await c.get("/api/stalker/vod")).json())["items"]
    assert vod[0]["title"] == "Xt Movie" and vod[0]["group"] == "Action"
    j = await (await c.post("/api/stalker/play", json={"kind": "vod", "id": "201"})).json()
    assert j["live"] is False and 11 < j["duration"] < 13
    live = (await (await c.get("/api/stalker/live")).json())["channels"]
    assert [x["id"] for x in live] == ["101", "102"]
    j = await (await c.post("/api/stalker/play", json={"kind": "live", "id": "101"})).json()
    assert j["live"] is True
    np = (await (await c.get("/api/stalker/nowplaying")).json())["items"]
    assert np["101"]["t"] == "Nachrichten" and np["102"]["t"] == "Zwei-Show"


async def test_xtream_series_are_fetched_per_series_and_episodes_need_their_series(hass, xtream_entry, hass_client):
    c = await hass_client()
    lst = (await (await c.get("/api/stalker/series")).json())["items"]
    assert lst == [{"id": "301", "title": "Xt Show", "logo": "c.jpg", "group": "Anime", "episodes": 0}]
    d = await (await c.get("/api/stalker/series/301")).json()
    assert [s["season"] for s in d["seasons"]] == [1, 2] and d["seasons"][0]["episodes"][0]["id"] == "401"
    assert (await c.get("/api/stalker/series/999")).status == 404
    play = lambda **b: c.post("/api/stalker/play", json={"kind": "episode", **b})  # noqa: E731
    assert (await play(id="401")).status == 404  # which series? (one lookup per series only)
    assert (await play(id="401", sid="999")).status == 404
    assert (await play(id="401", sid="301")).status == 200


async def test_xtream_progress_and_next_episode(hass, xtream_entry, hass_client):
    c = await hass_client()
    post = lambda **b: c.post("/api/stalker/progress", json=b)  # noqa: E731
    assert (await post(kind="episode", id="401", pos=95, dur=100)).status == 404  # no sid: unknown
    assert (await post(kind="episode", id="401", sid="301", pos=95, dur=100)).status == 200
    r = (await (await c.get("/api/stalker/progress")).json())["resume"]
    assert [(x["id"], x["sid"], x["label"]) for x in r] == [("402", "301", "Staffel 1 · Folge 2 · als Nächstes")]
    assert r[0]["title"] == "Xt Show" and r[0]["logo"] == "c.jpg"

"""M3U live, XMLTV and Xtream Codes backends against fake servers."""
import asyncio
import gzip
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.stalker import sources, xmltv
from custom_components.stalker.sources import M3uLive, M3uSeries, XtreamApi, XtreamLive, XtreamSeries, XtreamVod, fetch
from custom_components.stalker.stalker import StalkerAuthError, StalkerError

from . import fake_sources as fs


@pytest.fixture
async def server():
    app = web.Application()
    fs.add_routes(app)
    srv = TestServer(app)
    await srv.start_server()
    base = f"http://127.0.0.1:{srv.port}"
    yield base
    await srv.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


# -- XMLTV -------------------------------------------------------------------
def test_xmltv_parses_times_with_zone_and_trims_descriptions():
    now = 1_800_000_000
    xml = f"""<tv><channel id="a"><display-name>Das Erste HD</display-name></channel>
    <programme start="20270115080000 +0200" stop="20270115090000 +0200" channel="a"><title>T</title><desc>{'x' * 999}</desc></programme></tv>"""
    ts = xmltv.parse_time("20270115080000 +0200")
    progs, names = xmltv.parse_xmltv(xml.encode(), now=ts)
    assert progs["a"][0]["start"] == ts and progs["a"][0]["stop"] == ts + 3600
    assert len(progs["a"][0]["descr"]) == xmltv.DESCR_MAX
    assert names[xmltv.norm("das erste hd")] == "a"
    assert xmltv.parse_time("20270115080000 +0200") == xmltv.parse_time("20270115060000 +0000") == xmltv.parse_time("20270115060000")
    assert xmltv.parse_time("nonsense") is None and xmltv.parse_time("") is None


def test_xmltv_keeps_only_the_window_and_reads_gzip():
    raw = fs.xmltv(1_800_000_000).encode()
    for blob in (raw, gzip.compress(raw)):
        progs, _ = xmltv.parse_xmltv(blob, now=1_800_000_000)
        assert [p["title"] for p in progs["one.de"]] == ["Nachrichten", "Film <b>"]  # yesterday dropped
        assert len(progs["one.de"][0]["descr"]) == 300


def test_xmltv_garbage_and_oversize_are_refused(monkeypatch):
    with pytest.raises(xmltv.XmltvError):
        xmltv.parse_xmltv(b"not xml at all")
    monkeypatch.setattr(xmltv, "MAX_UNPACKED", 200)
    with pytest.raises(xmltv.XmltvError, match="too_large"):
        xmltv.parse_xmltv(gzip.compress(fs.xmltv().encode()))


def test_xmltv_cut_off_file_still_yields_what_came_before():
    raw = fs.xmltv(1_800_000_000).encode()
    progs, _ = xmltv.parse_xmltv(raw[: raw.index(b"channel=\"two.de\"")], now=1_800_000_000)
    assert "one.de" in progs


# -- fetch -------------------------------------------------------------------
async def test_fetch_reads_everything_even_when_the_body_arrives_in_pieces(session):
    async def slow(r):
        resp = web.StreamResponse()
        await resp.prepare(r)
        for _ in range(20):
            await resp.write(b"x" * 100_000)
            await asyncio.sleep(0.01)
        return resp

    app = web.Application()
    app.router.add_get("/big", slow)
    srv = TestServer(app)
    await srv.start_server()
    try:
        assert len(await fetch(session, f"http://127.0.0.1:{srv.port}/big", 10_000_000)) == 2_000_000
        with pytest.raises(StalkerError, match="too_large"):
            await fetch(session, f"http://127.0.0.1:{srv.port}/big", 500_000)
    finally:
        await srv.close()


async def test_fetch_errors_never_contain_the_url_or_password(session):
    with pytest.raises(StalkerError) as e:
        await fetch(session, "http://127.0.0.1:9/p?password=hunter2")
    assert "hunter2" not in str(e.value) and "127.0.0.1" not in str(e.value)


# -- M3U live ------------------------------------------------------------------
async def test_m3u_live_listing_ids_genres_and_numbers(server, session):
    genres, chans = await M3uLive(session, server + "/live.m3u").live_listing()
    assert {g["title"] for g in genres} == {"News", "Sport"}
    one = chans[0]
    assert one["name"] == "Eins HD" and one["number"] == "5" and one["logo"] == "http://l/one.png" and one["xmltv_id"] == "one.de"
    assert chans[1]["number"] == "2"  # running number without tvg-chno
    assert len({c["id"] for c in chans}) == 4


async def test_m3u_live_ids_survive_changing_stream_urls(session):
    lists = {"v": "A"}

    async def m3u(r):
        return web.Response(text=f"#EXTM3U\n#EXTINF:-1 tvg-id=\"x\",Same\nhttp://h/{lists['v']}.ts\n#EXTINF:-1,Same\nhttp://h/b.ts\n")

    app = web.Application()
    app.router.add_get("/l.m3u", m3u)
    srv = TestServer(app)
    await srv.start_server()
    try:
        live = M3uLive(session, f"http://127.0.0.1:{srv.port}/l.m3u")
        first = [c["id"] for c in (await live.live_listing())[1]]
        lists["v"] = "ROTATED-TOKEN"
        assert [c["id"] for c in (await live.live_listing())[1]] == first
    finally:
        await srv.close()


async def test_m3u_live_host_rules(server, session):
    live = M3uLive(session, server + "/live.m3u")
    await live.live_listing()
    assert "cdn.example.com" in " ".join(live.allowed_hosts)  # a public host named in the user's own list
    assert not any("10.9.8.7" in h for h in live.allowed_hosts)  # a LAN address only the list names is not trusted
    assert sources.is_private_host("http://192.168.1.2:80".split("//")[1]) and sources.is_private_host("localhost:8080")
    assert not sources.is_private_host("cdn.example.com")


async def test_m3u_live_epg_maps_by_id_then_by_name(server, session):
    live = M3uLive(session, server + "/live.m3u", server + "/guide.xml")
    _, chans = await live.live_listing()
    guide = await live.epg_all()
    by_name = {c["name"]: c["id"] for c in chans}
    assert guide[by_name["Eins HD"]][0]["title"] == "Nachrichten"  # by tvg-id
    assert guide[by_name["Zwei"]][0]["title"] == "Zwei-Show"  # no tvg-id: by display name
    assert by_name["Intern"] not in guide and by_name["Extern"] not in guide
    gz = M3uLive(session, server + "/live.m3u", server + "/guide.xml.gz")
    await gz.live_listing()
    assert len(await gz.epg_all()) == 2
    assert await M3uLive(session, server + "/live.m3u").epg_all() == {}


async def test_m3u_without_channels_is_an_error(session):
    app = web.Application()
    app.router.add_get("/e.m3u", lambda r: web.Response(text="#EXTM3U\n"))
    srv = TestServer(app)
    await srv.start_server()
    try:
        with pytest.raises(StalkerError, match="no_channels"):
            await M3uLive(session, f"http://127.0.0.1:{srv.port}/e.m3u").live_listing()
    finally:
        await srv.close()


# -- Xtream ----------------------------------------------------------------------
def test_xtream_base_url_is_normalised():
    assert sources.xtream_base("http://h:8080/") == "http://h:8080"
    assert sources.xtream_base("http://h:8080/player_api.php") == "http://h:8080"
    assert sources.xtream_base("http://h/get.php") == "http://h"
    with pytest.raises(StalkerError, match="invalid_url"):
        sources.xtream_base("nonsense")


async def test_xtream_login(server, session):
    await XtreamApi(session, server, fs.USER, fs.PASSWORD).login()
    with pytest.raises(StalkerAuthError):
        await XtreamApi(session, server, fs.USER, "wrong").login()


async def test_xtream_live_vod_series(server, session):
    api = XtreamApi(session, server, fs.USER, fs.PASSWORD)
    genres, chans = await XtreamLive(api).live_listing()
    assert genres == [{"id": "1", "title": "News"}]
    assert chans[0]["id"] == "101" and chans[0]["cmd"] == f"{server}/live/{fs.USER}/{fs.PASSWORD}/101.ts" and chans[0]["has_epg"] and not chans[1]["has_epg"]
    vod = await XtreamVod(api).entries()
    assert vod["201"].title == "Xt Movie" and vod["201"].group == "Action" and vod["201"].url.endswith("/movie/alice/s3cret/201.mkv")
    ser = XtreamSeries(api)
    listing = await ser.listing()
    assert listing["301"].title == "Xt Show" and listing["301"].group == "Anime"
    detail = await ser.detail("301")
    assert sorted(detail.seasons) == [1, 2] and [e.stream_id for e in detail.seasons[1]] == ["401", "402"]
    assert detail.seasons[1][0].url.endswith("/series/alice/s3cret/401.mkv")
    assert await ser.detail("999") is None


async def test_xtream_epg_comes_from_xmltv_php(server, session):
    live = XtreamLive(XtreamApi(session, server, fs.USER, fs.PASSWORD))
    guide = await live.epg_all()
    assert guide["101"][0]["title"] == "Nachrichten" and guide["102"][0]["title"] == "Zwei-Show"  # by name
    bad = XtreamLive(XtreamApi(session, server, fs.USER, "wrong"))
    with pytest.raises(StalkerError):
        await bad.epg_all()


async def test_xtream_errors_hide_the_password(session):
    with pytest.raises(StalkerError) as e:
        await XtreamApi(session, "http://127.0.0.1:9", "bob", "hunter2").login()
    assert "hunter2" not in str(e.value) and "bob" not in str(e.value)


async def test_xtream_episode_lists_given_as_list_of_lists(session):
    class Api:
        allowed_hosts = set()

        def url(self, *a):
            return "http://h/x"

        async def call(self, action, **k):
            return {"info": {"name": "N"}, "episodes": [[{"id": "1", "episode_num": "3", "title": "t", "container_extension": "mp4"}]]}

    detail = await XtreamSeries(Api()).detail("1")
    assert detail.seasons[1][0].episode == 3

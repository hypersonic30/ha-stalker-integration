"""Several portals at once: main entry + portal sub-entries, per-portal data, favourites, migration."""
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stalker.const import DOMAIN

from . import fake_sources as fs
from .conftest import portal_entry
from .fake_portal import MAC
from .test_integration import _second_user, portal  # noqa: F401  (fixtures)
from .test_sources_integration import srv  # noqa: F401


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _ffmpeg_everywhere(ffmpeg):
    """Reloads (after adding a portal) must find ffmpeg too, not only the first setup."""
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True), _patch(ffmpeg):
        yield


def _patch(ffmpeg):
    return patch("custom_components.stalker.get_ffmpeg_manager", return_value=MagicMock(binary=ffmpeg))


async def _load(hass, ffmpeg, entry):
    entry.add_to_hass(hass)
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True), _patch(ffmpeg):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _stalker(portal):
    return {"source": "stalker", "portal_url": portal.base + "/c/", "mac": MAC, "vod_m3u": "", "series_m3u": ""}


def _xtream(srv):
    return {"source": "xtream", "server_url": srv.base, "username": fs.USER, "password": fs.PASSWORD}


@pytest.fixture
async def two(hass, ffmpeg, portal, srv):  # noqa: F811
    e = await _load(hass, ffmpeg, portal_entry(_stalker(portal), "Zuhause", ("Anbieter B", _xtream(srv))))
    ids = {s.title: s.subentry_id for s in e.subentries.values()}
    yield e, ids
    await hass.config_entries.async_unload(e.entry_id)


# -- config flows ---------------------------------------------------------------------------
async def test_main_flow_takes_only_the_shared_settings(hass):
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert r["type"] == "form" and r["step_id"] == "user"
        ok = await hass.config_entries.flow.async_configure(r["flow_id"], {"max_streams": 2, "transcode": False})
    assert ok["type"] == "create_entry" and ok["data"] == {"max_streams": 2, "transcode": False}
    assert ok["version"] == 2


async def test_main_reconfigure_changes_shared_settings(hass, two):
    e, _ = two
    with patch("homeassistant.components.ffmpeg.async_setup", return_value=True):
        r = await e.start_reconfigure_flow(hass)
        assert r["step_id"] == "reconfigure"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"max_streams": 3, "transcode": False})
    assert r["type"] == "abort" and e.data == {"max_streams": 3, "transcode": False}


async def _sub(hass, e, step, data):
    r = await hass.config_entries.subentries.async_init((e.entry_id, "portal"), context={"source": "user"})
    assert r["type"] == "menu" and r["menu_options"] == ["stalker", "m3u", "xtream"]
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"next_step_id": step})
    assert r["type"] == "form" and r["step_id"] == step
    return await hass.config_entries.subentries.async_configure(r["flow_id"], data)


async def test_add_stalker_portal_checks_mac_and_names_it(hass, two, portal):  # noqa: F811
    e, _ = two
    base = {"name": "Dritter", "portal_url": portal.base + "/c/", "vod_m3u": "", "series_m3u": ""}
    bad = await _sub(hass, e, "stalker", {**base, "mac": "00:1A:79:00:00:00"})
    assert bad["errors"] == {"base": "auth"}
    bad = await hass.config_entries.subentries.async_configure(bad["flow_id"], {**base, "mac": "nonsense"})
    assert bad["errors"] == {"base": "invalid_mac"}
    ok = await hass.config_entries.subentries.async_configure(bad["flow_id"], {**base, "mac": MAC.lower()})
    assert ok["type"] == "create_entry" and ok["title"] == "Dritter"
    sub = next(s for s in e.subentries.values() if s.title == "Dritter")
    assert sub.data["mac"] == MAC and sub.data["source"] == "stalker" and "name" not in sub.data
    await hass.async_block_till_done()
    assert len((await _portals(hass, e))) == 3  # the entry reloaded with the new portal


async def test_add_m3u_and_xtream_portals_are_validated(hass, two, srv):  # noqa: F811
    e, _ = two
    bad = await _sub(hass, e, "m3u", {"name": "L", "live_m3u": srv.base + "/nothing"})
    assert bad["errors"] == {"base": "cannot_connect"}
    bad = await hass.config_entries.subentries.async_configure(bad["flow_id"], {"name": "L", "live_m3u": "ftp://x/y"})
    assert bad["errors"] == {"base": "invalid_url"}
    ok = await hass.config_entries.subentries.async_configure(
        bad["flow_id"], {"name": "Liste", "live_m3u": srv.base + "/live.m3u", "xmltv": ""})  # fmt: skip
    assert ok["type"] == "create_entry" and ok["title"] == "Liste"
    bad = await _sub(hass, e, "xtream", {"name": "X", "server_url": srv.base, "username": fs.USER, "password": "wrong"})
    assert bad["errors"] == {"base": "auth"}


async def test_reconfigure_portal_keeps_its_source_and_can_rename(hass, two, srv):  # noqa: F811
    e, ids = two
    r = await hass.config_entries.subentries.async_init(
        (e.entry_id, "portal"), context={"source": "reconfigure", "subentry_id": ids["Anbieter B"]})  # fmt: skip
    assert r["type"] == "form" and r["step_id"] == "reconfigure_xtream"
    r = await hass.config_entries.subentries.async_configure(
        r["flow_id"], {"name": "Neu", "server_url": srv.base, "username": fs.USER, "password": fs.PASSWORD})  # fmt: skip
    assert r["type"] == "abort"
    sub = e.subentries[ids["Anbieter B"]]
    assert sub.title == "Neu" and sub.data["source"] == "xtream"


# -- using several portals ------------------------------------------------------------------
async def _portals(hass, e):
    from homeassistant.helpers import entity_registry  # noqa: F401

    return hass.data[DOMAIN]["portals"]


async def test_portal_list_and_default_portal(hass, two, hass_client):
    e, ids = two
    c = await hass_client()
    j = await (await c.get("/api/stalker/portals")).json()
    assert j["items"] == [
        {"id": ids["Zuhause"], "title": "Zuhause", "source": "stalker"},
        {"id": ids["Anbieter B"], "title": "Anbieter B", "source": "xtream"},
    ]  # fmt: skip
    first = [x["name"] for x in (await (await c.get("/api/stalker/live")).json())["channels"]]
    assert first == ["Ch One", "Ch Two"]  # no portal given: the first one


async def test_each_portal_has_its_own_channels_and_capabilities(hass, two, hass_client):
    e, ids = two
    c = await hass_client()
    a = await (await c.get(f"/api/stalker/live?portal={ids['Zuhause']}")).json()
    b = await (await c.get(f"/api/stalker/live?portal={ids['Anbieter B']}")).json()
    assert [x["id"] for x in b["channels"]] == ["101", "102"] and a != b
    info = await (await c.get(f"/api/stalker/info?portal={ids['Anbieter B']}")).json()
    assert info["vod"] and info["series"]
    info = await (await c.get(f"/api/stalker/info?portal={ids['Zuhause']}")).json()
    assert not info["vod"] and not info["series"]
    r = await c.get("/api/stalker/live?portal=nope")
    assert r.status == 404 and (await r.json())["code"] == "portal_not_found"


async def test_play_on_the_chosen_portal_and_one_stream_across_portals(hass, two, hass_client):
    e, ids = two
    c = await hass_client()
    j = await (await c.post("/api/stalker/play", json={"kind": "live", "id": "101", "portal": ids["Anbieter B"]})).json()
    first = j["session"]
    assert j["live"] is True
    j2 = await (await c.post("/api/stalker/play", json={"kind": "live", "id": "10", "portal": ids["Zuhause"]})).json()
    assert j2["session"] != first
    assert list(hass.data[DOMAIN]["streams"].sessions) == [j2["session"]]  # the limit is shared by all portals
    assert (await c.post("/api/stalker/stop", json={"session": j2["session"]})).status == 200
    assert (await c.post("/api/stalker/play", json={"kind": "live", "id": "10", "portal": "nope"})).status == 404


async def test_progress_is_kept_per_portal(hass, two, hass_client):
    e, ids = two
    c = await hass_client()
    a, b = ids["Zuhause"], ids["Anbieter B"]
    await c.post("/api/stalker/progress", json={"kind": "vod", "id": "201", "pos": 100, "dur": 1000, "portal": b})
    jb = await (await c.get(f"/api/stalker/progress?portal={b}")).json()
    ja = await (await c.get(f"/api/stalker/progress?portal={a}")).json()
    assert "vod:201" in jb["items"] and ja["items"] == {}


async def test_favorites_per_user_and_portal(hass, two, hass_client):
    e, ids = two
    a, b = ids["Zuhause"], ids["Anbieter B"]
    c = await hass_client()
    post = lambda **k: c.post("/api/stalker/favorites", json=k)  # noqa: E731
    assert (await (await c.get(f"/api/stalker/favorites?portal={a}")).json()) == {"ids": []}
    assert (await (await post(id="11", fav=True, portal=a)).json()) == {"ids": ["11"]}
    assert (await (await post(id="10", fav=True, portal=a)).json()) == {"ids": ["11", "10"]}  # order kept
    assert (await (await post(id="10", fav=True, portal=a)).json()) == {"ids": ["11", "10"]}  # no duplicate
    await post(id="101", fav=True, portal=b)
    assert (await (await c.get(f"/api/stalker/favorites?portal={b}")).json()) == {"ids": ["101"]}
    assert (await (await post(id="11", fav=False, portal=a)).json()) == {"ids": ["10"]}
    # a second user sees nothing of this
    other = await _second_user(hass, hass_client)
    assert (await (await other.get(f"/api/stalker/favorites?portal={a}")).json()) == {"ids": []}
    # validation
    assert (await post(id="999", fav=True, portal=a)).status == 404  # not a channel of this portal
    assert (await post(id="10", fav=True, portal=b)).status == 404  # channel of the other portal
    assert (await post(id="10", fav="yes", portal=a)).status == 400
    assert (await c.post("/api/stalker/favorites", json={"fav": True})).status == 400
    assert (await post(id="999", fav=False, portal=a)).status == 200  # removing something unknown is fine


async def test_favorites_require_login_and_are_capped(hass, two, hass_client, hass_client_no_auth):
    e, ids = two
    anon = await hass_client_no_auth()
    assert (await anon.get("/api/stalker/favorites")).status == 401
    assert (await anon.post("/api/stalker/favorites", json={})).status == 401
    from custom_components.stalker import favorites as fav

    with patch.object(fav, "MAX_FAVORITES", 1):
        c = await hass_client()
        assert (await c.post("/api/stalker/favorites", json={"id": "10", "fav": True})).status == 200
        r = await c.post("/api/stalker/favorites", json={"id": "11", "fav": True})
        assert r.status == 409 and (await r.json())["code"] == "too_many_favorites"


async def test_removing_a_portal_forgets_its_favorites_and_progress(hass, two, hass_client):
    e, ids = two
    c = await hass_client()
    b = ids["Anbieter B"]
    await c.post("/api/stalker/favorites", json={"id": "101", "fav": True, "portal": b})
    await c.post("/api/stalker/progress", json={"kind": "vod", "id": "201", "pos": 100, "dur": 1000, "portal": b})
    hass.config_entries.async_remove_subentry(e, b)
    await hass.async_block_till_done()
    assert b not in hass.data[DOMAIN]["portals"]
    assert all(b not in mine for mine in hass.data[DOMAIN]["favorites"]._data.values())
    assert all(b not in mine for mine in hass.data[DOMAIN]["progress"]._data.values())


# -- upgrade from 0.1-0.3 -------------------------------------------------------------------
async def test_old_single_source_entry_becomes_the_first_portal(hass, ffmpeg, portal, hass_client, hass_storage, hass_admin_user):  # noqa: F811
    hass_storage["stalker_progress"] = {
        "version": 1, "minor_version": 1,
        "data": {hass_admin_user.id: {"vod:m1": {"title": "Alpha", "logo": "", "pos": 100, "dur": 1000, "done": False, "t": 1.0}}},
    }  # fmt: skip
    old = MockConfigEntry(domain=DOMAIN, version=1, data={
        "portal_url": portal.base + "/c/", "mac": MAC, "vod_m3u": "", "series_m3u": "", "max_streams": 2, "transcode": False,
    })  # fmt: skip
    await _load(hass, ffmpeg, old)
    assert old.version == 2 and old.data == {"max_streams": 2, "transcode": False}
    (sub,) = old.subentries.values()
    assert sub.subentry_type == "portal" and sub.title == "Stalker"
    assert sub.data["source"] == "stalker" and sub.data["mac"] == MAC and "max_streams" not in sub.data
    c = await hass_client()
    j = await (await c.get("/api/stalker/progress")).json()
    assert j["items"]["vod:m1"]["pos"] == 100  # the old watch list now belongs to this portal
    assert [x["name"] for x in (await (await c.get("/api/stalker/live")).json())["channels"]] == ["Ch One", "Ch Two"]
    await hass.config_entries.async_unload(old.entry_id)


async def test_old_xtream_entry_migrates_with_its_title(hass, ffmpeg, srv):  # noqa: F811
    old = MockConfigEntry(domain=DOMAIN, version=1, data={**_xtream(srv), "max_streams": 1})
    await _load(hass, ffmpeg, old)
    (sub,) = old.subentries.values()
    assert sub.title == "Xtream" and sub.data["username"] == fs.USER
    await hass.config_entries.async_unload(old.entry_id)

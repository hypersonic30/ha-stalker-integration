import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.stalker.stalker import (
    StalkerAuthError, StalkerClient, StalkerError,
)

from .fake_portal import MAC, FakePortal


@pytest.fixture
async def portal():
    p = FakePortal()
    srv = TestServer(p.app)
    await srv.start_server()
    async with aiohttp.ClientSession() as session:
        yield p, srv, session
    await srv.close()


def client(srv, session, mac=MAC):
    return StalkerClient(session, f"http://127.0.0.1:{srv.port}/c/", mac)


async def test_channels_genres_epg(portal):
    p, srv, session = portal
    c = client(srv, session)
    ch = await c.channels()
    assert [x["name"] for x in ch] == ["Ch One", "Ch Two"] and ch[0]["xmltv_id"] == "one"
    assert (await c.genres())[0] == {"id": "1", "title": "News"}
    assert (await c.epg("10"))[0]["start"] == 100
    assert p.handshakes == 1  # token reused


async def test_rehandshake_after_token_expiry(portal):
    p, srv, session = portal
    c = client(srv, session)
    await c.channels()
    p.token = "rotated-by-server"  # old token now rejected with 401
    assert len(await c.channels()) == 2
    assert p.handshakes == 2


async def test_create_link_strips_ffmpeg_prefix(portal):
    _, srv, session = portal
    url = await client(srv, session).create_link("ffmpeg http://localhost/ch/10")
    assert url.startswith("http://127.0.0.1:") and url.endswith("/media/live.ts")


async def test_wrong_mac_is_an_error_without_leaking_it(portal):
    _, srv, session = portal
    c = client(srv, session, "00:1A:79:00:00:00")
    with pytest.raises(StalkerError) as ei:
        await c.channels()
    assert "00:1A:79" not in str(ei.value) and str(srv.port) not in str(ei.value)


async def test_unreachable_portal_error_has_no_url(portal):
    _, srv, session = portal
    c = StalkerClient(session, "http://127.0.0.1:1/c/", MAC)
    with pytest.raises(StalkerError) as ei:
        await c.channels()
    assert "127.0.0.1" not in str(ei.value)


def test_invalid_mac_rejected():
    with pytest.raises(StalkerError):
        StalkerClient(None, "http://x/c/", "not-a-mac")

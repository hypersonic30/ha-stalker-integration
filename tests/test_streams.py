import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.stalker import streams
from custom_components.stalker.streams import (
    StreamError, StreamManager, build_command, parse_probe,
)

PROBE = """Input #0, matroska,webm, from 'http://h/x':
  Duration: 01:02:03.50, start: 0.000000, bitrate: 4997 kb/s
  Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 25 fps
  Stream #0:1(eng): Audio: ac3, 48000 Hz, 5.1(side), fltp
  Stream #0:2(ger): Audio: dts (DTS), 48000 Hz, 5.1
  Stream #0:3(ger): Subtitle: hdmv_pgs_subtitle
"""


def test_parse_probe_prefers_german_audio_and_reads_duration():
    p = parse_probe(PROBE)
    assert (p.video_codec, p.audio_index, p.audio_codec) == ("h264", 2, "dts")
    assert p.duration == 3723.5


def test_parse_probe_falls_back_to_first_audio():
    p = parse_probe(PROBE.replace("(ger)", "(fra)"))
    assert p.audio_index == 1


def test_command_never_reencodes_video_and_limits_protocols(tmp_path):
    cmd = build_command("ffmpeg", "http://x/y", tmp_path, live=True, use_nice=False)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "libx264" not in cmd and "-vf" not in cmd and "-ss" not in cmd and "-readrate" not in cmd
    assert cmd[cmd.index("-seekable") + 1] == "0"  # one connection: the portal rate-limits extras
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "http,https,tcp,tls,crypto"
    vod = build_command("ffmpeg", "http://x/y", tmp_path, live=False, start=30,
                        audio_index=2, audio_codec="aac", use_nice=False)  # fmt: skip
    assert vod[vod.index("-ss") + 1] == "30.000" and "-readrate" not in vod
    assert vod[vod.index("-c:a") + 1] == "copy" and "0:2" in vod


@pytest.fixture
async def server(media):
    app = web.Application()
    app.router.add_static("/media", media)
    srv = TestServer(app)
    await srv.start_server()
    yield f"http://127.0.0.1:{srv.port}/media"
    await srv.close()


@pytest.fixture
async def mgr(ffmpeg, tmp_path):
    m = StreamManager(ffmpeg, tmp_path / "hls", max_streams=1)
    await m.start_reaper()
    yield m
    await m.shutdown()


def ffinfo(ffmpeg, path: Path) -> str:
    return subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True).stderr  # fmt: skip


async def test_live_start_serves_playlist_and_segments(mgr, server):
    s = await mgr.start(f"{server}/live.ts", live=True)
    idx = mgr.file_for(s.id, "index.m3u8")
    assert idx and "#EXTINF" in idx.read_text()
    seg = next(f for f in s.dir.glob("seg*.ts"))
    assert mgr.file_for(s.id, seg.name) == seg


async def test_vod_transcodes_audio_to_aac_and_keeps_video(mgr, server, ffmpeg):
    s = await mgr.start(f"{server}/movie.mkv", live=False)
    assert s.duration and 11 < s.duration < 13
    info = ffinfo(ffmpeg, next(s.dir.glob("seg*.ts")))
    assert "Video: h264" in info and "Audio: aac" in info and "ac3" not in info


async def test_vod_picks_german_track(mgr, server, monkeypatch):
    seen = {}
    real = streams.build_command
    monkeypatch.setattr(streams, "build_command",
                        lambda *a, **k: seen.update(k) or real(*a, **k))  # fmt: skip
    await mgr.start(f"{server}/movie.mkv", live=False)
    assert seen["audio_index"] == 2


async def test_hevc_is_reencoded_to_h264_capped_at_720p(mgr, server, ffmpeg):
    s = await mgr.start(f"{server}/hevc.mkv", live=False)
    assert s.transcoding
    info = ffinfo(ffmpeg, next(s.dir.glob("seg*.ts")))
    assert "Video: h264" in info and "Video: hevc" not in info and "Audio: aac" in info


async def test_transcoding_can_be_switched_off(mgr, server):
    mgr.transcode = False
    with pytest.raises(StreamError, match="transcode_disabled"):
        await mgr.start(f"{server}/hevc.mkv", live=False)
    assert not mgr.sessions and not list(mgr.base_dir.iterdir())
    s = await mgr.start(f"{server}/movie.mkv", live=False)  # h264 still plays
    assert not s.transcoding


async def test_hevc_is_copied_as_fmp4_when_the_player_can_decode_it(mgr, server, ffmpeg):
    s = await mgr.start(f"{server}/hevc.mkv", live=False, hevc_ok=True)
    assert s.fmp4 and not s.transcoding
    idx = mgr.file_for(s.id, "index.m3u8").read_text()
    assert "#EXT-X-MAP" in idx and "init.mp4" in idx and ".m4s" in idx
    init = mgr.file_for(s.id, "init.mp4")
    assert init and mgr.file_for(s.id, next(f.name for f in s.dir.glob("seg*.m4s")))
    info = ffinfo(ffmpeg, init)
    assert "Video: hevc" in info and "hvc1" in info


async def test_hevc_flag_is_ignored_for_other_codecs(mgr, server):
    s = await mgr.start(f"{server}/movie.mkv", live=False, hevc_ok=True)
    assert not s.fmp4 and not s.transcoding


async def test_probe_failure_text_is_not_reported_as_a_codec_problem(mgr, monkeypatch):
    async def probe(source):
        return streams.Probe(None, None, None, None, tail=("Invalid data found when processing input",))

    monkeypatch.setattr(mgr, "probe", probe)
    with pytest.raises(StreamError, match="stream_failed"):
        await mgr.start("http://127.0.0.1:9/x.mkv", live=False)


def test_fmp4_command_uses_hvc1_tag_and_init_segment(tmp_path):
    cmd = build_command("ffmpeg", "http://x/y", tmp_path, live=False, fmp4=True, use_nice=False)
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-tag:v") + 1] == "hvc1"
    assert cmd[cmd.index("-hls_segment_type") + 1] == "fmp4" and str(tmp_path / "seg%d.m4s") in cmd


async def test_h264_is_never_reencoded(mgr, server):
    s = await mgr.start(f"{server}/movie.mkv", live=False)
    assert not s.transcoding


async def test_source_without_video_is_refused_without_starting_ffmpeg(mgr, server, monkeypatch):
    async def probe(source):
        return streams.Probe(None, None, None, 10.0)

    monkeypatch.setattr(mgr, "probe", probe)
    with pytest.raises(StreamError, match="video_codec_unsupported"):
        await mgr.start(f"{server}/movie.mkv", live=False)
    assert not mgr.sessions and not list(mgr.base_dir.iterdir())


def test_transcode_command_scales_down_and_cuts_segments_at_keyframes(tmp_path):
    cmd = build_command("ffmpeg", "http://x/y", tmp_path, live=False, transcode=True, use_nice=False)
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "min(720,ih)" in cmd[cmd.index("-vf") + 1]
    assert "expr:gte(t,n_forced*4)" in cmd


async def test_second_stream_replaces_first(mgr, server):
    a = await mgr.start(f"{server}/live.ts", live=True)
    b = await mgr.start(f"{server}/live.ts", live=True)
    assert list(mgr.sessions) == [b.id]
    assert a.proc.returncode is not None and not a.dir.exists()


async def test_idle_session_is_reaped_and_cleaned(mgr, server, monkeypatch):
    s = await mgr.start(f"{server}/live.ts", live=True)
    monkeypatch.setattr(streams, "IDLE_TIMEOUT", 0.0)
    await asyncio.sleep(0.05)
    await mgr.reap()
    assert not mgr.sessions and not s.dir.exists()


@pytest.mark.parametrize("name", ["init.mp4/../x", "init.mp4.bak", "seg1.m4s.bak", "../init.mp4", "index.m3u8/../x", "seg1.ts.bak", "x.txt",
                                  "/etc/passwd", "seg../a", "../secret.txt"])  # fmt: skip
async def test_file_for_rejects_unsafe_names(mgr, server, name):
    s = await mgr.start(f"{server}/live.ts", live=True)
    # files that really exist next to / inside the session dir must still be refused
    (s.dir.parent / "secret.txt").write_text("x")
    for extra in ("x.txt", "seg1.ts.bak", "a"):
        (s.dir / extra).write_text("x")
    (s.dir / "seg..").mkdir(exist_ok=True)
    (s.dir / "seg..").joinpath("a").write_text("x")
    assert mgr.file_for(s.id, name.replace("index.m3u8", "secret.txt") if ".." in name else name) is None


async def test_unknown_session_and_bad_source(mgr):
    assert mgr.file_for("nope", "index.m3u8") is None
    with pytest.raises(StreamError, match="bad_source"):
        await mgr.start("file:///etc/passwd", live=True)


async def test_dead_source_reports_error_and_cleans_up(mgr):
    with pytest.raises(StreamError):
        await mgr.start("http://127.0.0.1:1/none.ts", live=True)
    assert not mgr.sessions and not list(mgr.base_dir.iterdir())


async def test_shutdown_stops_everything(ffmpeg, tmp_path, server):
    m = StreamManager(ffmpeg, tmp_path / "h", max_streams=2)
    await m.start_reaper()
    s = await m.start(f"{server}/live.ts", live=True)
    await m.shutdown()
    assert s.proc.returncode is not None and not s.dir.exists()


# -- portal rate limiting ("No MAC currently eligible ... Retry in 8s") ------------------
@pytest.fixture
async def flaky(media):
    """Media server whose first N requests are answered 503, like the portal's MAC pool."""
    state = {"left": 0, "hits": 0, "status": 503}

    @web.middleware
    async def mw(request, handler):
        state["hits"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            return web.json_response({"detail": "No MAC currently eligible for selection. Retry in 8s"}, status=state["status"])
        return await handler(request)

    app = web.Application(middlewares=[mw])
    app.router.add_static("/media", media)
    srv = TestServer(app)
    await srv.start_server()
    yield f"http://127.0.0.1:{srv.port}/media", state
    await srv.close()


async def test_busy_portal_is_retried_for_vod_probe_and_start(mgr, flaky, monkeypatch):
    base, state = flaky
    monkeypatch.setattr(streams, "RETRY_WAIT", 0.2)
    state["left"] = 2  # probe fails twice, third try works
    s = await mgr.start(f"{base}/movie.mkv", live=False)
    assert s.duration and not s.proc.returncode


async def test_busy_portal_is_retried_for_live_start(mgr, flaky, monkeypatch):
    base, state = flaky
    monkeypatch.setattr(streams, "RETRY_WAIT", 0.2)
    state["left"] = 1
    s = await mgr.start(f"{base}/live.ts", live=True)
    assert mgr.file_for(s.id, "index.m3u8")


async def test_busy_portal_gives_up_with_a_clear_error(mgr, flaky, monkeypatch):
    base, state = flaky
    monkeypatch.setattr(streams, "RETRY_WAIT", 0.1)
    state["left"] = 999
    with pytest.raises(StreamError, match="portal_busy"):
        await mgr.start(f"{base}/live.ts", live=True)
    assert state["hits"] == streams.RETRIES + 1 and not mgr.sessions


@pytest.fixture
async def slow(media):
    """Serves long.mkv at roughly 0.6 MB/s so ffmpeg is still running when the reaper looks."""
    data = (media / "long.mkv").read_bytes()

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "video/x-matroska", "Content-Length": str(len(data))})
        await resp.prepare(request)
        for i in range(0, len(data), 65536):
            await resp.write(data[i : i + 65536])
            await asyncio.sleep(0.1)
        return resp

    app = web.Application()
    app.router.add_get("/long.mkv", handler)
    srv = TestServer(app)
    await srv.start_server()
    yield f"http://127.0.0.1:{srv.port}"
    await srv.close()


def _state(pid: int) -> str:
    return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]


async def test_vod_ffmpeg_is_suspended_when_far_ahead_and_resumes(mgr, slow, monkeypatch):
    monkeypatch.setattr(streams, "LEAD_PAUSE", 5)
    monkeypatch.setattr(streams, "LEAD_RESUME", 2)
    s = await mgr.start(f"{slow}/long.mkv", live=False)
    for _ in range(40):
        await asyncio.sleep(0.25)
        if s.paused:
            break
    assert s.paused and _state(s.proc.pid) == "T"
    ahead = mgr._produced(s)
    assert ahead < 25  # did not race through the whole 30-segment film
    # the player catches up -> ffmpeg continues
    for n in range(ahead + 1):
        mgr.file_for(s.id, f"seg{n}.ts")
    for _ in range(40):
        await asyncio.sleep(0.25)
        if not s.paused:
            break
    assert not s.paused and mgr._produced(s) > ahead


async def test_stopping_a_suspended_stream_does_not_hang(mgr, slow, monkeypatch):
    monkeypatch.setattr(streams, "LEAD_PAUSE", 3)
    s = await mgr.start(f"{slow}/long.mkv", live=False)
    for _ in range(40):
        await asyncio.sleep(0.25)
        if s.paused:
            break
    assert s.paused
    await asyncio.wait_for(mgr.stop(s.id), 3)  # SIGTERM works only after SIGCONT; the kill fallback takes 5 s
    assert s.proc.returncode is not None and not s.dir.exists()


async def test_live_never_gets_suspended(mgr, server, monkeypatch):
    monkeypatch.setattr(streams, "LEAD_PAUSE", 0)
    s = await mgr.start(f"{server}/live.ts", live=True)
    await asyncio.sleep(1.5)
    await mgr.reap()
    assert not s.paused


async def test_vod_survives_a_pause_that_would_kill_live(mgr, server, monkeypatch):
    monkeypatch.setattr(streams, "IDLE_TIMEOUT", 0.0)
    s = await mgr.start(f"{server}/movie.mkv", live=False)
    await asyncio.sleep(0.05)
    await mgr.reap()
    assert s.id in mgr.sessions  # VOD_IDLE_TIMEOUT (180 s) applies, not the live one


async def test_short_source_that_finishes_instantly_stays_playable(mgr, server):
    s = await mgr.start(f"{server}/movie.mkv", live=False)  # whole 12 s remuxed in well under a second
    await asyncio.sleep(1.0)
    await mgr.reap()
    assert s.id in mgr.sessions and s.proc.returncode == 0
    assert "#EXT-X-ENDLIST" in mgr.file_for(s.id, "index.m3u8").read_text()


async def test_crashed_ffmpeg_is_reaped(mgr, server):
    s = await mgr.start(f"{server}/live.ts", live=True)
    s.proc.kill()
    await s.proc.wait()
    await mgr.reap()
    assert s.id not in mgr.sessions


@pytest.mark.parametrize("status", [461, 429, 502])
async def test_unusual_busy_status_codes_are_retried_too(mgr, flaky, monkeypatch, status):
    """ffmpeg reports codes like 461 only as '4XX' - the real portal answered live starts with 461."""
    base, state = flaky
    monkeypatch.setattr(streams, "RETRY_WAIT", 0.2)
    state.update(left=1, status=status)
    s = await mgr.start(f"{base}/live.ts", live=True)
    assert mgr.file_for(s.id, "index.m3u8") and state["hits"] >= 2


@pytest.mark.parametrize("line", [
    "Error opening input files: Server returned 4XX Client Error, but not one of 40{0,1,3,4}",
    "Error opening input files: Server returned 5XX Server Error reply",
    "HTTP error 503 Service Unavailable",
])  # the exact texts ffmpeg printed for the portal's 461/503 answers
def test_retry_pattern_matches_real_ffmpeg_messages(line):
    assert streams.RETRY_RE.search(line)


@pytest.mark.parametrize("line", ["Server returned 404 Not Found", "Server returned 403 Forbidden",
                                  "Connection refused", "Invalid data found when processing input"])
def test_retry_pattern_ignores_permanent_errors(line):
    assert not streams.RETRY_RE.search(line)


async def test_live_hevc_is_reencoded_with_a_fresh_link(mgr, server, ffmpeg):
    links = []

    async def relink():
        links.append(1)
        return f"{server}/hevclive.ts"

    s = await mgr.start(f"{server}/hevclive.ts", live=True, relink=relink)
    assert s.transcoding and links == [1] and len(mgr.sessions) == 1
    info = ffinfo(ffmpeg, next(s.dir.glob("seg*.ts")))
    assert "Video: h264" in info and "Video: hevc" not in info and "Audio: aac" in info


async def test_live_hevc_is_copied_for_players_that_decode_it(mgr, server, ffmpeg):
    s = await mgr.start(f"{server}/hevclive.ts", live=True, hevc_ok=True)
    assert s.fmp4 and not s.transcoding
    assert "Video: hevc" in ffinfo(ffmpeg, mgr.file_for(s.id, "init.mp4"))


async def test_live_hevc_respects_the_transcode_switch(mgr, server):
    mgr.transcode = False
    with pytest.raises(StreamError, match="transcode_disabled"):
        await mgr.start(f"{server}/hevclive.ts", live=True)
    assert not mgr.sessions


async def test_live_h264_is_still_started_once(mgr, server):
    async def relink():
        raise AssertionError("h264 must not be restarted")

    s = await mgr.start(f"{server}/live.ts", live=True, relink=relink)
    assert not s.transcoding and not s.fmp4


async def test_probe_lists_audio_tracks_and_text_subtitles(mgr, server):
    p = await mgr.probe(f"{server}/subs.mkv")
    assert [a[1] for a in p.audios] == ["ger", "eng"] and [s[1] for s in p.subs] == ["ger", "eng"]


async def test_picked_audio_track_and_subtitles_are_served(mgr, server, ffmpeg):
    p = await mgr.probe(f"{server}/subs.mkv")
    eng = p.audios[1][0]
    s = await mgr.start(f"{server}/subs.mkv", live=False, audio=eng)
    assert s.audio_index == eng and [x[1] for x in s.subs] == ["ger", "eng"]
    sub = mgr.file_for(s.id, f"sub{p.subs[0][0]}.vtt")
    for _ in range(40):  # ffmpeg writes the file while it reads on
        if sub and "Hallo Welt" in sub.read_text():
            break
        await asyncio.sleep(0.25)
    assert "Hallo Welt" in sub.read_text() and sub.read_text().startswith("WEBVTT")
    info = ffinfo(ffmpeg, next(s.dir.glob("seg*.ts")))
    assert "Audio: aac" in info


async def test_unknown_audio_pick_falls_back_to_default(mgr, server):
    s = await mgr.start(f"{server}/subs.mkv", live=False, audio=99)
    assert s.audio_index == (await mgr.probe(f"{server}/subs.mkv")).audio_index


async def test_probe_is_cached_for_track_changes(mgr, server, monkeypatch):
    calls = []
    orig = mgr._probe

    async def counting(src):
        calls.append(src)
        return await orig(src)

    monkeypatch.setattr(mgr, "_probe", counting)
    await mgr.probe(f"{server}/subs.mkv")
    await mgr.probe(f"{server}/subs.mkv")
    assert len(calls) == 1

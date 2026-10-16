"""ffmpeg-based HLS remuxing with hard limits. Video is copied; only sources whose codec
browsers cannot play (HEVC ...) are re-encoded to H.264 (VOD only, capped at 720p)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import signal
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
MAX_STREAMS_LIMIT = 3
IDLE_TIMEOUT = 30.0  # live: seconds without a client request -> stop ffmpeg
VOD_IDLE_TIMEOUT = 180.0  # VOD: a paused player stops fetching for a while
LEAD_PAUSE = 20  # segments (4 s each) ffmpeg may run ahead of the player before it is suspended
LEAD_RESUME = 8
VOD_WINDOW = 60  # playlist/RAM window in segments
RETRY_WAIT = 8.0  # the portal says "Retry in 8s" when it has no free MAC
RETRIES = 2
# ffmpeg prints codes it does not know (461, 429, 503 ...) only as "4XX"/"5XX" Client/Server Error
RETRY_RE = re.compile(r"HTTP error (503|461|429)|Server returned (4XX|5XX|5\d\d|461|429)")
SEG_NUM = re.compile(r"^seg(\d+)\.(?:ts|m4s)$")
MAX_RUNTIME = 8 * 3600  # absolute cap for a single ffmpeg process
READY_TIMEOUT = 25.0
PROBE_TIMEOUT = 25.0
TRANSCODE_HEIGHT = 720  # re-encoded video is scaled down to this height (CPU)
PREFERRED_LANGS = ("ger", "deu", "de")
SAFE_NAME = re.compile(r"^(index\.m3u8|init\.mp4|seg\d{1,7}\.(?:ts|m4s)|sub\d{1,3}\.vtt)$")
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}  # picture subtitles (PGS, DVD) cannot be shown
PROBE_KEEP = 16
PROBE_TTL = 3600.0

_URL = re.compile(r"https?://\S+")
_STREAM = re.compile(
    r"Stream #0:(\d+)(?:\[0x[0-9a-fA-F]+\])?(?:\((\w+)\))?[^:]*: (Video|Audio|Subtitle): (\w+)"
)
_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")


class StreamError(Exception):
    """User-presentable streaming problem; text never contains source URLs."""


class _WrongCodec(Exception):
    def __init__(self, codec: str) -> None:
        super().__init__(codec)
        self.codec = codec


@dataclass
class Probe:
    video_codec: str | None
    audio_index: int | None
    audio_codec: str | None
    duration: float | None
    audios: tuple[tuple[int, str, str], ...] = ()  # (stream index, language, codec)
    subs: tuple[tuple[int, str, str], ...] = ()  # text subtitles only
    tail: tuple[str, ...] = ()  # last scrubbed stderr lines, for the log when nothing was found


def scrub(text: str) -> str:
    return _URL.sub("<url>", text)


def parse_probe(stderr: str) -> Probe:
    """Read codecs/duration from `ffmpeg -i` output and pick a German-first audio track."""
    video = None
    audios: list[tuple[int, str, str]] = []
    subs: list[tuple[int, str, str]] = []
    for idx, lang, kind, codec in _STREAM.findall(stderr):
        if kind == "Video" and video is None:
            video = codec
        elif kind == "Audio":
            audios.append((int(idx), (lang or "").lower(), codec))
        elif kind == "Subtitle" and codec in TEXT_SUBS:
            subs.append((int(idx), (lang or "").lower(), codec))
    pick = next((a for a in audios if a[1] in PREFERRED_LANGS), audios[0] if audios else None)
    m = _DURATION.search(stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    tail = tuple(scrub(ln.strip()) for ln in stderr.splitlines() if ln.strip())[-6:]
    return Probe(video, pick[0] if pick else None, pick[2] if pick else None, dur,
                 tuple(audios), tuple(subs), tail)  # fmt: skip


def build_command(
    ffmpeg: str,
    source: str,
    out_dir: Path,
    *,
    live: bool,
    start: float = 0.0,
    audio_index: int | None = None,
    audio_codec: str | None = None,
    use_nice: bool = True,
    transcode: bool = False,
    fmp4: bool = False,
    subs: tuple[int, ...] = (),
) -> list[str]:
    """ffmpeg argv: copy video (or re-encode to H.264 if transcode), AAC audio, HLS into out_dir."""
    cmd: list[str] = []
    nice = shutil.which("nice") if use_nice else None
    if nice:
        cmd += [nice, "-n", "10"]
    # Live is not probed first (a second portal connection), so ffmpeg itself reports the codecs.
    cmd += [ffmpeg, "-hide_banner", "-loglevel", "info" if live else "warning", "-nostats", "-nostdin"]
    cmd += ["-protocol_whitelist", "http,https,tcp,tls,crypto"]
    cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    # One plain streaming connection: this portal answers extra range requests with 503.
    cmd += ["-seekable", "0", "-rw_timeout", "15000000", "-fflags", "+genpts+discardcorrupt"]
    if not live and start > 0:
        cmd += ["-ss", f"{start:.3f}"]  # read-and-discard up to this point
    cmd += ["-i", source, "-map", "0:v:0"]
    cmd += ["-map", f"0:{audio_index}" if audio_index is not None else "0:a:0?"]
    cmd += ["-sn", "-dn"]
    if transcode:
        fps = "fps=25," if live else ""  # 50 fps live streams would double the CPU load
        cmd += [
            "-vf", f"{fps}scale=-2:'min({TRANSCODE_HEIGHT},ih)',format=yuv420p",
            "-c:v", "libx264", "-preset", "ultrafast" if live else "veryfast", "-crf", "23", "-maxrate", "6M", "-bufsize", "12M",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
        ]  # fmt: skip
    else:
        cmd += ["-c:v", "copy"]
        if fmp4:
            cmd += ["-tag:v", "hvc1"]  # Apple/Chrome only accept HEVC in fMP4 with the hvc1 tag
    if audio_codec == "aac":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    cmd += [
        "-f", "hls", "-hls_time", "4", "-hls_list_size", "10" if live else str(VOD_WINDOW),
        "-hls_flags", "delete_segments+independent_segments",
    ]  # fmt: skip
    if fmp4:
        cmd += ["-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4"]
    cmd += ["-hls_segment_filename", str(out_dir / ("seg%d.m4s" if fmp4 else "seg%d.ts")), str(out_dir / "index.m3u8")]
    for i in subs:  # text subtitles grow as ffmpeg reads on; the card polls them
        cmd += ["-map", f"0:{i}", "-c:s", "webvtt", "-flush_packets", "1", "-f", "webvtt", str(out_dir / f"sub{i}.vtt")]
    return cmd


@dataclass
class Session:
    id: str
    dir: Path
    proc: asyncio.subprocess.Process
    live: bool
    started: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)
    duration: float | None = None
    start_offset: float = 0.0
    stderr: list[str] = field(default_factory=list)
    reader: asyncio.Task | None = None
    requested: int = -1  # highest segment number a client has fetched
    paused: bool = False
    transcoding: bool = False
    fmp4: bool = False
    video_codec: str | None = None  # read from ffmpeg's own output (live only)
    audios: tuple = ()  # (stream index, language, codec) of the source
    subs: tuple = ()  # text subtitles written next to the playlist as sub<N>.vtt
    audio_index: int | None = None


def default_base_dir() -> Path:
    shm = Path("/dev/shm")
    root = shm if shm.is_dir() and os.access(shm, os.W_OK) else Path(tempfile.gettempdir())
    return root / "ha-stalker"


class StreamManager:
    def __init__(self, ffmpeg: str, base_dir: Path | None = None, max_streams: int = 1, transcode: bool = True
    ) -> None:
        self._probes: dict[str, tuple[float, Probe]] = {}
        self.transcode = transcode  # allow re-encoding unplayable video (CPU heavy)
        self.ffmpeg = ffmpeg
        self.base_dir = base_dir or default_base_dir()
        self.max_streams = max(1, min(MAX_STREAMS_LIMIT, max_streams))
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self.use_nice = True

    # -- lifecycle ------------------------------------------------------
    async def start_reaper(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        for old in self.base_dir.iterdir():  # leftovers from a crash
            shutil.rmtree(old, ignore_errors=True)
        self._reaper = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        for sid in list(self.sessions):
            await self.stop(sid)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.reap()

    async def reap(self) -> None:
        now = time.monotonic()
        for s in list(self.sessions.values()):
            idle = IDLE_TIMEOUT if s.live else VOD_IDLE_TIMEOUT
            crashed = s.proc.returncode not in (None, 0)  # exit 0 = source ended, files stay playable
            if crashed or now - s.last_access > idle or now - s.started > MAX_RUNTIME:
                await self.stop(s.id)
            elif not s.live and s.proc.returncode is None:
                self._flow_control(s)

    @staticmethod
    def _produced(s: Session) -> int:
        nums = [int(m.group(1)) for f in s.dir.glob("seg*") if (m := SEG_NUM.match(f.name))]
        return max(nums) if nums else -1

    def _flow_control(self, s: Session) -> None:
        """Suspend ffmpeg while it is far ahead of the player; resume when the player catches up."""
        lead = self._produced(s) - max(s.requested, 0)
        try:
            if not s.paused and lead >= LEAD_PAUSE:
                os.kill(s.proc.pid, signal.SIGSTOP)
                s.paused = True
            elif s.paused and lead <= LEAD_RESUME:
                os.kill(s.proc.pid, signal.SIGCONT)
                s.paused = False
        except ProcessLookupError:
            pass

    # -- probing ---------------------------------------------------------
    async def probe(self, source: str) -> Probe:
        """Probe a source; results are kept for a while so changing the audio track costs no connection."""
        _check_source(source)
        now = time.monotonic()
        hit = self._probes.get(source)
        if hit and now - hit[0] < PROBE_TTL:
            return hit[1]
        p = await self._probe(source)
        if p.video_codec is not None:
            self._probes[source] = (now, p)
            for k in sorted(self._probes, key=lambda k: self._probes[k][0])[:-PROBE_KEEP]:
                del self._probes[k]
        return p

    async def _probe(self, source: str) -> Probe:
        for attempt in range(RETRIES + 1):
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.ffmpeg, "-hide_banner", "-nostdin",
                    "-protocol_whitelist", "http,https,tcp,tls,crypto",
                    "-seekable", "0", "-rw_timeout", "15000000", "-i", source,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )  # fmt: skip
                _, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise StreamError("probe_timeout") from None
            except OSError:
                raise StreamError("ffmpeg_missing") from None
            text = err.decode("utf-8", "replace")
            if RETRY_RE.search(text) and attempt < RETRIES:
                await asyncio.sleep(RETRY_WAIT)  # portal has no free MAC right now
                continue
            return parse_probe(text)
        raise StreamError("portal_busy")  # pragma: no cover

    # -- start / stop -------------------------------------------------------
    async def make_room(self) -> None:
        """Stop the oldest stream(s) until one more fits.

        Call this BEFORE asking the portal for a new link: a portal usually allows only
        one stream per MAC, so the old stream has to be gone first.
        """
        async with self._lock:
            while len(self.sessions) >= self.max_streams:
                await self.stop(min(self.sessions.values(), key=lambda s: s.started).id)

    async def start(
        self, source: str, *, live: bool, start: float = 0.0, hevc_ok: bool = False, relink=None,
        audio: int | None = None,
    ) -> Session:
        _check_source(source)
        audio_index = audio_codec = duration = None
        audios = subs = ()
        transcode = fmp4 = False
        restarted = False
        if not live:
            p = await self.probe(source)
            if p.video_codec is None:
                _LOGGER.warning("Probe found no video stream: %s", " | ".join(p.tail))
                if any(re.search(r"Invalid data|[Ee]rror|Server returned|refused|timed out", t) for t in p.tail):
                    raise StreamError("stream_failed:probe")  # the source answered badly, not a codec issue
                raise StreamError("video_codec_unsupported")
            if p.video_codec == "hevc" and hevc_ok:
                fmp4 = True  # the player decodes HEVC itself: copy it, no CPU
            elif p.video_codec != "h264":  # HEVC for old players, VC-1, MPEG-4 ... browsers cannot play these
                transcode = True
                if not self.transcode:
                    raise StreamError("transcode_disabled")
            audio_index, audio_codec, duration = p.audio_index, p.audio_codec, p.duration
            audios, subs = p.audios, p.subs
            chosen = next((a for a in audios if a[0] == audio), None)
            if chosen:  # the viewer picked a track
                audio_index, audio_codec = chosen[0], chosen[2]
        for attempt in range(RETRIES + 1):
            s = await self._spawn(source, live, start, audio_index, audio_codec, duration, transcode, fmp4,
                                  tuple(i for i, _, _ in subs))  # fmt: skip
            s.audios, s.subs, s.audio_index = audios, subs, audio_index
            try:
                await self._wait_ready(s, check_codec=live and not transcode and not fmp4)
                return s
            except _WrongCodec as wrong:
                # Live video the player cannot take as it is (HEVC, MPEG-2 ...): start again.
                await self.stop(s.id)
                if restarted:
                    raise StreamError("video_codec_unsupported") from None
                restarted = True
                if wrong.codec == "hevc" and hevc_ok:
                    fmp4 = True
                elif self.transcode:
                    transcode = True
                else:
                    raise StreamError("transcode_disabled") from None
                _LOGGER.info("Live stream is %s, starting again as %s", wrong.codec, "copy" if fmp4 else "re-encode")
                if relink:  # the portal's link is one-shot
                    source = await relink()
                    _check_source(source)
            except StreamError:
                busy = bool(RETRY_RE.search("\n".join(s.stderr)))
                await self.stop(s.id)
                if not busy:
                    raise
                if attempt == RETRIES:
                    raise StreamError("portal_busy") from None
                await asyncio.sleep(RETRY_WAIT)
        raise StreamError("portal_busy")  # pragma: no cover

    async def _spawn(
        self, source, live, start, audio_index, audio_codec, duration, transcode=False, fmp4=False, subs=()
    ) -> Session:
        async with self._lock:
            while len(self.sessions) >= self.max_streams:
                oldest = min(self.sessions.values(), key=lambda s: s.started)
                await self.stop(oldest.id)
            sid = secrets.token_hex(16)  # unguessable: it is the only credential of the HLS URLs
            out = self.base_dir / sid
            out.mkdir(parents=True)
            cmd = build_command(
                self.ffmpeg, source, out, live=live, start=start,
                audio_index=audio_index, audio_codec=audio_codec, use_nice=self.use_nice,
                transcode=transcode, fmp4=fmp4, subs=subs,
            )  # fmt: skip
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
            except OSError:
                shutil.rmtree(out, ignore_errors=True)
                raise StreamError("ffmpeg_missing") from None
            s = Session(sid, out, proc, live, duration=duration, start_offset=start, transcoding=transcode, fmp4=fmp4)
            s.reader = asyncio.create_task(self._read_stderr(s))
            self.sessions[sid] = s
            return s

    async def _read_stderr(self, s: Session) -> None:
        assert s.proc.stderr
        async for line in s.proc.stderr:
            text = scrub(line.decode("utf-8", "replace").strip())
            if s.live and s.video_codec is None and (m := _STREAM.search(text)) and m.group(3) == "Video":
                s.video_codec = m.group(4)
            s.stderr = (s.stderr + [text])[-20:]

    async def _wait_ready(self, s: Session, check_codec: bool = False) -> None:
        index = s.dir / "index.m3u8"
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if check_codec and s.video_codec and s.video_codec != "h264":
                raise _WrongCodec(s.video_codec)
            if index.exists() and "#EXTINF" in index.read_text(errors="replace"):
                return
            if s.proc.returncode is not None:
                await asyncio.sleep(0.1)  # let the stderr reader catch up before reporting
                raise StreamError("stream_failed:" + (s.stderr[-1] if s.stderr else "exit"))
            await asyncio.sleep(0.25)
        raise StreamError("stream_timeout")

    async def stop(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if not s:
            return
        if s.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if s.paused:
                    os.kill(s.proc.pid, signal.SIGCONT)  # a stopped process would not see SIGTERM
                s.proc.terminate()
            try:
                await asyncio.wait_for(s.proc.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    s.proc.kill()
                await s.proc.wait()
        if s.reader:
            s.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await s.reader
        shutil.rmtree(s.dir, ignore_errors=True)

    # -- serving -------------------------------------------------------------
    def file_for(self, sid: str, name: str) -> Path | None:
        """Path of a playlist/segment of a live session, or None. Marks the session active."""
        s = self.sessions.get(sid)
        if not s or not SAFE_NAME.match(name):
            return None
        s.last_access = time.monotonic()
        if m := SEG_NUM.match(name):
            s.requested = max(s.requested, int(m.group(1)))
        p = s.dir / name
        return p if p.is_file() else None


def _check_source(source: str) -> None:
    if not source.lower().startswith(("http://", "https://")):
        raise StreamError("bad_source")

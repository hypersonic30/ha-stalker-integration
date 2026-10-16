import os
import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_socket

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg")


@pytest.fixture(autouse=True)
def _sockets(socket_enabled):
    """The fake portal and ffmpeg talk over loopback."""
    pytest_socket.socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)


@pytest.fixture(scope="session")
def ffmpeg():
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    return FFMPEG


def _make(ffmpeg, out: Path, args: list[str]):
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *args, str(out)], check=True)


@pytest.fixture(scope="session")
def media(ffmpeg, tmp_path_factory):
    """Tiny test media: MKV (h264 + ac3 eng + ac3 ger), MPEG-TS live (h264+aac), MKV hevc."""
    d = tmp_path_factory.mktemp("media")
    v = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25"]
    a = ["-f", "lavfi", "-i", "sine=frequency=440", "-f", "lavfi", "-i", "sine=frequency=880"]
    _make(ffmpeg, d / "movie.mkv", [*v, *a, "-t", "12", "-map", "0:v", "-map", "1:a", "-map", "2:a",
          "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "ac3",
          "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=ger"])  # fmt: skip
    (d / "s.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nHallo Welt\n\n2\n00:00:05,000 --> 00:00:07,000\nZweite Zeile\n")
    (d / "e.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nHello world\n")
    _make(ffmpeg, d / "subs.mkv", [*v, *a, "-i", str(d / "s.srt"), "-i", str(d / "e.srt"), "-t", "12",
          "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3", "-map", "4",
          "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-c:s", "srt",
          "-metadata:s:a:0", "language=ger", "-metadata:s:a:1", "language=eng",
          "-metadata:s:s:0", "language=ger", "-metadata:s:s:1", "language=eng"])  # fmt: skip
    _make(ffmpeg, d / "live.ts", [*v, *a[:4], "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
          "-g", "25", "-c:a", "aac", "-f", "mpegts"])  # fmt: skip
    _make(ffmpeg, d / "long.mkv", [*v, *a[:4], "-t", "120", "-c:v", "libx264", "-preset", "ultrafast",
          "-g", "25", "-c:a", "aac"])  # fmt: skip
    _make(ffmpeg, d / "hevclive.ts", [*v, *a[:4], "-t", "12", "-c:v", "libx265", "-g", "25", "-c:a", "aac",
          "-f", "mpegts"])  # fmt: skip
    _make(ffmpeg, d / "hevc.mkv", [*v, *a[:4], "-t", "3", "-c:v", "libx265", "-c:a", "aac"])
    return d


def portal_entry(portal: dict, title: str = "Test portal", *more: tuple[str, dict], **shared):
    """A main entry (shared settings) with one portal sub-entry, plus optional further (title, data) portals."""
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigSubentryData
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    subs = [ConfigSubentryData(data=MappingProxyType(d), subentry_type="portal", title=t, unique_id=None)
            for t, d in ((title, portal), *more)]  # fmt: skip
    return MockConfigEntry(domain="stalker", version=2, data={"max_streams": 1, **shared}, subentries_data=subs)


def first_portal(hass):
    from custom_components.stalker.const import DOMAIN
    return next(iter(hass.data[DOMAIN]["portals"].values()))

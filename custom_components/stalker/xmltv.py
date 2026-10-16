"""Streaming XMLTV parser: programmes of the next ~2 days, keyed by XMLTV channel id."""
from __future__ import annotations

import gzip
import io
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.etree.ElementTree import ParseError, iterparse

DESCR_MAX = 300
AHEAD_S = 48 * 3600
BEHIND_S = 3600
MAX_UNPACKED = 600 * 1024 * 1024  # a "gzip bomb" must not eat the memory/CPU of Home Assistant

_TIME = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?\s*([+-]\d{4}|Z)?$")


class XmltvError(Exception):
    """Unreadable or oversized guide; the message never contains URLs."""


def norm(name: str) -> str:
    """Channel names compare without case, spaces and punctuation ("Das Erste HD" == "dasersteHD")."""
    return re.sub(r"[^0-9a-z]", "", name.casefold())


def parse_time(text: str) -> int | None:
    m = _TIME.match((text or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    off = timezone.utc
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        off = timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))
    try:
        return int(datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), tzinfo=off).timestamp())
    except ValueError:
        return None


class _Capped(io.RawIOBase):
    def __init__(self, inner) -> None:
        self._inner, self._n = inner, 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._inner.read(len(b))
        self._n += len(data)
        if self._n > MAX_UNPACKED:
            raise XmltvError("too_large")
        b[: len(data)] = data
        return len(data)


def parse_xmltv(raw: bytes, now: float | None = None) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Return ({xmltv channel id: programmes sorted by start}, {normalised display name: channel id})."""
    now = time.time() if now is None else now
    stream = io.BytesIO(raw)
    if raw[:2] == b"\x1f\x8b":
        stream = gzip.GzipFile(fileobj=stream)
    src = io.BufferedReader(_Capped(stream))
    progs: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    try:
        for _, el in iterparse(src, events=("end",)):
            if el.tag == "programme":
                ch = el.get("channel") or ""
                start, stop = parse_time(el.get("start", "")), parse_time(el.get("stop", ""))
                if ch and start and stop and stop > start and stop > now - BEHIND_S and start < now + AHEAD_S:
                    progs.setdefault(ch, []).append({
                        "title": (el.findtext("title") or "").strip(),
                        "descr": (el.findtext("desc") or "").strip()[:DESCR_MAX],
                        "start": start, "stop": stop,
                    })  # fmt: skip
                el.clear()
            elif el.tag == "channel":
                cid = el.get("id") or ""
                for dn in el.findall("display-name"):
                    if dn.text and cid:
                        names.setdefault(norm(dn.text), cid)
                el.clear()
    except (ParseError, OSError, EOFError) as err:
        if not progs:
            raise XmltvError("unreadable") from err  # a cut-off file still yields what came before
    for items in progs.values():
        items.sort(key=lambda p: p["start"])
    return progs, names

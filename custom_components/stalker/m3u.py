"""Parser for the portal's M3U playlists (VOD and series)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_SXXEXX = re.compile(r"\bS(\d{1,3})E(\d{1,4})\b", re.IGNORECASE)


@dataclass
class Entry:
    title: str
    url: str
    logo: str = ""
    group: str = ""
    stream_id: str = ""
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class Episode:
    title: str
    season: int
    episode: int
    url: str
    stream_id: str


@dataclass
class SeriesInfo:
    """A series in a list (no episodes loaded yet)."""

    title: str
    logo: str
    group: str
    episodes: int = 0


@dataclass
class Series:
    title: str
    logo: str
    group: str
    seasons: dict[int, list[Episode]] = field(default_factory=dict)


def parse_m3u(text: str, keep_attrs: bool = True) -> list[Entry]:
    """Return all entries; lines that do not belong to an #EXTINF are ignored.

    keep_attrs=False drops the raw attribute dict (saves memory for the 19k-entry VOD list).
    """
    entries: list[Entry] = []
    pending: tuple[dict[str, str], str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            head, _, title = line.partition(",")
            pending = (dict(_ATTR.findall(head)), title.strip())
        elif line.startswith("#"):
            continue
        elif pending is not None:
            attrs, title = pending
            pending = None
            if not line.lower().startswith(("http://", "https://")):
                continue
            entries.append(
                Entry(
                    title=title,
                    url=line,
                    logo=attrs.get("tvg-logo", ""),
                    group=attrs.get("group-title", "").strip(),
                    stream_id=attrs.get("portal-stream-id", ""),
                    attrs=attrs if keep_attrs else {},
                )
            )
    return entries


def _season_episode(entry: Entry) -> tuple[int, int] | None:
    a = entry.attrs
    if a.get("tvg-season", "").isdigit() and a.get("tvg-episode", "").isdigit():
        return int(a["tvg-season"]), int(a["tvg-episode"])
    m = _SXXEXX.search(entry.title)
    return (int(m.group(1)), int(m.group(2))) if m else None


def group_series(entries: list[Entry]) -> dict[str, Series]:
    """Group episodes by series title (`serie-title`, else the title before SxxExx)."""
    out: dict[str, Series] = {}
    for e in entries:
        se = _season_episode(e)
        if se is None:
            continue
        name = e.attrs.get("serie-title") or _SXXEXX.split(e.title)[0].strip()
        if not name:
            continue
        s = out.setdefault(name, Series(name, e.logo, e.group))
        s.seasons.setdefault(se[0], []).append(
            Episode(e.title, se[0], se[1], e.url, e.stream_id)
        )
    for s in out.values():
        for eps in s.seasons.values():
            eps.sort(key=lambda x: x.episode)
    return out

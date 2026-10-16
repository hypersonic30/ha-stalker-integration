"""Per-user watch progress (resume position, "watched" flag) for movies and episodes."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .m3u import Series

STORE_KEY = "stalker_progress"
STORE_VERSION = 1
DONE_RATIO = 0.92  # watched this much = finished
MIN_RESUME = 30  # seconds: less than this is "just opened it"
MAX_ENTRIES = 1500  # per user; the oldest are dropped
MAX_RESUME = 20
SAVE_DELAY = 10
KINDS = ("vod", "episode")


def key_of(kind: str, item_id: str) -> str:
    return f"{kind}:{item_id}"


class Progress:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def load(self) -> None:
        stored = await self._store.async_load()
        self._data = stored if isinstance(stored, dict) else {}

    async def flush(self) -> None:
        await self._store.async_save(self._data)

    def _changed(self) -> None:
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)

    def entries(self, user: str) -> dict[str, dict[str, Any]]:
        return self._data.get(user, {})

    def save(self, user: str, kind: str, item_id: str, pos: float, dur: float, meta: dict[str, Any]) -> None:
        """Remember a position. `meta` (title, logo, sid, season, episode) comes from the server."""
        dur = max(0.0, dur)
        pos = min(max(0.0, pos), dur) if dur else max(0.0, pos)
        done = bool(dur) and pos >= dur * DONE_RATIO
        mine = self._data.setdefault(user, {})
        mine[key_of(kind, item_id)] = {
            **meta, "pos": int(pos), "dur": int(dur), "done": done, "t": round(time.time(), 3),
        }  # fmt: skip
        if len(mine) > MAX_ENTRIES:
            for k in sorted(mine, key=lambda k: mine[k]["t"])[: len(mine) - MAX_ENTRIES]:
                del mine[k]
        self._changed()

    def mark(self, user: str, kind: str, item_id: str, done: bool, meta: dict[str, Any]) -> None:
        k = key_of(kind, item_id)
        mine = self._data.setdefault(user, {})
        if not done:
            mine.pop(k, None)
        else:
            old = mine.get(k, {})
            dur = int(old.get("dur") or 0)
            mine[k] = {**meta, "pos": dur, "dur": dur, "done": True, "t": round(time.time(), 3)}
        self._changed()


def resume_list(entries: dict[str, dict[str, Any]], series: dict[str, Series]) -> list[dict[str, Any]]:
    """What to offer under "Continue watching": unfinished movies and, per series, the unfinished
    episode or the one after the last finished episode."""
    out: list[dict[str, Any]] = []
    by_series: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for k, e in entries.items():
        kind, _, item_id = k.partition(":")
        if kind == "vod":
            if not e.get("done") and e.get("pos", 0) >= MIN_RESUME:
                out.append({"kind": "vod", "id": item_id, "title": e.get("title", ""),
                            "logo": e.get("logo", ""), "pos": e["pos"], "dur": e.get("dur", 0),
                            "label": "", "t": e["t"]})  # fmt: skip
        elif kind == "episode" and e.get("sid") in series:
            if e.get("done") or e.get("pos", 0) >= MIN_RESUME:
                by_series.setdefault(e["sid"], []).append((item_id, e))
    for sid, items in by_series.items():
        s = series[sid]
        item_id, e = max(items, key=lambda x: x[1]["t"])
        flat = [ep for n in sorted(s.seasons) for ep in s.seasons[n]]
        if e.get("done"):
            idx = next((i for i, ep in enumerate(flat) if ep.stream_id == item_id), None)
            if idx is None or idx + 1 >= len(flat):
                continue
            ep, pos, dur, nxt = flat[idx + 1], 0, 0, True
        else:
            ep = next((x for x in flat if x.stream_id == item_id), None)
            if ep is None:
                continue
            pos, dur, nxt = e["pos"], e.get("dur", 0), False
        out.append({"kind": "episode", "id": ep.stream_id, "title": s.title, "logo": s.logo,
                    "pos": pos, "dur": dur, "sid": sid, "t": e["t"],
                    "label": f"Staffel {ep.season} · Folge {ep.episode}" + (" · als Nächstes" if nxt else "")})  # fmt: skip
    out.sort(key=lambda x: x["t"], reverse=True)
    return out[:MAX_RESUME]

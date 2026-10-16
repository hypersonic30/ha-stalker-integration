"""Per-user favourite channels, kept separately for every portal."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORE_KEY = "stalker_favorites"
STORE_VERSION = 1
MAX_FAVORITES = 500  # per user and portal
SAVE_DELAY = 5


class Favorites:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self._data: dict[str, dict[str, list[str]]] = {}

    async def load(self) -> None:
        stored = await self._store.async_load()
        self._data = stored if isinstance(stored, dict) else {}

    async def flush(self) -> None:
        await self._store.async_save(self._data)

    def prune(self, portals: set[str]) -> None:
        """Forget portals that were removed."""
        for mine in self._data.values():
            for pid in [p for p in mine if p not in portals]:
                del mine[pid]

    def ids(self, user: str, portal: str) -> list[str]:
        return list(self._data.get(user, {}).get(portal, []))

    def set(self, user: str, portal: str, channel_id: str, fav: bool) -> bool:
        """Switch a favourite on or off; False when the list is full."""
        ids = self._data.setdefault(user, {}).setdefault(portal, [])
        if fav and channel_id not in ids:
            if len(ids) >= MAX_FAVORITES:
                return False
            ids.append(channel_id)
        elif not fav and channel_id in ids:
            ids.remove(channel_id)
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)
        return True

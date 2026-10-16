"""Stalker IPTV client for Home Assistant (live TV, VOD, series; played as HLS).

The main entry holds the shared settings (stream limit, re-encoding); every portal
(Stalker, M3U + XMLTV, Xtream Codes) is a sub-entry, so several can be used at once."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_MAX_STREAMS,
    CONF_SOURCE,
    CONF_TRANSCODE,
    DEFAULT_MAX_STREAMS,
    DEFAULT_TRANSCODE,
    DOMAIN,
    SOURCE_M3U,
    SOURCE_STALKER,
    SOURCE_XTREAM,
    SUBENTRY_PORTAL,
)
from .data import StalkerData
from .favorites import Favorites
from .progress import Progress
from .sources import build_data
from .streams import StreamManager
from .views import register_views

_LOGGER = logging.getLogger(__name__)
_SOURCE_TITLES = {SOURCE_STALKER: "Stalker", SOURCE_M3U: "M3U", SOURCE_XTREAM: "Xtream"}


@dataclass
class Portal:
    id: str
    title: str
    source: str
    data: StalkerData


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """0.1-0.3 kept one source in the entry itself: it becomes the first portal."""
    if entry.version == 1:
        shared = {CONF_MAX_STREAMS, CONF_TRANSCODE}
        old = dict(entry.data)
        portal = {k: v for k, v in old.items() if k not in shared}
        portal.setdefault(CONF_SOURCE, SOURCE_STALKER)
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            data=MappingProxyType(portal), subentry_type=SUBENTRY_PORTAL,
            title=_SOURCE_TITLES.get(portal[CONF_SOURCE], "Portal"), unique_id=None,
        ))  # fmt: skip
        hass.config_entries.async_update_entry(
            entry, data={k: old[k] for k in shared if k in old}, version=2
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    portals: dict[str, Portal] = {}
    for sub in entry.subentries.values():
        if sub.subentry_type != SUBENTRY_PORTAL:
            continue
        try:
            portals[sub.subentry_id] = Portal(
                sub.subentry_id, sub.title, sub.data.get(CONF_SOURCE, SOURCE_STALKER),
                build_data(session, sub.data),
            )  # fmt: skip
        except (KeyError, ValueError) as err:  # a broken portal must not take the others down
            _LOGGER.warning("Portal %s skipped: %s", sub.title, type(err).__name__)
    streams = StreamManager(
        get_ffmpeg_manager(hass).binary,
        max_streams=entry.data.get(CONF_MAX_STREAMS, DEFAULT_MAX_STREAMS),
        transcode=entry.data.get(CONF_TRANSCODE, DEFAULT_TRANSCODE),
    )
    await streams.start_reaper()
    progress, favorites = Progress(hass), Favorites(hass)
    await progress.load()
    await favorites.load()
    if portals:
        progress.adopt_legacy(next(iter(portals)))
        progress.prune(set(portals))
        favorites.prune(set(portals))
    domain = hass.data.setdefault(DOMAIN, {})
    if "views" not in domain:  # views are registered once per HA run
        register_views(hass)
        domain["views"] = True
    domain["portals"], domain["streams"] = portals, streams
    domain["progress"], domain["favorites"] = progress, favorites
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain = hass.data.get(DOMAIN, {})
    streams: StreamManager | None = domain.pop("streams", None)
    domain.pop("portals", None)
    favorites: Favorites | None = domain.pop("favorites", None)
    progress: Progress | None = domain.pop("progress", None)
    if progress:
        await progress.flush()
    if favorites:
        await favorites.flush()
    if streams:
        await streams.shutdown()
    return True

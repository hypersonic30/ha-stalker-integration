"""Stalker IPTV client for Home Assistant (live TV, VOD, series; played as HLS)."""
from __future__ import annotations

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_MAC,
    CONF_MAX_STREAMS,
    CONF_PORTAL_URL,
    CONF_SERIES_M3U,
    CONF_VOD_M3U,
    DEFAULT_MAX_STREAMS,
    DOMAIN,
)
from .data import StalkerData
from .stalker import StalkerClient
from .streams import StreamManager
from .views import register_views


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = StalkerClient(session, entry.data[CONF_PORTAL_URL], entry.data[CONF_MAC])
    data = StalkerData(session, client, entry.data.get(CONF_VOD_M3U), entry.data.get(CONF_SERIES_M3U))
    streams = StreamManager(
        get_ffmpeg_manager(hass).binary,
        max_streams=entry.data.get(CONF_MAX_STREAMS, DEFAULT_MAX_STREAMS),
    )
    await streams.start_reaper()
    domain = hass.data.setdefault(DOMAIN, {})
    if "views" not in domain:  # views are registered once per HA run
        register_views(hass)
        domain["views"] = True
    domain["data"], domain["streams"] = data, streams
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    streams: StreamManager | None = hass.data.get(DOMAIN, {}).pop("streams", None)
    hass.data.get(DOMAIN, {}).pop("data", None)
    if streams:
        await streams.shutdown()
    return True

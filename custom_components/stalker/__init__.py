"""Stalker IPTV client for Home Assistant (live TV, VOD, series; played as HLS)."""
from __future__ import annotations

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_MAX_STREAMS,
    CONF_TRANSCODE,
    DEFAULT_MAX_STREAMS,
    DEFAULT_TRANSCODE,
    DOMAIN,
)
from .progress import Progress
from .sources import build_data
from .streams import StreamManager
from .views import register_views


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    data = build_data(session, entry.data)
    streams = StreamManager(
        get_ffmpeg_manager(hass).binary,
        max_streams=entry.data.get(CONF_MAX_STREAMS, DEFAULT_MAX_STREAMS),
        transcode=entry.data.get(CONF_TRANSCODE, DEFAULT_TRANSCODE),
    )
    await streams.start_reaper()
    progress = Progress(hass)
    await progress.load()
    domain = hass.data.setdefault(DOMAIN, {})
    if "views" not in domain:  # views are registered once per HA run
        register_views(hass)
        domain["views"] = True
    domain["data"], domain["streams"], domain["progress"] = data, streams, progress
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    streams: StreamManager | None = hass.data.get(DOMAIN, {}).pop("streams", None)
    hass.data.get(DOMAIN, {}).pop("data", None)
    progress: Progress | None = hass.data.get(DOMAIN, {}).pop("progress", None)
    if progress:
        await progress.flush()
    if streams:
        await streams.shutdown()
    return True

"""Config flow: pick a source (Stalker portal, M3U + XMLTV, Xtream Codes); it is checked before saving."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_LIVE_M3U,
    CONF_MAC,
    CONF_MAX_STREAMS,
    CONF_PASSWORD,
    CONF_PORTAL_URL,
    CONF_SERIES_M3U,
    CONF_SERVER_URL,
    CONF_SOURCE,
    CONF_TRANSCODE,
    CONF_USERNAME,
    CONF_VOD_M3U,
    CONF_XMLTV,
    DEFAULT_MAX_STREAMS,
    DEFAULT_TRANSCODE,
    DOMAIN,
    SOURCE_M3U,
    SOURCE_STALKER,
    SOURCE_XTREAM,
)
from .sources import M3uLive, XtreamApi
from .stalker import MAC_RE, StalkerAuthError, StalkerClient, StalkerError
from .streams import MAX_STREAMS_LIMIT

_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_TEXT = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_URL_KEYS = (CONF_PORTAL_URL, CONF_LIVE_M3U, CONF_XMLTV, CONF_SERVER_URL, CONF_VOD_M3U, CONF_SERIES_M3U)


def _common(d: dict[str, Any]) -> dict:
    return {
        vol.Required(CONF_MAX_STREAMS, default=d.get(CONF_MAX_STREAMS, DEFAULT_MAX_STREAMS)): NumberSelector(
            NumberSelectorConfig(min=1, max=MAX_STREAMS_LIMIT, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_TRANSCODE, default=d.get(CONF_TRANSCODE, DEFAULT_TRANSCODE)): BooleanSelector(),
    }



def _schema(source: str, d: dict[str, Any]) -> vol.Schema:
    if source == SOURCE_M3U:
        fields = {
            vol.Required(CONF_LIVE_M3U, default=d.get(CONF_LIVE_M3U, "")): _URL,
            vol.Optional(CONF_XMLTV, default=d.get(CONF_XMLTV, "")): _URL,
            vol.Optional(CONF_VOD_M3U, default=d.get(CONF_VOD_M3U, "")): _URL,
            vol.Optional(CONF_SERIES_M3U, default=d.get(CONF_SERIES_M3U, "")): _URL,
        }
    elif source == SOURCE_XTREAM:
        fields = {
            vol.Required(CONF_SERVER_URL, default=d.get(CONF_SERVER_URL, "")): _URL,
            vol.Required(CONF_USERNAME, default=d.get(CONF_USERNAME, "")): _TEXT,
            vol.Required(CONF_PASSWORD, default=d.get(CONF_PASSWORD, "")): _SECRET,
        }
    else:
        fields = {
            vol.Required(CONF_PORTAL_URL, default=d.get(CONF_PORTAL_URL, "")): _URL,
            vol.Required(CONF_MAC, default=d.get(CONF_MAC, "")): _SECRET,
            vol.Optional(CONF_VOD_M3U, default=d.get(CONF_VOD_M3U, "")): _URL,
            vol.Optional(CONF_SERIES_M3U, default=d.get(CONF_SERIES_M3U, "")): _URL,
        }
    return vol.Schema({**fields, **_common(d)})


def _clean(source: str, user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    out[CONF_SOURCE] = source
    out[CONF_MAX_STREAMS] = int(out[CONF_MAX_STREAMS])
    out[CONF_TRANSCODE] = bool(out.get(CONF_TRANSCODE, DEFAULT_TRANSCODE))
    if CONF_MAC in out:
        out[CONF_MAC] = out[CONF_MAC].strip().upper()
    for key in (*_URL_KEYS, CONF_USERNAME, CONF_PASSWORD):
        if key in out:
            out[key] = (out.get(key) or "").strip()
    return out


async def _validate(hass, source: str, data: dict[str, Any]) -> str | None:
    for key in _URL_KEYS:
        v = data.get(key)
        if v and not v.lower().startswith(("http://", "https://")):
            return "invalid_url"
    session = async_get_clientsession(hass)
    try:
        if source == SOURCE_M3U:
            await M3uLive(session, data[CONF_LIVE_M3U]).live_listing()
        elif source == SOURCE_XTREAM:
            await XtreamApi(session, data[CONF_SERVER_URL], data[CONF_USERNAME], data[CONF_PASSWORD]).login()
        else:
            if not MAC_RE.match(data[CONF_MAC]):
                return "invalid_mac"
            await StalkerClient(session, data[CONF_PORTAL_URL], data[CONF_MAC]).genres()
    except StalkerAuthError:
        return "auth"
    except StalkerError as err:
        return "no_channels" if str(err) == "no_channels" else "cannot_connect"
    return None


class StalkerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[SOURCE_STALKER, SOURCE_M3U, SOURCE_XTREAM])

    async def _step(self, source: str, user_input, reconfigure: bool) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry() if reconfigure else None
        if user_input is not None:
            data = _clean(source, user_input)
            if err := await _validate(self.hass, source, data):
                errors["base"] = err
            elif entry:
                return self.async_update_reload_and_abort(entry, data=data)
            else:
                return self.async_create_entry(title="Stalker IPTV", data=data)
        defaults = user_input or (dict(entry.data) if entry else {})
        return self.async_show_form(
            step_id=f"reconfigure_{source}" if reconfigure else source,
            data_schema=_schema(source, defaults), errors=errors,
        )  # fmt: skip

    async def async_step_stalker(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._step(SOURCE_STALKER, user_input, False)

    async def async_step_m3u(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._step(SOURCE_M3U, user_input, False)

    async def async_step_xtream(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._step(SOURCE_XTREAM, user_input, False)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        source = self._get_reconfigure_entry().data.get(CONF_SOURCE, SOURCE_STALKER)
        return await getattr(self, f"async_step_reconfigure_{source}")(user_input)

    async def async_step_reconfigure_stalker(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._step(SOURCE_STALKER, user_input, True)

    async def async_step_reconfigure_m3u(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._step(SOURCE_M3U, user_input, True)

    async def async_step_reconfigure_xtream(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._step(SOURCE_XTREAM, user_input, True)

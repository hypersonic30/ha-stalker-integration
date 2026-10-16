"""Config flow: portal URL + MAC are checked with a real handshake before saving."""
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
    CONF_MAC,
    CONF_MAX_STREAMS,
    CONF_PORTAL_URL,
    CONF_SERIES_M3U,
    CONF_TRANSCODE,
    CONF_VOD_M3U,
    DEFAULT_MAX_STREAMS,
    DEFAULT_TRANSCODE,
    DOMAIN,
)
from .stalker import MAC_RE, StalkerAuthError, StalkerClient, StalkerError
from .streams import MAX_STREAMS_LIMIT

_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _schema(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PORTAL_URL, default=d.get(CONF_PORTAL_URL, "")): _URL,
            vol.Required(CONF_MAC, default=d.get(CONF_MAC, "")): _SECRET,
            vol.Optional(CONF_VOD_M3U, default=d.get(CONF_VOD_M3U, "")): _URL,
            vol.Optional(CONF_SERIES_M3U, default=d.get(CONF_SERIES_M3U, "")): _URL,
            vol.Required(CONF_MAX_STREAMS, default=d.get(CONF_MAX_STREAMS, DEFAULT_MAX_STREAMS)): NumberSelector(
                NumberSelectorConfig(min=1, max=MAX_STREAMS_LIMIT, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_TRANSCODE, default=d.get(CONF_TRANSCODE, DEFAULT_TRANSCODE)): BooleanSelector(),
        }
    )


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    out[CONF_MAC] = out[CONF_MAC].strip().upper()
    out[CONF_MAX_STREAMS] = int(out[CONF_MAX_STREAMS])
    out[CONF_TRANSCODE] = bool(out.get(CONF_TRANSCODE, DEFAULT_TRANSCODE))
    for key in (CONF_PORTAL_URL, CONF_VOD_M3U, CONF_SERIES_M3U):
        out[key] = (out.get(key) or "").strip()
    return out


async def _validate(hass, data: dict[str, Any]) -> str | None:
    for key in (CONF_PORTAL_URL, CONF_VOD_M3U, CONF_SERIES_M3U):
        v = data.get(key)
        if v and not v.lower().startswith(("http://", "https://")):
            return "invalid_url"
    if not MAC_RE.match(data[CONF_MAC]):
        return "invalid_mac"
    try:
        await StalkerClient(
            async_get_clientsession(hass), data[CONF_PORTAL_URL], data[CONF_MAC]
        ).genres()
    except StalkerAuthError:
        return "auth"
    except StalkerError:
        return "cannot_connect"
    return None


class StalkerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(user_input)
            if err := await _validate(self.hass, data):
                errors["base"] = err
            else:
                return self.async_create_entry(title="Stalker IPTV", data=data)
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(user_input)
            if err := await _validate(self.hass, data):
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_schema(user_input or dict(entry.data)), errors=errors
        )

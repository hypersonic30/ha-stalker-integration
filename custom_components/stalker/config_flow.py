"""Config flow: pick a source (Stalker portal, M3U + XMLTV, Xtream Codes); it is checked before saving."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.core import callback
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
    CONF_NAME,
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
    SUBENTRY_PORTAL,
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



_TITLES = {SOURCE_STALKER: "Stalker", SOURCE_M3U: "M3U", SOURCE_XTREAM: "Xtream"}


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
    return vol.Schema({vol.Required(CONF_NAME, default=d.get(CONF_NAME, _TITLES[source])): _TEXT, **fields})


def _clean(source: str, user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    out[CONF_SOURCE] = source
    out[CONF_NAME] = (out.get(CONF_NAME) or _TITLES[source]).strip()
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


def _main_schema(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(_common(d))


class StalkerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Shared settings; the portals themselves are added as sub-entries."""

    VERSION = 2

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_PORTAL: PortalSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Stalker IPTV", data=_shared(user_input))
        return self.async_show_form(step_id="user", data_schema=_main_schema({}))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data=_shared(user_input))
        return self.async_show_form(step_id="reconfigure", data_schema=_main_schema(dict(entry.data)))


def _shared(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_MAX_STREAMS: int(user_input[CONF_MAX_STREAMS]),
        CONF_TRANSCODE: bool(user_input.get(CONF_TRANSCODE, DEFAULT_TRANSCODE)),
    }


class PortalSubentryFlow(ConfigSubentryFlow):
    """"Add portal": Stalker (MAC), M3U + XMLTV or Xtream Codes; checked before saving."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[SOURCE_STALKER, SOURCE_M3U, SOURCE_XTREAM])

    async def _step(self, source: str, user_input, sub=None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(source, user_input)
            if err := await _validate(self.hass, source, data):
                errors["base"] = err
            else:
                title = data.pop(CONF_NAME)
                if sub is not None:
                    return self.async_update_and_abort(self._get_entry(), sub, title=title, data=data)
                return self.async_create_entry(title=title, data=data)
        defaults = user_input or ({**sub.data, CONF_NAME: sub.title} if sub else {})
        return self.async_show_form(
            step_id=f"reconfigure_{source}" if sub else source,
            data_schema=_schema(source, defaults), errors=errors,
        )  # fmt: skip

    async def async_step_stalker(self, user_input=None) -> SubentryFlowResult:
        return await self._step(SOURCE_STALKER, user_input)

    async def async_step_m3u(self, user_input=None) -> SubentryFlowResult:
        return await self._step(SOURCE_M3U, user_input)

    async def async_step_xtream(self, user_input=None) -> SubentryFlowResult:
        return await self._step(SOURCE_XTREAM, user_input)

    async def async_step_reconfigure(self, user_input=None) -> SubentryFlowResult:
        source = self._get_reconfigure_subentry().data.get(CONF_SOURCE, SOURCE_STALKER)
        return await getattr(self, f"async_step_reconfigure_{source}")(user_input)

    async def async_step_reconfigure_stalker(self, user_input=None) -> SubentryFlowResult:
        return await self._step(SOURCE_STALKER, user_input, self._get_reconfigure_subentry())

    async def async_step_reconfigure_m3u(self, user_input=None) -> SubentryFlowResult:
        return await self._step(SOURCE_M3U, user_input, self._get_reconfigure_subentry())

    async def async_step_reconfigure_xtream(self, user_input=None) -> SubentryFlowResult:
        return await self._step(SOURCE_XTREAM, user_input, self._get_reconfigure_subentry())

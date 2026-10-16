"""Constants for the Stalker IPTV integration."""
DOMAIN = "stalker"

SOURCE_STALKER = "stalker"
SOURCE_M3U = "m3u"
SOURCE_XTREAM = "xtream"
CONF_SOURCE = "source"
CONF_PORTAL_URL = "portal_url"
CONF_LIVE_M3U = "live_m3u"
CONF_XMLTV = "xmltv"
CONF_SERVER_URL = "server_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_MAC = "mac"
CONF_VOD_M3U = "vod_m3u"
CONF_SERIES_M3U = "series_m3u"
CONF_MAX_STREAMS = "max_streams"
CONF_TRANSCODE = "transcode"

DEFAULT_MAX_STREAMS = 1
DEFAULT_TRANSCODE = True
CACHE_TTL = 6 * 3600
M3U_MAX_BYTES = 30 * 1024 * 1024
EPG_TTL = 30 * 60

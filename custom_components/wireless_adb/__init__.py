"""Wireless ADB: Android TV over wireless debugging (TLS).

Forked from Home Assistant's core `androidtv` integration (2026.11.0). The
media player and remote are kept close to upstream; the connection layer
(connection.py, discovery.py) replaces classic ADB with wireless debugging:
pairing codes, TLS sessions and an mDNS-advertised port that rotates.
"""

from dataclasses import dataclass
import logging
from typing import Any

from adb_shell_wifi.exceptions import (
    AdbTimeoutError,
    InvalidChecksumError,
    InvalidCommandError,
    InvalidResponseError,
    TcpTimeoutException,
)
from androidtv_wifi.setup_async import AndroidTVAsync

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .connection import async_connect, default_adbkey_path
from .const import (
    CONF_ADBKEY,
    CONF_STATE_DETECTION_RULES,
    DOMAIN,
    PROP_ETHMAC,
    PROP_WIFIMAC,
    SIGNAL_CONFIG_ENTITY,
)
from .discovery import async_find_connect_endpoint
from .services import async_setup_services

ADB_PYTHON_EXCEPTIONS: tuple = (
    AdbTimeoutError,
    BrokenPipeError,
    ConnectionResetError,
    ValueError,
    InvalidChecksumError,
    InvalidCommandError,
    InvalidResponseError,
    TcpTimeoutException,
    OSError,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.REMOTE]
RELOAD_OPTIONS = [CONF_STATE_DETECTION_RULES]

_INVALID_MACS = {"ff:ff:ff:ff:ff:ff"}

_LOGGER = logging.getLogger(__name__)


@dataclass
class WirelessAdbRuntimeData:
    """Runtime data definition."""

    aftv: AndroidTVAsync
    dev_opt: dict[str, Any]


WirelessAdbConfigEntry = ConfigEntry[WirelessAdbRuntimeData]


def get_androidtv_mac(dev_props: dict[str, Any]) -> str | None:
    """Return formatted mac from device properties."""
    for prop_mac in (PROP_ETHMAC, PROP_WIFIMAC):
        if if_mac := dev_props.get(prop_mac):
            mac = format_mac(if_mac)
            if mac not in _INVALID_MACS:
                return mac
    return None


def entry_adbkey(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return the ADB key path used by a config entry."""
    return entry.data.get(CONF_ADBKEY) or default_adbkey_path(hass)


async def async_refresh_endpoint(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Look the device up over mDNS and store a changed host/port.

    Returns True if the stored endpoint changed. The caller decides whether
    to reload.
    """
    if entry.unique_id is None:
        return False
    found = await async_find_connect_endpoint(hass, serial=entry.unique_id)
    if found is None:
        return False
    host, port = found
    if (host, port) == (entry.data[CONF_HOST], entry.data[CONF_PORT]):
        return False
    _LOGGER.info(
        "%s moved from %s:%s to %s:%s",
        entry.title,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        host,
        port,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_HOST: host, CONF_PORT: port}
    )
    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Wireless ADB integration."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: WirelessAdbConfigEntry) -> bool:
    """Set up a device from a config entry."""
    state_det_rules = entry.options.get(CONF_STATE_DETECTION_RULES)
    adbkey = entry_adbkey(hass, entry)

    async def _connect() -> AndroidTVAsync:
        try:
            return await async_connect(
                hass,
                entry.data[CONF_HOST],
                entry.data[CONF_PORT],
                adbkey,
                state_detection_rules=state_det_rules,
            )
        except ADB_PYTHON_EXCEPTIONS as exc:
            raise ConfigEntryNotReady(exc) from exc

    aftv = await _connect()
    if not aftv.available and await async_refresh_endpoint(hass, entry):
        # The stored port was stale (reboot, adbd restart); try the new one.
        aftv = await _connect()

    if not aftv.available:
        raise ConfigEntryNotReady(
            f"Could not open a wireless debugging session with"
            f" {entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}. Is wireless"
            " debugging on, and is this Home Assistant still paired?"
        )

    async def async_close_connection(event: Event) -> None:
        """Close the ADB connection on HA stop."""
        await aftv.adb_close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_close_connection)
    )
    entry.async_on_unload(entry.add_update_listener(update_listener))

    entry.runtime_data = WirelessAdbRuntimeData(aftv, entry.options.copy())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: WirelessAdbConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.aftv.adb_close()
    return unload_ok


async def update_listener(hass: HomeAssistant, entry: WirelessAdbConfigEntry) -> None:
    """Handle config entry updates."""
    # A changed endpoint (from mDNS) needs a fresh connection.
    aftv = entry.runtime_data.aftv
    if (aftv.host, aftv.port) != (entry.data[CONF_HOST], entry.data[CONF_PORT]):
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return

    reload_opt = False
    old_options = entry.runtime_data.dev_opt
    for opt_key, opt_val in entry.options.items():
        if opt_key in RELOAD_OPTIONS:
            old_val = old_options.get(opt_key)
            if old_val is None or old_val != opt_val:
                reload_opt = True
                break

    if reload_opt:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    entry.runtime_data.dev_opt = entry.options.copy()
    async_dispatcher_send(hass, f"{SIGNAL_CONFIG_ENTITY}_{entry.entry_id}")

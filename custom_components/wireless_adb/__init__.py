"""Wireless ADB: Android TV over wireless debugging (TLS).

Forked from Home Assistant's core `androidtv` integration (2026.11.0). The
media player and remote are kept close to upstream; the connection layer
(connection.py, discovery.py, health.py) replaces classic ADB with wireless
debugging: pairing codes, TLS sessions and an mDNS-advertised port that
rotates. helper.py installs the companion app that keeps wireless debugging on.
"""

import asyncio
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
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .connection import (
    ProbeResult,
    async_connect,
    async_probe,
    async_remove_adbkey,
    default_adbkey_path,
)
from .const import (
    CONF_ADBKEY,
    CONF_DEVICE_PROPERTIES,
    CONF_STATE_DETECTION_RULES,
    DOMAIN,
    PROP_ETHMAC,
    PROP_WIFIMAC,
    SIGNAL_CONFIG_ENTITY,
)
from .discovery import async_find_connect_endpoint
from .health import ConnectionHealth
from .helper import BundledHelper, async_get_bundled
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

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.MEDIA_PLAYER,
    Platform.REMOTE,
    Platform.UPDATE,
]
RELOAD_OPTIONS = [CONF_STATE_DETECTION_RULES]

_INVALID_MACS = {"ff:ff:ff:ff:ff:ff"}

_LOGGER = logging.getLogger(__name__)


@dataclass
class WirelessAdbRuntimeData:
    """Runtime data definition."""

    aftv: AndroidTVAsync
    dev_opt: dict[str, Any]
    health: ConnectionHealth
    bundled_helper: BundledHelper | None


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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Wireless ADB integration."""
    async_setup_services(hass)
    return True


async def _async_pairing_rejected(
    hass: HomeAssistant, host: str, port: int, adbkey: str
) -> bool:
    """Return True if the device rejects our key twice in a row.

    Only called for the port the device currently advertises. Two probes a few
    seconds apart, because adbd rejects keys for a moment after it (re)starts.
    """
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(5)
        if await async_probe(hass, host, port, adbkey) is not ProbeResult.REJECTED:
            return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: WirelessAdbConfigEntry) -> bool:
    """Set up a device from a config entry.

    The device doesn't have to be reachable: with its properties stored from an
    earlier session the entities start unavailable, and the media player keeps
    trying (and ConnectionHealth relaunches the helper if the TV is on).
    """
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
    if not aftv.available and entry.unique_id:
        stored = (entry.data[CONF_HOST], entry.data[CONF_PORT])
        found = await async_find_connect_endpoint(hass, serial=entry.unique_id)
        if found and found != stored:
            # The stored port was stale (reboot, adbd restart); use the new one.
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_HOST: found[0], CONF_PORT: found[1]}
            )
            aftv = await _connect()
        elif found and await _async_pairing_rejected(hass, *stored, adbkey):
            # Starts Home Assistant's re-authentication flow (new pairing code).
            raise ConfigEntryAuthFailed(
                f"{entry.title} no longer accepts Home Assistant's key; pair it again"
            )

    if aftv.available:
        props = dict(aftv.device_properties)
        if props and props != entry.data.get(CONF_DEVICE_PROPERTIES):
            # Kept so the entities can be set up while the device is offline.
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICE_PROPERTIES: props}
            )
    elif not entry.data.get(CONF_DEVICE_PROPERTIES):
        # Never connected since 0.2.0: nothing to build the device from yet.
        raise ConfigEntryNotReady(
            f"Could not open a wireless debugging session with"
            f" {entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}. Is wireless"
            " debugging on, and is this Home Assistant still paired?"
        )
    else:
        _LOGGER.info(
            "%s is not reachable yet; setting up its entities as unavailable",
            entry.title,
        )

    health = ConnectionHealth(hass, entry)
    health.adbkey = adbkey
    if aftv.available:
        health.async_connected()
    else:
        health.async_disconnected()

    async def async_close_connection(event: Event) -> None:
        """Close the ADB connection on HA stop."""
        await aftv.adb_close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_close_connection)
    )
    entry.async_on_unload(entry.add_update_listener(update_listener))

    entry.runtime_data = WirelessAdbRuntimeData(
        aftv,
        entry.options.copy(),
        health,
        await async_get_bundled(hass),
    )

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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the integration's key when its last device is removed.

    Re-adding a device then starts from scratch with a new key and a new
    pairing. A key given explicitly in the entry data is left alone.
    """
    if entry.data.get(CONF_ADBKEY):
        return
    others = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if not others:
        _LOGGER.info("Last Wireless ADB device removed; deleting its ADB key")
        await async_remove_adbkey(hass, default_adbkey_path(hass))


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

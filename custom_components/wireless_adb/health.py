"""Connection health: why is the device unreachable, and what to do about it.

The media player calls in here when a connect fails (about once a minute while
the device stays unreachable) and when one succeeds. Each check works out which
of these it is, and reacts:

- The device advertises a different port (adbd restarted): store it, which
  reloads the entry.
- The advertised port refuses our key, repeatedly: the pairing is gone. Start
  Home Assistant's re-authentication flow (enter a new pairing code) and turn
  the "Pairing" problem sensor on.
- Nothing is advertised although the TV is on: wireless debugging is off. Ask
  the helper app to turn it back on by launching it through the Android TV
  Remote integration, which doesn't need ADB.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from homeassistant.components.remote import ATTR_ACTIVITY, DOMAIN as REMOTE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_PORT,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .connection import ProbeResult, async_probe
from .const import (
    CONF_RELAUNCH_ENTITY,
    CONF_RELAUNCH_HELPER,
    DEFAULT_RELAUNCH_HELPER,
    DOMAIN,
    HELPER_LAUNCH_URI,
    SIGNAL_HEALTH,
)
from .discovery import async_find_connect_endpoint

if TYPE_CHECKING:
    from . import WirelessAdbConfigEntry

_LOGGER = logging.getLogger(__name__)

# Consecutive rejections at the advertised port before we call the pairing lost.
REJECTIONS_BEFORE_REAUTH = 3
# Unreachable for this long while the TV is on -> launch the helper ...
RELAUNCH_AFTER_S = 180
# ... and again at most this often.
RELAUNCH_EVERY_S = 300

ANDROIDTV_REMOTE_PLATFORM = "androidtv_remote"


class ConnectionHealth:
    """Tracks one device's connection problems and reacts to them."""

    def __init__(self, hass: HomeAssistant, entry: WirelessAdbConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.adbkey: str = ""  # set by async_setup_entry
        self.connected = False
        self.pairing_lost = False
        self._rejections = 0
        self._unreachable_since: float | None = None
        self._last_relaunch: float | None = None

    @property
    def signal(self) -> str:
        """Dispatcher signal fired when the health state changes."""
        return f"{SIGNAL_HEALTH}_{self.entry.entry_id}"

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def async_connected(self) -> None:
        """Record a working session."""
        changed = not self.connected or self.pairing_lost
        self.connected = True
        self.pairing_lost = False
        self._rejections = 0
        self._unreachable_since = None
        if changed:
            self._notify()

    @callback
    def async_disconnected(self) -> None:
        """Record a failed connect (cheap; the media player calls it every poll)."""
        if self._unreachable_since is None:
            self._unreachable_since = time.monotonic()
        if self.connected:
            self.connected = False
            self._notify()

    async def async_check(self) -> None:
        """Work out why the device is unreachable and react (see module doc)."""
        entry = self.entry
        host, port = entry.data[CONF_HOST], entry.data[CONF_PORT]

        found = (
            await async_find_connect_endpoint(self.hass, serial=entry.unique_id)
            if entry.unique_id
            else None
        )

        if found is None:
            # Not advertising: TV off, or wireless debugging off.
            self._rejections = 0
            await self._async_maybe_relaunch()
            return

        if found != (host, port):
            _LOGGER.info(
                "%s moved from %s:%s to %s:%s", entry.title, host, port, *found
            )
            # The update listener reloads the entry with the new endpoint.
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_HOST: found[0], CONF_PORT: found[1]}
            )
            return

        result = await async_probe(self.hass, host, port, self.adbkey)
        if result is ProbeResult.REJECTED:
            self._rejections += 1
            _LOGGER.debug(
                "%s rejected our key at its advertised port (%s/%s)",
                entry.title,
                self._rejections,
                REJECTIONS_BEFORE_REAUTH,
            )
            if self._rejections >= REJECTIONS_BEFORE_REAUTH and not self.pairing_lost:
                _LOGGER.warning(
                    "%s no longer accepts Home Assistant's key; pair it again",
                    entry.title,
                )
                self.pairing_lost = True
                self._notify()
                entry.async_start_reauth(self.hass)
        elif result is ProbeResult.UNREACHABLE:
            self._rejections = 0
            await self._async_maybe_relaunch()
        else:
            # The probe got in; the media player's next connect should too.
            self._rejections = 0

    async def _async_maybe_relaunch(self) -> None:
        """Launch the helper through Android TV Remote if the TV is on."""
        if self.pairing_lost or not self.entry.options.get(
            CONF_RELAUNCH_HELPER, DEFAULT_RELAUNCH_HELPER
        ):
            return
        now = time.monotonic()
        if (
            self._unreachable_since is None
            or now - self._unreachable_since < RELAUNCH_AFTER_S
        ):
            return
        if (
            self._last_relaunch is not None
            and now - self._last_relaunch < RELAUNCH_EVERY_S
        ):
            return

        remote = self.relaunch_entity_id()
        if remote is None:
            return
        state = self.hass.states.get(remote)
        if state is None or state.state != STATE_ON:
            # Off or unavailable: nothing to relaunch into.
            return

        self._last_relaunch = now
        _LOGGER.info(
            "%s unreachable for %d s while %s is on; launching the helper",
            self.entry.title,
            now - self._unreachable_since,
            remote,
        )
        await self.hass.services.async_call(
            REMOTE_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: remote, ATTR_ACTIVITY: HELPER_LAUNCH_URI},
            blocking=False,
        )

    @callback
    def relaunch_entity_id(self) -> str | None:
        """Return the configured remote, or the Android TV Remote on our device."""
        if configured := self.entry.options.get(CONF_RELAUNCH_ENTITY):
            return configured
        return find_androidtv_remote(self.hass, self.entry)


@callback
def find_androidtv_remote(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Find an Android TV Remote `remote` entity on the same device as ours.

    Our entities join the TV's existing device through its MAC address, the
    same way Android TV Remote's do, so they share a device.
    """
    if entry.unique_id is None:
        return None
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, entry.unique_id)}
    )
    if device is None:
        return None
    for entity in er.async_entries_for_device(er.async_get(hass), device.id):
        if (
            entity.domain == REMOTE_DOMAIN
            and entity.platform == ANDROIDTV_REMOTE_PLATFORM
        ):
            return entity.entity_id
    return None

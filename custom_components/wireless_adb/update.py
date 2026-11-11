"""Update entity for the Wireless ADB Helper app on the device.

Installed version from `dumpsys package`, latest from the APK bundled with this
integration release. "Install" pushes the APK, installs it in place, grants its
permissions and launches it. If the helper is installed but has lost its
WRITE_SECURE_SETTINGS grant (e.g. reinstalled by hand), the next check grants
it again without asking: the helper is useless without it.
"""

from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WirelessAdbConfigEntry
from .entity import WirelessAdbEntity
from .helper import (
    BundledHelper,
    HelperError,
    InstalledHelper,
    async_get_installed,
    async_grant_and_launch,
    async_install,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(hours=6)

NOT_INSTALLED = "not installed"
RELEASE_URL = "https://github.com/YamenSharaf/ha-wireless-adb/tree/main/android"
_CODE = re.compile(r"\((\d+)\)$")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WirelessAdbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the helper update entity (only when a helper APK is bundled)."""
    if bundled := entry.runtime_data.bundled_helper:
        async_add_entities(
            [HelperUpdateEntity(entry, bundled)], update_before_add=False
        )


class HelperUpdateEntity(WirelessAdbEntity, UpdateEntity):
    """The helper app's installed vs bundled version."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_translation_key = "helper"
    _attr_title = "Wireless ADB Helper"
    _attr_release_url = RELEASE_URL
    _attr_should_poll = True

    def __init__(self, entry: WirelessAdbConfigEntry, bundled: BundledHelper) -> None:
        """Initialize."""
        super().__init__(entry)
        self._bundled = bundled
        self._health = entry.runtime_data.health
        self._installed: InstalledHelper | None = None
        self._attr_unique_id = f"{entry.unique_id}_helper"
        self._attr_latest_version = str(bundled.version)

    @property
    def available(self) -> bool:
        """Available while there's a session and we've read the device once."""
        return self._health.connected and self._installed is not None

    @property
    def installed_version(self) -> str | None:
        """Return the installed version, or 'not installed'."""
        if self._installed is None:
            return None
        if self._installed.version is None:
            return NOT_INSTALLED
        return str(self._installed.version)

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Compare by versionCode; anything beats 'not installed'."""
        latest, installed = (
            _CODE.search(latest_version),
            _CODE.search(installed_version),
        )
        if latest is None or installed is None:
            return latest_version != installed_version
        return int(latest.group(1)) > int(installed.group(1))

    async def async_added_to_hass(self) -> None:
        """Re-read the device whenever the connection comes back."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._health.signal, self._async_health_changed
            )
        )
        if self._health.connected:
            self.async_schedule_update_ha_state(force_refresh=True)

    @callback
    def _async_health_changed(self) -> None:
        self.async_schedule_update_ha_state(force_refresh=self._health.connected)

    async def async_update(self) -> None:
        """Read the helper's state from the device; re-grant if needed."""
        if not (self._health.connected and self.aftv.available):
            # Also skips the closing session of an entry that's reloading.
            return
        try:
            installed = await async_get_installed(self.aftv)
            if installed.installed and not installed.granted:
                _LOGGER.warning(
                    "The helper app on %s lost WRITE_SECURE_SETTINGS; granting it again",
                    self._entry.title,
                )
                await async_grant_and_launch(self.aftv)
                installed = await async_get_installed(self.aftv)
        except Exception as err:  # noqa: BLE001 - busy or dropped connection: retry later
            _LOGGER.debug("Could not read the helper state: %r", err)
            return
        self._installed = installed

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install or update the bundled helper."""
        try:
            await async_install(self.aftv, self._bundled)
        except HelperError as err:
            raise HomeAssistantError(f"Installing the helper failed: {err}") from err
        except Exception as err:
            raise HomeAssistantError(
                f"Installing the helper failed (connection problem?): {err}"
            ) from err
        self._installed = await async_get_installed(self.aftv)

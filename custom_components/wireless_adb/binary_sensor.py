"""Connection and pairing status sensors.

For automations: "ADB connection" is on while a wireless-debugging session is
up; "Pairing" (a problem sensor) turns on when the device stops accepting Home
Assistant's key, alongside the re-authentication flow.
"""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WirelessAdbConfigEntry
from .entity import WirelessAdbEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WirelessAdbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the status sensors."""
    async_add_entities([ConnectionSensor(entry), PairingSensor(entry)])


class _HealthSensor(WirelessAdbEntity, BinarySensorEntity):
    """Base for sensors that mirror ConnectionHealth."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _key: str

    def __init__(self, entry: WirelessAdbConfigEntry) -> None:
        """Initialize."""
        super().__init__(entry)
        self._health = entry.runtime_data.health
        self._attr_unique_id = f"{entry.unique_id}_{self._key}"
        self._attr_translation_key = self._key
        # Reports on the connection; always available itself.
        self._attr_available = True

    async def async_added_to_hass(self) -> None:
        """Follow health changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._health.signal, self._async_health_changed
            )
        )

    @callback
    def _async_health_changed(self) -> None:
        self.async_write_ha_state()


class ConnectionSensor(_HealthSensor):
    """On while a wireless-debugging session is up."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _key = "connection"

    @property
    def is_on(self) -> bool:
        """Return True if connected."""
        return self._health.connected


class PairingSensor(_HealthSensor):
    """On (problem) when the device no longer accepts our key."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _key = "pairing"

    @property
    def is_on(self) -> bool:
        """Return True if the pairing is lost."""
        return self._health.pairing_lost

"""Valve binary sensor for Wein."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL
from .device import WeinDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wein binary sensors."""
    async_add_entities([WeinValveSensor(entry.runtime_data)])


class WeinValveSensor(BinarySensorEntity):
    """Valve open/closed state from BLE notifications."""

    _attr_has_entity_name = True
    _attr_translation_key = "valve"
    _attr_device_class = BinarySensorDeviceClass.OPENING
    _attr_should_poll = False

    def __init__(self, device: WeinDevice) -> None:
        self._device = device
        self._attr_unique_id = f"{device.address}_valve"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.address)},
            connections={(CONNECTION_BLUETOOTH, device.address)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_on(self) -> bool | None:
        return self._device.valve_open

    @property
    def available(self) -> bool:
        return self._device.available and self._device.valve_open is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._device.async_add_listener(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

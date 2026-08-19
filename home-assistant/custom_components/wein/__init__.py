"""The Wein BLE integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .device import WeinDevice

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

type WeinConfigEntry = ConfigEntry[WeinDevice]


async def async_setup_entry(hass: HomeAssistant, entry: WeinConfigEntry) -> bool:
    """Set up Wein from a config entry."""
    address = entry.unique_id or entry.data[CONF_ADDRESS]
    device = WeinDevice(hass, address, entry.title)
    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await device.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WeinConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok

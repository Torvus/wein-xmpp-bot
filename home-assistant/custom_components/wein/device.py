"""BLE device client for the Wein thermometer and valve."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import (
    TEMPERATURE_CHAR_UUID,
    VALVE_POLL_INTERVAL,
    VALVE_STATE_CHAR_UUID,
)
from .parser import normalize_uuid, parse_boolean, parse_temperature

_LOGGER = logging.getLogger(__name__)


class WeinDevice:
    """Keeps a GATT session and pushes temperature / valve updates."""

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.temperature: float | None = None
        self.valve_open: bool | None = None
        self.available = False
        self._client: BleakClient | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._connect_lock = asyncio.Lock()
        self._connect_task: asyncio.Task[None] | None = None
        self._valve_poll_task: asyncio.Task[None] | None = None
        self._unloaded = False
        self._unsubs: list[Callable[[], None]] = []

    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(update_callback)

        def _remove() -> None:
            self._listeners.discard(update_callback)

        return _remove

    @callback
    def async_update_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        self._unsubs.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_advertisement,
                {"address": self.address, "connectable": True},
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        if bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        ):
            self._schedule_connect()

    async def async_stop(self) -> None:
        self._unloaded = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        await self._disconnect()

    @callback
    def _async_advertisement(
        self,
        _service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        if self._unloaded:
            return
        if self._client is not None and self._client.is_connected:
            return
        self._schedule_connect()

    @callback
    def _schedule_connect(self) -> None:
        if self._connect_task and not self._connect_task.done():
            return
        self._connect_task = self.hass.async_create_task(self._async_connect())

    async def _async_connect(self) -> None:
        async with self._connect_lock:
            if self._unloaded or (self._client is not None and self._client.is_connected):
                return
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug("%s: not in range", self.address)
                return
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=3,
                    use_services_cache=True,
                )
                await self._subscribe(client)
            except Exception:
                _LOGGER.exception("%s: failed to connect", self.address)
                self.available = False
                self.async_update_listeners()
                return
            self._client = client
            self.available = True
            _LOGGER.info("%s: connected", self.address)
            self.async_update_listeners()

    def _on_disconnect(self, _client: BleakClient) -> None:
        self.hass.loop.call_soon_threadsafe(self._handle_disconnect)

    @callback
    def _handle_disconnect(self) -> None:
        _LOGGER.info("%s: disconnected", self.address)
        self._client = None
        self.available = False
        self._cancel_valve_poll()
        self.async_update_listeners()

    async def _subscribe(self, client: BleakClient) -> None:
        temp_char = _find_characteristic(client, TEMPERATURE_CHAR_UUID)
        if temp_char is None:
            raise RuntimeError(f"Temperature characteristic {TEMPERATURE_CHAR_UUID} not found")
        await client.start_notify(temp_char, self._temperature_handler)

        valve_char = _find_characteristic(client, VALVE_STATE_CHAR_UUID)
        if valve_char is None:
            raise RuntimeError(f"Valve characteristic {VALVE_STATE_CHAR_UUID} not found")

        props = list(valve_char.properties)
        if "notify" in props or "indicate" in props:
            await client.start_notify(valve_char, self._valve_handler)
        if "read" in props:
            try:
                data = await client.read_gatt_char(valve_char)
                self._apply_valve(data)
            except Exception:
                _LOGGER.debug("%s: initial valve read failed", self.address, exc_info=True)
            if "notify" not in props and "indicate" not in props:
                self._valve_poll_task = self.hass.async_create_task(
                    self._poll_valve(client, valve_char)
                )
        elif "notify" not in props and "indicate" not in props:
            raise RuntimeError(
                f"Valve characteristic {VALVE_STATE_CHAR_UUID} has no notify/read: {props}"
            )

    async def _poll_valve(
        self, client: BleakClient, char: BleakGATTCharacteristic
    ) -> None:
        try:
            while not self._unloaded and client.is_connected:
                try:
                    data = await client.read_gatt_char(char)
                    self._apply_valve(data)
                    self.async_update_listeners()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.debug("%s: valve poll failed", self.address, exc_info=True)
                await asyncio.sleep(VALVE_POLL_INTERVAL)
        except asyncio.CancelledError:
            return

    def _temperature_handler(
        self, _char: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        value = parse_temperature(data)
        if value is None:
            _LOGGER.debug("%s: bad temperature payload %r", self.address, bytes(data))
            return
        self.hass.loop.call_soon_threadsafe(self._set_temperature, value)

    def _valve_handler(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self.hass.loop.call_soon_threadsafe(self._set_valve_from_bytes, bytes(data))

    @callback
    def _set_temperature(self, value: float) -> None:
        self.temperature = value
        self.async_update_listeners()

    @callback
    def _set_valve_from_bytes(self, data: bytes) -> None:
        self._apply_valve(data)
        self.async_update_listeners()

    def _apply_valve(self, data: bytes | bytearray) -> None:
        value = parse_boolean(data)
        if value is None:
            _LOGGER.debug("%s: bad valve payload %r", self.address, bytes(data))
            return
        self.valve_open = value

    def _cancel_valve_poll(self) -> None:
        if self._valve_poll_task is not None and not self._valve_poll_task.done():
            self._valve_poll_task.cancel()
        self._valve_poll_task = None

    async def _disconnect(self) -> None:
        self._cancel_valve_poll()
        client = self._client
        self._client = None
        self.available = False
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            _LOGGER.debug("%s: disconnect failed", self.address, exc_info=True)


def _find_characteristic(
    client: BleakClient, uuid: str
) -> BleakGATTCharacteristic | None:
    target = normalize_uuid(uuid)
    for service in client.services:
        for char in service.characteristics:
            if normalize_uuid(char.uuid) == target:
                return char
    return None

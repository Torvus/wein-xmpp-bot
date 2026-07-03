"""Проверка сообщений об отсутствии Bluetooth-адаптера."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason

from ble_sensor import (
    BluetoothAdapterError,
    _bluetooth_unavailable_message,
    check_bluetooth_adapter,
)


class BluetoothUnavailableMessageTests(unittest.TestCase):
    def test_no_bluetooth(self) -> None:
        exc = BleakBluetoothNotAvailableError(
            "No Bluetooth adapters found.",
            BleakBluetoothNotAvailableReason.NO_BLUETOOTH,
        )
        msg = _bluetooth_unavailable_message(exc)
        self.assertIn("адаптер не найден", msg.lower())

    def test_powered_off(self) -> None:
        exc = BleakBluetoothNotAvailableError(
            "No powered Bluetooth adapters found.",
            BleakBluetoothNotAvailableReason.POWERED_OFF,
        )
        msg = _bluetooth_unavailable_message(exc)
        self.assertIn("выключен", msg.lower())


class CheckBluetoothAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_raises_adapter_error(self) -> None:
        bleak_error = BleakBluetoothNotAvailableError(
            "No Bluetooth adapters found.",
            BleakBluetoothNotAvailableReason.NO_BLUETOOTH,
        )
        mock_scanner = AsyncMock()
        mock_scanner.__aenter__ = AsyncMock(side_effect=bleak_error)
        mock_scanner.__aexit__ = AsyncMock(return_value=False)

        with patch("ble_sensor.BleakScanner", return_value=mock_scanner):
            with self.assertRaises(BluetoothAdapterError) as ctx:
                await check_bluetooth_adapter()

        self.assertIn("адаптер не найден", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()

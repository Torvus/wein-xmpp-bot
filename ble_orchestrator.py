"""Запуск и остановка BLE-мониторинга в зависимости от списка подписчиков."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app_config import AppConfig
from ble_sensor import BluetoothAdapterError, SensorCallbacks, check_bluetooth_adapter, run_sensor_loop

log = logging.getLogger(__name__)


class BleOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        callbacks: SensorCallbacks,
        *,
        on_fatal: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._config = config
        self._callbacks = callbacks
        self._on_fatal = on_fatal
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def has_subscribers(self) -> bool:
        return bool(self._config.subscribers)

    async def sync(self) -> None:
        log.debug(
            "BLE sync: subscribers=%s task_active=%s",
            self._config.subscribers,
            self._task is not None and not self._task.done(),
        )
        if self.has_subscribers():
            await self.start()
        else:
            await self.stop()

    async def on_subscribers_changed(self) -> None:
        await self.sync()

    async def start(self) -> None:
        async with self._lock:
            if not self.has_subscribers():
                log.info("BLE: подписчиков нет, мониторинг не запускается")
                return
            if self._task is not None and not self._task.done():
                return
            try:
                await check_bluetooth_adapter()
            except BluetoothAdapterError as exc:
                self._notify_fatal(exc)
                return
            self._task = asyncio.create_task(
                run_sensor_loop(
                    self._config,
                    self._callbacks,
                    self.has_subscribers,
                ),
                name="ble-sensor-loop",
            )
            self._task.add_done_callback(self._log_task_failure)
            log.info("BLE: мониторинг датчика запущен")
            log.debug("BLE: task=%s", self._task.get_name())

    def _log_task_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            log.debug("BLE-цикл: задача отменена")
            return
        exc = task.exception()
        if exc is not None:
            if isinstance(exc, BluetoothAdapterError):
                log.error("Bluetooth-адаптер недоступен: %s", exc)
                self._notify_fatal(exc)
                return
            log.error("BLE-цикл завершился неожиданно", exc_info=exc)
            if self.has_subscribers():
                log.info("BLE: перезапуск мониторинга после сбоя")
                asyncio.create_task(self._restart_after_failure())

    def _notify_fatal(self, exc: BaseException) -> None:
        if self._on_fatal is not None:
            self._on_fatal(exc)

    async def _restart_after_failure(self) -> None:
        await asyncio.sleep(10)
        async with self._lock:
            if not self.has_subscribers():
                return
            if self._task is not None and not self._task.done():
                return
            self._task = None
        await self.start()

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            if task is None:
                return
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._task = None
            log.info("BLE: мониторинг датчика остановлен")

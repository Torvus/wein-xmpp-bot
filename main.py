#!/usr/bin/env python3
"""Термометр BLE + XMPP-бот с OMEMO и пороговыми оповещениями."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app_config import AppConfig, ConfigError
from ble_orchestrator import BleOrchestrator
from ble_sensor import BluetoothAdapterError, SensorCallbacks, SensorError
from threshold_monitor import ThresholdMonitor
from xmpp_bot import create_bot

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

log = logging.getLogger("wein")


def configure_logging(log_file: str | None, *, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        log.info("Логирование в файл: %s", path.resolve())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        metavar="FILE",
        help="дополнительно писать логи в указанный файл",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="включить отладочные логи (DEBUG)",
    )
    return parser.parse_args()


async def run() -> None:
    log.info("Запуск Wein…")
    try:
        config = AppConfig.load()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    if config.sensor is None:
        raise SystemExit(
            "В config.json нужна секция «sensor» (см. config.example.json)."
        )

    monitor = ThresholdMonitor(config.thresholds)

    async def notify(text: str) -> None:
        await bot.notify_subscribers(text)

    async def on_connected() -> None:
        if not config.subscribers:
            return
        monitor.update_thresholds(config.thresholds)
        await notify(config.sensor.messages.connected)

    async def on_disconnected() -> None:
        if not config.subscribers:
            return
        await notify(config.sensor.messages.disconnected)

    async def on_valve_control_changed(active: bool) -> None:
        if not config.subscribers:
            return
        messages = config.sensor.messages
        text = (
            messages.valve_control_enabled
            if active
            else messages.valve_control_disabled
        )
        log.info("Режим клапана: %s", text)
        await notify(text)

    async def on_valve_state_changed(open: bool) -> None:
        if not config.subscribers:
            return
        messages = config.sensor.messages
        text = messages.valve_opened if open else messages.valve_closed
        log.info("Клапан: %s", text)
        await notify(text)

    def on_temperature(value: float) -> None:
        if not config.subscribers:
            return
        print(f"Температура: {value}")
        for alert in monitor.check(value):
            log.info("Порог: %s", alert.message)
            asyncio.create_task(notify(alert.message))

    callbacks = SensorCallbacks(
        on_connected=on_connected,
        on_disconnected=on_disconnected,
        on_temperature=on_temperature,
        on_valve_control_changed=on_valve_control_changed,
        on_valve_state_changed=on_valve_state_changed,
    )

    ble_fatal = asyncio.Event()
    fatal_exc: list[BaseException] = []

    def on_ble_fatal(exc: BaseException) -> None:
        fatal_exc.append(exc)
        ble_fatal.set()

    orchestrator = BleOrchestrator(config, callbacks, on_fatal=on_ble_fatal)
    bot = create_bot(config, orchestrator)
    await bot.connect()
    await bot.ready.wait()

    log.info(
        "Подписчики: %s",
        ", ".join(config.subscribers) or "(пусто — отправьте боту «start»)",
    )
    await orchestrator.sync()

    try:
        await ble_fatal.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await orchestrator.stop()
        await bot.disconnect()  # type: ignore[misc]

    if fatal_exc:
        raise fatal_exc[0]


def main() -> None:
    args = parse_args()
    configure_logging(args.log, verbose=args.verbose)
    try:
        asyncio.run(run())
    except (ConfigError, SensorError, BluetoothAdapterError) as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        log.info("Завершение по Ctrl+C")
    except Exception:
        log.exception("Необработанная ошибка")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

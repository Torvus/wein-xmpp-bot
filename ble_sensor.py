"""BLE-термометр: подключение, мониторинг и переподключение."""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import (
    BleakBluetoothNotAvailableError,
    BleakBluetoothNotAvailableReason,
    BleakDBusError,
    BleakDeviceNotFoundError,
)

from app_config import AppConfig, CONFIG_PATH, ConfigError, SensorConfig
from bluez_pairing_agent import pairing_agent_session

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 20.0
BONDED_CONNECT_TIMEOUT = 15.0
VALVE_CONTROL_POLL_INTERVAL = 2.0
SCAN_BUSY_RETRY_DELAY = 5.0
SCAN_BUSY_MAX_RETRIES = 6
BLUEZ_BUSY_ERRORS = frozenset(
    {
        "org.bluez.Error.InProgress",
        "org.bluez.Error.NotReady",
    }
)


class SensorError(Exception):
    """Ошибка при работе с BLE-датчиком."""


class BluetoothAdapterError(SensorError):
    """Нет подходящего Bluetooth-адаптера для BLE."""


def _bluetooth_unavailable_message(exc: BleakBluetoothNotAvailableError) -> str:
    reason = exc.reason
    if reason == BleakBluetoothNotAvailableReason.NO_BLUETOOTH:
        return (
            "Bluetooth-адаптер не найден. "
            "Подключите адаптер с поддержкой BLE и перезапустите бота."
        )
    if reason == BleakBluetoothNotAvailableReason.NO_BLE_CENTRAL_ROLE:
        return (
            "Найден Bluetooth-адаптер без поддержки BLE (роль central). "
            "Нужен другой адаптер."
        )
    if reason == BleakBluetoothNotAvailableReason.POWERED_OFF:
        return (
            "Bluetooth выключен. Включите Bluetooth и перезапустите бота."
        )
    if reason == BleakBluetoothNotAvailableReason.DENIED_BY_SYSTEM:
        return "Доступ к Bluetooth запрещён системой."
    if reason == BleakBluetoothNotAvailableReason.DENIED_BY_USER:
        return "Доступ к Bluetooth запрещён пользователем."
    return f"Bluetooth недоступен: {exc}"


async def check_bluetooth_adapter() -> None:
    """Проверить наличие включённого BLE-адаптера."""
    try:
        async with BleakScanner():
            pass
    except BleakBluetoothNotAvailableError as exc:
        raise BluetoothAdapterError(_bluetooth_unavailable_message(exc)) from exc


@dataclass
class SensorCallbacks:
    on_connected: Callable[[], Awaitable[None]]
    on_disconnected: Callable[[], Awaitable[None]]
    on_temperature: Callable[[float], None]
    on_valve_control_changed: Callable[[bool], Awaitable[None]]
    on_valve_state_changed: Callable[[bool], Awaitable[None]]


def normalize_uuid(uuid: str) -> str:
    return uuid.lower().replace("-", "")


def has_characteristic(client: BleakClient, uuid: str) -> bool:
    return get_characteristic(client, uuid) is not None


def get_characteristic(
    client: BleakClient, uuid: str
) -> BleakGATTCharacteristic | None:
    target = normalize_uuid(uuid)
    for service in client.services:
        for char in service.characteristics:
            if normalize_uuid(char.uuid) == target:
                return char
    return None


def characteristic_properties(char: BleakGATTCharacteristic) -> list[str]:
    return list(char.properties)


def supports_notify(char: BleakGATTCharacteristic) -> bool:
    props = characteristic_properties(char)
    return "notify" in props or "indicate" in props


def supports_read(char: BleakGATTCharacteristic) -> bool:
    return "read" in characteristic_properties(char)


def log_sensor_gatt(client: BleakClient, sensor: SensorConfig) -> None:
    for key, uuid in (
        ("temperature", sensor.temperature_notify_uuid),
        ("valve_control", sensor.valve_control_notify_uuid),
        ("valve_state", sensor.valve_state_notify_uuid),
    ):
        char = get_characteristic(client, uuid)
        if char is None:
            log.warning("GATT: %s %s — не найдена", key, uuid)
            continue
        log.info(
            "GATT: %s %s flags=%s",
            key,
            uuid,
            characteristic_properties(char),
        )

    temp_char = get_characteristic(client, sensor.temperature_notify_uuid)
    if temp_char is None:
        return
    service_uuid = temp_char.service_uuid
    for service in client.services:
        if normalize_uuid(service.uuid) != normalize_uuid(service_uuid):
            continue
        notify_chars = [
            c.uuid
            for c in service.characteristics
            if supports_notify(c)
            and normalize_uuid(c.uuid)
            != normalize_uuid(sensor.temperature_notify_uuid)
        ]
        if notify_chars:
            log.info(
                "GATT: прочие notify в сервисе %s: %s",
                service.uuid,
                ", ".join(notify_chars),
            )
        break


async def _bonded_ble_device(address: str) -> BLEDevice | None:
    """Сохранённое в BlueZ устройство (без активной рекламы)."""
    from bleak.backends.bluezdbus import defs
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager

    manager = await get_global_bluez_manager()
    normalized = address.upper()
    for path, interfaces in manager._properties.items():
        device_props = interfaces.get(defs.DEVICE_INTERFACE)
        if not device_props:
            continue
        if device_props.get("Address", "").upper() != normalized:
            continue
        if not (device_props.get("Paired") or device_props.get("Bonded")):
            continue
        name = device_props.get("Alias") or device_props.get("Name") or address
        log.debug("BlueZ cache: bonded device path=%s name=%r", path, name)
        return BLEDevice(
            address=device_props["Address"],
            name=name,
            details={"path": path, "props": device_props},
        )
    return None


def parse_temperature(data: bytes | bytearray) -> float | None:
    raw = bytes(data)
    if len(raw) == 0:
        return None
    if len(raw) == 2:
        return int.from_bytes(raw, "little", signed=True) / 100.0
    if len(raw) == 4:
        return struct.unpack("<f", raw)[0]
    try:
        text = raw.decode("utf-8").strip().replace(",", ".")
        return float(text)
    except (UnicodeDecodeError, ValueError):
        return None


async def scan_devices():
    print("Сканирование BLE-устройств...")
    return await BleakScanner.discover()


def print_device_list(devices) -> None:
    if not devices:
        print("Устройства не найдены.")
        return
    for index, device in enumerate(devices, start=1):
        name = device.name or "Без имени"
        print(f"  {index}. {name}  [{device.address}]")


async def select_device_address(config: AppConfig) -> str:
    devices = await scan_devices()
    print_device_list(devices)
    if not devices:
        raise SystemExit(1)

    while True:
        try:
            raw = input("Введите номер целевого устройства: ").strip()
            choice = int(raw)
            if 1 <= choice <= len(devices):
                address = devices[choice - 1].address
                config.mac_address = address
                config.save(CONFIG_PATH)
                log.info(f"MAC сохранён в {CONFIG_PATH.name}")
                return address
        except ValueError:
            pass
        print(f"Укажите число от 1 до {len(devices)}.")


async def resolve_device_address(config: AppConfig) -> str:
    if config.mac_address:
        log.info(f"MAC из конфигурации: {config.mac_address}")
        return config.mac_address
    return await select_device_address(config)


def parse_boolean_value(data: bytes | bytearray) -> bool | None:
    """True — включено управление клапаном, False — режим контроля температуры."""
    try:
        text = bytes(data).decode("utf-8").strip().lower()
    except UnicodeDecodeError:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _require_characteristic(
    client: BleakClient, uuid: str, config_key: str
) -> BleakGATTCharacteristic:
    char = get_characteristic(client, uuid)
    if char is None:
        raise SensorError(
            f"Устройство не содержит характеристику {uuid} "
            f"(sensor.{config_key} в config.json)"
        )
    return char


def _require_notify_characteristic(
    client: BleakClient, uuid: str, config_key: str
) -> BleakGATTCharacteristic:
    char = _require_characteristic(client, uuid, config_key)
    if not supports_notify(char):
        raise SensorError(
            f"Характеристика {uuid} (sensor.{config_key}) не поддерживает notify: "
            f"{characteristic_properties(char)}"
        )
    return char


async def disconnect_client(client: BleakClient, *notify_uuids: str) -> None:
    if not client.is_connected:
        log.debug("disconnect_client: уже отключён")
        return
    for uuid in notify_uuids:
        try:
            log.debug("stop_notify %s", uuid)
            await client.stop_notify(uuid)
        except Exception as exc:
            log.debug("stop_notify %s: %s", uuid, exc)
    log.debug("client.disconnect()")
    await client.disconnect()


async def _find_device_by_address(
    address: str,
    timeout_sec: float,
    *,
    context: str,
) -> BLEDevice | None:
    """Сканирование с повтором при занятом адаптере BlueZ."""
    for attempt in range(1, SCAN_BUSY_MAX_RETRIES + 1):
        try:
            return await BleakScanner.find_device_by_address(
                address, timeout=timeout_sec
            )
        except BleakBluetoothNotAvailableError as exc:
            raise BluetoothAdapterError(_bluetooth_unavailable_message(exc)) from exc
        except BleakDBusError as exc:
            if exc.dbus_error not in BLUEZ_BUSY_ERRORS:
                raise
            log.warning(
                "BlueZ занят при %s %s (%s), повтор через %s сек (%s/%s)",
                context,
                address,
                exc.dbus_error,
                int(SCAN_BUSY_RETRY_DELAY),
                attempt,
                SCAN_BUSY_MAX_RETRIES,
            )
            await asyncio.sleep(SCAN_BUSY_RETRY_DELAY)
    log.warning(
        "Сканирование %s (%s): BlueZ остаётся занятым после %s попыток",
        address,
        context,
        SCAN_BUSY_MAX_RETRIES,
    )
    return None


async def _connect_to_device(
    device: BLEDevice,
    address: str,
    *,
    disconnected_callback: Callable[[BleakClient], None] | None = None,
) -> BleakClient:
    if sys.platform == "linux":
        print("При необходимости сопряжения введите код с дисплея устройства.")
    client = BleakClient(
        device,
        pair=True,
        disconnected_callback=disconnected_callback,
    )
    await client.connect(timeout=CONNECT_TIMEOUT)
    log.info("Подключено к %s", address)
    return client


async def _connect_client(
    address: str,
    *,
    disconnected_callback: Callable[[BleakClient], None] | None = None,
    known_device: BLEDevice | None = None,
    scan_timeout_sec: float = 15.0,
) -> BleakClient:
    """Подключение: известное устройство → сканирование → сохранённое в BlueZ."""
    if known_device is not None:
        log.debug("Подключение к ранее обнаруженному устройству %s", address)
        return await _connect_to_device(
            known_device, address, disconnected_callback=disconnected_callback
        )

    log.debug("Сканирование %s (до %s сек)…", address, scan_timeout_sec)
    device = await _find_device_by_address(
        address, scan_timeout_sec, context="подключение"
    )
    if device is not None:
        log.info(
            "Датчик найден в эфире: name=%r rssi=%s",
            device.name,
            getattr(device, "rssi", "?"),
        )
        return await _connect_to_device(
            device, address, disconnected_callback=disconnected_callback
        )

    bonded = await _bonded_ble_device(address)
    if bonded is not None:
        log.info(
            "Попытка подключения к сохранённому устройству %r…",
            bonded.name,
        )
        client = BleakClient(
            bonded,
            pair=True,
            disconnected_callback=disconnected_callback,
        )
        try:
            await client.connect(timeout=BONDED_CONNECT_TIMEOUT)
            log.info("Подключено к %s", address)
            return client
        except TimeoutError:
            log.warning(
                "Датчик %s не ответил за %s сек (выключен или вне зоны)",
                address,
                int(BONDED_CONNECT_TIMEOUT),
            )
            try:
                await client.disconnect()
            except Exception:
                pass
            raise TimeoutError(
                f"Датчик {address} не отвечает на подключение и не рекламируется"
            ) from None

    raise BleakDeviceNotFoundError(
        address, f"Device with address {address} was not found."
    )


async def _watch_connection(
    client: BleakClient,
    disconnected: asyncio.Event,
    intentional_disconnect: list[bool],
    disconnect_reason: list[str],
) -> None:
    """Запасной контроль связи, если callback не сработал."""
    poll = 0
    while not disconnected.is_set():
        if intentional_disconnect[0]:
            log.debug("watch: штатное отключение")
            disconnected.set()
            return
        if not client.is_connected:
            disconnect_reason[0] = "watch_poll (is_connected=False)"
            log.warning(
                "Связь с датчиком потеряна (watch_poll, callback не сработал?)"
            )
            disconnected.set()
            return
        poll += 1
        if poll % 15 == 0:
            log.debug("watch: связь активна (poll #%s)", poll)
        await asyncio.sleep(2.0)


async def _run_monitoring_session(
    address: str,
    sensor: SensorConfig,
    callbacks: SensorCallbacks,
    *,
    known_device: BLEDevice | None = None,
) -> None:
    """Подключиться, слушать клапан и температуру (по режиму) до разрыва связи."""
    temp_uuid = sensor.temperature_notify_uuid
    valve_control_uuid = sensor.valve_control_notify_uuid
    valve_state_uuid = sensor.valve_state_notify_uuid
    disconnected = asyncio.Event()
    intentional_disconnect = [False]
    disconnect_reason = ["—"]
    client: BleakClient | None = None
    session_active = False
    watch_task: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()
    valve_control_active = False
    valve_open: bool | None = None
    valve_state_last_seen: bool | None = None
    temp_ignored_logged = False
    valve_poll_task: asyncio.Task[None] | None = None
    valve_state_char: BleakGATTCharacteristic | None = None

    def on_ble_disconnect(_client: BleakClient) -> None:
        if intentional_disconnect[0]:
            log.debug("BLE disconnected_callback: штатное отключение")
            return
        disconnect_reason[0] = "disconnected_callback"
        log.warning("BLE disconnected_callback: неожиданный разрыв связи")
        loop.call_soon_threadsafe(disconnected.set)

    async def apply_valve_state(open: bool, *, source: str, force: bool = False) -> None:
        nonlocal valve_open
        if not valve_control_active:
            log.debug(
                "valve_state (%s): игнорируется, контроль клапана выключен (%s)",
                source,
                "открыт" if open else "закрыт",
            )
            return
        if not force and open == valve_open:
            log.debug("valve_state (%s): без изменений (%s)", source, open)
            return
        valve_open = open
        log.info(
            "Состояние клапана (%s): %s",
            source,
            "открыт" if open else "закрыт",
        )
        await callbacks.on_valve_state_changed(open)

    async def report_initial_valve_state(*, source: str) -> None:
        if client is None or not client.is_connected:
            return
        state = valve_state_last_seen
        if state is None and valve_state_char is not None and supports_read(valve_state_char):
            try:
                data = await client.read_gatt_char(valve_state_uuid)
                state = parse_boolean_value(data)
                log.debug(
                    "valve_state начальное чтение: raw=%r parsed=%s",
                    bytes(data),
                    state,
                )
            except Exception as exc:
                log.warning("Не удалось прочитать valve_state: %s", exc)
        if state is not None:
            await apply_valve_state(state, source=source, force=True)

    async def apply_valve_control_state(active: bool, *, source: str) -> None:
        nonlocal valve_control_active, temp_ignored_logged, valve_open
        if active == valve_control_active:
            log.debug("valve_control (%s): без изменений (%s)", source, active)
            return
        valve_control_active = active
        temp_ignored_logged = False
        valve_open = None
        log.info(
            "Управление клапаном (%s): %s",
            source,
            "включено" if active else "выключено (контроль температуры)",
        )
        await callbacks.on_valve_control_changed(active)
        if active:
            await report_initial_valve_state(source="control_enable")

    def temperature_handler(_characteristic, data: bytearray) -> None:
        nonlocal temp_ignored_logged
        if valve_control_active:
            if not temp_ignored_logged:
                log.debug(
                    "Температура игнорируется (управление клапаном активно)"
                )
                temp_ignored_logged = True
            return
        temp_ignored_logged = False
        temp = parse_temperature(data)
        if temp is None:
            log.warning("Не удалось разобрать данные датчика: %r", data)
            return
        log.debug("Температура: %.2f °C (raw=%r)", temp, bytes(data))
        callbacks.on_temperature(temp)

    def valve_control_handler(_characteristic, data: bytearray) -> None:
        raw = bytes(data)
        active = parse_boolean_value(data)
        log.debug(
            "valve_control notify: raw=%r parsed=%s current=%s",
            raw,
            active,
            valve_control_active,
        )
        if active is None:
            log.warning("Не удалось разобрать состояние управления клапаном: %r", data)
            return
        asyncio.create_task(apply_valve_control_state(active, source="notify"))

    def valve_state_handler(_characteristic, data: bytearray) -> None:
        nonlocal valve_state_last_seen
        raw = bytes(data)
        open = parse_boolean_value(data)
        log.debug(
            "valve_state notify: raw=%r parsed=%s control_active=%s",
            raw,
            open,
            valve_control_active,
        )
        if open is None:
            log.warning("Не удалось разобрать состояние клапана: %r", data)
            return
        valve_state_last_seen = open
        if not valve_control_active:
            log.debug("valve_state notify: игнорируется (контроль выключен)")
            return
        asyncio.create_task(apply_valve_state(open, source="notify"))

    async def poll_valve_control() -> None:
        while not disconnected.is_set():
            if client is None or not client.is_connected:
                return
            try:
                data = await client.read_gatt_char(valve_control_uuid)
                active = parse_boolean_value(data)
                log.debug(
                    "valve_control poll: raw=%r parsed=%s current=%s",
                    bytes(data),
                    active,
                    valve_control_active,
                )
                if active is not None:
                    await apply_valve_control_state(active, source="poll")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("valve_control poll: %s", exc)
            await asyncio.sleep(VALVE_CONTROL_POLL_INTERVAL)

    try:
        log.debug("Сессия: подключение к %s", address)
        async with pairing_agent_session():
            client = await _connect_client(
                address,
                disconnected_callback=on_ble_disconnect,
                known_device=known_device,
                scan_timeout_sec=sensor.reconnect_scan_timeout_sec,
            )

        log.debug("Сессия: проверка характеристик GATT")
        log_sensor_gatt(client, sensor)
        _require_notify_characteristic(client, temp_uuid, "temperature_notify_uuid")
        valve_char = _require_characteristic(
            client, valve_control_uuid, "valve_control_notify_uuid"
        )
        valve_state_char = _require_notify_characteristic(
            client, valve_state_uuid, "valve_state_notify_uuid"
        )
        log.debug("Сессия: start_notify valve_state=%s", valve_state_uuid)
        await client.start_notify(valve_state_uuid, valve_state_handler)
        await asyncio.sleep(0.3)

        valve_props = characteristic_properties(valve_char)

        if supports_notify(valve_char):
            log.debug("Сессия: start_notify valve_control=%s", valve_control_uuid)
            await client.start_notify(valve_control_uuid, valve_control_handler)
        elif supports_read(valve_char):
            log.info(
                "valve_control %s — read/poll (flags=%s, notify недоступен)",
                valve_control_uuid,
                valve_props,
            )
            try:
                initial = await client.read_gatt_char(valve_control_uuid)
                active = parse_boolean_value(initial)
                log.info("valve_control начальное значение: raw=%r parsed=%s", bytes(initial), active)
                if active is not None:
                    await apply_valve_control_state(active, source="read")
            except Exception as exc:
                log.warning("Не удалось прочитать valve_control: %s", exc)
            valve_poll_task = asyncio.create_task(poll_valve_control())
        else:
            raise SensorError(
                f"Характеристика {valve_control_uuid} (valve_control_notify_uuid) "
                f"не поддерживает ни notify, ни read: {valve_props}"
            )

        log.debug("Сессия: start_notify temperature=%s", temp_uuid)
        await client.start_notify(temp_uuid, temperature_handler)
        log.info("Подписки на датчик активны")

        session_active = True
        await callbacks.on_connected()
        log.info("Сессия датчика активна")

        watch_task = asyncio.create_task(
            _watch_connection(
                client, disconnected, intentional_disconnect, disconnect_reason
            )
        )
        await disconnected.wait()
        log.info("Сессия: событие разрыва связи (%s)", disconnect_reason[0])
    finally:
        if valve_poll_task is not None:
            valve_poll_task.cancel()
            try:
                await valve_poll_task
            except asyncio.CancelledError:
                pass
        if watch_task is not None:
            watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass
        if client is not None:
            intentional_disconnect[0] = True
            log.debug(
                "Сессия: cleanup (connected=%s, reason=%s)",
                client.is_connected,
                disconnect_reason[0],
            )
            await disconnect_client(
                client, temp_uuid, valve_control_uuid, valve_state_uuid
            )
        if session_active:
            await callbacks.on_disconnected()
            log.info(
                "Сессия датчика завершена (%s)",
                disconnect_reason[0],
            )


async def _probe_for_sensor(
    address: str,
    interval_sec: float,
    scan_timeout_sec: float,
) -> BLEDevice | None:
    """Пауза (низкая нагрузка) и короткое сканирование. Повторяется в цикле ожидания."""
    log.info(
        "Ожидание датчика %s: пауза %s сек, затем сканирование %s сек…",
        address,
        int(interval_sec),
        int(scan_timeout_sec),
    )
    await asyncio.sleep(interval_sec)
    log.debug("Сканирование %s (timeout=%s сек)…", address, scan_timeout_sec)
    try:
        device = await _find_device_by_address(
            address, scan_timeout_sec, context="ожидание"
        )
    except BluetoothAdapterError:
        raise
    except Exception:
        log.exception("Ошибка сканирования %s в режиме ожидания", address)
        return None
    if device is None:
        log.info("Датчик %s не найден", address)
        return None
    log.info(
        "Датчик %s в эфире (name=%r, rssi=%s)",
        address,
        device.name,
        getattr(device, "rssi", "?"),
    )
    return device


async def run_sensor_loop(
    config: AppConfig,
    callbacks: SensorCallbacks,
    should_continue: Callable[[], bool],
) -> None:
    """Цикл: мониторинг → пауза и поиск → снова мониторинг (пока есть подписчики)."""
    if config.sensor is None:
        raise ConfigError(
            "В config.json нужна секция «sensor» (см. config.example.json)."
        )

    sensor = config.sensor
    address = await resolve_device_address(config)
    interval = sensor.reconnect_interval_sec
    scan_timeout = sensor.reconnect_scan_timeout_sec
    wait_before_next_attempt = False
    attempt = 0
    known_device: BLEDevice | None = None

    log.info(
        "Режим ожидания датчика: проверка каждые %s сек "
        "(сканирование %s сек), реакция на включение до ≈%s сек",
        int(interval),
        int(scan_timeout),
        int(interval + scan_timeout),
    )

    await check_bluetooth_adapter()

    try:
        while should_continue():
            attempt += 1
            log.debug(
                "BLE-цикл: попытка #%s (wait_before_connect=%s)",
                attempt,
                wait_before_next_attempt,
            )
            known_device = None
            if wait_before_next_attempt:
                log.debug("BLE-цикл: ожидание датчика перед подключением")
                while should_continue():
                    known_device = await _probe_for_sensor(
                        address, interval, scan_timeout
                    )
                    if known_device is not None:
                        break
                if not should_continue():
                    break

            try:
                log.debug("BLE-цикл: запуск сессии мониторинга")
                await _run_monitoring_session(
                    address,
                    sensor,
                    callbacks,
                    known_device=known_device,
                )
                log.debug("BLE-цикл: сессия завершена штатно")
            except asyncio.CancelledError:
                raise
            except BluetoothAdapterError:
                raise
            except BleakDeviceNotFoundError:
                log.warning(
                    "Датчик %s не найден при подключении (попытка #%s)",
                    address,
                    attempt,
                )
            except TimeoutError:
                log.warning(
                    "Датчик %s не ответил на подключение (попытка #%s)",
                    address,
                    attempt,
                )
            except (ConfigError, SensorError) as exc:
                log.error("Ошибка BLE-сессии (попытка #%s): %s", attempt, exc)
            except Exception:
                log.exception("Ошибка BLE-сессии (попытка #%s)", attempt)

            wait_before_next_attempt = True
    except asyncio.CancelledError:
        log.debug("BLE-цикл прерван")
        raise
    finally:
        log.info("BLE-цикл завершён (нет подписчиков или остановка)")


def _configure_standalone_logging(*, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)


async def _standalone_main(*, verbose: bool) -> None:
    _configure_standalone_logging(verbose=verbose)
    log.info(
        "Режим отладки BLE (без XMPP). Полное приложение: python main.py -v"
    )
    try:
        config = AppConfig.load()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    if config.sensor is None:
        raise SystemExit(
            "В config.json нужна секция «sensor» (см. config.example.json)."
        )

    async def noop_async() -> None:
        return None

    def on_temperature(value: float) -> None:
        print(f"Температура: {value:.2f} °C")

    async def on_valve_control_changed(active: bool) -> None:
        log.info(
            "Контроль клапана: %s",
            "включён (температура игнорируется)" if active else "выключен",
        )

    async def on_valve_state_changed(open: bool) -> None:
        log.info("Клапан: %s", "открыт" if open else "закрыт")

    callbacks = SensorCallbacks(
        on_connected=noop_async,
        on_disconnected=noop_async,
        on_temperature=on_temperature,
        on_valve_control_changed=on_valve_control_changed,
        on_valve_state_changed=on_valve_state_changed,
    )
    try:
        await run_sensor_loop(config, callbacks, lambda: True)
    except asyncio.CancelledError:
        pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Отладка BLE-датчика без XMPP. "
            "Для бота с XMPP используйте main.py."
        )
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="включить отладочные логи (DEBUG)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_standalone_main(verbose=args.verbose))
    except (ConfigError, SensorError) as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        print("\nЗавершение.")


if __name__ == "__main__":
    main()

"""Загрузка и сохранение config.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_OMEMO_STORE = "omemo_store.json"


class ConfigError(Exception):
    """Ошибка в config.json."""


@dataclass
class ThresholdRule:
    value: float
    message: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThresholdRule:
        message = data.get("message") or data.get("label")
        if not message:
            raise ValueError(
                "У каждого порога в config.json нужно поле «message» с текстом оповещения"
            )
        return cls(value=float(data["value"]), message=str(message).strip())


DEFAULT_HYSTERESIS = 0.2
DEFAULT_RECONNECT_INTERVAL_SEC = 600
DEFAULT_RECONNECT_SCAN_TIMEOUT_SEC = 15


@dataclass
class SensorMessages:
    connected: str
    disconnected: str
    valve_control_enabled: str
    valve_control_disabled: str
    valve_opened: str
    valve_closed: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SensorMessages:
        required = (
            "connected",
            "disconnected",
            "valve_control_enabled",
            "valve_control_disabled",
            "valve_opened",
            "valve_closed",
        )
        for key in required:
            if not data.get(key):
                raise ConfigError(
                    f"В config.json → sensor.messages нужно поле «{key}»"
                )
        return cls(
            connected=str(data["connected"]).strip(),
            disconnected=str(data["disconnected"]).strip(),
            valve_control_enabled=str(data["valve_control_enabled"]).strip(),
            valve_control_disabled=str(data["valve_control_disabled"]).strip(),
            valve_opened=str(data["valve_opened"]).strip(),
            valve_closed=str(data["valve_closed"]).strip(),
        )


@dataclass
class SensorConfig:
    temperature_notify_uuid: str
    valve_control_notify_uuid: str
    valve_state_notify_uuid: str
    messages: SensorMessages
    reconnect_interval_sec: float = DEFAULT_RECONNECT_INTERVAL_SEC
    reconnect_scan_timeout_sec: float = DEFAULT_RECONNECT_SCAN_TIMEOUT_SEC

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SensorConfig:
        if not data:
            raise ConfigError(
                "В config.json нужна секция «sensor» "
                "(temperature_notify_uuid, valve_control_notify_uuid, "
                "valve_state_notify_uuid, messages, …)"
            )
        temperature_notify_uuid = data.get("temperature_notify_uuid")
        if not temperature_notify_uuid or not str(temperature_notify_uuid).strip():
            raise ConfigError(
                "В config.json → sensor нужно поле «temperature_notify_uuid» "
                "(UUID GATT-характеристики уведомлений о температуре)"
            )
        valve_control_notify_uuid = data.get("valve_control_notify_uuid")
        if not valve_control_notify_uuid or not str(valve_control_notify_uuid).strip():
            raise ConfigError(
                "В config.json → sensor нужно поле «valve_control_notify_uuid» "
                "(UUID уведомления о включении/выключении управления клапаном)"
            )
        valve_state_notify_uuid = data.get("valve_state_notify_uuid")
        if not valve_state_notify_uuid or not str(valve_state_notify_uuid).strip():
            raise ConfigError(
                "В config.json → sensor нужно поле «valve_state_notify_uuid» "
                "(UUID уведомления об открытии/закрытии клапана)"
            )
        messages_raw = data.get("messages")
        if not isinstance(messages_raw, dict):
            raise ConfigError("В config.json → sensor.messages — объект с текстами")
        interval = float(
            data.get("reconnect_interval_sec", DEFAULT_RECONNECT_INTERVAL_SEC)
        )
        if interval <= 0:
            raise ConfigError("sensor.reconnect_interval_sec должен быть > 0")
        scan_timeout = float(
            data.get("reconnect_scan_timeout_sec", DEFAULT_RECONNECT_SCAN_TIMEOUT_SEC)
        )
        if scan_timeout <= 0:
            raise ConfigError("sensor.reconnect_scan_timeout_sec должен быть > 0")
        return cls(
            temperature_notify_uuid=str(temperature_notify_uuid).strip(),
            valve_control_notify_uuid=str(valve_control_notify_uuid).strip(),
            valve_state_notify_uuid=str(valve_state_notify_uuid).strip(),
            messages=SensorMessages.from_dict(messages_raw),
            reconnect_interval_sec=interval,
            reconnect_scan_timeout_sec=scan_timeout,
        )


@dataclass
class ThresholdsConfig:
    """
    high — срабатывание при повышении через порог; low — при понижении.
    hysteresis — на сколько °C уйти ниже/выше порога, чтобы можно было снова
    сработать (защита от дребезга датчика у границы).
    """

    high: list[ThresholdRule] = field(default_factory=list)
    low: list[ThresholdRule] = field(default_factory=list)
    hysteresis: float = DEFAULT_HYSTERESIS

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ThresholdsConfig:
        if not data:
            return cls()
        high = [ThresholdRule.from_dict(x) for x in data.get("high", [])]
        low = [ThresholdRule.from_dict(x) for x in data.get("low", [])]
        hysteresis = float(data.get("hysteresis", DEFAULT_HYSTERESIS))
        if hysteresis < 0:
            raise ValueError("thresholds.hysteresis не может быть отрицательным")
        return cls(high=high, low=low, hysteresis=hysteresis)


@dataclass
class XmppConfig:
    jid: str
    password: str
    omemo_store: str = DEFAULT_OMEMO_STORE

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> XmppConfig:
        return cls(
            jid=str(data["jid"]).strip(),
            password=str(data["password"]),
            omemo_store=str(data.get("omemo_store", DEFAULT_OMEMO_STORE)),
        )


@dataclass
class AppConfig:
    mac_address: str | None = None
    xmpp: XmppConfig | None = None
    sensor: SensorConfig | None = None
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    subscribers: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> AppConfig:
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: ожидается JSON-объект")

        mac = data.get("mac_address") or data.get("mac")
        mac = str(mac).strip() if mac else None

        xmpp_raw = data.get("xmpp")
        xmpp = XmppConfig.from_dict(xmpp_raw) if isinstance(xmpp_raw, dict) else None

        subscribers = [
            _normalize_jid(j)
            for j in data.get("subscribers", [])
            if j
        ]

        sensor_raw = data.get("sensor")
        sensor = (
            SensorConfig.from_dict(sensor_raw)
            if isinstance(sensor_raw, dict)
            else None
        )

        return cls(
            mac_address=mac or None,
            xmpp=xmpp,
            sensor=sensor,
            thresholds=ThresholdsConfig.from_dict(data.get("thresholds")),
            subscribers=subscribers,
        )

    def save(self, path: Path = CONFIG_PATH) -> None:
        data: dict[str, Any] = {}
        if self.mac_address:
            data["mac_address"] = self.mac_address
        if self.xmpp:
            data["xmpp"] = {
                "jid": self.xmpp.jid,
                "password": self.xmpp.password,
                "omemo_store": self.xmpp.omemo_store,
            }
        if self.sensor:
            data["sensor"] = {
                "temperature_notify_uuid": self.sensor.temperature_notify_uuid,
                "valve_control_notify_uuid": self.sensor.valve_control_notify_uuid,
                "valve_state_notify_uuid": self.sensor.valve_state_notify_uuid,
                "reconnect_interval_sec": self.sensor.reconnect_interval_sec,
                "reconnect_scan_timeout_sec": self.sensor.reconnect_scan_timeout_sec,
                "messages": {
                    "connected": self.sensor.messages.connected,
                    "disconnected": self.sensor.messages.disconnected,
                    "valve_control_enabled": self.sensor.messages.valve_control_enabled,
                    "valve_control_disabled": self.sensor.messages.valve_control_disabled,
                    "valve_opened": self.sensor.messages.valve_opened,
                    "valve_closed": self.sensor.messages.valve_closed,
                },
            }
        data["thresholds"] = {
            "hysteresis": self.thresholds.hysteresis,
            "high": [
                {"value": t.value, "message": t.message}
                for t in self.thresholds.high
            ],
            "low": [
                {"value": t.value, "message": t.message}
                for t in self.thresholds.low
            ],
        }
        data["subscribers"] = self.subscribers
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def add_subscriber(self, jid: str) -> bool:
        bare = _normalize_jid(jid)
        if bare in self.subscribers:
            return False
        self.subscribers.append(bare)
        self.save()
        return True

    def remove_subscriber(self, jid: str) -> bool:
        bare = _normalize_jid(jid)
        if bare not in self.subscribers:
            return False
        self.subscribers.remove(bare)
        self.save()
        return True


def _normalize_jid(jid: str) -> str:
    return str(jid).strip().lower().split("/")[0]

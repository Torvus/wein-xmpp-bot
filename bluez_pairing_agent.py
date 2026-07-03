"""BlueZ pairing agent: prompts for passkey/PIN shown on the device display."""

from __future__ import annotations

import concurrent.futures
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator, no_type_check

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

from bleak.backends.bluezdbus.utils import assert_reply, get_dbus_authenticator

BLUEZ_SERVICE = "org.bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
AGENT_MANAGER_PATH = "/org/bluez"
AGENT_PATH = "/org/bluez/wein/pairing_agent"
AGENT_CAPABILITY = "KeyboardDisplay"

_PROMPT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="wein-pairing-prompt"
)


def _prompt_passkey() -> int:
    print("\nНа дисплее устройства отображён код сопряжения.")
    while True:
        raw = input("Введите этот код (до 6 цифр): ").strip()
        if raw.isdigit() and 1 <= len(raw) <= 6:
            return int(raw)
        print("Код должен содержать только цифры (1–6 символов).")


def _prompt_pin() -> str:
    print("\nНа дисплее устройства отображён PIN.")
    while True:
        raw = input("Введите PIN (1–16 цифр): ").strip()
        if raw.isdigit() and 1 <= len(raw) <= 16:
            return raw
        print("PIN должен содержать только цифры (1–16 символов).")


def _prompt_confirmation(passkey: int) -> None:
    print(f"\nПроверьте код на дисплее устройства: {passkey:06d}")
    while True:
        raw = input("Код совпадает? [д/н]: ").strip().lower()
        if raw in ("д", "да", "y", "yes"):
            return
        if raw in ("н", "нет", "n", "no"):
            raise DBusError(
                "org.bluez.Error.Rejected", "Сопряжение отклонено пользователем"
            )
        print("Ответьте «д» (да) или «н» (нет).")


@no_type_check
class PairingAgent(ServiceInterface):
    def __init__(self) -> None:
        super().__init__(AGENT_INTERFACE)

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        print(f"\nЗапрос PIN для {device}")
        return _PROMPT_EXECUTOR.submit(_prompt_pin).result(timeout=300)

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        print(f"\nЗапрос кода для {device}")
        return _PROMPT_EXECUTOR.submit(_prompt_passkey).result(timeout=300)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        print(
            f"\nВведите на устройстве код {passkey:06d} "
            f"(на дисплее ПК; введено цифр: {entered}) [{device}]"
        )

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
        print(f"\nВведите на устройстве PIN {pincode} [{device}]")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        print(f"\nПодтверждение сопряжения для {device}")
        _PROMPT_EXECUTOR.submit(_prompt_confirmation, passkey).result(timeout=300)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        print(f"\nЗапрос авторизации для {device} — разрешено.")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        print(f"\nАвторизация сервиса {uuid} для {device} — разрешено.")

    @method()
    def Cancel(self):
        print("\nСопряжение отменено.")


class BlueZPairingAgentManager:
    def __init__(self) -> None:
        self._bus: MessageBus | None = None
        self._agent = PairingAgent()

    async def register(self) -> None:
        self._bus = MessageBus(bus_type=BusType.SYSTEM, auth=get_dbus_authenticator())
        await self._bus.connect()
        self._bus.export(AGENT_PATH, self._agent)

        for member, signature, body in (
            ("RegisterAgent", "os", [AGENT_PATH, AGENT_CAPABILITY]),
            ("RequestDefaultAgent", "o", [AGENT_PATH]),
        ):
            reply = await self._bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path=AGENT_MANAGER_PATH,
                    interface=AGENT_MANAGER_INTERFACE,
                    member=member,
                    signature=signature,
                    body=body,
                )
            )
            if reply.message_type == MessageType.ERROR:
                raise DBusError(reply.error_name or "unknown", reply.body[0])

    async def unregister(self) -> None:
        if self._bus is None:
            return
        try:
            reply = await self._bus.call(
                Message(
                    destination=BLUEZ_SERVICE,
                    path=AGENT_MANAGER_PATH,
                    interface=AGENT_MANAGER_INTERFACE,
                    member="UnregisterAgent",
                    signature="o",
                    body=[AGENT_PATH],
                )
            )
            if reply.message_type != MessageType.ERROR:
                assert_reply(reply)
        finally:
            self._bus.unexport(AGENT_PATH, self._agent)
            self._bus.disconnect()
            await self._bus.wait_for_disconnect()
            self._bus = None


@asynccontextmanager
async def pairing_agent_session() -> AsyncIterator[None]:
    if sys.platform != "linux":
        yield
        return

    manager = BlueZPairingAgentManager()
    await manager.register()
    try:
        yield
    finally:
        await manager.unregister()

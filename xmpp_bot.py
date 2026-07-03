"""XMPP-бот: OMEMO, подписки по «start», рассылка оповещений."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

import oldmemo
from slixmpp.clientxmpp import ClientXMPP
from slixmpp.jid import JID
from slixmpp.stanza import Message
from slixmpp.xmlstream.handler import CoroutineCallback
from slixmpp.xmlstream.matcher import MatchXPath

import omemo_plugin  # noqa: F401 — регистрация WeinOmemoPlugin
from app_config import AppConfig, XmppConfig
from ble_orchestrator import BleOrchestrator
from omemo_plugin import WeinOmemoPlugin

log = logging.getLogger(__name__)


class ThermometerBot(ClientXMPP):
    def __init__(
        self,
        xmpp_config: XmppConfig,
        app_config: AppConfig,
        ble_orchestrator: BleOrchestrator | None = None,
    ) -> None:
        super().__init__(xmpp_config.jid, xmpp_config.password)
        self.app_config = app_config
        self.ble_orchestrator = ble_orchestrator
        self._ready = asyncio.Event()

        self.add_event_handler("session_start", self._on_session_start)
        self.register_handler(
            CoroutineCallback(
                "IncomingMessages",
                MatchXPath(f"{{{self.default_ns}}}message"),
                self._on_message,
            )
        )

    @property
    def ready(self) -> asyncio.Event:
        return self._ready

    async def _on_session_start(self, _event) -> None:
        self.send_presence()
        await self.get_roster()
        await self["xep_0384"].get_session_manager()
        self._ready.set()
        log.info("XMPP-сессия и OMEMO готовы (%s)", self.boundjid.bare)

    async def _on_message(self, stanza: Message) -> None:
        if stanza["type"] not in ("chat", "normal"):
            return

        body = await self._message_body(stanza)
        if body is None:
            return

        sender = JID(stanza["from"]).bare
        if sender == self.boundjid.bare:
            return

        command = body.strip().lower()
        if command == "start":
            await self._handle_start(sender)
        elif command == "stop":
            await self._handle_stop(sender)

    async def _message_body(self, stanza: Message) -> str | None:
        xep_0384: WeinOmemoPlugin = self["xep_0384"]
        if xep_0384.is_encrypted(stanza):
            try:
                decrypted, _device = await xep_0384.decrypt_message(stanza)
                return decrypted.get("body") or None
            except Exception:
                log.warning("Не удалось расшифровать сообщение", exc_info=True)
                return None
        return stanza.get("body") or None

    async def _handle_start(self, jid: str) -> None:
        was_empty = not self.app_config.subscribers
        added = self.app_config.add_subscriber(jid)
        if added:
            text = (
                f"Вы подписаны на оповещения термометра ({jid}). "
                "Отправьте «stop», чтобы отписаться."
            )
            if was_empty and self.ble_orchestrator is not None:
                await self.ble_orchestrator.start()
        else:
            text = f"Вы уже в списке подписчиков ({jid})."
        await self.send_encrypted(JID(jid), text)

    async def _handle_stop(self, jid: str) -> None:
        removed = self.app_config.remove_subscriber(jid)
        if removed:
            text = f"Подписка отменена ({jid})."
            if not self.app_config.subscribers and self.ble_orchestrator is not None:
                await self.ble_orchestrator.stop()
        else:
            text = "Вы не были в списке подписчиков."
        await self.send_encrypted(JID(jid), text)

    async def send_encrypted(
        self,
        recipient: JID,
        text: str,
        mtype: Literal["chat", "normal"] = "chat",
    ) -> None:
        xep_0384: WeinOmemoPlugin = self["xep_0384"]
        msg = self.make_message(mto=recipient, mtype=mtype, mbody=text)
        msg.set_to(recipient)
        msg.set_from(self.boundjid)

        encrypted, errors = await xep_0384.encrypt_message(msg, {recipient})
        if errors:
            log.warning("OMEMO: некритичные ошибки шифрования: %s", errors)
        if encrypted is None:
            log.error("OMEMO: нечего отправить %s", recipient)
            return

        encrypted["eme"]["namespace"] = oldmemo.oldmemo.NAMESPACE
        encrypted["eme"]["name"] = self["xep_0380"].mechanisms[oldmemo.oldmemo.NAMESPACE]
        encrypted.send()

    async def notify_subscribers(self, text: str) -> None:
        if not self.app_config.subscribers:
            log.debug("Нет подписчиков для оповещения")
            return
        for jid_str in list(self.app_config.subscribers):
            try:
                await self.send_encrypted(JID(jid_str), text)
            except Exception:
                log.exception("Не удалось отправить оповещение %s", jid_str)


def create_bot(
    app_config: AppConfig,
    ble_orchestrator: BleOrchestrator | None = None,
) -> ThermometerBot:
    if app_config.xmpp is None:
        raise SystemExit(
            "В config.json нужна секция «xmpp» с полями jid и password "
            "(см. config.example.json)."
        )

    store_path = Path(app_config.xmpp.omemo_store)
    if not store_path.is_absolute():
        store_path = Path(__file__).resolve().parent / store_path

    bot = ThermometerBot(app_config.xmpp, app_config, ble_orchestrator)
    bot.register_plugin("xep_0030")
    bot.register_plugin("xep_0060")
    bot.register_plugin("xep_0163")
    bot.register_plugin("xep_0199")
    bot.register_plugin("xep_0280")
    bot.register_plugin("xep_0334")
    bot.register_plugin("xep_0380")
    bot.register_plugin(
        "xep_0384",
        {"json_file_path": str(store_path)},
        module=omemo_plugin,
    )
    return bot

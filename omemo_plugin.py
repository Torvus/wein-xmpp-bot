"""OMEMO: хранилище ключей и плагин XEP-0384 для Slixmpp."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, FrozenSet, Optional

from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import DeviceInformation, JSONType
from slixmpp.plugins import register_plugin
from slixmpp_omemo import TrustLevel, XEP_0384

log = logging.getLogger(__name__)


class JsonFileStorage(Storage):
    """Хранение OMEMO-данных в одном JSON-файле (как в примере slixmpp-omemo)."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._data: dict[str, JSONType] = {}
        if path.is_file():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2), encoding="utf-8"
        )

    async def _load(self, key: str) -> Maybe[JSONType]:
        if key in self._data:
            return Just(self._data[key])
        return Nothing()

    async def _store(self, key: str, value: JSONType) -> None:
        self._data[key] = value
        self._persist()

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._persist()


class PluginCouldNotLoad(Exception):
    pass


class WeinOmemoPlugin(XEP_0384):
    default_config = {
        "fallback_message": "Сообщение зашифровано (OMEMO).",
        "json_file_path": None,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._storage: Storage | None = None

    def plugin_init(self) -> None:
        if not self.json_file_path:
            raise PluginCouldNotLoad("Не задан путь к OMEMO-хранилищу (json_file_path).")
        self._storage = JsonFileStorage(Path(self.json_file_path))
        super().plugin_init()

    @property
    def storage(self) -> Storage:
        assert self._storage is not None
        return self._storage

    @property
    def _btbv_enabled(self) -> bool:
        return True

    async def _devices_blindly_trusted(
        self,
        blindly_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str],
    ) -> None:
        log.info("OMEMO: устройства доверены автоматически (BTBV): %s", blindly_trusted)

    async def _prompt_manual_trust(
        self,
        manually_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str],
    ) -> None:
        session_manager = await self.get_session_manager()
        for device in manually_trusted:
            log.warning(
                "OMEMO: требуется ручное доверие устройству %s — доверяем автоматически",
                device,
            )
            await session_manager.set_trust(
                device.bare_jid,
                device.identity_key,
                TrustLevel.TRUSTED.value,
            )


register_plugin(WeinOmemoPlugin)

"""Config flow for Wein."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import DOMAIN, LOCAL_NAME_PREFIX


def _is_wein_device(info: BluetoothServiceInfoBleak) -> bool:
    name = info.name or ""
    return name.startswith(LOCAL_NAME_PREFIX)


def _title(info: BluetoothServiceInfoBleak) -> str:
    return info.name or info.address


class WeinConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wein."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _is_wein_device(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device."""
        assert self._discovery_info is not None
        if user_input is not None:
            return self._async_create(self._discovery_info)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _title(self._discovery_info)},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        current_ids = self._async_current_ids(include_ignore=True)

        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current_ids or not _is_wein_device(info):
                continue
            self._discovered[info.address] = info

        if user_input is not None:
            address = str(user_input[CONF_ADDRESS]).strip().upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            info = self._discovered.get(address)
            title = _title(info) if info is not None else address
            return self.async_create_entry(
                title=title,
                data={CONF_ADDRESS: address},
            )

        options = [
            SelectOptionDict(value=address, label=f"{_title(info)} ({address})")
            for address, info in self._discovered.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        custom_value=True,
                        mode="dropdown",
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _async_create(self, info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_title(info),
            data={CONF_ADDRESS: info.address},
        )

"""UI setup: the logger found over Bluetooth (its service UUID), then options."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from . import CONF_ADDRESS, CONF_LIVE_S, CONF_PREFIX, CONF_SCAN_S, CONF_WRITES, DEFAULT_LIVE_S, DEFAULT_SCAN_S, DOMAIN

SERVICE_UUID = "00000922-0000-1000-8000-00805f9b34fb"


class DeyeBluetoothConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._found: dict[str, str] = {}  # address -> name

    async def async_step_bluetooth(self, info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(info.address.upper())
        self._abort_if_unique_id_configured()
        self._found = {info.address.upper(): info.name or info.address}
        self.context["title_placeholders"] = {"name": info.name or info.address}
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            # not raising: a waiting discovery of this logger must not block adding it by hand
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Deye logger {self._found.get(address, address)}",
                                           data={CONF_ADDRESS: address, CONF_PREFIX: self._free_prefix()})
        if not self._found:
            current = self._async_current_ids()
            self._found = {
                i.address.upper(): i.name or i.address
                for i in async_discovered_service_info(self.hass, connectable=True)
                if SERVICE_UUID in i.service_uuids and i.address.upper() not in current
            }
        if not self._found:
            return self.async_abort(reason="no_devices_found")
        schema = vol.Schema({vol.Required(CONF_ADDRESS): vol.In({a: f"{n} ({a})" for a, n in self._found.items()})})
        return self.async_show_form(step_id="user", data_schema=schema)

    def _free_prefix(self) -> str:
        """deye for the first logger, deye_2 for the second, ...: entity ids stay stable."""
        used = {entry.data.get(CONF_PREFIX) for entry in self._async_current_entries(include_ignore=False)}
        return next(p for p in ("deye", *(f"deye_{n}" for n in range(2, 100))) if p not in used)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return DeyeBluetoothOptionsFlow()


class DeyeBluetoothOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_LIVE_S, default=options.get(CONF_LIVE_S, DEFAULT_LIVE_S)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=15)
                ),
                vol.Required(CONF_SCAN_S, default=options.get(CONF_SCAN_S, DEFAULT_SCAN_S)): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=600)
                ),
                vol.Required(CONF_WRITES, default=options.get(CONF_WRITES, False)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

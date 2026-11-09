"""UI setup: the logger found over Bluetooth (its service UUID), then options."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from . import CONF_ADDRESS, CONF_PREFIX, DOMAIN
from . import options as o

SERVICE_UUID = "00000922-0000-1000-8000-00805f9b34fb"


class DeyeBluetoothConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

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
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlowWithReload:
        return DeyeBluetoothOptionsFlow()


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class _IntBox(selector.NumberSelector):
    """A number input box, never a slider, that stores an int."""

    def __call__(self, data: Any) -> int:
        value = super().__call__(data)  # float, already checked against min/max
        if not value.is_integer():
            raise vol.Invalid("must be a whole number")
        return int(value)


def _validator(option: o.Option):
    if option.low is not None:
        # plain vol.Range serialises to a number selector with min+max, whose
        # default mode is "slider"; an explicit box keeps every field editable
        return _IntBox(
            selector.NumberSelectorConfig(
                min=option.low, max=option.high, step=1, mode=selector.NumberSelectorMode.BOX
            )
        )
    return bool if isinstance(option.default, bool) else str


def _schema(current: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(name): section(
                vol.Schema(
                    {
                        vol.Required(opt.key, default=current.get(opt.key, opt.default)): _validator(opt)
                        for opt in o.OPTIONS
                        if opt.section == name
                    }
                ),
                {"collapsed": collapsed},
            )
            for name, collapsed in o.SECTIONS.items()
        }
    )


def _errors(flat: Mapping[str, Any]) -> dict[str, str]:
    if not (_UUID.fullmatch(flat[o.WRITE_CHAR]) and _UUID.fullmatch(flat[o.NOTIFY_CHAR])):
        return {"base": "bad_uuid"}
    if flat[o.STALE_S] < flat[o.SCAN_S] or flat[o.STALE_S] < flat[o.LIVE_S] or flat[o.SETTINGS_STALE_S] < flat[o.SETTINGS_S]:
        return {"base": "stale_too_short"}  # values would go unavailable between two polls
    return {}


class DeyeBluetoothOptionsFlow(OptionsFlowWithReload):
    """Saving the options reloads the entry, so the link and the poll intervals take the new values.

    OptionsFlowWithReload does the reload; the entry has no update listener (Home Assistant refuses both), so a
    change of the title alone does not reload.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current: Mapping[str, Any] = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            current = {key: value for group in user_input.values() for key, value in group.items()}  # flat in the entry
            errors = _errors(current)
            if not errors:
                return self.async_create_entry(data=dict(current))
        return self.async_show_form(step_id="init", data_schema=_schema(current), errors=errors)

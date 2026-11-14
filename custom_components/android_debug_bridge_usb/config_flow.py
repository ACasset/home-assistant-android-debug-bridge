"""Set up a single USB ADB hub from the Home Assistant UI."""

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
import voluptuous as vol

from .const import CONF_SERIAL, DOMAIN, NAME


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure the local USB hub; phones are discovered after setup."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return OptionsFlow()

    async def async_step_user(self, user_input=None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))


class OptionsFlow(config_entries.OptionsFlow):
    """Choose the phone that actions target when they give no serial."""

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_SERIAL)
        # Every phone seen so far has a device, which also remembers its model.
        phones = {
            serial: device.model
            for device in dr.async_entries_for_config_entry(
                dr.async_get(self.hass), self.config_entry.entry_id
            )
            for domain, serial in device.identifiers
            if domain == DOMAIN
        }
        if current:
            phones.setdefault(current, None)
        if not phones:
            return self.async_abort(reason="no_phones")
        options = [
            SelectOptionDict(value=serial, label=f"{model} ({serial})" if model else serial)
            for serial, model in sorted(phones.items())
        ]
        schema = vol.Schema({
            vol.Optional(CONF_SERIAL, description={"suggested_value": current}):
                SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)

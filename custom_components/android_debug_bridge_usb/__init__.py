"""Home Assistant integration for USB-connected Android phones."""

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .bridge import Bridge
from .const import DOMAIN
from .coordinator import UsbCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def _async_require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Mirror Home Assistant's admin-service check while retaining responses."""
    if call.context.user_id:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None:
            raise UnknownUser(context=call.context)
        if not user.is_admin:
            raise Unauthorized(context=call.context)


async def _async_hub(hass: HomeAssistant, call: ServiceCall) -> UsbCoordinator:
    """Authorize the call and return the hub, which may not be loaded."""
    await _async_require_admin(hass, call)
    coordinator = hass.data.get(DOMAIN)
    if coordinator is None:
        raise HomeAssistantError(
            "The USB ADB hub is not loaded. Check that Home Assistant can access "
            "USB devices and check the Home Assistant logs."
        )
    return coordinator


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions once, so they exist even while the hub cannot load."""

    async def async_shell(call: ServiceCall) -> dict:
        coordinator = await _async_hub(hass, call)
        serial = call.data["serial"]
        try:
            result = await hass.async_add_executor_job(
                coordinator.bridge.execute, serial, call.data["command"]
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except Exception as err:
            _LOGGER.exception("USB ADB command failed")
            raise HomeAssistantError(
                "ADB command failed. Check USB access, unlock the phone, approve "
                "USB debugging, and check the Home Assistant logs."
            ) from err
        coordinator.async_command_succeeded(serial)
        return {"output": result.output, "exit_code": result.exit_code}

    async def async_list_devices(call: ServiceCall) -> dict:
        coordinator = await _async_hub(hass, call)
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            raise HomeAssistantError("Cannot enumerate USB devices. Check Home Assistant logs.")
        return {"devices": list(coordinator.data.values())}

    async def async_send_sms(call: ServiceCall) -> dict:
        coordinator = await _async_hub(hass, call)
        serial = call.data["serial"]
        try:
            details = await hass.async_add_executor_job(
                coordinator.bridge.send_sms,
                serial,
                call.data["recipient"],
                call.data["message"],
                call.data.get("send_x"),
                call.data.get("send_y"),
                call.data["wait_seconds"],
                call.data["verify_ui"],
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except Exception as err:
            _LOGGER.exception("USB ADB SMS attempt failed; do not retry blindly")
            raise HomeAssistantError(
                "SMS attempt failed. Check the phone before retrying to avoid duplicates."
            ) from err
        coordinator.async_command_succeeded(serial)
        return {"status": "send_button_tapped", "delivery_confirmed": False, **details}

    hass.services.async_register(
        DOMAIN,
        "shell",
        async_shell,
        schema=vol.Schema({
            vol.Required("serial"): vol.All(cv.string, vol.Length(min=1)),
            vol.Required("command"): vol.All(
                cv.string, vol.Length(min=1, max=8192),
                vol.Match(r"[\s\S]*\S[\s\S]*"),
            ),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "list_devices",
        async_list_devices,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    coordinate = vol.All(vol.Coerce(int), vol.Range(min=0, max=10000))
    hass.services.async_register(
        DOMAIN,
        "send_sms",
        async_send_sms,
        schema=vol.Schema({
            vol.Required("serial"): vol.All(cv.string, vol.Length(min=1)),
            vol.Required("recipient"): vol.All(cv.string, vol.Match(r"^\+?[0-9]{3,15}$")),
            vol.Required("message"): vol.All(
                cv.string, vol.Length(min=1, max=1600),
                vol.Match(r"[\s\S]*\S[\s\S]*"),
            ),
            vol.Inclusive("send_x", "coordinates"): coordinate,
            vol.Inclusive("send_y", "coordinates"): coordinate,
            vol.Optional("wait_seconds", default=3): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=10),
            ),
            vol.Optional("verify_ui", default=True): cv.boolean,
            vol.Required("confirm_ui_risk"): vol.In([True]),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the hub and discover phones."""
    try:
        bridge = await hass.async_add_executor_job(
            Bridge, Path(hass.config.path(".storage", DOMAIN))
        )
    except Exception as err:
        raise ConfigEntryNotReady(f"Cannot initialize USB ADB identity: {err}") from err
    coordinator = UsbCoordinator(hass, bridge)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove entities when the hub is unloaded; actions then report it as unloaded."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data.pop(DOMAIN).async_shutdown_issues()
    return True

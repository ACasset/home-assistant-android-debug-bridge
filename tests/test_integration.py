"""Exercise config flows, discovery, actions and unload in Home Assistant."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant import loader
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import voluptuous as vol

from custom_components.android_debug_bridge_usb.const import DOMAIN

PHONE = {"serial": "phone", "port_path": [1, 2]}
SMS = {
    "serial": "phone", "recipient": "+123456789", "message": "Internet down",
    "send_x": 900, "send_y": 1800, "confirm_ui_risk": True,
}


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        Path(self.directory.name, "custom_components").symlink_to(
            Path(__file__).resolve().parents[1] / "custom_components",
            target_is_directory=True,
        )
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config.skip_pip = True
        loader.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.hass.auth = Mock()
        await async_load_base_functionality(self.hass)
        self.bridge = Mock()
        self.bridge.devices.return_value = [PHONE]
        self.bridge.shell.return_value = "Pixel\n"
        patcher = patch(
            "custom_components.android_debug_bridge_usb.Bridge",
            return_value=self.bridge,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    async def setup_hub(self):
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        self.assertEqual(result["type"], "form")
        result = await self.hass.config_entries.flow.async_configure(result["flow_id"], {})
        self.assertEqual(result["type"], "create_entry")
        await self.hass.async_block_till_done()
        self.entry = result["result"]
        return self.hass.data.get(DOMAIN)

    def phone_state(self, serial="phone"):
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{serial}_usb_connected"
        )
        return self.hass.states.get(entity_id)

    async def call(self, service, data=None, context=None):
        return await self.hass.services.async_call(
            DOMAIN, service, data or {}, blocking=True,
            return_response=True, context=context,
        )

    async def test_config_discovery_disconnect_reconnect_and_unload(self):
        coordinator = await self.setup_hub()
        self.assertEqual(self.entry.state, ConfigEntryState.LOADED)
        self.assertEqual(self.phone_state().state, "on")
        devices = dr.async_entries_for_config_entry(dr.async_get(self.hass), self.entry.entry_id)
        self.assertEqual(devices[0].identifiers, {(DOMAIN, "phone")})
        duplicate = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        self.assertEqual(duplicate["reason"], "single_instance_allowed")
        self.bridge.devices.return_value = []
        await coordinator.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(self.phone_state().state, "off")
        second = {"serial": "second", "port_path": [1, 3]}
        self.bridge.devices.return_value = [PHONE, second]
        self.assertEqual(
            await self.call("list_devices"), {"devices": [PHONE, second]}
        )
        await self.hass.async_block_till_done()
        self.assertEqual(self.phone_state().state, "on")
        self.assertEqual(self.phone_state("second").state, "on")
        self.assertTrue(await self.hass.config_entries.async_unload(self.entry.entry_id))
        self.assertNotIn(DOMAIN, self.hass.data)
        self.assertFalse(self.hass.services.has_service(DOMAIN, "shell"))
        self.assertFalse(self.hass.services.has_service(DOMAIN, "list_devices"))
        self.assertFalse(self.hass.services.has_service(DOMAIN, "send_sms"))
        # Previously discovered phones still get offline entities after reload.
        self.bridge.devices.return_value = []
        self.assertTrue(await self.hass.config_entries.async_setup(self.entry.entry_id))
        await self.hass.async_block_till_done()
        self.assertEqual(self.phone_state().state, "off")
        self.assertEqual(self.phone_state("second").state, "off")

    async def test_shell_response_validation_and_errors(self):
        await self.setup_hub()
        data = {"serial": "phone", "command": "getprop ro.product.model"}
        self.assertEqual(await self.call("shell", data), {"output": "Pixel\n"})
        self.bridge.shell.assert_called_once_with("phone", "getprop ro.product.model")
        for invalid in ("", " ", "x" * 8193):
            with self.subTest(command=invalid[:10]), self.assertRaises(vol.Invalid):
                await self.call("shell", {"serial": "phone", "command": invalid})
        self.bridge.shell.side_effect = ValueError("Device is missing")
        with self.assertRaisesRegex(ServiceValidationError, "Device is missing"):
            await self.call("shell", data)
        self.bridge.shell.side_effect = OSError("USB disconnected")
        with self.assertLogs("custom_components.android_debug_bridge_usb", level="ERROR"):
            with self.assertRaisesRegex(HomeAssistantError, "ADB command failed"):
                await self.call("shell", data)

    async def test_actions_require_admin_but_allow_automations(self):
        await self.setup_hub()
        for service, data in [
            ("shell", {"serial": "phone", "command": "id"}),
            ("list_devices", {}),
            ("send_sms", SMS),
        ]:
            with patch.object(self.hass.auth, "async_get_user", AsyncMock(return_value=None)):
                with self.assertRaises(UnknownUser):
                    await self.call(service, data, Context(user_id="missing"))
            with patch.object(
                self.hass.auth, "async_get_user", AsyncMock(return_value=Mock(is_admin=False))
            ):
                with self.assertRaises(Unauthorized):
                    await self.call(service, data, Context(user_id="user"))
            with patch.object(
                self.hass.auth, "async_get_user", AsyncMock(return_value=Mock(is_admin=True))
            ):
                self.assertIsNotNone(await self.call(service, data, Context(user_id="admin")))
            self.assertIsNotNone(await self.call(service, data))

    async def test_sms_response_and_validation(self):
        await self.setup_hub()
        self.assertEqual(await self.call("send_sms", SMS), {
            "status": "send_button_tapped", "delivery_confirmed": False,
        })
        self.bridge.send_sms.assert_called_once_with(
            "phone", "+123456789", "Internet down", 900, 1800, 3,
        )
        for changes in [
            {"confirm_ui_risk": False},
            {"recipient": "123;reboot"},
            {"send_x": -1},
            {"send_y": 10001},
            {"message": ""},
            {"wait_seconds": 11},
        ]:
            with self.subTest(changes=changes), self.assertRaises(vol.Invalid):
                await self.call("send_sms", SMS | changes)
        self.bridge.send_sms.side_effect = ValueError("Cannot open SMS composer")
        with self.assertRaisesRegex(ServiceValidationError, "Cannot open SMS composer"):
            await self.call("send_sms", SMS)
        self.bridge.send_sms.side_effect = OSError("Disconnected")
        with self.assertLogs("custom_components.android_debug_bridge_usb", level="ERROR"):
            with self.assertRaisesRegex(HomeAssistantError, "avoid duplicates"):
                await self.call("send_sms", SMS)

    async def test_enumeration_error_unavailable_and_recovery(self):
        coordinator = await self.setup_hub()
        self.bridge.devices.side_effect = OSError("USB denied")
        with self.assertLogs("custom_components.android_debug_bridge_usb", level="ERROR"):
            with self.assertRaises(HomeAssistantError):
                await self.call("list_devices")
        await self.hass.async_block_till_done()
        self.assertEqual(self.phone_state().state, "unavailable")
        self.bridge.devices.side_effect = None
        await coordinator.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(self.phone_state().state, "on")
        for invalid in ([PHONE, PHONE], [{"serial": "", "port_path": []}]):
            self.bridge.devices.return_value = invalid
            with self.assertRaises(HomeAssistantError):
                await self.call("list_devices")
            self.assertFalse(coordinator.last_update_success)

    async def test_initial_discovery_failure_retries_setup(self):
        self.bridge.devices.side_effect = OSError("USB denied")
        await self.setup_hub()
        self.assertEqual(self.entry.state, ConfigEntryState.SETUP_RETRY)
        self.assertFalse(self.hass.services.has_service(DOMAIN, "shell"))

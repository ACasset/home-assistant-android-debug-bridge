"""Tests for the USB boundary without connected hardware."""

import importlib.util
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock, patch

# Load the standalone USB boundary without importing Home Assistant.
spec = importlib.util.spec_from_file_location(
    "bridge",
    Path(__file__).resolve().parents[1]
    / "custom_components/android_debug_bridge_usb/bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bridge = bridge.Bridge(Path(self.directory.name))
        patcher = patch.object(self.bridge, "device_class")
        self.usb = patcher.start()
        self.addCleanup(patcher.stop)

    def test_identity_survives_restart_and_missing_public_key(self):
        key = Path(self.directory.name) / "adbkey"
        original = key.read_bytes()
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        key.with_suffix(".pub").unlink()
        bridge.Bridge(Path(self.directory.name))
        self.assertEqual(key.read_bytes(), original)
        self.assertTrue(key.with_suffix(".pub").exists())

    def test_enumeration(self):
        with patch.object(self.bridge.usb_transport, "find_all_adb_devices") as devices:
            devices.return_value = [Mock(serial_number="phone", port_path=[1, 2])]
            self.assertEqual(
                self.bridge.devices(), [{"serial": "phone", "port_path": [1, 2]}]
            )

    def test_invalid_requests_do_not_open_device(self):
        for serial, command in [
            ("", "id"), ("phone", ""), ("phone", "  "), ("phone", 1),
            (None, "id"), ("phone", "x" * 8193),
        ]:
            with self.subTest(serial=serial, command=command), self.assertRaises(ValueError):
                self.bridge.shell(serial, command)
        self.usb.assert_not_called()

    def test_missing_or_ambiguous_device_is_rejected(self):
        for devices in [[], [{"serial": "phone"}, {"serial": "phone"}]]:
            with patch.object(self.bridge, "_devices", return_value=devices):
                with self.assertRaises(ValueError):
                    self.bridge.shell("phone", "id")
        self.usb.assert_not_called()

    def test_shell_targets_selected_device_and_closes(self):
        usb = self.usb
        usb.return_value.shell.return_value = b"Pixel\n"
        with patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]):
            self.assertEqual(self.bridge.shell("phone", "getprop ro.product.model"), "Pixel\n")
        usb.assert_called_once_with(serial="phone", default_transport_timeout_s=5.0)
        usb.return_value.shell.assert_called_once_with(
            "getprop ro.product.model", read_timeout_s=30.0, timeout_s=60.0, decode=False
        )
        usb.return_value.close.assert_called_once()

    def test_failures_close_connection(self):
        usb = self.usb
        with patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]):
            usb.return_value.connect.return_value = False
            with self.assertRaises(ValueError):
                self.bridge.shell("phone", "id")
            usb.return_value.close.assert_called_once()
            usb.return_value.reset_mock()
            usb.return_value.connect.side_effect = RuntimeError("USB disconnected")
            with self.assertRaises(RuntimeError):
                self.bridge.shell("phone", "id")
            usb.return_value.close.assert_called_once()

    def test_output_limits_and_decoding(self):
        usb = self.usb
        with patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]):
            usb.return_value.shell.return_value = b"x" * bridge.MAX_OUTPUT
            self.assertEqual(len(self.bridge.shell("phone", "id")), bridge.MAX_OUTPUT)
            usb.return_value.shell.return_value = b"x" * (bridge.MAX_OUTPUT + 1)
            with self.assertRaises(ValueError):
                self.bridge.shell("phone", "id")
            usb.return_value.shell.return_value = b"\xff"
            self.assertEqual(self.bridge.shell("phone", "id"), "\ufffd")
        self.assertEqual(usb.return_value.close.call_count, 3)

    def test_usb_function_success_does_not_probe_or_change_output(self):
        device = self.usb.return_value
        device.shell.return_value = b"Permission denied\n"
        with (
            patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]),
            patch.object(bridge.time, "sleep") as sleep,
        ):
            self.assertEqual(
                self.bridge.shell("phone", "svc usb setFunctions rndis"),
                "Permission denied\n",
            )
        device.shell.assert_called_once()
        device.close.assert_called_once()
        sleep.assert_not_called()

    def test_usb_function_disconnect_checks_mode_without_replaying(self):
        for command, output in [
            ("svc usb setFunctions rndis", b"rndis\n"),
            ("svc usb setFunctions rndis", b"rndis,adb\n"),
            ("svc usb setFunctions none", b"none\n"),
            ("svc usb setFunctions none", b"adb\n"),
            (" svc  usb setFunctions none ", b""),
        ]:
            for error_class in bridge._TRANSPORT_ERRORS:
                with (
                    self.subTest(command=command, output=output, error=error_class),
                    patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]),
                    patch.object(bridge.time, "sleep") as sleep,
                    self.assertLogs("bridge", level="WARNING"),
                ):
                    self.usb.reset_mock()
                    device = self.usb.return_value
                    device.shell.side_effect = [
                        error_class("USB disconnected", OSError("disconnected")), output,
                    ]

                    def connect(**kwargs):
                        self.assertEqual(
                            device.close.call_count, device.connect.call_count - 1
                        )
                        return True

                    device.connect.side_effect = connect
                    self.assertEqual(self.bridge.shell("phone", command), "")
                    self.assertEqual(
                        [call.args[0] for call in device.shell.call_args_list],
                        [command, "svc usb getFunctions"],
                    )
                    self.assertEqual(device.close.call_count, 2)
                    sleep.assert_called_once_with(1)

    def test_usb_function_verification_waits_for_reenumeration_and_mode(self):
        device = self.usb.return_value
        device.shell.side_effect = [
            bridge.exceptions.UsbWriteFailedError("USB disconnected"),
            bridge.exceptions.UsbReadFailedError("USB not ready", OSError("not ready")),
            b"none\n",
            b"rndis,adb\n",
        ]
        with (
            patch.object(self.bridge, "_devices", side_effect=[
                [{"serial": "phone"}], [], [{"serial": "phone"}],
                [{"serial": "phone"}], [{"serial": "phone"}],
            ]),
            patch.object(bridge.time, "sleep") as sleep,
            self.assertLogs("bridge", level="WARNING"),
        ):
            self.assertEqual(self.bridge.shell("phone", "svc usb setFunctions rndis"), "")
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in device.shell.call_args_list],
            ["svc usb setFunctions rndis"] + ["svc usb getFunctions"] * 3,
        )
        self.assertEqual(device.close.call_count, 4)

    def test_usb_function_unconfirmed_outcome_is_not_success(self):
        for expected, output in [
            ("rndis", b"none\n"),
            ("none", b"rndis,adb\n"),
            ("none", b"Error: permission denied\n"),
            ("rndis", b"rndis,mtp\n"),
        ]:
            with (
                self.subTest(expected=expected, output=output),
                patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]),
                patch.object(bridge.time, "sleep") as sleep,
                self.assertLogs("bridge", level="WARNING"),
            ):
                self.usb.reset_mock()
                error = bridge.exceptions.UsbWriteFailedError("USB disconnected")
                self.usb.return_value.shell.side_effect = [error] + [output] * 5
                with self.assertRaisesRegex(ValueError, "may already have executed") as result:
                    self.bridge.shell("phone", f"svc usb setFunctions {expected}")
                self.assertIs(result.exception.__cause__, error)
                self.assertEqual(sleep.call_count, 5)
                self.assertEqual(self.usb.return_value.close.call_count, 6)

    def test_usb_function_phone_does_not_reconnect(self):
        error = bridge.exceptions.UsbReadFailedError(
            "USB disconnected", OSError("disconnected")
        )
        self.usb.return_value.shell.side_effect = error
        with (
            patch.object(self.bridge, "_devices", side_effect=[
                [{"serial": "phone"}], [], [], [], [], [],
            ]),
            patch.object(bridge.time, "sleep") as sleep,
            self.assertLogs("bridge", level="WARNING"),
            self.assertRaisesRegex(ValueError, "could not be confirmed"),
        ):
            self.bridge.shell("phone", "svc usb setFunctions none")
        self.assertEqual(sleep.call_count, 5)
        self.usb.return_value.shell.assert_called_once()
        self.usb.return_value.close.assert_called_once()

    def test_other_shell_commands_are_never_retried(self):
        for command in [
            "id", "svc usb getFunctions", "svc usb setFunctions mtp",
            "svc usb setFunctions rndis; input tap 1 2",
            "svc usb setFunctions none && echo done",
        ]:
            with (
                self.subTest(command=command),
                patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]),
                patch.object(bridge.time, "sleep") as sleep,
            ):
                self.usb.reset_mock()
                error = bridge.exceptions.UsbWriteFailedError("USB disconnected")
                self.usb.return_value.shell.side_effect = error
                with self.assertRaises(bridge.exceptions.UsbWriteFailedError):
                    self.bridge.shell("phone", command)
                self.usb.return_value.shell.assert_called_once()
                self.usb.return_value.close.assert_called_once()
                sleep.assert_not_called()

    def test_usb_function_connect_and_non_transport_errors_are_not_suppressed(self):
        for phase, error in [
            ("connect", bridge.exceptions.UsbWriteFailedError("Cannot connect")),
            ("shell", RuntimeError("Unexpected error")),
        ]:
            with (
                self.subTest(phase=phase),
                patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]),
                patch.object(bridge.time, "sleep") as sleep,
            ):
                self.usb.return_value.reset_mock(side_effect=True)
                getattr(self.usb.return_value, phase).side_effect = error
                with self.assertRaises(type(error)):
                    self.bridge.shell("phone", "svc usb setFunctions rndis")
                self.usb.return_value.close.assert_called_once()
                sleep.assert_not_called()

    def test_sms_quotes_message_and_orders_steps(self):
        message = "Internet down! 'hello'; $(touch /tmp/not-executed)\nPlease check."
        with (
            patch.object(self.bridge, "shell", side_effect=["", "Status: ok\n", ""]) as shell,
            patch.object(bridge.time, "sleep") as sleep,
        ):
            self.bridge.send_sms("phone", "+123456789", message, 900, 1800, 3)
        self.assertEqual(shell.call_args_list[0].args, ("phone", "input keyevent KEYCODE_WAKEUP"))
        args = shlex.split(shell.call_args_list[1].args[1])
        self.assertEqual(args, [
            "am", "start", "-W", "-a", "android.intent.action.SENDTO",
            "-d", "smsto:+123456789", "--es", "sms_body", message,
        ])
        self.assertEqual(shell.call_args_list[2].args, ("phone", "input tap 900 1800"))
        sleep.assert_called_once_with(3)

    def test_sms_failure_does_not_tap_or_retry(self):
        for responses in [
            ["Error: permission denied"],
            ["", "Error: no activity found"],
            ["", "Status: timeout"],
            ["", "Status: ok\n", "Error: invalid input"],
        ]:
            with (
                self.subTest(responses=responses),
                patch.object(self.bridge, "shell", side_effect=responses) as shell,
                patch.object(bridge.time, "sleep"),
            ):
                with self.assertRaises(ValueError):
                    self.bridge.send_sms("phone", "+123456789", "Outage", 900, 1800, 3)
                self.assertEqual(shell.call_count, len(responses))

    def test_sms_invalid_inputs_never_touch_phone(self):
        for overrides in [
            {"recipient": "123;reboot"},
            {"recipient": "12"},
            {"message": " "},
            {"message": "x" * 1601},
            {"message": "hello\0world"},
            {"send_x": -1},
            {"send_y": 10001},
            {"wait_seconds": 0},
            {"wait_seconds": 11},
        ]:
            data = dict(
                serial="phone", recipient="+123456789", message="Outage",
                send_x=900, send_y=1800, wait_seconds=3,
            )
            data.update(overrides)
            with self.subTest(overrides=overrides), patch.object(self.bridge, "shell") as shell:
                with self.assertRaises(ValueError):
                    self.bridge.send_sms(**data)
                shell.assert_not_called()

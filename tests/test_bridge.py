"""Tests for the USB boundary without connected hardware."""

import importlib.util
from pathlib import Path
import re
import shlex
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch
from xml.sax.saxutils import quoteattr

# Load the standalone USB boundary without importing Home Assistant.
spec = importlib.util.spec_from_file_location(
    "bridge",
    Path(__file__).resolve().parents[1]
    / "custom_components/android_debug_bridge_usb/bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

WRAPPED = re.compile(r'\(\n(.*)\n\)\necho "(__adb_usb_exit_[0-9a-f]{16}__:)\$\?"', re.DOTALL)
PHONE = {"serial": "phone", "port_path": [1, 2]}


def sent(call):
    """Return the user's command from the wrapper that reports its exit status."""
    return WRAPPED.fullmatch(call.args[0])[1]


class FakePhone:
    """Stand-in for Bridge.execute that models an SMS composer screen."""

    FIELD = (0, 1700, 800, 1900)
    SEND = (850, 1750, 1050, 1850)

    def __init__(
        self, draft="", send_id="com.sms:id/send_button", prefill=True, sends=True,
        awake=True, dump_ok=True, package="com.sms",
    ):
        self.text = draft
        self.send_id = send_id
        self.prefill = prefill
        self.sends = sends
        self.awake = awake
        self.dump_ok = dump_ok
        self.package = package
        self.commands = []
        self.taps = []
        self.sent = []

    def node(self, cls, bounds, text="", resource_id="", clickable=True):
        return (
            f"<node class={quoteattr(cls)} package={quoteattr(self.package)} "
            f"text={quoteattr(text)} resource-id={quoteattr(resource_id)} "
            f'clickable="{str(clickable).lower()}" enabled="true" '
            f'bounds="[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]" />'
        )

    def __call__(self, serial, command):
        self.commands.append(command)
        output, code = "", 0
        if command.startswith("am start"):
            if self.prefill and not self.text:
                self.text = shlex.split(command)[-1]
            output = "Starting: Intent\nStatus: ok\nActivity: com.sms/.Compose\n"
        elif command.startswith("rm -f /data/local/tmp/adb_usb_window.xml; uiautomator"):
            # Like the real tool, a failed dump prints nothing and still exits 0.
            if self.dump_ok:
                output = (
                    "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
                    '<hierarchy rotation="0">'
                    + self.node("android.widget.FrameLayout", (0, 0, 1080, 1920), clickable=False)
                    + self.node("android.widget.ImageButton", (0, 1600, 100, 1700))
                    + self.node("android.widget.EditText", self.FIELD, self.text)
                    + self.node("android.widget.ImageButton", self.SEND, resource_id=self.send_id)
                    + "</hierarchy>"
                )
        elif command.startswith("input tap"):
            x, y = (int(value) for value in command.split()[2:])
            self.taps.append((x, y))
            left, top, right, bottom = self.SEND
            if left <= x < right and top <= y < bottom and self.sends:
                self.sent.append(self.text)
                self.text = ""
        elif command == "input keyevent KEYCODE_WAKEUP":
            pass
        elif command.startswith("input keyevent"):
            self.text = self.text[: -command.split().count("67")]
        elif "mWakefulness" in command:
            output = f"  mWakefulness={'Awake' if self.awake else 'Asleep'}\n"
        else:
            raise AssertionError(f"Unexpected command: {command}")
        return bridge.ShellResult(output, code)


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
        unreadable = Mock(port_path=[1, 3])
        type(unreadable).serial_number = PropertyMock(side_effect=OSError("denied"))
        with patch.object(self.bridge.usb_transport, "find_all_adb_devices") as devices:
            devices.return_value = [
                unreadable, Mock(serial_number="phone", port_path=[1, 2]),
            ]
            self.assertEqual(self.bridge.devices(), [
                {"serial": None, "port_path": None},
                {"serial": "phone", "port_path": [1, 2]},
            ])

    def test_probe_reports_authorization_without_offering_the_key(self):
        device = self.usb.return_value
        device.shell.return_value = b"ok\r\nPixel 7\r\n"
        patcher = patch.object(self.bridge, "_devices", return_value=[PHONE])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(self.bridge.probe("phone"), "Pixel 7")
        self.usb.assert_called_once_with(port_path=[1, 2], default_transport_timeout_s=5.0)
        signer = device.connect.call_args.kwargs["rsa_keys"][0]
        self.assertEqual(signer.Sign(b"x" * 20), self.bridge.signer.Sign(b"x" * 20))
        # adb-shell asks for the public key only once the phone rejected the signature.
        device.connect.side_effect = lambda rsa_keys, **kwargs: rsa_keys[0].GetPublicKey()
        self.assertIsNone(self.bridge.probe("phone"))
        device.connect.side_effect = bridge.exceptions.UsbReadFailedError("gone", OSError())
        with self.assertRaises(bridge.exceptions.UsbReadFailedError):
            self.bridge.probe("phone")
        self.assertEqual(device.close.call_count, 3)

    def test_exit_code_is_reported_and_removed_from_output(self):
        device = self.usb.return_value
        for output, code, expected in [
            (b"", "0", ""), (b"not found\r\n", "127", "not found\r\n"), (b"no newline", "3", "no newline"),
        ]:
            device.shell.side_effect = lambda command, **kwargs: (
                output + WRAPPED.fullmatch(command)[2].encode() + code.encode() + b"\r\n"
            )
            with (
                self.subTest(code=code),
                patch.object(self.bridge, "_devices", return_value=[PHONE]),
            ):
                self.assertEqual(self.bridge.execute("phone", "id"), (expected, int(code)))
        # A guessed marker printed by the command itself is not trusted.
        device.shell.side_effect = None
        device.shell.return_value = b"__adb_usb_exit_0000000000000000__:0\n"
        with patch.object(self.bridge, "_devices", return_value=[PHONE]):
            self.assertEqual(
                self.bridge.execute("phone", "id"),
                ("__adb_usb_exit_0000000000000000__:0\n", None),
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
        other = {"serial": "other", "port_path": [1, 3]}
        for devices, message in [
            ([other], "No connected USB phone has the serial 'phone'.*'other'"),
            ([{"serial": "phone"}, {"serial": "phone"}], "2 connected USB phones"),
        ]:
            with (
                patch.object(self.bridge, "_devices", return_value=devices),
                patch.object(bridge.time, "sleep"),
                self.assertRaisesRegex(bridge.DeviceNotFoundError, message),
            ):
                self.bridge.shell("phone", "id")
        self.usb.assert_not_called()

    def test_phone_is_found_again_after_a_transient_identity_failure(self):
        self.usb.return_value.shell.return_value = b"Pixel\n"
        unreadable = Mock(port_path=[1, 2])
        type(unreadable).serial_number = PropertyMock(side_effect=OSError("busy"))
        readable = Mock(serial_number="phone", port_path=[1, 2])
        with (
            patch.object(self.bridge.usb_transport, "find_all_adb_devices") as found,
            patch.object(bridge.time, "sleep") as sleep,
        ):
            found.side_effect = [[unreadable], [], [readable]]
            self.assertEqual(self.bridge.shell("phone", "id"), "Pixel\n")
            self.assertEqual(sleep.call_count, 2)
            # A lasting failure names its cause instead of blaming the serial.
            found.side_effect = None
            found.return_value = [unreadable]
            with self.assertRaisesRegex(
                bridge.DeviceNotFoundError, "1 USB device.*OSError: busy"
            ):
                self.bridge.shell("phone", "id")

    def test_shell_targets_selected_device_and_closes(self):
        usb = self.usb
        usb.return_value.shell.return_value = b"Pixel\n"
        other = {"serial": "other", "port_path": [1, 3]}
        with patch.object(self.bridge, "_devices", return_value=[other, PHONE]):
            self.assertEqual(self.bridge.shell("phone", "getprop ro.product.model"), "Pixel\n")
        # Opened by port: selecting by serial would re-read every phone's serial.
        usb.assert_called_once_with(port_path=[1, 2], default_transport_timeout_s=5.0)
        usb.return_value.shell.assert_called_once()
        self.assertEqual(sent(usb.return_value.shell.call_args), "getprop ro.product.model")
        self.assertEqual(
            usb.return_value.shell.call_args.kwargs,
            {
                "transport_timeout_s": 30.0, "read_timeout_s": 30.0,
                "timeout_s": 60.0, "decode": False,
            },
        )
        usb.return_value.close.assert_called_once()

    def test_failures_close_connection(self):
        usb = self.usb
        with patch.object(self.bridge, "_devices", return_value=[PHONE]):
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
        with patch.object(self.bridge, "_devices", return_value=[PHONE]):
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
            patch.object(self.bridge, "_devices", return_value=[PHONE]),
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
                    patch.object(self.bridge, "_devices", return_value=[PHONE]),
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
                        [sent(call) for call in device.shell.call_args_list],
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
                [PHONE], [], [PHONE],
                [PHONE], [PHONE],
            ]),
            patch.object(bridge.time, "sleep") as sleep,
            self.assertLogs("bridge", level="WARNING"),
        ):
            self.assertEqual(self.bridge.shell("phone", "svc usb setFunctions rndis"), "")
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(
            [sent(call) for call in device.shell.call_args_list],
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
                patch.object(self.bridge, "_devices", return_value=[PHONE]),
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
            patch.object(self.bridge, "_devices", side_effect=[[PHONE]] + [[]] * 15),
            patch.object(bridge.time, "sleep") as sleep,
            self.assertLogs("bridge", level="WARNING"),
            self.assertRaisesRegex(ValueError, "could not be confirmed"),
        ):
            self.bridge.shell("phone", "svc usb setFunctions none")
        # Five checks, each looking for the phone three times.
        self.assertEqual(sleep.call_count, 15)
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
                patch.object(self.bridge, "_devices", return_value=[PHONE]),
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
                patch.object(self.bridge, "_devices", return_value=[PHONE]),
                patch.object(bridge.time, "sleep") as sleep,
            ):
                self.usb.return_value.reset_mock(side_effect=True)
                getattr(self.usb.return_value, phase).side_effect = error
                with self.assertRaises(type(error)):
                    self.bridge.shell("phone", "svc usb setFunctions rndis")
                self.usb.return_value.close.assert_called_once()
                sleep.assert_not_called()

    def send(self, phone, message="Outage", **overrides):
        with (
            patch.object(self.bridge, "execute", side_effect=phone),
            patch.object(bridge.time, "sleep"),
        ):
            return self.bridge.send_sms("phone", "+123456789", message, **overrides)

    def test_sms_quotes_message_and_taps_detected_button(self):
        message = "Panne internet ! 'hello'; $(touch /tmp/not-executed)\n<Vérifiez> & \"é\""
        phone = FakePhone()
        self.assertEqual(
            self.send(phone, message, wait_seconds=4),
            {"send_button": "detected", "composer_cleared": True},
        )
        self.assertEqual(phone.commands[0], "input keyevent KEYCODE_WAKEUP")
        self.assertEqual(shlex.split(phone.commands[2]), [
            "am", "start", "-W", "--activity-clear-task",
            "-a", "android.intent.action.SENDTO",
            "-d", "smsto:+123456789", "--es", "sms_body", message,
        ])
        self.assertEqual(phone.sent, [message])
        self.assertEqual(phone.taps, [(950, 1800)])

    def test_sms_clears_leftover_draft_before_sending(self):
        phone = FakePhone(draft="old unsent text")
        with self.assertLogs("bridge", level="WARNING"):
            result = self.send(phone)
        self.assertEqual(result["composer_cleared"], True)
        self.assertEqual(phone.sent, ["Outage"])
        keys = [c for c in phone.commands if c.startswith("input keyevent 67")]
        self.assertEqual(keys[0].split()[2:], ["67"] * 15 + ["112"] * 15)
        self.assertEqual(phone.taps[0], (400, 1800))

    def test_sms_coordinates_are_a_verified_fallback(self):
        self.assertEqual(
            self.send(FakePhone(send_id=""), send_x=900, send_y=1800),
            {"send_button": "coordinates", "composer_cleared": True},
        )
        for coordinates in [{}, {"send_x": 500, "send_y": 500}, {"send_x": 400, "send_y": 1800}]:
            phone = FakePhone(send_id="")
            with self.subTest(coordinates=coordinates), self.assertRaisesRegex(ValueError, "Nothing was sent"):
                self.send(phone, **coordinates)
            self.assertEqual(phone.taps, [])

    def test_sms_aborts_without_tapping_when_screen_is_not_as_expected(self):
        for phone in [
            FakePhone(prefill=False),
            FakePhone(dump_ok=False),
            FakePhone(awake=False),
            FakePhone(package="com.android.systemui"),
            FakePhone(draft="x" * 2001),
        ]:
            with self.subTest(phone=vars(phone)), self.assertRaisesRegex(ValueError, "Nothing was sent"):
                self.send(phone, send_x=900, send_y=1800)
            self.assertEqual(phone.taps, [])

    def test_sms_reports_message_left_in_composer(self):
        phone = FakePhone(sends=False)
        with self.assertRaisesRegex(ValueError, "still in the composer"):
            self.send(phone)
        self.assertEqual(len(phone.taps), 1)

    def test_sms_without_ui_verification_taps_coordinates_blindly(self):
        phone = FakePhone(prefill=False)
        self.assertEqual(
            self.send(phone, send_x=900, send_y=1800, verify_ui=False),
            {"send_button": "coordinates", "composer_cleared": None},
        )
        self.assertEqual(phone.taps, [(900, 1800)])
        self.assertFalse(any("uiautomator" in c for c in phone.commands))

    def test_sms_failure_does_not_tap_or_retry(self):
        for responses in [
            ["Error: permission denied"],
            ["", "", "Error: no activity found"],
            ["", "", "Status: timeout"],
        ]:
            results = [bridge.ShellResult(output, 0) for output in responses]
            with (
                self.subTest(responses=responses),
                patch.object(self.bridge, "execute", side_effect=results) as shell,
                patch.object(bridge.time, "sleep"),
            ):
                with self.assertRaises(ValueError):
                    self.bridge.send_sms("phone", "+123456789", "Outage", 900, 1800, 3)
                self.assertEqual(shell.call_count, len(responses))

    def test_sms_usb_failures_are_retried_only_when_nothing_can_be_sent(self):
        error = bridge.exceptions.UsbReadFailedError("timeout", OSError())

        def flaky(phone, match, failures):
            def execute(serial, command):
                if match in command and failures:
                    failures.pop()
                    raise error
                return phone(serial, command)
            return execute

        # Before the tap, each step is retried; persistent failure sends nothing.
        for match in ["KEYCODE_WAKEUP", "am start", "uiautomator"]:
            phone = FakePhone()
            with self.subTest(match=match), self.assertLogs("bridge", level="WARNING"):
                result = self.send(flaky(phone, match, [1, 1]))
            self.assertEqual(result["composer_cleared"], True)
            self.assertEqual(phone.sent, ["Outage"])
        phone = FakePhone()
        with (
            self.assertLogs("bridge", level="WARNING"),
            self.assertRaisesRegex(ValueError, "UsbReadFailedError.*Nothing was sent"),
        ):
            self.send(flaky(phone, "am start", [1] * 10))
        self.assertEqual(phone.taps, [])

        # The tap on Send is never repeated.
        phone = FakePhone()
        failures = [1]
        with self.assertRaisesRegex(ValueError, "may or may not have been sent"):
            self.send(flaky(phone, "input tap", failures))
        self.assertEqual((failures, phone.taps), ([], []))

        # A phone that is briefly off the bus is retried like a USB failure, and
        # one that is absent at the tap was never asked to send.
        phone = FakePhone()
        absent = [1, 1]

        def reenumerating(serial, command):
            if absent:
                absent.pop()
                raise bridge.DeviceNotFoundError("No connected USB phone")
            return phone(serial, command)

        with self.assertLogs("bridge", level="WARNING"):
            self.send(reenumerating)
        self.assertEqual(phone.sent, ["Outage"])
        phone = FakePhone()

        def unplugged_at_tap(serial, command):
            if command.startswith("input tap"):
                raise bridge.DeviceNotFoundError("No connected USB phone")
            return phone(serial, command)

        with self.assertRaisesRegex(ValueError, "No connected.*Nothing was sent"):
            self.send(unplugged_at_tap)

        # After the tap, an unreadable screen is an unconfirmed attempt, not an error.
        phone = FakePhone()

        def after_tap(serial, command):
            if phone.taps and "uiautomator" in command:
                raise error
            return phone(serial, command)

        with self.assertLogs("bridge", level="WARNING"):
            result = self.send(after_tap)
        self.assertEqual(result, {"send_button": "detected", "composer_cleared": None})
        self.assertEqual(phone.sent, ["Outage"])

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
            {"send_x": None},
            {"send_x": None, "send_y": None, "verify_ui": False},
        ]:
            data = dict(
                serial="phone", recipient="+123456789", message="Outage",
                send_x=900, send_y=1800, wait_seconds=3,
            )
            data.update(overrides)
            with self.subTest(overrides=overrides), patch.object(self.bridge, "execute") as shell:
                with self.assertRaises(ValueError):
                    self.bridge.send_sms(**data)
                shell.assert_not_called()

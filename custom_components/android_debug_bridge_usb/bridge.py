"""Serialized USB ADB operations, run outside Home Assistant's event loop."""

import logging
from pathlib import Path
import re
import secrets
import shlex
import threading
import time
from typing import NamedTuple, TypedDict
from xml.etree import ElementTree

from adb_shell import exceptions
from adb_shell.auth.keygen import keygen, write_public_keyfile
from adb_shell.auth.sign_pythonrsa import PythonRSASigner


MAX_OUTPUT = 1024 * 1024
_LOGGER = logging.getLogger(__name__)
_USB_FUNCTION_COMMAND = re.compile(r"\s*svc\s+usb\s+setFunctions\s+(none|rndis)\s*")
_TRANSPORT_ERRORS = (
    exceptions.UsbReadFailedError,
    exceptions.UsbWriteFailedError,
    exceptions.AdbConnectionError,
    exceptions.AdbTimeoutError,
)


_UI_DUMP_FILE = "/data/local/tmp/adb_usb_window.xml"
# uiautomator exits 0 even when it fails, so remove the file first: a failed dump
# must yield no hierarchy rather than the previous screen.
_UI_DUMP_COMMAND = (
    f"rm -f {_UI_DUMP_FILE}; uiautomator dump {_UI_DUMP_FILE} >/dev/null 2>&1; "
    f"cat {_UI_DUMP_FILE} 2>/dev/null; rm -f {_UI_DUMP_FILE}"
)
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_SYSTEM_PACKAGES = {"android", "com.android.systemui"}
_KEYCODE_DEL = 67
_KEYCODE_FORWARD_DEL = 112
_MAX_DRAFT = 2000
_NOTHING_SENT = "Nothing was sent."
# The USB transport fails a read after this long without data, whatever the
# overall read timeout, so it must cover slow silent commands such as
# `am start -W` or `uiautomator dump`.
_SHELL_TRANSPORT_TIMEOUT = 30.0
_SMS_STEP_ATTEMPTS = 3


class UsbDevice(TypedDict):
    """USB identity returned by discovery."""

    serial: str | None
    port_path: list[int] | None


class ShellResult(NamedTuple):
    """Command output and Android exit status (None when it was not reported)."""

    output: str
    exit_code: int | None


class _UiDumpError(ValueError):
    """The phone screen could not be read."""


class _UsbFailure(ValueError):
    """A repeatable SMS step kept failing to reach the phone."""


class _ProbeUnauthorized(Exception):
    """The phone does not trust this ADB identity."""


class _ProbeSigner:
    """Sign with the real key but never offer the public key.

    Offering the key is what opens Android's authorization dialog; a background
    probe must not make that dialog appear.
    """

    def __init__(self, signer) -> None:
        self._signer = signer

    def Sign(self, data):  # noqa: N802 - adb-shell signer interface
        return self._signer.Sign(data)

    def GetPublicKey(self):  # noqa: N802 - adb-shell signer interface
        raise _ProbeUnauthorized


def _bounds(node: dict) -> tuple[int, int, int, int] | None:
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    if match is None:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _is_text_field(node: dict) -> bool:
    return node.get("class", "").endswith("EditText")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


class Bridge:
    def __init__(self, data_dir: Path):
        # Importing USB transport initializes libusb; keep it in the executor.
        from adb_shell.adb_device import AdbDeviceUsb
        from adb_shell.transport.usb_transport import UsbTransport

        self.device_class = AdbDeviceUsb
        self.usb_transport = UsbTransport
        self.lock = threading.RLock()
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        key_path = data_dir / "adbkey"
        if not key_path.exists():
            keygen(str(key_path))
        key_path.chmod(0o600)
        if not key_path.with_suffix(".pub").exists():
            write_public_keyfile(str(key_path), str(key_path) + ".pub")
        self.signer = PythonRSASigner.FromRSAKeyPath(str(key_path))

    def devices(self) -> list[UsbDevice]:
        with self.lock:
            return self._devices()

    def _devices(self) -> list[UsbDevice]:
        devices: list[UsbDevice] = []
        for transport in self.usb_transport.find_all_adb_devices():
            # One unreadable phone must not hide the others.
            try:
                device = {
                    "serial": transport.serial_number,
                    "port_path": transport.port_path,
                }
            except Exception:  # noqa: BLE001 - libusb errors vary by host
                _LOGGER.debug("Cannot read a USB device identity", exc_info=True)
                device = {"serial": None, "port_path": None}
            devices.append(device)
        return devices

    def _open(self, serial: str):
        """Create a connection object for the one phone with this serial.

        Opening by port path avoids re-reading every phone's serial number, which
        would fail for all phones as soon as one of them is unreadable.
        """
        matches = [device for device in self._devices() if device["serial"] == serial]
        if len(matches) != 1:
            raise ValueError("Device is missing or its serial is ambiguous. Refresh devices.")
        return self.device_class(
            port_path=matches[0]["port_path"], default_transport_timeout_s=5.0
        )

    def probe(self, serial: str) -> bool:
        """Return whether the phone answers ADB, without prompting on the phone.

        False means the phone does not trust this identity; other failures raise.
        """
        with self.lock:
            device = self._open(serial)
            try:
                device.connect(
                    rsa_keys=[_ProbeSigner(self.signer)],
                    auth_timeout_s=5.0,
                    read_timeout_s=5.0,
                )
                output = device.shell(
                    "echo ok", read_timeout_s=5.0, timeout_s=10.0, decode=False
                )
            except _ProbeUnauthorized:
                return False
            finally:
                device.close()
        if output.strip() != b"ok":
            raise ValueError("Unexpected ADB probe response.")
        return True

    def shell(self, serial: str, command: str) -> str:
        return self.execute(serial, command).output

    def execute(self, serial: str, command: str) -> ShellResult:
        if not isinstance(serial, str) or not serial:
            raise ValueError("Select a USB device.")
        if not isinstance(command, str) or not command.strip() or len(command) > 8192:
            raise ValueError("Enter a shell command of 1–8192 characters.")
        # The legacy ADB shell service has no exit status, so run the command in
        # a subshell (which also captures `exit N`) and print its status after an
        # unguessable marker. The newlines keep comments and `&` self-contained.
        marker = f"__adb_usb_exit_{secrets.token_hex(8)}__:"
        wrapped = f'(\n{command}\n)\necho "{marker}$?"'
        with self.lock:
            device = self._open(serial)
            try:
                if not device.connect(
                    rsa_keys=[self.signer], auth_timeout_s=60.0, read_timeout_s=60.0
                ):
                    raise ValueError("ADB authorization failed. Unlock the phone and allow USB debugging.")
                try:
                    output = device.shell(
                        wrapped,
                        transport_timeout_s=_SHELL_TRANSPORT_TIMEOUT,
                        read_timeout_s=30.0,
                        timeout_s=60.0,
                        decode=False,
                    )
                except _TRANSPORT_ERRORS as err:
                    usb_function = _USB_FUNCTION_COMMAND.fullmatch(command)
                    if usb_function is None:
                        raise
                    disconnect_error = err
                else:
                    exit_code = None
                    status = re.search(
                        re.escape(marker.encode()) + rb"(\d{1,3})\s*\Z", output
                    )
                    if status is not None:
                        exit_code = int(status[1])
                        output = output[: status.start()]
                    if len(output) > MAX_OUTPUT:
                        raise ValueError("Command output exceeded 1 MiB. Use a smaller command.")
                    return ShellResult(output.decode("utf-8", errors="replace"), exit_code)
            finally:
                device.close()
            return ShellResult(
                self._verify_usb_functions(serial, usb_function[1], disconnect_error),
                None,
            )

    def _verify_usb_functions(
        self, serial: str, expected: str, disconnect_error: Exception
    ) -> str:
        """Check the requested mode after re-enumeration without replaying a write."""
        _LOGGER.warning(
            "USB disconnected during setFunctions %s; checking the resulting mode "
            "without resending the command",
            expected,
        )
        for _ in range(5):
            time.sleep(1)
            try:
                output = self.shell(serial, "svc usb getFunctions")
            except (*_TRANSPORT_ERRORS, exceptions.UsbDeviceNotFoundError, ValueError):
                _LOGGER.debug("USB mode verification could not connect", exc_info=True)
                continue
            functions = {part.strip() for part in output.strip().split(",")}
            functions.difference_update({"", "none", "adb"})
            if functions == ({"rndis"} if expected == "rndis" else set()):
                _LOGGER.info("USB mode %s confirmed after reconnection", expected)
                return ""
        raise ValueError(
            f"USB disconnected during setFunctions {expected}; the requested mode "
            "could not be confirmed. The command may already have executed. "
            "Check the phone and USB connection before retrying."
        ) from disconnect_error

    def send_sms(
        self,
        serial: str,
        recipient: str,
        message: str,
        send_x: int | None = None,
        send_y: int | None = None,
        wait_seconds: float = 3,
        verify_ui: bool = True,
    ) -> dict:
        """Attempt one UI send; a completed tap is not proof of SMS delivery."""
        if not re.fullmatch(r"\+?[0-9]{3,15}", recipient):
            raise ValueError("Use one phone number with 3-15 digits and an optional +.")
        if not message.strip() or len(message) > 1600 or "\0" in message:
            raise ValueError("Enter a message of 1-1600 characters without NUL bytes.")
        if (send_x is None) != (send_y is None):
            raise ValueError("Provide both Send coordinates or neither.")
        if send_x is None and not verify_ui:
            raise ValueError("Send coordinates are required when verify_ui is off.")
        if send_x is not None and (
            not 0 <= send_x <= 10000 or not 0 <= send_y <= 10000
        ):
            raise ValueError("Send coordinates must be between 0 and 10000.")
        if not 1 <= wait_seconds <= 10:
            raise ValueError("Wait time must be between 1 and 10 seconds.")
        # Recreate the task so an earlier dialog or screen of the app is discarded.
        intent = shlex.join([
            "am", "start", "-W", "--activity-clear-task",
            "-a", "android.intent.action.SENDTO",
            "-d", f"smsto:{recipient}", "--es", "sms_body", message,
        ])
        if len(intent) > 8192:
            raise ValueError("Encoded message exceeds the shell command limit.")
        # Hold the same lock across all steps so other actions cannot steal focus.
        with self.lock:
            try:
                wake_output = self._step(serial, "input keyevent KEYCODE_WAKEUP")
                if wake_output.strip():
                    raise ValueError(f"Cannot wake phone: {wake_output}")
                power = self._step(
                    serial,
                    "wm dismiss-keyguard >/dev/null 2>&1; "
                    "dumpsys power | grep mWakefulness=",
                )
                if "mWakefulness=" in power and "mWakefulness=Awake" not in power:
                    raise ValueError(
                        f"The phone screen did not turn on. {_NOTHING_SENT}"
                    )
                package = self._open_composer(serial, intent, wait_seconds)
                if verify_ui:
                    nodes = self._dump_ui(serial)
                    field = self._message_field(nodes, message)
                    if field is None:
                        self._clear_draft(serial, nodes, package)
                        self._open_composer(serial, intent, wait_seconds)
                        nodes = self._dump_ui(serial)
                        field = self._message_field(nodes, message)
                    if field is None:
                        raise ValueError(
                            "The SMS app is not showing the requested message "
                            "(wrong screen, dialog, or an app that ignores "
                            f"prefilled text). {_NOTHING_SENT}"
                        )
                    package = field.get("package", "")
                    x, y, source = self._send_target(nodes, package, send_x, send_y)
            except _UsbFailure as err:
                raise ValueError(f"{err} {_NOTHING_SENT}") from err
            if not verify_ui:
                self._tap_send(serial, send_x, send_y)
                return {"send_button": "coordinates", "composer_cleared": None}

            self._tap_send(serial, x, y)
            time.sleep(2)
            try:
                nodes = self._dump_ui(serial)
            except (_UiDumpError, _UsbFailure):
                _LOGGER.warning("Send was tapped but the screen could not be re-read")
                return {"send_button": source, "composer_cleared": None}
            if self._message_field(nodes, message, package) is not None:
                raise ValueError(
                    "Send was tapped but the message is still in the composer, so it "
                    "was probably not sent. Check the phone before retrying."
                )
            return {"send_button": source, "composer_cleared": True}

    def _step(self, serial: str, command: str) -> str:
        """Run a repeatable SMS step, retrying when the phone cannot be reached.

        Only for steps that cannot send anything: never for the tap on Send.
        """
        for attempt in range(_SMS_STEP_ATTEMPTS):
            if attempt:
                time.sleep(1)
            try:
                return self.execute(serial, command).output
            except (ValueError, exceptions.DeviceAuthError):
                raise
            except Exception as err:  # noqa: BLE001 - libusb errors vary by host
                error = err
                _LOGGER.warning(
                    "USB communication failed during an SMS step (attempt %d of %d): "
                    "%s: %s",
                    attempt + 1, _SMS_STEP_ATTEMPTS, type(err).__name__, err,
                )
        raise _UsbFailure(
            "USB communication with the phone failed "
            f"({type(error).__name__}: {error})."
        ) from error

    def _tap_send(self, serial: str, x: int, y: int) -> None:
        """Tap Send exactly once; a failure here leaves the outcome unknown."""
        try:
            self._tap(serial, x, y)
        except (ValueError, exceptions.DeviceAuthError):
            raise
        except Exception as err:  # noqa: BLE001 - libusb errors vary by host
            raise ValueError(
                "USB communication failed while tapping Send "
                f"({type(err).__name__}: {err}), so the SMS may or may not have "
                "been sent. Check the phone before retrying."
            ) from err

    def _open_composer(self, serial: str, intent: str, wait_seconds: float) -> str | None:
        """Open the prefilled composer and return the app package when reported."""
        output = self._step(serial, intent)
        if "Status: ok" not in output or "Error:" in output or "Exception" in output:
            raise ValueError(f"Cannot open SMS composer: {output}")
        time.sleep(wait_seconds)
        activity = re.search(r"^Activity: ([\w.]+)/", output, re.MULTILINE)
        return activity[1] if activity else None

    def _tap(self, serial: str, x: int, y: int, repeatable: bool = False) -> None:
        run = self._step if repeatable else self.shell
        output = run(serial, f"input tap {x} {y}")
        if output.strip():
            raise ValueError(f"Tap reported an error: {output}")

    def _dump_ui(self, serial: str) -> list[dict]:
        """Return the attributes of every node currently on screen."""
        for attempt in range(3):
            if attempt:
                # uiautomator fails transiently while the screen is animating.
                time.sleep(1)
            output = self._step(serial, _UI_DUMP_COMMAND)
            start = output.find("<hierarchy")
            end = output.rfind("</hierarchy>")
            if start < 0 or end < 0:
                continue
            try:
                root = ElementTree.fromstring(
                    output[start : end + len("</hierarchy>")]
                )
            except ElementTree.ParseError:
                continue
            return [dict(node.attrib) for node in root.iter("node")]
        raise _UiDumpError(
            f"Cannot read the phone screen (uiautomator dump failed). {_NOTHING_SENT}"
        )

    @staticmethod
    def _message_field(
        nodes: list[dict], message: str, package: str | None = None
    ) -> dict | None:
        """Find the text field holding exactly the requested message."""
        expected = _normalize(message)
        for node in nodes:
            if (
                _is_text_field(node)
                and (package is None or node.get("package") == package)
                and node.get("package") not in _SYSTEM_PACKAGES
                and _normalize(node.get("text", "")) == expected
            ):
                return node
        return None

    def _clear_draft(self, serial: str, nodes: list[dict], package: str | None) -> None:
        """Empty a leftover draft so the app can prefill the requested message."""
        fields = [
            node for node in nodes
            if _is_text_field(node)
            and node.get("package") not in _SYSTEM_PACKAGES
            and (package is None or node.get("package") == package)
        ]
        bounds = _bounds(fields[0]) if len(fields) == 1 else None
        if bounds is None:
            raise ValueError(
                "The SMS composer is not showing as expected (wrong screen, dialog, "
                f"lock screen or app chooser). {_NOTHING_SENT}"
            )
        length = len(fields[0].get("text", ""))
        if not length:
            return
        if length > _MAX_DRAFT:
            raise ValueError(
                f"The SMS composer holds an unexpectedly long draft. {_NOTHING_SENT}"
            )
        _LOGGER.warning(
            "SMS composer held %d unexpected characters; clearing them before retrying",
            length,
        )
        left, top, right, bottom = bounds
        self._tap(serial, (left + right) // 2, (top + bottom) // 2, repeatable=True)
        # The cursor position is unknown, so delete in both directions.
        keys = [_KEYCODE_DEL] * length + [_KEYCODE_FORWARD_DEL] * length
        for index in range(0, len(keys), 400):
            chunk = " ".join(str(key) for key in keys[index : index + 400])
            output = self._step(serial, f"input keyevent {chunk}")
            if output.strip():
                raise ValueError(f"Cannot clear the SMS composer: {output}")

    @staticmethod
    def _send_target(
        nodes: list[dict], package: str, send_x: int | None, send_y: int | None
    ) -> tuple[int, int, str]:
        """Choose where to tap: the app's Send button, else verified coordinates."""
        usable = [
            (node, bounds) for node in nodes
            if node.get("package") == package
            and node.get("enabled") == "true"
            and not _is_text_field(node)
            and (bounds := _bounds(node)) is not None
        ]
        detected = {
            bounds for node, bounds in usable
            if re.search(
                "send", node.get("resource-id", "").rpartition("/")[2], re.IGNORECASE
            )
        }
        if detected:
            left, top, right, bottom = min(
                detected, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])
            )
            # Nested nodes of one button are fine; separate matches are ambiguous.
            if all(
                b[0] <= left and b[1] <= top and b[2] >= right and b[3] >= bottom
                for b in detected
            ):
                return (left + right) // 2, (top + bottom) // 2, "detected"
        if send_x is None:
            raise ValueError(
                "Cannot identify the Send button of the SMS app. Provide send_x and "
                f"send_y. {_NOTHING_SENT}"
            )
        for node, (left, top, right, bottom) in usable:
            if (
                node.get("clickable") == "true"
                and left <= send_x < right
                and top <= send_y < bottom
            ):
                return send_x, send_y, "coordinates"
        raise ValueError(
            "The Send coordinates do not point at a button of the SMS app. "
            f"Recalibrate send_x and send_y. {_NOTHING_SENT}"
        )

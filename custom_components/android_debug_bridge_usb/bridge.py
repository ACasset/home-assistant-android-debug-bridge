"""Serialized USB ADB operations, run outside Home Assistant's event loop."""

from pathlib import Path
import re
import shlex
import threading
import time
from typing import TypedDict

from adb_shell.auth.keygen import keygen, write_public_keyfile
from adb_shell.auth.sign_pythonrsa import PythonRSASigner


MAX_OUTPUT = 1024 * 1024


class UsbDevice(TypedDict):
    """USB identity returned by discovery."""

    serial: str
    port_path: list[int]


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
        return [
            {"serial": transport.serial_number, "port_path": transport.port_path}
            for transport in self.usb_transport.find_all_adb_devices()
        ]

    def shell(self, serial: str, command: str) -> str:
        if not isinstance(serial, str) or not serial:
            raise ValueError("Select a USB device.")
        if not isinstance(command, str) or not command.strip() or len(command) > 8192:
            raise ValueError("Enter a shell command of 1–8192 characters.")
        with self.lock:
            matches = [device for device in self._devices() if device["serial"] == serial]
            if len(matches) != 1:
                raise ValueError("Device is missing or its serial is ambiguous. Refresh devices.")
            device = self.device_class(
                serial=serial, default_transport_timeout_s=5.0
            )
            try:
                if not device.connect(
                    rsa_keys=[self.signer], auth_timeout_s=60.0, read_timeout_s=60.0
                ):
                    raise ValueError("ADB authorization failed. Unlock the phone and allow USB debugging.")
                output = device.shell(
                    command, read_timeout_s=30.0, timeout_s=60.0, decode=False
                )
                if len(output) > MAX_OUTPUT:
                    raise ValueError("Command output exceeded 1 MiB. Use a smaller command.")
                return output.decode("utf-8", errors="replace")
            finally:
                device.close()

    def send_sms(
        self,
        serial: str,
        recipient: str,
        message: str,
        send_x: int,
        send_y: int,
        wait_seconds: float,
    ) -> None:
        """Attempt one UI send; a completed tap is not proof of SMS delivery."""
        if not re.fullmatch(r"\+?[0-9]{3,15}", recipient):
            raise ValueError("Use one phone number with 3-15 digits and an optional +.")
        if not message.strip() or len(message) > 1600 or "\0" in message:
            raise ValueError("Enter a message of 1-1600 characters without NUL bytes.")
        if not 0 <= send_x <= 10000 or not 0 <= send_y <= 10000:
            raise ValueError("Send coordinates must be between 0 and 10000.")
        if not 1 <= wait_seconds <= 10:
            raise ValueError("Wait time must be between 1 and 10 seconds.")
        intent = shlex.join([
            "am", "start", "-W", "-a", "android.intent.action.SENDTO",
            "-d", f"smsto:{recipient}", "--es", "sms_body", message,
        ])
        if len(intent) > 8192:
            raise ValueError("Encoded message exceeds the shell command limit.")
        # Hold the same lock across all steps so other actions cannot steal focus.
        with self.lock:
            wake_output = self.shell(serial, "input keyevent KEYCODE_WAKEUP")
            if wake_output.strip():
                raise ValueError(f"Cannot wake phone: {wake_output}")
            output = self.shell(serial, intent)
            if "Status: ok" not in output or "Error:" in output or "Exception" in output:
                raise ValueError(f"Cannot open SMS composer: {output}")
            time.sleep(wait_seconds)
            tap_output = self.shell(serial, f"input tap {send_x} {send_y}")
            if tap_output.strip():
                raise ValueError(f"Send tap reported an error: {tap_output}")

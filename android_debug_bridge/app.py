"""Ingress-only USB ADB shell for Home Assistant."""

import json
import logging
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from adb_shell.adb_device import AdbDeviceUsb
from adb_shell.auth.keygen import keygen, write_public_keyfile
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.transport.usb_transport import UsbTransport


INGRESS_ADDRESS = "172.30.32.2"
MAX_BODY = 16384
MAX_OUTPUT = 1024 * 1024


class Bridge:
    def __init__(self, data_dir=Path("/data")):
        self.lock = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path = data_dir / "adbkey"
        if not key_path.exists():
            keygen(str(key_path))
        key_path.chmod(0o600)
        if not key_path.with_suffix(".pub").exists():
            write_public_keyfile(str(key_path), str(key_path) + ".pub")
        self.signer = PythonRSASigner.FromRSAKeyPath(str(key_path))

    def devices(self):
        with self.lock:
            return self._devices()

    @staticmethod
    def _devices():
        return [
            {"serial": transport.serial_number, "port_path": transport.port_path}
            for transport in UsbTransport.find_all_adb_devices()
        ]

    def shell(self, serial, command):
        if not isinstance(serial, str) or not serial:
            raise ValueError("Select a USB device.")
        if not isinstance(command, str) or not command.strip() or len(command) > 8192:
            raise ValueError("Enter a shell command of 1–8192 characters.")
        with self.lock:
            matches = [device for device in self._devices() if device["serial"] == serial]
            if len(matches) != 1:
                raise ValueError("Device is missing or its serial is ambiguous. Refresh devices.")
            device = AdbDeviceUsb(
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


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="application/json"):
        if content_type == "application/json":
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        if self.client_address[0] != INGRESS_ADDRESS:
            self._send(403, {"error": "Use Home Assistant ingress to access this app."})
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        if self.path == "/":
            page = Path(__file__).with_name("index.html").read_text()
            self._send(
                200, page.replace("{{TOKEN}}", self.server.csrf_token).encode(),
                "text/html; charset=utf-8",
            )
        elif self.path == "/api/devices":
            try:
                self._send(200, {"devices": self.server.bridge.devices()})
            except Exception:
                logging.exception("USB enumeration failed")
                self._send(502, {"error": "Cannot enumerate USB devices. Check the app logs and USB access."})
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._allowed():
            return
        if self.path != "/api/shell":
            self._send(404, {"error": "Not found"})
            return
        if not secrets.compare_digest(
            self.headers.get("X-ADB-Token", ""), self.server.csrf_token
        ):
            self._send(403, {"error": "Invalid request token. Reload the page."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid request size.")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected application/json.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
            output = self.server.bridge.shell(payload.get("serial"), payload.get("command"))
            self._send(200, {"output": output})
        except (ValueError, UnicodeDecodeError) as error:
            self._send(400, {"error": str(error)})
        except Exception:
            logging.exception("ADB command failed")
            self._send(502, {
                "error": "ADB connection or command failed. Unlock the phone, approve USB "
                "debugging, and check the cable and app logs before retrying."
            })


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    server.bridge = Bridge()
    server.csrf_token = secrets.token_hex(32)
    logging.info("USB ADB ingress listening on port 8099")
    server.serve_forever()


if __name__ == "__main__":
    main()

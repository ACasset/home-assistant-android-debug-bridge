import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "android_debug_bridge"))
import app


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bridge = app.Bridge(Path(self.directory.name))

    def test_identity_survives_restart_and_missing_public_key(self):
        key = Path(self.directory.name) / "adbkey"
        original = key.read_bytes()
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        key.with_suffix(".pub").unlink()
        app.Bridge(Path(self.directory.name))
        self.assertEqual(key.read_bytes(), original)
        self.assertTrue(key.with_suffix(".pub").exists())

    @patch.object(app.UsbTransport, "find_all_adb_devices")
    def test_enumeration(self, devices):
        devices.return_value = [Mock(serial_number="phone", port_path=[1, 2])]
        self.assertEqual(
            self.bridge.devices(), [{"serial": "phone", "port_path": [1, 2]}]
        )

    @patch.object(app, "AdbDeviceUsb")
    def test_invalid_requests_do_not_open_device(self, usb):
        for serial, command in [("", "id"), ("phone", ""), ("phone", 1), (None, "id")]:
            with self.subTest(serial=serial, command=command), self.assertRaises(ValueError):
                self.bridge.shell(serial, command)
        usb.assert_not_called()

    @patch.object(app, "AdbDeviceUsb")
    def test_missing_or_ambiguous_device_is_rejected(self, usb):
        for devices in [[], [{"serial": "phone"}, {"serial": "phone"}]]:
            with patch.object(self.bridge, "_devices", return_value=devices):
                with self.assertRaises(ValueError):
                    self.bridge.shell("phone", "id")
        usb.assert_not_called()

    @patch.object(app, "AdbDeviceUsb")
    def test_shell_targets_selected_device_and_closes(self, usb):
        usb.return_value.shell.return_value = b"Pixel\n"
        with patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]):
            self.assertEqual(self.bridge.shell("phone", "getprop ro.product.model"), "Pixel\n")
        usb.assert_called_once_with(serial="phone", default_transport_timeout_s=5.0)
        usb.return_value.connect.assert_called_once()
        usb.return_value.shell.assert_called_once_with(
            "getprop ro.product.model", read_timeout_s=30.0, timeout_s=60.0, decode=False
        )
        usb.return_value.close.assert_called_once()

    @patch.object(app, "AdbDeviceUsb")
    def test_failures_close_connection(self, usb):
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

    @patch.object(app, "AdbDeviceUsb")
    def test_large_output_is_rejected_and_connection_closed(self, usb):
        usb.return_value.shell.return_value = b"x" * (app.MAX_OUTPUT + 1)
        with patch.object(self.bridge, "_devices", return_value=[{"serial": "phone"}]):
            with self.assertRaises(ValueError):
                self.bridge.shell("phone", "id")
        usb.return_value.close.assert_called_once()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.server.bridge = Mock()
        self.server.csrf_token = "test-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.read()

    def test_direct_access_denied_even_with_forwarded_header(self):
        status, _ = self.request("GET", "/", headers={"X-Forwarded-For": app.INGRESS_ADDRESS})
        self.assertEqual(status, 403)
        self.server.bridge.devices.assert_not_called()

    @patch.object(app, "INGRESS_ADDRESS", "127.0.0.1")
    def test_ingress_ui_and_devices(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'"X-ADB-Token": "test-token"', body)
        self.assertNotIn(b"{{TOKEN}}", body)
        self.server.bridge.devices.return_value = [{"serial": "phone", "port_path": [1]}]
        status, body = self.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["devices"][0]["serial"], "phone")

    @patch.object(app, "INGRESS_ADDRESS", "127.0.0.1")
    def test_post_requires_token(self):
        status, _ = self.request("POST", "/api/shell", "{}")
        self.assertEqual(status, 403)
        self.server.bridge.shell.assert_not_called()

    @patch.object(app, "INGRESS_ADDRESS", "127.0.0.1")
    def test_shell_request(self):
        self.server.bridge.shell.return_value = "Pixel\n"
        status, body = self.request(
            "POST", "/api/shell", json.dumps({"serial": "phone", "command": "id"}),
            {"Content-Type": "application/json", "X-ADB-Token": "test-token"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"output": "Pixel\n"})
        self.server.bridge.shell.assert_called_once_with("phone", "id")

    @patch.object(app, "INGRESS_ADDRESS", "127.0.0.1")
    def test_bad_payloads(self):
        for body in ["{", "[]", "null", "x" * (app.MAX_BODY + 1)]:
            with self.subTest(body=body[:10]):
                status, _ = self.request(
                    "POST", "/api/shell", body,
                    {"Content-Type": "application/json", "X-ADB-Token": "test-token"},
                )
                self.assertEqual(status, 400)
        self.server.bridge.shell.assert_not_called()

    @patch.object(app, "INGRESS_ADDRESS", "127.0.0.1")
    def test_usb_error_is_reported(self):
        self.server.bridge.shell.side_effect = RuntimeError("Disconnected")
        with self.assertLogs(level="ERROR"):
            status, body = self.request(
                "POST", "/api/shell", '{"serial":"phone","command":"id"}',
                {"Content-Type": "application/json", "X-ADB-Token": "test-token"},
            )
        self.assertEqual(status, 502)
        self.assertIn("ADB connection or command failed", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()

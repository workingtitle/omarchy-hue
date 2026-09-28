import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("huectl_under_test", PLUGIN_DIR / "huectl.py")
huectl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(huectl)


class FakeResponse:
    def __init__(self, body, content_length=None):
        self.body = body
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body


class FakeSocket:
    def __init__(self, peer_id):
        self.peer_id = peer_id

    def getpeercert(self):
        return {"subject": ((('commonName', self.peer_id),),)}


class FakeHttpResponse(FakeResponse):
    def __init__(self, body, status):
        super().__init__(body, len(body))
        self.status = status


class FakeConnection:
    def __init__(self, peer_id="001788FFFE4A8B17", body=None, status=200):
        self.sock = FakeSocket(peer_id)
        self.response = FakeHttpResponse(body or b'{"bridgeid":"001788FFFE4A8B17"}', status)
        self.requests = []
        self.closed = False

    def connect(self):
        return None

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class HueSecurityTests(unittest.TestCase):
    def test_bridge_tls_context_requires_verification_and_pinned_roots(self):
        context = huectl.bridge_tls_context()
        self.assertEqual(context.verify_mode, huectl.ssl.CERT_REQUIRED)
        self.assertFalse(context.check_hostname)
        self.assertGreaterEqual(len(context.get_ca_certs()), 2)

    def test_bounded_read_reads_one_extra_byte_before_rejecting(self):
        response = FakeResponse(b"12345")
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            huectl.bounded_read(response, 4)
        self.assertEqual(response.read_sizes, [5])

    def test_bounded_read_rejects_oversized_content_length_before_reading(self):
        response = FakeResponse(b"", huectl.MAX_JSON_BYTES + 1)
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            huectl.bounded_read(response, huectl.MAX_JSON_BYTES)
        self.assertEqual(response.read_sizes, [])

    def test_bounded_read_rejects_oversized_body_without_content_length(self):
        response = FakeResponse(b"12345")
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            huectl.bounded_read(response, 4)
        self.assertEqual(response.read_sizes, [5])

    def test_http_error_body_uses_small_error_limit(self):
        body = b"x" * (huectl.MAX_ERROR_BYTES + 1)
        connection = FakeConnection(body=body, status=500)
        with mock.patch.object(huectl.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                huectl.request_json("GET", "192.0.2.1", "/clip/v2/resource")
        self.assertEqual(connection.response.read_sizes, [])

    def test_emit_caps_helper_output(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as exit_info:
                huectl.emit({"data": "x" * (huectl.MAX_HELPER_OUTPUT_BYTES + 1)})
        self.assertEqual(exit_info.exception.code, 1)
        self.assertLessEqual(len(output.getvalue().encode()), 256)

    def test_request_uses_https_connection_and_sends_key_after_identity_check(self):
        connection = FakeConnection()
        with mock.patch.object(huectl.http.client, "HTTPSConnection", return_value=connection) as https:
            result = huectl.request_json(
                "GET", "192.0.2.1", "/clip/v2/resource", "valid-app-key", expected_bridge_id="001788FFFE4A8B17"
            )
        self.assertEqual(result["bridgeid"], "001788FFFE4A8B17")
        https.assert_called_once()
        self.assertEqual(connection.requests[0][0:2], ("GET", "/clip/v2/resource"))
        self.assertEqual(connection.requests[0][3]["hue-application-key"], "valid-app-key")

    def test_request_rejects_bridge_identity_mismatch_before_http_request(self):
        connection = FakeConnection(peer_id="001788FFFE4A8B17")
        with mock.patch.object(huectl.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                huectl.request_json(
                    "GET", "192.0.2.1", "/clip/v2/resource", "valid-app-key", expected_bridge_id="001788FFFE4A8B18"
                )
        self.assertEqual(connection.requests, [])

    def test_pairing_key_rejects_malformed_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "invalid application key"):
            huectl.pairing_key([{"success": {"username": "attacker\r\nX-Leak: true"}}])

    def test_pair_does_not_save_invalid_credential(self):
        saved = []
        exits = []

        def fake_emit(value, code=0):
            exits.append((value, code))
            raise SystemExit(code)

        with mock.patch.object(huectl, "bridge_identity", return_value="001788FFFE4A8B17"), \
             mock.patch.object(huectl, "request_json", return_value=[{"success": {"username": "bad\nkey"}}]), \
             mock.patch.object(huectl, "save_config", side_effect=lambda *args: saved.append(args)), \
             mock.patch.object(huectl, "emit", side_effect=fake_emit):
            with self.assertRaises(SystemExit):
                huectl.pair("192.0.2.1")
        self.assertEqual(saved, [])
        self.assertEqual(exits[0][0]["ok"], False)

    def test_light_rows_bound_resource_and_light_counts_and_names(self):
        too_many = {"data": [{}] * (huectl.MAX_RESOURCES + 1)}
        with self.assertRaisesRegex(RuntimeError, "too many resources"):
            huectl.light_rows(too_many)

        device_id = "device-1"
        light = {
            "type": "light",
            "id": "451da4d0-da01-4fb8-b0a4-74a5d6f20ea4",
            "owner": {"rid": device_id},
            "metadata": {"name": "x" * (huectl.MAX_LIGHT_NAME + 20)},
            "on": {"on": False},
            "dimming": {"brightness": 50},
        }
        rows = huectl.light_rows({"data": [{"type": "zigbee_connectivity", "owner": {"rid": device_id}, "status": "connected"}, light]})
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(rows[0]["name"]), huectl.MAX_LIGHT_NAME)

    def test_xy_to_hue_saturation_inverts_srgb_to_xy(self):
        # Same colors the panel sends: HSL at 50% lightness.
        for color, hue, saturation in (("#ff0000", 0, 100), ("#00ff00", 120, 100), ("#0000ff", 240, 100),
                                       ("#bf40bf", 300, 50), ("#40bfbf", 180, 50), ("#808080", 0, 0)):
            x_val, y_val = huectl.srgb_to_xy(color)
            result = huectl.xy_to_hue_saturation({"x": x_val, "y": y_val})
            self.assertAlmostEqual(result[0], hue, delta=1, msg=color)
            self.assertAlmostEqual(result[1], saturation, delta=1, msg=color)

    def test_xy_to_hue_saturation_rejects_malformed_points(self):
        for value in (None, {}, {"x": "a", "y": 0.3}, {"x": 0.3, "y": 0}, {"x": float("nan"), "y": 0.3}):
            self.assertIsNone(huectl.xy_to_hue_saturation(value))

    def test_qml_uses_plain_text_and_bounds_dynamic_sinks(self):
        panel = (PLUGIN_DIR / "Panel.qml").read_text()
        self.assertGreaterEqual(panel.count("textFormat: Text.PlainText"), 3)
        self.assertIn("maxHelperOutputLength", panel)
        self.assertIn("outputTooLarge", panel)
        self.assertIn("root.boundedText(data.error", panel)
        self.assertIn("root.boundedText(modelData.name", panel)
        self.assertIn("root.boundedText(detailView.light", panel)


if __name__ == "__main__":
    unittest.main()

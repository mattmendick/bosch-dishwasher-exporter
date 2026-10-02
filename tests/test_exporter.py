import json
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from prometheus_client import CollectorRegistry, generate_latest

from exporter import APIError, Client, Collector, PREFIX


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tokens.json"
        env = patch.dict(os.environ, {
            "HOME_CONNECT_CLIENT_ID": "test-client",
            "HOME_CONNECT_CLIENT_SECRET": "test-secret",
            "HOME_CONNECT_APPLIANCE_ID": "",
            "TOKEN_FILE": str(self.path),
        })
        env.start()
        self.addCleanup(env.stop)

    def client(self):
        client = Client()
        client.save({"access_token": "old", "refresh_token": "refresh-old", "expires_in": 86400})
        return client

    def poll(self, collector, state="Run", connected=True, remaining=120):
        collector.client.get.side_effect = [
            {"homeappliances": [{"haId": "dishwasher", "type": "Dishwasher", "connected": connected}]},
            {"status": [{"key": PREFIX + "Status.OperationState", "value": PREFIX + "EnumType.OperationState." + state}]},
            {"options": [{"key": PREFIX + "Option.RemainingProgramTime", "value": remaining}]},
        ]
        collector.poll()

    def test_running_to_idle_removes_finish_time(self):
        collector = Collector(Mock())
        with patch("exporter.time.time", return_value=1000):
            self.poll(collector)
        self.assertEqual(collector.snapshot[0]["finish"], 1120)
        self.poll(collector, state="Ready")
        self.assertNotIn("finish", collector.snapshot[0])
        self.assertFalse(collector.snapshot[0]["running"])

    def test_pause_disconnect_and_missing_value_are_unknown(self):
        collector = Collector(Mock())
        for state, connected, remaining in [("Pause", True, 120), ("Run", False, 120), ("Run", True, None)]:
            self.poll(collector, state, connected, remaining)
            self.assertNotIn("finish", collector.snapshot[0])
        self.assertEqual(collector.up, 1)

    def test_scrapes_do_not_call_api_and_failure_removes_samples(self):
        collector = Collector(Mock())
        self.poll(collector)
        calls = collector.client.get.call_count
        registry = CollectorRegistry()
        registry.register(collector)
        self.assertIn(b"bosch_dishwasher_remaining_seconds", generate_latest(registry))
        generate_latest(registry)
        self.assertEqual(collector.client.get.call_count, calls)
        last_success = collector.last_success
        collector.fail()
        output = generate_latest(registry)
        self.assertNotIn(b'appliance_id="dishwasher"', output)
        self.assertIn(b"bosch_dishwasher_exporter_up 0.0", output)
        self.assertEqual(collector.last_success, last_success)

    def test_no_appliance_is_failure(self):
        collector = Collector(Mock())
        collector.client.get.return_value = {"homeappliances": []}
        with self.assertRaises(ValueError):
            collector.poll()

    def test_cycle_finishes_between_status_and_program(self):
        collector = Collector(Mock())
        collector.client.get.side_effect = [
            {"homeappliances": [{"haId": "dishwasher", "type": "Dishwasher", "connected": True}]},
            {"status": [{"key": PREFIX + "Status.OperationState", "value": "Run"}]},
            APIError(404, "SDK.Error.NoProgramActive"),
        ]
        collector.poll()
        self.assertFalse(collector.snapshot[0]["running"])
        self.assertNotIn("finish", collector.snapshot[0])

    def test_refresh_rotates_tokens_and_persists_restrictively(self):
        client = self.client()
        client.request = Mock(return_value={"access_token": "new", "refresh_token": "refresh-new", "expires_in": 86400})
        client.refresh()
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "refresh-new")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Client().tokens["access_token"], "new")
        self.assertEqual(client.request.call_args.kwargs["form"]["client_secret"], "test-secret")

    def test_401_refreshes_and_retries_once(self):
        client = self.client()
        client.request = Mock(side_effect=[
            APIError(401, "expired"),
            {"access_token": "new", "refresh_token": "refresh-new", "expires_in": 86400},
            {"data": {"homeappliances": []}},
        ])
        self.assertEqual(client.get("/api/homeappliances"), {"homeappliances": []})
        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(client.request.call_args.kwargs["token"], "new")

    def test_device_pending_and_slow_down(self):
        client = Client()
        client.request = Mock(side_effect=[
            {"verification_uri": "https://example.com", "user_code": "code", "device_code": "device", "expires_in": 300, "interval": 5},
            APIError(400, "authorization_pending"), APIError(400, "slow_down"),
            {"access_token": "new", "refresh_token": "refresh", "expires_in": 86400},
        ])
        with patch("exporter.time.sleep") as sleep, patch("builtins.print"):
            client.authorize()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5, 10])
        self.assertEqual(client.request.call_args.kwargs["form"]["grant_type"], "device_code")
        self.assertTrue(self.path.exists())

    def test_denied_authorization_does_not_save(self):
        client = Client()
        client.request = Mock(side_effect=[
            {"verification_uri": "https://example.com", "user_code": "code", "device_code": "device", "expires_in": 300},
            APIError(400, "access_denied"),
        ])
        with patch("exporter.time.sleep"), patch("builtins.print"), self.assertRaises(APIError):
            client.authorize()
        self.assertFalse(self.path.exists())

    def test_rate_limit_logs_diagnostics_and_preserves_backoff(self):
        client = self.client()
        body = {"error": {"key": "429", "description": "Daily request limit reached"}}
        error = HTTPError("https://api.home-connect.com/api/homeappliances", 429,
                          "Too Many Requests", {"Retry-After": "3600"}, BytesIO(json.dumps(body).encode()))
        with patch("exporter.urlopen", side_effect=error), self.assertLogs("exporter", level="WARNING") as logs:
            with self.assertRaises(APIError) as raised:
                client.request("/api/homeappliances", token="old")
        self.assertEqual(raised.exception.retry_after, 3600)
        self.assertEqual(raised.exception.status, 429)
        message = logs.output[0]
        for expected in ["method=GET", "endpoint=/api/homeappliances", "status=429",
                         "error=429", "Daily request limit reached", "retry_after=3600"]:
            self.assertIn(expected, message)

    def test_oauth_error_redacts_credentials_and_newlines(self):
        client = self.client()
        body = {"error": "invalid_grant", "error_description":
                "test-secret refresh-old test-client device-value\nrejected"}
        error = HTTPError("https://api.home-connect.com/security/oauth/token",
                          400, "Bad Request", {}, BytesIO(json.dumps(body).encode()))
        with patch("exporter.urlopen", side_effect=error), self.assertLogs("exporter", level="WARNING") as logs:
            with self.assertRaises(APIError):
                client.request("/security/oauth/token", form={"device_code": "device-value"})
        message = logs.output[0]
        self.assertIn("error=invalid_grant", message)
        self.assertIn("[redacted]", message)
        for secret in ["test-secret", "refresh-old", "test-client", "device-value", "\n"]:
            self.assertNotIn(secret, message)

    def test_non_json_error_and_invalid_retry_after(self):
        client = self.client()
        error = HTTPError("https://api.home-connect.com/api/homeappliances", 503,
                          "Unavailable", {"Retry-After": "invalid"}, BytesIO(b"private proxy response"))
        with patch("exporter.urlopen", side_effect=error), self.assertLogs("exporter", level="WARNING") as logs:
            with self.assertRaises(APIError) as raised:
                client.request("/api/homeappliances")
        self.assertEqual(raised.exception.retry_after, 0)
        self.assertIn("status=503", logs.output[0])
        self.assertIn("No JSON error details", logs.output[0])
        self.assertNotIn("private proxy response", logs.output[0])


if __name__ == "__main__":
    unittest.main()

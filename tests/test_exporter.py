import json
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from prometheus_client import CollectorRegistry, generate_latest

from exporter import APIError, Client, Collector, PREFIX, ResyncRequired, monitor, sse_frames


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

    def test_saved_rate_limit_blocks_network_after_restart(self):
        client = self.client()
        error = HTTPError("https://api.home-connect.com/api/homeappliances/events", 429,
                          "Limited", {"Retry-After": "10828"}, BytesIO(b'{"error":{"key":"429"}}'))
        with patch("exporter.time.time", return_value=1000), patch("exporter.urlopen", side_effect=error):
            with self.assertLogs("exporter"), self.assertRaises(APIError):
                client.events()
        restarted = Client()
        with patch("exporter.time.time", return_value=1100), patch("exporter.urlopen") as network:
            with self.assertRaises(APIError) as error:
                restarted.events()
        self.assertEqual(error.exception.retry_after, 10728)
        network.assert_not_called()

    def test_sse_parser_handles_multiline_data_heartbeats_and_ids(self):
        stream = BytesIO(b': heartbeat\r\n\r\nevent: KEEP-ALIVE\n\n'
                         b'event: NOTIFY\nid: dishwasher\ndata: {"items":\ndata: []}\n\n')
        frames = sse_frames(stream, threading.Event())
        self.assertEqual(next(frames), ("KEEP-ALIVE", "", ""))
        self.assertEqual(next(frames), ("KEEP-ALIVE", "", ""))
        self.assertEqual(next(frames), ("NOTIFY", "dishwasher", '{"items":\n[]}'))
        with self.assertRaises(ConnectionError):
            next(frames)

    def event_collector(self):
        collector = Collector(Mock())
        with patch("exporter.time.time", return_value=1000):
            self.poll(collector)
        return collector

    def send(self, collector, key, value, timestamp=1010, kind="NOTIFY"):
        collector.event(kind, "dishwasher", json.dumps({"items": [
            {"key": PREFIX + key, "value": value, "timestamp": timestamp}]}))

    def test_event_timestamp_and_out_of_order_updates(self):
        collector = self.event_collector()
        self.send(collector, "Option.RemainingProgramTime", 60)
        self.assertEqual(collector.snapshot[0]["finish"], 1070)
        self.send(collector, "Option.RemainingProgramTime", 999, 1005)
        self.assertEqual(collector.snapshot[0]["finish"], 1070)
        self.assertEqual(collector.client.get.call_count, 3)

    def test_finish_pause_and_disconnect_remove_old_metrics(self):
        for key, value in [
            ("Status.OperationState", PREFIX + "EnumType.OperationState.Pause"),
            ("Event.ProgramFinished", PREFIX + "EnumType.EventPresentState.Present"),
            ("Event.ProgramAborted", PREFIX + "EnumType.EventPresentState.Present"),
        ]:
            collector = self.event_collector()
            self.send(collector, key, value)
            self.assertFalse(collector.snapshot[0]["running"])
            self.assertNotIn("finish", collector.snapshot[0])
            self.send(collector, "Option.RemainingProgramTime", 90, 1005)
            self.assertNotIn("finish", collector.snapshot[0])
        collector.event("DISCONNECTED", "dishwasher", "")
        self.assertFalse(collector.snapshot[0]["connected"])
        self.assertNotIn("running", collector.snapshot[0])
        with self.assertRaises(ResyncRequired):
            collector.event("CONNECTED", "dishwasher", "")

    def test_timing_before_run_is_retained_but_not_exposed_while_idle(self):
        collector = self.event_collector()
        self.send(collector, "Status.OperationState", PREFIX + "EnumType.OperationState.Ready", 1010)
        self.send(collector, "Option.RemainingProgramTime", 60, 1020)
        self.assertNotIn("finish", collector.snapshot[0])
        self.send(collector, "Status.OperationState", PREFIX + "EnumType.OperationState.Run", 1020)
        self.assertEqual(collector.snapshot[0]["finish"], 1080)

    def test_duplicate_connected_does_not_resync_and_depaired_removes_device(self):
        collector = self.event_collector()
        collector.event("CONNECTED", "dishwasher", "")
        collector.event("DEPAIRED", "dishwasher", "")
        self.assertEqual(collector.snapshot, [])

    def test_monitor_snapshot_after_open_and_eof_backoff(self):
        client = Mock()
        client.tokens = {"expires_at": float("inf")}
        response = BytesIO(b"event: KEEP-ALIVE\n\n")
        client.events.return_value = response
        collector = Mock()
        calls = []
        client.events.side_effect = lambda: (calls.append("open") or response)
        collector.poll.side_effect = lambda: calls.append("snapshot")
        stop = threading.Event()
        with patch.object(stop, "wait", side_effect=lambda delay: stop.set()) as wait:
            with self.assertLogs("exporter"):
                monitor(client, collector, stop)
        self.assertEqual(calls, ["open", "snapshot"])
        collector.event.assert_called_once_with("KEEP-ALIVE", "", "")
        collector.fail.assert_called_once()
        self.assertTrue(response.closed)
        wait.assert_called_once_with(60)

    def test_monitor_honors_retry_after_without_snapshot(self):
        client, collector = Mock(), Mock()
        client.events.side_effect = APIError(429, "429", 10828)
        stop = threading.Event()
        with patch.object(stop, "wait", side_effect=lambda delay: stop.set()) as wait:
            with self.assertLogs("exporter"):
                monitor(client, collector, stop)
        wait.assert_called_once_with(10828)
        collector.poll.assert_not_called()
        collector.fail.assert_called_once()

    def test_real_http_stream_and_snapshot_integration(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Accept")))
                if self.path.endswith("/events"):
                    body = (b'event: NOTIFY\nid: dishwasher\n'
                            b'data: {"items":[{"key":"BSH.Common.Option.RemainingProgramTime",'
                            b'"value":60,"timestamp":1010}]}\n\n')
                    content_type = "text/event-stream"
                else:
                    content_type = "application/json"
                    data = {
                        "/api/homeappliances": {"homeappliances": [
                            {"haId": "dishwasher", "type": "Dishwasher", "connected": True}]},
                        "/api/homeappliances/dishwasher/status": {"status": [
                            {"key": PREFIX + "Status.OperationState", "value": PREFIX + "EnumType.OperationState.Run"}]},
                        "/api/homeappliances/dishwasher/programs/active": {"options": [
                            {"key": PREFIX + "Option.RemainingProgramTime", "value": 120}]},
                    }[self.path]
                    body = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self.client()
            collector = Collector(client)
            with patch("exporter.BASE", f"http://127.0.0.1:{server.server_port}"), patch("exporter.time.time", return_value=1000):
                with client.events() as response:
                    collector.poll()
                    frames = sse_frames(response, threading.Event())
                    collector.event(*next(frames))
                    self.assertEqual(collector.snapshot[0]["finish"], 1070)
                    with self.assertRaises(ConnectionError):
                        next(frames)
            self.assertEqual(requests[0], ("/api/homeappliances/events", "text/event-stream"))
            self.assertEqual(len(requests), 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

"""Read-only Home Connect dishwasher exporter."""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

LOG = logging.getLogger(__name__)
BASE = "https://api.home-connect.com"
PREFIX = "BSH.Common."


class APIError(Exception):
    def __init__(self, status, key, retry_after=0):
        # Never include response bodies, request headers, or credentials in logs.
        super().__init__(f"Home Connect HTTP {status}")
        self.status = status
        self.key = key
        self.retry_after = retry_after


class Client:
    def __init__(self):
        self.client_id = os.environ["HOME_CONNECT_CLIENT_ID"]
        if not self.client_id.strip():
            raise ValueError("Set HOME_CONNECT_CLIENT_ID in .env before starting")
        self.secret = os.environ.get("HOME_CONNECT_CLIENT_SECRET", "")
        self.path = Path(os.environ.get("TOKEN_FILE", "data/tokens.json"))
        self.tokens = {}
        if self.path.exists():
            self.tokens = json.loads(self.path.read_text())

    def request(self, path, form=None, token=None, stream=False):
        cooldown = self.tokens.get("retry_not_before", 0) - time.time()
        if cooldown > 0:
            raise APIError(429, "saved_cooldown", cooldown)
        headers = {"Accept": "application/vnd.bsh.sdk.v1+json"}
        if stream:
            headers["Accept"] = "text/event-stream"
        body = None
        if form is not None:
            headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
            body = urlencode(form).encode()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = urlopen(Request(BASE + path, data=body, headers=headers), timeout=90 if stream else 30)
            if stream:
                if response.headers.get_content_type() != "text/event-stream":
                    response.close()
                    raise ValueError("Expected text/event-stream response")
                return response
            with response:
                return json.load(response)
        except HTTPError as exc:
            description = "No JSON error details returned"
            try:
                payload = json.loads(exc.read())
                error = payload.get("error", {})
                key = error if isinstance(error, str) else error.get("key", "unknown")
                description = (payload.get("error_description", "") if isinstance(error, str)
                               else error.get("description", ""))
            except (ValueError, AttributeError, TypeError):
                key = "unknown"
            finally:
                exc.close()
            retry_after = exc.headers.get("Retry-After", "")
            try:
                delay = float(retry_after or "0")
                if not math.isfinite(delay) or delay < 0:
                    delay = 0
            except ValueError:
                delay = 0
            # Log only selected diagnostics, never raw bodies or auth headers.
            # Redact credentials even if an upstream error echoes them back.
            secrets = [self.client_id, self.secret, token,
                       self.tokens.get("access_token"), self.tokens.get("refresh_token"),
                       self.tokens.get("id_token")]
            if form:
                secrets.extend(value for name, value in form.items()
                               if name in {"client_id", "client_secret", "device_code", "refresh_token"})

            def safe(value):
                text = str(value)
                for secret in secrets:
                    if secret:
                        text = text.replace(str(secret), "[redacted]")
                return " ".join(text.split())[:500]

            LOG.warning(
                "Home Connect request failed: method=%s endpoint=%s status=%s "
                "error=%s description=%s retry_after=%s",
                "POST" if form is not None else "GET", safe(path.split("?", 1)[0]),
                exc.code, safe(key), safe(description), safe(retry_after) or "not provided",
            )
            if exc.code == 429:
                # Survive redeploys without issuing more requests during a block.
                delay = max(delay, 60)
                self.tokens["retry_not_before"] = time.time() + delay
                self.persist(self.tokens)
            raise APIError(exc.code, key, delay) from None

    def save(self, result):
        tokens = {**self.tokens, **result}
        tokens["expires_at"] = time.time() + float(result["expires_in"])
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise ValueError("Token response missing required tokens")
        self.persist(tokens)
        self.tokens = tokens

    def persist(self, tokens):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(tokens, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def refresh(self):
        if not self.tokens.get("refresh_token"):
            raise ValueError("Run the authorize command first")
        form = {"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"],
                "client_id": self.client_id}
        if self.secret:
            form["client_secret"] = self.secret
        self.save(self.request("/security/oauth/token", form=form))

    def get(self, path):
        return self.authenticated_request(path)["data"]

    def authenticated_request(self, path, stream=False):
        if time.time() >= self.tokens.get("expires_at", 0) - 60:
            self.refresh()
        try:
            return self.request(path, token=self.tokens["access_token"], stream=stream)
        except APIError as exc:
            if exc.status != 401:
                raise
            self.refresh()
            return self.request(path, token=self.tokens["access_token"], stream=stream)

    def events(self):
        return self.authenticated_request("/api/homeappliances/events", stream=True)

    def authorize(self):
        result = self.request("/security/oauth/device_authorization", form={
            "client_id": self.client_id, "scope": "IdentifyAppliance Dishwasher-Monitor"})
        print(f"Open {result['verification_uri']} and enter code: {result['user_code']}", flush=True)
        deadline = time.monotonic() + float(result["expires_in"])
        interval = max(1, float(result.get("interval", 5)))
        while time.monotonic() + interval < deadline:
            time.sleep(interval)
            try:
                # Home Connect specifies grant_type=device_code (not the RFC URN).
                tokens = self.request("/security/oauth/token", form={
                    "grant_type": "device_code", "device_code": result["device_code"],
                    "client_id": self.client_id})
            except APIError as exc:
                if exc.key == "authorization_pending":
                    continue
                if exc.key == "slow_down":
                    interval = max(interval + 5, exc.retry_after)
                    continue
                raise
            self.save(tokens)
            print("Authorized. Tokens saved; you can now start the exporter.")
            return
        raise ValueError("Device authorization expired; run authorize again")


def sse_frames(response, stop):
    """Decode SSE frames, including comment heartbeats and multiline data.

    Home Connect's id is an appliance ID, not a resumable event cursor. Don't send
    Last-Event-ID on reconnect; obtain a fresh snapshot instead.
    """
    event, appliance_id, data = "", "", []
    size = 0
    while not stop.is_set():
        raw = response.readline(65537)
        if not raw:
            raise ConnectionError("Event stream closed")
        size += len(raw)
        if size > 1048576 or len(raw) > 65536:
            raise ValueError("Event stream frame too large")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if event or data:
                yield event or "message", appliance_id, "\n".join(data)
            event, appliance_id, data, size = "", "", [], 0
        elif line.startswith(":"):
            yield "KEEP-ALIVE", "", ""
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "id":
                appliance_id = value
            elif field == "data":
                data.append(value)


class ResyncRequired(Exception):
    """Device topology or connectivity changed; reload the snapshot."""


class Collector:
    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()
        self.snapshot = []
        self.up = 0
        self.last_success = 0
        self.stream_connected = 0
        self.appliance_id = os.environ.get("HOME_CONNECT_APPLIANCE_ID", "")

    def poll(self):
        rows = []
        appliances = self.client.get("/api/homeappliances")["homeappliances"]
        for appliance in appliances:
            if appliance["type"] != "Dishwasher":
                continue
            ha_id = appliance["haId"]
            if self.appliance_id and ha_id != self.appliance_id:
                continue
            row = {"id": ha_id, "connected": bool(appliance["connected"]), "_times": {}}
            if row["connected"]:
                path = "/api/homeappliances/" + quote(ha_id, safe="")
                state_time = int(time.time())
                status = {item["key"]: item["value"] for item in self.client.get(path + "/status")["status"]}
                state = status[PREFIX + "Status.OperationState"].rsplit(".", 1)[-1]
                row["running"] = state == "Run"
                row["_times"]["state"] = state_time
                row["_times"]["remaining"] = state_time
                if row["running"]:
                    try:
                        remaining_time = int(time.time())
                        program = self.client.get(path + "/programs/active")
                    except APIError as exc:
                        # A cycle may finish between reading status and program.
                        if exc.key != "SDK.Error.NoProgramActive":
                            raise
                        row["running"] = False
                    else:
                        options = {item["key"]: item["value"] for item in program.get("options", [])}
                        remaining = options.get(PREFIX + "Option.RemainingProgramTime")
                        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and math.isfinite(remaining) and remaining >= 0:
                            row["remaining"] = remaining
                            row["finish"] = time.time() + remaining
                            row["_times"]["remaining"] = remaining_time
            rows.append(row)
        if not rows:
            raise ValueError("No matching dishwasher found")
        with self.lock:
            self.snapshot = rows
            self.up = 1
            self.last_success = time.time()

    def stream_activity(self):
        with self.lock:
            self.stream_connected = 1
            self.up = 1
            self.last_success = time.time()

    def event(self, kind, appliance_id, data):
        if kind == "KEEP-ALIVE":
            self.stream_activity()
            return
        if self.appliance_id and appliance_id != self.appliance_id:
            return
        if kind == "PAIRED":
            raise ResyncRequired()
        with self.lock:
            # Copy-on-write: a concurrent scrape can safely read its old snapshot.
            rows = [{**row, "_times": dict(row["_times"])} for row in self.snapshot]
            row = next((row for row in rows if row["id"] == appliance_id), None)
            if row is None:
                return
            if kind == "DEPAIRED":
                rows.remove(row)
            elif kind == "DISCONNECTED":
                row.clear()
                row.update(id=appliance_id, connected=False, _times={})
            elif kind == "CONNECTED":
                # Duplicate CONNECTED frames must not cause a reconnect loop.
                if not row["connected"]:
                    raise ResyncRequired()
            elif kind in {"STATUS", "NOTIFY", "EVENT"}:
                payload = json.loads(data)
                items = payload["items"]
                if not isinstance(items, list):
                    raise ValueError("Invalid event items")
                if not row["connected"]:
                    return
                # Apply state before timing if they arrive in the same frame.
                items = sorted(items, key=lambda item: (
                    item.get("timestamp", time.time()),
                    item.get("key") == PREFIX + "Option.RemainingProgramTime"))
                for item in items:
                    key, value = item["key"], item.get("value")
                    timestamp = item.get("timestamp", time.time())
                    if not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp):
                        raise ValueError("Invalid event timestamp")
                    state_event = key == PREFIX + "Status.OperationState"
                    terminal = (key in {PREFIX + "Event.ProgramFinished", PREFIX + "Event.ProgramAborted"}
                                and value == PREFIX + "EnumType.EventPresentState.Present")
                    if state_event or terminal:
                        if timestamp < row["_times"].get("state", 0):
                            continue
                        row["_times"]["state"] = timestamp
                        row["running"] = state_event and value == PREFIX + "EnumType.OperationState.Run"
                        if not row["running"]:
                            row.pop("remaining", None)
                            row.pop("finish", None)
                            row.pop("_pending", None)
                            row["_times"]["remaining"] = max(timestamp, row["_times"].get("remaining", 0))
                        elif "_pending" in row:
                            pending_time, pending_value = row.pop("_pending")
                            if pending_time >= timestamp:
                                row["remaining"] = pending_value
                                row["finish"] = pending_time + pending_value
                    elif key == PREFIX + "Option.RemainingProgramTime":
                        if timestamp < row["_times"].get("remaining", 0):
                            continue
                        row["_times"]["remaining"] = timestamp
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                            if row.get("running"):
                                row["remaining"] = value
                                row["finish"] = timestamp + value
                            else:
                                # Timing and Run notifications can arrive separately.
                                row["_pending"] = (timestamp, value)
                        else:
                            row.pop("remaining", None)
                            row.pop("finish", None)
                            row.pop("_pending", None)
            self.snapshot = rows
            self.last_success = time.time()

    def fail(self):
        with self.lock:
            self.up = 0
            self.stream_connected = 0
            # Unknown is not zero: remove appliance samples on failed updates.
            self.snapshot = []

    def collect(self):
        with self.lock:
            rows, up, success, connected = self.snapshot, self.up, self.last_success, self.stream_connected
        yield GaugeMetricFamily("bosch_dishwasher_exporter_up", "Whether monitoring is healthy.", value=up)
        yield GaugeMetricFamily("bosch_dishwasher_event_stream_connected", "Whether the event stream is connected and initialized.", value=connected)
        yield GaugeMetricFamily("bosch_dishwasher_last_successful_update_timestamp_seconds", "Unix time of last snapshot, event, or heartbeat.", value=success)
        for suffix, field, description in [
            ("connected", "connected", "Whether the appliance is connected to Home Connect."),
            ("running", "running", "Whether the program is actively running (not paused or delayed)."),
            ("remaining_seconds", "remaining", "Remaining program seconds at last update; only while running."),
            ("estimated_finish_timestamp_seconds", "finish", "Estimated finish Unix timestamp; only while running."),
        ]:
            metric = GaugeMetricFamily("bosch_dishwasher_" + suffix, description, labels=["appliance_id"])
            for row in rows:
                if field in row:
                    metric.add_metric([row["id"]], float(row[field]))
            yield metric


def monitor(client, collector, stop):
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        delay = 60
        try:
            # Open first so events during the snapshot are buffered, not lost.
            with client.events() as response:
                collector.poll()
                collector.stream_activity()
                LOG.info("Event stream connected; initial dishwasher snapshot loaded")
                for kind, appliance_id, data in sse_frames(response, stop):
                    if stop.is_set():
                        break
                    collector.event(kind, appliance_id, data)
                    if time.time() >= client.tokens.get("expires_at", 0) - 60:
                        # Close before refreshing/reconnecting; never overlap streams.
                        break
        except Exception as exc:
            if time.monotonic() - started >= 300:
                failures = 0
            failures += 1
            delay = min(3600, 60 * 2 ** min(failures - 1, 6))
            if isinstance(exc, APIError):
                delay = max(delay, exc.retry_after)
            LOG.warning("Monitoring interrupted (%s); reconnecting in %.0fs", type(exc).__name__, delay)
        finally:
            collector.fail()
        stop.wait(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["authorize", "serve"], nargs="?", default="serve")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        client = Client()
        if args.command == "authorize":
            client.authorize()
            return
        if "POLL_INTERVAL_SECONDS" in os.environ:
            LOG.info("POLL_INTERVAL_SECONDS is ignored: monitoring now uses server-sent events")
        if not client.tokens.get("refresh_token"):
            raise ValueError("Run the authorize command first")
        collector = Collector(client)
        REGISTRY.register(collector)
        server, _ = start_http_server(int(os.environ.get("PORT", "9809")), addr="0.0.0.0")
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        worker = threading.Thread(target=monitor, args=(client, collector, stop), daemon=True)
        worker.start()
        stop.wait()
        server.shutdown()
        server.server_close()
        # Don't let a blocked network read delay Docker SIGTERM indefinitely.
        worker.join(timeout=1)
    except (KeyError, ValueError) as exc:
        parser.exit(1, f"Configuration error: {exc}\n")
    except Exception as exc:
        parser.exit(1, f"Operation failed ({type(exc).__name__}); check configuration and try again.\n")


if __name__ == "__main__":
    main()

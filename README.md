# Bosch dishwasher exporter

Read-only Python Prometheus exporter for Bosch dishwashers paired with Home Connect.
Runs in Docker on Linux. Uses the official cloud API; no access to your LAN appliance is needed.

## Setup

1. Register a **Device Flow** application at https://developer.home-connect.com/.
   Associate your real Home Connect account with the application's test user.
   Portal changes can take about 15 minutes to propagate.
2. Copy `.env.example` to `.env` and fill in your client ID and client secret.
   Both `.env` and local token storage are git-ignored and excluded from Docker builds.
3. Build and authorize:

   ```sh
   docker compose build
   docker compose run --rm exporter authorize
   ```

   Open the printed URL on any computer/phone and enter the code. Sign in with
   the Home Connect account paired to your dishwasher, not your developer account.
   Authorization requests only `IdentifyAppliance Dishwasher-Monitor`.
4. Start the exporter:

   ```sh
   docker compose up -d
   curl http://localhost:9809/metrics
   docker compose logs -f exporter
   ```

Tokens persist in the `tokens` named volume, are written atomically with mode 0600,
and refresh automatically. Don't run authorization and the exporter concurrently:
stop the exporter before reauthorizing. `docker compose down -v` deletes tokens.
The container runs as non-root UID 10001.

## Update and redeploy

Run from the project directory:

```sh
./update.sh
```

The script pulls the latest Git changes (fast-forward only), builds the image with
an updated base image, and recreates the service as needed. It preserves the
existing `tokens` volume, so you don't need to authorize again. It stops on errors
and leaves the running service in place if pulling or building fails. You can also
invoke the script by its full path from another directory.

To rebuild and deploy manually without pulling Git changes:

```sh
docker compose up -d --build
```

To only build the image, use `docker compose build`. These commands don't remove
the auth volume; don't use `docker compose down -v` during updates.

## Prometheus

```yaml
scrape_configs:
  - job_name: bosch-dishwasher
    scrape_interval: 30s
    static_configs:
      - targets: ['YOUR_LINUX_HOST:9809']
```

The HTTP metrics endpoint has no authentication; expose it to your monitoring network.
If Prometheus shares the Compose network, use `exporter:9809`.

| Metric | Meaning |
| --- | --- |
| `bosch_dishwasher_exporter_up` | 1 while monitoring is healthy, else 0 |
| `bosch_dishwasher_event_stream_connected` | 1 while the event stream is connected and initialized |
| `bosch_dishwasher_connected{appliance_id}` | Appliance connected to Home Connect |
| `bosch_dishwasher_running{appliance_id}` | Actively running; 0 when paused, delayed, or idle |
| `bosch_dishwasher_remaining_seconds{appliance_id}` | Remaining seconds reported at last snapshot/event |
| `bosch_dishwasher_estimated_finish_timestamp_seconds{appliance_id}` | Unix finish timestamp calculated from the latest remaining time |
| `bosch_dishwasher_last_successful_update_timestamp_seconds` | Unix timestamp of last snapshot, event, or heartbeat |

Remaining and finish metrics are omitted when idle, paused, disconnected, or not
reported by the device. Running is omitted when disconnected. Stream failures remove
appliance samples, set exporter_up to 0, and retain the last-success timestamp.
A disconnected appliance isn't an exporter failure if the event stream is healthy.
The complete snapshot is replaced atomically, including when a device is unpaired.

Scrapes read cached data, never call Home Connect. Monitoring uses **one persistent
server-sent events (SSE) connection** to `/api/homeappliances/events` for the account.
At startup and after reconnecting, it reads a snapshot: one discovery request,
one status request per connected dishwasher, and one program request per running
dishwasher. It opens the stream before fetching the snapshot to buffer changes
during initialization. Ordinary status/timing events don't trigger REST requests.

Opening the stream counts as one request; events and keep-alives don't consume
the request quota. A stable connection therefore uses only a handful of requests
per day for a single dishwasher, rather than hundreds. Network outages and device
reconnections add requests. The stream is renewed around token expiry; a read
timeout of 90 seconds detects missing heartbeats. Reconnection failures back off
from 60 seconds to one hour, resetting after a session lasts at least five minutes.
Numeric `Retry-After` values take precedence when longer. HTTP 429 cooldowns are
saved alongside tokens and honored after restarting the container.

Program completion, pause, disconnect, and unpair events clear obsolete metrics.
Appliance reconnection or pairing causes a fresh snapshot after backoff. Finish
estimates use the event timestamp plus remaining seconds, not the receipt time;
older per-field notifications are ignored. Estimates can still change mid-cycle.

### Upgrading from polling

Existing tokens, scopes, and the Docker volume work unchanged; no reauthorization
is required. `POLL_INTERVAL_SECONDS` is now ignored and can be removed from `.env`.
Deploy with `./update.sh` once the changes are available in your Git remote.
If already quota-blocked, the exporter must still wait for the existing block to
expire before establishing its first stream. The old polling version didn't save
cooldowns, so the first upgraded request may receive one more 429 and save its delay.

Unsuccessful HTTP requests log the method, endpoint, HTTP status, API error code,
API error description, and raw `Retry-After` value (or `not provided`). For example,
`status=429` indicates rate limiting; the following monitoring-failure log shows the actual
retry delay after backoff. OAuth failures are logged too. Credentials are redacted,
and raw response bodies and authorization headers aren't logged.

[Home Connect's rate limits](https://api-docs.home-connect.com/general/#rate-limiting)
include 1,000 requests per client/account per day, 50 per minute, and a 10-minute
block after 10 successive errors within 10 minutes. Failed requests and retries
count toward quotas. A 429 doesn't necessarily mean the daily quota was reached:
check its description and `Retry-After` in `docker compose logs -f exporter`.

For a countdown between events, use PromQL:

```promql
clamp_min(bosch_dishwasher_estimated_finish_timestamp_seconds - time(), 0)
```

For monitoring activity age (includes keep-alives, not just appliance changes):

```promql
time() - bosch_dishwasher_last_successful_update_timestamp_seconds
```

## Configuration

| Environment variable | Default |
| --- | --- |
| `HOME_CONNECT_CLIENT_ID` | Required |
| `HOME_CONNECT_CLIENT_SECRET` | Optional; used for refresh when provided |
| `HOME_CONNECT_APPLIANCE_ID` | All dishwashers |
| `TOKEN_FILE` | `data/tokens.json`, `/data/tokens.json` in Docker |
| `PORT` | 9809 (update Compose port mapping if changed) |

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Local runs read environment variables, not `.env` automatically. Docker Compose
loads `.env` using `env_file`.

References: [OAuth](https://api-docs.home-connect.com/authorization),
[API and limits](https://api-docs.home-connect.com/general),
[program-time fields](https://api-docs.home-connect.com/events).

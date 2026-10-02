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
| `bosch_dishwasher_exporter_up` | 1 if the latest API poll succeeded, else 0 |
| `bosch_dishwasher_connected{appliance_id}` | Appliance connected to Home Connect |
| `bosch_dishwasher_running{appliance_id}` | Actively running; 0 when paused, delayed, or idle |
| `bosch_dishwasher_remaining_seconds{appliance_id}` | Remaining seconds reported at last poll |
| `bosch_dishwasher_estimated_finish_timestamp_seconds{appliance_id}` | Unix finish timestamp calculated from the latest remaining time |
| `bosch_dishwasher_last_successful_update_timestamp_seconds` | Unix timestamp of last successful API poll |

Remaining and finish metrics are omitted when idle, paused, disconnected, or not
reported by the device. Running is omitted when disconnected. Failed polls remove
appliance samples, set exporter_up to 0, and retain the last-success timestamp.
A disconnected appliance is a successful API poll, not an exporter failure.
The complete snapshot is replaced atomically, including when a device is unpaired.

Scrapes read cached data, never call Home Connect. Polling defaults to **300 seconds**:
one discovery request plus one status request per connected dishwasher, and one
active-program request per running dishwasher. Thus a single continuously running
dishwasher uses about 864 REST requests/day, plus OAuth requests. Consider the API
quota before reducing the interval or monitoring multiple devices. Errors use
exponential backoff and honor numeric `Retry-After` values. This first version uses
polling rather than a persistent event stream; cycle changes can take up to one poll
interval to appear. Estimates may change during a cycle and aren't completion events.

Unsuccessful HTTP requests log the method, endpoint, HTTP status, API error code,
API error description, and raw `Retry-After` value (or `not provided`). For example,
`status=429` indicates rate limiting; the following poll-failure log shows the actual
retry delay after backoff. OAuth failures are logged too. Credentials are redacted,
and raw response bodies and authorization headers aren't logged.

[Home Connect's rate limits](https://api-docs.home-connect.com/general/#rate-limiting)
include 1,000 requests per client/account per day, 50 per minute, and a 10-minute
block after 10 successive errors within 10 minutes. Failed requests and retries
count toward quotas. A 429 doesn't necessarily mean the daily quota was reached:
check its description and `Retry-After` in `docker compose logs -f exporter`.

For a countdown between polls, use PromQL:

```promql
clamp_min(bosch_dishwasher_estimated_finish_timestamp_seconds - time(), 0)
```

For data age:

```promql
time() - bosch_dishwasher_last_successful_update_timestamp_seconds
```

## Configuration

| Environment variable | Default |
| --- | --- |
| `HOME_CONNECT_CLIENT_ID` | Required |
| `HOME_CONNECT_CLIENT_SECRET` | Optional; used for refresh when provided |
| `HOME_CONNECT_APPLIANCE_ID` | All dishwashers |
| `POLL_INTERVAL_SECONDS` | 300 (minimum 60) |
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

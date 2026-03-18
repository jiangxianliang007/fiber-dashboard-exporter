# fiber-dashboard-exporter

Prometheus exporter for the [Fiber Dashboard](https://github.com/cryptape/fiber-dashboard) backend.

Polls the `/health_check` and `/analysis_hourly` endpoints and exposes
heartbeat timestamps, total node counts, total channel counts, capacity,
liquidity, geographic distribution, channel state breakdown, and more as
Prometheus metrics — with **mainnet / testnet** breakdown.

## Quick Start

### Run directly

```bash
pip install -r requirements.txt

# Monitor both mainnet and testnet (default)
python exporter.py --target-url http://localhost:8080

# Monitor only mainnet
python exporter.py --target-url http://localhost:8080 --networks mainnet

# Monitor only testnet
python exporter.py --target-url http://localhost:8080 --networks testnet
```

Metrics are now available at `http://localhost:9101/metrics`.

### Run with Docker Compose

```bash
# 1. Copy the example env file and edit it
cp .env.example .env

# 2. Edit .env — at minimum set TARGET_URL to your Fiber Dashboard backend URL
#    e.g. TARGET_URL=http://your-fiber-dashboard-host:8080

# 3. Start the exporter
docker compose up -d

# 4. Check logs
docker compose logs -f
```

Metrics are available at `http://localhost:9101/metrics` (or the port you configured in `.env`).

## Metrics

### Health check (heartbeat liveness)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_heartbeat_timestamp_seconds` | Gauge | `task` | Unix timestamp of the last heartbeat |
| `fiber_dashboard_heartbeat_age_seconds` | Gauge | `task` | Seconds elapsed since the last heartbeat |
| `fiber_dashboard_heartbeat_healthy` | Gauge | `task` | `1` if fresh, `0` if stale |
| `fiber_dashboard_scrape_success` | Gauge | — | `1` if the last /health_check scrape succeeded |
| `fiber_dashboard_scrape_duration_seconds` | Gauge | — | Duration of the last /health_check scrape |

The `task` label takes one of:

- `timed_commit_states_heartbeat` — node/channel graph sync
- `daily_commit_task_heartbeat` — daily statistics aggregation
- `hourly_fresh_task_heartbeat` — materialized view refresh
- `channel_monitor_heartbeat` — on-chain channel state monitor

### Network statistics (nodes & channels)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_total_nodes` | Gauge | `network` | Total active nodes |
| `fiber_dashboard_total_channels` | Gauge | `network` | Total active channels |
| `fiber_dashboard_total_capacity` | Gauge | `network` | Total capacity of the network |
| `fiber_dashboard_total_liquidity` | Gauge | `network` | Total liquidity of the network |
| `fiber_dashboard_avg_channel_capacity` | Gauge | `network` | Average channel capacity (total_capacity / total_channels) |
| `fiber_dashboard_network_scrape_success` | Gauge | `network` | `1` if the last /analysis_hourly scrape succeeded |

The `network` label is `mainnet` or `testnet`.

### Geographic distribution

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_region_nodes` | Gauge | `network`, `region` | Number of nodes in each geographic region |

### Channel state & capacity distribution

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_channel_count_by_state` | Gauge | `network`, `asset`, `state` | Number of channels per asset and state (open, closed_cooperative, etc.) |
| `fiber_dashboard_channel_capacity_distribution` | Gauge | `network`, `asset`, `range` | Number of channels in each capacity range per asset |

### Pagination totals

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_nodes_total_from_page` | Gauge | `network` | Total nodes from /nodes_hourly pagination metadata |
| `fiber_dashboard_channels_total_from_page` | Gauge | `network` | Total channels from /channels_hourly pagination metadata |

### Per-endpoint deep scrape status

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_endpoint_scrape_success` | Gauge | `network`, `endpoint` | `1` if the last deep scrape succeeded, `0` otherwise |
| `fiber_dashboard_endpoint_scrape_duration_seconds` | Gauge | `network`, `endpoint` | Duration of the last deep scrape in seconds |

### API endpoint availability

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_api_http_status_code` | Gauge | `endpoint`, `network`, `url` | HTTP status code (200, 404, 500, etc.). `0` means connection failure. |
| `fiber_dashboard_api_http_duration_seconds` | Gauge | `endpoint`, `network`, `url` | Response time in seconds |
| `fiber_dashboard_api_up` | Gauge | `endpoint`, `network`, `url` | `1` if status code is 2xx, `0` otherwise |

- `endpoint` label = the path portion from `endpoints.yaml` (e.g. `/channels_hourly`, `/group_channel_by_state`)
- `network` label = `mainnet` or `testnet` (empty string `""` for `/health_check`)
- `url` label = the full URL that was probed

### Global error counter & exporter health

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_scrape_errors_total` | Counter | — | Cumulative number of scrape errors across all endpoints |
| `fiber_dashboard_exporter_up` | Gauge | — | `1` if the exporter itself is healthy |

### Meta

| Metric | Type | Description |
|---|---|---|
| `fiber_dashboard_exporter_info` | Info | Build metadata (version, target URL, networks) |

## CLI Options

| Flag | Default | Description |
|---|---|---|
| `--target-url` | (required) | Base URL of the Fiber Dashboard backend |
| `--networks` | `mainnet,testnet` | Comma-separated networks to monitor |
| `--listen-port` | `9101` | Prometheus metrics server port |
| `--scrape-interval` | `15` | Seconds between scrapes |
| `--request-timeout` | `5` | HTTP request timeout in seconds |
| `--stale-threshold` | `120` | Seconds before a heartbeat is marked stale |
| `--log-level` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `--endpoints-file` | `endpoints.yaml` | Endpoints YAML config file path |

## Endpoint Monitoring (`endpoints.yaml`)

Create an `endpoints.yaml` file to configure which API endpoints are monitored.
The file lists endpoint **paths** (with optional query parameters, but **without** the `net=` parameter).

```yaml
endpoints:
  - /health_check
  - /analysis_hourly
  - /channels_hourly?page=0&page_size=500
  - /group_channel_by_state?state=closed_cooperative&state=closed_uncooperative&state=closed_waiting_onchain_settlement&state=open&page=0&page_size=10&sort_by=last_commit_time&order=desc&asset_name=ckb
```

**How it works:**

- The base URL comes from `--target-url` (e.g. `http://localhost:8080`).
- `/health_check` is special: probed **once** with no `net=` parameter and an empty `network` label.
- All other endpoints: the exporter automatically appends `&net=mainnet` and `&net=testnet`
  (or `?net=` if the path has no existing `?`), creating **two probes per endpoint**.
- If `endpoints.yaml` does not exist, endpoint monitoring is silently skipped.

In addition to the HTTP probe results above, the exporter also performs **deep JSON parsing** of
`/all_region`, `/channel_count_by_state`, `/channel_capacity_distribution`, `/nodes_hourly`, and
`/channels_hourly` on every scrape cycle to extract richer business metrics.

## Prometheus & Alerting

Add a scrape job to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: fiber_dashboard
    static_configs:
      - targets:
          - "localhost:9101"
        labels:
          env: prod
```

See [`alerts.yml`](alerts.yml) for example Prometheus alerting rules.

## Grafana

![grafana dashboard](img/grafana1.png)

## License

MIT

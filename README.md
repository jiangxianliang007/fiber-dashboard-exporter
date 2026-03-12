# fiber-dashboard-exporter

Prometheus exporter for the [Fiber Dashboard](https://github.com/cryptape/fiber-dashboard) backend.

Polls the `/health_check` and `/analysis_hourly` endpoints and exposes
heartbeat timestamps, total node counts, and total channel counts as
Prometheus metrics — with **mainnet / testnet** breakdown.

Optionally, a YAML configuration file (`--config`) can activate scraping of
additional API endpoints (region distribution, channel states, capacity
distribution, etc.) with automatic network detection.

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

- `timed_commit_states_heartbeat` — fetches node/channel graphs from Fiber RPC (every 30 min)
- `daily_commit_task_heartbeat` — daily statistics aggregation (daily at 00:11 UTC)
- `hourly_fresh_task_heartbeat` — refreshes materialized views (every 5 min)
- `channel_monitor_heartbeat` — monitors on-chain channel states

### Network statistics (nodes & channels)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_total_nodes` | Gauge | `network` | Total active nodes |
| `fiber_dashboard_total_channels` | Gauge | `network` | Total active channels |
| `fiber_dashboard_total_capacity` | Gauge | `network` | Total network capacity from `/analysis_hourly` |
| `fiber_dashboard_total_liquidity` | Gauge | `network` | Total network liquidity from `/analysis_hourly` |
| `fiber_dashboard_network_scrape_success` | Gauge | `network` | `1` if the last /analysis_hourly scrape succeeded |

### Additional endpoint metrics (requires `--config`)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `fiber_dashboard_region_nodes` | Gauge | `network`, `region` | Node count per geographic region from `/all_region` |
| `fiber_dashboard_channel_count_by_state` | Gauge | `network`, `state` | Channel count per state from `/channel_count_by_state` or `/group_channel_by_state` |
| `fiber_dashboard_channel_capacity_distribution` | Gauge | `network`, `range` | Channel count per capacity bucket from `/channel_capacity_distribution` |
| `fiber_dashboard_nodes_total_from_page` | Gauge | `network` | Total node count from `/nodes_hourly` pagination metadata |
| `fiber_dashboard_channels_total_from_page` | Gauge | `network` | Total channel count from `/channels_hourly` pagination metadata |
| `fiber_dashboard_endpoint_scrape_success` | Gauge | `network`, `endpoint` | `1` if the last scrape of a configured endpoint succeeded |
| `fiber_dashboard_endpoint_scrape_duration_seconds` | Gauge | `network`, `endpoint` | Duration of the last scrape for a configured endpoint |

The `network` label is `mainnet` or `testnet`.

### Meta

| Metric | Type | Description |
|---|---|---|
| `fiber_dashboard_exporter_info` | Info | Build metadata (version, target URL, networks) |

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

# Also scrape additional endpoints from a config file
python exporter.py --target-url http://localhost:8080 --config endpoints.yaml
```

Metrics are now available at `http://localhost:9101/metrics`.

### Run with Docker

```bash
docker build -t fiber-dashboard-exporter .

docker run -d \
  --name fiber-dashboard-exporter \
  -p 9101:9101 \
  fiber-dashboard-exporter \
  --target-url http://host.docker.internal:8080

# With extra endpoint config file
docker run -d \
  --name fiber-dashboard-exporter \
  -p 9101:9101 \
  -v /path/to/endpoints.yaml:/endpoints.yaml \
  fiber-dashboard-exporter \
  --target-url http://host.docker.internal:8080 \
  --config /endpoints.yaml
```

### Run alongside Fiber Dashboard (Docker Compose)

Add the exporter service to the existing Fiber Dashboard `compose.yaml`:

```yaml
services:
  # ... existing timescaledb and fiber-dashbord services ...

  fiber-dashboard-exporter:
    build:
      context: ./fiber-dashboard-exporter
      dockerfile: Dockerfile
    container_name: fiber-dashboard-exporter
    ports:
      - "9101:9101"
    volumes:
      - ./endpoints.yaml:/endpoints.yaml
    command:
      - "--target-url=http://fiber-dashbord-backend:8080"
      - "--networks=mainnet,testnet"
      - "--scrape-interval=15"
      - "--stale-threshold=120"
      - "--config=/endpoints.yaml"
    networks:
      - fiber-network
    depends_on:
      - fiber-dashbord
    restart: always
```

## CLI Options

```
usage: exporter.py [-h] --target-url TARGET_URL
                   [--networks NETWORKS]
                   [--config FILE]
                   [--listen-port LISTEN_PORT]
                   [--scrape-interval SCRAPE_INTERVAL]
                   [--request-timeout REQUEST_TIMEOUT]
                   [--stale-threshold STALE_THRESHOLD]
                   [--log-level {DEBUG,INFO,WARNING,ERROR}]

options:
  --target-url          Base URL of the Fiber Dashboard backend (required)
                        e.g. http://localhost:8080
                        (also accepts http://localhost:8080/health_check)
  --networks            Comma-separated networks to monitor (default: mainnet,testnet)
  --config FILE         Path to a YAML config file listing additional endpoints to scrape
  --listen-port         Port for the Prometheus metrics server (default: 9101)
  --scrape-interval     Seconds between scrapes (default: 15)
  --request-timeout     HTTP request timeout in seconds (default: 5)
  --stale-threshold     Seconds after which a heartbeat is marked stale (default: 120)
  --log-level           Logging level (default: INFO)
```

## Endpoint Configuration File

Create a YAML file to list additional endpoints to monitor. Each entry needs
a `url` key (relative to `--target-url` or an absolute URL) and a `name` key
that tells the exporter which metric parser to use.

The **network** label is **auto-detected** from the `net=` query parameter in
the URL — no need to specify it manually.

```yaml
endpoints:
  - url: "/analysis_hourly?net=mainnet"
    name: "analysis_hourly"
  - url: "/analysis_hourly?net=testnet"
    name: "analysis_hourly"
  - url: "/all_region?net=mainnet"
    name: "all_region"
  - url: "/nodes_hourly?page=0&page_size=10&net=mainnet"
    name: "nodes_hourly"
  - url: "/channels_hourly?page=0&page_size=500&net=mainnet"
    name: "channels_hourly"
  - url: "/channel_capacity_distribution?net=mainnet"
    name: "channel_capacity_distribution"
  - url: "/channel_count_by_state?net=mainnet"
    name: "channel_count_by_state"
  - url: "/group_channel_by_state?state=open&state=closed_cooperative&state=closed_uncooperative&state=closed_waiting_onchain_settlement&page=0&page_size=10&sort_by=last_commit_time&order=desc&asset_name=ckb&net=mainnet"
    name: "group_channel_by_state"
```

A full example is provided in [`endpoints.yaml`](endpoints.yaml).

Supported `name` values:

| Name | Endpoint | Metrics Updated |
|---|---|---|
| `analysis_hourly` | `/analysis_hourly` | `total_nodes`, `total_channels`, `total_capacity`, `total_liquidity` |
| `all_region` | `/all_region` | `fiber_dashboard_region_nodes` |
| `nodes_hourly` | `/nodes_hourly` | `fiber_dashboard_nodes_total_from_page` |
| `channels_hourly` | `/channels_hourly` | `fiber_dashboard_channels_total_from_page` |
| `channel_capacity_distribution` | `/channel_capacity_distribution` | `fiber_dashboard_channel_capacity_distribution` |
| `channel_count_by_state` | `/channel_count_by_state` | `fiber_dashboard_channel_count_by_state` |
| `group_channel_by_state` | `/group_channel_by_state` | `fiber_dashboard_channel_count_by_state` |

## Prometheus Configuration

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

## Alert Rules

An example alerting rules file is provided in [`alerts.yml`](alerts.yml):

```yaml
groups:
  - name: fiber_dashboard
    rules:
      - alert: FiberDashboardScrapeDown
        expr: fiber_dashboard_scrape_success == 0
        for: 2m
        labels:
          severity: critical

      - alert: FiberDashboardTaskStale
        expr: fiber_dashboard_heartbeat_healthy == 0
        for: 5m
        labels:
          severity: warning

      - alert: FiberDashboardNoNodes
        expr: fiber_dashboard_total_nodes == 0
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "No active nodes on {{ $labels.network }}"

      - alert: FiberDashboardNoChannels
        expr: fiber_dashboard_total_channels == 0
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "No active channels on {{ $labels.network }}"

      - alert: FiberDashboardEndpointScrapeDown
        expr: fiber_dashboard_endpoint_scrape_success == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Endpoint {{ $labels.endpoint }} ({{ $labels.network }}) scrape failing"
```

## Example /metrics Output

```
# HELP fiber_dashboard_heartbeat_timestamp_seconds Unix timestamp of the last heartbeat
# TYPE fiber_dashboard_heartbeat_timestamp_seconds gauge
fiber_dashboard_heartbeat_timestamp_seconds{task="timed_commit_states_heartbeat"} 1.738857600e+09
fiber_dashboard_heartbeat_timestamp_seconds{task="daily_commit_task_heartbeat"} 1.738857600e+09
fiber_dashboard_heartbeat_timestamp_seconds{task="hourly_fresh_task_heartbeat"} 1.738857600e+09
fiber_dashboard_heartbeat_timestamp_seconds{task="channel_monitor_heartbeat"} 1.738857540e+09

# HELP fiber_dashboard_total_nodes Total number of active nodes
# TYPE fiber_dashboard_total_nodes gauge
fiber_dashboard_total_nodes{network="mainnet"} 42.0
fiber_dashboard_total_nodes{network="testnet"} 15.0

# HELP fiber_dashboard_total_channels Total number of active channels
# TYPE fiber_dashboard_total_channels gauge
fiber_dashboard_total_channels{network="mainnet"} 128.0
fiber_dashboard_total_channels{network="testnet"} 37.0

# HELP fiber_dashboard_total_capacity Total network capacity
# TYPE fiber_dashboard_total_capacity gauge
fiber_dashboard_total_capacity{network="mainnet"} 50000000000.0

# HELP fiber_dashboard_region_nodes Number of nodes per region
# TYPE fiber_dashboard_region_nodes gauge
fiber_dashboard_region_nodes{network="mainnet",region="Asia"} 18.0
fiber_dashboard_region_nodes{network="mainnet",region="Europe"} 12.0

# HELP fiber_dashboard_channel_count_by_state Number of channels per state
# TYPE fiber_dashboard_channel_count_by_state gauge
fiber_dashboard_channel_count_by_state{network="mainnet",state="open"} 100.0
fiber_dashboard_channel_count_by_state{network="mainnet",state="closed_cooperative"} 28.0

# HELP fiber_dashboard_endpoint_scrape_success Per-endpoint scrape success
# TYPE fiber_dashboard_endpoint_scrape_success gauge
fiber_dashboard_endpoint_scrape_success{endpoint="all_region",network="mainnet"} 1.0
fiber_dashboard_endpoint_scrape_success{endpoint="channel_count_by_state",network="mainnet"} 1.0
```

## How It Works

```
                         GET /health_check
┌──────────────┐    GET /analysis_hourly?net=mainnet    ┌──────────────────┐
│              │    GET /analysis_hourly?net=testnet     │                  │
│   Exporter   │    GET /all_region?net=mainnet  ──────> │  Fiber Dashboard │
│  :9101       │    GET /channel_count_by_state?net=...  │  Backend :8080   │
│              │ <────────────────────────────────────   │                  │
│              │         JSON responses                  │                  │
└──────────────┘                                        └──────────────────┘
       │
       │  GET /metrics
       v
┌──────────────┐
│  Prometheus  │
└──────────────┘
       │
       v
┌──────────────┐
│   Grafana    │
│  (optional)  │
└──────────────┘
```

## Import Grafana

![grafana dashboard](img/grafana1.png)

## License

MIT


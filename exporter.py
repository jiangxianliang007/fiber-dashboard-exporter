#!/usr/bin/env python3
"""
Prometheus exporter for Fiber Dashboard.

Polls the Fiber Dashboard backend /health_check and /analysis_hourly APIs
and exposes heartbeat timestamps, node counts, and channel counts as
Prometheus metrics.  Also performs deep JSON parsing of /all_region,
/channel_count_by_state, /channel_capacity_distribution, /nodes_hourly,
and /channels_hourly to expose richer network telemetry.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import signal
import sys
import time
import urllib3
from urllib.parse import urlparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from urllib3.exceptions import NotOpenSSLWarning
    import warnings
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import requests
import yaml
from prometheus_client import Counter, Gauge, Info, start_http_server

logger = logging.getLogger("fiber_dashboard_exporter")

# ---------------------------------------------------------------------------
# Metrics — health_check
# ---------------------------------------------------------------------------

HEARTBEAT_TIMESTAMP = Gauge(
    "fiber_dashboard_heartbeat_timestamp_seconds",
    "Unix timestamp of the last heartbeat reported by a background task",
    ["task"],
)

HEARTBEAT_AGE = Gauge(
    "fiber_dashboard_heartbeat_age_seconds",
    "Seconds since the last heartbeat for a background task",
    ["task"],
)

HEARTBEAT_HEALTHY = Gauge(
    "fiber_dashboard_heartbeat_healthy",
    "Whether the heartbeat is considered healthy (1) or stale (0)",
    ["task"],
)

SCRAPE_SUCCESS = Gauge(
    "fiber_dashboard_scrape_success",
    "Whether the last scrape of /health_check succeeded (1) or failed (0)",
)

SCRAPE_DURATION = Gauge(
    "fiber_dashboard_scrape_duration_seconds",
    "Duration of the last /health_check scrape in seconds",
)

# ---------------------------------------------------------------------------
# Metrics — network stats (nodes / channels)
# ---------------------------------------------------------------------------

TOTAL_NODES = Gauge(
    "fiber_dashboard_total_nodes",
    "Total number of active nodes in the Fiber Network",
    ["network"],
)

TOTAL_CHANNELS = Gauge(
    "fiber_dashboard_total_channels",
    "Total number of active channels in the Fiber Network",
    ["network"],
)

NETWORK_SCRAPE_SUCCESS = Gauge(
    "fiber_dashboard_network_scrape_success",
    "Whether the last scrape of /analysis_hourly succeeded (1) or failed (0)",
    ["network"],
)

# ---------------------------------------------------------------------------
# Metrics — enhanced network stats (capacity / liquidity / avg capacity)
# ---------------------------------------------------------------------------

TOTAL_CAPACITY = Gauge(
    "fiber_dashboard_total_capacity",
    "Total capacity of the Fiber Network",
    ["network"],
)

TOTAL_LIQUIDITY = Gauge(
    "fiber_dashboard_total_liquidity",
    "Total liquidity of the Fiber Network",
    ["network"],
)

AVG_CHANNEL_CAPACITY = Gauge(
    "fiber_dashboard_avg_channel_capacity",
    "Average channel capacity (total_capacity / total_channels)",
    ["network"],
)

# ---------------------------------------------------------------------------
# Metrics — region / channel state / capacity distribution
# ---------------------------------------------------------------------------

REGION_NODES = Gauge(
    "fiber_dashboard_region_nodes",
    "Number of nodes in each geographic region",
    ["network", "region"],
)

CHANNEL_COUNT_BY_STATE = Gauge(
    "fiber_dashboard_channel_count_by_state",
    "Number of channels in each state",
    ["network", "state"],
)

CHANNEL_CAPACITY_DISTRIBUTION = Gauge(
    "fiber_dashboard_channel_capacity_distribution",
    "Number of channels in each capacity range",
    ["network", "range"],
)

NODES_TOTAL_FROM_PAGE = Gauge(
    "fiber_dashboard_nodes_total_from_page",
    "Total number of nodes reported by the /nodes_hourly pagination metadata",
    ["network"],
)

CHANNELS_TOTAL_FROM_PAGE = Gauge(
    "fiber_dashboard_channels_total_from_page",
    "Total number of channels reported by the /channels_hourly pagination metadata",
    ["network"],
)

# ---------------------------------------------------------------------------
# Metrics — per-endpoint deep scrape status
# ---------------------------------------------------------------------------

ENDPOINT_SCRAPE_SUCCESS = Gauge(
    "fiber_dashboard_endpoint_scrape_success",
    "Whether the last deep scrape of an endpoint succeeded (1) or failed (0)",
    ["network", "endpoint"],
)

ENDPOINT_SCRAPE_DURATION = Gauge(
    "fiber_dashboard_endpoint_scrape_duration_seconds",
    "Duration of the last deep scrape of an endpoint in seconds",
    ["network", "endpoint"],
)

# ---------------------------------------------------------------------------
# Metrics — global error counter and exporter health
# ---------------------------------------------------------------------------

SCRAPE_ERRORS_TOTAL = Counter(
    "fiber_dashboard_scrape_errors_total",
    "Cumulative number of scrape errors across all endpoints",
)

EXPORTER_UP = Gauge(
    "fiber_dashboard_exporter_up",
    "Whether the exporter itself is healthy (1) or not (0)",
)

# ---------------------------------------------------------------------------
# Metrics — API endpoint availability
# ---------------------------------------------------------------------------

API_HTTP_STATUS_CODE = Gauge(
    "fiber_dashboard_api_http_status_code",
    "HTTP status code returned by the API endpoint (0 = connection failure)",
    ["endpoint", "network", "url"],
)

API_HTTP_DURATION_SECONDS = Gauge(
    "fiber_dashboard_api_http_duration_seconds",
    "Response time in seconds for the API endpoint",
    ["endpoint", "network", "url"],
)

API_UP = Gauge(
    "fiber_dashboard_api_up",
    "1 if the API endpoint returned a 2xx status code, 0 otherwise",
    ["endpoint", "network", "url"],
)

# ---------------------------------------------------------------------------
# Metrics — meta
# ---------------------------------------------------------------------------

BUILD_INFO = Info(
    "fiber_dashboard_exporter",
    "Metadata about the exporter itself",
)

# The four tasks exposed by /health_check
TASK_NAMES = [
    "timed_commit_states_heartbeat",
    "daily_commit_task_heartbeat",
    "hourly_fresh_task_heartbeat",
    "channel_monitor_heartbeat",
]

VALID_NETWORKS = ("mainnet", "testnet")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex_to_int(value) -> int:
    """Convert a hex-encoded string (0x...) or plain integer to int.

    The /analysis_hourly endpoint returns total_nodes and channel_len as
    hex strings like ``"0x1a"``.  Plain integers are also accepted so the
    exporter stays forward-compatible.
    """
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


def _to_float(value, default: float = 0.0) -> float:
    """Convert a value to float, supporting hex strings (0x...).

    Returns *default* when the value is ``None`` or cannot be converted.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s.startswith("0x") or s.startswith("0X"):
        return float(int(s, 16))
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _derive_base_url(health_check_url: str) -> str:
    """Derive the base URL (scheme + host + port) from a full health_check URL.

    Examples:
        http://localhost:8080/health_check  -> http://localhost:8080
        http://10.0.0.1:8080/health_check/  -> http://10.0.0.1:8080
    """
    parsed = urlparse(health_check_url)
    return f"{parsed.scheme}://{parsed.netloc}"


# ---------------------------------------------------------------------------
# Scrape logic
# ---------------------------------------------------------------------------


def scrape_health_check(url: str, timeout: float, stale_threshold: float) -> None:
    """Fetch /health_check and update heartbeat Prometheus metrics."""
    start = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.monotonic() - start

        SCRAPE_SUCCESS.set(1)
        SCRAPE_DURATION.set(elapsed)

        now = time.time()
        for task in TASK_NAMES:
            ts = data.get(task)
            if ts is None:
                logger.warning("task %s missing from response", task)
                continue

            ts = float(ts)
            HEARTBEAT_TIMESTAMP.labels(task=task).set(ts)

            age = now - ts if ts > 0 else float("inf")
            HEARTBEAT_AGE.labels(task=task).set(age)

            healthy = 1 if 0 < ts and age < stale_threshold else 0
            HEARTBEAT_HEALTHY.labels(task=task).set(healthy)

        logger.debug("health_check scrape ok in %.3fs", elapsed)

    except Exception:
        elapsed = time.monotonic() - start
        SCRAPE_SUCCESS.set(0)
        SCRAPE_DURATION.set(elapsed)
        logger.exception("health_check scrape failed")


def scrape_network_stats(
    base_url: str, network: str, timeout: float
) -> None:
    """Fetch /analysis_hourly?net=<network> and update node/channel metrics."""
    url = f"{base_url}/analysis_hourly?net={network}"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        total_nodes = _hex_to_int(data.get("total_nodes", 0))
        total_channels = _hex_to_int(data.get("channel_len", 0))

        TOTAL_NODES.labels(network=network).set(total_nodes)
        TOTAL_CHANNELS.labels(network=network).set(total_channels)

        # total_capacity = sum of capacity_analysis[*].total (hex strings)
        total_capacity = 0.0
        for item in data.get("capacity_analysis", []):
            if isinstance(item, dict):
                total_capacity += _to_float(_hex_to_int(item.get("total", 0)))
        TOTAL_CAPACITY.labels(network=network).set(total_capacity)

        # total_liquidity = sum of asset_analysis[*].total (hex strings)
        total_liquidity = 0.0
        for item in data.get("asset_analysis", []):
            if isinstance(item, dict):
                total_liquidity += _to_float(_hex_to_int(item.get("total", 0)))
        TOTAL_LIQUIDITY.labels(network=network).set(total_liquidity)

        if total_channels > 0:
            AVG_CHANNEL_CAPACITY.labels(network=network).set(
                total_capacity / total_channels
            )

        NETWORK_SCRAPE_SUCCESS.labels(network=network).set(1)

        logger.debug(
            "network=%s  nodes=%d  channels=%d  capacity=%.0f  liquidity=%.0f",
            network, total_nodes, total_channels, total_capacity, total_liquidity
        )

    except Exception:
        NETWORK_SCRAPE_SUCCESS.labels(network=network).set(0)
        SCRAPE_ERRORS_TOTAL.inc()
        logger.exception("analysis_hourly scrape failed for network=%s", network)


def load_endpoints(path: str) -> list[str]:
    """Load endpoint paths from a YAML file.

    Returns an empty list if the file does not exist or cannot be parsed.
    """
    if not os.path.exists(path):
        logger.info("endpoints file not found at %s, skipping endpoint monitoring", path)
        return []
    try:
        with open(path, "r") as fh:
            data = yaml.safe_load(fh)
        endpoints = data.get("endpoints", []) if isinstance(data, dict) else []
        return [str(e) for e in endpoints if e]
    except Exception:
        logger.exception("failed to load endpoints file %s", path)
        return []


def scrape_api_endpoints(
    base_url: str, endpoints: list[str], networks: list[str], timeout: float
) -> None:
    """Probe each configured endpoint and record HTTP status code, duration, and up/down.

    Rules:
    - ``/health_check`` is special: no ``net=`` parameter is appended; probed once
      with an empty network label.
    - All other endpoints: ``net=<network>`` is appended for every configured
      network (one probe per network).  If the path already contains a ``?`` the
      parameter is appended with ``&``, otherwise with ``?``.
    """
    for path in endpoints:
        path_part = path.split("?")[0] if "?" in path else path
        # Strip query string to derive the endpoint label (path only)
        endpoint_label = path_part

        if path == "/health_check":
            # Special case: probe once, no network
            full_url = f"{base_url}/health_check"
            _probe_endpoint(endpoint_label, "", full_url, timeout)
        else:
            separator = "&" if "?" in path else "?"
            for net in networks:
                full_url = f"{base_url}{path}{separator}net={net}"
                _probe_endpoint(endpoint_label, net, full_url, timeout)


def _probe_endpoint(endpoint: str, network: str, url: str, timeout: float) -> None:
    """Make a single HTTP GET request and update the API availability metrics."""
    start = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout)
        elapsed = time.monotonic() - start
        status_code = resp.status_code
        up = 1 if 200 <= status_code < 300 else 0
        logger.debug(
            "endpoint=%s network=%s status=%d duration=%.3fs",
            endpoint, network, status_code, elapsed,
        )
    except Exception:
        elapsed = time.monotonic() - start
        status_code = 0
        up = 0
        logger.exception("endpoint probe failed: endpoint=%s network=%s url=%s", endpoint, network, url)

    API_HTTP_STATUS_CODE.labels(endpoint=endpoint, network=network, url=url).set(status_code)
    API_HTTP_DURATION_SECONDS.labels(endpoint=endpoint, network=network, url=url).set(elapsed)
    API_UP.labels(endpoint=endpoint, network=network, url=url).set(up)


def _scrape_endpoint_wrapper(
    endpoint_label: str, network: str, func, *args, **kwargs
) -> bool:
    """Call *func* and record ENDPOINT_SCRAPE_SUCCESS / ENDPOINT_SCRAPE_DURATION.

    Returns True on success, False on failure.
    """
    start = time.monotonic()
    try:
        func(*args, **kwargs)
        elapsed = time.monotonic() - start
        ENDPOINT_SCRAPE_SUCCESS.labels(network=network, endpoint=endpoint_label).set(1)
        ENDPOINT_SCRAPE_DURATION.labels(network=network, endpoint=endpoint_label).set(elapsed)
        return True
    except Exception:
        elapsed = time.monotonic() - start
        ENDPOINT_SCRAPE_SUCCESS.labels(network=network, endpoint=endpoint_label).set(0)
        ENDPOINT_SCRAPE_DURATION.labels(network=network, endpoint=endpoint_label).set(elapsed)
        SCRAPE_ERRORS_TOTAL.inc()
        logger.exception(
            "deep scrape failed: endpoint=%s network=%s", endpoint_label, network
        )
        return False


def _scrape_all_region(base_url: str, network: str, timeout: float) -> None:
    """Fetch /all_region?net=<network> and set REGION_NODES gauges."""
    url = f"{base_url}/all_region?net={network}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        # API returns {"US": 10, "CN": 5, ...}
        for region, count in data.items():
            REGION_NODES.labels(network=network, region=str(region)).set(
                _to_float(count)
            )
    elif isinstance(data, list):
        if not data:
            return
        first = data[0]
        if isinstance(first, dict):
            # Format: [{"region": "US", "count": 10}, ...]
            for item in data:
                if isinstance(item, dict):
                    region = str(item.get("region", "unknown"))
                    count = _to_float(item.get("count", 0))
                    REGION_NODES.labels(network=network, region=region).set(count)
        elif isinstance(first, str):
            # Format: ["US", "US", "CN", "DE", ...] — count occurrences
            region_counts = collections.Counter(str(r) for r in data)
            for region, count in region_counts.items():
                REGION_NODES.labels(network=network, region=region).set(count)
        else:
            logger.warning(
                "all_region: unexpected item type %s in list for network=%s",
                type(first).__name__,
                network,
            )
    else:
        logger.warning("all_region: unexpected data format for network=%s", network)


def scrape_all_region(base_url: str, network: str, timeout: float) -> None:
    """Wrapped version of _scrape_all_region with success/duration tracking."""
    _scrape_endpoint_wrapper("/all_region", network, _scrape_all_region, base_url, network, timeout)


def _scrape_channel_count_by_state(base_url: str, network: str, timeout: float) -> None:
    """Fetch /channel_count_by_state?net=<network> and set CHANNEL_COUNT_BY_STATE gauges."""
    url = f"{base_url}/channel_count_by_state?net={network}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Actual API returns nested: {"ckb": {"open": 23, "closed_cooperative": 1, ...}, ...}
    # Flat fallback: {"open": 100, "closed_cooperative": 5, ...}
    if isinstance(data, dict):
        first_asset_data = next(iter(data.values()), None) if data else None
        if isinstance(first_asset_data, dict):
            # Nested format: outer key = asset, inner key = state, inner value = count
            for asset, states in data.items():
                if isinstance(states, dict):
                    for state, count in states.items():
                        CHANNEL_COUNT_BY_STATE.labels(
                            network=network, state=str(state)
                        ).set(_to_float(count))
        else:
            # Flat format: {"open": 100, ...}
            for state, count in data.items():
                CHANNEL_COUNT_BY_STATE.labels(network=network, state=str(state)).set(
                    _to_float(count)
                )
    elif isinstance(data, list):
        # Tolerate list-of-dicts format: [{"state": "open", "count": 100}, ...]
        for item in data:
            if isinstance(item, dict):
                state = str(item.get("state", "unknown"))
                count = _to_float(item.get("count", 0))
                CHANNEL_COUNT_BY_STATE.labels(network=network, state=state).set(count)
            else:
                logger.warning(
                    "channel_count_by_state: unexpected item type %s in list for network=%s",
                    type(item).__name__,
                    network,
                )
    else:
        logger.warning(
            "channel_count_by_state: unexpected data format for network=%s", network
        )


def scrape_channel_count_by_state(base_url: str, network: str, timeout: float) -> None:
    """Wrapped version of _scrape_channel_count_by_state with success/duration tracking."""
    _scrape_endpoint_wrapper(
        "/channel_count_by_state",
        network,
        _scrape_channel_count_by_state,
        base_url,
        network,
        timeout,
    )


def _scrape_channel_capacity_distribution(
    base_url: str, network: str, timeout: float
) -> None:
    """Fetch /channel_capacity_distribution?net=<network> and set CHANNEL_CAPACITY_DISTRIBUTION gauges."""
    url = f"{base_url}/channel_capacity_distribution?net={network}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Actual API returns three-level nested:
    # {"capacity": {"ckb": {"Capacity 10^0k": 26, ...}}, "asset": {...}}
    # Flat dict fallback: {"range_name": count, ...}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                rng = str(item.get("range", "unknown"))
                count = _to_float(item.get("count", 0))
                CHANNEL_CAPACITY_DISTRIBUTION.labels(network=network, range=rng).set(count)
            else:
                logger.warning(
                    "channel_capacity_distribution: unexpected item type %s in list for network=%s",
                    type(item).__name__,
                    network,
                )
    elif isinstance(data, dict):
        capacity_data = data.get("capacity", {})
        if isinstance(capacity_data, dict) and capacity_data:
            # Three-level nested: data["capacity"][asset][range_label] = count
            for asset_name, distribution in capacity_data.items():
                if isinstance(distribution, dict):
                    for range_label, count in distribution.items():
                        CHANNEL_CAPACITY_DISTRIBUTION.labels(
                            network=network, range=str(range_label)
                        ).set(_to_float(count))
        else:
            # Flat dict fallback: {"range_name": count, ...}
            for rng, count in data.items():
                if not isinstance(count, dict):
                    CHANNEL_CAPACITY_DISTRIBUTION.labels(
                        network=network, range=str(rng)
                    ).set(_to_float(count))
    else:
        logger.warning(
            "channel_capacity_distribution: unexpected data format for network=%s",
            network,
        )


def scrape_channel_capacity_distribution(
    base_url: str, network: str, timeout: float
) -> None:
    """Wrapped version of _scrape_channel_capacity_distribution with success/duration tracking."""
    _scrape_endpoint_wrapper(
        "/channel_capacity_distribution",
        network,
        _scrape_channel_capacity_distribution,
        base_url,
        network,
        timeout,
    )


def _scrape_nodes_hourly(base_url: str, network: str, timeout: float) -> None:
    """Fetch /nodes_hourly pagination metadata and set NODES_TOTAL_FROM_PAGE."""
    url = f"{base_url}/nodes_hourly?page=0&page_size=10&net={network}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Actual API returns: {"total_count": 22, "nodes": [...], "next_page": 1}
    # Try "total_count" first, fall back to "total" for compatibility
    if not isinstance(data, dict):
        logger.warning("nodes_hourly: unexpected data format for network=%s", network)
        return
    total = _to_float(data.get("total_count", data.get("total", 0)))
    NODES_TOTAL_FROM_PAGE.labels(network=network).set(total)


def scrape_nodes_hourly(base_url: str, network: str, timeout: float) -> None:
    """Wrapped version of _scrape_nodes_hourly with success/duration tracking."""
    _scrape_endpoint_wrapper(
        "/nodes_hourly", network, _scrape_nodes_hourly, base_url, network, timeout
    )


def _scrape_channels_hourly(base_url: str, network: str, timeout: float) -> None:
    """Fetch /channels_hourly pagination metadata and set CHANNELS_TOTAL_FROM_PAGE."""
    url = f"{base_url}/channels_hourly?page=0&page_size=500&net={network}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Actual API returns: {"total_count": 26, "channels": [...], "next_page": 1}
    # Try "total_count" first, fall back to "total" for compatibility
    if not isinstance(data, dict):
        logger.warning("channels_hourly: unexpected data format for network=%s", network)
        return
    total = _to_float(data.get("total_count", data.get("total", 0)))
    CHANNELS_TOTAL_FROM_PAGE.labels(network=network).set(total)


def scrape_channels_hourly(base_url: str, network: str, timeout: float) -> None:
    """Wrapped version of _scrape_channels_hourly with success/duration tracking."""
    _scrape_endpoint_wrapper(
        "/channels_hourly", network, _scrape_channels_hourly, base_url, network, timeout
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Prometheus exporter for Fiber Dashboard",
    )
    p.add_argument(
        "--target-url",
        required=True,
        help=(
            "Base URL of the Fiber Dashboard backend, "
            "e.g. http://localhost:8080 "
            "(also accepts the full /health_check URL for backward compatibility)"
        ),
    )
    p.add_argument(
        "--networks",
        default="mainnet,testnet",
        help=(
            "Comma-separated list of networks to monitor: mainnet, testnet, "
            "or both (default: mainnet,testnet)"
        ),
    )
    p.add_argument(
        "--listen-port",
        type=int,
        default=9101,
        help="Port on which to expose Prometheus metrics (default: 9101)",
    )
    p.add_argument(
        "--scrape-interval",
        type=float,
        default=15,
        help="Seconds between scrapes (default: 15)",
    )
    p.add_argument(
        "--request-timeout",
        type=float,
        default=5,
        help="HTTP request timeout in seconds (default: 5)",
    )
    p.add_argument(
        "--stale-threshold",
        type=float,
        default=120,
        help="Seconds after which a heartbeat is considered stale (default: 120)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    p.add_argument(
        "--endpoints-file",
        default="endpoints.yaml",
        help="Path to the endpoints YAML config file (default: endpoints.yaml). "
             "If the file does not exist, endpoint monitoring is skipped.",
    )
    return p.parse_args(argv)


def _normalise_urls(target_url: str) -> tuple[str, str]:
    """Return (health_check_url, base_url) from the user-supplied target URL.

    Accepts either a base URL  (http://host:8080)
    or a full endpoint URL     (http://host:8080/health_check).
    """
    parsed = urlparse(target_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    health = f"{base}/health_check"
    return health, base


def _parse_networks(raw: str) -> list[str]:
    nets = [n.strip().lower() for n in raw.split(",") if n.strip()]
    for n in nets:
        if n not in VALID_NETWORKS:
            raise SystemExit(f"invalid network: {n!r}  (choose from {VALID_NETWORKS})")
    return nets


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )

    health_url, base_url = _normalise_urls(args.target_url)
    networks = _parse_networks(args.networks)
    endpoints = load_endpoints(args.endpoints_file)

    BUILD_INFO.info({
        "version": "0.4.0",
        "target_url": args.target_url,
        "networks": ",".join(networks),
    })

    EXPORTER_UP.set(1)

    # Graceful shutdown
    running = True

    def _stop(signum, _frame):
        nonlocal running
        logger.info("received signal %s, shutting down", signum)
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # Start the Prometheus HTTP server
    start_http_server(args.listen_port)
    logger.info(
        "exporter started  listen=:%d  target=%s  networks=%s  interval=%ds  stale=%ds",
        args.listen_port,
        base_url,
        ",".join(networks),
        args.scrape_interval,
        args.stale_threshold,
    )

    while running:
        scrape_health_check(health_url, args.request_timeout, args.stale_threshold)

        for net in networks:
            scrape_network_stats(base_url, net, args.request_timeout)
            scrape_all_region(base_url, net, args.request_timeout)
            scrape_channel_count_by_state(base_url, net, args.request_timeout)
            scrape_channel_capacity_distribution(base_url, net, args.request_timeout)
            scrape_nodes_hourly(base_url, net, args.request_timeout)
            scrape_channels_hourly(base_url, net, args.request_timeout)

        if endpoints:
            scrape_api_endpoints(base_url, endpoints, networks, args.request_timeout)

        # Sleep in small increments so we can react to signals quickly
        deadline = time.monotonic() + args.scrape_interval
        while running and time.monotonic() < deadline:
            time.sleep(0.5)

    logger.info("exporter stopped")
    sys.exit(0)


if __name__ == "__main__":
    main()

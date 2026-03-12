#!/usr/bin/env python3
"""
Prometheus exporter for Fiber Dashboard.

Polls the Fiber Dashboard backend /health_check and /analysis_hourly APIs
and exposes heartbeat timestamps, node counts, and channel counts as
Prometheus metrics.

Optionally, a YAML config file (--config) can specify additional API
endpoints to monitor, with metrics labelled by network auto-detected from
the ``net=`` query parameter.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import urllib3
from urllib.parse import urlparse, parse_qs

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from urllib3.exceptions import NotOpenSSLWarning
    import warnings
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import requests
import yaml
from prometheus_client import Gauge, Info, start_http_server

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

TOTAL_CAPACITY = Gauge(
    "fiber_dashboard_total_capacity",
    "Total network capacity from /analysis_hourly",
    ["network"],
)

TOTAL_LIQUIDITY = Gauge(
    "fiber_dashboard_total_liquidity",
    "Total network liquidity from /analysis_hourly",
    ["network"],
)

# ---------------------------------------------------------------------------
# Metrics — additional endpoints
# ---------------------------------------------------------------------------

REGION_NODES = Gauge(
    "fiber_dashboard_region_nodes",
    "Number of nodes per region from /all_region",
    ["network", "region"],
)

CHANNEL_COUNT_BY_STATE = Gauge(
    "fiber_dashboard_channel_count_by_state",
    "Number of channels per state from /channel_count_by_state or /group_channel_by_state",
    ["network", "state"],
)

CHANNEL_CAPACITY_DISTRIBUTION = Gauge(
    "fiber_dashboard_channel_capacity_distribution",
    "Distribution of channel counts per capacity range from /channel_capacity_distribution",
    ["network", "range"],
)

NODES_TOTAL_FROM_PAGE = Gauge(
    "fiber_dashboard_nodes_total_from_page",
    "Total node count from /nodes_hourly pagination metadata",
    ["network"],
)

CHANNELS_TOTAL_FROM_PAGE = Gauge(
    "fiber_dashboard_channels_total_from_page",
    "Total channel count from /channels_hourly pagination metadata",
    ["network"],
)

ENDPOINT_SCRAPE_SUCCESS = Gauge(
    "fiber_dashboard_endpoint_scrape_success",
    "Whether the last scrape of a configured endpoint succeeded (1) or failed (0)",
    ["network", "endpoint"],
)

ENDPOINT_SCRAPE_DURATION = Gauge(
    "fiber_dashboard_endpoint_scrape_duration_seconds",
    "Duration of the last scrape for a configured endpoint in seconds",
    ["network", "endpoint"],
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


def _to_float(value) -> float:
    """Safely convert a value to float; return 0.0 on failure."""
    try:
        if isinstance(value, str) and (value.startswith("0x") or value.startswith("0X")):
            return float(int(value, 16))
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _detect_network(url: str) -> str:
    """Detect network from the ``net=`` query parameter in *url*.

    Returns ``"unknown"`` when the parameter is absent.
    """
    params = parse_qs(urlparse(url).query)
    nets = params.get("net", [])
    return nets[0] if nets else "unknown"


def _resolve_url(base_url: str, path_or_url: str) -> str:
    """Return an absolute URL given a *base_url* and a *path_or_url*.

    If *path_or_url* is already an absolute URL it is returned unchanged.
    Otherwise it is joined onto *base_url*.
    """
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    # Ensure base ends without trailing slash before joining
    return base_url.rstrip("/") + "/" + path_or_url.lstrip("/")


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
        NETWORK_SCRAPE_SUCCESS.labels(network=network).set(1)

        # Enhanced: also expose total_capacity and total_liquidity when present
        if "total_capacity" in data:
            TOTAL_CAPACITY.labels(network=network).set(_to_float(data["total_capacity"]))
        if "total_liquidity" in data:
            TOTAL_LIQUIDITY.labels(network=network).set(_to_float(data["total_liquidity"]))

        logger.debug(
            "network=%s  nodes=%d  channels=%d", network, total_nodes, total_channels
        )

    except Exception:
        NETWORK_SCRAPE_SUCCESS.labels(network=network).set(0)
        logger.exception("analysis_hourly scrape failed for network=%s", network)


def _scrape_endpoint_wrapper(
    url: str, network: str, endpoint_name: str, timeout: float, scrape_fn
) -> None:
    """Wrap a scrape function with per-endpoint success/duration tracking."""
    start = time.monotonic()
    try:
        scrape_fn(url, network, timeout)
        elapsed = time.monotonic() - start
        ENDPOINT_SCRAPE_SUCCESS.labels(network=network, endpoint=endpoint_name).set(1)
        ENDPOINT_SCRAPE_DURATION.labels(network=network, endpoint=endpoint_name).set(elapsed)
        logger.debug("endpoint=%s network=%s scrape ok in %.3fs", endpoint_name, network, elapsed)
    except Exception:
        elapsed = time.monotonic() - start
        ENDPOINT_SCRAPE_SUCCESS.labels(network=network, endpoint=endpoint_name).set(0)
        ENDPOINT_SCRAPE_DURATION.labels(network=network, endpoint=endpoint_name).set(elapsed)
        logger.exception("endpoint=%s network=%s scrape failed", endpoint_name, network)


def _scrape_all_region(url: str, network: str, timeout: float) -> None:
    """Fetch /all_region and update per-region node count metrics."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    # Accept list-of-dicts [{"region": "...", "count": N}, ...] or a plain dict
    if isinstance(data, list):
        for item in data:
            region = str(item.get("region", item.get("name", "unknown")))
            count = _to_float(item.get("count", item.get("node_count", 0)))
            REGION_NODES.labels(network=network, region=region).set(count)
    elif isinstance(data, dict):
        for region, count in data.items():
            REGION_NODES.labels(network=network, region=str(region)).set(_to_float(count))


def _scrape_nodes_hourly(url: str, network: str, timeout: float) -> None:
    """Fetch /nodes_hourly and update total-node-count-from-pagination metric."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    # Accept {"total": N, ...} or {"count": N, ...} or {"pagination": {"total": N}}
    total = (
        data.get("total")
        or data.get("count")
        or (data.get("pagination") or {}).get("total")
        or 0
    )
    NODES_TOTAL_FROM_PAGE.labels(network=network).set(_to_float(total))


def _scrape_channels_hourly(url: str, network: str, timeout: float) -> None:
    """Fetch /channels_hourly and update total-channel-count-from-pagination metric."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    total = (
        data.get("total")
        or data.get("count")
        or (data.get("pagination") or {}).get("total")
        or 0
    )
    CHANNELS_TOTAL_FROM_PAGE.labels(network=network).set(_to_float(total))


def _scrape_channel_capacity_distribution(url: str, network: str, timeout: float) -> None:
    """Fetch /channel_capacity_distribution and update capacity-range metrics."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    # Accept list-of-dicts [{"range": "...", "count": N}, ...] or a plain dict
    if isinstance(data, list):
        for item in data:
            rng = str(item.get("range", item.get("label", item.get("bucket", "unknown"))))
            count = _to_float(item.get("count", item.get("value", 0)))
            CHANNEL_CAPACITY_DISTRIBUTION.labels(network=network, range=rng).set(count)
    elif isinstance(data, dict):
        for rng, count in data.items():
            CHANNEL_CAPACITY_DISTRIBUTION.labels(network=network, range=str(rng)).set(_to_float(count))


def _scrape_channel_count_by_state(url: str, network: str, timeout: float) -> None:
    """Fetch /channel_count_by_state and update per-state channel count metrics."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    # Accept list-of-dicts [{"state": "...", "count": N}, ...] or a plain dict
    if isinstance(data, list):
        for item in data:
            state = str(item.get("state", item.get("name", "unknown")))
            count = _to_float(item.get("count", item.get("value", 0)))
            CHANNEL_COUNT_BY_STATE.labels(network=network, state=state).set(count)
    elif isinstance(data, dict):
        for state, count in data.items():
            CHANNEL_COUNT_BY_STATE.labels(network=network, state=str(state)).set(_to_float(count))


def _scrape_group_channel_by_state(url: str, network: str, timeout: float) -> None:
    """Fetch /group_channel_by_state and update per-state channel count metrics."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    # Accept list-of-dicts [{"state": "...", "count": N}, ...] or a plain dict
    if isinstance(data, list):
        for item in data:
            state = str(item.get("state", item.get("name", "unknown")))
            count = _to_float(item.get("count", item.get("total", 0)))
            CHANNEL_COUNT_BY_STATE.labels(network=network, state=state).set(count)
    elif isinstance(data, dict):
        for state, count in data.items():
            if isinstance(count, (int, float, str)):
                CHANNEL_COUNT_BY_STATE.labels(network=network, state=str(state)).set(_to_float(count))


# Map endpoint name → raw scrape function (without wrapper)
_ENDPOINT_SCRAPERS: dict[str, object] = {
    "all_region": _scrape_all_region,
    "nodes_hourly": _scrape_nodes_hourly,
    "channels_hourly": _scrape_channels_hourly,
    "channel_capacity_distribution": _scrape_channel_capacity_distribution,
    "channel_count_by_state": _scrape_channel_count_by_state,
    "group_channel_by_state": _scrape_group_channel_by_state,
}


def scrape_configured_endpoint(
    base_url: str, ep_cfg: dict, timeout: float
) -> None:
    """Scrape a single user-configured endpoint described by *ep_cfg*.

    *ep_cfg* should have keys ``url`` (required) and ``name`` (optional).
    The network is auto-detected from the ``net=`` query parameter of the URL.
    """
    raw_url = ep_cfg.get("url", "")
    if not raw_url:
        logger.warning("configured endpoint missing 'url' key: %r", ep_cfg)
        return

    url = _resolve_url(base_url, raw_url)
    endpoint_name = ep_cfg.get("name") or urlparse(url).path.strip("/").replace("/", "_")
    network = _detect_network(url)

    scrape_fn = _ENDPOINT_SCRAPERS.get(endpoint_name)
    if scrape_fn is None:
        logger.warning(
            "no scraper registered for endpoint name %r; skipping %s", endpoint_name, url
        )
        return

    _scrape_endpoint_wrapper(url, network, endpoint_name, timeout, scrape_fn)


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
        "--config",
        default=None,
        metavar="FILE",
        help=(
            "Path to a YAML config file listing additional endpoints to scrape. "
            "Each entry needs a 'url' key (relative or absolute) and an optional "
            "'name' key used to select the right parser. The network is "
            "auto-detected from the net= query parameter."
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


def _load_config(path: str) -> list[dict]:
    """Load endpoint configuration from a YAML file.

    Returns a list of endpoint dicts, each with at least a ``url`` key.
    """
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"config file {path!r}: expected a YAML mapping at the top level")
    endpoints = data.get("endpoints", [])
    if not isinstance(endpoints, list):
        raise ValueError(f"config file {path!r}: 'endpoints' must be a list")
    return endpoints


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )

    health_url, base_url = _normalise_urls(args.target_url)
    networks = _parse_networks(args.networks)

    # Load optional extra-endpoint config
    config_endpoints: list[dict] = []
    if args.config:
        try:
            config_endpoints = _load_config(args.config)
            logger.info("loaded %d endpoint(s) from config %s", len(config_endpoints), args.config)
        except Exception as exc:
            raise SystemExit(f"failed to load config file {args.config!r}: {exc}") from exc

    BUILD_INFO.info({
        "version": "0.3.0",
        "target_url": args.target_url,
        "networks": ",".join(networks),
    })

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
        "exporter started  listen=:%d  target=%s  networks=%s  interval=%ds  stale=%ds  config_endpoints=%d",
        args.listen_port,
        base_url,
        ",".join(networks),
        args.scrape_interval,
        args.stale_threshold,
        len(config_endpoints),
    )

    while running:
        scrape_health_check(health_url, args.request_timeout, args.stale_threshold)

        for net in networks:
            scrape_network_stats(base_url, net, args.request_timeout)

        for ep_cfg in config_endpoints:
            scrape_configured_endpoint(base_url, ep_cfg, args.request_timeout)

        # Sleep in small increments so we can react to signals quickly
        deadline = time.monotonic() + args.scrape_interval
        while running and time.monotonic() < deadline:
            time.sleep(0.5)

    logger.info("exporter stopped")
    sys.exit(0)


if __name__ == "__main__":
    main()

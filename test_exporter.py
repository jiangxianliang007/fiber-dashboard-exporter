"""Tests for fiber-dashboard-exporter.

Covers the channel_count_by_state parsing and export logic, including:
- testnet multi-token multi-state
- mainnet single-token multi-state  (acceptance criteria)
- mainnet new token auto-support
- stale metric removal when asset/state disappears between scrapes
"""

import pytest
from prometheus_client import CollectorRegistry, Gauge, generate_latest

import exporter
from exporter import _process_channel_count_by_state, _clear_gauge_for_network


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gauge(registry: CollectorRegistry) -> Gauge:
    """Create a fresh CHANNEL_COUNT_BY_STATE gauge bound to *registry*."""
    return Gauge(
        "fiber_dashboard_channel_count_by_state",
        "Number of channels in each state",
        ["network", "asset", "state"],
        registry=registry,
    )


def _metric_lines(registry: CollectorRegistry) -> list[str]:
    """Return non-comment metric lines from *registry*."""
    output = generate_latest(registry).decode("utf-8")
    return [
        line for line in output.splitlines()
        if line and not line.startswith("#")
    ]


def _channel_state_lines(registry: CollectorRegistry) -> list[str]:
    return [line for line in _metric_lines(registry) if "channel_count_by_state" in line]


# ---------------------------------------------------------------------------
# Tests: _process_channel_count_by_state
# ---------------------------------------------------------------------------

class TestProcessChannelCountByState:

    def setup_method(self):
        self.registry = CollectorRegistry()
        self.gauge = _make_gauge(self.registry)

    # --- acceptance criteria (mainnet) ---

    def test_mainnet_nested_produces_all_states(self):
        """Mainnet nested-dict response exports all three states for ckb."""
        data = {"ckb": {"closed_waiting_onchain_settlement": 2, "open": 23, "closed_cooperative": 1}}
        _process_channel_count_by_state(data, "mainnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        assert len(lines) == 3, f"Expected 3 metric lines, got {len(lines)}: {lines}"
        assert any('asset="ckb",network="mainnet",state="open"} 23.0' in l for l in lines)
        assert any('asset="ckb",network="mainnet",state="closed_cooperative"} 1.0' in l for l in lines)
        assert any('asset="ckb",network="mainnet",state="closed_waiting_onchain_settlement"} 2.0' in l for l in lines)

    # --- testnet multi-token multi-state ---

    def test_testnet_multiple_tokens_and_states(self):
        """Testnet nested-dict with USDI, ckb, RUSD is fully exported."""
        data = {
            "USDI": {"open": 1},
            "ckb": {"closed_cooperative": 662, "open": 1211, "closed_waiting_onchain_settlement": 3},
            "RUSD": {"open": 4, "closed_cooperative": 5},
        }
        _process_channel_count_by_state(data, "testnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        assert len(lines) == 6, f"Expected 6 metric lines, got {len(lines)}: {lines}"
        assert any('asset="USDI",network="testnet",state="open"} 1.0' in l for l in lines)
        assert any('asset="ckb",network="testnet",state="open"} 1211.0' in l for l in lines)
        assert any('asset="ckb",network="testnet",state="closed_cooperative"} 662.0' in l for l in lines)
        assert any('asset="ckb",network="testnet",state="closed_waiting_onchain_settlement"} 3.0' in l for l in lines)
        assert any('asset="RUSD",network="testnet",state="open"} 4.0' in l for l in lines)
        assert any('asset="RUSD",network="testnet",state="closed_cooperative"} 5.0' in l for l in lines)

    # --- mainnet new token auto-reported ---

    def test_mainnet_new_token_auto_reported(self):
        """When mainnet API adds USDI, it appears without code changes."""
        # Round 1: only ckb
        data_r1 = {"ckb": {"open": 23, "closed_cooperative": 1}}
        _process_channel_count_by_state(data_r1, "mainnet", self.gauge)
        lines_r1 = _channel_state_lines(self.registry)
        assert all("USDI" not in l for l in lines_r1), "USDI should not exist yet"

        # Round 2: ckb + new USDI token
        data_r2 = {"ckb": {"open": 25, "closed_cooperative": 2}, "USDI": {"open": 10}}
        _process_channel_count_by_state(data_r2, "mainnet", self.gauge)
        lines_r2 = _channel_state_lines(self.registry)
        assert any('asset="USDI",network="mainnet",state="open"} 10.0' in l for l in lines_r2)
        assert any('asset="ckb",network="mainnet",state="open"} 25.0' in l for l in lines_r2)
        assert len(lines_r2) == 3

    # --- stale metric removal ---

    def test_stale_state_removed_after_disappearing(self):
        """A state present in round 1 but absent in round 2 must not linger."""
        # Round 1: ckb has closed_waiting_onchain_settlement
        data_r1 = {"ckb": {"open": 23, "closed_cooperative": 1, "closed_waiting_onchain_settlement": 2}}
        _process_channel_count_by_state(data_r1, "mainnet", self.gauge)

        # Round 2: closed_waiting_onchain_settlement disappears
        data_r2 = {"ckb": {"open": 20, "closed_cooperative": 3}}
        _process_channel_count_by_state(data_r2, "mainnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        assert all("closed_waiting_onchain_settlement" not in l for l in lines), \
            f"Stale state must be removed: {lines}"
        assert len(lines) == 2

    def test_stale_asset_removed_after_disappearing(self):
        """An asset present in round 1 but absent in round 2 must not linger."""
        data_r1 = {"ckb": {"open": 10}, "RUSD": {"open": 5}}
        _process_channel_count_by_state(data_r1, "testnet", self.gauge)

        data_r2 = {"ckb": {"open": 12}}
        _process_channel_count_by_state(data_r2, "testnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        assert all("RUSD" not in l for l in lines), f"Stale asset must be removed: {lines}"
        assert len(lines) == 1

    def test_stale_removal_is_per_network(self):
        """Clearing stale labels for one network must not affect other networks."""
        # Set up both networks
        data_mainnet = {"ckb": {"open": 23}}
        data_testnet = {"ckb": {"open": 1211}, "USDI": {"open": 1}}
        _process_channel_count_by_state(data_mainnet, "mainnet", self.gauge)
        _process_channel_count_by_state(data_testnet, "testnet", self.gauge)

        # Re-scrape only mainnet with updated data
        data_mainnet_r2 = {"ckb": {"open": 25, "closed_cooperative": 1}}
        _process_channel_count_by_state(data_mainnet_r2, "mainnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        mainnet_lines = [l for l in lines if "mainnet" in l]
        testnet_lines = [l for l in lines if "testnet" in l]

        # testnet unchanged
        assert len(testnet_lines) == 2
        assert any('asset="USDI"' in l for l in testnet_lines)

        # mainnet updated
        assert len(mainnet_lines) == 2
        assert any('state="closed_cooperative"} 1.0' in l for l in mainnet_lines)

    # --- edge cases ---

    def test_empty_data_clears_previous_metrics(self):
        """Empty API response should clear all existing metrics for that network."""
        _process_channel_count_by_state({"ckb": {"open": 23}}, "mainnet", self.gauge)
        _process_channel_count_by_state({}, "mainnet", self.gauge)

        lines = [l for l in _channel_state_lines(self.registry) if "mainnet" in l]
        assert lines == [], f"Expected no mainnet lines after empty response: {lines}"

    def test_asset_with_empty_states_dict(self):
        """An asset whose states dict is empty should produce no metrics."""
        data = {"ckb": {}, "USDI": {"open": 5}}
        _process_channel_count_by_state(data, "testnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        assert len(lines) == 1
        assert any('asset="USDI"' in l for l in lines)

    def test_flat_format_uses_unknown_asset(self):
        """Flat format (legacy/unknown API shape) uses asset='unknown'."""
        data = {"open": 100, "closed_cooperative": 10}
        _process_channel_count_by_state(data, "mainnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        assert all('asset="unknown"' in l for l in lines)
        assert len(lines) == 2

    def test_list_of_dicts_format(self):
        """List-of-dicts format is handled correctly."""
        data = [
            {"asset": "ckb", "state": "open", "count": 50},
            {"asset": "ckb", "state": "closed_cooperative", "count": 5},
        ]
        _process_channel_count_by_state(data, "testnet", self.gauge)

        lines = _channel_state_lines(self.registry)
        assert len(lines) == 2
        assert any('asset="ckb",network="testnet",state="open"} 50.0' in l for l in lines)
        assert any('asset="ckb",network="testnet",state="closed_cooperative"} 5.0' in l for l in lines)


# ---------------------------------------------------------------------------
# Tests: _clear_gauge_for_network
# ---------------------------------------------------------------------------

class TestClearGaugeForNetwork:

    def setup_method(self):
        self.registry = CollectorRegistry()
        self.gauge = _make_gauge(self.registry)

    def test_clears_only_matching_network(self):
        self.gauge.labels(network="mainnet", asset="ckb", state="open").set(10)
        self.gauge.labels(network="testnet", asset="ckb", state="open").set(20)

        _clear_gauge_for_network(self.gauge, "mainnet")

        lines = _channel_state_lines(self.registry)
        assert all("mainnet" not in l for l in lines)
        assert any("testnet" in l for l in lines)

    def test_clears_all_label_combinations_for_network(self):
        for state in ("open", "closed_cooperative", "closed_waiting_onchain_settlement"):
            self.gauge.labels(network="mainnet", asset="ckb", state=state).set(1)
        self.gauge.labels(network="mainnet", asset="USDI", state="open").set(5)

        _clear_gauge_for_network(self.gauge, "mainnet")

        lines = _channel_state_lines(self.registry)
        assert lines == []

    def test_noop_when_network_has_no_metrics(self):
        self.gauge.labels(network="testnet", asset="ckb", state="open").set(10)
        _clear_gauge_for_network(self.gauge, "mainnet")  # nothing to clear
        lines = _channel_state_lines(self.registry)
        assert len(lines) == 1

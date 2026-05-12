"""
results.py
Typed result dataclasses for each test type.
Handles serialization to JSON and local storage.
Ready for InfluxDB writer to be bolted on later.
"""

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, List


# ── Per-test result types ──────────────────────────────────

@dataclass
class ThroughputResult:
    tx_mbps: float                     # Transmit throughput
    rx_mbps: float                     # Receive throughput (bidir)
    retransmits: int                   # TCP retransmits (congestion signal)
    parallel_streams: int
    duration_sec: int
    protocol: str
    raw: Optional[dict] = None         # Full iPerf3 JSON for debugging


@dataclass
class LatencyResult:
    rtt_min_ms: float
    rtt_avg_ms: float
    rtt_max_ms: float
    rtt_mdev_ms: float                 # Standard deviation
    packet_loss_pct: float
    packets_sent: int
    packets_received: int


@dataclass
class LatencyUnderLoadResult:
    idle_rtt_avg_ms: float             # Measured before load test (baseline)
    loaded_rtt_avg_ms: float           # Measured DURING throughput saturation
    delta_ms: float                    # Bufferbloat indicator — idle vs loaded
    loaded_packet_loss_pct: float
    mtr_hops: List[dict]               # Per-hop latency from mtr run under load


@dataclass
class JitterResult:
    jitter_ms: float
    packet_loss_pct: float
    packets_sent: int
    packets_received: int
    latency_avg_ms: float


@dataclass
class MTUResult:
    effective_mtu_bytes: int           # Largest packet size that succeeded
    fragmentation_detected: bool


# ── Top-level test run result ──────────────────────────────

@dataclass
class PathTestResult:
    result_id: str                     # UUID for this result record
    path_id: str
    path_label: str
    source_agent_id: str
    destination_agent_id: str
    source_host: str
    destination_host: str
    timestamp_utc: str                 # ISO 8601
    duration_total_sec: float          # Wall time for entire path test
    success: bool
    error: Optional[str] = None        # Set if test failed

    # Individual test results — None if that test wasn't run for this path
    throughput: Optional[ThroughputResult] = None
    latency: Optional[LatencyResult] = None
    latency_under_load: Optional[LatencyUnderLoadResult] = None
    jitter: Optional[JitterResult] = None
    mtu: Optional[MTUResult] = None


# ── Result store ───────────────────────────────────────────

class ResultStore:
    """
    Writes results to a local JSON file per day.
    Acts as a buffer until InfluxDB is wired in.
    Each line in the file is a self-contained JSON record (JSONL format).
    """

    def __init__(self, results_dir: str):
        self.results_dir = results_dir
        os.makedirs(results_dir, exist_ok=True)

    def _today_file(self) -> str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return os.path.join(self.results_dir, f"results_{date_str}.jsonl")

    def save(self, result: PathTestResult):
        record = asdict(result)
        with open(self._today_file(), "a") as f:
            f.write(json.dumps(record) + "\n")

    def load_today(self) -> List[dict]:
        fpath = self._today_file()
        if not os.path.exists(fpath):
            return []
        results = []
        with open(fpath, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    results.append(json.loads(line))
        return results

    def load_file(self, date_str: str) -> List[dict]:
        """Load results for a specific date (YYYY-MM-DD)."""
        fpath = os.path.join(self.results_dir, f"results_{date_str}.jsonl")
        if not os.path.exists(fpath):
            return []
        results = []
        with open(fpath, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    results.append(json.loads(line))
        return results

    def list_result_files(self) -> List[str]:
        return sorted([
            f for f in os.listdir(self.results_dir)
            if f.startswith("results_") and f.endswith(".jsonl")
        ])


def make_result_id() -> str:
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

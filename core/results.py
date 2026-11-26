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
    tx_mbps: Optional[float]           # Transmit throughput — None if this direction didn't upload
    rx_mbps: Optional[float]           # Receive throughput — None if this direction didn't download
    retransmits: int                   # TCP retransmits (congestion signal)
    parallel_streams: int
    duration_sec: int
    protocol: str
    direction: str = "upload"          # upload | download | bidir
    raw: Optional[dict] = None         # Full iPerf3 JSON for debugging


def _iperf3_flow_mbps(received: Optional[dict], sent: Optional[dict]) -> Optional[float]:
    """Rate of one iPerf3 TCP flow in Mbps, or None if it moved no data.

    Prefers the receiver's figure (what actually arrived). The sender's
    figure counts bytes written into socket buffers, so it overstates a flow
    that stalled. Falls back to the sender only when there is no receiver
    summary. A flow that moved 0 bytes stalled; it was not measured as 0 Mbps.
    """
    summary = received if received else sent
    if not summary:
        return None
    bps = summary.get("bits_per_second") or 0
    if bps <= 0 or summary.get("bytes") == 0:
        return None
    return round(bps / 1_000_000, 2)


def iperf3_tcp_rates(data: dict, direction: str):
    """(tx_mbps, rx_mbps, retransmits) from an iPerf3 TCP client's -J output.

    The direction that wasn't tested is None, and so is a direction whose
    flow stalled and moved no data.

    With --bidir, end.sum_sent/sum_received describe only the forward
    (client→server) flow. The reverse flow is in sum_*_bidir_reverse.
    Records from before 2.3.3 took RX from sum_received, which is the
    forward flow, so their bidir RX was wrong.
    """
    end = data.get("end") or {}
    sum_sent = end.get("sum_sent") or ((end.get("streams") or [{}])[0].get("sender") or {})
    sum_received = end.get("sum_received") or {}
    retransmits = sum_sent.get("retransmits", 0) or 0
    forward = _iperf3_flow_mbps(sum_received, sum_sent)

    if direction == "download":
        # -R: the one flow runs server→client; sum_received is what the
        # local agent itself received.
        return None, forward, retransmits
    if direction == "bidir":
        reverse = _iperf3_flow_mbps(end.get("sum_received_bidir_reverse"),
                                    end.get("sum_sent_bidir_reverse"))
        return forward, reverse, retransmits
    return forward, None, retransmits


def _repair_throughput_entry(t: dict) -> dict:
    """Recompute stored rates from the saved iPerf3 JSON so older records
    graph the same way new ones do: the right bidir RX, and None instead of
    0 for a stalled direction."""
    raw = t.get("raw")
    if isinstance(raw, dict) and raw.get("end") and "error" not in raw:
        tx, rx, _ = iperf3_tcp_rates(raw, t.get("direction") or "upload")
        t["tx_mbps"], t["rx_mbps"] = tx, rx
    else:
        # No raw JSON to recompute from: a 0 can only be a stall.
        for k in ("tx_mbps", "rx_mbps"):
            if t.get(k) == 0:
                t[k] = None
    return t


def repair_record(record: dict) -> dict:
    """Fix up values that older code stored wrongly, in place."""
    t = record.get("throughput")
    if isinstance(t, list):
        for entry in t:
            if isinstance(entry, dict):
                _repair_throughput_entry(entry)
    elif isinstance(t, dict):
        _repair_throughput_entry(t)
    j = record.get("jitter")
    # Before 2.3.3 a jitter run where no UDP packet arrived was stored as
    # 0 ms jitter, which reads as a perfect link.
    if isinstance(j, dict) and j.get("packets_sent") and j.get("packets_received", 1) <= 0:
        record["jitter"] = None
    # Likewise MTR hops that never replied were stored with 0.0 timings.
    lul = record.get("latency_under_load")
    if isinstance(lul, dict):
        for h in lul.get("mtr_hops") or []:
            if isinstance(h, dict) and (h.get("loss_pct") or 0) >= 100:
                for k in ("avg_ms", "best_ms", "worst_ms", "stddev_ms"):
                    h[k] = None
    return record


def measured_rtt(latency: Optional[dict], key: str = "rtt_avg_ms") -> Optional[float]:
    """RTT from a serialized LatencyResult, or None if nothing was measured.

    Runs with 100% loss store rtt_*_ms=None; records written before that
    change stored 0 instead, which is also treated as not measured.
    """
    if not latency:
        return None
    val = latency.get(key)
    if val is None:
        return None
    if latency.get("packets_received") == 0 or (latency.get("packet_loss_pct") or 0) >= 100:
        return None
    return val


@dataclass
class LatencyResult:
    # All four are None when no probe got a reply (100% loss) — nothing was
    # measured, and a 0 would read as a perfect link.
    rtt_min_ms: Optional[float]
    rtt_avg_ms: Optional[float]
    rtt_max_ms: Optional[float]
    rtt_mdev_ms: Optional[float]       # Standard deviation
    packet_loss_pct: float
    packets_sent: int
    packets_received: int
    # "icmp" (ping) or "tcp" (handshake timing — web_server destinations that
    # filter ICMP). TCP RTTs run slightly higher, so keep them apart in trends.
    method: str = "icmp"
    tcp_port: Optional[int] = None


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


# ── Traceroute result ─────────────────────────────────────

@dataclass
class TracerouteHop:
    hop:        int
    ip:         Optional[str]
    hostname:   Optional[str]
    rtt_ms:     List[float]
    responding: bool

    @property
    def rtt_avg_ms(self) -> Optional[float]:
        if not self.rtt_ms:
            return None
        return round(sum(self.rtt_ms) / len(self.rtt_ms), 3)

    @property
    def display_host(self) -> str:
        if self.hostname and self.hostname != self.ip:
            return self.hostname
        return self.ip or '*'


@dataclass
class TracerouteResult:
    direction:   str
    source_host: str
    dest_host:   str
    hops:        List[TracerouteHop] = field(default_factory=list)
    completed:   bool = False
    error:       Optional[str] = None


# ── Segment result (one hop pair in a multi-hop path) ─────

@dataclass
class SegmentResult:
    segment_index: int                 # 0 = source→first_hop, 1 = source→second_hop, etc.
    source_agent_id: str
    destination_agent_id: str
    source_host: str
    destination_host: str
    # Test results for this segment
    latency: Optional[LatencyResult] = None
    mtu: Optional[MTUResult] = None
    # Full tests only available on source→final_destination segment
    throughput: Optional[ThroughputResult] = None
    latency_under_load: Optional[LatencyUnderLoadResult] = None
    jitter: Optional[JitterResult] = None
    error: Optional[str] = None
    # IP the controller resolved destination_host to and tested (None when
    # the source agent resolved it itself — resolve_on: agent).
    destination_addr: Optional[str] = None


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
    # The destination's test host as configured (IP or hostname) and the IP
    # it resolved to on the controller for this run. destination_host above
    # is the management host. destination_addr is None when the source agent
    # resolved the name itself (resolve_on: agent). Older records lack both.
    destination_test_host: Optional[str] = None
    destination_addr: Optional[str] = None

    # Individual test results — None/empty if that test wasn't run for this path.
    # throughput is a list because a path can run multiple directions
    # (upload/download/bidir) as separate iPerf3 invocations in one test cycle.
    throughput: List[ThroughputResult] = field(default_factory=list)
    latency: Optional[LatencyResult] = None
    latency_under_load: Optional[LatencyUnderLoadResult] = None
    jitter: Optional[JitterResult] = None
    mtu: Optional[MTUResult] = None

    # Multi-hop segment results — populated for paths with intermediate hops
    # Each entry is source→hop_n results. Empty list for simple 2-agent paths.
    segments: List[SegmentResult] = field(default_factory=list)

    # Traceroute — forward always, reverse if destination supports SSH
    traceroute_forward: Optional[TracerouteResult] = None
    traceroute_reverse: Optional[TracerouteResult] = None


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
        # Inject computed properties into traceroute hops (not included by asdict)
        for direction in ("traceroute_forward", "traceroute_reverse"):
            tr = record.get(direction)
            if tr and tr.get("hops"):
                for hop in tr["hops"]:
                    rtt_ms = hop.get("rtt_ms") or []
                    hop["rtt_avg_ms"]   = round(sum(rtt_ms) / len(rtt_ms), 3) if rtt_ms else None
                    ip = hop.get("ip")
                    hn = hop.get("hostname")
                    hop["display_host"] = (hn if hn and hn != ip else ip) or "*"
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
                    results.append(repair_record(json.loads(line)))
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
                    results.append(repair_record(json.loads(line)))
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

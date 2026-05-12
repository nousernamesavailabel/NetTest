"""
runner_throughput.py
Runs iPerf3 throughput tests between two agents via SSH.
"""

import json
import logging
import time

from core.ssh_manager import SSHManager
from core.results import ThroughputResult
from core.config_loader import ThroughputParams

logger = logging.getLogger(__name__)




def _start_iperf3_server(ssh, port: int, label: str = "") -> None:
    """Kill any existing iPerf3, wait for port to free, start persistent server."""
    ssh.run("pkill -9 -f iperf3 2>/dev/null || true", timeout=10)
    time.sleep(0.5)
    for _ in range(16):
        check = ssh.run(
            f"ss -tlnp 2>/dev/null | grep ':{port} ' || echo FREE",
            timeout=5
        )
        if "FREE" in check or f":{port}" not in check:
            break
        time.sleep(0.5)
    ssh.run_background(f"iperf3 -s -p {port} -D")
    time.sleep(1.5)
    for _ in range(6):
        check = ssh.run(
            f"ss -tlnp 2>/dev/null | grep ':{port} ' || echo NOT_READY",
            timeout=5
        )
        if "NOT_READY" not in check and f":{port}" in check:
            logger.debug(f"iPerf3 server confirmed listening on port {port}")
            return
        time.sleep(0.5)
    logger.warning(f"iPerf3 server may not be ready on port {port} — proceeding anyway")

class ThroughputRunner:

    def __init__(self, params: ThroughputParams):
        self.params = params

    def run(self, src_ssh: SSHManager, dst_ssh: SSHManager,
            dst_host: str) -> ThroughputResult:
        p = self.params

        logger.info(f"  Starting iPerf3 server on {dst_host}:{p.iperf3_port}...")
        self._start_server(dst_ssh, p.iperf3_port)
        time.sleep(1)

        cmd = self._build_client_command(dst_host, p)
        logger.info(f"  Running {p.parallel_streams}-stream TCP test for {p.duration_sec}s "
                    f"({'bidirectional' if p.bidirectional else 'upload only'})...")
        logger.info(f"  Please wait {p.duration_sec}s for test to complete...")

        timeout_sec = p.duration_sec + 30
        try:
            raw_output = src_ssh.run(cmd, timeout=timeout_sec)
        except Exception as e:
            raise RuntimeError(f"iPerf3 client failed: {e}")

        return self._parse_output(raw_output, p)

    def _start_server(self, dst_ssh: SSHManager, port: int):
        # Extra kill pass before starting — ensures any daemon from
        # a previous test (e.g. jitter) is fully gone before we start
        dst_ssh.run("pkill -9 -f iperf3 2>/dev/null || true", timeout=10)
        time.sleep(1.0)
        _start_iperf3_server(dst_ssh, port)

    def _build_client_command(self, dst_host: str, p: ThroughputParams) -> str:
        cmd_parts = [
            "iperf3",
            f"-c {dst_host}",
            f"-p {p.iperf3_port}",
            f"-t {p.duration_sec}",
            f"-P {p.parallel_streams}",
            "-J",
            "--connect-timeout 5000",
        ]
        if p.bidirectional:
            cmd_parts.append("--bidir")
        if p.protocol == "udp":
            cmd_parts.append("-u")
        return " ".join(cmd_parts)

    def _parse_output(self, raw_output: str, p: ThroughputParams) -> ThroughputResult:
        json_start = raw_output.find("{")
        if json_start == -1:
            raise ValueError(
                f"iPerf3 produced no JSON output — is iperf3 installed on the agent?\n"
                f"Raw output: {raw_output[:300]}"
            )

        try:
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(raw_output[json_start:])
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse iPerf3 output: {e}")

        if "error" in data:
            raise RuntimeError(f"iPerf3 error: {data['error']}")

        end         = data.get("end", {})
        sum_sent    = end.get("sum_sent", end.get("streams", [{}])[0].get("sender", {}))
        tx_bps      = sum_sent.get("bits_per_second", 0)
        tx_mbps     = round(tx_bps / 1_000_000, 2)
        retransmits = sum_sent.get("retransmits", 0)

        sum_received = end.get("sum_received", {})
        rx_bps  = sum_received.get("bits_per_second", 0)
        rx_mbps = round(rx_bps / 1_000_000, 2) if rx_bps else tx_mbps

        retr_note = f"  ({retransmits} retransmits — possible congestion)" if retransmits > 10 else \
                    f"  ({retransmits} retransmits)" if retransmits else ""
        logger.info(f"  Throughput: TX {tx_mbps} Mbps  RX {rx_mbps} Mbps{retr_note}")

        return ThroughputResult(
            tx_mbps=tx_mbps,
            rx_mbps=rx_mbps,
            retransmits=retransmits,
            parallel_streams=p.parallel_streams,
            duration_sec=p.duration_sec,
            protocol=p.protocol,
            raw=data,
        )

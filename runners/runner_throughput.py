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

    def run(self, src_ssh: SSHManager, dst_ssh,
            dst_host: str,
            server_managed: bool = True,
            port_override: int = None,
            busy_retry_seconds: int = 0,
            direction: str = "upload") -> ThroughputResult:
        p    = self.params
        port = port_override if port_override else p.iperf3_port

        if server_managed:
            logger.info(f"  Starting iPerf3 server on {dst_host}:{port}...")
            self._start_server(dst_ssh, port)
            time.sleep(1)
        else:
            logger.info(f"  Using assumed-running iPerf3 server on {dst_host}:{port}...")

        # Bidirectional requires server-side control — not possible for agent_not_installed
        effective_direction = direction
        if direction == "bidir" and not server_managed:
            logger.info("  Note: bidirectional disabled for Agent Not Installed "
                        "(no server-side control) — running upload only")
            effective_direction = "upload"
        cmd = self._build_client_command(dst_host, p, port, direction=effective_direction)
        dir_label = {"upload": "upload only", "download": "download only",
                     "bidir": "bidirectional"}.get(effective_direction, effective_direction)
        logger.info(f"  Running {p.parallel_streams}-stream TCP test for {p.duration_sec}s "
                    f"({dir_label})...")
        logger.info(f"  Please wait {p.duration_sec}s for test to complete...")

        timeout_sec = p.duration_sec + 30
        # Retry loop for agent_not_installed — server may be busy with another client
        raw_output = None
        deadline   = time.time() + (busy_retry_seconds if not server_managed else 0)
        attempt    = 0
        while True:
            attempt += 1
            try:
                raw_output = src_ssh.run(cmd, timeout=timeout_sec)
            except Exception as e:
                raise RuntimeError(f"iPerf3 client failed: {e}")
            # Check if server was busy
            if "server is busy" in raw_output.lower() and time.time() < deadline:
                logger.info(f"  iPerf3 server busy — retrying in 1s "
                            f"(attempt {attempt}, {int(deadline - time.time())}s remaining)...")
                time.sleep(1)
                continue
            break

        # Log interval lines and final sender/receiver summary
        import re as _re
        for line in raw_output.splitlines():
            line = line.strip()
            if not line: continue
            # Interval lines: [  5]  0.00-1.00 sec  X MBytes  Y Mbits/sec
            if _re.search(r'\[\s*\d+\]\s+[\d.]+\-[\d.]+\s+sec\s+[\d.]+\s+\S+\s+[\d.]+\s+\S+bits', line):
                m = _re.search(
                    r'([\d.]+-[\d.]+)\s+sec\s+[\d.]+\s+\S+\s+([\d.]+\s+\S+bits/sec)'
                    r'(?:\s+(\d+))?',
                    line
                )
                if m:
                    retr = f"  retr={m.group(3)}" if m.group(3) and m.group(3) != '0' else ""
                    logger.info(f"    [{m.group(1)}s] {m.group(2)}{retr}")
            # Final summary lines (contain "sender" or "receiver" AND bitrate)
            elif _re.search(r'bits/sec', line) and                  (_re.search(r'sender', line) or _re.search(r'receiver', line)):
                logger.info(f"    {line}")

        return self._parse_output(raw_output, p, effective_direction)



    def _start_server(self, dst_ssh: SSHManager, port: int):
        # Two-pass kill: name-based then port-based to catch zombie daemons
        dst_ssh.run("pkill -9 -f iperf3 2>/dev/null || true", timeout=10)
        time.sleep(0.5)
        dst_ssh.run(
            f"fuser -k {port}/tcp 2>/dev/null || true; "
            f"fuser -k {port}/udp 2>/dev/null || true",
            timeout=10
        )
        time.sleep(1.0)
        _start_iperf3_server(dst_ssh, port)

    def _build_client_command(self, dst_host: str, p: ThroughputParams,
                              port: int = None,
                              direction: str = "upload") -> str:
        port = port or p.iperf3_port
        cmd_parts = [
            "iperf3",
            f"-c {dst_host}",
            f"-p {port}",
            f"-t {p.duration_sec}",
            f"-P {p.parallel_streams}",
            "-J",
            "--connect-timeout 5000",
        ]
        if direction == "bidir":
            cmd_parts.append("--bidir")
        elif direction == "download":
            cmd_parts.append("-R")
        if p.protocol == "udp":
            cmd_parts.append("-u")
        return " ".join(cmd_parts)

    def _parse_output(self, raw_output: str, p: ThroughputParams,
                       direction: str = "upload") -> ThroughputResult:
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
        sent_mbps   = round(sum_sent.get("bits_per_second", 0) / 1_000_000, 2)
        # Retransmits are inherently a sender-side stat, regardless of direction.
        retransmits = sum_sent.get("retransmits", 0)

        sum_received = end.get("sum_received", {})
        rx_bps       = sum_received.get("bits_per_second", 0)
        recv_mbps    = round(rx_bps / 1_000_000, 2) if rx_bps else None

        # Only report a number for the direction that was actually tested —
        # mirroring it onto the untested direction (an earlier "symmetric link
        # estimate") reads as if both directions were measured, which is
        # misleading. The untested direction is None (not 0 — 0 would read as
        # "measured zero throughput", a real failure state, not "not tested").
        if direction == "download":
            # -R: the local agent is the receiver of the one flow. sum_sent
            # reports the remote server's send rate (fallback only), sum_received
            # is what the local agent itself measured receiving — the real "RX".
            rx_mbps = recv_mbps if recv_mbps is not None else sent_mbps
            tx_mbps = None
        elif direction == "upload":
            tx_mbps = sent_mbps
            rx_mbps = None
        else:  # bidir — both directions genuinely ran; fall back to tx only if
               # iperf3's JSON is missing sum_received (parsing edge case, not
               # an untested direction).
            tx_mbps = sent_mbps
            rx_mbps = recv_mbps if recv_mbps is not None else tx_mbps

        retr_note = f"  ({retransmits} retransmits — possible congestion)" if retransmits > 10 else \
                    f"  ({retransmits} retransmits)" if retransmits else ""
        tx_str = f"{tx_mbps} Mbps" if tx_mbps is not None else "—"
        rx_str = f"{rx_mbps} Mbps" if rx_mbps is not None else "—"
        logger.info(f"  Throughput: TX {tx_str}  RX {rx_str}{retr_note}")

        return ThroughputResult(
            tx_mbps=tx_mbps,
            rx_mbps=rx_mbps,
            retransmits=retransmits,
            parallel_streams=p.parallel_streams,
            duration_sec=p.duration_sec,
            protocol=p.protocol,
            direction=direction,
            raw=data,
        )

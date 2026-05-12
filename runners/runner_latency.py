"""
runner_latency.py
Latency, jitter, latency-under-load, and MTU test runners.
"""

import re
import json
import logging
import time

from core.ssh_manager import SSHManager
from core.results import LatencyResult, JitterResult, LatencyUnderLoadResult
from core.config_loader import LatencyParams, JitterParams, LatencyUnderLoadParams

logger = logging.getLogger(__name__)



# ── iPerf3 server management ───────────────────────────────

def _start_iperf3_server(ssh, port: int, label: str = "",
                         udp: bool = False) -> None:
    """
    Kill any existing iPerf3, wait for port to be free,
    start a persistent server, verify it is actually listening.
    udp=True adds extra settle time since UDP binding is slower.
    """
    # Kill everything iperf3-related
    ssh.run("pkill -9 -f iperf3 2>/dev/null || true", timeout=10)
    time.sleep(0.5)

    # Wait for port to be fully released (up to 8 seconds)
    for _ in range(16):
        check = ssh.run(
            f"ss -tlnp 2>/dev/null | grep ':{port} ' || echo FREE",
            timeout=5
        )
        if "FREE" in check or f":{port}" not in check:
            break
        time.sleep(0.5)

    # Start server in daemon mode — no --one-off so it survives connection issues
    ssh.run_background(f"iperf3 -s -p {port} -D")

    # Verify it is actually listening before returning
    settle = 2.0 if udp else 1.5
    time.sleep(settle)
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

# ── Latency (ping) ─────────────────────────────────────────

class LatencyRunner:

    def __init__(self, params: LatencyParams):
        self.params = params

    def run(self, src_ssh: SSHManager, dst_host: str) -> LatencyResult:
        p = self.params
        interval_sec = p.interval_ms / 1000.0
        cmd = (
            f"ping -c {p.packet_count} "
            f"-i {interval_sec:.3f} "
            f"-s {p.packet_size_bytes} "
            f"-W 2 "
            f"{dst_host}"
        )
        timeout_sec = int(p.packet_count * interval_sec) + 30
        output = src_ssh.run(cmd, timeout=timeout_sec)
        return self._parse_ping(output, p.packet_count)

    def _parse_ping(self, output: str, expected_count: int) -> LatencyResult:
        stats_match = re.search(
            r"(\d+) packets transmitted, (\d+) received,.*?([\d.]+)% packet loss",
            output
        )
        if not stats_match:
            raise ValueError(
                f"Could not read ping results — unexpected output:\n{output[:400]}"
            )

        sent     = int(stats_match.group(1))
        received = int(stats_match.group(2))
        loss_pct = float(stats_match.group(3))

        rtt_match = re.search(
            r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms",
            output
        )
        if not rtt_match:
            logger.warning(f"  No RTT data returned — all {sent} packets lost")
            return LatencyResult(
                rtt_min_ms=0, rtt_avg_ms=0, rtt_max_ms=0, rtt_mdev_ms=0,
                packet_loss_pct=100.0,
                packets_sent=sent, packets_received=0,
            )

        rtt_min  = float(rtt_match.group(1))
        rtt_avg  = float(rtt_match.group(2))
        rtt_max  = float(rtt_match.group(3))
        rtt_mdev = float(rtt_match.group(4))

        loss_note = f"  ⚠ {loss_pct}% packet loss" if loss_pct > 0 else ""
        logger.info(f"  Latency result: avg={rtt_avg}ms  min={rtt_min}ms  "
                    f"max={rtt_max}ms  loss={loss_pct}%{loss_note}")

        return LatencyResult(
            rtt_min_ms=rtt_min,
            rtt_avg_ms=rtt_avg,
            rtt_max_ms=rtt_max,
            rtt_mdev_ms=rtt_mdev,
            packet_loss_pct=loss_pct,
            packets_sent=sent,
            packets_received=received,
        )


# ── Jitter (iPerf3 UDP) ────────────────────────────────────

class JitterRunner:

    def __init__(self, params: JitterParams):
        self.params = params

    def run(self, src_ssh: SSHManager, dst_ssh: SSHManager, dst_host: str) -> JitterResult:
        p = self.params

        logger.info(f"  Starting iPerf3 UDP server on {dst_host}:{p.iperf3_port}...")
        _start_iperf3_server(dst_ssh, p.iperf3_port, label=dst_host, udp=True)

        duration_sec = max(10, int(p.packet_count * p.interval_ms / 1000))
        logger.info(f"  Sending UDP stream for {duration_sec}s "
                    f"at {p.bandwidth_kbps} Kbps...")

        cmd = (
            f"iperf3 -c {dst_host} -p {p.iperf3_port} "
            f"-u "
            f"-b {p.bandwidth_kbps}K "
            f"-l {p.packet_size_bytes} "
            f"-t {duration_sec} "
            f"-J"
        )
        output = src_ssh.run(cmd, timeout=duration_sec + 30)
        return self._parse_output(output)

    def _parse_output(self, raw_output: str) -> JitterResult:
        json_start = raw_output.find("{")
        if json_start == -1:
            raise ValueError(
                f"iPerf3 UDP produced no JSON. Raw output:\n{raw_output[:300]}\n"
                f"Is iperf3 installed? Run: sudo apt install iperf3"
            )

        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(raw_output[json_start:])
        if "error" in data:
            raise RuntimeError(f"iPerf3 UDP error: {data['error']}")

        end     = data.get("end", {})
        udp_sum = end.get("sum", {})

        jitter_ms   = round(udp_sum.get("jitter_ms", 0), 3)
        lost        = udp_sum.get("lost_packets", 0)
        sent        = udp_sum.get("packets", 0)
        received    = sent - lost
        loss_pct    = round((lost / sent * 100) if sent > 0 else 0, 2)
        latency_avg = round(
            data.get("intervals", [{}])[-1]
            .get("sum", {})
            .get("seconds", 0) * 1000 / 2, 2
        ) if data.get("intervals") else 0

        loss_note = f"  ⚠ {loss_pct}% packet loss" if loss_pct > 1 else ""
        logger.info(f"  Jitter result: {jitter_ms}ms  "
                    f"sent={sent}  received={received}  loss={loss_pct}%{loss_note}")

        return JitterResult(
            jitter_ms=jitter_ms,
            packet_loss_pct=loss_pct,
            packets_sent=sent,
            packets_received=received,
            latency_avg_ms=latency_avg,
        )


# ── Latency Under Load ─────────────────────────────────────

class LatencyUnderLoadRunner:

    def __init__(self, params: LatencyUnderLoadParams, iperf3_port: int = 5201,
                 iperf3_streams: int = 8, iperf3_duration: int = 60):
        self.params = params
        self.iperf3_port    = iperf3_port
        self.iperf3_streams = iperf3_streams
        self.iperf3_duration = iperf3_duration

    def run(self, src_ssh: SSHManager, dst_ssh: SSHManager,
            dst_host: str) -> LatencyUnderLoadResult:
        p = self.params

        # Phase 1: idle baseline
        logger.info(f"  Phase 1/4: Measuring idle baseline latency to {dst_host}...")
        idle_ping_cmd = (
            f"ping -c {p.ping_count} "
            f"-i {p.ping_interval_ms / 1000:.3f} "
            f"{dst_host}"
        )
        idle_output = src_ssh.run(idle_ping_cmd, timeout=p.ping_count * 2 + 15)
        idle_result = _parse_ping_avg(idle_output)
        logger.info(f"  Baseline latency: {idle_result}ms")

        # Phase 2: saturate link
        logger.info(f"  Phase 2/4: Saturating link with {self.iperf3_streams}-stream "
                    f"iPerf3 for {self.iperf3_duration}s...")
        _start_iperf3_server(dst_ssh, self.iperf3_port, label=dst_host)
        src_ssh.run_background(
            f"iperf3 -c {dst_host} -p {self.iperf3_port} "
            f"-P {self.iperf3_streams} -t {self.iperf3_duration}"
        )
        time.sleep(3)

        # Phase 3: latency under load
        logger.info(f"  Phase 3/4: Measuring latency while link is saturated...")
        loaded_ping_cmd = (
            f"ping -c {p.ping_count} "
            f"-i {p.ping_interval_ms / 1000:.3f} "
            f"{dst_host}"
        )
        loaded_output = src_ssh.run(loaded_ping_cmd, timeout=p.ping_count * 2 + 15)
        loaded_result = _parse_ping_avg(loaded_output)
        loaded_loss   = _parse_ping_loss(loaded_output)
        logger.info(f"  Loaded latency: {loaded_result}ms")

        # Phase 4: MTR hop breakdown
        logger.info(f"  Phase 4/4: Running MTR hop trace ({p.mtr_cycles} cycles)...")
        mtr_output = src_ssh.run(
            f"mtr --report --report-cycles {p.mtr_cycles} --json {dst_host}",
            timeout=p.mtr_cycles * 3 + 30
        )
        mtr_hops = _parse_mtr(mtr_output)
        if mtr_hops:
            logger.info(f"  MTR traced {len(mtr_hops)} hop(s)")

        # Cleanup
        src_ssh.kill_background("iperf3")
        dst_ssh.kill_background("iperf3")
        logger.info(f"  Saturation load stopped")

        delta = round(loaded_result - idle_result, 3)
        sign  = "+" if delta >= 0 else ""
        severity = ""
        if abs(delta) > 100:  severity = "  ⚠ severe bufferbloat"
        elif abs(delta) > 30: severity = "  ⚠ bufferbloat detected"

        logger.info(f"  Latency under load result: "
                    f"idle={idle_result}ms  loaded={loaded_result}ms  "
                    f"delta={sign}{delta}ms{severity}")

        return LatencyUnderLoadResult(
            idle_rtt_avg_ms=idle_result,
            loaded_rtt_avg_ms=loaded_result,
            delta_ms=delta,
            loaded_packet_loss_pct=loaded_loss,
            mtr_hops=mtr_hops,
        )


# ── MTU Discovery ──────────────────────────────────────────

class MTURunner:

    def __init__(self, max_size: int = 9000, min_size: int = 576, step: int = 10):
        self.max_size = max_size
        self.min_size = min_size
        self.step     = step

    def run(self, src_ssh: SSHManager, dst_host: str):
        from core.results import MTUResult

        # Calculate how many probes binary search will need
        import math
        probes = math.ceil(math.log2(self.max_size - self.min_size + 1))
        logger.info(f"  Probing MTU via binary search "
                    f"({self.min_size}–{self.max_size} bytes, ~{probes} probes)...")

        effective_mtu = self._binary_search(src_ssh, dst_host)
        fragmentation = effective_mtu < 1500

        if fragmentation:
            logger.warning(f"  MTU result: {effective_mtu} bytes  "
                           f"⚠ below standard 1500 — fragmentation likely on this path")
        else:
            logger.info(f"  MTU result: {effective_mtu} bytes — no fragmentation detected")

        return MTUResult(
            effective_mtu_bytes=effective_mtu,
            fragmentation_detected=fragmentation,
        )

    def _probe(self, src_ssh: SSHManager, dst_host: str, size: int) -> bool:
        payload = size - 28
        if payload < 0:
            return False
        cmd = (
            f"ping -c 3 -M do -s {payload} -W 1 {dst_host} "
            "&& printf '\\n__NETTEST_PING_OK__\\n' "
            "|| printf '\\n__NETTEST_PING_FAIL__\\n'"
        )
        try:
            output = src_ssh.run(cmd, timeout=15)
            return "__NETTEST_PING_OK__" in output and "0% packet loss" in output
        except Exception:
            return False

    def _binary_search(self, src_ssh: SSHManager, dst_host: str) -> int:
        lo, hi = self.min_size, self.max_size
        result = self.min_size
        probe_num = 0

        while lo <= hi:
            mid = (lo + hi) // 2
            probe_num += 1
            success = self._probe(src_ssh, dst_host, mid)
            status = "pass ✓" if success else "fail ✗"
            logger.info(f"  MTU probe #{probe_num}: {mid} bytes — {status} "
                        f"(range narrowed to {lo}–{hi})")
            if success:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1

        return result


# ── Parse helpers ──────────────────────────────────────────

def _parse_ping_avg(output: str) -> float:
    match = re.search(
        r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms", output
    )
    return float(match.group(1)) if match else 0.0


def _parse_ping_loss(output: str) -> float:
    match = re.search(r"([\d.]+)% packet loss", output)
    return float(match.group(1)) if match else 0.0


def _parse_mtr(output: str) -> list:
    json_start = output.find("{")
    if json_start == -1:
        return []
    try:
        data = json.loads(output[json_start:])
        hops = []
        for hub in data.get("report", {}).get("hubs", []):
            hops.append({
                "hop":       hub.get("count"),
                "host":      hub.get("host"),
                "loss_pct":  hub.get("Loss%", 0),
                "avg_ms":    hub.get("Avg", 0),
                "best_ms":   hub.get("Best", 0),
                "worst_ms":  hub.get("Wrst", 0),
                "stddev_ms": hub.get("StDev", 0),
            })
        return hops
    except Exception as e:
        logger.warning(f"  Could not parse MTR output: {e}")
        return []

"""
runner_latency.py
Latency, jitter, latency-under-load, and MTU test runners.
"""

import re
import json
import logging
import time

from core.ssh_manager import SSHManager, guard_iperf3_client, iperf3_error_text
from core.results import LatencyResult, JitterResult, LatencyUnderLoadResult
from core.config_loader import LatencyParams, JitterParams, LatencyUnderLoadParams

logger = logging.getLogger(__name__)


def _strip_escapes(text: str) -> str:
    """Strip terminal escape sequences from SSH command output.
    Handles shell integration markers (OSC 3008 etc), CSI, and OSC sequences.
    """
    # Shell integration sequences like ]3008;... or \]3008;...
    text = re.sub(r'[\\]?]\d+;[^\n]*', '', text)
    # OSC sequences: ESC ] ... BEL
    text = re.sub('\x1b][^\x07\x1b]*\x07', '', text)
    # CSI sequences: ESC [ ... letter
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
    # Remaining ESC
    text = re.sub(r'\x1b.', '', text)
    return text.replace('\r', '')




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
        # Log summary lines only (skip per-packet ICMP replies)
        for line in _strip_escapes(output).splitlines():
            line = line.strip()
            if line and ('packet' in line or 'rtt' in line or '---' in line):
                logger.info(f"    {line}")
        return self._parse_ping(output, p.packet_count)

    def _parse_ping(self, output: str, expected_count: int) -> LatencyResult:
        output = _strip_escapes(output)
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

    def run(self, src_ssh: SSHManager, dst_ssh,
            dst_host: str,
            server_managed: bool = True,
            port_override: int = None,
            busy_retry_seconds: int = 0) -> JitterResult:
        p    = self.params
        port = port_override if port_override else p.iperf3_port

        if server_managed:
            logger.info(f"  Starting iPerf3 UDP server on {dst_host}:{port}...")
            _start_iperf3_server(dst_ssh, port, label=dst_host, udp=True)
        else:
            logger.info(f"  Using assumed-running iPerf3 server on {dst_host}:{port}...")

        duration_sec = max(10, int(p.packet_count * p.interval_ms / 1000))
        logger.info(f"  Sending UDP stream for {duration_sec}s "
                    f"at {p.bandwidth_kbps} Kbps...")

        cmd = (
            f"iperf3 -c {dst_host} -p {port} "
            f"-u "
            f"-b {p.bandwidth_kbps}K "
            f"-l {p.packet_size_bytes} "
            f"-t {duration_sec} "
            f"-J"
        )
        # iperf3 -J is silent until it exits — see guard_iperf3_client().
        cmd, timeout_sec, limit_sec = guard_iperf3_client(cmd, duration_sec)
        # Retry loop for agent_not_installed
        deadline = time.time() + (busy_retry_seconds if not server_managed else 0)
        attempt  = 0
        while True:
            attempt += 1
            output = src_ssh.run(cmd, timeout=timeout_sec)
            if "server is busy" in output.lower() and time.time() < deadline:
                logger.info(f"  iPerf3 server busy — retrying in 1s "
                            f"(attempt {attempt}, {int(deadline - time.time())}s remaining)...")
                time.sleep(1)
                continue
            break
        # Log jitter summary from JSON result only
        # (raw output is too noisy with OSC sequences and JSON fragments)
        # Will log after parse below
        return self._parse_output(output, limit_sec)

    def _parse_output(self, raw_output: str, limit_sec: int = None) -> JitterResult:
        json_start = raw_output.find("{")
        if json_start == -1:
            raise ValueError(
                f"iPerf3 UDP produced no JSON. Raw output:\n{raw_output[:300]}\n"
                f"Is iperf3 installed? Run: sudo apt install iperf3"
            )

        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(raw_output[json_start:])
        if "error" in data:
            raise RuntimeError(f"iPerf3 UDP error: {iperf3_error_text(data['error'], limit_sec)}")

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
        logger.info(f"  Jitter: {jitter_ms}ms  sent={sent}  received={received}  "
                    f"loss={loss_pct}%{loss_note}")

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

    def run(self, src_ssh: SSHManager, dst_ssh,
            dst_host: str,
            server_managed: bool = True,
            port_override: int = None,
            busy_retry_seconds: int = 0) -> LatencyUnderLoadResult:
        p = self.params

        # Phase 1: idle baseline
        logger.info(f"  Phase 1/4: Measuring idle baseline latency to {dst_host}...")
        idle_ping_cmd = (
            f"ping -c {p.ping_count} "
            f"-i {p.ping_interval_ms / 1000:.3f} "
            f"{dst_host}"
        )
        idle_output = src_ssh.run(idle_ping_cmd, timeout=p.ping_count * 2 + 15)
        for line in _strip_escapes(idle_output).splitlines():
            line = line.strip()
            if line and ('packet' in line or 'rtt' in line or '---' in line):
                logger.info(f"    {line}")
        idle_result = _parse_ping_avg(idle_output)
        logger.info(f"  Baseline latency: {idle_result}ms")

        # Phase 2: saturate link
        port = port_override if port_override else self.iperf3_port
        logger.info(f"  Phase 2/4: Saturating link with {self.iperf3_streams}-stream "
                    f"iPerf3 for {self.iperf3_duration}s...")
        if server_managed:
            logger.info(f"  Starting iPerf3 server on {dst_host}:{port}...")
            dst_ssh.run("pkill -9 -f iperf3 2>/dev/null || true", timeout=10)
            time.sleep(0.5)
            dst_ssh.run(
                f"fuser -k {port}/tcp 2>/dev/null || true; "
                f"fuser -k {port}/udp 2>/dev/null || true",
                timeout=10
            )
            time.sleep(0.5)
            _start_iperf3_server(dst_ssh, port, label=dst_host)
        else:
            logger.info(f"  Using assumed-running iPerf3 server on {dst_host}:{port}...")
        # For agent_not_installed, retry background iperf3 start if server busy
        # (run a quick probe first to check availability)
        if not server_managed and busy_retry_seconds > 0:
            probe_deadline = time.time() + busy_retry_seconds
            probe_attempt  = 0
            while time.time() < probe_deadline:
                probe_attempt += 1
                probe = src_ssh.run(
                    f"iperf3 -c {dst_host} -p {port} -t 1 -J 2>&1 | head -5",
                    timeout=10
                )
                if "server is busy" not in probe.lower():
                    break
                logger.info(f"  iPerf3 server busy — retrying in 1s "
                            f"(attempt {probe_attempt}, "
                            f"{int(probe_deadline - time.time())}s remaining)...")
                time.sleep(1)
        src_ssh.run_background(
            f"iperf3 -c {dst_host} -p {port} "
            f"-P {self.iperf3_streams} -t {self.iperf3_duration}"
        )
        time.sleep(2)
        bg_log = _strip_escapes(src_ssh.read_background_log())
        if re.search(r'error|unable to connect|no route to host|connection refused',
                     bg_log, re.IGNORECASE):
            raise RuntimeError(
                f"iPerf3 saturation stream never connected — 'loaded' latency "
                f"would be measuring an unloaded link: {bg_log.strip()}"
            )
        time.sleep(1)

        # Phase 3: latency under load
        logger.info(f"  Phase 3/4: Measuring latency while link is saturated...")
        loaded_ping_cmd = (
            f"ping -c {p.ping_count} "
            f"-i {p.ping_interval_ms / 1000:.3f} "
            f"{dst_host}"
        )
        loaded_output = src_ssh.run(loaded_ping_cmd, timeout=p.ping_count * 2 + 15)
        for line in _strip_escapes(loaded_output).splitlines():
            line = line.strip()
            if line and ('packet' in line or 'rtt' in line or '---' in line):
                logger.info(f"    {line}")
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

        # Cleanup — dst_ssh may be None for agent_not_installed destinations
        src_ssh.kill_background("iperf3")
        if dst_ssh is not None:
            dst_ssh.kill_background("iperf3")
            # Also kill by port to handle any lingering UDP sockets
            try:
                dst_ssh.run(
                    f"fuser -k {port}/tcp 2>/dev/null || true; "
                    f"fuser -k {port}/udp 2>/dev/null || true",
                    timeout=10
                )
            except Exception:
                pass
        logger.info(f"  Saturation load stopped")
        # Brief cooldown to let iPerf3 fully release the port before next test
        time.sleep(3)

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
            clean = _strip_escapes(output)
            # Log the RTT line if present
            for line in clean.splitlines():
                line = line.strip()
                if line and ('rtt' in line or 'packet loss' in line):
                    logger.info(f"      {line}")
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
    output = _strip_escapes(output)
    match = re.search(
        r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms", output
    )
    return float(match.group(1)) if match else 0.0


def _parse_ping_loss(output: str) -> float:
    output = _strip_escapes(output)
    match = re.search(r"([\d.]+)% packet loss", output)
    return float(match.group(1)) if match else 0.0


def _parse_mtr(output: str) -> list:
    json_start = output.find("{")
    if json_start == -1:
        return []
    try:
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(output[json_start:])
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

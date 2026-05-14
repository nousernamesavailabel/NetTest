"""
path_tester.py
Orchestrates all test runners for a single path (source → destination).
"""

import logging
import time
from datetime import datetime, timezone

from core.config_loader import ControllerConfig, TestPath
from core.results import PathTestResult, SegmentResult, make_result_id, utc_now_iso
from runners.runner_traceroute import TracerouteRunner
from core.ssh_manager import ssh_connection, SSHConnectionError
from runners.runner_throughput import ThroughputRunner
from runners.runner_latency import (
    LatencyRunner, JitterRunner, LatencyUnderLoadRunner, MTURunner
)

logger = logging.getLogger(__name__)

# Tests supported by svi_adjacent agents (passive ping targets only)
SVI_SUPPORTED_TESTS = {"latency", "mtu", "traceroute"}

TEST_LABELS = {
    "throughput":         "Throughput",
    "latency":            "Latency",
    "latency_under_load": "Latency Under Load",
    "jitter":             "Jitter",
    "mtu":                "MTU Discovery",
    "traceroute":         "Traceroute",
}

class PathTester:

    def __init__(self, config: ControllerConfig):
        self.config = config

    def run_path(self, path: TestPath) -> PathTestResult:
        src_agent = self.config.get_agent(path.source)
        dst_agent = self.config.get_agent(path.destination)
        hop_agents = [self.config.get_agent(h) for h in path.hops]

        if not src_agent or not dst_agent:
            logger.error(f"Cannot run '{path.label}' — unknown agent ID "
                         f"(source='{path.source}', destination='{path.destination}')")
            return self._error_result(path, "Unknown source or destination agent ID")

        missing_hops = [h for h, a in zip(path.hops, hop_agents) if not a]
        if missing_hops:
            logger.error(f"Cannot run '{path.label}' — unknown hop agent ID(s): {missing_hops}")
            return self._error_result(path, f"Unknown hop agent ID(s): {missing_hops}")

        # Filter unsupported tests if destination is svi_adjacent
        if dst_agent.type == "svi_adjacent":
            filtered_tests = []
            for t in path.tests:
                if t in SVI_SUPPORTED_TESTS:
                    filtered_tests.append(t)
                else:
                    logger.warning(
                        f"  Skipping '{TEST_LABELS.get(t, t)}' — destination "
                        f"'{dst_agent.label}' is svi_adjacent (passive ping target only). "
                        f"Supported tests: latency, mtu"
                    )
            from dataclasses import replace as dc_replace
            path = dc_replace(path, tests=filtered_tests)
            if not path.tests:
                logger.error(f"  No supported tests remain for svi_adjacent destination "
                             f"'{dst_agent.label}'")
                return self._error_result(
                    path, f"No supported tests for svi_adjacent agent '{dst_agent.label}'"
                )

        src_ssh_params = self.config.get_ssh_params(src_agent)
        dst_ssh_params = self.config.get_ssh_params(dst_agent)

        result = PathTestResult(
            result_id=make_result_id(),
            path_id=path.id,
            path_label=path.label,
            source_agent_id=src_agent.id,
            destination_agent_id=dst_agent.id,
            source_host=src_agent.host_mgmt_ip,
            destination_host=dst_agent.host_mgmt_ip,
            timestamp_utc=utc_now_iso(),
            duration_total_sec=0,
            success=False,
        )
        # For multi-hop paths, segments holds per-hop results
        # The main result fields hold source→destination (final segment)

        test_labels = [TEST_LABELS.get(t, t) for t in path.tests]
        logger.info(f"")
        logger.info(f"===  {path.label}  ===")
        src_test  = f" → test: {src_agent.host_test_ip}" if src_agent.host_test_ip else ""
        dst_test  = f" → test: {dst_agent.host_test_ip}" if dst_agent.host_test_ip else ""
        logger.info(f"     Source      : {src_agent.label} ({src_agent.host_mgmt_ip}{src_test})")
        if hop_agents:
            for hop in hop_agents:
                hop_test = f" → test: {hop.host_test_ip}" if hop.host_test_ip else ""
                logger.info(f"     Via         : {hop.label} ({hop.host_mgmt_ip}{hop_test})")
        logger.info(f"     Destination : {dst_agent.label} ({dst_agent.host_mgmt_ip}{dst_test})")
        logger.info(f"     Tests       : {', '.join(test_labels)}")

        start = time.monotonic()

        try:
            logger.info(f"Connecting to {src_agent.label} ({src_agent.host_mgmt_ip})...")

            with ssh_connection(**self._to_ssh_kwargs(src_ssh_params)) as src_ssh:

                # ── Intermediate hop segments ──────────────────────
                if hop_agents:
                    logger.info(f"Running {len(hop_agents)} intermediate hop segment(s)...")

                for seg_idx, hop_agent in enumerate(hop_agents):
                    logger.info(f"")
                    logger.info(f"── Segment {seg_idx+1}/{len(hop_agents)+1}: "
                                f"{src_agent.label} → {hop_agent.label} ──")

                    # Determine which tests to run on this segment
                    # Intermediate hops only support latency + mtu
                    seg_tests = [t for t in path.tests
                                 if t in SVI_SUPPORTED_TESTS or
                                 hop_agent.type not in ("svi_adjacent",)]
                    # Always restrict to latency+mtu for intermediate hops
                    seg_tests = [t for t in ("latency", "mtu") if t in path.tests]

                    seg_result = SegmentResult(
                        segment_index=seg_idx,
                        source_agent_id=src_agent.id,
                        destination_agent_id=hop_agent.id,
                        source_host=src_agent.host_mgmt_ip,
                        destination_host=hop_agent.test_host,
                    )

                    for i, test_type in enumerate(seg_tests, 1):
                        label = TEST_LABELS.get(test_type, test_type)
                        logger.info(f"-- Test {i}/{len(seg_tests)}: {label} --")
                        self._run_test(
                            test_type=test_type,
                            result=seg_result,
                            src_ssh=src_ssh,
                            dst_ssh=None,
                            dst_host=hop_agent.test_host,
                        )

                    result.segments.append(seg_result)

                # ── Final segment: source → destination ────────────
                is_svi_dst = dst_agent.type == "svi_adjacent"
                total_segs = len(hop_agents) + 1
                if hop_agents:
                    logger.info(f"")
                    logger.info(f"── Segment {total_segs}/{total_segs}: "
                                f"{src_agent.label} → {dst_agent.label} ──")

                if is_svi_dst:
                    logger.info(f"Destination {dst_agent.label} is svi_adjacent — "
                                f"no SSH needed (ping target only)")
                    for i, test_type in enumerate(path.tests, 1):
                        label = TEST_LABELS.get(test_type, test_type)
                        logger.info(f"")
                        logger.info(f"-- Test {i}/{len(path.tests)}: {label} --")
                        self._run_test(
                            test_type=test_type,
                            result=result,
                            src_ssh=src_ssh,
                            dst_ssh=None,
                            dst_host=dst_agent.test_host,
                        )
                else:
                    dst_ssh_params = self.config.get_ssh_params(dst_agent)
                    logger.info(f"Connecting to {dst_agent.label} ({dst_agent.host_mgmt_ip})...")
                    with ssh_connection(**self._to_ssh_kwargs(dst_ssh_params)) as dst_ssh:
                        logger.info(f"Both endpoints connected — beginning "
                                    f"{len(path.tests)} test(s)")
                        for i, test_type in enumerate(path.tests, 1):
                            label = TEST_LABELS.get(test_type, test_type)
                            logger.info(f"")
                            logger.info(f"-- Test {i}/{len(path.tests)}: {label} --")
                            self._run_test(
                                test_type=test_type,
                                result=result,
                                src_ssh=src_ssh,
                                dst_ssh=dst_ssh,
                                dst_host=dst_agent.test_host,
                            )

            result.success = True

        except SSHConnectionError as e:
            logger.error(f"")
            logger.error(f"SSH connection failed for '{path.label}'")
            logger.error(f"  Detail  : {e}")
            logger.error(f"  Check   : agent is reachable, nettest user exists, key is deployed")
            result.error = f"SSH connection error: {e}"

        except Exception as e:
            logger.error(f"Unexpected error during '{path.label}': {e}", exc_info=True)
            result.error = str(e)

        finally:
            result.duration_total_sec = round(time.monotonic() - start, 2)

        logger.info(f"")
        if result.success:
            logger.info(f"PASSED: {path.label} completed in {result.duration_total_sec}s")
            # Log intermediate segment summaries first
            if result.segments:
                logger.info(f"  Intermediate segments:")
                prev_lat = 0.0
                for seg in result.segments:
                    hop_agent = self.config.get_agent(seg.destination_agent_id)
                    hop_label = hop_agent.label if hop_agent else seg.destination_agent_id
                    if seg.latency:
                        delta = round(seg.latency.rtt_avg_ms - prev_lat, 3)
                        sign = "+" if delta >= 0 else ""
                        logger.info(f"    → {hop_label}: "
                                    f"{seg.latency.rtt_avg_ms}ms avg  "
                                    f"({sign}{delta}ms from prev hop)")
                        prev_lat = seg.latency.rtt_avg_ms
                    if seg.mtu and seg.mtu.fragmentation_detected:
                        logger.info(f"      MTU: {seg.mtu.effective_mtu_bytes} bytes "
                                    f"⚠ fragmentation")
            self._log_summary(result)
        else:
            logger.info(f"FAILED: {path.label} after {result.duration_total_sec}s")
            if result.error:
                logger.info(f"  Reason: {result.error}")
        logger.info(f"")

        return result

    def _run_test(self, test_type: str, result,
                  src_ssh, dst_ssh, dst_host: str):
        """Run a single test type. result can be PathTestResult or SegmentResult."""
        p = self.config.test_params

        try:
            if test_type == "throughput":
                logger.info(f"  Starting iPerf3 server on destination ({dst_host})...")
                logger.info(f"  Running {p.throughput.parallel_streams}-stream TCP throughput "
                            f"for {p.throughput.duration_sec}s "
                            f"({'bidirectional' if p.throughput.bidirectional else 'unidirectional'})")
                runner = ThroughputRunner(p.throughput)
                result.throughput = runner.run(src_ssh, dst_ssh, dst_host)

            elif test_type == "latency":
                logger.info(f"  Pinging {dst_host} — "
                            f"{p.latency.packet_count} packets at "
                            f"{p.latency.interval_ms}ms intervals...")
                runner = LatencyRunner(p.latency)
                result.latency = runner.run(src_ssh, dst_host)

            elif test_type == "latency_under_load":
                logger.info(f"  Phase 1: Measuring idle (baseline) latency to {dst_host}...")
                runner = LatencyUnderLoadRunner(
                    params=p.latency_under_load,
                    iperf3_port=p.throughput.iperf3_port,
                    iperf3_streams=p.throughput.parallel_streams,
                )
                result.latency_under_load = runner.run(src_ssh, dst_ssh, dst_host)

            elif test_type == "jitter":
                logger.info(f"  Sending {p.jitter.packet_count} UDP packets "
                            f"at {p.jitter.bandwidth_kbps} Kbps "
                            f"({p.jitter.packet_size_bytes}B each) to {dst_host}...")
                runner = JitterRunner(p.jitter)
                result.jitter = runner.run(src_ssh, dst_ssh, dst_host)

            elif test_type == "mtu":
                logger.info(f"  Probing path MTU to {dst_host} "
                            f"(range: {p.mtu.min_size}–{p.mtu.max_size} bytes)...")
                runner = MTURunner(
                    max_size=p.mtu.max_size,
                    min_size=p.mtu.min_size,
                    step=p.mtu.step,
                )
                result.mtu = runner.run(src_ssh, dst_host)

            elif test_type == 'traceroute':
                tp = self.config.test_params
                tr_cfg = tp.traceroute
                tr_runner = TracerouteRunner(
                    max_hops=tr_cfg.max_hops,
                    probes=tr_cfg.probes,
                    wait_sec=tr_cfg.wait_sec,
                    resolve_dns=tr_cfg.resolve_dns,
                )
                result.traceroute_forward = tr_runner.run_forward(src_ssh, dst_host)
                if dst_ssh is not None:
                    result.traceroute_reverse = tr_runner.run_reverse(dst_ssh, src_ssh.host)

            else:
                logger.warning(f"  Unknown test type '{test_type}' — skipping")

        except Exception as e:
            label = TEST_LABELS.get(test_type, test_type)
            logger.error(f"  {label} test failed: {e}")
            logger.debug(f"  [{test_type}] traceback", exc_info=True)
            setattr(result, test_type, None)
            existing = result.error or ""
            result.error = f"{existing} | {test_type} failed: {e}".strip(" |")

    def _log_summary(self, result: PathTestResult):
        """Log a clean results summary after a successful path run."""
        logger.info(f"  Results:")
        if result.throughput:
            t = result.throughput
            retr = f"  ({t.retransmits} retransmits)" if t.retransmits else ""
            logger.info(f"    Throughput        : TX {t.tx_mbps} Mbps  /  RX {t.rx_mbps} Mbps{retr}")

        if result.latency:
            l = result.latency
            logger.info(f"    Latency           : avg {l.rtt_avg_ms}ms  max {l.rtt_max_ms}ms  "
                        f"loss {l.packet_loss_pct}%")

        if result.latency_under_load:
            lu = result.latency_under_load
            sign = "+" if lu.delta_ms >= 0 else ""
            severity = ""
            if abs(lu.delta_ms) > 100:  severity = "  ⚠ severe bufferbloat"
            elif abs(lu.delta_ms) > 30: severity = "  ⚠ bufferbloat detected"
            logger.info(f"    Latency under load: idle {lu.idle_rtt_avg_ms}ms  "
                        f"loaded {lu.loaded_rtt_avg_ms}ms  "
                        f"delta {sign}{lu.delta_ms}ms{severity}")

        if result.jitter:
            j = result.jitter
            logger.info(f"    Jitter            : {j.jitter_ms}ms  loss {j.packet_loss_pct}%")

        if result.mtu:
            m = result.mtu
            flag = "  ⚠ fragmentation detected — check tunnel/VPN MTU" if m.fragmentation_detected else ""
            logger.info(f"    MTU               : {m.effective_mtu_bytes} bytes{flag}")

    def _to_ssh_kwargs(self, params: dict) -> dict:
        return {
            "host":     params["host"],
            "username": params["username"],
            "password": params.get("password", ""),
            "key_file": params.get("key_file", ""),
            "port":     params.get("port", 22),
            "timeout":  params.get("timeout", 30),
        }

    def _error_result(self, path: TestPath, error: str) -> PathTestResult:
        return PathTestResult(
            result_id=make_result_id(),
            path_id=path.id,
            path_label=path.label,
            source_agent_id=path.source,
            destination_agent_id=path.destination,
            source_host="unknown",
            destination_host="unknown",
            timestamp_utc=utc_now_iso(),
            duration_total_sec=0,
            success=False,
            error=error,
        )

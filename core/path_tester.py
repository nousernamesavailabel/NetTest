"""
path_tester.py
Orchestrates all test runners for a single path (source → destination).
"""

import logging
import time
from datetime import datetime, timezone

from core.config_loader import ControllerConfig, TestPath
from core.results import PathTestResult, make_result_id, utc_now_iso
from core.ssh_manager import ssh_connection, SSHConnectionError
from runners.runner_throughput import ThroughputRunner
from runners.runner_latency import (
    LatencyRunner, JitterRunner, LatencyUnderLoadRunner, MTURunner
)

logger = logging.getLogger(__name__)

TEST_LABELS = {
    "throughput":         "Throughput",
    "latency":            "Latency",
    "latency_under_load": "Latency Under Load",
    "jitter":             "Jitter",
    "mtu":                "MTU Discovery",
}


class PathTester:

    def __init__(self, config: ControllerConfig):
        self.config = config

    def run_path(self, path: TestPath) -> PathTestResult:
        src_agent = self.config.get_agent(path.source)
        dst_agent = self.config.get_agent(path.destination)

        if not src_agent or not dst_agent:
            logger.error(f"Cannot run '{path.label}' — unknown agent ID "
                         f"(source='{path.source}', destination='{path.destination}')")
            return self._error_result(path, "Unknown source or destination agent ID")

        src_ssh_params = self.config.get_ssh_params(src_agent)
        dst_ssh_params = self.config.get_ssh_params(dst_agent)

        result = PathTestResult(
            result_id=make_result_id(),
            path_id=path.id,
            path_label=path.label,
            source_agent_id=src_agent.id,
            destination_agent_id=dst_agent.id,
            source_host=src_agent.host,
            destination_host=dst_agent.host,
            timestamp_utc=utc_now_iso(),
            duration_total_sec=0,
            success=False,
        )

        test_labels = [TEST_LABELS.get(t, t) for t in path.tests]
        logger.info(f"")
        logger.info(f"===  {path.label}  ===")
        logger.info(f"     Source      : {src_agent.label} ({src_agent.host})")
        logger.info(f"     Destination : {dst_agent.label} ({dst_agent.host})")
        logger.info(f"     Tests       : {', '.join(test_labels)}")

        start = time.monotonic()

        try:
            logger.info(f"Connecting to {src_agent.label} ({src_agent.host})...")
            logger.info(f"Connecting to {dst_agent.label} ({dst_agent.host})...")

            with ssh_connection(**self._to_ssh_kwargs(src_ssh_params)) as src_ssh, \
                 ssh_connection(**self._to_ssh_kwargs(dst_ssh_params)) as dst_ssh:

                logger.info(f"Both agents connected — beginning {len(path.tests)} test(s)")

                for i, test_type in enumerate(path.tests, 1):
                    label = TEST_LABELS.get(test_type, test_type)
                    logger.info(f"")
                    logger.info(f"-- Test {i}/{len(path.tests)}: {label} --")
                    self._run_test(
                        test_type=test_type,
                        result=result,
                        src_ssh=src_ssh,
                        dst_ssh=dst_ssh,
                        dst_host=dst_agent.host,
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
            self._log_summary(result)
        else:
            logger.info(f"FAILED: {path.label} after {result.duration_total_sec}s")
            if result.error:
                logger.info(f"  Reason: {result.error}")
        logger.info(f"")

        return result

    def _run_test(self, test_type: str, result: PathTestResult,
                  src_ssh, dst_ssh, dst_host: str):
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

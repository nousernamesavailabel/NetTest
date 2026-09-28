"""
path_tester.py
Orchestrates all test runners for a single path (source → destination).
"""

import logging
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import List

from core.config_loader import ControllerConfig, TestPath, SWITCH_ROUTER_TESTS, AGENT_NOT_INSTALLED_TESTS
from core.results import PathTestResult, SegmentResult, make_result_id, utc_now_iso
from runners.runner_traceroute import TracerouteRunner
from core.ssh_manager import ssh_connection, SSHConnectionError
from core.host_locks import iperf_hosts_locked
from runners.runner_throughput import ThroughputRunner
from runners.runner_latency import (
    LatencyRunner, JitterRunner, LatencyUnderLoadRunner, MTURunner
)

logger = logging.getLogger(__name__)

# Tests supported by each agent type as destination
# (imported from config_loader for single source of truth)

TEST_LABELS = {
    "throughput":         "Throughput",
    "latency":            "Latency",
    "latency_under_load": "Latency Under Load",
    "jitter":             "Jitter",
    "mtu":                "MTU Discovery",
    "traceroute":         "Traceroute",
}

# Tests that start/kill iPerf3 on their endpoints — serialized per host
IPERF_TESTS = ("throughput", "jitter", "latency_under_load")

class PathTester:

    def __init__(self, config: ControllerConfig):
        self.config = config

    def run_path(self, path: TestPath, abort_event=None) -> PathTestResult:
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

        # Validate source — agent_not_installed cannot be a source
        if src_agent.type == "agent_not_installed":
            logger.error(f"Cannot run '{path.label}' — source agent "
                         f"'{src_agent.label}' is agent_not_installed (no SSH access).")
            return self._error_result(
                path, f"agent_not_installed '{src_agent.label}' cannot be a path source"
            )

        # Filter unsupported tests based on destination type
        from dataclasses import replace as dc_replace
        if dst_agent.type == "switch_router":
            filtered_tests = [t for t in path.tests if t in SWITCH_ROUTER_TESTS]
            for t in path.tests:
                if t not in SWITCH_ROUTER_TESTS:
                    logger.warning(
                        f"  Skipping '{TEST_LABELS.get(t, t)}' — destination "
                        f"'{dst_agent.label}' is Switch/Router (latency/MTU/traceroute only)."
                    )
            path = dc_replace(path, tests=filtered_tests)
            if not path.tests:
                logger.error(f"  No supported tests for Switch/Router destination '{dst_agent.label}'")
                return self._error_result(
                    path, f"No supported tests for Switch/Router agent '{dst_agent.label}'"
                )
        elif dst_agent.type == "agent_not_installed":
            filtered_tests = [t for t in path.tests if t in AGENT_NOT_INSTALLED_TESTS]
            path = dc_replace(path, tests=filtered_tests)
        # agent_installed: all tests supported
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

                    # Intermediate hops only support latency + mtu
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
                            server_managed=False,
                            port_override=None,
                        )

                    result.segments.append(seg_result)

                # ── Final segment: source → destination ────────────
                no_ssh_dst = dst_agent.type in ("switch_router", "agent_not_installed")
                server_managed = dst_agent.type == "agent_installed"
                # Per-agent iPerf3 port for agent_not_installed, else use test_params default
                dst_iperf3_port = (
                    dst_agent.iperf3_port
                    if dst_agent.iperf3_port
                    else self.config.test_params.throughput.iperf3_port
                )

                total_segs = len(hop_agents) + 1
                if hop_agents:
                    logger.info(f"")
                    logger.info(f"── Segment {total_segs}/{total_segs}: "
                                f"{src_agent.label} → {dst_agent.label} ──")

                if no_ssh_dst:
                    if dst_agent.type == "switch_router":
                        logger.info(f"Destination {dst_agent.label} is Switch/Router — "
                                    f"no SSH (ping target only)")
                    else:
                        logger.info(f"Destination {dst_agent.label} is Agent Not Installed — "
                                    f"no SSH, iPerf3 assumed running on port {dst_iperf3_port}")
                    for i, test_type in enumerate(path.tests, 1):
                        if abort_event and abort_event.is_set():
                            logger.warning("⚠ Abort signal received — stopping test run")
                            result.error = (result.error or "") + " | aborted"
                            break
                        label = TEST_LABELS.get(test_type, test_type)
                        logger.info(f"")
                        logger.info(f"-- Test {i}/{len(path.tests)}: {label} --")
                        self._run_test(
                            test_type=test_type,
                            result=result,
                            src_ssh=src_ssh,
                            dst_ssh=None,
                            dst_host=dst_agent.test_host,
                            server_managed=server_managed,
                            port_override=dst_iperf3_port,
                            directions=path.directions,
                            parallel_streams=path.parallel_streams,
                            abort_event=abort_event,
                        )
                    # Retry busy iPerf3 tests
                    retries = getattr(result, "_iperf_retry", [])
                    if retries and not (abort_event and abort_event.is_set()):
                        import time as _t
                        logger.info("")
                        logger.info(f"── Retrying {len(retries)} iPerf3 test(s) "
                                    f"that failed due to busy server ──")
                        _t.sleep(3)
                        for test_type in retries:
                            label = TEST_LABELS.get(test_type, test_type)
                            logger.info(f"")
                            logger.info(f"-- Retry: {label} --")
                            setattr(result, test_type, None)
                            self._clear_test_error(result, test_type)
                            self._run_test(
                                test_type=test_type,
                                result=result,
                                src_ssh=src_ssh,
                                dst_ssh=None,
                                dst_host=dst_agent.test_host,
                                server_managed=server_managed,
                                port_override=dst_iperf3_port,
                                directions=path.directions,
                                parallel_streams=path.parallel_streams,
                                abort_event=abort_event,
                            )
                        result._iperf_retry = []
                else:
                    dst_ssh_params = self.config.get_ssh_params(dst_agent)
                    logger.info(f"Connecting to {dst_agent.label} ({dst_agent.host_mgmt_ip})...")
                    with ssh_connection(**self._to_ssh_kwargs(dst_ssh_params)) as dst_ssh:
                        logger.info(f"Both endpoints connected — beginning "
                                    f"{len(path.tests)} test(s)")
                        for i, test_type in enumerate(path.tests, 1):
                            if abort_event and abort_event.is_set():
                                logger.warning("⚠ Abort signal received — stopping test run")
                                result.error = (result.error or "") + " | aborted"
                                break
                            label = TEST_LABELS.get(test_type, test_type)
                            logger.info(f"")
                            logger.info(f"-- Test {i}/{len(path.tests)}: {label} --")
                            self._run_test(
                                test_type=test_type,
                                result=result,
                                src_ssh=src_ssh,
                                dst_ssh=dst_ssh,
                                dst_host=dst_agent.test_host,
                                server_managed=True,
                                port_override=None,
                                directions=path.directions,
                                parallel_streams=path.parallel_streams,
                                abort_event=abort_event,
                            )
                        # Retry any iPerf3 tests that failed due to busy server
                        retries = getattr(result, "_iperf_retry", [])
                        if retries and not (abort_event and abort_event.is_set()):
                            import time as _t
                            logger.info("")
                            logger.info(f"── Retrying {len(retries)} iPerf3 test(s) "
                                        f"that failed due to busy server ──")
                            _t.sleep(3)
                            for test_type in retries:
                                label = TEST_LABELS.get(test_type, test_type)
                                logger.info(f"")
                                logger.info(f"-- Retry: {label} --")
                                # Clear previous error for this test
                                setattr(result, test_type, None)
                                self._clear_test_error(result, test_type)
                                self._run_test(
                                    test_type=test_type,
                                    result=result,
                                    src_ssh=src_ssh,
                                    dst_ssh=dst_ssh,
                                    dst_host=dst_agent.test_host,
                                    server_managed=True,
                                    port_override=None,
                                    directions=path.directions,
                                    parallel_streams=path.parallel_streams,
                                    abort_event=abort_event,
                                )
                            result._iperf_retry = []

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
            # success only means both endpoints were reached and every test
            # was attempted — result.error lists the tests that failed.
            if result.error:
                failed = [TEST_LABELS.get(t, t) for t in path.tests
                          if f"{t} failed:" in result.error]
                logger.warning(f"COMPLETED WITH ERRORS: {path.label} in "
                               f"{result.duration_total_sec}s — {len(failed)} of "
                               f"{len(path.tests)} test(s) failed"
                               + (f": {', '.join(failed)}" if failed else ""))
                for part in filter(None, (p.strip() for p in result.error.split(" | "))):
                    logger.warning(f"  ✗ {part}")
            else:
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
                  src_ssh, dst_ssh, dst_host: str,
                  server_managed: bool = True,
                  port_override: int = None,
                  directions: List[str] = None,
                  parallel_streams: int = 8,
                  abort_event=None):
        """Run a single test type. result can be PathTestResult or SegmentResult.
        server_managed=False skips iPerf3 server start (agent_not_installed destinations).
        port_override sets the iPerf3 port when not using test_params default.
        directions (throughput only) is per-path: any combination of
        upload | download | bidir, run as separate iPerf3 invocations.
        parallel_streams (throughput and latency_under_load) is per-path —
        the right count depends on the path's own bandwidth/shaping.
        iPerf3 tests first take the per-host lock on both endpoints (see
        core/host_locks.py), waiting out any other path using either host.
        """
        p = self.config.test_params
        directions = directions or ["upload"]
        DIR_LABELS = {"upload": "upload", "download": "download", "bidir": "bidirectional"}

        held = ExitStack()
        try:
            if test_type in IPERF_TESTS:
                held.enter_context(iperf_hosts_locked(
                    [src_ssh.host, dst_ssh.host if dst_ssh is not None else dst_host],
                    abort_event=abort_event,
                ))

            if test_type == "throughput":
                if server_managed:
                    logger.info(f"  Starting iPerf3 server on destination ({dst_host})...")
                else:
                    port = port_override or p.throughput.iperf3_port
                    logger.info(f"  Connecting to assumed-running iPerf3 server "
                                f"on {dst_host}:{port}...")
                logger.info(f"  Running {parallel_streams}-stream TCP throughput "
                            f"for {p.throughput.duration_sec}s each "
                            f"({', '.join(DIR_LABELS.get(d, d) for d in directions)})")
                runner = ThroughputRunner(p.throughput)
                throughput_results = []
                direction_errors = []
                for i, direction in enumerate(directions):
                    dir_label = DIR_LABELS.get(direction, direction)
                    if len(directions) > 1:
                        logger.info(f"  [{i+1}/{len(directions)}] Direction: {dir_label}")
                    try:
                        throughput_results.append(runner.run(
                            src_ssh, dst_ssh, dst_host,
                            server_managed=server_managed,
                            port_override=port_override,
                            busy_retry_seconds=p.iperf3_busy_retry_seconds if not server_managed else 0,
                            direction=direction,
                            parallel_streams=parallel_streams,
                        ))
                    except Exception as e:
                        # Busy server → let the outer handler queue a full
                        # throughput retry. Any other failure only loses this
                        # direction; keep the ones that already succeeded.
                        if "busy" in str(e).lower():
                            raise
                        logger.error(f"  Throughput ({dir_label}) failed: {e}")
                        logger.debug(f"  [throughput/{direction}] traceback", exc_info=True)
                        direction_errors.append(f"{dir_label}: {e}")
                result.throughput = throughput_results
                if direction_errors:
                    existing = result.error or ""
                    result.error = (f"{existing} | throughput failed: "
                                    f"{'; '.join(direction_errors)}").strip(" |")

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
                    iperf3_streams=parallel_streams,
                )
                result.latency_under_load = runner.run(
                    src_ssh, dst_ssh, dst_host,
                    server_managed=server_managed,
                    port_override=port_override,
                    busy_retry_seconds=p.iperf3_busy_retry_seconds if not server_managed else 0,
                )

            elif test_type == "jitter":
                if server_managed:
                    logger.info(f"  Starting iPerf3 UDP server on destination ({dst_host})...")
                else:
                    port = port_override or p.jitter.iperf3_port
                    logger.info(f"  Connecting to assumed-running iPerf3 server "
                                f"on {dst_host}:{port} for UDP jitter...")
                logger.info(f"  Sending {p.jitter.packet_count} UDP packets "
                            f"at {p.jitter.bandwidth_kbps} Kbps "
                            f"({p.jitter.packet_size_bytes}B each) to {dst_host}...")
                runner = JitterRunner(p.jitter)
                result.jitter = runner.run(
                    src_ssh, dst_ssh, dst_host,
                    server_managed=server_managed,
                    port_override=port_override,
                    busy_retry_seconds=p.iperf3_busy_retry_seconds if not server_managed else 0,
                )

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
            err_str = str(e)
            logger.error(f"  {label} test failed: {err_str}")
            logger.debug(f"  [{test_type}] traceback", exc_info=True)
            # throughput is a list (List[ThroughputResult]), not an
            # Optional[X] like the other test types — reset it to match
            # its actual type instead of None.
            setattr(result, test_type, [] if test_type == "throughput" else None)
            existing = result.error or ""
            result.error = f"{existing} | {test_type} failed: {err_str}".strip(" |")
            # Track busy iPerf3 for retry after other tests
            if test_type in ("throughput", "jitter", "latency_under_load") and                "busy" in err_str.lower():
                if not hasattr(result, "_iperf_retry"):
                    result._iperf_retry = []
                result._iperf_retry.append(test_type)
                logger.warning(f"  iPerf3 was busy — will retry {test_type} "
                               f"after remaining tests complete")
        finally:
            held.close()

    @staticmethod
    def _clear_test_error(result, test_type: str):
        """Drop test_type's entry from result.error before it is re-run, so
        the retry's own outcome is the only one recorded."""
        if not result.error:
            return
        parts = [p for p in result.error.split(" | ")
                 if not p.startswith(f"{test_type} failed:")]
        result.error = " | ".join(parts) or None

    def _log_summary(self, result: PathTestResult):
        """Log a clean results summary after a successful path run."""
        logger.info(f"  Results:")
        DIR_LABELS = {"upload": "upload", "download": "download", "bidir": "bidirectional"}
        for t in (result.throughput or []):
            retr = f"  ({t.retransmits} retransmits)" if t.retransmits else ""
            dir_label = DIR_LABELS.get(t.direction, t.direction)
            tx_str = f"{t.tx_mbps} Mbps" if t.tx_mbps is not None else "—"
            rx_str = f"{t.rx_mbps} Mbps" if t.rx_mbps is not None else "—"
            logger.info(f"    Throughput ({dir_label:<11}): TX {tx_str}  /  RX {rx_str}{retr}")

        if result.latency:
            l = result.latency
            if l.packets_received == 0:
                logger.info(f"    Latency           : no replies — "
                            f"all {l.packets_sent} pings lost (100% loss)")
            else:
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

#!/usr/bin/env python3
"""
main.py
Network Test Controller — Entry Point

Usage:
  python main.py                        # Run scheduler (continuous)
  python main.py --run-once             # Run all paths once and exit
  python main.py --run-once --latency   # Run latency/jitter only, once
  python main.py --path branch_a_to_hub # Run a single named path
  python main.py --list-paths           # Print all configured paths
  python main.py --results today        # Print today's results summary
  python main.py --config path/to/cfg   # Use alternate config file
  python main.py --onboard              # Onboard a new agent interactively
  python main.py --onboard --agent-ip 10.5.1.10 --agent-label "Branch E"
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import asdict

from core.config_loader import load_config
from core.results import ResultStore
from core.scheduler import Scheduler


def setup_logging(log_level: str, log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "controller.log")

    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def print_paths(config):
    print(f"\nConfigured paths ({len(config.paths)}):")
    print("-" * 60)
    for p in config.paths:
        src = config.get_agent(p.source)
        dst = config.get_agent(p.destination)
        src_label = src.label if src else p.source
        dst_label = dst.label if dst else p.destination
        print(f"  {p.id}")
        print(f"    {src_label} → {dst_label}")
        print(f"    Tests: {', '.join(p.tests)}")
    print()


def _summarize_error(error: str) -> list[str]:
    """Turn verbose stored errors into operator-friendly one-line summaries."""
    if not error:
        return []

    summaries = []
    for part in re.split(r"\s+\|\s+", error):
        part = part.strip()
        if not part:
            continue

        test_name = None
        if " failed: " in part:
            test_name, part = part.split(" failed: ", 1)

        part = part.split("\nOutput:", 1)[0]
        part = part.split("\n\nCommon causes", 1)[0]
        part = re.sub(r"\s+", " ", part).strip()

        if "Failed to parse iPerf3 JSON: Extra data" in part:
            part = "iPerf3 returned JSON plus trailing shell output; restart any running dashboard/scheduler so the parser fix is loaded"
        elif part.startswith("No JSON found in iPerf3 output"):
            part = "iPerf3 did not return JSON; check server startup, port 5201 reachability, or whether another command consumed the SSH session output"
        elif part.startswith("No JSON in iPerf3 UDP output"):
            part = "UDP iPerf3 did not return JSON; check server startup and port 5201 reachability"
        elif part.startswith("Could not parse ping stats"):
            part = "ping output did not include usable packet statistics; target may be unreachable or SSH command output was interleaved"
        elif "Pattern not detected" in part:
            part = "SSH command prompt was not detected before timeout; remote command may still be running or prompt format changed"

        label = f"{test_name}: " if test_name else ""
        summaries.append(f"{label}{part}")

    return summaries


def print_result_record(r: dict):
    if r.get("error"):
        status = "!" if r.get("success") else "✗"
    else:
        status = "✓" if r.get("success") else "✗"
    ts = r["timestamp_utc"][:19].replace("T", " ")
    print(f"\n{status} [{ts}] {r['path_label']}")

    if r.get("throughput"):
        t = r["throughput"]
        print(f"   Throughput:  TX={t['tx_mbps']} Mbps  RX={t['rx_mbps']} Mbps  "
              f"Retransmits={t['retransmits']}")

    if r.get("latency"):
        l = r["latency"]
        print(f"   Latency:     avg={l['rtt_avg_ms']}ms  max={l['rtt_max_ms']}ms  "
              f"loss={l['packet_loss_pct']}%")

    if r.get("latency_under_load"):
        lu = r["latency_under_load"]
        print(f"   Under load:  idle={lu['idle_rtt_avg_ms']}ms  "
              f"loaded={lu['loaded_rtt_avg_ms']}ms  "
              f"Δ={lu['delta_ms']}ms")

    if r.get("jitter"):
        j = r["jitter"]
        print(f"   Jitter:      {j['jitter_ms']}ms  loss={j['packet_loss_pct']}%")

    if r.get("mtu"):
        m = r["mtu"]
        frag = " ⚠ FRAGMENTATION DETECTED" if m["fragmentation_detected"] else ""
        print(f"   MTU:         {m['effective_mtu_bytes']} bytes{frag}")

    if r.get("error"):
        for idx, summary in enumerate(_summarize_error(r["error"])):
            label = "Issue:" if idx == 0 else "      "
            print(f"   {label:<11}{summary}")


def print_results_summary(store: ResultStore, date_str: str = "today"):
    if date_str == "today":
        records = store.load_today()
    else:
        records = store.load_file(date_str)

    if not records:
        print(f"No results found for '{date_str}'")
        return

    print(f"\nResults summary ({len(records)} records):")
    print("-" * 70)

    for r in records:
        print_result_record(r)


def main():
    parser = argparse.ArgumentParser(
        description="Network Test Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config/config.yaml",
                        help="Path to config file")
    parser.add_argument("--run-once", action="store_true",
                        help="Run all paths once and exit")
    parser.add_argument("--latency", action="store_true",
                        help="With --run-once: only run latency/jitter tests")
    parser.add_argument("--path", metavar="PATH_ID",
                        help="Run a single named path and exit")
    parser.add_argument("--list-paths", action="store_true",
                        help="Print all configured paths and exit")
    parser.add_argument("--results", metavar="DATE",
                        help="Print results summary for DATE (YYYY-MM-DD or 'today')")
    parser.add_argument("--onboard", action="store_true",
                        help="Onboard a new agent interactively")
    parser.add_argument("--agent-ip", metavar="IP",
                        help="Agent IP address (used with --onboard)")
    parser.add_argument("--agent-label", metavar="LABEL",
                        help="Agent label / display name (used with --onboard)")
    parser.add_argument("--agent-id", metavar="ID",
                        help="Agent ID for config.yaml (used with --onboard)")
    parser.add_argument("--agent-type", metavar="TYPE",
                        default="endpoint",
                        help="Agent type: endpoint or svi_adjacent (used with --onboard)")
    parser.add_argument("--admin-user", metavar="USER",
                        help="Admin SSH username on the new agent (used with --onboard)")
    parser.add_argument("--admin-port", metavar="PORT", type=int, default=22,
                        help="SSH port on the new agent (default: 22)")

    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Error: config file not found at {args.config}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

    setup_logging(config.log_level, config.log_dir)
    logger = logging.getLogger("main")
    logger.info(f"Controller starting — {config.name}")

    store = ResultStore(config.results_dir)
    scheduler = Scheduler(config, store)

    # ── CLI modes ──────────────────────────────────────────

    if args.onboard:
        from onboard import onboard_agent
        success = onboard_agent(
            config_path=args.config,
            agent_ip=getattr(args, "agent_ip", None),
            agent_label=getattr(args, "agent_label", None),
            agent_id=getattr(args, "agent_id", None),
            agent_type=getattr(args, "agent_type", "endpoint"),
            admin_user=getattr(args, "admin_user", None),
            admin_port=getattr(args, "admin_port", 22),
        )
        sys.exit(0 if success else 1)

    if args.list_paths:
        print_paths(config)
        sys.exit(0)

    if args.results:
        print_results_summary(store, args.results)
        sys.exit(0)

    if args.path:
        # Run a single path by ID
        path = next((p for p in config.paths if p.id == args.path), None)
        if not path:
            print(f"Error: path '{args.path}' not found. Use --list-paths to see options.")
            sys.exit(1)
        logger.info(f"Running single path: {path.label}")
        from core.path_tester import PathTester
        tester = PathTester(config)
        result = tester.run_path(path)
        store.save(result)
        print("\nPath result:")
        print("-" * 70)
        print_result_record(asdict(result))
        sys.exit(0 if result.success else 1)

    if args.run_once:
        test_filter = ["latency", "jitter"] if args.latency else None
        scheduler.run_once(test_filter=test_filter)
        # Wait briefly for threaded path tests to complete
        time.sleep(2)
        print_results_summary(store, "today")
        sys.exit(0)

    # ── Continuous scheduler mode (default) ───────────────

    def handle_signal(sig, frame):
        logger.info(f"Signal {sig} received — shutting down")
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    scheduler.start()

    logger.info("Controller running. Press Ctrl+C to stop.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()

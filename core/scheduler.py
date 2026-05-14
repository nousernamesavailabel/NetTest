"""
scheduler.py
Drives periodic test execution across all configured paths.

Two schedules run concurrently:
  - Full suite (throughput + all metrics): hourly by default
  - Latency-only (fast, lightweight): every 5 minutes by default

Paths are staggered so they don't all fire simultaneously,
which would itself introduce artificial load on the network.
"""

import logging
import time
import threading
from datetime import datetime
from typing import Callable, List
import pytz

from core.config_loader import ControllerConfig, TestPath
from core.path_tester import PathTester
from core.results import PathTestResult, ResultStore

logger = logging.getLogger(__name__)

# Callback type for result handling (e.g. write to store, push to InfluxDB)
ResultCallback = Callable[[PathTestResult], None]


class Scheduler:

    def __init__(self, config: ControllerConfig, result_store: ResultStore):
        self.config = config
        self.store = result_store
        self.tester = PathTester(config)
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._result_callbacks: List[ResultCallback] = []

        # Always register the local store writer
        self.add_result_callback(self._store_result)

    def add_result_callback(self, cb: ResultCallback):
        """Register a callback to be called with each PathTestResult."""
        self._result_callbacks.append(cb)

    def start(self):
        """Start scheduler threads. Returns immediately (non-blocking)."""
        logger.info(f"Starting scheduler — full suite every "
                    f"{self.config.schedule.full_test_interval_minutes}m, "
                    f"latency-only every "
                    f"{self.config.schedule.latency_only_interval_minutes}m")

        # Full suite thread
        full_thread = threading.Thread(
            target=self._schedule_loop,
            args=(
                self.config.schedule.full_test_interval_minutes * 60,
                None,                  # None = run all tests defined per path
                "full-suite",
            ),
            daemon=True,
            name="scheduler-full",
        )

        # Latency-only thread
        latency_thread = threading.Thread(
            target=self._schedule_loop,
            args=(
                self.config.schedule.latency_only_interval_minutes * 60,
                ["latency", "jitter", "traceroute"], # Only these test types
                "latency-only",
            ),
            daemon=True,
            name="scheduler-latency",
        )

        self._threads = [full_thread, latency_thread]
        for t in self._threads:
            t.start()

    def stop(self):
        """Signal scheduler threads to stop and wait for them."""
        logger.info("Stopping scheduler...")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=10)

    def run_once(self, test_filter: List[str] = None):
        """
        Run all paths immediately, once.
        Useful for manual triggering and testing.
        test_filter: if set, only run these test types regardless of path config.
        """
        logger.info(f"Running all paths immediately (test_filter={test_filter})")
        self._run_all_paths(test_filter)

    # ── Internal ───────────────────────────────────────────

    def _schedule_loop(self, interval_sec: int, test_filter: List[str], label: str):
        """
        Main loop for a schedule tier.
        Waits interval_sec between runs, checks business hours,
        and runs paths with staggering.
        """
        logger.info(f"[{label}] Schedule loop started (interval={interval_sec}s)")

        # Run immediately on first start rather than waiting for first interval
        if not self._stop_event.is_set():
            self._run_all_paths(test_filter, label)

        while not self._stop_event.wait(timeout=interval_sec):
            if self._should_run_now():
                self._run_all_paths(test_filter, label)
            else:
                logger.info(f"[{label}] Outside business hours — skipping run")

        logger.info(f"[{label}] Schedule loop stopped")

    def _should_run_now(self) -> bool:
        sched = self.config.schedule
        if not sched.business_hours_only:
            return True

        tz = pytz.timezone(sched.timezone)
        now = datetime.now(tz)
        start_h, start_m = map(int, sched.business_hours_start.split(":"))
        end_h, end_m     = map(int, sched.business_hours_end.split(":"))
        start_mins = start_h * 60 + start_m
        end_mins   = end_h   * 60 + end_m
        now_mins   = now.hour * 60 + now.minute
        return start_mins <= now_mins <= end_mins

    def _run_all_paths(self, test_filter: List[str] = None, label: str = ""):
        """
        Run all paths sequentially with staggering between each.
        test_filter: if set, only run these test types on each path.
        """
        stagger = self.config.schedule.stagger_seconds
        paths = self.config.paths
        total = len(paths)

        logger.info(f"[{label}] Starting run across {total} paths "
                    f"(stagger={stagger}s between paths)")

        for i, path in enumerate(paths):
            if self._stop_event.is_set():
                break

            # Build an effective path with filtered test types if needed
            effective_path = path
            if test_filter:
                filtered_tests = [t for t in path.tests if t in test_filter]
                if not filtered_tests:
                    logger.debug(f"[{label}] Skipping path {path.id} — no matching tests")
                    continue
                # Create a shallow copy with filtered tests
                from dataclasses import replace
                effective_path = replace(path, tests=filtered_tests)

            # Stagger: wait between paths (not before the first)
            if i > 0 and stagger > 0:
                logger.debug(f"[{label}] Stagger wait {stagger}s before path {path.id}")
                if self._stop_event.wait(timeout=stagger):
                    break

            # Run in a thread so stagger timing isn't blocked by test duration
            thread = threading.Thread(
                target=self._run_path_and_notify,
                args=(effective_path,),
                name=f"path-{path.id}",
                daemon=True,
            )
            thread.start()

        logger.info(f"[{label}] All paths dispatched")

    def _run_path_and_notify(self, path: TestPath):
        """Run a single path test and invoke all registered callbacks."""
        try:
            result = self.tester.run_path(path)
            for cb in self._result_callbacks:
                try:
                    cb(result)
                except Exception as e:
                    logger.error(f"Result callback error: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unhandled error running path {path.id}: {e}", exc_info=True)

    def _store_result(self, result: PathTestResult):
        """Default callback: save result to local JSON store."""
        self.store.save(result)
        logger.debug(f"Result saved: {result.result_id} path={result.path_id}")

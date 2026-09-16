"""
scheduler.py
Drives periodic test execution across all configured paths.

Two schedules run concurrently:
  - Full suite (throughput + all metrics): every 30 minutes by default
  - Latency-only (fast, lightweight): every 15 minutes by default

Both are aligned to wall-clock boundaries (e.g. a 30-minute interval fires
at :00 and :30, a 15-minute interval at :00/:15/:30/:45) rather than
counting from whenever the scheduler process happened to start — so a
service restart doesn't shift the schedule, and the two tiers land on
predictable, shared boundaries (every :00 and :30, since 30 is a multiple
of 15). A per-path lock (see _active_paths) keeps those shared boundaries
from ever running the same path on both tiers at once.

Paths are staggered so they don't all fire simultaneously,
which would itself introduce artificial load on the network.
"""

import logging
import time
import threading
from datetime import datetime, timedelta
from typing import Callable, List
import pytz

from core.config_loader import ControllerConfig, TestPath
from core.path_tester import PathTester
from core.results import PathTestResult, ResultStore

logger = logging.getLogger(__name__)

# Callback type for result handling (e.g. write to store, push to InfluxDB)
ResultCallback = Callable[[PathTestResult], None]


def _seconds_until_next_boundary(interval_minutes: int, tz) -> float:
    """Seconds from now until the next wall-clock boundary that's a multiple
    of interval_minutes past midnight, in the given timezone (e.g. 30 ->
    next :00 or :30; 15 -> next :00/:15/:30/:45)."""
    now = datetime.now(tz)
    minutes_since_midnight = now.hour * 60 + now.minute + now.second / 60
    next_boundary_minutes = (int(minutes_since_midnight // interval_minutes) + 1) * interval_minutes
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_run = midnight + timedelta(minutes=next_boundary_minutes)
    return (next_run - now).total_seconds()


class Scheduler:

    def __init__(self, config: ControllerConfig, result_store: ResultStore):
        self.config = config
        self.store = result_store
        self.tester = PathTester(config)
        self._stop_event = threading.Event()
        self._abort_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._path_threads: List[threading.Thread] = []
        self._path_threads_lock = threading.Lock()
        self._result_callbacks: List[ResultCallback] = []

        # Shared across both schedule tiers — prevents full-suite and
        # latency-only from testing the same path concurrently (they'd
        # otherwise fight over the same destination's iperf3 server/port).
        self._active_paths: set = set()
        self._active_paths_lock = threading.Lock()

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

    def stop(self, path_drain_timeout: float = 60):
        """Signal scheduler threads to stop and wait for them.

        Sets abort_event so in-flight path tests stop *between* sub-tests
        (an already-running SSH/iPerf3 command still runs to completion —
        it isn't interruptible mid-call) rather than being killed outright
        when the process exits. path_drain_timeout bounds how long we'll
        wait for currently-running path tests to wind down before giving up
        (kept comfortably under systemd's default 90s stop timeout).
        """
        logger.info("Stopping scheduler...")
        self._stop_event.set()
        self._abort_event.set()
        for t in self._threads:
            t.join(timeout=10)

        with self._path_threads_lock:
            path_threads = [t for t in self._path_threads if t.is_alive()]
        if path_threads:
            logger.info(f"Waiting up to {path_drain_timeout}s for "
                        f"{len(path_threads)} in-flight path test(s) to finish...")
            deadline = time.monotonic() + path_drain_timeout
            for t in path_threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
            still_running = [t for t in path_threads if t.is_alive()]
            if still_running:
                logger.warning(f"{len(still_running)} path test(s) still running "
                                f"after {path_drain_timeout}s — proceeding with shutdown")

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
        Waits for the next wall-clock boundary that's a multiple of the
        interval (e.g. 30m -> :00/:30, 15m -> :00/:15/:30/:45), checks
        business hours, and runs paths with staggering. Aligning to the
        clock — rather than counting the interval from process start —
        means a scheduler restart doesn't reset the cadence, and the two
        tiers land on shared, predictable boundaries instead of drifting
        into an arbitrary, ever-changing overlap.
        """
        interval_minutes = max(1, interval_sec // 60)
        tz = pytz.timezone(self.config.schedule.timezone)
        logger.info(f"[{label}] Schedule loop started "
                    f"(every {interval_minutes}m, aligned to the wall clock)")

        while not self._stop_event.is_set():
            wait_sec = _seconds_until_next_boundary(interval_minutes, tz)
            logger.debug(f"[{label}] Next run in {wait_sec:.0f}s")
            if self._stop_event.wait(timeout=wait_sec):
                break
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
        tz = pytz.timezone(self.config.schedule.timezone)
        now = datetime.now(tz)
        paths = [p for p in self.config.paths if p.is_active_at(now)]
        skipped = len(self.config.paths) - len(paths)
        total = len(paths)

        logger.info(f"[{label}] Starting run across {total} paths "
                    f"(stagger={stagger}s between paths"
                    + (f", {skipped} skipped — outside their schedule grid" if skipped else "")
                    + ")")

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

            # Reserve this path for the duration of the test — if the other
            # schedule tier is already mid-test on it (their boundaries do
            # coincide, e.g. every :00/:30 for the default 30m/15m tiers),
            # skip rather than run a second, colliding test concurrently.
            with self._active_paths_lock:
                if path.id in self._active_paths:
                    logger.info(f"[{label}] Skipping {path.id} — already being "
                                f"tested by another schedule tier right now")
                    continue
                self._active_paths.add(path.id)

            # Run in a thread so stagger timing isn't blocked by test duration
            thread = threading.Thread(
                target=self._run_path_and_notify,
                args=(effective_path,),
                name=f"path-{path.id}",
                daemon=True,
            )
            with self._path_threads_lock:
                self._path_threads = [t for t in self._path_threads if t.is_alive()]
                self._path_threads.append(thread)
            thread.start()

        logger.info(f"[{label}] All paths dispatched")

    def _run_path_and_notify(self, path: TestPath):
        """Run a single path test and invoke all registered callbacks."""
        try:
            result = self.tester.run_path(path, abort_event=self._abort_event)
            for cb in self._result_callbacks:
                try:
                    cb(result)
                except Exception as e:
                    logger.error(f"Result callback error: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unhandled error running path {path.id}: {e}", exc_info=True)
        finally:
            with self._active_paths_lock:
                self._active_paths.discard(path.id)

    def _store_result(self, result: PathTestResult):
        """Default callback: save result to local JSON store."""
        self.store.save(result)
        logger.debug(f"Result saved: {result.result_id} path={result.path_id}")

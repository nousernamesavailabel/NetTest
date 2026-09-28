"""
host_locks.py
Serializes iPerf3 tests per host, across threads and processes.

Every iPerf3 test starts by running `pkill -9 -f iperf3` on its destination,
which kills every iPerf3 on that box — including another path's server, or
another path's client when the box is that path's source. Two paths sharing
a host (e.g. A→B and B→C) must therefore never run iPerf3 tests at the same
time; even without the pkill they would share port 5201 and each other's
link capacity.

The scheduler (main.py) and the dashboard (gunicorn) are separate processes,
so an in-process threading.Lock isn't enough. flock() on a per-host file
works for both: locks taken through separately opened files conflict even
within the same process.
"""

import fcntl
import logging
import os
import re
import time
from contextlib import ExitStack, contextmanager
from typing import Iterable

logger = logging.getLogger(__name__)

_APP_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_DIR  = os.path.join(_APP_DIR, "run", "locks")
WAIT_LOG_EVERY_SEC = 30


def _lock_path(host: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return os.path.join(LOCK_DIR, f"iperf-{safe}.lock")


def _open_lock_file(host: str):
    """Open (creating if needed) host's lock file so any user can lock it.

    The services run as `nettest`, but the CLI/TUI may be run by an admin
    user, who would otherwise leave behind a lock dir or file the services
    can't use. flock() needs no write access, so the file is opened
    read-only; the dir is made sticky and world-writable (like /tmp) so any
    user can add lock files for new hosts. The chmods only succeed for the
    creator, which is the only one that needs them.
    """
    if not os.path.isdir(LOCK_DIR):
        os.makedirs(LOCK_DIR, exist_ok=True)
        try:
            os.chmod(LOCK_DIR, 0o1777)
        except OSError:
            pass
    fd = os.open(_lock_path(host), os.O_RDONLY | os.O_CREAT, 0o666)
    try:
        os.fchmod(fd, 0o666)
    except OSError:
        pass
    return os.fdopen(fd, "r")


@contextmanager
def _host_lock(host: str, abort_event=None):
    try:
        f = _open_lock_file(host)
    except OSError as e:
        # Never block a test run on the lock itself (e.g. a CLI run as a user
        # that can't write the lock dir) — run unserialized and say so.
        logger.warning(f"  Could not open iPerf3 lock for {host} ({e}) — "
                       f"running without it; concurrent tests on this host may collide")
        yield
        return
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info(f"  Waiting for another test's iPerf3 on {host} to finish...")
            started = time.monotonic()
            next_log = started + WAIT_LOG_EVERY_SEC
            while True:
                if abort_event is not None and abort_event.is_set():
                    raise RuntimeError(f"aborted while waiting for iPerf3 on {host}")
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
                now = time.monotonic()
                if now >= next_log:
                    logger.info(f"  Still waiting for iPerf3 on {host} "
                                f"({int(now - started)}s)...")
                    next_log = now + WAIT_LOG_EVERY_SEC
                time.sleep(0.5)
            logger.info(f"  iPerf3 on {host} is free (waited "
                        f"{time.monotonic() - started:.0f}s)")
        yield
    finally:
        f.close()   # closing the file releases the flock


@contextmanager
def iperf_hosts_locked(hosts: Iterable[str], abort_event=None):
    """Hold the iPerf3 lock for every host in `hosts` for the duration.

    Hosts are locked in sorted order so two paths locking the same pair
    (A→B and B→A) can't deadlock each other.
    """
    with ExitStack() as stack:
        for host in sorted({h for h in hosts if h}):
            stack.enter_context(_host_lock(host, abort_event))
        yield

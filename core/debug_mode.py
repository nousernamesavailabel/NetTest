"""
debug_mode.py
Time-boxed debug logging, shared by the scheduler and the web dashboard.

The two run as separate processes, so the on/off state lives in a small JSON
file (<log_dir>/debug_mode.json) holding an absolute expiry time. Each process
runs a watcher thread (start_watcher) that polls that file and, while debug is
active, sends DEBUG records to its own rotating file:

    logs/debug-scheduler.log
    logs/debug-dashboard.log

Two properties follow from keeping the expiry in the file rather than in a
process timer:
  - a service restart mid-window (e.g. a config save restarting the scheduler)
    resumes debug until the original expiry instead of silently dropping it;
  - expiry needs no coordination — each process just compares its own clock.

Each process writes only its own file so rotation never races between them.
Existing handlers (console, controller.log) stay at the configured level;
only the debug files see DEBUG.
"""

import json
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Dict, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "debug_mode.json"
DURATION_OPTIONS_MIN = (15, 30, 60, 180)
SOURCES = ("scheduler", "dashboard")

_POLL_SECONDS = 5
_MAX_BYTES    = 10 * 1024 * 1024   # per file; with backups a process keeps at most ~30 MB
_BACKUPS      = 2

# Logger levels applied while debug is active. netmiko's DEBUG output is the
# raw channel read/write trace (what prompt detection saw); paramiko at DEBUG
# is packet-level noise, so it stays at INFO (connect/auth events only).
_DEBUG_LEVELS = {
    "netmiko":  logging.DEBUG,
    "paramiko": logging.INFO,
    "werkzeug": logging.WARNING,   # per-request access lines would drown everything
    "urllib3":  logging.WARNING,
}

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_active = False   # set by the watcher in this process; read by root_level()/lib_level()


def resolve_log_dir(log_dir: str) -> str:
    """Relative log_dir values are relative to the app directory, so the
    scheduler and the dashboard agree on it whatever their cwd is."""
    return log_dir if os.path.isabs(log_dir) else os.path.join(_APP_DIR, log_dir)


def log_path(log_dir: str, source: str) -> str:
    if source not in SOURCES:
        raise ValueError(f"unknown log source: {source!r}")
    return os.path.join(resolve_log_dir(log_dir), f"debug-{source}.log")


# ── State file ─────────────────────────────────────────────

def _state_path(log_dir: str) -> str:
    return os.path.join(resolve_log_dir(log_dir), STATE_FILENAME)


def read_state(log_dir: str) -> Optional[dict]:
    """The active session as {started, until, duration_min}, or None if debug
    is off, expired, or the state file is missing/unreadable."""
    try:
        with open(_state_path(log_dir), "r") as f:
            state = json.load(f)
        if float(state["until"]) > time.time():
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_state(log_dir: str, state: dict) -> None:
    path = _state_path(log_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)   # atomic — a watcher never reads a half-written file


def enable(log_dir: str, minutes: int) -> dict:
    """Turn debug on for `minutes` from now (restarts the timer if already on)."""
    if minutes not in DURATION_OPTIONS_MIN:
        raise ValueError(f"duration must be one of {DURATION_OPTIONS_MIN} minutes")
    now = time.time()
    state = {"started": now, "until": now + minutes * 60, "duration_min": minutes}
    _write_state(log_dir, state)
    return state


def disable(log_dir: str) -> None:
    try:
        os.remove(_state_path(log_dir))
    except FileNotFoundError:
        pass


# ── Level helpers for code that sets levels itself ─────────

def is_active() -> bool:
    return _active


def root_level(default: int) -> int:
    """Root logger level to use — DEBUG while debug mode is on, else `default`."""
    return logging.DEBUG if _active else default


def lib_level(name: str, default: int) -> int:
    """Level for a third-party logger — the debug-mode level while it's on,
    else `default`. Lets code that quiets netmiko/paramiko for its own
    purposes avoid muting them mid-debug-session."""
    return _DEBUG_LEVELS.get(name, default) if _active else default


# ── Watcher ────────────────────────────────────────────────

class DebugWatcher:
    def __init__(self, source: str, log_dir: str):
        self.source   = source
        self.log_dir  = log_dir
        self._lock    = threading.Lock()
        self._handler: Optional[RotatingFileHandler] = None
        self._saved: Dict[str, int] = {}
        self._until: Optional[float] = None

    def start(self) -> None:
        threading.Thread(target=self._loop, name=f"debug-watch-{self.source}",
                         daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.sync()
            except Exception:
                logger.warning("Debug-mode watcher error", exc_info=True)
            time.sleep(_POLL_SECONDS)

    def sync(self) -> None:
        """Bring this process in line with the state file. Called by the
        watcher every few seconds, and directly by the dashboard's API so a
        toggle takes effect there immediately."""
        state = read_state(self.log_dir)
        with self._lock:
            if state and self._handler is None:
                self._activate(state)
            elif state and state["until"] != self._until:
                self._until = state["until"]   # timer restarted while already on
                logger.info("Debug mode timer reset — now until %s",
                            time.strftime("%H:%M:%S", time.localtime(self._until)))
            elif not state and self._handler is not None:
                self._deactivate()

    def _activate(self, state: dict) -> None:
        global _active
        path = log_path(self.log_dir, self.source)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
                                      encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] "
            "%(name)s:%(lineno)d: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))

        root = logging.getLogger()
        self._saved = {name: logging.getLogger(name).level
                       for name in ("", *_DEBUG_LEVELS)}
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        for name, level in _DEBUG_LEVELS.items():
            logging.getLogger(name).setLevel(level)

        self._handler = handler
        self._until   = state["until"]
        _active = True
        logger.info("Debug mode ENABLED for %s until %s — writing to %s",
                    self.source, time.strftime("%H:%M:%S", time.localtime(self._until)),
                    os.path.basename(path))

    def _deactivate(self) -> None:
        global _active
        logger.info("Debug mode DISABLED for %s", self.source)   # last line into the debug file
        _active = False
        root = logging.getLogger()
        root.removeHandler(self._handler)
        self._handler.close()
        self._handler = None
        self._until   = None
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)


_watcher: Optional[DebugWatcher] = None


def start_watcher(source: str, log_dir: str) -> DebugWatcher:
    """Start this process's watcher (idempotent). Call once at startup, after
    the process's normal handlers are configured."""
    global _watcher
    if _watcher is None:
        _watcher = DebugWatcher(source, log_dir)
        _watcher.sync()   # pick up a session that's already running (restart mid-window)
        _watcher.start()
    return _watcher


def sync_now() -> None:
    if _watcher is not None:
        _watcher.sync()

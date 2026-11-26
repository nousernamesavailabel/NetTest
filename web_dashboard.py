#!/usr/bin/env python3
"""
web_dashboard.py
Lightweight Flask web server that serves the dashboard UI
and exposes a JSON API backed by the result store.

Usage:
  python web_dashboard.py                  # http://localhost:8080
  python web_dashboard.py --port 9000
  python web_dashboard.py --host 0.0.0.0  # Expose on all interfaces
"""

import argparse
import html as _html
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import collections
import secrets
import threading
import time
import uuid
from dataclasses import replace, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from flask import Flask, jsonify, send_from_directory, send_file, abort, request, Response, session, redirect, url_for, stream_with_context

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config_loader import (load_config, agent_host_errors, parse_thresholds, THRESHOLD_DEFAULTS,
                                parse_branding, Branding, ROLE_ADMIN, ROLE_READONLY, ROLES,
                                RADIUS_ROLE_ATTRIBUTES, normalize_role, NO_SSH_TYPES)
from core.hostaddr import validate_host, resolve_all, is_ip, HostResolutionError
from core.results import ResultStore, measured_rtt
from core.path_tester import PathTester
from core.annotations import AnnotationStore
from core import debug_mode
from core import branding

app = Flask(__name__, static_folder="web/static")
# Browsers don't send the session cookie on cross-site POSTs, so another
# site can't make a logged-in browser start runs or clear results.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

STATIC_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_config_path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config/config.yaml")
_packages_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packages")
os.makedirs(_packages_dir, exist_ok=True)

# ── Module-level init (required for gunicorn) ──────────────
try:
    _config      = load_config(_config_path)
    _store       = ResultStore(_config.results_dir)
    _tester      = PathTester(_config)
    _annotations = AnnotationStore(os.path.join(_config.results_dir, "annotations.json"))
    app.secret_key = _config.auth.session_secret or secrets.token_hex(32)
except Exception as _init_err:
    import traceback
    print(f"FATAL: Failed to initialize — {_init_err}")
    traceback.print_exc()
    _config      = None
    _store       = None
    _tester      = None
    _annotations = None
    app.secret_key = secrets.token_hex(32)

# Timed debug logging (Config → Logging). Started here rather than in main so it
# also runs under gunicorn.
if _config:
    debug_mode.start_watcher("dashboard", _config.log_dir)

# ── Debounced scheduler restart ────────────────────────────
# Config saves/imports can happen in quick succession (e.g. editing
# several paths one at a time) — each restart kills whatever the
# scheduler is mid-test on. Collapsing rapid calls into a single
# restart, issued once things settle, avoids repeatedly aborting
# in-flight scheduled runs.
RESTART_DEBOUNCE_SECONDS = 15
_restart_lock  = threading.Lock()
_restart_timer = None


def _restart_service(name: str, timeout: int = 120):
    """Restart a systemd service through sudo. Returns None on success, or a
    short description of what went wrong.

    -n makes sudo fail at once instead of waiting for a password it can never
    get. The timeout is generous because stopping a service waits for it to
    shut down (the web service waits up to gunicorn's graceful timeout for
    open streams). It doesn't cancel the restart: systemd carries on with it.
    """
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "restart", name],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"didn't finish within {timeout}s"
    except OSError as exc:
        return str(exc)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:300]
        return f"exit code {r.returncode}" + (f": {detail}" if detail else "")
    return None


def _restart_linkstate_scheduler():
    err = _restart_service(branding.SCHEDULER_SERVICE)
    if err:
        logging.getLogger(__name__).error(
            "Couldn't restart %s after a config change (%s). Changes to paths, "
            "agents or the schedule take effect once it restarts: "
            "sudo systemctl restart %s", branding.SCHEDULER_SERVICE, err,
            branding.SCHEDULER_SERVICE)


def schedule_linkstate_restart():
    """Debounced restart of the linkstate scheduler service."""
    global _restart_timer
    with _restart_lock:
        if _restart_timer is not None:
            _restart_timer.cancel()
        _restart_timer = threading.Timer(RESTART_DEBOUNCE_SECONDS, _restart_linkstate_scheduler)
        _restart_timer.daemon = True
        _restart_timer.start()


# ── Login rate limiting ───────────────────────────────────
_login_attempts: dict = collections.defaultdict(collections.deque)
_login_lock = threading.Lock()


def _check_rate_limit(ip: str):
    if not _config: return True, 0
    auth = _config.auth
    now  = time.time()
    with _login_lock:
        attempts = _login_attempts[ip]
        while attempts and now - attempts[0] > auth.login_window_seconds:
            attempts.popleft()
        if len(attempts) >= auth.login_max_attempts:
            unlock_at = attempts[0] + auth.login_lockout_seconds
            if now < unlock_at:
                return False, int(unlock_at - now)
            attempts.clear()
        return True, 0


def _record_attempt(ip: str):
    with _login_lock:
        _login_attempts[ip].append(time.time())


def _session_lifetime() -> timedelta:
    minutes = _config.auth.session_lifetime_minutes if _config else 480
    return timedelta(minutes=max(1, int(minutes or 480)))


@app.before_request
def _apply_session_lifetime():
    # Read per request so a config save/import takes effect without a restart.
    # Without this Flask falls back to its 31-day default for permanent sessions.
    app.permanent_session_lifetime = _session_lifetime()


def _session_expired() -> bool:
    """Absolute expiry measured from login. The cookie's own expiry slides
    forward on every request, so a dashboard left open (polling) would
    otherwise stay logged in forever."""
    login_at = session.get("login_at")
    if not isinstance(login_at, (int, float)):
        return True   # pre-fix session with no login time — make it log in again
    return time.time() - login_at > _session_lifetime().total_seconds()


# ── Account types ─────────────────────────────────────────
# Read-only accounts may use any GET below except the admin-only ones, plus the
# browser speed test (it measures the viewer's own link and saves nothing but
# its result). Everything else that changes state is admin-only. Checked
# centrally so a new write endpoint is admin-only unless listed here.
_READONLY_WRITE_ALLOWED = ("/login", "/logout", "/api/speedtest/")
_ADMIN_ONLY_PREFIXES = (
    "/config", "/api/config", "/api/ssh/", "/api/packages", "/api/https/",
    "/api/update/", "/api/export", "/api/import/", "/api/onboard",
)


def _path_matches(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p if p.endswith("/") else p + "/")
               or (p.endswith("/") and path == p[:-1]) for p in prefixes)


def _current_role():
    """Role of the logged-in session, or None if it must log in again."""
    if not _config or not _config.auth.method:
        return ROLE_ADMIN
    role = session.get("role")
    if role not in ROLES:
        return None   # session from before account types — log in again
    if session.get("auth_source") == "local":
        # Re-read so a demoted or removed local user takes effect immediately.
        user = next((u for u in _config.auth.local_users
                     if u.username == session.get("username")), None)
        return user.role if user else None
    return role


def _is_admin() -> bool:
    return _current_role() == ROLE_ADMIN


@app.before_request
def _enforce_role():
    if not session.get("authenticated") or _current_role() != ROLE_READONLY:
        return None   # admins, and not-logged-in (login_required handles those)
    path = request.path
    if request.method in ("GET", "HEAD", "OPTIONS"):
        blocked = _path_matches(path, _ADMIN_ONLY_PREFIXES)
    else:
        blocked = not _path_matches(path, _READONLY_WRITE_ALLOWED)
    if not blocked:
        return None
    if path.startswith("/api/"):
        return jsonify({"error": "Your account is read-only"}), 403
    return redirect("/")


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _config or not _config.auth.method:
            return f(*args, **kwargs)
        if session.get("authenticated") and (_session_expired() or _current_role() is None):
            session.clear()
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Job tracking ───────────────────────────────────────────
_jobs: dict      = {}
_jobs_lock           = threading.Lock()
_job_logs_lock       = threading.Lock()   # guards _job_log_history
_job_log_history: dict = {}   # job_id -> list of log line strings (last 2000) — the
                               # single source of truth for both history and live tail
_abort_events: dict  = {}     # job_id -> threading.Event
_abort_lock          = threading.Lock()
_MAX_HISTORY_JOBS    = 20     # evict oldest jobs when over this limit
_MAX_HISTORY_LINES   = 2000   # cap per-job log lines kept in memory
_import_cache: dict  = {}     # stores parsed import data server-side (avoids session size limit)
_import_cache_lock   = threading.Lock()


def _quiet_libs(*names: str):
    """Hold noisy third-party loggers at WARNING for job output — except
    while timed debug mode is on, when they run at debug-mode levels."""
    for name in names:
        logging.getLogger(name).setLevel(debug_mode.lib_level(name, logging.WARNING))


# ── Log handler that feeds job history (read by the SSE tail poller) ──
class JobLogHandler(logging.Handler):
    """Attaches to the root logger and appends records to the job's history.

    only_this_thread keeps just the records logged by the thread that built
    the handler — without it, path jobs running side by side (Run All) each
    collect every other job's lines too.
    """
    def __init__(self, job_id: str, only_this_thread: bool = False):
        super().__init__()
        self.job_id = job_id
        self.thread_ident = threading.get_ident() if only_this_thread else None
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        ))

    def emit(self, record):
        if self.thread_ident is not None and record.thread != self.thread_ident:
            return
        line = self.format(record)
        with _job_logs_lock:
            hist = _job_log_history.setdefault(self.job_id, [])
            if len(hist) < _MAX_HISTORY_LINES:
                hist.append(line)


def _run_job(job_id: str, path_id: str, test_filter: List[str] = None,
             direction_filter: List[str] = None, start_delay: float = 0):
    """Execute a test path in a background thread and stream logs via SSE.
    start_delay (Run All's stagger) keeps the job queued, still abortable,
    for that many seconds first."""
    handler = JobLogHandler(job_id, only_this_thread=True)
    handler.setLevel(logging.INFO)

    # Silence noisy third-party loggers
    _quiet_libs("netmiko", "paramiko", "werkzeug")

    root_logger = logging.getLogger()
    root_logger.setLevel(debug_mode.root_level(logging.INFO))
    root_logger.addHandler(handler)

    # Create abort event for this job
    abort_event = threading.Event()
    with _abort_lock:
        _abort_events[job_id] = abort_event

    try:
        if start_delay > 0:
            logging.getLogger(__name__).info(
                f"Queued — starting in {start_delay:.0f}s (Run All staggers paths)")
            if abort_event.wait(timeout=start_delay):
                raise RuntimeError("aborted before starting")

        with _jobs_lock:
            _jobs[job_id]["status"] = "running"

        path = next((p for p in _config.paths if p.id == path_id), None)
        if not path:
            raise ValueError(f"Path '{path_id}' not found in config")

        if test_filter:
            filtered = [t for t in path.tests if t in test_filter]
            path = replace(path, tests=filtered if filtered else path.tests)

        if direction_filter:
            filtered_dirs = [d for d in path.directions if d in direction_filter]
            path = replace(path, directions=filtered_dirs if filtered_dirs else path.directions)

        result = _tester.run_path(path, abort_event=abort_event)
        _store.save(result)

        with _jobs_lock:
            _jobs[job_id]["status"]   = "done"
            _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["success"]  = result.success
            _jobs[job_id]["error"]    = result.error

    except Exception as e:
        with _jobs_lock:
            _jobs[job_id]["status"]   = "error"
            _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["error"]    = str(e)

    finally:
        root_logger.removeHandler(handler)
        with _abort_lock:
            _abort_events.pop(job_id, None)


# ── Helpers ────────────────────────────────────────────────

def _load_records(minutes: int = 1440, path_id: Optional[str] = None) -> List[dict]:
    """Load records from a rolling window ending now (not a calendar-day
    snap) — e.g. minutes=1440 means "the last 24 hours", however that spans
    across today's and yesterday's result files, rather than "today" (which
    could be nearly empty right after midnight even with a full day's worth
    of recent data sitting in yesterday's file)."""
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    cutoff_iso = cutoff.isoformat()

    records = []
    d = cutoff.date()
    while d <= now.date():
        records.extend(_store.load_file(d.strftime("%Y-%m-%d")))
        d += timedelta(days=1)

    records = [r for r in records if r.get("timestamp_utc", "") >= cutoff_iso]

    if path_id:
        records = [r for r in records if r.get("path_id") == path_id]

    # load_file() re-parses from disk on every call (no caching), so these
    # are fresh dicts each time — safe to attach notes/tags in place.
    if _annotations:
        ann = _annotations.get_all()
        for r in records:
            entry = ann.get(r.get("result_id"), {})
            r["notes"] = entry.get("notes", "")
            r["tags"]  = entry.get("tags", [])

    return sorted(records, key=lambda r: r.get("timestamp_utc", ""))


def _throughput_entries(record: dict) -> List[dict]:
    """Normalize a record's throughput field to a list of entry dicts.

    Newer records store a list (one entry per direction run: upload/download/
    bidir). Records saved before that store a single dict. Missing/empty
    either way returns [].
    """
    t = record.get("throughput")
    if not t:
        return []
    return t if isinstance(t, list) else [t]


def _summarise(records: List[dict]) -> dict:
    if not records:
        return {
            "total_runs": 0, "successful": 0, "partial": 0, "failed": 0,
            "avg_latency_ms": None, "avg_throughput_mbps": None,
            "avg_jitter_ms": None, "avg_loss_pct": None,
        }

    # A run with success=True can still have failed tests (listed in its
    # error) — count those as partial, not successful.
    successful  = [r for r in records if r.get("success") and not r.get("error")]
    partial     = [r for r in records if r.get("success") and r.get("error")]
    # Runs with 100% loss have no RTT (None, or 0 in older records) — skip them.
    lat_vals    = [v for r in records if (v := measured_rtt(r.get("latency"))) is not None]
    # A record can hold multiple throughput entries (one per direction run:
    # upload/download/bidir). tx_mbps/rx_mbps are None on whichever entry
    # didn't test that side — excluded here rather than averaged in, so
    # "Avg TX"/"Avg RX" reads as "no data" instead of a misleading zero.
    # Bidir entries have both tx_mbps and rx_mbps set too, but a bidir run's
    # numbers aren't comparable to a dedicated upload/download run (both
    # directions are contending for bandwidth at once) — excluded here so
    # they only ever show up against other bidir data.
    throughput_entries = [t for r in records for t in _throughput_entries(r)]
    tput_vals   = [t["tx_mbps"] for t in throughput_entries if t.get("direction") == "upload"   and t.get("tx_mbps") is not None]
    rx_vals     = [t["rx_mbps"] for t in throughput_entries if t.get("direction") == "download" and t.get("rx_mbps") is not None]
    jitter_vals = [r["jitter"]["jitter_ms"]        for r in records if r.get("jitter")]
    loss_vals   = [r["latency"]["packet_loss_pct"] for r in records if r.get("latency")]

    def avg(lst): return round(sum(lst) / len(lst), 2) if lst else None

    return {
        "total_runs":          len(records),
        "successful":          len(successful),
        "partial":             len(partial),
        "failed":              len(records) - len(successful) - len(partial),
        "avg_latency_ms":      avg(lat_vals),
        "avg_throughput_mbps": avg(tput_vals),
        "avg_throughput_rx_mbps": avg(rx_vals),
        "avg_jitter_ms":       avg(jitter_vals),
        "avg_loss_pct":        avg(loss_vals),
        "last_run":            records[-1]["timestamp_utc"] if records else None,
    }


# ── Read API ───────────────────────────────────────────────

@app.route("/api/summary")
@login_required
def api_summary():
    minutes = int(request.args.get("minutes", 1440))
    path_id = request.args.get("path_id")
    records = _load_records(minutes, path_id)
    return jsonify(_summarise(records))


@app.route("/api/summary/by_path")
@login_required
def api_summary_by_path():
    """Per-path aggregates over the selected window, for the Path Overview
    table — averages rather than just the most recent run, so it agrees with
    the Recent results time selector."""
    minutes = int(request.args.get("minutes", 1440))
    by_path: Dict[str, List[dict]] = {}
    for r in _load_records(minutes):
        by_path.setdefault(r["path_id"], []).append(r)

    def avg(lst): return round(sum(lst) / len(lst), 2) if lst else None

    out = {}
    for pid, recs in by_path.items():
        s = _summarise(recs)
        bb_vals = [r["latency_under_load"]["delta_ms"] for r in recs
                   if r.get("latency_under_load") and r["latency_under_load"].get("delta_ms") is not None]
        mtus = [r["mtu"] for r in recs if r.get("mtu") and r["mtu"].get("effective_mtu_bytes")]

        # Per-hop segment aggregates, keyed by destination agent, in the
        # hop order of the most recent run that had segments.
        seg_lat: Dict[str, List[float]] = {}
        seg_mtu: Dict[str, List[dict]] = {}
        hop_order: List[str] = []
        for r in recs:
            segs = r.get("segments") or []
            if segs:
                hop_order = [sg.get("destination_agent_id") for sg in segs]
            for sg in segs:
                aid = sg.get("destination_agent_id")
                seg_rtt = measured_rtt(sg.get("latency"))
                if seg_rtt is not None:
                    seg_lat.setdefault(aid, []).append(seg_rtt)
                if sg.get("mtu") and sg["mtu"].get("effective_mtu_bytes"):
                    seg_mtu.setdefault(aid, []).append(sg["mtu"])

        def mtu_agg(ms):
            if not ms:
                return None
            return {
                "effective_mtu_bytes":    min(m["effective_mtu_bytes"] for m in ms),
                "fragmentation_detected": any(m.get("fragmentation_detected") for m in ms),
            }

        out[pid] = {
            "total_runs":      s["total_runs"],
            "successful":      s["successful"],
            "partial":         s["partial"],
            "failed":          s["failed"],
            "avg_latency_ms":  s["avg_latency_ms"],
            "avg_tx_mbps":     s["avg_throughput_mbps"],
            "avg_rx_mbps":     s["avg_throughput_rx_mbps"],
            "avg_jitter_ms":   s["avg_jitter_ms"],
            "avg_loss_pct":    s["avg_loss_pct"],
            "avg_bufferbloat_ms": avg(bb_vals),
            "mtu":             mtu_agg(mtus),
            "segments": [
                {
                    "destination_agent_id": aid,
                    "avg_latency_ms":       avg(seg_lat.get(aid, [])),
                    "mtu":                  mtu_agg(seg_mtu.get(aid, [])),
                }
                for aid in hop_order
            ],
        }
    return jsonify(out)


@app.route("/api/paths")
@login_required
def api_paths():
    paths = [
        {
            "id":          p.id,
            "label":       p.label,
            "source":      p.source,
            "hops":        p.hops,
            "destination": p.destination,
            "tests":       p.tests,
            "directions":  p.directions,
            "group":       p.group,
        }
        for p in _config.paths
    ]
    return jsonify(paths)


@app.route("/api/results")
@login_required
def api_results():
    minutes = int(request.args.get("minutes", 1440))
    path_id = request.args.get("path_id")
    limit   = int(request.args.get("limit", 200))
    records = _load_records(minutes, path_id)
    return jsonify(list(reversed(records[-limit:])))


@app.route("/api/results/latest")
@login_required
def api_results_latest():
    records = _load_records(minutes=1440)
    latest  = {}
    for r in records:
        latest[r["path_id"]] = r
    return jsonify(list(latest.values()))


@app.route("/api/timeseries/<metric>")
@login_required
def api_timeseries(metric: str):
    minutes = int(request.args.get("minutes", 1440))
    path_id = request.args.get("path_id")
    records = _load_records(minutes, path_id)

    series_by_path = {}
    for r in records:
        pid = r["path_id"]
        ts  = r["timestamp_utc"]
        val = None

        if metric == "latency":
            val = measured_rtt(r.get("latency"))
        elif metric == "throughput":
            # A record may hold multiple direction entries (upload/download/
            # bidir) — this chart is upload-only, so bidir entries (which
            # also carry a tx_mbps, measured while contending with a
            # simultaneous download) are excluded; they belong on the
            # dedicated bidir chart instead, not mixed in here.
            tx_vals = [t["tx_mbps"] for t in _throughput_entries(r) if t.get("direction") == "upload" and t.get("tx_mbps") is not None]
            if tx_vals:
                val = max(tx_vals)
        elif metric == "throughput_rx":
            rx_vals = [t["rx_mbps"] for t in _throughput_entries(r) if t.get("direction") == "download" and t.get("rx_mbps") is not None]
            if rx_vals:
                val = max(rx_vals)
        elif metric == "throughput_bidir":
            # Only bidir runs genuinely measure both directions at once —
            # plotting tx/rx from separate upload/download runs on one axis
            # would pair values that were never actually concurrent.
            bidir_entries = [t for t in _throughput_entries(r) if t.get("direction") == "bidir"]
            tx_vals = [t["tx_mbps"] for t in bidir_entries if t.get("tx_mbps") is not None]
            rx_vals = [t["rx_mbps"] for t in bidir_entries if t.get("rx_mbps") is not None]
            if tx_vals:
                tx_pid = f"{pid}_tx"
                series_by_path.setdefault(tx_pid, {"path_id": tx_pid, "label": f"{r['path_label']} TX", "points": []})
                series_by_path[tx_pid]["points"].append({"ts": ts, "value": max(tx_vals)})
            if rx_vals:
                rx_pid = f"{pid}_rx"
                series_by_path.setdefault(rx_pid, {"path_id": rx_pid, "label": f"{r['path_label']} RX", "points": []})
                series_by_path[rx_pid]["points"].append({"ts": ts, "value": max(rx_vals)})
            continue
        elif metric == "jitter"      and r.get("jitter"):
            val = r["jitter"]["jitter_ms"]
        elif metric == "loss"        and r.get("latency"):
            val = r["latency"]["packet_loss_pct"]
        elif metric == "bufferbloat" and r.get("latency_under_load"):
            val = r["latency_under_load"]["delta_ms"]

        if val is not None:
            series_by_path.setdefault(pid, {"path_id": pid, "label": r["path_label"], "points": []})
            series_by_path[pid]["points"].append({"ts": ts, "value": val})

    return jsonify(list(series_by_path.values()))


@app.route("/api/traceroute/<path_id>")
@login_required
def api_traceroute(path_id: str):
    """Return most recent traceroute result for a path."""
    records = _load_records(minutes=7 * 1440, path_id=path_id)
    for r in reversed(records):
        if r.get("traceroute_forward"):
            return jsonify({
                "path_id":    path_id,
                "path_label": r.get("path_label", ""),
                "timestamp":  r.get("timestamp_utc", ""),
                "forward":    r["traceroute_forward"],
                "reverse":    r.get("traceroute_reverse"),
            })
    return jsonify({"path_id": path_id, "forward": None, "reverse": None})


@app.route("/api/traceroute/result/<result_id>")
@login_required
def api_traceroute_by_result(result_id: str):
    """Return traceroute for a specific result ID."""
    records = _load_records(minutes=7 * 1440)
    for r in records:
        if r.get("result_id") == result_id:
            if r.get("traceroute_forward"):
                return jsonify({
                    "path_id":    r.get("path_id", ""),
                    "path_label": r.get("path_label", ""),
                    "timestamp":  r.get("timestamp_utc", ""),
                    "forward":    r["traceroute_forward"],
                    "reverse":    r.get("traceroute_reverse"),
                })
            return jsonify({"path_id": r.get("path_id",""), "forward": None, "reverse": None})
    return jsonify({"error": "Result not found"}), 404


# ── Lightweight per-file run index (cached) ────────────────
# Result files can grow to tens of MB — each line carries the full raw
# iPerf3 JSON blob. Re-parsing every file on every /api/runs or
# /api/result call (as the naive version did) got slower as results/
# grew. Cache the small picker-relevant fields per file, keyed by the
# file's mtime, so only files that changed since the last request (in
# practice just today's, still-growing file) get re-parsed.
_light_cache_lock = threading.Lock()
_light_cache: dict = {}   # filename -> {"mtime": float, "records": [light dict, ...]}


def _lightweight_record(r: dict) -> dict:
    return {
        "result_id":              r.get("result_id"),
        "path_id":                r.get("path_id"),
        "path_label":             r.get("path_label"),
        "timestamp_utc":          r.get("timestamp_utc"),
        "success":                r.get("success"),
        "source_host":            r.get("source_host"),
        "destination_host":       r.get("destination_host"),
        "destination_test_host":  r.get("destination_test_host"),
        "destination_addr":       r.get("destination_addr"),
        # Per-test presence flags — let the compare picker filter to only
        # runs that actually exercised a given test (a path's config can
        # change over time, or an individual test can be run on demand).
        "has_latency":            bool(r.get("latency")),
        "has_throughput":         bool(r.get("throughput")),
        "has_jitter":             bool(r.get("jitter")),
        "has_latency_under_load": bool(r.get("latency_under_load")),
        "has_mtu":                bool(r.get("mtu")),
        "has_traceroute":         bool((r.get("traceroute_forward") or {}).get("hops")),
        "has_traceroute_reverse": bool((r.get("traceroute_reverse") or {}).get("hops")),
    }


def _get_light_records(fname: str) -> List[dict]:
    fpath = os.path.join(_config.results_dir, fname)
    try:
        mtime = os.path.getmtime(fpath)
    except OSError:
        return []

    with _light_cache_lock:
        cached = _light_cache.get(fname)
        if cached and cached["mtime"] == mtime:
            return cached["records"]

    date_str = fname[len("results_"):-len(".jsonl")]
    light = [_lightweight_record(r) for r in _store.load_file(date_str)]

    with _light_cache_lock:
        _light_cache[fname] = {"mtime": mtime, "records": light}
    return light


def _find_record(result_id: str) -> Optional[dict]:
    """Locate a single result record by ID.

    First does a cheap pass over the cached lightweight index (newest
    file first) to find which single file contains this result_id, then
    parses only that one file — instead of fully parsing every file
    (raw iPerf3 blobs included) until a match turns up.
    """
    for fname in reversed(_store.list_result_files()):
        if any(rec["result_id"] == result_id for rec in _get_light_records(fname)):
            date_str = fname[len("results_"):-len(".jsonl")]
            for r in _store.load_file(date_str):
                if r.get("result_id") == result_id:
                    return r
            break  # file's light index said it was here but it's gone — don't keep scanning
    return None


# ── Compare API ────────────────────────────────────────────

_RUN_TEST_FLAGS = {
    "latency":             "has_latency",
    "throughput":          "has_throughput",
    "jitter":              "has_jitter",
    "latency_under_load":  "has_latency_under_load",
    "mtu":                 "has_mtu",
    "traceroute":          "has_traceroute",
    "traceroute_reverse":  "has_traceroute_reverse",
}


def _file_date_str(fname: str) -> str:
    return fname[len("results_"):-len(".jsonl")]


def _time_of_day_in_range(ts: str, time_from: str, time_to: str) -> bool:
    """ts is an ISO timestamp (UTC); time_from/time_to are 'HH:MM' (UTC).
    Wraps past midnight if time_from > time_to (e.g. 22:00–06:00)."""
    if len(ts) < 16:
        return False
    hm = ts[11:16]
    if time_from and time_to:
        if time_from <= time_to:
            return time_from <= hm <= time_to
        return hm >= time_from or hm <= time_to
    if time_from:
        return hm >= time_from
    if time_to:
        return hm <= time_to
    return True


@app.route("/api/runs")
@login_required
def api_runs():
    """Lightweight listing of runs for the compare picker — spans all
    available result files (not just the last N days) so older runs
    stay pickable, but only returns the fields the picker needs.

    Supports narrowing by path, calendar date range, time-of-day range
    (UTC), and which tests must be present on the run (AND semantics
    across multiple `tests` values)."""
    path_id    = request.args.get("path_id")
    limit      = int(request.args.get("limit", 500))
    days_param = request.args.get("days")
    date_from  = request.args.get("date_from")   # YYYY-MM-DD
    date_to    = request.args.get("date_to")     # YYYY-MM-DD
    time_from  = request.args.get("time_from")   # HH:MM (UTC)
    time_to    = request.args.get("time_to")     # HH:MM (UTC)
    tests_arg  = request.args.get("tests", "")
    required_tests = [t for t in tests_arg.split(",") if t]
    tag_arg    = request.args.get("tag", "")
    tag_filters = [t for t in tag_arg.split(",") if t]
    q          = request.args.get("q", "").strip().lower()

    if days_param:
        fnames = [
            f"results_{(datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')}.jsonl"
            for i in range(int(days_param))
        ]
    else:
        fnames = _store.list_result_files()
        if date_from or date_to:
            # Result files are named results_YYYY-MM-DD.jsonl, so the date
            # range can prune which files even get read/parsed at all.
            fnames = [
                f for f in fnames
                if (not date_from or _file_date_str(f) >= date_from)
                and (not date_to or _file_date_str(f) <= date_to)
            ]

    runs = []
    for fname in fnames:
        runs.extend(_get_light_records(fname))

    if path_id:
        runs = [r for r in runs if r.get("path_id") == path_id]

    if time_from or time_to:
        runs = [r for r in runs if _time_of_day_in_range(r.get("timestamp_utc", ""), time_from, time_to)]

    for t in required_tests:
        flag = _RUN_TEST_FLAGS.get(t)
        if flag:
            runs = [r for r in runs if r.get(flag)]

    # Annotations (notes/tags) live in a separate store, keyed by result_id,
    # and can change independently of the underlying result file — so they're
    # merged in per-request rather than baked into the cached light records.
    annotations = _annotations.get_all() if _annotations else {}
    runs = [
        dict(r, notes=annotations.get(r["result_id"], {}).get("notes", ""),
                tags=annotations.get(r["result_id"], {}).get("tags", []))
        for r in runs
    ]

    if tag_filters:
        runs = [r for r in runs if set(r["tags"]) & set(tag_filters)]

    if q:
        runs = [
            r for r in runs
            if q in r["notes"].lower() or any(q in t.lower() for t in r["tags"])
        ]

    runs.sort(key=lambda r: r.get("timestamp_utc", ""))
    runs.reverse()  # newest first
    return jsonify(runs[:limit])


@app.route("/api/result/<result_id>")
@login_required
def api_result(result_id: str):
    """Return the full result record for a single run — used by the
    compare page to load the two runs being placed side by side."""
    r = _find_record(result_id)
    if not r:
        return jsonify({"error": "Result not found"}), 404
    annotation = _annotations.get(result_id) if _annotations else {"notes": "", "tags": []}
    r = dict(r, notes=annotation.get("notes", ""), tags=annotation.get("tags", []))
    return jsonify(r)


# ── Annotations API (notes/tags on individual runs) ────────

@app.route("/api/annotations/<result_id>")
@login_required
def api_annotation_get(result_id: str):
    return jsonify(_annotations.get(result_id))


@app.route("/api/annotations/<result_id>", methods=["PUT"])
@login_required
def api_annotation_set(result_id: str):
    if not _find_record(result_id):
        return jsonify({"error": "Result not found"}), 404
    body = request.get_json(silent=True) or {}
    entry = _annotations.set(
        result_id,
        notes=body.get("notes", ""),
        tags=body.get("tags", []),
    )
    return jsonify(entry)


@app.route("/api/tags")
@login_required
def api_tags():
    return jsonify(_annotations.all_tags() if _annotations else [])


# ── Speed Test API ──────────────────────────────────────────
# On-demand browser <-> server speed test (like speedtest.net), run
# entirely against this dashboard — it measures the client's link to
# this box, not general internet speed.

_SPEEDTEST_DOWNLOAD_CHUNK = 262144       # bytes yielded per stream write
_SPEEDTEST_MAX_DOWNLOAD   = 200_000_000  # cap per request, regardless of ?size=
_SPEEDTEST_MAX_UPLOAD     = 32_000_000   # cap per request body (the client posts
                                          # 16MB; install.sh sets nginx's
                                          # client_max_body_size to 100M)


@app.route("/api/speedtest/ping")
@login_required
def api_speedtest_ping():
    """Minimal round-trip endpoint for client-side latency/jitter sampling.

    Also echoes back the caller's address (as seen through nginx's
    X-Real-IP) so the page can show what it's actually testing against.
    """
    resp = jsonify({
        "t":         time.time(),
        "client_ip": request.headers.get("X-Real-IP", request.remote_addr),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/speedtest/download")
@login_required
def api_speedtest_download():
    """Stream random bytes for a browser download speed test.

    Random rather than repeated/zeroed content so it can't be shrunk by
    gzip somewhere in front of this process — the client's measured byte
    count needs to match actual wire bytes.
    """
    try:
        size = int(request.args.get("size", 25_000_000))
    except ValueError:
        size = 25_000_000
    size = max(0, min(size, _SPEEDTEST_MAX_DOWNLOAD))

    def generate():
        remaining = size
        while remaining > 0:
            n = min(_SPEEDTEST_DOWNLOAD_CHUNK, remaining)
            yield os.urandom(n)
            remaining -= n

    return Response(
        stream_with_context(generate()),
        mimetype="application/octet-stream",
        headers={"Content-Length": str(size), "Cache-Control": "no-store"},
    )


@app.route("/api/speedtest/upload", methods=["POST"])
@login_required
def api_speedtest_upload():
    """Discard an uploaded chunk for a browser upload speed test.

    The client posts large bodies and aborts the one still in flight
    when its test time is up, so a cut-off body is normal, not an error.
    """
    content_length = request.content_length or 0
    if content_length > _SPEEDTEST_MAX_UPLOAD:
        return jsonify({"error": "Chunk too large"}), 413

    total = 0
    try:
        while True:
            chunk = request.stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > _SPEEDTEST_MAX_UPLOAD:
                return jsonify({"error": "Chunk too large"}), 413
    except (OSError, IOError):
        pass   # client aborted mid-body at the end of its test window

    return jsonify({"received": total})


_SPEEDTEST_MTU_MIN = 1000
_SPEEDTEST_MTU_MAX = 1500


@app.route("/api/speedtest/mtu/probe")
@login_required
def api_speedtest_mtu_probe():
    """Single DF-flagged ping probe, used by the browser to binary-search
    path MTU toward itself — the same technique MTURunner (runners/
    runner_latency.py) uses over SSH toward a configured agent, just run
    locally against the requesting browser's own address instead. A
    packet larger than the path MTU gets dropped/rejected rather than
    silently fragmented, which is what "-M do" (Don't Fragment) forces.

    `ping` carries cap_net_raw on this host, so no sudo/root is needed
    to open the raw socket it requires.
    """
    client_ip = request.headers.get("X-Real-IP", request.remote_addr)
    try:
        ipaddress.ip_address(client_ip)
    except ValueError:
        return jsonify({"error": "Could not determine a valid client IP"}), 400

    try:
        size = int(request.args.get("size", _SPEEDTEST_MTU_MAX))
    except ValueError:
        return jsonify({"error": "Invalid size"}), 400
    size = max(28, min(size, 9000))
    payload = size - 28

    try:
        result = subprocess.run(
            ["ping", "-c", "2", "-M", "do", "-s", str(payload), "-W", "1", client_ip],
            capture_output=True, text=True, timeout=6,
        )
        success = result.returncode == 0 and "0% packet loss" in result.stdout
    except Exception:
        success = False

    return jsonify({"size": size, "success": success})


# Completed speed test runs are appended here, one JSON object per line.
# Deliberately not named results_*.jsonl so ResultStore/run listings,
# CSV export and "clear results" leave it alone.
_SPEEDTEST_HISTORY_FILE  = "speedtest_history.jsonl"
_SPEEDTEST_HISTORY_LIMIT = 50
_SPEEDTEST_WINDOW_LIMIT  = 500
_speedtest_history_lock  = threading.Lock()


def _speedtest_history_path() -> str:
    return os.path.join(_config.results_dir, _SPEEDTEST_HISTORY_FILE)


def _num(v, ndigits=1):
    """Coerce a client-supplied value to a rounded float, or None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, ndigits)


@app.route("/api/speedtest/history")
@login_required
def api_speedtest_history():
    """Completed speed test runs, newest first.

    With ?minutes=N, returns runs from the last N minutes (capped at
    _SPEEDTEST_WINDOW_LIMIT); otherwise the most recent
    _SPEEDTEST_HISTORY_LIMIT runs.
    """
    minutes = request.args.get("minutes", type=int)
    path = _speedtest_history_path()
    runs = []
    if os.path.exists(path):
        with _speedtest_history_lock, open(path) as f:
            lines = list(f) if minutes else collections.deque(f, maxlen=_SPEEDTEST_HISTORY_LIMIT)
        for line in lines:
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue
    if minutes:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        def in_window(r):
            try:
                return datetime.fromisoformat(r["ts"]) >= cutoff
            except (KeyError, TypeError, ValueError):
                return False
        runs = [r for r in runs if in_window(r)][-_SPEEDTEST_WINDOW_LIMIT:]
    runs.reverse()
    return jsonify(runs)


@app.route("/api/speedtest/history", methods=["POST"])
@login_required
def api_speedtest_history_add():
    """Record a completed browser speed test run.

    The source IP is taken from the request (nginx's X-Real-IP), not
    from the client payload, so it reflects where the test really ran.
    """
    body = request.get_json(silent=True) or {}
    grade = body.get("bufferbloat_grade")
    if grade not in ("A+", "A", "B", "C", "D", "F"):
        grade = None
    entry = {
        "ts":                datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "client_ip":         request.headers.get("X-Real-IP", request.remote_addr),
        "user":              session.get("username"),
        "ping_ms":           _num(body.get("ping_ms")),
        "jitter_ms":         _num(body.get("jitter_ms")),
        "mtu":               _num(body.get("mtu"), 0),
        "mtu_fragmented":    bool(body.get("mtu_fragmented")),
        "bufferbloat_grade": grade,
        "bufferbloat_dl_ms": _num(body.get("bufferbloat_dl_ms"), 0),
        "bufferbloat_ul_ms": _num(body.get("bufferbloat_ul_ms"), 0),
        "download_mbps":     _num(body.get("download_mbps")),
        "download_peak":     _num(body.get("download_peak")),
        "upload_mbps":       _num(body.get("upload_mbps")),
        "upload_peak":       _num(body.get("upload_peak")),
    }
    if entry["mtu"] is not None:
        entry["mtu"] = int(entry["mtu"])
    with _speedtest_history_lock, open(_speedtest_history_path(), "a") as f:
        f.write(json.dumps(entry) + "\n")
    return jsonify(entry)


def _warm_light_cache():
    """Pre-build the lightweight run index at startup so the first
    /api/runs or /api/result call of a process doesn't have to eat the
    cost of parsing every historical result file (which can be large —
    each line carries the full raw iPerf3 JSON blob)."""
    if not _config or not _store:
        return
    try:
        for fname in _store.list_result_files():
            _get_light_records(fname)
    except Exception:
        logging.getLogger(__name__).warning("Failed to warm run index cache", exc_info=True)


threading.Thread(target=_warm_light_cache, daemon=True, name="warm-run-index").start()


# ── Onboarding API ─────────────────────────────────────────

@app.route("/api/onboard", methods=["POST"])
@login_required
def api_onboard():
    """Onboard a new agent via the web UI."""
    body          = request.get_json(silent=True) or {}
    agent_ip      = body.get("agent_ip", "").strip()
    agent_test_ip = body.get("agent_test_ip", "").strip() or None
    agent_label   = body.get("agent_label", "").strip() or agent_ip
    agent_id      = body.get("agent_id",   "").strip()
    agent_type    = body.get("agent_type", "endpoint")
    admin_user    = body.get("admin_user", "").strip()
    admin_pass    = body.get("admin_pass", "")
    admin_port    = int(body.get("admin_port", 22))
    air_gapped    = bool(body.get("air_gapped", False))
    reonboard     = bool(body.get("reonboard", False))

    if reonboard:
        # Re-onboard an agent already in the saved config: its details come
        # from the config, not the request, so a stale or mistyped form can't
        # onboard a different host under that ID.
        agent = _config.get_agent(agent_id) if agent_id else None
        if agent is None:
            return jsonify({"error": f"Agent '{agent_id}' is not in the saved config "
                                     f"— save the config first"}), 400
        if agent.type != "agent_installed":
            return jsonify({"error": f"Agent '{agent_id}' is not an installed agent "
                                     f"— only installed agents can be re-onboarded"}), 400
        agent_ip      = agent.host_mgmt_ip
        agent_test_ip = agent.host_test_ip
        agent_label   = agent.label or agent.id
        agent_type    = agent.type

    if not agent_ip:   return jsonify({"error": "agent_ip is required"}), 400
    if agent_type in NO_SSH_TYPES or agent_type == "svi_adjacent":
        return jsonify({"error": f"{agent_type} devices have no SSH and aren't onboarded "
                                 f"— add them in the config editor instead"}), 400
    for _what, _h in (("Management host", agent_ip), ("Test host", agent_test_ip)):
        _e = validate_host(_h) if _h else None
        if _e:
            return jsonify({"error": f"{_what} {_e}"}), 400
    if not admin_user: return jsonify({"error": "admin_user is required"}), 400
    if not admin_pass: return jsonify({"error": "admin_pass is required"}), 400

    job_id  = "onboard-" + str(uuid.uuid4())[:8]
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        if len(_job_log_history) >= _MAX_HISTORY_JOBS:
            oldest = sorted(_job_log_history.keys())[0]
            del _job_log_history[oldest]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":   job_id,
            "path_id":  "onboard",
            "label":    f"{'Re-onboarding' if reonboard else 'Onboarding'} {agent_label}",
            "status":   "queued",
            "started":  datetime.now(timezone.utc).isoformat(),
            "finished": None, "success": None, "error": None,
        }

    def run_onboard():
        root = logging.getLogger()
        root.setLevel(debug_mode.root_level(logging.INFO))
        root.addHandler(handler)
        _quiet_libs("netmiko", "paramiko")
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
        try:
            from onboard import onboard_agent
            ok = onboard_agent(
                config_path=_config_path,
                agent_ip=agent_ip,
                agent_test_ip=agent_test_ip,
                agent_label=agent_label,
                agent_id=agent_id or None,
                agent_type=agent_type,
                admin_user=admin_user,
                admin_pass=admin_pass,
                admin_port=admin_port,
                interactive=False,
                air_gapped=air_gapped,
                packages_dir=_packages_dir,
                reonboard=reonboard,
            )
            global _config, _tester
            from core.config_loader import load_config
            from core.path_tester import PathTester
            _config = load_config(_config_path)
            _tester = PathTester(_config)
            with _jobs_lock:
                _jobs[job_id]["status"]   = "done" if ok else "error"
                _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
                _jobs[job_id]["success"]  = ok
                _jobs[job_id]["error"]    = None if ok else "Onboarding failed — check output above"
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]["status"]   = "error"
                _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
                _jobs[job_id]["error"]    = str(e)
        finally:
            root.removeHandler(handler)

    threading.Thread(target=run_onboard, daemon=True,
                     name=f"onboard-{agent_ip}").start()
    return jsonify({"job_id": job_id, "status": "queued"})


# ── SSH Key Management API ───────────────────────────────

@app.route("/api/ssh/pubkey")
@login_required
def api_ssh_pubkey():
    """Return public key info and content."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    key_file = os.path.expanduser(_config.ssh_defaults.key_file)
    pub_file = key_file + ".pub"
    if not os.path.exists(pub_file):
        return jsonify({"error": "Public key not found", "path": pub_file}), 404
    content = open(pub_file).read().strip()
    # Get fingerprint
    try:
        import subprocess
        fp = subprocess.run(
            ["ssh-keygen", "-l", "-f", pub_file],
            capture_output=True, text=True, timeout=10
        )
        fingerprint = fp.stdout.strip() if fp.returncode == 0 else ""
    except Exception:
        fingerprint = ""
    return jsonify({
        "path":        pub_file,
        "content":     content,
        "fingerprint": fingerprint,
    })


@app.route("/api/ssh/pubkey/download")
@login_required
def api_ssh_pubkey_download():
    """Download public key as a file."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    key_file = os.path.expanduser(_config.ssh_defaults.key_file)
    pub_file = key_file + ".pub"
    if not os.path.exists(pub_file):
        return jsonify({"error": "Public key not found"}), 404
    content = open(pub_file).read()
    return Response(
        content,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=linkstate_key.pub"}
    )


@app.route("/api/ssh/privkey")
@login_required
def api_ssh_privkey():
    """Download private key file."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    key_file = os.path.expanduser(_config.ssh_defaults.key_file)
    if not os.path.exists(key_file):
        return jsonify({"error": "Private key not found"}), 404
    content = open(key_file).read()
    return Response(
        content,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename=linkstate_key"}
    )


@app.route("/api/ssh/import", methods=["POST"])
@login_required
def api_ssh_import():
    """Import a new private key. Derives and writes public key automatically."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    key_file = os.path.expanduser(_config.ssh_defaults.key_file)
    pub_file = key_file + ".pub"

    if "file" in request.files:
        key_content = request.files["file"].read().decode("utf-8")
    elif request.is_json:
        key_content = request.get_json().get("key", "")
    else:
        return jsonify({"error": "No key provided"}), 400

    if not key_content.strip().startswith("-----BEGIN"):
        return jsonify({"error": "Invalid private key format"}), 400

    # Write private key
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    with open(key_file, "w") as f:
        f.write(key_content)
    os.chmod(key_file, 0o600)

    # Derive public key
    try:
        import subprocess
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", key_file],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return jsonify({"error": f"Failed to derive public key: {result.stderr}"}), 400
        pub_content = result.stdout.strip()
        with open(pub_file, "w") as f:
            f.write(pub_content + "\n")
        os.chmod(pub_file, 0o644)
    except Exception as e:
        return jsonify({"error": f"ssh-keygen error: {e}"}), 500

    return jsonify({"ok": True, "pubkey": pub_content})


@app.route("/api/ssh/push-key", methods=["POST"])
@login_required
def api_ssh_push_key():
    """Push public key to an agent using admin credentials."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503

    body       = request.get_json(silent=True) or {}
    agent_id   = body.get("agent_id", "").strip()
    admin_user = body.get("admin_user", "").strip()
    admin_pass = body.get("admin_pass", "")
    admin_port = int(body.get("admin_port", 22))
    push_all   = bool(body.get("push_all", False))

    if not admin_user: return jsonify({"error": "admin_user is required"}), 400
    if not admin_pass: return jsonify({"error": "admin_pass is required"}), 400

    # Get agents to push to
    if push_all:
        # Only agents with SSH — key push to a switch/router, web server or
        # agent-not-installed host would just fail (or worse, succeed on a box
        # the controller isn't meant to log into).
        agents = [a for a in _config.agents if a.type not in NO_SSH_TYPES]
    else:
        agent = _config.get_agent(agent_id)
        if not agent: return jsonify({"error": f"Agent '{agent_id}' not found"}), 404
        if agent.type in NO_SSH_TYPES:
            return jsonify({"error": f"Agent '{agent_id}' is {agent.type} — the controller "
                                     f"doesn't SSH to it, so there is no key to push"}), 400
        agents = [agent]

    # Read public key
    key_file = os.path.expanduser(_config.ssh_defaults.key_file)
    pub_file = key_file + ".pub"
    if not os.path.exists(pub_file):
        return jsonify({"error": "Public key not found — import a key first"}), 400
    pub_key = open(pub_file).read().strip()

    linkstate_user = _config.ssh_defaults.username

    job_id  = "push-key-" + str(uuid.uuid4())[:8]
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        if len(_job_log_history) >= _MAX_HISTORY_JOBS:
            oldest = sorted(_job_log_history.keys())[0]
            del _job_log_history[oldest]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":   job_id,
            "path_id":  "push-key",
            "label":    f"Push key → {len(agents)} agent(s)",
            "status":   "queued",
            "started":  datetime.now(timezone.utc).isoformat(),
            "finished": None, "success": None, "error": None,
        }

    def run_push():
        from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
        # Attach handler directly to a named logger and ensure propagation
        _log = logging.getLogger("push-key")
        _log.setLevel(logging.DEBUG)
        _log.addHandler(handler)
        _quiet_libs("netmiko", "paramiko")

        with _jobs_lock:
            _jobs[job_id]["status"] = "running"

        target_desc = "all endpoint agents" if len(agents) > 1 else agents[0].label
        _log.info(f"── Push Public Key ──")
        _log.info(f"  Target  : {target_desc}")
        _log.info(f"  User    : {admin_user}:{admin_port}")
        _log.info(f"  LinkState : {linkstate_user}")
        _log.info(f"  Key     : {pub_file}")

        all_ok = True
        for i, agent in enumerate(agents, 1):
            _log.info(f"")
            _log.info(f"[{i}/{len(agents)}] {agent.label} ({agent.host_mgmt_ip})")
            _log.info(f"  Connecting as {admin_user}...")
            try:
                conn = ConnectHandler(
                    device_type="linux",
                    host=agent.host_mgmt_ip,
                    username=admin_user,
                    password=admin_pass,
                    port=admin_port,
                    timeout=30,
                )
                _log.info(f"  ✓ Connected")

                # Prime sudo
                _log.info(f"  Priming sudo...")
                conn.send_command_timing(
                    f"echo '{admin_pass}' | sudo -S true 2>/dev/null",
                    last_read=2.0
                )

                # Ensure .ssh directory exists with correct permissions
                auth_keys = f"/home/{linkstate_user}/.ssh/authorized_keys"
                _log.info(f"  Creating .ssh directory for {linkstate_user}...")
                conn.send_command_timing(
                    f"echo '{admin_pass}' | sudo -S bash -c "
                    f"'mkdir -p /home/{linkstate_user}/.ssh && "
                    f"chmod 700 /home/{linkstate_user}/.ssh && "
                    f"chown {linkstate_user}:{linkstate_user} /home/{linkstate_user}/.ssh'",
                    read_timeout=15, last_read=2.0
                )

                # Write public key — use printf to avoid quoting issues
                _log.info(f"  Writing public key to {auth_keys}...")
                # Copy key to a temp file first, then move into place with sudo
                tmp_key = "/tmp/linkstate_pubkey_tmp"
                conn.send_command_timing(
                    f"printf '%s\n' {repr(pub_key)} > {tmp_key}",
                    read_timeout=10, last_read=2.0
                )
                deploy_cmd = (
                    f"echo '{admin_pass}' | sudo -S bash -c "
                    f"'cp {tmp_key} {auth_keys} && "
                    f"chmod 600 {auth_keys} && "
                    f"chown {linkstate_user}:{linkstate_user} {auth_keys} && "
                    f"rm -f {tmp_key}'"
                )
                conn.send_command_timing(deploy_cmd, read_timeout=15, last_read=2.0)

                # Verify
                _log.info(f"  Verifying key was written...")
                check = conn.send_command_timing(
                    f"echo '{admin_pass}' | sudo -S cat {auth_keys} 2>/dev/null",
                    last_read=2.0
                )
                conn.disconnect()

                if pub_key[:20] in check:
                    _log.info(f"  ✓ Key verified in authorized_keys")
                    _log.info(f"✓ {agent.label} — key deployed successfully")
                else:
                    _log.error(f"  ✗ Key not found in authorized_keys after write")
                    _log.error(f"✗ {agent.label} — verification failed")
                    all_ok = False

            except NetmikoAuthenticationException:
                _log.error(f"✗ {agent.label}: Authentication failed for {admin_user}@{agent.host_mgmt_ip}:{admin_port}")
                all_ok = False
            except Exception as e:
                _log.error(f"✗ {agent.label}: {e}")
                all_ok = False

        _log.info(f"")
        if all_ok:
            _log.info(f"✓ All agents updated successfully")
        else:
            _log.error(f"✗ One or more agents failed — check output above")

        with _jobs_lock:
            _jobs[job_id]["status"]   = "done" if all_ok else "error"
            _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["success"]  = all_ok
            _jobs[job_id]["error"]    = None if all_ok else "Key push failed on one or more agents"
        _log.removeHandler(handler)

    threading.Thread(target=run_push, daemon=True, name="push-key").start()
    return jsonify({"job_id": job_id, "status": "queued"})


# ── Export / Import API ───────────────────────────────────

# Key file names inside export bundles. Bundles exported before the rename
# to LinkState carry nettest_key, and still import.
_KEY_NAMES    = ("linkstate_key", "nettest_key")
_PUBKEY_NAMES = ("linkstate_key.pub", "nettest_key.pub")


@app.route("/api/export", methods=["POST"])
@login_required
def api_export():
    """Export config sections as a .tar.gz bundle."""
    import tarfile, io, copy
    body    = request.get_json(silent=True) or {}
    include = body.get("include", {})

    if not _config:
        return jsonify({"error": "Config not loaded"}), 503

    # Build config dict to export
    raw = {}

    # Always include agents and paths
    raw["agents"] = [
        {k: v for k, v in {
            "id":           a.id,
            "label":        a.label,
            "host_mgmt_ip": a.host_mgmt_ip,
            "host_test_ip": a.host_test_ip,
            "type":         a.type,
            **({"address_family": a.address_family} if a.address_family != "ipv4" else {}),
            **({"resolve_on": a.resolve_on} if a.resolve_on != "controller" else {}),
            **({"iperf3_port": a.iperf3_port} if a.iperf3_port else {}),
            **({"tcp_port": a.tcp_port} if a.tcp_port else {}),
            **({"latency_method": a.latency_method}
               if a.type == "web_server" and a.latency_method != "auto" else {}),
            **({"username": a.username} if a.username else {}),
            **({"password": a.password} if a.password else {}),
            **({"key_file": a.key_file} if a.key_file else {}),
            **({"port":     a.port}     if a.port     else {}),
        }.items() if v is not None}
        for a in _config.agents
    ]
    raw["paths"] = [
        {
            "id":          p.id,
            "label":       p.label,
            "source":      p.source,
            "destination": p.destination,
            "hops":        p.hops,
            "tests":       p.tests,
            **({"schedule": p.schedule} if p.schedule else {}),
            **({"group": p.group} if p.group else {}),
        }
        for p in _config.paths
    ]

    if include.get("controller"):
        raw["controller"] = {
            "name":        _config.name,
            "results_dir": _config.results_dir,
            "log_dir":     _config.log_dir,
            "log_level":   _config.log_level,
        }

    if include.get("schedule"):
        s = _config.schedule
        raw["schedule"] = {
            "enabled":                       s.enabled,
            "full_test_interval_minutes":    s.full_test_interval_minutes,
            "latency_only_interval_minutes": s.latency_only_interval_minutes,
            "business_hours_only":           s.business_hours_only,
            "business_hours_start":          s.business_hours_start,
            "business_hours_end":            s.business_hours_end,
            "timezone":                      s.timezone,
            "stagger_seconds":               s.stagger_seconds,
        }

    if include.get("test_params"):
        tp = _config.test_params
        from dataclasses import asdict
        raw["test_params"] = asdict(tp)
        raw["thresholds"] = _config.thresholds

    if include.get("ssh_credentials"):
        sd = _config.ssh_defaults
        raw["ssh_defaults"] = {
            "username": sd.username,
            "password": sd.password,
            "port":     sd.port,
            "timeout":  sd.timeout,
            "key_file": sd.key_file,
        }

    if include.get("auth"):
        a = _config.auth
        raw["auth"] = {
            "enabled":                    a.enabled,
            "method":                     a.method or "none",   # blank would re-infer on import
            "radius_server":              a.radius_server,
            "radius_port":                a.radius_port,
            "radius_secret":              a.radius_secret,
            "radius_timeout":             a.radius_timeout,
            "radius_role_attribute":      a.radius_role_attribute,
            "radius_admin_value":         a.radius_admin_value,
            "radius_readonly_value":      a.radius_readonly_value,
            "radius_default_role":        a.radius_default_role,
            "local_users": [
                {"username": u.username, "password_hash": u.password_hash, "role": u.role}
                for u in a.local_users
            ],
            "session_secret":             a.session_secret,
            "session_lifetime_minutes":   a.session_lifetime_minutes,
            "login_max_attempts":         a.login_max_attempts,
            "login_window_seconds":       a.login_window_seconds,
            "login_lockout_seconds":      a.login_lockout_seconds,
            "cookie_secure":              a.cookie_secure,
        }

    # Build tar.gz in memory
    import yaml as _yaml
    buf = io.BytesIO()
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = f"linkstate-export-{ts}"

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # config.yaml
        config_bytes = _yaml.dump(raw, default_flow_style=False,
                                   allow_unicode=True).encode("utf-8")
        ti = tarfile.TarInfo(name=f"{prefix}/config.yaml")
        ti.size = len(config_bytes)
        tar.addfile(ti, io.BytesIO(config_bytes))

        # SSH keys
        if include.get("ssh_keys"):
            key_file = os.path.expanduser(_config.ssh_defaults.key_file)
            for fpath, arcname in [
                (key_file,          f"{prefix}/linkstate_key"),
                (key_file + ".pub", f"{prefix}/linkstate_key.pub"),
            ]:
                if os.path.exists(fpath):
                    tar.add(fpath, arcname=arcname)

        # Readme
        sensitive = []
        if include.get("ssh_credentials"): sensitive.append("SSH credentials")
        if include.get("ssh_keys"):        sensitive.append("SSH private key")
        if include.get("auth"):            sensitive.append("RADIUS/auth config")
        readme = f"""LinkState Configuration Export
Generated: {datetime.now(timezone.utc).isoformat()}

Contents:
- config.yaml  (agents, paths{", controller" if include.get("controller") else ""}{", schedule" if include.get("schedule") else ""}{", test params & thresholds" if include.get("test_params") else ""}{", SSH credentials" if include.get("ssh_credentials") else ""}{", auth" if include.get("auth") else ""})
{"- linkstate_key / linkstate_key.pub" if include.get("ssh_keys") else ""}

{"⚠ SENSITIVE: This bundle contains " + ", ".join(sensitive) + ". Keep it secure." if sensitive else ""}

To import: Config → Export/Import → Import Bundle
"""
        readme_bytes = readme.encode("utf-8")
        ti = tarfile.TarInfo(name=f"{prefix}/README.txt")
        ti.size = len(readme_bytes)
        tar.addfile(ti, io.BytesIO(readme_bytes))

    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/gzip",
        headers={"Content-Disposition": f"attachment; filename=linkstate-export-{ts}.tar.gz"}
    )


@app.route("/api/import/preview", methods=["POST"])
@login_required
def api_import_preview():
    """Parse an uploaded export bundle and return a diff preview."""
    import tarfile, io
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    try:
        buf = io.BytesIO(f.read())
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            config_member = next(
                (m for m in tar.getmembers() if m.name.endswith("config.yaml")), None
            )
            if not config_member:
                return jsonify({"error": "No config.yaml found in bundle"}), 400

            import yaml as _yaml
            raw = _yaml.safe_load(tar.extractfile(config_member).read())

            # Check for SSH keys
            has_privkey = any(
                m.name.endswith(_KEY_NAMES) for m in tar.getmembers()
            )
            has_pubkey = any(m.name.endswith(_PUBKEY_NAMES) for m in tar.getmembers())

    except Exception as e:
        return jsonify({"error": f"Failed to parse bundle: {e}"}), 400

    if not _config:
        return jsonify({"error": "Config not loaded"}), 503

    # Build diff
    existing_agent_ids = {a.id for a in _config.agents}
    existing_path_ids  = {p.id for p in _config.paths}

    imported_agents = raw.get("agents", [])
    imported_paths  = raw.get("paths",  [])

    agent_diff = []
    for a in imported_agents:
        aid = a.get("id", "")
        agent_diff.append({
            "id":     aid,
            "label":  a.get("label", aid),
            "host":   a.get("host_mgmt_ip", ""),
            "type":   a.get("type", "endpoint"),
            "status": "existing" if aid in existing_agent_ids else "new",
        })

    path_diff = []
    for p in imported_paths:
        pid = p.get("id", "")
        path_diff.append({
            "id":     pid,
            "label":  p.get("label", pid),
            "source": p.get("source", ""),
            "dest":   p.get("destination", ""),
            "status": "existing" if pid in existing_path_ids else "new",
        })

    sections = {
        "agents":           bool(imported_agents),
        "paths":            bool(imported_paths),
        "controller":       "controller"   in raw,
        "schedule":         "schedule"     in raw,
        "test_params":      "test_params"  in raw,
        "ssh_credentials":  "ssh_defaults" in raw,
        "ssh_keys":         has_privkey or has_pubkey,
        "auth":             "auth"         in raw,
    }

    # Store raw server-side (session cookie too small for large configs)
    import uuid as _uuid
    import_id = str(_uuid.uuid4())
    with _import_cache_lock:
        _import_cache[import_id] = {"raw": raw, "has_keys": has_privkey}
        # Evict old entries (keep last 10)
        if len(_import_cache) > 10:
            oldest = sorted(_import_cache.keys())[0]
            del _import_cache[oldest]
    session["_import_id"] = import_id

    return jsonify({
        "sections":    sections,
        "agent_diff":  agent_diff,
        "path_diff":   path_diff,
        "agent_new":   sum(1 for a in agent_diff if a["status"] == "new"),
        "agent_exist": sum(1 for a in agent_diff if a["status"] == "existing"),
        "path_new":    sum(1 for p in path_diff  if p["status"] == "new"),
        "path_exist":  sum(1 for p in path_diff  if p["status"] == "existing"),
    })


@app.route("/api/import/confirm", methods=["POST"])
@login_required
def api_import_confirm():
    """Apply the previewed import with selected options."""
    import yaml as _yaml
    body    = request.get_json(silent=True) or {}
    mode    = body.get("mode", "merge")      # merge | replace
    include = body.get("include", {})

    import_id = session.get("_import_id")
    if not import_id:
        return jsonify({"error": "No import preview found — upload the bundle first"}), 400
    with _import_cache_lock:
        cache = _import_cache.get(import_id)
    if not cache:
        return jsonify({"error": "Import session expired — please upload the bundle again"}), 400
    raw = cache["raw"]

    # Load current config file
    with open(_config_path, "r") as fh:
        current = _yaml.safe_load(fh)

    # Apply agents
    if include.get("agents") and "agents" in raw:
        bad = [e for a in raw["agents"] for e in agent_host_errors(a)]
        if bad:
            return jsonify({"error": "Invalid agent host(s) in bundle: " + "; ".join(bad)}), 400
        if mode == "replace":
            current["agents"] = raw["agents"]
        else:  # merge
            existing_ids = {a["id"] for a in current.get("agents", [])}
            for a in raw["agents"]:
                if a["id"] not in existing_ids:
                    current.setdefault("agents", []).append(a)

    # Apply paths
    if include.get("paths") and "paths" in raw:
        if mode == "replace":
            current["paths"] = raw["paths"]
        else:
            existing_ids = {p["id"] for p in current.get("paths", [])}
            for p in raw["paths"]:
                if p["id"] not in existing_ids:
                    current.setdefault("paths", []).append(p)

    # Apply other sections (always replace)
    for section, key in [
        ("controller",      "controller"),
        ("schedule",        "schedule"),
        ("test_params",     "test_params"),
        ("ssh_credentials", "ssh_defaults"),
        ("auth",            "auth"),
    ]:
        if include.get(section) and key in raw:
            current[key] = raw[key]
    # Thresholds travel with test params (older bundles have none — keep ours).
    if include.get("test_params") and "thresholds" in raw:
        thresholds, errs = parse_thresholds(raw["thresholds"])
        if not errs:
            current["thresholds"] = thresholds

    # Write config
    with open(_config_path, "w") as fh:
        _yaml.dump(current, fh, default_flow_style=False, allow_unicode=True)

    # Apply SSH keys if requested
    if include.get("ssh_keys") and cache.get("has_keys"):
        # Keys were in the bundle — re-extract from the uploaded file
        # They were stored in temp session; user needs to re-upload
        # (session doesn't store binary) — handled by client re-submitting file
        pass

    # Reload config in web process immediately
    global _config, _tester
    from core.config_loader import load_config
    from core.path_tester   import PathTester
    try:
        _config = load_config(_config_path)
        _tester = PathTester(_config)
    except Exception as e:
        return jsonify({"error": f"Config saved but failed to reload: {e}"}), 500

    # Restart scheduler (debounced — collapses rapid repeated saves into one restart)
    schedule_linkstate_restart()

    # Also signal web process to reload by touching a reload sentinel
    # (ensures dashboard reflects new config even if scheduler restart fails)
    try:
        import signal
        os.kill(os.getpid(), signal.SIGUSR1)
    except Exception:
        pass

    with _import_cache_lock:
        _import_cache.pop(import_id, None)
    session.pop("_import_id", None)

    return jsonify({"ok": True, "mode": mode})


@app.route("/api/import/keys", methods=["POST"])
@login_required
def api_import_keys():
    """Extract and install SSH keys from an uploaded bundle."""
    import tarfile, io
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    try:
        buf = io.BytesIO(f.read())
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            key_file = os.path.expanduser(_config.ssh_defaults.key_file)
            for member in tar.getmembers():
                if member.name.endswith(_PUBKEY_NAMES):
                    content = tar.extractfile(member).read()
                    with open(key_file + ".pub", "wb") as fh: fh.write(content)
                    os.chmod(key_file + ".pub", 0o644)
                elif member.name.endswith(_KEY_NAMES):
                    content = tar.extractfile(member).read()
                    with open(key_file, "wb") as fh: fh.write(content)
                    os.chmod(key_file, 0o600)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ── Update Management API ─────────────────────────────────

_update_cache: dict = {}   # stores validated update bundle info
_update_cache_lock = threading.Lock()
_APP_DIR       = branding.APP_DIR
_SNAPSHOTS_DIR = os.path.join(_APP_DIR, "snapshots")


def _pip_install_cmd() -> list:
    """pip command that syncs the venv to the pinned dependencies.

    Uses requirements.lock (falling back to requirements.txt) and, when the
    release bundled its wheels in vendor/wheels, installs from those alone
    so updates and rollbacks work without an internet connection.
    """
    pip = os.path.join(_APP_DIR, "venv/bin/pip")
    req = os.path.join(_APP_DIR, "requirements.lock")
    if not os.path.isfile(req):
        req = os.path.join(_APP_DIR, "requirements.txt")
    cmd = [pip, "install", "-r", req, "-q"]
    wheels = os.path.join(_APP_DIR, "vendor", "wheels")
    if os.path.isdir(wheels) and any(f.endswith(".whl") for f in os.listdir(wheels)):
        cmd += ["--no-index", "--find-links", wheels]
    return cmd


def _restart_services_for_job(job_id: str, _log, done_msg: str) -> None:
    """Last step of an update or rollback: restart the scheduler, then the web
    service, which ends this process when it works.

    The job stays "running" until then, so the browser keeps streaming the log.
    A failed restart marks the job as an error with the command to run, rather
    than reporting success while the old code keeps running.
    """
    sched, web = branding.SCHEDULER_SERVICE, branding.WEB_SERVICE

    def finish(error: Optional[str] = None) -> None:
        with _jobs_lock:
            _jobs[job_id]["status"]   = "error" if error else "done"
            _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["success"]  = not error
            _jobs[job_id]["error"]    = error

    def fail(what: str, err: str, still_running: str) -> None:
        manual = f"sudo systemctl restart {still_running}"
        _log.error(f"  ✗ Couldn't restart {what}: {err}")
        _log.error(f"  The new files are in place, but {what} is still running the "
                   f"old code. Run on the controller: {manual}")
        finish(f"{what} restart failed ({err}). Run: {manual}")

    _log.info("  Restarting scheduler...")
    err = _restart_service(sched)
    if err:
        # Stop here, before the web restart, so the error stays on screen.
        fail(f"the scheduler ({sched})", err, f"{sched} {web}")
        return
    _log.info("  ✓ Scheduler restarted")
    _log.info(f"  ✓ {done_msg}")
    _log.info("  Restarting web service — this page reconnects when it's back...")
    time.sleep(1)   # let open log streams send the lines above first

    err = _restart_service(web, timeout=150)
    # Still here: a working restart would have stopped this process.
    if err:
        fail(f"the web service ({web})", err, web)
        return
    _log.warning(f"  ⚠ systemd restarted {web}, but this dashboard process "
                 "wasn't stopped, so it isn't the one systemd runs. Restart it "
                 "yourself to load the new code.")
    finish()


def _read_version(path: str = None) -> str:
    """Read version from version.txt."""
    vpath = path or os.path.join(_APP_DIR, "version.txt")
    try:
        return open(vpath).read().strip()
    except Exception:
        return "unknown"


def _parse_changelog(content: str, version: str = None) -> str:
    """Extract changelog entry for a specific version, or return full changelog."""
    if not version:
        return content
    lines = content.splitlines()
    in_section = False
    section = []
    for line in lines:
        if line.startswith(f"## [{version}]"):
            in_section = True
        elif in_section and line.startswith("## ["):
            break
        if in_section:
            section.append(line)
    return "\n".join(section) if section else f"No changelog entry for v{version}"


@app.route("/api/version")
@login_required
def api_version():
    """Return current version and changelog."""
    version = _read_version()
    changelog = ""
    cl_path = os.path.join(_APP_DIR, "CHANGELOG.md")
    if os.path.exists(cl_path):
        changelog = open(cl_path).read()
    snapshots = []
    if os.path.isdir(_SNAPSHOTS_DIR):
        for snap in sorted(os.listdir(_SNAPSHOTS_DIR), reverse=True)[:5]:
            snap_ver = _read_version(os.path.join(_SNAPSHOTS_DIR, snap, "version.txt"))
            snapshots.append({"name": snap, "version": snap_ver})
    return jsonify({
        "version":   version,
        "changelog": _parse_changelog(changelog, version),
        "snapshots": snapshots,
        "migrate_hint": branding.MIGRATE_HINT if branding.LEGACY_INSTALL else "",
    })


@app.route("/api/update/preview", methods=["POST"])
@login_required
def api_update_preview():
    """Validate and preview an update bundle."""
    import tarfile, io, uuid as _uuid
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    try:
        buf = io.BytesIO(f.read())
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            members = tar.getmembers()
            names   = [m.name for m in members]

            # Find version.txt inside the bundle
            ver_member = next((m for m in members
                               if m.name.endswith("version.txt") and ".ssh" not in m.name), None)
            if not ver_member:
                return jsonify({"error": "Invalid bundle — version.txt not found"}), 400

            incoming_version = tar.extractfile(ver_member).read().decode().strip()

            # Find changelog
            cl_member = next((m for m in members
                              if m.name.endswith("CHANGELOG.md")), None)
            changelog_entry = ""
            if cl_member:
                cl_content = tar.extractfile(cl_member).read().decode()
                changelog_entry = _parse_changelog(cl_content, incoming_version)

            # List changed Python/key files
            py_files = [m.name for m in members
                        if m.name.endswith(".py") or m.name.endswith(".html")
                        or m.name.endswith(".sh")]

    except Exception as e:
        return jsonify({"error": f"Failed to parse bundle: {e}"}), 400

    current_version = _read_version()

    # Cache bundle for apply step
    import uuid as _uuid2
    update_id = str(_uuid2.uuid4())
    # Save bundle to temp file
    buf.seek(0)
    tmp_path = f"/tmp/linkstate-update-{update_id}.tar.gz"
    with open(tmp_path, "wb") as tf:
        tf.write(buf.read())

    with _update_cache_lock:
        _update_cache[update_id] = {
            "tmp_path":        tmp_path,
            "version":         incoming_version,
            "changelog_entry": changelog_entry,
        }

    return jsonify({
        "update_id":       update_id,
        "current_version": current_version,
        "incoming_version": incoming_version,
        "changelog_entry": changelog_entry,
        "file_count":      len(py_files),
    })


@app.route("/api/update/apply", methods=["POST"])
@login_required
def api_update_apply():
    """Apply a validated update bundle with live SSE output."""
    import tarfile
    body      = request.get_json(silent=True) or {}
    update_id = body.get("update_id", "")

    with _update_cache_lock:
        cache = _update_cache.get(update_id)
    if not cache:
        return jsonify({"error": "Update session expired — upload bundle again"}), 400

    job_id  = "update-" + str(uuid.uuid4())[:8]
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        if len(_job_log_history) >= _MAX_HISTORY_JOBS:
            oldest = sorted(_job_log_history.keys())[0]
            del _job_log_history[oldest]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":   job_id,
            "path_id":  "update",
            "label":    f"Update to v{cache['version']}",
            "status":   "queued",
            "started":  datetime.now(timezone.utc).isoformat(),
            "finished": None, "success": None, "error": None,
        }

    def run_update():
        import subprocess, tarfile
        _log = logging.getLogger("update")
        _log.setLevel(logging.DEBUG)
        _log.addHandler(handler)

        with _jobs_lock:
            _jobs[job_id]["status"] = "running"

        try:
            tmp_path = cache["tmp_path"]
            version  = cache["version"]
            extract_dir = f"/tmp/linkstate-update-apply-{update_id}"

            _log.info(f"── LinkState Update ──")
            _log.info(f"  Installing v{version}...")

            # Step 1: Extract bundle
            _log.info("  Extracting bundle...")
            os.makedirs(extract_dir, exist_ok=True)
            with tarfile.open(tmp_path, "r:gz") as tar:
                tar.extractall(extract_dir)

            # Find the root dir inside the tarball
            contents = os.listdir(extract_dir)
            src_dir  = os.path.join(extract_dir, contents[0]) if len(contents) == 1 else extract_dir
            _log.info(f"  ✓ Extracted to {src_dir}")

            # Step 2: Snapshot current version
            current_ver = _read_version()
            snap_ts  = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            snap_dir = os.path.join(_SNAPSHOTS_DIR, f"{current_ver}-{snap_ts}")
            _log.info(f"  Snapshotting current v{current_ver}...")
            os.makedirs(snap_dir, exist_ok=True)
            result = subprocess.run([
                "rsync", "-a",
                "--exclude=config/",
                "--exclude=logs/",
                "--exclude=results/",
                "--exclude=packages/",
                "--exclude=snapshots/",
                "--exclude=.ssh/",
                "--exclude=ssl/",
                "--exclude=venv/",
                f"{_APP_DIR}/", f"{snap_dir}/"
            ], capture_output=True, text=True)
            if result.returncode == 0:
                _log.info(f"  ✓ Snapshot saved: snapshots/{current_ver}-{snap_ts}")
            else:
                _log.warning(f"  ⚠ Snapshot warning: {result.stderr[:100]}")

            # Evict old snapshots (keep 3)
            if os.path.isdir(_SNAPSHOTS_DIR):
                snaps = sorted(os.listdir(_SNAPSHOTS_DIR), reverse=True)
                for old in snaps[3:]:
                    import shutil
                    shutil.rmtree(os.path.join(_SNAPSHOTS_DIR, old), ignore_errors=True)
                    _log.info(f"  Removed old snapshot: {old}")

            # Step 3: Backup SSH keys and SSL certs before sync
            import shutil as _shutil
            key_file = os.path.expanduser(_config.ssh_defaults.key_file)
            key_backup = {}
            for kpath in [key_file, key_file + ".pub"]:
                if os.path.exists(kpath):
                    key_backup[kpath] = open(kpath, "rb").read()
            _log.info(f"  Backed up {len(key_backup)} key file(s)")

            # Step 4: Sync new code
            _log.info("  Syncing new code files...")
            # Only config.yaml is the user's. The release also ships other
            # files in config/ (config.example.yaml), so they must be
            # updated — same as install.sh, which excludes only
            # config/config.yaml.
            result = subprocess.run([
                "rsync", "-a",
                "--exclude=config/config.yaml",
                "--exclude=logs/",
                "--exclude=results/",
                "--exclude=packages/",
                "--exclude=snapshots/",
                "--exclude=.ssh/",
                "--exclude=ssl/",
                "--exclude=venv/",
                "--exclude=vendor/",
                "--exclude=.release_info",
                f"{src_dir}/", f"{_APP_DIR}/"
            ], capture_output=True, text=True)
            if result.returncode == 0:
                _log.info("  ✓ Code files updated")
            else:
                raise RuntimeError(f"rsync failed: {result.stderr}")

            # Bundled wheels replace the old set so pip installs offline.
            # The bundled .debs are skipped — system packages aren't
            # changed by web updates.
            src_wheels = os.path.join(src_dir, "vendor", "wheels")
            if os.path.isdir(src_wheels):
                os.makedirs(os.path.join(_APP_DIR, "vendor", "wheels"), exist_ok=True)
                result = subprocess.run([
                    "rsync", "-a", "--delete",
                    f"{src_wheels}/", f"{_APP_DIR}/vendor/wheels/"
                ], capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(f"rsync of bundled wheels failed: {result.stderr}")

            # Refresh the agent packages staged for air-gapped onboarding
            src_debs = os.path.join(src_dir, "vendor", "debs")
            if os.path.isdir(src_debs):
                try:
                    from core.agent_packages import stage_bundled
                    stage_bundled(src_debs, _packages_dir, log=lambda m: _log.info(f"  ✓ {m}"))
                except Exception as e:
                    _log.warning(f"  ⚠ Couldn't stage bundled agent packages: {e}")

            # Restore SSH keys if they were wiped (e.g. key outside .ssh/ dir)
            restored = 0
            for kpath, kdata in key_backup.items():
                if not os.path.exists(kpath):
                    os.makedirs(os.path.dirname(kpath), exist_ok=True)
                    with open(kpath, "wb") as fh:
                        fh.write(kdata)
                    os.chmod(kpath, 0o600 if not kpath.endswith(".pub") else 0o644)
                    restored += 1
            if restored:
                _log.info(f"  ✓ Restored {restored} SSH key file(s) after sync")

            # Step 4: Update pip dependencies
            _log.info("  Updating Python dependencies...")  # Step 5
            result = subprocess.run(
                _pip_install_cmd(),
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                _log.info("  ✓ Dependencies up to date")
            else:
                _log.warning(f"  ⚠ pip warning: {result.stderr[:200]}")

            # Clean up before restarting: a successful web restart ends this
            # process.
            import shutil
            shutil.rmtree(extract_dir, ignore_errors=True)
            os.unlink(tmp_path)
            with _update_cache_lock:
                _update_cache.pop(update_id, None)

            if branding.LEGACY_INSTALL:
                _log.warning(f"  ⚠ {branding.MIGRATE_HINT}")

            # Step 5: Restart services
            _restart_services_for_job(job_id, _log, f"v{version} installed")

        except Exception as e:
            _log.error(f"  ✗ Update failed: {e}")
            with _jobs_lock:
                _jobs[job_id]["status"]   = "error"
                _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
                _jobs[job_id]["error"]    = str(e)

    threading.Thread(target=run_update, daemon=True, name="update").start()
    return jsonify({"job_id": job_id, "status": "queued"})


def _is_pre_rename_snapshot(snap_path: str) -> bool:
    """True if a snapshot holds NetTest code from before the LinkState rename."""
    return (os.path.exists(os.path.join(snap_path, "systemd", "nettest.service"))
            and not os.path.exists(os.path.join(snap_path, "systemd", "linkstate.service")))


@app.route("/api/update/rollback", methods=["POST"])
@login_required
def api_update_rollback():
    """Rollback to a named snapshot."""
    import subprocess
    body     = request.get_json(silent=True) or {}
    snapshot = body.get("snapshot", "").strip()

    if not snapshot or "/" in snapshot or ".." in snapshot:
        return jsonify({"error": "Invalid snapshot name"}), 400

    snap_path = os.path.join(_SNAPSHOTS_DIR, snapshot)
    if not os.path.isdir(snap_path):
        return jsonify({"error": f"Snapshot not found: {snapshot}"}), 404

    if _is_pre_rename_snapshot(snap_path) and not branding.LEGACY_INSTALL:
        return jsonify({"error": (
            f"{snapshot} is a NetTest snapshot from before the rename to LinkState. "
            "Its code expects the old service names, user and paths, so it can't "
            "be restored onto this LinkState install. Reinstall that release instead."
        )}), 400

    job_id  = "rollback-" + str(uuid.uuid4())[:8]
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":   job_id,
            "path_id":  "rollback",
            "label":    f"Rollback to {snapshot}",
            "status":   "queued",
            "started":  datetime.now(timezone.utc).isoformat(),
            "finished": None, "success": None, "error": None,
        }

    def run_rollback():
        _log = logging.getLogger("rollback")
        _log.setLevel(logging.DEBUG)
        _log.addHandler(handler)
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"

        try:
            snap_ver = _read_version(os.path.join(snap_path, "version.txt"))
            _log.info(f"── LinkState Rollback ──")
            _log.info(f"  Restoring v{snap_ver} from {snapshot}...")

            result = subprocess.run([
                "rsync", "-a",
                "--exclude=config/",
                "--exclude=logs/",
                "--exclude=results/",
                "--exclude=packages/",
                "--exclude=snapshots/",
                "--exclude=.ssh/",
                "--exclude=ssl/",
                "--exclude=venv/",
                f"{snap_path}/", f"{_APP_DIR}/"
            ], capture_output=True, text=True)

            if result.returncode != 0:
                raise RuntimeError(f"rsync failed: {result.stderr}")
            _log.info("  ✓ Code files restored")

            subprocess.run(_pip_install_cmd(),
                           capture_output=True, timeout=120)
            _log.info("  ✓ Dependencies synced")

            _restart_services_for_job(job_id, _log, f"v{snap_ver} restored")

        except Exception as e:
            _log.error(f"  ✗ Rollback failed: {e}")
            with _jobs_lock:
                _jobs[job_id]["status"]   = "error"
                _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
                _jobs[job_id]["error"]    = str(e)

    threading.Thread(target=run_rollback, daemon=True, name="rollback").start()
    return jsonify({"job_id": job_id, "status": "queued"})



# ── Live Output & Abort ───────────────────────────────────

@app.route("/live")
@login_required
def live_output_page():
    """Standalone live output page — opened as a popout window."""
    if _config and _config.auth.method and not session.get("authenticated"):
        return ("<html><body style='background:#0a0c10;color:#f44336;"
                "font-family:monospace;padding:40px;font-size:14px'>"
                "Not authenticated. Log in to the dashboard first.</body></html>"), 401
    job_id = request.args.get("job_id", "")
    # The live page has its own colours, so it only gets the title and favicon.
    return _apply_branding(_render_live_page(job_id), "Live Output", theme=False)


def _render_live_page(job_id: str) -> str:
    """Return the live output page HTML (kept out of f-string to allow JS braces)."""
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Live Output</title>
<!--LS:HEAD-->
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0c10;color:#c8cdd6;font-family:'Menlo','Consolas',monospace;
         font-size:12px;display:flex;flex-direction:column;height:100vh}
    #header{padding:10px 16px;background:#12151c;border-bottom:1px solid #1e2330;
            display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
    #job-label{font-size:13px;font-weight:600;color:#5bc4f5}
    #job-meta{font-size:11px;color:#5a6070;margin-top:2px}
    #status-badge{font-size:11px;padding:3px 10px;border-radius:12px;
                  background:#1a2a1a;color:#4caf50;border:1px solid #2a4a2a}
    #toolbar{padding:6px 16px;background:#0e1118;border-bottom:1px solid #1e2330;
             display:flex;gap:8px;align-items:center;flex-shrink:0}
    .btn{font-size:11px;padding:4px 12px;border-radius:4px;cursor:pointer;
         border:1px solid #2a3040;background:#1a2030;color:#c8cdd6}
    .btn:hover{background:#243040}
    .btn-abort{border-color:#8b2020;background:#1a0808;color:#ff6060}
    .btn-abort:hover{background:#2a0808}
    .btn-abort:disabled{opacity:0.4;cursor:default}
    #elapsed{font-size:11px;color:#5a6070;margin-left:auto}
    #output{flex:1;overflow-y:auto;padding:12px 16px;scroll-behavior:smooth}
    .line{padding:1px 0;white-space:pre-wrap;line-height:1.5}
    .ok{color:#4caf50}.err{color:#f44336}.warn{color:#ff9800}
    .info{color:#c8cdd6}.dim{color:#5a6070}.iperf{color:#5bc4f5}
    #footer{padding:6px 16px;background:#0e1118;border-top:1px solid #1e2330;
            font-size:11px;color:#5a6070;flex-shrink:0}
  </style>
</head>
<body>
<div id="header">
  <div>
    <div id="job-label">Connecting...</div>
    <div id="job-meta">Job: """ + job_id + """</div>
  </div>
  <div id="status-badge">● connecting</div>
</div>
<div id="toolbar">
  <button class="btn" onclick="copyAll()">&#10088; Copy</button>
  <button class="btn" onclick="clearOut()">&times; Clear</button>
  <button class="btn btn-abort admin-only" id="abort-btn" onclick="doAbort()">&#9209; Abort</button>
  <span id="elapsed">0s</span>
  <label style="display:flex;align-items:center;gap:6px;font-size:11px;cursor:pointer;margin-left:auto">
    <input type="checkbox" id="asc" checked> Auto-scroll
  </label>
</div>
<div id="output"></div>
<div id="footer"><span id="lc">0 lines</span></div>
<script>
const JOB  = '""" + job_id + r"""';
const out  = document.getElementById('output');
const badge= document.getElementById('status-badge');
const lbl  = document.getElementById('job-label');
const abrt = document.getElementById('abort-btn');
const elps = document.getElementById('elapsed');
const lc   = document.getElementById('lc');
let lines=0, start=Date.now(), done=false, es=null;

setInterval(()=>{
  const s=Math.floor((Date.now()-start)/1000);
  elps.textContent=s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s';
},1000);

function cls(t){
  if(/PASS|completed successfully/.test(t))return 'ok';
  if(/ERROR|FAIL|failed/.test(t))return 'err';
  if(/WARNING/.test(t))return 'warn';
  if(/\[\s*\d+\].*Mbits\/sec/.test(t))return 'iperf';
  if(/\[INFO\]/.test(t))return 'dim';
  return 'info';
}

function addLine(t){
  const d=document.createElement('div');
  d.className='line '+cls(t);
  d.textContent=t;
  out.appendChild(d);
  lines++;lc.textContent=lines+' lines';
  if(document.getElementById('asc').checked)out.scrollTop=out.scrollHeight;
}

async function loadHistory(){
  try{
    const r=await fetch('/api/jobs/'+JOB+'/log');
    const d=await r.json();
    if(d.job){
      lbl.textContent=d.job.label||JOB;
      document.title='LinkState - '+(d.job.label||JOB);
      if(d.job.started)start=new Date(d.job.started).getTime()||Date.now();
    }
    (d.lines||[]).forEach(addLine);
  }catch(e){addLine('(history load failed: '+e.message+')');}
}

function connect(){
  es=new EventSource('/api/jobs/'+JOB+'/stream?since='+lines);
  badge.textContent='running';
  badge.style.color='#4caf50';
  es.onmessage=e=>{
    const t=e.data;
    if(t==='[STREAM END]'||t==='[STREAM TIMEOUT]'){
      es.close();done=true;
      badge.textContent=t==='[STREAM END]'?'done':'timed out';
      badge.style.color='#5a6070';
      abrt.disabled=true;
      return;
    }
    if(t)addLine(t);
  };
  es.onerror=()=>{
    if(!done){badge.textContent='disconnected';badge.style.color='#ff9800';}
    es.close();
  };
}

async function doAbort(){
  if(!confirm('Abort? Current test finishes, then job stops.'))return;
  abrt.disabled=true;abrt.textContent='Aborting...';
  await fetch('/api/jobs/'+JOB+'/abort',{method:'POST'}).catch(()=>{});
}

function copyAll(){
  const t=Array.from(out.children).map(d=>d.textContent).join('\n');
  navigator.clipboard.writeText(t).then(()=>{
    const b=event.target;b.textContent='Copied';
    setTimeout(()=>{b.textContent='Copy';},1500);
  });
}
function clearOut(){out.innerHTML='';lines=0;lc.textContent='0 lines';}

loadHistory().then(()=>{if(!done)connect();});
</script>
</body>
</html>"""



@app.route("/api/jobs/<job_id>/abort", methods=["POST"])
@login_required
def api_job_abort(job_id: str):
    """Signal a running job to abort after its current test completes."""
    with _abort_lock:
        event = _abort_events.get(job_id)
    if not event:
        # Job may already be done
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job and job.get("status") in ("done", "error"):
            return jsonify({"ok": True, "note": "Job already finished"})
        return jsonify({"error": "Job not found or not abortable"}), 404

    event.set()
    logging.getLogger(__name__).info(
        f"Abort requested for job {job_id}"
    )

    # Also log it into the job's history (picked up by the live tail poller)
    msg = "⚠ Abort requested — stopping after current test completes..."
    with _job_logs_lock:
        _job_log_history.setdefault(job_id, []).append(msg)

    return jsonify({"ok": True})


# ── HTTPS / nginx Management API ─────────────────────────

SSL_DIR  = branding.SSL_DIR
SSL_CERT = branding.SSL_CERT
SSL_KEY  = branding.SSL_KEY
NGINX_CONF = f"/etc/nginx/sites-available/{branding.NGINX_SITE}"


def _nginx_running() -> bool:
    try:
        import subprocess
        r = subprocess.run(["systemctl", "is-active", "nginx"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _cert_info(cert_path: str) -> dict:
    try:
        import subprocess
        r = subprocess.run(
            ["openssl", "x509", "-in", cert_path, "-noout",
             "-subject", "-dates", "-fingerprint", "-sha256"],
            capture_output=True, text=True, timeout=10
        )
        lines = r.stdout.strip().splitlines()
        info = {}
        for line in lines:
            if line.startswith("subject="):
                info["subject"] = line.split("=", 1)[1].strip()
            elif line.startswith("notBefore="):
                info["not_before"] = line.split("=", 1)[1].strip()
            elif line.startswith("notAfter="):
                info["not_after"] = line.split("=", 1)[1].strip()
            elif "Fingerprint=" in line:
                info["fingerprint"] = line.split("=", 1)[1].strip()
        return info
    except Exception as e:
        return {"error": str(e)}


@app.route("/api/https/status")
@login_required
def api_https_status():
    nginx_ok  = _nginx_running()
    cert_info = _cert_info(SSL_CERT) if os.path.exists(SSL_CERT) else {}
    return jsonify({
        "nginx_running": nginx_ok,
        "cert_exists":   os.path.exists(SSL_CERT),
        "nginx_conf":    os.path.exists(NGINX_CONF),
        "cert_info":     cert_info,
    })


@app.route("/api/https/generate", methods=["POST"])
@login_required
def api_https_generate():
    """Generate a new self-signed certificate."""
    import subprocess
    body       = request.get_json(silent=True) or {}
    cn         = body.get("cn", "linkstate").strip()
    org        = body.get("org", "LinkState").strip()
    days       = int(body.get("days", 3650))
    san_ips    = body.get("san_ips", [])   # list of IPs and/or hostnames

    # Ensure SSL dir exists and is writable
    # Run: sudo chown linkstate:linkstate <app dir>/ssl  if this fails
    os.makedirs(SSL_DIR, exist_ok=True)

    sans = [str(x).strip() for x in san_ips if str(x).strip()] or [cn]
    for name in sans:
        err = validate_host(name[2:] if name.startswith("*.") else name)
        if err:
            return jsonify({"error": f"Certificate name {err}"}), 400
    if any(c in cn for c in "/=\\"):
        return jsonify({"error": "Common Name can't contain / = or \\"}), 400
    san_str = ",".join(f"IP:{n}" if is_ip(n) else f"DNS:{n}" for n in sans)

    cmd = [
        "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:4096",
        "-keyout", SSL_KEY, "-out", SSL_CERT,
        "-days", str(days),
        "-subj", f"/CN={cn}/O={org}",
        "-addext", f"subjectAltName={san_str}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "openssl failed"}), 500

    os.chmod(SSL_KEY,  0o600)
    os.chmod(SSL_CERT, 0o644)

    # Write nginx config if not present
    _write_nginx_conf()

    # Reload nginx
    reload = subprocess.run(
        ["sudo", "systemctl", "reload-or-restart", "nginx"],
        capture_output=True, text=True, timeout=15
    )

    return jsonify({
        "ok":        True,
        "cert_info": _cert_info(SSL_CERT),
        "nginx_ok":  reload.returncode == 0,
    })


@app.route("/api/https/upload", methods=["POST"])
@login_required
def api_https_upload():
    """Upload a certificate and key."""
    import subprocess
    if "cert" not in request.files or "key" not in request.files:
        return jsonify({"error": "Both cert and key files are required"}), 400

    cert_content = request.files["cert"].read().decode("utf-8")
    key_content  = request.files["key"].read().decode("utf-8")

    if "BEGIN CERTIFICATE" not in cert_content:
        return jsonify({"error": "Invalid certificate format"}), 400
    if "BEGIN" not in key_content:
        return jsonify({"error": "Invalid key format"}), 400

    os.makedirs(SSL_DIR, exist_ok=True)
    with open(SSL_CERT, "w") as f: f.write(cert_content)
    with open(SSL_KEY,  "w") as f: f.write(key_content)
    os.chmod(SSL_KEY,  0o600)
    os.chmod(SSL_CERT, 0o644)

    # Verify cert and key match
    r_cert = subprocess.run(["openssl", "x509", "-modulus", "-noout", "-in", SSL_CERT],
                             capture_output=True, text=True)
    r_key  = subprocess.run(["openssl", "rsa",  "-modulus", "-noout", "-in", SSL_KEY],
                             capture_output=True, text=True)
    if r_cert.returncode != 0 or r_key.returncode != 0:
        return jsonify({"error": "Could not verify certificate/key"}), 400
    if r_cert.stdout.strip() != r_key.stdout.strip():
        return jsonify({"error": "Certificate and key do not match"}), 400

    _write_nginx_conf()
    reload = subprocess.run(
        ["sudo", "systemctl", "reload-or-restart", "nginx"],
        capture_output=True, text=True, timeout=15
    )
    return jsonify({
        "ok":        True,
        "cert_info": _cert_info(SSL_CERT),
        "nginx_ok":  reload.returncode == 0,
    })


@app.route("/api/https/nginx/start", methods=["POST"])
@login_required
def api_https_nginx_start():
    import subprocess
    _write_nginx_conf()
    subprocess.run(["sudo", "systemctl", "enable", "nginx"],
                   capture_output=True, timeout=10)
    r = subprocess.run(["sudo", "systemctl", "restart", "nginx"],
                       capture_output=True, text=True, timeout=15)
    return jsonify({"ok": r.returncode == 0, "output": r.stderr})


@app.route("/api/https/nginx/stop", methods=["POST"])
@login_required
def api_https_nginx_stop():
    import subprocess
    r = subprocess.run(["sudo", "systemctl", "stop", "nginx"],
                       capture_output=True, text=True, timeout=15)
    return jsonify({"ok": r.returncode == 0})


def _write_nginx_conf():
    """Write the nginx reverse proxy config using sudo tee (file is root-owned)."""
    # Skip if already present — install.sh writes this during initial setup
    if os.path.exists(NGINX_CONF):
        return

    conf = """server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     __SSL_CERT__;
    ssl_certificate_key __SSL_KEY__;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    client_max_body_size      100M;
    proxy_buffering           off;
    proxy_cache               off;
    chunked_transfer_encoding on;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Connection        "";
        proxy_read_timeout 300s;
    }
}
""".replace("__SSL_CERT__", SSL_CERT).replace("__SSL_KEY__", SSL_KEY)
    import subprocess
    # Write via sudo tee since /etc/nginx is root-owned
    proc = subprocess.run(
        ["sudo", "tee", NGINX_CONF],
        input=conf, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise PermissionError(f"Could not write nginx config: {proc.stderr}")

    link = f"/etc/nginx/sites-enabled/{branding.NGINX_SITE}"
    if not os.path.exists(link):
        subprocess.run(["sudo", "ln", "-sf", NGINX_CONF, link], capture_output=True)
    subprocess.run(["sudo", "rm", "-f", "/etc/nginx/sites-enabled/default"],
                   capture_output=True)


# ── Package management API ────────────────────────────────

@app.route("/api/packages")
@login_required
def api_packages_list():
    """List staged .deb files."""
    files = []
    for f in sorted(os.listdir(_packages_dir)):
        if f.endswith(".deb"):
            fp = os.path.join(_packages_dir, f)
            stat = os.stat(fp)
            files.append({
                "name":     f,
                "size":     stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
    return jsonify(files)


@app.route("/api/packages/upload", methods=["POST"])
@login_required
def api_packages_upload():
    """Upload a .deb file to the staging area."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.endswith(".deb"):
        return jsonify({"error": "Only .deb files are accepted"}), 400
    safe_name = os.path.basename(f.filename)
    dest = os.path.join(_packages_dir, safe_name)
    f.save(dest)
    return jsonify({"ok": True, "name": safe_name})


@app.route("/api/packages/delete/<filename>", methods=["DELETE"])
@login_required
def api_packages_delete(filename: str):
    """Delete a staged .deb file."""
    safe_name = os.path.basename(filename)
    dest = os.path.join(_packages_dir, safe_name)
    if not os.path.exists(dest):
        return jsonify({"error": "File not found"}), 404
    os.remove(dest)
    return jsonify({"ok": True})


@app.route("/api/packages/push/<agent_id>", methods=["POST"])
@login_required
def api_packages_push(agent_id: str):
    """SCP staged packages to an agent and install with dpkg."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503

    agent = _config.get_agent(agent_id)
    if not agent:
        return jsonify({"error": f"Agent '{agent_id}' not found"}), 404

    deb_files = [f for f in os.listdir(_packages_dir) if f.endswith(".deb")]
    if not deb_files:
        return jsonify({"error": "No .deb files staged — upload packages first"}), 400

    job_id  = "pkg-push-" + str(uuid.uuid4())[:8]
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        if len(_job_log_history) >= _MAX_HISTORY_JOBS:
            oldest = sorted(_job_log_history.keys())[0]
            del _job_log_history[oldest]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":   job_id,
            "path_id":  "packages",
            "label":    f"Push packages → {agent.label}",
            "status":   "queued",
            "started":  datetime.now(timezone.utc).isoformat(),
            "finished": None, "success": None, "error": None,
        }

    def run_push():
        import subprocess
        root = logging.getLogger()
        root.addHandler(handler)
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
        try:
            key_file    = _config.ssh_defaults.key_file
            linkstate_usr = agent.username or _config.ssh_defaults.username
            port        = agent.port or _config.ssh_defaults.port
            remote_tmp  = "/tmp/linkstate_packages"

            _push_log = logging.getLogger("packages")
            _push_log.info(f"Pushing {len(deb_files)} package(s) to {agent.label} ({agent.host_mgmt_ip})...")

            # Create remote tmp dir
            subprocess.run([
                "ssh", "-i", key_file, "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", "-p", str(port),
                f"{linkstate_usr}@{agent.host_mgmt_ip}",
                f"mkdir -p {remote_tmp}"
            ], capture_output=True, timeout=30)

            # SCP each file
            for deb in sorted(deb_files):
                local_path = os.path.join(_packages_dir, deb)
                _push_log.info(f"  Copying {deb}...")
                proc = subprocess.run([
                    "scp", "-i", key_file,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes",
                    "-P", str(port),
                    local_path,
                    f"{linkstate_usr}@{agent.host_mgmt_ip}:{remote_tmp}/{deb}",
                ], capture_output=True, timeout=120)
                if proc.returncode == 0:
                    _push_log.info(f"  ✓ {deb}")
                else:
                    _push_log.error(f"  ✗ {deb}: {proc.stderr.decode()[:100]}")

            # Install
            _push_log.info("Installing packages with dpkg...")
            proc = subprocess.run([
                "ssh", "-i", key_file, "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", "-p", str(port),
                f"{linkstate_usr}@{agent.host_mgmt_ip}",
                f"sudo dpkg -i {remote_tmp}/*.deb && rm -rf {remote_tmp}"
            ], capture_output=True, timeout=120)
            if proc.returncode == 0:
                _push_log.info("✓ Packages installed successfully")
            else:
                _push_log.error(f"dpkg failed: {proc.stderr.decode()[:200]}")

            with _jobs_lock:
                _jobs[job_id]["status"]   = "done"
                _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
                _jobs[job_id]["success"]  = True
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]["status"]  = "error"
                _jobs[job_id]["finished"]= datetime.now(timezone.utc).isoformat()
                _jobs[job_id]["error"]   = str(e)
        finally:
            root.removeHandler(handler)

    threading.Thread(target=run_push, daemon=True,
                     name=f"pkg-push-{agent_id}").start()
    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/packages/push_all", methods=["POST"])
@login_required
def api_packages_push_all():
    """Push staged packages to all endpoint agents."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    results = []
    for agent in _config.agents:
        if agent.type == "svi_adjacent":
            continue
        resp = api_packages_push(agent.id)
        results.append({"agent_id": agent.id, "job_id": resp.get_json().get("job_id")})
    return jsonify(results)


@app.route("/api/agents")
@login_required
def api_agents():
    if not _config:
        return jsonify([])
    return jsonify([
        {
            "id":           a.id,
            "label":        a.label,
            "host_mgmt_ip": a.host_mgmt_ip,
            "host_test_ip": a.host_test_ip,
            "type":         a.type,
            "address_family": a.address_family,
            "resolve_on":   a.resolve_on,
            "tcp_port":     a.tcp_port,
            "latency_method": a.latency_method,
        }
        for a in _config.agents
    ])


@app.route("/api/hops/<path_id>")
@login_required
def api_hops(path_id: str):
    records = _load_records(minutes=1440, path_id=path_id)
    for r in reversed(records):
        if r.get("latency_under_load") and r["latency_under_load"].get("mtr_hops"):
            return jsonify({
                "path_id":    path_id,
                "path_label": r["path_label"],
                "timestamp":  r["timestamp_utc"],
                "hops":       r["latency_under_load"]["mtr_hops"],
            })
    return jsonify({"path_id": path_id, "hops": []})





# ── Trigger API ────────────────────────────────────────────

@app.route("/api/run/<path_id>", methods=["POST"])
@login_required
def api_run_path(path_id: str):
    body             = request.get_json(silent=True) or {}
    test_filter      = body.get("tests")
    direction_filter = body.get("directions")

    path = next((p for p in _config.paths if p.id == path_id), None)
    if not path:
        return jsonify({"error": f"Path '{path_id}' not found"}), 404

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":   job_id,
            "path_id":  path_id,
            "label":    path.label,
            "status":   "queued",
            "started":  datetime.now(timezone.utc).isoformat(),
            "finished": None,
            "success":  None,
            "error":    None,
        }

    threading.Thread(
        target=_run_job, args=(job_id, path_id, test_filter, direction_filter),
        daemon=True, name=f"job-{job_id}"
    ).start()

    return jsonify({"job_id": job_id, "path_id": path_id, "status": "queued"})


@app.route("/api/run/all", methods=["POST"])
@login_required
def api_run_all():
    body        = request.get_json(silent=True) or {}
    test_filter = body.get("tests")
    job_ids     = []
    # Same stagger as scheduled runs, so the paths don't all SSH in and start
    # at once. Paths sharing a host are serialized by core/host_locks.py anyway.
    stagger     = _config.schedule.stagger_seconds

    for i, path in enumerate(_config.paths):
        job_id = str(uuid.uuid4())[:8]
        with _jobs_lock:
            _jobs[job_id] = {
                "job_id":   job_id,
                "path_id":  path.id,
                "label":    path.label,
                "status":   "queued",
                "started":  datetime.now(timezone.utc).isoformat(),
                "finished": None,
                "success":  None,
                "error":    None,
            }
        threading.Thread(
            target=_run_job, args=(job_id, path.id, test_filter),
            kwargs={"start_delay": i * stagger},
            daemon=True, name=f"job-{job_id}"
        ).start()
        job_ids.append(job_id)

    return jsonify({"jobs": job_ids, "count": len(job_ids)})


@app.route("/api/jobs")
@login_required
def api_jobs():
    with _jobs_lock:
        jobs = list(_jobs.values())
    return jsonify(list(reversed(jobs[-50:])))


@app.route("/api/jobs/<job_id>")
@login_required
def api_job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>/log")
@login_required
def api_job_log(job_id: str):
    """Return accumulated log lines for a completed or running job."""
    with _job_logs_lock:
        lines = list(_job_log_history.get(job_id, []))
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    status = job.get("status", "")
    err    = job.get("error") or ""
    if status in ("done", "error") and lines:
        if status == "done" and not err:
            lines.append("✓ Test completed successfully")
        elif err:
            lines.append("✗ Finished with error: " + err)
    return jsonify({"job_id": job_id, "lines": lines, "job": job})


@app.route("/api/jobs/<job_id>/stream")
@login_required
def api_job_stream(job_id: str):
    """SSE stream — tails the job's log history.

    Reads _job_log_history by index rather than draining a per-job queue, so
    any number of viewers (including a reloaded /live tab racing a
    not-yet-closed old connection) can tail the same job independently with
    no lines stolen out from under each other. ``since`` lets a caller that
    already has the first N lines (e.g. from /log) resume without
    re-sending them.
    """
    NL = "\n"
    try:
        start_idx = max(0, int(request.args.get("since", 0)))
    except (TypeError, ValueError):
        start_idx = 0

    def generate():
        with _jobs_lock:
            exists = job_id in _jobs
        if not exists:
            yield "data: [Job " + job_id + "] Status: unknown" + NL + NL
            yield "data: [STREAM END]" + NL + NL
            return

        pos  = start_idx
        idle = 0.0
        while True:
            with _job_logs_lock:
                hist      = _job_log_history.get(job_id, [])
                new_lines = hist[pos:]
                pos       = len(hist)

            if new_lines:
                idle = 0.0
                for line in new_lines:
                    safe = line.replace(NL, " | ")
                    yield "data: " + safe + NL + NL

            with _jobs_lock:
                job = dict(_jobs.get(job_id, {}))

            status = job.get("status", "")
            if status in ("done", "error"):
                err = job.get("error") or ""
                yield "data: " + NL + NL
                if status == "done" and not err:
                    yield "data: \u2713 Test completed successfully" + NL + NL
                elif err:
                    yield "data: \u2717 Finished with error: " + err + NL + NL
                yield "data: [STREAM END]" + NL + NL
                break

            if not job:   # evicted from memory — nothing more will ever arrive
                yield "data: [STREAM END]" + NL + NL
                break

            if idle >= 120:   # covers throughput + latency-under-load stalls
                yield "data: [STREAM TIMEOUT]" + NL + NL
                break

            time.sleep(0.3)
            idle += 0.3

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Config API ─────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
@login_required
def api_config_get():
    import yaml
    with open(_config_path, 'r') as f:
        raw = yaml.safe_load(f)
    # Keep secrets out of the browser. Saves preserve them: blank or missing
    # values keep what's on disk (see api_config_save).
    auth = raw.setdefault('auth', {})
    # Show the method actually in effect — blank in config.yaml can still mean
    # RADIUS/local by inference (see load_config).
    if _config:
        auth['method'] = _config.auth.method
        for key in ('radius_role_attribute', 'radius_admin_value',
                    'radius_readonly_value', 'radius_default_role'):
            auth[key] = getattr(_config.auth, key)
    if 'radius_secret' in auth:
        auth['radius_secret_set'] = bool(auth.pop('radius_secret'))
    auth.pop('session_secret', None)
    for u in auth.get('local_users') or []:
        u.pop('password_hash', None)
        u['role'] = normalize_role(u.get('role'))
    ssh = raw.get('ssh_defaults') or {}
    if 'password' in ssh:
        ssh['password_set'] = bool(ssh.pop('password'))
    # Effective values (defaults filled in) so the Thresholds tab is never blank.
    raw['thresholds'] = _config.thresholds if _config else parse_thresholds(raw.get('thresholds'))[0]
    raw['branding'] = asdict(_config.branding if _config else parse_branding(raw.get('branding'))[0])
    raw['branding_assets'] = {k: branding.asset_url(k) for k in branding.ASSET_KINDS}
    return jsonify(raw)


@app.route('/api/schedule')
@login_required
def api_schedule():
    """Schedule only — what the dashboard's next-run countdown needs, without
    the rest of the config (which read-only accounts can't see)."""
    import yaml
    with open(_config_path, 'r') as f:
        raw = yaml.safe_load(f) or {}
    return jsonify(raw.get('schedule') or {})


@app.route('/api/thresholds')
@login_required
def api_thresholds():
    return jsonify(_config.thresholds if _config else THRESHOLD_DEFAULTS)


@app.route('/api/config', methods=['POST'])
@login_required
def api_config_save():
    import yaml
    global _config, _tester

    body = request.get_json(silent=True)
    if not body:
        return jsonify({'error': 'No JSON body'}), 400

    with open(_config_path, 'r') as f:
        raw = yaml.safe_load(f)

    if 'agents' in body:
        agents_in = []
        errs = []
        for a in body['agents'] or []:
            a = dict(a)
            for k in ('host_mgmt_ip', 'host_test_ip'):
                if isinstance(a.get(k), str):
                    a[k] = a[k].strip()
            if not a.get('host_test_ip'):
                a.pop('host_test_ip', None)
            # Leave defaults out of config.yaml so it stays as before for
            # anyone not using them.
            if a.get('address_family') in (None, '', 'ipv4'):
                a.pop('address_family', None)
            if a.get('resolve_on') in (None, '', 'controller'):
                a.pop('resolve_on', None)
            # tcp_port / latency_method only mean something for a web_server
            if a.get('type') != 'web_server' or not a.get('tcp_port'):
                a.pop('tcp_port', None)
            if a.get('type') != 'web_server' or a.get('latency_method') in (None, '', 'auto'):
                a.pop('latency_method', None)
            errs.extend(agent_host_errors(a))
            agents_in.append(a)
        if errs:
            # 200, like the other save errors — the frontend reads {error}.
            return jsonify({'error': 'Invalid agent host: ' + '; '.join(errs)})
        raw['agents'] = agents_in
    if 'paths'       in body: raw['paths']        = body['paths']
    if 'test_params' in body: raw['test_params']  = body['test_params']
    if 'ssh_defaults'in body:
        incoming_ssh = dict(body['ssh_defaults'])
        incoming_ssh.pop('password_set', None)          # display-only flag from GET
        if not incoming_ssh.get('password'):            # never sent to the browser
            incoming_ssh.pop('password', None)
        raw['ssh_defaults'].update(incoming_ssh)
    if 'schedule'    in body: raw['schedule'].update(body['schedule'])
    if 'thresholds'  in body:
        thresholds, errs = parse_thresholds(body['thresholds'])
        if errs:
            return jsonify({'error': 'Invalid thresholds: ' + '; '.join(errs)})
        raw['thresholds'] = thresholds
    if 'branding' in body:
        brand, errs = parse_branding(body['branding'])
        if errs:
            return jsonify({'error': 'Invalid branding: ' + '; '.join(errs)})
        # Only non-default values, so config.yaml stays as before for anyone
        # not using branding.
        kept = {k: v for k, v in asdict(brand).items() if v not in ('', False)}
        if kept:
            raw['branding'] = kept
        else:
            raw.pop('branding', None)
    if 'controller' in body:
        name = ' '.join(str((body['controller'] or {}).get('name') or '').split())
        if not name:
            return jsonify({'error': 'Controller name is required'})
        if len(name) > 80:
            return jsonify({'error': 'Controller name is longer than 80 characters'})
        raw['controller']['name'] = name

    if 'auth' in body:
        raw.setdefault('auth', {})
        incoming_auth = dict(body['auth'])
        # Local users are only ever changed via /api/config/local-users, so a
        # password hash can never be overwritten (or wiped) by a generic save.
        incoming_auth.pop('local_users', None)
        incoming_auth.pop('radius_secret_set', None)   # display-only flag from GET
        incoming_auth.pop('session_secret', None)      # never sent to the browser

        # Note: 200 status (not 4xx) — the frontend's generic save handler
        # reads {ok|error} from the body rather than the HTTP status code.
        # The secret is never sent to the browser, so a blank value means
        # "keep the current one" rather than "clear it".
        if not incoming_auth.get('radius_secret'):
            incoming_auth.pop('radius_secret', None)
        if 'radius_server' in incoming_auth:
            incoming_auth['radius_server'] = str(incoming_auth['radius_server'] or '').strip()
            if incoming_auth['radius_server']:
                err = validate_host(incoming_auth['radius_server'])
                if err:
                    return jsonify({'error': f'RADIUS server {err}'})
        for key, default, lo, hi in (('radius_port', 1812, 1, 65535),
                                     ('radius_timeout', 5, 1, 60)):
            if key not in incoming_auth:
                continue
            try:
                val = int(incoming_auth[key] if incoming_auth[key] not in ('', None) else default)
            except (TypeError, ValueError):
                return jsonify({'error': f'{key} must be a number'})
            if not lo <= val <= hi:
                return jsonify({'error': f'{key} must be between {lo} and {hi}'})
            incoming_auth[key] = val

        if 'radius_role_attribute' in incoming_auth:
            if incoming_auth['radius_role_attribute'] not in RADIUS_ROLE_ATTRIBUTES:
                return jsonify({'error': 'RADIUS role attribute must be one of '
                                         + ', '.join(RADIUS_ROLE_ATTRIBUTES)})
        for key in ('radius_admin_value', 'radius_readonly_value'):
            if key in incoming_auth:
                incoming_auth[key] = str(incoming_auth[key] or '').strip()
        if (incoming_auth.get('radius_admin_value') and
                incoming_auth.get('radius_admin_value', '').lower()
                == incoming_auth.get('radius_readonly_value', '').lower()):
            return jsonify({'error': 'The RADIUS admin and read-only values must differ'})
        if 'radius_default_role' in incoming_auth:
            role = normalize_role(incoming_auth['radius_default_role'], default='')
            if not role:
                return jsonify({'error': 'RADIUS default account type must be "admin" or "readonly"'})
            incoming_auth['radius_default_role'] = role

        # "Disabled" in the UI is sent as "". Store it as "none": a blank method
        # is inferred as RADIUS/local by load_config, which wouldn't disable login.
        if 'method' in incoming_auth and not incoming_auth['method']:
            incoming_auth['method'] = 'none'

        merged = {**raw['auth'], **incoming_auth}
        new_method = merged.get('method', '')
        if new_method == 'local' and not raw['auth'].get('local_users'):
            return jsonify({'error': 'Cannot set login method to "Local accounts" — '
                                      'add a local user first.'})
        if new_method == 'local' and not _local_admins_left(raw['auth'].get('local_users') or []):
            return jsonify({'error': 'Cannot set login method to "Local accounts" — '
                                      'every local user is read-only. Make one an admin first.'})
        if new_method == 'radius' and not merged.get('radius_server'):
            return jsonify({'error': 'Cannot set login method to "RADIUS" — enter a RADIUS '
                                      'server first.'})
        if new_method == 'radius' and not merged.get('radius_secret'):
            return jsonify({'error': 'Cannot set login method to "RADIUS" — enter the RADIUS '
                                      'shared secret first.'})

        raw['auth'].update(incoming_auth)

    with open(_config_path, 'w') as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    try:
        _config = load_config(_config_path)
        _tester = PathTester(_config)
        # Restart scheduler so it picks up path/agent changes — debounced so
        # saving several times in a row doesn't restart it (and abort
        # whatever it's mid-test on) once per save.
        schedule_linkstate_restart()
        return jsonify({'ok': True, 'agents': len(_config.agents), 'paths': len(_config.paths)})
    except Exception as e:
        return jsonify({'error': f'Config saved but reload failed: {e}'}), 500


def _local_users_json():
    return [{'username': u.username, 'role': u.role} for u in _config.auth.local_users]


def _local_admins_left(users_raw) -> int:
    return sum(1 for u in users_raw if normalize_role(u.get('role')) == ROLE_ADMIN)


@app.route('/api/config/local-users', methods=['GET'])
@login_required
def api_local_users_list():
    return jsonify({'users': _local_users_json()})


@app.route('/api/config/local-users', methods=['POST'])
@login_required
def api_local_users_add():
    """Add a new local user, or update an existing one's password and/or
    account type (upsert). Password may be left blank when only the account
    type of an existing user changes."""
    import yaml
    from werkzeug.security import generate_password_hash
    global _config, _tester

    body     = request.get_json(silent=True) or {}
    username = (body.get('username') or '').strip()
    password = body.get('password') or ''
    role_in  = body.get('role')
    role     = normalize_role(role_in, default='')
    if role_in not in (None, '') and not role:
        return jsonify({'error': 'Account type must be "admin" or "readonly"'}), 400

    if not username:
        return jsonify({'error': 'Username is required'}), 400

    with open(_config_path, 'r') as f:
        raw = yaml.safe_load(f)

    auth  = raw.setdefault('auth', {})
    users = auth.setdefault('local_users', [])
    existing = next((u for u in users if u.get('username') == username), None)

    if password or not existing:
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400

    if existing:
        if password:
            existing['password_hash'] = generate_password_hash(password)
        if role:
            existing['role'] = role
    else:
        users.append({'username': username, 'password_hash': generate_password_hash(password),
                      'role': role or ROLE_ADMIN})

    if _config.auth.method == 'local' and not _local_admins_left(users):
        return jsonify({'error': 'Can\'t make the last local admin read-only while "Local '
                                  'accounts" is the login method — no one could change '
                                  'settings any more. Add another admin first.'}), 400

    with open(_config_path, 'w') as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    try:
        _config = load_config(_config_path)
        _tester = PathTester(_config)
    except Exception as e:
        return jsonify({'error': f'Saved but reload failed: {e}'}), 500

    return jsonify({'ok': True, 'users': _local_users_json()})


@app.route('/api/config/local-users/<username>', methods=['DELETE'])
@login_required
def api_local_users_delete(username):
    import yaml
    global _config, _tester

    with open(_config_path, 'r') as f:
        raw = yaml.safe_load(f)

    auth  = raw.setdefault('auth', {})
    users = auth.get('local_users', [])
    remaining = [u for u in users if u.get('username') != username]
    if len(remaining) == len(users):
        return jsonify({'error': f'No such user: {username}'}), 404

    if not remaining and _config.auth.method == 'local':
        return jsonify({'error': 'Cannot remove the last local user while "Local accounts" is '
                                  'the active login method — switch method or add another '
                                  'user first.'}), 400
    if _config.auth.method == 'local' and not _local_admins_left(remaining):
        return jsonify({'error': 'Cannot remove the last local admin while "Local accounts" is '
                                  'the active login method — add another admin first.'}), 400

    auth['local_users'] = remaining

    with open(_config_path, 'w') as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    try:
        _config = load_config(_config_path)
        _tester = PathTester(_config)
    except Exception as e:
        return jsonify({'error': f'Saved but reload failed: {e}'}), 500

    return jsonify({'ok': True, 'users': _local_users_json()})


@app.route('/api/config/iperf-service-status')
@login_required
def api_iperf_service_status():
    """iPerf3 service state of every installed agent in the saved config.

    {agent_id: "ok" | "missing" | "down" | "unreachable"} — "missing" means the
    agent was onboarded before the service existed and needs re-onboarding.
    """
    from concurrent.futures import ThreadPoolExecutor
    from core import iperf_service
    from core.ssh_manager import SSHManager

    cfg  = _config
    port = cfg.test_params.throughput.iperf3_port

    def check(agent):
        p   = cfg.get_ssh_params(agent)
        mgr = SSHManager(host=p["host"], username=p["username"], password=p["password"],
                         key_file=p["key_file"], port=p["port"], timeout=8, retries=1)
        try:
            mgr.connect()
        except Exception:
            return agent.id, "unreachable"
        try:
            if not iperf_service.has_service(mgr):
                return agent.id, "missing"
            return agent.id, "ok" if iperf_service.is_listening(mgr, port) else "down"
        except Exception:
            return agent.id, "unreachable"
        finally:
            try:
                mgr.disconnect()
            except Exception:
                pass

    installed = [a for a in cfg.agents if a.type == "agent_installed"]
    if not installed:
        return jsonify({})
    with ThreadPoolExecutor(max_workers=min(8, len(installed))) as pool:
        return jsonify(dict(pool.map(check, installed)))


@app.route('/api/config/resolve', methods=['POST'])
@login_required
def api_resolve_host():
    """Validate a host and show what it resolves to from the controller."""
    body   = request.get_json(silent=True) or {}
    host   = str(body.get('host') or '').strip()
    family = body.get('address_family') or 'ipv4'
    err = validate_host(host)
    if err:
        return jsonify({'ok': False, 'error': f'Host {err}'})
    if is_ip(host):
        return jsonify({'ok': True, 'host': host, 'is_ip': True, 'addresses': [host]})
    try:
        addrs = resolve_all(host, family)
    except HostResolutionError as e:
        return jsonify({'ok': False, 'error': str(e)})
    return jsonify({'ok': True, 'host': host, 'is_ip': False, 'addresses': addrs})


def _tcp_connect_ms(addr: str, port: int, timeout: float = 3.0):
    """Time a TCP connect from the controller: (ms, None) or (None, reason)."""
    import socket
    start = time.monotonic()
    try:
        with socket.create_connection((addr, port), timeout=timeout):
            return round((time.monotonic() - start) * 1000, 1), None
    except socket.timeout:
        return None, "timed out"
    except ConnectionRefusedError:
        # The host answered (RST) — reachable, nothing listening on the port
        return None, "connection refused"
    except OSError as e:
        return None, e.strerror or str(e)


def _ping_ms(addr: str):
    """Avg RTT of 3 pings from the controller (ping has cap_net_raw), or None."""
    try:
        out = subprocess.run(["ping", "-c", "3", "-i", "0.2", "-W", "1", addr],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", out)
    return float(m.group(1)) if m else None


def _reachability_check(body: dict, mgmt_host: str) -> dict:
    """The ⚡ Test button for agents the controller doesn't SSH to: check the
    test host answers what its tests will use — ping for a Switch/Router,
    ping and/or the web port for a Web Server, the iPerf3 port for Agent Not
    Installed. Checked from the controller, which may not have the same
    path as the source agents."""
    agent_type = body.get('type')
    host = str(body.get('host_test') or '').strip() or mgmt_host
    err = validate_host(host)
    if err:
        return {'ok': False, 'error': f'Test host {err}'}
    try:
        addr = resolve_all(host, body.get('address_family') or 'ipv4')[0]
    except HostResolutionError as e:
        return {'ok': False, 'error': str(e)}
    shown = host if host == addr else f"{host} ({addr})"

    parts, ok = [], False
    if agent_type in ('switch_router', 'web_server'):
        rtt = _ping_ms(addr)
        parts.append(f"ping {rtt}ms" if rtt is not None else "no ping reply")
        ok = rtt is not None
    if agent_type == 'web_server':
        port = int(body.get('tcp_port') or 443)
        ms, why = _tcp_connect_ms(addr, port)
        if ms is not None:
            parts.append(f"TCP {port} open ({ms}ms)")
            ok = True
        else:
            if why == "connection refused":
                why = "refused — host is up but nothing listens there; check the TCP port"
            parts.append(f"TCP {port} {why}")
            ok = ok or why.startswith("refused")
    if agent_type == 'agent_not_installed':
        port = int(body.get('iperf3_port') or _config.test_params.throughput.iperf3_port)
        ms, why = _tcp_connect_ms(addr, port)
        parts.append(f"iPerf3 port {port} open ({ms}ms)" if ms is not None
                     else f"iPerf3 port {port} {why}")
        ok = ms is not None
    summary = f"{shown}: " + " | ".join(parts) + " — checked from the controller"
    return {'ok': ok, 'reachability': True, 'summary': summary,
            **({} if ok else {'error': summary})}


@app.route('/api/config/test-agent', methods=['POST'])
@login_required
def api_test_agent():
    from core.ssh_manager import SSHManager, SSHConnectionError
    body     = request.get_json(silent=True) or {}
    host     = body.get('host')
    username = body.get('username', _config.ssh_defaults.username)
    key_file = body.get('key_file', _config.ssh_defaults.key_file)
    port     = body.get('port', _config.ssh_defaults.port)

    if not host:
        return jsonify({'ok': False, 'error': 'No host provided'}), 400
    host = str(host).strip()
    err = validate_host(host)
    if err:
        return jsonify({'ok': False, 'error': f'Host {err}'})

    if body.get('type') in NO_SSH_TYPES:
        return jsonify(_reachability_check(body, host))

    mgr = SSHManager(host=host, username=username, key_file=key_file,
                     port=port, timeout=8, retries=1)
    try:
        mgr.connect()
        out   = mgr.run('echo ok && hostname && iperf3 --version 2>&1 | head -1', timeout=10)
        from core import iperf_service
        iperf_port = _config.test_params.throughput.iperf3_port
        if not iperf_service.has_service(mgr):
            state, service = 'missing', 'iPerf3 service not installed — re-onboard to install it'
        elif iperf_service.is_listening(mgr, iperf_port):
            state, service = 'ok', f'iPerf3 service listening on {iperf_port}'
        else:
            state, service = 'down', f'iPerf3 service installed but not listening on {iperf_port}'
        mgr.disconnect()
        lines    = [l.strip() for l in out.strip().splitlines() if l.strip()]
        hostname = lines[1] if len(lines) > 1 else '?'
        iperf3   = lines[2] if len(lines) > 2 else 'not found'
        return jsonify({'ok': True, 'hostname': hostname, 'iperf3': iperf3,
                        'iperf3_service': service, 'iperf3_service_state': state})
    except SSHConnectionError as e:
        return jsonify({'ok': False, 'error': str(e)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ── Logging API ────────────────────────────────────────────

def _debug_status() -> dict:
    state = debug_mode.read_state(_config.log_dir)
    files = []
    for src in debug_mode.SOURCES:
        try:
            st = os.stat(debug_mode.log_path(_config.log_dir, src))
            files.append({"source": src, "size": st.st_size, "modified": st.st_mtime})
        except OSError:
            files.append({"source": src, "size": 0, "modified": None})
    return {
        "active":        bool(state),
        "until":         state["until"] if state else None,
        "duration_min":  state["duration_min"] if state else None,
        # Countdown is driven from remaining_sec, not "until", so a browser
        # clock that differs from the server's doesn't skew it.
        "remaining_sec": max(0, int(state["until"] - time.time())) if state else 0,
        "options":       list(debug_mode.DURATION_OPTIONS_MIN),
        "files":         files,
    }


def _tail_lines(path: str, max_lines: int, max_bytes: int = 512 * 1024) -> List[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]   # first line is cut off mid-way
    return lines[-max_lines:]


@app.route("/api/logging/debug", methods=["GET"])
@login_required
def api_debug_get():
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    return jsonify(_debug_status())


@app.route("/api/logging/debug", methods=["POST"])
@login_required
def api_debug_enable():
    """Turn debug mode on for N minutes from now (or restart the timer)."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    body = request.get_json(silent=True) or {}
    try:
        minutes = int(body.get("minutes"))
        debug_mode.enable(_config.log_dir, minutes)
    except (TypeError, ValueError):
        return jsonify({"error": "minutes must be one of "
                                 + ", ".join(map(str, debug_mode.DURATION_OPTIONS_MIN))}), 400
    except OSError as e:
        return jsonify({"error": f"Could not write debug state: {e}"}), 500
    debug_mode.sync_now()   # takes effect here immediately; the scheduler polls every few seconds
    # After the sync, so the first enable's audit line lands in the debug log too.
    logging.getLogger("debug_mode").info("Debug mode requested for %d min by '%s'",
                                         minutes, session.get("username", "?"))
    return jsonify(_debug_status())


@app.route("/api/logging/debug/stop", methods=["POST"])
@login_required
def api_debug_stop():
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    try:
        debug_mode.disable(_config.log_dir)
    except OSError as e:
        return jsonify({"error": f"Could not clear debug state: {e}"}), 500
    # Before the sync, while the debug log is still attached.
    logging.getLogger("debug_mode").info("Debug mode turned off by '%s'",
                                         session.get("username", "?"))
    debug_mode.sync_now()
    return jsonify(_debug_status())


def _log_source_arg() -> Optional[str]:
    src = request.args.get("source", "scheduler")
    return src if src in debug_mode.SOURCES else None   # allowlist — never a path


@app.route("/api/logging/tail")
@login_required
def api_debug_tail():
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    src = _log_source_arg()
    if not src:
        return jsonify({"error": "Unknown log source"}), 400
    try:
        max_lines = min(5000, max(1, int(request.args.get("lines", 500))))
    except ValueError:
        max_lines = 500
    path = debug_mode.log_path(_config.log_dir, src)
    try:
        lines = _tail_lines(path, max_lines)
    except FileNotFoundError:
        lines = []
    return jsonify({"source": src, "lines": lines})


@app.route("/api/logging/download")
@login_required
def api_debug_download():
    """The whole debug log for a source — rotated backups oldest-first, then the live file."""
    if not _config:
        return jsonify({"error": "Config not loaded"}), 503
    src = _log_source_arg()
    if not src:
        return jsonify({"error": "Unknown log source"}), 400
    base  = debug_mode.log_path(_config.log_dir, src)
    parts = [p for p in (f"{base}.2", f"{base}.1", base) if os.path.isfile(p)]
    if not parts:
        return jsonify({"error": "No debug log yet"}), 404

    def generate():
        for part in parts:
            try:
                with open(part, "rb") as f:
                    while chunk := f.read(64 * 1024):
                        yield chunk
            except FileNotFoundError:
                continue   # rotated away mid-download

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return Response(generate(), mimetype="text/plain",
                    headers={"Content-Disposition":
                             f'attachment; filename="linkstate-debug-{src}-{stamp}.log"'})


# ── Results utility API ────────────────────────────────────

@app.route("/api/results/export.csv")
@login_required
def api_export_csv():
    """Export results as a CSV file download."""
    import csv, io
    minutes = int(request.args.get("minutes", 1440))
    path_id = request.args.get("path_id")
    records = _load_records(minutes, path_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "timestamp", "path", "status",
        "tx_mbps", "rx_mbps", "retransmits",
        "bidir_tx_mbps", "bidir_rx_mbps", "bidir_retransmits",
        "rtt_avg_ms", "rtt_max_ms", "loss_pct",
        "jitter_ms", "jitter_loss_pct",
        "idle_rtt_ms", "loaded_rtt_ms", "bufferbloat_delta_ms",
        "mtu_bytes", "fragmentation", "duration_sec", "error",
    ])
    for r in records:
        # A record may hold multiple throughput entries (one per direction
        # run: upload/download/bidir) — collapse upload/download to one
        # tx/rx/retransmits triple per row (untested = blank, not 0). Bidir
        # is kept in its own columns rather than folded in — both directions
        # were measured while contending with each other, so its numbers
        # aren't comparable to a dedicated upload/download run.
        t_entries    = [t for t in _throughput_entries(r) if t.get("direction") != "bidir"]
        bidir_entries = [t for t in _throughput_entries(r) if t.get("direction") == "bidir"]
        tx   = max((t["tx_mbps"] for t in t_entries if t.get("tx_mbps") is not None), default="")
        rx   = max((t["rx_mbps"] for t in t_entries if t.get("rx_mbps") is not None), default="")
        retr = max((t["retransmits"] for t in t_entries), default="")
        bidir_tx   = max((t["tx_mbps"] for t in bidir_entries if t.get("tx_mbps") is not None), default="")
        bidir_rx   = max((t["rx_mbps"] for t in bidir_entries if t.get("rx_mbps") is not None), default="")
        bidir_retr = max((t["retransmits"] for t in bidir_entries), default="")
        l  = r.get("latency")            or {}
        j  = r.get("jitter")             or {}
        lu = r.get("latency_under_load") or {}
        m  = r.get("mtu")                or {}
        writer.writerow([
            r.get("timestamp_utc", "")[:19],
            r.get("path_label", ""),
            ("FAIL" if not r.get("success")
             else "PARTIAL" if r.get("error")   # some tests failed — see error column
             else "OK"),
            tx, rx, retr,
            bidir_tx, bidir_rx, bidir_retr,
            l.get("rtt_avg_ms", ""),    l.get("rtt_max_ms", ""),
            l.get("packet_loss_pct", ""),
            j.get("jitter_ms", ""),     j.get("packet_loss_pct", ""),
            lu.get("idle_rtt_avg_ms", ""), lu.get("loaded_rtt_avg_ms", ""),
            lu.get("delta_ms", ""),
            m.get("effective_mtu_bytes", ""), m.get("fragmentation_detected", ""),
            r.get("duration_total_sec", ""), r.get("error", ""),
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=linkstate_results.csv"},
    )


@app.route("/api/results/clear", methods=["POST"])
@login_required
def api_results_clear():
    """Delete result files for the requested day range."""
    body = request.get_json(silent=True) or {}
    days = int(body.get("days", 1))
    deleted = []
    for i in range(days):
        date  = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        fpath = os.path.join(_config.results_dir, f"results_{date}.jsonl")
        if os.path.exists(fpath):
            os.remove(fpath)
            deleted.append(date)
    return jsonify({"ok": True, "deleted": deleted})


# ── Auth stubs ─────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if not _config or not _config.auth.method:
        session["authenticated"] = True
        session["username"] = "admin"
        session["role"] = ROLE_ADMIN
        return redirect(request.args.get("next", "/"))

    error = ""
    brand = _config.branding
    must_ack = bool(brand.login_banner and brand.login_banner_ack)
    if request.method == "POST":
        ip       = request.remote_addr
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        allowed, wait = _check_rate_limit(ip)
        if must_ack and request.form.get("ack") != "1":
            error = "You must accept the notice to log in."
        elif not allowed:
            error = f"Too many failed attempts. Try again in {wait}s."
        elif not username or not password:
            error = "Username and password are required."
        else:
            _record_attempt(ip)
            try:
                auth_source = _config.auth.method
                if _config.auth.method == "local":
                    from core.local_auth import authenticate_local
                    authed = authenticate_local(username, password, _config.auth)
                else:
                    from core.radius_auth import authenticate_radius, RadiusAuthError
                    try:
                        authed = authenticate_radius(username, password, _config.auth)
                    except RadiusAuthError as radius_err:
                        # RADIUS didn't respond (down/unreachable/misconfigured) rather
                        # than actively rejecting the credentials — fall back to a
                        # local account if one is configured, instead of locking
                        # everyone out whenever the RADIUS server is unavailable.
                        if not _config.auth.local_users:
                            raise
                        from core.local_auth import authenticate_local, LocalAuthError
                        auth_log = logging.getLogger("auth")
                        auth_log.warning(
                            "RADIUS unreachable (%s) — trying local fallback for user '%s'",
                            radius_err, username,
                        )
                        auth_source = "local"
                        try:
                            authed = authenticate_local(username, password, _config.auth)
                        except LocalAuthError:
                            authed = None
                        if authed:
                            auth_log.warning(
                                "User '%s' authenticated via local fallback account "
                                "(RADIUS unreachable)", username,
                            )

                if authed:
                    # authed is the account's role
                    session.clear()
                    session.permanent = True
                    session["authenticated"] = True
                    session["username"] = username
                    session["role"] = authed
                    session["auth_source"] = auth_source
                    session["login_at"] = time.time()
                    with _login_lock:
                        _login_attempts[ip].clear()
                    return redirect(request.form.get("next") or "/")
                else:
                    error = "Invalid username or password."
            except Exception as e:
                error = f"Authentication error: {e}"

    esc = _html.escape
    next_url = request.values.get("next", "/")
    banner = ack = powered = ""
    if brand.login_banner:
        banner = f'<div class="banner" role="note">{esc(brand.login_banner)}</div>'
    if must_ack:
        ack = ('<label class="ack"><input type="checkbox" name="ack" value="1" required>'
               '<span>I have read and accept the notice above</span></label>')
    if brand.org_name or branding.asset_url("logo"):
        powered = f'<div class="powered">Powered by {branding.PRODUCT}</div>'
    # Filled before _apply_branding, so a {{...}} typed into a branding field
    # is never mistaken for a placeholder.
    page = open(os.path.join(STATIC_DIR, "login.html"), encoding="utf-8").read()
    for key, val in (("next", esc(next_url)),
                     ("csrf_token", secrets.token_hex(16)),
                     ("error", esc(error)),
                     ("sub", esc(brand.org_name or "Sign in to continue")),
                     ("login_class", " has-banner" if banner else ""),
                     ("banner", banner), ("ack", ack), ("powered", powered)):
        page = page.replace("{{" + key + "}}", val)
    return _html_response(_apply_branding(page, "Log In"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Branding ───────────────────────────────────────────────
# Pages carry two markers the server fills in: <!--LS:HEAD--> (shared theme
# stylesheet, accent override, favicon, window.LS_BRAND) and <!--LS:BRAND-->
# (header logo + controller name). Their <title> is replaced too.

# Names config.example.yaml has shipped with — not worth showing next to the
# LinkState wordmark, which already says it.
_DEFAULT_CONTROLLER_NAMES = {"linkstate controller", "nettest controller"}


def _controller_name() -> str:
    name = (_config.name if _config else "") or ""
    return "" if name.strip().lower() in _DEFAULT_CONTROLLER_NAMES else name.strip()


def _brand_head(theme: bool = True) -> str:
    brand = _config.branding if _config else Branding()
    parts = []
    if theme:
        try:
            ver = int(os.path.getmtime(os.path.join(STATIC_DIR, "static", "theme.css")))
        except OSError:
            ver = 0
        parts.append(f'<link rel="stylesheet" href="/static/theme.css?v={ver}">')
        if brand.accent_color:
            # Button/badge fills are the accent blended into the page
            # background, so any accent gets matching ones.
            parts.append(
                "<style>:root{"
                f"--accent:{brand.accent_color};"
                "--accent-bg:color-mix(in srgb,var(--accent) 16%,#0b0d11);"
                "--accent-bdr:color-mix(in srgb,var(--accent) 35%,#0b0d11)"
                "}</style>")
    favicon = branding.asset_url("favicon")
    if favicon:
        parts.append(f'<link rel="icon" href="{favicon}">')
    info = {
        "product":         branding.PRODUCT,
        "controller_name": _controller_name(),
        "org_name":        brand.org_name,
        "report_footer":   brand.report_footer,
        "logo_url":        branding.asset_url("logo"),
    }
    # "<" escaped so no value can close the <script> element.
    parts.append("<script>window.LS_BRAND=" +
                 json.dumps(info).replace("<", "\\u003c") + ";</script>")
    # Account type: read-only sessions hide controls marked .admin-only. The
    # server enforces it regardless (see _enforce_role) — this is just UI.
    if session.get("authenticated") or not (_config and _config.auth.method):
        user = {"username": session.get("username", ""), "role": _current_role() or ROLE_READONLY}
        parts.append("<style>.ls-readonly .admin-only{display:none!important}</style>")
        parts.append("<script>window.LS_USER=" +
                     json.dumps(user).replace("<", "\\u003c") + ";"
                     "if(LS_USER.role!=='admin')document.documentElement.classList.add('ls-readonly');"
                     "</script>")
    return "\n".join(parts)


def _brand_html() -> str:
    esc = _html.escape
    brand = _config.branding if _config else Branding()
    logo = branding.asset_url("logo")
    if logo:
        alt = brand.org_name or branding.PRODUCT
        mark = f'<img class="brand-img" src="{logo}" alt="{esc(alt)}">'
    else:
        mark = '<span class="logo">Link<span>State</span></span>'
    name = _controller_name()
    return (f'<span class="brand">{mark}'
            f'<span class="brand-name" title="{esc(name)}">{esc(name)}</span></span>')


def _apply_branding(page: str, title: str, theme: bool = True) -> str:
    name = _controller_name() or branding.PRODUCT
    page = page.replace("<!--LS:HEAD-->", _brand_head(theme), 1)
    page = page.replace("<!--LS:BRAND-->", _brand_html(), 1)
    return re.sub(r"<title>.*?</title>",
                  lambda _m: f"<title>{_html.escape(f'{title} — {name}')}</title>",
                  page, count=1, flags=re.S)


def _html_response(page: str) -> Response:
    # Branding changes take effect on the next load, so don't let browsers
    # reuse a cached copy without asking.
    return Response(page, mimetype="text/html", headers={"Cache-Control": "no-cache"})


def _render_page(filename: str, title: str) -> Response:
    with open(os.path.join(STATIC_DIR, filename), encoding="utf-8") as f:
        return _html_response(_apply_branding(f.read(), title))


@app.route("/branding/<kind>")
def serve_brand_asset(kind):
    # Not behind login: the login page shows the logo and favicon.
    found = branding.find_asset(kind)
    if not found:
        abort(404)
    path, ext, _mtime = found
    # URLs carry ?v=<mtime>, so a long cache lifetime is safe.
    resp = send_file(path, mimetype=branding.ASSET_MIMETYPES[ext], max_age=30 * 86400)
    # An SVG opened directly is a document that could run script — this
    # stops it. (Inside <img>, browsers never run SVG script anyway.)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/api/branding/<kind>", methods=["POST"])
@login_required
def api_branding_upload(kind):
    if kind not in branding.ASSET_KINDS:
        return jsonify({"error": f"Unknown asset '{kind}'"}), 404
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file uploaded"})
    data = upload.read(branding.ASSET_KINDS[kind]["max_bytes"] + 1)
    err = branding.save_asset(kind, data)
    if err:
        return jsonify({"error": err})
    return jsonify({"ok": True, "url": branding.asset_url(kind)})


@app.route("/api/branding/<kind>", methods=["DELETE"])
@login_required
def api_branding_delete(kind):
    if kind not in branding.ASSET_KINDS:
        return jsonify({"error": f"Unknown asset '{kind}'"}), 404
    branding.delete_asset(kind)
    return jsonify({"ok": True})


# ── Serve HTML ─────────────────────────────────────────────

@app.route("/")
@login_required
def serve_dashboard():
    return _render_page("index.html", "Network Monitor")


@app.route("/config")
@login_required
def serve_config_page():
    return _render_page("config.html", "Config Editor")


@app.route("/compare")
@login_required
def serve_compare_page():
    return _render_page("compare.html", "Compare Runs")


@app.route("/speedtest")
@login_required
def serve_speedtest_page():
    return _render_page("speedtest.html", "Speed Test")


# ── Entry point (flask dev server only — use gunicorn in prod) ─

def main():
    parser = argparse.ArgumentParser(description="LinkState Web Dashboard")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--port",   type=int, default=8080)
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"LinkState dashboard → http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

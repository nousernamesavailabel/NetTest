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
import json
import logging
import os
import queue
import subprocess
import sys
import collections
import secrets
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from flask import Flask, jsonify, send_from_directory, request, Response, session, redirect, url_for

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config_loader import load_config
from core.results import ResultStore
from core.path_tester import PathTester

app = Flask(__name__, static_folder="web/static")

STATIC_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_config_path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config/config.yaml")
_packages_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packages")
os.makedirs(_packages_dir, exist_ok=True)

# ── Module-level init (required for gunicorn) ──────────────
try:
    _config = load_config(_config_path)
    _store  = ResultStore(_config.results_dir)
    _tester = PathTester(_config)
    app.secret_key = _config.auth.session_secret or secrets.token_hex(32)
except Exception as _init_err:
    import traceback
    print(f"FATAL: Failed to initialize — {_init_err}")
    traceback.print_exc()
    _config = None
    _store  = None
    _tester = None
    app.secret_key = secrets.token_hex(32)

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


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _config or not _config.auth.radius_server:
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Job tracking ───────────────────────────────────────────
_jobs: dict      = {}
_jobs_lock           = threading.Lock()
_job_logs: dict      = {}
_job_logs_lock       = threading.Lock()
_job_log_history: dict = {}   # job_id -> list of log line strings (last 500)
_MAX_HISTORY_JOBS    = 20     # evict oldest jobs when over this limit
_import_cache: dict  = {}     # stores parsed import data server-side (avoids session size limit)
_import_cache_lock   = threading.Lock()


# ── Log handler that feeds the SSE queue ──────────────────
class JobLogHandler(logging.Handler):
    """Attaches to the root logger and copies records into a job queue."""
    def __init__(self, job_id: str):
        super().__init__()
        self.job_id = job_id
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        ))

    def emit(self, record):
        line = self.format(record)
        with _job_logs_lock:
            q = _job_logs.get(self.job_id)
            hist = _job_log_history.setdefault(self.job_id, [])
            if len(hist) < 500:
                hist.append(line)
        if q:
            try:
                q.put_nowait(line)
            except Exception:
                pass


def _run_job(job_id: str, path_id: str, test_filter: List[str] = None):
    """Execute a test path in a background thread and stream logs via SSE."""
    # Set up log queue and attach handler to root logger
    log_q   = queue.Queue(maxsize=2000)
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        _job_logs[job_id] = log_q

    # Silence noisy third-party loggers
    logging.getLogger("netmiko").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    with _jobs_lock:
        _jobs[job_id]["status"] = "running"

    try:
        path = next((p for p in _config.paths if p.id == path_id), None)
        if not path:
            raise ValueError(f"Path '{path_id}' not found in config")

        if test_filter:
            filtered = [t for t in path.tests if t in test_filter]
            path = replace(path, tests=filtered if filtered else path.tests)

        result = _tester.run_path(path)
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
        log_q.put(None)   # sentinel — tells SSE generator to close


# ── Helpers ────────────────────────────────────────────────

def _load_records(days: int = 1, path_id: Optional[str] = None) -> List[dict]:
    records = []
    for i in range(days):
        date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        day_records = _store.load_file(date)
        records.extend(day_records)
    if path_id:
        records = [r for r in records if r.get("path_id") == path_id]
    return sorted(records, key=lambda r: r.get("timestamp_utc", ""))


def _summarise(records: List[dict]) -> dict:
    if not records:
        return {
            "total_runs": 0, "successful": 0, "failed": 0,
            "avg_latency_ms": None, "avg_throughput_mbps": None,
            "avg_jitter_ms": None, "avg_loss_pct": None,
        }

    successful  = [r for r in records if r.get("success")]
    lat_vals    = [r["latency"]["rtt_avg_ms"]     for r in records if r.get("latency")]
    tput_vals   = [r["throughput"]["tx_mbps"]      for r in records if r.get("throughput")]
    rx_vals     = [r["throughput"]["rx_mbps"]      for r in records if r.get("throughput") and r["throughput"].get("rx_mbps")]
    jitter_vals = [r["jitter"]["jitter_ms"]        for r in records if r.get("jitter")]
    loss_vals   = [r["latency"]["packet_loss_pct"] for r in records if r.get("latency")]

    def avg(lst): return round(sum(lst) / len(lst), 2) if lst else None

    return {
        "total_runs":          len(records),
        "successful":          len(successful),
        "failed":              len(records) - len(successful),
        "avg_latency_ms":      avg(lat_vals),
        "avg_throughput_mbps": avg(tput_vals),
        "avg_throughput_rx_mbps": avg(rx_vals),
        "avg_jitter_ms":       avg(jitter_vals),
        "avg_loss_pct":        avg(loss_vals),
        "last_run":            records[-1]["timestamp_utc"] if records else None,
    }


# ── Read API ───────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    days    = int(request.args.get("days", 1))
    path_id = request.args.get("path_id")
    records = _load_records(days, path_id)
    return jsonify(_summarise(records))


@app.route("/api/paths")
def api_paths():
    paths = [
        {
            "id":          p.id,
            "label":       p.label,
            "source":      p.source,
            "hops":        p.hops,
            "destination": p.destination,
            "tests":       p.tests,
        }
        for p in _config.paths
    ]
    return jsonify(paths)


@app.route("/api/results")
def api_results():
    days    = int(request.args.get("days", 1))
    path_id = request.args.get("path_id")
    limit   = int(request.args.get("limit", 200))
    records = _load_records(days, path_id)
    return jsonify(list(reversed(records[-limit:])))


@app.route("/api/results/latest")
def api_results_latest():
    records = _load_records(days=1)
    latest  = {}
    for r in records:
        latest[r["path_id"]] = r
    return jsonify(list(latest.values()))


@app.route("/api/timeseries/<metric>")
def api_timeseries(metric: str):
    days    = int(request.args.get("days", 1))
    path_id = request.args.get("path_id")
    records = _load_records(days, path_id)

    series_by_path = {}
    for r in records:
        pid = r["path_id"]
        ts  = r["timestamp_utc"]
        val = None

        if metric == "latency"      and r.get("latency"):
            val = r["latency"]["rtt_avg_ms"]
        elif metric == "throughput"  and r.get("throughput"):
            val = r["throughput"]["tx_mbps"]
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
    records = _load_records(days=7, path_id=path_id)
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
    records = _load_records(days=7)
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

    if not agent_ip:   return jsonify({"error": "agent_ip is required"}), 400
    if not admin_user: return jsonify({"error": "admin_user is required"}), 400
    if not admin_pass: return jsonify({"error": "admin_pass is required"}), 400

    job_id  = "onboard-" + str(uuid.uuid4())[:8]
    log_q   = queue.Queue(maxsize=500)
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        _job_logs[job_id] = log_q
        if len(_job_log_history) >= _MAX_HISTORY_JOBS:
            oldest = sorted(_job_log_history.keys())[0]
            del _job_log_history[oldest]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":   job_id,
            "path_id":  "onboard",
            "label":    f"Onboarding {agent_label}",
            "status":   "queued",
            "started":  datetime.now(timezone.utc).isoformat(),
            "finished": None, "success": None, "error": None,
        }

    def run_onboard():
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        logging.getLogger("netmiko").setLevel(logging.WARNING)
        logging.getLogger("paramiko").setLevel(logging.WARNING)
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
            log_q.put(None)

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
        headers={"Content-Disposition": "attachment; filename=nettest_key.pub"}
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
        headers={"Content-Disposition": f"attachment; filename=nettest_key"}
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
        agents = [a for a in _config.agents if a.type != "svi_adjacent"]
    else:
        agent = _config.get_agent(agent_id)
        if not agent: return jsonify({"error": f"Agent '{agent_id}' not found"}), 404
        agents = [agent]

    # Read public key
    key_file = os.path.expanduser(_config.ssh_defaults.key_file)
    pub_file = key_file + ".pub"
    if not os.path.exists(pub_file):
        return jsonify({"error": "Public key not found — import a key first"}), 400
    pub_key = open(pub_file).read().strip()

    nettest_user = _config.ssh_defaults.username

    job_id  = "push-key-" + str(uuid.uuid4())[:8]
    log_q   = queue.Queue(maxsize=500)
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        _job_logs[job_id] = log_q
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
        logging.getLogger("netmiko").setLevel(logging.WARNING)
        logging.getLogger("paramiko").setLevel(logging.WARNING)

        with _jobs_lock:
            _jobs[job_id]["status"] = "running"

        target_desc = "all endpoint agents" if len(agents) > 1 else agents[0].label
        _log.info(f"── Push Public Key ──")
        _log.info(f"  Target  : {target_desc}")
        _log.info(f"  User    : {admin_user}:{admin_port}")
        _log.info(f"  NetTest : {nettest_user}")
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
                auth_keys = f"/home/{nettest_user}/.ssh/authorized_keys"
                _log.info(f"  Creating .ssh directory for {nettest_user}...")
                conn.send_command_timing(
                    f"echo '{admin_pass}' | sudo -S bash -c "
                    f"'mkdir -p /home/{nettest_user}/.ssh && "
                    f"chmod 700 /home/{nettest_user}/.ssh && "
                    f"chown {nettest_user}:{nettest_user} /home/{nettest_user}/.ssh'",
                    read_timeout=15, last_read=2.0
                )

                # Write public key
                _log.info(f"  Writing public key to {auth_keys}...")
                deploy_cmd = (
                    f"echo '{admin_pass}' | sudo -S bash -c "
                    f"'echo {repr(pub_key)} > {auth_keys} && "
                    f"chmod 600 {auth_keys} && "
                    f"chown {nettest_user}:{nettest_user} {auth_keys}'"
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
        log_q.put(None)

    threading.Thread(target=run_push, daemon=True, name="push-key").start()
    return jsonify({"job_id": job_id, "status": "queued"})


# ── Export / Import API ───────────────────────────────────

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
            "radius_server":              a.radius_server,
            "radius_port":                a.radius_port,
            "radius_secret":              a.radius_secret,
            "radius_timeout":             a.radius_timeout,
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
    prefix = f"nettest-export-{ts}"

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
                (key_file,          f"{prefix}/nettest_key"),
                (key_file + ".pub", f"{prefix}/nettest_key.pub"),
            ]:
                if os.path.exists(fpath):
                    tar.add(fpath, arcname=arcname)

        # Readme
        sensitive = []
        if include.get("ssh_credentials"): sensitive.append("SSH credentials")
        if include.get("ssh_keys"):        sensitive.append("SSH private key")
        if include.get("auth"):            sensitive.append("RADIUS/auth config")
        readme = f"""NetTest Configuration Export
Generated: {datetime.now(timezone.utc).isoformat()}

Contents:
- config.yaml  (agents, paths{", controller" if include.get("controller") else ""}{", schedule" if include.get("schedule") else ""}{", test params" if include.get("test_params") else ""}{", SSH credentials" if include.get("ssh_credentials") else ""}{", auth" if include.get("auth") else ""})
{"- nettest_key / nettest_key.pub" if include.get("ssh_keys") else ""}

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
        headers={"Content-Disposition": f"attachment; filename=nettest-export-{ts}.tar.gz"}
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
                m.name.endswith("nettest_key") and not m.name.endswith(".pub")
                for m in tar.getmembers()
            )
            has_pubkey = any(m.name.endswith("nettest_key.pub") for m in tar.getmembers())

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

    # Restart scheduler (best-effort — don't fail import if this fails)
    try:
        import subprocess
        subprocess.run(["sudo", "systemctl", "restart", "nettest"],
                       capture_output=True, timeout=10)
    except Exception:
        pass

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
                if member.name.endswith("nettest_key.pub"):
                    content = tar.extractfile(member).read()
                    with open(key_file + ".pub", "wb") as fh: fh.write(content)
                    os.chmod(key_file + ".pub", 0o644)
                elif member.name.endswith("nettest_key"):
                    content = tar.extractfile(member).read()
                    with open(key_file, "wb") as fh: fh.write(content)
                    os.chmod(key_file, 0o600)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ── HTTPS / nginx Management API ─────────────────────────

SSL_DIR  = "/opt/nettest/ssl"
SSL_CERT = "/opt/nettest/ssl/nettest.crt"
SSL_KEY  = "/opt/nettest/ssl/nettest.key"
NGINX_CONF = "/etc/nginx/sites-available/nettest"


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
    cn         = body.get("cn", "nettest").strip()
    org        = body.get("org", "NetTest").strip()
    days       = int(body.get("days", 3650))
    san_ips    = body.get("san_ips", [])   # list of IP strings

    os.makedirs(SSL_DIR, exist_ok=True)

    san_str = ",".join(f"IP:{ip}" for ip in san_ips if ip)
    if not san_str:
        san_str = f"IP:{cn}" if cn.replace(".", "").isdigit() else f"DNS:{cn}"

    cmd = [
        "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:4096",
        "-keyout", SSL_KEY, "-out", SSL_CERT,
        "-days", str(days),
        "-subj", f"/CN={cn}/O={org}",
        "-addext", f"subjectAltName={san_str}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return jsonify({"error": result.stderr}), 500

    os.chmod(SSL_KEY, 0o600)
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
    """Write the nginx reverse proxy config if not already present."""
    conf = """server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /opt/nettest/ssl/nettest.crt;
    ssl_certificate_key /opt/nettest/ssl/nettest.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

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
"""
    os.makedirs(os.path.dirname(NGINX_CONF), exist_ok=True)
    with open(NGINX_CONF, "w") as f:
        f.write(conf)
    link = "/etc/nginx/sites-enabled/nettest"
    if not os.path.exists(link):
        os.symlink(NGINX_CONF, link)
    default = "/etc/nginx/sites-enabled/default"
    if os.path.exists(default):
        os.remove(default)


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
    log_q   = queue.Queue(maxsize=500)
    handler = JobLogHandler(job_id)
    handler.setLevel(logging.INFO)

    with _job_logs_lock:
        _job_logs[job_id] = log_q
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
            nettest_usr = _config.ssh_defaults.username
            port        = agent.port or _config.ssh_defaults.port
            remote_tmp  = "/tmp/nettest_packages"

            _push_log = logging.getLogger("packages")
            _push_log.info(f"Pushing {len(deb_files)} package(s) to {agent.label} ({agent.host_mgmt_ip})...")

            # Create remote tmp dir
            subprocess.run([
                "ssh", "-i", key_file, "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", "-p", str(port),
                f"{nettest_usr}@{agent.host_mgmt_ip}",
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
                    f"{nettest_usr}@{agent.host_mgmt_ip}:{remote_tmp}/{deb}",
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
                f"{nettest_usr}@{agent.host_mgmt_ip}",
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
            log_q.put(None)

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
        }
        for a in _config.agents
    ])


@app.route("/api/hops/<path_id>")
def api_hops(path_id: str):
    records = _load_records(days=1, path_id=path_id)
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
def api_run_path(path_id: str):
    body        = request.get_json(silent=True) or {}
    test_filter = body.get("tests")

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
        target=_run_job, args=(job_id, path_id, test_filter),
        daemon=True, name=f"job-{job_id}"
    ).start()

    return jsonify({"job_id": job_id, "path_id": path_id, "status": "queued"})


@app.route("/api/run/all", methods=["POST"])
def api_run_all():
    body        = request.get_json(silent=True) or {}
    test_filter = body.get("tests")
    job_ids     = []

    for path in _config.paths:
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
            daemon=True, name=f"job-{job_id}"
        ).start()
        job_ids.append(job_id)

    return jsonify({"jobs": job_ids, "count": len(job_ids)})


@app.route("/api/jobs")
def api_jobs():
    with _jobs_lock:
        jobs = list(_jobs.values())
    return jsonify(list(reversed(jobs[-50:])))


@app.route("/api/jobs/<job_id>")
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
def api_job_stream(job_id: str):
    """SSE stream — delivers live log lines to the browser."""
    NL = "\n"

    def generate():
        # Wait up to 3s for the job thread to register its log queue
        q = None
        for _ in range(30):
            with _job_logs_lock:
                q = _job_logs.get(job_id)
            if q:
                break
            time.sleep(0.1)

        if not q:
            with _jobs_lock:
                job = _jobs.get(job_id, {})
            yield "data: [Job " + job_id + "] Status: " + job.get("status", "unknown") + NL + NL
            yield "data: [STREAM END]" + NL + NL
            return

        while True:
            try:
                line = q.get(timeout=120)   # 120s covers throughput + latency-under-load
            except Exception:
                yield "data: [STREAM TIMEOUT]" + NL + NL
                break

            if line is None:   # sentinel — job finished
                with _jobs_lock:
                    job = _jobs.get(job_id, {})
                status = job.get("status", "done")
                err    = job.get("error") or ""
                yield "data: " + NL + NL
                if status == "done" and not err:
                    yield "data: \u2713 Test completed successfully" + NL + NL
                elif err:
                    yield "data: \u2717 Finished with error: " + err + NL + NL
                yield "data: [STREAM END]" + NL + NL
                break

            safe = line.replace(NL, " | ")
            yield "data: " + safe + NL + NL

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Config API ─────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
def api_config_get():
    import yaml
    with open(_config_path, 'r') as f:
        raw = yaml.safe_load(f)
    return jsonify(raw)


@app.route('/api/config', methods=['POST'])
def api_config_save():
    import yaml
    global _config, _tester

    body = request.get_json(silent=True)
    if not body:
        return jsonify({'error': 'No JSON body'}), 400

    with open(_config_path, 'r') as f:
        raw = yaml.safe_load(f)

    if 'agents'      in body: raw['agents']      = body['agents']
    if 'paths'       in body: raw['paths']        = body['paths']
    if 'test_params' in body: raw['test_params']  = body['test_params']
    if 'ssh_defaults'in body: raw['ssh_defaults'].update(body['ssh_defaults'])
    if 'schedule'    in body: raw['schedule'].update(body['schedule'])

    with open(_config_path, 'w') as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    try:
        _config = load_config(_config_path)
        _tester = PathTester(_config)
        # Restart scheduler so it picks up path/agent changes immediately
        subprocess.run(["sudo", "systemctl", "restart", "nettest"], check=False)
        return jsonify({'ok': True, 'agents': len(_config.agents), 'paths': len(_config.paths)})
    except Exception as e:
        return jsonify({'error': f'Config saved but reload failed: {e}'}), 500


@app.route('/api/config/test-agent', methods=['POST'])
def api_test_agent():
    from core.ssh_manager import SSHManager, SSHConnectionError
    body     = request.get_json(silent=True) or {}
    host     = body.get('host')
    username = body.get('username', _config.ssh_defaults.username)
    key_file = body.get('key_file', _config.ssh_defaults.key_file)
    port     = body.get('port', _config.ssh_defaults.port)

    if not host:
        return jsonify({'ok': False, 'error': 'No host provided'}), 400

    mgr = SSHManager(host=host, username=username, key_file=key_file,
                     port=port, timeout=8, retries=1)
    try:
        mgr.connect()
        out   = mgr.run('echo ok && hostname && iperf3 --version 2>&1 | head -1', timeout=10)
        mgr.disconnect()
        lines    = [l.strip() for l in out.strip().splitlines() if l.strip()]
        hostname = lines[1] if len(lines) > 1 else '?'
        iperf3   = lines[2] if len(lines) > 2 else 'not found'
        return jsonify({'ok': True, 'hostname': hostname, 'iperf3': iperf3})
    except SSHConnectionError as e:
        return jsonify({'ok': False, 'error': str(e)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ── Results utility API ────────────────────────────────────

@app.route("/api/results/export.csv")
def api_export_csv():
    """Export results as a CSV file download."""
    import csv, io
    days    = int(request.args.get("days", 1))
    path_id = request.args.get("path_id")
    records = _load_records(days, path_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "timestamp", "path", "status",
        "tx_mbps", "rx_mbps", "retransmits",
        "rtt_avg_ms", "rtt_max_ms", "loss_pct",
        "jitter_ms", "jitter_loss_pct",
        "idle_rtt_ms", "loaded_rtt_ms", "bufferbloat_delta_ms",
        "mtu_bytes", "fragmentation", "duration_sec", "error",
    ])
    for r in records:
        t  = r.get("throughput")         or {}
        l  = r.get("latency")            or {}
        j  = r.get("jitter")             or {}
        lu = r.get("latency_under_load") or {}
        m  = r.get("mtu")                or {}
        writer.writerow([
            r.get("timestamp_utc", "")[:19],
            r.get("path_label", ""),
            "OK" if r.get("success") else "FAIL",
            t.get("tx_mbps", ""),       t.get("rx_mbps", ""),
            t.get("retransmits", ""),
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
        headers={"Content-Disposition": "attachment; filename=nettest_results.csv"},
    )


@app.route("/api/results/clear", methods=["POST"])
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
    if not _config or not _config.auth.radius_server:
        session["authenticated"] = True
        session["username"] = "admin"
        return redirect(request.args.get("next", "/"))

    error = ""
    if request.method == "POST":
        ip       = request.remote_addr
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        allowed, wait = _check_rate_limit(ip)
        if not allowed:
            error = f"Too many failed attempts. Try again in {wait}s."
        elif not username or not password:
            error = "Username and password are required."
        else:
            _record_attempt(ip)
            try:
                from core.radius_auth import authenticate_radius
                if authenticate_radius(username, password, _config.auth):
                    session.permanent = True
                    session["authenticated"] = True
                    session["username"] = username
                    with _login_lock:
                        _login_attempts[ip].clear()
                    return redirect(request.form.get("next") or "/")
                else:
                    error = "Invalid username or password."
            except Exception as e:
                error = f"Authentication error: {e}"

    next_url = request.args.get("next", "/")
    html = open(os.path.join(STATIC_DIR, "login.html")).read()
    html = html.replace("{{next}}", next_url)
    html = html.replace("{{csrf_token}}", secrets.token_hex(16))
    html = html.replace("{{error}}", error)
    return html


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Serve HTML ─────────────────────────────────────────────

@app.route("/")
@login_required
def serve_dashboard():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/config")
@login_required
def serve_config_page():
    return send_from_directory(STATIC_DIR, "config.html")


# ── Entry point (flask dev server only — use gunicorn in prod) ─

def main():
    parser = argparse.ArgumentParser(description="NetTest Web Dashboard")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--port",   type=int, default=8080)
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"NetTest dashboard → http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

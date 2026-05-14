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
_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config/config.yaml")

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
_jobs_lock       = threading.Lock()
_job_logs: dict  = {}
_job_logs_lock   = threading.Lock()


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
        with _job_logs_lock:
            q = _job_logs.get(self.job_id)
        if q:
            try:
                q.put_nowait(self.format(record))
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


@app.route("/api/agents")
def api_agents():
    agents = [
        {"id": a.id, "label": a.label, "host": a.host, "type": a.type}
        for a in _config.agents
    ]
    return jsonify(agents)


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

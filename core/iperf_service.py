"""
iperf_service.py
Persistent iPerf3 servers on agents and the controller.

Onboarding (and install.sh on the controller) installs the templated unit
systemd/nettest-iperf3@.service and enables it for the configured iPerf3
port(s), so every agent always has a server listening. Tests only check that
it is listening and restart it if it has stopped answering — they no longer
kill and relaunch iPerf3 on the destination for every test.

The service runs as a systemd DynamicUser, so the nettest user's own
`pkill iperf3` / `fuser -k` cleanup of test clients can't touch it.

Agents onboarded before the service existed have no unit (and no sudoers
entry to restart one). For those the old behaviour is kept: kill any
iPerf3 the nettest user owns and start a temporary `iperf3 -s -D`.
Re-onboarding an agent installs the service.
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

_APP_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT_NAME    = "nettest-iperf3@.service"
UNIT_SOURCE  = os.path.join(_APP_DIR, "systemd", UNIT_NAME)
UNIT_PATH    = f"/etc/systemd/system/{UNIT_NAME}"

# iperf3 client output meaning the server isn't taking tests: stuck "busy"
# after an aborted client, or not running at all.
_UNAVAILABLE_MARKERS = ("server is busy", "connection refused", "unable to connect")


def unit_for_port(port: int) -> str:
    return f"nettest-iperf3@{int(port)}.service"


def server_unavailable(client_output: str) -> bool:
    """True if an iperf3 client's output says the server isn't taking tests."""
    low = (client_output or "").lower()
    return any(m in low for m in _UNAVAILABLE_MARKERS)


def is_listening(ssh, port: int) -> bool:
    out = ssh.run(
        f"ss -Htln 'sport = :{port}' 2>/dev/null | grep -q . "
        f"&& echo __IPERF_UP__ || echo __IPERF_DOWN__",
        timeout=5,
    )
    return "__IPERF_UP__" in out and "__IPERF_DOWN__" not in out


def has_service(ssh) -> bool:
    """True if the host has the nettest-iperf3 unit installed (cached per session)."""
    cached = getattr(ssh, "_nettest_iperf_service", None)
    if cached is not None:
        return cached
    out = ssh.run(
        f"test -f {UNIT_PATH} && echo __UNIT_YES__ || echo __UNIT_NO__",
        timeout=5,
    )
    found = "__UNIT_YES__" in out and "__UNIT_NO__" not in out
    try:
        ssh._nettest_iperf_service = found
    except AttributeError:
        pass
    return found


def _wait_listening(ssh, port: int, attempts: int = 10) -> bool:
    for _ in range(attempts):
        if is_listening(ssh, port):
            return True
        time.sleep(0.5)
    return False


def _restart_service(ssh, port: int, label: str) -> bool:
    out = ssh.run(
        f"sudo -n systemctl restart {unit_for_port(port)} 2>&1 "
        f"&& echo __RESTART_OK__ || echo __RESTART_FAIL__",
        timeout=30,
    )
    if "__RESTART_OK__" not in out or "__RESTART_FAIL__" in out:
        detail = out.replace("__RESTART_FAIL__", "").strip()
        logger.warning(f"  Could not restart {unit_for_port(port)} on {label}: "
                       f"{detail[:200] or 'no output'}")
        return False
    if _wait_listening(ssh, port):
        logger.info(f"  iPerf3 service on {label}:{port} restarted and listening")
        return True
    logger.warning(f"  {unit_for_port(port)} on {label} restarted but is not "
                   f"listening on port {port}")
    return False


def _start_temporary_server(ssh, port: int, label: str) -> None:
    """Pre-service behaviour: kill our iPerf3s, free the port, start a daemon."""
    ssh.run("pkill -9 -f iperf3 2>/dev/null || true", timeout=10)
    time.sleep(0.5)
    ssh.run(
        f"fuser -k {port}/tcp 2>/dev/null || true; "
        f"fuser -k {port}/udp 2>/dev/null || true",
        timeout=10
    )
    for _ in range(16):
        if not is_listening(ssh, port):
            break
        time.sleep(0.5)
    ssh.run_background(f"iperf3 -s -p {port} -D")
    time.sleep(1.5)
    if _wait_listening(ssh, port, attempts=6):
        logger.debug(f"iPerf3 server confirmed listening on {label}:{port}")
    else:
        logger.warning(f"iPerf3 server may not be ready on {label}:{port} — proceeding anyway")


def ensure_server(ssh, port: int, label: str = "") -> None:
    """Make sure an iPerf3 server is listening on `port` before a test."""
    label = label or ssh.host
    if has_service(ssh):
        if is_listening(ssh, port):
            logger.debug(f"iPerf3 service listening on {label}:{port}")
            return
        logger.info(f"  iPerf3 service on {label}:{port} is not listening — restarting it...")
        _restart_service(ssh, port, label)
        return
    logger.info(f"  {label} has no iPerf3 service (re-onboard the agent to install it) "
                f"— starting a temporary server")
    _start_temporary_server(ssh, port, label)


def recover_server(ssh, port: int, label: str = "") -> None:
    """Restart a server that is refusing tests or stuck reporting busy."""
    label = label or ssh.host
    if has_service(ssh):
        _restart_service(ssh, port, label)
    else:
        _start_temporary_server(ssh, port, label)


def stop_temporary_server(ssh, port: int) -> None:
    """After a test, stop a temporary (pre-service) server; leave the service alone."""
    if has_service(ssh):
        return
    ssh.kill_background("iperf3")
    try:
        ssh.run(
            f"fuser -k {port}/tcp 2>/dev/null || true; "
            f"fuser -k {port}/udp 2>/dev/null || true",
            timeout=10
        )
    except Exception:
        pass

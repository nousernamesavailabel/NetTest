"""
onboard.py
Agent onboarding module — SSHes into a new host using admin credentials,
installs all required tools, creates the nettest user, deploys the SSH key,
configures sudoers, installs the persistent iPerf3 service, configures the
firewall, verifies the setup, then optionally adds
the agent to config.yaml automatically.

Usage (via main.py):
  python main.py --onboard
  python main.py --onboard --agent-ip 10.5.1.10 --agent-label "Branch E"
"""

import base64
import getpass
import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import time
import yaml

from dataclasses import asdict

from core.agent_packages import REQUIRED_TOOLS, debs_for_agent, os_release_id
from core import iperf_service


# ── Colour helpers for terminal output ────────────────────

def _c(text, code): return f"\033[{code}m{text}\033[0m"
def green(t):  return _c(t, "32")
def yellow(t): return _c(t, "33")
def red(t):    return _c(t, "31")
def cyan(t):   return _c(t, "36")
def bold(t):   return _c(t, "1")
def dim(t):    return _c(t, "2")


import logging as _logging
import re as _re
_log = _logging.getLogger("onboard")

def _strip_ansi(s: str) -> str:
    """Remove ANSI terminal escape sequences."""
    return _re.sub(r'\x1b\[[0-9;]*[mGKHFJ]|\x1b\][^\x07]*\x07|\][0-9]+;[^\\]*\\', '', s)

def _ok(msg):   _log.info(f"  ✓ {_strip_ansi(str(msg))}")
def _warn(msg): _log.warning(f"  ! {_strip_ansi(str(msg))}")
def _err(msg):  _log.error(f"  ✗ {_strip_ansi(str(msg))}")
def _info(msg): _log.info(f"  · {_strip_ansi(str(msg))}")
def _step(n, total, msg): _log.info(f"\n[{n}/{total}] {msg}")


# ── SSH helper (uses Netmiko with password auth) ───────────

def _ssh_admin(host: str, username: str, password: str, port: int = 22, timeout: int = 30):
    """Returns a connected Netmiko SSH manager using password auth."""
    from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
    try:
        conn = ConnectHandler(
            device_type="linux",
            host=host,
            username=username,
            password=password,
            port=port,
            timeout=timeout,
            conn_timeout=timeout,
            auth_timeout=timeout,
            global_cmd_verify=False,
            ssh_config_file=None,
            # Accept new host keys automatically during onboarding
            ssh_strict=False,
        )
        return conn
    except NetmikoAuthenticationException:
        raise RuntimeError(f"Authentication failed for {username}@{host} — check credentials")
    except NetmikoTimeoutException:
        raise RuntimeError(f"Connection timed out to {host}:{port} — check host is reachable")
    except Exception as e:
        raise RuntimeError(f"SSH connection failed to {host}: {e}")


def _run(conn, cmd: str, timeout: int = 60) -> str:
    """Run a command and return output."""
    return conn.send_command_timing(
        cmd,
        read_timeout=timeout,
        last_read=3.0,
        strip_prompt=True,
        strip_command=True,
    )


def _sudo_init(conn, password: str) -> None:
    """Prime sudo credential cache so subsequent sudo calls need no password."""
    conn.send_command_timing(
        f" printf '%s\\n' {shlex.quote(password)} | sudo -S -p '' -v 2>/dev/null",
        last_read=2.0,
        strip_prompt=True,
    )


def _sudo(conn, cmd: str, password: str = "", timeout: int = 120) -> str:
    """Run a sudo command. Assumes sudo is already primed via _sudo_init."""
    return conn.send_command_timing(
        f"sudo {cmd}",
        read_timeout=timeout,
        last_read=3.0,
        strip_prompt=True,
        strip_command=True,
    )


# ── Air-gapped package install ─────────────────────────────

_REMOTE_PKG_DIR = "/tmp/nettest_packages"

# Runs on the agent as root. Serves the staged .debs to apt as a temporary
# local repository — the only source apt sees — and installs the requested
# package *names*: apt picks only what the agent is missing, resolves
# dependencies, and never downgrades. --no-remove: an installed package whose
# matching upgrade isn't staged would otherwise be removed to make room.
_REMOTE_INSTALL_SCRIPT = r"""#!/bin/bash
set -u
D="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$D/lists/partial"
echo "deb [trusted=yes] file:$D ./" > "$D/sources.list"
A=(-o Dir::Etc::SourceList="$D/sources.list"
   -o Dir::Etc::SourceParts=/nonexistent
   -o Dir::State::Lists="$D/lists"
   -o APT::Sandbox::User=root
   -o DPkg::Lock::Timeout=300)
export DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true
apt-get "${A[@]}" update -qq || exit 10
apt-get "${A[@]}" install -y -q --no-install-recommends --no-remove \
  -o Dpkg::Options::=--force-confold "$@"
"""


# apt progress chatter, left out when showing a failed install's output
_APT_PROGRESS = re.compile(r"^\s*((Ign|Get|Hit):\d+ |Reading |Building dependency|Solving dependencies)")


def _packages_index(deb_paths) -> str:
    """Build an apt Packages index for the staged .debs (a flat local repo)."""
    entries = []
    for path in deb_paths:
        deb = os.path.basename(path)
        fields = subprocess.run(["dpkg-deb", "-f", path], capture_output=True,
                                text=True, check=True).stdout.rstrip("\n")
        with open(path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
        entries.append(f"{fields}\nFilename: ./{deb}\n"
                       f"Size: {os.path.getsize(path)}\nSHA256: {sha256}\n")
    return "\n".join(entries)


def _pkg_installed(conn, pkg: str) -> bool:
    # The \n matters: without it the status and the next shell prompt share a
    # line, and netmiko's strip_prompt drops that whole line — status included.
    out = _run(conn, f"dpkg-query -W -f='${{Status}}\\n' {pkg} 2>/dev/null || echo NOT_INSTALLED")
    return "install ok installed" in out


def _install_offline(conn, admin_pass: str, pkg_dir: str, required_tools) -> None:
    """Install the missing required tools from the controller's staged .debs.
    Raises RuntimeError if anything is still missing afterwards."""
    needed = [pkg for tool, pkg in required_tools if not _pkg_installed(conn, pkg)]
    if not needed:
        _ok("All required tools already present")
        return
    _info(f"Missing on agent: {' '.join(needed)}")

    os_release = _strip_ansi(_run(conn, "cat /etc/os-release 2>/dev/null"))
    release = os_release_id(os_release)
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)', os_release, re.M)
    _info(f"Agent OS: {m.group(1) if m else 'unknown'} ({release or 'release unknown'})")

    # Uploaded packages, plus any a release bundle staged for this OS release
    deb_paths = debs_for_agent(pkg_dir, release)
    bundled = [p for p in deb_paths if os.path.dirname(p) != pkg_dir]
    if not deb_paths:
        _err(f"No packages staged for this agent's release ({release or 'unknown'}) in {pkg_dir}")
        _err("Upload packages built for that release via Config → Packages and retry onboarding")
        raise RuntimeError("Air-gapped install failed — no packages staged")
    _info(f"Using {len(deb_paths) - len(bundled)} uploaded and {len(bundled)} bundled package(s) "
          f"— uploaded packages must be built for this agent's release")

    try:
        index = _packages_index(deb_paths)
    except (OSError, subprocess.CalledProcessError) as e:
        raise RuntimeError(f"Could not read staged packages in {pkg_dir}: {e}")

    remote = _REMOTE_PKG_DIR
    _run(conn, f" printf '%s\\n' {shlex.quote(admin_pass)} | sudo -S -p '' rm -rf {remote} 2>/dev/null; "
               f"rm -rf {remote} 2>/dev/null; mkdir -p {remote}")

    # Copy over SFTP on the existing admin connection
    import paramiko
    try:
        sftp = paramiko.SFTPClient.from_transport(conn.remote_conn.get_transport())
    except Exception as e:
        raise RuntimeError(f"Could not open SFTP channel to agent: {e}")
    try:
        _info(f"Copying {len(deb_paths)} package(s) to agent...")
        for path in deb_paths:
            sftp.put(path, f"{remote}/{os.path.basename(path)}")
        sftp.putfo(io.BytesIO(index.encode()), f"{remote}/Packages")
        sftp.putfo(io.BytesIO(_REMOTE_INSTALL_SCRIPT.encode()), f"{remote}/install.sh")
    except Exception as e:
        raise RuntimeError(f"Copying packages to agent failed: {e}")
    finally:
        sftp.close()
    _ok("Packages copied")

    # Run apt and wait for it to really finish — the __RC_n__ marker only
    # prints once apt exits, however long unpacking takes.
    _info(f"Installing with apt from the staged packages: {' '.join(needed)}")
    _sudo_init(conn, admin_pass)
    out = conn.send_command(
        f"sudo -n bash {remote}/install.sh {' '.join(needed)} > {remote}/apt.log 2>&1; "
        f"echo __RC_$?__",
        expect_string=r"__RC_\d+__", read_timeout=900,
        strip_prompt=True, strip_command=True)
    m = re.search(r"__RC_(\d+)__", out)
    rc = int(m.group(1)) if m else -1
    log_tail = _strip_ansi(_run(conn, f"tail -n 40 {remote}/apt.log 2>/dev/null"))

    _run(conn, f" printf '%s\\n' {shlex.quote(admin_pass)} | sudo -S -p '' rm -rf {remote} 2>/dev/null; true")

    if rc != 0:
        _warn(f"apt exited with code {rc} — output:")
        for line in log_tail.splitlines():
            if line.strip() and not _APT_PROGRESS.match(line):
                _warn(f"    {line.rstrip()[:160]}")
        if "remove is disabled" in log_tail:
            _err("Installing would REMOVE packages already on the agent: the staged packages upgrade")
            _err("something they depend on without including their matching upgrade. Nothing was changed.")
            _err("Stage those packages' .debs too (same version as the staged ones), or stage")
            _err("packages that match the versions already on the agent.")
        elif "unmet dependencies" in log_tail.lower() or "Unable to locate package" in log_tail:
            _err("A required .deb or one of its dependencies is not staged, or the staged packages")
            _err("were built for a different OS release than the agent. Nothing was changed.")
        elif "a password is required" in log_tail:
            _err("sudo refused — check the admin account can run sudo")

    failures = []
    for tool, pkg in required_tools:
        if _pkg_installed(conn, pkg):
            _ok(f"  {tool} installed")
        else:
            _warn(f"  {tool} NOT installed ({pkg})")
            failures.append(pkg)
    if failures:
        _err("Upload the missing packages (and their dependencies) via Config → Packages and retry onboarding")
        raise RuntimeError(f"Air-gapped install failed — missing packages: {', '.join(failures)}")
    _ok("All packages installed successfully")


# ── Main onboarding logic ──────────────────────────────────

def _iperf_ports(config) -> list:
    """The iPerf3 port(s) tests use — one service instance per port."""
    tp = config.test_params
    return sorted({int(tp.throughput.iperf3_port), int(tp.jitter.iperf3_port)})


def _install_iperf_service(conn, admin_pass: str, config) -> None:
    """Install the nettest-iperf3@ unit and enable it for each test port.

    Any iPerf3 left running by the nettest user (the pre-service temporary
    servers) is stopped first, or it would hold the port the service binds.
    """
    try:
        with open(iperf_service.UNIT_SOURCE, "rb") as f:
            unit_b64 = base64.b64encode(f.read()).decode()
    except OSError as e:
        _warn(f"Could not read {iperf_service.UNIT_SOURCE}: {e} — skipping iPerf3 service")
        _warn("Tests will start a temporary iPerf3 server on this agent instead")
        return
    nettest_user = config.ssh_defaults.username or "nettest"
    _sudo(conn, f"bash -c \"echo {unit_b64} | base64 -d > {iperf_service.UNIT_PATH}\"",
          admin_pass)
    _sudo(conn, f"chmod 644 {iperf_service.UNIT_PATH}", admin_pass)
    _sudo(conn, f"pkill -u {nettest_user} -x iperf3", admin_pass)
    _sudo(conn, "systemctl daemon-reload", admin_pass)
    for port in _iperf_ports(config):
        unit = iperf_service.unit_for_port(port)
        _sudo(conn, f"systemctl enable {unit}", admin_pass)
        _sudo(conn, f"systemctl restart {unit}", admin_pass)
        time.sleep(1)
        state = _run(conn, f"systemctl is-active --quiet {unit} "
                           f"&& echo __ACTIVE__ || echo __INACTIVE__")
        if "__ACTIVE__" in state and "__INACTIVE__" not in state:
            _ok(f"{unit} running — iPerf3 listening on port {port}")
        else:
            _warn(f"{unit} is not running — check: sudo journalctl -u {unit}")


TOTAL_STEPS = 10


def onboard_agent(config_path: str,
                  agent_ip: str = None,
                  agent_test_ip: str = None,
                  agent_label: str = None,
                  agent_id: str = None,
                  agent_type: str = None,
                  admin_user: str = None,
                  admin_pass: str = None,
                  admin_port: int = 22,
                  interactive: bool = None,
                  air_gapped: bool = False,
                  packages_dir: str = None,
                  reonboard: bool = False) -> bool:
    """
    Full agent onboarding flow.
    reonboard=True re-runs it for an agent already in config.yaml (e.g. to
    install the iPerf3 service): the config entry is left as it is.
    Returns True on success, False on failure.
    """

    _log.info(f"\n{'='*55}")
    _log.info(f"  NetTest Agent {'Re-onboarding' if reonboard else 'Onboarding'}")
    _log.info(f"{'='*55}\n")

    # ── Load config to get SSH key and nettest username ────
    try:
        from core.config_loader import load_config
        config = load_config(config_path)
    except Exception as e:
        _err(f"Failed to load config: {e}")
        return False

    nettest_user = config.ssh_defaults.username or "nettest"
    key_file     = os.path.expanduser(config.ssh_defaults.key_file)
    key_pub_file = key_file + ".pub"

    if not os.path.isfile(key_pub_file):
        _err(f"Public key not found: {key_pub_file}")
        _err("Generate one with: ssh-keygen -t ed25519 -f ~/.ssh/nettest_key -C nettest-controller")
        return False

    with open(key_pub_file) as f:
        public_key = f.read().strip()

    # ── Collect info interactively if not provided ─────────
    # When called from the web UI all values are pre-supplied so we skip prompts.
    if interactive is None:
        interactive = sys.stdin.isatty()

    if not agent_ip:
        if not interactive:
            _err("agent_ip is required")
            return False
        agent_ip = input("  Agent IP address: ").strip()
    if not agent_ip:
        _err("IP address is required.")
        return False

    if not agent_label:
        agent_label = (input(f"  Agent label (human name) [{agent_ip}]: ").strip()
                       if interactive else "") or agent_ip

    if not agent_id:
        default_id = re.sub(r"[^a-z0-9_]", "_", agent_label.lower())
        agent_id = (input(f"  Agent ID (no spaces) [{default_id}]: ").strip()
                    if interactive else "") or default_id

    if not agent_type:
        if interactive:
            type_input = input("  Agent type [endpoint/svi_adjacent] (default: endpoint): ").strip()
            agent_type = type_input if type_input in ("endpoint", "svi_adjacent") else "endpoint"
        else:
            agent_type = "endpoint"

    if interactive:
        print()

    if not admin_user:
        if not interactive:
            _err("admin_user is required")
            return False
        admin_user = input(f"  Admin SSH username on {agent_ip}: ").strip()
    if not admin_user:
        _err("Admin username is required.")
        return False

    if not admin_pass:
        if not interactive:
            _err("admin_pass is required")
            return False
        admin_pass = getpass.getpass(f"  Admin SSH password for {admin_user}@{agent_ip}: ")
    if not admin_pass:
        _err("Admin password is required.")
        return False

    _log.info(f"\n{dim('─'*55)}")
    _log.info(f"  Host    : {agent_ip}")
    if agent_test_ip:
        _log.info(f"  Test IP : {agent_test_ip}")
    _log.info(f"  Label   : {agent_label}")
    _log.info(f"  ID      : {agent_id}")
    _log.info(f"  Type    : {agent_type}")
    _log.info(f"  Admin   : {admin_user}@{agent_ip}:{admin_port}")
    _log.info(f"  NetTest : {nettest_user}")
    _log.info(f"  Key     : {key_pub_file}")
    _log.info(f"{dim('─'*55)}\n")

    if interactive:
        confirm = input("Proceed with onboarding? [Y/n]: ").strip().lower()
        if confirm not in ("", "y", "yes"):
            _log.info("Aborted.")
            return False
        print()

    # ── Step 1: Connect with admin credentials ─────────────
    _step(1, TOTAL_STEPS, f"Connecting to {agent_ip} as {admin_user}...")
    try:
        conn = _ssh_admin(agent_ip, admin_user, admin_pass, admin_port)
        _ok(f"Connected to {agent_ip}")
        # Prime sudo credential cache so all subsequent sudo calls work without prompts
        _sudo_init(conn, admin_pass)
    except RuntimeError as e:
        _err(str(e))
        return False

    success = False
    try:
        # ── Step 2: Install required tools ───────────────────
        if air_gapped:
            _step(2, TOTAL_STEPS, "Air-gapped mode — installing packages from staged .deb files...")
            pkg_dir = os.path.abspath(packages_dir or os.path.join(
                os.path.dirname(os.path.abspath(config_path)), "..", "packages"))
            _install_offline(conn, admin_pass, pkg_dir, REQUIRED_TOOLS)
        else:
            _step(2, TOTAL_STEPS, "Installing required packages via apt...")
            _info("Checking which packages are needed...")
            pkgs_needed = []
            for tool, pkg in REQUIRED_TOOLS:
                if _pkg_installed(conn, pkg):
                    _ok(f"{tool} already installed")
                else:
                    _info(f"{tool} not installed — will install")
                    pkgs_needed.append(pkg)

            if pkgs_needed:
                _info(f"Installing: {' '.join(pkgs_needed)}")
                pkgs_str = ' '.join(pkgs_needed)
                install_cmd = (
                    f"sudo DEBIAN_FRONTEND=noninteractive "
                    f"DEBCONF_NONINTERACTIVE_SEEN=true "
                    f"apt-get install -y -o Dpkg::Options::='--force-confdef' "
                    f"-o Dpkg::Options::='--force-confold' "
                    f"{pkgs_str} "
                    f"> /tmp/nettest_apt.log 2>&1 && echo __APT_OK__ || echo __APT_FAIL__"
                )
                result = _run(conn, install_cmd, timeout=180)
                log_content = _run(conn, "cat /tmp/nettest_apt.log 2>/dev/null | tail -8")
                log_clean = _strip_ansi(log_content.strip())
                if "__APT_OK__" in result or "__APT_OK__" in log_clean:
                    _ok("Package installation completed")
                else:
                    _warn("apt-get returned non-zero exit — log:")
                    for line in log_clean.splitlines():
                        line = line.strip()
                        if line and "__APT" not in line and "@" not in line:
                            _warn(line[:120])
                for tool, pkg in REQUIRED_TOOLS:
                    if pkg in pkgs_needed:
                        if _pkg_installed(conn, pkg):
                            _ok(f"{tool} installed successfully")
                        else:
                            _warn(f"{tool} still not found — run manually: sudo apt install {pkg}")
            else:
                _ok("All required tools already present")

        # ── Step 3: Create nettest user ────────────────────
        _step(3, TOTAL_STEPS, f"Creating user '{nettest_user}'...")
        existing = _run(conn, f"id {nettest_user} 2>&1")
        if "uid=" in existing:
            _ok(f"User '{nettest_user}' already exists")
        else:
            _sudo(conn, f"useradd -m -s /bin/bash {nettest_user}", admin_pass)
            _ok(f"User '{nettest_user}' created")

        # ── Step 4: Create .ssh directory ─────────────────
        _step(4, TOTAL_STEPS, "Configuring SSH directory...")
        _sudo(conn, f"mkdir -p /home/{nettest_user}/.ssh", admin_pass)
        _sudo(conn, f"chown {nettest_user}:{nettest_user} /home/{nettest_user}/.ssh", admin_pass)
        _sudo(conn, f"chmod 700 /home/{nettest_user}/.ssh", admin_pass)
        _ok("SSH directory created with correct permissions")

        # ── Step 5: Deploy public key ──────────────────────
        _step(5, TOTAL_STEPS, "Deploying SSH public key...")
        # Write key safely — escape single quotes in the key
        safe_key = public_key.replace("'", "'\\''")
        _sudo(conn,
              f"bash -c \"echo '{safe_key}' > /home/{nettest_user}/.ssh/authorized_keys\"",
              admin_pass)
        _sudo(conn,
              f"chown {nettest_user}:{nettest_user} /home/{nettest_user}/.ssh/authorized_keys",
              admin_pass)
        _sudo(conn, f"chmod 600 /home/{nettest_user}/.ssh/authorized_keys", admin_pass)

        # Verify key landed correctly
        installed = _run(conn, f"sudo cat /home/{nettest_user}/.ssh/authorized_keys 2>&1")
        if "ssh-ed25519" in installed or "ssh-rsa" in installed:
            _ok("Public key installed")
        else:
            _err("Key does not appear to be installed correctly")
            _err(f"authorized_keys content: {installed[:100]}")
            return False

        # ── Step 6: Sudoers ────────────────────────────────
        _step(6, TOTAL_STEPS, "Configuring sudoers...")
        sudoers_line = (
            f"{nettest_user} ALL=(ALL) NOPASSWD: "
            f"/usr/bin/iperf3, /usr/bin/mtr, /usr/bin/pkill, /usr/bin/ping, "
            f"/usr/bin/traceroute, /usr/bin/fuser, /usr/bin/dpkg"
        )
        # One entry per iPerf3 port — sudo(-rs) allows no wildcards in arguments
        for port in _iperf_ports(config):
            sudoers_line += (f", /usr/bin/systemctl restart "
                             f"{iperf_service.unit_for_port(port)}")
        _sudo(conn,
              f"bash -c \"echo '{sudoers_line}' > /etc/sudoers.d/nettest\"",
              admin_pass)
        _sudo(conn, "chmod 440 /etc/sudoers.d/nettest", admin_pass)

        # Validate
        valid = _run(conn, "sudo visudo -c -f /etc/sudoers.d/nettest 2>&1")
        if "parsed OK" in valid or "OK" in valid:
            _ok("Sudoers entry valid")
        else:
            _warn(f"Sudoers validation returned: {valid.strip()[:80]}")

        # ── Step 7: iPerf3 service ─────────────────────────
        _step(7, TOTAL_STEPS, "Installing iPerf3 service...")
        _install_iperf_service(conn, admin_pass, config)

        # ── Step 8: Firewall ───────────────────────────────
        _step(8, TOTAL_STEPS, "Configuring firewall...")
        ufw_status = _run(conn, "sudo ufw status 2>&1")
        if "inactive" in ufw_status.lower():
            _info("UFW is inactive — skipping firewall rules")
        else:
            _sudo(conn, "ufw allow 22/tcp",   admin_pass)
            _sudo(conn, "ufw allow 5201/tcp",  admin_pass)
            _sudo(conn, "ufw allow 5201/udp",  admin_pass)
            _ok("Ports 22 (SSH) and 5201 (iPerf3) opened")

        # ── Step 9: Verify key-based access ───────────────
        _step(9, TOTAL_STEPS, "Verifying key-based SSH access...")
        conn.disconnect()
        time.sleep(1)

        # Try to connect with the nettest key
        from netmiko import ConnectHandler
        try:
            test_conn = ConnectHandler(
                device_type="linux",
                host=agent_ip,
                username=nettest_user,
                use_keys=True,
                key_file=key_file,
                port=admin_port,
                timeout=15,
                conn_timeout=15,
                global_cmd_verify=False,
            )
            hostname_raw = test_conn.send_command_timing("hostname", last_read=2.0)
            test_conn.disconnect()
            # Strip ANSI/OSC escape sequences from hostname output
            hostname = _re.sub(r'[\x00-\x1f\x7f].*?(?=[a-zA-Z0-9])|\\033\\][^\\007]*\\007|', '', hostname_raw)
            hostname = _re.sub(r'[^a-zA-Z0-9._-]', '', hostname_raw.split('\n')[0]).strip() or hostname_raw.split()[0]
            _ok(f"Key-based login successful — hostname: {hostname}")
        except Exception as e:
            _err(f"Key-based login failed: {e}")
            _err("The agent was configured but key auth is not working.")
            _err("Check /home/nettest/.ssh/ ownership and permissions manually.")
            return False

        success = True

    except Exception as e:
        _err(f"Onboarding failed: {e}")
        try:
            conn.disconnect()
        except Exception:
            pass
        return False

    if not success:
        return False

    # ── Step 10: Add to config.yaml ────────────────────────
    _step(10, TOTAL_STEPS, "Adding agent to config.yaml...")

    # Check for duplicate ID
    existing_ids = [a.id for a in config.agents]
    if reonboard and agent_id in existing_ids:
        _ok(f"Agent '{agent_id}' already in config.yaml — kept as is")
    elif agent_id in existing_ids:
        _warn(f"Agent ID '{agent_id}' already exists in config — skipping config update")
        _warn("Edit config.yaml or the web config editor to update it manually")
    else:
        try:
            with open(config_path, "r") as f:
                raw = yaml.safe_load(f)

            entry = {
                "id":           agent_id,
                "label":        agent_label,
                "host_mgmt_ip": agent_ip,
                "type":         agent_type,
            }
            if agent_test_ip:
                entry["host_test_ip"] = agent_test_ip
            raw["agents"].append(entry)

            with open(config_path, "w") as f:
                yaml.dump(raw, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)

            _ok(f"Agent '{agent_label}' added to config.yaml")
            _info("Restart the scheduler to pick up the new agent:")
            _info("  sudo systemctl restart nettest")
        except Exception as e:
            _warn(f"Could not update config.yaml: {e}")
            _warn("Add the agent manually in the web config editor")

    # ── Done ───────────────────────────────────────────────
    _log.info(f"\n{'='*55}")
    _log.info(f"  {'Re-onboarding' if reonboard else 'Onboarding'} complete!  "
              f"{agent_label} ({agent_ip})")
    _log.info(f"{'='*55}")
    _log.info(f"\n  Agent ID : {agent_id}")
    if not reonboard:
        _log.info(f"  Next step: Define test paths in the config editor")
    _log.info(f"  Dashboard: http://<controller-ip>:8080/config\n")

    return True

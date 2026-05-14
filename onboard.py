"""
onboard.py
Agent onboarding module — SSHes into a new host using admin credentials,
installs all required tools, creates the nettest user, deploys the SSH key,
configures sudoers and firewall, verifies the setup, then optionally adds
the agent to config.yaml automatically.

Usage (via main.py):
  python main.py --onboard
  python main.py --onboard --agent-ip 10.5.1.10 --agent-label "Branch E"
"""

import getpass
import os
import re
import sys
import time
import yaml

from dataclasses import asdict


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
        f"echo '{password}' | sudo -S true 2>/dev/null",
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


# ── Main onboarding logic ──────────────────────────────────

TOTAL_STEPS = 9


def onboard_agent(config_path: str,
                  agent_ip: str = None,
                  agent_label: str = None,
                  agent_id: str = None,
                  agent_type: str = None,
                  admin_user: str = None,
                  admin_pass: str = None,
                  admin_port: int = 22,
                  interactive: bool = None) -> bool:
    """
    Full agent onboarding flow.
    Returns True on success, False on failure.
    """

    _log.info(f"\n{'='*55}")
    _log.info(f"  NetTest Agent Onboarding")
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
        _step(2, TOTAL_STEPS, "Installing iperf3, mtr-tiny, and iputils-ping...")
        _info("Checking which packages are needed...")

        # Check what is missing using a reliable sentinel pattern
        pkgs_needed = []
        for tool, pkg in [
            ("iperf3",      "iperf3"),
            ("mtr",         "mtr-tiny"),
            ("ping",        "iputils-ping"),
            ("traceroute",  "traceroute"),
        ]:
            # Use dpkg-query — definitive, no false positives from shell noise
            result = _run(conn,
                f"dpkg-query -W -f='${{Status}}' {pkg} 2>/dev/null "
                f"|| echo NOT_INSTALLED")
            if "install ok installed" in result:
                _ok(f"{tool} already installed")
            else:
                _info(f"{tool} not installed — will install")
                pkgs_needed.append(pkg)

        if pkgs_needed:
            _info(f"Installing: {' '.join(pkgs_needed)}")
            # Run install via sudo with full output captured to log
            # Using bash -c with explicit sudo -n (non-interactive) after priming
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

            # Verify each newly installed package
            for tool, pkg in [("iperf3","iperf3"),("mtr","mtr-tiny"),("ping","iputils-ping"),("traceroute","traceroute")]:
                if pkg in pkgs_needed:
                    result = _run(conn,
                        f"dpkg-query -W -f='${{Status}}' {pkg} 2>/dev/null "
                        f"|| echo NOT_INSTALLED")
                    if "install ok installed" in result:
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
            f"/usr/bin/iperf3, /usr/bin/mtr, /usr/bin/pkill, /usr/bin/ping"
        )
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

        # ── Step 7: Firewall ───────────────────────────────
        _step(7, TOTAL_STEPS, "Configuring firewall...")
        ufw_status = _run(conn, "sudo ufw status 2>&1")
        if "inactive" in ufw_status.lower():
            _info("UFW is inactive — skipping firewall rules")
        else:
            _sudo(conn, "ufw allow 22/tcp",   admin_pass)
            _sudo(conn, "ufw allow 5201/tcp",  admin_pass)
            _sudo(conn, "ufw allow 5201/udp",  admin_pass)
            _ok("Ports 22 (SSH) and 5201 (iPerf3) opened")

        # ── Step 8: Verify key-based access ───────────────
        _step(8, TOTAL_STEPS, "Verifying key-based SSH access...")
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

    # ── Step 9: Add to config.yaml ─────────────────────────
    _step(9, TOTAL_STEPS, "Adding agent to config.yaml...")

    # Check for duplicate ID
    existing_ids = [a.id for a in config.agents]
    if agent_id in existing_ids:
        _warn(f"Agent ID '{agent_id}' already exists in config — skipping config update")
        _warn("Edit config.yaml or the web config editor to update it manually")
    else:
        try:
            with open(config_path, "r") as f:
                raw = yaml.safe_load(f)

            raw["agents"].append({
                "id":    agent_id,
                "label": agent_label,
                "host":  agent_ip,
                "type":  agent_type,
            })

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
    _log.info(f"  Onboarding complete!  {agent_label} ({agent_ip})")
    _log.info(f"{'='*55}")
    _log.info(f"\n  Agent ID : {agent_id}")
    _log.info(f"  Next step: Define test paths in the config editor")
    _log.info(f"  Dashboard: http://<controller-ip>:8080/config\n")

    return True

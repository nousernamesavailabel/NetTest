# Network Test Controller

Automated network measurement controller for multi-site, multi-VLAN environments.
Tests throughput, latency, jitter, and MTU across defined paths between agents.

<img width="1917" height="862" alt="image" src="https://github.com/user-attachments/assets/597dbb9e-b3b2-441d-ac4a-307f987e1570" />

<img width="1177" height="612" alt="image" src="https://github.com/user-attachments/assets/530b9816-9a52-4598-8cc4-41388054e763" />


## Requirements (Controller Host)

- Ubuntu/Debian Linux
- Python 3.10+
- SSH key-based access to all agent hosts

## Requirements (Agent Hosts)

Each branch endpoint and SVI-adjacent probe needs:
```bash
sudo apt install -y iperf3 mtr-tiny iputils-ping
```

The controller connects to agents via SSH and runs these tools remotely.
No persistent daemon is required on agents beyond sshd.

---

## Quick Install (Recommended)

`install.sh` sets up the venv, dependencies, `nettest` service user, SSH
key, systemd services, and (optionally) an nginx HTTPS reverse proxy in
one pass. Run it from the extracted release bundle or a clone of this repo:

```bash
sudo ./install.sh                 # fresh install
sudo ./install.sh --upgrade       # upgrade code, keep config and keys
sudo ./install.sh --show-key      # print the controller's SSH public key
sudo ./install.sh --setup-https   # also configure nginx + self-signed TLS
sudo ./install.sh --setup-local-user  # add a local dashboard login account
```

Release bundles install with no internet connection. `make_release.sh`
ships every system package (with dependencies) in `vendor/debs` and every
Python wheel pinned in `requirements.lock` in `vendor/wheels`, and
`install.sh` uses them automatically (set `NETTEST_ONLINE=true` to use apt
and PyPI instead). Build releases on an online machine running the same
OS release as the controllers (currently Ubuntu 26.04, Python 3.14).

Offline installs never remove packages. If a target is at an older patch
level than the build machine, apt may have to upgrade some of its installed
packages to the bundled versions. The bundle includes the exact-version
companions of those upgrades (e.g. `libpython3.14` with `python3.14`) for
every package installed on the build machine. If a target has one the build
machine doesn't, `install.sh` stops without changing anything and names it.
In that case, rebuild with that target's package list:

```bash
# on the target
dpkg-query -W -f='${Package}\n' > manifest.txt
# on the build machine
NETTEST_TARGET_MANIFEST=manifest.txt ./make_release.sh
```

To install from your own package directories instead, set
`NETTEST_PACKAGES_DIR` / `NETTEST_WHEELS_DIR` to local `.deb` / `.whl`
directories.

Python dependencies: edit `requirements.txt`, then run
`./make_release.sh --lock` to regenerate the exact pins in `requirements.lock`.

Then edit `config/config.yaml` (Steps 2–5 below still apply) and start the
service:

```bash
sudo systemctl start nettest nettest-web
```

To roll back a bad upgrade, run `sudo ./rollback.sh` to list snapshots
taken automatically by `install.sh --upgrade`, then `sudo ./rollback.sh <snapshot>`.

### Manual Setup

If you'd rather not use `install.sh`, or you're setting up a dev environment:

#### 1. Clone and install dependencies
```bash
cd nettest/
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create your local config from the example. The real `config/config.yaml`
contains site inventory and secrets, so it is intentionally ignored by git.

```bash
cp config/config.example.yaml config/config.yaml
```

#### 2. Configure SSH key access
```bash
# Generate key if needed
ssh-keygen -t ed25519 -f ~/.ssh/nettest_key

# Copy to each agent
ssh-copy-id -i ~/.ssh/nettest_key nettest@10.1.1.10
ssh-copy-id -i ~/.ssh/nettest_key nettest@10.2.1.10
# ... repeat for all agents
```

Update `config/config.yaml`:
```yaml
ssh_defaults:
  username: "nettest"
  key_file: "~/.ssh/nettest_key"
```

#### 3. Create the nettest user on each agent
```bash
# Run on each agent host
sudo useradd -m -s /bin/bash nettest
sudo mkdir -p /home/nettest/.ssh
# Paste controller's public key into /home/nettest/.ssh/authorized_keys
sudo chown -R nettest:nettest /home/nettest/.ssh
sudo chmod 700 /home/nettest/.ssh
sudo chmod 600 /home/nettest/.ssh/authorized_keys
```

#### 4. Open firewall ports on agents
```bash
# iPerf3 (TCP + UDP)
sudo ufw allow 5201/tcp
sudo ufw allow 5201/udp

# SSH (already open if you're connected)
sudo ufw allow 22/tcp
```

#### 5. Edit config/config.yaml
- Update agent IPs to match your environment
- Define your actual test paths
- Adjust schedule intervals as needed
- Set `auth.session_secret` to a long random string (it signs dashboard
  login cookies; if blank, a new one is generated on every restart and
  everyone is logged out)
- Dashboard login (RADIUS or local accounts) can be set here or later
  from Config → Auth in the web UI — see [Authentication](#authentication)

---

## Usage

```bash
# List all configured paths
python main.py --list-paths

# Run all paths once (useful for initial testing)
python main.py --run-once

# Run latency/jitter only (faster, good for initial validation)
python main.py --run-once --latency

# Run a specific path by ID
python main.py --path branch_a_to_hub

# View today's results
python main.py --results today

# View results for a specific date
python main.py --results 2024-11-15

# Run continuous scheduler (production mode)
python main.py

# Run with alternate config
python main.py --config /etc/nettest/config.yaml
```

### Terminal UI

`tui.py` gives a live-updating terminal dashboard of path status and
per-metric health, useful for keeping an eye on things over SSH without
the web dashboard:

```bash
# Live dashboard (auto-refreshes)
python tui.py

# Print a snapshot and exit
python tui.py --once

# Focus on a single path
python tui.py --path branch_a_to_hub
```

### Web Dashboard

`web_dashboard.py` (served via gunicorn under systemd, see below) provides:

- **Dashboard** (`/`) — live streaming test output, sparkline trend grid,
  traceroute and MTR hop visualization
- **Config** (`/config`) — edit agents, paths, schedule, SSH, tests,
  packages, HTTPS and login (Auth) settings from the browser;
  export/import config as a `.tar.gz` bundle; apply or roll back release
  updates (Updates); turn on timed debug logging (Logging)
- **Compare** (`/compare`) — side-by-side comparison of past runs
- **Speed Test** (`/speedtest`) — on-demand ping/download/upload/MTU probe
- Result annotations — attach notes and tags to individual test runs
- Optional login (RADIUS or local accounts) with session management and
  rate limiting — see [Authentication](#authentication)

### Authentication

Dashboard login is off by default. Choose the login method under
**Config → Auth**, which also holds the RADIUS server settings and the
local user list, so no `config.yaml` editing is needed. The methods are:

- **Disabled** — no login; anyone who can reach the dashboard can use it,
  including the Config page.
- **RADIUS** — logins are checked against a RADIUS server. Set the server,
  port (default 1812), shared secret and timeout on the Auth page; the
  method can't be switched to RADIUS until a server and secret are set.
  If the RADIUS server can't be reached (timeout or connection error),
  any local users are accepted as a fallback login. A rejected password
  is not retried against local users.
- **Local accounts** — logins are checked against accounts stored in
  `config.yaml` as salted password hashes. Add them on the Auth page or
  with `sudo ./install.sh --setup-local-user`.

The same settings live in the `auth:` section of `config.yaml`
(`method: none | radius | local`; see `config/config.example.yaml`).
`method: none` disables login even when RADIUS or local users are
configured. A blank `method` is inferred for older configs:
`radius_server` set means RADIUS, otherwise local users mean local,
otherwise disabled. The Auth page always shows the method actually in use.

Secrets stay on the server: the RADIUS shared secret, `session_secret`,
local password hashes and the SSH default password are never sent to the
browser. The RADIUS secret field on the Auth page starts blank; leave it
blank to keep the saved secret, or type a new one to replace it.


### Onboarding agents without internet

Agents on the same OS release as the controller need no preparation. A
release bundle already contains the agent tools and their dependencies, and
`install.sh` and Config → Updates stage them in `packages/bundled/<os>-<version>/`
(e.g. `ubuntu-26.04`). Just onboard with **Air-gapped** ticked.

For agents on another release, first upload their packages under
**Config → Packages**: `iperf3`, `mtr-tiny`, `iputils-ping`, `traceroute`,
`psmisc` and their dependencies (`libiperf0`, `libsctp1`, and so on), built
for that release. Uploaded packages are offered to every agent. Bundled ones
are offered only to agents whose release matches, so an agent on another
release never gets packages it can't use.

Onboarding copies the staged packages to the agent as a temporary local apt
repository and installs only the tools the agent is missing. apt resolves
dependencies from the staged packages, never downgrades, and never removes
anything. If a package is missing, built for a different release, or would
force a removal, onboarding stops without changing the agent. It shows
apt's error and the agent's OS release.

To collect the packages for another release, on an online machine running it:

```bash
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests \
  --no-conflicts --no-breaks --no-replaces --no-enhances \
  iperf3 mtr-tiny iputils-ping traceroute psmisc | grep '^[a-z0-9]' | sort -u)
```

---

## Project Structure

```
nettest/
├── main.py                   # Entry point and CLI (scheduler/one-shot runs)
├── tui.py                    # Terminal UI — live dashboard
├── web_dashboard.py          # Flask web dashboard + REST API
├── install.sh                # Install / upgrade (online or air-gapped)
├── rollback.sh                # Restore a previous version from a snapshot
├── make_release.sh            # Build a versioned release tarball
├── version.txt
├── requirements.txt           # Top-level Python dependencies
├── requirements.lock          # Exact pins (generated: make_release.sh --lock)
├── config/
│   ├── config.example.yaml   # Sanitized starter config committed to git
│   └── config.yaml           # Local/private config ignored by git
├── core/
│   ├── config_loader.py      # Loads and validates config.yaml
│   ├── results.py            # Result dataclasses + local JSON store
│   ├── annotations.py        # Notes/tags sidecar store for run results
│   ├── ssh_manager.py        # Netmiko SSH wrapper with retry logic
│   ├── path_tester.py        # Orchestrates tests for one path
│   ├── scheduler.py          # Drives periodic test execution
│   ├── radius_auth.py        # RADIUS authentication for the web dashboard
│   └── agent_packages.py     # Agent packages for air-gapped onboarding
├── runners/
│   ├── runner_throughput.py  # iPerf3 TCP/UDP throughput
│   ├── runner_latency.py     # Ping latency, UDP jitter, MTU discovery,
│   │                         # and latency-under-load (bufferbloat)
│   └── runner_traceroute.py  # Traceroute with forward/reverse hop flow
├── web/                       # Dashboard static pages (index, config,
│   │                          # compare, speedtest, login)
│   └── static/chart.umd.js    # Chart.js, bundled so graphs work offline
├── systemd/                   # nettest.service + nettest-web.service units
├── vendor/                    # Release bundles only (built by make_release.sh)
│   ├── wheels/                # Python wheels for requirements.lock
│   └── debs/                  # System packages + dependencies
├── packages/                  # Agent .debs: uploads, plus bundled/<os>-<version>/
├── results/                  # Auto-created — JSONL result files per day
└── logs/                     # Auto-created — controller.log
```

---

## Result Files

Results are stored as JSONL (one JSON record per line) in `results/`:
```
results/results_2024-11-15.jsonl
results/results_2024-11-16.jsonl
```

Each record contains the full result for one path test run, including
all sub-test results. These files are the handoff point for future
InfluxDB integration — the InfluxDB writer will consume this format.

---

## Adding InfluxDB Integration (Next Step)

1. Uncomment `influxdb-client` in requirements.txt
2. Add InfluxDB connection details to config.yaml
3. Create `core/influx_writer.py` with a `write_result(PathTestResult)` function
4. Register it as a callback in main.py:
   ```python
   from core.influx_writer import InfluxWriter
   writer = InfluxWriter(config)
   scheduler.add_result_callback(writer.write_result)
   ```

The scheduler's callback system is already wired for this — no other
changes needed.

---

## Running as a systemd Service

`install.sh` installs and enables both units from `systemd/` automatically:

- **`nettest.service`** — the scheduler (`main.py`)
- **`nettest-web.service`** — the web dashboard, served via gunicorn (gevent workers) on port 8080

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nettest nettest-web
sudo journalctl -u nettest -f        # Follow scheduler logs
sudo journalctl -u nettest-web -f    # Follow web dashboard logs
```

Run `sudo ./install.sh --setup-https` to put nginx in front of
`nettest-web` with a self-signed certificate.

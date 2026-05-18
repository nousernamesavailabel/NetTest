# NetTest — Network Test Controller

Automated network measurement controller for multi-site, multi-VLAN environments.
Tests throughput, latency, jitter, MTU, and traceroute across defined paths between agents,
with a live web dashboard, HTTPS support, and air-gapped agent onboarding.

<img width="1917" height="862" alt="image" src="https://github.com/user-attachments/assets/597dbb9e-b3b2-441d-ac4a-307f987e1570" />

<img width="1177" height="612" alt="image" src="https://github.com/user-attachments/assets/530b9816-9a52-4598-8cc4-41388054e763" />

---

## Requirements

### Controller Host
- Ubuntu/Debian Linux (tested on Ubuntu 26.04)
- Python 3.10+
- nginx (installed automatically by install.sh)
- SSH key-based access to all agent hosts

### Agent Hosts
Each agent needs the following packages. On internet-connected agents:
```bash
sudo apt install -y iperf3 libiperf0 libsctp1 mtr-tiny iputils-ping traceroute psmisc
```

For air-gapped agents, see the **Air-Gapped Onboarding** section below.

---

## Fresh Install

Run the install script from the project root (the directory containing `main.py`):

```bash
sudo ./install.sh
```

This will:
- Install system packages (python3, nginx, openssl, iperf3, mtr-tiny, traceroute, psmisc)
- Create the `nettest` service user and `/opt/nettest` directory
- Sync all project files to `/opt/nettest`
- Set up a Python virtual environment and install dependencies
- Generate an SSH key pair at `/opt/nettest/.ssh/nettest_key`
- Configure nginx as a reverse proxy with a self-signed TLS certificate
- Install and enable `nettest.service` and `nettest-web.service`

After install:
1. Edit the config: `sudo nano /opt/nettest/config/config.yaml`
2. Start services: `sudo systemctl start nettest nettest-web`
3. Open the dashboard:
   - `http://<server-ip>:8080` — direct (always available)
   - `https://<server-ip>` — HTTPS via nginx (self-signed cert)

### Upgrade

```bash
sudo ./install.sh --upgrade
```

Syncs code files, updates Python dependencies, and restarts services.
Existing config, SSH keys, and results are preserved.

### Show Controller Public Key

```bash
sudo ./install.sh --show-key
```

---

## Configuration

The config file lives at `/opt/nettest/config/config.yaml` (ignored by git).
A sanitized example is at `config/config.example.yaml`.

Key sections:

```yaml
controller:
  name: "NetTest Controller"
  results_dir: results
  log_dir: logs

ssh_defaults:
  username: nettest
  key_file: /opt/nettest/.ssh/nettest_key
  port: 22
  timeout: 30

agents:
  - id: branch_a
    label: "Branch A"
    host_mgmt_ip: 10.1.1.10     # SSH management IP
    host_test_ip: 10.2.1.10     # Test traffic IP (falls back to host_mgmt_ip)
    type: endpoint               # endpoint | svi_adjacent

paths:
  - id: branch_a_to_hub
    label: "Branch A to Hub"
    source: branch_a
    destination: hub
    hops: []                     # Optional intermediate agent IDs
    tests:
      - latency
      - throughput
      - jitter
      - mtu
      - traceroute
      - latency_under_load

schedule:
  full_test_interval_minutes: 60
  latency_only_interval_minutes: 15
  stagger_seconds: 30
```

Most settings can be edited live via the web UI at **Config → [section]** without
manually editing YAML. The scheduler restarts automatically on config save.

---

## Firewall Requirements

### Controller Host (inbound)

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 8080 | TCP | Inbound | NetTest web dashboard (direct) |
| 443  | TCP | Inbound | NetTest web dashboard (HTTPS via nginx) |
| 80   | TCP | Inbound | HTTP → HTTPS redirect (nginx) |
| 22   | TCP | Inbound | SSH management (optional, for admin access) |

### Agent Hosts (inbound)

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 22   | TCP | From controller | SSH — controller connects here for all test execution |
| 5201 | TCP | From controller source agent | iPerf3 throughput and latency-under-load tests |
| 5201 | UDP | From controller source agent | iPerf3 jitter tests |

### Between Agents (test traffic)

| Protocol | Direction | Purpose |
|----------|-----------|---------|
| ICMP | Bidirectional | Ping latency, MTU discovery |
| UDP 5201 | Source → Destination | iPerf3 jitter |
| TCP 5201 | Source → Destination | iPerf3 throughput, latency-under-load |
| UDP (traceroute) | Source → Destination | Traceroute forward path |
| ICMP (TTL exceeded) | Routers → Source | Traceroute hop responses |

### Notes
- The controller SSHes into **both** endpoints to run tests — port 22 must be
  reachable from the controller to every agent, not just between agents.
- `svi_adjacent` agents (routers, SVIs) are **ping targets only** — the controller
  never SSHes into them. Only ICMP needs to be open from the source agent.
- iPerf3 always runs server-side on the **destination** agent. The source agent
  connects outbound to port 5201 on the destination.
- MTU discovery uses ICMP with the DF bit set. ICMP must not be filtered or
  rate-limited between agents or MTU probes will all fail.
- Traceroute uses UDP by default (Linux `traceroute`). Some networks block UDP;
  if traceroute shows all `* * *` hops, check UDP filtering between sites.

---

## Web Dashboard

Access at `http://<controller-ip>:8080` or `https://<controller-ip>`.

### Features
- **Live test output** — streaming log panel shows test progress in real time
- **Results table** — per-path results with latency, throughput, jitter, MTU, loss
- **Trend charts** — latency, throughput, jitter, bufferbloat over time
- **Traceroute visualization** — forward and reverse hop flow with RTT per hop
- **Hop segment breakdown** — RTT and MTU per defined intermediate hop
- **MTR hop detail** — per-hop loss and latency under load
- **Config editor** — edit agents, paths, schedule, SSH, tests, packages, HTTPS

### Config Editor Tabs

| Tab | Purpose |
|-----|---------|
| Agents | Add, edit, delete agents; onboard new agents via SSH wizard |
| Paths | Define test paths, source/destination, intermediate hops, test types |
| Schedule | Full test and latency-only intervals, business hours, stagger |
| SSH | SSH defaults, key management, import/export keys, push key to agents |
| Tests | Per-test parameters (packet counts, duration, MTU range, etc.) |
| Packages | Stage .deb files for air-gapped onboarding; push to agents |
| HTTPS | nginx status, certificate info, generate/upload certificates |
| Export/Import | Export full or partial config as .tar.gz; import on another controller |

---

## Agent Types

### `endpoint`
Full SSH access. Supports all tests: latency, throughput, jitter, MTU, traceroute,
latency-under-load. The controller SSHes in to run iPerf3 and other tools.

### `svi_adjacent`
No SSH access — ping target only. Represents a router SVI, gateway, or external
host (e.g. Google DNS). Supports: latency, MTU, traceroute. The controller pings
this IP from the source agent without connecting to it directly.

---

## Multi-Hop Paths

Paths can include intermediate hops (e.g. routers or firewalls between sites):

```yaml
paths:
  - id: branch_to_dc_via_fw
    label: "Branch to DC via Firewall"
    source: branch_agent
    hops:
      - firewall_svi      # svi_adjacent agent
      - dc_router_svi     # svi_adjacent agent
    destination: dc_agent
    tests: [latency, mtu, throughput, traceroute]
```

For multi-hop paths, the controller runs latency and MTU tests from the source
to each intermediate hop before running the full test suite to the destination.
Results show per-segment RTT and MTU so you can pinpoint where degradation occurs.

---

## Agent Onboarding

### Internet-Connected Agents

Use the web wizard at **Config → Agents → Onboard New Agent**:
1. Enter agent IP, label, admin credentials
2. The controller SSHes in, installs packages via apt, creates the `nettest` user,
   deploys the SSH key, and configures sudoers
3. The agent is added to config.yaml automatically

### Air-Gapped Agents

For agents with no internet access:

1. On an internet-connected machine with the same Ubuntu version as your agents,
   download the required packages:
   ```bash
   apt-get download iperf3 libiperf0 libsctp1 mtr-tiny iputils-ping traceroute psmisc
   ```

2. Upload the `.deb` files via **Config → Packages** in the web UI

3. Check **Air-gapped** in the onboard wizard — the controller will SCP the staged
   packages to the agent and install them with `dpkg` instead of using apt

Required packages for air-gapped install:
- `iperf3` + `libiperf0` + `libsctp1` (iPerf3 and its dependencies)
- `mtr-tiny` (MTR hop tracing)
- `iputils-ping` (ping/MTU)
- `traceroute` (traceroute)
- `psmisc` (provides `fuser`, used for port cleanup)

---

## HTTPS Setup

HTTPS is configured automatically during install (self-signed certificate).
To manage certificates via the web UI: **Config → HTTPS**.

- **Generate** — creates a new self-signed cert for your server IP
- **Upload** — install a cert from your internal CA
- **Start/Stop nginx** — toggle the nginx reverse proxy

Port 8080 remains accessible directly at all times regardless of nginx status.

To regenerate the certificate for a different IP after install:
```bash
sudo bash ./install.sh --setup-https
```

---

## SSH Key Management

The controller uses a single ED25519 key pair to authenticate to all agents.
Manage keys at **Config → SSH**:

- **Export** — download the public or private key
- **Import** — replace the key pair (warning: breaks access to all agents until
  the new public key is pushed to them)
- **Push Public Key** — deploy the current public key to one or all agents using
  admin credentials (use after importing a new key)

---

## Export / Import

Transfer configuration between controllers at **Config → Export/Import**.

**Export options:**
- Agents & Paths (always included)
- Controller name/dirs
- Schedule settings
- Test parameters
- SSH credentials
- SSH keys (private + public) — treat as sensitive
- Auth / RADIUS config — treat as sensitive

**Import modes:**
- **Merge** — adds new agents/paths, skips existing IDs
- **Replace** — overwrites all agents/paths with the imported set

The export bundle is a `.tar.gz` containing `config.yaml` and optionally the
SSH key files. A `README.txt` inside notes what sensitive data is included.

---

## Project Structure

```
nettest/
├── main.py                      # Entry point and CLI
├── web_dashboard.py             # Flask web dashboard + REST API
├── onboard.py                   # Agent onboarding logic
├── requirements.txt
├── install.sh                   # Install / upgrade script
├── config/
│   ├── config.example.yaml      # Sanitized starter config (committed to git)
│   └── config.yaml              # Live config with secrets (gitignored)
├── core/
│   ├── config_loader.py         # Loads and validates config.yaml
│   ├── results.py               # Result dataclasses + JSONL store
│   ├── ssh_manager.py           # Netmiko SSH wrapper with retry logic
│   ├── path_tester.py           # Orchestrates tests for one path
│   └── scheduler.py             # Drives periodic test execution
├── runners/
│   ├── runner_throughput.py     # iPerf3 TCP throughput
│   ├── runner_latency.py        # Ping, jitter, MTU, latency-under-load
│   └── runner_traceroute.py     # Traceroute (forward + reverse)
├── web/
│   ├── index.html               # Main dashboard
│   ├── config.html              # Config editor
│   └── login.html               # Login page
├── systemd/
│   ├── nettest.service          # Scheduler service
│   └── nettest-web.service      # Web dashboard service (gunicorn + gevent)
├── results/                     # Auto-created — JSONL result files per day
├── logs/                        # Auto-created — controller.log
└── packages/                    # Staged .deb files for air-gapped onboarding
```

---

## Result Files

Results are stored as JSONL (one JSON record per line) in `results/`:
```
results/results_2026-05-16.jsonl
```

Each record contains the full result for one path test run including all
sub-test results (latency, throughput, jitter, MTU, traceroute, segments).

---

## InfluxDB Integration (Future)

The scheduler supports result callbacks. To add InfluxDB:

1. Uncomment `influxdb-client` in `requirements.txt`
2. Add InfluxDB connection details to `config.yaml`
3. Create `core/influx_writer.py`:
   ```python
   class InfluxWriter:
       def __init__(self, config): ...
       def write_result(self, result: PathTestResult): ...
   ```
4. Register in `main.py`:
   ```python
   from core.influx_writer import InfluxWriter
   scheduler.add_result_callback(InfluxWriter(config).write_result)
   ```

No other changes needed — the callback system is already wired.

---

## Troubleshooting

### Services won't start
```bash
sudo journalctl -u nettest -n 50 --no-pager
sudo journalctl -u nettest-web -n 50 --no-pager
```

### iPerf3 fails with "control socket closed"
A zombie iPerf3 daemon is holding port 5201. The runner automatically kills it
with `fuser -k 5201/tcp` before each test. If it persists, on the agent:
```bash
sudo fuser -k 5201/tcp; sudo fuser -k 5201/udp
```

### MTU probes all failing (576 bytes result)
ICMP is being filtered or rate-limited between agents. Check firewall rules on
intermediate devices. MTU discovery requires ICMP with the DF bit to pass.

### Traceroute shows all `* * *`
UDP traceroute is being blocked. Check that UDP is permitted between sites, or
test with ICMP traceroute manually: `traceroute -I <dest>`.

### SSH connection refused on agent
Verify the `nettest` user exists, the SSH key is in `authorized_keys`, and
port 22 is reachable from the controller:
```bash
ssh -i /opt/nettest/.ssh/nettest_key nettest@<agent-ip>
```

### Config changes not reflected after import
Restart both services:
```bash
sudo systemctl restart nettest nettest-web
```

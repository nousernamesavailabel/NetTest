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

## Setup

### 1. Clone and install dependencies
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

### 2. Configure SSH key access
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

### 3. Create the nettest user on each agent
```bash
# Run on each agent host
sudo useradd -m -s /bin/bash nettest
sudo mkdir -p /home/nettest/.ssh
# Paste controller's public key into /home/nettest/.ssh/authorized_keys
sudo chown -R nettest:nettest /home/nettest/.ssh
sudo chmod 700 /home/nettest/.ssh
sudo chmod 600 /home/nettest/.ssh/authorized_keys
```

### 4. Open firewall ports on agents
```bash
# iPerf3 (TCP + UDP)
sudo ufw allow 5201/tcp
sudo ufw allow 5201/udp

# SSH (already open if you're connected)
sudo ufw allow 22/tcp
```

### 5. Edit config/config.yaml
- Update agent IPs to match your environment
- Define your actual test paths
- Adjust schedule intervals as needed

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

---

## Project Structure

```
nettest/
├── main.py                   # Entry point and CLI
├── requirements.txt
├── config/
│   ├── config.example.yaml   # Sanitized starter config committed to git
│   └── config.yaml           # Local/private config ignored by git
├── core/
│   ├── config_loader.py      # Loads and validates config.yaml
│   ├── results.py            # Result dataclasses + local JSON store
│   ├── ssh_manager.py        # Netmiko SSH wrapper with retry logic
│   ├── path_tester.py        # Orchestrates tests for one path
│   └── scheduler.py          # Drives periodic test execution
├── runners/
│   ├── runner_throughput.py  # iPerf3 TCP/UDP throughput
│   └── runner_latency.py     # Ping latency, UDP jitter, MTU discovery,
│                             # and latency-under-load (bufferbloat)
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

```ini
# /etc/systemd/system/nettest.service
[Unit]
Description=Network Test Controller
After=network.target

[Service]
Type=simple
User=nettest
WorkingDirectory=/opt/nettest
ExecStart=/opt/nettest/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable nettest
sudo systemctl start nettest
sudo journalctl -u nettest -f    # Follow logs
```

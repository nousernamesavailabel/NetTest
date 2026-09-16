"""
config_loader.py
Loads and validates config.yaml, exposes typed dataclasses
for use throughout the controller.
"""

import yaml
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict

# Day-of-week codes used as keys in TestPath.schedule, indexed by datetime.weekday()
DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SLOTS_PER_DAY = 48  # 30-minute increments


# ── Dataclasses ────────────────────────────────────────────

@dataclass
class SSHDefaults:
    username: str
    password: str
    key_file: str
    port: int
    timeout: int


# Valid agent types
AGENT_TYPES = {
    "agent_installed":     "Agent Installed",    # Full SSH — all tests
    "switch_router":       "Switch/Router",       # Ping target only — latency/MTU/traceroute
    "agent_not_installed": "Agent Not Installed", # iPerf3 assumed running — no SSH
    # Legacy names — auto-renamed on load
    "endpoint":            "agent_installed",
    "svi_adjacent":        "switch_router",
}

# Tests that don't require SSH on the destination
NO_SSH_TESTS = {"latency", "mtu", "traceroute", "throughput", "jitter", "latency_under_load"}

# Tests that switch_router destinations support (ping only)
SWITCH_ROUTER_TESTS = {"latency", "mtu", "traceroute"}

# Tests that agent_not_installed destinations support (no server mgmt, iPerf assumed running)
AGENT_NOT_INSTALLED_TESTS = {"latency", "mtu", "traceroute", "throughput", "jitter", "latency_under_load"}


@dataclass
class Agent:
    id: str
    label: str
    host_mgmt_ip: str                  # SSH management IP — controller connects here
    type: str                          # agent_installed | switch_router | agent_not_installed
    host_test_ip: Optional[str] = None # Test traffic IP — falls back to host_mgmt_ip
    iperf3_port: Optional[int] = None  # For agent_not_installed — port iPerf3 listens on
    # Per-agent SSH overrides (fall back to SSHDefaults if None)
    username: Optional[str] = None
    password: Optional[str] = None
    key_file: Optional[str] = None
    port: Optional[int] = None

    @property
    def host(self) -> str:
        return self.host_mgmt_ip

    @property
    def test_host(self) -> str:
        return self.host_test_ip or self.host_mgmt_ip

    @property
    def has_ssh(self) -> bool:
        return self.type == "agent_installed"

    @property
    def has_iperf3_server(self) -> bool:
        """True if we manage the iPerf3 server (agent_installed)."""
        return self.type == "agent_installed"

    @property
    def is_ping_only(self) -> bool:
        return self.type == "switch_router"

THROUGHPUT_DIRECTIONS = {"upload", "download", "bidir"}


@dataclass
class TestPath:
    id: str
    label: str
    source: str                        # Agent ID
    destination: str                   # Agent ID
    tests: List[str]                   # e.g. [throughput, latency, jitter]
    hops: List[str] = field(default_factory=list)  # Intermediate agent IDs
    # Per-day 48-char '0'/'1' strings (30-min slots, index 0 = 00:00).
    # Missing entirely, or missing a given day, means "always active".
    schedule: Optional[Dict[str, str]] = None
    # Throughput directions to run for this path — any combination of
    # upload | download | bidir. Each runs as its own iPerf3 invocation.
    # Duration/streams/protocol stay global (test_params.throughput).
    directions: List[str] = field(default_factory=lambda: ["upload"])

    @property
    def all_agents(self) -> List[str]:
        """All agent IDs in order: source → hops → destination."""
        return [self.source] + self.hops + [self.destination]

    @property
    def is_multihop(self) -> bool:
        return len(self.hops) > 0

    def is_active_at(self, dt: datetime) -> bool:
        """Whether this path's per-day/time grid allows a run at dt (dt's own tzinfo, if any, is used as-is)."""
        if not self.schedule:
            return True
        mask = self.schedule.get(DAY_CODES[dt.weekday()])
        if not mask:
            return True
        slot = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
        if slot < 0 or slot >= len(mask):
            return True
        return mask[slot] == "1"


@dataclass
class ThroughputParams:
    duration_sec: int
    parallel_streams: int
    protocol: str
    iperf3_port: int


@dataclass
class LatencyParams:
    packet_count: int
    interval_ms: int
    packet_size_bytes: int


@dataclass
class LatencyUnderLoadParams:
    ping_count: int
    ping_interval_ms: int
    mtr_cycles: int


@dataclass
class JitterParams:
    packet_count: int
    interval_ms: int
    packet_size_bytes: int
    iperf3_port: int
    protocol: str
    bandwidth_kbps: int


@dataclass
class MTUParams:
    max_size: int
    min_size: int
    step: int


@dataclass
class TracerouteParams:
    max_hops:    int  = 30
    probes:      int  = 2
    wait_sec:    int  = 1
    resolve_dns: bool = True


@dataclass
class TestParams:
    throughput: ThroughputParams
    latency: LatencyParams
    latency_under_load: LatencyUnderLoadParams
    jitter: JitterParams
    mtu: MTUParams
    traceroute: TracerouteParams = field(default_factory=TracerouteParams)
    iperf3_busy_retry_seconds: int = 10  # for agent_not_installed: retry if server busy


@dataclass
class Schedule:
    full_test_interval_minutes: int
    latency_only_interval_minutes: int
    business_hours_only: bool
    business_hours_start: str
    business_hours_end: str
    timezone: str
    stagger_seconds: int


@dataclass
class AuthConfig:
    enabled: bool = False
    radius_server: str = ""
    radius_port: int = 1812
    radius_secret: str = ""
    radius_timeout: int = 5
    session_secret: str = ""
    session_lifetime_minutes: int = 480
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    login_lockout_seconds: int = 900
    cookie_secure: bool = False


@dataclass
class ControllerConfig:
    name: str
    results_dir: str
    log_dir: str
    log_level: str
    ssh_defaults: SSHDefaults
    agents: List[Agent]
    paths: List[TestPath]
    test_params: TestParams
    schedule: Schedule
    auth: AuthConfig = field(default_factory=AuthConfig)

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        return next((a for a in self.agents if a.id == agent_id), None)

    def get_ssh_params(self, agent: Agent) -> dict:
        """Returns merged SSH params for an agent (agent overrides > defaults)."""
        return {
            "host":     agent.host_mgmt_ip,
            "username": agent.username or self.ssh_defaults.username,
            "password": agent.password or self.ssh_defaults.password,
            "key_file": agent.key_file or self.ssh_defaults.key_file,
            "port":     agent.port     or self.ssh_defaults.port,
            "timeout":  self.ssh_defaults.timeout,
        }


# ── Loader ─────────────────────────────────────────────────

def load_config(config_path: str = "config/config.yaml") -> ControllerConfig:
    config_path = os.path.expanduser(config_path)
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    ssh_defaults = SSHDefaults(**raw["ssh_defaults"])

    agents = []
    for a in raw["agents"]:
        agent_data = dict(a)
        if "host" in agent_data and "host_mgmt_ip" not in agent_data:
            agent_data["host_mgmt_ip"] = agent_data.pop("host")
        # Backward compat: rename legacy type names
        if agent_data.get("type") == "endpoint":
            agent_data["type"] = "agent_installed"
        elif agent_data.get("type") == "svi_adjacent":
            agent_data["type"] = "switch_router"
        agents.append(Agent(**agent_data))

    # Back-compat: a config saved before per-path direction existed only had
    # a single global bidirectional flag — use it as the default direction
    # for paths that don't specify their own.
    legacy_bidir = bool(raw.get("test_params", {}).get("throughput", {}).get("bidirectional"))
    default_directions = ["bidir"] if legacy_bidir else ["upload"]

    def _path_directions(p: dict) -> List[str]:
        # New format: directions (list). Old format: direction (single string).
        if p.get("directions"):
            valid = [d for d in p["directions"] if d in THROUGHPUT_DIRECTIONS]
            if valid:
                return valid
        if p.get("direction") in THROUGHPUT_DIRECTIONS:
            return [p["direction"]]
        return list(default_directions)

    paths = [
        TestPath(
            id=p["id"],
            label=p["label"],
            source=p["source"],
            destination=p["destination"],
            tests=p["tests"],
            hops=p.get("hops", []),
            schedule=p.get("schedule"),
            directions=_path_directions(p),
        )
        for p in raw["paths"]
    ]

    tp = raw["test_params"]
    throughput_kwargs = {k: v for k, v in tp["throughput"].items() if k != "bidirectional"}
    test_params = TestParams(
        throughput=ThroughputParams(**throughput_kwargs),
        latency=LatencyParams(**tp["latency"]),
        latency_under_load=LatencyUnderLoadParams(**tp["latency_under_load"]),
        jitter=JitterParams(**tp["jitter"]),
        mtu=MTUParams(**tp["mtu"]),
        traceroute=TracerouteParams(**(tp.get("traceroute") or {})),
    )

    schedule = Schedule(**raw["schedule"])
    auth = AuthConfig(**(raw.get("auth") or {}))

    ctrl = raw["controller"]
    return ControllerConfig(
        name=ctrl["name"],
        results_dir=ctrl["results_dir"],
        log_dir=ctrl["log_dir"],
        log_level=ctrl["log_level"],
        ssh_defaults=ssh_defaults,
        agents=agents,
        paths=paths,
        test_params=test_params,
        schedule=schedule,
        auth=auth,
    )

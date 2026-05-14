"""
config_loader.py
Loads and validates config.yaml, exposes typed dataclasses
for use throughout the controller.
"""

import yaml
import os
from dataclasses import dataclass, field
from typing import List, Optional


# ── Dataclasses ────────────────────────────────────────────

@dataclass
class SSHDefaults:
    username: str
    password: str
    key_file: str
    port: int
    timeout: int


@dataclass
class Agent:
    id: str
    label: str
    host_mgmt_ip: str                  # SSH management IP — controller connects here
    type: str                          # endpoint | svi_adjacent
    host_test_ip: Optional[str] = None # Test traffic IP — falls back to host_mgmt_ip
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

@dataclass
class TestPath:
    id: str
    label: str
    source: str                        # Agent ID
    destination: str                   # Agent ID
    tests: List[str]                   # e.g. [throughput, latency, jitter]
    hops: List[str] = field(default_factory=list)  # Intermediate agent IDs

    @property
    def all_agents(self) -> List[str]:
        """All agent IDs in order: source → hops → destination."""
        return [self.source] + self.hops + [self.destination]

    @property
    def is_multihop(self) -> bool:
        return len(self.hops) > 0


@dataclass
class ThroughputParams:
    duration_sec: int
    parallel_streams: int
    bidirectional: bool
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
        agents.append(Agent(**agent_data))

    paths = [
        TestPath(
            id=p["id"],
            label=p["label"],
            source=p["source"],
            destination=p["destination"],
            tests=p["tests"],
            hops=p.get("hops", []),
        )
        for p in raw["paths"]
    ]

    tp = raw["test_params"]
    test_params = TestParams(
        throughput=ThroughputParams(**tp["throughput"]),
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

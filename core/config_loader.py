"""
config_loader.py
Loads and validates config.yaml, exposes typed dataclasses
for use throughout the controller.
"""

import logging
import re
import yaml
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict

from core.hostaddr import validate_host, ADDRESS_FAMILIES, RESOLVE_ON

logger = logging.getLogger(__name__)

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
    "web_server":          "Web Server",          # No SSH — latency via TCP handshake if ICMP is filtered
    # Legacy names — auto-renamed on load
    "endpoint":            "agent_installed",
    "svi_adjacent":        "switch_router",
}

# Tests that don't require SSH on the destination
NO_SSH_TESTS = {"latency", "mtu", "traceroute", "throughput", "jitter", "latency_under_load"}

# Tests that switch_router destinations support (ping only)
SWITCH_ROUTER_TESTS = {"latency", "mtu", "traceroute"}

# Tests that web_server destinations support. No MTU: path-MTU probing needs
# ICMP replies, which a web server may filter.
WEB_SERVER_TESTS = {"latency", "traceroute"}

# How latency is measured for a web_server: auto = ICMP if the host answers
# pings, otherwise TCP handshake timing to tcp_port.
LATENCY_METHODS = ("auto", "tcp", "icmp")

# Agent types with no SSH access — destination only, never a source or hop.
NO_SSH_TYPES = {"switch_router", "agent_not_installed", "web_server"}

# Tests that agent_not_installed destinations support (no server mgmt, iPerf assumed running)
AGENT_NOT_INSTALLED_TESTS = {"latency", "mtu", "traceroute", "throughput", "jitter", "latency_under_load"}


@dataclass
class Agent:
    id: str
    label: str
    # Field names keep "_ip" for config compatibility, but both accept an
    # IPv4/IPv6 address or a DNS hostname/FQDN (see core/hostaddr.py).
    host_mgmt_ip: str                  # SSH management host — controller connects here
    type: str                          # agent_installed | switch_router | agent_not_installed | web_server
    host_test_ip: Optional[str] = None # Test traffic host — falls back to host_mgmt_ip
    iperf3_port: Optional[int] = None  # For agent_not_installed — port iPerf3 listens on
    # Which address family a hostname resolves to, and pins the test tools to
    # when resolve_on is "agent": ipv4 | ipv6 | auto. Ignored for IP literals.
    address_family: str = "ipv4"
    # Where hostnames are resolved for test traffic: "controller" (default —
    # agents get an IP, no DNS needed on them) or "agent" (the source agent's
    # own resolver, for split-horizon DNS).
    resolve_on: str = "controller"
    # web_server only: the TCP port latency/traceroute probes target, and how
    # latency is measured (see LATENCY_METHODS).
    tcp_port: Optional[int] = None
    latency_method: str = "auto"
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

    @property
    def probe_port(self) -> int:
        """TCP port for web_server latency/traceroute probes."""
        return self.tcp_port or 443

THROUGHPUT_DIRECTIONS = {"upload", "download", "bidir"}


@dataclass
class TestPath:
    id: str
    label: str
    source: str                        # Agent ID
    destination: str                   # Agent ID
    tests: List[str]                   # e.g. [throughput, latency, jitter]
    hops: List[str] = field(default_factory=list)  # Intermediate agent IDs
    # Free-text label used to cluster paths in the dashboard sidebar and the
    # config editor. Empty/None means ungrouped. Display order of groups
    # follows the order paths appear in config.yaml, not alphabetical.
    group: Optional[str] = None
    # Per-day 48-char '0'/'1' strings (30-min slots, index 0 = 00:00).
    # Missing entirely, or missing a given day, means "always active".
    schedule: Optional[Dict[str, str]] = None
    # Throughput directions to run for this path — any combination of
    # upload | download | bidir. Each runs as its own iPerf3 invocation.
    # Duration/protocol stay global (test_params.throughput).
    directions: List[str] = field(default_factory=lambda: ["upload"])
    # Per-path iPerf3 parallel stream count — also used as the saturation
    # stream count for this path's Latency Under Load test. Per-path because
    # the right number depends on the path's own bandwidth/shaping: too many
    # streams on a constrained or policed link causes congestion collapse
    # (retransmit storms, stalled streams) rather than a useful measurement.
    parallel_streams: int = 8

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
    protocol: str
    iperf3_port: int


@dataclass
class LatencyParams:
    packet_count: int
    interval_ms: int
    packet_size_bytes: int
    # TCP handshake probes for web_server latency. mtr sends at most one
    # probe per second without root, so this is kept separate from (and
    # much smaller than) packet_count.
    tcp_probe_count: int = 20


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
    # False pauses all automatic runs (both tiers). Manual runs still work.
    # Missing from config.yaml means enabled, so older configs keep running.
    enabled: bool = True


# Green/amber/red cut-offs used by the web dashboard and TUI. For most
# metrics higher is worse: green below warn, amber from warn, red from crit.
# Throughput is the other way round: amber at or below warn, red at or
# below crit. Missing metrics in config.yaml fall back to these defaults.
THRESHOLD_DEFAULTS: Dict[str, Dict[str, float]] = {
    "latency_ms":      {"warn": 15, "crit": 50},
    "packet_loss_pct": {"warn": 1,  "crit": 5},
    "jitter_ms":       {"warn": 5,  "crit": 20},
    "throughput_mbps": {"warn": 10, "crit": 2},
    "bufferbloat_ms":  {"warn": 30, "crit": 100},
}
HIGHER_IS_BETTER = {"throughput_mbps"}


def parse_thresholds(raw) -> "tuple[Dict[str, Dict[str, float]], List[str]]":
    """Merge thresholds from config.yaml over THRESHOLD_DEFAULTS.

    Returns (thresholds, errors). A metric with a bad value keeps its default
    and adds an error, so a hand-edited typo can't take the dashboard down;
    saves from the web UI reject on any error instead.
    """
    out = {k: dict(v) for k, v in THRESHOLD_DEFAULTS.items()}
    errors: List[str] = []
    if raw is None:
        return out, errors
    if not isinstance(raw, dict):
        return out, ["thresholds must be a mapping"]
    for metric, val in raw.items():
        if metric not in THRESHOLD_DEFAULTS:
            errors.append(f"thresholds: unknown metric '{metric}'")
            continue
        if not isinstance(val, dict):
            errors.append(f"thresholds.{metric} must have warn and crit values")
            continue
        merged = dict(THRESHOLD_DEFAULTS[metric])
        bad = False
        for level in ("warn", "crit"):
            if level not in val or val[level] in (None, ""):
                continue
            try:
                num = float(val[level])
            except (TypeError, ValueError):
                errors.append(f"thresholds.{metric}.{level} must be a number")
                bad = True
                continue
            if num < 0:
                errors.append(f"thresholds.{metric}.{level} can't be negative")
                bad = True
                continue
            merged[level] = int(num) if num.is_integer() else num
        if bad:
            continue
        if metric in HIGHER_IS_BETTER:
            if merged["crit"] >= merged["warn"]:
                errors.append(f"thresholds.{metric}: crit must be lower than warn "
                              f"(higher is better)")
                continue
        elif merged["crit"] <= merged["warn"]:
            errors.append(f"thresholds.{metric}: crit must be higher than warn")
            continue
        out[metric] = merged
    return out, errors


# ── Branding ───────────────────────────────────────────────
# Per-install look and wording (Config → Branding). Logo and favicon images
# aren't in config.yaml — they're files in config/branding/ (see
# core/branding.py), so a bad upload can't break config parsing.

BRANDING_LIMITS = {          # max characters per text field
    "org_name":      80,
    "report_footer": 200,
    "login_banner":  4000,
}
_HEX_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class Branding:
    org_name: str = ""            # shown on login page and reports
    accent_color: str = ""        # "#rrggbb"; blank = default cyan
    login_banner: str = ""        # notice shown above the login form
    login_banner_ack: bool = False  # require a tick-box before logging in
    report_footer: str = ""       # footer line on executive summaries


def parse_branding(raw) -> "tuple[Branding, List[str]]":
    """Build Branding from config.yaml's branding block.

    Returns (branding, errors). Like parse_thresholds, a bad field keeps its
    default and adds an error; web UI saves reject on any error instead.
    """
    out = Branding()
    errors: List[str] = []
    if raw is None:
        return out, errors
    if not isinstance(raw, dict):
        return out, ["branding must be a mapping"]
    for key, val in raw.items():
        if key in BRANDING_LIMITS:
            text = "" if val is None else str(val)
            # Banner keeps its line breaks; single-line fields don't.
            text = text.strip() if key == "login_banner" else " ".join(text.split())
            if len(text) > BRANDING_LIMITS[key]:
                errors.append(f"branding.{key} is longer than "
                              f"{BRANDING_LIMITS[key]} characters")
                continue
            setattr(out, key, text)
        elif key == "accent_color":
            text = str(val or "").strip()
            if text and not _HEX_COLOUR.match(text):
                errors.append("branding.accent_color must be a colour like #06b6d4")
                continue
            out.accent_color = text.lower()
        elif key == "login_banner_ack":
            if not isinstance(val, bool):
                errors.append("branding.login_banner_ack must be true or false")
                continue
            out.login_banner_ack = val
        else:
            errors.append(f"branding: unknown setting '{key}'")
    return out, errors


# Account types. "admin" can do everything; "readonly" can view results but
# can't run tests, change config, or manage the controller.
ROLE_ADMIN    = "admin"
ROLE_READONLY = "readonly"
ROLES = (ROLE_ADMIN, ROLE_READONLY)

# RADIUS reply attributes that can carry the account type (see AuthConfig).
RADIUS_ROLE_ATTRIBUTES = ("Class", "Filter-Id")


def normalize_role(value, default: str = ROLE_ADMIN) -> str:
    role = str(value or "").strip().lower().replace("-", "").replace("_", "")
    if role == "admin":
        return ROLE_ADMIN
    if role in ("readonly", "ro", "viewer"):
        return ROLE_READONLY
    return default


@dataclass
class LocalUser:
    username: str
    password_hash: str  # werkzeug generate_password_hash() output — never plaintext
    role: str = ROLE_ADMIN   # accounts from before roles existed were all admins


@dataclass
class AuthConfig:
    enabled: bool = False
    # Effective method: "" (disabled) | "radius" | "local". In config.yaml,
    # "none" disables explicitly; blank is inferred from radius_server /
    # local_users for backward compatibility.
    method: str = ""
    radius_server: str = ""
    radius_port: int = 1812
    radius_secret: str = ""
    radius_timeout: int = 5
    # Account type for RADIUS logins, read from a reply attribute of the
    # Access-Accept. A value matching radius_admin_value / radius_readonly_value
    # (case-insensitive) picks that type; anything else, including no attribute
    # at all, gets radius_default_role.
    radius_role_attribute: str = "Class"
    radius_admin_value: str = "linkstate-admin"
    radius_readonly_value: str = "linkstate-readonly"
    radius_default_role: str = ROLE_ADMIN   # admin keeps pre-role behaviour
    local_users: List[LocalUser] = field(default_factory=list)
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
    thresholds: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in THRESHOLD_DEFAULTS.items()})
    branding: Branding = field(default_factory=Branding)

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


# ── Validation ─────────────────────────────────────────────

def agent_host_errors(a: dict) -> List[str]:
    """Problems with an agent dict's host fields (empty list if none)."""
    errs = []
    name = a.get("id") or a.get("label") or "?"
    mgmt = (a.get("host_mgmt_ip") or a.get("host") or "").strip()
    if not mgmt:
        errs.append(f"agent '{name}': management host is required")
    else:
        e = validate_host(mgmt)
        if e:
            errs.append(f"agent '{name}': management host {e}")
    test = (a.get("host_test_ip") or "").strip()
    if test:
        e = validate_host(test)
        if e:
            errs.append(f"agent '{name}': test host {e}")
    af = a.get("address_family")
    if af not in (None, "") and af not in ADDRESS_FAMILIES:
        errs.append(f"agent '{name}': address_family must be one of {', '.join(ADDRESS_FAMILIES)}")
    ro = a.get("resolve_on")
    if ro not in (None, "") and ro not in RESOLVE_ON:
        errs.append(f"agent '{name}': resolve_on must be one of {', '.join(RESOLVE_ON)}")
    tp = a.get("tcp_port")
    if tp not in (None, ""):
        try:
            ok = 1 <= int(tp) <= 65535
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errs.append(f"agent '{name}': tcp_port must be a number from 1 to 65535")
    lm = a.get("latency_method")
    if lm not in (None, "") and lm not in LATENCY_METHODS:
        errs.append(f"agent '{name}': latency_method must be one of {', '.join(LATENCY_METHODS)}")
    return errs


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
        for k in ("host_mgmt_ip", "host_test_ip"):
            if isinstance(agent_data.get(k), str):
                agent_data[k] = agent_data[k].strip() or None
        if agent_data.get("address_family") not in ADDRESS_FAMILIES:
            agent_data["address_family"] = "ipv4"
        if agent_data.get("resolve_on") not in RESOLVE_ON:
            agent_data["resolve_on"] = "controller"
        if agent_data.get("latency_method") not in LATENCY_METHODS:
            agent_data["latency_method"] = "auto"
        try:
            tp = int(agent_data.get("tcp_port") or 0)
            agent_data["tcp_port"] = tp if 1 <= tp <= 65535 else None
        except (TypeError, ValueError):
            agent_data["tcp_port"] = None
        agent = Agent(**agent_data)
        # Don't refuse to start over a bad host (the dashboard must stay up so
        # it can be fixed) — the path run reports it, and saves reject it.
        for err in agent_host_errors(agent_data):
            logger.warning(f"config: {err}")
        agents.append(agent)

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

    # Back-compat: parallel_streams used to be a single global value under
    # test_params.throughput. A config saved before per-path streams existed
    # has no per-path value at all — fall back to that old global (or 8) so
    # existing paths keep behaving exactly as before until edited.
    legacy_streams = raw.get("test_params", {}).get("throughput", {}).get("parallel_streams", 8)

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
            parallel_streams=int(p.get("parallel_streams") or legacy_streams),
            group=(p.get("group") or None),
        )
        for p in raw["paths"]
    ]

    tp = raw["test_params"]
    throughput_kwargs = {k: v for k, v in tp["throughput"].items()
                          if k not in ("bidirectional", "parallel_streams")}
    test_params = TestParams(
        throughput=ThroughputParams(**throughput_kwargs),
        latency=LatencyParams(**tp["latency"]),
        latency_under_load=LatencyUnderLoadParams(**tp["latency_under_load"]),
        jitter=JitterParams(**tp["jitter"]),
        mtu=MTUParams(**tp["mtu"]),
        traceroute=TracerouteParams(**(tp.get("traceroute") or {})),
    )

    schedule = Schedule(**raw["schedule"])

    raw_auth = dict(raw.get("auth") or {})
    raw_local_users = raw_auth.pop("local_users", None) or []
    local_users = [
        LocalUser(username=u["username"], password_hash=u["password_hash"],
                  role=normalize_role(u.get("role")))
        for u in raw_local_users
    ]
    if "radius_default_role" in raw_auth:
        raw_auth["radius_default_role"] = normalize_role(raw_auth["radius_default_role"])
    if raw_auth.get("radius_role_attribute") not in (None, *RADIUS_ROLE_ATTRIBUTES):
        logger.warning(f"config: auth.radius_role_attribute must be one of "
                       f"{', '.join(RADIUS_ROLE_ATTRIBUTES)} — using Class")
        raw_auth["radius_role_attribute"] = "Class"
    method = (raw_auth.pop("method", "") or "").strip().lower()
    if method not in ("", "none", "radius", "local"):
        method = ""
    if method == "none":
        # Explicitly disabled — skip the inference below, so login stays off
        # even when radius_server / local_users are still configured.
        method = ""
    elif not method:
        # Backward compat: older configs only set radius_server, with no
        # explicit method field.
        if raw_auth.get("radius_server"):
            method = "radius"
        elif local_users:
            method = "local"
    auth = AuthConfig(method=method, local_users=local_users, **raw_auth)

    thresholds, threshold_errs = parse_thresholds(raw.get("thresholds"))
    for err in threshold_errs:
        logger.warning(f"config: {err} — using the default")

    branding, branding_errs = parse_branding(raw.get("branding"))
    for err in branding_errs:
        logger.warning(f"config: {err} — using the default")

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
        thresholds=thresholds,
        branding=branding,
    )

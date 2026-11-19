"""
hostaddr.py
Validation and resolution for agent / server addresses.

Anywhere LinkState takes a host it accepts an IPv4 address, an IPv6 address,
or a DNS hostname / FQDN. Names are resolved on the controller at test time
(by default) and the resulting IP is what ping / iPerf3 / mtr / traceroute
target on the agent — so agents don't need working DNS, and every tool in a
run hits the same address.
"""

import ipaddress
import re
import socket
import threading
from typing import List, Optional

ADDRESS_FAMILIES = ("ipv4", "ipv6", "auto")
RESOLVE_ON = ("controller", "agent")

# RFC 1123 hostname: dot-separated labels of letters/digits/hyphens, each
# 1-63 chars, not starting or ending with a hyphen; 253 chars total. An
# optional trailing dot (fully-qualified form) is allowed. Underscores are
# accepted too — they're common in internal DNS (e.g. AD SRV-derived names)
# and harmless here, since the value is never passed to a shell unquoted.
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9_-]{1,63}(?<!-)$")


class HostResolutionError(Exception):
    pass


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address((value or "").strip())
        return True
    except ValueError:
        return False


def is_hostname(value: str) -> bool:
    v = (value or "").strip()
    if v.endswith("."):
        v = v[:-1]
    if not v or len(v) > 253:
        return False
    labels = v.split(".")
    if not all(_LABEL_RE.match(l) for l in labels):
        return False
    # An all-numeric dotted string that isn't a valid IP (e.g. 10.0.0.300)
    # is a typo, not a hostname.
    if all(l.isdigit() for l in labels):
        return False
    return True


def validate_host(value: str) -> Optional[str]:
    """Return an error message if value isn't an IP or hostname, else None."""
    v = (value or "").strip()
    if not v:
        return "is empty"
    if is_ip(v) or is_hostname(v):
        return None
    return f"'{v}' is not a valid IP address or hostname"


def _family_const(family: str) -> int:
    return {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}.get(family, socket.AF_UNSPEC)


def resolve_all(host: str, family: str = "ipv4", timeout: float = 5.0) -> List[str]:
    """All addresses host resolves to from the controller, in resolver order.

    An IP literal is returned as-is (after checking it matches family).
    Raises HostResolutionError on failure or timeout.
    """
    h = (host or "").strip()
    if not h:
        raise HostResolutionError("no host given")
    if is_ip(h):
        ver = ipaddress.ip_address(h).version
        if family == "ipv4" and ver != 4:
            raise HostResolutionError(f"{h} is an IPv6 address but the agent's address family is IPv4")
        if family == "ipv6" and ver != 6:
            raise HostResolutionError(f"{h} is an IPv4 address but the agent's address family is IPv6")
        return [h]

    # getaddrinfo has no timeout of its own — run it in a thread and stop
    # waiting after `timeout` so a dead DNS server can't stall a test run.
    box = {}

    def _lookup():
        try:
            box["res"] = socket.getaddrinfo(h, None, _family_const(family),
                                            socket.SOCK_STREAM)
        except Exception as e:  # socket.gaierror, UnicodeError, ...
            box["err"] = e

    t = threading.Thread(target=_lookup, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise HostResolutionError(f"timed out after {timeout:g}s resolving {h} from the controller")
    if "err" in box:
        fam = {"ipv4": " (IPv4)", "ipv6": " (IPv6)"}.get(family, "")
        raise HostResolutionError(f"could not resolve {h}{fam} from the controller: {box['err']}")

    addrs = []
    for _fam, _t, _p, _c, sockaddr in box.get("res", []):
        ip = sockaddr[0].split("%")[0]  # drop IPv6 zone id
        if ip not in addrs:
            addrs.append(ip)
    if not addrs:
        raise HostResolutionError(f"{h} has no {family} address")
    return addrs


def resolve_host(host: str, family: str = "ipv4", timeout: float = 5.0) -> str:
    """First address host resolves to from the controller. See resolve_all."""
    return resolve_all(host, family, timeout)[0]


def af_flag(family: str) -> str:
    """Command-line flag pinning ping/iperf3/mtr/traceroute to a family."""
    return {"ipv4": "-4", "ipv6": "-6"}.get(family, "")


def display(host: str, addr: Optional[str]) -> str:
    """'name (ip)' when they differ, else just the host."""
    if addr and addr != host:
        return f"{host} ({addr})"
    return host

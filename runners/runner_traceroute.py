"""
runner_traceroute.py
Runs traceroute from source agent toward destination.
Supports forward-only (when destination is svi_adjacent or unreachable via SSH)
and bidirectional (endpoint-to-endpoint).
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from core.ssh_manager import SSHManager

logger = logging.getLogger(__name__)


@dataclass
class TracerouteHop:
    hop:        int
    ip:         Optional[str]      # None if non-responding
    hostname:   Optional[str]      # None if no DNS or same as IP
    rtt_ms:     List[float]        # Up to 3 probe RTTs (empty if *)
    responding: bool               # False if all probes were *

    @property
    def rtt_avg_ms(self) -> Optional[float]:
        if not self.rtt_ms:
            return None
        return round(sum(self.rtt_ms) / len(self.rtt_ms), 3)

    @property
    def display_host(self) -> str:
        """Best available label for this hop."""
        if self.hostname and self.hostname != self.ip:
            return self.hostname
        return self.ip or '*'


@dataclass
class TracerouteResult:
    direction:    str               # 'forward' or 'reverse'
    source_host:  str
    dest_host:    str
    hops:         List[TracerouteHop] = field(default_factory=list)
    completed:    bool = False      # True if destination was reached
    error:        Optional[str] = None


class TracerouteRunner:

    def __init__(self, max_hops: int = 30, probes: int = 2,
                 wait_sec: int = 1, resolve_dns: bool = True):
        self.max_hops   = max_hops
        self.probes     = probes
        self.wait_sec   = wait_sec
        self.resolve_dns = resolve_dns

    def run_forward(self, src_ssh: SSHManager,
                    dst_host: str) -> TracerouteResult:
        """Run traceroute from source agent to destination."""
        logger.info(f"  Running traceroute to {dst_host} "
                    f"(max {self.max_hops} hops, {self.probes} probes, "
                    f"{self.wait_sec}s wait)...")

        dns_flag = '' if self.resolve_dns else '-n '
        cmd = (f"traceroute {dns_flag}"
               f"-m {self.max_hops} "
               f"-q {self.probes} "
               f"-w {self.wait_sec} "
               f"{dst_host} 2>&1")

        timeout = self.max_hops * self.probes * self.wait_sec + 30
        try:
            output = src_ssh.run(cmd, timeout=timeout)
        except Exception as e:
            return TracerouteResult(
                direction='forward',
                source_host=src_ssh.host,
                dest_host=dst_host,
                error=str(e),
            )

        result = self._parse(output, 'forward', src_ssh.host, dst_host)
        responding = sum(1 for h in result.hops if h.responding)
        logger.info(f"  Traceroute forward: {len(result.hops)} hops, "
                    f"{responding} responding, "
                    f"{'reached destination' if result.completed else 'did not reach destination'}")
        return result

    def run_reverse(self, dst_ssh: SSHManager,
                    src_host: str) -> TracerouteResult:
        """Run traceroute from destination agent back to source."""
        logger.info(f"  Running reverse traceroute from {dst_ssh.host} → {src_host}...")

        dns_flag = '' if self.resolve_dns else '-n '
        cmd = (f"traceroute {dns_flag}"
               f"-m {self.max_hops} "
               f"-q {self.probes} "
               f"-w {self.wait_sec} "
               f"{src_host} 2>&1")

        timeout = self.max_hops * self.probes * self.wait_sec + 30
        try:
            output = dst_ssh.run(cmd, timeout=timeout)
        except Exception as e:
            return TracerouteResult(
                direction='reverse',
                source_host=dst_ssh.host,
                dest_host=src_host,
                error=str(e),
            )

        result = self._parse(output, 'reverse', dst_ssh.host, src_host)
        responding = sum(1 for h in result.hops if h.responding)
        logger.info(f"  Traceroute reverse: {len(result.hops)} hops, "
                    f"{responding} responding")
        return result

    def _parse(self, output: str, direction: str,
               source_host: str, dest_host: str) -> TracerouteResult:
        """Parse traceroute output into TracerouteResult."""
        hops = []
        completed = False

        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith('traceroute'):
                continue

            hop = self._parse_line(line, dest_host)
            if hop:
                hops.append(hop)
                if hop.ip == dest_host or hop.hostname == dest_host:
                    completed = True

        return TracerouteResult(
            direction=direction,
            source_host=source_host,
            dest_host=dest_host,
            hops=hops,
            completed=completed,
        )

    def _parse_line(self, line: str, dest_host: str) -> Optional[TracerouteHop]:
        """
        Parse a single traceroute output line.

        Examples:
          1  192.168.5.1 (192.168.5.1)  0.432 ms  0.381 ms  0.290 ms
          2  * * *
          3  router.isp.net (1.2.3.4)  12.1 ms  11.8 ms  12.3 ms
          4  1.2.3.4  5.2 ms  * 6.1 ms
        """
        # Match hop number at start of line
        m = re.match(r'^\s*(\d+)\s+(.+)$', line)
        if not m:
            return None

        hop_num = int(m.group(1))
        rest    = m.group(2).strip()

        # All non-responding
        if re.match(r'^[\*\s]+$', rest):
            return TracerouteHop(
                hop=hop_num, ip=None, hostname=None,
                rtt_ms=[], responding=False
            )

        # Extract RTTs
        rtt_vals = re.findall(r'([\d.]+)\s*ms', rest)
        rtts = [float(r) for r in rtt_vals]

        # Extract IP and hostname
        # Pattern: hostname (ip) or just ip
        host_match = re.search(
            r'([a-zA-Z0-9._-]+)\s+\((\d+\.\d+\.\d+\.\d+)\)', rest
        )
        ip_only_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', rest)

        if host_match:
            hostname = host_match.group(1)
            ip       = host_match.group(2)
            # If hostname looks like an IP, treat it as no hostname
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', hostname):
                hostname = None
        elif ip_only_match:
            ip       = ip_only_match.group(1)
            hostname = None
        else:
            ip       = None
            hostname = None

        return TracerouteHop(
            hop=hop_num,
            ip=ip,
            hostname=hostname,
            rtt_ms=rtts,
            responding=bool(rtts),
        )

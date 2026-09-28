"""
Agent packages for air-gapped onboarding.

Onboarding installs the agent tools from .debs on the controller:
  packages/*.deb                  uploaded via Config → Packages, used for every agent
  packages/bundled/<id>-<ver>/    staged from a release bundle, used only for
                                  agents on that OS release (e.g. ubuntu-26.04)

A release bundle's vendor/debs already holds the agent tools and their
dependencies (they're controller requirements too), built for the
controller's own OS release. stage_bundled() copies just those into
packages/bundled/ at install/update time, so agents on the controller's
release onboard offline with nothing uploaded.
"""

import os
import re
import shutil
import subprocess

# (tool, package) — what onboarding makes sure every agent has
REQUIRED_TOOLS = [
    ("iperf3",     "iperf3"),
    ("mtr",        "mtr-tiny"),
    ("ping",       "iputils-ping"),
    ("traceroute", "traceroute"),
    ("fuser",      "psmisc"),
    ("libsctp1",   "libsctp1"),
]

BUNDLED_SUBDIR = "bundled"


def os_release_id(os_release_text: str) -> str:
    """'ubuntu-26.04' from the contents of /etc/os-release ('' if unknown)."""
    fields = dict(re.findall(r'^(\w+)=\"?([^\"\n]*)\"?$', os_release_text, re.M))
    if not fields.get("ID") or not fields.get("VERSION_ID"):
        return ""
    return f"{fields['ID']}-{fields['VERSION_ID']}"


def local_os_release_id() -> str:
    try:
        with open("/etc/os-release") as f:
            return os_release_id(f.read())
    except OSError:
        return ""


def debs_for_agent(packages_dir: str, agent_release: str):
    """Full paths of the .debs onboarding offers an agent on agent_release."""
    paths = {}
    dirs = [packages_dir]
    if agent_release:
        dirs.append(os.path.join(packages_dir, BUNDLED_SUBDIR, agent_release))
    for d in dirs:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".deb"):
                    paths.setdefault(f, os.path.join(d, f))
    return [paths[f] for f in sorted(paths)]


def _control(path: str) -> dict:
    out = subprocess.run(["dpkg-deb", "-f", path, "Package", "Depends", "Pre-Depends"],
                         capture_output=True, text=True, check=True).stdout
    return dict(re.findall(r"^([A-Za-z-]+): (.*)$", out, re.M))


def _dep_names(ctrl: dict) -> set:
    """Every package named in Depends/Pre-Depends, all alternatives included."""
    names = set()
    for rel in (ctrl.get("Depends", "") + "," + ctrl.get("Pre-Depends", "")).split(","):
        for alt in rel.split("|"):
            m = re.match(r"\s*([a-z0-9][a-z0-9.+-]*)", alt)
            if m:
                names.add(m.group(1))
    return names


def stage_bundled(vendor_debs: str, packages_dir: str, release: str = None, log=print) -> int:
    """Copy the agent tools and their dependencies from a bundle's vendor/debs
    into packages/bundled/<release>/, replacing what was staged there before.
    Returns the count.

    Unlike the controller bundle, packages that pin one of these to an exact
    version aren't added: the agent tools only need minimum versions, so apt
    rarely has to upgrade anything already on an agent, and following those
    pins drags in most of the bundle (systemd, perl, python). If an upgrade
    ever would need one, onboarding's apt --no-remove stops without changes."""
    release = release or local_os_release_id()
    if not release or not os.path.isdir(vendor_debs):
        return 0

    debs = {}
    for f in os.listdir(vendor_debs):
        if f.endswith(".deb"):
            path = os.path.join(vendor_debs, f)
            ctrl = _control(path)
            debs[ctrl["Package"]] = (path, _dep_names(ctrl))

    wanted = {pkg for _, pkg in REQUIRED_TOOLS if pkg in debs}
    while True:
        add = {d for p in wanted for d in debs[p][1] if d in debs}
        if add <= wanted:
            break
        wanted |= add

    dest = os.path.join(packages_dir, BUNDLED_SUBDIR, release)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    for p in sorted(wanted):
        shutil.copy2(debs[p][0], dest)
    log(f"Staged {len(wanted)} bundled agent package(s) for {release} agents in {dest}")
    return len(wanted)


if __name__ == "__main__":
    # install.sh: python3 -m core.agent_packages <vendor/debs> <packages dir>
    import sys
    stage_bundled(sys.argv[1], sys.argv[2])

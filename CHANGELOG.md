# NetTest Changelog

All notable changes to NetTest are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.1.0] - 2026-09-16

### Added
- Terminal UI (`tui.py`) — live-updating dashboard with per-path health at a glance, `--once` snapshot mode, and `--path` focus mode
- Compare Runs page — side-by-side comparison of past test runs
- Speed Test page — on-demand ping/download/upload/MTU probe from the browser
- Result annotations — notes and tags on individual runs, stored as a sidecar JSON store
- `rollback.sh` — restore a previous version from an install-time snapshot
- `make_release.sh` — build a versioned, dependency-vendored release tarball with Python compile validation

### Changed
- Scheduler and path tester internals reworked to support the new comparison and annotation features

## [1.0.0] - 2026-05-18

### Added
- Initial release
- Multi-site network test controller with SSH-based agent execution
- Test types: latency, throughput, jitter, MTU discovery, traceroute, latency-under-load
- Multi-hop path support with per-segment RTT and MTU breakdown
- Web dashboard with live streaming test output
- RADIUS authentication with session management and rate limiting
- Agent onboarding wizard (internet-connected and air-gapped)
- Air-gapped package staging and push via Config → Packages
- Traceroute visualization with forward and reverse hop flow
- MTR hop detail panel for latency-under-load paths
- Sparkline overview grid with per-path trend charts
- HTTPS support via nginx reverse proxy with self-signed cert generation
- SSH key management — import, export, push to agents
- Config export/import as .tar.gz bundle with selective section support
- Config editor UI — agents, paths, schedule, SSH, tests, packages, HTTPS, export/import
- Scheduler with full-suite and latency-only tiers, business hours support
- Results stored as JSONL with 7-day history in web UI
- iPerf3 zombie daemon fix using two-pass kill (pkill + fuser)
- install.sh for fresh install and upgrade with nginx and SSL setup
- systemd service files for scheduler and web dashboard (gunicorn + gevent)

## [1.0.5] - 2026-08-29

## Fixed
- Restored update ui


## [1.0.6] - 2026-08-29

## Added
- Pop out log
- Abort 


## [1.0.12] - 2026-09-10

## Added
- Enhanced scheduler

## [1.0.13] - 2026-09-10

## Added
- Offline installer enhancements

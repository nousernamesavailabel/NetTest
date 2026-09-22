# NetTest Changelog

All notable changes to NetTest are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

## [1.1.4] - 2026-09-22

### Fixed
- The Config → Updates bundle uploader failed with `Unexpected token '<'... is not valid JSON` for any release bundle over 1MB (i.e. every real release). nginx's default `client_max_body_size` (1MB) was rejecting the upload with a 413 HTML error page before it reached the app, and the webui tried to parse that HTML as JSON. Both `install.sh` and the webui's own nginx-config writer now set `client_max_body_size 100M`, and the uploader reports a proper error message instead of a JS parse failure if a non-JSON response comes back.
  - Note: servers already running with the old nginx config won't pick this up through the webui updater itself, since the same 1MB cap blocks the upload of this very fix. Apply it out of band first — either run `sudo ./install.sh --upgrade` from a locally-copied bundle (which always regenerates the nginx config), or manually add `client_max_body_size 100M;` to `/etc/nginx/sites-available/nettest` and reload nginx — after which future webui updates work normally.

## [1.1.3] - 2026-09-22

### Added
- Config → Logging page with timed debug mode (15 min, 30 min, 1 hour, 3 hours). Debug output goes to size-capped `logs/debug-scheduler.log` and `logs/debug-dashboard.log` (viewable and downloadable from the page) and never to the console or `controller.log`. The scheduler and dashboard both honour it, and it survives a service restart until it expires.
- With debug on, SSH commands log elapsed time and their full raw output (escape sequences visible), and Netmiko's channel read/write trace is captured.

### Fixed
- Throughput and UDP jitter iPerf3 clients now run under a remote `timeout` (test duration + 20s). A hung iPerf3 is stopped by the controller and reported as "did not finish within Ns" instead of surfacing as Netmiko's `Pattern not detected: '\$'`.
- A command that never returns to the shell prompt now fails with a message stating how long it waited (`no shell prompt within Ns (waited Xs) running: …`), and says so when the read loop overran its limit and Netmiko discarded the final read.

### Changed
- iPerf3 parallel stream count (`-P`) moved from a single global Test Params setting to a per-path field on Test Paths, since the right count depends on each path's own bandwidth — too many streams on a constrained or policed link causes congestion collapse rather than a useful measurement. It also now drives Latency Under Load's saturation stream count for that path. Duration stays global. Existing paths keep the old global value as their per-path default until edited.

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

## [1.1.1] - 2026-09-16

## Added 
- Comparison tool
- Local login
- Uninstaller

## [1.1.2] - 1016-09-21

## Added
- Debug mode

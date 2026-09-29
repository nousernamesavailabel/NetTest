# NetTest Changelog

All notable changes to NetTest are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.3.0] - 2026-09-29

### Changed
- iPerf3 now runs as a persistent service (`nettest-iperf3@<port>`) on every agent and on the controller, instead of being killed and restarted on the destination before every test. Onboarding installs and enables it; `install.sh` does the same on the controller, and `uninstall.sh` removes it. Tests check that the server is listening, and if it is stuck "busy" or refusing connections they restart it once and retry. This saves the few seconds each test spent killing and restarting the server. The service runs as its own throwaway user, so the tests' cleanup can't kill it.
- Agents onboarded before this change keep working: each test still starts a temporary server there. Re-onboard them to install the service. On the Config page, installed agents without the service show a **NEEDS RE-ONBOARD** badge (or **IPERF3 DOWN** if the service isn't listening), and the agent test reports the service state.
- The agent sudoers entry now allows restarting the iPerf3 service for each configured iPerf3 port.

### Added
- **RX Mbps** column in the Path Overview table (All paths). It shows the average download throughput over the selected window, next to TX Mbps.
- **↻ Re-onboard** button on each installed agent (Config → Agents). It re-runs onboarding with only the admin credentials to enter. The agent's IP, label, ID and type are taken from the saved config and can't be changed, and its config entry is left as it is. Use it to install the iPerf3 service on existing agents, or to repair an agent's packages, SSH key or sudoers.

### Security
- 17 dashboard routes that didn't check the login now require it: results, summaries and exports, clearing results, starting test runs, job status/logs/live streams (which include onboarding output), the agent SSH test and the `/live` page. Previously anyone who could reach the dashboard could use them without logging in, even with RADIUS or local login turned on. Only `/login`, `/logout` and static files stay public.
- The dashboard session cookie is now `SameSite=Lax`, so another website can't use a logged-in browser to start test runs, clear results or change config. Chrome already did this by default; Firefox and Safari did not.

### Fixed
- Latency under load now fails (and is retried later) when its load stream hits a busy iPerf3 server. It previously went on to measure "loaded" latency on an idle link.

## [1.2.6] - 2026-09-28

### Fixed
- Failure of one iPerf test no longer causes all to fail and not be recorded. 

## [1.2.5] - 2026-09-24

### Security
- `install.sh --upgrade` no longer leaves a copy of the controller's private SSH key in `/tmp` if the upgrade fails partway. The key is backed up before the file sync and restored afterwards if missing; a failure in between left the backup in `/tmp/nettest-key-backup-<pid>`. The backup folder is now created with `mktemp` (private, unpredictable name) and removed whenever the script exits, successfully or not.

### Fixed
- Throughput, jitter and latency-under-load tests failed when two paths sharing an agent ran at the same time (e.g. Run All, or scheduled paths still running when the next one starts). Each iPerf3 test begins with `pkill -9 -f iperf3` on its destination, which killed the other path's iPerf3 server — or its client, when that agent was the other path's source — showing up as "control socket has closed unexpectedly", "Connection refused", or "iPerf3 UDP produced no JSON". iPerf3 tests now hold a per-agent lock on both endpoints (`core/host_locks.py`, `flock` files in `run/locks/`, shared by the scheduler, the dashboard and CLI runs by any user), so a test waits for any other path's iPerf3 test on either agent to finish first. The wait is logged and can be aborted. Tests that don't use iPerf3 are unaffected.
- Run All started every path in the same instant. It now staggers them by `schedule.stagger_seconds`, like scheduled runs; a staggered job shows as queued and can be aborted before it starts.
- A job's live log in the dashboard also collected every other concurrently running job's log lines. Each path job's log now only keeps its own.
- An SSH session to an agent could start out one command behind: every command returned the previous command's output, so the first test failed with "Could not read ping results" and later tests parsed the wrong output (e.g. ping output inside a jitter test). Netmiko's login matched a `$`/`#` in the login banner as the prompt, leaving a real prompt unread for the first command to pick up; more likely when the agent is slow to log in. Each connection now echoes a unique marker and reads through its output and the following prompt before running any test.
- A path run was reported as PASSED (✓ / OK in the dashboard, TUI and CSV export) even when some or all of its tests had failed. The run's `success` flag only means both agents were reached; failed tests are listed in its error. Such runs now show as "COMPLETED WITH ERRORS" in the log (with the failed tests listed), ⚠ / PARTIAL in the dashboard, `!` in the TUI and `PARTIAL` in the CSV export, and are no longer counted as successful in the summary stats. The summary API has a new `partial` count.
- Latency under load stored idle 0.0ms / loaded 0.0ms when the source got no ping replies, and a loaded 0.0ms (read as a large improvement) when all pings were lost under load. With no baseline replies the test now fails before saturating the link; with no replies under load it stops the load, skips the MTR trace and fails with the baseline in the message.
- A path's latency summary showed "avg 0ms max 0ms" when every ping was lost; it now says there were no replies.
- A throughput, jitter or latency-under-load test that was retried after "server busy" and then succeeded still left its busy error on SSH-reachable destinations, so the run counted as having failed tests. For agent-not-installed destinations, a retry that failed again with "busy" had its error silently dropped. A test's old error is now cleared just before it is retried, so the retry's own outcome is what gets recorded.
- The standalone live output page (`/live?job_id=…`) never showed any output. Its page is a Python string, which turned the script's `'\n'` into a real line break inside a JavaScript string — a syntax error that stopped the whole script. The string is now raw, which also clears the "invalid escape sequence" SyntaxWarning `web_dashboard.py` printed on start (and that future Python versions will make an error).

## [1.2.4] - 2026-09-24

### Added
- Agents on the same OS release as the controller now onboard offline with nothing uploaded. A release bundle already contains the agent tools and their dependencies, so `install.sh` (fresh installs and `--upgrade`) and Config → Updates now stage them in `packages/bundled/<os>-<version>/` (e.g. `ubuntu-26.04`), replacing the previous bundle's set. On Ubuntu 26.04 that's 46 packages (12MB).
- Air-gapped onboarding reads the agent's OS release and offers bundled packages only to agents whose release matches, so an agent on another release never gets packages built for the wrong one. Packages uploaded via Config → Packages are still offered to every agent, and uploads are never touched by staging.
  - Note: when upgrading from 1.2.3 or earlier through Config → Updates, the old version's updater runs and doesn't stage the agent packages, so they only appear after the next update. To stage them now, upgrade with `sudo ./install.sh --upgrade` from the extracted bundle instead, or run `cd /opt/nettest && sudo -u nettest python3 -m core.agent_packages <bundle>/vendor/debs /opt/nettest/packages` after a web update.

### Fixed
- Onboarding reported every agent tool as "NOT installed" even when it was installed, so air-gapped onboarding always failed, and online onboarding reinstalled tools the agent already had and then warned they were missing. The installed-check (`dpkg-query -f='${Status}'`) printed no trailing newline, so the status landed on the same line as the next shell prompt. Netmiko strips the prompt line from command output, which removed the status with it. The check now ends the status with a newline, and online and air-gapped onboarding share it.

## [1.2.3] - 2026-09-24

### Fixed
- Air-gapped agent onboarding installed packages unreliably. It ran `dpkg -i` on every staged `.deb` in a hardcoded order. That installed or downgraded packages the agent didn't need, left packages half-configured when dependencies didn't line up, and could send the next command before `dpkg` had finished. Onboarding now installs the way the offline controller install does:
  - The staged packages are copied to the agent as a temporary local apt repository, and apt installs only the required tools the agent is missing, by name. apt resolves dependencies from the staged packages and never downgrades anything.
  - apt runs with `--no-remove`. If installing would remove any package on the agent, it stops before changing anything.
  - Onboarding waits for apt to finish (up to 15 minutes, and up to 5 minutes for another apt process such as unattended-upgrades to release its lock).
  - If the install fails, the agent's OS release and apt's actual error output are shown, with the likely cause: a package not staged, packages built for a different OS release, or an install that would remove packages.
  - If copying the packages to the agent fails, onboarding now stops instead of carrying on without them.
- The admin password used during onboarding is now quoted safely. Passwords containing `'` broke the sudo commands. The password also no longer lands in the agent's shell history.

## [1.2.2] - 2026-09-24

### Fixed
- An offline install could remove unrelated packages from the controller. When the target was at an older patch level than the build machine, apt had to upgrade some installed packages (e.g. `python3.14`) to the bundled versions. Packages tied to those at an exact version (e.g. `libpython3.14`) weren't bundled, so apt removed them and everything depending on them (`vim`, `linux-perf`, `ubuntu-server`). Two changes:
  - `make_release.sh` now also bundles every installed package that pins a bundled package to an exact version, plus its dependencies. "Installed" means installed on the build machine, or listed in `NETTEST_TARGET_MANIFEST` (a `dpkg-query -W -f='${Package}\n'` list from a real target). `-dev`, `-doc` and `-dbg` packages are left out. On Ubuntu 26.04 this grows `vendor/debs` from about 28MB to 63MB.
  - `install.sh` runs the offline apt install with `--no-remove`. If installing would remove any package, it stops before changing anything and explains how to fix the bundle.
- On controllers with no internet access, the dashboard graphs never loaded and the status at the top stuck on "connecting..." or "error". The Dashboard and Compare pages loaded Chart.js from a CDN; the missing library made every dashboard refresh fail. Chart.js 4.4.1 now ships in `web/static/chart.umd.js` and is served by the dashboard itself.

## [1.2.1] - 2026-09-23

### Added
- RADIUS server settings (server, port, shared secret, timeout) are now editable under Config → Auth, so RADIUS no longer needs `config.yaml` or a re-run of `install.sh`. The shared secret is never shown; the field starts blank and leaving it blank keeps the saved secret. Port must be 1–65535 and timeout 1–60 seconds, and the login method can't be switched to RADIUS until a server and secret are set.

### Fixed
- Config → Auth showed "Disabled" when RADIUS login was actually active. With `method: ""` in `config.yaml`, the method is inferred from `radius_server` / `local_users`, but the page showed the raw blank value. It now shows the method the dashboard is really using.
- Choosing "Disabled" in Config → Auth didn't turn login off when RADIUS or local users were configured, because it saved a blank method that was then inferred back to RADIUS/local. It now saves `method: none`, which disables login regardless of other auth settings. A blank method keeps its existing inferred behavior, and config exports write `none` instead of a blank so importing them keeps login off.
- Removing the last local user is now blocked whenever local login is the method actually in use, including when it was inferred rather than set explicitly.

### Security
- The Config page's config API no longer sends secrets to the browser: the RADIUS shared secret, `auth.session_secret`, local users' password hashes and the SSH default password are removed from the response (the RADIUS secret and SSH password are reported only as set/not set). Saving from the Config page keeps all of them on disk, and `session_secret` can no longer be changed through the config API.

### Changed
- `install.sh`'s add-local-user step now treats `method: none` like a blank method and enables login.

## [1.2.0] - 2026-09-23

### Changed
- Release bundles are now fully self-contained, so a controller installs with no internet connection. `make_release.sh` downloads every system package and dependency into `vendor/debs` and a wheel for every pinned Python package into `vendor/wheels`, then checks that the wheels install with no index before building. `install.sh` detects the bundled dependencies and uses them without prompting for paths. Set `NETTEST_ONLINE=true` to use apt and PyPI instead.
- Offline system-package installs now run through a temporary local apt repository, so apt installs only the packages the server is missing and never downgrades anything already installed.
- Python dependencies are pinned in `requirements.lock`, generated from `requirements.txt` with `./make_release.sh --lock`. Fresh installs, `install.sh --upgrade`, `rollback.sh` and Config → Updates / rollback in the web UI all install from the lock file, and from the bundled wheels when present, so they no longer need PyPI. `install.sh --upgrade` now syncs Python dependencies from a bundle instead of skipping pip, so a release that adds a dependency can't leave an upgraded controller broken.
- Release bundles now contain only the files the app needs, no longer notes, logs, `__pycache__` or the build script. The bundle is about 44MB instead of 68MB.

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

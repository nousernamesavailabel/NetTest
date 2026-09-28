"""
ssh_manager.py
Manages SSH connections to agents via Netmiko.
"""

import logging
import os
import time
import uuid
import getpass
from contextlib import contextmanager
from typing import Optional

from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from netmiko.exceptions import ReadTimeout

logger = logging.getLogger(__name__)


class SSHConnectionError(Exception):
    pass


class SSHCommandTimeout(Exception):
    """A command never handed the shell prompt back within its time limit.

    Deliberately not an SSHConnectionError: the connection is fine, and
    path_tester reports that class as "SSH connection failed" for the whole path.
    """


# iperf3 -J prints nothing until it exits, so a client that hangs (server or
# path stopped answering) is indistinguishable from a slow one until netmiko's
# read_timeout fires — and then all it can say is "Pattern not detected".
# Wrapping the client in a remote `timeout` makes iperf3 itself stop and report
# ("interrupt - the client has terminated" in its JSON) before netmiko gives up.
IPERF_CLIENT_GRACE_SEC = 20    # remote timeout = test duration + this
IPERF_KILL_AFTER_SEC   = 5     # SIGKILL if iperf3 ignores the SIGINT
IPERF_READ_MARGIN_SEC  = 15    # netmiko waits this much longer than the remote limit
IPERF_INTERRUPT_ERROR  = "interrupt - the client has terminated"


def guard_iperf3_client(cmd: str, duration_sec: int):
    """Return (command, read_timeout, limit_sec) for a foreground `iperf3 -J` client.

    The command is wrapped so iperf3 is stopped `limit_sec` after it starts; the
    netmiko read_timeout sits past that (including the SIGKILL delay) so the
    JSON iperf3 emits on interrupt is always read before the deadline.
    No `$` may appear in the wrapper: send_command's expect_string is r"\\$" and
    it is matched against the command echo too.
    """
    limit_sec    = duration_sec + IPERF_CLIENT_GRACE_SEC
    read_timeout = limit_sec + IPERF_KILL_AFTER_SEC + IPERF_READ_MARGIN_SEC
    wrapped = f"timeout -s INT -k {IPERF_KILL_AFTER_SEC} {limit_sec} {cmd}"
    return wrapped, read_timeout, limit_sec


def iperf3_error_text(error: str, limit_sec: int = None) -> str:
    """Turn an iperf3 JSON "error" string into the message to raise, spelling out
    the case where it was our own remote `timeout` that stopped the client."""
    if error.strip() == IPERF_INTERRUPT_ERROR:
        after = f" within {limit_sec}s" if limit_sec else ""
        return (f"iperf3 did not finish{after} and was stopped by the controller "
                f"(no result exchange — the server or path stopped responding)")
    return error


class SSHManager:

    BG_LOG_PATH = "/tmp/nettest_bg.log"
    DEBUG_OUTPUT_CHARS = 20000   # cap on raw command output written to the debug log

    def __init__(self, host: str, username: str, port: int = 22,
                 password: str = "", key_file: str = "",
                 timeout: int = 30, retries: int = 3, retry_delay: int = 5):
        self.host = host
        self.username = username
        self.port = port
        self.password = password
        self.key_file_input = key_file or ""
        self.key_file = os.path.expanduser(self.key_file_input) if self.key_file_input else ""
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._connection = None

    def _build_netmiko_params(self) -> dict:
        params = {
            "device_type":       "linux",
            "host":              self.host,
            "username":          self.username,
            "port":              self.port,
            "timeout":           self.timeout,
            "conn_timeout":      self.timeout,
            "auth_timeout":      self.timeout,
            "global_cmd_verify": False,
        }
        if self.password:
            params["password"] = self.password
        if self.key_file:
            if not os.path.isfile(self.key_file):
                raise SSHConnectionError(
                    f"SSH key file not found: {self.key_file} "
                    f"(configured as {self.key_file_input!r}, running as {getpass.getuser()})"
                )
            params["use_keys"]    = True
            params["key_file"]    = self.key_file
            params["allow_agent"] = False
        return params

    def connect(self):
        """Open SSH connection with retry logic."""
        for attempt in range(1, self.retries + 1):
            try:
                logger.debug(f"SSH connecting to {self.host}:{self.port} "
                             f"as {self.username} (attempt {attempt}/{self.retries})")
                self._connection = ConnectHandler(**self._build_netmiko_params())
                try:
                    self._sync_channel()
                except Exception:
                    self.disconnect()
                    raise
                logger.info(f"SSH connection established to {self.host}")
                return
            except NetmikoAuthenticationException as e:
                raise SSHConnectionError(
                    f"Authentication failed for {self.username}@{self.host} — "
                    f"check SSH key is installed on the agent"
                )
            except NetmikoTimeoutException:
                logger.warning(f"Connection timed out to {self.host} "
                               f"(attempt {attempt}/{self.retries}, timeout={self.timeout}s)")
                if attempt < self.retries:
                    logger.info(f"Retrying in {self.retry_delay}s...")
                    time.sleep(self.retry_delay)
            except Exception as e:
                logger.warning(f"SSH error connecting to {self.host} "
                               f"(attempt {attempt}/{self.retries}): {e}")
                if attempt < self.retries:
                    logger.info(f"Retrying in {self.retry_delay}s...")
                    time.sleep(self.retry_delay)

        raise SSHConnectionError(
            f"Could not connect to {self.host} after {self.retries} attempts — "
            f"check the host is reachable on port {self.port}"
        )

    def _sync_channel(self, timeout: int = 15):
        """Consume everything the login left unread, up to a fresh prompt.

        netmiko's session setup sends newlines to find the prompt and can
        match a `$`/`#` in the login banner instead, leaving a real prompt
        unread. run()'s expect_string r"\\$" then matches that leftover
        prompt instantly on the first command, returning nothing, and every
        later command reads the output of the one before it. Echoing a
        unique marker and reading through its output and the prompt that
        follows puts the channel back in step.
        """
        token = uuid.uuid4().hex[:12]
        # The quotes keep the command echo from containing the marker —
        # only the command's output can match.
        self._connection.write_channel(f'echo NT_SYNC_""{token}\n')
        self._connection.read_until_pattern(
            pattern=rf"NT_SYNC_{token}[^\n]*\n[^\n]*\$",
            read_timeout=timeout,
        )
        # Drop what followed the matched `$` (the prompt's trailing space) —
        # left in netmiko's read buffer, it would prefix the next command's
        # echo and stop send_command from stripping it.
        self._connection.clear_buffer()

    def disconnect(self):
        if self._connection:
            try:
                self._connection.disconnect()
            except Exception:
                pass
            self._connection = None
            logger.debug(f"SSH disconnected from {self.host}")

    def reconnect(self):
        """Tear down and re-establish the session.

        Closing the transport sends SIGHUP to whatever the remote shell was
        running in the foreground, so a command that never returned control
        (e.g. a hung iPerf3 client) gets killed off instead of continuing to
        occupy the shell that the next test's command would otherwise be
        queued behind.
        """
        logger.debug(f"[{self.host}] Reconnecting session")
        self.disconnect()
        self.connect()

    def run(self, command: str, timeout: int = 120) -> str:
        if not self._connection:
            raise SSHConnectionError("Not connected — call connect() first")
        logger.debug(f"[{self.host}] Running: {command}")
        started = time.monotonic()
        try:
            output = self._connection.send_command(
                command,
                read_timeout=timeout,
                expect_string=r"\$",
            )
        except Exception as e:
            elapsed = time.monotonic() - started
            logger.debug(f"[{self.host}] Command raised after "
                         f"{elapsed:.1f}s (timeout={timeout}s)",
                         exc_info=True)
            # The shell may still be occupied by whatever just failed to
            # return (or the channel buffer may be left mid-read) — leaving
            # it as-is would corrupt every subsequent command on this
            # connection. Reconnect so the caller's next call starts clean.
            logger.warning(f"[{self.host}] Command failed to complete — "
                            f"reconnecting session to clear it")
            try:
                self.reconnect()
            except Exception as reconnect_err:
                logger.warning(f"[{self.host}] Reconnect after failed command "
                                f"also failed: {reconnect_err}")
            if isinstance(e, ReadTimeout):
                shown = command if len(command) <= 100 else command[:100] + "…"
                msg = (f"no shell prompt within {timeout}s "
                       f"(waited {elapsed:.1f}s) running: {shown}")
                # netmiko checks its deadline only after each read and throws
                # away that final read, so a loop that ran long can discard a
                # finished command's output (and prompt) and report a timeout.
                if elapsed > timeout + 2:
                    msg += (f" — the read loop overran the limit by "
                            f"{elapsed - timeout:.1f}s; output that arrived in "
                            f"that last read was discarded")
                raise SSHCommandTimeout(msg) from e
            raise
        # repr() so control/escape sequences from the remote shell (e.g. an
        # OSC prompt marker) show up instead of being invisible in the log.
        if logger.isEnabledFor(logging.DEBUG):
            shown = output[:self.DEBUG_OUTPUT_CHARS]
            more  = (f" …[+{len(output) - len(shown)} chars]"
                     if len(output) > len(shown) else "")
            logger.debug(f"[{self.host}] Output ({len(output)} chars, "
                         f"{time.monotonic() - started:.1f}s): {shown!r}{more}")
        return output

    def run_background(self, command: str) -> None:
        if not self._connection:
            raise SSHConnectionError("Not connected")
        bg_command = f"nohup {command} > {self.BG_LOG_PATH} 2>&1 &"
        logger.debug(f"[{self.host}] Background launch: {command}")
        self._connection.send_command(bg_command, expect_string=r"\$", read_timeout=10)

    def read_background_log(self, timeout: int = 5) -> str:
        """Read back whatever the most recent run_background() command has
        written so far — the only way to tell a backgrounded command ever
        actually ran, since run_background() itself doesn't wait for it."""
        return self.run(f"cat {self.BG_LOG_PATH} 2>/dev/null || true", timeout=timeout)

    def kill_background(self, process_name: str = "iperf3") -> None:
        try:
            self.run(f"pkill -f {process_name} || true", timeout=10)
            logger.debug(f"[{self.host}] Killed background process: {process_name}")
        except Exception:
            pass


@contextmanager
def ssh_connection(host: str, username: str, port: int = 22,
                   password: str = "", key_file: str = "",
                   timeout: int = 30):
    mgr = SSHManager(
        host=host, username=username, port=port,
        password=password, key_file=key_file, timeout=timeout
    )
    try:
        mgr.connect()
        yield mgr
    finally:
        mgr.disconnect()

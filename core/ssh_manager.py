"""
ssh_manager.py
Manages SSH connections to agents via Netmiko.
"""

import logging
import os
import time
import getpass
from contextlib import contextmanager
from typing import Optional

from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

logger = logging.getLogger(__name__)


class SSHConnectionError(Exception):
    pass


class SSHManager:

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

    def disconnect(self):
        if self._connection:
            try:
                self._connection.disconnect()
            except Exception:
                pass
            self._connection = None
            logger.debug(f"SSH disconnected from {self.host}")

    def run(self, command: str, timeout: int = 120) -> str:
        if not self._connection:
            raise SSHConnectionError("Not connected — call connect() first")
        logger.debug(f"[{self.host}] Running: {command}")
        output = self._connection.send_command(
            command,
            read_timeout=timeout,
            expect_string=r"\$",
        )
        logger.debug(f"[{self.host}] Output ({len(output)} chars): {output[:200]}")
        return output

    def run_background(self, command: str) -> None:
        if not self._connection:
            raise SSHConnectionError("Not connected")
        bg_command = f"nohup {command} > /tmp/nettest_bg.log 2>&1 &"
        logger.debug(f"[{self.host}] Background launch: {command}")
        self._connection.send_command(bg_command, expect_string=r"\$", read_timeout=10)

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

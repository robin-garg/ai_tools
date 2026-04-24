"""SSH tunnel helper.

Opens a local port-forward to a remote Redis endpoint using the system `ssh`
command so that ~/.ssh/config (host aliases, IdentityFile, Keychain) is
honoured transparently. Returns a context manager that tears the tunnel down
on exit.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass


DEFAULT_LOCAL_PORT = 6380
READY_TIMEOUT_SECONDS = 10.0


@dataclass
class Tunnel:
    local_host: str
    local_port: int


def _wait_until_ready(host: str, port: int, timeout: float) -> None:
    """Poll a TCP port until it accepts connections or the timeout elapses."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(0.1)
    raise TimeoutError(
        f"SSH tunnel did not become ready on {host}:{port} within {timeout}s"
        f" (last error: {last_err})"
    )


@contextlib.contextmanager
def ssh_tunnel(
    ssh_alias: str,
    remote_host: str,
    remote_port: int,
    local_port: int = DEFAULT_LOCAL_PORT,
    local_host: str = "127.0.0.1",
) -> Iterator[Tunnel]:
    """Open an SSH tunnel and yield a `Tunnel` describing the local endpoint.

    The tunnel is opened with `ssh -N -L <local>:<remote_host>:<remote_port>
    <alias>` as a foreground subprocess; it is terminated on context exit.
    """
    cmd = [
        "ssh",
        "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-L", f"{local_host}:{local_port}:{remote_host}:{remote_port}",
        ssh_alias,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_until_ready(local_host, local_port, READY_TIMEOUT_SECONDS)
        yield Tunnel(local_host=local_host, local_port=local_port)
    except Exception:
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(f"ssh exited early: {stderr.strip()}") from None
        raise
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

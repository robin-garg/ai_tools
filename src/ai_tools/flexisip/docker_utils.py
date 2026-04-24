"""Discover Docker containers on a remote Flexisip server.

Flexisip deploys two containers per host — `flexisip-proxy` (call routing,
SIP traffic, registrations) and `flexisip-regevent` (registration events).
Container names can drift across deployments, so this module resolves them
dynamically by listing `docker ps` over SSH.

All commands require `sudo -n` (passwordless sudo must be configured for
`docker` on the remote host).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ai_tools.flexisip.servers import Server


@dataclass(frozen=True)
class Container:
    name: str
    image: str
    status: str


def _ssh_run(ssh_alias: str, remote_cmd: str, timeout: float = 15.0) -> str:
    """Run `remote_cmd` over SSH and return stdout (raises on non-zero exit)."""
    result = subprocess.run(
        ["ssh", ssh_alias, remote_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ssh {ssh_alias} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def list_containers(server: Server) -> list[Container]:
    """Return all running containers on `server`."""
    out = _ssh_run(
        server.ssh_alias,
        'sudo -n docker ps --format "{{.Names}}|{{.Image}}|{{.Status}}"',
    )
    containers: list[Container] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        containers.append(Container(name=parts[0], image=parts[1], status=parts[2]))
    return containers


def find_proxy_container(server: Server) -> Container:
    """Return the `flexisip-proxy` container, resolving by substring if needed."""
    containers = list_containers(server)
    # Prefer exact match, then substring match on 'proxy' (but not 'regevent').
    for c in containers:
        if c.name == "flexisip-proxy":
            return c
    for c in containers:
        if "proxy" in c.name and "regevent" not in c.name:
            return c
    names = ", ".join(c.name for c in containers) or "<none>"
    raise RuntimeError(
        f"Could not find flexisip-proxy container on {server.name}. Running: {names}"
    )

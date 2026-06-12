"""Manage iptables DROP rules on a remote Flexisip server via SSH.

Rules are inserted at position 1 of the chosen chain (default INPUT) so they
take effect immediately, ahead of any existing ACCEPT rules.

The remote host must allow passwordless ``sudo`` for the ``iptables`` command
(the same requirement that exists for ``docker`` commands elsewhere in this
package).
"""

from __future__ import annotations

import subprocess

from ai_tools.flexisip.servers import Server


def block_ip(
    server: Server,
    ip: str,
    *,
    port: int = 5060,
    proto: str = "udp",
    chain: str = "FORWARD",
    dry_run: bool = False,
) -> str:
    """Insert an iptables DROP rule for *ip* on the remote server.

    Args:
        server:  Target :class:`~ai_tools.flexisip.servers.Server`.
        ip:      Source IP address to block.
        port:    Destination port to match (default ``5060``, standard SIP).
        proto:   Layer-4 protocol: ``"udp"`` or ``"tcp"`` (default ``"udp"``).
        chain:   iptables chain name (default ``"INPUT"``).
        dry_run: If ``True``, return the command string without executing it.

    Returns:
        The iptables command string that was (or would be) executed.

    Raises:
        RuntimeError: If the SSH command exits with a non-zero return code.
    """
    cmd = (
        f"sudo iptables -I {chain} 1 -s {ip}"
        f" -p {proto} --dport {port} -j DROP"
    )

    if dry_run:
        return f"[dry-run] ssh {server.ssh_alias} '{cmd}'"

    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", server.ssh_alias, cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"iptables failed on {server.name}: {result.stderr.strip()}"
        )
    return cmd


def unblock_ip(
    server: Server,
    ip: str,
    *,
    port: int = 5060,
    proto: str = "udp",
    chain: str = "FORWARD",
    dry_run: bool = False,
) -> str:
    """Remove an iptables DROP rule for *ip* on the remote server.

    Uses ``iptables -D`` (delete by rule specification) so no line-number
    lookup is required.  The arguments must match those used when the rule
    was originally inserted with :func:`block_ip`.

    Args:
        server:  Target :class:`~ai_tools.flexisip.servers.Server`.
        ip:      Source IP address to unblock.
        port:    Destination port that was matched (default ``5060``).
        proto:   Layer-4 protocol: ``"udp"`` or ``"tcp"`` (default ``"udp"``).
        chain:   iptables chain name (default ``"INPUT"``).
        dry_run: If ``True``, return the command string without executing it.

    Returns:
        The iptables command string that was (or would be) executed.

    Raises:
        RuntimeError: If the SSH command exits with a non-zero return code
            (e.g. the rule does not exist).
    """
    cmd = (
        f"sudo iptables -D {chain} -s {ip}"
        f" -p {proto} --dport {port} -j DROP"
    )

    if dry_run:
        return f"[dry-run] ssh {server.ssh_alias} '{cmd}'"

    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", server.ssh_alias, cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"iptables unblock failed on {server.name}: {result.stderr.strip()}"
        )
    return cmd


def list_rules(
    server: Server,
    chain: str = "FORWARD",
) -> str:
    """Return the current iptables rules for *chain* on the remote server.

    Args:
        server: Target server.
        chain:  iptables chain to inspect (default ``"INPUT"``).

    Returns:
        Raw text output of ``sudo iptables -L <chain> -n --line-numbers``.

    Raises:
        RuntimeError: If the SSH command exits with a non-zero return code.
    """
    cmd = f"sudo iptables -L {chain} -n --line-numbers"
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", server.ssh_alias, cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"iptables list failed on {server.name}: {result.stderr.strip()}"
        )
    return result.stdout

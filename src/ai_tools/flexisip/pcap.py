"""Live SIP packet capture on a remote Flexisip server via tcpdump.

Runs ``sudo tcpdump`` over SSH, streams each line to a caller-supplied
``on_line`` callback in real-time, and returns all collected lines when the
capture ends (via Ctrl-C or natural termination).

Usage pattern
-------------
::

    from ai_tools.flexisip.pcap import run_pcap
    from ai_tools.flexisip.servers import get_server

    srv = get_server("prod2")
    lines, pkt_count = run_pcap(srv, source_hosts=["162.252.248.230"],
                                 on_line=print)

Notes
-----
- Requires passwordless ``sudo`` for ``tcpdump`` on the remote host (same
  requirement as the existing ``docker`` / ``iptables`` helpers).
- The output uses ``tcpdump -A`` (ASCII), which renders SIP message bodies
  cleanly without a binary pcap file or Wireshark.
- ``-l`` (line-buffered) is essential so lines arrive immediately through SSH
  rather than being held in a kernel buffer.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from ai_tools.flexisip.servers import Server

# Matches a tcpdump packet-header line, e.g.:
#   07:07:39.232056 IP 162.252.251.67.11000 > 10.5.10.77.5060: ...
_RE_PKT_HEADER = re.compile(
    r"^\d{2}:\d{2}:\d{2}\.\d+ IP[6]?\s+\S+ > \S+"
)


def _build_bpf(port: int, source_hosts: list[str]) -> str:
    """Return a BPF filter expression for SIP traffic."""
    port_part = f"port {port}"
    if not source_hosts:
        return port_part
    if len(source_hosts) == 1:
        host_part = f"host {source_hosts[0]}"
    else:
        inner = " or ".join(f"host {h}" for h in source_hosts)
        host_part = f"({inner})"
    return f"{host_part} and {port_part}"


def run_pcap(
    server: Server,
    *,
    iface: str = "any",
    port: int = 5060,
    source_hosts: list[str] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[list[str], int]:
    """Stream a live tcpdump capture from *server* until interrupted.

    Args:
        server:       Target Flexisip server (SSH alias is used).
        iface:        Network interface to capture on (default ``any``).
        port:         SIP port to filter on (default ``5060``).
        source_hosts: Optional list of source IPs to narrow the capture
                      (e.g. Kazoo's IPs).  Empty list → capture all hosts.
        on_line:      Callback invoked with each line as it arrives.
                      Typically ``click.echo`` or ``print``.

    Returns:
        ``(lines, packet_count)`` where *lines* is the full captured text
        split by line and *packet_count* is the number of distinct SIP
        packets seen (counted by tcpdump packet-header lines).

    Raises:
        ``KeyboardInterrupt`` is caught internally; the process is terminated
        cleanly and the collected lines are returned.
    """
    bpf = _build_bpf(port, source_hosts or [])
    # -l  line-buffered (flushes each line immediately through SSH)
    # -n  no reverse-DNS lookups
    # -A  ASCII dump (renders SIP headers cleanly)
    # -s 0  full packet capture (no truncation)
    remote_cmd = f"sudo -n tcpdump -i {iface} -n -l -A -s 0 '{bpf}'"

    proc = subprocess.Popen(
        ["ssh", server.ssh_alias, remote_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # merge stderr so "listening on any" banner appears
        text=True,
        bufsize=1,
    )

    lines: list[str] = []
    packet_count = 0

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if _RE_PKT_HEADER.match(line):
                packet_count += 1
            if on_line:
                on_line(line)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return lines, packet_count

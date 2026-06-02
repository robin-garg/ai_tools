"""Detect SIP flood traffic from a specific User-Agent in Flexisip proxy logs.

Parses raw Flexisip proxy log text and identifies source IPs sending high
volumes of SIP messages with a given User-Agent string (e.g. "SIPVOIP").

Typical SIP block in the log
-----------------------------
  2026-05-05 12:00:00:123 ...
  REGISTER sip:domain.com SIP/2.0
  Via: SIP/2.0/UDP 203.0.113.5:5060;branch=z9hG4bK...;received=198.51.100.7
  User-Agent: SIPVOIP/2.0
  ...

The source IP is taken from the ``received=`` parameter of the first Via
header (the actual remote address after NAT), falling back to the Via host if
``received=`` is absent.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ai_tools.flexisip.log_utils import split_blocks_text


# --------------------------------------------------------------------------- #
# Compiled regexes (local to this module)                                      #
# --------------------------------------------------------------------------- #

# User-Agent header value (first occurrence in the block)
_RE_UA = re.compile(r"(?m)^User-Agent:\s*(.+)")

# First Via header: captures host and the remainder of the header line
# so we can look for the received= parameter.
_RE_VIA = re.compile(
    r"(?mi)^Via:\s*SIP/2\.0/\w+\s+([\d.]+)(?::\d+)?([^\r\n]*)"
)

# received= parameter inside a Via header
_RE_RECEIVED = re.compile(r";received=([\d.]+)")


# --------------------------------------------------------------------------- #
# Data model                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class AgentHit:
    """One source IP's traffic summary for a given User-Agent."""
    ip: str
    agent: str    # representative User-Agent string seen from this IP
    count: int    # number of SIP message blocks from this IP


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _source_ip(block: str) -> str | None:
    """Return the true source IP from the first Via header in a SIP block."""
    m = _RE_VIA.search(block)
    if not m:
        return None
    via_host = m.group(1)
    via_rest = m.group(2) or ""
    recv = _RE_RECEIVED.search(via_rest)
    return recv.group(1) if recv else via_host


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def parse_agent_traffic(
    log_text: str,
    agent_filter: str = "",
) -> list[AgentHit]:
    """Parse *log_text* and return per-IP SIP message counts.

    Args:
        log_text:     Raw Flexisip proxy log text.
        agent_filter: Case-insensitive substring match on the User-Agent value.
                      Empty string matches every block that has a User-Agent.

    Returns:
        List of :class:`AgentHit` sorted by ``count`` descending.
    """
    counts: Counter[str] = Counter()
    ip_agent: dict[str, str] = {}
    filter_lower = agent_filter.lower()

    for block in split_blocks_text(log_text):
        ua_m = _RE_UA.search(block)
        if not ua_m:
            continue
        agent = ua_m.group(1).strip()
        if filter_lower and filter_lower not in agent.lower():
            continue
        ip = _source_ip(block)
        if not ip:
            continue
        counts[ip] += 1
        ip_agent.setdefault(ip, agent)

    return sorted(
        [
            AgentHit(ip=ip, agent=ip_agent[ip], count=cnt)
            for ip, cnt in counts.items()
        ],
        key=lambda h: h.count,
        reverse=True,
    )

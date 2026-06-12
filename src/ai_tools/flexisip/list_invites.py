"""List inbound SIP INVITEs received by Flexisip in a time window.

Useful as a first triage step when investigating failed calls: it answers
"did the INVITE actually reach Flexisip?" by enumerating every
``Receiving new Request SIP message INVITE`` block in the proxy log between
``start`` and ``end`` (UTC).

Each entry exposes the key SIP headers needed to decide which call to drill
into further (Call-ID, From, To, CSeq, User-Agent, source contact IP).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ai_tools.flexisip.log_extractor import extract
from ai_tools.flexisip.log_utils import split_blocks
from ai_tools.flexisip.servers import Server
from ai_tools.flexisip.sip_patterns import (
    _CALL_ID_RE,
    _CSEQ_RE,
    _INVITE_FROM_RE,
)

# Inbound-INVITE marker — only the proxy's "Receiving new Request SIP message
# INVITE" line counts as a fresh INVITE arrival. Other lines mention "INVITE"
# (nta: received INVITE, Sending Request, retransmissions, …) but they are not
# new arrivals and would lead to duplicates.
_INVITE_MARKER = "Receiving new Request SIP message INVITE"

# Request-URI line that immediately follows the marker, e.g.
#   INVITE sip:99f36bd2d4db@13.216.83.86:5060;... SIP/2.0
_REQUEST_URI_RE = re.compile(r"^INVITE\s+(sip:\S+)\s+SIP/2\.0", re.MULTILINE)
_FROM_HDR_RE    = re.compile(r"^From:\s+(.+)$", re.MULTILINE)
_TO_HDR_RE      = re.compile(r"^To:\s+(.+)$", re.MULTILINE)
_USER_AGENT_RE  = re.compile(r"^User-Agent:\s+(.+)$", re.MULTILINE)
_CONTACT_RE     = re.compile(r"^Contact:\s+<sip:[^@]+@([0-9.]+):(\d+)", re.MULTILINE)


@dataclass
class InviteEntry:
    timestamp: str              # YYYY-MM-DD HH:MM:SS:mmm (UTC, as logged)
    call_id: str
    from_user: str              # SIP user from "INVITE from sip:USER@..."
    from_header: str            # full From header value
    to_header: str              # full To header value
    request_uri: str            # Request-URI of the INVITE (callee target)
    cseq: str                   # "<n> INVITE"
    user_agent: str
    source_ip_port: str         # Contact header "ip:port", caller's hop


def _strip_cr(line: str) -> str:
    return line.rstrip("\r")


def _first(pat: re.Pattern[str], block: str, default: str = "") -> str:
    m = pat.search(block)
    return _strip_cr(m.group(1)) if m else default


def parse_invite_block(timestamp: str, block: str) -> Optional[InviteEntry]:
    """Parse a single Flexisip log block into an :class:`InviteEntry`.

    Returns ``None`` when the block does not look like a fresh inbound INVITE
    (missing marker or no Call-ID — both indicate a non-INVITE block).
    """
    if _INVITE_MARKER not in block:
        return None
    cm = _CALL_ID_RE.search(block)
    if not cm:
        return None

    fm = _INVITE_FROM_RE.search(block)
    from_user = fm.group(1) if fm else ""

    cseq_m = _CSEQ_RE.search(block)
    cseq = f"{cseq_m.group(1)} INVITE" if cseq_m else ""

    contact_m = _CONTACT_RE.search(block)
    source = f"{contact_m.group(1)}:{contact_m.group(2)}" if contact_m else ""

    return InviteEntry(
        timestamp=timestamp,
        call_id=cm.group(1),
        from_user=from_user,
        from_header=_first(_FROM_HDR_RE, block),
        to_header=_first(_TO_HDR_RE, block),
        request_uri=_first(_REQUEST_URI_RE, block),
        cseq=cseq,
        user_agent=_first(_USER_AGENT_RE, block),
        source_ip_port=source,
    )


def list_invites(
    server: Server,
    *,
    start: str,
    end: str,
    container: str = "",
    from_filter: str = "",
    to_filter: str = "",
    log_path: Optional[str] = None,
) -> list[InviteEntry]:
    """Return all inbound INVITEs received by ``server`` in [start, end] UTC.

    Args:
        server:       Flexisip :class:`Server` to query.
        start, end:   UTC bounds 'YYYY-MM-DD HH:MM:SS' — both required so the
                      remote ``awk`` scan stays bounded.
        container:    Docker/podman container running the proxy. Only used when
                      ``server.proxy_log_in_container`` is True; pass "" for
                      host-mounted logs (stg2b, prod2).
        from_filter:  Optional case-insensitive substring filter on the SIP
                      user extracted from the INVITE's From URI.
        to_filter:    Optional case-insensitive substring filter on the
                      Request-URI (the callee target).
        log_path:     Optional override for the proxy log path on the server
                      (e.g. a rotated ``flexisip-proxy.log-YYYYMMDD.gz`` file).
                      ``.gz`` paths are streamed through ``zcat`` automatically.
    """
    if not start or not end:
        raise ValueError("list_invites requires both 'start' and 'end' UTC bounds")

    log_text = extract(
        server,
        container,
        pattern=_INVITE_MARKER,
        start=start,
        end=end,
        log_path=log_path,
    )
    entries: list[InviteEntry] = []
    for ts, block in split_blocks(log_text):
        entry = parse_invite_block(ts, block)
        if entry is None:
            continue
        if from_filter and from_filter.lower() not in entry.from_user.lower():
            continue
        if to_filter and to_filter.lower() not in entry.request_uri.lower():
            continue
        entries.append(entry)

    entries.sort(key=lambda e: e.timestamp)
    return entries

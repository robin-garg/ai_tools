"""Flexisip MCP Server — diagnostic and management tools.

Exposes Flexisip CLI functionality as MCP tools using FastMCP so that any
MCP-compatible AI assistant can call them directly with typed parameters.

Transport: stdio (launched as a subprocess by the AI client).

Available tools
---------------
Phase 1 — read-only diagnostics
  list_registrations_tool     List all active SIP registrations on a server.
  get_user_registration       Get registration details for a specific SIP user.
  count_registrations         Count active registrations (optionally filtered).
  extract_proxy_logs          Extract proxy log blocks matching filters.
  detect_sip_flood            Detect IPs flooding with a specific User-Agent.
  analyze_reg_gaps            Generate a registration-gap PDF report for a user.
  list_call_invites           List inbound SIP INVITEs in a UTC time window.

Phase 2 — management and analysis
  block_ip_tool               Block an IP address via remote iptables.
  unblock_ip_tool             Unblock an IP address via remote iptables.
  list_blocked_ips            List current iptables DROP rules on a server.
  analyze_call_records        Analyse call records CSV and produce a PDF report.
  analyze_expiration_notifier Analyse ContactExpirationNotifier push activity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fastmcp

from ai_tools.flexisip.call_records_analyzer import (
    analyze_call_records as _analyze_call_records,
)
from ai_tools.flexisip.docker_utils import find_proxy_container
from ai_tools.flexisip.expiration_notifier import parse_notifier_log
from ai_tools.flexisip.iptables import block_ip, list_rules, unblock_ip
from ai_tools.flexisip.list_invites import list_invites as _list_invites
from ai_tools.flexisip.log_extractor import extract
from ai_tools.flexisip.real_registration import get_real_registration as _get_real_registration
from ai_tools.flexisip.reg_gap_analysis import analyze_reg_gaps as _analyze_reg_gaps
from ai_tools.flexisip.registrar import connect, count_keys, list_registrations
from ai_tools.flexisip.servers import SERVERS, get_server
from ai_tools.flexisip.sip_agent_analyzer import parse_agent_traffic

mcp = fastmcp.FastMCP(
    "flexisip-mcp",
    instructions=(
        "Flexisip diagnostic tools. All tools connect to remote Flexisip servers "
        "via SSH. Valid server names: " + ", ".join(sorted(SERVERS)) + ". "
        "Production servers (prod, prod2) are READ-ONLY — never mutate them. "
        "All timestamps are UTC in 'YYYY-MM-DD HH:MM:SS' format."
    ),
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _reg_to_dict(r: Any) -> dict[str, Any]:
    """Convert a Registration dataclass to a JSON-serialisable dict."""
    return {
        "redis_key": r.redis_key,
        "user": r.user,
        "domain": r.domain,
        "platform": r.platform,
        "ip": r.ip,
        "port": r.port,
        "updated_utc": r.updated_utc.isoformat(),
        "mins_ago": r.mins_ago,
        "mins_ago_label": r.mins_ago_label,
    }


# ---------------------------------------------------------------------------
# Tool: list_registrations
# ---------------------------------------------------------------------------

@mcp.tool()
def list_registrations_tool(
    server: str,
    platform: str = "",
    domain: str = "",
) -> dict[str, Any]:
    """List all active SIP registrations on a Flexisip server.

    Args:
        server:   Server name (stg2, stg2b, prod, prod2).
        platform: Optional filter — 'iOS', 'Android', or '' for all.
        domain:   Optional domain substring filter (e.g. 'pinkreseller').

    Returns:
        Dict with 'server', 'total', and 'registrations' list.
    """
    srv = get_server(server)
    with connect(srv) as client:
        regs = list_registrations(client)

    if platform:
        regs = [r for r in regs if r.platform.lower() == platform.lower()]
    if domain:
        regs = [r for r in regs if domain.lower() in r.domain.lower()]

    return {
        "server": server,
        "total": len(regs),
        "registrations": [_reg_to_dict(r) for r in regs],
    }


# ---------------------------------------------------------------------------
# Tool: get_user_registration
# ---------------------------------------------------------------------------

@mcp.tool()
def get_user_registration(
    server: str,
    username: str,
) -> dict[str, Any]:
    """Get registration details for a specific SIP user.

    Args:
        server:   Server name (stg2, stg2b, prod, prod2).
        username: SIP username or AOR fragment (e.g. '762cbe93429d').

    Returns:
        Dict with 'server', 'username', 'found', and 'registrations' list.
    """
    srv = get_server(server)
    with connect(srv) as client:
        regs = list_registrations(client, pattern=f"fs:{username}*")

    return {
        "server": server,
        "username": username,
        "found": len(regs),
        "registrations": [_reg_to_dict(r) for r in regs],
    }


# ---------------------------------------------------------------------------
# Tool: count_registrations
# ---------------------------------------------------------------------------

@mcp.tool()
def count_registrations(
    server: str,
    pattern: str = "fs:*",
) -> dict[str, Any]:
    """Count active SIP registration keys on a server.

    Args:
        server:  Server name (stg2, stg2b, prod, prod2).
        pattern: Redis key pattern (default 'fs:*' counts all registrations).

    Returns:
        Dict with 'server', 'pattern', and 'count'.
    """
    srv = get_server(server)
    with connect(srv) as client:
        total = count_keys(client, pattern)
    return {"server": server, "pattern": pattern, "count": total}


# ---------------------------------------------------------------------------
# Tool: extract_proxy_logs
# ---------------------------------------------------------------------------

@mcp.tool()
def extract_proxy_logs(
    server: str,
    pattern: str = "",
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    """Extract Flexisip proxy log blocks matching filters.

    The AWK extractor preserves full multi-line SIP message blocks so that
    push-notification payloads, routing decisions, and continuation lines
    are not lost.

    Args:
        server:  Server name (stg2, stg2b, prod, prod2).
        pattern: Regex pattern to match inside log blocks (e.g. a Call-ID or username).
                 Empty string returns all blocks in the time window.
        start:   UTC window start — 'YYYY-MM-DD HH:MM:SS'. Empty means no lower bound.
        end:     UTC window end   — 'YYYY-MM-DD HH:MM:SS'. Empty means no upper bound.

    Returns:
        Dict with 'server', 'container', 'lines', and 'content' (raw log text).
    """
    srv = get_server(server)
    container = ""
    if srv.proxy_log_in_container:
        container = find_proxy_container(srv)

    content = extract(srv, container, pattern=pattern, start=start, end=end)
    return {
        "server": server,
        "container": container,
        "lines": content.count("\n"),
        "content": content,
    }


# ---------------------------------------------------------------------------
# Tool: detect_sip_flood
# ---------------------------------------------------------------------------

@mcp.tool()
def detect_sip_flood(
    server: str,
    agent: str = "SIPVOIP",
    min_count: int = 1,
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    """Detect IPs flooding a Flexisip server with a specific User-Agent.

    Fetches proxy logs and counts SIP messages per source IP.  Only read-only
    detection — this tool never blocks any IP.

    Args:
        server:    Server name (stg2, stg2b, prod, prod2).
        agent:     User-Agent substring to filter (case-insensitive). Default 'SIPVOIP'.
        min_count: Only report IPs with at least this many messages.
        start:     UTC window start — 'YYYY-MM-DD HH:MM:SS'. Empty means no lower bound.
        end:       UTC window end   — 'YYYY-MM-DD HH:MM:SS'. Empty means no upper bound.

    Returns:
        Dict with 'server', 'agent_filter', 'total_offenders', and 'hits' list
        (each hit has 'ip', 'agent', 'count').
    """
    srv = get_server(server)
    container = ""
    if srv.proxy_log_in_container:
        container = find_proxy_container(srv)

    log_text = extract(srv, container, pattern=agent, start=start, end=end)
    hits = parse_agent_traffic(log_text, agent_filter=agent)
    hits = [h for h in hits if h.count >= min_count]

    return {
        "server": server,
        "agent_filter": agent,
        "total_offenders": len(hits),
        "hits": [{"ip": h.ip, "agent": h.agent, "count": h.count} for h in hits],
    }


# ---------------------------------------------------------------------------
# Tool: analyze_reg_gaps
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_reg_gaps(
    server: str,
    username: str,
    hours: int = 24,
) -> dict[str, Any]:
    """Generate a registration-gap PDF report for a SIP user.

    Fetches live event logs from the server, identifies gaps in registration
    continuity, and saves a colour-coded PDF to reports/registration/<date>/.

    Args:
        server:   Server name (stg2, stg2b, prod, prod2).
        username: SIP username or AOR fragment (e.g. '762cbe93429d').
        hours:    Look-back window in hours. Use 0 for all available history.

    Returns:
        Dict with 'server', 'username', 'hours', and 'pdf_path'.
    """
    srv = get_server(server)
    pdf_path = _analyze_reg_gaps(srv, username, hours=hours)
    return {
        "server": server,
        "username": username,
        "hours": hours,
        "pdf_path": str(pdf_path),
    }


# ---------------------------------------------------------------------------
# Tool: block_ip
# ---------------------------------------------------------------------------

@mcp.tool()
def block_ip_tool(
    server: str,
    ip: str,
    port: int = 5060,
    proto: str = "udp",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Block an IP address on a Flexisip server using iptables.

    Args:
        server:  Server name (stg2, stg2b, prod, prod2).
        ip:      The IP address to block.
        port:    Destination port (default 5060).
        proto:   Protocol ('udp' or 'tcp', default 'udp').
        dry_run: If True (default), only print the command without executing.

    Returns:
        Dict with 'server', 'ip', 'command', and 'status'.
    """
    srv = get_server(server)
    res = block_ip(srv, ip, port=port, proto=proto, dry_run=dry_run)
    return {
        "server": server,
        "ip": ip,
        "command": res,
        "status": "dry-run" if dry_run else "executed",
    }


# ---------------------------------------------------------------------------
# Tool: unblock_ip
# ---------------------------------------------------------------------------

@mcp.tool()
def unblock_ip_tool(
    server: str,
    ip: str,
    port: int = 5060,
    proto: str = "udp",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Unblock an IP address on a Flexisip server using iptables.

    Args:
        server:  Server name (stg2, stg2b, prod, prod2).
        ip:      The IP address to unblock.
        port:    Destination port (default 5060).
        proto:   Protocol ('udp' or 'tcp', default 'udp').
        dry_run: If True (default), only print the command without executing.

    Returns:
        Dict with 'server', 'ip', 'command', and 'status'.
    """
    srv = get_server(server)
    res = unblock_ip(srv, ip, port=port, proto=proto, dry_run=dry_run)
    return {
        "server": server,
        "ip": ip,
        "command": res,
        "status": "dry-run" if dry_run else "executed",
    }


# ---------------------------------------------------------------------------
# Tool: list_blocked_ips
# ---------------------------------------------------------------------------

@mcp.tool()
def list_blocked_ips(
    server: str,
    chain: str = "FORWARD",
) -> dict[str, Any]:
    """List currently blocked IPs on a Flexisip server.

    Args:
        server: Server name (stg2, stg2b, prod, prod2).
        chain:  iptables chain to inspect (default 'FORWARD').

    Returns:
        Dict with 'server', 'chain', and 'rules' (raw iptables -L output).
    """
    srv = get_server(server)
    rules = list_rules(srv, chain=chain)
    return {"server": server, "chain": chain, "rules": rules}


# ---------------------------------------------------------------------------
# Tool: analyze_call_records
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_call_records(
    server: str,
    csv_path: str,
    save_logs: bool = False,
    batch_window_mins: int = 30,
) -> dict[str, Any]:
    """Analyse VoIP call records from a CSV and generate a PDF report.

    Full pipeline: parse CSV → batch log fetch → per-call SIP flow analysis → PDF.

    Args:
        server:            Server name (stg2, stg2b, prod, prod2).
        csv_path:          Path to the call records CSV file.
        save_logs:         If True, persist extracted logs to logs/.
        batch_window_mins: Group calls within this many minutes into one log fetch batch.

    Returns:
        Dict with 'server', 'csv_path', and 'pdf_path'.
    """
    srv = get_server(server)
    container = ""
    if srv.proxy_log_in_container:
        container = find_proxy_container(srv)

    path = _analyze_call_records(
        csv_path=Path(csv_path),
        server=srv,
        container=container,
        save_logs=save_logs,
        batch_window_mins=batch_window_mins,
    )
    return {
        "server": server,
        "csv_path": csv_path,
        "pdf_path": str(path),
    }


# ---------------------------------------------------------------------------
# Tool: list_call_invites
# ---------------------------------------------------------------------------

@mcp.tool()
def list_call_invites(
    server: str,
    start: str,
    end: str,
    from_user: str = "",
    to_user: str = "",
    log_path: str = "",
) -> dict[str, Any]:
    """List inbound SIP INVITEs received by Flexisip in a UTC time window.

    First-line triage for failed-call investigations: enumerates every
    ``Receiving new Request SIP message INVITE`` block between ``start``
    and ``end`` so the caller can decide whether each call reached the
    proxy and which Call-ID to drill into further.

    Args:
        server:    Server name (stg2, stg2b, prod, prod2).
        start:     UTC window start — 'YYYY-MM-DD HH:MM:SS' (required).
        end:       UTC window end   — 'YYYY-MM-DD HH:MM:SS' (required).
        from_user: Optional case-insensitive substring filter on the SIP
                   user in the INVITE's From URI.
        to_user:   Optional case-insensitive substring filter on the
                   INVITE Request-URI (the callee target).
        log_path:  Optional override for the proxy log path on the server
                   (e.g. a rotated 'flexisip-proxy.log-YYYYMMDD.gz' file).
                   '.gz' paths are auto-streamed through zcat.

    Returns:
        Dict with 'server', 'window', 'total', and 'invites' list. Each
        invite has: timestamp, call_id, from_user, from_header, to_header,
        request_uri, cseq, user_agent, source_ip_port.
    """
    srv = get_server(server)
    container = ""
    if srv.proxy_log_in_container:
        container = find_proxy_container(srv).name

    entries = _list_invites(
        srv,
        container=container,
        start=start,
        end=end,
        from_filter=from_user,
        to_filter=to_user,
        log_path=log_path or None,
    )

    return {
        "server": server,
        "window": {"start": start, "end": end},
        "total": len(entries),
        "invites": [
            {
                "timestamp": e.timestamp,
                "call_id": e.call_id,
                "from_user": e.from_user,
                "from_header": e.from_header,
                "to_header": e.to_header,
                "request_uri": e.request_uri,
                "cseq": e.cseq,
                "user_agent": e.user_agent,
                "source_ip_port": e.source_ip_port,
            }
            for e in entries
        ],
    }


# ---------------------------------------------------------------------------
# Tool: analyze_expiration_notifier
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_expiration_notifier(
    server: str,
    user: str = "",
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    """Analyse ContactExpirationNotifier push activity from proxy logs.

    Args:
        server: Server name (stg2, stg2b, prod, prod2).
        user:   Optional SIP username filter.
        start:  UTC window start — 'YYYY-MM-DD HH:MM:SS'. Empty means no lower bound.
        end:    UTC window end   — 'YYYY-MM-DD HH:MM:SS'. Empty means no upper bound.

    Returns:
        Dict with 'server', 'total_runs', 'total_push_attempts', 'total_errors',
        and 'runs' list (each run has timestamp, counts, and per-device events).
    """
    srv = get_server(server)
    container = ""
    if srv.proxy_log_in_container:
        container = find_proxy_container(srv)

    content = extract(
        srv,
        container,
        pattern="ContactExpirationNotifier",
        start=start,
        end=end,
    )
    runs = parse_notifier_log(content, user_filter=user)

    res_runs = []
    total_push = 0
    total_errors = 0

    for r in runs:
        run_dict = {
            "timestamp": r.timestamp,
            "checking_count": r.checking_count,
            "extended_count": r.extended_count,
            "push_count": r.push_count,
            "error_count": r.error_count,
            "events": [
                {
                    "ts": e.timestamp,
                    "user": e.user,
                    "device": e.device_id,
                    "provider": e.provider,
                    "success": e.success,
                    "status": e.http_status,
                    "error": e.error_message,
                }
                for e in r.push_events
            ],
        }
        res_runs.append(run_dict)
        total_push += r.push_count
        total_errors += r.error_count

    return {
        "server": server,
        "total_runs": len(runs),
        "total_push_attempts": total_push,
        "total_errors": total_errors,
        "runs": res_runs,
    }


# ---------------------------------------------------------------------------
# Tool: get_real_registration
# ---------------------------------------------------------------------------

@mcp.tool()
def get_real_registration_tool(
    server: str,
    username: str,
    domain: str,
) -> dict[str, Any]:
    """Get the last *real* SIP registration for a user from the event log.

    The event log is written ONLY when an actual SIP REGISTER is received from
    the device.  The ContactExpirationNotifier never writes to it — it only
    extends the Redis TTL.  This tool compares the event-log timestamp against
    the Redis ``updatedAt`` to tell you whether the current Redis binding is a
    genuine device registration or a server-side extension.

    Args:
        server:   Server name (stg2, stg2b, prod, prod2).
        username: SIP username (e.g. '5e62ee261327').
        domain:   Full SIP domain (e.g. 'testing.voip.pinkreseller.com').

    Returns:
        Dict with:
          real_registered_utc / real_registered_ist — last real REGISTER time
          real_ip / real_port                        — device address at that time
          redis_updated_utc / redis_updated_ist      — current Redis updatedAt
          redis_ip / redis_port                      — address stored in Redis now
          registration_type                          — 'real' | 'extended' | 'no_redis'
          gap_secs                                   — redis_updated - real_registered (secs)
    """
    srv = get_server(server)
    result = _get_real_registration(srv, username, domain)
    if result is None:
        return {
            "server": server,
            "username": username,
            "domain": domain,
            "error": "No event-log entry found for today. User may not have registered today.",
        }
    return {
        "server": server,
        "username": result.username,
        "domain": result.domain,
        "real_registered_utc": result.real_registered_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "real_registered_ist": result.real_registered_ist.strftime("%Y-%m-%d %H:%M:%S"),
        "real_ip": result.real_ip,
        "real_port": result.real_port,
        "redis_updated_utc": (
            result.redis_updated_utc.strftime("%Y-%m-%d %H:%M:%S")
            if result.redis_updated_utc else None
        ),
        "redis_updated_ist": (
            result.redis_updated_ist.strftime("%Y-%m-%d %H:%M:%S")
            if result.redis_updated_ist else None
        ),
        "redis_ip": result.redis_ip,
        "redis_port": result.redis_port,
        "registration_type": result.registration_type,
        "gap_secs": result.gap_secs,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

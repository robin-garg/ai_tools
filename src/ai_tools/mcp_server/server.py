"""Flexisip MCP Server — Phase 1 read-only diagnostic tools.

Exposes Flexisip CLI functionality as MCP tools using FastMCP so that any
MCP-compatible AI assistant can call them directly with typed parameters.

Transport: stdio (launched as a subprocess by the AI client).

Available tools
---------------
list_registrations      List all active SIP registrations on a server.
get_user_registration   Get registration details for a specific SIP user.
count_registrations     Count active registrations (optionally filtered).
extract_proxy_logs      Extract proxy log blocks matching filters.
detect_sip_flood        Detect IPs flooding with a specific User-Agent.
analyze_reg_gaps        Generate a registration-gap PDF report for a user.
"""

from __future__ import annotations

from typing import Any

import fastmcp

from ai_tools.flexisip.docker_utils import find_proxy_container
from ai_tools.flexisip.log_extractor import extract
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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

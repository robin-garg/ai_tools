"""Command-line interface for the Flexisip registration tools.

Designed for simple, low-noise output suitable for both human reading and AI
analysis. No colour or box-drawing; each record is printed as aligned
`key: value` lines with blank separators between records.
"""

from __future__ import annotations

import re
import statistics
import sys
from collections import Counter
from pathlib import Path

from datetime import datetime, timezone, timedelta

import click

from ai_tools.flexisip.docker_utils import find_proxy_container, list_containers
from ai_tools.flexisip.expiration_notifier import parse_notifier_log
from ai_tools.flexisip.log_extractor import (
    extract_and_save,
    extract_event_logs,
    save_to_logs_dir,
)
from ai_tools.flexisip.registrar import (
    connect,
    count_keys,
    get_entry,
    list_registrations,
    sample_entries,
)
from ai_tools.flexisip.servers import SERVERS, get_server

IST = timezone(timedelta(hours=5, minutes=30))


SERVER_CHOICE = click.Choice(sorted(SERVERS), case_sensitive=False)


def _format_entry(key: str, entry: dict[str, str] | None) -> str:
    if entry is None:
        return f"key: {key}\n  (missing)"
    lines = [f"key: {key}"]
    if "__type__" in entry:
        lines.append(f"  type: {entry['__type__']} (non-hash key, skipped)")
        return "\n".join(lines)
    width = max((len(f) for f in entry), default=0)
    for field, value in entry.items():
        lines.append(f"  {field.ljust(width)}  {value}")
    return "\n".join(lines)


@click.group()
def flexisip() -> None:
    """Inspect Flexisip registrations via Redis."""


@flexisip.command()
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.option(
    "--pattern", "-p",
    default="*",
    show_default=True,
    help="Redis key pattern to match.",
)
def count(server: str, pattern: str) -> None:
    """Count registration keys matching a pattern."""
    srv = get_server(server)
    click.echo(f"connecting to {srv.name} ({srv.redis_host})...", err=True)
    with connect(srv) as client:
        total = count_keys(client, pattern)
    click.echo(f"server:  {srv.name}")
    click.echo(f"pattern: {pattern}")
    click.echo(f"count:   {total}")


@flexisip.command()
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.option(
    "--pattern", "-p",
    default="*",
    show_default=True,
    help="Redis key pattern to match.",
)
@click.option(
    "-n", "--limit",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="Number of entries to show.",
)
def inspect(server: str, pattern: str, limit: int) -> None:
    """Show the raw HGETALL contents of the first N matching keys."""
    srv = get_server(server)
    click.echo(f"connecting to {srv.name} ({srv.redis_host})...", err=True)
    with connect(srv) as client:
        entries = sample_entries(client, limit=limit, pattern=pattern)
    if not entries:
        click.echo(f"no keys matched pattern '{pattern}' on {srv.name}")
        return
    click.echo(f"server: {srv.name}")
    click.echo(f"showing {len(entries)} of pattern '{pattern}':")
    click.echo("")
    for key, entry in entries:
        click.echo(_format_entry(key, entry))
        click.echo("")


@flexisip.command()
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.argument("key")
def show(server: str, key: str) -> None:
    """Show the HGETALL contents of a specific key."""
    srv = get_server(server)
    click.echo(f"connecting to {srv.name} ({srv.redis_host})...", err=True)
    with connect(srv) as client:
        entry = get_entry(client, key)
    if entry is None:
        click.echo(f"key not found: {key}", err=True)
        sys.exit(1)
    click.echo(_format_entry(key, entry))


def _sep(width: int = 100) -> None:
    click.echo("-" * width)


def _print_report(server_name: str, regs: list, filters: dict | None = None) -> None:
    now_utc = datetime.now(tz=timezone.utc)
    now_ist = now_utc.astimezone(IST)

    active = {k: v for k, v in (filters or {}).items() if v}
    filters_label = ("  |  Filters: " + "  ".join(f"{k}={v}" for k, v in active.items())) if active else ""

    click.echo("")
    click.echo(f"Registration Report  —  {server_name}{filters_label}")
    click.echo(f"Report time : {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
               f"  /  {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST")
    click.echo(f"Total       : {len(regs)} registered device(s)")
    _sep()

    if not regs:
        click.echo("No registrations found.")
        return

    # Column widths
    W_USER   = max(len(r.user)          for r in regs)
    W_DOM    = max(len(r.domain)        for r in regs)
    W_PLAT   = max(len(r.platform)      for r in regs)
    W_IP     = max(len(r.ip_port)       for r in regs)
    W_SINCE  = 14  # "X hr Y mins ago"

    # Header
    click.echo(
        f"{'User':<{W_USER}}  {'Domain':<{W_DOM}}  {'Platform':<{W_PLAT}}"
        f"  {'IP : Port':<{W_IP}}"
        f"  {'Updated (UTC)':<19}  {'Updated (IST)':<19}  Since"
    )
    _sep()

    for r in sorted(regs, key=lambda x: x.updated_utc, reverse=True):
        click.echo(
            f"{r.user:<{W_USER}}  {r.domain:<{W_DOM}}  {r.platform:<{W_PLAT}}"
            f"  {r.ip_port:<{W_IP}}"
            f"  {r.updated_utc.strftime('%Y-%m-%d %H:%M:%S')}"
            f"  {r.updated_ist.strftime('%Y-%m-%d %H:%M:%S')}"
            f"  {r.mins_ago_label}"
        )
    _sep()


@flexisip.command()
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.option(
    "--platform", "-pl",
    type=click.Choice(["ios", "android"], case_sensitive=False),
    default=None,
    help="Filter by device platform: ios or android.",
)
@click.option(
    "--domain", "-d",
    default=None,
    help="Filter by domain (substring match on first two domain labels, e.g. 'bdaprthirteen').",
)
@click.option(
    "--pattern", "-p",
    default="fs:*",
    show_default=True,
    help="Redis key pattern (advanced). Defaults to all registrations.",
)
def registrations(server: str, platform: str | None, domain: str | None, pattern: str) -> None:
    """Show a clean registration report for all registered devices.

    Use --platform to filter by iOS or Android.
    Use --domain to filter by domain name (partial match).
    Both filters can be combined.
    """
    srv = get_server(server)
    click.echo(f"connecting to {srv.name} ({srv.redis_host})...", err=True)

    with connect(srv) as client:
        regs = list_registrations(client, pattern=pattern)

    # Apply filters
    if platform:
        regs = [r for r in regs if r.platform.lower() == platform.lower()]
    if domain:
        regs = [r for r in regs if domain.lower() in r.domain.lower()]

    _print_report(srv.name, regs, filters={"platform": platform, "domain": domain})


@flexisip.command()
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.argument("username")
def user(server: str, username: str) -> None:
    """Show registration details for a specific USERNAME."""
    srv = get_server(server)
    click.echo(f"connecting to {srv.name} ({srv.redis_host})...", err=True)
    with connect(srv) as client:
        regs = list_registrations(client, pattern=f"fs:{username}*")
    if not regs:
        click.echo(f"No registration found for user '{username}' on {srv.name}", err=True)
        sys.exit(1)
    _print_report(srv.name, regs, filters={"user": username})


@flexisip.command()
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
def containers(server: str) -> None:
    """List running Docker containers on the target server."""
    srv = get_server(server)
    click.echo(f"listing containers on {srv.name}...", err=True)
    found = list_containers(srv)
    if not found:
        click.echo(f"No running containers on {srv.name}.")
        return
    w_name = max(len(c.name) for c in found)
    w_img = max(len(c.image) for c in found)
    click.echo(f"{'NAME':<{w_name}}  {'IMAGE':<{w_img}}  STATUS")
    _sep()
    for c in found:
        click.echo(f"{c.name:<{w_name}}  {c.image:<{w_img}}  {c.status}")


@flexisip.command()
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.option(
    "--call-id", "-c",
    "call_id",
    default=None,
    help="Filter logs by SIP Call-ID.",
)
@click.option(
    "--user", "-u",
    multiple=True,
    help="Filter logs by user extension (matched anywhere in the block). "
         "Repeat the flag to match multiple users, e.g. -u 49810 -u 1a97435c96ba.",
)
@click.option(
    "--start",
    default=None,
    help="Start timestamp, UTC, 'YYYY-MM-DD HH:MM:SS'.",
)
@click.option(
    "--end",
    default=None,
    help="End timestamp, UTC, 'YYYY-MM-DD HH:MM:SS'.",
)
@click.option(
    "--save/--no-save",
    default=False,
    help="Also persist the extracted logs to logs/<server>_<desc>_<utc>.log.",
)
def logs(
    server: str,
    call_id: str | None,
    user: tuple[str, ...],
    start: str | None,
    end: str | None,
    save: bool,
) -> None:
    """Extract Flexisip proxy call logs and print them to stdout.

    At least one filter (--call-id, --user, or --start/--end) must be given.
    --user may be repeated to match blocks mentioning any of the given users.
    Use --save to also write the logs to the logs/ directory.
    """
    if not any([call_id, user, start, end]):
        click.echo("Provide at least one of --call-id, --user, --start/--end.", err=True)
        sys.exit(2)

    srv = get_server(server)
    if srv.proxy_log_in_container:
        click.echo(f"resolving proxy container on {srv.name}...", err=True)
        container_name = find_proxy_container(srv).name
        click.echo(f"using container: {container_name}", err=True)
    else:
        container_name = ""
        click.echo(f"proxy log is on host filesystem ({srv.proxy_log_path})", err=True)

    if call_id:
        pattern = call_id
        descriptor = f"callid-{call_id[:12]}"
    elif user:
        # OR-match any of the provided users via awk/ERE alternation.
        pattern = "|".join(user)
        descriptor = (
            f"user-{user[0]}" if len(user) == 1
            else f"users-{user[0]}+{len(user) - 1}"
        )
    else:
        pattern = ""
        descriptor = f"range-{(start or 'open').replace(' ', 'T')}"

    click.echo("extracting logs...", err=True)
    result = extract_and_save(
        srv,
        container_name,
        descriptor=descriptor,
        pattern=pattern,
        start=start or "",
        end=end or "",
        save=save,
    )

    # Summary goes to stderr so stdout stays clean for piping / analysis.
    click.echo("", err=True)
    click.echo(f"Server    : {result.server}", err=True)
    click.echo(f"Container : {result.container}", err=True)
    active = {k: v for k, v in result.filters.items() if v}
    if active:
        click.echo("Filters   : " + "  ".join(f"{k}={v}" for k, v in active.items()), err=True)
    if result.filters.get("pattern"):
        click.echo(
            f"Blocks    : {result.total_blocks} total  "
            f"({result.matched_blocks} matched + "
            f"{result.total_blocks - result.matched_blocks} context)",
            err=True,
        )
    else:
        click.echo(f"Blocks    : {result.total_blocks}", err=True)
    if save:
        if result.saved_to is not None:
            click.echo(f"Saved to  : {result.saved_to}", err=True)
        else:
            click.echo("Saved to  : (nothing matched — no file written)", err=True)
    click.echo("", err=True)

    if result.content:
        click.echo(result.content, nl=False)
    else:
        click.echo("(no matching log blocks found)", err=True)


@flexisip.command("expiration-notifier")
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.option(
    "--user", "-u",
    "user_filter",
    default=None,
    help="Show only pushes for a specific user extension (substring match).",
)
@click.option(
    "--start",
    default=None,
    help="Start timestamp, UTC, 'YYYY-MM-DD HH:MM:SS'.",
)
@click.option(
    "--end",
    default=None,
    help="End timestamp, UTC, 'YYYY-MM-DD HH:MM:SS'.",
)
@click.option(
    "--save/--no-save",
    default=False,
    help="Also save the raw extracted logs to logs/<server>_<desc>_<utc>.log.",
)
def expiration_notifier(
    server: str,
    user_filter: str | None,
    start: str | None,
    end: str | None,
    save: bool,
) -> None:
    """Report ContactExpirationNotifier activity: push attempts and their status.

    Shows each notifier run in the requested time window, which users received
    a background push notification, the push provider (APNS / FCM), whether the
    provider accepted the request (HTTP 200), and any error details.
    """
    srv = get_server(server)
    if srv.proxy_log_in_container:
        click.echo(f"resolving proxy container on {srv.name}...", err=True)
        container_name = find_proxy_container(srv).name
        click.echo(f"using container: {container_name}", err=True)
    else:
        container_name = ""
        click.echo(f"proxy log is on host filesystem ({srv.proxy_log_path})", err=True)
    click.echo("extracting ContactExpirationNotifier logs...", err=True)

    result = extract_and_save(
        srv,
        container_name,
        descriptor="expiration-notifier",
        pattern="ContactExpirationNotifier",
        start=start or "",
        end=end or "",
        save=save,
    )

    if save and result.saved_to:
        click.echo(f"Saved to  : {result.saved_to}", err=True)

    if not result.content:
        click.echo("No ContactExpirationNotifier log entries found.", err=True)
        return

    runs = parse_notifier_log(result.content, user_filter=user_filter or "")

    # ------------------------------------------------------------------ #
    # Output                                                               #
    # ------------------------------------------------------------------ #
    now_utc = datetime.now(tz=timezone.utc)
    now_ist = now_utc.astimezone(IST)

    click.echo("")
    click.echo(f"ContactExpirationNotifier Report  —  {srv.name}")
    click.echo(f"Report time : {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
               f"  /  {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST")
    if user_filter:
        click.echo(f"User filter : {user_filter}")
    if start or end:
        click.echo(f"Window      : {start or '(open)'} → {end or '(open)'} UTC")
    click.echo(f"Notifier runs found : {len(runs)}")
    _sep()

    if not runs:
        click.echo("No notifier runs found in the log window.")
        return

    for run in runs:
        pushes = run.push_events
        status_icon = "✓" if run.error_count == 0 else "✗"
        click.echo(
            f"[{status_icon}] Run @ {run.timestamp} UTC"
            f"  |  checking={run.checking_count}"
            f"  extended={run.extended_count}"
            f"  pushes={run.push_count}"
            + (f"  errors={run.error_count}" if run.error_count else "")
        )
        if not pushes:
            click.echo("     (no pushes sent in this run)")
        for ev in pushes:
            provider_label = ev.provider.upper()
            if ev.http_status is not None:
                http_part = f"  HTTP={ev.http_status}"
            else:
                http_part = ""
            if ev.success:
                result_label = "SUCCESS"
            else:
                result_label = f"FAILED  {ev.error_message or ''}"
            click.echo(
                f"     {'✓' if ev.success else '✗'} {ev.timestamp}"
                f"  [{provider_label}]"
                f"  user={ev.user}"
                f"  device={ev.device_id}"
                f"{http_part}"
                f"  → {result_label}"
            )
        _sep()

    total_pushes = sum(r.push_count for r in runs)
    total_errors = sum(r.error_count for r in runs)
    click.echo(
        f"Summary : {len(runs)} runs  |  {total_pushes} total pushes"
        + (f"  |  {total_errors} errors" if total_errors else "  |  all successful")
    )


@flexisip.command("agent-flood")
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.option(
    "--agent", "-a",
    default="SIPVOIP",
    show_default=True,
    help="User-Agent substring to filter on (case-insensitive).",
)
@click.option(
    "--min-count", "-m",
    default=1,
    show_default=True,
    type=int,
    help="Only report IPs with at least this many SIP messages.",
)
@click.option(
    "--block/--no-block",
    default=False,
    help="Apply iptables DROP rules for all reported IPs.",
)
@click.option(
    "--port",
    default=5060,
    show_default=True,
    type=int,
    help="Destination port for the iptables rule.",
)
@click.option(
    "--proto",
    default="udp",
    show_default=True,
    type=click.Choice(["udp", "tcp"], case_sensitive=False),
    help="Protocol for the iptables rule.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Print iptables commands without executing them.",
)
@click.option(
    "--start",
    default=None,
    help="Start timestamp, UTC, 'YYYY-MM-DD HH:MM:SS'.",
)
@click.option(
    "--end",
    default=None,
    help="End timestamp, UTC, 'YYYY-MM-DD HH:MM:SS'.",
)
@click.option(
    "--save/--no-save",
    default=False,
    help="Also save raw extracted logs to logs/.",
)
def agent_flood(
    server: str,
    agent: str,
    min_count: int,
    block: bool,
    port: int,
    proto: str,
    dry_run: bool,
    start: str | None,
    end: str | None,
    save: bool,
) -> None:
    """Detect and optionally block SIP flood traffic by User-Agent.

    Extracts proxy logs matching the given User-Agent pattern, counts SIP
    messages per source IP, and optionally inserts iptables DROP rules for
    each offending IP on the remote server.

    \b
    Examples:
      # Just report — which IPs are flooding with SIPVOIP?
      ai-tools flexisip agent-flood -s stg2b

      # Report and block (live iptables rules applied on the server)
      ai-tools flexisip agent-flood -s stg2b --block

      # Dry-run — see the iptables commands without running them
      ai-tools flexisip agent-flood -s stg2b --block --dry-run
    """
    from ai_tools.flexisip.sip_agent_analyzer import parse_agent_traffic
    from ai_tools.flexisip.iptables import block_ip as apply_block

    srv = get_server(server)
    if srv.proxy_log_in_container:
        click.echo(f"resolving proxy container on {srv.name}...", err=True)
        container_name = find_proxy_container(srv).name
        click.echo(f"using container: {container_name}", err=True)
    else:
        container_name = ""
        click.echo(f"proxy log is on host filesystem ({srv.proxy_log_path})", err=True)
    click.echo(f"extracting logs matching User-Agent '{agent}'...", err=True)

    result = extract_and_save(
        srv,
        container_name,
        descriptor=f"agent-flood-{agent.lower()}",
        pattern=f"User-Agent.*{agent}",
        start=start or "",
        end=end or "",
        save=save,
    )

    if save and result.saved_to:
        click.echo(f"Saved to  : {result.saved_to}", err=True)

    if not result.content:
        click.echo(f"No log entries found matching User-Agent '{agent}'.", err=True)
        return

    hits = parse_agent_traffic(result.content, agent_filter=agent)
    hits = [h for h in hits if h.count >= min_count]

    # ── Report ───────────────────────────────────────────────────────────────
    now_utc = datetime.now(tz=timezone.utc)
    now_ist = now_utc.astimezone(IST)

    click.echo("")
    click.echo(f"SIP Agent Flood Report  —  {srv.name}")
    click.echo(
        f"Report time : {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        f"  /  {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST"
    )
    click.echo(f"Agent filter: {agent}")
    if start or end:
        click.echo(f"Window      : {start or '(open)'} → {end or '(open)'} UTC")
    click.echo(f"Unique IPs  : {len(hits)}")
    _sep()

    if not hits:
        click.echo(f"No SIP messages found for User-Agent '{agent}'.")
        return

    w_ip = max(len(h.ip) for h in hits)
    click.echo(f"{'Source IP':<{w_ip}}  {'Msgs':>7}  User-Agent")
    _sep()
    for h in hits:
        click.echo(f"{h.ip:<{w_ip}}  {h.count:>7}  {h.agent}")
    _sep()
    total = sum(h.count for h in hits)
    click.echo(f"Total SIP messages: {total}  across {len(hits)} source IP(s)")

    # ── Block ────────────────────────────────────────────────────────────────
    if not block and not dry_run:
        return

    click.echo("")
    if dry_run:
        click.echo("Dry-run — iptables commands that would be applied:")
    else:
        click.echo(f"Applying iptables DROP rules on {srv.name}...")
    _sep()

    blocked = 0
    failed = 0
    for h in hits:
        try:
            cmd = apply_block(srv, h.ip, port=port, proto=proto, dry_run=dry_run)
            label = "[dry-run]" if dry_run else "✓ blocked"
            click.echo(f"  {label}  {h.ip}  ({h.count} msgs)  →  {cmd}")
            blocked += 1
        except RuntimeError as exc:
            click.echo(f"  ✗ FAILED  {h.ip}:  {exc}", err=True)
            failed += 1

    _sep()
    if dry_run:
        click.echo(f"Dry-run complete: {blocked} rule(s) would be applied.")
    else:
        click.echo(
            f"Done: {blocked} IP(s) blocked"
            + (f"  |  {failed} failed" if failed else "")
        )


@flexisip.command("call-records")
@click.option(
    "--csv", "-f", "csv_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the call records CSV file (EDT timestamps, To, From columns required).",
)
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target Flexisip server (stg2 or stg2b).",
)
@click.option(
    "--container", "-c",
    default=None,
    help="Docker container name running the proxy.  Auto-detected if omitted.",
)
@click.option(
    "--output", "-o", "output_pdf",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output PDF path.  Defaults to reports/call_records_analysis_<utc-stamp>.pdf.",
)
@click.option(
    "--save/--no-save",
    default=False,
    help="Also persist each raw log batch to the logs/ directory.",
)
@click.option(
    "--window", "batch_window_mins",
    default=30,
    show_default=True,
    type=int,
    help="Max minutes to group calls into one log fetch batch.",
)
@click.option(
    "--buffer", "buffer_mins",
    default=2,
    show_default=True,
    type=int,
    help="Buffer minutes before/after the actual call span in each batch window.",
)
@click.option(
    "--tolerance", "tolerance_mins",
    default=3,
    show_default=True,
    type=int,
    help="Max minutes allowed between the CSV call time and the log INVITE timestamp.",
)
def call_records(
    csv_path: Path,
    server: str,
    container: str | None,
    output_pdf: Path | None,
    save: bool,
    batch_window_mins: int,
    buffer_mins: int,
    tolerance_mins: int,
) -> None:
    """Fetch Flexisip proxy logs for CSV call records and produce a PDF call-flow analysis.

    \b
    The CSV must have at minimum these columns (case-sensitive):
      Call Date/Time   — "May 04 2026 08:53:57 PM"  (EDT, UTC−4)
      To               — callee extension / user ID
      From             — caller extension / user ID

    \b
    Batching strategy:
      • Calls within --window minutes of the first call in a group share one log fetch.
      • The actual fetch window = (first_call − buffer) … (last_call + buffer).
      • A single-call group uses only the per-call buffer on each side.
    """
    from ai_tools.flexisip.call_records_analyzer import analyze_call_records
    from ai_tools.flexisip.docker_utils import find_proxy_container

    srv = get_server(server)

    if container:
        container_name = container
    elif srv.proxy_log_in_container:
        click.echo(f"resolving proxy container on {srv.name}...", err=True)
        container_name = find_proxy_container(srv).name
        click.echo(f"using container: {container_name}", err=True)
    else:
        container_name = ""
        click.echo(f"proxy log is on host filesystem ({srv.proxy_log_path})", err=True)

    click.echo(f"parsing {csv_path.name}...", err=True)

    try:
        pdf_path = analyze_call_records(
            csv_path,
            srv,
            container_name,
            output_pdf=output_pdf,
            save_logs=save,
            batch_window_mins=batch_window_mins,
            buffer_mins=buffer_mins,
            tolerance_mins=tolerance_mins,
            progress_cb=lambda msg: click.echo(f"  {msg}", err=True),
        )
        click.echo("", err=True)
        click.echo(f"PDF report saved to: {pdf_path}", err=True)
        click.echo(str(pdf_path))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# ─── event-logs helpers ────────────────────────────────────────────────────

_REGEVENT_PAT = re.compile(
    r"^\w+\s+(\w+)\s+(\d+)\s+(\d{2}:\d{2}:\d{2})\s+(\d{4}):\s+(\w+)\s+<([^>]+)>"
)


def _parse_event_ts(line: str) -> datetime | None:
    """Parse the syslog-style timestamp from a Flexisip event-log line (UTC)."""
    m = _REGEVENT_PAT.match(line)
    if not m:
        return None
    mon, day, tm, yr, _verb, _aor = m.groups()
    try:
        return datetime.strptime(
            f"{mon} {day} {yr} {tm}", "%b %d %Y %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_gap(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec:02d}s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s"


@flexisip.command("event-logs")
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target server (stg2 or stg2b).",
)
@click.option(
    "--user", "-u", "username",
    required=True,
    help="SIP username to filter (matched as ERE pattern).",
)
@click.option(
    "--hours",
    default=24,
    show_default=True,
    type=int,
    help="Fetch events from the past N hours (ignored when --start is set).",
)
@click.option(
    "--start",
    default=None,
    help="Start timestamp UTC 'YYYY-MM-DD HH:MM:SS' (overrides --hours).",
)
@click.option(
    "--end",
    default=None,
    help="End timestamp UTC 'YYYY-MM-DD HH:MM:SS' (defaults to now).",
)
@click.option(
    "--log-path",
    default=None,
    help=(
        "Path to event-log file or directory on the remote host. "
        "Defaults to the server's configured event_log_path (see servers.py)."
    ),
)
@click.option(
    "--save/--no-save",
    default=False,
    help="Also save the filtered events to logs/<server>_regevent-<user>_<utc>.log.",
)
def event_logs(
    server: str,
    username: str,
    hours: int,
    start: str | None,
    end: str | None,
    log_path: str | None,
    save: bool,
) -> None:
    """Fetch and analyse Flexisip event-logs for a user from the host.

    Reads /var/log/flexisip/event-logs directly on the remote host (no Docker).
    Logs are always fetched live from the server.
    Filters by user and time window, then shows a registration gap analysis.
    """
    srv = get_server(server)
    now_utc = datetime.now(tz=timezone.utc)

    window_start = (
        datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if start
        else now_utc - timedelta(hours=hours)
    )
    window_end = (
        datetime.strptime(end, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if end
        else now_utc
    )

    click.echo(
        f"fetching event-logs for '{username}' on {srv.name} "
        f"({window_start.strftime('%Y-%m-%d %H:%M:%S')} → "
        f"{window_end.strftime('%Y-%m-%d %H:%M:%S')} UTC)...",
        err=True,
    )

    raw = extract_event_logs(srv, log_path=log_path, pattern=username)

    # Parse lines and apply time window filter
    events: list[tuple[datetime, str, str]] = []  # (ts, verb, raw_line)
    for line in raw.splitlines():
        ts = _parse_event_ts(line)
        if ts is None:
            continue
        if ts < window_start or ts > window_end:
            continue
        m = _REGEVENT_PAT.match(line)
        verb = m.group(5) if m else "?"
        events.append((ts, verb, line))
    events.sort(key=lambda e: e[0])

    if save and events:
        content = "\n".join(e[2] for e in events) + "\n"
        saved = save_to_logs_dir(content, srv.name, f"regevent-{username}")
        click.echo(f"Saved to  : {saved}", err=True)

    # ── Header ────────────────────────────────────────────────────────────
    now_ist = now_utc.astimezone(IST)
    click.echo("")
    click.echo(f"Event-Log Report  —  {srv.name}")
    click.echo(
        f"Report time  : {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        f"  /  {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST"
    )
    click.echo(f"User         : {username}")
    click.echo(
        f"Window       : {window_start.strftime('%Y-%m-%d %H:%M:%S')}"
        f" → {window_end.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    click.echo(f"Total events : {len(events)}")
    _sep()

    if not events:
        click.echo("No events found in the requested time window.")
        return

    # ── Event timeline ────────────────────────────────────────────────────
    for ts, verb, line in events:
        ist = ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
        # Extract the contact/detail part after the AOR
        detail = line.split(">", 1)[-1].strip().lstrip("(").rstrip(")")
        click.echo(
            f"[{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC  /  {ist} IST]"
            f"  {verb:<12}  {detail[:80]}"
        )
    _sep()

    # ── Gap analysis on Registered events ─────────────────────────────────
    reg_times = sorted(ts for ts, verb, _ in events if verb.lower() == "registered")
    if len(reg_times) < 2:
        click.echo(f"Only {len(reg_times)} Registered event(s) in window — no gap analysis.")
        return

    diffs = [
        (reg_times[i + 1] - reg_times[i]).total_seconds()
        for i in range(len(reg_times) - 1)
    ]
    buckets: Counter[str] = Counter()
    for d in diffs:
        if d < 2:
            buckets["< 2s (burst)"] += 1
        elif d < 10:
            buckets["2-10s"] += 1
        elif d < 60:
            buckets["10-60s"] += 1
        elif d < 300:
            buckets["1-5 min"] += 1
        elif d < 1200:
            buckets["5-20 min"] += 1
        elif d < 2400:
            buckets["20-40 min"] += 1
        elif d < 3600:
            buckets["40-60 min"] += 1
        else:
            buckets["> 60 min"] += 1

    click.echo("")
    click.echo("=== Registration Gap Summary ===")
    click.echo(f"  Registered events : {len(reg_times)}")
    click.echo(f"  Min               : {_fmt_gap(min(diffs))}")
    click.echo(f"  Median            : {_fmt_gap(statistics.median(diffs))}")
    click.echo(f"  Mean              : {_fmt_gap(statistics.mean(diffs))}")
    click.echo(f"  Max               : {_fmt_gap(max(diffs))}")
    click.echo("")
    click.echo("=== Gap Distribution ===")
    for k in ["< 2s (burst)", "2-10s", "10-60s", "1-5 min",
               "5-20 min", "20-40 min", "40-60 min", "> 60 min"]:
        if buckets[k]:
            click.echo(f"  {k:<14}  {buckets[k]:>3}  {'#' * buckets[k]}")


@flexisip.command("call-flow")
@click.option(
    "--log-file", "-l",
    "log_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Extracted Flexisip proxy log file to analyse.",
)
@click.option(
    "--caller", "-c",
    required=True,
    help="SIP username of the caller (e.g. 230).",
)
@click.option(
    "--callee", "-e",
    required=True,
    help="SIP username of the callee (e.g. 902493a6d16b).",
)
@click.option(
    "--start",
    default=None,
    help="Start timestamp UTC 'YYYY-MM-DD HH:MM:SS' to filter INVITEs.",
)
@click.option(
    "--end",
    default=None,
    help="End timestamp UTC 'YYYY-MM-DD HH:MM:SS' to filter INVITEs.",
)
@click.option(
    "--output-pdf", "-o",
    "output_pdf",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Destination PDF path (default: reports/call_flow_<caller>_to_<callee>_<ts>.pdf).",
)
@click.option(
    "--output-md",
    "output_md",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Destination Markdown path (default: reports/call_flow_<caller>_to_<callee>_<ts>.md).",
)
def call_flow(
    log_file: Path,
    caller: str,
    callee: str,
    start: str | None,
    end: str | None,
    output_pdf: Path | None,
    output_md: Path | None,
) -> None:
    """Analyse a Flexisip proxy log and produce a per-call timeline report.

    Finds every INVITE from CALLER to CALLEE in the log (optionally filtered
    by --start / --end), builds a detailed per-call event timeline (INVITE,
    Redis lookup, FCM push, re-REGISTER, 180 Ringing, CANCEL, 487, …) and
    saves a Markdown report and a PDF.

    \b
    Example:
        ai-tools flexisip call-flow \\
            --log-file logs/stg2b_calls_230_…log \\
            --caller 230 --callee 902493a6d16b \\
            --start "2026-05-06 15:33:00" --end "2026-05-06 15:48:00"
    """
    from ai_tools.flexisip.call_flow_analyzer import analyze_and_render

    click.echo(f"analysing {log_file.name} for {caller} → {callee}...", err=True)
    if start or end:
        click.echo(f"  window: {start or '(open)'} → {end or '(open)'} UTC", err=True)

    try:
        md_path, pdf_path = analyze_and_render(
            log_file=log_file,
            caller=caller,
            callee=callee,
            start=start or "",
            end=end or "",
            output_pdf=output_pdf,
            output_md=output_md,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Markdown : {md_path}", err=True)
    click.echo(f"PDF      : {pdf_path}", err=True)
    click.echo(str(pdf_path))


@flexisip.command("pcap")
@click.option(
    "--server", "-s",
    type=SERVER_CHOICE,
    required=True,
    help="Target Flexisip server.",
)
@click.option(
    "--source", "-H",
    "source_hosts",
    multiple=True,
    help=(
        "Source IP(s) to filter on (e.g. Kazoo's IP). "
        "Repeat for multiple IPs: -H 162.252.248.230 -H 162.252.251.67. "
        "Omit to capture ALL SIP traffic on the port."
    ),
)
@click.option(
    "--port", "-p",
    default=5060,
    show_default=True,
    type=int,
    help="SIP port to capture on.",
)
@click.option(
    "--iface", "-i",
    default="any",
    show_default=True,
    help="Network interface on the remote host.",
)
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Save captured output to logs/<server>_pcap_<utc>.log on stop.",
)
def pcap_cmd(
    server: str,
    source_hosts: tuple[str, ...],
    port: int,
    iface: str,
    save: bool,
) -> None:
    """Capture live SIP packets on the remote host. Press Ctrl+C to stop.

    Runs ``sudo tcpdump`` over SSH and streams every SIP packet to the
    terminal in real-time.  When you press Ctrl+C the capture stops and the
    full output is saved to the logs/ directory.

    \b
    Typical workflow:
      1.  Run this command.
      2.  Make the call from Kazoo.
      3.  Press Ctrl+C.
      4.  Inspect the saved log file to see whether the INVITE arrived.

    \b
    Examples:
      # Capture all SIP on prod2 (broad — use when Kazoo IP is unknown)
      ai-tools flexisip pcap -s prod2

      # Narrow to Kazoo's two IPs (less noise)
      ai-tools flexisip pcap -s prod2 -H 162.252.248.230 -H 162.252.251.67
    """
    from ai_tools.flexisip.pcap import run_pcap
    from ai_tools.flexisip.log_extractor import save_to_logs_dir

    srv = get_server(server)
    hosts_list = list(source_hosts)

    # ── Header ────────────────────────────────────────────────────────────────
    click.echo("")
    click.echo(f"SIP Packet Capture  —  {srv.name}")
    click.echo(f"Interface : {iface}   Port : {port}")
    if hosts_list:
        click.echo(f"Filter    : {' | '.join(hosts_list)}")
    else:
        click.echo("Filter    : ALL hosts (no source IP filter)")
    click.echo("Press Ctrl+C to stop the capture.")
    click.echo("-" * 70)

    # ── Capture ───────────────────────────────────────────────────────────────
    lines, pkt_count = run_pcap(
        srv,
        iface=iface,
        port=port,
        source_hosts=hosts_list,
        on_line=click.echo,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    click.echo("-" * 70)
    click.echo(f"Capture stopped.  Packets seen: {pkt_count}  |  Lines: {len(lines)}")

    if not lines:
        click.echo("(nothing captured)", err=True)
        return

    if save:
        content = "\n".join(lines) + "\n"
        saved = save_to_logs_dir(content, srv.name, "pcap")
        click.echo(f"Saved to  : {saved}")
    else:
        click.echo("(--no-save: output not written to disk)")


# ─────────────────────────────────────────────────────────────────────────────

@click.group()
def cli() -> None:
    """ai-tools: personal tools for daily work."""


from ai_tools.pdf.cli import pdf  # noqa: E402

cli.add_command(flexisip)
cli.add_command(pdf)


if __name__ == "__main__":
    cli()

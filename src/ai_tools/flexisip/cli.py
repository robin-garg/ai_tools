"""Command-line interface for the Flexisip registration tools.

Designed for simple, low-noise output suitable for both human reading and AI
analysis. No colour or box-drawing; each record is printed as aligned
`key: value` lines with blank separators between records.
"""

from __future__ import annotations

import sys

from datetime import datetime, timezone, timedelta

import click

from ai_tools.flexisip.docker_utils import find_proxy_container, list_containers
from ai_tools.flexisip.log_extractor import extract_and_save
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
    W_DOM    = max(len(r.domain_short)  for r in regs)
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
            f"{r.user:<{W_USER}}  {r.domain_short:<{W_DOM}}  {r.platform:<{W_PLAT}}"
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
        regs = [r for r in regs if domain.lower() in r.domain_short.lower()]

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
    click.echo(f"resolving proxy container on {srv.name}...", err=True)
    container = find_proxy_container(srv)
    click.echo(f"using container: {container.name}", err=True)

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
        container.name,
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


@click.group()
def cli() -> None:
    """ai-tools: personal tools for daily work."""


cli.add_command(flexisip)


if __name__ == "__main__":
    cli()

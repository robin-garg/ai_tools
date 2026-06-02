"""Registration Gap Analysis — parse event logs and generate a markdown/PDF report.

Exposes the analysis logic used by the ``ai-tools flexisip reg-gap`` CLI command.
Can also be called directly by other tools that need registration gap data.

Public API
----------
parse_events(raw, username, hours)     → list[Event]
is_unreg(verb, ip)                     → bool
generate_reg_gap_markdown(...)         → str
analyze_reg_gaps(server, username, …)  → Path   (full pipeline → PDF)
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ai_tools.flexisip.log_extractor import extract_event_logs
from ai_tools.flexisip.log_utils import fmt_gap
from ai_tools.flexisip.servers import Server
from ai_tools.pdf.writer import markdown_to_pdf

UTC = timezone.utc

# Matches lines like:
#   Mon May 13 09:28:17 2026: Registered <user@domain> (sip:user@1.2.3.4:5060)
_EVENT_PAT = re.compile(
    r'^\w+\s+(\w+)\s+(\d+)\s+(\d{2}:\d{2}:\d{2})\s+(\d{4}):\s+(\w+)'
    r'\s+<([^>]+)>\s+\((sip:[^)]+)\)'
)
_IP_PAT = re.compile(r'@([0-9.]+):(\d+)')

# ── Types ─────────────────────────────────────────────────────────────────────
# (timestamp_utc, verb, ip_or_None, port_or_None)
Event = tuple[datetime, str, str | None, int | None]


# ── Log parsing ───────────────────────────────────────────────────────────────

def parse_events(raw: str, username: str, hours: int) -> list[Event]:
    """Parse raw event log text into a sorted list of :data:`Event` tuples.

    Args:
        raw:      Raw event-log text fetched from the server.
        username: Username or AOR fragment to filter (case-insensitive match).
        hours:    Look-back window in hours.  ``0`` means no time restriction.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours) if hours > 0 else None
    events: list[Event] = []
    for line in raw.splitlines():
        m = _EVENT_PAT.match(line.strip())
        if not m:
            continue
        mon, day, tm, yr, verb, aor, contact = m.groups()
        if username.lower() not in aor.lower():
            continue
        ts = datetime.strptime(f"{mon} {day} {yr} {tm}", "%b %d %Y %H:%M:%S").replace(tzinfo=UTC)
        if cutoff and ts < cutoff:
            continue
        mm = _IP_PAT.search(contact)
        ip   = mm.group(1)      if mm else None
        port = int(mm.group(2)) if mm else None
        events.append((ts, verb, ip, port))
    events.sort(key=lambda e: e[0])
    return events


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_unreg(verb: str, ip: str | None) -> bool:
    """Return True if this event is an UNREGISTER or a no-contact event."""
    return ip is None or verb.lower().startswith("unreg")


def _gap_label(secs: float) -> str:
    """Return a short gap category label for notable-event classification."""
    if secs == 0:
        return "duplicate"
    if secs < 120:
        return "rapid"
    if secs > 3600:
        return "expired"
    return "normal"


# ── Markdown report ───────────────────────────────────────────────────────────

def generate_reg_gap_markdown(
    events: list[Event],
    server_name: str,
    username: str,
    hours: int,
    event_log_path: str = "",
) -> str:
    """Build a Markdown registration-gap report from a list of :data:`Event` tuples."""
    now_utc = datetime.now(UTC)
    window_label = "All time" if hours == 0 else f"Last {hours}h"

    lines: list[str] = [
        "# SIP Registration Gap Analysis",
        "",
        f"**Server:** {server_name}  |  **Username:** {username}  |  **Window:** {window_label}",
    ]
    if event_log_path:
        lines.append(f"**Event log:** {event_log_path}")
    lines += [
        f"**Generated:** {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Period:** {events[0][0].strftime('%Y-%m-%d %H:%M:%S')} – "
        f"{events[-1][0].strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"**Total events:** {len(events)}",
        "",
        "---",
        "",
        "## Gap Legend",
        "",
        "| Gap | Meaning |",
        "|-----|---------|",
        "| < 2 min | ⚠️ Anomaly / rapid re-registration |",
        "| 2 – 60 min | ✅ Normal (within 60-min expiry window) |",
        "| > 60 min | 🔴 Expired — registration lapsed |",
        "| UNREGISTER | ❌ Explicit unregister / no-contact |",
        "",
        "## Registration Events",
        "",
        "| # | UTC Time | Verb | IP | Port | Gap from Previous |",
        "|---|---------|------|----|------|-------------------|",
    ]

    for i, (ts, verb, ip, port) in enumerate(events):
        unreg = is_unreg(verb, ip)
        if i == 0:
            gap_str = "— (first)"
        else:
            secs = (ts - events[i - 1][0]).total_seconds()
            gap_str = "0s [duplicate]" if secs == 0 else fmt_gap(secs)
            lbl = _gap_label(secs) if not unreg else "normal"
            if lbl == "expired":
                gap_str = f"🔴 {gap_str}"
            elif lbl == "rapid":
                gap_str = f"⚠️ {gap_str}"
        verb_disp = f"**{verb}**" if unreg else verb
        ip_disp   = "⊘ UNREGISTER" if unreg else (ip or "—")
        lines.append(
            f"| {i + 1} | {ts.strftime('%Y-%m-%d %H:%M:%S')} "
            f"| {verb_disp} | {ip_disp} | {port or '—'} | {gap_str} |"
        )

    # ── Notable events ────────────────────────────────────────────────────────
    notable: list[str] = []
    for i in range(1, len(events)):
        ts_prev, verb_prev, ip_prev, _ = events[i - 1]
        ts_curr, _, _, _               = events[i]
        g = (ts_curr - ts_prev).total_seconds()
        p_utc = ts_prev.strftime("%H:%M:%S UTC")
        c_utc = ts_curr.strftime("%H:%M:%S UTC")
        if is_unreg(verb_prev, ip_prev):
            notable.append(f"- **[UNREGISTER]** #{i} at {p_utc} → re-registered at {c_utc}, gap: {fmt_gap(g)}")
        elif g > 3600:
            notable.append(f"- 🔴 **[EXPIRED gap]** #{i}→#{i+1}: {p_utc} → {c_utc} — {fmt_gap(g)} (registration expired)")
        elif 0 < g < 120:
            notable.append(f"- ⚠️ **[Rapid re-reg]** #{i}→#{i+1}: {p_utc} → {c_utc} — {fmt_gap(g)} (network handoff)")
        elif g == 0:
            notable.append(f"- **[Duplicate]** #{i + 1}: {c_utc} — same second as previous")

    if notable:
        lines += ["", "## Notable Events", ""]
        lines.extend(notable)

    # ── Summary statistics ────────────────────────────────────────────────────
    reg_events = [ts for ts, verb, ip, _ in events if not is_unreg(verb, ip)]
    gaps_sec = [
        (reg_events[i] - reg_events[i - 1]).total_seconds()
        for i in range(1, len(reg_events))
    ]
    unreg_count = sum(1 for _, verb, ip, _ in events if is_unreg(verb, ip))

    lines += [
        "",
        "## Summary Statistics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total events | {len(events)} |",
        f"| Unreg / no-contact | {unreg_count} |",
    ]
    if gaps_sec:
        lines += [
            f"| Avg gap | {fmt_gap(statistics.mean(gaps_sec))} |",
            f"| Min gap | {fmt_gap(min(gaps_sec))} |",
            f"| Max gap | {fmt_gap(max(gaps_sec))} |",
            f"| Expired gaps (>60 min) | {sum(1 for g in gaps_sec if g > 3600)} |",
            f"| Rapid re-regs (<2 min) | {sum(1 for g in gaps_sec if 0 < g < 120)} |",
        ]
    lines.append("")
    return "\n".join(lines)


# ── Full pipeline ─────────────────────────────────────────────────────────────

def analyze_reg_gaps(
    server: Server,
    username: str,
    hours: int = 24,
    *,
    reports_dir: Path = Path("reports") / "registration",
    progress_cb: Optional[object] = None,
) -> Path:
    """Fetch event logs from the server, parse registration gaps, and render a PDF report.

    Logs are always fetched live from ``server.event_log_path`` over SSH.

    Args:
        server:      Flexisip :class:`~ai_tools.flexisip.servers.Server` to query.
        username:    Username or AOR fragment to filter.
        hours:       Look-back window in hours (``0`` = all time).
        reports_dir: Base directory for the auto-generated PDF.
                     Defaults to ``reports/registration/``.
        progress_cb: Optional ``callable(msg: str)`` for progress output.

    Returns:
        Path to the generated PDF file.
    """
    def _log(msg: str) -> None:
        if callable(progress_cb):
            progress_cb(msg)  # type: ignore[operator]

    _log(f"Fetching event logs for '{username}' from {server.name} ({server.event_log_path})...")
    raw = extract_event_logs(server, pattern=username)

    if not raw.strip():
        raise ValueError(f"No event logs found for '{username}' on {server.name}.")

    events = parse_events(raw, username, hours)
    if not events:
        window = "all time" if hours == 0 else f"last {hours}h"
        raise ValueError(f"No registration events for '{username}' in {window}.")

    _log(f"Found {len(events)} registration event(s).")

    md = generate_reg_gap_markdown(
        events,
        server_name=server.name,
        username=username,
        hours=hours,
        event_log_path=server.event_log_path,
    )

    now_utc = datetime.now(UTC)
    date_folder = reports_dir / now_utc.strftime("%Y-%m-%d")
    date_folder.mkdir(parents=True, exist_ok=True)
    stamp = now_utc.strftime("%Y%m%d_%H%M%S")
    output_pdf = date_folder / f"reg_gap_{server.name}_{username}_{stamp}.pdf"

    _log(f"Rendering PDF → {output_pdf}")
    return markdown_to_pdf(
        md,
        output_pdf,
        title=f"Registration Gap — {username} @ {server.name}",
        tz_label="UTC",
    )


"""Registration Gap Analysis — fetch live event logs and generate a PDF report.

Fetches registration event logs for a given username from any configured
Flexisip server (stg2, stg2b, prod) and produces a colour-coded PDF report
showing registration gaps, notable events, and summary statistics.

Usage:
    uv run python src/ai_tools/flexisip/reg_gap_analysis.py \\
        --server prod --username 762cbe93429d
    uv run python src/ai_tools/flexisip/reg_gap_analysis.py \\
        --server stg2 --username abc123 --hours 48
    uv run python src/ai_tools/flexisip/reg_gap_analysis.py \\
        --server stg2b --username abc123 --hours 0   # all time
"""

import argparse
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from ai_tools.flexisip.log_extractor import extract_event_logs
from ai_tools.flexisip.servers import get_server

IST = timedelta(hours=5, minutes=30)

# Matches lines like:
#   Mon May 13 09:28:17 2026: Registered <user@domain> (sip:user@1.2.3.4:5060)
_EVENT_PAT = re.compile(
    r'^\w+\s+(\w+)\s+(\d+)\s+(\d{2}:\d{2}:\d{2})\s+(\d{4}):\s+(\w+)'
    r'\s+<([^>]+)>\s+\((sip:[^)]+)\)'
)
_IP_PAT = re.compile(r'@([0-9.]+):(\d+)')

# ── Types ─────────────────────────────────────────────────────────────────────
# (timestamp, verb, ip_or_None, port_or_None)
Event = tuple[datetime, str, str | None, int | None]


# ── Argument parsing ──────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch live Flexisip event logs and generate a registration gap PDF report.",
    )
    p.add_argument("--server", required=True, choices=["stg2", "stg2b", "prod"],
                   help="Server to fetch event logs from.")
    p.add_argument("--username", required=True,
                   help="Username or AOR fragment to filter (e.g. 762cbe93429d).")
    p.add_argument("--hours", type=int, default=24,
                   help="Look-back window in hours. Use 0 for all available history. (default: 24)")
    p.add_argument("--output", default=None,
                   help="Output PDF path. Defaults to reports/<YYYY-MM-DD>/reg_gap_<server>_<user>_<ts>.pdf")
    return p.parse_args()


# ── Log parsing ───────────────────────────────────────────────────────────────
def _parse_events(raw: str, username: str, hours: int) -> list[Event]:
    """Parse raw event log text into a sorted list of Events."""
    cutoff = datetime.utcnow() - timedelta(hours=hours) if hours > 0 else None
    events: list[Event] = []
    for line in raw.splitlines():
        m = _EVENT_PAT.match(line.strip())
        if not m:
            continue
        mon, day, tm, yr, verb, aor, contact = m.groups()
        if username.lower() not in aor.lower():
            continue
        ts = datetime.strptime(f"{mon} {day} {yr} {tm}", "%b %d %Y %H:%M:%S")
        if cutoff and ts < cutoff:
            continue
        mm = _IP_PAT.search(contact)
        ip   = mm.group(1)      if mm else None
        port = int(mm.group(2)) if mm else None
        events.append((ts, verb, ip, port))
    events.sort(key=lambda e: e[0])
    return events


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt_gap(secs: float) -> str:
    secs = int(abs(secs))
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def _is_unreg(verb: str, ip: str | None) -> bool:
    return ip is None or verb.lower().startswith("unreg")


def _gap_color(secs: float):
    if secs < 120:
        return colors.HexColor("#FFAAAA")   # < 2 min: anomaly
    if secs <= 2400:
        return colors.HexColor("#AAFFAA")   # 2–40 min: normal
    return colors.HexColor("#FFE599")       # > 40 min: large gap


# ── PDF tables ────────────────────────────────────────────────────────────────
def _build_legend() -> Table:
    rows = [
        ["", "< 2 min",    "Anomaly / rapid re-registration (network switch or reconnect burst)"],
        ["", "2 – 40 min", "Normal periodic re-registration"],
        ["", "> 40 min",   "Large gap — device offline, backgrounded, or missed heartbeat"],
        ["", "Pink row",   "Explicit UNREGISTER / no-contact event"],
    ]
    t = Table(rows, colWidths=[0.5*cm, 2.5*cm, 13.3*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (1, 0), colors.HexColor("#FFAAAA")),
        ("BACKGROUND", (0, 1), (1, 1), colors.HexColor("#AAFFAA")),
        ("BACKGROUND", (0, 2), (1, 2), colors.HexColor("#FFE599")),
        ("BACKGROUND", (0, 3), (1, 3), colors.HexColor("#FFD6D6")),
        ("FONTSIZE",   (0, 0), (-1, -1), 8),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _build_events_table(events: list[Event]) -> Table:
    rows = [["#", "UTC Time", "IST Time", "Verb", "IP", "Port", "Gap from Previous"]]
    gap_style: list[tuple[int, object]] = []
    for i, (ts, verb, ip, port) in enumerate(events):
        ist_str = (ts + IST).strftime("%H:%M:%S")
        unreg = _is_unreg(verb, ip)
        if i == 0:
            gap_str = "— (first)"
        else:
            secs = (ts - events[i - 1][0]).total_seconds()
            gap_str = "0s [duplicate]" if secs == 0 else _fmt_gap(secs)
            if not unreg:
                gap_style.append((i + 1, _gap_color(secs)))
        rows.append([
            str(i + 1),
            ts.strftime("%Y-%m-%d %H:%M:%S"),
            ist_str,
            verb,
            ip or "⊘ UNREGISTER",
            str(port) if port else "—",
            gap_str,
        ])

    col_widths = [0.6*cm, 3.8*cm, 2.2*cm, 2.5*cm, 3.2*cm, 1.4*cm, 2.7*cm]
    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 7.5),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("GRID",       (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8F9FA")]),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for i, (_, verb, ip, _) in enumerate(events):
        if _is_unreg(verb, ip):
            style.append(("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#FFD6D6")))
            style.append(("FONTNAME",   (0, i + 1), (-1, i + 1), "Helvetica-Bold"))
    for row_i, gc in gap_style:
        style.append(("BACKGROUND", (6, row_i), (6, row_i), gc))
    tbl.setStyle(TableStyle(style))
    return tbl


def _build_stats_table(events: list[Event]) -> Table:
    reg_events = [(ts, ip) for ts, verb, ip, _ in events if not _is_unreg(verb, ip)]
    gaps = [(reg_events[i][0] - reg_events[i-1][0]).total_seconds()
            for i in range(1, len(reg_events))]
    unreg_count = sum(1 for _, verb, ip, _ in events if _is_unreg(verb, ip))
    rows = [["Total Events", "Unregs", "Avg Gap", "Min Gap", "Max Gap", "Large >40m", "Anomalies <2m"]]
    rows.append([
        str(len(events)),
        str(unreg_count),
        _fmt_gap(statistics.mean(gaps)) if gaps else "—",
        _fmt_gap(min(gaps))             if gaps else "—",
        _fmt_gap(max(gaps))             if gaps else "—",
        str(sum(1 for g in gaps if g > 2400)),
        str(sum(1 for g in gaps if 0 < g < 120)),
    ])
    col_widths = [2.6*cm, 1.8*cm, 2.5*cm, 2.5*cm, 2.5*cm, 2.5*cm, 2.5*cm]
    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A5276")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 8.5),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("GRID",       (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#EBF5FB"), colors.white]),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    args = _parse_args()
    srv  = get_server(args.server)

    print(f"Server   : {srv.name}  ({srv.ssh_alias})", file=sys.stderr)
    print(f"Log path : {srv.event_log_path}", file=sys.stderr)
    print(f"Username : {args.username}", file=sys.stderr)
    print(f"Window   : {'all time' if args.hours == 0 else f'last {args.hours}h'}", file=sys.stderr)
    print("Fetching event logs...", file=sys.stderr)

    raw = extract_event_logs(srv, pattern=args.username)
    if not raw.strip():
        print(f"No event logs found for '{args.username}' on {srv.name}.", file=sys.stderr)
        sys.exit(1)

    events = _parse_events(raw, args.username, args.hours)
    if not events:
        window = "all time" if args.hours == 0 else f"last {args.hours}h"
        print(f"No registration events for '{args.username}' in {window}.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(events)} events.", file=sys.stderr)

    now_utc = datetime.utcnow()
    now_ist = now_utc + IST
    ts_str  = now_utc.strftime("%Y%m%d_%H%M%S")
    if args.output:
        out = args.output
    else:
        date_folder = Path("reports") / now_utc.strftime("%Y-%m-%d")
        date_folder.mkdir(parents=True, exist_ok=True)
        out = str(date_folder / f"reg_gap_{args.server}_{args.username}_{ts_str}.pdf")

    # ── Build PDF ─────────────────────────────────────────────────────────────
    doc = SimpleDocTemplate(out, pagesize=A4,
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=2*cm,    bottomMargin=2*cm)
    styles  = getSampleStyleSheet()
    title_s = ParagraphStyle("T",  parent=styles["Title"],    fontSize=16,
                              spaceAfter=4, textColor=colors.HexColor("#1A5276"))
    h1      = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=12,
                              textColor=colors.HexColor("#2C3E50"), spaceBefore=12, spaceAfter=4)
    h2      = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=10,
                              textColor=colors.HexColor("#566573"), spaceBefore=8, spaceAfter=3)
    body    = styles["Normal"]
    body.fontSize = 9

    window_label = "All time" if args.hours == 0 else f"Last {args.hours}h"
    story = []
    story.append(Paragraph("SIP Registration Gap Analysis", title_s))
    story.append(Paragraph(
        f"Server: <b>{srv.name}</b>  |  Username: <b>{args.username}</b>  |  "
        f"Window: <b>{window_label}</b>", body))
    story.append(Paragraph(
        f"Event log: <b>{srv.event_log_path}</b>  |  "
        f"Generated: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"({now_ist.strftime('%H:%M:%S')} IST)", body))
    story.append(Paragraph(
        f"Period: <b>{events[0][0].strftime('%Y-%m-%d %H:%M:%S')}</b> – "
        f"<b>{events[-1][0].strftime('%Y-%m-%d %H:%M:%S')}</b> UTC  |  "
        f"Total events: <b>{len(events)}</b>", body))
    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1A5276")))
    story.append(Spacer(1, 0.3*cm))

    story.append(Paragraph("Gap Colour Legend", h2))
    story.append(_build_legend())
    story.append(Spacer(1, 0.5*cm))

    story.append(Paragraph("Registration Events", h1))
    story.append(Spacer(1, 0.2*cm))
    story.append(_build_events_table(events))
    story.append(Spacer(1, 0.4*cm))

    # Notable events
    notable: list[str] = []
    for i in range(1, len(events)):
        ts_prev, verb_prev, ip_prev, _ = events[i - 1]
        ts_curr, _,         _,       _ = events[i]
        g = (ts_curr - ts_prev).total_seconds()
        p_utc = ts_prev.strftime("%H:%M:%S UTC")
        c_utc = ts_curr.strftime("%H:%M:%S UTC")
        p_ist = (ts_prev + IST).strftime("%H:%M IST")
        c_ist = (ts_curr + IST).strftime("%H:%M IST")
        if _is_unreg(verb_prev, ip_prev):
            notable.append(f"[UNREGISTER] #{i} at {p_utc} ({p_ist}) — "
                           f"re-registered at {c_utc} ({c_ist}), gap: {_fmt_gap(g)}")
        elif g > 2400:
            notable.append(f"[Large gap] #{i}→#{i+1}: {p_utc} ({p_ist}) → "
                           f"{c_utc} ({c_ist}) — {_fmt_gap(g)}")
        elif 0 < g < 120:
            notable.append(f"[Rapid re-reg] #{i}→#{i+1}: {p_utc} ({p_ist}) → "
                           f"{c_utc} ({c_ist}) — {_fmt_gap(g)} (network handoff)")
        elif g == 0:
            notable.append(f"[Duplicate] #{i+1}: {c_utc} ({c_ist}) — same second as previous")

    if notable:
        story.append(Paragraph("Notable Events", h2))
        for n in notable:
            story.append(Paragraph(f"• {n}", body))
        story.append(Spacer(1, 0.4*cm))

    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#BDC3C7")))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph("Summary Statistics", h1))
    story.append(Spacer(1, 0.2*cm))
    story.append(_build_stats_table(events))

    doc.build(story)
    print(f"PDF saved: {out}")


if __name__ == "__main__":
    main()


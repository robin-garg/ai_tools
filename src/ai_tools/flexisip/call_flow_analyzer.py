"""Call-flow timeline analyzer for Flexisip proxy logs.

Accepts a local Flexisip proxy log file (already extracted / from a backup),
a caller and callee user ID, and an optional UTC time window.  Finds every
INVITE from caller → callee in that window, builds a per-call event timeline
table (INVITE, push, re-REGISTER, 180/200/CANCEL/487, …) and renders a
structured Markdown / PDF report in the same format produced by manual analysis.

Typical workflow
----------------
1. Extract or obtain a Flexisip proxy log file (e.g. from ``ai-tools flexisip logs``
   with ``--save``, or from a daily backup).
2. Run::

       ai-tools flexisip call-flow \\
           --log-file logs/stg2b_calls_…log \\
           --caller 230 --callee 902493a6d16b \\
           --start "2026-05-06 15:33:00" --end "2026-05-06 15:48:00" \\
           --output reports/call_flow.pdf
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ai_tools.pdf.writer import markdown_to_pdf

UTC = timezone.utc

# ── Compiled regex patterns ───────────────────────────────────────────────────

_TS_MS_RE   = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3})")
_TS_RE      = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# INVITE detection
_INVITE_FROM_RE  = re.compile(r"Receiving new Request SIP message INVITE from sip:([^@]+)@")
_INVITE_TO_RE    = re.compile(r"nta: received INVITE sip:([^@]+)@")
_CALL_ID_RE      = re.compile(r"^Call-ID:\s+(\S+)", re.MULTILINE)
_CSEQ_RE         = re.compile(r"^CSeq:\s+(\d+)\s+INVITE", re.MULTILINE)

# Redis contact lookup
_CONTACT_GOT_RE  = re.compile(r'GOT fs:(\S+?) .+?-->\s*"(<sip:[^@]+@([0-9.]+):(\d+)[^"]*)"')
_PN_PROVIDER_RE  = re.compile(r"pn-provider=([^;>?&\s\"]+)")
_CONN_ID_RE      = re.compile(r"fs-conn-id=([0-9a-f]+)")

# ForkCallContext
_FORK_CTX_RE     = re.compile(r"New ForkCallContext (0x[0-9a-f]+)")
_FORK_NEW_REG_RE = re.compile(r"ForkCallContext::onNewRegister")

# Push notification
_PNR_CREATE_RE   = re.compile(r"Creating a push notif context (PNR 0x[0-9a-f]+)")
_FIREBASE_RE     = re.compile(r"FirebaseV1 request")
_PUSH_TTL_RE     = re.compile(r'"ttl":\s*"(\d+)s"')
_FCM_STATUS_RE   = re.compile(r":status = (\d+)")

# SIP signalling
_110_RE          = re.compile(r"110 Push sent")
_NTA_RE          = re.compile(r"nta: (sent|received) (\d{3})")
_CANCEL_RE       = re.compile(r"Receiving new Request SIP message CANCEL")
_BYE_RE          = re.compile(r"Receiving new Request SIP message BYE")
_REGISTER_RE     = re.compile(r"Receiving new Request SIP message REGISTER from sip:([^@]+)@")
_REGISTER_CSEQ_RE= re.compile(r"^CSeq:\s+(\d+)\s+REGISTER", re.MULTILINE)
_NEW_CONTACT_RE  = re.compile(r"RegistrarDB.*[Bb]inding|New contact.*registered")
_SEND_INVITE_RE  = re.compile(r"Sending Request SIP message to (sip:\S+)")
_FORK_REMOVED_RE = re.compile(r"Remove fork ")
# Additional events not carried on Call-ID blocks
_CANCEL_FWD_RE   = re.compile(r"nta: sent CANCEL \(\d+\) to (\S+)")
_ACK_RECV_RE     = re.compile(r"nta: received ACK")
_PNR_CANCEL_RE   = re.compile(r"PNR 0x[0-9a-f]+: canceling push request")

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class CallEvent:
    """A single event in a call's timeline."""
    timestamp: str           # HH:MM:SS.mmm display string
    direction: str           # RECV / SEND / INTERNAL
    event: str               # short event label
    detail: str = ""         # additional context


@dataclass
class CallFlow:
    """All extracted data for one SIP call (one INVITE transaction)."""
    call_index: int
    cseq: str
    call_id: str
    invite_ts: str
    events: list[CallEvent] = field(default_factory=list)
    push_sent: bool = False
    device_registered: bool = False
    reached_device: bool = False    # got a 180 / 200 from device
    outcome: str = ""               # CANCEL / BYE / 200 OK / timeout / etc.
    cancel_ts: Optional[str] = None
    response_ts: Optional[str] = None   # 180 or 200


@dataclass
class CallFlowReport:
    caller: str
    callee: str
    log_file: str
    start: str
    end: str
    calls: list[CallFlow]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_blocks(text: str) -> list[tuple[str, str]]:
    """Split raw log text into (timestamp_with_ms, full_block_text) pairs."""
    blocks: list[tuple[str, str]] = []
    cur_ts = ""
    cur_lines: list[str] = []
    for line in text.splitlines():
        m = _TS_MS_RE.match(line) or _TS_RE.match(line)
        if m:
            if cur_ts:
                blocks.append((cur_ts, "\n".join(cur_lines)))
            cur_ts = m.group(1)
            cur_lines = [line]
        else:
            cur_lines.append(line)
    if cur_ts:
        blocks.append((cur_ts, "\n".join(cur_lines)))
    return blocks


def _ts_to_dt(ts: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S:%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _display_ts(ts: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM:SS:mmm' → 'HH:MM:SS.mmm'."""
    parts = ts.split(" ")
    if len(parts) < 2:
        return ts
    seg = parts[1].split(":")
    if len(seg) == 4:
        return f"{seg[0]}:{seg[1]}:{seg[2]}.{seg[3]}"
    return parts[1]


def _in_window(ts: str, start: str, end: str) -> bool:
    """Return True if ts (YYYY-MM-DD HH:MM:SS…) falls within [start, end]."""
    prefix = ts[:19]
    if start and prefix < start[:19]:
        return False
    if end and prefix > end[:19]:
        return False
    return True


# ── Core analysis ─────────────────────────────────────────────────────────────

def analyze_log(
    log_text: str,
    caller: str,
    callee: str,
    start: str = "",
    end: str = "",
) -> CallFlowReport:
    """Parse *log_text* and return a :class:`CallFlowReport` for all calls from
    *caller* to *callee* within the optional UTC time window (*start* / *end*
    as ``'YYYY-MM-DD HH:MM:SS'`` strings).
    """
    blocks = _split_blocks(log_text)
    calls: list[CallFlow] = []

    # ── Find all INVITE blocks for this caller → callee pair ─────────────────
    for ts, block in blocks:
        if not _in_window(ts, start, end):
            continue
        # Must be "Receiving new Request SIP message INVITE from sip:<caller>@"
        fm = _INVITE_FROM_RE.search(block)
        if not fm or caller not in fm.group(1):
            continue
        # Must mention the callee somewhere (in the Request-URI or To header)
        if callee not in block:
            continue
        # Extract Call-ID and CSeq
        cm = _CALL_ID_RE.search(block)
        if not cm:
            continue
        call_id = cm.group(1)
        sq = _CSEQ_RE.search(block)
        cseq = sq.group(1) if sq else "?"

        # ── Collect blocks by different matching strategies ───────────────
        # call_blocks: blocks containing Call-ID (push payloads, SIP messages)
        call_blocks = [(t, b) for t, b in blocks if call_id in b]
        # cseq_blocks: blocks containing CSeq ref (nta events).  Expanded to
        # also catch CANCEL/ACK events that reference the CSeq but not "INVITE".
        cseq_blocks = [(t, b) for t, b in blocks if
                       f"for INVITE ({cseq})" in b or
                       f"INVITE ({cseq})" in b or
                       f"CANCEL ({cseq})" in b or
                       f"(CSeq {cseq})" in b]
        # callee_blocks: blocks that mention the callee AOR within the call
        # window — captures Redis GOT / ForkCallContext / PNR lines that
        # Flexisip emits without the Call-ID.
        callee_blocks = [
            (t, b) for t, b in blocks
            if callee in b and t[:19] >= ts[:19] and _in_window(t, ts[:19], end)
        ]

        # ── Walk blocks and build event timeline ──────────────────────────
        events: list[CallEvent] = []
        push_sent = False
        device_registered = False
        reached_device = False
        outcome = "Unknown"
        cancel_ts_disp: Optional[str] = None
        response_ts_disp: Optional[str] = None
        seen_100 = False
        seen_push = False
        seen_110 = False
        seen_fork = False
        seen_register: set[str] = set()
        seen_invite_forward: set[str] = set()
        seen_180 = False
        seen_200 = False
        seen_cancel = False
        seen_bye = False
        seen_487 = False
        seen_cancel_fwd = False
        seen_ack = False
        seen_pnr_cancel = False
        seen_redis_ts: set[str] = set()

        for bt, bb in call_blocks:
            dts = _display_ts(bt)

            # INVITE received (the opening event)
            if _INVITE_FROM_RE.search(bb) and caller in bb and _CALL_ID_RE.search(bb):
                events.append(CallEvent(dts, "RECV", "INVITE received",
                    f"From sip:{caller}@ → sip:{callee}@ | CSeq {cseq}"))

            # 100 Trying sent (appears in call_blocks as part of SIP message)
            if not seen_100 and "sent 100 Trying" in bb and "INVITE" in bb:
                seen_100 = True
                events.append(CallEvent(dts, "SEND", "100 Trying", "Caller acknowledged"))

            # Redis contact lookup hit
            gm = _CONTACT_GOT_RE.search(bb)
            if gm and callee in gm.group(1) and bt not in seen_redis_ts:
                seen_redis_ts.add(bt)
                ip_port = f"{gm.group(3)}:{gm.group(4)}"
                conn = _CONN_ID_RE.search(gm.group(2))
                conn_str = f"conn={conn.group(1)}" if conn else ""
                pn = _PN_PROVIDER_RE.search(gm.group(2))
                pn_str = f"pn-provider={pn.group(1)}" if pn else ""
                events.append(CallEvent(dts, "INTERNAL", "Redis contact found",
                    f"{ip_port} {conn_str} {pn_str}".strip()))

            # ForkCallContext created
            fkm = _FORK_CTX_RE.search(bb)
            if fkm and not seen_fork:
                seen_fork = True
                events.append(CallEvent(dts, "INTERNAL", "Fork created", fkm.group(1)))

            # FCM push payload (FirebaseV1 + call-id in JSON + ttl=90s)
            if not seen_push and _FIREBASE_RE.search(bb):
                ttlm = _PUSH_TTL_RE.search(bb)
                if ttlm and int(ttlm.group(1)) > 10:
                    seen_push = True
                    push_sent = True
                    events.append(CallEvent(dts, "INTERNAL", "FCM push dispatched",
                        f"ttl={ttlm.group(1)}s — push sent to device"))

            # FCM HTTP response
            fcm_st = _FCM_STATUS_RE.search(bb)
            if fcm_st and "FirebaseV1" not in bb and ":status" in bb:
                events.append(CallEvent(dts, "INTERNAL", "FCM HTTP response",
                    f"status={fcm_st.group(1)}"))

            # INVITE forwarded to device
            sm = _SEND_INVITE_RE.search(bb)
            if sm:
                target = sm.group(1)
                if target not in seen_invite_forward:
                    seen_invite_forward.add(target)
                    events.append(CallEvent(dts, "SEND", "INVITE forwarded", target))

            # Fork notified of new registration
            if _FORK_NEW_REG_RE.search(bb):
                events.append(CallEvent(dts, "INTERNAL", "Fork: new registration detected",
                    "Flexisip re-resolves contact and re-sends INVITE"))

            # CANCEL received
            if not seen_cancel and _CANCEL_RE.search(bb):
                seen_cancel = True
                cancel_ts_disp = dts
                events.append(CallEvent(dts, "RECV", "CANCEL received", "Caller hung up"))

            # BYE received
            if not seen_bye and _BYE_RE.search(bb):
                seen_bye = True
                events.append(CallEvent(dts, "RECV", "BYE received", "Call ended normally"))

            # Fork removed
            if _FORK_REMOVED_RE.search(bb) and callee in bb:
                events.append(CallEvent(dts, "INTERNAL", "Fork removed", "Transaction complete"))

            # Push notification cancelled (call torn down before push completed)
            if not seen_pnr_cancel and _PNR_CANCEL_RE.search(bb):
                seen_pnr_cancel = True
                events.append(CallEvent(dts, "INTERNAL", "Push notification cancelled",
                    "PNR cancelled — call terminated before push completed"))

        # ── NTA signal events + CANCEL/ACK (matched by CSeq) ─────────────
        for bt, bb in cseq_blocks:
            dts = _display_ts(bt)

            # NTA numeric response codes
            ntam = _NTA_RE.search(bb)
            if ntam:
                direction, code = ntam.group(1), ntam.group(2)
                direction_label = "RECV" if direction == "received" else "SEND"
                if code == "100" and not seen_100:
                    seen_100 = True
                    events.append(CallEvent(dts, "SEND", "100 Trying",
                        "Flexisip acknowledged the INVITE"))
                elif code == "110" and not seen_110:
                    seen_110 = True
                    events.append(CallEvent(dts, "SEND", "110 Push Sent",
                        "Caller notified that FCM push was dispatched"))
                elif code == "180" and not seen_180:
                    seen_180 = True
                    reached_device = True
                    response_ts_disp = dts
                    events.append(CallEvent(dts, direction_label, "180 Ringing", "Device is ringing!"))
                elif code == "200" and not seen_200:
                    seen_200 = True
                    reached_device = True
                    response_ts_disp = response_ts_disp or dts
                    events.append(CallEvent(dts, direction_label, "200 OK", "Call answered"))
                elif code == "487" and not seen_487:
                    seen_487 = True
                    events.append(CallEvent(dts, direction_label, "487 Request Terminated", ""))
                elif code in ("503", "408"):
                    events.append(CallEvent(dts, direction_label, f"{code} (connection error)",
                        "Old TCP connection dead — attempt on stale contact"))

            # CANCEL forwarded downstream to device
            cfm = _CANCEL_FWD_RE.search(bb)
            if cfm and not seen_cancel_fwd:
                seen_cancel_fwd = True
                events.append(CallEvent(dts, "SEND", "CANCEL forwarded to device",
                    cfm.group(1)))

            # ACK received (after 487 / 200)
            if _ACK_RECV_RE.search(bb) and not seen_ack:
                seen_ack = True
                events.append(CallEvent(dts, "RECV", "ACK received", ""))

        # ── REGISTER events (device wake-up): time-window scan ───────────
        # Only include REGISTERs that arrive AFTER the INVITE and within 60s
        # (the typical push wake-up latency).  This avoids attributing REGISTERs
        # triggered by a later call back to earlier calls.
        invite_dt = _ts_to_dt(ts)
        for bt, bb in blocks:
            rm = _REGISTER_RE.search(bb)
            if not rm or callee not in rm.group(1):
                continue
            reg_dt = _ts_to_dt(bt)
            if invite_dt and reg_dt:
                delta = (reg_dt - invite_dt).total_seconds()
                if delta < 0 or delta > 60:   # must be 0-60s after this INVITE
                    continue
            rcseq = _REGISTER_CSEQ_RE.search(bb)
            rcseq_str = f"CSeq {rcseq.group(1)}" if rcseq else ""
            key = rcseq_str or bt
            if key not in seen_register:
                seen_register.add(key)
                device_registered = True
                dts = _display_ts(bt)
                events.append(CallEvent(dts, "RECV", "REGISTER (device woke up)",
                    f"{rcseq_str} — device re-registering on new TCP connection"))

        # ── Callee-keyed blocks: Redis GOT / ForkCallContext / PNR ──────
        # Flexisip logs these without the Call-ID so they are invisible to the
        # call_blocks scan.  Scan all callee_blocks and pick them up here,
        # deduplicating via the same seen_* flags used above.
        for bt, bb in callee_blocks:
            dts = _display_ts(bt)

            # Redis contact lookup result
            gm = _CONTACT_GOT_RE.search(bb)
            if gm and callee in gm.group(1) and bt not in seen_redis_ts:
                seen_redis_ts.add(bt)
                ip_port = f"{gm.group(3)}:{gm.group(4)}"
                conn = _CONN_ID_RE.search(gm.group(2))
                conn_str = f"conn={conn.group(1)}" if conn else ""
                pn = _PN_PROVIDER_RE.search(gm.group(2))
                pn_str = f"pn-provider={pn.group(1)}" if pn else ""
                events.append(CallEvent(dts, "INTERNAL", "Redis contact found",
                    f"{ip_port} {conn_str} {pn_str}".strip()))

            # ForkCallContext created
            if not seen_fork:
                fkm = _FORK_CTX_RE.search(bb)
                if fkm:
                    seen_fork = True
                    events.append(CallEvent(dts, "INTERNAL", "Fork created", fkm.group(1)))

            # PNR cancellation (not in call_blocks — block contains no Call-ID)
            if not seen_pnr_cancel and _PNR_CANCEL_RE.search(bb):
                seen_pnr_cancel = True
                events.append(CallEvent(dts, "INTERNAL", "Push notification cancelled",
                    "PNR cancelled — call terminated before push completed"))

        # Sort all events by timestamp
        events.sort(key=lambda e: e.timestamp)

        # Determine outcome
        if seen_200:
            outcome = "Answered (200 OK)"
        elif seen_180 and seen_cancel:
            outcome = "Ringing → CANCEL by caller"
        elif seen_cancel:
            outcome = "CANCEL (no device response)"
        elif seen_bye:
            outcome = "BYE (call ended)"
        elif seen_487:
            outcome = "487 Request Terminated"

        calls.append(CallFlow(
            call_index=len(calls) + 1,
            cseq=cseq,
            call_id=call_id,
            invite_ts=_display_ts(ts),
            events=events,
            push_sent=push_sent,
            device_registered=device_registered,
            reached_device=reached_device,
            outcome=outcome,
            cancel_ts=cancel_ts_disp,
            response_ts=response_ts_disp,
        ))

    return CallFlowReport(
        caller=caller,
        callee=callee,
        log_file="",
        start=start,
        end=end,
        calls=calls,
    )


# ── Markdown rendering ────────────────────────────────────────────────────────

def render_markdown(report: CallFlowReport) -> str:
    """Convert a :class:`CallFlowReport` to a structured Markdown report."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = []

    lines.append(f"# Call Flow Analysis: {report.caller} → {report.callee}")
    lines.append("")
    lines.append(f"**Caller:** sip:{report.caller}@  |  **Callee:** sip:{report.callee}@")
    if report.start or report.end:
        lines.append(f"**Time window:** {report.start or '(open)'} → {report.end or '(open)'} UTC")
    if report.log_file:
        lines.append(f"**Log source:** {report.log_file}")
    lines.append(f"**Generated:** {now}")
    lines.append(f"**Calls found:** {len(report.calls)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for cf in report.calls:
        lines.append(f"## Call {cf.call_index} — CSeq {cf.cseq} | {cf.outcome}")
        lines.append("")
        lines.append("| Timestamp | Direction | Event | Detail |")
        lines.append("| --- | --- | --- | --- |")
        for ev in cf.events:
            detail = ev.detail.replace("|", "\\|")
            lines.append(f"| {ev.timestamp} | {ev.direction} | {ev.event} | {detail} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Call # | CSeq | INVITE at | FCM Push | Device REGISTER | Reached Device | Outcome |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for cf in report.calls:
        push = "✓" if cf.push_sent else "✗"
        reg = "✓" if cf.device_registered else "✗"
        reached = "✓" if cf.reached_device else "✗"
        lines.append(f"| {cf.call_index} | {cf.cseq} | {cf.invite_ts} | {push} | {reg} | {reached} | {cf.outcome} |")
    lines.append("")

    return "\n".join(lines)


# ── Public entry point ────────────────────────────────────────────────────────

def analyze_and_render(
    log_file: Path,
    caller: str,
    callee: str,
    start: str = "",
    end: str = "",
    output_pdf: Optional[Path] = None,
    output_md: Optional[Path] = None,
) -> tuple[Path, Path]:
    """Analyze *log_file* and produce Markdown + PDF reports.

    Args:
        log_file:   Path to the extracted Flexisip proxy log file.
        caller:     Caller SIP username (e.g. ``"230"``).
        callee:     Callee SIP username (e.g. ``"902493a6d16b"``).
        start:      Optional start timestamp ``"YYYY-MM-DD HH:MM:SS"`` (UTC).
        end:        Optional end timestamp ``"YYYY-MM-DD HH:MM:SS"`` (UTC).
        output_pdf: Destination PDF path.  Defaults to ``reports/`` directory.
        output_md:  Destination Markdown path.  Defaults to ``reports/`` directory.

    Returns:
        ``(md_path, pdf_path)`` tuple.
    """
    log_text = log_file.read_text(encoding="utf-8")
    report = analyze_log(log_text, caller, callee, start=start, end=end)
    report.log_file = str(log_file)
    md_content = render_markdown(report)

    # Default output paths
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    if output_md is None:
        output_md = reports_dir / f"call_flow_{caller}_to_{callee}_{stamp}.md"
    if output_pdf is None:
        output_pdf = reports_dir / f"call_flow_{caller}_to_{callee}_{stamp}.pdf"

    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(md_content, encoding="utf-8")

    title = f"Call Flow: {caller} → {callee}"
    markdown_to_pdf(md_content, output_pdf, title=title, tz_label="UTC")

    return output_md, output_pdf

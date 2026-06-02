"""Analyze VoIP call records from a CSV against Flexisip proxy logs.

Workflow
--------
1. Parse a call-records CSV (EDT timestamps, To / From user IDs).
2. Convert all times to UTC.
3. Group records into smart time batches to minimise SSH log fetches.
4. For each batch, extract Flexisip proxy logs via log_extractor.
5. Analyse each call's SIP flow: INVITE → push notification → device response → outcome.
6. Produce a structured markdown report and render it to PDF.

Batch grouping rules
--------------------
- Calls within ``batch_window_mins`` of the *first* call in a group share one fetch.
- The actual fetch window = (first_call − buffer) … (last_call + buffer).
- A single-call batch gets only the per-call buffer on each side.

Push notification distinction
------------------------------
- **Call push** (what we analyse): FCM / APNS push sent when an INVITE arrives for
  a sleeping device.  Identified by ``"ttl": "90s"`` in the FirebaseV1 payload.
- **ContactExpirationNotifier push** (excluded): background token-refresh push with
  ``"ttl": "0s"``, sent by the notifier every ~5 min.  These are intentionally
  filtered out because they are unrelated to any individual call.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ai_tools.flexisip.log_extractor import extract_and_save
from ai_tools.flexisip.log_utils import display_ts, ms_delta, split_blocks, ts_to_dt
from ai_tools.flexisip.servers import Server
from ai_tools.flexisip.sip_patterns import (
    _BYE_RE,
    _CALL_ID_RE,
    _CANCEL_RE,
    _CONTACT_GOT_RE,
    _CSEQ_RE,
    _FCM_STATUS_RE,
    _FIREBASE_RE,
    _FORK_CTX_RE,
    _FORK_NEW_REG_RE,
    _INVITE_FROM_RE,
    _NTA_RE,
    _PN_PROVIDER_RE,
    _PUSH_TTL_RE,
    _REGISTER_CSEQ_RE,
    _REGISTER_RE,
    _110_RE,
)
from ai_tools.pdf.writer import markdown_to_pdf

EDT = timezone(timedelta(hours=-4))
UTC = timezone.utc

BATCH_WINDOW_MINS: int = 30
BUFFER_MINS: int = 2


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CallRecord:
    call_time_edt: datetime
    call_time_utc: datetime
    direction: str
    to_user: str        # callee — the Android device
    from_user: str      # caller
    hangup_cause: str
    duration_seconds: int
    billed_seconds: int


@dataclass
class CallBatch:
    records: list[CallRecord]
    window_start_utc: datetime
    window_end_utc: datetime

    @property
    def users(self) -> list[str]:
        """Unique user IDs in this batch (To + From), insertion-ordered."""
        seen: set[str] = set()
        result: list[str] = []
        for r in self.records:
            for u in [r.to_user, r.from_user]:
                if u not in seen:
                    seen.add(u)
                    result.append(u)
        return result


@dataclass
class SipEvent:
    timestamp: str
    description: str
    is_key_event: bool = False
    detail: str = ""


@dataclass
class CallFlowSummary:
    record: CallRecord
    call_id: Optional[str]

    # INVITE details
    invite_received: bool
    invite_timestamp: Optional[str]
    invite_cseq: Optional[str] = None
    invite_display_name: Optional[str] = None
    invite_user_agent: Optional[str] = None
    caller_ip: Optional[str] = None

    # Redis / registrar contact
    contact_found: bool = False
    device_address: Optional[str] = None       # full SIP contact URI from Redis
    device_ip_port: Optional[str] = None       # ip:port of callee device
    pn_provider: Optional[str] = None          # fcm / apns
    pn_silent: Optional[str] = None
    pn_timeout: Optional[str] = None
    redis_latency_ms: Optional[str] = None

    # ForkCallContext
    fork_created: bool = False
    fork_address: Optional[str] = None

    # Push notification (call push, ttl=90s)
    push_sent: bool = False
    push_ttl: Optional[str] = None
    push_provider: Optional[str] = None
    push_timestamp: Optional[str] = None
    push_token_short: Optional[str] = None     # first 20 chars of FCM token
    push_from_uri: Optional[str] = None
    push_display_name: Optional[str] = None
    push_fcm_project: Optional[str] = None
    push_priority: Optional[str] = None

    # FCM HTTP/2 response
    fcm_http_status: Optional[str] = None
    fcm_message_name: Optional[str] = None
    fcm_delivery_ms: Optional[int] = None      # ms from push sent to 200 OK
    pnr_state: Optional[str] = None            # e.g. "NotSubmitted → InProgress → Successful"

    # 110 Push Sent
    response_110: bool = False
    ts_110: Optional[str] = None

    # Device REGISTER (wake-up)
    device_registered: bool = False
    register_timestamp: Optional[str] = None
    register_cseq: Optional[str] = None
    register_ip_port: Optional[str] = None
    wake_latency_ms: Optional[int] = None      # ms from push sent to REGISTER

    # SIP call progress
    ts_100: Optional[str] = None
    ts_180: Optional[str] = None
    ts_200: Optional[str] = None
    ts_ack: Optional[str] = None
    ts_bye: Optional[str] = None
    ts_cancel: Optional[str] = None
    cancel_reason: Optional[str] = None        # Q.850 reason phrase

    # Summary
    sip_responses: list[tuple[str, str]] = field(default_factory=list)
    bye_received: bool = False
    cancel_received: bool = False
    outcome: str = ""
    timeline: list[SipEvent] = field(default_factory=list)


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _parse_edt_time(s: str) -> datetime:
    """Parse "May 04 2026 08:53:57 PM" → timezone-aware EDT datetime."""
    return datetime.strptime(s.strip(), "%b %d %Y %I:%M:%S %p").replace(tzinfo=EDT)


def parse_csv(path: Path) -> list[CallRecord]:
    """Parse a call-records CSV, returning records sorted chronologically (oldest first)."""
    records: list[CallRecord] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dt_edt = _parse_edt_time(row["Call Date/Time"])
            records.append(CallRecord(
                call_time_edt=dt_edt,
                call_time_utc=dt_edt.astimezone(UTC),
                direction=row.get("Direction", "").strip(),
                to_user=row.get("To", "").strip(),
                from_user=row.get("From", "").strip(),
                hangup_cause=row.get("Hangup Cause", "").strip(),
                duration_seconds=int(row.get("Duration Seconds", 0) or 0),
                billed_seconds=int(row.get("Billed Seconds", 0) or 0),
            ))
    records.sort(key=lambda r: r.call_time_utc)
    return records


# ── Batch grouping ────────────────────────────────────────────────────────────

def group_into_batches(
    records: list[CallRecord],
    window_mins: int = BATCH_WINDOW_MINS,
    buffer_mins: int = BUFFER_MINS,
) -> list[CallBatch]:
    """Group records into time batches for efficient log fetching.

    Rules
    -----
    - Start a new batch with the first record.
    - Add subsequent records to the current batch while they fall within
      ``window_mins`` of that batch's *first* record's call time.
    - The fetch window is the actual span of calls in the batch (not the full
      window_mins), padded by ``buffer_mins`` on each side.
    """
    if not records:
        return []
    window = timedelta(minutes=window_mins)
    buf = timedelta(minutes=buffer_mins)
    batches: list[CallBatch] = []
    grp: list[CallRecord] = [records[0]]
    for rec in records[1:]:
        if rec.call_time_utc - grp[0].call_time_utc <= window:
            grp.append(rec)
        else:
            batches.append(CallBatch(
                records=grp[:],
                window_start_utc=grp[0].call_time_utc - buf,
                window_end_utc=grp[-1].call_time_utc + buf,
            ))
            grp = [rec]
    batches.append(CallBatch(
        records=grp[:],
        window_start_utc=grp[0].call_time_utc - buf,
        window_end_utc=grp[-1].call_time_utc + buf,
    ))
    return batches


# ── Log analysis — compiled patterns (local to this module) ──────────────────

_FROM_DISPLAY_RE = re.compile(r'^From:\s+"([^"]+)"', re.MULTILINE)
_USER_AGENT_RE = re.compile(r"^User-Agent:\s+(.+)", re.MULTILINE)
_CALLER_CONTACT_RE = re.compile(r"^Contact:\s+<sip:[^@]+@([0-9.]+):(\d+)", re.MULTILINE)
_FETCHING_RE = re.compile(r"Fetching fs:([^\s\[]+)")
_REDIS_LATENCY_RE = re.compile(r"Redis command completed in (\d+)ms")
_PN_SILENT_RE = re.compile(r"pn-silent=([^;>?&\s\"]+)")
_PN_TIMEOUT_RE = re.compile(r"pn-timeout=([^;>?&\s\"]+)")
_FORK_RE = re.compile(r"Fork to (sip:[^\s]+)")
_PUSH_PRIORITY_RE = re.compile(r'"priority":\s*"([^"]+)"')
_PUSH_TOKEN_RE = re.compile(r'"token":\s*"([^"]+)"')
_PUSH_FROM_URI_RE = re.compile(r'"from-uri":\s*"([^"]+)"')
_PUSH_DISPLAY_NAME_RE = re.compile(r'"display-name":\s*"([^"]+)"')
_PUSH_CALL_ID_IN_PAYLOAD_RE = re.compile(r'"call-id":\s*"([^"]+)"')
_FCM_PROJECT_RE = re.compile(r"/v1/projects/([^/]+)/messages")
_FCM_MSG_NAME_RE = re.compile(r'"name":\s*"(projects/[^"]+)"')
_PNR_STATE_RE = re.compile(r"switching state from (\w+) -> (\w+)")
_ACK_RE = re.compile(r"Receiving new Request SIP message ACK")
_CANCEL_REASON_RE = re.compile(r"Reason:\s+Q\.850\s*;cause=\d+\s*;text=\"([^\"]+)\"", re.MULTILINE)
_SENDING_INVITE_RE = re.compile(r"Sending Request SIP message to (sip:[0-9a-zA-Z]+@[0-9.]+:\d+)")
_CONN_REFUSED_RE = re.compile(r"nta: INVITE .+: Connection refused \(\d+\) with udp/\[([0-9.]+)\]:(\d+)")


def _analyze_call(
    record: CallRecord,
    blocks: list[tuple[str, str]],
    tolerance_mins: int = 3,
) -> CallFlowSummary:
    """Analyse log blocks for a single call record and return a CallFlowSummary."""
    tol = timedelta(minutes=tolerance_mins)
    call_time = record.call_time_utc

    # ── Step 1: Locate the INVITE block for this call ────────────────────────
    invite_block: Optional[str] = None
    invite_ts: Optional[str] = None
    call_id: Optional[str] = None
    invite_cseq: Optional[str] = None
    invite_display_name: Optional[str] = None
    invite_user_agent: Optional[str] = None
    caller_ip: Optional[str] = None

    for ts, block in blocks:
        block_dt = ts_to_dt(ts)
        if block_dt is None or abs((block_dt - call_time).total_seconds()) > tol.total_seconds():
            continue
        fm = _INVITE_FROM_RE.search(block)
        if not fm or record.from_user not in fm.group(1):
            continue
        cm = _CALL_ID_RE.search(block)
        if cm:
            call_id = cm.group(1)
            invite_block = block
            invite_ts = ts
            m = _CSEQ_RE.search(block)
            if m:
                # _CSEQ_RE captures only the number; reconstruct display form
                invite_cseq = f"{m.group(1)} INVITE"
            m = _FROM_DISPLAY_RE.search(block)
            if m:
                invite_display_name = m.group(1)
            m = _USER_AGENT_RE.search(block)
            if m:
                invite_user_agent = m.group(1).strip()
            m = _CALLER_CONTACT_RE.search(block)
            if m:
                caller_ip = f"{m.group(1)}:{m.group(2)}"
            break

    # Fallback: check FirebaseV1 payload "call-id" near the call time
    if call_id is None:
        for ts, block in blocks:
            block_dt = ts_to_dt(ts)
            if block_dt and abs((block_dt - call_time).total_seconds()) <= tol.total_seconds():
                pm = _PUSH_CALL_ID_IN_PAYLOAD_RE.search(block)
                if pm:
                    call_id = pm.group(1)
                    invite_ts = ts
                    break

    # ── Step 2: Collect all blocks that reference this Call-ID ───────────────
    call_blocks: list[tuple[str, str]] = []
    if call_id:
        call_blocks = [(ts, blk) for ts, blk in blocks if call_id in blk]

    # ── Step 3: Walk call_blocks and build timeline / extract events ─────────
    timeline: list[SipEvent] = []
    sip_responses: list[tuple[str, str]] = []

    # State accumulators
    contact_found = False
    device_address: Optional[str] = None
    device_ip_port: Optional[str] = None
    pn_provider: Optional[str] = None
    pn_silent: Optional[str] = None
    pn_timeout: Optional[str] = None
    redis_latency_ms: Optional[str] = None
    fork_created = False
    fork_address: Optional[str] = None

    push_sent = False
    push_ttl: Optional[str] = None
    push_provider_label: Optional[str] = None
    push_ts: Optional[str] = None
    push_token_short: Optional[str] = None
    push_from_uri: Optional[str] = None
    push_display_name: Optional[str] = None
    push_fcm_project: Optional[str] = None
    push_priority: Optional[str] = None

    fcm_http_status: Optional[str] = None
    fcm_message_name: Optional[str] = None
    pnr_states: list[str] = []

    response_110 = False
    ts_110: Optional[str] = None
    ts_100: Optional[str] = None
    ts_180: Optional[str] = None
    ts_200: Optional[str] = None
    ts_ack: Optional[str] = None
    ts_bye: Optional[str] = None
    ts_cancel: Optional[str] = None
    cancel_reason: Optional[str] = None
    bye_received = False
    cancel_received = False

    # INVITE is the first timeline entry
    if invite_block:
        disp = f'"{invite_display_name}"' if invite_display_name else ""
        invite_detail_parts = [p for p in [
            f"From: {disp}" if disp else "",
            f"CSeq: {invite_cseq}" if invite_cseq else "",
            f"UA: {invite_user_agent}" if invite_user_agent else "",
            f"Caller IP: {caller_ip}" if caller_ip else "",
        ] if p]
        timeline.append(SipEvent(
            timestamp=invite_ts or "",
            description=f"INVITE received: sip:{record.from_user}@ → sip:{record.to_user}@",
            is_key_event=True,
            detail=", ".join(invite_detail_parts),
        ))

    conn_refused_logged: set[str] = set()
    invite_sent_logged = False

    for ts, block in call_blocks:
        # ── 100 Trying sent ──────────────────────────────────────────────────
        if not ts_100 and "sent 100 Trying for INVITE" in block:
            ts_100 = ts
            timeline.append(SipEvent(ts, "100 Trying sent", detail="Caller acknowledged — waiting for contact lookup"))

        # ── Redis contact lookup ──────────────────────────────────────────────
        lm = _REDIS_LATENCY_RE.search(block)
        if lm and not redis_latency_ms:
            redis_latency_ms = lm.group(1)

        gm = _CONTACT_GOT_RE.search(block)
        if gm and not contact_found:
            contact_found = True
            device_address = gm.group(2)
            device_ip_port = f"{gm.group(3)}:{gm.group(4)}"
            pm = _PN_PROVIDER_RE.search(device_address)
            if pm:
                pn_provider = pm.group(1)
            sm = _PN_SILENT_RE.search(device_address)
            if sm:
                pn_silent = sm.group(1)
            tm2 = _PN_TIMEOUT_RE.search(device_address)
            if tm2:
                pn_timeout = tm2.group(1)
            lat = f", lookup={redis_latency_ms}ms" if redis_latency_ms else ""
            redis_detail = (
                f"{device_ip_port}, pn-provider={pn_provider}, "
                f"pn-silent={pn_silent}, pn-timeout={pn_timeout}{lat}"
            )
            timeline.append(SipEvent(
                ts,
                f"Redis: contact found for sip:{record.to_user}@",
                is_key_event=True,
                detail=redis_detail,
            ))

        # ── ForkCallContext created ───────────────────────────────────────────
        if not fork_created and _FORK_CTX_RE.search(block):
            fork_created = True
            timeline.append(SipEvent(ts, "ForkCallContext created", detail="INVITE forking begins"))

        # ── Fork branch (INVITE sent to device) ──────────────────────────────
        fk = _FORK_RE.search(block)
        if fk and not fork_address:
            fork_address = fk.group(1)
            short = fork_address.split(";")[0]
            timeline.append(SipEvent(ts, "INVITE forked to callee contact", detail=short))

        # ── INVITE actually sent out to device IP ─────────────────────────────
        if not invite_sent_logged:
            si = _SENDING_INVITE_RE.search(block)
            if si:
                invite_sent_logged = True
                timeline.append(SipEvent(ts, "INVITE sent to device", detail=si.group(1)))

        # ── FirebaseV1 call push (ttl=90s) ───────────────────────────────────
        if _FIREBASE_RE.search(block) and not push_sent:
            tm = _PUSH_TTL_RE.search(block)
            if tm and tm.group(1) != "0":
                push_sent = True
                push_ttl = f"{tm.group(1)}s"
                push_provider_label = "FCM (Firebase)"
                push_ts = ts
                pm2 = _PUSH_PRIORITY_RE.search(block)
                if pm2:
                    push_priority = pm2.group(1)
                tkm = _PUSH_TOKEN_RE.search(block)
                if tkm:
                    push_token_short = tkm.group(1)[:20] + "…"
                fum = _PUSH_FROM_URI_RE.search(block)
                if fum:
                    push_from_uri = fum.group(1)
                dnm = _PUSH_DISPLAY_NAME_RE.search(block)
                if dnm:
                    push_display_name = dnm.group(1)
                prjm = _FCM_PROJECT_RE.search(block)
                if prjm:
                    push_fcm_project = prjm.group(1)
                push_detail = (
                    f"ttl={push_ttl}, priority={push_priority}, "
                    f"project={push_fcm_project or 'unknown'}, "
                    f"display-name={push_display_name or '—'}, "
                    f"from-uri={push_from_uri or '—'}"
                )
                timeline.append(SipEvent(
                    ts,
                    "FCM call push dispatched → fcm.googleapis.com",
                    is_key_event=True,
                    detail=push_detail,
                ))

        # ── PNR state transitions — each as individual timeline event ─────────
        for sm2 in _PNR_STATE_RE.finditer(block):
            from_state, to_state = sm2.group(1), sm2.group(2)
            state_entry = f"{from_state} → {to_state}"
            if state_entry not in pnr_states:
                pnr_states.append(state_entry)
                timeline.append(SipEvent(
                    ts,
                    f"PNR state: {state_entry}",
                    detail="Push Notification Request lifecycle",
                ))

        # ── FCM HTTP/2 response ───────────────────────────────────────────────
        if not fcm_http_status and _FCM_STATUS_RE.search(block):
            st = _FCM_STATUS_RE.search(block)
            if st:
                fcm_http_status = st.group(1)
                nm = _FCM_MSG_NAME_RE.search(block)
                if nm:
                    fcm_message_name = nm.group(1)
                delivery_ms = ms_delta(push_ts, ts) if push_ts else None
                delivery_str = f"{delivery_ms}ms round-trip" if delivery_ms is not None else ""
                fcm_detail_parts = [p for p in [delivery_str, fcm_message_name or ""] if p]
                timeline.append(SipEvent(
                    ts,
                    f"FCM HTTP/2 {fcm_http_status} — push accepted by Firebase",
                    is_key_event=(fcm_http_status == "200"),
                    detail=", ".join(fcm_detail_parts),
                ))

        # ── 110 Push Sent ─────────────────────────────────────────────────────
        if not response_110 and _110_RE.search(block):
            response_110 = True
            ts_110 = ts
            timeline.append(SipEvent(ts, "110 Push Sent", detail="Caller notified that push was dispatched"))

        # ── Connection refused (device offline) ───────────────────────────────
        cr = _CONN_REFUSED_RE.search(block)
        if cr:
            cr_addr = f"{cr.group(1)}:{cr.group(2)}"
            if cr_addr not in conn_refused_logged:
                conn_refused_logged.add(cr_addr)
                timeline.append(SipEvent(
                    ts,
                    "INVITE delivery failed — Connection refused",
                    detail=f"Device offline at {cr_addr} (will re-try after push wake-up)",
                ))

        # ── onNewRegister (device woke, pending INVITE re-dispatched) ─────────
        if _FORK_NEW_REG_RE.search(block):
            timeline.append(SipEvent(
                ts,
                "ForkCallContext::onNewRegister — INVITE re-dispatched to woken device",
                is_key_event=True,
            ))

        # ── NTA SIP responses (100/180/200/480/486…) ─────────────────────────
        for m2 in _NTA_RE.finditer(block):
            direction, code, reason = m2.group(1), m2.group(2), m2.group(3)
            entry = f"{direction} {code} {reason}"
            sip_responses.append((ts, entry))
            is_key = code in ("180", "183", "200", "408", "480", "486", "503")
            desc = f"nta: {direction} {code} {reason}"
            detail = ""
            if code == "180" and not ts_180:
                ts_180 = ts
                desc = "180 Ringing"
                detail = f"{direction} — device is ringing"
                is_key = True
            elif code == "200" and not ts_200:
                ts_200 = ts
                desc = "200 OK — call answered"
                detail = direction
                is_key = True
            timeline.append(SipEvent(ts, desc, is_key, detail=detail))

        # ── ACK ───────────────────────────────────────────────────────────────
        if not ts_ack and _ACK_RE.search(block):
            ts_ack = ts
            timeline.append(SipEvent(ts, "ACK received", is_key_event=True, detail="Three-way handshake complete — media flows"))

        # ── BYE ───────────────────────────────────────────────────────────────
        if not bye_received and _BYE_RE.search(block):
            bye_received = True
            ts_bye = ts
            timeline.append(SipEvent(ts, "BYE received", is_key_event=True, detail="Call ended normally"))

        # ── CANCEL ────────────────────────────────────────────────────────────
        if not cancel_received and _CANCEL_RE.search(block):
            cancel_received = True
            ts_cancel = ts
            rm = _CANCEL_REASON_RE.search(block)
            if rm:
                cancel_reason = rm.group(1)
            desc = "CANCEL received"
            detail = f"Q.850: {cancel_reason}" if cancel_reason else ""
            timeline.append(SipEvent(ts, desc, is_key_event=True, detail=detail))

    # ── Step 4: Look for callee REGISTER (device wake-up) in all blocks ──────
    device_registered = False
    register_ts: Optional[str] = None
    register_cseq: Optional[str] = None
    register_ip_port: Optional[str] = None
    wake_latency_ms: Optional[int] = None

    if push_ts and record.to_user:
        push_dt = ts_to_dt(push_ts)
        wake_window = timedelta(seconds=90)
        for ts, block in blocks:
            blk_dt = ts_to_dt(ts)
            if not blk_dt or not push_dt:
                continue
            delta = (blk_dt - push_dt).total_seconds()
            if delta < -1 or delta > wake_window.total_seconds():
                continue
            rm2 = _REGISTER_RE.search(block)
            if rm2 and record.to_user in rm2.group(1):
                device_registered = True
                register_ts = ts
                cm2 = _REGISTER_CSEQ_RE.search(block)
                if cm2:
                    register_cseq = cm2.group(1)
                cct = _CALLER_CONTACT_RE.search(block)
                if cct:
                    register_ip_port = f"{cct.group(1)}:{cct.group(2)}"
                wake_latency_ms = ms_delta(push_ts, ts)
                reg_detail_parts = [p for p in [
                    f"Wake latency: {wake_latency_ms}ms after push" if wake_latency_ms else "",
                    f"CSeq: {register_cseq}" if register_cseq else "",
                    f"IP: {register_ip_port}" if register_ip_port else "",
                ] if p]
                timeline.append(SipEvent(
                    ts,
                    "REGISTER from callee — device woke up",
                    is_key_event=True,
                    detail=", ".join(reg_detail_parts),
                ))
                break

    # ── Deduplicate timeline ──────────────────────────────────────────────────
    seen_events: set[tuple[str, str]] = set()
    deduped: list[SipEvent] = []
    for ev in timeline:
        key = (ev.timestamp, ev.description[:60])
        if key not in seen_events:
            seen_events.add(key)
            deduped.append(ev)
    timeline = sorted(deduped, key=lambda e: e.timestamp)

    # ── Determine outcome ─────────────────────────────────────────────────────
    codes = {r.split()[1] for _, r in sip_responses if len(r.split()) >= 2}
    if "200" in codes:
        outcome = "Answered (200 OK)"
    elif "486" in codes:
        outcome = "Busy Here (486)"
    elif "480" in codes or record.hangup_cause == "NO_ANSWER":
        outcome = "No Answer / Timeout (480)"
    elif "408" in codes:
        outcome = "Request Timeout (408)"
    elif "503" in codes:
        outcome = "Service Unavailable (503)"
    elif cancel_received:
        reason_label = f" ({cancel_reason})" if cancel_reason else ""
        outcome = f"Cancelled{reason_label} — {record.hangup_cause}"
    elif not call_id:
        outcome = f"Not found in Flexisip logs ({record.hangup_cause})"
    else:
        outcome = f"Ended — {record.hangup_cause}"

    fcm_delivery_ms = ms_delta(push_ts, next(
        (ts for ts, blk in call_blocks if _FCM_STATUS_RE.search(blk)), None
    )) if push_ts else None

    return CallFlowSummary(
        record=record,
        call_id=call_id,
        invite_received=invite_block is not None,
        invite_timestamp=invite_ts,
        invite_cseq=invite_cseq,
        invite_display_name=invite_display_name,
        invite_user_agent=invite_user_agent,
        caller_ip=caller_ip,
        contact_found=contact_found,
        device_address=device_address,
        device_ip_port=device_ip_port,
        pn_provider=pn_provider,
        pn_silent=pn_silent,
        pn_timeout=pn_timeout,
        redis_latency_ms=redis_latency_ms,
        fork_created=fork_created,
        fork_address=fork_address,
        push_sent=push_sent,
        push_ttl=push_ttl,
        push_provider=push_provider_label,
        push_timestamp=push_ts,
        push_token_short=push_token_short,
        push_from_uri=push_from_uri,
        push_display_name=push_display_name,
        push_fcm_project=push_fcm_project,
        push_priority=push_priority,
        fcm_http_status=fcm_http_status,
        fcm_message_name=fcm_message_name,
        fcm_delivery_ms=fcm_delivery_ms,
        pnr_state=" → ".join(dict.fromkeys(  # unique-ordered states
            s for t in pnr_states for s in t.split(" → ")
        )) if pnr_states else None,
        response_110=response_110,
        ts_110=ts_110,
        device_registered=device_registered,
        register_timestamp=register_ts,
        register_cseq=register_cseq,
        register_ip_port=register_ip_port,
        wake_latency_ms=wake_latency_ms,
        ts_100=ts_100,
        ts_180=ts_180,
        ts_200=ts_200,
        ts_ack=ts_ack,
        ts_bye=ts_bye,
        ts_cancel=ts_cancel,
        cancel_reason=cancel_reason,
        sip_responses=sip_responses,
        bye_received=bye_received,
        cancel_received=cancel_received,
        outcome=outcome,
        timeline=timeline,
    )


def analyze_batch(
    log_text: str,
    batch: CallBatch,
    tolerance_mins: int = 3,
) -> list[CallFlowSummary]:
    """Analyse all calls in *batch* against *log_text* and return per-call summaries."""
    blocks = split_blocks(log_text)
    return [_analyze_call(rec, blocks, tolerance_mins) for rec in batch.records]


# ── Markdown report ───────────────────────────────────────────────────────────

def _fmt_ts(ts: Optional[str]) -> str:
    """Return HH:MM:SS.mmm for display, or '—' if None."""
    return display_ts(ts)


def generate_markdown_report(
    csv_path: Path,
    server_name: str,
    all_summaries: list[CallFlowSummary],
    batches: list[CallBatch],
) -> str:
    """Build a detailed markdown analysis report with a flat chronological timeline per call."""
    now_utc = datetime.now(tz=UTC)
    lines: list[str] = [
        "# Flexisip Call Flow Analysis",
        "",
        f"**CSV source:** `{csv_path.name}`  |  **Server:** {server_name}  |  "
        f"**Generated:** {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        f"**Total calls:** {len(all_summaries)}  |  "
        f"**Log fetch batches:** {len(batches)}  |  "
        f"**Input timezone:** EDT (UTC−4)  |  **Log timestamps:** UTC",
        "",
        "---",
        "",
        "## Summary Table",
        "",
        "| # | Time (EDT) | From | To | Call-ID | Push | FCM | Wake (ms) | Outcome |",
        "|---|-----------|------|----|---------|------|-----|-----------|---------|",
    ]
    for i, s in enumerate(all_summaries, 1):
        cid = (s.call_id[:24] + "…") if s.call_id and len(s.call_id) > 24 else (s.call_id or "—")
        edt = s.record.call_time_edt.strftime("%b %d %I:%M:%S %p")
        push_col = f"✓ ttl={s.push_ttl}" if s.push_sent else "—"
        fcm_col = f"HTTP {s.fcm_http_status}" if s.fcm_http_status else "—"
        wake_col = f"{s.wake_latency_ms}" if s.wake_latency_ms else "—"
        lines.append(
            f"| {i} | {edt} | {s.record.from_user} | {s.record.to_user} "
            f"| `{cid}` | {push_col} | {fcm_col} | {wake_col} | {s.outcome} |"
        )

    lines += ["", "---", ""]

    # ── Per-call sections — single flat timeline table ────────────────────────
    for i, s in enumerate(all_summaries, 1):
        edt_str = s.record.call_time_edt.strftime("%b %d, %Y %I:%M:%S %p EDT")
        utc_str = s.record.call_time_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

        lines += [
            f"## Call {i} — {s.outcome}",
            "",
            f"**{edt_str}** ({utc_str})  |  "
            f"From: `{s.record.from_user}` → To (Android): `{s.record.to_user}`  |  "
            f"Hangup: `{s.record.hangup_cause}`  |  Duration: {s.record.duration_seconds}s",
            "",
        ]

        if s.call_id:
            lines += [f"**Call-ID:** `{s.call_id}`", ""]

        # ── No logs found ─────────────────────────────────────────────────────
        if not s.call_id and not s.invite_received:
            lines += [
                "> **No INVITE found in Flexisip logs.** "
                "This call was most likely dropped upstream (Kazoo / 2600Hz dialplan) "
                "before a SIP INVITE reached the proxy. "
                "No routing, push notification, or device response was generated.",
                "",
                "---",
                "",
            ]
            continue

        # ── Timing summary line ───────────────────────────────────────────────
        timing_parts: list[str] = []
        invite_to_ring = ms_delta(s.invite_timestamp, s.ts_180)
        invite_to_answer = ms_delta(s.invite_timestamp, s.ts_200)
        reg_to_ring = ms_delta(s.register_timestamp, s.ts_180)
        if invite_to_answer is not None:
            timing_parts.append(f"INVITE→200 OK: {invite_to_answer/1000:.1f}s")
        if invite_to_ring is not None:
            timing_parts.append(f"INVITE→Ringing: {invite_to_ring/1000:.1f}s")
        if s.fcm_delivery_ms is not None:
            timing_parts.append(f"FCM round-trip: {s.fcm_delivery_ms}ms")
        if s.wake_latency_ms is not None:
            timing_parts.append(f"Push→REGISTER: {s.wake_latency_ms}ms")
        if reg_to_ring is not None:
            timing_parts.append(f"REGISTER→Ringing: {reg_to_ring}ms")
        if timing_parts:
            lines += [f"**Timing:** {' | '.join(timing_parts)}", ""]

        # ── Flat chronological event table ────────────────────────────────────
        lines += [
            "### Event Timeline",
            "",
            "| # | Time (UTC) | Event | Detail |",
            "|---|-----------|-------|--------|",
        ]

        if s.timeline:
            for j, ev in enumerate(s.timeline, 1):
                ts_disp = display_ts(ev.timestamp) if ev.timestamp else "—"
                safe_desc = ev.description.replace("|", "∣")
                safe_detail = (ev.detail or "").replace("|", "∣")
                bold = f"**{safe_desc}**" if ev.is_key_event else safe_desc
                lines.append(f"| {j} | {ts_disp} | {bold} | {safe_detail} |")
        else:
            lines.append("| — | — | No events captured | Check log window / Call-ID |")

        lines += ["", "---", ""]

    # ── Batch fetch window appendix ───────────────────────────────────────────
    lines += [
        "## Log Fetch Batches",
        "",
        "| Batch | Calls | Fetch Window (UTC) | Users |",
        "|-------|-------|--------------------|-------|",
    ]
    for bi, b in enumerate(batches, 1):
        ws = b.window_start_utc.strftime("%Y-%m-%d %H:%M:%S")
        we = b.window_end_utc.strftime("%Y-%m-%d %H:%M:%S")
        users = ", ".join(b.users)
        lines.append(f"| {bi} | {len(b.records)} | {ws} → {we} | {users} |")

    lines.append("")
    return "\n".join(lines)


# ── Main orchestration ────────────────────────────────────────────────────────

def analyze_call_records(
    csv_path: Path,
    server: Server,
    container: str = "",
    *,
    output_pdf: Optional[Path] = None,
    save_logs: bool = False,
    logs_dir: Path = Path("logs"),
    reports_dir: Path = Path("reports") / "calls",
    batch_window_mins: int = BATCH_WINDOW_MINS,
    buffer_mins: int = BUFFER_MINS,
    tolerance_mins: int = 3,
    progress_cb: Optional[object] = None,
) -> Path:
    """Full pipeline: CSV → smart batch log fetch → call-flow analysis → PDF.

    Args:
        csv_path:         Path to the call records CSV file.
        server:           Flexisip :class:`Server` to fetch logs from.
        container:        Container name for log access.  Pass an empty string
                          (or omit) when ``server.proxy_log_in_container`` is
                          False (log fetched directly from host filesystem).
        output_pdf:       PDF destination. Defaults to
                          ``reports/call_records_analysis_<utc-stamp>.pdf``.
        save_logs:        Persist each raw log batch to ``logs/``.
        logs_dir:         Directory for raw log files.
        reports_dir:      Directory for the PDF report.
        batch_window_mins: Max span (minutes) to group calls into one log fetch.
        buffer_mins:      Padding (minutes) before/after each batch window.
        tolerance_mins:   Max minutes between CSV call time and log INVITE stamp.
        progress_cb:      Optional callable(msg: str) for progress reporting.

    Returns:
        Path to the generated PDF file.
    """

    def _log(msg: str) -> None:
        if callable(progress_cb):
            progress_cb(msg)  # type: ignore[operator]

    records = parse_csv(csv_path)
    if not records:
        raise ValueError(f"No call records found in {csv_path}")

    _log(f"Loaded {len(records)} call record(s) from {csv_path.name}")
    batches = group_into_batches(records, batch_window_mins, buffer_mins)
    _log(f"Grouped into {len(batches)} log fetch batch(es)")

    all_summaries: list[CallFlowSummary] = []

    for idx, batch in enumerate(batches, 1):
        ws = batch.window_start_utc.strftime("%Y-%m-%d %H:%M:%S")
        we = batch.window_end_utc.strftime("%Y-%m-%d %H:%M:%S")
        pattern = "|".join(f"sip:{u}@" for u in batch.users)
        descriptor = (
            f"callrec-b{idx}-"
            f"{batch.records[0].call_time_utc.strftime('%Y%m%d_%H%M%S')}"
        )
        _log(f"Batch {idx}/{len(batches)}: fetching {ws} → {we}  ({len(batch.records)} call(s))")

        result = extract_and_save(
            server, container,
            descriptor=descriptor,
            pattern=pattern,
            start=ws,
            end=we,
            save=save_logs,
            logs_dir=logs_dir,
        )

        # ── Fallback: use local saved log when server returns nothing ─────────
        if not result.content and logs_dir.exists():
            prefix = f"{server.name}_{descriptor}"
            candidates = sorted(
                logs_dir.glob(f"{prefix}_*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                best = candidates[0]
                result = result.__class__(
                    server=result.server,
                    container=result.container,
                    filters=result.filters,
                    content=best.read_text(encoding="utf-8"),
                    saved_to=best,
                    total_blocks=0,
                    matched_blocks=0,
                )
                _log(f"  ↳ Server returned nothing — using cached log: {best.name}")
            else:
                _log(f"  ↳ No log blocks found for batch {idx} (server empty, no local cache)")
        elif not result.content:
            _log(f"  ↳ No log blocks found for batch {idx}")

        summaries = analyze_batch(result.content, batch, tolerance_mins)
        all_summaries.extend(summaries)
        _log(f"  ↳ Analysed {len(summaries)} call(s)")

    all_summaries.sort(key=lambda s: s.record.call_time_utc)

    md = generate_markdown_report(csv_path, server.name, all_summaries, batches)

    if output_pdf is None:
        now_utc = datetime.now(tz=UTC)
        date_folder = reports_dir / now_utc.strftime("%Y-%m-%d")
        date_folder.mkdir(parents=True, exist_ok=True)
        stamp = now_utc.strftime("%Y%m%d_%H%M%S")
        output_pdf = date_folder / f"call_records_analysis_{stamp}.pdf"

    _log(f"Rendering PDF → {output_pdf}")
    return markdown_to_pdf(
        md,
        output_pdf,
        title=f"Call Records Analysis — {csv_path.stem}",
        tz_label="UTC",
    )

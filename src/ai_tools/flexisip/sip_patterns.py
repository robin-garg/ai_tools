"""Shared compiled regex patterns for Flexisip log analysis.

Patterns in this module are used by call_flow_analyzer, call_records_analyzer,
and any future analyzer that reads Flexisip proxy logs.

Reconciliation notes for patterns that differed between the two original files
-----------------------------------------------------------------------------
_CSEQ_RE
    Captures only the numeric CSeq number (e.g. ``"1"``).  call_flow_analyzer
    uses this number directly in string-matching against log text, so it *must*
    be a bare integer.  call_records_analyzer reconstructs the full display
    string as ``f"{m.group(1)} INVITE"`` where needed.

_NTA_RE
    Uses the three-group form from call_records_analyzer
    ``(sent|received) (\\d{3}) ([^\\s(]+)`` so that both the numeric code and
    the reason phrase are available.  call_flow_analyzer ignores group(3).

_REGISTER_CSEQ_RE
    Captures the full ``"<n> REGISTER"`` string (call_records_analyzer form).
    call_flow_analyzer uses this as a dedup / display string, which is fine.
"""

from __future__ import annotations

import re

# ── Timestamp ─────────────────────────────────────────────────────────────────

_TS_RE    = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_TS_MS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3})")

# ── INVITE detection ──────────────────────────────────────────────────────────

_INVITE_FROM_RE = re.compile(
    r"Receiving new Request SIP message INVITE from sip:([^@]+)@"
)
_CALL_ID_RE = re.compile(r"^Call-ID:\s+(\S+)", re.MULTILINE)

# Captures the bare numeric CSeq only (e.g. group(1) == "1").
_CSEQ_RE = re.compile(r"^CSeq:\s+(\d+)\s+INVITE", re.MULTILINE)

# ── Redis contact lookup ───────────────────────────────────────────────────────

_CONTACT_GOT_RE = re.compile(
    r'GOT fs:(\S+?) .+?-->\s*"(<sip:[^@]+@([0-9.]+):(\d+)[^"]*)"'
)
_PN_PROVIDER_RE = re.compile(r"pn-provider=([^;>?&\s\"]+)")

# ── ForkCallContext ───────────────────────────────────────────────────────────

_FORK_CTX_RE     = re.compile(r"New ForkCallContext (0x[0-9a-f]+)")
_FORK_NEW_REG_RE = re.compile(r"ForkCallContext::onNewRegister")

# ── Push notification ─────────────────────────────────────────────────────────

_FIREBASE_RE   = re.compile(r"FirebaseV1 request")
_PUSH_TTL_RE   = re.compile(r'"ttl":\s*"(\d+)s"')
_FCM_STATUS_RE = re.compile(r":status = (\d+)")

# ── SIP signalling ────────────────────────────────────────────────────────────

_110_RE    = re.compile(r"110 Push sent")

# Three groups: direction, code, reason-phrase.
# call_flow_analyzer uses only groups 1-2; call_records_analyzer uses all three.
_NTA_RE    = re.compile(r"nta: (sent|received) (\d{3}) ([^\s(]+)")

_CANCEL_RE   = re.compile(r"Receiving new Request SIP message CANCEL")
_BYE_RE      = re.compile(r"Receiving new Request SIP message BYE")
_REGISTER_RE = re.compile(
    r"Receiving new Request SIP message REGISTER from sip:([^@]+)@"
)

# Captures the full "<n> REGISTER" string (e.g. "1 REGISTER").
_REGISTER_CSEQ_RE = re.compile(r"^CSeq:\s+(\d+\s+REGISTER)", re.MULTILINE)

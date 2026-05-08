"""Parse ContactExpirationNotifier events from Flexisip proxy logs.

Extracts structured data from log text:
  - NotifierRun  : one firing of the notifier (every ~5 min)
  - PushEvent    : a push notification attempt for a single device
  - HttpResponse : the APNS / FCM HTTP response captured in the same log span

Typical log patterns
--------------------
Run trigger (message level):
  [ContactExpirationNotifier] Sending service push notifications and refreshing tokens...

Checking line (debug level):
  [ContactExpirationNotifier] Checking N expiring contacts for token refresh

Extended line (message level):
  Extended N eligible registrations

Successful push (message level):
  [ContactExpirationNotifier] background push notification successfully sent to device
    'fs-gen-XYZ' of user 'sip:USER@...'

APNS HTTP/2 response header (debug level, continuation block):
  :status = 200

FCM HTTP response body (debug level, continuation block):
  "error" / HTTP 200-OK
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Compiled regexes                                                             #
# --------------------------------------------------------------------------- #

_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

RE_RUN_START = re.compile(
    _TS + r".*\[ContactExpirationNotifier\] Sending service push notifications"
)
RE_CHECKING = re.compile(
    r"\[ContactExpirationNotifier\] Checking (\d+) expiring contacts"
)
RE_EXTENDED_RUN = re.compile(
    r"\[ContactExpirationNotifier\] Extended (\d+) eligible registrations"
)
RE_PUSH_SENT = re.compile(
    _TS
    + r".*\[ContactExpirationNotifier\] background push notification successfully sent"
    r" to device '(fs-gen-[^']+)' of user 'sip:([^@]+)@[^;']*"
    r"(?:;[^;']*)*?;pn-provider=([^;>']+)"
)
RE_PUSH_FAIL = re.compile(
    _TS + r".*\[ContactExpirationNotifier\].*(?:[Ff]ailed?|[Ee]rror).*push"
)
# APNS HTTP/2 response header
RE_APNS_STATUS = re.compile(r":status\s*=\s*(\d+)")
# FCM / Firebase JSON error field
RE_FCM_ERROR = re.compile(r'"error"\s*:\s*"([^"]+)"')
# FCM / Firebase "message" → success
RE_FCM_NAME = re.compile(r'"name"\s*:\s*"projects/')


# --------------------------------------------------------------------------- #
# Data model                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class PushEvent:
    timestamp: str
    user: str           # SIP username, e.g. "1a97435c96ba"
    device_id: str      # fs-gen-XYZ
    provider: str       # "apns", "apns.dev", "fcm"
    success: bool
    http_status: Optional[int] = None   # from APNS :status header
    error_message: Optional[str] = None # from FCM error body or notifier


@dataclass
class NotifierRun:
    timestamp: str
    checking_count: int = 0
    extended_count: int = 0
    push_events: list[PushEvent] = field(default_factory=list)

    @property
    def push_count(self) -> int:
        return len(self.push_events)

    @property
    def error_count(self) -> int:
        return sum(1 for e in self.push_events if not e.success)


# --------------------------------------------------------------------------- #
# Parser                                                                       #
# --------------------------------------------------------------------------- #

def parse_notifier_log(log_text: str, user_filter: str = "") -> list[NotifierRun]:
    """Parse raw log text and return a list of NotifierRun objects.

    Args:
        log_text:    Raw text from the Flexisip proxy log.
        user_filter: If non-empty, only include PushEvents where user contains
                     this substring.
    """
    runs: list[NotifierRun] = []
    current_run: Optional[NotifierRun] = None

    # We track the most recent HTTP status seen so we can attach it to the
    # previous push event (APNS response comes in the next log block).
    pending_push: Optional[PushEvent] = None

    for line in log_text.splitlines():
        # ------------------------------------------------------------------ #
        # Notifier run trigger                                                 #
        # ------------------------------------------------------------------ #
        m = RE_RUN_START.search(line)
        if m:
            current_run = NotifierRun(timestamp=m.group(1))
            runs.append(current_run)
            pending_push = None
            continue

        if current_run is None:
            # Lines before the first run start — look for response headers
            # from a run that started before the log window.
            pass

        # ------------------------------------------------------------------ #
        # Checking / extended counters                                         #
        # ------------------------------------------------------------------ #
        m = RE_CHECKING.search(line)
        if m and current_run:
            current_run.checking_count = int(m.group(1))
            continue

        m = RE_EXTENDED_RUN.search(line)
        if m and current_run:
            current_run.extended_count = int(m.group(1))
            continue

        # ------------------------------------------------------------------ #
        # Successful push                                                       #
        # ------------------------------------------------------------------ #
        m = RE_PUSH_SENT.search(line)
        if m:
            ts, device_id, user, provider = m.group(1), m.group(2), m.group(3), m.group(4)
            if not user_filter or user_filter.lower() in user.lower():
                ev = PushEvent(
                    timestamp=ts,
                    user=user,
                    device_id=device_id,
                    provider=provider.lower(),
                    success=True,
                )
                if current_run:
                    current_run.push_events.append(ev)
                pending_push = ev
            continue

        # ------------------------------------------------------------------ #
        # Explicit failure from the notifier                                   #
        # ------------------------------------------------------------------ #
        m = RE_PUSH_FAIL.search(line)
        if m and current_run:
            ev = PushEvent(
                timestamp=m.group(1),
                user="unknown",
                device_id="",
                provider="",
                success=False,
                error_message=line.strip(),
            )
            current_run.push_events.append(ev)
            pending_push = ev
            continue

        # ------------------------------------------------------------------ #
        # APNS :status header (appears in continuation block after the push)  #
        # ------------------------------------------------------------------ #
        m = RE_APNS_STATUS.search(line)
        if m and pending_push is not None:
            status = int(m.group(1))
            pending_push.http_status = status
            if status != 200:
                pending_push.success = False
            continue

        # ------------------------------------------------------------------ #
        # FCM error body                                                        #
        # ------------------------------------------------------------------ #
        m = RE_FCM_ERROR.search(line)
        if m and pending_push is not None:
            pending_push.error_message = m.group(1)
            pending_push.success = False
            continue

        # FCM success (message name present → accepted)
        if RE_FCM_NAME.search(line) and pending_push is not None:
            pending_push.http_status = 200

    return runs

"""Retrieve the last *real* SIP registration for a user from the event log.

The Flexisip event-log filesystem writes a ``Registered`` entry **only** when
an actual SIP ``REGISTER`` message is received and accepted by the proxy.  The
ContactExpirationNotifier never writes to the event log — it only updates the
Redis TTL / ``updatedAt`` field.

Therefore:
  - Last event-log line  → definitive real registration time (device was online)
  - Redis ``updatedAt``  → may be real *or* notifier-extended

By comparing the two we can tell callers whether the current Redis binding
reflects a genuine device REGISTER or a server-side safety-net extension.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ai_tools.flexisip.registrar import connect, list_registrations
from ai_tools.flexisip.servers import Server

IST = timezone(timedelta(hours=5, minutes=30))

# Threshold: if Redis updatedAt is more than this many seconds ahead of the
# event-log timestamp, the Redis entry is considered an extension.
EXTENSION_THRESHOLD_SECS = 30

_EVENT_LINE_RE = re.compile(
    r"(\w{3} \w{3} +\d+ \d{2}:\d{2}:\d{2} \d{4}):\s+Registered\s+"
    r"<sip:[^>]+>\s+\(sip:[^@]+@([0-9.]+):(\d+)"
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RealRegistration:
    username: str
    domain: str
    # From event log — always a real device REGISTER
    real_registered_utc: datetime
    real_registered_ist: datetime
    real_ip: str
    real_port: str
    # From Redis — real OR notifier extension
    redis_updated_utc: datetime | None
    redis_updated_ist: datetime | None
    redis_ip: str
    redis_port: str
    # Verdict
    registration_type: str   # "real" | "extended" | "no_redis"
    gap_secs: int            # redis_updated_utc - real_registered_utc in seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ssh(ssh_alias: str, cmd: str, timeout: float = 15.0) -> str:
    """Run a command over SSH and return stdout (best-effort, no raise)."""
    result = subprocess.run(
        ["ssh", ssh_alias, cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _read_last_event_log_line(server: Server, username: str, domain: str) -> str:
    """Return the last line of today's event-log file for the user.

    Tries via ``docker exec proxy`` first (in case logs are inside the
    container), then falls back to direct host filesystem access.
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    log_path = (
        f"{server.event_log_path}/users/{domain}/{username}/registers/{today}.log"
    )

    # Attempt 1: inside the proxy container
    line = _ssh(
        server.ssh_alias,
        f"sudo docker exec proxy tail -1 {log_path} 2>/dev/null",
    )
    if line and "Registered" in line:
        return line

    # Attempt 2: host filesystem
    line = _ssh(
        server.ssh_alias,
        f"tail -1 {log_path} 2>/dev/null",
    )
    return line


def _parse_event_line(line: str) -> tuple[datetime, str, str] | None:
    """Parse ``Registered`` event-log line → (utc_dt, ip, port) or None."""
    m = _EVENT_LINE_RE.search(line)
    if not m:
        return None
    # Normalise multiple spaces (e.g. single-digit days: "Jun  5")
    ts_raw = " ".join(m.group(1).split())
    dt = datetime.strptime(ts_raw, "%a %b %d %H:%M:%S %Y").replace(
        tzinfo=timezone.utc
    )
    return dt, m.group(2), m.group(3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_real_registration(
    server: Server,
    username: str,
    domain: str,
) -> RealRegistration | None:
    """Return a RealRegistration for *username*@*domain* on *server*.

    Returns None if no event-log entry exists for today (user never registered
    today or the log path is inaccessible).
    """
    # 1. Event log — real registration
    raw_line = _read_last_event_log_line(server, username, domain)
    parsed = _parse_event_line(raw_line) if raw_line else None
    if parsed is None:
        return None
    real_utc, real_ip, real_port = parsed
    real_ist = real_utc.astimezone(IST)

    # 2. Redis — current binding
    redis_utc: datetime | None = None
    redis_ip = "unknown"
    redis_port = "0"
    with connect(server) as client:
        regs = list_registrations(client, pattern=f"fs:{username}@{domain}")
    if regs:
        r = regs[0]
        redis_utc = r.updated_utc
        redis_ip = r.ip
        redis_port = r.port

    redis_ist = redis_utc.astimezone(IST) if redis_utc else None

    # 3. Verdict
    if redis_utc is None:
        reg_type = "no_redis"
        gap_secs = 0
    else:
        gap_secs = int((redis_utc - real_utc).total_seconds())
        reg_type = "real" if gap_secs <= EXTENSION_THRESHOLD_SECS else "extended"

    return RealRegistration(
        username=username,
        domain=domain,
        real_registered_utc=real_utc,
        real_registered_ist=real_ist,
        real_ip=real_ip,
        real_port=real_port,
        redis_updated_utc=redis_utc,
        redis_updated_ist=redis_ist,
        redis_ip=redis_ip,
        redis_port=redis_port,
        registration_type=reg_type,
        gap_secs=gap_secs,
    )

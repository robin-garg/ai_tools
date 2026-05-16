"""Parse a raw Flexisip Redis contact string into a typed Registration object.

Flexisip stores each registration as a Redis hash:
  key   = fs:<user>@<domain>
  field = "<urn:uuid:...>"          (instance / device ID)
  value = <sip:user@ip:port;...>    (packed SIP contact string)

All fields the caller needs are extracted with targeted regex patterns so the
parser stays simple and is easy to extend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# IST is UTC + 5 hours 30 minutes
IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class Registration:
    redis_key: str      # full Redis key, e.g. fs:abc123@domain.com
    user: str           # SIP username only,  e.g. abc123
    domain: str         # full domain, e.g. testing.voip.pinkreseller.com
    domain_short: str   # first two domain labels, e.g. bdaprthirteen.bdapr
    instance_id: str    # UUID of the device/instance
    platform: str       # "Android", "iOS", or "Unknown"
    ip: str             # remote IP address
    port: str           # remote port
    updated_utc: datetime
    updated_ist: datetime
    mins_ago: int       # minutes since last registration (negative = future)

    @property
    def ip_port(self) -> str:
        return f"{self.ip}:{self.port}"

    @property
    def mins_ago_label(self) -> str:
        n = self.mins_ago
        if n < 0:
            return "just now"
        if n < 60:
            return f"{n} mins ago"
        hrs, mins = divmod(n, 60)
        if hrs < 24:
            return f"{hrs} hr {mins} mins ago"
        days, hrs = divmod(hrs, 24)
        return f"{days}d {hrs}h {mins}m ago"


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #

def _short_domain(domain: str) -> str:
    """Return the first two dot-separated labels of a domain."""
    parts = domain.split(".")
    return ".".join(parts[:2])


def _platform(contact: str) -> str:
    m = re.search(r"pn-provider=([^;>?&\s]+)", contact)
    if not m:
        return "Unknown"
    provider = m.group(1).lower()
    return {"fcm": "Android", "apns": "iOS"}.get(provider, provider.capitalize())


def _ip_port(contact: str) -> tuple[str, str]:
    """Extract IP and port from <sip:user@ip:port;..."""
    m = re.search(r"<sip:[^@]+@([0-9.]+):(\d+)", contact)
    if m:
        return m.group(1), m.group(2)
    return "unknown", "0"


def _updated_at(contact: str) -> datetime | None:
    m = re.search(r"updatedAt=(\d+)", contact)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)


def _instance_id(field_name: str) -> str:
    """Extract UUID from a field name like '<urn:uuid:abc-def>'."""
    m = re.search(r"urn:uuid:([^\">\s]+)", field_name)
    return m.group(1) if m else field_name


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #

def parse_registration(
    redis_key: str,
    instance_field: str,
    contact: str,
) -> Registration:
    """Build a Registration from the raw Redis key, field name, and contact value."""
    key_body = redis_key.removeprefix("fs:")
    user, _, domain = key_body.partition("@")
    domain_short = _short_domain(domain) if domain else ""
    instance_id = _instance_id(instance_field)
    platform = _platform(contact)
    ip, port = _ip_port(contact)

    now_utc = datetime.now(tz=timezone.utc)
    updated_utc = _updated_at(contact) or now_utc
    updated_ist = updated_utc.astimezone(IST)
    mins_ago = int((now_utc - updated_utc).total_seconds() / 60)

    return Registration(
        redis_key=redis_key,
        user=user,
        domain=domain,
        domain_short=domain_short,
        instance_id=instance_id,
        platform=platform,
        ip=ip,
        port=port,
        updated_utc=updated_utc,
        updated_ist=updated_ist,
        mins_ago=mins_ago,
    )

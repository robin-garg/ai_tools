"""Shared utility functions for Flexisip log analysis.

Functions in this module are used by call_flow_analyzer, call_records_analyzer,
sip_agent_analyzer, reg_gap_analysis, and the flexisip CLI.

Consolidation notes
-------------------
split_blocks
    The two-value ``(timestamp, text)`` form that was ``_split_blocks()`` in
    both call_flow_analyzer and call_records_analyzer (identical implementations).
    sip_agent_analyzer had a ``_split_blocks()`` variant that returned only the
    block text (no separate timestamp).  That variant is now ``split_blocks_text``,
    implemented as a thin wrapper around ``split_blocks``.

display_ts
    Merges ``_display_ts()`` (call_flow_analyzer) and ``_ts_seconds()``
    (call_records_analyzer) — same logic, different names.  The unified version
    accepts ``None`` and returns *default* (``"—"`` by default), matching the
    behaviour callers already expected.

fmt_gap
    Merges ``_fmt_gap()`` in cli.py and reg_gap_analysis.py.  The cli.py version
    is used as the canonical implementation because it handles the sub-minute
    case (returns ``"45s"`` instead of ``"0m 45s"``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ai_tools.flexisip.sip_patterns import _TS_MS_RE, _TS_RE

UTC = timezone.utc


# ── Block splitting ───────────────────────────────────────────────────────────

def split_blocks(text: str) -> list[tuple[str, str]]:
    """Split raw Flexisip log text into ``(timestamp, full_block_text)`` pairs.

    A block begins on a line that starts with a ``YYYY-MM-DD`` timestamp and
    includes all following continuation lines (SIP headers, JSON payloads, …)
    until the next timestamp line.  The stored timestamp preserves milliseconds
    (``YYYY-MM-DD HH:MM:SS:mmm``) when present.
    """
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


def split_blocks_text(text: str) -> list[str]:
    """Split raw log text and return only the block texts (no timestamps).

    Thin wrapper around :func:`split_blocks` for callers that do not need the
    per-block timestamp (e.g. :mod:`sip_agent_analyzer`).
    """
    return [block for _, block in split_blocks(text)]


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def ts_to_dt(ts: Optional[str]) -> Optional[datetime]:
    """Parse a Flexisip log timestamp to a UTC-aware :class:`~datetime.datetime`.

    Accepts ``None`` (returns ``None``) and both timestamp forms:

    * ``YYYY-MM-DD HH:MM:SS:mmm``  (with milliseconds)
    * ``YYYY-MM-DD HH:MM:SS``      (seconds only)
    """
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S:%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def display_ts(ts: Optional[str], default: str = "—") -> str:
    """Return ``HH:MM:SS.mmm`` display string from a Flexisip log timestamp.

    Returns *default* (``"—"``) when *ts* is ``None`` or empty.

    Handles both ``YYYY-MM-DD HH:MM:SS:mmm`` and ``YYYY-MM-DD HH:MM:SS``.
    """
    if not ts:
        return default
    parts = ts.split(" ")
    if len(parts) < 2:
        return ts
    segments = parts[1].split(":")
    if len(segments) == 4:   # HH:MM:SS:mmm  →  HH:MM:SS.mmm
        return f"{segments[0]}:{segments[1]}:{segments[2]}.{segments[3]}"
    return parts[1]


# ── Timing helpers ────────────────────────────────────────────────────────────

def ms_delta(ts_start: Optional[str], ts_end: Optional[str]) -> Optional[int]:
    """Return integer milliseconds between two Flexisip log timestamps, or ``None``."""
    if not ts_start or not ts_end:
        return None
    dt1 = ts_to_dt(ts_start)
    dt2 = ts_to_dt(ts_end)
    if dt1 and dt2:
        return int((dt2 - dt1).total_seconds() * 1000)
    return None


def fmt_gap(secs: float) -> str:
    """Format a duration in seconds as a human-readable string.

    Examples::

        fmt_gap(45)    → "45s"
        fmt_gap(125)   → "2m 05s"
        fmt_gap(3725)  → "1h 02m 05s"
    """
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec:02d}s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s"

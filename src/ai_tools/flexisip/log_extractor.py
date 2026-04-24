"""Extract Flexisip proxy call logs from a remote container.

Flexisip proxy logs live at `/usr/local/var/log/flexisip/flexisip-proxy.log`
inside the container. Each log *entry* begins with a timestamp like
`2026-04-24 11:17:35:123` but the body of SIP messages and push-notification
payloads span many continuation lines without their own timestamp, so plain
`grep` truncates them.

The extraction strategy is a small `awk` program that buffers lines into
"blocks" (a timestamp line plus following un-timestamped continuation lines)
and emits the whole block whenever it matches the caller's filters.

Filters:
- `pattern` — regex matched anywhere in the block (used for call-id or user).
- `start` / `end` — optional "YYYY-MM-DD HH:MM:SS" bounds compared lexically
  against the block's leading timestamp.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ai_tools.flexisip.servers import Server

PROXY_LOG_PATH = "/usr/local/var/log/flexisip/flexisip-proxy.log"

# awk block-extractor: groups lines starting with a date into a single block
# ("block" = timestamp line + its continuation lines — SIP bodies, JSON push
# payloads, etc.). Emits the whole contiguous span of blocks from the first
# to the last block that matches `pat` (and optional time window), so that
# non-matching but contextually relevant lines — push notifications being
# sent, routing decisions, REGISTERs from a woken device — are preserved.
_AWK_EXTRACTOR = r"""
function ts_of(b,   a) { split(b, a, " "); return a[1] " " a[2] }
function in_window(b,   t) {
  if (start == "" && end == "") return 1
  t = ts_of(b)
  if (start != "" && t < start) return 0
  if (end != "" && t > end) return 0
  return 1
}
function process(b,   is_match) {
  if (!in_window(b)) return
  is_match = (pat == "" || b ~ pat)
  if (started || is_match) {
    blocks[n++] = b
    started = 1
    if (is_match) last_idx = n - 1
  }
}
BEGIN { started = 0; last_idx = -1; n = 0 }
/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / {
  if (buf != "") process(buf)
  buf = $0; next
}
{ buf = (buf == "" ? $0 : buf ORS $0) }
END {
  if (buf != "") process(buf)
  if (last_idx < 0) exit 2
  for (i = 0; i <= last_idx; i++) print blocks[i]
}
"""


@dataclass
class ExtractResult:
    server: str
    container: str
    filters: dict[str, str]
    content: str
    saved_to: Path | None
    total_blocks: int   # every block in the emitted span (matched + context)
    matched_blocks: int  # blocks that actually contain the pattern


def _count_blocks(text: str, pattern: str) -> tuple[int, int]:
    """Return (total_blocks, matched_blocks) in `text`.

    A block starts on a line beginning with `YYYY-MM-DD`. `matched_blocks`
    counts how many blocks actually contain `pattern` (empty pattern → 0).
    """
    if not text:
        return 0, 0
    regex = re.compile(pattern) if pattern else None
    total = 0
    matched = 0
    current: list[str] = []

    def _flush() -> None:
        nonlocal matched
        if current and regex is not None:
            block = "\n".join(current)
            if regex.search(block):
                matched += 1

    for line in text.splitlines():
        if len(line) >= 10 and line[:4].isdigit() and line[4] == "-":
            _flush()
            current = [line]
            total += 1
        else:
            current.append(line)
    _flush()
    return total, matched


def extract(
    server: Server,
    container: str,
    *,
    pattern: str = "",
    start: str = "",
    end: str = "",
    log_path: str = PROXY_LOG_PATH,
    timeout: float = 60.0,
) -> str:
    """Return log blocks matching the given filters (raw text)."""
    remote_cmd = (
        f"sudo -n docker exec {shlex.quote(container)} "
        f"awk -v pat={shlex.quote(pattern)} "
        f"-v start={shlex.quote(start)} "
        f"-v end={shlex.quote(end)} "
        f"{shlex.quote(_AWK_EXTRACTOR)} {shlex.quote(log_path)}"
    )
    result = subprocess.run(
        ["ssh", server.ssh_alias, remote_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # awk exits 2 (via our END block) when no blocks matched — treat as empty.
    if result.returncode == 2:
        return ""
    if result.returncode != 0:
        raise RuntimeError(
            f"remote awk failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def save_to_logs_dir(
    content: str,
    server_name: str,
    descriptor: str,
    logs_dir: Path = Path("logs"),
) -> Path:
    """Save `content` to logs/<server>_<descriptor>_<utc-timestamp>.log."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_desc = descriptor.replace("/", "_").replace(" ", "_")
    path = logs_dir / f"{server_name}_{safe_desc}_{stamp}.log"
    path.write_text(content, encoding="utf-8")
    return path


def extract_and_save(
    server: Server,
    container: str,
    *,
    descriptor: str,
    pattern: str = "",
    start: str = "",
    end: str = "",
    log_path: str = PROXY_LOG_PATH,
    logs_dir: Path = Path("logs"),
) -> ExtractResult:
    """Convenience: extract, save to logs/, and return a summary."""
    content = extract(
        server, container, pattern=pattern, start=start, end=end, log_path=log_path,
    )
    saved = save_to_logs_dir(content, server.name, descriptor, logs_dir) if content else None
    total, matched = _count_blocks(content, pattern)
    return ExtractResult(
        server=server.name,
        container=container,
        filters={"pattern": pattern, "start": start, "end": end},
        content=content,
        saved_to=saved,
        total_blocks=total,
        matched_blocks=matched,
    )

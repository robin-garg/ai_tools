"""Fetch proxy call logs for a given Call-ID from any configured server.

Routing (container vs host) is determined automatically by
``server.proxy_log_in_container`` (configured in servers.py):
  - True  → awk runs inside the docker/podman container via exec
  - False → awk runs directly on the host over SSH

Usage:
    uv run python scratch/fetch_call_logs.py <call_id> [minutes_back]

Defaults:
    minutes_back = 30  (search last 30 minutes; use 0 for no time filter)
"""
import subprocess
import shlex
import sys
from datetime import datetime, timedelta, timezone
from ai_tools.flexisip.servers import get_server
from ai_tools.flexisip.log_extractor import _AWK_EXTRACTOR, save_to_logs_dir

server   = get_server('stg2b')
log_path = server.proxy_log_path  # configured per-server in servers.py

call_id      = sys.argv[1] if len(sys.argv) > 1 else '6ksbudk66jfb5923lbnr'
minutes_back = int(sys.argv[2]) if len(sys.argv) > 2 else 30

now = datetime.now(timezone.utc)
if minutes_back > 0:
    start = (now - timedelta(minutes=minutes_back)).strftime('%Y-%m-%d %H:%M:%S')
    end   = now.strftime('%Y-%m-%d %H:%M:%S')
else:
    start = end = ''

print(f"Server  : {server.ssh_alias}", file=sys.stderr)
print(f"Log     : {log_path}", file=sys.stderr)
print(f"In container: {server.proxy_log_in_container}", file=sys.stderr)
print(f"Call-ID : {call_id}", file=sys.stderr)
if start:
    print(f"Window  : {start} → {end} UTC", file=sys.stderr)
else:
    print("Window  : entire log (no time filter)", file=sys.stderr)

remote_cmd = (
    f"sudo -n awk -v pat={shlex.quote(call_id)} "
    + (f"-v start={shlex.quote(start)} -v end={shlex.quote(end)} " if start else "")
    + f"{shlex.quote(_AWK_EXTRACTOR)} {shlex.quote(log_path)}"
)

result = subprocess.run(
    ['ssh', server.ssh_alias, remote_cmd],
    capture_output=True, text=True, timeout=120,
)

if result.returncode == 2 or not result.stdout.strip():
    print("No matching log blocks found.", file=sys.stderr)
    grep_cmd = (
        f"sudo -n grep -ic {shlex.quote(call_id)} {shlex.quote(log_path)}"
        f" 2>/dev/null || echo 0"
    )
    g = subprocess.run(['ssh', server.ssh_alias, grep_cmd],
                       capture_output=True, text=True, timeout=30)
    print(f"grep -ic match count in full log: {g.stdout.strip()}", file=sys.stderr)
    sys.exit(0)

if result.returncode != 0:
    print(f"ERROR ({result.returncode}): {result.stderr.strip()}", file=sys.stderr)
    sys.exit(1)

lines = result.stdout.splitlines()
ts_lines = [l for l in lines if len(l) > 10 and l[:4].isdigit()]
if ts_lines:
    print(f"First entry : {ts_lines[0][:23]} UTC", file=sys.stderr)
    print(f"Last entry  : {ts_lines[-1][:23]} UTC", file=sys.stderr)
print(f"Total lines : {len(lines)}", file=sys.stderr)

saved = save_to_logs_dir(result.stdout, server.name, f'callid-{call_id[:12]}')
print(f"Saved to    : {saved}", file=sys.stderr)
print(result.stdout)

#!/usr/bin/env python3
"""Registration gap report for a single user — shows only registered times and gaps."""
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta

PATH = "/tmp/reg.log"
IST = timedelta(hours=5, minutes=30)

pat = re.compile(
    r'^\w+\s+(\w+)\s+(\d+)\s+(\d{2}:\d{2}:\d{2})\s+(\d{4}):\s+(\w+)\s+<([^>]+)>'
)

times = []
for line in open(PATH):
    line = line.rstrip()
    if not line:
        continue
    m = pat.match(line)
    if not m:
        continue
    mon, day, tm, yr, verb, aor = m.groups()
    if verb.lower() != "registered":
        continue
    ts = datetime.strptime(f"{mon} {day} {yr} {tm}", "%b %d %Y %H:%M:%S")
    times.append(ts)

times.sort()

if not times:
    print("No Registered events found.")
    raise SystemExit(1)


def fmt_gap(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    elif secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}m {s:02d}s"
    else:
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}m {s:02d}s"


diffs = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]

print("=" * 55)
print(f"  User  : 1a97435c96ba")
print(f"  Dates : Apr 28 - Apr 29, 2026")
print(f"  Total : {len(times)} registration events")
print(
    f"  From  : {(times[0] + IST).strftime('%Y-%m-%d %H:%M:%S')} IST"
)
print(
    f"  To    : {(times[-1] + IST).strftime('%Y-%m-%d %H:%M:%S')} IST"
)
print("=" * 55)

print()
print(f"  {'#':>3}  {'Registered At (IST)':<22}  {'Gap to Next':>12}")
print("  " + "-" * 42)
for i, ts in enumerate(times, 1):
    ist = (ts + IST).strftime("%Y-%m-%d %H:%M:%S")
    gap = fmt_gap(diffs[i - 1]) if i <= len(diffs) else "—  (last)"
    print(f"  {i:>3}  {ist:<22}  {gap:>12}")

print()
print("=== Gap Summary ===")
print(f"  Min    : {fmt_gap(min(diffs))}")
print(f"  Median : {fmt_gap(statistics.median(diffs))}")
print(f"  Mean   : {fmt_gap(statistics.mean(diffs))}")
print(f"  Max    : {fmt_gap(max(diffs))}")

print()
print("=== Gap Distribution ===")
buckets = Counter()
for d in diffs:
    if d < 2:
        buckets["< 2s (burst)"] += 1
    elif d < 10:
        buckets["2-10s"] += 1
    elif d < 60:
        buckets["10-60s"] += 1
    elif d < 5 * 60:
        buckets["1-5 min"] += 1
    elif d < 20 * 60:
        buckets["5-20 min"] += 1
    elif d < 40 * 60:
        buckets["20-40 min"] += 1
    elif d < 90 * 60:
        buckets["40-90 min"] += 1
    else:
        buckets["> 90 min"] += 1

order = [
    "< 2s (burst)", "2-10s", "10-60s", "1-5 min",
    "5-20 min", "20-40 min", "40-90 min", "> 90 min",
]
for k in order:
    if buckets[k]:
        bar = "#" * buckets[k]
        print(f"  {k:<14}  {buckets[k]:>3}  {bar}")

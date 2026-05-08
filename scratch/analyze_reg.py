#!/usr/bin/env python3
"""Analyze Flexisip register event-log for a single user (one-off scratch script)."""
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta

PATH = "/tmp/reg.log"
IST = timedelta(hours=5, minutes=30)

pat = re.compile(
    r'^\w+\s+(\w+)\s+(\d+)\s+(\d{2}:\d{2}:\d{2})\s+(\d{4}):\s+(\w+)\s+<([^>]+)>\s+\((sip:[^)]+)\)'
)

events = []
for line in open(PATH):
    line = line.rstrip()
    if not line:
        continue
    m = pat.match(line)
    if not m:
        continue
    mon, day, tm, yr, verb, aor, contact = m.groups()
    ts = datetime.strptime(f"{mon} {day} {yr} {tm}", "%b %d %Y %H:%M:%S")
    mm = re.search(r'@([0-9.]+):(\d+)', contact)
    ip, port = (mm.group(1), int(mm.group(2))) if mm else (None, None)
    events.append((ts, verb, ip, port))

print(f"Total events     : {len(events)}")
print(f"Window (UTC)     : {events[0][0]} .. {events[-1][0]}")
print(f"Window (IST)     : {events[0][0]+IST} .. {events[-1][0]+IST}")
print(f"Verbs            : {dict(Counter(e[1] for e in events))}")
print(f"IPs              : {Counter(e[2] for e in events).most_common()}")
print(f"Unique (ip,port) : {len(set((e[2],e[3]) for e in events))}")

print("\n=== Per-hour (UTC) ===")
for h, n in sorted(Counter(e[0].strftime('%H') for e in events).items()):
    print(f"  {h}:00  {n:3d}  {'#'*n}")

print("\n=== Inter-registration intervals ===")
diffs = [(events[i+1][0]-events[i][0]).total_seconds() for i in range(len(events)-1)]
print(f"min={min(diffs):.0f}s  p50={statistics.median(diffs):.0f}s  "
      f"mean={statistics.mean(diffs):.0f}s  max={max(diffs):.0f}s")
buckets = Counter()
for d in diffs:
    if d < 2: buckets['< 2s (burst)'] += 1
    elif d < 10: buckets['2-10s'] += 1
    elif d < 60: buckets['10-60s'] += 1
    elif d < 5*60: buckets['1-5min'] += 1
    elif d < 20*60: buckets['5-20min'] += 1
    elif d < 40*60: buckets['20-40min'] += 1
    elif d < 90*60: buckets['40-90min'] += 1
    else: buckets['> 90min'] += 1
for k in ['< 2s (burst)','2-10s','10-60s','1-5min','5-20min','20-40min','40-90min','> 90min']:
    if buckets[k]:
        print(f"  {k:12s} {buckets[k]:3d}")

print("\n=== IP switches and port rotations ===")
ip_switches = sum(1 for i in range(1,len(events)) if events[i][2] != events[i-1][2])
same_ip_port = sum(1 for i in range(1,len(events))
                   if events[i][2] == events[i-1][2] and events[i][3] != events[i-1][3])
print(f"Consecutive IP switches       : {ip_switches}")
print(f"Port rotations (same IP)      : {same_ip_port}")

print("\n=== Bursts (>=2 registrations within 5s) ===")
bursts = []
i = 0
while i < len(events):
    j = i
    while j+1 < len(events) and (events[j+1][0]-events[j][0]).total_seconds() <= 5:
        j += 1
    if j > i:
        bursts.append((i, j))
    i = j + 1
print(f"Burst clusters: {len(bursts)}")
for a, b in bursts[:20]:
    print(f"  {events[a][0]}  ->  {events[b][0]}  ({b-a+1} regs)  "
          f"{events[a][2]}:{events[a][3]} -> {events[b][2]}:{events[b][3]}")

print("\n=== Full chronological timeline ===")
print(f"{'#':>3}  {'UTC':<19}  {'IST':<19}  {'Δ':>9}  {'IP':<16}  {'Port':>6}  Note")
prev_ts = None
prev_ip = None
prev_port = None
for i, (ts, verb, ip, port) in enumerate(events, 1):
    delta = '' if prev_ts is None else f"{(ts-prev_ts).total_seconds():.0f}s"
    note_parts = []
    if prev_ip and ip != prev_ip:
        note_parts.append(f"IP change ({prev_ip}->{ip})")
    elif prev_port and port != prev_port:
        note_parts.append("port rotate")
    note = ', '.join(note_parts)
    print(f"{i:>3}  {ts.strftime('%Y-%m-%d %H:%M:%S')}  "
          f"{(ts+IST).strftime('%Y-%m-%d %H:%M:%S')}  {delta:>9}  "
          f"{ip or '-':<16}  {port or 0:>6}  {note}")
    prev_ts, prev_ip, prev_port = ts, ip, port

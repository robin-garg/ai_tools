# ai-tools Skills Reference

> Updated: 2026-06-02 | Package: `src/ai_tools` | Entry point: `ai-tools` → `ai_tools.flexisip.cli:cli`

---

## Architecture

```
ai-tools
├── flexisip/           # Core SIP proxy tools
│   ├── servers.py          # Server registry (get_server, Server dataclass)
│   ├── sip_patterns.py     # 18 shared compiled regex patterns
│   ├── log_utils.py        # Shared utilities (split_blocks, fmt_gap, ts helpers)
│   ├── tunnel.py           # SSH port-forward to remote Redis
│   ├── registrar.py        # Redis registration queries
│   ├── parser.py           # SIP contact string → Registration dataclass
│   ├── docker_utils.py     # Remote Docker container discovery
│   ├── log_extractor.py    # AWK-based proxy log extraction over SSH
│   ├── expiration_notifier.py  # ContactExpirationNotifier log parser
│   ├── sip_agent_analyzer.py   # SIP flood / User-Agent traffic analyser
│   ├── iptables.py         # Remote iptables rule management (block/unblock/list)
│   ├── call_flow_analyzer.py   # Per-call SIP timeline analyser
│   ├── call_records_analyzer.py # Bulk CSV → log fetch → PDF pipeline
│   ├── reg_gap_analysis.py     # Registration gap PDF report
│   ├── pcap.py             # Live tcpdump capture over SSH
│   └── cli.py              # All CLI commands (Click group: flexisip)
├── pdf/
│   ├── writer.py           # markdown_to_pdf, text_to_pdf, rtf_to_pdf
│   └── cli.py              # pdf save / pdf text / pdf rtf commands
└── utils/
    └── organize_by_date.py # Move files into YYYY-MM-DD subdirectories
```

### Shared Modules
| Module | Purpose |
|--------|---------|
| `servers.py` · `get_server(name)` | Looks up a `Server` by name; raises `ValueError` for unknown names |
| `sip_patterns.py` | 18 pre-compiled regex patterns shared across all analysers |
| `log_utils.py` | `split_blocks_text`, `ts_to_dt`, `display_ts`, `ms_delta`, `fmt_gap` |

### Report Output Paths
| Type | Folder |
|------|--------|
| Registration gap PDFs | `reports/registration/<YYYY-MM-DD>/` |
| Call-flow & call-records PDFs/MD | `reports/calls/<YYYY-MM-DD>/` |
| Raw extracted logs | `logs/<server>_<descriptor>_<utc>.log` |

---

## Skills (10 Total)

| # | Skill | CLI Command(s) | Module |
|---|-------|---------------|--------|
| 1 | Registration Inspector | `count` `inspect` `show` `registrations` `user` | `registrar.py` + `parser.py` |
| 2 | Proxy Log Extractor | `logs` | `log_extractor.py` |
| 3 | Event Log Fetcher | `event-logs` | `log_extractor.py` |
| 4 | Registration Gap Analyzer | `reg-gap` | `reg_gap_analysis.py` |
| 5 | SIP Call Flow Analyzer | `call-flow` | `call_flow_analyzer.py` |
| 6 | Bulk Call Records Analyzer | `call-records` | `call_records_analyzer.py` |
| 7 | SIP Flood / Agent Detector | `agent-flood` | `sip_agent_analyzer.py` + `iptables.py` |
| 8 | Blocked IPs Manager | `blocked-ips` | `iptables.py` |
| 9 | Expiration Notifier Analyzer | `expiration-notifier` | `expiration_notifier.py` |
| 10 | Live SIP Capture | `pcap` | `pcap.py` |

> **Helper CLI (not a skill):** `containers` — lists Docker containers on a server.
> **PDF tools (not skills):** `pdf save` / `pdf text` / `pdf rtf` — standalone PDF converters.

---

## CLI Command Reference

### Registration Inspector

#### `flexisip count`
Count Redis registration keys matching a pattern.
```
-s / --server   REQUIRED   Target server
--pattern       default *  Redis key glob pattern
```

#### `flexisip inspect`
Show raw HGETALL for the first N matching keys.
```
-s / --server   REQUIRED
--pattern       default *
--limit         default 5
```

#### `flexisip show`
Show HGETALL for a specific Redis key.
```
-s / --server   REQUIRED
--key           REQUIRED   Full Redis key
```

#### `flexisip registrations`
Clean registration report for all registered devices.
```
-s / --server   REQUIRED
--platform      optional   android | ios | unknown
--domain        optional   Domain fragment filter
--pattern       default *  Redis key glob pattern
```

#### `flexisip user`
Registration details for a specific username.
```
-s / --server   REQUIRED
USERNAME        REQUIRED   positional
```

---

### Proxy Log Extractor

#### `flexisip logs`
Extract proxy logs matching a filter and print to stdout. At least one filter is required.
```
-s / --server   REQUIRED
-c / --call-id  optional   Filter by SIP Call-ID
-u / --user     optional   Filter by user extension (repeatable: -u A -u B)
--start         optional   UTC 'YYYY-MM-DD HH:MM:SS'
--end           optional   UTC 'YYYY-MM-DD HH:MM:SS'
--save          optional   Also write to logs/ directory
```
Output goes to stdout (pipeable). Summary (server, container, block counts) goes to stderr.

---

### Event Log Fetcher

#### `flexisip event-logs`
Fetch and display registration events from host-level Flexisip event logs (no Docker).
Shows a gap analysis inline.
```
-s / --server   REQUIRED
-u / --user     REQUIRED   SIP username (ERE pattern)
--hours         default 24 Look-back window (ignored when --start is set)
--start         optional   UTC 'YYYY-MM-DD HH:MM:SS' (overrides --hours)
--end           optional   UTC 'YYYY-MM-DD HH:MM:SS' (defaults to now)
--log-path      optional   Override server's configured event_log_path
--save          optional   Write filtered events to logs/
```

---

### Registration Gap Analyzer

#### `flexisip reg-gap`
Fetch live event logs and generate a colour-coded registration-gap PDF.
Always fetches live from the server. PDF auto-saved to `reports/registration/<date>/`.
```
-s / --server    REQUIRED
-u / --username  REQUIRED   Username / AOR fragment (e.g. 762cbe93429d)
--hours          default 24 Look-back window. Use 0 for all available history
```

---

### SIP Call Flow Analyzer

#### `flexisip call-flow`
Parse an extracted proxy log file and produce a per-call SIP timeline report.
Outputs Markdown + PDF to `reports/calls/<date>/`.
```
-l / --log-file  REQUIRED   Path to extracted proxy log file
-c / --caller    REQUIRED   Caller SIP username (e.g. 230)
-e / --callee    REQUIRED   Callee SIP username (e.g. 902493a6d16b)
--start          optional   UTC 'YYYY-MM-DD HH:MM:SS'
--end            optional   UTC 'YYYY-MM-DD HH:MM:SS'
--output-pdf     optional   Override PDF output path
--output-md      optional   Override Markdown output path
```

---

### Bulk Call Records Analyzer

#### `flexisip call-records`
Full pipeline: parse a CSV of call records → smart batch log fetch → per-call SIP analysis → PDF.
```
-f / --csv       REQUIRED   CSV file (needs Call Date/Time, To, From columns)
-s / --server    REQUIRED
-c / --container optional   Docker container name (auto-detected if omitted)
-o / --output    optional   Override PDF output path
--save           optional   Also persist each log batch to logs/
--window         default 30 Max minutes to group calls into one fetch batch
--buffer         default 2  Buffer minutes before/after each batch window
--tolerance      default 3  Max minutes between CSV time and log INVITE
```

> **CSV date format:** `May 04 2026 08:53:57 PM` (EDT, UTC−4)

---

### SIP Flood / Agent Detector

#### `flexisip agent-flood`
Detect IPs flooding with a specific User-Agent. Optionally block offenders with iptables.
```
-s / --server        REQUIRED
-a / --agent         default SIPVOIP  User-Agent substring (case-insensitive)
-m / --min-count     default 1        Only report IPs with >= N messages
--block / --no-block default no       Apply iptables DROP rules for all IPs
--port               default 5060     Destination port for the iptables rule
--proto              default udp      Protocol: udp | tcp
--dry-run            optional         Print iptables commands without executing
--start              optional         UTC 'YYYY-MM-DD HH:MM:SS'
--end                optional         UTC 'YYYY-MM-DD HH:MM:SS'
--save               optional         Save raw extracted logs to logs/
--list-rules         optional         Show current iptables INPUT rules after report
```

---

### Blocked IPs Manager

#### `flexisip blocked-ips`
List currently blocked IPs on a server, or unblock a specific IP.
```
-s / --server       REQUIRED
-u / --unblock      optional   IP address to unblock (omit to just list rules)
--port              default 5060  Port used in the original block rule
--proto             default udp   Protocol used in the original block rule
--chain             default INPUT iptables chain to inspect / modify
--dry-run           optional      Print unblock command without executing
```

**Default behaviour (no `--unblock`):** prints `iptables -L INPUT -n --line-numbers` output.

---

### Expiration Notifier Analyzer

#### `flexisip expiration-notifier`
Report `ContactExpirationNotifier` push activity: runs, users notified, provider, HTTP status.
```
-s / --server   REQUIRED
-u / --user     optional   Filter to a specific user extension
--start         optional   UTC 'YYYY-MM-DD HH:MM:SS'
--end           optional   UTC 'YYYY-MM-DD HH:MM:SS'
--save          optional   Save raw extracted logs to logs/
```

---

### Live SIP Capture

#### `flexisip pcap`
Run `sudo tcpdump` over SSH and stream SIP packets live to the terminal.
Press `Ctrl+C` to stop; output is saved to `logs/` automatically.
```
-s / --server       REQUIRED
-H / --source       optional   Source IP filter (repeatable: -H IP1 -H IP2)
-p / --port         default 5060  SIP port to capture on
-i / --iface        default any   Network interface on the remote host
--save / --no-save  default save  Save captured output to logs/ on stop
```

**Typical workflow:**
1. Run `pcap` — capture starts.
2. Make the call from Kazoo.
3. Press `Ctrl+C` — file saved to `logs/<server>_pcap_<utc>.log`.
4. Inspect the saved log to confirm the INVITE arrived.

---

### Helper: Docker Containers

#### `flexisip containers`
List running Docker containers on a server (used internally by other commands).
```
-s / --server   REQUIRED
```

---

## Module Reference

| Module | Key Exports |
|--------|------------|
| `servers.py` | `Server` (dataclass), `SERVERS` (dict), `get_server(name)` |
| `sip_patterns.py` | 18 compiled regexes: `BLOCK_START`, `CALL_ID`, `FROM_TAG`, `INVITE`, `CANCEL`, `BYE`, `REGISTER`, `VIA_RECEIVED`, `REDIS_CHECK`, `PUSH_FCM`, `PUSH_APNS`, `RING_180`, `RESPONSE_CODE`, `PNR_STATE`, `EXPIRY`, `USER_AGENT`, `SOURCE_IP`, `TIMESTAMP` |
| `log_utils.py` | `split_blocks_text(text)`, `ts_to_dt(ts)`, `display_ts(ts)`, `ms_delta(ts1, ts2)`, `fmt_gap(secs)` |
| `tunnel.py` | `ssh_tunnel(ssh_alias, remote_host, remote_port, ...)` context manager |
| `registrar.py` | `connect(server, local_port)`, `iter_keys`, `count_keys`, `get_entry`, `sample_entries`, `list_registrations` |
| `parser.py` | `Registration` (dataclass), `parse_registration(redis_key, instance_field, contact)` |
| `docker_utils.py` | `Container` (dataclass), `list_containers(server)`, `find_proxy_container(server)` |
| `log_extractor.py` | `ExtractResult` (dataclass), `extract`, `extract_and_save`, `extract_event_logs`, `save_to_logs_dir` |
| `expiration_notifier.py` | `PushEvent`, `NotifierRun` (dataclasses), `parse_notifier_log(log_text, user_filter)` |
| `sip_agent_analyzer.py` | `AgentHit` (dataclass), `parse_agent_traffic(log_text, agent_filter)` |
| `iptables.py` | `block_ip(server, ip, *, port, proto, chain, dry_run)`, `unblock_ip(server, ip, *, port, proto, chain, dry_run)`, `list_rules(server, chain)` |
| `call_flow_analyzer.py` | `CallFlowReport`, `CallFlow`, `CallEvent` (dataclasses), `analyze_log`, `render_markdown`, `analyze_and_render` |
| `call_records_analyzer.py` | `CallRecord`, `CallBatch`, `CallFlowSummary` (dataclasses), `parse_csv`, `group_into_batches`, `analyze_batch`, `analyze_call_records` |
| `reg_gap_analysis.py` | `Event` (type alias), `parse_events`, `generate_reg_gap_markdown`, `analyze_reg_gaps` |
| `pcap.py` | `run_pcap(server, *, iface, port, source_hosts, on_line)` |
| `pdf/writer.py` | `markdown_to_pdf`, `text_to_pdf`, `rtf_to_pdf`, `convert_utc_timestamps` |
| `utils/organize_by_date.py` | `extract_date(filename)`, `organize(directory)` |

---

## Design Rules

- **No hard-coded server values.** All dynamic inputs (log paths, SSH alias, container flag) come from `servers.py` via `get_server()`.
- **No default Call-IDs or usernames.** Every command requires explicit user-supplied identifiers.
- **All timestamps are UTC** in the CLI. IST conversions are display-only in report output.
- **Report folders are date-segregated:** `reports/registration/<YYYY-MM-DD>/` and `reports/calls/<YYYY-MM-DD>/`.
- **Proxy logs via AWK over SSH** (or `docker exec`) — no log files are copied to the local machine unless `--save` is passed.
- **iptables changes require passwordless `sudo`** on the remote host (same requirement as docker commands).

---

## Summary Statistics

| Metric | Count |
|--------|-------|
| Python source files | 17 |
| CLI commands (`flexisip` group) | 16 |
| PDF CLI commands (`pdf` group) | 3 |
| Skills | 10 |
| Shared regex patterns (`sip_patterns.py`) | 18 |
| Shared utility functions (`log_utils.py`) | 5 |

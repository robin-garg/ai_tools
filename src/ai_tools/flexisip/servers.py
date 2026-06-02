"""Registry of known Flexisip servers and their Redis endpoints.

Each server has:
- `ssh_alias`       : the Host alias from ~/.ssh/config used to open a tunnel
- `redis_host`      : the ElastiCache endpoint reachable from inside the VPC
- `redis_port`      : Redis port (default 6379)
- `proxy_log_path`  : path to the proxy log *inside* the flexisip-proxy
                      container (used by docker/podman exec)
- `event_log_path`  : path to the registration event-logs *on the host*
                      filesystem (used via direct SSH, no container)

PRODUCTION SERVER POLICY  (applies to BOTH "prod" and "prod2")
--------------------------------------------------------------
Only READ operations are permitted on production servers:
  - Redis: GET, HGETALL, KEYS, SCAN, TTL, TYPE — no writes
  - Flexisip socket: REGISTRAR_GET, CONFIG_GET, CONFIG_LIST — no mutations
  - Logs: cat, tail, grep on log files via podman/docker exec or direct SSH

NEVER make configuration changes, write to Redis, restart services, or perform
any other mutating operation on any production server.
If a task cannot be completed without a change → stop and ask the user first.

Servers
-------
  prod   : flexisip.e1a.aws.wlcomm.net      — stable production
  prod2  : flexisip-v2.e1a.aws.wlcomm.net   — v2 production (latest changes)
  stg2   : flexisip-stg2.e1a.stg2.wlclabs.net  — staging (read/write OK)
  stg2b  : flexisip-stg2b.e1a.stg2.wlclabs.net — staging (read/write OK)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Server:
    name: str
    ssh_alias: str
    redis_host: str
    redis_port: int = 6379
    # Path to the proxy log file.  When proxy_log_in_container=True this is the
    # path *inside* the container; when False it is a direct host filesystem path.
    proxy_log_path: str = "/usr/local/var/log/flexisip/flexisip-proxy.log"
    # Path to registration event-logs on the filesystem (always host-level, no container).
    event_log_path: str = "/var/log/flexisip/event-logs"
    # True  → proxy log lives inside a Docker/Podman container (use exec to read it).
    # False → proxy log is on the host filesystem (fetch directly over SSH).
    proxy_log_in_container: bool = True


SERVERS: dict[str, Server] = {
    "stg2": Server(
        name="stg2",
        ssh_alias="stg2",
        redis_host="flexisip-stg2.y19cqc.ng.0001.use1.cache.amazonaws.com",
        proxy_log_path="/usr/local/var/log/flexisip/flexisip-proxy.log",
        event_log_path="/var/log/flexisip/event-logs",
    ),
    "stg2b": Server(
        name="stg2b",
        ssh_alias="stg2b",
        redis_host="flexisip-stg2b.y19cqc.ng.0001.use1.cache.amazonaws.com",
        proxy_log_path="/var/log/flexisip/flexisip-proxy.log",
        event_log_path="/var/log/flexisip/event-logs",
        proxy_log_in_container=False,   # log lives on the host filesystem (Podman host mount)
    ),
    # ⚠️  PRODUCTION — READ-ONLY.  No config changes, no writes, no restarts.
    "prod": Server(
        name="prod",
        ssh_alias="prod",
        redis_host="ucaas-prod-flexisip-prod.qiser2.ng.0001.use1.cache.amazonaws.com",
        proxy_log_path="/usr/local/var/log/flexisip/flexisip-proxy.log",
        event_log_path="/var/log/flexisip/event-logs",
    ),
    # ⚠️  PRODUCTION v2 — READ-ONLY.  Same policy as "prod". Latest code changes.
    # Proxy log and event-logs both live on the host filesystem (same layout as stg2b).
    "prod2": Server(
        name="prod2",
        ssh_alias="prod2",
        redis_host="ucaas-prod-flexisip-prod-v2.qiser2.ng.0001.use1.cache.amazonaws.com",
        proxy_log_path="/var/log/flexisip/flexisip-proxy.log",
        event_log_path="/var/log/flexisip/event-logs",
        proxy_log_in_container=False,   # log lives on the host filesystem (same as stg2b)
    ),
}


def get_server(name: str) -> Server:
    try:
        return SERVERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(SERVERS))
        raise ValueError(f"Unknown server '{name}'. Known: {known}") from exc

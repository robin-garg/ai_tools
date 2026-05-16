"""Registry of known Flexisip servers and their Redis endpoints.

Each server has:
- `ssh_alias`       : the Host alias from ~/.ssh/config used to open a tunnel
- `redis_host`      : the ElastiCache endpoint reachable from inside the VPC
- `redis_port`      : Redis port (default 6379)
- `proxy_log_path`  : path to the proxy log *inside* the flexisip-proxy
                      container (used by docker/podman exec)
- `event_log_path`  : path to the registration event-logs *on the host*
                      filesystem (used via direct SSH, no container)

PRODUCTION SERVER POLICY
------------------------
The "prod" entry below is the PRODUCTION server (flexisip.e1a.aws.wlcomm.net).
Only READ operations are permitted on this server:
  - Fetching registrations (Redis reads / SCAN)
  - Fetching / tailing logs (docker exec, SSH cat/tail)
NEVER make configuration changes, write to Redis, restart services, or perform
any other mutating operation on the production server.
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
        proxy_log_path="/usr/local/var/log/flexisip/flexisip-proxy.log",
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
}


def get_server(name: str) -> Server:
    try:
        return SERVERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(SERVERS))
        raise ValueError(f"Unknown server '{name}'. Known: {known}") from exc

"""Registry of known Flexisip servers and their Redis endpoints.

Each server has:
- `ssh_alias`: the Host alias from ~/.ssh/config used to open a tunnel
- `redis_host` / `redis_port`: the ElastiCache endpoint reachable from inside
  the server's VPC
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Server:
    name: str
    ssh_alias: str
    redis_host: str
    redis_port: int = 6379


SERVERS: dict[str, Server] = {
    "stg2": Server(
        name="stg2",
        ssh_alias="stg2",
        redis_host="flexisip-stg2.y19cqc.ng.0001.use1.cache.amazonaws.com",
    ),
    "stg2b": Server(
        name="stg2b",
        ssh_alias="stg2b",
        redis_host="flexisip-stg2b.y19cqc.ng.0001.use1.cache.amazonaws.com",
    ),
}


def get_server(name: str) -> Server:
    try:
        return SERVERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(SERVERS))
        raise ValueError(f"Unknown server '{name}'. Known: {known}") from exc

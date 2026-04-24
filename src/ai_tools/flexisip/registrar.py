"""Redis queries against Flexisip's RegistrarDb.

Opens an SSH tunnel to the configured server's Redis endpoint and exposes
small, typed helpers for enumerating and inspecting keys.

The shape of a registration entry varies by Flexisip version, so these helpers
return raw field maps; higher-level typed models come in a later phase once
real data has been observed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import redis

from ai_tools.flexisip.parser import Registration, parse_registration
from ai_tools.flexisip.servers import Server
from ai_tools.flexisip.tunnel import DEFAULT_LOCAL_PORT, ssh_tunnel


SCAN_COUNT_HINT = 500


@contextlib.contextmanager
def connect(
    server: Server,
    local_port: int = DEFAULT_LOCAL_PORT,
) -> Iterator[redis.Redis]:
    """Open an SSH tunnel and yield a connected Redis client."""
    with ssh_tunnel(
        ssh_alias=server.ssh_alias,
        remote_host=server.redis_host,
        remote_port=server.redis_port,
        local_port=local_port,
    ) as tunnel:
        client = redis.Redis(
            host=tunnel.local_host,
            port=tunnel.local_port,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        try:
            client.ping()
            yield client
        finally:
            client.close()


def iter_keys(client: redis.Redis, pattern: str = "*") -> Iterator[str]:
    """Iterate over all keys matching `pattern` using non-blocking SCAN."""
    for key in client.scan_iter(match=pattern, count=SCAN_COUNT_HINT):
        yield key


def count_keys(client: redis.Redis, pattern: str = "*") -> int:
    """Return the number of keys matching `pattern`."""
    return sum(1 for _ in iter_keys(client, pattern))


def get_entry(client: redis.Redis, key: str) -> dict[str, str] | None:
    """Return the HGETALL map for `key`, or None if the key does not exist
    or is not a hash."""
    key_type = client.type(key)
    if key_type == "none":
        return None
    if key_type != "hash":
        return {"__type__": key_type}
    return dict(client.hgetall(key))


def sample_entries(
    client: redis.Redis,
    limit: int,
    pattern: str = "*",
) -> list[tuple[str, dict[str, str] | None]]:
    """Return up to `limit` (key, entry) pairs for raw inspection."""
    out: list[tuple[str, dict[str, str] | None]] = []
    for key in iter_keys(client, pattern):
        out.append((key, get_entry(client, key)))
        if len(out) >= limit:
            break
    return out


def list_registrations(
    client: redis.Redis,
    pattern: str = "fs:*",
) -> list[Registration]:
    """Return all parsed Registration objects from matching Redis keys.

    Each device contact is a separate Registration entry — a single SIP user
    registered from two devices yields two Registration objects.
    """
    results: list[Registration] = []
    for key in iter_keys(client, pattern):
        entry = get_entry(client, key)
        if not entry:
            continue
        for instance_field, contact in entry.items():
            if instance_field == "__type__":
                continue
            results.append(parse_registration(key, instance_field, contact))
    return results

"""Flexisip registration inspection tools.

Exposes a small library for connecting to a Flexisip server's Redis RegistrarDb
via an SSH tunnel and querying registered users.
"""

from ai_tools.flexisip.servers import SERVERS, Server, get_server

__all__ = ["SERVERS", "Server", "get_server"]

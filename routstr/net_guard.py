"""Destination checks for URLs a client chose and the node will connect to."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedDestinationError(ValueError):
    """The URL points at something the node must not connect to."""


def is_blocked_address(address: str) -> bool:
    """Allow only globally reachable addresses (RFC 6890)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        # An embedded v4 address would otherwise smuggle a rejected target past
        # the v6 checks.
        for embedded in (ip.ipv4_mapped, ip.sixtofour):
            if embedded is not None:
                return is_blocked_address(str(embedded))
    return not ip.is_global or ip.is_multicast


async def assert_public_https_origin(url: str) -> None:
    """Reject a client-supplied origin unless it is HTTPS to a public host.

    Used for mints named inside incoming Cashu tokens: the token is
    unauthenticated input, so without this the node would open connections to
    whatever address the sender wrote into it. HTTPS is required because the
    host is only verified by name here; certificate validation binds the later
    connection to the same name.
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise BlockedDestinationError("mint URL must use https")
    if parts.username is not None or parts.password is not None:
        raise BlockedDestinationError("mint URL must not carry credentials")
    if parts.query or parts.fragment:
        raise BlockedDestinationError("mint URL must not carry a query or fragment")
    host = parts.hostname
    if not host:
        raise BlockedDestinationError("mint URL has no host")
    try:
        port = parts.port or 443
    except ValueError as error:
        raise BlockedDestinationError("mint URL port is invalid") from error
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as error:
        raise BlockedDestinationError("mint host did not resolve") from error
    if not infos:
        raise BlockedDestinationError("mint host did not resolve")
    for info in infos:
        if is_blocked_address(str(info[4][0])):
            raise BlockedDestinationError("mint host resolves to a blocked address")

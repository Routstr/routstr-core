"""Destination checks for URLs a client chose and the node will connect to."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx

# The lookup runs before the HTTP client's own timeout applies, so a slow
# resolver for an attacker-chosen name would otherwise stall the caller.
DNS_LOOKUP_TIMEOUT_SECONDS = 5.0


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


async def validated_fetch_target(url: str) -> tuple[str, str]:
    """Return the URL to request and its ``Host`` header.

    For URLs taken from unauthenticated input (image URLs in a request body,
    endpoints in provider announcements) that would otherwise aim the node at
    internal hosts. HTTP is rewritten to the resolved address so the name cannot
    rebind between check and connect; HTTPS keeps its hostname because
    certificate validation already binds the connection.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedDestinationError(f"unsupported scheme: {parts.scheme or 'none'}")
    if not parts.hostname:
        raise BlockedDestinationError("missing host")
    # httpx connects to the IDNA 2008 form of the host; resolving the Unicode
    # name would use Python's IDNA 2003 codec, which maps some names ("ß")
    # elsewhere, and a Unicode Host header cannot be encoded at all.
    try:
        host = httpx.URL(url).raw_host.decode("ascii")
    except httpx.InvalidURL as error:
        raise BlockedDestinationError("invalid host") from error

    default_port = 443 if parts.scheme == "https" else 80
    port = parts.port or default_port
    host_header = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        host_header = f"{host_header}:{parts.port}"

    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host, port, proto=socket.IPPROTO_TCP
            ),
            DNS_LOOKUP_TIMEOUT_SECONDS,
        )
    except (socket.gaierror, TimeoutError) as error:
        raise BlockedDestinationError("host did not resolve") from error
    if not infos:
        raise BlockedDestinationError("host did not resolve")
    for info in infos:
        if is_blocked_address(str(info[4][0])):
            raise BlockedDestinationError("host resolves to a blocked address")

    if parts.scheme == "https":
        return url, host_header

    family, _, _, _, sockaddr = infos[0]
    address = str(sockaddr[0])
    pinned = f"[{address}]" if family == socket.AF_INET6 else address
    if parts.port is not None:
        pinned = f"{pinned}:{parts.port}"
    return urlunsplit((parts.scheme, pinned, parts.path, parts.query, "")), host_header

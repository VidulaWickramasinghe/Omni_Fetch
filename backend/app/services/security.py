"""URL validation used both before admission and inside download workers."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from ..config import Settings
from .platform import is_known_platform_url


class UnsafeURLError(ValueError):
    """Raised when a URL must not be fetched."""


Resolver = Callable[[str, int | None], Iterable[tuple]]


def validate_url(
    url: str,
    settings: Settings,
    *,
    resolver: Resolver | None = None,
) -> str:
    """Return a safe public URL or raise ``UnsafeURLError``."""

    if len(url) > settings.max_url_length:
        raise UnsafeURLError("URL is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise UnsafeURLError("URL contains control characters")
    if "\\" in url:
        raise UnsafeURLError("Backslashes are not allowed in URLs")

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL contains an invalid hostname or port") from exc

    if parsed.scheme.lower() not in settings.allowed_schemes:
        raise UnsafeURLError(f"URL scheme '{parsed.scheme}' is not allowed")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("Credentials are not allowed in URLs")
    if port is not None and port not in settings.allowed_ports:
        raise UnsafeURLError(f"URL port {port} is not allowed")
    if (
        settings.public_mode
        and not settings.allow_generic_extractor
        and not is_known_platform_url(url)
    ):
        raise UnsafeURLError("Generic website extraction is disabled on this instance")

    hostname = parsed.hostname.rstrip(".")
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(hostname, port)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UnsafeURLError(f"Could not resolve host '{hostname}'") from exc

    found_address = False
    for _family, _type, _proto, _canonname, sockaddr in infos:
        try:
            address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeURLError("Host resolved to an invalid address") from exc
        found_address = True
        if not address.is_global or address.is_multicast:
            raise UnsafeURLError("URL resolves to a non-public address")
    if not found_address:
        raise UnsafeURLError(f"Could not resolve host '{hostname}'")
    return url

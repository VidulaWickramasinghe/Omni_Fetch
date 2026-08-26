from __future__ import annotations

import socket
from collections.abc import Callable

import pytest

from app.services.security import UnsafeURLError, validate_url


def resolving_to(*addresses: str) -> Callable[[str, int | None], list[tuple]]:
    def resolver(_hostname: str, port: int | None) -> list[tuple]:
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 0))
            for address in addresses
        ]

    return resolver


def test_accepts_https_url_when_every_dns_answer_is_global(settings_factory) -> None:
    url = "https://media.example/video?id=123"

    result = validate_url(
        url,
        settings_factory(),
        resolver=resolving_to("93.184.216.34", "2606:4700:4700::1111"),
    )

    assert result == url


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
        "ff02::1",
    ],
)
def test_rejects_every_non_global_dns_answer(settings_factory, address: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url(
            "https://media.example/video",
            settings_factory(),
            resolver=resolving_to(address),
        )


def test_rejects_mixed_public_and_private_dns_answers(settings_factory) -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url(
            "https://media.example/video",
            settings_factory(),
            resolver=resolving_to("93.184.216.34", "127.0.0.1"),
        )


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "scheme"),
        ("https://", "hostname"),
        ("https://user:secret@example.com/video", "Credentials"),
        ("https://example.com:22/video", "port 22"),
        ("https://example.com\\@127.0.0.1/video", "Backslashes"),
        ("https://example.com/video\nnext", "control"),
        ("https://[broken/video", "invalid hostname"),
    ],
)
def test_rejects_unsafe_url_syntax(settings_factory, url: str, message: str) -> None:
    with pytest.raises(UnsafeURLError, match=message):
        validate_url(url, settings_factory(), resolver=resolving_to("93.184.216.34"))


def test_rejects_overlong_url_before_dns(settings_factory) -> None:
    resolver_called = False

    def resolver(_hostname: str, _port: int | None) -> list[tuple]:
        nonlocal resolver_called
        resolver_called = True
        return []

    with pytest.raises(UnsafeURLError, match="too long"):
        validate_url("https://example.com/" + "a" * 300, settings_factory(), resolver=resolver)
    assert resolver_called is False


def test_rejects_empty_or_failed_resolution(settings_factory) -> None:
    with pytest.raises(UnsafeURLError, match="Could not resolve"):
        validate_url("https://example.com/video", settings_factory(), resolver=lambda *_: [])

    def failed(*_args):
        raise socket.gaierror("not found")

    with pytest.raises(UnsafeURLError, match="Could not resolve"):
        validate_url("https://example.com/video", settings_factory(), resolver=failed)


def test_public_mode_rejects_unknown_platform_before_dns(settings_factory) -> None:
    settings = settings_factory(public_mode=True, allow_generic_extractor=False)

    with pytest.raises(UnsafeURLError, match="Generic website extraction is disabled"):
        validate_url(
            "https://example.com/video",
            settings,
            resolver=resolving_to("93.184.216.34"),
        )

    assert (
        validate_url(
            "https://youtu.be/example",
            settings,
            resolver=resolving_to("142.250.70.174"),
        )
        == "https://youtu.be/example"
    )

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from app.services.authentication import (
    AuthenticationUnavailable,
    authentication_available,
    create_cookie_copy,
    remove_cookie_copy,
    require_authentication,
)

COOKIE_DATA = (
    b"# Netscape HTTP Cookie File\n"
    b".example.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tsecret-value\n"
)


def configured_settings(settings_factory, tmp_path: Path, data: bytes = COOKIE_DATA):
    cookie_file = tmp_path / "session.cookies.txt"
    cookie_file.write_bytes(data)
    return settings_factory(
        authenticated_media_enabled=True,
        cookie_file=cookie_file,
    )


def test_authentication_is_opt_in_and_fails_closed(settings_factory) -> None:
    settings = settings_factory()

    assert authentication_available(settings) is False
    require_authentication(settings, False)
    with pytest.raises(AuthenticationUnavailable, match="not configured"):
        require_authentication(settings, True)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "Netscape format"),
        (b"{}", "Netscape format"),
        (b"# Netscape HTTP Cookie File\n", "no usable"),
        (b"# Netscape HTTP Cookie File\ninvalid\n", "no usable"),
        (b"# Netscape HTTP Cookie File\nline\x00value\n", "invalid"),
    ],
)
def test_cookie_validation_rejects_unsafe_or_unusable_files(
    settings_factory, tmp_path: Path, data: bytes, message: str
) -> None:
    settings = configured_settings(settings_factory, tmp_path, data)

    with pytest.raises(AuthenticationUnavailable, match=message):
        require_authentication(settings, True)
    assert authentication_available(settings) is False


def test_cookie_validation_rejects_symlink_and_size_overflow(
    settings_factory, tmp_path: Path
) -> None:
    real_cookie = tmp_path / "real.txt"
    real_cookie.write_bytes(COOKIE_DATA)
    link = tmp_path / "link.txt"
    link.symlink_to(real_cookie)

    with pytest.raises(AuthenticationUnavailable, match="invalid"):
        require_authentication(
            settings_factory(
                authenticated_media_enabled=True,
                cookie_file=link,
            ),
            True,
        )
    with pytest.raises(AuthenticationUnavailable, match="too large"):
        require_authentication(
            settings_factory(
                authenticated_media_enabled=True,
                cookie_file=real_cookie,
                max_cookie_file_bytes=len(COOKIE_DATA) - 1,
            ),
            True,
        )


def test_cookie_copy_is_private_writable_and_removable(settings_factory, tmp_path: Path) -> None:
    settings = configured_settings(settings_factory, tmp_path)
    operation_root = tmp_path / "operation"

    cookie_copy = create_cookie_copy(settings, operation_root)

    assert cookie_copy.read_bytes() == COOKIE_DATA
    assert stat.S_IMODE(cookie_copy.stat().st_mode) == 0o600
    assert stat.S_IMODE(cookie_copy.parent.stat().st_mode) == 0o700
    assert cookie_copy != settings.cookie_file

    remove_cookie_copy(cookie_copy)

    assert not cookie_copy.exists()
    assert not cookie_copy.parent.exists()
    assert settings.cookie_file is not None
    assert settings.cookie_file.read_bytes() == COOKIE_DATA

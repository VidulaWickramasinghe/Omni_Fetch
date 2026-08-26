"""Server-owned cookie authentication with short-lived operation copies."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from ..config import Settings

_COOKIE_HEADERS = {b"# HTTP Cookie File", b"# Netscape HTTP Cookie File"}


class AuthenticationUnavailable(ValueError):
    """The configured authenticated-media session cannot be used safely."""


def _cookie_bytes(settings: Settings) -> bytes:
    if not settings.authenticated_media_enabled or settings.cookie_file is None:
        raise AuthenticationUnavailable("Authenticated media is not configured on this instance")

    configured = settings.cookie_file
    descriptor = -1
    try:
        if configured.is_symlink():
            raise AuthenticationUnavailable("The configured cookie file is invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(configured, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuthenticationUnavailable("The configured cookie file is invalid")
        if metadata.st_size > settings.max_cookie_file_bytes:
            raise AuthenticationUnavailable("The configured cookie file is too large")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            data = stream.read(settings.max_cookie_file_bytes + 1)
    except AuthenticationUnavailable:
        raise
    except (OSError, RuntimeError) as exc:
        raise AuthenticationUnavailable(
            "The configured authenticated-media session is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(data) > settings.max_cookie_file_bytes:
        raise AuthenticationUnavailable("The configured cookie file is too large")
    if b"\x00" in data:
        raise AuthenticationUnavailable("The configured cookie file is invalid")
    lines = data.splitlines()
    if not lines or lines[0].removeprefix(b"\xef\xbb\xbf") not in _COOKIE_HEADERS:
        raise AuthenticationUnavailable("The configured cookie file must use Netscape format")
    cookie_lines = [
        line
        for line in lines[1:]
        if line and (not line.startswith(b"#") or line.startswith(b"#HttpOnly_"))
    ]
    if not cookie_lines or any(len(line.split(b"\t", 6)) != 7 for line in cookie_lines):
        raise AuthenticationUnavailable(
            "The configured cookie file contains no usable Netscape cookies"
        )
    return data


def authentication_available(settings: Settings) -> bool:
    try:
        _cookie_bytes(settings)
    except AuthenticationUnavailable:
        return False
    return True


def require_authentication(settings: Settings, use_auth: bool) -> None:
    if use_auth:
        _cookie_bytes(settings)


def create_cookie_copy(settings: Settings, parent: Path) -> Path:
    """Create a private writable copy because yt-dlp updates its cookie jar."""

    data = _cookie_bytes(settings)
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=".auth-", dir=parent))
    directory.chmod(0o700)
    cookie_path = directory / "cookies.txt"
    try:
        descriptor = os.open(cookie_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        return cookie_path
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def remove_cookie_copy(cookie_path: Path | None) -> None:
    if cookie_path is None:
        return
    directory = cookie_path.parent
    try:
        cookie_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(directory, ignore_errors=True)

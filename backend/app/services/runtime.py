"""Resolve the media tools required by yt-dlp across Docker and source installs."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

import imageio_ffmpeg
from yt_dlp.dependencies import curl_cffi, yt_dlp_ejs

from ..config import Settings

_JS_EXECUTABLES = (
    ("deno", "deno"),
    ("node", "node"),
    ("quickjs", "qjs"),
)


def _executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


@lru_cache(maxsize=1)
def _automatic_ffmpeg() -> str | None:
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError:
        return None
    resolved = Path(bundled).expanduser().resolve(strict=False)
    return str(resolved) if _executable_file(resolved) else None


def resolve_ffmpeg_location(settings: Settings) -> str | None:
    """Return an explicit, system, or wheel-bundled FFmpeg executable."""

    if settings.ffmpeg_location:
        configured = Path(settings.ffmpeg_location).expanduser().resolve(strict=False)
        candidate = configured / "ffmpeg" if configured.is_dir() else configured
        return str(candidate) if _executable_file(candidate) else None
    return _automatic_ffmpeg()


@lru_cache(maxsize=1)
def resolve_js_runtimes() -> dict[str, dict[str, str]]:
    """Enable the best installed runtime instead of relying on Deno-only defaults."""

    for runtime, executable in _JS_EXECUTABLES:
        path = shutil.which(executable)
        if path:
            return {runtime: {"path": path}}
    return {}


def ejs_available() -> bool:
    """Whether the matching yt-dlp external challenge scripts are installed."""

    return yt_dlp_ejs is not None


def impersonation_available() -> bool:
    """Whether yt-dlp can impersonate a browser for TLS-sensitive sources."""

    return curl_cffi is not None


def ytdlp_runtime_options(settings: Settings) -> dict[str, object]:
    """Build the safe local runtime subset shared by extraction and download."""

    options: dict[str, object] = {}
    if ffmpeg := resolve_ffmpeg_location(settings):
        options["ffmpeg_location"] = ffmpeg
    if runtimes := resolve_js_runtimes():
        options["js_runtimes"] = runtimes
    return options

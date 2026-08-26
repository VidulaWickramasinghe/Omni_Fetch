"""Safe metadata extraction and normalized quality choices."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp

from app.config import Settings
from app.models import ExtractResponse, QualityInfo
from app.services.authentication import create_cookie_copy, remove_cookie_copy
from app.services.platform import platform_from_info
from app.services.runtime import ytdlp_runtime_options


class UnsupportedCollectionError(ValueError):
    """The MVP intentionally accepts only one media item per request."""


class GenericExtractorDisabled(ValueError):
    """Raised when public-mode policy disallows yt-dlp's generic extractor."""


def base_options(settings: Settings, cookie_file: Path | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "skip_download": True,
        "socket_timeout": settings.socket_timeout_seconds,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "cachedir": False,
    }
    if cookie_file is not None:
        options["cookiefile"] = str(cookie_file)
    options.update(ytdlp_runtime_options(settings))
    return options


def ensure_single_item(info: dict | None) -> dict:
    if not info:
        raise ValueError("No media information was extracted")
    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise UnsupportedCollectionError("Playlists and multi-item URLs are not supported")
    return info


def ensure_extractor_allowed(info: dict, settings: Settings) -> None:
    key = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if settings.public_mode and not settings.allow_generic_extractor and key == "generic":
        raise GenericExtractorDisabled("Generic website extraction is disabled on this instance")


def _qualities(formats: list[dict]) -> list[QualityInfo]:
    audio_sizes = [
        item.get("filesize") or item.get("filesize_approx")
        for item in formats
        if item.get("vcodec") in {None, "none"}
        and item.get("acodec") not in {None, "none"}
        and not item.get("has_drm")
    ]
    known_audio_sizes = [size for size in audio_sizes if isinstance(size, int) and size > 0]
    best_audio_size = max(known_audio_sizes) if known_audio_sizes else None
    by_height: dict[int, dict] = {}
    for item in formats:
        if item.get("has_drm"):
            continue
        if item.get("vcodec") in {None, "none"}:
            continue
        height = item.get("height")
        if not isinstance(height, int) or height <= 0:
            continue
        score = (
            item.get("filesize") or item.get("filesize_approx") or 0,
            item.get("fps") or 0,
            item.get("tbr") or 0,
        )
        previous = by_height.get(height)
        previous_score = (
            (
                previous.get("filesize") or previous.get("filesize_approx") or 0,
                previous.get("fps") or 0,
                previous.get("tbr") or 0,
            )
            if previous
            else (-1, -1, -1)
        )
        if score > previous_score:
            by_height[height] = item

    return [
        QualityInfo(
            id=f"height:{height}",
            label=f"{height}p",
            height=height,
            fps=by_height[height].get("fps"),
            note=by_height[height].get("format_note"),
            estimated_size=(
                (by_height[height].get("filesize") or by_height[height].get("filesize_approx"))
                + best_audio_size
                if best_audio_size
                and (by_height[height].get("filesize") or by_height[height].get("filesize_approx"))
                else None
            ),
        )
        for height in sorted(by_height, reverse=True)
    ]


def extract_metadata(
    url: str,
    settings: Settings,
    *,
    use_auth: bool = False,
    ydl_factory: Callable[..., Any] = yt_dlp.YoutubeDL,
) -> ExtractResponse:
    cookie_file = create_cookie_copy(settings, settings.download_dir) if use_auth else None
    try:
        with ydl_factory(base_options(settings, cookie_file)) as ydl:
            info = ensure_single_item(ydl.extract_info(url, download=False))
    finally:
        remove_cookie_copy(cookie_file)
    ensure_extractor_allowed(info, settings)

    formats = list(info.get("formats") or [])
    supports_video = any(
        item.get("vcodec") not in {None, "none"} and not item.get("has_drm") for item in formats
    )
    supports_audio = any(
        item.get("acodec") not in {None, "none"} and not item.get("has_drm") for item in formats
    )
    return ExtractResponse(
        platform=platform_from_info(info, url),
        title=info.get("title"),
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        uploader=info.get("uploader") or info.get("channel"),
        is_live=bool(info.get("is_live") or info.get("live_status") == "is_live"),
        authenticated=use_auth,
        qualities=_qualities(formats),
        supports_video=supports_video,
        supports_audio=supports_audio,
    )

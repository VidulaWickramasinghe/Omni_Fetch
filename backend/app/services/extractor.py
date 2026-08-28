"""Safe metadata extraction and normalized quality choices."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp

from ..config import Settings
from ..models import ExtractResponse, QualityInfo
from .authentication import create_cookie_copy, remove_cookie_copy
from .platform import platform_from_info
from .runtime import browser_impersonation_options, ytdlp_runtime_options


class UnsupportedCollectionError(ValueError):
    """The MVP intentionally accepts only one media item per request."""


class GenericExtractorDisabled(ValueError):
    """Raised when public-mode policy disallows yt-dlp's generic extractor."""


_TRANSIENT_EXTRACTION_MARKERS = (
    "universal data for rehydration",
    "unable to solve js challenge",
    "unexpected response from webpage request",
    "http error 403",
    "http error 429",
    "too many requests",
    "account authentication is required",
)

_VIDEO_EXTENSIONS = frozenset({"3gp", "avi", "flv", "m4v", "mkv", "mov", "mp4", "webm"})
_AUDIO_EXTENSIONS = frozenset({"aac", "flac", "m4a", "mp3", "ogg", "opus", "wav", "weba"})


def is_retryable_extraction_error(error: yt_dlp.utils.DownloadError) -> bool:
    """Recognize short-lived source verification and throttling responses."""

    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_EXTRACTION_MARKERS)


def safe_extraction_error(error: yt_dlp.utils.DownloadError) -> str:
    """Classify extraction failures without returning URLs or signed tokens."""

    message = str(error).lower()
    if "impersonation" in message or "universal data for rehydration" in message:
        return "The source's browser verification could not be completed"
    if "private video" in message or "sign in" in message or "login" in message:
        return "The source requires an authorized login session"
    if "authentication is required" in message:
        return "The source requires an authorized login session"
    if "video unavailable" in message or "media is unavailable" in message:
        return "The source reports that this media is unavailable"
    if "http error 404" in message or "not found" in message:
        return "The source media link is unavailable or has expired"
    if "http error 400" in message or "bad request" in message:
        return "The source did not recognize this media URL"
    if "no media found" in message or "no video formats" in message:
        return "No downloadable video or audio streams were found at this URL"
    if "unsupported url" in message:
        return "This URL is not supported by the configured extraction engine"
    return "The source could not be inspected"


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


def _normalized_formats(info: dict) -> list[dict]:
    """Include direct-file extractors that expose media only at the top level."""

    formats = [item for item in info.get("formats") or [] if isinstance(item, dict)]
    if not formats and info.get("url"):
        formats.append(info)
    return formats


def _media_types(item: dict) -> tuple[bool, bool]:
    """Infer streams when an extractor knows the container but omits codec probes."""

    if item.get("has_drm"):
        return False, False
    video_codec = item.get("vcodec")
    audio_codec = item.get("acodec")
    has_video = video_codec not in {None, "none"}
    has_audio = audio_codec not in {None, "none"}
    if video_codec is None and audio_codec is None:
        extension = str(item.get("ext") or "").lower().lstrip(".")
        if extension in _VIDEO_EXTENSIONS:
            return True, item.get("audio_channels") != 0
        if extension in _AUDIO_EXTENSIONS:
            return False, True
    return has_video, has_audio


def _qualities(formats: list[dict]) -> list[QualityInfo]:
    audio_sizes = [
        item.get("filesize") or item.get("filesize_approx")
        for item in formats
        if _media_types(item) == (False, True)
    ]
    known_audio_sizes = [size for size in audio_sizes if isinstance(size, int) and size > 0]
    best_audio_size = max(known_audio_sizes) if known_audio_sizes else None
    by_height: dict[int, dict] = {}
    for item in formats:
        if not _media_types(item)[0]:
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
        info: dict | None = None
        for attempt in range(3):
            try:
                options = base_options(settings, cookie_file)
                if attempt:
                    options.update(browser_impersonation_options())
                with ydl_factory(options) as ydl:
                    info = ensure_single_item(ydl.extract_info(url, download=False))
                break
            except yt_dlp.utils.DownloadError as exc:
                if attempt == 2 or not is_retryable_extraction_error(exc):
                    raise
                time.sleep(0.35 * (attempt + 1))
    finally:
        remove_cookie_copy(cookie_file)
    if info is None:
        raise ValueError("No media information was extracted")
    ensure_extractor_allowed(info, settings)

    formats = _normalized_formats(info)
    media_types = [_media_types(item) for item in formats]
    supports_video = any(has_video for has_video, _has_audio in media_types)
    supports_audio = any(has_audio for _has_video, has_audio in media_types)
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

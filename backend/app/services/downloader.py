"""yt-dlp execution used only inside an isolated worker process."""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp

from ..config import Settings
from ..models import FormatMode
from .authentication import create_cookie_copy, remove_cookie_copy
from .extractor import ensure_extractor_allowed, ensure_single_item
from .platform import platform_from_info
from .runtime import resolve_ffmpeg_location, ytdlp_runtime_options
from .security import validate_url


class DownloadRejected(Exception):
    """A user-visible policy rejection rather than an internal failure."""


EventEmitter = Callable[[dict[str, object]], None]

_RESTRICTED_AVAILABILITY = frozenset({"private", "premium_only", "subscriber_only", "needs_auth"})
_TRANSIENT_DOWNLOAD_MARKERS = (
    "http error 404",
    "http error 403",
    "http error 429",
    "unable to download video data",
    "universal data for rehydration",
    "unable to solve js challenge",
    "unexpected response from webpage request",
    "timed out",
    "incomplete read",
    "connection reset",
    "remote end closed connection",
    "did not get any data blocks",
)


def _selector(mode: FormatMode, max_height: int | None) -> str:
    height = f"[height<={max_height}]" if max_height else ""
    if mode == FormatMode.ORIGINAL:
        return f"bv*{height}+ba/b{height}"
    if mode == FormatMode.MP4_COMPATIBLE:
        return (
            f"bv*{height}[ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]"
            f"/b{height}[ext=mp4][vcodec^=avc1][acodec^=mp4a]"
            f"/bv*{height}[ext=mp4]+ba[ext=m4a]/b{height}[ext=mp4]"
        )
    return "ba/b"


def _workspace_size(workspace: Path) -> int:
    total = 0
    for path in workspace.iterdir():
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _is_transient_download_error(error: yt_dlp.utils.DownloadError) -> bool:
    """Recognize failures where a fresh extraction may yield a new media URL."""

    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_DOWNLOAD_MARKERS)


def safe_download_error(error: yt_dlp.utils.DownloadError, settings: Settings) -> str:
    """Classify a yt-dlp failure without exposing source URLs or credentials."""

    message = str(error).lower()
    if "ffmpeg" in message:
        return "FFmpeg is unavailable; video and audio streams could not be merged"
    if "javascript runtime" in message or "challenge solver" in message:
        return "YouTube JavaScript challenge support is unavailable on this server"
    if "impersonation" in message or "universal data for rehydration" in message:
        return "The source's browser verification could not be completed"
    if "requested format is not available" in message:
        return "The selected source format is no longer available"
    if "http error 403" in message or "forbidden" in message:
        return "The source denied the media transfer (HTTP 403)"
    if "http error 404" in message or "not found" in message:
        return "The source media link expired or was not found (HTTP 404)"
    if "private video" in message or "sign in" in message or "login" in message:
        return "The source requires an authorized login session"
    if "video unavailable" in message or "media is unavailable" in message:
        return "The source reports that this media is unavailable"
    if resolve_ffmpeg_location(settings) is None:
        return "FFmpeg is unavailable; video and audio streams could not be merged"
    return "The source could not be downloaded"


def _clear_retryable_output(workspace: Path, cookie_file: Path | None) -> None:
    """Remove only this job's partial output before a bounded fresh retry."""

    cookie_directory = cookie_file.parent if cookie_file is not None else None
    for child in workspace.iterdir():
        if cookie_directory is not None and child == cookie_directory:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _safe_output_name(title: str | None, path: Path) -> str:
    stem = re.sub(r"[^\w .()-]+", "_", title or "download", flags=re.UNICODE).strip(" ._")
    return f"{(stem or 'download')[:120]}{path.suffix.lower()}"


def _contained_file(raw_path: str | os.PathLike[str], workspace: Path) -> Path:
    root = workspace.resolve(strict=True)
    candidate = Path(raw_path)
    if candidate.is_symlink():
        raise RuntimeError("Worker produced an invalid output path")
    path = candidate.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise RuntimeError("Worker produced an invalid output path")
    return path


def _enforce_media_policy(info: dict[str, Any], *, use_auth: bool) -> None:
    """Allow authorized sessions while always rejecting wholly DRM media."""

    availability = str(info.get("availability") or "").strip().lower()
    if availability in _RESTRICTED_AVAILABILITY and not use_auth:
        raise DownloadRejected("This media requires the configured authenticated session")

    formats = info.get("formats")
    if isinstance(formats, list) and formats:
        media_formats = [
            item
            for item in formats
            if isinstance(item, dict)
            and (
                item.get("vcodec") not in {None, "none"} or item.get("acodec") not in {None, "none"}
            )
        ]
        if media_formats and all(bool(item.get("has_drm")) for item in media_formats):
            raise DownloadRejected("DRM-protected media is not supported")
    elif info.get("has_drm"):
        raise DownloadRejected("DRM-protected media is not supported")


def run_download_task(
    *,
    job_id: str,
    url: str,
    mode: FormatMode,
    max_height: int | None,
    use_auth: bool = False,
    settings: Settings,
    emit: EventEmitter,
) -> None:
    """Inspect, download, post-process, and emit JSON-serializable events."""

    validate_url(url, settings)
    workspace = settings.job_workspace(job_id)
    workspace.mkdir(parents=True, exist_ok=False)
    selector = _selector(mode, max_height)
    final_path: str | None = None
    last_progress = 0.0
    last_size_check = 0.0

    def progress_hook(data: dict[str, Any]) -> None:
        nonlocal last_progress, last_size_check
        if data.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last_size_check >= 0.2:
            last_size_check = now
            if _workspace_size(workspace) > settings.max_filesize_bytes:
                raise DownloadRejected("Download exceeds this instance's size limit")
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if total:
            current = min(95.0, 95.0 * float(data.get("downloaded_bytes") or 0) / total)
            last_progress = max(last_progress, round(current, 1))
        emit({"type": "progress", "status": "downloading", "progress": last_progress})

    def postprocessor_hook(data: dict[str, Any]) -> None:
        nonlocal final_path
        info = data.get("info_dict") or {}
        if info.get("filepath"):
            final_path = str(info["filepath"])
        if data.get("status") in {"started", "processing"}:
            emit({"type": "progress", "status": "processing", "progress": 98.0})

    common: dict[str, Any] = {
        "format": selector,
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": settings.socket_timeout_seconds,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "cachedir": False,
        "max_filesize": settings.max_filesize_bytes,
        "outtmpl": str(workspace / "media.%(ext)s"),
    }
    common.update(ytdlp_runtime_options(settings))

    cookie_file = create_cookie_copy(settings, workspace) if use_auth else None
    if cookie_file is not None:
        common["cookiefile"] = str(cookie_file)
    try:
        inspect_options = dict(common, skip_download=True)
        preview: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                with yt_dlp.YoutubeDL(inspect_options) as ydl:
                    preview = ensure_single_item(ydl.extract_info(url, download=False))
                break
            except yt_dlp.utils.DownloadError as exc:
                if attempt == 2 or not _is_transient_download_error(exc):
                    raise
                time.sleep(0.35 * (attempt + 1))
        if preview is None:
            raise RuntimeError("The source did not return media information")
        ensure_extractor_allowed(preview, settings)
        _enforce_media_policy(preview, use_auth=use_auth)
        is_live = bool(preview.get("is_live") or preview.get("live_status") == "is_live")
        if is_live:
            raise DownloadRejected("Live streams are not supported")
        duration = preview.get("duration")
        # Many short-form extractors omit duration even for finite, downloadable
        # posts. File-size, wall-clock, live-stream, and worker limits still bound
        # the operation when this optional metadata is absent.
        if duration is not None and float(duration) > settings.max_duration_seconds:
            raise DownloadRejected("Media exceeds this instance's duration limit")
        requested = preview.get("requested_formats") or [preview]
        known_sizes = [item.get("filesize") or item.get("filesize_approx") for item in requested]
        if (
            known_sizes
            and all(isinstance(size, int) for size in known_sizes)
            and sum(known_sizes) > settings.max_filesize_bytes
        ):
            raise DownloadRejected("Media exceeds this instance's size limit")

        title = preview.get("title")
        emit(
            {
                "type": "metadata",
                "title": str(title)[:500] if title else None,
                "platform": platform_from_info(preview, url)[:80],
            }
        )

        options = dict(common)
        options["progress_hooks"] = [progress_hook]
        options["postprocessor_hooks"] = [postprocessor_hook]
        if mode == FormatMode.ORIGINAL:
            options["merge_output_format"] = "mkv"
        elif mode == FormatMode.MP4_COMPATIBLE:
            options["merge_output_format"] = "mp4"
        elif mode == FormatMode.AUDIO_ONLY:
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]
        elif mode == FormatMode.AUDIO_MP3:
            options["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
            ]

        info: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ensure_single_item(ydl.extract_info(url, download=True))
                break
            except yt_dlp.utils.DownloadError as exc:
                if attempt or not _is_transient_download_error(exc):
                    raise
                # Signed CDN URLs can expire or intermittently return 404. A
                # single fresh extraction is bounded and often obtains a new URL.
                _clear_retryable_output(workspace, cookie_file)
    finally:
        remove_cookie_copy(cookie_file)
    if info is None:
        raise RuntimeError("The download did not produce media information")
    output: Path | None = None
    for candidate in (final_path, info.get("filepath")):
        if not candidate:
            continue
        try:
            output = _contained_file(str(candidate), workspace)
            break
        except (FileNotFoundError, OSError, RuntimeError):
            continue
    if output is None:
        candidates = [
            path
            for path in workspace.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and not path.name.endswith((".part", ".ytdl"))
        ]
        if len(candidates) != 1:
            raise RuntimeError("Could not identify the completed output")
        output = _contained_file(candidates[0], workspace)
    size = output.stat().st_size
    if size > settings.max_filesize_bytes:
        raise DownloadRejected("Completed media exceeds this instance's size limit")
    emit(
        {
            "type": "complete",
            "path": str(output),
            "name": _safe_output_name(title, output),
            "size": size,
        }
    )

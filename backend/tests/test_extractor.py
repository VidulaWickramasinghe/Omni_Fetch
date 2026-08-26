from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yt_dlp

from app.models import ExtractResponse
from app.services.extractor import (
    GenericExtractorDisabled,
    UnsupportedCollectionError,
    base_options,
    ensure_extractor_allowed,
    ensure_single_item,
    extract_metadata,
    is_retryable_extraction_error,
    safe_extraction_error,
)


class FakeYDL:
    def __init__(self, options: dict[str, Any], info: dict[str, Any]) -> None:
        self.options = options
        self.info = info
        self.calls: list[tuple[str, bool]] = []

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        self.calls.append((url, download))
        return self.info


def fake_factory(info: dict[str, Any], captured: list[FakeYDL]):
    def factory(options: dict[str, Any]) -> FakeYDL:
        instance = FakeYDL(options, info)
        captured.append(instance)
        return instance

    return factory


def test_base_options_are_metadata_only_and_playlist_bounded(
    settings_factory, tmp_path: Path
) -> None:
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("fixture", encoding="utf-8")
    ffmpeg.chmod(0o700)
    options = base_options(settings_factory(ffmpeg_location=str(ffmpeg)))

    assert options["skip_download"] is True
    assert options["noplaylist"] is True
    assert options["playlist_items"] == "1"
    assert options["retries"] == 2
    assert options["fragment_retries"] == 2
    assert options["ffmpeg_location"] == str(ffmpeg)


@pytest.mark.parametrize(
    "info",
    [
        None,
        {},
        {"_type": "playlist", "entries": [{"id": "one"}]},
        {"_type": "multi_video"},
        {"entries": ({"id": "one"},)},
    ],
)
def test_single_item_policy_fails_closed(info: dict | None) -> None:
    expected = ValueError if not info else UnsupportedCollectionError
    with pytest.raises(expected):
        ensure_single_item(info)


def test_generic_extractor_is_disabled_only_by_explicit_public_policy(settings_factory) -> None:
    info = {"extractor_key": "Generic"}

    with pytest.raises(GenericExtractorDisabled):
        ensure_extractor_allowed(
            info,
            settings_factory(public_mode=True, allow_generic_extractor=False),
        )

    ensure_extractor_allowed(
        info,
        settings_factory(public_mode=False, allow_generic_extractor=False),
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Unable to extract universal data for rehydration https://example.com/?token=secret",
            "browser verification",
        ),
        ("Private video. Sign in to view", "authorized login session"),
        ("Unsupported URL: https://example.com/private", "not supported"),
    ],
)
def test_extraction_errors_are_classified_without_leaking_urls(message: str, expected: str) -> None:
    safe = safe_extraction_error(yt_dlp.utils.DownloadError(message))

    assert expected in safe
    assert "example.com" not in safe
    assert "secret" not in safe


def test_tiktok_verification_failures_are_retryable() -> None:
    error = yt_dlp.utils.DownloadError("Unable to extract universal data for rehydration")

    assert is_retryable_extraction_error(error) is True


def test_extract_metadata_normalizes_qualities_and_ignores_drm(settings_factory) -> None:
    captured: list[FakeYDL] = []
    info = {
        "extractor_key": "YouTube",
        "title": "A test video",
        "duration": 42.5,
        "thumbnail": "https://cdn.example/thumb.jpg",
        "channel": "Test channel",
        "formats": [
            {
                "format_id": "drm-4k",
                "height": 2160,
                "vcodec": "av01",
                "acodec": "none",
                "filesize": 5000,
                "has_drm": True,
            },
            {
                "format_id": "1080-large",
                "height": 1080,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
                "filesize": 1000,
                "format_note": "large",
            },
            {
                "format_id": "1080-small",
                "height": 1080,
                "fps": 60,
                "vcodec": "vp9",
                "acodec": "none",
                "filesize": 800,
                "format_note": "small",
            },
            {
                "format_id": "720",
                "height": 720,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
                "filesize_approx": 500,
            },
            {
                "format_id": "audio",
                "vcodec": "none",
                "acodec": "mp4a",
                "filesize": 200,
            },
        ],
    }

    response = extract_metadata(
        "https://youtu.be/owned-test-video",
        settings_factory(),
        ydl_factory=fake_factory(info, captured),
    )

    assert isinstance(response, ExtractResponse)
    assert response.platform == "youtube"
    assert response.title == "A test video"
    assert response.uploader == "Test channel"
    assert response.is_live is False
    assert response.supports_video is True
    assert response.supports_audio is True
    assert [quality.id for quality in response.qualities] == ["height:1080", "height:720"]
    assert response.qualities[0].note == "large"
    assert response.qualities[0].estimated_size == 1200
    assert response.qualities[1].estimated_size == 700
    assert len(captured) == 1
    assert captured[0].calls == [("https://youtu.be/owned-test-video", False)]


def test_extract_metadata_reports_live_and_audio_only_media(settings_factory) -> None:
    info = {
        "extractor": "SoundCloud",
        "title": "Live audio",
        "live_status": "is_live",
        "formats": [{"format_id": "audio", "vcodec": "none", "acodec": "opus"}],
    }

    response = extract_metadata(
        "https://soundcloud.example/item",
        settings_factory(),
        ydl_factory=fake_factory(info, []),
    )

    assert response.is_live is True
    assert response.supports_video is False
    assert response.supports_audio is True
    assert response.qualities == []


def test_extract_metadata_uses_and_removes_private_cookie_copy(settings_factory, tmp_path) -> None:
    cookie_file = tmp_path / "session.cookies.txt"
    cookie_data = (
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tsecret-value\n"
    )
    cookie_file.write_text(cookie_data, encoding="utf-8")
    settings = settings_factory(
        authenticated_media_enabled=True,
        cookie_file=cookie_file,
    )
    captured: list[FakeYDL] = []

    response = extract_metadata(
        "https://example.com/private",
        settings,
        use_auth=True,
        ydl_factory=fake_factory(
            {
                "extractor_key": "YouTube",
                "title": "Authorized media",
                "duration": 20,
                "formats": [{"format_id": "audio", "vcodec": "none", "acodec": "opus"}],
            },
            captured,
        ),
    )

    temporary_cookie = Path(captured[0].options["cookiefile"])
    assert response.authenticated is True
    assert not temporary_cookie.exists()
    assert cookie_file.read_text(encoding="utf-8") == cookie_data
    assert not list(settings.download_dir.glob(".auth-*"))

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.models import FormatMode
from app.services import downloader
from app.services.downloader import DownloadRejected


@pytest.mark.parametrize(
    ("mode", "height", "expected"),
    [
        (FormatMode.ORIGINAL, None, "bv*+ba/b"),
        (FormatMode.ORIGINAL, 1080, "bv*[height<=1080]+ba/b[height<=1080]"),
        (FormatMode.AUDIO_ONLY, 1080, "ba/b"),
        (FormatMode.AUDIO_MP3, None, "ba/b"),
    ],
)
def test_selector_is_constructed_only_from_server_policy(
    mode: FormatMode, height: int | None, expected: str
) -> None:
    assert downloader._selector(mode, height) == expected


def test_mp4_selector_prefers_h264_and_aac() -> None:
    selector = downloader._selector(FormatMode.MP4_COMPATIBLE, 720)

    assert "height<=720" in selector
    assert "vcodec^=avc1" in selector
    assert "acodec^=mp4a" in selector
    assert selector.count(",") == 0


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: ffmpeg is not installed", "FFmpeg is unavailable"),
        ("ERROR: Requested format is not available", "selected source format"),
        ("ERROR: HTTP Error 403: Forbidden", "HTTP 403"),
        ("ERROR: Private video. Sign in", "authorized login session"),
    ],
)
def test_download_errors_are_classified_without_exposing_source_urls(
    monkeypatch, settings_factory, message: str, expected: str
) -> None:
    monkeypatch.setattr(downloader, "resolve_ffmpeg_location", lambda _settings: "/ffmpeg")
    error = downloader.yt_dlp.utils.DownloadError(
        f"{message}: https://example.com/watch?token=must-not-leak"
    )

    safe = downloader.safe_download_error(error, settings_factory())

    assert expected in safe
    assert "example.com" not in safe
    assert "must-not-leak" not in safe


def test_output_name_is_sanitized_and_bounded(tmp_path: Path) -> None:
    output = tmp_path / "media.MKV"

    name = downloader._safe_output_name(" ../unsafe?/" + "x" * 200, output)

    assert "/" not in name
    assert ".." not in name
    assert name.endswith(".mkv")
    assert len(Path(name).stem) <= 120


def test_contained_file_rejects_escape_and_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "media.mkv"
    inside.write_bytes(b"safe")
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    link = workspace / "link.mkv"
    link.symlink_to(outside)

    assert downloader._contained_file(inside, workspace) == inside.resolve()
    with pytest.raises(RuntimeError, match="invalid output path"):
        downloader._contained_file(outside, workspace)
    with pytest.raises(RuntimeError, match="invalid output path"):
        downloader._contained_file(link, workspace)


class SuccessfulYDL:
    instances: ClassVar[list[SuccessfulYDL]] = []
    preview: ClassVar[dict[str, Any]] = {
        "title": "Owned / test?",
        "duration": 12,
        "extractor_key": "YouTube",
        "formats": [],
    }

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.__class__.instances.append(self)

    def __enter__(self) -> SuccessfulYDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        if not download:
            return self.preview
        workspace = Path(self.options["outtmpl"]).parent
        output = workspace / "media.mkv"
        output.write_bytes(b"media")
        self.options["progress_hooks"][0](
            {"status": "downloading", "downloaded_bytes": 5, "total_bytes": 5}
        )
        self.options["postprocessor_hooks"][0](
            {"status": "started", "info_dict": {"filepath": str(output)}}
        )
        return {"filepath": str(output)}


def test_download_task_emits_sanitized_completion_and_uses_isolated_workspace(
    monkeypatch, settings_factory
) -> None:
    settings = settings_factory()
    settings.prepare_runtime_dirs()
    SuccessfulYDL.instances = []
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", SuccessfulYDL)
    monkeypatch.setattr(downloader, "validate_url", lambda url, _settings: url)
    events: list[dict[str, object]] = []

    downloader.run_download_task(
        job_id="a" * 32,
        url="https://youtu.be/owned-test-video",
        mode=FormatMode.ORIGINAL,
        max_height=1080,
        settings=settings,
        emit=events.append,
    )

    assert len(SuccessfulYDL.instances) == 2
    inspect_options = SuccessfulYDL.instances[0].options
    download_options = SuccessfulYDL.instances[1].options
    assert inspect_options["skip_download"] is True
    assert download_options["format"] == "bv*[height<=1080]+ba/b[height<=1080]"
    assert download_options["merge_output_format"] == "mkv"
    assert Path(download_options["outtmpl"]).parent == settings.job_workspace("a" * 32)
    assert events[0] == {"type": "metadata", "title": "Owned / test?", "platform": "youtube"}
    complete = events[-1]
    assert complete["type"] == "complete"
    assert complete["name"] == "Owned _ test.mkv"
    assert complete["size"] == 5
    assert Path(str(complete["path"])).is_relative_to(settings.job_workspace("a" * 32))


class InspectOnlyYDL:
    preview: ClassVar[dict[str, Any]] = {}
    calls = 0

    def __init__(self, _options: dict[str, Any]) -> None:
        self.__class__.calls += 1

    def __enter__(self) -> InspectOnlyYDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        assert download is False
        return self.preview


@pytest.mark.parametrize(
    ("preview", "message"),
    [
        ({"title": "live", "duration": 10, "is_live": True}, "Live streams"),
        (
            {"title": "private", "duration": 10, "availability": "PRIVATE"},
            "requires the configured authenticated session",
        ),
        (
            {
                "title": "protected",
                "duration": 10,
                "formats": [
                    {
                        "format_id": "drm-only",
                        "vcodec": "avc1",
                        "acodec": "none",
                        "has_drm": True,
                    }
                ],
            },
            "DRM-protected media",
        ),
        ({"title": "long", "duration": 601}, "duration limit"),
        (
            {
                "title": "large",
                "duration": 10,
                "requested_formats": [{"filesize": 600_000}, {"filesize": 500_000}],
            },
            "size limit",
        ),
        ({"_type": "playlist", "entries": [{"id": "one"}]}, "Playlists"),
    ],
)
def test_download_policy_rejects_before_media_transfer(
    monkeypatch, settings_factory, preview: dict[str, Any], message: str
) -> None:
    settings = settings_factory()
    settings.prepare_runtime_dirs()
    InspectOnlyYDL.preview = preview
    InspectOnlyYDL.calls = 0
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", InspectOnlyYDL)
    monkeypatch.setattr(downloader, "validate_url", lambda url, _settings: url)

    with pytest.raises((DownloadRejected, ValueError), match=message):
        downloader.run_download_task(
            job_id="b" * 32,
            url="https://example.com/media",
            mode=FormatMode.ORIGINAL,
            max_height=None,
            settings=settings,
            emit=lambda _event: None,
        )

    assert InspectOnlyYDL.calls == 1


class OversizeDuringDownloadYDL(SuccessfulYDL):
    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        if not download:
            return {"title": "unknown size", "duration": 10, "extractor_key": "YouTube"}
        workspace = Path(self.options["outtmpl"]).parent
        (workspace / "media.part").write_bytes(b"too large")
        self.options["progress_hooks"][0](
            {"status": "downloading", "downloaded_bytes": 9, "total_bytes": None}
        )
        raise AssertionError("the progress hook should abort first")


def test_unknown_size_is_stopped_by_aggregate_workspace_limit(
    monkeypatch, settings_factory
) -> None:
    settings = settings_factory(max_filesize_bytes=4)
    settings.prepare_runtime_dirs()
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", OversizeDuringDownloadYDL)
    monkeypatch.setattr(downloader, "validate_url", lambda url, _settings: url)

    with pytest.raises(DownloadRejected, match="size limit"):
        downloader.run_download_task(
            job_id="c" * 32,
            url="https://youtu.be/owned-test-video",
            mode=FormatMode.ORIGINAL,
            max_height=None,
            settings=settings,
            emit=lambda _event: None,
        )


class UnknownDurationYDL(SuccessfulYDL):
    instances: ClassVar[list[UnknownDurationYDL]] = []
    preview: ClassVar[dict[str, Any]] = {
        "title": "Short-form post without duration metadata",
        "extractor_key": "TikTok",
        "formats": [],
    }


def test_finite_media_without_duration_metadata_is_downloaded(
    monkeypatch, settings_factory
) -> None:
    settings = settings_factory()
    settings.prepare_runtime_dirs()
    UnknownDurationYDL.instances = []
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", UnknownDurationYDL)
    monkeypatch.setattr(downloader, "validate_url", lambda url, _settings: url)
    events: list[dict[str, object]] = []

    downloader.run_download_task(
        job_id="f" * 32,
        url="https://www.tiktok.com/owned-post",
        mode=FormatMode.ORIGINAL,
        max_height=None,
        settings=settings,
        emit=events.append,
    )

    assert events[-1]["type"] == "complete"
    assert UnknownDurationYDL.instances[0].options["extractor_retries"] == 3


class TransientDownloadYDL(SuccessfulYDL):
    instances: ClassVar[list[TransientDownloadYDL]] = []
    download_attempts = 0

    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        if not download:
            return self.preview
        self.__class__.download_attempts += 1
        if self.__class__.download_attempts == 1:
            workspace = Path(self.options["outtmpl"]).parent
            (workspace / "media.part").write_bytes(b"expired signed response")
            raise downloader.yt_dlp.utils.DownloadError(
                "Unable to download video data: HTTP Error 404: Not Found"
            )
        return super().extract_info(url, download=download)


def test_transient_media_url_failure_gets_one_fresh_bounded_retry(
    monkeypatch, settings_factory
) -> None:
    settings = settings_factory()
    settings.prepare_runtime_dirs()
    TransientDownloadYDL.instances = []
    TransientDownloadYDL.download_attempts = 0
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", TransientDownloadYDL)
    monkeypatch.setattr(downloader, "validate_url", lambda url, _settings: url)

    downloader.run_download_task(
        job_id="9" * 32,
        url="https://www.tiktok.com/owned-post",
        mode=FormatMode.ORIGINAL,
        max_height=None,
        settings=settings,
        emit=lambda _event: None,
    )

    assert TransientDownloadYDL.download_attempts == 2
    assert len(TransientDownloadYDL.instances) == 3
    assert not (settings.job_workspace("9" * 32) / "media.part").exists()


def test_authenticated_download_uses_temporary_cookie_copy_and_allows_private_media(
    monkeypatch, settings_factory, tmp_path: Path
) -> None:
    cookie_source = tmp_path / "session.cookies.txt"
    cookie_data = (
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tsecret-value\n"
    )
    cookie_source.write_text(cookie_data, encoding="utf-8")
    settings = settings_factory(
        authenticated_media_enabled=True,
        cookie_file=cookie_source,
    )
    settings.prepare_runtime_dirs()
    SuccessfulYDL.instances = []
    SuccessfulYDL.preview = {
        "title": "Authorized private media",
        "duration": 12,
        "availability": "private",
        "extractor_key": "YouTube",
        "formats": [],
    }
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", SuccessfulYDL)
    monkeypatch.setattr(downloader, "validate_url", lambda url, _settings: url)

    downloader.run_download_task(
        job_id="d" * 32,
        url="https://example.com/private",
        mode=FormatMode.ORIGINAL,
        max_height=None,
        use_auth=True,
        settings=settings,
        emit=lambda _event: None,
    )

    cookie_paths = [Path(instance.options["cookiefile"]) for instance in SuccessfulYDL.instances]
    assert len(set(cookie_paths)) == 1
    assert all(not path.exists() for path in cookie_paths)
    assert cookie_source.read_text(encoding="utf-8") == cookie_data
    assert not list(settings.job_workspace("d" * 32).glob(".auth-*"))


def test_authenticated_mode_still_rejects_drm_and_cleans_cookie_copy(
    monkeypatch, settings_factory, tmp_path: Path
) -> None:
    cookie_source = tmp_path / "session.cookies.txt"
    cookie_source.write_text(
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tsecret-value\n",
        encoding="utf-8",
    )
    settings = settings_factory(
        authenticated_media_enabled=True,
        cookie_file=cookie_source,
    )
    settings.prepare_runtime_dirs()
    InspectOnlyYDL.preview = {
        "title": "Protected",
        "duration": 10,
        "availability": "private",
        "formats": [
            {
                "format_id": "drm-only",
                "vcodec": "avc1",
                "acodec": "none",
                "has_drm": True,
            }
        ],
    }
    InspectOnlyYDL.calls = 0
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", InspectOnlyYDL)
    monkeypatch.setattr(downloader, "validate_url", lambda url, _settings: url)

    with pytest.raises(DownloadRejected, match="DRM-protected"):
        downloader.run_download_task(
            job_id="e" * 32,
            url="https://example.com/private-drm",
            mode=FormatMode.ORIGINAL,
            max_height=None,
            use_auth=True,
            settings=settings,
            emit=lambda _event: None,
        )

    assert not list(settings.job_workspace("e" * 32).glob(".auth-*"))

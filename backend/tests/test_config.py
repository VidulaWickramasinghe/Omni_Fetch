from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


def test_from_env_parses_policy_without_creating_directories(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "OMNIFETCH_DOWNLOAD_DIR": "runtime/media",
            "OMNIFETCH_FRONTEND_DIR": "web",
            "OMNIFETCH_MAX_FILESIZE_MB": "12",
            "OMNIFETCH_MAX_DURATION_MIN": "7",
            "OMNIFETCH_MAX_CONCURRENT_JOBS": "2",
            "OMNIFETCH_MAX_QUEUED_JOBS": "5",
            "OMNIFETCH_JOB_TIMEOUT_SECONDS": "99",
            "OMNIFETCH_JOB_TTL_HOURS": "3",
            "OMNIFETCH_ALLOWED_PORTS": "443, 8443",
            "OMNIFETCH_ALLOWED_ORIGINS": "https://one.example, https://two.example",
            "OMNIFETCH_PUBLIC_MODE": "yes",
            "OMNIFETCH_ALLOW_GENERIC_EXTRACTOR": "off",
            "OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA": "yes",
            "OMNIFETCH_COOKIE_FILE": "secrets/cookies.txt",
            "OMNIFETCH_MAX_COOKIE_FILE_BYTES": "2048",
        },
        base_dir=tmp_path,
    )

    assert settings.download_dir == (tmp_path / "runtime/media").resolve()
    assert settings.frontend_dir == (tmp_path.parent / "web").resolve()
    assert settings.max_filesize_bytes == 12 * 1024 * 1024
    assert settings.max_duration_seconds == 7 * 60
    assert settings.job_capacity == 7
    assert settings.job_timeout_seconds == 99
    assert settings.job_ttl_seconds == 3 * 3600
    assert settings.allowed_ports == frozenset({443, 8443})
    assert settings.cors_origins == ("https://one.example", "https://two.example")
    assert settings.public_mode is True
    assert settings.allow_generic_extractor is False
    assert settings.authenticated_media_enabled is True
    assert settings.cookie_file == (tmp_path / "secrets/cookies.txt").resolve()
    assert settings.max_cookie_file_bytes == 2048
    assert not settings.download_dir.exists()


def test_from_env_treats_blank_optional_values_as_unset(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "OMNIFETCH_MAX_FILESIZE_MB": "",
            "OMNIFETCH_MAX_DURATION_MIN": "   ",
            "OMNIFETCH_PUBLIC_MODE": "",
            "OMNIFETCH_ALLOW_GENERIC_EXTRACTOR": "   ",
            "OMNIFETCH_ALLOWED_PORTS": "",
            "OMNIFETCH_ALLOWED_ORIGINS": "   ",
        },
        base_dir=tmp_path,
    )

    assert settings.max_filesize_bytes == 2048 * 1024 * 1024
    assert settings.max_duration_seconds == 180 * 60
    assert settings.public_mode is False
    assert settings.allow_generic_extractor is False
    assert settings.allowed_ports == frozenset({80, 443})
    assert settings.cors_origins == (
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    )


def test_from_env_uses_writable_vercel_scratch_directory(tmp_path: Path) -> None:
    settings = Settings.from_env({"VERCEL": "1"}, base_dir=tmp_path)

    assert settings.download_dir == Path("/tmp/omnifetch/downloads").resolve()


def test_from_env_honors_explicit_download_directory_on_vercel(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {"VERCEL": "1", "OMNIFETCH_DOWNLOAD_DIR": "runtime/media"},
        base_dir=tmp_path,
    )

    assert settings.download_dir == (tmp_path / "runtime/media").resolve()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"OMNIFETCH_MAX_FILESIZE_MB": "many"}, "must be an integer"),
        ({"OMNIFETCH_MAX_DURATION_MIN": "0"}, "must be positive"),
        ({"OMNIFETCH_MAX_QUEUED_JOBS": "-1"}, "zero or greater"),
        ({"OMNIFETCH_ALLOWED_PORTS": "443,nope"}, "must contain integers"),
        ({"OMNIFETCH_ALLOWED_PORTS": "0"}, "between 1 and 65535"),
        ({"OMNIFETCH_PUBLIC_MODE": "sometimes"}, "must be true or false"),
        (
            {"OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA": "true"},
            "COOKIE_FILE is required",
        ),
    ],
)
def test_from_env_rejects_invalid_limits(tmp_path: Path, env: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env, base_dir=tmp_path)


def test_runtime_directory_creation_is_explicit(settings_factory) -> None:
    settings = settings_factory()

    assert not settings.download_dir.exists()
    settings.prepare_runtime_dirs()
    assert settings.download_dir.is_dir()


@pytest.mark.parametrize("job_id", ["", "../escape", "ABCDEF", "abcd-1234", "xyz"])
def test_job_workspace_rejects_non_hex_identifiers(settings_factory, job_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid job identifier"):
        settings_factory().job_workspace(job_id)


def test_worker_policy_round_trip_contains_no_cookie_content(settings_factory, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    settings = settings_factory(
        public_mode=True,
        allow_generic_extractor=False,
        authenticated_media_enabled=True,
        cookie_file=cookie_file,
    )

    policy = settings.worker_policy()
    rebuilt = Settings.from_worker_policy(policy)

    assert rebuilt.download_dir == settings.download_dir
    assert rebuilt.max_filesize_bytes == settings.max_filesize_bytes
    assert rebuilt.public_mode is True
    assert rebuilt.allow_generic_extractor is False
    assert rebuilt.authenticated_media_enabled is True
    assert rebuilt.cookie_file == cookie_file
    assert "frontend_dir" not in policy
    assert "cors_origins" not in policy
    assert "cookie_content" not in policy

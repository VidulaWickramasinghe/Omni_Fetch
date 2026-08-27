"""Validated, injectable application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_VERCEL_DOWNLOAD_DIR = Path("/tmp/omnifetch/downloads")


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _path(raw: str | None, default: Path, *, base_dir: Path) -> Path:
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def _csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime policy. Construction has no filesystem side effects."""

    download_dir: Path
    frontend_dir: Path
    max_filesize_bytes: int = 2 * 1024 * 1024 * 1024
    max_duration_seconds: int = 180 * 60
    max_concurrent_jobs: int = 3
    max_queued_jobs: int = 8
    job_ttl_seconds: int = 6 * 3600
    job_timeout_seconds: int = 4 * 3600
    cleanup_interval_seconds: int = 600
    socket_timeout_seconds: int = 15
    max_url_length: int = 4096
    max_body_bytes: int = 64 * 1024
    subprocess_terminate_grace_seconds: int = 3
    max_worker_event_bytes: int = 256 * 1024
    max_cookie_file_bytes: int = 1024 * 1024
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    allowed_ports: frozenset[int] = frozenset({80, 443})
    cors_origins: tuple[str, ...] = (
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    )
    public_mode: bool = False
    allow_generic_extractor: bool = False
    authenticated_media_enabled: bool = False
    cookie_file: Path | None = None
    ffmpeg_location: str | None = None
    serverless: bool = False

    def __post_init__(self) -> None:
        positive = {
            "max_filesize_bytes": self.max_filesize_bytes,
            "max_duration_seconds": self.max_duration_seconds,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "job_ttl_seconds": self.job_ttl_seconds,
            "job_timeout_seconds": self.job_timeout_seconds,
            "cleanup_interval_seconds": self.cleanup_interval_seconds,
            "socket_timeout_seconds": self.socket_timeout_seconds,
            "max_url_length": self.max_url_length,
            "max_body_bytes": self.max_body_bytes,
            "subprocess_terminate_grace_seconds": self.subprocess_terminate_grace_seconds,
            "max_worker_event_bytes": self.max_worker_event_bytes,
            "max_cookie_file_bytes": self.max_cookie_file_bytes,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"Settings must be positive: {', '.join(invalid)}")
        if self.max_queued_jobs < 0:
            raise ValueError("max_queued_jobs must be zero or greater")
        if not self.allowed_schemes:
            raise ValueError("At least one URL scheme must be allowed")
        if any(port < 1 or port > 65535 for port in self.allowed_ports):
            raise ValueError("Allowed ports must be between 1 and 65535")
        if self.authenticated_media_enabled and self.cookie_file is None:
            raise ValueError(
                "OMNIFETCH_COOKIE_FILE is required when authenticated media is enabled"
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        base_dir: Path | None = None,
    ) -> Settings:
        """Build settings from an explicit mapping, making tests deterministic."""

        values = os.environ if env is None else env
        backend_dir = (base_dir or _BACKEND_DIR).resolve(strict=False)
        project_dir = backend_dir.parent
        serverless = _boolean(values, "VERCEL", False)
        default_download_dir = _VERCEL_DOWNLOAD_DIR if serverless else backend_dir / "downloads"
        # A Vercel Hobby invocation has a five-minute wall-clock ceiling and a
        # 500 MB writable scratch filesystem. Leave headroom for split streams,
        # muxing, and delivery of the final response.
        default_max_mb = 256 if serverless else 2048
        default_max_minutes = 60 if serverless else 180
        default_concurrent_jobs = 1 if serverless else 3
        default_queued_jobs = 0 if serverless else 8
        default_job_timeout = 240 if serverless else 4 * 3600
        max_mb = _integer(values, "OMNIFETCH_MAX_FILESIZE_MB", default_max_mb)
        max_minutes = _integer(values, "OMNIFETCH_MAX_DURATION_MIN", default_max_minutes)
        ttl_hours = _integer(values, "OMNIFETCH_JOB_TTL_HOURS", 6)
        raw_ports = _csv(values.get("OMNIFETCH_ALLOWED_PORTS"), ("80", "443"))
        try:
            allowed_ports = frozenset(int(port) for port in raw_ports)
        except ValueError as exc:
            raise ValueError("OMNIFETCH_ALLOWED_PORTS must contain integers") from exc
        raw_cookie_file = values.get("OMNIFETCH_COOKIE_FILE")

        return cls(
            download_dir=_path(
                values.get("OMNIFETCH_DOWNLOAD_DIR"),
                default_download_dir,
                base_dir=backend_dir,
            ),
            frontend_dir=_path(
                values.get("OMNIFETCH_FRONTEND_DIR"),
                project_dir / "frontend",
                base_dir=project_dir,
            ),
            max_filesize_bytes=max_mb * 1024 * 1024,
            max_duration_seconds=max_minutes * 60,
            max_concurrent_jobs=_integer(
                values, "OMNIFETCH_MAX_CONCURRENT_JOBS", default_concurrent_jobs
            ),
            max_queued_jobs=_integer(values, "OMNIFETCH_MAX_QUEUED_JOBS", default_queued_jobs),
            job_ttl_seconds=ttl_hours * 3600,
            job_timeout_seconds=_integer(
                values, "OMNIFETCH_JOB_TIMEOUT_SECONDS", default_job_timeout
            ),
            cleanup_interval_seconds=_integer(values, "OMNIFETCH_CLEANUP_INTERVAL_SECONDS", 600),
            socket_timeout_seconds=_integer(values, "OMNIFETCH_SOCKET_TIMEOUT_SECONDS", 15),
            max_url_length=_integer(values, "OMNIFETCH_MAX_URL_LENGTH", 4096),
            max_body_bytes=_integer(values, "OMNIFETCH_MAX_BODY_BYTES", 64 * 1024),
            subprocess_terminate_grace_seconds=_integer(
                values, "OMNIFETCH_TERMINATE_GRACE_SECONDS", 3
            ),
            max_worker_event_bytes=_integer(values, "OMNIFETCH_MAX_WORKER_EVENT_BYTES", 256 * 1024),
            max_cookie_file_bytes=_integer(values, "OMNIFETCH_MAX_COOKIE_FILE_BYTES", 1024 * 1024),
            allowed_ports=allowed_ports,
            cors_origins=_csv(
                values.get("OMNIFETCH_ALLOWED_ORIGINS"),
                ("http://localhost:8000", "http://127.0.0.1:8000"),
            ),
            public_mode=_boolean(values, "OMNIFETCH_PUBLIC_MODE", False),
            allow_generic_extractor=_boolean(values, "OMNIFETCH_ALLOW_GENERIC_EXTRACTOR", False),
            authenticated_media_enabled=_boolean(
                values, "OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA", False
            ),
            cookie_file=(
                _path(raw_cookie_file, backend_dir / "cookies.txt", base_dir=backend_dir)
                if raw_cookie_file
                else None
            ),
            ffmpeg_location=values.get("OMNIFETCH_FFMPEG_LOCATION") or None,
            serverless=serverless,
        )

    @property
    def job_capacity(self) -> int:
        return self.max_concurrent_jobs + self.max_queued_jobs

    def job_workspace(self, job_id: str) -> Path:
        if not job_id or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("Invalid job identifier")
        return self.download_dir / job_id

    def prepare_runtime_dirs(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def worker_policy(self) -> dict[str, object]:
        """Return bounded policy and a server-owned cookie path, never cookie content."""

        return {
            "download_dir": str(self.download_dir),
            "max_filesize_bytes": self.max_filesize_bytes,
            "max_duration_seconds": self.max_duration_seconds,
            "socket_timeout_seconds": self.socket_timeout_seconds,
            "max_url_length": self.max_url_length,
            "max_cookie_file_bytes": self.max_cookie_file_bytes,
            "allowed_schemes": sorted(self.allowed_schemes),
            "allowed_ports": sorted(self.allowed_ports),
            "public_mode": self.public_mode,
            "allow_generic_extractor": self.allow_generic_extractor,
            "authenticated_media_enabled": self.authenticated_media_enabled,
            "cookie_file": str(self.cookie_file) if self.cookie_file else None,
            "ffmpeg_location": self.ffmpeg_location,
        }

    @classmethod
    def from_worker_policy(cls, policy: Mapping[str, object]) -> Settings:
        """Rebuild the small policy subset received by an isolated worker."""

        download_dir = Path(str(policy["download_dir"])).resolve(strict=False)
        return cls(
            download_dir=download_dir,
            frontend_dir=download_dir,
            max_filesize_bytes=int(policy["max_filesize_bytes"]),
            max_duration_seconds=int(policy["max_duration_seconds"]),
            socket_timeout_seconds=int(policy["socket_timeout_seconds"]),
            max_url_length=int(policy["max_url_length"]),
            max_cookie_file_bytes=int(policy.get("max_cookie_file_bytes", 1024 * 1024)),
            allowed_schemes=frozenset(str(item) for item in policy["allowed_schemes"]),
            allowed_ports=frozenset(int(item) for item in policy["allowed_ports"]),
            public_mode=bool(policy["public_mode"]),
            allow_generic_extractor=bool(policy["allow_generic_extractor"]),
            authenticated_media_enabled=bool(policy.get("authenticated_media_enabled", False)),
            cookie_file=(
                Path(str(policy["cookie_file"])).resolve(strict=False)
                if policy.get("cookie_file")
                else None
            ),
            ffmpeg_location=(
                str(policy["ffmpeg_location"]) if policy.get("ffmpeg_location") else None
            ),
        )

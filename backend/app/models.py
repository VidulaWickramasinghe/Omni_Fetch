"""Validated API contracts and internal job state."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FormatMode(StrEnum):
    ORIGINAL = "original"
    MP4_COMPATIBLE = "mp4"
    AUDIO_ONLY = "audio"
    AUDIO_MP3 = "audio_mp3"


class JobStatus(StrEnum):
    QUEUED = "queued"
    INSPECTING = "inspecting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.REJECTED, JobStatus.FAILED, JobStatus.CANCELLED}
)
ACTIVE_STATUSES = frozenset(status for status in JobStatus if status not in TERMINAL_STATUSES)


class _URLRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=8, max_length=4096)
    use_auth: bool = False

    @field_validator("url")
    @classmethod
    def reject_blank_url(cls, value: str) -> str:
        if not value:
            raise ValueError("URL cannot be blank")
        return value


class ExtractRequest(_URLRequest):
    pass


class QualityInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    height: int | None = None
    fps: float | None = None
    note: str | None = None
    estimated_size: int | None = None


class ExtractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    title: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    is_live: bool
    authenticated: bool = False
    qualities: list[QualityInfo] = Field(default_factory=list)
    supports_video: bool
    supports_audio: bool


class DownloadRequest(_URLRequest):
    mode: FormatMode = FormatMode.ORIGINAL
    max_height: int | None = Field(default=None, ge=144, le=8640)


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus


class AuthenticationStatus(BaseModel):
    enabled: bool
    available: bool
    method: str | None = None


class JobRecord(BaseModel):
    """Private state; source URL and filesystem path never enter API responses."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source_url: str
    mode: FormatMode
    max_height: int | None = None
    use_auth: bool = False
    status: JobStatus = JobStatus.QUEUED
    progress: float = Field(default=0.0, ge=0.0, le=100.0)
    title: str | None = None
    platform: str | None = None
    output_path: Path | None = None
    download_name: str | None = None
    output_size: int | None = None
    error: str | None = None
    worker_pid: int | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None


class JobResponse(BaseModel):
    """Sanitized public projection of a job record."""

    job_id: str
    status: JobStatus
    phase: str
    progress: float
    mode: FormatMode
    max_height: int | None = None
    authenticated: bool
    title: str | None = None
    platform: str | None = None
    output_name: str | None = None
    output_size: int | None = None
    expires_at: float | None = None
    download_url: str | None = None
    error: str | None = None
    created_at: float
    updated_at: float
    completed_at: float | None = None

    @classmethod
    def from_record(cls, job: JobRecord, *, ttl_seconds: int) -> JobResponse:
        expires_at = job.updated_at + ttl_seconds if job.status in TERMINAL_STATUSES else None
        return cls(
            job_id=job.job_id,
            status=job.status,
            phase=job.status.value,
            progress=job.progress,
            mode=job.mode,
            max_height=job.max_height,
            authenticated=job.use_auth,
            title=job.title,
            platform=job.platform,
            output_name=job.download_name,
            output_size=job.output_size,
            expires_at=expires_at,
            download_url=(
                f"/api/v1/jobs/{job.job_id}/file"
                if job.status == JobStatus.COMPLETED and job.output_path
                else None
            ),
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
        )

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import (
    DownloadRequest,
    FormatMode,
    JobRecord,
    JobResponse,
    JobStatus,
)


def test_download_request_accepts_policy_not_raw_selector() -> None:
    request = DownloadRequest(
        url="https://example.com/video",
        mode="original",
        max_height=1080,
    )

    assert request.mode == FormatMode.ORIGINAL
    assert request.max_height == 1080

    with pytest.raises(ValidationError, match="format_id"):
        DownloadRequest(
            url="https://example.com/video",
            mode="original",
            format_id="all,bv+ba",
        )


@pytest.mark.parametrize("height", [0, 143, 8641, 100_000])
def test_download_request_bounds_height(height: int) -> None:
    with pytest.raises(ValidationError):
        DownloadRequest(url="https://example.com/video", max_height=height)


def test_job_response_is_a_sanitized_projection(tmp_path: Path) -> None:
    secret_url = "https://example.com/video?token=do-not-return"
    private_path = tmp_path / "private" / "result.mkv"
    job = JobRecord(
        source_url=secret_url,
        mode=FormatMode.ORIGINAL,
        status=JobStatus.COMPLETED,
        progress=100,
        output_path=private_path,
        download_name="result.mkv",
        output_size=123,
        worker_pid=999,
        error=None,
    )

    payload = JobResponse.from_record(job, ttl_seconds=60).model_dump(mode="json")
    serialized = str(payload)

    assert payload["download_url"] == f"/api/v1/jobs/{job.job_id}/file"
    assert payload["output_name"] == "result.mkv"
    assert "source_url" not in payload
    assert "output_path" not in payload
    assert "worker_pid" not in payload
    assert secret_url not in serialized
    assert str(private_path) not in serialized


def test_incomplete_job_has_no_download_url_or_expiry() -> None:
    response = JobResponse.from_record(
        JobRecord(source_url="https://example.com/video", mode=FormatMode.AUDIO_ONLY),
        ttl_seconds=60,
    )

    assert response.download_url is None
    assert response.expires_at is None

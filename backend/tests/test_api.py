from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
import yt_dlp
from fastapi import FastAPI
from starlette.testclient import TestClient

from app import main as main_module
from app.api import routes
from app.config import Settings
from app.main import create_app
from app.models import (
    DownloadRequest,
    ExtractResponse,
    FormatMode,
    JobRecord,
    JobStatus,
    QualityInfo,
)
from app.services.jobs import JobStore
from app.services.manager import QueueFullError
from app.services.security import UnsafeURLError


class StubManager:
    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store
        self.queue_full = False
        self.ready = True

    def submit(self, request: DownloadRequest) -> JobRecord:
        if self.queue_full:
            raise QueueFullError("The download queue is full")
        job = JobRecord(
            source_url=request.url,
            mode=request.mode,
            max_height=request.max_height,
            use_auth=request.use_auth,
        )
        accepted = self.store.try_create(job, self.settings.job_capacity)
        if accepted is None:
            raise QueueFullError("The download queue is full")
        return accepted

    def cancel(self, job_id: str) -> JobRecord | None:
        return self.store.request_cancel(job_id)

    def delete_files(self, job_id: str) -> None:
        shutil.rmtree(self.settings.job_workspace(job_id), ignore_errors=True)


@dataclass
class ApiHarness:
    client: TestClient
    app: FastAPI
    settings: Settings
    store: JobStore
    manager: StubManager


@pytest.fixture
def api_harness(settings_factory, monkeypatch) -> ApiHarness:
    settings = settings_factory(max_body_bytes=512)
    settings.frontend_dir.mkdir(parents=True)
    (settings.frontend_dir / "index.html").write_text(
        "<!doctype html><title>OmniFetch fixture</title>",
        encoding="utf-8",
    )
    monkeypatch.setattr(main_module, "_ffmpeg_ready", lambda _settings: True)
    monkeypatch.setattr(routes, "validate_url", lambda url, _settings: url)
    monkeypatch.setattr(
        routes,
        "extract_metadata",
        lambda _url, _settings, *, use_auth=False: ExtractResponse(
            platform="youtube",
            title="Owned fixture",
            duration=12,
            thumbnail="https://cdn.example/fixture.jpg",
            uploader="Fixture owner",
            is_live=False,
            authenticated=use_auth,
            qualities=[QualityInfo(id="height:1080", label="1080p", height=1080)],
            supports_video=True,
            supports_audio=True,
        ),
    )

    application = create_app(settings)
    with TestClient(application) as client:
        store = application.state.job_store
        manager = StubManager(settings, store)
        application.state.download_manager = manager
        yield ApiHarness(client, application, settings, store, manager)


def completed_job(harness: ApiHarness, index: int = 1) -> tuple[JobRecord, Path]:
    job_id = f"{index:032x}"
    workspace = harness.settings.job_workspace(job_id)
    workspace.mkdir(parents=True)
    output = workspace / "media.mkv"
    output.write_bytes(b"fixture media")
    job = JobRecord(
        job_id=job_id,
        source_url="https://example.com/video?token=private",
        mode=FormatMode.ORIGINAL,
        status=JobStatus.COMPLETED,
        progress=100,
        output_path=output,
        download_name="Fixture media.mkv",
        output_size=output.stat().st_size,
        completed_at=123,
    )
    assert harness.store.try_create(job, max_jobs=harness.settings.job_capacity) is not None
    return job, output


def test_frontend_health_and_readiness_are_served_from_one_origin(api_harness: ApiHarness) -> None:
    frontend = api_harness.client.get("/")
    health = api_harness.client.get("/health")
    ready = api_harness.client.get("/ready")

    assert frontend.status_code == 200
    assert "OmniFetch fixture" in frontend.text
    assert health.json() == {"status": "ok"}
    assert ready.json() == {"status": "ready"}


def test_readiness_fails_closed_when_worker_or_ffmpeg_is_unavailable(
    api_harness: ApiHarness, monkeypatch
) -> None:
    api_harness.manager.ready = False
    response = api_harness.client.get("/ready")
    assert response.status_code == 503

    api_harness.manager.ready = True
    monkeypatch.setattr(main_module, "_ffmpeg_ready", lambda _settings: False)
    response = api_harness.client.get("/ready")
    assert response.status_code == 503


def test_security_headers_and_cors_are_explicit(api_harness: ApiHarness) -> None:
    allowed = api_harness.client.get(
        "/health",
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    denied = api_harness.client.get(
        "/health",
        headers={"Origin": "https://attacker.example"},
    )
    preflight = api_harness.client.options(
        "/api/v1/extract",
        headers={
            "Origin": "http://127.0.0.1:8000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
    assert "access-control-allow-origin" not in denied.headers
    assert preflight.status_code == 200
    assert allowed.headers["x-content-type-options"] == "nosniff"
    assert allowed.headers["x-frame-options"] == "DENY"
    assert allowed.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in allowed.headers["permissions-policy"]


def test_request_body_limit_runs_before_validation(api_harness: ApiHarness) -> None:
    response = api_harness.client.post(
        "/api/v1/extract",
        content=b"{" + b"x" * 700 + b"}",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}


def test_extract_returns_normalized_quality_contract(api_harness: ApiHarness) -> None:
    response = api_harness.client.post(
        "/api/v1/extract",
        json={"url": "https://youtu.be/owned-fixture"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "Owned fixture"
    assert payload["qualities"] == [
        {
            "id": "height:1080",
            "label": "1080p",
            "height": 1080,
            "fps": None,
            "note": None,
            "estimated_size": None,
        }
    ]
    assert "formats" not in payload


def test_extract_maps_url_policy_and_extractor_errors_safely(
    api_harness: ApiHarness, monkeypatch
) -> None:
    def unsafe(_url: str, _settings: Settings) -> str:
        raise UnsafeURLError("URL resolves to a non-public address")

    monkeypatch.setattr(routes, "validate_url", unsafe)
    response = api_harness.client.post(
        "/api/v1/extract",
        json={"url": "https://example.com/video"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "URL resolves to a non-public address"}

    monkeypatch.setattr(routes, "validate_url", lambda url, _settings: url)

    def extraction_failed(
        _url: str, _settings: Settings, *, use_auth: bool = False
    ) -> ExtractResponse:
        del use_auth
        raise yt_dlp.utils.DownloadError(
            "source https://example.com/video?token=must-not-leak failed"
        )

    monkeypatch.setattr(routes, "extract_metadata", extraction_failed)
    response = api_harness.client.post(
        "/api/v1/extract",
        json={"url": "https://example.com/video"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "The source could not be inspected"}
    assert "must-not-leak" not in response.text


def test_extract_admission_is_bounded(api_harness: ApiHarness) -> None:
    semaphore = api_harness.app.state.extract_semaphore
    for _ in range(api_harness.settings.max_concurrent_jobs):
        assert semaphore.acquire(blocking=False)
    try:
        response = api_harness.client.post(
            "/api/v1/extract",
            json={"url": "https://example.com/video"},
        )
    finally:
        for _ in range(api_harness.settings.max_concurrent_jobs):
            semaphore.release()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3"


def test_download_admission_uses_safe_policy_and_location(api_harness: ApiHarness) -> None:
    source = "https://example.com/video?token=private"
    response = api_harness.client.post(
        "/api/v1/download",
        json={"url": source, "mode": "original", "max_height": 1080},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert response.headers["location"] == f"/api/v1/jobs/{payload['job_id']}"
    stored = api_harness.store.get(payload["job_id"])
    assert stored is not None
    assert stored.source_url == source
    assert stored.max_height == 1080
    assert source not in response.text


def test_auth_status_is_safe_and_auth_requests_fail_closed_when_disabled(
    api_harness: ApiHarness,
) -> None:
    status_response = api_harness.client.get("/api/v1/auth/status")
    assert status_response.status_code == 200
    assert status_response.json() == {
        "enabled": False,
        "available": False,
        "method": None,
    }

    inspect_response = api_harness.client.post(
        "/api/v1/extract",
        json={"url": "https://example.com/private", "use_auth": True},
    )
    download_response = api_harness.client.post(
        "/api/v1/download",
        json={"url": "https://example.com/private", "use_auth": True},
    )

    assert inspect_response.status_code == 409
    assert download_response.status_code == 409
    assert "not configured" in inspect_response.json()["detail"]
    assert api_harness.store.count_active() == 0


def test_configured_auth_status_exposes_no_path_and_readiness_detects_loss(
    settings_factory, tmp_path: Path, monkeypatch
) -> None:
    cookie_file = tmp_path / "session.cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tsecret-value\n",
        encoding="utf-8",
    )
    settings = settings_factory(
        authenticated_media_enabled=True,
        cookie_file=cookie_file,
    )
    monkeypatch.setattr(main_module, "_ffmpeg_ready", lambda _settings: True)
    application = create_app(settings)

    with TestClient(application) as client:
        response = client.get("/api/v1/auth/status")
        assert response.json() == {
            "enabled": True,
            "available": True,
            "method": "mounted_cookie_file",
        }
        assert str(cookie_file) not in response.text
        assert "secret-value" not in response.text

        cookie_file.unlink()
        assert client.get("/api/v1/auth/status").json()["available"] is False
        assert client.get("/ready").status_code == 503


def test_authenticated_download_admission_records_only_a_boolean(
    api_harness: ApiHarness, monkeypatch
) -> None:
    monkeypatch.setattr(routes, "require_authentication", lambda _settings, _enabled: None)

    response = api_harness.client.post(
        "/api/v1/download",
        json={
            "url": "https://example.com/private?secret=signed",
            "use_auth": True,
        },
    )

    assert response.status_code == 202
    stored = api_harness.store.get(response.json()["job_id"])
    assert stored is not None
    assert stored.use_auth is True
    job_response = api_harness.client.get(f"/api/v1/jobs/{stored.job_id}")
    assert job_response.json()["authenticated"] is True
    assert "signed" not in job_response.text


def test_raw_format_selectors_are_rejected_by_api(api_harness: ApiHarness) -> None:
    response = api_harness.client.post(
        "/api/v1/download",
        json={
            "url": "https://example.com/video",
            "mode": "original",
            "format_id": "all,bv+ba",
        },
    )

    assert response.status_code == 422
    assert api_harness.store.count_active() == 0


def test_queue_full_has_retry_hint(api_harness: ApiHarness) -> None:
    api_harness.manager.queue_full = True

    response = api_harness.client.post(
        "/api/v1/download",
        json={"url": "https://example.com/video"},
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": "The download queue is full"}


def test_job_response_never_exposes_source_url_or_output_path(api_harness: ApiHarness) -> None:
    job, output = completed_job(api_harness)

    response = api_harness.client.get(f"/api/v1/jobs/{job.job_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["download_url"] == f"/api/v1/jobs/{job.job_id}/file"
    assert payload["output_name"] == "Fixture media.mkv"
    assert "source_url" not in payload
    assert "output_path" not in payload
    assert "worker_pid" not in payload
    assert "token=private" not in response.text
    assert str(output) not in response.text


def test_completed_file_is_served_with_safe_headers(api_harness: ApiHarness) -> None:
    job, _output = completed_job(api_harness)

    response = api_harness.client.get(f"/api/v1/jobs/{job.job_id}/file")

    assert response.status_code == 200
    assert response.content == b"fixture media"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "filename*=utf-8''Fixture%20media.mkv" in response.headers["content-disposition"]


def test_file_route_rejects_outside_and_symlink_paths(api_harness: ApiHarness) -> None:
    outside = api_harness.settings.download_dir / "outside.mkv"
    outside.write_bytes(b"outside")
    outside_job_id = "2" * 32
    api_harness.settings.job_workspace(outside_job_id).mkdir()
    outside_job = JobRecord(
        job_id=outside_job_id,
        source_url="https://example.com/video",
        mode=FormatMode.ORIGINAL,
        status=JobStatus.COMPLETED,
        output_path=outside,
    )
    assert api_harness.store.try_create(outside_job, max_jobs=10) is not None

    symlink_job_id = "3" * 32
    symlink_workspace = api_harness.settings.job_workspace(symlink_job_id)
    symlink_workspace.mkdir()
    symlink = symlink_workspace / "media.mkv"
    symlink.symlink_to(outside)
    symlink_job = JobRecord(
        job_id=symlink_job_id,
        source_url="https://example.com/video",
        mode=FormatMode.ORIGINAL,
        status=JobStatus.COMPLETED,
        output_path=symlink,
    )
    assert api_harness.store.try_create(symlink_job, max_jobs=10) is not None

    assert api_harness.client.get(f"/api/v1/jobs/{outside_job_id}/file").status_code == 410
    assert api_harness.client.get(f"/api/v1/jobs/{symlink_job_id}/file").status_code == 410


def test_delete_active_requests_cancellation_without_leaking_source(
    api_harness: ApiHarness,
) -> None:
    accepted = api_harness.client.post(
        "/api/v1/download",
        json={"url": "https://example.com/video?token=private"},
    ).json()

    response = api_harness.client.delete(f"/api/v1/jobs/{accepted['job_id']}")

    assert response.status_code == 202
    assert response.headers["location"] == f"/api/v1/jobs/{accepted['job_id']}"
    assert response.json()["status"] == "cancelling"
    assert "token=private" not in response.text


def test_delete_terminal_removes_record_and_workspace(api_harness: ApiHarness) -> None:
    job, _output = completed_job(api_harness)
    workspace = api_harness.settings.job_workspace(job.job_id)

    response = api_harness.client.delete(f"/api/v1/jobs/{job.job_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "job_id": job.job_id}
    assert api_harness.store.get(job.job_id) is None
    assert not workspace.exists()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/jobs/00000000000000000000000000000000",
        "/api/v1/jobs/00000000000000000000000000000000/file",
    ],
)
def test_unknown_job_is_not_found(api_harness: ApiHarness, path: str) -> None:
    assert api_harness.client.get(path).status_code == 404
    assert api_harness.client.delete(path.removesuffix("/file")).status_code == 404

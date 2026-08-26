"""Versioned OmniFetch API routes."""

from __future__ import annotations

from pathlib import Path

import yt_dlp
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from ..config import Settings
from ..models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    AuthenticationStatus,
    DownloadRequest,
    ExtractRequest,
    ExtractResponse,
    JobAccepted,
    JobResponse,
    JobStatus,
)
from ..services.authentication import (
    AuthenticationUnavailable,
    authentication_available,
    require_authentication,
)
from ..services.extractor import (
    GenericExtractorDisabled,
    UnsupportedCollectionError,
    extract_metadata,
    safe_extraction_error,
)
from ..services.jobs import JobStore
from ..services.manager import DownloadManager, QueueFullError
from ..services.platform import list_known_platforms
from ..services.security import UnsafeURLError, validate_url

router = APIRouter(prefix="/api/v1")


def _services(request: Request) -> tuple[Settings, JobStore, DownloadManager]:
    try:
        return (
            request.app.state.settings,
            request.app.state.job_store,
            request.app.state.download_manager,
        )
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="Service is starting") from exc


@router.get("/platforms")
def platforms() -> dict[str, object]:
    return {
        "known": list_known_platforms(),
        "note": "Availability is determined by the configured yt-dlp extractors.",
    }


@router.get("/auth/status", response_model=AuthenticationStatus)
def auth_status(request: Request) -> AuthenticationStatus:
    settings, _store, _manager = _services(request)
    available = authentication_available(settings)
    return AuthenticationStatus(
        enabled=settings.authenticated_media_enabled,
        available=available,
        method="mounted_cookie_file" if available else None,
    )


@router.post("/extract", response_model=ExtractResponse)
def extract(payload: ExtractRequest, request: Request) -> ExtractResponse:
    settings, _store, _manager = _services(request)
    semaphore = request.app.state.extract_semaphore
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Too many inspections are in progress",
            headers={"Retry-After": "3"},
        )
    try:
        validate_url(payload.url, settings)
        require_authentication(settings, payload.use_auth)
        return extract_metadata(payload.url, settings, use_auth=payload.use_auth)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (UnsupportedCollectionError, GenericExtractorDisabled) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AuthenticationUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=422, detail=safe_extraction_error(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        semaphore.release()


@router.post("/download", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def download(payload: DownloadRequest, request: Request, response: Response) -> JobAccepted:
    settings, _store, manager = _services(request)
    try:
        validate_url(payload.url, settings)
        require_authentication(settings, payload.use_auth)
        job = manager.submit(payload)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthenticationUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
    response.headers["Location"] = f"/api/v1/jobs/{job.job_id}"
    return JobAccepted(job_id=job.job_id, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, request: Request) -> JobResponse:
    settings, store, _manager = _services(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse.from_record(job, ttl_seconds=settings.job_ttl_seconds)


@router.get("/jobs/{job_id}/file")
def get_job_file(job_id: str, request: Request) -> FileResponse:
    settings, store, _manager = _services(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED or job.output_path is None:
        raise HTTPException(status_code=409, detail=f"Job is {job.status.value}, not ready")
    try:
        root = settings.job_workspace(job_id).resolve(strict=True)
        candidate = Path(job.output_path)
        if candidate.is_symlink():
            raise HTTPException(status_code=410, detail="File is no longer available")
        output = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=410, detail="File is no longer available") from None
    if not output.is_relative_to(root) or not output.is_file():
        raise HTTPException(status_code=410, detail="File is no longer available")
    return FileResponse(
        output,
        filename=job.download_name or output.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete(
    "/jobs/{job_id}",
    summary="Cancel or delete one job",
    description=(
        "Enter the job_id returned by POST /api/v1/download. Active jobs are cancelled; "
        "completed, failed, rejected, or cancelled jobs are deleted."
    ),
)
def delete_job(
    job_id: str, request: Request, response: Response
) -> dict[str, object] | JobResponse:
    settings, store, manager = _services(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ACTIVE_STATUSES:
        cancelling = manager.cancel(job_id)
        if cancelling is None:
            raise HTTPException(status_code=404, detail="Job not found")
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = f"/api/v1/jobs/{job_id}"
        return JobResponse.from_record(cancelling, ttl_seconds=settings.job_ttl_seconds)
    if job.status not in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Job cannot be removed")
    removed = store.delete_terminal(job_id)
    if removed is None:
        raise HTTPException(status_code=409, detail="Job state changed; retry")
    manager.delete_files(job_id)
    return {"deleted": True, "job_id": job_id}

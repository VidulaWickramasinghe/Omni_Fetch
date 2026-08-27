"""Versioned OmniFetch API routes."""

from __future__ import annotations

import json
import mimetypes
import time
from collections.abc import Iterator
from pathlib import Path

import yt_dlp
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse

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
_DIRECT_STREAM_MEDIA_TYPE = "application/vnd.omnifetch.download"
_DIRECT_STREAM_CHUNK_BYTES = 1024 * 1024


def _services(request: Request) -> tuple[Settings, JobStore, DownloadManager]:
    try:
        return (
            request.app.state.settings,
            request.app.state.job_store,
            request.app.state.download_manager,
        )
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="Service is starting") from exc


def _stream_event(event: dict[str, object]) -> bytes:
    return (json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")


def _completed_output(settings: Settings, job_id: str, output_path: Path | None) -> Path:
    if output_path is None:
        raise FileNotFoundError
    root = settings.job_workspace(job_id).resolve(strict=True)
    candidate = Path(output_path)
    if candidate.is_symlink():
        raise FileNotFoundError
    output = candidate.resolve(strict=True)
    if not output.is_relative_to(root) or not output.is_file():
        raise FileNotFoundError
    return output


def _direct_download_stream(
    *,
    job_id: str,
    settings: Settings,
    store: JobStore,
    manager: DownloadManager,
) -> Iterator[bytes]:
    """Keep one serverless invocation alive through work and file delivery."""

    last_signature: tuple[object, ...] | None = None
    last_emit = 0.0
    try:
        while True:
            job = store.get(job_id)
            if job is None:
                yield _stream_event(
                    {
                        "type": "error",
                        "detail": "The download job is no longer available",
                    }
                )
                return
            signature = (
                job.status,
                job.progress,
                job.updated_at,
                job.output_size,
                job.error,
            )
            if signature != last_signature:
                snapshot = JobResponse.from_record(
                    job, ttl_seconds=settings.job_ttl_seconds
                ).model_dump(mode="json")
                yield _stream_event({"type": "job", "job": snapshot})
                last_signature = signature
                last_emit = time.monotonic()
            elif time.monotonic() - last_emit >= 5:
                yield _stream_event({"type": "heartbeat"})
                last_emit = time.monotonic()
            if job.status in TERMINAL_STATUSES:
                if job.status != JobStatus.COMPLETED:
                    return
                try:
                    output = _completed_output(settings, job_id, job.output_path)
                except (FileNotFoundError, OSError):
                    yield _stream_event(
                        {
                            "type": "error",
                            "detail": "The completed file is no longer available",
                        }
                    )
                    return
                content_type = mimetypes.guess_type(output.name)[0] or "application/octet-stream"
                yield _stream_event(
                    {
                        "type": "file",
                        "name": job.download_name or output.name,
                        "size": output.stat().st_size,
                        "content_type": content_type,
                    }
                )
                with output.open("rb") as handle:
                    while chunk := handle.read(_DIRECT_STREAM_CHUNK_BYTES):
                        yield chunk
                return
            time.sleep(0.25)
    finally:
        current = store.get(job_id)
        if current is not None and current.status in ACTIVE_STATUSES:
            manager.cancel(job_id)
        elif current is not None and current.status in TERMINAL_STATUSES:
            store.delete_terminal(job_id)
            manager.delete_files(job_id)


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
def download(
    payload: DownloadRequest, request: Request, response: Response
) -> JobAccepted | StreamingResponse:
    settings, store, manager = _services(request)
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
    if settings.serverless:
        return StreamingResponse(
            _direct_download_stream(
                job_id=job.job_id,
                settings=settings,
                store=store,
                manager=manager,
            ),
            status_code=status.HTTP_200_OK,
            media_type=_DIRECT_STREAM_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
                "X-OmniFetch-Delivery": "stream",
            },
        )
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
        output = _completed_output(settings, job_id, job.output_path)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=410, detail="File is no longer available") from None
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

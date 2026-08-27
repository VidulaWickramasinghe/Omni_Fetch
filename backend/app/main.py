"""FastAPI application factory and owned runtime lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import Settings
from .services.authentication import authentication_available, require_authentication
from .services.jobs import JobStore
from .services.manager import DownloadManager
from .services.runtime import (
    ejs_available,
    impersonation_available,
    resolve_ffmpeg_location,
    resolve_js_runtimes,
)


def _media_runtime_ready(settings: Settings) -> bool:
    """Require the media tools used by the configured delivery profile."""

    core_ready = resolve_ffmpeg_location(settings) is not None and impersonation_available()
    if settings.serverless:
        # Vercel has no bundled JavaScript runtime, but its streaming delivery
        # supports sources such as Instagram and TikTok that use the core media
        # and browser-impersonation toolchain. YouTube reports a scoped error if
        # a source requires JavaScript challenge execution.
        return core_ready
    return core_ready and bool(resolve_js_runtimes()) and ejs_available()


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured.prepare_runtime_dirs()
        require_authentication(configured, configured.authenticated_media_enabled)
        store = JobStore()
        manager = DownloadManager(configured, store)
        application.state.job_store = store
        application.state.download_manager = manager
        application.state.extract_semaphore = threading.BoundedSemaphore(
            configured.max_concurrent_jobs
        )
        await asyncio.to_thread(manager.sweep_orphans)
        stop = asyncio.Event()

        async def cleanup_loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=configured.cleanup_interval_seconds)
                except TimeoutError:
                    await asyncio.to_thread(manager.cleanup_expired)

        cleanup_task = asyncio.create_task(cleanup_loop(), name="omnifetch-cleanup")
        application.state.cleanup_task = cleanup_task
        try:
            yield
        finally:
            stop.set()
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
            await asyncio.to_thread(manager.close)

    application = FastAPI(
        title="OmniFetch API",
        description=(
            "Extract and download public or explicitly authorized media through isolated "
            "yt-dlp workers."
        ),
        version="0.4.0",
        lifespan=lifespan,
    )
    application.state.settings = configured
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )

    @application.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > configured.max_body_bytes:
                        return JSONResponse(
                            status_code=413, content={"detail": "Request body is too large"}
                        )
                except ValueError:
                    return JSONResponse(
                        status_code=400, content={"detail": "Invalid Content-Length"}
                    )
            body = await request.body()
            if len(body) > configured.max_body_bytes:
                return JSONResponse(
                    status_code=413, content={"detail": "Request body is too large"}
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response

    application.include_router(router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        manager = getattr(request.app.state, "download_manager", None)
        writable = configured.download_dir.is_dir() and os.access(configured.download_dir, os.W_OK)
        auth_ready = not configured.authenticated_media_enabled or authentication_available(
            configured
        )
        if (
            manager is None
            or not manager.ready
            or not writable
            or not auth_ready
            or not _media_runtime_ready(configured)
        ):
            raise HTTPException(status_code=503, detail="Service is not ready")
        return {"status": "ready"}

    if configured.frontend_dir.is_dir() and (configured.frontend_dir / "index.html").is_file():
        application.mount(
            "/",
            StaticFiles(directory=str(configured.frontend_dir), html=True),
            name="frontend",
        )
    return application


app = create_app()

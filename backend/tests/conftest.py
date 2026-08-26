from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def settings_factory(tmp_path: Path) -> SettingsFactory:
    base = Settings(
        download_dir=tmp_path / "downloads",
        frontend_dir=tmp_path / "frontend",
        max_filesize_bytes=1024 * 1024,
        max_duration_seconds=600,
        max_concurrent_jobs=2,
        max_queued_jobs=2,
        job_ttl_seconds=60,
        job_timeout_seconds=30,
        cleanup_interval_seconds=10,
        socket_timeout_seconds=1,
        max_url_length=256,
        max_body_bytes=1024,
        subprocess_terminate_grace_seconds=1,
        max_worker_event_bytes=4096,
        allowed_ports=frozenset({80, 443}),
        cors_origins=("http://127.0.0.1:8000",),
        public_mode=False,
        allow_generic_extractor=False,
    )

    def build(**overrides: object) -> Settings:
        return replace(base, **overrides)

    return build

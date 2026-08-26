from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.models import TERMINAL_STATUSES, DownloadRequest, FormatMode, JobRecord, JobStatus
from app.services.jobs import JobStore
from app.services.manager import DownloadManager


def make_job(index: int = 0, *, status: JobStatus = JobStatus.QUEUED) -> JobRecord:
    return JobRecord(
        job_id=f"{index + 101:032x}",
        source_url=f"https://example.com/video/{index}",
        mode=FormatMode.ORIGINAL,
        status=status,
    )


@pytest.fixture
def manager_harness(settings_factory):
    settings = settings_factory()
    settings.prepare_runtime_dirs()
    store = JobStore()
    manager = DownloadManager(settings, store)
    try:
        yield settings, store, manager
    finally:
        manager.close()


def test_sweep_orphans_removes_only_unowned_valid_job_directories(manager_harness) -> None:
    settings, store, manager = manager_harness
    known = make_job()
    assert store.try_create(known, max_jobs=1) is not None
    known_dir = settings.job_workspace(known.job_id)
    known_dir.mkdir()
    orphan_id = "f" * 32
    orphan_dir = settings.job_workspace(orphan_id)
    orphan_dir.mkdir()
    (orphan_dir / "partial.part").write_bytes(b"partial")
    unrelated_dir = settings.download_dir / "operator-notes"
    unrelated_dir.mkdir()
    unrelated_file = settings.download_dir / "README.txt"
    unrelated_file.write_text("do not remove")

    manager.sweep_orphans()

    assert known_dir.is_dir()
    assert not orphan_dir.exists()
    assert unrelated_dir.is_dir()
    assert unrelated_file.is_file()


def test_cleanup_expired_removes_record_and_workspace(manager_harness) -> None:
    settings, store, manager = manager_harness
    expired = make_job(status=JobStatus.FAILED)
    expired.updated_at = 0
    assert store.try_create(expired, max_jobs=1) is not None
    workspace = settings.job_workspace(expired.job_id)
    workspace.mkdir()
    (workspace / "partial.part").write_bytes(b"partial")

    assert manager.cleanup_expired() == 1
    assert store.get(expired.job_id) is None
    assert not workspace.exists()


def test_delete_files_cannot_escape_download_root(manager_harness, tmp_path: Path) -> None:
    _settings, _store, manager = manager_harness
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep")

    manager.delete_files("../outside")

    assert sentinel.read_text() == "keep"


def test_output_path_validation_accepts_only_regular_contained_file(manager_harness) -> None:
    settings, _store, manager = manager_harness
    job_id = "d" * 32
    workspace = settings.job_workspace(job_id)
    workspace.mkdir()
    inside = workspace / "media.mkv"
    inside.write_bytes(b"media")
    outside = settings.download_dir / "outside.mkv"
    outside.write_bytes(b"outside")
    symlink = workspace / "link.mkv"
    symlink.symlink_to(outside)

    assert manager._validated_output_path(job_id, str(inside)) == inside.resolve()
    with pytest.raises(RuntimeError, match="unsafe output path"):
        manager._validated_output_path(job_id, str(outside))
    with pytest.raises(RuntimeError, match="unsafe output path"):
        manager._validated_output_path(job_id, str(symlink))


def test_worker_events_advance_monotonically_and_sanitize_filename(manager_harness) -> None:
    settings, store, manager = manager_harness
    job = make_job()
    assert store.try_create(job, max_jobs=1) is not None
    store.patch(job.job_id, status=JobStatus.INSPECTING)
    workspace = settings.job_workspace(job.job_id)
    workspace.mkdir()
    output = workspace / "media.mkv"
    output.write_bytes(b"media")

    manager._handle_event_line(
        job.job_id,
        json.dumps({"type": "metadata", "title": "t" * 900, "platform": "p" * 200}),
    )
    manager._handle_event_line(
        job.job_id,
        json.dumps({"type": "progress", "status": "downloading", "progress": 150}),
    )
    manager._handle_event_line(
        job.job_id,
        json.dumps({"type": "progress", "status": "downloading", "progress": 25}),
    )
    manager._handle_event_line(
        job.job_id,
        json.dumps(
            {
                "type": "complete",
                "path": str(output),
                "name": "../../unsafe-name.mkv",
                "size": 999999,
            }
        ),
    )

    completed = store.get(job.job_id)
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED
    assert completed.progress == 100
    assert len(completed.title or "") == 500
    assert len(completed.platform or "") == 80
    assert completed.download_name == "unsafe-name.mkv"
    assert completed.output_size == len(b"media")
    assert completed.output_path == output.resolve()


def test_cancelled_job_ignores_late_complete_and_cannot_resurrect(manager_harness) -> None:
    settings, store, manager = manager_harness
    job = make_job()
    assert store.try_create(job, max_jobs=1) is not None
    store.patch(job.job_id, status=JobStatus.INSPECTING)
    workspace = settings.job_workspace(job.job_id)
    workspace.mkdir()
    output = workspace / "media.mkv"
    output.write_bytes(b"media")
    manager.cancel(job.job_id)

    manager._handle_event_line(
        job.job_id,
        json.dumps({"type": "complete", "path": str(output), "name": "late.mkv"}),
    )
    assert store.get(job.job_id).status == JobStatus.CANCELLING

    manager._finish_cancel(job.job_id)
    cancelled = store.get(job.job_id)
    assert cancelled is not None
    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.output_path is None
    assert not workspace.exists()

    manager._handle_event_line(
        job.job_id,
        json.dumps({"type": "complete", "path": str(output), "name": "late.mkv"}),
    )
    assert store.get(job.job_id).status == JobStatus.CANCELLED


def test_invalid_or_oversized_worker_events_are_ignored(manager_harness) -> None:
    settings, store, manager = manager_harness
    job = make_job()
    assert store.try_create(job, max_jobs=1) is not None
    store.patch(job.job_id, status=JobStatus.INSPECTING)

    manager._handle_event_line(job.job_id, "not-json")
    manager._handle_event_line(job.job_id, "x" * (settings.max_worker_event_bytes + 1))
    manager._handle_event_line(job.job_id, json.dumps(["not", "an", "object"]))

    assert store.get(job.job_id).status == JobStatus.INSPECTING


@pytest.mark.integration
def test_real_worker_process_rejects_unsafe_url_without_leaving_files(
    manager_harness,
) -> None:
    settings, store, manager = manager_harness

    accepted = manager.submit(
        DownloadRequest(url="https://127.0.0.1/private", mode=FormatMode.ORIGINAL)
    )
    deadline = time.monotonic() + 5
    current = store.get(accepted.job_id)
    while current is not None and current.status not in TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            pytest.fail("isolated worker did not terminate within five seconds")
        time.sleep(0.02)
        current = store.get(accepted.job_id)

    assert current is not None
    assert current.status == JobStatus.REJECTED
    assert "non-public" in (current.error or "")
    assert not settings.job_workspace(accepted.job_id).exists()

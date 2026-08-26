from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.models import FormatMode, JobRecord, JobStatus
from app.services.jobs import JobStore, JobTransitionError


def make_job(index: int = 0, *, status: JobStatus = JobStatus.QUEUED) -> JobRecord:
    return JobRecord(
        job_id=f"{index + 1:032x}",
        source_url=f"https://example.com/video/{index}",
        mode=FormatMode.ORIGINAL,
        status=status,
    )


def test_store_returns_deep_snapshots() -> None:
    store = JobStore()
    created = store.try_create(make_job(), max_jobs=1)
    assert created is not None

    created.title = "mutated outside the store"
    fetched = store.get(created.job_id)

    assert fetched is not None
    assert fetched.title is None
    fetched.title = "another mutation"
    assert store.get(created.job_id).title is None


def test_atomic_admission_never_exceeds_capacity() -> None:
    store = JobStore()
    contenders = 24
    capacity = 3
    barrier = Barrier(contenders)

    def admit(index: int) -> bool:
        barrier.wait()
        return store.try_create(make_job(index), max_jobs=capacity) is not None

    with ThreadPoolExecutor(max_workers=contenders) as executor:
        admitted = list(executor.map(admit, range(contenders)))

    assert sum(admitted) == capacity
    assert store.count_active() == capacity
    assert len(store.ids()) == capacity


def test_duplicate_identifier_does_not_replace_existing_job() -> None:
    store = JobStore()
    original = make_job()
    assert store.try_create(original, max_jobs=2) is not None
    duplicate = original.model_copy(update={"source_url": "https://attacker.example/replaced"})

    assert store.try_create(duplicate, max_jobs=2) is None
    assert store.get(original.job_id).source_url == original.source_url


def test_lifecycle_transitions_are_validated() -> None:
    store = JobStore()
    job = store.try_create(make_job(), max_jobs=1)
    assert job is not None

    with pytest.raises(JobTransitionError, match="queued -> completed"):
        store.patch(job.job_id, status=JobStatus.COMPLETED)

    for status in (
        JobStatus.INSPECTING,
        JobStatus.DOWNLOADING,
        JobStatus.PROCESSING,
        JobStatus.COMPLETED,
    ):
        updated = store.patch(job.job_id, status=status)
        assert updated is not None
        assert updated.status == status


def test_expected_status_prevents_stale_worker_update() -> None:
    store = JobStore()
    job = store.try_create(make_job(), max_jobs=1)
    assert job is not None
    store.patch(job.job_id, status=JobStatus.INSPECTING)

    stale = store.patch(
        job.job_id,
        expected_status=JobStatus.QUEUED,
        status=JobStatus.FAILED,
    )

    assert stale is not None
    assert stale.status == JobStatus.INSPECTING


def test_cancel_delete_and_late_patch_cannot_resurrect_job() -> None:
    store = JobStore()
    job = store.try_create(make_job(), max_jobs=1)
    assert job is not None

    cancelling = store.request_cancel(job.job_id)
    assert cancelling is not None
    assert cancelling.status == JobStatus.CANCELLING
    assert store.is_cancel_requested(job.job_id)
    cancelled = store.patch(
        job.job_id,
        expected_status=JobStatus.CANCELLING,
        status=JobStatus.CANCELLED,
    )
    assert cancelled is not None
    assert store.delete_terminal(job.job_id) is not None

    assert store.patch(job.job_id, status=JobStatus.FAILED) is None
    assert store.get(job.job_id) is None
    assert job.job_id not in store.ids()


def test_terminal_only_deletion_and_expiry() -> None:
    store = JobStore()
    active = make_job(0)
    expired = make_job(1, status=JobStatus.FAILED)
    fresh = make_job(2, status=JobStatus.COMPLETED)
    expired.updated_at = 10
    fresh.updated_at = 19
    assert store.try_create(active, max_jobs=3) is not None
    assert store.try_create(expired, max_jobs=3) is not None
    assert store.try_create(fresh, max_jobs=3) is not None

    assert store.delete_terminal(active.job_id) is None
    removed = store.purge_expired(ttl_seconds=5, now=20)

    assert [job.job_id for job in removed] == [expired.job_id]
    assert store.get(active.job_id) is not None
    assert store.get(fresh.job_id) is not None

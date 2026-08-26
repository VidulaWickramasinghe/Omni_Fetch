"""Thread-safe in-memory job state with atomic admission and cancellation."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from app.models import ACTIVE_STATUSES, TERMINAL_STATUSES, JobRecord, JobStatus


class JobTransitionError(RuntimeError):
    """Raised when a caller attempts an invalid lifecycle transition."""


_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.INSPECTING, JobStatus.CANCELLING, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.INSPECTING: frozenset(
        {
            JobStatus.DOWNLOADING,
            JobStatus.PROCESSING,
            JobStatus.REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLING,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.DOWNLOADING: frozenset(
        {
            JobStatus.PROCESSING,
            JobStatus.COMPLETED,
            JobStatus.REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLING,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PROCESSING: frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLING,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.REJECTED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class JobStore:
    """Own all mutable job state and return deep snapshots to callers."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _snapshot(job: JobRecord) -> JobRecord:
        return job.model_copy(deep=True)

    def try_create(self, job: JobRecord, max_jobs: int) -> JobRecord | None:
        """Atomically reserve queue capacity and create a job."""

        with self._lock:
            active = sum(1 for record in self._jobs.values() if record.status in ACTIVE_STATUSES)
            if active >= max_jobs or job.job_id in self._jobs:
                return None
            stored = self._snapshot(job)
            self._jobs[job.job_id] = stored
            self._cancel_events[job.job_id] = threading.Event()
            return self._snapshot(stored)

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._snapshot(job) if job else None

    def patch(
        self,
        job_id: str,
        *,
        expected_status: JobStatus | Iterable[JobStatus] | None = None,
        **changes: object,
    ) -> JobRecord | None:
        """Validate and atomically apply changes, returning a new snapshot."""

        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return None
            if expected_status is not None:
                expected = (
                    {expected_status}
                    if isinstance(expected_status, JobStatus)
                    else set(expected_status)
                )
                if current.status not in expected:
                    return self._snapshot(current)

            next_status = changes.get("status", current.status)
            if not isinstance(next_status, JobStatus):
                next_status = JobStatus(str(next_status))
                changes["status"] = next_status
            if next_status != current.status and next_status not in _TRANSITIONS[current.status]:
                raise JobTransitionError(
                    f"Invalid job transition: {current.status.value} -> {next_status.value}"
                )

            data = current.model_dump()
            data.update(changes)
            data["updated_at"] = time.time()
            updated = JobRecord.model_validate(data)
            self._jobs[job_id] = updated
            return self._snapshot(updated)

    def request_cancel(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in TERMINAL_STATUSES:
                return self._snapshot(job)
            self._cancel_events[job_id].set()
            data = job.model_dump()
            data.update(status=JobStatus.CANCELLING, updated_at=time.time())
            updated = JobRecord.model_validate(data)
            self._jobs[job_id] = updated
            return self._snapshot(updated)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
            return bool(event and event.is_set())

    def cancellation_event(self, job_id: str) -> threading.Event | None:
        with self._lock:
            return self._cancel_events.get(job_id)

    def delete_terminal(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in TERMINAL_STATUSES:
                return None
            removed = self._jobs.pop(job_id)
            self._cancel_events.pop(job_id, None)
            return self._snapshot(removed)

    def count_active(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status in ACTIVE_STATUSES)

    def purge_expired(self, ttl_seconds: int, *, now: float | None = None) -> list[JobRecord]:
        """Remove expired terminal records; active jobs are never TTL-purged."""

        cutoff = (time.time() if now is None else now) - ttl_seconds
        with self._lock:
            expired_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.status in TERMINAL_STATUSES and job.updated_at < cutoff
            ]
            removed = [self._jobs.pop(job_id) for job_id in expired_ids]
            for job_id in expired_ids:
                self._cancel_events.pop(job_id, None)
            return [self._snapshot(job) for job in removed]

    def ids(self) -> set[str]:
        with self._lock:
            return set(self._jobs)

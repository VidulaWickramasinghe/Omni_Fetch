"""Bounded supervisor for killable per-job downloader processes."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import IO

from ..config import Settings
from ..models import ACTIVE_STATUSES, TERMINAL_STATUSES, DownloadRequest, JobRecord, JobStatus
from .jobs import JobStore, JobTransitionError


class QueueFullError(RuntimeError):
    """No concurrency or queue capacity remains."""


class DownloadManager:
    """Own a bounded executor and supervise one child process per active slot."""

    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_jobs,
            thread_name_prefix="omnifetch-job",
        )
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._futures: dict[str, Future[None]] = {}
        self._closed = False

    def submit(self, request: DownloadRequest) -> JobRecord:
        job = JobRecord(
            source_url=request.url,
            mode=request.mode,
            max_height=request.max_height,
            use_auth=request.use_auth,
        )
        accepted = self.store.try_create(job, self.settings.job_capacity)
        if accepted is None:
            raise QueueFullError("The download queue is full")
        with self._lock:
            if self._closed:
                self.store.patch(job.job_id, status=JobStatus.FAILED, error="Service is stopping")
                raise RuntimeError("Download manager is closed")
            future = self._executor.submit(self._supervise, job.job_id)
            self._futures[job.job_id] = future
            future.add_done_callback(lambda _future, jid=job.job_id: self._forget_future(jid))
        return accepted

    def cancel(self, job_id: str) -> JobRecord | None:
        job = self.store.request_cancel(job_id)
        if job and job.status == JobStatus.CANCELLING:
            with self._lock:
                process = self._processes.get(job_id)
            if process and process.poll() is None:
                self._signal_process(process)
        return job

    def cleanup_expired(self) -> int:
        expired = self.store.purge_expired(self.settings.job_ttl_seconds)
        for job in expired:
            self._remove_workspace(job.job_id)
        return len(expired)

    def sweep_orphans(self) -> None:
        """Remove per-job directories that cannot be reached after a restart."""

        known = self.store.ids()
        for child in self.settings.download_dir.iterdir():
            if child.is_dir() and child.name not in known:
                self._remove_workspace(child.name)

    def delete_files(self, job_id: str) -> None:
        self._remove_workspace(job_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            job_ids = list(self._futures)
        for job_id in job_ids:
            self.cancel(job_id)
        self._executor.shutdown(wait=True, cancel_futures=False)

    @property
    def ready(self) -> bool:
        with self._lock:
            return not self._closed

    def _forget_future(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def _supervise(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        if self.store.is_cancel_requested(job_id):
            self._finish_cancel(job_id)
            return
        job = self.store.patch(
            job_id, expected_status=JobStatus.QUEUED, status=JobStatus.INSPECTING
        )
        if job is None or job.status != JobStatus.INSPECTING:
            self._finish_cancel(job_id)
            return

        payload = {
            "job_id": job.job_id,
            "url": job.source_url,
            "mode": job.mode.value,
            "max_height": job.max_height,
            "use_auth": job.use_auth,
            "policy": self.settings.worker_policy(),
        }
        try:
            process, messages = self._launch_worker(job_id, payload)
        except Exception:
            self.store.patch(
                job_id,
                expected_status=ACTIVE_STATUSES,
                status=JobStatus.FAILED,
                error="The isolated downloader could not be started",
                worker_pid=None,
            )
            self._remove_workspace(job_id)
            return
        stderr_tail: deque[str] = deque(maxlen=20)

        deadline = time.monotonic() + self.settings.job_timeout_seconds
        streams_done: set[str] = set()
        timed_out = False
        signalled_at: float | None = None
        try:
            while process.poll() is None or len(streams_done) < 2 or not messages.empty():
                try:
                    stream, line = messages.get(timeout=0.1)
                    if line is None:
                        streams_done.add(stream)
                    elif stream == "stderr":
                        stderr_tail.append(line[-2000:])
                    else:
                        self._handle_event_line(job_id, line)
                except queue.Empty:
                    pass

                now = time.monotonic()
                cancel_requested = self.store.is_cancel_requested(job_id)
                if now >= deadline and not timed_out:
                    timed_out = True
                    self._signal_process(process)
                    signalled_at = now
                elif cancel_requested and signalled_at is None:
                    self._signal_process(process)
                    signalled_at = now
                elif (
                    signalled_at is not None
                    and now - signalled_at >= self.settings.subprocess_terminate_grace_seconds
                ):
                    self._kill_process(process)
                    signalled_at = now + 86400

            process.wait()
            current = self.store.get(job_id)
            if current is None:
                self._remove_workspace(job_id)
            elif self.store.is_cancel_requested(job_id):
                self._finish_cancel(job_id)
            elif timed_out and current.status not in TERMINAL_STATUSES:
                self.store.patch(
                    job_id,
                    expected_status=ACTIVE_STATUSES,
                    status=JobStatus.REJECTED,
                    error="Download exceeded the wall-clock time limit",
                    worker_pid=None,
                )
                self._remove_workspace(job_id)
            elif current.status not in TERMINAL_STATUSES:
                self.store.patch(
                    job_id,
                    expected_status=ACTIVE_STATUSES,
                    status=JobStatus.FAILED,
                    error="The downloader stopped before producing a file",
                    worker_pid=None,
                )
                self._remove_workspace(job_id)
        except Exception:
            self._kill_process(process)
            current = self.store.get(job_id)
            if current and current.status not in TERMINAL_STATUSES:
                if self.store.is_cancel_requested(job_id):
                    self._finish_cancel(job_id)
                else:
                    with suppress(JobTransitionError):
                        self.store.patch(
                            job_id,
                            expected_status=ACTIVE_STATUSES,
                            status=JobStatus.FAILED,
                            error="The download supervisor failed",
                            worker_pid=None,
                        )
                    self._remove_workspace(job_id)
        finally:
            with self._lock:
                self._processes.pop(job_id, None)

    def _launch_worker(
        self, job_id: str, payload: dict[str, object]
    ) -> tuple[subprocess.Popen[str], queue.Queue[tuple[str, str | None]]]:
        """Start and fully wire a worker or tear down any partial process."""

        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.workers.download_worker"],
                cwd=str(Path(__file__).resolve().parents[2]),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                self._processes[job_id] = process
            self.store.patch(job_id, expected_status=JobStatus.INSPECTING, worker_pid=process.pid)
            messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
            threading.Thread(
                target=self._read_pipe,
                args=("stdout", process.stdout, messages),
                daemon=True,
            ).start()
            threading.Thread(
                target=self._read_pipe,
                args=("stderr", process.stderr, messages),
                daemon=True,
            ).start()
            if process.stdin is None:
                raise RuntimeError("Worker stdin is unavailable")
            process.stdin.write(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
            )
            process.stdin.close()
            return process, messages
        except Exception:
            if process is not None:
                self._kill_process(process)
                with self._lock:
                    self._processes.pop(job_id, None)
            raise

    @staticmethod
    def _read_pipe(
        name: str,
        pipe: IO[str] | None,
        messages: queue.Queue[tuple[str, str | None]],
    ) -> None:
        if pipe is None:
            messages.put((name, None))
            return
        try:
            for line in pipe:
                messages.put((name, line.rstrip("\r\n")))
        finally:
            pipe.close()
            messages.put((name, None))

    def _handle_event_line(self, job_id: str, line: str) -> None:
        if len(line.encode("utf-8")) > self.settings.max_worker_event_bytes:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return
        if self.store.is_cancel_requested(job_id):
            return

        event_type = event["type"]
        if event_type == "metadata":
            self.store.patch(
                job_id,
                expected_status=JobStatus.INSPECTING,
                title=str(event.get("title"))[:500] if event.get("title") else None,
                platform=str(event.get("platform"))[:80] if event.get("platform") else None,
            )
        elif event_type == "progress":
            status = (
                JobStatus.DOWNLOADING
                if event.get("status") == "downloading"
                else JobStatus.PROCESSING
            )
            value = max(0.0, min(99.0, float(event.get("progress") or 0.0)))
            current = self.store.get(job_id)
            if (
                current
                and current.status in ACTIVE_STATUSES
                and current.status != JobStatus.CANCELLING
            ):
                self.store.patch(
                    job_id,
                    expected_status=current.status,
                    status=status,
                    progress=max(current.progress, value),
                )
        elif event_type == "complete":
            output = self._validated_output_path(job_id, str(event.get("path") or ""))
            self.store.patch(
                job_id,
                expected_status={
                    JobStatus.INSPECTING,
                    JobStatus.DOWNLOADING,
                    JobStatus.PROCESSING,
                },
                status=JobStatus.COMPLETED,
                progress=100.0,
                output_path=output,
                download_name=Path(str(event.get("name") or output.name)).name[:180],
                output_size=output.stat().st_size,
                completed_at=time.time(),
                worker_pid=None,
            )
        elif event_type in {"rejected", "failed"}:
            status = JobStatus.REJECTED if event_type == "rejected" else JobStatus.FAILED
            error = str(event.get("error") or "Download failed")[:500]
            self.store.patch(
                job_id,
                expected_status=ACTIVE_STATUSES,
                status=status,
                error=error,
                worker_pid=None,
            )
            self._remove_workspace(job_id)

    def _validated_output_path(self, job_id: str, raw_path: str) -> Path:
        workspace = self.settings.job_workspace(job_id).resolve(strict=True)
        candidate = Path(raw_path)
        if candidate.is_symlink():
            raise RuntimeError("Worker returned an unsafe output path")
        output = candidate.resolve(strict=True)
        if not output.is_relative_to(workspace) or not output.is_file():
            raise RuntimeError("Worker returned an unsafe output path")
        return output

    def _finish_cancel(self, job_id: str) -> None:
        current = self.store.get(job_id)
        if current and current.status == JobStatus.CANCELLING:
            self.store.patch(
                job_id,
                expected_status=JobStatus.CANCELLING,
                status=JobStatus.CANCELLED,
                error=None,
                worker_pid=None,
                completed_at=time.time(),
            )
        elif current and current.status == JobStatus.QUEUED:
            self.store.patch(
                job_id,
                expected_status=JobStatus.QUEUED,
                status=JobStatus.CANCELLED,
                worker_pid=None,
                completed_at=time.time(),
            )
        self._remove_workspace(job_id)

    @staticmethod
    def _signal_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def _remove_workspace(self, job_id: str) -> None:
        try:
            workspace = self.settings.job_workspace(job_id).resolve(strict=False)
        except ValueError:
            return
        root = self.settings.download_dir.resolve(strict=False)
        if workspace.parent == root and workspace.name == job_id and workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)

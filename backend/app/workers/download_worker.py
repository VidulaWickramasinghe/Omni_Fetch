"""Read one task from stdin and emit newline-delimited JSON events."""

from __future__ import annotations

import json
import sys

import yt_dlp

from ..config import Settings
from ..models import FormatMode
from ..services.downloader import DownloadRejected, run_download_task, safe_download_error
from ..services.security import UnsafeURLError

_MAX_INPUT_BYTES = 512 * 1024


def _emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    raw = sys.stdin.buffer.readline(_MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > _MAX_INPUT_BYTES:
        _emit({"type": "failed", "error": "Worker received an invalid task"})
        return 2
    try:
        payload = json.loads(raw)
        settings = Settings.from_worker_policy(payload["policy"])
        run_download_task(
            job_id=str(payload["job_id"]),
            url=str(payload["url"]),
            mode=FormatMode(payload["mode"]),
            max_height=(int(payload["max_height"]) if payload.get("max_height") else None),
            use_auth=bool(payload.get("use_auth", False)),
            settings=settings,
            emit=_emit,
        )
        return 0
    except (DownloadRejected, UnsafeURLError, ValueError) as exc:
        _emit({"type": "rejected", "error": str(exc)[:500]})
        return 3
    except yt_dlp.utils.DownloadError as exc:
        _emit({"type": "failed", "error": safe_download_error(exc, settings)})
        return 4
    except Exception:
        _emit({"type": "failed", "error": "The download failed unexpectedly"})
        return 5


if __name__ == "__main__":
    raise SystemExit(main())

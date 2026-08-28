# Architecture

OmniFetch Phase 1 is intentionally a single-API-instance, single-user
application. Downloads run in killable child worker processes, while policy,
job state, storage, and API serialization remain explicit seams that can be
replaced independently.

## Request and download flow

```text
┌─────────────────────────┐
│ Same-origin web client  │
└────────────┬────────────┘
             │ HTTP / JSON
             ▼
┌─────────────────────────┐
│ FastAPI routes          │
│ body + response models  │
└────────────┬────────────┘
             │
             ├──────────────► URL and extractor policy
             │                 scheme / host / DNS / port
             │                 public default / auth opt-in / no playlist-live-DRM
             │
             ├──────────────► metadata extractor (yt-dlp, no download)
             │                 normalized safe quality choices
             │                 optional private cookie copy
             │
             ▼
┌─────────────────────────┐
│ Atomic job admission    │
│ in-memory snapshots     │
└────────────┬────────────┘
             │ bounded child-process execution
             ▼
┌─────────────────────────┐       ┌─────────────────────────┐
│ Downloader service      │──────►│ FFmpeg                  │
│ yt-dlp + policy hooks   │       │ mux / explicit MP3 only │
└────────────┬────────────┘       └─────────────────────────┘
             │
             ▼
┌─────────────────────────┐
│ Isolated per-job dir    │
│ final file + temp data  │
└─────────────────────────┘
```

The static frontend is copied into the API image and served by FastAPI, so the
default deployment has one origin and requires no cross-origin credentials.

## Boundaries

### API boundary

Pydantic request models accept a small policy vocabulary. In particular, a
caller can choose a mode and optional maximum height but cannot submit raw
yt-dlp selectors or options. A caller may request the configured authenticated
session with one boolean, but cannot submit cookie contents, secret paths,
headers, tokens, or proxy configuration. Public response models omit the source
URL, local path, raw exception, and extractor-internal data.

### Extraction boundary

yt-dlp decides whether a site has an extractor and returns source metadata.
OmniFetch normalizes that data into its own API schema. Platform labels are for
display; they are not a static promise that every URL on a domain works.

The downloader performs its own policy preflight, then passes that already
processed extraction result back to yt-dlp for transfer. This avoids an
unnecessary second social-page request. A fresh extraction is bounded to one
retry and occurs only after a recognized transient media-transfer failure.

Public mode can disable the Generic extractor because accepting arbitrary web
pages materially broadens SSRF and bandwidth-relay risk. Trusted local mode
retains yt-dlp's broader compatibility; neither mode replaces egress isolation.

Authenticated extraction is a distinct, server-owned capability. At startup,
the configured source must be a bounded, regular Netscape cookie file with at
least one usable record. Each inspection gets a private `0600` copy under the
download root because yt-dlp can update cookie jars. The copy is removed in a
`finally` path, and the response exposes only whether authenticated mode was
used.

### Job boundary

Admission and insertion are one atomic operation, so concurrent API calls
cannot each pass a stale capacity check. Readers receive snapshots rather than
shared mutable job objects. A cancelled/deleted job becomes tombstoned for the
runner so a late worker update cannot recreate it.

Phase 1 state is local to the API process. An API restart loses job records and
terminates its managed children; filesystem orphan cleanup is therefore
independent of the in-memory store.

The job record contains only an authenticated-mode boolean. Worker stdin also
contains the server-owned source path from configuration, never cookie content.
For a future Redis queue, that path becomes an opaque secret-vault reference;
raw sessions must not be serialized into Redis.

### Storage boundary

Each job owns one directory below a configured root. Serving, deletion, and
cleanup resolve paths and verify containment below that root. Failure and
cancellation remove partial data. Periodic and startup sweeps remove old
orphaned directories by age.

Authenticated workers create their private cookie copy inside the job
workspace, use it for both preflight and transfer, then remove it before the
completed output is recorded. Unlinking is lifecycle hygiene, not a guarantee
of physical erasure from host snapshots, backups, or SSD history.

### Process and network boundary

yt-dlp and FFmpeg process hostile remote input. Application limits bound known
duration, aggregate bytes, job time, and concurrency, while Compose adds
container CPU, memory, PID, filesystem, and privilege restrictions.

URL validation is not egress isolation. The submitted page can redirect or
yield new manifests/media URLs, and FFmpeg may make network requests. A public
deployment requires an external network policy that denies all non-global and
metadata destinations for the worker.

## Quality model

"Original" means the best source streams selected by policy. It does not imply
that video and audio came in one file or that the resulting container has a
specific extension. Compatible streams can be muxed without decoding them.

`audio_mp3` is different: converting AAC, Opus, or another source codec to MP3
is lossy and therefore explicit. Normal video modes do not upscale or re-encode
media merely to change resolution or container.

## Phase-2 evolution

The API contract does not need to change when durability or scale is required:

```text
FastAPI ──► Redis-backed admission/queue ──► isolated worker fleet
   │                                             │
   └── job snapshots / ownership                └── yt-dlp + FFmpeg
                                                       │
                                                       ▼
                                                S3 / MinIO
```

- Replace the in-memory job store and runner with Redis plus a bounded worker
  queue.
- Keep media processing out of the API container and enforce worker egress.
- Replace filesystem results with object keys and short-lived signed URLs.
- Store multi-user platform credentials in a dedicated encrypted vault and put
  only scoped, expiring references on Redis jobs.
- Add authentication, ownership, quotas, and PostgreSQL only when accounts or
  durable history need relational data.
- Add metrics around admission, rejection reason, bytes, duration, cleanup, and
  worker failures before adding autoscaling.

This order preserves the small local developer experience while creating the
security boundaries needed by a multi-user service.

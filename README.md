# OmniFetch

Universal media downloader and extractor.

Paste a public or authorized media URL, inspect the streams exposed by the
source, choose a quality policy, and download without re-encoding unless you
explicitly request MP3 audio.

> [!IMPORTANT]
> OmniFetch 0.4.0 is a local, single-user MVP. Docker Compose binds it to
> `127.0.0.1` deliberately. It is not a hardened public download service, an
> access-control bypass, credential-sharing service, or DRM circumvention tool.

## What it does

- Uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) as the extraction engine
  instead of maintaining one downloader per social network.
- Shows normalized video qualities and audio choices before download.
- Selects the highest stream actually offered by the source. It does not
  upscale or reconstruct missing quality.
- Muxes compatible video and audio streams with FFmpeg. MP3 is the only
  intentionally lossy conversion and is opt-in.
- Runs bounded in-memory jobs with status polling, cancellation, expiry, and
  local cleanup.
- Accepts finite media whose extractor omits optional duration metadata, while
  retaining file-size, wall-clock, live-stream, and worker limits.
- Re-extracts once after recognized transient media-link failures, which helps
  with short-lived or intermittently unavailable signed CDN URLs.
- Optionally uses one operator-mounted Netscape cookie file for media the
  configured account is already authorized to access. The API never accepts
  cookie contents, passwords, bearer tokens, or proxy settings.
- Rejects unauthenticated private media, live streams, playlists, DRM-only
  media, oversized inputs, and other policy-incompatible sources where the
  extractor provides enough information.

Site support changes as platforms and yt-dlp evolve. A URL can stop working
without an OmniFetch code change.

## Quick start with Docker

Requirements: Docker Engine with Compose v2.

```bash
cp .env.example .env
docker compose up --build
```

Open <http://127.0.0.1:8000>. The API and frontend use the same origin. Runtime
files live in the `omnifetch-data` named volume and expire according to the job
TTL.

Stop the app with:

```bash
docker compose down
```

`docker compose down -v` also removes the named data volume, so use it only
when you intentionally want to erase retained downloads.

The image includes FFmpeg, Deno, yt-dlp's external challenge scripts, and its
browser-impersonation transport for current YouTube and TikTok extraction. It
runs as UID/GID `10001`, uses a read-only
root filesystem, drops Linux capabilities, enables `no-new-privileges`, and
writes only to `/data` and a small `/tmp` tmpfs. Compose publishes the port on
loopback, not all interfaces.

## Run from source

Requirements: Python 3.12–3.14 and either Node.js 22+ or Deno 2.3+ on
`PATH`. `make install-dev` installs yt-dlp's challenge scripts and a bundled
FFmpeg binary for supported platforms. A system FFmpeg installation is used
when available.

```bash
make install-dev
make run
```

Then open <http://127.0.0.1:8000>. Useful commands:

```bash
make test
make test-cov
make lint
make format-check
make audit
```

## Optional authenticated media

Authenticated mode is designed for one trusted, self-hosted operator. Export a
narrowly scoped Mozilla/Netscape cookie file, keep it outside Git, and point the
Compose bind mount at it:

```bash
cp .env.example .env
# Edit .env:
# OMNIFETCH_COOKIE_FILE_HOST=/absolute/path/to/cookies.txt
# OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA=true
docker compose up --build
```

The source file is mounted read-only. For each inspection or job, OmniFetch
creates a private `0600` working copy because yt-dlp may update its cookie jar,
then removes that copy on success or failure. See [the secrets guide](secrets/README.md)
and [deployment documentation](docs/deployment.md#authenticated-media).

Use a dedicated, least-privileged account and only retrieve media that account
is entitled to access. Session cookies are credentials; revoking the platform
session is the reliable response to suspected leakage. Authenticated mode does
not enable DRM downloads, feeds/playlists, paywall bypass, proxy rotation, or
anti-bot evasion.

## API at a glance

Media and job endpoints are under `/api/v1`; health endpoints are at the root.

| Method and path | Purpose |
|---|---|
| `POST /api/v1/extract` | Validate a media URL and return metadata plus safe quality choices. |
| `POST /api/v1/download` | Admit a bounded background job using `mode` and optional `max_height`. |
| `GET /api/v1/auth/status` | Report whether the operator-mounted login session is available, without secret details. |
| `GET /api/v1/jobs/{job_id}` | Return a sanitized job snapshot and progress. |
| `GET /api/v1/jobs/{job_id}/file` | Download a completed job's result. |
| `DELETE /api/v1/jobs/{job_id}` | Request cancellation for an active job or delete a terminal job. |
| `GET /api/v1/platforms` | Return cosmetic platform labels; this is not a guaranteed support list. |
| `GET /health` | Root liveness endpoint used by Compose. |
| `GET /ready` | Root readiness check for the manager, storage, and FFmpeg. |

Example:

```bash
curl --request POST http://127.0.0.1:8000/api/v1/extract \
  --header 'Content-Type: application/json' \
  --data '{"url":"https://example.com/authorized-video","use_auth":true}'

curl --request POST http://127.0.0.1:8000/api/v1/download \
  --header 'Content-Type: application/json' \
  --data '{"url":"https://example.com/authorized-video","mode":"original","max_height":1080,"use_auth":true}'
```

Download modes are:

- `original`: best available video plus audio, preferring a lossless mux.
- `mp4`: prefer an MP4-compatible selection where the source offers one.
- `audio`: retain the best source audio codec.
- `audio_mp3`: explicitly transcode the best audio stream to MP3.

Clients cannot submit yt-dlp format-selector expressions. The API constructs
selectors from a small policy model so one request cannot fan out into an
unbounded set of formats. See [the API reference](docs/api.md) for schemas and
status semantics.

## Architecture

```text
Browser
  │  same-origin HTTP
  ▼
FastAPI ── URL/policy/auth validation ── metadata extraction
  │
  ├── in-memory bounded job store
  ├── mounted cookie source ── private per-operation copy
  │
  └── managed job runner ── yt-dlp ── FFmpeg
                                  │
                                  ▼
                         per-job local storage
```

This deliberately remains a small Phase-1 design: one API instance with
killable child worker processes. The API returns sanitized snapshots rather
than internal URLs or filesystem paths; each job has isolated storage and a
cancellation signal; cleanup handles terminal and orphaned data.
See [Architecture](docs/architecture.md) for boundaries and the Phase-2 path.

## Security model and limits

OmniFetch performs application-level URL checks before extraction: it allows
HTTP(S), validates ports and hostnames, resolves every address, and rejects
non-global destinations. It also constrains request size, duration, total
downloaded bytes, job count, and elapsed time.

Those checks are useful defense in depth, but **they are not network egress
isolation**. Redirects, DNS rebinding, extractor-discovered manifests and media
URLs, and FFmpeg network access create additional request paths. The bundled
Compose file still has ordinary outbound network access because yt-dlp needs
the internet.

Therefore:

- keep the bundled instance on loopback and a trusted machine;
- do not expose it by changing the binding to `0.0.0.0`;
- do not rely on CORS as authentication or SSRF protection;
- use a dedicated worker network/VM with enforced non-global egress denial,
  authentication, rate limits, TLS, and durable queueing before considering a
  public deployment.

Details and reporting instructions are in [SECURITY.md](SECURITY.md) and
[Local deployment](docs/deployment.md).

## Content and legal policy

OmniFetch is for public or authenticated media that you have the right or
permission to save. Users remain responsible for copyright, licenses, privacy,
and each source's terms.

The project does not support bypassing DRM, paywalls, geographic restrictions,
or other access controls. Authenticated mode only presents an existing,
operator-configured session to a source that has already authorized it. The API
does not accept social-network passwords, cookies, bearer tokens, or arbitrary
headers. "Highest quality" means the highest stream the source makes available
to the extractor, not an enhanced version.

This is a technical project description, not legal advice. Operating a public
third-party download service has materially different legal and abuse risks
from personal self-hosting.

## Configuration

Copy [`.env.example`](.env.example) for documented defaults. Important groups
are:

- storage and expiry: `OMNIFETCH_DOWNLOAD_DIR`, `OMNIFETCH_JOB_TTL_HOURS`;
- job bounds: `OMNIFETCH_MAX_CONCURRENT_JOBS`,
  `OMNIFETCH_MAX_QUEUED_JOBS`, `OMNIFETCH_JOB_TIMEOUT_SECONDS`;
- media bounds: `OMNIFETCH_MAX_FILESIZE_MB`,
  `OMNIFETCH_MAX_DURATION_MIN`;
- input/network policy: `OMNIFETCH_ALLOWED_PORTS`,
  `OMNIFETCH_ALLOW_GENERIC_EXTRACTOR`, `OMNIFETCH_MAX_BODY_BYTES`;
- authenticated mode: `OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA`,
  `OMNIFETCH_COOKIE_FILE` (direct runs), `OMNIFETCH_COOKIE_FILE_HOST`
  (Compose), and `OMNIFETCH_MAX_COOKIE_FILE_BYTES`;
- browser policy: `OMNIFETCH_ALLOWED_ORIGINS`.

Invalid or unsafe values fail at startup rather than silently disabling a
limit. The full table is in [Local deployment](docs/deployment.md).

## Phase 2

Move beyond the single-host MVP only when the operating requirements call
for it:

1. Replace the in-memory store/runner with Redis plus RQ, Celery, or a focused
   worker service so jobs survive API restarts.
2. Run workers behind enforced egress policy and independent CPU, memory, PID,
   disk, and wall-time limits.
3. Replace local job directories with S3/MinIO and short-lived signed downloads.
4. Add authenticated OmniFetch users, job ownership, per-user quotas, a secret
   vault that passes opaque credential references rather than cookie contents,
   audit logging, and a reverse proxy with TLS and rate limiting.
5. Add PostgreSQL only when durable users, billing, or download history require
   relational state.

Redis, PostgreSQL, S3, Kubernetes, and a monitoring stack are intentionally not
required for local Phase 1.

## Repository layout

```text
backend/app/       FastAPI API, extraction, download, policy, and job services
backend/tests/     Unit and API tests with network/downloader mocks
frontend/          Same-origin static web application
docs/              API, architecture, deployment, and testing notes
backend/Dockerfile Non-root API image including the frontend and FFmpeg
docker-compose.yml Local-only hardened runtime profile
```

Contributions are welcome; start with [CONTRIBUTING.md](CONTRIBUTING.md).
# Omni_Fetch

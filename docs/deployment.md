# Local deployment and configuration

The supported Phase-1 deployment is one trusted user on one machine. Compose
binds the combined API/frontend to `127.0.0.1`; it is not a public-production
profile.

## Compose profile

```bash
cp .env.example .env
docker compose up --build
```

Browse to <http://127.0.0.1:8000>. Inspect state with:

```bash
docker compose ps
docker compose logs api
```

The container runs as UID/GID `10001` with:

- a read-only root filesystem;
- all Linux capabilities dropped and `no-new-privileges` enabled;
- a small writable `/tmp` tmpfs;
- one writable named volume at `/data`;
- CPU, memory, and PID ceilings;
- an init process, graceful stop window, and healthcheck;
- loopback-only host publishing.

These controls reduce impact from malformed media and accidental overload. The
container still has normal outbound internet access and is not a complete SSRF
sandbox.

## Environment variables

Values shown are the Compose defaults. Application defaults are validated at
startup; zero, negative, malformed, or unsafe values fail closed.

| Variable | Default | Meaning |
|---|---:|---|
| `OMNIFETCH_API_PORT` | `8000` | Loopback host port used by Compose. |
| `OMNIFETCH_DOWNLOAD_DIR` | `/data/downloads` | Controlled job storage root in the container. |
| `OMNIFETCH_FRONTEND_DIR` | `/app/frontend` | Static frontend directory served at `/`. |
| `OMNIFETCH_MAX_FILESIZE_MB` | `1024` | Aggregate per-job media-byte ceiling. |
| `OMNIFETCH_MAX_DURATION_MIN` | `120` | Maximum accepted known media duration. |
| `OMNIFETCH_MAX_CONCURRENT_JOBS` | `2` | Jobs allowed to execute concurrently. |
| `OMNIFETCH_MAX_QUEUED_JOBS` | `8` | Total active/queued admission ceiling. |
| `OMNIFETCH_JOB_TIMEOUT_SECONDS` | `1800` | Per-job wall-time deadline. |
| `OMNIFETCH_JOB_TTL_HOURS` | `6` | Terminal record/result retention. |
| `OMNIFETCH_CLEANUP_INTERVAL_SECONDS` | `600` | Periodic cleanup cadence. |
| `OMNIFETCH_MAX_BODY_BYTES` | `8192` | Maximum JSON request body accepted by the app. |
| `OMNIFETCH_MAX_URL_LENGTH` | `4096` | URL character limit before parsing or DNS. |
| `OMNIFETCH_SOCKET_TIMEOUT_SECONDS` | `15` | Timeout for individual yt-dlp socket operations. |
| `OMNIFETCH_TERMINATE_GRACE_SECONDS` | `3` | Grace between worker termination and forced kill. |
| `OMNIFETCH_MAX_WORKER_EVENT_BYTES` | `262144` | Maximum one-line child event accepted by the supervisor. |
| `OMNIFETCH_MAX_COOKIE_FILE_BYTES` | `1048576` | Maximum accepted Netscape cookie-file size. |
| `OMNIFETCH_PUBLIC_MODE` | `false` | Enable stricter known-platform policy; not a production-readiness flag. |
| `OMNIFETCH_ALLOW_GENERIC_EXTRACTOR` | `false` | In public mode, permit yt-dlp's broad Generic extractor. |
| `OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA` | `false` | Allow requests to opt into the operator-mounted session. |
| `OMNIFETCH_COOKIE_FILE_HOST` | `./secrets/disabled.cookies.txt` | Host path bind-mounted read-only by Compose. |
| `OMNIFETCH_COOKIE_FILE` | `/run/secrets/omnifetch-cookies.txt` | In-container cookie source; set directly for non-Compose runs. |
| `OMNIFETCH_ALLOWED_PORTS` | `80,443` | Explicit URL destination ports. |
| `OMNIFETCH_ALLOWED_ORIGINS` | loopback origin list | Origins allowed by CORS for source development. |
| `OMNIFETCH_FFMPEG_LOCATION` | empty | Optional FFmpeg executable/directory; empty uses `PATH`. |

With `OMNIFETCH_PUBLIC_MODE=true` and Generic extraction disabled, unknown
platform URLs fail before extraction. Public mode does not add OmniFetch user
authentication, TLS, rate limiting, or network egress isolation. It must never
be interpreted as permission to publish the bundled container directly.

## Authenticated media

This feature is for one trusted, self-hosted operator. It lets yt-dlp present an
already-authorized platform session for an individual URL. It is not an
OmniFetch login system and does not add job ownership or make the API safe for
other users.

1. Export a Mozilla/Netscape cookie file from a trusted desktop session. The
   first line must be `# Netscape HTTP Cookie File` or `# HTTP Cookie File`, and
   at least one valid seven-column cookie record must be present.
2. Prefer a dedicated, least-privileged platform account and a narrowly scoped
   browser profile. Treat the export like a password.
3. Store the file outside Git and restrict its host permissions.
4. In `.env`, set:

   ```dotenv
   OMNIFETCH_COOKIE_FILE_HOST=/absolute/path/to/cookies.txt
   OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA=true
   ```

5. Restart with `docker compose up --build`, then verify:

   ```bash
   curl http://127.0.0.1:8000/api/v1/auth/status
   ```

   A usable configuration returns `enabled: true`, `available: true`, and
   `method: mounted_cookie_file`. The response never includes a path, domain,
   cookie name, or cookie value.

For a direct source run, set `OMNIFETCH_COOKIE_FILE` to the source file path as
well as `OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA=true` in the process environment.
The application does not automatically load `.env` outside Compose.

The source is bind-mounted read-only. yt-dlp documents its cookie option as a
file it both reads and updates, so OmniFetch makes a private writable copy for
each inspection or job. That copy and its private directory are removed in a
`finally` path on success or failure. Cookie contents never enter request
bodies, job records, API responses, logs, or child events. The child receives
only the server-owned source path and a boolean indicating whether the job may
use it.

Revoking the source platform session is the reliable cleanup after suspected
leakage; unlinking files cannot erase backups, snapshots, or SSD history.
OmniFetch deliberately does not implement API cookie uploads, browser-profile
reading, arbitrary Authorization headers, residential proxies, account
rotation, feeds/playlists, or anti-bot evasion. DRM-only media remains rejected.

## Data lifecycle

The `omnifetch-data` named volume survives ordinary container replacement.
Jobs use isolated subdirectories, and cleanup removes expired terminal results
and orphaned directories. Active downloads and job records are not durable
across a process restart in Phase 1.

To inspect the volume without changing it:

```bash
docker volume inspect omnifetch_omnifetch-data
```

Do not back up the volume unless you have a specific retention need: it can
contain copyrighted media and source-derived metadata. Treat it as sensitive.

## Health and operations

`GET /health` is used by the image and Compose healthchecks. A healthy process
does not guarantee that a particular platform currently works. Platform
extractors, source authentication requirements, geo-policy, and anti-bot
changes remain external dependencies.

Watch for:

- repeated policy rejections or extraction errors;
- low disk space and cleanup failures;
- jobs reaching their byte or time deadline;
- FFmpeg/yt-dlp crashes or dependency advisories;
- a sustained full queue.

Logs must not include source query strings, signed URLs, local result paths, or
raw extractor exceptions.

## Public deployment is a separate project

Do not expose the Compose port by changing it to `0.0.0.0`. CORS and URL
validation do not make an anonymous media downloader safe for the internet.

Before a public deployment, design and verify all of the following:

- authenticated users and ownership checks on status, delete, and file routes;
- per-user and per-address rate, concurrency, byte, and storage quotas;
- a TLS reverse proxy with request-size and connection limits;
- API/worker separation and a durable bounded queue;
- worker network enforcement that denies loopback, RFC1918, link-local,
  carrier-NAT, special-use, cloud-metadata, and the rest of the private network;
- process/container wall-time, CPU, memory, PID, and disk enforcement;
- object storage lifecycle and short-lived download authorization;
- redacted centralized logs, alerting, and incident response;
- legal review for jurisdictions, sources, takedowns, privacy, and platform
  terms.

The practical Phase-2 stack is Redis plus a focused worker, S3/MinIO, and an
existing reverse proxy. PostgreSQL is needed only when accounts, billing, or
durable history are introduced.

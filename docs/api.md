# API reference

Media and job endpoints are versioned below `/api/v1`. Health endpoints remain
at the root. The generated OpenAPI document at `/openapi.json` is authoritative
for machine-readable schemas.

The Phase-1 API has no OmniFetch user authentication and is intended only for
the loopback deployment. Its optional mounted platform session is not API
authentication. Job identifiers are not a replacement for authorization in a
multi-user service.

## Errors

Errors use FastAPI's JSON envelope:

```json
{"detail":"Safe explanation for the caller"}
```

Validation failures return `422`. URL policy failures return `400`. The API
does not return raw yt-dlp errors, tracebacks, source URLs, signed query strings,
worker PIDs, or server filesystem paths.

Requests with a body larger than `OMNIFETCH_MAX_BODY_BYTES` return `413`.
Unknown JSON fields are rejected.

## Inspect a URL

`POST /api/v1/extract`

```json
{"url":"https://example.com/authorized-video","use_auth":true}
```

Representative `200` response:

```json
{
  "platform": "youtube",
  "title": "Example video",
  "duration": 120.5,
  "thumbnail": "https://cdn.example/thumbnail.jpg",
  "uploader": "Example channel",
  "is_live": false,
  "authenticated": true,
  "qualities": [
    {
      "id": "height:1080",
      "label": "1080p",
      "height": 1080,
      "fps": 30.0,
      "note": "1080p",
      "estimated_size": 52428800
    }
  ],
  "supports_video": true,
  "supports_audio": true
}
```

Quality IDs are display identifiers, not raw yt-dlp format selectors. To bound
a download, send the listed `height` as `max_height`.

Additional responses:

- `400`: unsafe URL scheme, syntax, credentials, port, DNS result, or source
  policy;
- `422`: playlist/multi-item URL, disabled Generic extractor, invalid request,
  or source that could not be inspected;
- `429`: all metadata-inspection slots are busy, with `Retry-After`.

Extraction is metadata-only, but it still performs outbound requests.
`use_auth` defaults to `false`. When `true`, the server uses only its configured
cookie session; clients cannot supply a cookie, path, header, or token. A `409`
means authenticated mode is disabled or its mounted cookie source is no longer
usable.

## Start a download

`POST /api/v1/download`

```json
{
  "url": "https://example.com/authorized-video",
  "mode": "original",
  "max_height": 1080,
  "use_auth": true
}
```

`mode` is `original`, `mp4`, `audio`, or `audio_mp3`. `max_height` is optional
and must be between 144 and 8640. Audio modes ignore height.
`use_auth` is optional and defaults to `false`. The worker receives only this
boolean and a server-owned configured path, never cookie content from the
request.

Successful admission returns `202`, a `Location` header pointing at the job,
and:

```json
{"job_id":"4fc2f57f85154c4e999f906aa8a24872","status":"queued"}
```

`429` means the bounded active-plus-queued capacity is full and includes a
`Retry-After` header. Admission and insertion are atomic.

## Read job state

`GET /api/v1/jobs/{job_id}`

Representative completed response:

```json
{
  "job_id": "4fc2f57f85154c4e999f906aa8a24872",
  "status": "completed",
  "phase": "completed",
  "progress": 100.0,
  "mode": "original",
  "max_height": 1080,
  "authenticated": true,
  "title": "Example video",
  "platform": "youtube",
  "output_name": "Example video.mkv",
  "output_size": 52428800,
  "expires_at": 1787700000.0,
  "download_url": "/api/v1/jobs/4fc2f57f85154c4e999f906aa8a24872/file",
  "error": null,
  "created_at": 1787678000.0,
  "updated_at": 1787678400.0,
  "completed_at": 1787678400.0
}
```

Statuses are:

- active: `queued`, `inspecting`, `downloading`, `processing`, `cancelling`;
- terminal: `completed`, `rejected`, `failed`, `cancelled`.

`download_url` and `expires_at` are populated only when meaningful. A missing
job returns `404`.

## Download the result

`GET /api/v1/jobs/{job_id}/file`

Returns the completed file with `Content-Disposition` and
`X-Content-Type-Options: nosniff`.

- `404`: unknown/expired job record;
- `409`: known job is not completed;
- `410`: result is missing, a symlink, or outside the job workspace.

The server never accepts a caller-supplied path.

## Cancel or delete a job

`DELETE /api/v1/jobs/{job_id}`

For an active job, the API requests child-process termination and returns `202`
with the sanitized job response in `cancelling` state plus a `Location` header.
Poll that location until `cancelled`.

For a terminal job, the API removes the record and its isolated workspace and
returns `200`:

```json
{"deleted":true,"job_id":"4fc2f57f85154c4e999f906aa8a24872"}
```

Deletion is idempotent only from the client's perspective: a subsequent call
returns `404` because the record no longer exists.

## Authenticated-session status

`GET /api/v1/auth/status`

Disabled response:

```json
{"enabled":false,"available":false,"method":null}
```

When a valid operator-mounted session is enabled, `enabled` and `available` are
`true`, and `method` is `mounted_cookie_file`. The response deliberately omits
the cookie path, domains, cookie names, expiry values, and all credential
content. This endpoint does not test whether a particular platform still
accepts the session.

## Platform labels

`GET /api/v1/platforms`

Returns a sorted list of UI labels and a note. It is not a live yt-dlp support
matrix and must not be used as an authorization allowlist.

## Health endpoints

- `GET /health` is liveness and returns `{"status":"ok"}` while the API can
  answer requests.
- `GET /ready` checks the manager, writable download root, FFmpeg, a supported
  JavaScript runtime, yt-dlp's challenge scripts, and browser impersonation
  support for TLS-sensitive sources;
  it returns `{"status":"ready"}` or `503`.

Compose uses `/health` to avoid coupling container restarts to temporary
readiness conditions. Operators can use `/ready` before routing workload.

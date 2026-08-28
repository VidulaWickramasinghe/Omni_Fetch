# Testing

The test suite is deterministic and does not contact social networks or other
third-party hosts. DNS, yt-dlp, clocks, and filesystem roots are replaced at
their service boundaries.

## Run the suite

```bash
make install-dev
make test
make lint
make format-check
```

Generate branch coverage with:

```bash
make test-cov
```

Tests marked `integration` may invoke FFmpeg or another local process. Exclude
them when that dependency is unavailable:

```bash
backend/.venv/bin/python -m pytest -m 'not integration'
```

## Test layers

### URL and input policy

The security matrix covers public and special-use IPv4/IPv6 destinations,
mixed DNS answers, schemes, credentials, control characters, explicit ports,
body limits, and hostname-resolution failure. Redirect/manifest safety cannot
be proven by initial URL validation and belongs in deployment egress tests.

### Job state

Job-store tests exercise atomic admission under concurrent callers, deep
snapshots, cancellation, terminal deletion, expiration, and the rule that a
late worker update cannot resurrect a cancelled/deleted job.

### Download policy

Fake yt-dlp sessions verify that API inputs create only server-owned format
selectors, playlists/live/DRM/unauthenticated-private media fail closed, byte
and time ceilings cancel work, finite media without duration metadata remains
downloadable, codec-light direct MP4 streams remain selectable, recognized
transient source checks receive a bounded browser-impersonated retry, final
paths remain contained, and partial data is cleaned.

Authentication tests use synthetic Netscape records only. They cover disabled,
invalid, oversized, symlinked, missing, and valid sources; `0600` operation
copies; cleanup on success and policy failure; authenticated private metadata;
DRM rejection with authentication; safe status responses; and readiness after
the mounted source disappears. No real session value enters the suite.

### API contract

FastAPI tests use `create_app` with a temporary download root and mocked
services. They assert response status/shape, `Location`, queue saturation,
cancellation, file containment, request-size rejection, CORS, and that public
models never expose source URLs, query strings, local paths, or raw exceptions.

### Container smoke tests

Where Docker is available, verify:

1. `docker compose config` succeeds;
2. the image builds and starts healthy;
3. `/` serves the frontend and `/health` succeeds;
4. the process UID is `10001`;
5. the root filesystem is read-only while `/data` and `/tmp` are writable;
6. the published socket listens only on `127.0.0.1`;
7. a graceful stop leaves no child FFmpeg/yt-dlp processes.

## Fixtures and responsible testing

Use tiny self-generated media or public-domain fixtures with recorded license
information. Never commit downloaded runtime output, credentials, platform
cookies, signed URLs, or private media. A real-site compatibility smoke test,
when intentionally run by a maintainer, must use content they control and is
not part of CI.

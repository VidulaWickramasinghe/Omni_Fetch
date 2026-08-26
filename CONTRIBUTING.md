# Contributing to OmniFetch

Thank you for helping improve OmniFetch. Keep changes focused on public media
or content an operator-owned session is already authorized to access. Features
intended to bypass DRM, paywalls, platform authorization decisions, or other
access controls are out of scope.

## Development setup

Install Python 3.12–3.14 and FFmpeg, then run:

```bash
make install-dev
make test
make lint
make format-check
```

Start the local application with `make run`, or build the hardened local image
with `docker compose up --build`.

## Design expectations

- Keep yt-dlp as the generic extraction boundary. Do not add one downloader
  class per social network unless a small, documented policy adapter is truly
  needed outside the extraction engine.
- Preserve source streams and mux without re-encoding when possible. Any lossy
  conversion must be explicit in the API and UI.
- Treat every URL, extractor field, filename, and media stream as hostile.
- Do not pass user input to a shell or accept raw yt-dlp selectors/options.
- Never accept cookie contents, passwords, bearer tokens, arbitrary headers, or
  proxy credentials through the API. Multi-user designs must pass opaque vault
  references through queues rather than credentials.
- Public API responses must not expose source URLs, query strings, local paths,
  tracebacks, or raw extractor errors.
- Resource policy must fail closed. Unknown live/duration/size cases require a
  deliberate, tested decision.
- Keep Phase 1 small. Redis, S3, PostgreSQL, and orchestration belong in a
  change with a concrete durability or scaling requirement.

## Tests

Tests live under `backend/tests` and must not make requests to third-party
sites. Mock DNS and yt-dlp at the service boundary. Local HTTP servers, tiny
generated files, and FFmpeg fixtures are allowed under the `integration`
marker.

New behavior should cover its success path, policy rejection, cleanup, and
sanitized API error. Security-sensitive changes should include regression
tests for both IPv4 and IPv6 where applicable.

Run:

```bash
make test-cov
make lint
make format-check
```

## Style

Ruff owns import sorting, linting, and formatting. Python targets 3.12. Prefer
small typed services and dependency injection over module-level patching; this
keeps network and filesystem tests deterministic.

## Dependency updates

yt-dlp needs frequent compatibility updates, but runtime ranges should move
deliberately. For a dependency change:

1. update the constrained range;
2. run `make audit`, tests, lint, and format checks;
3. build both architecture variants you support, if available;
4. exercise extraction and download using media you own or a local fixture;
5. summarize relevant compatibility or security changes in the pull request.

## Pull requests

Explain the user-visible outcome, the threat/resource implications, and how
you verified it. Keep unrelated refactors separate. Never attach copyrighted
media, private URLs, credentials, cookies, or downloaded runtime artifacts.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

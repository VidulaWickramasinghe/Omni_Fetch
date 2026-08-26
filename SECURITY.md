# Security policy

## Supported versions

OmniFetch is currently pre-1.0. Security fixes are applied to the latest commit
and latest tagged 0.3.x release only.

| Version | Security support |
|---|---|
| Latest 0.3.x | Yes |
| Older snapshots | No |

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use the
repository's **Security → Report a vulnerability** workflow to submit a private
GitHub Security Advisory. If private reporting is unavailable, contact the
maintainer privately and include only enough information to establish a safe
reporting channel.

Include:

- affected version or commit;
- deployment model and relevant configuration;
- reproducible steps or a minimal proof of concept;
- security impact and which boundary was crossed;
- suggested mitigation, if known.

Do not test against systems, accounts, media, or networks you do not own or have
explicit permission to assess. Do not include real credentials, signed media
URLs, private content, or personal data in a report.

## Security posture

The bundled deployment is a local, single-user MVP bound to `127.0.0.1`. It is
not approved as an internet-facing service. Public media is the default. An
operator can explicitly enable one mounted cookie session for content that
account is already authorized to access. Live streams, playlists, and DRM-only
media remain rejected.

Primary risks include:

- server-side request forgery through the submitted URL, redirects, DNS
  rebinding, extractor-discovered resources, manifests, or FFmpeg;
- bandwidth, disk, CPU, memory, process, and queue exhaustion;
- malicious or malformed media reaching yt-dlp or FFmpeg;
- leakage of signed source URLs, filesystem paths, or extractor diagnostics;
- unauthorized retrieval of completed files;
- legal and platform-policy abuse.

Application-level checks reject non-global addresses and unsafe URL forms and
apply media/job bounds. They are not a substitute for network-level egress
controls. Docker Compose cannot guarantee that every request made by yt-dlp or
FFmpeg avoids internal networks while retaining general internet access.

Any public deployment requires, at minimum:

1. a separately isolated worker with an egress firewall or proxy that cannot
   route to non-global/internal networks or cloud metadata;
2. authentication and job ownership checks;
3. per-user and per-address admission/rate limits;
4. hard process-level CPU, memory, PID, disk, byte, and wall-time limits;
5. TLS and a maintained reverse proxy;
6. a durable bounded queue and storage lifecycle;
7. centralized, redacted operational logging and dependency updates;
8. an independent legal and abuse review.

Changing the Compose port binding from loopback to `0.0.0.0` does not satisfy
those requirements.

## Secrets and private content

Phase 1 never accepts platform cookies, passwords, bearer tokens, arbitrary
headers, or proxy credentials through its HTTP API. Authenticated media uses a
single operator-mounted Netscape cookie file. The source is read-only; a `0600`
copy is created for one inspection/job and unlinked immediately afterward.
Deletion cannot guarantee physical erasure from SSD snapshots or host backups,
so the source file, `/data`, backups, crash dumps, and host access remain
sensitive boundaries.

Do not add credentials to URLs, `.env`, Git, test fixtures, screenshots, logs,
issues, or queue payloads. Source URLs and signed query strings can themselves
be secrets and must be redacted. Use a dedicated, least-privileged platform
account and revoke its session if compromise is suspected.

The feature authorizes requests only as the configured account. It does not
bypass DRM, paywalls, geographic restrictions, revoked sessions, or platform
authorization decisions. It does not harvest feeds or playlists, rotate
accounts/proxies, or implement anti-bot evasion.

## Dependencies

yt-dlp and FFmpeg process hostile remote input and need regular security and
compatibility updates. Runtime Python dependencies are constrained to tested
version ranges. Maintainers should run `make audit`, the full test suite, and a
container smoke test for every dependency update.

# Local authentication secret

Do not place real cookie files in Git. Files in this directory are ignored
except for this guide and the intentionally empty disabled placeholder.

For self-hosted authenticated media:

1. Export a narrowly scoped Mozilla/Netscape `cookies.txt` file on a trusted
   machine. The first line must be `# Netscape HTTP Cookie File` or
   `# HTTP Cookie File`.
2. Store it outside the repository when practical and restrict its host file
   permissions to the OmniFetch operator.
3. Set `OMNIFETCH_COOKIE_FILE_HOST` in `.env` to that file's absolute or
   project-relative path.
4. Set `OMNIFETCH_ENABLE_AUTHENTICATED_MEDIA=true` and restart Compose.
5. Confirm `GET /api/v1/auth/status` reports `available: true`.

Treat the export like a password. Use a dedicated, least-privileged account,
download only media that account is authorized to access, and revoke the
session immediately if the file may have leaked. OmniFetch does not accept
cookie uploads, passwords, bearer tokens, or proxy credentials through its API.

"""First-party Reddit embed fallback for cloud hosts blocked from Reddit's JSON API."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import yt_dlp

_MAX_EMBED_BYTES = 2 * 1024 * 1024
_REDDIT_POST_PATH = re.compile(
    r"^/(?P<slug>(?:(?:r|user)/[^/]+/)?comments/(?P<id>[A-Za-z0-9]+)(?:/[^/?#]+)?)"
)
_REDDIT_HOSTS = frozenset({"reddit.com", "redditmedia.com"})
_MEDIA_HOSTS = frozenset({"v.redd.it", "packaged-media.redd.it"})


class _RedditEmbedParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.player: dict[str, str] | None = None
        self.title_parts: list[str] = []
        self.author_parts: list[str] = []
        self._in_title = False
        self._in_author = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "shreddit-player" and self.player is None:
            self.player = attributes
        elif tag == "h1" and not self.title_parts:
            self._in_title = True
        elif tag == "a":
            href = attributes.get("href", "")
            path = urlsplit(href).path
            if path.startswith("/user/") and not self.author_parts:
                self._in_author = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self._in_title = False
        elif tag == "a":
            self._in_author = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        elif self._in_author:
            self.author_parts.append(text)


def _reddit_embed_url(url: str) -> str | None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not any(hostname == host or hostname.endswith(f".{host}") for host in _REDDIT_HOSTS):
        return None
    match = _REDDIT_POST_PATH.match(parsed.path)
    if not match:
        return None
    safe_slug = "/".join(quote(part, safe="") for part in match.group("slug").split("/"))
    return f"https://embed.reddit.com/{safe_slug}/"


def _trusted_media_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.port not in {None, 443} or hostname not in _MEDIA_HOSTS:
        return None
    return url


def _download_embed_html(url: str, timeout: int) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "OmniFetch/0.5 (+public media inspector)",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        final_host = (urlsplit(response.geturl()).hostname or "").lower().rstrip(".")
        if final_host != "embed.reddit.com":
            raise ValueError("Reddit embed redirected to an unexpected host")
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > _MAX_EMBED_BYTES:
            raise ValueError("Reddit embed response is too large")
        payload = response.read(_MAX_EMBED_BYTES + 1)
    if len(payload) > _MAX_EMBED_BYTES:
        raise ValueError("Reddit embed response is too large")
    return payload.decode("utf-8", errors="replace")


def _packaged_formats(player: dict[str, str]) -> tuple[list[dict[str, Any]], float | None]:
    try:
        media = json.loads(player.get("packaged-media-json", "{}"))
    except (TypeError, ValueError):
        return [], None
    playback = media.get("playbackMp4s") if isinstance(media, dict) else None
    if not isinstance(playback, dict):
        return [], None

    formats: list[dict[str, Any]] = []
    for item in playback.get("permutations") or []:
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            continue
        source = item["source"]
        media_url = _trusted_media_url(source.get("url"))
        dimensions = source.get("dimensions") or {}
        if not media_url or not isinstance(dimensions, dict):
            continue
        height = dimensions.get("height")
        width = dimensions.get("width")
        formats.append(
            {
                "url": media_url,
                "format_id": f"reddit-embed-{height or len(formats) + 1}",
                "format_note": "Reddit public embed",
                "ext": "mp4",
                "protocol": "https",
                "height": height if isinstance(height, int) else None,
                "width": width if isinstance(width, int) else None,
                "vcodec": str(source.get("videoCodec") or "h264").lower(),
                "acodec": "aac",
            }
        )
    duration = playback.get("duration")
    return formats, float(duration) if isinstance(duration, (int, float)) else None


def _hls_formats(
    player: dict[str, str],
    options: dict[str, Any],
    ydl_factory: Callable[..., Any],
) -> list[dict[str, Any]]:
    playlist_url = _trusted_media_url(player.get("src"))
    if not playlist_url:
        return []
    try:
        hls_options = dict(options, skip_download=True, noplaylist=True)
        with ydl_factory(hls_options) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
    except (OSError, ValueError, yt_dlp.utils.DownloadError):
        return []
    formats: list[dict[str, Any]] = []
    for item in (info or {}).get("formats") or []:
        if not isinstance(item, dict) or not _trusted_media_url(item.get("url")):
            continue
        formats.append({**item, "format_note": item.get("format_note") or "Reddit HLS"})
    return formats


def extract_reddit_embed_info(
    url: str,
    *,
    socket_timeout: int,
    options: dict[str, Any],
    ydl_factory: Callable[..., Any] = yt_dlp.YoutubeDL,
    fetch_html: Callable[[str, int], str] = _download_embed_html,
) -> dict[str, Any] | None:
    """Return a normal yt-dlp info mapping using only Reddit-owned public endpoints."""

    embed_url = _reddit_embed_url(url)
    if not embed_url:
        return None
    try:
        html = fetch_html(embed_url, socket_timeout)
        parser = _RedditEmbedParser()
        parser.feed(html)
    except (OSError, UnicodeError, ValueError):
        return None
    if parser.player is None:
        return None

    packaged, duration = _packaged_formats(parser.player)
    hls = _hls_formats(parser.player, options, ydl_factory)
    formats = hls + packaged
    if not formats:
        return None

    post_id = parser.player.get("post-id", "").removeprefix("t3_")
    thumbnail = _trusted_media_url(parser.player.get("poster"))
    if thumbnail is None:
        poster = parser.player.get("poster")
        poster_host = (urlsplit(poster).hostname or "").lower() if poster else ""
        thumbnail = poster if poster_host.endswith(".redd.it") else None
    return {
        "id": post_id or _REDDIT_POST_PATH.match(urlsplit(url).path).group("id"),
        "title": " ".join(parser.title_parts) or f"Reddit post {post_id}",
        "uploader": " ".join(parser.author_parts) or None,
        "duration": duration,
        "thumbnail": thumbnail,
        "formats": formats,
        "extractor": "Reddit",
        "extractor_key": "Reddit",
        "webpage_url": url,
        "_omnifetch_reddit_embed": True,
    }

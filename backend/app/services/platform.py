"""Cosmetic platform labels; yt-dlp remains the extraction authority."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_KNOWN_DOMAINS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "reddit.com": "reddit",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "threads.net": "threads",
    "tumblr.com": "tumblr",
    "bsky.app": "bluesky",
    "snapchat.com": "snapchat",
    "linkedin.com": "linkedin",
    "pinterest.com": "pinterest",
    "discord.com": "discord",
    "discordapp.com": "discord",
    "twitch.tv": "twitch",
    "mastodon.social": "mastodon",
}

_EXTRACTOR_LABELS = {
    "youtube": "youtube",
    "instagram": "instagram",
    "tiktok": "tiktok",
    "reddit": "reddit",
    "twitter": "twitter",
    "facebook": "facebook",
    "threads": "threads",
    "tumblr": "tumblr",
    "bluesky": "bluesky",
    "snapchat": "snapchat",
    "linkedin": "linkedin",
    "pinterest": "pinterest",
    "discord": "discord",
    "twitch": "twitch",
    "generic": "unknown",
}


def detect_platform(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    for known, label in _KNOWN_DOMAINS.items():
        if hostname == known or hostname.endswith(f".{known}"):
            return label
    return "unknown"


def platform_from_info(info: dict, url: str) -> str:
    """Prefer yt-dlp's extractor identity over URL heuristics."""

    raw = str(info.get("extractor_key") or info.get("extractor") or "").strip()
    normalized = re.split(r"[:._]", raw, maxsplit=1)[0].lower()
    if normalized in _EXTRACTOR_LABELS:
        label = _EXTRACTOR_LABELS[normalized]
        return detect_platform(url) if label == "unknown" else label
    return normalized or detect_platform(url)


def is_known_platform_url(url: str) -> bool:
    return detect_platform(url) != "unknown"


def list_known_platforms() -> list[str]:
    return sorted(set(_KNOWN_DOMAINS.values()))

from __future__ import annotations

import pytest

from app.services.platform import detect_platform, platform_from_info


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/posts/example-1234-abcd", "linkedin"),
        ("https://lnkd.in/example", "linkedin"),
        ("https://www.reddit.com/r/videos/comments/example", "reddit"),
        ("https://v.redd.it/example", "reddit"),
        ("https://redd.it/example", "reddit"),
        ("https://www.snapchat.com/spotlight/example", "snapchat"),
        ("https://bsky.app/profile/example/post/abc", "bluesky"),
        ("https://main.bsky.dev/profile/example/post/abc", "bluesky"),
        ("https://example.tumblr.com/post/123/example", "tumblr"),
    ],
)
def test_social_platform_aliases_are_recognized(url: str, expected: str) -> None:
    assert detect_platform(url) == expected


def test_snapchat_spotlight_extractor_has_consistent_platform_label() -> None:
    assert (
        platform_from_info(
            {"extractor_key": "SnapchatSpotlight"},
            "https://www.snapchat.com/spotlight/example",
        )
        == "snapchat"
    )

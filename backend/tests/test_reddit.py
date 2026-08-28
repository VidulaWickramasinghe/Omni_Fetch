from __future__ import annotations

from typing import Any

from app.services.reddit import _reddit_embed_url, extract_reddit_embed_info


class FakeHlsYDL:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def __enter__(self) -> FakeHlsYDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        assert download is False
        return {
            "formats": [
                {
                    "url": "https://v.redd.it/media/HLS_720.m3u8",
                    "format_id": "hls-720",
                    "height": 720,
                    "vcodec": "h264",
                    "acodec": "aac",
                },
                {
                    "url": "https://attacker.example/internal",
                    "format_id": "untrusted",
                },
            ]
        }


def test_reddit_embed_fallback_normalizes_first_party_media() -> None:
    html = """
    <a href="https://www.reddit.com/user/example/">Example author</a>
    <h1>A public Reddit video</h1>
    <shreddit-player
      src="https://v.redd.it/media/HLSPlaylist.m3u8"
      post-id="t3_abc123"
      poster="https://external-preview.redd.it/poster.png"
      packaged-media-json='{"playbackMp4s":{"duration":12,"permutations":[
        {"source":{"url":"https://packaged-media.redd.it/media/video.mp4",
        "dimensions":{"width":1280,"height":720},"videoCodec":"H264"}}
      ]}}'>
    </shreddit-player>
    """

    info = extract_reddit_embed_info(
        "https://www.reddit.com/r/videos/comments/abc123/a_public_video/",
        socket_timeout=15,
        options={"quiet": True},
        ydl_factory=FakeHlsYDL,
        fetch_html=lambda _url, _timeout: html,
    )

    assert info is not None
    assert info["id"] == "abc123"
    assert info["title"] == "A public Reddit video"
    assert info["uploader"] == "Example author"
    assert info["duration"] == 12
    assert info["extractor_key"] == "Reddit"
    assert info["_omnifetch_reddit_embed"] is True
    assert len(info["formats"]) == 2
    assert {item["height"] for item in info["formats"]} == {720}
    assert all(
        item["url"].endswith("redd.it/media/HLS_720.m3u8")
        or item["url"].endswith("redd.it/media/video.mp4")
        for item in info["formats"]
    )


def test_reddit_embed_url_is_fixed_to_reddit_owned_posts() -> None:
    assert (
        _reddit_embed_url("https://www.reddit.com/r/videos/comments/abc123/a_public_video/?share=1")
        == "https://embed.reddit.com/r/videos/comments/abc123/a_public_video/"
    )
    assert _reddit_embed_url("https://reddit.com.attacker.example/r/x/comments/abc/video") is None
    assert _reddit_embed_url("https://www.reddit.com/r/videos/") is None


def test_reddit_embed_fallback_fails_closed_without_a_player() -> None:
    info = extract_reddit_embed_info(
        "https://www.reddit.com/r/videos/comments/abc123/a_public_video/",
        socket_timeout=15,
        options={},
        ydl_factory=FakeHlsYDL,
        fetch_html=lambda _url, _timeout: "<html><h1>No media</h1></html>",
    )

    assert info is None

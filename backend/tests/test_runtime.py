from __future__ import annotations

from pathlib import Path

from app.services import runtime


def test_explicit_ffmpeg_executable_is_preferred(settings_factory, tmp_path: Path) -> None:
    executable = tmp_path / "custom-ffmpeg"
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o700)

    resolved = runtime.resolve_ffmpeg_location(settings_factory(ffmpeg_location=str(executable)))

    assert resolved == str(executable)


def test_bundled_ffmpeg_is_used_when_system_binary_is_missing(
    settings_factory, monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "bundled-ffmpeg"
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime.imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(executable))
    runtime._automatic_ffmpeg.cache_clear()

    try:
        assert runtime.resolve_ffmpeg_location(settings_factory()) == str(executable)
    finally:
        runtime._automatic_ffmpeg.cache_clear()


def test_node_is_enabled_when_deno_is_not_installed(monkeypatch) -> None:
    paths = {"deno": None, "node": "/opt/node", "qjs": None}
    monkeypatch.setattr(runtime.shutil, "which", paths.get)
    runtime.resolve_js_runtimes.cache_clear()

    try:
        assert runtime.resolve_js_runtimes() == {"node": {"path": "/opt/node"}}
    finally:
        runtime.resolve_js_runtimes.cache_clear()


def test_runtime_options_include_local_media_tools(settings_factory, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "resolve_ffmpeg_location", lambda _settings: "/opt/ffmpeg")
    monkeypatch.setattr(
        runtime,
        "resolve_js_runtimes",
        lambda: {"deno": {"path": "/opt/deno"}},
    )

    assert runtime.ytdlp_runtime_options(settings_factory()) == {
        "ffmpeg_location": "/opt/ffmpeg",
        "js_runtimes": {"deno": {"path": "/opt/deno"}},
    }


def test_impersonation_dependency_is_available() -> None:
    assert runtime.impersonation_available() is True

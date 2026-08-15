"""CLI routing: default command, clipboard mode, batch files."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from media import cli as cli_module
from media.config import Config
from media.pipeline import DOWNLOADED, FAILED, Outcome

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    """Never touch the real ~/.config/media during tests."""
    config = Config(download_folder=str(tmp_path / "out"))
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli_module, "ensure_config_file", lambda *a, **k: tmp_path / "config.toml")
    return config


@pytest.fixture
def captured(monkeypatch) -> list[str]:
    """Record the URLs the pipeline is asked to download."""
    seen: list[str] = []

    class FakePipeline:
        def __init__(self, cfg, reporter, destination=None):
            self.destination = destination

        def run(self, target):
            seen.append(target.url)
            return [Outcome(DOWNLOADED, target.url, path=None, converted=True)]

    monkeypatch.setattr(cli_module, "Pipeline", FakePipeline)
    return seen


def test_url_argument_routes_to_the_default_command(captured):
    result = runner.invoke(cli_module.app, ["https://youtube.com/shorts/dQw4w9WgXcQ"])
    assert result.exit_code == 0
    assert captured == ["https://www.youtube.com/shorts/dQw4w9WgXcQ"]


def test_multiple_urls_are_deduplicated(captured):
    result = runner.invoke(
        cli_module.app,
        [
            "https://youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.instagram.com/reel/Dac9ebRiOCF/",
            "https://youtu.be/dQw4w9WgXcQ?si=x",  # same video, different form
        ],
    )
    assert result.exit_code == 0
    assert len(captured) == 2


def test_unsupported_url_fails_with_a_clear_message(captured):
    result = runner.invoke(cli_module.app, ["https://vimeo.com/12345"])
    assert result.exit_code == 2
    assert "Not an Instagram or YouTube link" in result.output
    assert captured == []


def test_no_arguments_reads_the_clipboard(monkeypatch, captured):
    monkeypatch.setattr(
        cli_module, "read_clipboard",
        lambda: "look at this https://www.instagram.com/reel/Dac9ebRiOCF/ 🔥",
    )
    result = runner.invoke(cli_module.app, [])
    assert result.exit_code == 0
    assert captured == ["https://www.instagram.com/reel/Dac9ebRiOCF/"]
    assert "clipboard" in result.output.lower()


def test_empty_clipboard_explains_what_to_do(monkeypatch, captured):
    monkeypatch.setattr(cli_module, "read_clipboard", lambda: "   ")
    result = runner.invoke(cli_module.app, [])
    assert result.exit_code == 1
    assert "Nothing on the clipboard" in result.output
    assert captured == []


def test_clipboard_without_a_supported_link(monkeypatch, captured):
    monkeypatch.setattr(cli_module, "read_clipboard", lambda: "https://example.com/hello")
    result = runner.invoke(cli_module.app, [])
    assert result.exit_code == 1
    assert "No Instagram or YouTube link" in result.output


def test_batch_file_is_read_line_by_line(tmp_path, captured):
    listing = tmp_path / "urls.txt"
    listing.write_text(
        "# my links\n"
        "https://youtube.com/shorts/dQw4w9WgXcQ\n"
        "\n"
        "https://www.instagram.com/reel/Dac9ebRiOCF/\n"
        "not-a-url\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli_module.app, [str(listing)])
    assert result.exit_code == 0
    assert len(captured) == 2
    assert "not a supported URL" in result.output


def test_batch_file_without_any_urls_is_an_error(tmp_path, captured):
    listing = tmp_path / "urls.txt"
    listing.write_text("# nothing here\nhello\n", encoding="utf-8")
    result = runner.invoke(cli_module.app, [str(listing)])
    assert result.exit_code == 2
    assert "No supported URLs" in result.output


def test_batch_summary_is_printed(monkeypatch, tmp_path):
    class FakePipeline:
        def __init__(self, cfg, reporter, destination=None):
            pass

        def run(self, target):
            if "instagram" in target.url:
                return [Outcome(FAILED, target.url, reason="This post is private.")]
            return [Outcome(DOWNLOADED, target.url, converted=True)]

    monkeypatch.setattr(cli_module, "Pipeline", FakePipeline)
    listing = tmp_path / "urls.txt"
    listing.write_text(
        "https://youtube.com/shorts/dQw4w9WgXcQ\nhttps://www.instagram.com/reel/Dac9ebRiOCF/\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli_module.app, [str(listing)])

    assert result.exit_code == 1  # a failure happened
    assert "Downloaded" in result.output and "Failed" in result.output
    assert "This post is private." in result.output


def test_output_flag_overrides_the_destination(monkeypatch, tmp_path, isolated_config):
    seen = {}

    class FakePipeline:
        def __init__(self, cfg, reporter, destination=None):
            seen["destination"] = destination
            seen["overwrite"] = cfg.overwrite
            seen["convert_codecs"] = cfg.convert_codecs

        def run(self, target):
            return [Outcome(DOWNLOADED, target.url)]

    monkeypatch.setattr(cli_module, "Pipeline", FakePipeline)
    target = tmp_path / "Videos"
    result = runner.invoke(
        cli_module.app,
        ["https://youtube.com/shorts/dQw4w9WgXcQ", "-o", str(target), "--force", "--no-convert"],
    )
    assert result.exit_code == 0
    assert seen["destination"] == target
    assert seen["overwrite"] is True
    assert seen["convert_codecs"] == []


def test_subcommands_are_still_reachable():
    assert runner.invoke(cli_module.app, ["doctor"]).exit_code in (0, 1)
    assert runner.invoke(cli_module.app, ["watch", "--help"]).exit_code == 0
    assert runner.invoke(cli_module.app, ["update", "--help"]).exit_code == 0
    assert runner.invoke(cli_module.app, ["library", "--help"]).exit_code == 0


def test_library_builds_a_page_and_opens_it(monkeypatch, tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"")
    page = folder / "library.html"
    opened: list = []

    monkeypatch.setattr(cli_module.library, "candidates", lambda target: [folder / "a.mp4"])
    monkeypatch.setattr(cli_module.library, "scan", lambda target, **kwargs: ["entry"])
    monkeypatch.setattr(cli_module.library, "prune_cache", lambda target, entries: 0)
    monkeypatch.setattr(cli_module.library, "build", lambda target, entries: page)
    monkeypatch.setattr(cli_module, "open_path", lambda path: opened.append(path) or True)

    result = runner.invoke(cli_module.app, ["library", str(folder)])

    assert result.exit_code == 0
    assert opened == [page]


def test_library_without_videos_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.library, "candidates", lambda target: [])

    result = runner.invoke(cli_module.app, ["library", str(tmp_path)])

    assert result.exit_code == 1
    assert "No videos" in result.output


def test_library_reports_when_nothing_came_from_media(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.library, "candidates", lambda target: [tmp_path / "a.mp4"])
    monkeypatch.setattr(cli_module.library, "scan", lambda target, **kwargs: [])

    result = runner.invoke(cli_module.app, ["library", str(tmp_path)])

    assert result.exit_code == 1
    assert "--all" in result.output


def test_library_no_open_leaves_the_browser_alone(monkeypatch, tmp_path):
    page = tmp_path / "library.html"
    opened: list = []

    monkeypatch.setattr(cli_module.library, "candidates", lambda target: [tmp_path / "a.mp4"])
    monkeypatch.setattr(cli_module.library, "scan", lambda target, **kwargs: ["entry"])
    monkeypatch.setattr(cli_module.library, "prune_cache", lambda target, entries: 0)
    monkeypatch.setattr(cli_module.library, "build", lambda target, entries: page)
    monkeypatch.setattr(cli_module, "open_path", lambda path: opened.append(path) or True)

    result = runner.invoke(cli_module.app, ["library", str(tmp_path), "--no-open"])

    assert result.exit_code == 0
    assert opened == []


def test_help_and_version_are_not_swallowed_by_the_default_command():
    top = runner.invoke(cli_module.app, ["--help"])
    assert top.exit_code == 0
    assert "watch" in top.output and "doctor" in top.output

    version = runner.invoke(cli_module.app, ["--version"])
    assert version.exit_code == 0
    assert "media" in version.output


def test_config_path_is_printed(tmp_path):
    result = runner.invoke(cli_module.app, ["config", "--path"])
    assert result.exit_code == 0
    assert "config.toml" in result.output

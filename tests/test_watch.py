"""Watch mode: reacts to clipboard changes, never downloads the same thing twice."""

from __future__ import annotations

import pytest
from rich.console import Console

from media import watch as watch_module
from media.config import Config
from media.pipeline import DOWNLOADED, FAILED, Outcome
from media.ui import Reporter

REEL = "https://www.instagram.com/reel/Dac9ebRiOCF/"
SHORT = "https://youtube.com/shorts/dQw4w9WgXcQ"


@pytest.fixture
def reporter(tmp_path):
    with (tmp_path / "console.log").open("w", encoding="utf-8") as sink:
        yield Reporter(console=Console(file=sink, force_terminal=False))


def drive(monkeypatch, clipboard_sequence, outcome_for=None):
    """Run watch() over a scripted clipboard, then stop it."""
    downloaded: list[str] = []
    notifications: list[tuple[str, str]] = []
    values = list(clipboard_sequence)

    def fake_clipboard():
        if not values:
            raise KeyboardInterrupt
        return values.pop(0)

    class FakePipeline:
        def __init__(self, cfg, reporter, destination=None):
            self.destination = destination or cfg.destination

        def run(self, target):
            downloaded.append(target.url)
            if outcome_for:
                return outcome_for(target)
            return [Outcome(DOWNLOADED, target.url, path=None, converted=False)]

    monkeypatch.setattr(watch_module, "read_clipboard", fake_clipboard)
    monkeypatch.setattr(watch_module, "Pipeline", FakePipeline)
    monkeypatch.setattr(
        watch_module, "notify",
        lambda title, message, **kwargs: notifications.append((title, message)),
    )
    monkeypatch.setattr(watch_module.time, "sleep", lambda seconds: None)
    return downloaded, notifications


def test_new_clipboard_links_are_downloaded(monkeypatch, reporter, tmp_path):
    downloaded, notifications = drive(monkeypatch, ["", REEL, SHORT])
    watch_module.watch(Config(download_folder=str(tmp_path)), reporter)

    assert downloaded == [REEL, "https://www.youtube.com/shorts/dQw4w9WgXcQ"]
    assert [title for title, _ in notifications] == ["Download complete"] * 2


def test_the_same_link_is_never_downloaded_twice(monkeypatch, reporter, tmp_path):
    downloaded, _ = drive(
        monkeypatch,
        ["", REEL, "unrelated text", REEL, "https://youtu.be/dQw4w9WgXcQ", SHORT],
    )
    watch_module.watch(Config(download_folder=str(tmp_path)), reporter)

    # The reel once, and the Short once despite arriving in two different forms.
    assert downloaded == [REEL, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


def test_whatever_is_already_on_the_clipboard_at_startup_is_ignored(
    monkeypatch, reporter, tmp_path
):
    downloaded, _ = drive(monkeypatch, [REEL, REEL, SHORT])
    watch_module.watch(Config(download_folder=str(tmp_path)), reporter)

    assert downloaded == ["https://www.youtube.com/shorts/dQw4w9WgXcQ"]


def test_unsupported_clipboard_content_is_ignored(monkeypatch, reporter, tmp_path):
    downloaded, notifications = drive(
        monkeypatch, ["", "just some copied text", "https://example.com/page"]
    )
    watch_module.watch(Config(download_folder=str(tmp_path)), reporter)

    assert downloaded == []
    assert notifications == []


def test_failures_notify_but_do_not_stop_watching(monkeypatch, reporter, tmp_path):
    def outcome_for(target):
        if "instagram" in target.url:
            return [Outcome(FAILED, target.url, reason="This post is private.")]
        return [Outcome(DOWNLOADED, target.url)]

    downloaded, notifications = drive(monkeypatch, ["", REEL, SHORT], outcome_for)
    report = watch_module.watch(Config(download_folder=str(tmp_path)), reporter)

    assert len(downloaded) == 2  # kept going after the failure
    assert notifications[0] == ("Download failed", "This post is private.")
    assert report.counts["failed"] == 1
    assert report.counts["downloaded"] == 1

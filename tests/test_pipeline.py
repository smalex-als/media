"""End-to-end pipeline tests with the network layer stubbed out."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest
from rich.console import Console

from media import ffmpeg, pipeline as pipeline_module
from media.config import Config
from media.downloader import Item, Plan
from media.errors import LoginRequired
from media.pipeline import DOWNLOADED, FAILED, SKIPPED, Pipeline
from media.ui import Reporter
from media.urls import detect

from .conftest import faststart, needs_ffmpeg

pytestmark = needs_ffmpeg

REEL = detect("https://www.instagram.com/reel/Dac9ebRiOCF/")
SHORT = detect("https://youtube.com/shorts/dQw4w9WgXcQ")

INFO = {
    "id": "Dac9ebRiOCF",
    "title": "Video by John Smith [Dac9ebRiOCF]",
    "description": "Squat Tutorial",
    "uploader": "John Smith",
    "upload_date": "20240115",
    "webpage_url": "https://www.instagram.com/reel/Dac9ebRiOCF/",
}


@pytest.fixture
def reporter(tmp_path) -> Reporter:
    # A non-terminal console keeps progress bars out of the test output.
    with (tmp_path / "console.log").open("w", encoding="utf-8") as sink:
        yield Reporter(console=Console(file=sink, force_terminal=False))


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(download_folder=str(tmp_path / "out"), embed_thumbnail=False)
    config.validate()
    return config


def install_fake_download(monkeypatch, source: Path, entries: list[dict] | None = None) -> dict:
    """Replace the yt-dlp layer with one that copies a local sample file."""
    entries = entries or [INFO]
    calls = {"inspect": 0, "download": 0, "playlist_items": None}

    def fake_inspect(target, config):
        calls["inspect"] += 1
        return Plan(entries=entries, is_playlist=len(entries) > 1)

    def fake_download(target, config, workdir, *, on_progress=None, playlist_items=None):
        calls["download"] += 1
        calls["playlist_items"] = playlist_items
        wanted = entries
        if playlist_items:
            positions = {int(p) for p in playlist_items.split(",")}
            wanted = [e for i, e in enumerate(entries, start=1) if i in positions]
        items = []
        for index, entry in enumerate(wanted):
            copy = Path(workdir) / f"{index}{source.suffix}"
            shutil.copy2(source, copy)
            items.append(Item(info=entry, path=copy))
        return items

    monkeypatch.setattr(pipeline_module, "inspect", fake_inspect)
    monkeypatch.setattr(pipeline_module, "download", fake_download)
    return calls


def test_vp9_reel_becomes_a_clean_h264_mp4(monkeypatch, cfg, reporter, sample_vp9):
    install_fake_download(monkeypatch, sample_vp9)

    outcomes = Pipeline(cfg, reporter).run(REEL)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == DOWNLOADED
    assert outcome.converted is True
    assert outcome.source_codec == "vp9"
    assert outcome.path.name == "John Smith - Squat Tutorial.mp4"

    result = ffmpeg.probe(outcome.path)
    assert result.video.codec == "h264"
    assert result.audio.codec == "aac"
    assert faststart(outcome.path)


def test_metadata_and_file_date_are_applied(monkeypatch, cfg, reporter, sample_vp9):
    install_fake_download(monkeypatch, sample_vp9)
    outcome = Pipeline(cfg, reporter).run(REEL)[0]

    modified = datetime.fromtimestamp(outcome.path.stat().st_mtime).date()
    assert modified.isoformat() == "2024-01-15"


def test_h264_source_is_not_re_encoded(monkeypatch, cfg, reporter, sample_h264):
    install_fake_download(monkeypatch, sample_h264)
    outcome = Pipeline(cfg, reporter).run(SHORT)[0]

    assert outcome.status == DOWNLOADED
    assert outcome.converted is False
    assert ffmpeg.probe(outcome.path).video.codec == "h264"


def test_temporary_files_are_cleaned_up(monkeypatch, cfg, reporter, sample_vp9):
    install_fake_download(monkeypatch, sample_vp9)
    outcome = Pipeline(cfg, reporter).run(REEL)[0]

    destination = outcome.path.parent
    assert list(destination.iterdir()) == [outcome.path]
    assert not any(destination.glob(".media-*"))


def test_original_is_preserved_when_asked(monkeypatch, cfg, reporter, sample_vp9):
    cfg.preserve_original = True
    install_fake_download(monkeypatch, sample_vp9)
    outcome = Pipeline(cfg, reporter).run(REEL)[0]

    kept = outcome.path.with_name(f"{outcome.path.stem}.original.webm")
    assert kept.exists()
    assert ffmpeg.probe(kept).video.codec == "vp9"


def test_existing_files_are_skipped_without_downloading(monkeypatch, cfg, reporter, sample_vp9):
    calls = install_fake_download(monkeypatch, sample_vp9)
    first = Pipeline(cfg, reporter).run(REEL)[0]
    assert calls["download"] == 1

    second = Pipeline(cfg, reporter).run(REEL)
    assert calls["download"] == 1  # nothing was fetched the second time
    assert second[0].status == SKIPPED
    assert second[0].path == first.path


def test_force_overwrites_an_existing_file(monkeypatch, cfg, reporter, sample_vp9):
    calls = install_fake_download(monkeypatch, sample_vp9)
    Pipeline(cfg, reporter).run(REEL)
    cfg.overwrite = True

    outcome = Pipeline(cfg, reporter).run(REEL)[0]
    assert calls["download"] == 2
    assert outcome.status == DOWNLOADED


def test_carousel_produces_one_file_per_entry(monkeypatch, cfg, reporter, sample_vp9):
    entries = [INFO | {"id": "one"}, INFO | {"id": "two"}]
    install_fake_download(monkeypatch, sample_vp9, entries)

    post = detect("https://www.instagram.com/p/ABC123xyz/")
    outcomes = Pipeline(cfg, reporter).run(post)

    assert len(outcomes) == 2
    names = sorted(o.path.name for o in outcomes)
    assert names == [
        "John Smith - Squat Tutorial (1).mp4",
        "John Smith - Squat Tutorial (2).mp4",
    ]
    assert all(o.status == DOWNLOADED for o in outcomes)


def test_carousel_only_fetches_missing_entries(monkeypatch, cfg, reporter, sample_vp9):
    entries = [INFO | {"id": "one"}, INFO | {"id": "two"}]
    calls = install_fake_download(monkeypatch, sample_vp9, entries)
    post = detect("https://www.instagram.com/p/ABC123xyz/")

    Pipeline(cfg, reporter).run(post)
    (cfg.destination / "John Smith - Squat Tutorial (2).mp4").unlink()

    outcomes = Pipeline(cfg, reporter).run(post)
    assert calls["playlist_items"] == "2"
    statuses = sorted(o.status for o in outcomes)
    assert statuses == [DOWNLOADED, SKIPPED]


def test_one_bad_carousel_entry_does_not_lose_the_good_ones(
    monkeypatch, cfg, reporter, sample_vp9, tmp_path
):
    entries = [INFO | {"id": "good"}, INFO | {"id": "broken"}]
    install_fake_download(monkeypatch, sample_vp9, entries)

    # Corrupt the second file after it "downloads" so probing it fails.
    original_probe = ffmpeg.probe

    def probe(path):
        info = original_probe(path)
        if path.name.startswith("1"):
            raise pipeline_module.ffmpeg.ConversionFailed("ffprobe could not read the file.")
        return info

    monkeypatch.setattr(pipeline_module.ffmpeg, "probe", probe)
    post = detect("https://www.instagram.com/p/ABC123xyz/")
    outcomes = Pipeline(cfg, reporter).run(post)

    assert sorted(o.status for o in outcomes) == [DOWNLOADED, FAILED]
    good = next(o for o in outcomes if o.status == DOWNLOADED)
    assert good.path.exists()


def test_download_failures_are_reported_not_raised(monkeypatch, cfg, reporter):
    def fail(target, config):
        raise LoginRequired("This content requires a logged-in session.", "Use cookies.")

    monkeypatch.setattr(pipeline_module, "inspect", fail)
    outcomes = Pipeline(cfg, reporter).run(REEL)

    assert len(outcomes) == 1
    assert outcomes[0].status == FAILED
    assert "logged-in" in outcomes[0].reason


def test_report_counts_and_failures(monkeypatch, cfg, reporter, sample_vp9):
    install_fake_download(monkeypatch, sample_vp9)
    report = pipeline_module.Report()
    pipe = Pipeline(cfg, reporter)

    for outcome in pipe.run(REEL):
        report.add(outcome)
    for outcome in pipe.run(REEL):  # same URL again -> skipped
        report.add(outcome)
    report.add(pipeline_module.Outcome(FAILED, "https://x", reason="boom"))

    counts = report.counts
    assert counts == {"downloaded": 1, "converted": 1, "skipped": 1, "failed": 1}
    assert report.failures == [("https://x", "boom")]
    assert report.ok is False


def test_no_convert_still_produces_a_playable_mp4(monkeypatch, cfg, reporter, sample_h264):
    cfg.convert_codecs = []
    install_fake_download(monkeypatch, sample_h264)

    outcome = Pipeline(cfg, reporter).run(SHORT)[0]
    assert outcome.converted is False
    assert ffmpeg.probe(outcome.path).video.codec == "h264"
    assert faststart(outcome.path)


def test_destination_override_is_created(monkeypatch, reporter, sample_vp9, tmp_path):
    config = Config(embed_thumbnail=False)
    install_fake_download(monkeypatch, sample_vp9)
    destination = tmp_path / "deep" / "nested"

    outcome = Pipeline(config, reporter, destination).run(REEL)[0]
    assert outcome.path.parent == destination
    assert destination.is_dir()

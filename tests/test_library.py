"""Library page: scanning, thumbnails and HTML rendering."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from media import library
from media.errors import MediaError

from .conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def _tagged(source: Path, destination: Path, **tags: str) -> Path:
    """Copy a sample and stamp container tags onto it."""
    args: list[str] = []
    for key, value in tags.items():
        args += ["-metadata", f"{key}={value}"]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(source), "-c", "copy", *args, str(destination)],
        check=True, capture_output=True, timeout=120,
    )
    return destination


@pytest.fixture
def folder(sample_h264, tmp_path) -> Path:
    """A download folder: two tagged videos and one that media never touched."""
    home = tmp_path / "library"
    home.mkdir()
    _tagged(
        sample_h264, home / "Anna - Morning.mp4",
        title="Morning", artist="Anna", date="2026-08-01",
        comment="https://www.instagram.com/reel/AAA111/",
    )
    _tagged(
        sample_h264, home / "Bob - Evening.mp4",
        title="Evening", artist="Bob", date="2026-07-14",
        comment="https://youtube.com/shorts/BBB222",
    )
    shutil.copy2(sample_h264, home / "from-my-phone.mp4")
    return home


# ------------------------------------------------------------------ scanning


def test_candidates_lists_only_videos(folder):
    (folder / "notes.txt").write_text("ignore me", encoding="utf-8")
    (folder / ".hidden.mp4").write_text("", encoding="utf-8")

    names = [path.name for path in library.candidates(folder)]

    assert names == ["Anna - Morning.mp4", "Bob - Evening.mp4", "from-my-phone.mp4"]


def test_candidates_rejects_a_missing_folder(tmp_path):
    with pytest.raises(MediaError):
        library.candidates(tmp_path / "nope")


def test_scan_keeps_only_files_media_downloaded(folder):
    entries = library.scan(folder)

    assert sorted(entry.title for entry in entries) == ["Evening", "Morning"]
    assert all(entry.is_from_media for entry in entries)


def test_scan_does_not_thumbnail_files_it_skips(folder):
    """Every extra frame grab is an ffmpeg run, so skipped files must not cost one."""
    library.scan(folder)

    cached = sorted(path.name for path in (folder / library.CACHE_DIR_NAME).glob("*.jpg"))

    assert len(cached) == 2
    assert not any(name.startswith("from-my-phone") for name in cached)


def test_scan_include_untagged_adds_the_rest(folder):
    entries = library.scan(folder, include_untagged=True)

    titles = sorted(entry.title for entry in entries)
    assert titles == ["Evening", "Morning", "from-my-phone"]


def test_scan_reads_tags_and_reports_progress(folder):
    seen: list[tuple[int, int, str]] = []

    entries = library.scan(folder, on_progress=lambda *args: seen.append(args))

    morning = next(entry for entry in entries if entry.title == "Morning")
    assert morning.creator == "Anna"
    assert morning.day == date(2026, 8, 1)
    assert morning.url == "https://www.instagram.com/reel/AAA111/"
    assert [step[0] for step in seen] == [1, 2, 3]
    assert all(step[1] == 3 for step in seen)


def test_scan_sorts_newest_first(folder):
    import os

    os.utime(folder / "Anna - Morning.mp4", (1_000, 1_000))
    os.utime(folder / "Bob - Evening.mp4", (2_000, 2_000))

    entries = library.scan(folder)

    assert [entry.title for entry in entries] == ["Evening", "Morning"]


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"date": "2026-08-01"}, date(2026, 8, 1)),
        ({"creation_time": "2026-08-01T12:00:00Z"}, date(2026, 8, 1)),
        ({"year": "2026"}, date(2026, 1, 1)),
        ({"date": "nonsense"}, None),
        ({}, None),
    ],
)
def test_tag_date_parsing(tags, expected):
    assert library._tag_date(tags) == expected


# ------------------------------------------------------------------ thumbnails


def test_thumbnail_is_generated_and_cached(folder, tmp_path):
    cache = tmp_path / "cache"
    video = folder / "Anna - Morning.mp4"

    first = library.make_thumbnail(video, cache, duration=2.0)
    assert first is not None and first.exists() and first.stat().st_size > 0

    stamp = first.stat().st_mtime_ns
    second = library.make_thumbnail(video, cache, duration=2.0)
    assert second == first
    assert second.stat().st_mtime_ns == stamp, "an unchanged video should not be re-encoded"


def test_thumbnail_name_is_stable_across_processes(folder, tmp_path):
    """Cache hits depend on the name, so it must not use salted hash()."""
    video = folder / "Anna - Morning.mp4"
    code = (
        "from pathlib import Path; from media import library; "
        f"print(library.make_thumbnail(Path({str(video)!r}), Path({str(tmp_path / 'c')!r}), "
        "duration=2.0))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code], check=True, capture_output=True, text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        for seed in ("0", "1")
    }
    assert len(runs) == 1


def test_thumbnail_survives_awkward_filenames(sample_h264, tmp_path):
    weird = tmp_path / "Оле́г • 50% скидка_ #рилс.mp4"
    shutil.copy2(sample_h264, weird)

    thumb = library.make_thumbnail(weird, tmp_path / "cache", duration=2.0)

    assert thumb is not None and thumb.exists()
    assert thumb.suffix == ".jpg"


def test_prune_cache_drops_orphans(folder):
    entries = library.scan(folder)
    cache = folder / library.CACHE_DIR_NAME
    orphan = cache / "gone-12345678.jpg"
    orphan.write_bytes(b"stale")

    removed = library.prune_cache(folder, entries)

    assert removed == 1
    assert not orphan.exists()
    assert all(entry.thumb.exists() for entry in entries if entry.thumb)


# ------------------------------------------------------------------ rendering


def _payload(page: Path) -> list[dict]:
    text = page.read_text(encoding="utf-8")
    raw = text.split('<script id="items" type="application/json">')[1].split("</script>")[0]
    return json.loads(raw.replace("<\\/", "</"))


def test_build_writes_a_page_describing_every_entry(folder):
    entries = library.scan(folder)

    page = library.build(folder, entries)

    assert page == folder / library.PAGE_NAME
    items = _payload(page)
    assert {item["title"] for item in items} == {"Morning", "Evening"}
    morning = next(item for item in items if item["title"] == "Morning")
    assert morning["creator"] == "Anna"
    assert morning["url"] == "https://www.instagram.com/reel/AAA111/"
    assert morning["file"] == "Anna%20-%20Morning.mp4"
    assert morning["thumb"].startswith(".media-library/")
    assert morning["vertical"] is True
    assert morning["durationText"] and morning["sizeText"]


def test_build_keeps_video_paths_relative(folder):
    """The page sits next to the videos, so file:// playback needs relative hrefs."""
    page = library.build(folder, library.scan(folder))

    for item in _payload(page):
        assert not item["file"].startswith(("/", "file:"))
        assert not item["thumb"].startswith(("/", "file:"))


def test_build_does_not_let_data_escape_the_script_block(sample_h264, tmp_path):
    home = tmp_path / "library"
    home.mkdir()
    _tagged(
        sample_h264, home / "x.mp4",
        title="</script><img src=x onerror=alert(1)>", artist="Anna",
        comment="https://example.com/reel/1",
    )

    page = library.build(home, library.scan(home))
    text = page.read_text(encoding="utf-8")

    body = text.split('<script id="items" type="application/json">')[1].split("</script>")[0]
    assert "</script>" not in body
    assert _payload(page)[0]["title"] == "</script><img src=x onerror=alert(1)>"


def test_build_escapes_the_folder_name(sample_h264, tmp_path):
    home = tmp_path / "a <b> & c"
    home.mkdir()
    _tagged(sample_h264, home / "x.mp4", title="X", comment="https://example.com/1")

    text = library.build(home, library.scan(home)).read_text(encoding="utf-8")

    assert "a &lt;b&gt; &amp; c" in text
    assert "<b>" not in text.split("<header>")[1].split("</header>")[0]


def test_build_on_an_empty_list_still_writes_a_page(tmp_path):
    page = library.build(tmp_path, [])

    assert page.exists()
    assert _payload(page) == []

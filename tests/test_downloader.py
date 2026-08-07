"""Extraction-result handling: carousels, partial failures, error surfacing."""

from __future__ import annotations

import pytest

from media import downloader
from media.errors import LoginRequired, NoVideoFound
from media.urls import detect

POST = detect("https://www.instagram.com/p/DYzI3FYimEw/")
REEL = detect("https://www.instagram.com/reel/Dac9ebRiOCF/")


def logger_with(*messages: str) -> downloader._Logger:
    ydl_logger = downloader._Logger()
    ydl_logger.errors.extend(messages)
    return ydl_logger


def test_single_video_becomes_a_one_entry_plan():
    plan = downloader._flatten({"id": "abc", "title": "T"}, logger_with(), REEL)
    assert plan.count == 1
    assert plan.is_playlist is False
    assert plan.unavailable == 0


def test_carousel_survives_an_unreadable_slide():
    # yt-dlp leaves None in place of an entry it could not extract.
    info = {
        "_type": "playlist",
        "entries": [{"id": "a"}, {"id": "b"}, None, {"id": "c"}, {"id": "d"}, {"id": "e"}],
    }
    plan = downloader._flatten(info, logger_with("No video formats found!"), POST)

    assert plan.count == 5
    assert plan.unavailable == 1
    assert plan.is_playlist is True
    assert [entry["id"] for entry in plan.entries] == ["a", "b", "c", "d", "e"]


def test_a_completely_unreadable_post_reports_why():
    info = {"_type": "playlist", "entries": [None, None]}
    with pytest.raises(NoVideoFound) as caught:
        downloader._flatten(
            info, logger_with("ERROR: [Instagram] x: No video formats found!"), POST
        )
    hint = (caught.value.hint or "").lower()
    assert "image" in hint and "cookies_from_browser" in hint


def test_a_login_gated_post_still_points_at_cookies():
    info = {"_type": "playlist", "entries": [None]}
    with pytest.raises(LoginRequired):
        downloader._flatten(
            info,
            logger_with("ERROR: [Instagram] x: rate-limit reached or login required"),
            POST,
        )


def test_an_empty_post_without_logged_errors_falls_back():
    with pytest.raises(NoVideoFound):
        downloader._flatten({"_type": "playlist", "entries": []}, logger_with(), POST)


def test_entry_paths_are_read_from_requested_downloads(tmp_path):
    media_file = tmp_path / "0.abc.mp4"
    media_file.write_bytes(b"x")
    info = {
        "_type": "playlist",
        "entries": [{"id": "abc", "requested_downloads": [{"filepath": str(media_file)}]}],
    }
    items = downloader._collect(info, tmp_path)
    assert [item.path for item in items] == [media_file]


def test_entries_without_a_file_on_disk_are_dropped(tmp_path):
    info = {
        "_type": "playlist",
        "entries": [
            {"id": "gone", "requested_downloads": [{"filepath": str(tmp_path / "missing.mp4")}]},
            {"id": "none-at-all"},
        ],
    }
    assert downloader._collect(info, tmp_path) == []


def test_thumbnails_do_not_leak_between_carousel_entries(tmp_path):
    first = tmp_path / "1.aaa.mp4"
    first.write_bytes(b"x")
    second = tmp_path / "2.bbb.mp4"
    second.write_bytes(b"x")
    (tmp_path / "1.aaa.webp").write_bytes(b"x")  # only the first has a thumbnail

    info = {
        "_type": "playlist",
        "entries": [
            {"id": "aaa", "requested_downloads": [{"filepath": str(first)}]},
            {"id": "bbb", "requested_downloads": [{"filepath": str(second)}]},
        ],
    }
    items = downloader._collect(info, tmp_path)
    assert items[0].thumbnail == tmp_path / "1.aaa.webp"
    assert items[1].thumbnail is None

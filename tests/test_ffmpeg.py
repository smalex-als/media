"""Integration tests against the real ffmpeg/ffprobe binaries."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from media import ffmpeg
from media.errors import ConversionFailed

from .conftest import faststart, needs_ffmpeg

pytestmark = needs_ffmpeg


def test_normalize_codec_maps_container_names():
    assert ffmpeg.normalize_codec("avc1") == "h264"
    assert ffmpeg.normalize_codec("vp09") == "vp9"
    assert ffmpeg.normalize_codec("av01") == "av1"
    assert ffmpeg.normalize_codec("hev1") == "hevc"
    assert ffmpeg.normalize_codec(None) == ""


def test_probe_reads_vp9_and_opus(sample_vp9):
    info = ffmpeg.probe(sample_vp9)
    assert info.video is not None and info.video.codec == "vp9"
    assert info.video.width == 270 and info.video.height == 480
    assert info.audio is not None and info.audio.codec == "opus"
    assert 1.5 < info.duration < 2.5
    assert info.size > 0


def test_probe_rejects_a_file_that_is_not_media(tmp_path):
    junk = tmp_path / "not-video.mp4"
    junk.write_bytes(b"definitely not an mp4")
    with pytest.raises(ConversionFailed):
        ffmpeg.probe(junk)


def test_plan_converts_vp9_but_leaves_h264_alone(sample_vp9, sample_h264):
    vp9_plan = ffmpeg.plan_for(ffmpeg.probe(sample_vp9), ["vp9", "av1"])
    assert vp9_plan.transcode_video and vp9_plan.transcode_audio  # opus -> aac

    h264_plan = ffmpeg.plan_for(ffmpeg.probe(sample_h264), ["vp9", "av1"])
    assert not h264_plan.transcode_video and not h264_plan.transcode_audio


def test_plan_honours_an_empty_convert_list(sample_vp9):
    plan = ffmpeg.plan_for(ffmpeg.probe(sample_vp9), [])
    # VP9 still isn't macOS-friendly, so it converts even when not listed.
    assert plan.transcode_video


def test_vp9_converts_to_h264_aac_mp4(sample_vp9, tmp_path):
    source = ffmpeg.probe(sample_vp9)
    plan = ffmpeg.plan_for(source, ["vp9", "av1"])
    destination = tmp_path / "out.mp4"
    seen: list[float] = []

    cmd = ffmpeg.build_command(
        sample_vp9, destination, source, plan,
        metadata=ffmpeg.Metadata(
            title="Squat Tutorial", creator="John Smith", day=date(2024, 1, 15),
            url="https://www.instagram.com/reel/Dac9ebRiOCF/",
        ),
    )
    ffmpeg.run_ffmpeg(cmd, duration=source.duration, on_progress=seen.append)

    result = ffmpeg.probe(destination)
    assert result.video is not None and result.video.codec == "h264"
    assert result.audio is not None and result.audio.codec == "aac"
    assert result.video.pix_fmt == "yuv420p"
    assert (result.video.width, result.video.height) == (270, 480)
    assert "mp4" in result.format_name
    assert faststart(destination)
    assert seen and max(seen) > 0.5


def test_conversion_embeds_metadata(sample_vp9, tmp_path):
    source = ffmpeg.probe(sample_vp9)
    destination = tmp_path / "meta.mp4"
    cmd = ffmpeg.build_command(
        sample_vp9, destination, source, ffmpeg.plan_for(source, ["vp9"]),
        metadata=ffmpeg.Metadata(
            title="Squat Tutorial", creator="John Smith", day=date(2024, 1, 15),
            url="https://example.com/reel", description="A tutorial.",
        ),
    )
    ffmpeg.run_ffmpeg(cmd, duration=source.duration)

    tags = _format_tags(destination)
    assert tags.get("title") == "Squat Tutorial"
    assert tags.get("artist") == "John Smith"
    assert tags.get("comment") == "https://example.com/reel"
    assert (tags.get("date") or "").startswith("2024")


def test_h264_source_is_remuxed_without_re_encoding(sample_h264, tmp_path):
    source = ffmpeg.probe(sample_h264)
    plan = ffmpeg.plan_for(source, ["vp9", "av1"])
    destination = tmp_path / "remux.mp4"
    ffmpeg.run_ffmpeg(
        ffmpeg.build_command(sample_h264, destination, source, plan),
        duration=source.duration,
    )
    result = ffmpeg.probe(destination)
    assert result.video.codec == "h264" and result.audio.codec == "aac"
    assert faststart(destination)


def test_cover_art_is_attached(sample_vp9, tmp_path):
    cover_source = tmp_path / "thumb.jpg"
    import subprocess

    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=200x200", "-frames:v", "1", str(cover_source)],
        check=True, capture_output=True,
    )
    cover = ffmpeg.make_cover_jpeg(cover_source, tmp_path / "cover.jpg")
    assert cover is not None and cover.exists()

    source = ffmpeg.probe(sample_vp9)
    destination = tmp_path / "with-cover.mp4"
    ffmpeg.run_ffmpeg(
        ffmpeg.build_command(
            sample_vp9, destination, source, ffmpeg.plan_for(source, ["vp9"]), cover=cover
        ),
        duration=source.duration,
    )
    assert _has_attached_picture(destination)
    assert ffmpeg.probe(destination).video.codec == "h264"


def test_run_ffmpeg_reports_a_readable_failure(tmp_path):
    with pytest.raises(ConversionFailed) as caught:
        ffmpeg.run_ffmpeg(
            [ffmpeg.require_tool("ffmpeg"), "-i", str(tmp_path / "missing.webm"),
             str(tmp_path / "out.mp4")]
        )
    assert "ffmpeg failed" in caught.value.message


def test_target_bitrate_stays_in_a_sane_range():
    small = ffmpeg.VideoStream("vp9", 320, 240, 30, "yuv420p", "", "", False)
    large = ffmpeg.VideoStream("vp9", 3840, 2160, 60, "yuv420p", "", "", False)
    assert ffmpeg._target_bitrate(small) == "2.0M"
    assert ffmpeg._target_bitrate(large) == "20.0M"


def _format_tags(path: Path) -> dict[str, str]:
    import json
    import subprocess

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    )
    return {k.lower(): v for k, v in (json.loads(result.stdout)["format"].get("tags") or {}).items()}


def _has_attached_picture(path: Path) -> bool:
    import json
    import subprocess

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    return any(
        (stream.get("disposition") or {}).get("attached_pic")
        for stream in json.loads(result.stdout)["streams"]
    )

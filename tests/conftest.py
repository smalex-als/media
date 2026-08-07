"""Shared fixtures: real sample media, generated once per session with ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest_plugins: list[str] = []

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")

_SOURCES = ["-f", "lavfi", "-i", "testsrc2=size=270x480:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2"]


def _encode(destination: Path, *codec_args: str) -> Path:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *_SOURCES, "-map", "0:v", "-map", "1:a", *codec_args, str(destination),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return destination


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> dict[str, Path]:
    """A VP9/Opus webm and an H.264/AAC mp4 to exercise both pipeline branches."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    directory = tmp_path_factory.mktemp("samples")
    return {
        "vp9": _encode(
            directory / "vp9.webm",
            "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "300k",
            "-c:a", "libopus", "-b:a", "64k",
        ),
        "h264": _encode(
            directory / "h264.mp4",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
        ),
    }


@pytest.fixture
def sample_vp9(samples, tmp_path) -> Path:
    copy = tmp_path / "source.webm"
    shutil.copy2(samples["vp9"], copy)
    return copy


@pytest.fixture
def sample_h264(samples, tmp_path) -> Path:
    copy = tmp_path / "source.mp4"
    shutil.copy2(samples["h264"], copy)
    return copy


def faststart(path: Path) -> bool:
    """True when the moov atom precedes mdat, which is what Quick Look wants."""
    head = path.read_bytes()[:4_000_000]
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    return moov != -1 and (mdat == -1 or moov < mdat)

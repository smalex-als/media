"""ffprobe inspection and ffmpeg conversion, tuned for macOS playback."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .errors import ConversionFailed, MissingDependency
from .logs import get_logger

log = get_logger("ffmpeg")

BREW_HINT = "Install it with:  brew install ffmpeg"

# ffmpeg codec names -> the short names we speak in config and output.
_CODEC_ALIASES = {
    "avc1": "h264",
    "h264": "h264",
    "libx264": "h264",
    "hev1": "hevc",
    "hvc1": "hevc",
    "h265": "hevc",
    "hevc": "hevc",
    "vp09": "vp9",
    "vp9": "vp9",
    "vp8": "vp8",
    "av01": "av1",
    "av1": "av1",
    "libaom-av1": "av1",
}
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_MP4_TAGS = {"h264": "avc1", "hevc": "hvc1"}


def normalize_codec(name: str | None) -> str:
    key = (name or "").strip().lower()
    return _CODEC_ALIASES.get(key, key)


@dataclass(slots=True)
class VideoStream:
    codec: str
    width: int
    height: int
    fps: float
    pix_fmt: str
    color_transfer: str
    color_primaries: str
    is_attached_pic: bool

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in _HDR_TRANSFERS

    @property
    def is_10bit(self) -> bool:
        return any(tag in self.pix_fmt for tag in ("10", "12", "p010", "16"))


@dataclass(slots=True)
class AudioStream:
    codec: str
    channels: int
    sample_rate: int


@dataclass(slots=True)
class MediaInfo:
    path: Path
    duration: float
    size: int
    format_name: str
    video: VideoStream | None
    audio: AudioStream | None
    # Container-level tags, lowercased keys: title, artist, date, comment, …
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def resolution(self) -> str:
        if not self.video:
            return "audio only"
        return f"{self.video.width}×{self.video.height}"

    @property
    def codec_summary(self) -> str:
        parts = []
        if self.video:
            parts.append(self.video.codec.upper().replace("H264", "H.264"))
        if self.audio:
            parts.append(self.audio.codec.upper())
        return "/".join(parts) or "unknown"


# --------------------------------------------------------------------------- tools


def tool_path(name: str) -> str | None:
    return shutil.which(name)


def require_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise MissingDependency(f"{name} was not found on your PATH.", BREW_HINT)
    return found


def tool_version(name: str) -> str | None:
    """First-line version string for ffmpeg/ffprobe/yt-dlp, or None if absent."""
    executable = shutil.which(name)
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version" if name == "yt-dlp" else "-version"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (result.stdout or result.stderr or "").strip().splitlines()
    if not first:
        return None
    line = first[0]
    if line.startswith("ffmpeg version") or line.startswith("ffprobe version"):
        return line.split()[2]
    return line.strip()


def has_encoder(name: str) -> bool:
    executable = shutil.which("ffmpeg")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-loglevel", "error", "-encoders"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [name] for line in result.stdout.splitlines() if line.strip())


def supports_constant_quality() -> bool:
    """VideoToolbox constant-quality (-q:v) is Apple-silicon only."""
    return platform.machine() == "arm64"


# --------------------------------------------------------------------------- probe


def probe(path: Path) -> MediaInfo:
    """Read codecs, resolution and duration out of a file with ffprobe."""
    executable = require_tool("ffprobe")
    cmd = [
        executable, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConversionFailed(f"ffprobe could not run: {exc}") from exc
    if result.returncode != 0:
        raise ConversionFailed(
            f"ffprobe could not read {path.name}.",
            (result.stderr or "").strip().splitlines()[-1] if result.stderr else None,
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionFailed(f"ffprobe returned unreadable output for {path.name}.") from exc

    fmt = data.get("format") or {}
    video: VideoStream | None = None
    audio: AudioStream | None = None
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video":
            attached = bool((stream.get("disposition") or {}).get("attached_pic"))
            candidate = VideoStream(
                codec=normalize_codec(stream.get("codec_name")),
                width=int(stream.get("width") or 0),
                height=int(stream.get("height") or 0),
                fps=_parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
                pix_fmt=str(stream.get("pix_fmt") or ""),
                color_transfer=str(stream.get("color_transfer") or ""),
                color_primaries=str(stream.get("color_primaries") or ""),
                is_attached_pic=attached,
            )
            if video is None or (video.is_attached_pic and not attached):
                video = candidate
        elif kind == "audio" and audio is None:
            audio = AudioStream(
                codec=normalize_codec(stream.get("codec_name")),
                channels=int(stream.get("channels") or 0),
                sample_rate=int(stream.get("sample_rate") or 0),
            )

    try:
        size = int(fmt.get("size") or path.stat().st_size)
    except OSError:
        size = 0
    return MediaInfo(
        path=path,
        duration=float(fmt.get("duration") or 0.0),
        size=size,
        format_name=str(fmt.get("format_name") or ""),
        video=video,
        audio=audio,
        tags={str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()},
    )


def _parse_fps(value: str | None) -> float:
    if not value or "/" not in str(value):
        try:
            return float(value or 0)
        except ValueError:
            return 0.0
    num, _, den = str(value).partition("/")
    try:
        numerator, denominator = float(num), float(den)
    except ValueError:
        return 0.0
    return numerator / denominator if denominator else 0.0


# --------------------------------------------------------------------------- plan


@dataclass(slots=True)
class Plan:
    """What has to happen to make a file macOS-perfect."""

    transcode_video: bool
    transcode_audio: bool
    reason: str

    @property
    def is_conversion(self) -> bool:
        return self.transcode_video


def plan_for(info: MediaInfo, convert_codecs: list[str]) -> Plan:
    """Decide between a straight remux and a real conversion."""
    video = info.video
    if video is None:
        return Plan(False, False, "no video stream")

    codec = video.codec
    convertibles = {c.lower() for c in convert_codecs}
    transcode_video = codec in convertibles or codec not in ("h264", "hevc")
    if transcode_video:
        reason = f"{codec.upper()} is not natively macOS-friendly"
    elif codec == "hevc":
        reason = "HEVC kept as-is (add \"hevc\" to convert_codecs to force H.264)"
    else:
        reason = "already H.264"

    audio = info.audio
    transcode_audio = audio is not None and audio.codec not in ("aac", "alac")
    return Plan(transcode_video, transcode_audio, reason)


# --------------------------------------------------------------------------- convert


@dataclass(slots=True)
class Metadata:
    title: str = ""
    creator: str = ""
    day: date | None = None
    url: str = ""
    description: str = ""

    def as_ffmpeg_args(self) -> list[str]:
        args: list[str] = []
        if self.title:
            args += ["-metadata", f"title={self.title}"]
        if self.creator:
            args += [
                "-metadata", f"artist={self.creator}",
                "-metadata", f"author={self.creator}",
                "-metadata", f"album_artist={self.creator}",
            ]
        if self.day:
            args += [
                "-metadata", f"date={self.day.isoformat()}",
                "-metadata", f"year={self.day.year}",
                "-metadata", f"creation_time={self.day.isoformat()}T12:00:00Z",
            ]
        if self.description:
            args += ["-metadata", f"description={self.description}"]
        if self.url:
            args += ["-metadata", f"comment={self.url}"]
        return args


ProgressCallback = Callable[[float], None]


def build_command(
    source: Path,
    destination: Path,
    info: MediaInfo,
    plan: Plan,
    *,
    encoder: str = "h264_videotoolbox",
    quality: int = 65,
    bitrate: str = "auto",
    audio_bitrate: str = "192k",
    metadata: Metadata | None = None,
    cover: Path | None = None,
    aac_encoder: str = "aac",
) -> list[str]:
    """Assemble the ffmpeg invocation for a remux or a VideoToolbox encode."""
    cmd = [require_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    cmd += ["-i", str(source)]
    if cover is not None:
        cmd += ["-i", str(cover)]

    cmd += ["-map", "0:v:0"]
    if info.audio is not None:
        cmd += ["-map", "0:a:0"]
    if cover is not None:
        cmd += ["-map", "1:v:0"]

    if metadata is not None:
        cmd += ["-map_metadata", "-1"]

    video = info.video
    if plan.transcode_video and video is not None:
        cmd += ["-c:v:0", encoder]
        if encoder == "h264_videotoolbox":
            cmd += ["-allow_sw", "1", "-profile:v:0", "high"]
            if supports_constant_quality() and bitrate == "auto":
                cmd += ["-q:v", str(quality)]
            else:
                target = _target_bitrate(video) if bitrate == "auto" else bitrate
                cmd += ["-b:v", target, "-maxrate", target, "-bufsize", _double(target)]
        else:  # libx264 or anything else the user configured
            cmd += ["-preset", "slow", "-crf", str(max(1, min(51, round((100 - quality) * 0.4))))]
            if bitrate != "auto":
                cmd += ["-maxrate", bitrate, "-bufsize", _double(bitrate)]

        filters = _video_filters(video)
        if filters:
            cmd += ["-filter:v:0", ",".join(filters)]
        cmd += ["-pix_fmt:v:0", "yuv420p"]
        cmd += [
            "-colorspace:v:0", "bt709",
            "-color_primaries:v:0", "bt709",
            "-color_trc:v:0", "bt709",
        ]
        cmd += ["-tag:v:0", "avc1" if encoder.startswith("h264") else "hvc1"]
    else:
        cmd += ["-c:v:0", "copy"]
        if video is not None and video.codec in _MP4_TAGS:
            cmd += ["-tag:v:0", _MP4_TAGS[video.codec]]

    if info.audio is not None:
        if plan.transcode_audio:
            cmd += ["-c:a", aac_encoder, "-b:a", audio_bitrate, "-ac", "2"]
        else:
            cmd += ["-c:a", "copy"]

    if cover is not None:
        cmd += ["-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"]

    if metadata is not None:
        cmd += metadata.as_ffmpeg_args()

    cmd += ["-movflags", "+faststart", "-f", "mp4", str(destination)]
    cmd += ["-progress", "pipe:1", "-nostats"]
    return cmd


def _video_filters(video: VideoStream) -> list[str]:
    filters: list[str] = []
    if video.is_hdr:
        # Tone-map HDR to SDR so colours don't wash out in QuickTime.
        filters.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
        )
    if video.width % 2 or video.height % 2:
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    return filters


def _target_bitrate(video: VideoStream) -> str:
    """~0.1 bits per pixel per second, clamped to something sane."""
    pixels = max(video.width * video.height, 1)
    fps = video.fps if 1 <= video.fps <= 240 else 30.0
    bits = pixels * fps * 0.1
    mbps = max(2.0, min(20.0, bits / 1_000_000))
    return f"{mbps:.1f}M"


def _double(bitrate: str) -> str:
    match = bitrate.strip().upper()
    try:
        if match.endswith("M"):
            return f"{float(match[:-1]) * 2:.1f}M"
        if match.endswith("K"):
            return f"{float(match[:-1]) * 2:.0f}K"
        return str(int(float(match) * 2))
    except ValueError:
        return bitrate


def run_ffmpeg(
    cmd: list[str],
    *,
    duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
    timeout: float = 3600.0,
) -> None:
    """Run ffmpeg, streaming -progress output back as a 0..1 fraction."""
    log.debug("ffmpeg: %s", " ".join(cmd))
    # stderr goes to a temp file rather than a pipe: we only drain stdout while the
    # process runs, and a chatty stderr on a pipe could fill its buffer and deadlock.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors_file:
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=errors_file,
                text=True, bufsize=1, stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ConversionFailed(f"Could not start ffmpeg: {exc}", BREW_HINT) from exc

        assert process.stdout is not None
        try:
            for line in process.stdout:
                if on_progress is None or duration <= 0:
                    continue
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.strip("-").isdigit():
                    on_progress(min(1.0, int(value) / 1_000_000 / duration))
                elif key == "progress" and value == "end":
                    on_progress(1.0)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise ConversionFailed("ffmpeg timed out.") from exc
        finally:
            if process.poll() is None:  # pragma: no cover - defensive
                process.kill()
                process.wait()

        if process.returncode != 0:
            errors_file.seek(0)
            detail = errors_file.read().strip().splitlines()
            log.error("ffmpeg failed (%s): %s", process.returncode, " | ".join(detail[-3:]))
            raise ConversionFailed(
                "ffmpeg failed while writing the MP4.",
                detail[-1] if detail else f"exit code {process.returncode}",
            )


def make_cover_jpeg(source: Path, destination: Path) -> Path | None:
    """Normalise a downloaded thumbnail (often WebP) to a JPEG ffmpeg can attach."""
    executable = tool_path("ffmpeg")
    if not executable or not source.exists():
        return None
    cmd = [
        executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-vf", "scale=min(1280\\,iw):-2", "-frames:v", "1",
        "-q:v", "3", str(destination),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return destination if result.returncode == 0 and destination.exists() else None

"""User configuration stored at ~/.config/media/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .errors import ConfigError

CONFIG_DIR = Path(os.environ.get("MEDIA_CONFIG_DIR") or (Path.home() / ".config" / "media"))
CONFIG_PATH = CONFIG_DIR / "config.toml"
DATA_DIR = Path(os.environ.get("MEDIA_DATA_DIR") or (Path.home() / ".local" / "share" / "media"))
LOG_DIR = DATA_DIR / "logs"

VALID_CODEC_PREFERENCE = ("auto", "h264")
KNOWN_CONVERTIBLE = ("vp9", "av1", "hevc", "vp8", "mpeg4", "theora")


@dataclass(slots=True)
class Config:
    """Effective settings. Every field maps 1:1 to a key in config.toml."""

    # Where files land. "." (the default) means the current working directory.
    download_folder: str = "."
    # "auto"  -> always grab the highest quality, convert afterwards if needed.
    # "h264"  -> prefer streams that are already H.264 so no conversion is needed.
    codec_preference: str = "auto"
    # Video codecs that trigger a re-encode to H.264.
    convert_codecs: list[str] = field(default_factory=lambda: ["vp9", "av1"])
    encoder: str = "h264_videotoolbox"
    # 1-100 constant quality (Apple silicon VideoToolbox). Higher = better.
    quality: int = 65
    # "auto" derives a bitrate from resolution/fps; or set e.g. "8M".
    bitrate: str = "auto"
    audio_bitrate: str = "192k"
    # Remove the pre-conversion file once the H.264 file is written.
    delete_original: bool = True
    # Keep the pre-conversion file next to the result (wins over delete_original).
    preserve_original: bool = False
    overwrite: bool = False
    filename_template: str = "{creator} - {title}"
    strip_emoji: bool = True
    max_filename_length: int = 120
    embed_metadata: bool = True
    embed_thumbnail: bool = True
    # Stamp the file's modification date with the upload date.
    set_file_time: bool = True
    watch_interval: float = 1.0
    notifications: bool = True
    # Browser to pull cookies from for logged-in content, e.g. "safari".
    cookies_from_browser: str = ""
    retries: int = 3
    concurrent_fragments: int = 4

    # ------------------------------------------------------------------ helpers
    @property
    def destination(self) -> Path:
        folder = (self.download_folder or ".").strip()
        if folder in ("", "."):
            return Path.cwd()
        return Path(folder).expanduser().resolve()

    @property
    def keep_original(self) -> bool:
        return self.preserve_original or not self.delete_original

    def validate(self) -> None:
        if self.codec_preference not in VALID_CODEC_PREFERENCE:
            raise ConfigError(
                f"codec_preference must be one of {', '.join(VALID_CODEC_PREFERENCE)} "
                f"(got {self.codec_preference!r})."
            )
        if not 1 <= self.quality <= 100:
            raise ConfigError(f"quality must be between 1 and 100 (got {self.quality}).")
        if self.max_filename_length < 16:
            raise ConfigError("max_filename_length must be at least 16.")
        if self.watch_interval <= 0:
            raise ConfigError("watch_interval must be greater than 0.")
        self.convert_codecs = [c.strip().lower() for c in self.convert_codecs if c.strip()]


DEFAULT_CONFIG_TOML = """\
# Configuration for `media`. Delete any line to fall back to its default.

# Where downloads are written. "." means the current working directory.
download_folder = "."

# "auto" always fetches the highest quality and converts afterwards if needed.
# "h264" prefers streams that are already H.264, avoiding a conversion step.
codec_preference = "auto"

# Video codecs that get re-encoded to H.264 for macOS compatibility.
convert_codecs = ["vp9", "av1"]

# Apple hardware encoder. Use "libx264" for a (slower) software encode.
encoder = "h264_videotoolbox"

# Constant quality, 1-100 (Apple silicon). Higher is better quality.
quality = 65

# "auto" derives a bitrate from resolution and frame rate; or set e.g. "8M".
bitrate = "auto"
audio_bitrate = "192k"

# Remove the temporary VP9/AV1 file after a successful conversion.
delete_original = true
# Keep the pre-conversion file next to the result (overrides delete_original).
preserve_original = false

# Overwrite files that already exist instead of skipping them.
overwrite = false

# Available fields: {creator} {title} {date} {id} {platform} {index}
filename_template = "{creator} - {title}"
strip_emoji = true
max_filename_length = 120

embed_metadata = true
embed_thumbnail = true
set_file_time = true

# Clipboard polling interval for `media watch`, in seconds.
watch_interval = 1.0
notifications = true

# Browser to read cookies from for logged-in content: "safari", "chrome", "firefox", ...
cookies_from_browser = ""

retries = 3
concurrent_fragments = 4
"""


def load_config(path: Path | None = None) -> Config:
    """Load config.toml, falling back to defaults for anything missing."""
    path = path or CONFIG_PATH
    cfg = Config()
    if path.exists():
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"{path} is not valid TOML: {exc}",
                "Fix the syntax or delete the file to regenerate defaults.",
            ) from exc
        _apply(cfg, data, path)
    cfg.validate()
    return cfg


def _apply(cfg: Config, data: dict, path: Path) -> None:
    known = {f.name: f for f in fields(Config)}
    for key, value in data.items():
        spec = known.get(key)
        if spec is None:
            continue  # forward-compatible: ignore keys we don't know
        expected = spec.type
        try:
            if expected == "bool":
                value = bool(value)
            elif expected == "int":
                value = int(value)
            elif expected == "float":
                value = float(value)
            elif expected == "str":
                value = str(value)
            elif expected == "list[str]":
                value = [str(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: {key} has the wrong type ({exc}).") from exc
        setattr(cfg, key, value)


def ensure_config_file(path: Path | None = None) -> Path:
    """Create config.toml with documented defaults if it isn't there yet."""
    path = path or CONFIG_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return path

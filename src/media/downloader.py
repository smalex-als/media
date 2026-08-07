"""yt-dlp driver: metadata extraction and the actual download."""

from __future__ import annotations

import glob
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .errors import (
    DownloadFailed,
    MediaError,
    MissingDependency,
    NoVideoFound,
    classify_download_error,
)
from .logs import get_logger
from .urls import Target

log = get_logger("download")

try:  # yt-dlp is a hard dependency, but we want a friendly message if it's gone.
    import yt_dlp
    from yt_dlp.utils import DownloadError, ExtractorError, UnsupportedError
except ImportError:  # pragma: no cover - exercised only on a broken install
    yt_dlp = None
    DownloadError = ExtractorError = UnsupportedError = Exception


@dataclass(slots=True)
class Progress:
    """A snapshot of one file's download, handed to the UI."""

    status: str            # downloading | finished
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: float = 0.0
    label: str = ""        # "video" / "audio" / "media"


@dataclass(slots=True)
class Item:
    """One downloaded video plus the metadata that came with it."""

    info: dict[str, Any]
    path: Path
    thumbnail: Path | None = None

    @property
    def title(self) -> str:
        return str(self.info.get("title") or "")


@dataclass(slots=True)
class Plan:
    """The result of a metadata-only extraction, before anything is fetched."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    is_playlist: bool = False
    # Carousel slides yt-dlp could not read (usually login-gated or image-only).
    unavailable: int = 0

    @property
    def count(self) -> int:
        return len(self.entries)


ProgressCallback = Callable[[Progress], None]


class _Logger:
    """Route yt-dlp's chatter into our log file instead of the terminal."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.warning(msg)

    def error(self, msg: str) -> None:
        log.error(msg)
        self.errors.append(msg)


def _require_yt_dlp() -> None:
    if yt_dlp is None:
        raise MissingDependency(
            "yt-dlp is not installed in this environment.",
            "Install it with:  pip install -U yt-dlp   (or: media update)",
        )


def format_selector(cfg: Config) -> str:
    """Highest quality by default; H.264-first when the user asked for it."""
    if cfg.codec_preference == "h264":
        return (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "best[vcodec^=avc1]/"
            "bestvideo*+bestaudio/best"
        )
    return "bestvideo*+bestaudio/best"


def base_options(cfg: Config, ydl_logger: _Logger) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": format_selector(cfg),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "noplaylist": False,
        "logger": ydl_logger,
        "retries": cfg.retries,
        "fragment_retries": cfg.retries,
        "extractor_retries": cfg.retries,
        "concurrent_fragment_downloads": max(1, cfg.concurrent_fragments),
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": True,
        "ignoreerrors": False,
        "no_color": True,
        "trim_file_name": 120,
        "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
    }
    if cfg.cookies_from_browser:
        opts["cookiesfrombrowser"] = (cfg.cookies_from_browser.strip().lower(),)
    return opts


def inspect(target: Target, cfg: Config) -> Plan:
    """Fetch metadata only — no media transfer — so we can plan filenames."""
    _require_yt_dlp()
    ydl_logger = _Logger()
    # ignoreerrors keeps a carousel alive when one slide can't be read: yt-dlp
    # leaves a None in its place instead of abandoning the whole post.
    opts = base_options(cfg, ydl_logger) | {"skip_download": True, "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target.url, download=False)
    except (DownloadError, ExtractorError, UnsupportedError, OSError) as exc:
        raise classify_download_error(str(exc), target.platform) from exc

    if not info:
        raise _explain(ydl_logger, target, "Nothing could be extracted from that URL.")
    return _flatten(info, ydl_logger, target)


def _explain(ydl_logger: _Logger, target: Target, fallback: str) -> MediaError:
    """Turn whatever yt-dlp logged into the best error we can offer."""
    if ydl_logger.errors:
        return classify_download_error(ydl_logger.errors[-1], target.platform)
    return NoVideoFound(fallback)


def _flatten(info: dict[str, Any], ydl_logger: _Logger, target: Target) -> Plan:
    """Normalise single videos and carousels/playlists into a list of entries."""
    if info.get("_type") == "playlist" or "entries" in info:
        raw = list(info.get("entries") or [])
        entries = [entry for entry in raw if entry]
        if not entries:
            raise _explain(
                ydl_logger, target,
                "That post has no video in it.",
            )
        return Plan(
            entries=entries,
            is_playlist=len(entries) > 1,
            unavailable=len(raw) - len(entries),
        )
    return Plan(entries=[info], is_playlist=False)


def download(
    target: Target,
    cfg: Config,
    workdir: Path,
    *,
    on_progress: ProgressCallback | None = None,
    playlist_items: str | None = None,
) -> list[Item]:
    """Download to `workdir` and return one Item per video that arrived."""
    _require_yt_dlp()
    ydl_logger = _Logger()
    opts = base_options(cfg, ydl_logger) | {
        "ignoreerrors": True,
        "paths": {"home": str(workdir), "temp": str(workdir)},
        "outtmpl": {
            "default": "%(playlist_index|0)s.%(id).60s.%(ext)s",
            "thumbnail": "%(playlist_index|0)s.%(id).60s.%(ext)s",
        },
        "writethumbnail": cfg.embed_thumbnail,
        "progress_hooks": [_make_hook(on_progress)] if on_progress else [],
    }
    if playlist_items:
        opts["playlist_items"] = playlist_items

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target.url, download=True)
    except (DownloadError, ExtractorError, UnsupportedError, OSError) as exc:
        message = str(exc) or (ydl_logger.errors[-1] if ydl_logger.errors else "")
        raise classify_download_error(message, target.platform) from exc

    if not info:
        raise _explain(ydl_logger, target, "yt-dlp returned no result.")

    items = _collect(info, workdir)
    if not items:
        if ydl_logger.errors:
            raise classify_download_error(ydl_logger.errors[-1], target.platform)
        raise DownloadFailed("The download finished but no media file was produced.")
    return items


def _make_hook(callback: ProgressCallback) -> Callable[[dict[str, Any]], None]:
    def hook(payload: dict[str, Any]) -> None:
        status = payload.get("status")
        if status not in ("downloading", "finished"):
            return
        info = payload.get("info_dict") or {}
        vcodec = str(info.get("vcodec") or "none")
        acodec = str(info.get("acodec") or "none")
        if vcodec != "none" and acodec == "none":
            label = "video"
        elif acodec != "none" and vcodec == "none":
            label = "audio"
        else:
            label = "media"
        callback(
            Progress(
                status=str(status),
                downloaded=int(payload.get("downloaded_bytes") or 0),
                total=int(
                    payload.get("total_bytes")
                    or payload.get("total_bytes_estimate")
                    or 0
                ),
                speed=float(payload.get("speed") or 0.0),
                eta=float(payload.get("eta") or 0.0),
                label=label,
            )
        )

    return hook


_THUMB_SUFFIXES = (".jpg", ".jpeg", ".webp", ".png")


def _collect(info: dict[str, Any], workdir: Path) -> list[Item]:
    """Pull the resulting file path out of yt-dlp's result structure."""
    entries = info.get("entries") if (info.get("_type") == "playlist" or "entries" in info) else None
    sources = [entry for entry in (entries or [info]) if entry]

    items: list[Item] = []
    for entry in sources:
        path = _entry_path(entry)
        if path is None or not path.exists():
            continue
        items.append(Item(info=entry, path=path, thumbnail=_find_thumbnail(entry, path, workdir)))
    return items


def _entry_path(entry: dict[str, Any]) -> Path | None:
    requested = entry.get("requested_downloads") or []
    for download_info in requested:
        for key in ("filepath", "_filename", "filename"):
            value = download_info.get(key)
            if value:
                return Path(value)
    for key in ("filepath", "_filename"):
        value = entry.get(key)
        if value:
            return Path(value)
    return None


def _find_thumbnail(entry: dict[str, Any], media: Path, workdir: Path) -> Path | None:
    for thumb in entry.get("thumbnails") or []:
        value = thumb.get("filepath")
        if value and Path(value).exists():
            return Path(value)
    stem = media.stem
    for suffix in _THUMB_SUFFIXES:
        candidate = workdir / f"{stem}{suffix}"
        if candidate.exists():
            return candidate
    # The muxer may have changed the extension, so match this entry's stem only —
    # a looser prefix would pick up a sibling carousel entry's thumbnail.
    for candidate in sorted(workdir.glob(f"{glob.escape(stem)}.*")):
        if candidate.suffix.lower() in _THUMB_SUFFIXES:
            return candidate
    return None

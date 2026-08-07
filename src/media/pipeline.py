"""The download → probe → convert → finalise pipeline for a single URL."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import ffmpeg, naming
from .config import Config
from .downloader import Item, download, inspect
from .errors import ConversionFailed, MediaError, NoVideoFound
from .logs import get_logger
from .ui import ItemView, Reporter, human_duration, human_size, human_speed
from .urls import Target

log = get_logger("pipeline")

DOWNLOADED = "downloaded"
CONVERTED = "converted"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(slots=True)
class Outcome:
    """What happened to one video."""

    status: str
    url: str
    path: Path | None = None
    converted: bool = False
    reason: str = ""
    source_codec: str = ""
    final_codec: str = ""


@dataclass(slots=True)
class Report:
    """Aggregate of every Outcome in a run."""

    outcomes: list[Outcome] = field(default_factory=list)

    def add(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)

    @property
    def counts(self) -> dict[str, int]:
        return {
            DOWNLOADED: sum(1 for o in self.outcomes if o.status == DOWNLOADED),
            CONVERTED: sum(1 for o in self.outcomes if o.converted),
            SKIPPED: sum(1 for o in self.outcomes if o.status == SKIPPED),
            FAILED: sum(1 for o in self.outcomes if o.status == FAILED),
        }

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(o.url, o.reason) for o in self.outcomes if o.status == FAILED]

    @property
    def ok(self) -> bool:
        return not any(o.status == FAILED for o in self.outcomes)


class Pipeline:
    """Turns a URL into a Finder-friendly MP4."""

    def __init__(self, cfg: Config, reporter: Reporter, destination: Path | None = None) -> None:
        self.cfg = cfg
        self.reporter = reporter
        self.destination = (destination or cfg.destination).expanduser()

    # ------------------------------------------------------------------ public
    def run(self, target: Target) -> list[Outcome]:
        """Process one URL; never raises for expected failures."""
        self.reporter.header(target.kind, target.url)
        try:
            self._ensure_destination()
            with self.reporter.item() as view:
                return self._process(target, view)
        except MediaError as exc:
            self.reporter.error(exc)
            log.error("%s failed: %s", target.url, exc.message)
            return [Outcome(status=FAILED, url=target.url, reason=exc.message)]
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # unexpected — log the traceback, tell the user plainly
            log.exception("Unhandled error for %s", target.url)
            self.reporter.error(exc, "Unexpected error")
            return [Outcome(status=FAILED, url=target.url, reason=str(exc))]

    # ----------------------------------------------------------------- stages
    def _process(self, target: Target, view: ItemView) -> list[Outcome]:
        view.stage("info")
        plan = inspect(target, self.cfg)
        if plan.unavailable:
            total = plan.count + plan.unavailable
            self.reporter.warn(
                f"{plan.unavailable} of {total} items in this post have no video "
                f"(images, or login-gated) — downloading the other {plan.count}."
            )

        planned = self._plan_names(target, plan.entries)
        pending = [entry for entry in planned if not entry["skip"]]
        outcomes: list[Outcome] = []
        for entry in planned:
            if entry["skip"]:
                self.reporter.skipped(entry["path"].name, "already downloaded")
                outcomes.append(
                    Outcome(status=SKIPPED, url=target.url, path=entry["path"],
                            reason="file exists")
                )
        if not pending:
            return outcomes

        workdir = Path(tempfile.mkdtemp(prefix=".media-", dir=self.destination))
        try:
            items = download(
                target,
                self.cfg,
                workdir,
                on_progress=lambda progress: self._on_download(view, progress),
                playlist_items=_playlist_items(planned) if plan.is_playlist else None,
            )
            by_id = {str(entry["entry"].get("id") or ""): entry for entry in pending}
            leftovers = list(pending)
            for item in items:
                entry = by_id.pop(str(item.info.get("id") or ""), None)
                if entry is None:
                    entry = leftovers[0] if leftovers else None
                if entry in leftovers:
                    leftovers.remove(entry)
                destination = entry["path"] if entry else self._target_path(item.info, target)
                # One bad entry in a carousel must not discard the good ones.
                try:
                    outcomes.append(self._finish(target, item, destination, view))
                except MediaError as exc:
                    self.reporter.error(exc, destination.name)
                    log.error("%s (%s) failed: %s", target.url, destination.name, exc.message)
                    outcomes.append(Outcome(status=FAILED, url=target.url, reason=exc.message))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return outcomes

    def _plan_names(self, target: Target, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        multiple = len(entries) > 1
        planned: list[dict[str, Any]] = []
        used: set[Path] = set()
        for position, entry in enumerate(entries, start=1):
            path = self._target_path(
                entry, target, index=position if multiple else None, avoid=used
            )
            used.add(path)
            planned.append(
                {
                    "position": position,
                    "entry": entry,
                    "path": path,
                    "skip": path.exists() and not self.cfg.overwrite,
                }
            )
        return planned

    def _target_path(
        self,
        info: dict[str, Any],
        target: Target,
        *,
        index: int | None = None,
        avoid: set[Path] | None = None,
    ) -> Path:
        """Where this entry should land, avoiding names claimed earlier in the run."""
        stem = naming.build_stem(
            info,
            template=self.cfg.filename_template,
            platform=target.platform,
            remove_emoji=self.cfg.strip_emoji,
            max_length=self.cfg.max_filename_length,
            index=index,
        )
        path = self.destination / f"{stem}.mp4"
        if not avoid:
            return path
        counter = 2
        while path in avoid:
            path = self.destination / f"{stem} ({counter}).mp4"
            counter += 1
        return path

    def _finish(self, target: Target, item: Item, destination: Path, view: ItemView) -> Outcome:
        view.stage("probe")
        info = ffmpeg.probe(item.path)
        if info.video is None:
            raise NoVideoFound(
                f"{item.path.name} has no video stream.",
                "Image-only posts can't be converted to MP4.",
            )

        plan = ffmpeg.plan_for(info, self.cfg.convert_codecs)
        if destination.exists():
            if not self.cfg.overwrite:
                self.reporter.skipped(destination.name, "already downloaded")
                return Outcome(SKIPPED, target.url, destination, reason="file exists")
            destination.unlink()

        source_codec = info.video.codec
        if plan.is_conversion:
            view.set_fraction(0.0, plan.reason, key="convert")
        else:
            view.stage("finalize")

        cover = self._prepare_cover(item)
        metadata = self._metadata(item, target) if self.cfg.embed_metadata else None
        self._write(info, plan, destination, metadata, cover, view)

        final = ffmpeg.probe(destination)
        self._apply_file_time(item, destination)
        self._handle_original(item, destination, plan)

        details = [
            final.resolution,
            human_duration(final.duration),
            final.codec_summary,
            human_size(final.size),
        ]
        if plan.is_conversion:
            details.append(f"converted from {source_codec.upper()}")
        self.reporter.result(destination.name, details)
        log.info(
            "%s -> %s (%s%s)",
            target.url, destination, final.codec_summary,
            f", from {source_codec}" if plan.is_conversion else "",
        )
        return Outcome(
            status=DOWNLOADED,
            url=target.url,
            path=destination,
            converted=plan.is_conversion,
            source_codec=source_codec,
            final_codec=final.video.codec if final.video else "",
        )

    # ---------------------------------------------------------------- helpers
    def _write(
        self,
        info: ffmpeg.MediaInfo,
        plan: ffmpeg.Plan,
        destination: Path,
        metadata: ffmpeg.Metadata | None,
        cover: Path | None,
        view: ItemView,
    ) -> None:
        """Run ffmpeg, degrading gracefully rather than failing the download."""
        started = time.monotonic()
        attempts: list[tuple[Path | None, ffmpeg.Metadata | None, str]] = [
            (cover, metadata, ""),
        ]
        if cover is not None:
            attempts.append((None, metadata, "cover art could not be embedded"))
        if metadata is not None:
            attempts.append((None, None, "metadata could not be embedded"))

        aac_encoder = "aac_at" if ffmpeg.has_encoder("aac_at") else "aac"
        last: ConversionFailed | None = None
        for attempt_cover, attempt_meta, complaint in attempts:
            cmd = ffmpeg.build_command(
                info.path,
                destination,
                info,
                plan,
                encoder=self.cfg.encoder,
                quality=self.cfg.quality,
                bitrate=self.cfg.bitrate,
                audio_bitrate=self.cfg.audio_bitrate,
                metadata=attempt_meta,
                cover=attempt_cover,
                aac_encoder=aac_encoder,
            )
            try:
                ffmpeg.run_ffmpeg(
                    cmd,
                    duration=info.duration,
                    on_progress=(
                        (lambda fraction: view.set_fraction(fraction))
                        if plan.is_conversion
                        else None
                    ),
                )
            except ConversionFailed as exc:
                last = exc
                destination.unlink(missing_ok=True)
                log.warning("ffmpeg attempt failed (%s): %s", complaint or "full", exc.hint or exc)
                continue
            if complaint:
                self.reporter.warn(f"{destination.name}: {complaint}.")
            if plan.is_conversion:
                log.info("converted in %.1fs: %s", time.monotonic() - started, destination.name)
            return

        assert last is not None
        raise last

    def _prepare_cover(self, item: Item) -> Path | None:
        if not self.cfg.embed_thumbnail or item.thumbnail is None:
            return None
        jpeg = item.thumbnail.with_name(f"{item.thumbnail.stem}.cover.jpg")
        return ffmpeg.make_cover_jpeg(item.thumbnail, jpeg)

    def _metadata(self, item: Item, target: Target) -> ffmpeg.Metadata:
        info = item.info
        creator = naming.clean_creator(info)
        title = naming.clean_title(info.get("title"), creator=creator, video_id=str(info.get("id") or ""))
        description = str(info.get("description") or "").strip()
        return ffmpeg.Metadata(
            title=title or (info.get("title") or "").strip(),
            creator=creator,
            day=naming.upload_date(info),
            url=str(info.get("webpage_url") or target.url),
            description=description[:400],
        )

    def _apply_file_time(self, item: Item, destination: Path) -> None:
        if not self.cfg.set_file_time:
            return
        day = naming.upload_date(item.info)
        if day is None:
            return
        stamp = datetime.combine(day, datetime.min.time()).replace(hour=12).timestamp()
        try:
            os.utime(destination, (stamp, stamp))
        except OSError as exc:  # pragma: no cover - filesystem dependent
            log.debug("could not set file time: %s", exc)

    def _handle_original(self, item: Item, destination: Path, plan: ffmpeg.Plan) -> None:
        """Keep the pre-conversion file when asked; otherwise let the temp dir go."""
        if not plan.is_conversion or not self.cfg.keep_original:
            return
        kept = destination.with_name(f"{destination.stem}.original{item.path.suffix}")
        try:
            shutil.copy2(item.path, naming.unique_path(kept))
        except OSError as exc:
            log.warning("could not preserve the original file: %s", exc)

    def _ensure_destination(self) -> None:
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MediaError(
                f"Cannot write to {self.destination}: {exc.strerror or exc}.",
                "Pick another folder with -o, or fix the permissions.",
            ) from exc

    def _on_download(self, view: ItemView, progress) -> None:
        if progress.status == "finished":
            view.stage("merge")
            return
        label = f"Downloading {progress.label}…" if progress.label != "media" else "Downloading…"
        view.set_bytes(progress.downloaded, progress.total, label, human_speed(progress.speed))


def _playlist_items(planned: list[dict[str, Any]]) -> str | None:
    """Ask yt-dlp for only the carousel entries we still need."""
    wanted = [str(entry["position"]) for entry in planned if not entry["skip"]]
    if not wanted or len(wanted) == len(planned):
        return None
    return ",".join(wanted)

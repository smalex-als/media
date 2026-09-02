"""Clipboard watch mode."""

from __future__ import annotations

import time
from pathlib import Path

from .config import Config
from .errors import MediaError
from .logs import get_logger
from .pipeline import DOWNLOADED, SKIPPED, Pipeline, Report
from .system import notify, read_clipboard
from .ui import Reporter
from .urls import Target, find_supported

log = get_logger("watch")


def watch(cfg: Config, reporter: Reporter, destination: Path | None = None) -> Report:
    """Poll the clipboard and download every new supported link that appears."""
    pipeline = Pipeline(cfg, reporter, destination)
    report = Report()
    seen: set[tuple[str, str]] = set()

    # Whatever is already on the clipboard at startup is treated as old, so
    # launching watch mode never re-downloads the last thing you copied.
    previous = read_clipboard()
    for target in find_supported(previous):
        seen.add(target.key)

    folder = pipeline.destination
    reporter.console.print(
        f"[stage]Watching the clipboard[/stage] [muted]→ {folder}[/muted]"
    )
    reporter.console.print("[muted]Copy an Instagram, YouTube or X link. Ctrl-C to stop.[/muted]\n")
    if seen:
        reporter.note(f"Ignoring {len(seen)} link(s) already on the clipboard.")

    try:
        while True:
            time.sleep(cfg.watch_interval)
            current = read_clipboard()
            if current == previous:
                continue
            previous = current
            for target in find_supported(current):
                if target.key in seen:
                    continue
                seen.add(target.key)
                _handle(pipeline, cfg, reporter, report, target)
    except KeyboardInterrupt:
        reporter.console.print("\n[muted]Stopped watching.[/muted]")
    return report


def _handle(
    pipeline: Pipeline, cfg: Config, reporter: Reporter, report: Report, target: Target
) -> None:
    log.info("clipboard: %s", target.url)
    try:
        outcomes = pipeline.run(target)
    except MediaError as exc:  # pragma: no cover - Pipeline.run already absorbs these
        reporter.error(exc)
        notify("Download failed", exc.message, enabled=cfg.notifications)
        return

    for outcome in outcomes:
        report.add(outcome)

    succeeded = [o for o in outcomes if o.status == DOWNLOADED]
    skipped = [o for o in outcomes if o.status == SKIPPED]
    if succeeded:
        first = succeeded[0]
        name = first.path.name if first.path else "video"
        extra = f" (+{len(succeeded) - 1} more)" if len(succeeded) > 1 else ""
        notify(
            "Download complete",
            f"{name}{extra}",
            subtitle="Converted to H.264" if first.converted else target.kind,
            enabled=cfg.notifications,
        )
    elif skipped:
        notify("Already downloaded", skipped[0].path.name if skipped[0].path else target.url,
               enabled=cfg.notifications)
    else:
        failed = [o for o in outcomes if o.reason]
        notify(
            "Download failed",
            failed[0].reason if failed else target.url,
            subtitle=target.kind,
            enabled=cfg.notifications,
        )
    reporter.console.print()

"""Rich-based terminal output: progress, results and summaries."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import Lock

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.theme import Theme

from .errors import MediaError

THEME = Theme(
    {
        "ok": "bold green",
        "warn": "yellow",
        "err": "bold red",
        "muted": "dim",
        "stage": "cyan",
        "name": "bold",
        "kind": "magenta",
    }
)

STAGES = {
    "info": "Fetching info…",
    "download": "Downloading…",
    "merge": "Merging…",
    "probe": "Checking codec…",
    "convert": "Converting…",
    "finalize": "Finalising…",
}


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"  # pragma: no cover - unreachable


def human_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0:00"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return ""
    return f"{human_size(bytes_per_second)}/s"


@dataclass(slots=True)
class ItemView:
    """Handle the pipeline uses to drive one item's progress display.

    A task's total can't be reset back to "unknown" in rich, so each stage gets a
    freshly created task — that keeps the spinner honest between phases.

    yt-dlp calls progress hooks from its fragment worker threads, so every method
    that touches `task_id` holds the lock: recreating a task without it lets one
    thread update an id another has just removed, which rich reports as a bare
    KeyError.
    """

    progress: Progress | None
    task_id: int | None
    console: Console
    quiet: bool = False
    _total: float | None = None
    _description: str = ""
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    @property
    def active(self) -> bool:
        return self.progress is not None and self.task_id is not None

    def stage(self, key: str, detail: str = "") -> None:
        """Switch to an indeterminate phase such as probing or merging."""
        with self._lock:
            self._replace(description=STAGES.get(key, key), total=None, detail=detail)

    def set_fraction(self, fraction: float, detail: str | None = None, key: str = "") -> None:
        """Update a 0..1 phase such as conversion."""
        if not self.active:
            return
        with self._lock:
            if key:
                self._replace(description=STAGES.get(key, key), total=100.0, detail=detail or "")
                return
            self.progress.update(  # type: ignore[union-attr]
                self.task_id, completed=max(0.0, min(1.0, fraction)) * 100
            )
            if detail is not None:
                self.progress.update(self.task_id, detail=detail)  # type: ignore[union-attr]

    def set_bytes(self, done: int, total: int, description: str, detail: str = "") -> None:
        """Update a byte-counted phase such as downloading."""
        if not self.active:
            return
        known_total = float(total) if total > 0 else None
        with self._lock:
            if description != self._description:
                # A new file (video, then audio) — start a fresh bar for it.
                self._replace(
                    description=description, total=known_total, detail=detail, completed=done
                )
                return
            # An HLS total is an estimate that creeps up with every fragment, so it
            # is adjusted in place; recreating the task here is what used to race.
            # rich throws its ETA samples away whenever the total moves, so only a
            # materially different figure is worth pushing.
            drifted = known_total is not None and (
                self._total is None or abs(known_total - self._total) > known_total * 0.01
            )
            if drifted:
                self._total = known_total
            self.progress.update(  # type: ignore[union-attr]
                self.task_id,
                completed=float(done),
                total=known_total if drifted else None,
                description=description,
                detail=detail,
            )

    def _replace(
        self,
        *,
        description: str,
        total: float | None,
        detail: str = "",
        completed: float = 0.0,
    ) -> None:
        if not self.active:
            return
        progress = self.progress
        assert progress is not None and self.task_id is not None
        progress.remove_task(self.task_id)
        self.task_id = progress.add_task(
            description, total=total, completed=completed, detail=detail
        )
        self._total = total
        self._description = description


class Reporter:
    """All user-facing output goes through here, so --quiet works everywhere."""

    def __init__(self, console: Console | None = None, quiet: bool = False) -> None:
        self.console = console or Console(theme=THEME, highlight=False)
        self.quiet = quiet

    # ---------------------------------------------------------------- messages
    def print(self, *args, **kwargs) -> None:
        if not self.quiet:
            self.console.print(*args, **kwargs)

    def note(self, message: str) -> None:
        self.print(f"[muted]{message}[/muted]")

    def warn(self, message: str) -> None:
        self.console.print(f"[warn]![/warn] {message}")

    def error(self, error: BaseException, context: str = "") -> None:
        prefix = f"[err]✗[/err] {context + ': ' if context else ''}"
        if isinstance(error, MediaError):
            self.console.print(f"{prefix}{error.message}")
            if error.hint:
                self.console.print(f"  [muted]{error.hint}[/muted]")
        else:
            self.console.print(f"{prefix}{error}")

    def header(self, target_kind: str, url: str) -> None:
        self.print(f"[kind]{target_kind}[/kind] [muted]{_shorten_url(url)}[/muted]")

    def result(self, name: str, details: list[str]) -> None:
        self.console.print(f"[ok]✓[/ok] [name]{name}[/name]")
        if details and not self.quiet:
            self.console.print(f"  [muted]{' · '.join(details)}[/muted]")

    def skipped(self, name: str, reason: str) -> None:
        self.print(f"[warn]•[/warn] [name]{name}[/name] [muted]— {reason}[/muted]")

    # ---------------------------------------------------------------- progress
    @contextmanager
    def item(self) -> Iterator[ItemView]:
        if self.quiet or not self.console.is_terminal:
            yield ItemView(progress=None, task_id=None, console=self.console, quiet=self.quiet)
            return
        progress = Progress(
            SpinnerColumn(style="stage"),
            TextColumn("[stage]{task.description}"),
            BarColumn(bar_width=24, complete_style="cyan", finished_style="green"),
            TaskProgressColumn(),
            TextColumn("[muted]{task.fields[detail]}"),
            TimeRemainingColumn(compact=True),
            console=self.console,
            transient=True,
            refresh_per_second=12,
        )
        with progress:
            task_id = progress.add_task(STAGES["info"], total=None, detail="")
            yield ItemView(progress=progress, task_id=task_id, console=self.console)

    # ---------------------------------------------------------------- summary
    def summary(self, counts: dict[str, int], failures: list[tuple[str, str]]) -> None:
        table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column(style="bold")
        table.add_column(justify="right")
        rows = [
            ("Downloaded", counts.get("downloaded", 0), "ok"),
            ("Converted", counts.get("converted", 0), "stage"),
            ("Skipped", counts.get("skipped", 0), "warn"),
            ("Failed", counts.get("failed", 0), "err" if counts.get("failed") else "muted"),
        ]
        self.console.print()
        for label, value, style in rows:
            table.add_row(f"[{style}]{label}[/{style}]", str(value))
        self.console.print(table)
        if failures:
            self.console.print()
            for url, reason in failures:
                self.console.print(f"  [err]✗[/err] [muted]{_shorten_url(url)}[/muted] — {reason}")


def _shorten_url(url: str, limit: int = 64) -> str:
    trimmed = url.replace("https://", "").replace("www.", "")
    return trimmed if len(trimmed) <= limit else trimmed[: limit - 1] + "…"

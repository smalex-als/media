"""Command line interface."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperGroup

from . import __version__, library
from .config import Config, ensure_config_file, load_config
from .doctor import doctor, update
from .errors import ConfigError, MediaError
from .logs import setup_logging
from .pipeline import DOWNLOADED, Pipeline, Report
from .system import open_path, read_clipboard, reveal_in_finder
from .ui import Reporter
from .urls import Target, find_supported, require
from .watch import watch as watch_mode

DEFAULT_COMMAND = "get"
_PASSTHROUGH = ("--help", "-h", "--version", "-V")


class DefaultGroup(TyperGroup):
    """Lets `media <URL>` work alongside `media watch`, `media doctor`, …"""

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        if not args:
            args = [DEFAULT_COMMAND]
        elif args[0] not in self.commands and args[0] not in _PASSTHROUGH:
            args = [DEFAULT_COMMAND, *args]
        return super().parse_args(ctx, args)


app = typer.Typer(
    cls=DefaultGroup,
    add_completion=False,
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Download Instagram Reels and YouTube Shorts as Finder-friendly H.264 MP4s.\n\n"
        "Run [bold]media <URL>[/bold], [bold]media urls.txt[/bold], or just "
        "[bold]media[/bold] to use the clipboard."
    ),
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"media {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", callback=_version_callback, is_eager=True,
                     help="Show the version and exit."),
    ] = False,
) -> None:
    """Root options."""


@app.command(
    "get",
    help="Download one or more URLs, a file of URLs, or whatever is on the clipboard.",
)
def get(
    targets: Annotated[
        list[str] | None,
        typer.Argument(metavar="[URL|FILE]...", help="URLs, or a text file with one URL per line."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Destination folder.", show_default=False),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Overwrite files that already exist.")
    ] = False,
    no_convert: Annotated[
        bool, typer.Option("--no-convert", help="Skip the H.264 conversion step.")
    ] = False,
    keep_original: Annotated[
        bool, typer.Option("--keep-original", help="Keep the pre-conversion file.")
    ] = False,
    cookies_from_browser: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", metavar="NAME",
                     help="Use cookies from safari, chrome, firefox, …"),
    ] = None,
    template: Annotated[
        str | None, typer.Option("--template", help="Filename template for this run.")
    ] = None,
    reveal: Annotated[
        bool, typer.Option("--reveal", help="Reveal the finished file in Finder.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only show results.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
) -> None:
    cfg, reporter = _bootstrap(
        quiet=quiet,
        verbose=verbose,
        output=output,
        force=force,
        no_convert=no_convert,
        keep_original=keep_original,
        cookies_from_browser=cookies_from_browser,
        template=template,
    )

    try:
        resolved, from_clipboard = _resolve_targets(targets or [], reporter)
    except MediaError as exc:
        reporter.error(exc)
        raise typer.Exit(2) from exc

    if not resolved:
        raise typer.Exit(1)
    if from_clipboard:
        reporter.note("Using the URL on your clipboard.")

    pipeline = Pipeline(cfg, reporter, output)
    report = Report()
    for index, target in enumerate(resolved):
        if index:
            reporter.print()
        for outcome in pipeline.run(target):
            report.add(outcome)

    if len(resolved) > 1 or report.counts["failed"]:
        reporter.summary(report.counts, report.failures)

    if reveal:
        for outcome in report.outcomes:
            if outcome.status == DOWNLOADED and outcome.path:
                reveal_in_finder(outcome.path)
                break

    raise typer.Exit(0 if report.ok else 1)


@app.command("watch", help="Watch the clipboard and download links as you copy them.")
def watch_command(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Destination folder.", show_default=False)
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Overwrite files that already exist.")
    ] = False,
    cookies_from_browser: Annotated[
        str | None, typer.Option("--cookies-from-browser", metavar="NAME",
                                 help="Use cookies from safari, chrome, firefox, …")
    ] = None,
    no_notifications: Annotated[
        bool, typer.Option("--no-notifications", help="Don't post desktop notifications.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
) -> None:
    cfg, reporter = _bootstrap(
        quiet=False,
        verbose=verbose,
        output=output,
        force=force,
        cookies_from_browser=cookies_from_browser,
    )
    if no_notifications:
        cfg.notifications = False

    report = watch_mode(cfg, reporter, output)
    if report.outcomes:
        reporter.summary(report.counts, report.failures)


@app.command("library", help="Build a browsable HTML page of everything you've downloaded.")
def library_command(
    folder: Annotated[
        Path | None,
        typer.Argument(help="Folder to index. Defaults to your download folder.",
                       show_default=False),
    ] = None,
    include_all: Annotated[
        bool,
        typer.Option("--all", help="Also include videos that have no media metadata."),
    ] = False,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Write the page but don't open it.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
) -> None:
    cfg, reporter = _bootstrap(quiet=False, verbose=verbose)
    target = folder.expanduser().resolve() if folder else cfg.destination

    try:
        total = len(library.candidates(target))
        if not total:
            reporter.warn(f"No videos in {target}.")
            raise typer.Exit(1)

        with reporter.console.status("") as status:
            entries = library.scan(
                target,
                include_untagged=include_all,
                on_progress=lambda done, count, name: status.update(
                    f"[muted]Reading {done}/{count} — {name[:52]}[/muted]"
                ),
            )
        if not entries:
            reporter.warn(f"None of the {total} video(s) in {target} came from media.")
            reporter.console.print("  [muted]Use --all to list them anyway.[/muted]")
            raise typer.Exit(1)

        library.prune_cache(target, entries)
        page = library.build(target, entries)
    except MediaError as exc:
        reporter.error(exc)
        raise typer.Exit(2) from exc

    skipped = total - len(entries)
    reporter.result(
        page.name,
        [f"{len(entries)} video(s)"] + ([f"{skipped} skipped"] if skipped else []),
    )
    reporter.console.print(f"  [muted]{page}[/muted]")

    if not no_open and not open_path(page):
        reporter.warn("Could not open a browser — open the file above yourself.")

    raise typer.Exit(0)


@app.command("doctor", help="Check that ffmpeg, ffprobe and yt-dlp are ready.")
def doctor_command() -> None:
    cfg, reporter = _bootstrap(quiet=False, verbose=False)
    raise typer.Exit(doctor(cfg, reporter))


@app.command("update", help="Update yt-dlp to the latest version.")
def update_command() -> None:
    _, reporter = _bootstrap(quiet=False, verbose=False)
    raise typer.Exit(update(reporter))


@app.command("config", help="Show, create or edit the configuration file.")
def config_command(
    edit: Annotated[bool, typer.Option("--edit", "-e", help="Open the config in $EDITOR.")] = False,
    path_only: Annotated[bool, typer.Option("--path", help="Print the config path and exit.")] = False,
) -> None:
    _, reporter = _bootstrap(quiet=False, verbose=False)
    path = ensure_config_file()
    if path_only:
        typer.echo(str(path))
        raise typer.Exit(0)
    if edit:
        raise typer.Exit(_open_in_editor(path, reporter))

    reporter.console.print(f"[muted]{path}[/muted]\n")
    try:
        reporter.console.print(path.read_text(encoding="utf-8").rstrip())
    except OSError as exc:
        reporter.error(exc, "Could not read the config")
        raise typer.Exit(1) from exc


# --------------------------------------------------------------------------- helpers


def _open_in_editor(path: Path, reporter: Reporter) -> int:
    """Open the config in $VISUAL/$EDITOR, falling back to TextEdit."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    cmd = [*editor.split(), str(path)] if editor else None
    if cmd is None and shutil.which("open"):
        cmd = ["open", "-t", str(path)]
    if cmd is None:
        reporter.warn("Set $EDITOR to edit the config, or open it yourself:")
        reporter.console.print(f"  [muted]{path}[/muted]")
        return 1
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        reporter.error(exc, "Could not open an editor")
        return 1


def _bootstrap(
    *,
    quiet: bool,
    verbose: bool,
    output: Path | None = None,
    force: bool = False,
    no_convert: bool = False,
    keep_original: bool = False,
    cookies_from_browser: str | None = None,
    template: str | None = None,
) -> tuple[Config, Reporter]:
    reporter = Reporter(quiet=quiet)
    setup_logging(verbose)
    try:
        cfg = load_config()
    except ConfigError as exc:
        reporter.error(exc)
        raise typer.Exit(2) from exc

    if output is not None:
        cfg.download_folder = str(output)
    if force:
        cfg.overwrite = True
    if no_convert:
        cfg.convert_codecs = []
    if keep_original:
        cfg.preserve_original = True
    if cookies_from_browser:
        cfg.cookies_from_browser = cookies_from_browser
    if template:
        cfg.filename_template = template

    try:
        cfg.validate()
    except ConfigError as exc:
        reporter.error(exc)
        raise typer.Exit(2) from exc

    ensure_config_file()
    return cfg, reporter


def _resolve_targets(raw: list[str], reporter: Reporter) -> tuple[list[Target], bool]:
    """Expand arguments (URLs and/or list files) or fall back to the clipboard."""
    if not raw:
        return _from_clipboard(reporter), True

    targets: list[Target] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        candidate = Path(item).expanduser()
        if candidate.is_file():
            found = _from_file(candidate, reporter)
        else:
            found = [require(item)]
        for target in found:
            if target.key not in seen:
                seen.add(target.key)
                targets.append(target)
    return targets, False


def _from_file(path: Path, reporter: Reporter) -> list[Target]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MediaError(f"Could not read {path}: {exc.strerror or exc}.") from exc

    targets: list[Target] = []
    unsupported = 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found = find_supported(stripped)
        if found:
            targets.extend(found)
        else:
            unsupported += 1
            reporter.warn(f"{path.name}:{number} — not a supported URL: {stripped[:60]}")

    if not targets:
        raise MediaError(
            f"No supported URLs found in {path.name}.",
            "Expected one Instagram or YouTube link per line.",
        )
    reporter.note(
        f"{len(targets)} URL(s) from {path.name}"
        + (f", {unsupported} line(s) ignored" if unsupported else "")
    )
    return targets


def _from_clipboard(reporter: Reporter) -> list[Target]:
    text = read_clipboard()
    if not text.strip():
        reporter.warn("Nothing on the clipboard.")
        reporter.console.print(
            "  [muted]Copy an Instagram or YouTube link, or run:  media <URL>[/muted]"
        )
        return []
    found = find_supported(text)
    if not found:
        reporter.warn("No Instagram or YouTube link on the clipboard.")
        snippet = " ".join(text.split())[:70]
        if snippet:
            reporter.console.print(f"  [muted]Clipboard starts with: {snippet}…[/muted]")
        return []
    return found


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        Reporter().console.print("\n[muted]Interrupted.[/muted]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()

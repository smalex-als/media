"""`media doctor` and `media update`."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.table import Table

from . import ffmpeg
from .config import CONFIG_PATH, Config
from .logs import log_path
from .ui import Reporter


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


def run_checks(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        version = ffmpeg.tool_version(tool)
        path = ffmpeg.tool_path(tool)
        if version:
            checks.append(Check(tool, True, f"{version}  [dim]{path}[/dim]"))
        else:
            checks.append(
                Check(
                    tool,
                    False,
                    "not found",
                    "brew install ffmpeg" if tool != "yt-dlp" else "pip install -U yt-dlp",
                )
            )

    library = _yt_dlp_library_version()
    checks.append(
        Check(
            "yt-dlp (library)",
            library is not None,
            library or "not importable",
            "" if library else "pip install -U yt-dlp",
        )
    )

    encoder = cfg.encoder
    has = ffmpeg.has_encoder(encoder)
    checks.append(
        Check(
            f"encoder {encoder}",
            has,
            "available" + (" (constant quality)" if has and ffmpeg.supports_constant_quality() else ""),
            "" if has else f"Your ffmpeg lacks {encoder}; set encoder = \"libx264\" in the config.",
        )
    )

    aac = "aac_at" if ffmpeg.has_encoder("aac_at") else "aac"
    checks.append(Check("AAC encoder", True, aac))

    destination = cfg.destination
    writable = _writable(destination)
    checks.append(
        Check(
            "download folder",
            writable,
            f"{destination}",
            "" if writable else "Not writable — change download_folder or use -o.",
        )
    )

    checks.append(
        Check(
            "config",
            True,
            f"{CONFIG_PATH}" + ("" if CONFIG_PATH.exists() else "  [dim](defaults, not created yet)[/dim]"),
        )
    )
    checks.append(Check("logs", True, str(log_path())))
    return checks


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".media-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _yt_dlp_library_version() -> str | None:
    try:
        import yt_dlp  # noqa: PLC0415 - optional at runtime
    except ImportError:
        return None
    version = getattr(yt_dlp, "__version__", None)
    if version:
        return str(version)
    try:  # newer yt-dlp keeps it in a submodule
        from yt_dlp.version import __version__ as submodule_version  # noqa: PLC0415

        return str(submodule_version)
    except ImportError:
        return "unknown"


def doctor(cfg: Config, reporter: Reporter) -> int:
    checks = run_checks(cfg)
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column(width=2)
    table.add_column(style="bold", no_wrap=True)
    table.add_column(overflow="fold")
    for check in checks:
        table.add_row("[ok]✓[/ok]" if check.ok else "[err]✗[/err]", check.name, check.detail)
    reporter.console.print()
    reporter.console.print(table)

    problems = [check for check in checks if not check.ok]
    reporter.console.print()
    if not problems:
        reporter.console.print("[ok]Everything checks out.[/ok] Try:  [bold]media <URL>[/bold]")
        return 0
    for check in problems:
        if check.hint:
            reporter.console.print(f"  [warn]→[/warn] {check.name}: {check.hint}")
    return 1


def update(reporter: Reporter) -> int:
    """Update yt-dlp using whichever installation method is in play."""
    reporter.print("[stage]Updating yt-dlp…[/stage]")
    before = _yt_dlp_library_version() or ffmpeg.tool_version("yt-dlp") or "unknown"

    for label, cmd in _update_commands():
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            after = _reimported_version() or ffmpeg.tool_version("yt-dlp") or "unknown"
            if after == before:
                reporter.console.print(f"[ok]✓[/ok] yt-dlp is already current ([bold]{after}[/bold]).")
            else:
                reporter.console.print(
                    f"[ok]✓[/ok] yt-dlp updated: [muted]{before}[/muted] → [bold]{after}[/bold]"
                )
            reporter.note(f"via {label}")
            return 0

    reporter.console.print("[err]✗[/err] Could not update yt-dlp automatically.")
    reporter.console.print(
        "  [muted]Try one of:  pip install -U yt-dlp   ·   "
        "uv tool upgrade yt-dlp   ·   brew upgrade yt-dlp[/muted]"
    )
    return 1


def _update_commands() -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    executable = shutil.which("yt-dlp")
    # A self-updating standalone binary knows best.
    if executable and "/Cellar/" not in (Path(executable).resolve().as_posix()):
        commands.append(("yt-dlp -U", [executable, "-U"]))
    commands.append(("pip", [sys.executable, "-m", "pip", "install", "-U", "--quiet", "yt-dlp"]))
    if shutil.which("uv"):
        # uv-managed environments have no bundled pip, so target this interpreter.
        commands.append(
            ("uv pip", ["uv", "pip", "install", "--python", sys.executable, "-U", "yt-dlp"])
        )
        commands.append(("uv tool", ["uv", "tool", "upgrade", "yt-dlp"]))
    if shutil.which("brew"):
        commands.append(("homebrew", ["brew", "upgrade", "yt-dlp"]))
    return commands


def _reimported_version() -> str | None:
    """Re-read the version in a clean interpreter so an in-place upgrade shows."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import yt_dlp; print(yt_dlp.__version__)"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None

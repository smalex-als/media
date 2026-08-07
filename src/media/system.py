"""Small macOS integrations: clipboard reads and desktop notifications."""

from __future__ import annotations

import shutil
import subprocess
from contextlib import suppress

from .logs import get_logger

log = get_logger("system")


def read_clipboard() -> str:
    """Clipboard text via pbpaste. Returns "" when it isn't available."""
    executable = shutil.which("pbpaste")
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("pbpaste failed: %s", exc)
        return ""
    return result.stdout or ""


def notify(title: str, message: str, *, subtitle: str = "", enabled: bool = True) -> None:
    """Post a Notification Centre banner. Never raises."""
    if not enabled:
        return
    terminal_notifier = shutil.which("terminal-notifier")
    if terminal_notifier:
        cmd = [terminal_notifier, "-title", title, "-message", message]
        if subtitle:
            cmd += ["-subtitle", subtitle]
    else:
        osascript = shutil.which("osascript")
        if not osascript:
            return
        script = (
            f'display notification {_applescript_string(message)} '
            f'with title {_applescript_string(title)}'
        )
        if subtitle:
            script += f" subtitle {_applescript_string(subtitle)}"
        cmd = [osascript, "-e", script]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("notification failed: %s", exc)


def _applescript_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def reveal_in_finder(path) -> None:
    """Best-effort `open -R`; used by --reveal."""
    executable = shutil.which("open")
    if not executable:
        return
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run([executable, "-R", str(path)], capture_output=True, timeout=10, check=False)

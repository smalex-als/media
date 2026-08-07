"""Human-readable error types and translation of yt-dlp/ffmpeg failures."""

from __future__ import annotations

import re


class MediaError(Exception):
    """Base class for every failure we can explain to a human."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class MissingDependency(MediaError):
    pass


class UnsupportedURL(MediaError):
    pass


class LoginRequired(MediaError):
    pass


class PrivateContent(MediaError):
    pass


class ContentUnavailable(MediaError):
    pass


class RateLimited(MediaError):
    pass


class NetworkError(MediaError):
    pass


class NoVideoFound(MediaError):
    pass


class DownloadFailed(MediaError):
    pass


class ConversionFailed(MediaError):
    pass


class ConfigError(MediaError):
    pass


_COOKIE_HINT = (
    "This post needs a logged-in session. Set [bold]cookies_from_browser[/bold] in "
    "[dim]~/.config/media/config.toml[/dim] (e.g. \"safari\", \"chrome\", \"firefox\") "
    "or pass [bold]--cookies-from-browser safari[/bold]."
)

# Ordered: the first pattern that matches a yt-dlp message wins.
_PATTERNS: list[tuple[str, type[MediaError], str, str | None]] = [
    (
        # Instagram's combined message. Login is the usual cause, so lead with that,
        # and check it before the generic rate-limit pattern below.
        r"rate.?limit reached or login required",
        LoginRequired,
        "Instagram wants a logged-in session (or you've been rate-limited).",
        _COOKIE_HINT + " If cookies are already set, wait a few minutes and retry.",
    ),
    (
        r"(rate.?limit|too many requests|http error 429|please wait a few minutes)",
        RateLimited,
        "Instagram/YouTube is rate-limiting this machine.",
        "Wait a few minutes before retrying. Downloading fewer items at a time helps.",
    ),
    (
        r"(login required|requires authentication|sign in to confirm|use --cookies|"
        r"account cookies|you need to log in|empty media response|rate-limit reached or login required)",
        LoginRequired,
        "This content requires a logged-in session.",
        _COOKIE_HINT,
    ),
    (
        r"(private|only available to (?:approved|its)|restricted video|members-only|"
        r"this post is not available|age.?restricted)",
        PrivateContent,
        "This post is private or restricted.",
        "You must be logged in as an account that can see it — see cookies_from_browser.",
    ),
    (
        r"(video unavailable|has been removed|no longer available|removed by the uploader|"
        r"does not exist|not found|http error 404|content isn't available|deleted)",
        ContentUnavailable,
        "The video was deleted or the link is wrong.",
        None,
    ),
    (
        r"(there's no video|no media found|unsupported url|"
        r"unable to extract shared data|no video could be found)",
        NoVideoFound,
        "No downloadable video was found at that URL.",
        "Image-only Instagram posts have no video track.",
    ),
    (
        r"(unable to download webpage|timed out|timeout|connection reset|connection aborted|"
        r"network is unreachable|temporary failure in name resolution|getaddrinfo|"
        r"remote end closed|connection refused|ssl)",
        NetworkError,
        "Network problem while contacting the site.",
        "Check your connection and try again — partial downloads resume automatically.",
    ),
]


def classify_download_error(raw: str, platform: str = "") -> MediaError:
    """Turn a raw yt-dlp error string into a specific, human-readable error."""
    text = _clean(raw)
    lowered = text.lower()

    # "No video formats found" has two very different causes and the message alone
    # can't tell them apart: an image slide inside a carousel (confirmed in the
    # wild), or a video whose streams need a session. Name both, guess neither.
    if "no video formats found" in lowered:
        if platform == "instagram":
            return NoVideoFound(
                "Instagram offered no playable video for that item.",
                "Usually an image in a carousel. If it should be a video, it may need "
                "a logged-in session — " + _COOKIE_HINT,
            )
        return NoVideoFound(
            "No playable video streams were offered for that URL.",
            "The post may be image-only, or the streams need a logged-in session.",
        )

    for pattern, cls, message, hint in _PATTERNS:
        if re.search(pattern, lowered):
            return cls(message, hint)
    return DownloadFailed(text or "Download failed for an unknown reason.")


def _clean(raw: str) -> str:
    """Strip ANSI codes and yt-dlp's noisy prefixes from an error message."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", raw or "").strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^\[[^\]]+\]\s*[\w.-]+:\s*", "", text)
    text = re.sub(r"\s*;\s*please report this issue.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(text.split())

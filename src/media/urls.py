"""Recognising and normalising the URLs we support."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse, urlunparse

from .errors import UnsupportedURL

INSTAGRAM = "instagram"
YOUTUBE = "youtube"
X = "x"

_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "ddinstagram.com"}
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_X_HOSTS = {
    "x.com", "www.x.com", "mobile.x.com",
    "twitter.com", "www.twitter.com", "mobile.twitter.com",
    # Embed-fixer mirrors people paste from chat apps; same paths, same ids.
    "fxtwitter.com", "vxtwitter.com", "fixupx.com",
}

# path prefix -> human label
_INSTAGRAM_KINDS = {
    "reel": "Instagram Reel",
    "reels": "Instagram Reel",
    "p": "Instagram Post",
    "tv": "Instagram Video",
    "stories": "Instagram Story",
    "share": "Instagram Post",
}

_TRACKING_PARAMS = {
    "igshid", "igsh", "img_index", "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "feature", "si", "pp", "ab_channel", "fbclid", "gclid",
}

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Target:
    """A supported URL, plus what we think it points at."""

    url: str          # normalised, tracking-free
    platform: str     # INSTAGRAM | YOUTUBE | X
    kind: str         # human label, e.g. "Instagram Reel"
    shortcode: str    # post/video id when we can read it from the path

    @property
    def key(self) -> tuple[str, str]:
        """Identity of the underlying video.

        A Short and its youtu.be link normalise to different URLs but are the same
        video, so de-duplication keys on this rather than on the URL string.
        """
        return (self.platform, self.shortcode)

    @property
    def is_carousel_capable(self) -> bool:
        """Instagram posts and X posts may hold several videos; Shorts never do."""
        if self.platform == X:
            return True  # a post can carry up to four media items
        return self.platform == INSTAGRAM and self.kind in ("Instagram Post", "Instagram Video")


def detect(url: str) -> Target | None:
    """Classify a URL, or return None when it isn't something we handle."""
    url = (url or "").strip().strip("<>“”\"'")
    if not url:
        return None
    if "://" not in url:
        bare = r"^(?:www\.)?(?:instagram\.com|youtube\.com|youtu\.be|x\.com|twitter\.com)/"
        if not re.match(bare, url, re.I):
            return None
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()

    if host in _INSTAGRAM_HOSTS:
        return _instagram(parsed)
    if host in _YOUTUBE_HOSTS:
        return _youtube(parsed)
    if host in _X_HOSTS:
        return _x(parsed)
    return None


def require(url: str) -> Target:
    """detect(), but raise a helpful error instead of returning None."""
    target = detect(url)
    if target is None:
        raise UnsupportedURL(
            f"Not an Instagram, YouTube or X link: {url}",
            "Supported: instagram.com/reel|p|tv/…, youtube.com/shorts|watch, youtu.be/…, "
            "x.com/<user>/status/…",
        )
    return target


def find_supported(text: str) -> list[Target]:
    """Pull every supported URL out of a blob of text, keeping order, no duplicates."""
    found: list[Target] = []
    seen: set[tuple[str, str]] = set()
    for match in _URL_RE.findall(text or ""):
        target = detect(match)
        if target and target.key not in seen:
            seen.add(target.key)
            found.append(target)
    return found


def _clean_query(parsed, keep: tuple[str, ...] = ()) -> str:
    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k in keep and k not in _TRACKING_PARAMS
    ]
    return "&".join(f"{k}={v}" for k, v in pairs)


def _instagram(parsed) -> Target | None:
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None

    # /<user>/reel/<code>/ is equivalent to /reel/<code>/
    if len(parts) >= 3 and parts[1] in _INSTAGRAM_KINDS:
        parts = parts[1:]
    head = parts[0].lower()
    if head not in _INSTAGRAM_KINDS or len(parts) < 2:
        return None

    shortcode = parts[1]
    canonical = "reel" if head in ("reel", "reels") else head
    if canonical == "stories":  # /stories/<user>/<id>
        path = "/" + "/".join(parts[:3])
        shortcode = parts[2] if len(parts) > 2 else parts[1]
    else:
        path = f"/{canonical}/{shortcode}/"

    return Target(
        url=urlunparse(("https", "www.instagram.com", path, "", "", "")),
        platform=INSTAGRAM,
        kind=_INSTAGRAM_KINDS[head],
        shortcode=shortcode,
    )


def _youtube(parsed) -> Target | None:
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    if host.endswith("youtu.be"):
        if not parts:
            return None
        return _yt_target(parts[0], "YouTube Video")

    if not parts:
        return None
    head = parts[0].lower()

    if head == "shorts" and len(parts) > 1:
        return _yt_target(parts[1], "YouTube Short")
    if head in ("live", "embed", "v") and len(parts) > 1:
        return _yt_target(parts[1], "YouTube Video")
    if head == "watch":
        video_id = dict(parse_qsl(parsed.query)).get("v")
        if video_id:
            return _yt_target(video_id, "YouTube Video")
    if head == "clip" and len(parts) > 1:
        # Clips can't be rebuilt from an id; keep the original URL intact.
        return Target(
            url=urlunparse(("https", "www.youtube.com", parsed.path, "", "", "")),
            platform=YOUTUBE,
            kind="YouTube Clip",
            shortcode=parts[1],
        )
    return None


def _yt_target(video_id: str, kind: str) -> Target | None:
    video_id = video_id.split("?")[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", video_id):
        return None
    path = f"/shorts/{video_id}" if kind == "YouTube Short" else "/watch"
    query = "" if kind == "YouTube Short" else f"v={video_id}"
    return Target(
        url=urlunparse(("https", "www.youtube.com", path, "", query, "")),
        platform=YOUTUBE,
        kind=kind,
        shortcode=video_id,
    )


def _x(parsed) -> Target | None:
    """x.com/<user>/status/<id>, plus the /i/web/, /statuses/ and mirror variants."""
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None

    # /i/web/status/<id> carries no real handle; everything else is /<user>/status/<id>.
    if parts[0].lower() == "i" and len(parts) > 1 and parts[1].lower() == "web":
        parts = ["i", *parts[2:]]

    user, marker, status_id = "", "", ""
    if len(parts) >= 3 and parts[1].lower() in ("status", "statuses"):
        user, marker, status_id = parts[0], parts[1].lower(), parts[2]
    elif len(parts) >= 2 and parts[0].lower() == "statuses":
        user, marker, status_id = "i", "statuses", parts[1]
    if not marker:
        return None

    status_id = status_id.split("?")[0]
    if not re.fullmatch(r"\d{5,25}", status_id):
        return None

    # Trailing /video/1 or /photo/2 just selects a slide; the post URL covers it.
    handle = user if re.fullmatch(r"[A-Za-z0-9_]{1,15}", user) else "i"
    return Target(
        url=urlunparse(("https", "x.com", f"/{handle}/status/{status_id}", "", "", "")),
        platform=X,
        kind="X Post",
        shortcode=status_id,
    )

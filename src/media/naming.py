"""Turning messy platform metadata into clean, Finder-friendly filenames."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

# Characters macOS/Finder dislike, plus the ones that make shell life painful.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Trailing "[dQw4w9WgXcQ]" style ids that yt-dlp bakes into titles.
_TRAILING_ID = re.compile(r"\s*[\[\(]([A-Za-z0-9_-]{8,20})[\]\)]\s*$")
# A run of hashtags at the end of a caption.
_TRAILING_HASHTAGS = re.compile(r"(?:\s*#[\wÀ-￿]+)+\s*$")
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, symbols, extended-A
    "←-⇿"          # arrows
    "⌀-⏿"          # technical (⌚, ⏰)
    "①-➿"          # enclosed alphanumerics, dingbats
    "⬀-⯿"          # misc symbols and arrows
    "︀-️"          # variation selectors
    "‍"                 # zero-width joiner
    "⃣"                 # combining enclosing keycap
    "]+",
    flags=re.UNICODE,
)
# "someone on Instagram: "the actual caption"" — yt-dlp's Instagram title shape.
_IG_TITLE = re.compile(
    r"^(?P<user>.+?)\s+on\s+(?:Instagram|Threads)\s*:\s*[\"“”'‘’]?(?P<caption>.*?)[\"“”'‘’]?$",
    re.IGNORECASE | re.DOTALL,
)
_PLACEHOLDER_TITLE = re.compile(
    r"^(?:video by [\w.\s-]+|instagram (?:reel|post|video|photo)|reel by .+|untitled|"
    r"video|shorts?|#?shorts)$",
    re.IGNORECASE,
)

MAX_TITLE_WORDS = 12

# Words that make a truncated title read as if it were cut off mid-thought.
_TRAILING_FILLER = {
    "a", "an", "the", "and", "or", "but", "so", "to", "of", "in", "on", "at", "by",
    "for", "from", "with", "as", "is", "are", "was", "were", "be", "my", "your",
    "his", "her", "their", "our", "its", "this", "that", "these", "those", "it",
    "you", "we", "they", "i", "if", "how", "when", "what", "who", "why", "&",
}


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text)


def sanitize(text: str, *, remove_emoji: bool = True, max_length: int = 120) -> str:
    """Make an arbitrary string safe and pleasant as a macOS filename component."""
    if not text:
        return ""
    # NFKC folds the styled Unicode captions love (𝗕𝗢𝗟𝗗, ﬁ, ｗｉｄｅ) down to plain
    # ASCII-ish letters, which is what you want in a filename.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    if remove_emoji:
        text = strip_emoji(text)
    text = _ILLEGAL.sub("", text)
    text = text.replace("／", "-").replace("：", " -")
    # Collapse punctuation runs left behind by the removals.
    text = re.sub(r"\s*[-–—]\s*[-–—]+\s*", " - ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_·|")
    text = _truncate(text, max_length)
    if text.lower() in ("", ".", "..", "con", "aux", "nul"):
        return ""
    return text


def _truncate(text: str, max_length: int) -> str:
    """Shorten on a word boundary, and keep the result under 255 UTF-8 bytes."""
    if len(text) > max_length:
        cut = text[:max_length]
        if " " in cut[max_length // 2 :]:
            cut = cut.rsplit(" ", 1)[0]
        text = drop_trailing_filler(cut.rstrip(" ,.;:-–—"))
    while len(text.encode("utf-8")) > 200:
        text = text[:-1]
    return text.strip()


def drop_trailing_filler(text: str) -> str:
    """Remove dangling words so a cut title doesn't end on "in my" or "and"."""
    words = text.split()
    while len(words) > 2 and words[-1].strip(".,;:!?").lower() in _TRAILING_FILLER:
        words.pop()
    return " ".join(words)


def clean_title(raw: str | None, *, creator: str = "", video_id: str = "") -> str:
    """Strip IDs, boilerplate and hashtag spam out of a platform title."""
    title = (raw or "").strip()
    if not title:
        return ""

    title = _TRAILING_ID.sub("", title).strip()

    match = _IG_TITLE.match(title)
    if match:
        title = match.group("caption").strip()

    title = _TRAILING_HASHTAGS.sub("", title).strip()
    title = re.sub(r"\s*#shorts\b", "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s*\|\s*(?:instagram|youtube|shorts)\s*$", "", title, flags=re.IGNORECASE)

    # A caption is often a paragraph; keep the first sentence or line.
    first_line = next((line.strip() for line in title.splitlines() if line.strip()), "")
    if first_line:
        title = first_line
    sentence = re.split(r"(?<=[.!?])\s+", title)[0].strip()
    if 3 <= len(sentence) < len(title):
        title = sentence
    title = title.rstrip(".!,;: ")

    words = title.split()
    if len(words) > MAX_TITLE_WORDS:
        title = drop_trailing_filler(" ".join(words[:MAX_TITLE_WORDS]))

    if video_id and title.strip().lower() == video_id.lower():
        return ""
    if _PLACEHOLDER_TITLE.match(title.strip()):
        return ""
    if creator and title.strip().lower() == creator.strip().lower():
        return ""
    return title.strip()


def clean_creator(info: dict) -> str:
    """Best available human name for whoever posted it."""
    for key in ("creator", "uploader", "channel", "artist", "uploader_id", "channel_id"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            name = re.sub(r"\s*-\s*Topic$", "", name)
            name = name.lstrip("@")
            if re.fullmatch(r"UC[\w-]{20,}", name):  # raw channel id, not a name
                continue
            return name
    return ""


def upload_date(info: dict) -> date | None:
    raw = info.get("upload_date") or info.get("release_date")
    if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            return None
    stamp = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        try:
            return datetime.fromtimestamp(stamp).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def build_stem(
    info: dict,
    *,
    template: str = "{creator} - {title}",
    platform: str = "",
    remove_emoji: bool = True,
    max_length: int = 120,
    index: int | None = None,
) -> str:
    """Render the filename template, gracefully dropping fields we don't have."""
    video_id = str(info.get("id") or "")
    creator = clean_creator(info)
    title = clean_title(
        info.get("title"), creator=creator, video_id=video_id
    ) or clean_title(info.get("description"), creator=creator, video_id=video_id)
    day = upload_date(info)

    values = {
        "creator": sanitize(creator, remove_emoji=remove_emoji, max_length=60),
        "title": sanitize(title, remove_emoji=remove_emoji, max_length=max_length),
        "date": day.isoformat() if day else "",
        "id": sanitize(video_id, remove_emoji=True, max_length=40),
        "platform": platform,
        "index": f"{index:02d}" if index is not None else "",
    }

    stem = _render(template, values)
    if not stem:
        # Nothing usable in the template — build something reasonable anyway.
        fallback = " - ".join(p for p in (values["creator"], values["date"] or values["id"]) if p)
        stem = fallback or f"{platform or 'video'} {datetime.now():%Y-%m-%d %H%M%S}".strip()

    if index is not None and "{index}" not in template:
        stem = f"{stem} ({index})"
    return sanitize(stem, remove_emoji=remove_emoji, max_length=max_length + 40) or "video"


def _render(template: str, values: dict[str, str]) -> str:
    """Substitute fields, then tidy separators orphaned by empty values."""
    try:
        rendered = template.format_map(_Missing(values))
    except (ValueError, IndexError):
        rendered = " - ".join(p for p in (values["creator"], values["title"]) if p)
    rendered = re.sub(r"\s*-\s*(?=\s*-|$)", "", rendered)
    rendered = re.sub(r"^\s*-\s*", "", rendered)
    rendered = re.sub(r"\s{2,}", " ", rendered)
    return rendered.strip(" -_·")


class _Missing(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - trivial
        return ""


def unique_path(path: Path) -> Path:
    """Return `path`, or `name (2).mp4`, `name (3).mp4`, … if it's taken."""
    if not path.exists():
        return path
    for counter in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} ({datetime.now():%Y%m%d%H%M%S}){path.suffix}")

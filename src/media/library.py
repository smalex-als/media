"""Builds a browsable HTML index of a download folder."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from .errors import MediaError
from .ffmpeg import MediaInfo, probe, tool_path
from .logs import get_logger
from .ui import human_duration, human_size

log = get_logger("library")

VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv")
#: Thumbnails and other generated bits live here, next to the videos.
CACHE_DIR_NAME = ".media-library"
PAGE_NAME = "library.html"
THUMB_HEIGHT = 400

#: Called as (done, total, filename) while scanning.
Progress = Callable[[int, int, str], None]


@dataclass(slots=True)
class Entry:
    """One video, as the page needs it."""

    path: Path
    title: str
    creator: str
    day: date | None
    url: str
    info: MediaInfo
    thumb: Path | None
    mtime: float

    @property
    def is_from_media(self) -> bool:
        """True when this file carries the tags the download pipeline writes."""
        return self.url.startswith(("http://", "https://"))

    def as_json(self, folder: Path) -> dict:
        return {
            "file": _relative_url(self.path, folder),
            "thumb": _relative_url(self.thumb, folder) if self.thumb else "",
            "title": self.title,
            "creator": self.creator,
            "date": self.day.isoformat() if self.day else "",
            "url": self.url,
            "path": str(self.path),
            "duration": round(self.info.duration, 2),
            "durationText": human_duration(self.info.duration) if self.info.duration else "",
            "size": self.info.size,
            "sizeText": human_size(self.info.size) if self.info.size else "",
            "resolution": self.info.resolution,
            "codec": self.info.codec_summary,
            "vertical": bool(
                self.info.video and self.info.video.height > self.info.video.width
            ),
            "mtime": self.mtime,
        }


# --------------------------------------------------------------------------- scanning


def candidates(folder: Path) -> list[Path]:
    """Video files directly inside `folder`, in a stable order."""
    if not folder.is_dir():
        raise MediaError(
            f"{folder} is not a folder.",
            "Point media library at the folder your downloads land in.",
        )
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in VIDEO_SUFFIXES
    )


def scan(
    folder: Path,
    *,
    include_untagged: bool = False,
    on_progress: Progress | None = None,
) -> list[Entry]:
    """Describe every video in `folder`. Probing and thumbnailing happen here."""
    files = candidates(folder)
    entries: list[Entry] = []
    cache = folder / CACHE_DIR_NAME

    for index, path in enumerate(files, start=1):
        if on_progress:
            on_progress(index, len(files), path.name)
        entry = _describe(path)
        if entry is None:
            continue
        if not (entry.is_from_media or include_untagged):
            continue
        # Only now, once the file has earned a place on the page, pay for a frame.
        entry.thumb = make_thumbnail(path, cache, duration=entry.info.duration)
        entries.append(entry)

    entries.sort(key=lambda item: item.mtime, reverse=True)
    return entries


def _describe(path: Path) -> Entry | None:
    try:
        info = probe(path)
    except MediaError as exc:  # unreadable or not really a video — skip it
        log.debug("skipping %s: %s", path.name, exc)
        return None

    tags = info.tags
    creator = tags.get("artist") or tags.get("author") or tags.get("album_artist") or ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return Entry(
        path=path,
        title=tags.get("title") or path.stem,
        creator=creator,
        day=_tag_date(tags),
        url=(tags.get("comment") or "").strip(),
        info=info,
        thumb=None,
        mtime=mtime,
    )


def _tag_date(tags: dict[str, str]) -> date | None:
    """The pipeline writes `date=YYYY-MM-DD`; other tools write fuller stamps."""
    for key in ("date", "creation_time", "year"):
        raw = (tags.get(key) or "").strip()
        if not raw:
            continue
        for text in (raw[:10], raw[:4]):
            try:
                return date.fromisoformat(text) if len(text) == 10 else date(int(text), 1, 1)
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------- thumbnails


def make_thumbnail(source: Path, cache: Path, *, duration: float = 0.0) -> Path | None:
    """Pull a poster frame into the cache folder. Reuses whatever is still fresh."""
    executable = tool_path("ffmpeg")
    if not executable:
        return None

    # Hashed suffix keeps the name unique and stable across runs (hash() is salted).
    digest = hashlib.sha1(source.name.encode("utf-8")).hexdigest()[:8]
    destination = cache / f"{_slug(source.stem)}-{digest}.jpg"
    try:
        if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
            return destination
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("thumbnail cache unavailable: %s", exc)
        return None

    # A little way in, so we don't land on a black first frame.
    offset = min(1.5, duration * 0.1) if duration else 0.0
    cmd = [
        executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{offset:.2f}", "-i", str(source),
        # 0:V:0 is the first *real* video stream, skipping any attached cover art.
        "-map", "0:V:0", "-frames:v", "1",
        "-vf", f"scale=-2:{THUMB_HEIGHT}", "-q:v", "4", str(destination),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("thumbnail failed for %s: %s", source.name, exc)
        return None
    if result.returncode != 0 or not destination.exists():
        log.debug("thumbnail failed for %s: %s", source.name, (result.stderr or "").strip())
        return None
    return destination


def _slug(stem: str) -> str:
    """Keep cache filenames short and free of anything awkward in a URL."""
    return re.sub(r"[^\w.-]+", "-", stem, flags=re.UNICODE).strip("-")[:48] or "video"


def prune_cache(folder: Path, entries: list[Entry]) -> int:
    """Drop thumbnails whose video is gone. Returns how many were removed."""
    cache = folder / CACHE_DIR_NAME
    if not cache.is_dir():
        return 0
    keep = {entry.thumb for entry in entries if entry.thumb}
    removed = 0
    for stale in cache.glob("*.jpg"):
        if stale not in keep:
            try:
                stale.unlink()
                removed += 1
            except OSError:  # pragma: no cover - racy filesystem
                pass
    return removed


# --------------------------------------------------------------------------- rendering


def build(folder: Path, entries: list[Entry]) -> Path:
    """Write library.html into `folder` and return its path."""
    payload = [entry.as_json(folder) for entry in entries]
    page = _PAGE.replace("__DATA__", _embed(payload))
    page = page.replace("__FOLDER__", _escape(str(folder)))
    page = page.replace("__COUNT__", str(len(entries)))
    page = page.replace("__BUILT__", datetime.now().strftime("%d.%m.%Y %H:%M"))

    destination = folder / PAGE_NAME
    try:
        destination.write_text(page, encoding="utf-8")
    except OSError as exc:
        raise MediaError(f"Could not write {destination}: {exc.strerror or exc}.") from exc
    return destination


def _relative_url(path: Path, folder: Path) -> str:
    try:
        relative = path.relative_to(folder)
    except ValueError:
        return quote(str(path))
    return quote(relative.as_posix())


def _embed(payload: list[dict]) -> str:
    """JSON for a <script> block: the closing tag must not appear literally."""
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>media — библиотека</title>
<style>
  :root {
    --bg: #f6f6f7; --card: #fff; --text: #16161a; --muted: #6b6b75;
    --line: #e3e3e7; --accent: #2f6feb; --shadow: 0 1px 3px rgba(0,0,0,.08);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #121215; --card: #1c1c21; --text: #ececf0; --muted: #9a9aa5;
      --line: #2c2c33; --accent: #5b8dfb; --shadow: 0 1px 3px rgba(0,0,0,.4);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 5; background: var(--bg);
    border-bottom: 1px solid var(--line); padding: 18px 24px 14px;
  }
  h1 { margin: 0 0 2px; font-size: 19px; font-weight: 600; letter-spacing: -.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  .sub code { font-size: 12px; }
  .controls { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  input[type=search], select {
    font: inherit; font-size: 14px; color: var(--text); background: var(--card);
    border: 1px solid var(--line); border-radius: 8px; padding: 7px 11px;
  }
  input[type=search] { flex: 1; min-width: 220px; }
  input[type=search]:focus, select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  main { padding: 22px 24px 60px; }
  .grid {
    display: grid; gap: 20px; align-items: start;
    grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    overflow: hidden; box-shadow: var(--shadow); cursor: pointer;
    transition: transform .12s ease, box-shadow .12s ease;
  }
  .card:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,.14); }
  .poster {
    position: relative; background: #000; aspect-ratio: 9 / 16;
    display: flex; align-items: center; justify-content: center;
  }
  .card.wide .poster { aspect-ratio: 16 / 9; }
  .poster img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .poster .missing { color: var(--muted); font-size: 12px; }
  .badge {
    position: absolute; right: 7px; bottom: 7px; background: rgba(0,0,0,.75);
    color: #fff; font-size: 11px; font-variant-numeric: tabular-nums;
    padding: 2px 6px; border-radius: 5px;
  }
  .play {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    opacity: 0; transition: opacity .12s ease;
  }
  .card:hover .play { opacity: 1; }
  .play span {
    width: 46px; height: 46px; border-radius: 50%; background: rgba(0,0,0,.6);
    display: flex; align-items: center; justify-content: center;
  }
  .play svg { fill: #fff; margin-left: 3px; }
  .meta { padding: 10px 12px 12px; }
  .title {
    font-size: 13.5px; font-weight: 550; line-height: 1.35; margin: 0 0 3px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .creator { font-size: 12.5px; color: var(--muted); }
  .facts {
    font-size: 11.5px; color: var(--muted); margin-top: 6px;
    font-variant-numeric: tabular-nums;
  }
  .empty { color: var(--muted); padding: 40px 0; text-align: center; }

  dialog {
    border: none; border-radius: 14px; padding: 0; background: var(--card); color: var(--text);
    max-width: min(96vw, 900px); box-shadow: 0 20px 60px rgba(0,0,0,.45);
  }
  dialog::backdrop { background: rgba(0,0,0,.72); }
  dialog video { display: block; max-height: 76vh; max-width: 100%; background: #000; }
  .player-meta { padding: 13px 16px 15px; }
  .player-meta h2 { margin: 0 0 3px; font-size: 15px; font-weight: 600; }
  .actions { display: flex; gap: 8px; margin-top: 11px; flex-wrap: wrap; }
  .actions a, .actions button {
    font: inherit; font-size: 13px; text-decoration: none; cursor: pointer;
    color: var(--text); background: transparent; border: 1px solid var(--line);
    border-radius: 7px; padding: 5px 11px;
  }
  .actions a:hover, .actions button:hover { border-color: var(--accent); color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>Библиотека</h1>
  <div class="sub"><span id="shown">__COUNT__</span> из __COUNT__ · <code>__FOLDER__</code> · собрано __BUILT__</div>
  <div class="controls">
    <input type="search" id="q" placeholder="Поиск по названию, автору, файлу…" autocomplete="off">
    <select id="sort">
      <option value="new">Сначала новые</option>
      <option value="old">Сначала старые</option>
      <option value="long">Сначала длинные</option>
      <option value="short">Сначала короткие</option>
      <option value="big">Сначала большие</option>
      <option value="title">По названию</option>
      <option value="creator">По автору</option>
    </select>
  </div>
</header>

<main><div class="grid" id="grid"></div><div class="empty" id="empty" hidden>Ничего не найдено.</div></main>

<dialog id="player">
  <video id="video" controls playsinline></video>
  <div class="player-meta">
    <h2 id="p-title"></h2>
    <div class="creator" id="p-creator"></div>
    <div class="facts" id="p-facts"></div>
    <div class="actions">
      <a id="p-source" href="#" target="_blank" rel="noreferrer noopener">Источник ↗</a>
      <button id="p-copy" type="button">Скопировать путь</button>
      <button id="p-close" type="button">Закрыть</button>
    </div>
  </div>
</dialog>

<script id="items" type="application/json">__DATA__</script>
<script>
const ITEMS = JSON.parse(document.getElementById('items').textContent);
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const shown = document.getElementById('shown');
const dialog = document.getElementById('player');
const video = document.getElementById('video');

const PLAY_ICON = '<svg width="17" height="17" viewBox="0 0 16 16"><path d="M3 1.8v12.4L14 8z"/></svg>';

const SORTS = {
  new:     (a, b) => b.mtime - a.mtime,
  old:     (a, b) => a.mtime - b.mtime,
  long:    (a, b) => b.duration - a.duration,
  short:   (a, b) => a.duration - b.duration,
  big:     (a, b) => b.size - a.size,
  title:   (a, b) => a.title.localeCompare(b.title, 'ru'),
  creator: (a, b) => (a.creator || '\\uffff').localeCompare(b.creator || '\\uffff', 'ru'),
};

function facts(item) {
  return [item.date, item.resolution, item.codec, item.sizeText].filter(Boolean).join(' · ');
}

function render() {
  const needle = document.getElementById('q').value.trim().toLowerCase();
  const rows = ITEMS
    .filter(item => !needle || [item.title, item.creator, item.file]
      .join(' ').toLowerCase().includes(needle))
    .sort(SORTS[document.getElementById('sort').value]);

  grid.replaceChildren(...rows.map(item => {
    const card = document.createElement('div');
    card.className = 'card' + (item.vertical ? '' : ' wide');
    const poster = item.thumb
      ? `<img src="${item.thumb}" alt="" loading="lazy">`
      : '<span class="missing">нет превью</span>';
    card.innerHTML =
      `<div class="poster">${poster}` +
      `<div class="play"><span>${PLAY_ICON}</span></div>` +
      (item.durationText ? `<div class="badge">${item.durationText}</div>` : '') +
      `</div><div class="meta"><p class="title"></p>` +
      `<div class="creator"></div><div class="facts"></div></div>`;
    card.querySelector('.title').textContent = item.title;
    card.querySelector('.creator').textContent = item.creator;
    card.querySelector('.facts').textContent = facts(item);
    card.addEventListener('click', () => openPlayer(item));
    return card;
  }));

  shown.textContent = rows.length;
  empty.hidden = rows.length > 0;
}

function openPlayer(item) {
  video.src = item.file;
  document.getElementById('p-title').textContent = item.title;
  document.getElementById('p-creator').textContent = item.creator;
  document.getElementById('p-facts').textContent = facts(item);
  const source = document.getElementById('p-source');
  source.hidden = !item.url;
  if (item.url) source.href = item.url;
  document.getElementById('p-copy').onclick = event => copyPath(item.path, event.target);
  dialog.showModal();
  video.play().catch(() => {});
}

function copyPath(path, button) {
  const done = () => { button.textContent = 'Скопировано'; setTimeout(() => {
    button.textContent = 'Скопировать путь';
  }, 1400); };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(path).then(done, () => fallback(path, done));
  } else {
    fallback(path, done);
  }
}

function fallback(path, done) {
  const field = document.createElement('textarea');
  field.value = path;
  document.body.appendChild(field);
  field.select();
  try { document.execCommand('copy'); done(); } catch (err) { prompt('Путь к файлу:', path); }
  field.remove();
}

dialog.addEventListener('close', () => { video.pause(); video.removeAttribute('src'); video.load(); });
document.getElementById('p-close').addEventListener('click', () => dialog.close());
document.getElementById('q').addEventListener('input', render);
document.getElementById('sort').addEventListener('change', render);
render();
</script>
</body>
</html>
"""

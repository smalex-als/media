# media

Download Instagram Reels and YouTube Shorts on macOS and get back a file that just
works — H.264 + AAC in MP4, with a Finder thumbnail, Quick Look preview, QuickTime
playback and clean metadata.

```bash
media https://www.instagram.com/reel/Dac9ebRiOCF/
```

```
Instagram Reel instagram.com/reel/Dac9ebRiOCF/
✓ John Smith - Squat Tutorial.mp4
  1080×1920 · 0:47 · H.264/AAC · 12.4 MB · converted from VP9
```

That's the whole workflow. No format flags, no codec inspection, no manual merging,
no `ffmpeg` incantations.

## What it does

1. Detects whether the URL is an Instagram Reel, Post, Carousel, YouTube Short or
   regular YouTube video.
2. Downloads the highest quality streams with `yt-dlp` and merges video + audio.
3. Runs `ffprobe` to read the real codec.
4. If the video is VP9 or AV1, converts it to H.264 with Apple's hardware encoder
   (`h264_videotoolbox`). Audio is copied untouched whenever it's already AAC.
5. Writes a `+faststart` MP4 tagged `avc1`, embeds title/creator/date/source URL and
   the poster frame, and stamps the file's date with the upload date.
6. Deletes the temporary pre-conversion file.

## Install

Requires macOS, Python 3.12+, and `ffmpeg`:

```bash
brew install ffmpeg
```

Then install the tool itself (uv or pipx both work):

```bash
uv tool install --from . media
```

```bash
pipx install .
```

Verify everything is wired up:

```bash
media doctor
```

## Usage

### One URL

```bash
media https://youtube.com/shorts/dQw4w9WgXcQ
```

### Into a specific folder

```bash
media https://www.instagram.com/reel/Dac9ebRiOCF/ -o ~/Videos
```

### From the clipboard (the fast path)

Copy a link in Safari or the Instagram app, then just:

```bash
media
```

With no arguments it reads the clipboard, and downloads if it holds a supported link.

### Batch

```bash
media urls.txt
```

One URL per line; `#` comments and blank lines are ignored. Every URL is attempted,
failures don't stop the run, and you get a summary:

```
Downloaded  24
Converted   19
Skipped      2
Failed       1
```

Several URLs on one command line work too:

```bash
media https://youtube.com/shorts/aaa https://www.instagram.com/reel/bbb/
```

### Carousels

An Instagram post can hold several videos. Point at the post and every video in it
is downloaded, numbered in order:

```bash
media "https://www.instagram.com/p/DYzI3FYimEw/"
```

```
! 1 of 6 items in this post could not be read — downloading the other 5.
✓ Nikola Janković - 5 Upper Back Exercises to Fix Your Back Hump (1).mp4
✓ Nikola Janković - 5 Upper Back Exercises to Fix Your Back Hump (2).mp4
…
```

Slides that can't be read (image-only, or login-gated) are counted and skipped
rather than failing the whole post. The `?img_index=` fragment in a copied link is
ignored — it selects a slide in the web UI but carries no meaning for downloading.
Quote URLs containing `&` so your shell doesn't split them.

### Watch mode

```bash
media watch
```

Monitors the clipboard and downloads any supported link the moment you copy it,
with a Notification Centre banner when the file lands. The same URL is never
downloaded twice in a session, and files that already exist are skipped.

### Maintenance

```bash
media doctor     # check ffmpeg / ffprobe / yt-dlp, versions, encoder, config
media update     # update yt-dlp in place
media config     # show the effective configuration and where it lives
media config --edit
```

## Options

| Flag | Meaning |
| --- | --- |
| `-o, --output DIR` | Destination folder (default: current directory) |
| `-f, --force` | Overwrite existing files instead of skipping them |
| `--no-convert` | Leave VP9/AV1 alone; download and remux only |
| `--keep-original` | Keep the pre-conversion file as `name.original.webm` |
| `--cookies-from-browser NAME` | Use cookies from `safari`, `chrome`, `firefox`, … for logged-in posts |
| `--template TEXT` | Filename template for this run |
| `--reveal` | Reveal the finished file in Finder |
| `-q, --quiet` / `-v, --verbose` | Less / more output |

## Configuration

`~/.config/media/config.toml` is created on first run:

```toml
download_folder = "."
codec_preference = "auto"        # "auto" = best quality, convert after; "h264" = avoid converting
convert_codecs = ["vp9", "av1"]  # add "hevc" to force H.264 for HEVC too
encoder = "h264_videotoolbox"
quality = 65                     # 1-100 constant quality (Apple silicon)
bitrate = "auto"                 # or "8M"
audio_bitrate = "192k"
delete_original = true
preserve_original = false
overwrite = false
filename_template = "{creator} - {title}"
strip_emoji = true
max_filename_length = 120
embed_metadata = true
embed_thumbnail = true
set_file_time = true
watch_interval = 1.0
notifications = true
cookies_from_browser = ""
retries = 3
concurrent_fragments = 4
```

Template fields: `{creator}`, `{title}`, `{date}`, `{id}`, `{platform}`, `{index}`.
Empty fields and their separators are dropped automatically, so
`"{creator} - {title}"` degrades to just the creator when a video has no usable title.

## Filenames

Platform titles are messy, so they get cleaned up:

| From | To |
| --- | --- |
| `Video by John Smith [Dac9ebRiOCF].mp4` | `John Smith - Squat Tutorial.mp4` |
| `johnsmith on Instagram: "Squat tutorial 🏋️ #gym #fitness"` | `John Smith - Squat tutorial.mp4` |
| `How to squat properly #shorts [dQw4w9WgXcQ]` | `Fitness Channel - How to squat properly.mp4` |

Trailing IDs, hashtag spam, `#shorts`, emoji (optional) and characters Finder
dislikes are all removed; long captions are cut at a word boundary.

## Private and logged-in content

Instagram increasingly requires a session. Point the tool at a browser you're logged
into:

```bash
media https://www.instagram.com/reel/xxxx/ --cookies-from-browser safari
```

or set `cookies_from_browser = "safari"` in the config to make it permanent.

## Logs

`~/.local/share/media/logs/media.log`, rotated at 2 MB × 5. Every download,
conversion and failure is timestamped there; `media doctor` prints the path.

## Why the conversion matters

YouTube serves its best quality as VP9 or AV1, which macOS won't thumbnail in Finder
and QuickTime often won't play at all. Converting to H.264 with `h264_videotoolbox`
is hardware-accelerated — typically a few seconds for a Short — and the result plays
everywhere, including iPhone and iPad. HEVC is left alone by default (macOS handles
it natively) but is tagged `hvc1` so QuickTime accepts it; add `"hevc"` to
`convert_codecs` if you want H.264 unconditionally.

## Development

```bash
uv sync --extra dev
uv run pytest
```

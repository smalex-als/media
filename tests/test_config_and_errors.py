from pathlib import Path

import pytest

from media import errors
from media.config import DEFAULT_CONFIG_TOML, Config, ensure_config_file, load_config
from media.downloader import format_selector


def test_defaults_are_sensible():
    cfg = Config()
    assert cfg.convert_codecs == ["vp9", "av1"]
    assert cfg.encoder == "h264_videotoolbox"
    assert cfg.destination == Path.cwd()
    assert cfg.keep_original is False


def test_load_config_overrides_only_what_is_present(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'download_folder = "~/Videos"\nquality = 80\nconvert_codecs = ["vp9", "av1", "hevc"]\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.destination == Path.home() / "Videos"
    assert cfg.quality == 80
    assert cfg.convert_codecs == ["vp9", "av1", "hevc"]
    assert cfg.filename_template == "{creator} - {title}"  # untouched default


def test_missing_config_file_yields_defaults(tmp_path):
    assert load_config(tmp_path / "absent.toml") == Config()


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('future_option = true\nquality = 50\n', encoding="utf-8")
    assert load_config(path).quality == 50


def test_invalid_toml_is_explained(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("quality = = 3", encoding="utf-8")
    with pytest.raises(errors.ConfigError) as caught:
        load_config(path)
    assert "not valid TOML" in caught.value.message


def test_out_of_range_values_are_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("quality = 500", encoding="utf-8")
    with pytest.raises(errors.ConfigError):
        load_config(path)

    path.write_text('codec_preference = "vp9"', encoding="utf-8")
    with pytest.raises(errors.ConfigError):
        load_config(path)


def test_preserve_original_wins_over_delete_original():
    assert Config(preserve_original=True, delete_original=True).keep_original is True
    assert Config(delete_original=False).keep_original is True


def test_shipped_default_config_parses_and_matches_the_dataclass(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    assert load_config(path) == Config()


def test_ensure_config_file_creates_it_once(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    ensure_config_file(path)
    assert path.exists()
    path.write_text("quality = 42", encoding="utf-8")
    ensure_config_file(path)
    assert path.read_text(encoding="utf-8") == "quality = 42"  # not clobbered


def test_format_selector_reflects_codec_preference():
    assert format_selector(Config()) == "bestvideo*+bestaudio/best"
    assert "avc1" in format_selector(Config(codec_preference="h264"))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: [Instagram] xyz: Requested content is not available, rate-limit reached or login required",
         errors.LoginRequired),
        ("ERROR: HTTP Error 429: Too Many Requests", errors.RateLimited),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
         errors.ContentUnavailable),
        ("ERROR: [Instagram] abc: This post is private", errors.PrivateContent),
        ("ERROR: Unable to download webpage: <urlopen error timed out>", errors.NetworkError),
        ("ERROR: [Instagram] abc: There's no video in this post", errors.NoVideoFound),
        ("ERROR: Sign in to confirm you're not a bot", errors.LoginRequired),
        ("ERROR: something nobody has ever seen", errors.DownloadFailed),
    ],
)
def test_yt_dlp_errors_are_classified(message, expected):
    error = errors.classify_download_error(message)
    assert isinstance(error, expected)
    assert error.message
    assert not error.message.startswith("ERROR:")


def test_no_video_formats_names_both_causes():
    # Observed in the wild: an image slide in a video carousel produces this, and
    # so does a login-gated video. The message must not assert either one alone.
    raw = "ERROR: [Instagram] DYzIvwMqR1i: No video formats found!"
    instagram = errors.classify_download_error(raw, "instagram")
    assert isinstance(instagram, errors.NoVideoFound)
    hint = (instagram.hint or "").lower()
    assert "image" in hint and "cookies_from_browser" in hint

    generic = errors.classify_download_error(raw, "youtube")
    assert isinstance(generic, errors.NoVideoFound)
    assert "image" in (generic.hint or "").lower()


def test_classification_strips_noise():
    error = errors.classify_download_error(
        "\x1b[0;31mERROR:\x1b[0m [generic] test: weird failure; please report this issue on ..."
    )
    assert error.message == "weird failure"

from media import urls


def test_instagram_reel_variants_normalise_to_one_url():
    expected = "https://www.instagram.com/reel/Dac9ebRiOCF/"
    for raw in (
        "https://www.instagram.com/reel/Dac9ebRiOCF/",
        "https://instagram.com/reel/Dac9ebRiOCF",
        "https://www.instagram.com/reels/Dac9ebRiOCF/?igshid=abc123",
        "https://www.instagram.com/johnsmith/reel/Dac9ebRiOCF/",
        "instagram.com/reel/Dac9ebRiOCF/",
    ):
        target = urls.detect(raw)
        assert target is not None, raw
        assert target.url == expected
        assert target.platform == urls.INSTAGRAM
        assert target.kind == "Instagram Reel"
        assert target.shortcode == "Dac9ebRiOCF"


def test_instagram_post_and_tv():
    post = urls.detect("https://www.instagram.com/p/ABC123xyz/")
    assert post.kind == "Instagram Post"
    assert post.is_carousel_capable
    assert urls.detect("https://www.instagram.com/tv/ABC123xyz/").kind == "Instagram Video"


def test_youtube_shorts_and_videos():
    short = urls.detect("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share")
    assert short.kind == "YouTube Short"
    assert short.url == "https://www.youtube.com/shorts/dQw4w9WgXcQ"
    assert not short.is_carousel_capable

    for raw in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
        "https://youtu.be/dQw4w9WgXcQ?si=trackingjunk",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    ):
        target = urls.detect(raw)
        assert target is not None, raw
        assert target.shortcode == "dQw4w9WgXcQ"
        assert target.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_unsupported_urls_return_none():
    for raw in (
        "https://example.com/video.mp4",
        "https://vimeo.com/12345",
        "https://www.instagram.com/johnsmith/",
        "not a url at all",
        "",
        "ftp://instagram.com/reel/abc/",
    ):
        assert urls.detect(raw) is None, raw


def test_find_supported_extracts_and_deduplicates():
    text = """
    check this https://www.instagram.com/reel/Dac9ebRiOCF/ out
    and https://youtu.be/dQw4w9WgXcQ plus a dupe
    https://instagram.com/reel/Dac9ebRiOCF?igshid=zzz
    https://example.com/ignored
    """
    found = urls.find_supported(text)
    assert [t.shortcode for t in found] == ["Dac9ebRiOCF", "dQw4w9WgXcQ"]

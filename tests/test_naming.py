from media import naming


def test_sanitize_removes_illegal_characters_and_emoji():
    assert naming.sanitize('a/b:c*d?e"f|g') == "abcdefg"
    assert naming.sanitize("Squat tutorial 🏋️‍♂️🔥") == "Squat tutorial"
    assert naming.sanitize("Squat 🏋️", remove_emoji=False) == "Squat 🏋️"
    assert naming.sanitize("  ...spaced.  ") == "spaced"
    assert naming.sanitize("") == ""


def test_sanitize_truncates_on_a_word_boundary():
    result = naming.sanitize(" ".join(["word"] * 40), max_length=30)
    assert len(result) <= 30
    assert not result.endswith("wo")


def test_sanitize_folds_styled_unicode_to_plain_letters():
    # Instagram captions are full of mathematical-bold text like this.
    assert naming.sanitize("𝗦𝗛𝗢𝗨𝗟𝗗𝗘𝗥 𝗙𝗜𝗫") == "SHOULDER FIX"
    assert naming.sanitize("ｗｉｄｅ ﬁle") == "wide file"


def test_truncation_does_not_end_on_a_dangling_word():
    assert naming.drop_trailing_filler("get my program in my") == "get my program"
    assert naming.drop_trailing_filler("how to squat properly and") == "how to squat properly"
    # Short titles are left alone: trimming them would eat real words ("The Who").
    assert naming.drop_trailing_filler("squats and") == "squats and"
    assert naming.drop_trailing_filler("The Who") == "The Who"

    long_caption = "Comment SHOULDER FIX to get my SHOULDER RECOVERY program in my"
    assert not naming.clean_title(long_caption + " bio today").endswith((" in", " my"))


def test_real_instagram_caption_produces_a_readable_name():
    info = {
        "id": "Dac9ebRiOCF",
        "title": "Comment 𝗦𝗛𝗢𝗨𝗟𝗗𝗘𝗥 𝗙𝗜𝗫 to get my 𝗦𝗛𝗢𝗨𝗟𝗗𝗘𝗥 𝗥𝗘𝗖𝗢𝗩𝗘𝗥𝗬 program 💪 #shoulderpain",
        "uploader": "Dr. Caleb Burgess DPT OCS CSCS",
    }
    stem = naming.build_stem(info)
    assert stem == (
        "Dr. Caleb Burgess DPT OCS CSCS - Comment SHOULDER FIX to get my "
        "SHOULDER RECOVERY program"
    )
    assert "#" not in stem and "𝗦" not in stem


def test_clean_title_strips_trailing_ids_and_hashtags():
    assert naming.clean_title("Squat Tutorial [Dac9ebRiOCF]") == "Squat Tutorial"
    assert naming.clean_title("How to squat #gym #fitness #reels") == "How to squat"
    assert naming.clean_title("How to squat properly #shorts") == "How to squat properly"


def test_clean_title_unwraps_instagram_phrasing():
    raw = 'johnsmith on Instagram: "Squat tutorial for beginners #gym"'
    assert naming.clean_title(raw) == "Squat tutorial for beginners"


def test_clean_title_rejects_placeholders():
    assert naming.clean_title("Video by John Smith") == ""
    assert naming.clean_title("Instagram Reel") == ""
    assert naming.clean_title("dQw4w9WgXcQ", video_id="dQw4w9WgXcQ") == ""
    assert naming.clean_title("John Smith", creator="John Smith") == ""
    assert naming.clean_title(None) == ""


def test_clean_creator_prefers_a_human_name():
    assert naming.clean_creator({"uploader": "@johnsmith"}) == "johnsmith"
    assert naming.clean_creator({"creator": "John Smith", "uploader": "js"}) == "John Smith"
    assert naming.clean_creator({"uploader": "Band - Topic"}) == "Band"
    assert naming.clean_creator({"uploader_id": "UCabcdefghijklmnopqrstuv"}) == ""
    assert naming.clean_creator({}) == ""


def test_build_stem_matches_the_documented_example():
    info = {"id": "Dac9ebRiOCF", "title": "Video by John Smith [Dac9ebRiOCF]",
            "description": "Squat Tutorial", "uploader": "John Smith"}
    assert naming.build_stem(info) == "John Smith - Squat Tutorial"


def test_build_stem_drops_empty_fields_and_their_separators():
    assert naming.build_stem({"id": "abc", "uploader": "John Smith"}) == "John Smith"
    stem = naming.build_stem({"id": "abc", "title": "Real Title"})
    assert stem == "Real Title"


def test_build_stem_falls_back_when_nothing_is_usable():
    stem = naming.build_stem({"id": "abc123", "upload_date": "20240115"}, platform="youtube")
    assert stem == "2024-01-15"
    assert naming.build_stem({}, platform="instagram").startswith("instagram ")


def test_build_stem_supports_custom_templates():
    info = {"id": "abc123", "title": "Squat Tutorial", "uploader": "John Smith",
            "upload_date": "20240115"}
    assert naming.build_stem(info, template="{date} {title}") == "2024-01-15 Squat Tutorial"
    assert naming.build_stem(info, template="{title} [{id}]") == "Squat Tutorial [abc123]"
    assert naming.build_stem(info, template="{creator}", index=2) == "John Smith (2)"


def test_unique_path_avoids_clobbering(tmp_path):
    first = tmp_path / "video.mp4"
    assert naming.unique_path(first) == first
    first.write_text("x")
    assert naming.unique_path(first).name == "video (2).mp4"


def test_creator_prefix_is_dropped_from_x_titles():
    """X titles arrive as "Creator - caption"; the template adds the creator itself."""
    assert naming.clean_title(
        "Anatoli Kopadze - Still the best 2 hours ever recorded", creator="Anatoli Kopadze"
    ) == "Still the best 2 hours ever recorded"
    assert naming.clean_title("CentrFit: A little training sesh", creator="CentrFit") == \
        "A little training sesh"


def test_creator_prefix_needs_a_real_separator():
    """"Fit" must not eat the start of "Fitness tips"."""
    assert naming.clean_title("Fitness tips for beginners", creator="Fit") == \
        "Fitness tips for beginners"


def test_a_title_that_is_only_the_creator_name_is_dropped():
    assert naming.clean_title("Bob - ", creator="Bob") == ""
    assert naming.clean_title("Bob", creator="Bob") == ""


def test_x_post_filename_does_not_repeat_the_creator():
    info = {
        "uploader": "Anatoli Kopadze",
        "title": "Anatoli Kopadze - Still the best 2 hours ever recorded",
        "id": "2094789990710456378",
        "upload_date": "20260901",
    }
    stem = naming.build_stem(info, template="{creator} - {title}", platform="x")

    assert stem == "Anatoli Kopadze - Still the best 2 hours ever recorded"

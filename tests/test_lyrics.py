import importlib.util
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("omarchy_lyrics", ROOT / "lyrics.py")
assert SPEC and SPEC.loader
lyrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lyrics)


class FakeResponse:
    def __init__(self, payload: object):
        self.payload = json.dumps(payload, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        return False

    def read(self, maximum: int):
        return self.payload[:maximum]


class LyricsTests(unittest.TestCase):
    def test_parse_lrc_orders_multiple_timestamps_and_ignores_metadata(self):
        parsed = lyrics.parse_lrc(
            "[ar:Artist]\n[00:10.50][00:20.005] Chorus\n[00:03.2] Verse\n[00:30.00]"
        )
        self.assertEqual(
            parsed,
            [
                {"atMs": 3200, "text": "Verse"},
                {"atMs": 10500, "text": "Chorus"},
                {"atMs": 20005, "text": "Chorus"},
            ],
        )

    def test_clean_metadata_removes_controls_and_bounds_input(self):
        cleaned = lyrics.clean_metadata("  Track\x00\n Name  " + "x" * 1000)
        self.assertTrue(cleaned.startswith("Track Name"))
        self.assertNotIn("\x00", cleaned)
        self.assertLessEqual(len(cleaned), lyrics.MAX_METADATA_CHARS)

    def test_candidate_selection_prefers_matching_synced_track(self):
        candidates = [
            {
                "trackName": "A Different Song",
                "artistName": "Another Artist",
                "syncedLyrics": "[00:01] no",
            },
            {
                "id": 7,
                "trackName": "吻别",
                "artistName": "张学友",
                "duration": 306,
                "syncedLyrics": "[00:36.36] line",
            },
        ]
        selected = lyrics.choose_candidate(
            candidates, {"title": "吻别", "artist": "张学友", "duration": 306}
        )
        self.assertEqual(selected["id"], 7)

    def test_request_json_url_encodes_metadata_and_sets_client_header(self):
        observed = {}

        def opener(request, timeout):
            observed["url"] = request.full_url
            observed["timeout"] = timeout
            observed["client"] = request.get_header("Lrclib-client")
            return FakeResponse({"ok": True})

        result = lyrics.request_json(
            "https://example.test/get",
            {"track_name": "one & two", "artist_name": "A/B"},
            opener,
        )
        self.assertEqual(result, {"ok": True})
        self.assertIn("track_name=one+%26+two", observed["url"])
        self.assertIn("artist_name=A%2FB", observed["url"])
        self.assertEqual(observed["timeout"], lyrics.HTTP_TIMEOUT_SEC)
        self.assertIn("Omarchy Lyrics", observed["client"])

    def test_http_404_becomes_a_searchable_miss(self):
        def opener(_request, timeout):
            self.assertEqual(timeout, lyrics.HTTP_TIMEOUT_SEC)
            raise urllib.error.HTTPError("https://example.test", 404, "missing", {}, None)

        self.assertIsNone(lyrics.request_json("https://example.test/get", {}, opener))

    def test_payload_validates_and_bounds_remote_fields(self):
        result = lyrics.payload_from_track(
            {
                "id": 3,
                "trackName": "T" * 1000,
                "artistName": "Artist",
                "duration": 10,
                "syncedLyrics": "[00:01.25] First line",
            }
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["lines"], [{"atMs": 1250, "text": "First line"}])
        self.assertEqual(len(result["track"]["title"]), lyrics.MAX_METADATA_CHARS)

    def test_remote_invalid_duration_is_not_propagated(self):
        result = lyrics.payload_from_track(
            {"id": 4, "trackName": "Song", "duration": "not-a-number", "plainLyrics": "Line"}
        )
        self.assertEqual(result["track"]["duration"], 0)
        self.assertEqual(lyrics.finite_float(float("nan")), 0)

    def test_cache_round_trip_and_refresh(self):
        calls = []

        def fetcher(metadata, source="netease"):
            calls.append(metadata)
            return {
                "schemaVersion": 1,
                "ok": True,
                "status": "ready",
                "plainLyrics": "cached",
                "lines": [],
            }

        metadata = {"title": "Song", "artist": "Artist", "duration": 100}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            first = lyrics.fetch_with_cache(metadata, cache, fetcher=fetcher)
            second = lyrics.fetch_with_cache(metadata, cache, fetcher=fetcher)
            third = lyrics.fetch_with_cache(metadata, cache, refresh=True, fetcher=fetcher)

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(third["cached"])
        self.assertEqual(len(calls), 2)


# Traditional-script library tags against NetEase's simplified names.
TITLE_T = "\u98c4\u96ea"            # piao xue, traditional
TITLE_S = "\u98d8\u96ea"            # piao xue, simplified
ARTIST_T = "\u9673\u6167\u5afb"    # traditional
ARTIST_S = "\u9648\u6167\u5a34"    # simplified
OTHER_TITLE = "\u6625\u5929"         # an unrelated two-character title


def song(song_id, name, artist, seconds):
    return {"id": song_id, "name": name, "artists": [{"name": artist}], "album": {"name": "A"},
            "duration": int(seconds * 1000)}


class NeteaseTests(unittest.TestCase):
    metadata = {"title": TITLE_T, "artist": ARTIST_T, "album": "Library album", "duration": 236.8}

    def test_traditional_tags_match_simplified_names_when_the_duration_agrees(self):
        points = lyrics.netease_score(song(1, TITLE_S, ARTIST_S, 237), self.metadata)
        self.assertIsNotNone(points)
        self.assertGreaterEqual(points, 6)

    def test_a_same_length_title_needs_the_duration_to_agree(self):
        self.assertIsNone(lyrics.netease_score(song(2, OTHER_TITLE, ARTIST_S, 237), self.metadata))
        self.assertIsNone(lyrics.netease_score(song(3, TITLE_S, ARTIST_S, 300), self.metadata))

    def test_translation_and_romanization_attach_to_their_lines(self):
        data = {
            "code": 200,
            "lrc": {"lyric": "[00:01.00]Lyricist: Someone\n[00:02.00]one\n[00:03.00]two\n"
                             "[00:04.00]three\n[00:05.00]four\n"},
            "tlyric": {"lyric": "[00:02.10]uno\n[00:04.00]tres\n"},
            "romalrc": {"lyric": "[00:03.00]ni\n"},
        }
        payload = lyrics.netease_payload(song(7, "Song", "Artist", 100), data)
        self.assertEqual([line["text"] for line in payload["lines"]], ["one", "two", "three", "four"])
        self.assertEqual(payload["lines"][0]["translation"], "uno")
        self.assertEqual(payload["lines"][2]["translation"], "tres")
        self.assertNotIn("translation", payload["lines"][1])
        self.assertEqual(payload["lines"][1]["romanization"], "ni")
        self.assertEqual(payload["source"], "netease")
        self.assertIn("music.163.com/song?id=7", payload["track"]["sourceUrl"])

    def test_too_few_lines_is_not_lyrics(self):
        data = {"code": 200, "lrc": {"lyric": "[00:01.00]pure music, enjoy\n"}}
        self.assertIsNone(lyrics.netease_payload(song(8, "Song", "Artist", 100), data))

    def test_preferred_source_first_and_the_other_as_fallback(self):
        ready = {"status": "ready", "lines": [{"atMs": 0, "text": "x"}]}
        miss = {"status": "not_found", "lines": []}
        asked = []

        def netease(metadata):
            asked.append("netease")
            return miss

        def lrclib(metadata):
            asked.append("lrclib")
            return ready

        self.assertIs(lyrics.fetch_remote(self.metadata, "netease", {"netease": netease, "lrclib": lrclib}), ready)
        self.assertEqual(asked, ["netease", "lrclib"])
        asked.clear()
        lyrics.fetch_remote(self.metadata, "lrclib", {"netease": netease, "lrclib": lrclib})
        self.assertEqual(asked, ["lrclib"])

    def test_timed_lyrics_from_the_fallback_beat_plain_text_from_the_first(self):
        plain = {"status": "ready", "lines": [], "plainLyrics": "words"}
        timed = {"status": "ready", "lines": [{"atMs": 0, "text": "words"}]}
        self.assertIs(lyrics.fetch_remote(self.metadata, "lrclib",
                                          {"lrclib": lambda m: plain, "netease": lambda m: timed}), timed)
        miss = {"status": "not_found", "lines": []}
        self.assertIs(lyrics.fetch_remote(self.metadata, "lrclib",
                                          {"lrclib": lambda m: plain, "netease": lambda m: miss}), plain)

    def test_an_unreachable_source_does_not_hide_the_other(self):
        def down(metadata):
            raise lyrics.LyricsError("Could not reach the lyrics service")

        ready = {"status": "ready", "lines": []}
        self.assertIs(lyrics.fetch_remote(self.metadata, "netease", {"netease": down, "lrclib": lambda m: ready}), ready)
        with self.assertRaises(lyrics.LyricsError):
            lyrics.fetch_remote(self.metadata, "netease", {"netease": down, "lrclib": down})


class LrclibSearchTests(unittest.TestCase):
    def test_search_does_not_filter_by_album(self):
        urls = []

        def opener(request, timeout):
            urls.append(request.full_url)
            return FakeResponse([] if "/search" in request.full_url else None)

        def not_found_get(request, timeout):
            urls.append(request.full_url)
            if "/get" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            return FakeResponse([])

        lyrics.fetch_lrclib({"title": "Song", "artist": "Artist", "album": "Album", "duration": 100},
                            opener=not_found_get)
        search = [url for url in urls if "/search" in url]
        self.assertEqual(len(search), 1)
        self.assertNotIn("album_name", search[0])

    def test_cache_keys_differ_by_source(self):
        metadata = {"title": "Song", "artist": "Artist", "duration": 100}
        self.assertNotEqual(lyrics.cache_key(metadata, "netease"), lyrics.cache_key(metadata, "lrclib"))


class JellyfinTests(unittest.TestCase):
    item = "0123456789abcdef0123456789abcdef"

    def write_settings(self, directory):
        folder = Path(directory) / "omarchy-lyrics"
        folder.mkdir()
        (folder / "jellyfin.env").write_text("JELLYFIN_URL=https://media.example\nJELLYFIN_API_KEY=secret\n")

    def test_library_lyrics_convert_ticks_and_carry_the_key(self):
        seen = []

        def opener(request, timeout):
            seen.append((request.full_url, request.headers.get("Authorization")))
            return FakeResponse({"Lyrics": [{"Start": 159_900_000, "Text": "second"},
                                             {"Start": 12_000_000, "Text": "first"},
                                             {"Text": "  "}]})

        with tempfile.TemporaryDirectory() as directory:
            self.write_settings(directory)
            payload = lyrics.fetch_jellyfin(self.item, opener=opener, config_home=directory)
        self.assertEqual(payload["source"], "jellyfin")
        self.assertEqual([(line["atMs"], line["text"]) for line in payload["lines"]],
                         [(1200, "first"), (15990, "second")])
        self.assertEqual(seen[0][0], f"https://media.example/Audio/{self.item}/Lyrics")
        self.assertEqual(seen[0][1], 'MediaBrowser Token="secret"')

    def test_a_line_split_differently_still_aligns_on_its_start(self):
        self.assertTrue(lyrics.same_words("abcdefghij", "abcdefgh"))
        self.assertTrue(lyrics.same_words("abcdefghij", "defghij"))
        self.assertFalse(lyrics.same_words("abcdefghij", "abc"))
        self.assertFalse(lyrics.same_words("abcdefghij", "zyxwvu"))

    def test_a_line_split_in_two_elsewhere_gets_both_halves(self):
        lines = [{"text": "abcdef ghijkl"}, {"text": "mnopqr"}]
        other = [{"text": "abcdef", "romanization": "one"}, {"text": "ghijkl", "romanization": "two"},
                 {"text": "mnopqr", "romanization": "three"}]
        self.assertEqual(lyrics.align_by_text(lines, other), 2)
        self.assertEqual([line["romanization"] for line in lines], ["one two", "three"])

    def test_a_line_in_the_other_script_is_placed_by_its_neighbours(self):
        lines = [{"text": "wxyz"}, {"text": "abcd"}, {"text": "WXYZ"}, {"text": "efgh"}]
        other = [{"text": "pqrs", "romanization": "zero"}, {"text": "abcd", "romanization": "one"},
                 {"text": "PQRS", "romanization": "two"}, {"text": "efgh", "romanization": "three"}]
        self.assertEqual(lyrics.align_by_text(lines, other), 4)
        self.assertEqual([line.get("romanization") for line in lines], ["zero", "one", "two", "three"])

    def test_a_line_between_two_candidates_is_left_alone(self):
        lines = [{"text": "abcd"}, {"text": "wxyz"}, {"text": "efgh"}]
        other = [{"text": "abcd", "romanization": "one"}, {"text": "pqrs", "romanization": "x"},
                 {"text": "tuvw", "romanization": "y"}, {"text": "efgh", "romanization": "three"}]
        self.assertEqual(lyrics.align_by_text(lines, other), 2)
        self.assertNotIn("romanization", lines[1])

    def test_a_translation_stamped_with_its_original_is_folded_into_it(self):
        lines = [{"atMs": 0, "text": "\u7a93\u306b\u897f\u967d"}, {"atMs": 0, "text": "\u5915\u9633"},
                 {"atMs": 5, "text": "Hello"}, {"atMs": 5, "text": "World"}]
        folded = lyrics.fold_same_time(lines)
        self.assertEqual([line["text"] for line in folded], ["\u7a93\u306b\u897f\u967d", "Hello", "World"])
        self.assertEqual(folded[0]["translation"], "\u5915\u9633")

    def test_credits_after_a_title_line_are_dropped(self):
        lines = [{"atMs": 0, "text": "Title - Artist"}, {"atMs": 1, "text": "\u8a5e\uff1aA"},
                 {"atMs": 2, "text": "\u66f2\uff1aB"}, {"atMs": 3, "text": "words"}]
        self.assertEqual([line["text"] for line in lyrics.strip_leading_credits(lines)],
                         ["Title - Artist", "words"])

    def test_word_times_become_times_per_character(self):
        timed = lyrics.parse_yrc("[1000,900](1000,300,0)ab(1300,200,0)c (1500,400,0)d")
        lines = [{"atMs": 1100, "text": "abc d"}]
        lyrics.attach_karaoke(lines, timed)
        self.assertEqual(lines[0]["karaoke"], [[0, 150], [150, 150], [300, 200], [500, 0], [500, 400]])

    def test_character_times_carry_over_to_a_matching_line(self):
        other = {"text": "ab cd", "karaoke": [[0, 1], [1, 1], [2, 0], [2, 1], [3, 1]]}
        line = {"text": "AB,CD"}
        lyrics.copy_karaoke(line, other)
        self.assertEqual(line["karaoke"], [[0, 1], [1, 1], [2, 0], [2, 1], [3, 1]])
        mismatch = {"text": "abc"}
        lyrics.copy_karaoke(mismatch, other)
        self.assertNotIn("karaoke", mismatch)

    def test_chinese_readings_pair_one_syllable_per_character(self):
        segments = lyrics.ruby_segments("\u6211\u6068 \u6211 Wo...", "o han o WOO")
        self.assertEqual(segments, [["\u6211", "o"], ["\u6068", "han"], [" ", ""], ["\u6211", "o"],
                                    [" ", ""], ["Wo", "woo"], ["...", ""]])

    def test_japanese_kanji_take_the_syllables_between_kana(self):
        # 夢ならば / 以上傷つく: kanji runs take whatever the kana leave them.
        self.assertEqual(lyrics.ruby_segments("\u5922\u306a\u3089\u3070", "yu me na ra ba"),
                         [["\u5922", "\u3086\u3081", "yume"], ["\u306a", "", "na"], ["\u3089", "", "ra"],
                          ["\u3070", "", "ba"]])
        segments = lyrics.ruby_segments("\u304d\u3063\u3068\u4ee5\u4e0a\u50b7\u3064\u304f",
                                        "ki tto i jyo u ki zu tsu ku")
        # 以上傷 -> いじょうきず
        self.assertEqual(segments[2], ["\u4ee5\u4e0a\u50b7", "\u3044\u3058\u3087\u3046\u304d\u305a", "ijyoukizu"])

    def test_readings_that_do_not_fit_the_line_are_left_out(self):
        self.assertIsNone(lyrics.ruby_segments("\u6211\u6068", "o han o gong"))
        self.assertIsNone(lyrics.ruby_segments("\u306a\u3089", "ka ki"))

    def test_the_playing_item_is_found_through_sessions(self):
        def opener(request, timeout):
            if "/Sessions" in request.full_url:
                return FakeResponse([{"NowPlayingItem": {"Id": self.item, "Name": "Song", "Artists": ["Artist"],
                                                          "RunTimeTicks": 1_000_000_000}}])
            return FakeResponse({"Items": []})

        with tempfile.TemporaryDirectory() as directory:
            self.write_settings(directory)
            metadata = {"title": "Song", "artist": "Artist", "duration": 100.5}
            self.assertEqual(lyrics.jellyfin_item_for(metadata, opener, directory), self.item)
            self.assertEqual(lyrics.jellyfin_item_for(dict(metadata, title="Other"), opener, directory), "")
            self.assertEqual(lyrics.jellyfin_item_for(dict(metadata, duration=200), opener, directory), "")

    def test_no_settings_or_no_item_falls_through(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(lyrics.fetch_jellyfin(self.item, config_home=directory))
            self.write_settings(directory)
            self.assertIsNone(lyrics.fetch_jellyfin("../../etc", config_home=directory))

    def test_text_alignment_tolerates_script_and_skips_strangers(self):
        # One lyric line in simplified (library) and traditional (NetEase) forms.
        library = [{"atMs": 0, "text": "\u659c\u9633\u65e0\u9650\u65e0\u5948\u53ea\u4e00\u606f\u95f4\u707f\u70c2"},
                   {"atMs": 5000, "text": "an extra line"}, {"atMs": 9000, "text": "hello world"}]
        other = [{"atMs": 100, "text": "\u659c\u967d\u7121\u9650\u7121\u5948\u53ea\u4e00\u606f\u9593\u71e6\u721b",
                  "romanization": "ce joeng mou haan"},
                 {"atMs": 8000, "text": "Hello, world!", "translation": "hi"}]
        self.assertEqual(lyrics.align_by_text(library, other), 2)
        self.assertEqual(library[0]["romanization"], "ce joeng mou haan")
        self.assertNotIn("translation", library[1])
        self.assertEqual(library[2]["translation"], "hi")


if __name__ == "__main__":
    unittest.main()

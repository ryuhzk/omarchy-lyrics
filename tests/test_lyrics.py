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


if __name__ == "__main__":
    unittest.main()

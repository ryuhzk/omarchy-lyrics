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

        def fetcher(metadata):
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


if __name__ == "__main__":
    unittest.main()

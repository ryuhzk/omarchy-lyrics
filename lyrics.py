#!/usr/bin/env python3
"""Fetch, validate, parse, and cache synced lyrics for the Omarchy plugin.

Sources, in order:

1. The user's own Jellyfin server, when the player names a Jellyfin item and
   ~/.config/omarchy-lyrics/jellyfin.env holds the server's URL and an API key:
   the library's own lyrics, the same ones Feishin shows, with no guessing.
   NetEase is then asked only for the translation and romanization of the same
   words, matched line by line on the text.
2. NetEase Cloud Music, which also carries a translation and a romanization
   for many songs, and LRCLIB. The preferred one is asked first and the other
   only when it has nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
# Part of every cache key: bumping it retires entries written by older rules,
# such as a "not found" from before the album stopped filtering LRCLIB searches.
CACHE_VERSION = 10
API_BASE_URL = "https://lrclib.net/api"
NETEASE_SEARCH_URL = "https://music.163.com/api/search/get"
NETEASE_LYRIC_URL = "https://music.163.com/api/song/lyric"
NETEASE_SEARCH_LIMIT = 10
NETEASE_SEARCH_SONGS = 1
NETEASE_SEARCH_LYRICS = 1006
JELLYFIN_ENV = "omarchy-lyrics/jellyfin.env"
# The NetEase song the user picked by hand for a track whose name finds the
# wrong one, keyed by title and artist. Kept with the settings rather than the
# cache: clearing the cache must not forget a choice.
CHOICES_FILE = "omarchy-lyrics/choices.json"
MAX_CHOICES = 5000
ITEM_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Jellyfin times lyric lines in ticks of 100 ns.
TICKS_PER_MS = 10_000
# A NetEase version counts as the same words when this share of the library's
# lines find their counterpart in it.
MIN_ALIGNED_SHARE = 0.5
SOURCES = ("netease", "lrclib")
# How far apart a translated line and its original may be stamped and still be
# the same line; NetEase's translations are usually stamped identically.
SECONDARY_TOLERANCE_MS = 300
CLIENT_HEADER = "Omarchy Lyrics v0.1.0 (https://github.com/ryuhzk/omarchy-lyrics)"
HTTP_TIMEOUT_SEC = 8
MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = MAX_HTTP_BYTES + 64 * 1024
MAX_METADATA_CHARS = 512
MAX_LYRICS_CHARS = 1_000_000
READY_TTL_SEC = 30 * 24 * 60 * 60
MISSING_TTL_SEC = 6 * 60 * 60
TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
DECORATION_RE = re.compile(
    r"\s*[\[(](?:official\s+)?(?:audio|video|lyrics?|visuali[sz]er|remaster(?:ed)?(?:\s+\d{4})?)[\]) ]*$",
    re.IGNORECASE,
)


# Credit lines at the top of NetEase lyrics: lyricist, composer, publisher...
CREDIT_RE = re.compile(
    r"^\s*(\u4f5c\u8bcd|\u4f5c\u66f2|\u7f16\u66f2|\u5236\u4f5c\u4eba|\u51fa\u54c1|\u53d1\u884c|"
    r"\u76d1\u5236|\u7edf\u7b79|\u4f01\u5212|\u5f55\u97f3|\u6df7\u97f3|\u6bcd\u5e26|\u548c\u58f0|"
    r"\u4f5c\u8a5e|\u7de8\u66f2|\u88fd\u4f5c|\u8a5e|"
    r"\u8bcd|\u66f2|OP|SP|Lyricist|Lyrics|Composer|Arranger|Producer|Produced|Publisher)\s*[:\uff1a]",
    re.IGNORECASE,
)


class LyricsError(RuntimeError):
    """A safe, user-facing error produced at an external-data boundary."""


def clean_metadata(value: object) -> str:
    """Normalize MPRIS metadata without allowing unbounded process arguments."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(character for character in text if character >= " " or character in "\t\n")
    return " ".join(text.split())[:MAX_METADATA_CHARS]


def canonical(value: object) -> str:
    text = clean_metadata(value).casefold()
    text = DECORATION_RE.sub("", text)
    return "".join(character for character in text if character.isalnum())


def timestamp_ms(minutes: str, seconds: str, fraction: str | None) -> int:
    fraction_text = fraction or "0"
    if len(fraction_text) == 1:
        milliseconds = int(fraction_text) * 100
    elif len(fraction_text) == 2:
        milliseconds = int(fraction_text) * 10
    else:
        milliseconds = int(fraction_text[:3].ljust(3, "0"))
    return (int(minutes) * 60 + int(seconds)) * 1000 + milliseconds


def parse_lrc(raw: object) -> list[dict[str, object]]:
    """Parse ordinary and multi-timestamp LRC into a stable, deduplicated timeline."""
    lyrics = str(raw or "")[:MAX_LYRICS_CHARS]
    timeline: list[tuple[int, int, str]] = []
    sequence = 0

    for raw_line in lyrics.splitlines():
        matches = list(TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue
        text = TIMESTAMP_RE.sub("", raw_line).strip()
        if not text:
            continue
        for match in matches:
            timeline.append(
                (timestamp_ms(match.group(1), match.group(2), match.group(3)), sequence, text)
            )
            sequence += 1

    timeline.sort(key=lambda entry: (entry[0], entry[1]))
    result: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for at_ms, _, text in timeline:
        key = (at_ms, text)
        if key in seen:
            continue
        seen.add(key)
        result.append({"atMs": at_ms, "text": text})
    return result


def bounded_remote_text(value: object, maximum: int = MAX_METADATA_CHARS) -> str:
    text = str(value or "")
    return text[:maximum]


def finite_float(
    value: object,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float = 3600.0,
) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


def request_json(
    endpoint: str,
    params: dict[str, object],
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> object | None:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
    request = urllib.request.Request(
        f"{endpoint}?{query}",
        headers={"Accept": "application/json", "Lrclib-Client": CLIENT_HEADER},
    )
    return read_json(request, opener)


def read_json(request: urllib.request.Request, opener: Callable[..., Any]) -> object | None:
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SEC) as response:
            payload = response.read(MAX_HTTP_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            return None
        if error.code == 429:
            raise LyricsError("The lyrics service is rate limiting requests; try again later") from None
        raise LyricsError(f"The lyrics service returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LyricsError("Could not reach the lyrics service") from None

    if len(payload) > MAX_HTTP_BYTES:
        raise LyricsError("The lyrics response was too large")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LyricsError("The lyrics service returned invalid data") from None


def similarity(left: object, right: object) -> float:
    left_key = canonical(left)
    right_key = canonical(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return SequenceMatcher(None, left_key, right_key).ratio()


def candidate_score(candidate: dict[str, object], metadata: dict[str, object]) -> float:
    score = similarity(candidate.get("trackName"), metadata.get("title")) * 0.58
    artist = clean_metadata(metadata.get("artist"))
    if artist:
        score += similarity(candidate.get("artistName"), artist) * 0.28
    else:
        score += 0.14

    album = clean_metadata(metadata.get("album"))
    if album:
        score += similarity(candidate.get("albumName"), album) * 0.04

    duration = finite_float(metadata.get("duration"))
    candidate_duration = finite_float(candidate.get("duration"))
    if duration > 0 and candidate_duration > 0:
        difference = abs(duration - candidate_duration)
        score += max(0.0, 1.0 - difference / 15.0) * 0.05

    if candidate.get("syncedLyrics"):
        score += 0.05
    return score


def choose_candidate(candidates: object, metadata: dict[str, object]) -> dict[str, object] | None:
    if not isinstance(candidates, list):
        return None
    valid = [candidate for candidate in candidates[:100] if isinstance(candidate, dict)]
    if not valid:
        return None
    selected = max(valid, key=lambda candidate: candidate_score(candidate, metadata))
    return selected if candidate_score(selected, metadata) >= 0.62 else None


def payload_from_track(track: dict[str, object] | None) -> dict[str, object]:
    if not track:
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "status": "not_found", "lines": []}

    synced = bounded_remote_text(track.get("syncedLyrics"), MAX_LYRICS_CHARS)
    plain = bounded_remote_text(track.get("plainLyrics"), MAX_LYRICS_CHARS)
    instrumental = track.get("instrumental") is True
    lines = fold_same_time(parse_lrc(synced))
    if instrumental:
        status = "instrumental"
    elif lines or plain:
        status = "ready"
    else:
        status = "not_found"

    track_id = track.get("id") if isinstance(track.get("id"), int) else None
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": True,
        "status": status,
        "track": {
            "id": track_id,
            "title": bounded_remote_text(track.get("trackName")),
            "artist": bounded_remote_text(track.get("artistName")),
            "album": bounded_remote_text(track.get("albumName")),
            "duration": finite_float(track.get("duration")),
            "sourceUrl": f"https://lrclib.net/api/get/{track_id}" if track_id is not None else "",
        },
        "source": "lrclib",
        "instrumental": instrumental,
        "plainLyrics": plain,
        "lines": lines,
    }


def fetch_lrclib(
    metadata: dict[str, object],
    api_base_url: str = API_BASE_URL,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, object]:
    title = clean_metadata(metadata.get("title"))
    artist = clean_metadata(metadata.get("artist"))
    album = clean_metadata(metadata.get("album"))
    duration = finite_float(metadata.get("duration"))
    if not title:
        return payload_from_track(None)

    base = api_base_url.rstrip("/")
    exact: object | None = None
    if artist:
        exact = request_json(
            f"{base}/get",
            {
                "track_name": title,
                "artist_name": artist,
                "album_name": album,
                "duration": round(duration, 3) if duration > 0 else None,
            },
            opener,
        )
    if isinstance(exact, dict) and (
        exact.get("syncedLyrics") or exact.get("plainLyrics") or exact.get("instrumental") is True
    ):
        return payload_from_track(exact)

    # No album here: a library's album name rarely matches LRCLIB's, and as a
    # filter it turned "no exact match" into "no results at all". The album
    # still counts, lightly, in candidate_score().
    search_params: dict[str, object]
    if artist:
        search_params = {"track_name": title, "artist_name": artist}
    else:
        search_params = {"q": title}
    candidates = request_json(f"{base}/search", search_params, opener)
    return payload_from_track(choose_candidate(candidates, metadata))


def netease_headers() -> dict[str, str]:
    return {"Accept": "application/json", "Referer": "https://music.163.com", "User-Agent": CLIENT_HEADER}


def netease_search(
    metadata: dict[str, object],
    opener: Callable[..., Any],
    keyword: str = "",
    kind: int = NETEASE_SEARCH_SONGS,
) -> list[dict[str, object]]:
    if not keyword:
        title = clean_metadata(metadata.get("title"))
        artist = clean_metadata(metadata.get("artist"))
        keyword = f"{title} {artist}".strip()
    body = urllib.parse.urlencode(
        {"s": keyword[:MAX_METADATA_CHARS], "type": kind, "offset": 0, "limit": NETEASE_SEARCH_LIMIT}
    ).encode("utf-8")
    request = urllib.request.Request(
        NETEASE_SEARCH_URL,
        data=body,
        headers={**netease_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    data = read_json(request, opener)
    if not isinstance(data, dict) or data.get("code") != 200:
        return []
    result = data.get("result")
    songs = result.get("songs") if isinstance(result, dict) else None
    return [song for song in songs[:NETEASE_SEARCH_LIMIT] if isinstance(song, dict)] if isinstance(songs, list) else []


def is_han(text: str) -> bool:
    return bool(text) and all("\u3400" <= character <= "\u9fff" for character in text)


def variant_match(left: str, right: str) -> bool:
    """Whether two Han names could be one name in traditional and simplified
    script: same length, some characters shared (the rest differ only in form).
    """
    if len(left) != len(right) or not is_han(left) or not is_han(right):
        return False
    shared = sum(1 for a, b in zip(left, right) if a == b)
    return shared * 3 >= len(left)


def strip_suffix(value: str) -> str:
    return re.sub(r"[(\uff08\u3010\[].*?[)\uff09\u3011\]]", "", value)


def netease_score(song: dict[str, object], metadata: dict[str, object]) -> int | None:
    """Points for a NetEase search result, or None when it is not this song.

    A library tagged in traditional script and NetEase's simplified names do
    not compare equal, so a same-length Han title counts when the duration
    agrees closely; the duration is what keeps that from matching another song.
    """
    want_title = canonical(metadata.get("title"))
    got_title = canonical(song.get("name"))
    if not want_title or not got_title:
        return None

    duration = finite_float(metadata.get("duration"))
    got_duration = finite_float(finite_float(song.get("duration"), maximum=3_600_000) / 1000)
    delta = abs(duration - got_duration) if duration > 0 and got_duration > 0 else None
    if delta is not None and delta > 20:
        return None

    if want_title == got_title:
        points = 6
    elif canonical(strip_suffix(clean_metadata(metadata.get("title")))) == canonical(
            strip_suffix(clean_metadata(song.get("name")))):
        points = 4
    elif want_title in got_title or got_title in want_title:
        points = 2
    elif variant_match(want_title, got_title) and delta is not None and delta <= 3:
        points = 5
    else:
        return None

    want_artist = canonical(metadata.get("artist"))
    if want_artist:
        names = []
        artists = song.get("artists")
        for entry in artists if isinstance(artists, list) else []:
            if isinstance(entry, dict):
                names.append(canonical(entry.get("name")))
        if any(name and (name in want_artist or want_artist in name or variant_match(name, want_artist))
               for name in names):
            points += 3

    if delta is not None:
        if delta <= 3:
            points += 3
        elif delta <= 8:
            points += 1
    return points


# Credits sit in the first few lines, sometimes after a title line.
CREDIT_WINDOW = 6


def strip_leading_credits(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    return [line for number, line in enumerate(lines)
            if number >= CREDIT_WINDOW or not CREDIT_RE.match(str(line.get("text", "")))]


KANA_TEXT_RE = re.compile(r"[\u3040-\u30ff']")
HANGUL_TEXT_RE = re.compile(r"[\uac00-\ud7af']")


def script_of(text: str) -> str:
    if KANA_TEXT_RE.search(text):
        return "kana"
    if HANGUL_TEXT_RE.search(text):
        return "hangul"
    letters = sum(1 for character in text if character.isascii() and character.isalpha())
    han = sum(1 for character in text if HAN_RE.match(character))
    return "latin" if letters > han else "han"


def fold_same_time(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    """Bilingual LRC stamps each translation with its original's time, right
    after it. As a line of its own the translation would replace the original
    the moment it starts; folded in, it becomes the original's translation,
    shown under it. Only a line in another script is folded, so a duet's two
    voices sharing a moment stay two lines."""
    result: list[dict[str, object]] = []
    for line in lines:
        previous = result[-1] if result else None
        text = str(line.get("text", ""))
        if (previous is not None and int(previous["atMs"]) == int(line["atMs"])
                and script_of(text) != script_of(str(previous.get("text", "")))):
            if not previous.get("translation"):
                previous["translation"] = text
            continue
        result.append(line)
    return result


def attach_secondary(
    lines: list[dict[str, object]], secondary: list[dict[str, object]], key: str
) -> None:
    """Give each original line the translated (or romanized) line stamped with it."""
    if not secondary:
        return
    stamps = [int(entry["atMs"]) for entry in secondary]
    position = 0
    for line in lines:
        at_ms = int(line["atMs"])
        while position + 1 < len(stamps) and stamps[position + 1] <= at_ms:
            position += 1
        best = None
        for candidate in (position, position + 1):
            if 0 <= candidate < len(stamps) and abs(stamps[candidate] - at_ms) <= SECONDARY_TOLERANCE_MS:
                if best is None or abs(stamps[candidate] - at_ms) < abs(stamps[best] - at_ms):
                    best = candidate
        if best is not None:
            text = str(secondary[best].get("text", ""))
            if text and text != line.get("text"):
                line[key] = text


def netease_payload(song: dict[str, object], data: object) -> dict[str, object] | None:
    if not isinstance(data, dict) or data.get("code") != 200 or data.get("nolyric") or data.get("uncollected"):
        return None

    def section(name: str) -> str:
        value = data.get(name)
        return bounded_remote_text(value.get("lyric") if isinstance(value, dict) else "", MAX_LYRICS_CHARS)

    lines = strip_leading_credits(parse_lrc(section("lrc")))
    if len(lines) < 4:
        return None
    attach_secondary(lines, strip_leading_credits(parse_lrc(section("tlyric"))), "translation")
    attach_secondary(lines, strip_leading_credits(parse_lrc(section("romalrc"))), "romanization")

    song_id = song.get("id") if isinstance(song.get("id"), int) else None
    artists = song.get("artists")
    artist_names = [bounded_remote_text(entry.get("name")) for entry in artists
                    if isinstance(entry, dict)] if isinstance(artists, list) else []
    album = song.get("album")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": True,
        "status": "ready",
        "track": {
            "id": song_id,
            "title": bounded_remote_text(song.get("name")),
            "artist": ", ".join(artist_names),
            "album": bounded_remote_text(album.get("name") if isinstance(album, dict) else ""),
            "duration": finite_float(finite_float(song.get("duration"), maximum=3_600_000) / 1000),
            "sourceUrl": f"https://music.163.com/song?id={song_id}" if song_id is not None else "",
        },
        "source": "netease",
        "instrumental": False,
        "plainLyrics": "",
        "lines": lines,
    }


def netease_lyric_data(song_id: int, opener: Callable[..., Any]) -> object | None:
    query = urllib.parse.urlencode({"id": song_id, "lv": -1, "kv": -1, "tv": -1, "rv": -1})
    request = urllib.request.Request(f"{NETEASE_LYRIC_URL}?{query}", headers=netease_headers())
    return read_json(request, opener)


def line_key(text: object) -> str:
    return canonical(text)


def same_form(left: str, right: str) -> bool:
    """Equal-length text that differs at most in traditional and simplified
    forms of the same characters (a third or more of them identical)."""
    if len(left) != len(right) or len(left) < 2:
        return False
    shared = sum(1 for a, b in zip(left, right) if a == b)
    return shared * 3 >= len(left)


def same_words(left: str, right: str) -> bool:
    """Whether two lyric lines say the same thing, allowing traditional and
    simplified forms, and a line one source splits where the other does not
    (the shorter is then the start or the end of the longer)."""
    if not left or not right:
        return False
    if left == right or same_form(left, right):
        return True
    short, long = sorted((left, right), key=len)
    return len(short) >= 4 and (same_form(short, long[:len(short)]) or same_form(short, long[-len(short):]))


def align_by_text(lines: list[dict[str, object]], other: list[dict[str, object]]) -> int:
    """Copy translation and romanization from `other` onto the lines that say
    the same words, walking both in order. Returns how many lines matched."""
    matched = 0
    position = 0
    keys = [line_key(entry.get("text")) for entry in other]
    # Where in `other` each line ended up: its first and last counterpart.
    placed: list[tuple[int, int] | None] = [None] * len(lines)
    for number, line in enumerate(lines):
        key = line_key(line.get("text"))
        # Near the last match first; then anywhere, since a chorus the other
        # source repeats in a different order still carries the same extras.
        nearby = range(position, min(len(other), position + 12))
        for candidate in [*nearby, *(index for index in range(len(other)) if index not in nearby)]:
            if same_words(key, keys[candidate]):
                # A line the other source splits in two: take the lines that
                # follow for as long as they continue it, so the extras cover
                # the whole line rather than its first half.
                span = [candidate]
                rest = key[len(keys[candidate]):] if same_form(keys[candidate], key[:len(keys[candidate])]) else ""
                while rest and span[-1] + 1 < len(other):
                    following = keys[span[-1] + 1]
                    size = min(len(rest), len(following))
                    if size < 2 or not same_form(rest[:size], following[:size]):
                        break
                    span.append(span[-1] + 1)
                    rest = rest[len(following):]
                # The lyrics' own translation, from a bilingual file, wins.
                for field in ("translation", "romanization"):
                    parts = [str(other[index].get(field) or "") for index in span]
                    if any(parts) and not line.get(field):
                        line[field] = " ".join(part for part in parts if part)
                matched += 1
                placed[number] = (span[0], span[-1])
                position = span[-1] + 1
                break
    # A line written in the other script with too few characters in common
    # (隨浪隨風飄蕩 and 随浪随风飘荡 share one) is still pinned down by its
    # neighbours: when exactly one line of the same length lies between where
    # the lines around it matched, that is the one. At either end of the song a
    # single matched neighbour is enough.
    for number, line in enumerate(lines):
        if placed[number] is not None:
            continue
        before = placed[number - 1] if number > 0 else (-1, -1)
        after = placed[number + 1] if number + 1 < len(lines) else (len(other), len(other))
        if before is None or after is None:
            continue
        if number == 0 and after[0] == len(other) or number == len(lines) - 1 and before[1] == -1:
            continue
        candidate = before[1] + 1
        if after[0] - before[1] != 2 or not 0 <= candidate < len(other):
            continue
        if len(keys[candidate]) != len(line_key(line.get("text"))):
            continue
        for field in ("translation", "romanization"):
            value = other[candidate].get(field)
            if value and not line.get(field):
                line[field] = value
        placed[number] = (candidate, candidate)
        matched += 1
    return matched


def enrich_from_netease(
    payload: dict[str, object],
    metadata: dict[str, object],
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, object]:
    """Add NetEase's translation or romanization to lyrics that came from
    elsewhere, matched on the words rather than the times: the library's
    recording and NetEase's may be different versions of the same song."""
    lines = payload.get("lines")
    if not isinstance(lines, list) or len(lines) < 4:
        return payload
    artist = clean_metadata(metadata.get("artist"))
    sample = max((str(line.get("text", "")) for line in lines[:12]), key=len)
    # By name first (works when the tags are Han), then by a line of the words
    # (works when they are romanized, "Xi Yang Zhi Ge"), with and without the
    # artist. Each search is cheap; the lyrics of each candidate are not, so
    # only the first few distinct songs are opened.
    searches = [
        (f"{clean_metadata(metadata.get('title'))} {artist}".strip(), NETEASE_SEARCH_SONGS),
        (sample, NETEASE_SEARCH_LYRICS),
        (f"{sample} {artist}".strip(), NETEASE_SEARCH_LYRICS),
    ]
    best: tuple[int, list[dict[str, object]]] | None = None
    seen: set[int] = set()
    try:
        for keyword, kind in searches:
            for song in netease_search(metadata, opener, keyword=keyword, kind=kind)[:4]:
                song_id = song.get("id")
                if not isinstance(song_id, int) or song_id in seen or len(seen) >= 8:
                    continue
                seen.add(song_id)
                other = netease_payload(song, netease_lyric_data(song_id, opener))
                if other is None:
                    continue
                if not any(line.get("translation") or line.get("romanization") for line in other["lines"]):
                    continue
                trial = [dict(line) for line in lines]
                matched = align_by_text(trial, other["lines"])
                if best is None or matched > best[0]:
                    best = (matched, trial)
            if best is not None and best[0] >= len(lines) * 0.9:
                break
    except LyricsError:
        pass
    if best is None or best[0] < len(lines) * MIN_ALIGNED_SHARE:
        return payload
    result = dict(payload)
    result["lines"] = best[1]
    result["secondarySource"] = "netease"
    return result


def jellyfin_settings(config_home: str = "") -> tuple[str, str]:
    base = Path(config_home or os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    values: dict[str, str] = {}
    try:
        for raw in (base / JELLYFIN_ENV).read_text(encoding="utf-8").splitlines()[:50]:
            key, _, value = raw.strip().partition("=")
            values[key] = value.strip().strip("\"'")
    except (OSError, UnicodeDecodeError):
        return "", ""
    url = values.get("JELLYFIN_URL", "").rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.:_-]+(/[A-Za-z0-9._~/-]*)?", url):
        return "", ""
    return url, values.get("JELLYFIN_API_KEY", "")


def jellyfin_json(path: str, opener: Callable[..., Any], config_home: str = "") -> object | None:
    url, key = jellyfin_settings(config_home)
    if not url or not key:
        return None
    request = urllib.request.Request(
        f"{url}{path}",
        headers={"Accept": "application/json", "Authorization": f'MediaBrowser Token="{key}"'},
    )
    try:
        return read_json(request, opener)
    except LyricsError:
        return None


def is_this_item(item: object, metadata: dict[str, object]) -> bool:
    if not isinstance(item, dict) or not ITEM_ID_RE.fullmatch(str(item.get("Id") or "")):
        return False
    if canonical(item.get("Name")) != canonical(metadata.get("title")):
        return False
    artist = canonical(metadata.get("artist"))
    artists = item.get("Artists") if isinstance(item.get("Artists"), list) else []
    if artist and artists and not any(canonical(name) and (canonical(name) in artist or artist in canonical(name))
                                      for name in artists):
        return False
    duration = finite_float(metadata.get("duration"))
    ticks = item.get("RunTimeTicks")
    if duration > 0 and isinstance(ticks, int) and ticks > 0 and abs(ticks / 1e7 - duration) > 3:
        return False
    return True


def jellyfin_item_for(
    metadata: dict[str, object],
    opener: Callable[..., Any] = urllib.request.urlopen,
    config_home: str = "",
) -> str:
    """The Jellyfin item being played: the one the player named, else what a
    Jellyfin client is playing now, else the library's own track by that name."""
    given = str(metadata.get("itemId") or "")
    if ITEM_ID_RE.fullmatch(given):
        return given
    title = clean_metadata(metadata.get("title"))
    if not title:
        return ""
    sessions = jellyfin_json("/Sessions?activeWithinSeconds=600", opener, config_home)
    for session in sessions[:50] if isinstance(sessions, list) else []:
        item = session.get("NowPlayingItem") if isinstance(session, dict) else None
        if is_this_item(item, metadata):
            return str(item["Id"])
    query = urllib.parse.urlencode({
        "searchTerm": title[:200], "IncludeItemTypes": "Audio", "Recursive": "true", "Limit": 10,
    })
    found = jellyfin_json(f"/Items?{query}", opener, config_home)
    items = found.get("Items") if isinstance(found, dict) else None
    for item in items[:10] if isinstance(items, list) else []:
        if is_this_item(item, metadata):
            return str(item["Id"])
    return ""


def fetch_jellyfin(
    item_id: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    config_home: str = "",
) -> dict[str, object] | None:
    """The library's own lyrics for a Jellyfin item, or None to fall through."""
    if not ITEM_ID_RE.fullmatch(item_id or ""):
        return None
    url, key = jellyfin_settings(config_home)
    if not url or not key:
        return None
    request = urllib.request.Request(
        f"{url}/Audio/{item_id}/Lyrics",
        headers={"Accept": "application/json", "Authorization": f'MediaBrowser Token="{key}"'},
    )
    try:
        data = read_json(request, opener)
    except LyricsError:
        return None
    entries = data.get("Lyrics") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    lines: list[dict[str, object]] = []
    plain: list[str] = []
    for entry in entries[:5000]:
        if not isinstance(entry, dict):
            continue
        text = bounded_remote_text(entry.get("Text"), 4096).strip()
        if not text:
            continue
        plain.append(text)
        start = entry.get("Start")
        if isinstance(start, int) and start >= 0:
            lines.append({"atMs": start // TICKS_PER_MS, "text": text})
    if not plain:
        return None
    lines.sort(key=lambda line: int(line["atMs"]))
    lines = fold_same_time(strip_leading_credits(lines))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": True,
        "status": "ready",
        "track": {"id": None, "title": "", "artist": "", "album": "", "duration": 0.0,
                  "sourceUrl": ""},
        "source": "jellyfin",
        "instrumental": False,
        "plainLyrics": "" if lines else "\n".join(plain),
        "lines": lines,
    }


def fetch_netease(
    metadata: dict[str, object],
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, object]:
    if not clean_metadata(metadata.get("title")):
        return payload_from_track(None)
    ranked = []
    for song in netease_search(metadata, opener):
        points = netease_score(song, metadata)
        if points is not None and points >= 6 and isinstance(song.get("id"), int):
            ranked.append((points, song))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    for _, song in ranked[:3]:
        payload = netease_payload(song, netease_lyric_data(song["id"], opener))
        if payload is not None:
            return payload
    return payload_from_track(None)


def config_base(config_home: str = "") -> Path:
    return Path(config_home or os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def choice_key(metadata: dict[str, object]) -> str:
    return f"{canonical(metadata.get('title'))}\u001f{canonical(metadata.get('artist'))}"


def read_choices(config_home: str = "") -> dict[str, dict[str, object]]:
    try:
        with (config_base(config_home) / CHOICES_FILE).open("rb") as handle:
            data = json.loads(handle.read(MAX_CACHE_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict) and isinstance(value.get("netease"), int)}


def read_choice(metadata: dict[str, object], config_home: str = "") -> int:
    if not canonical(metadata.get("title")):
        return 0
    return int(read_choices(config_home).get(choice_key(metadata), {}).get("netease", 0))


def write_choice(metadata: dict[str, object], song_id: int, config_home: str = "") -> None:
    """Remember `song_id` for this track, or forget the choice when it is 0."""
    if not canonical(metadata.get("title")):
        return
    choices = read_choices(config_home)
    choices.pop(choice_key(metadata), None)
    if song_id > 0:
        choices[choice_key(metadata)] = {
            "netease": song_id,
            "title": clean_metadata(metadata.get("title")),
            "artist": clean_metadata(metadata.get("artist")),
        }
    path = config_base(config_home) / CHOICES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = dict(list(choices.items())[-MAX_CHOICES:])
    descriptor, temporary = tempfile.mkstemp(prefix=".choices.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(kept, handle, ensure_ascii=False, indent=1)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise LyricsError("Could not save the chosen song") from None


def search_results(keyword: str, opener: Callable[..., Any] = urllib.request.urlopen) -> list[dict[str, object]]:
    """NetEase songs for the search box in the panel."""
    results = []
    for song in netease_search({}, opener, keyword=clean_metadata(keyword)):
        if not isinstance(song.get("id"), int):
            continue
        artists = song.get("artists") if isinstance(song.get("artists"), list) else []
        album = song.get("album") if isinstance(song.get("album"), dict) else {}
        results.append({
            "id": song["id"],
            "title": bounded_remote_text(song.get("name")),
            "artist": ", ".join(bounded_remote_text(entry.get("name")) for entry in artists if isinstance(entry, dict)),
            "album": bounded_remote_text(album.get("name")),
            "duration": finite_float(finite_float(song.get("duration"), maximum=3_600_000) / 1000),
        })
    return results


def without_title_line(payload: dict[str, object] | None, metadata: dict[str, object]) -> dict[str, object] | None:
    """Drop the "Title - Artist" line some lyrics files open with: it is not
    sung, and it would borrow a stranger's romanization from its neighbour."""
    lines = payload.get("lines") if payload else None
    title = canonical(metadata.get("title"))
    if not isinstance(lines, list) or not lines or not title:
        return payload
    first = str(lines[0].get("text", ""))
    if " - " in first and title in canonical(first):
        payload = dict(payload)
        payload["lines"] = lines[1:]
    return payload


def fetch_chosen(
    metadata: dict[str, object],
    song_id: int,
    library: dict[str, object] | None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, object] | None:
    """Lyrics for a track the user matched to a NetEase song by hand.

    The library's own lyrics keep their timing when the chosen song says the
    same words, and only borrow its translation and romanization; when they do
    not (the library has the wrong song's lyrics), the chosen song's are used.
    """
    chosen = netease_payload({"id": song_id}, netease_lyric_data(song_id, opener))
    if chosen is None:
        return None
    chosen["choice"] = song_id
    lines = library.get("lines") if library else None
    if isinstance(lines, list) and lines:
        trial = [dict(line) for line in lines]
        if align_by_text(trial, chosen["lines"]) >= len(lines) * MIN_ALIGNED_SHARE:
            result = dict(library)
            result["lines"] = trial
            result["secondarySource"] = "netease"
            result["choice"] = song_id
            return result
    return chosen


def fetch_remote(
    metadata: dict[str, object],
    source: str = "netease",
    fetchers: dict[str, Callable[[dict[str, object]], dict[str, object]]] | None = None,
) -> dict[str, object]:
    """Ask the preferred source, then the other.

    Timed lyrics win: plain text from the first source is kept only until the
    other has been asked, since without timestamps nothing can follow the song
    on the desktop. A source that cannot be reached does not hide an answer the
    other one has.
    """
    live = fetchers is None
    if live:
        library = without_title_line(fetch_jellyfin(jellyfin_item_for(metadata)), metadata)
        chosen_id = int(metadata.get("choice") or 0)
        if chosen_id > 0:
            chosen = fetch_chosen(metadata, chosen_id, library)
            if chosen is not None:
                return chosen
        if library is not None and library.get("lines"):
            return enrich_from_netease(library, metadata)
    fetchers = fetchers or {"netease": fetch_netease, "lrclib": fetch_lrclib}
    order = [source] + [name for name in SOURCES if name != source] if source in SOURCES else list(SOURCES)
    first_error: LyricsError | None = None
    plain: dict[str, object] | None = None
    miss: dict[str, object] | None = None
    for name in order:
        try:
            payload = fetchers[name](metadata)
        except LyricsError as error:
            first_error = first_error or error
            continue
        if payload.get("status") == "instrumental":
            return payload
        if payload.get("status") == "ready" and payload.get("lines"):
            # LRCLIB carries neither translation nor romanization, and a track
            # tagged with an English title ("Sky" for 海闊天空) is not found on
            # NetEase by name; its words still are.
            lines = payload["lines"]
            if live and not any(line.get("translation") or line.get("romanization") for line in lines):
                return enrich_from_netease(payload, metadata)
            return payload
        if payload.get("status") == "ready":
            plain = plain or payload
        else:
            miss = miss or payload
    if plain is not None:
        return plain
    if miss is not None:
        return miss
    raise first_error or LyricsError("Could not reach the lyrics service")


def cache_root(override: str = "") -> Path:
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "omarchy-lyrics"


def cache_key(metadata: dict[str, object], source: str = "netease") -> str:
    stable = json.dumps(
        {
            "version": CACHE_VERSION,
            "source": source,
            "choice": int(metadata.get("choice") or 0),
            "item": str(metadata.get("itemId") or ""),
            "title": clean_metadata(metadata.get("title")),
            "artist": clean_metadata(metadata.get("artist")),
            "album": clean_metadata(metadata.get("album")),
            "duration": round(finite_float(metadata.get("duration"))),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def read_cache(path: Path, now: float | None = None) -> dict[str, object] | None:
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("payload"), dict):
        return None
    payload = wrapper["payload"]
    ttl = READY_TTL_SEC if payload.get("status") in {"ready", "instrumental"} else MISSING_TTL_SEC
    cached_at = float(wrapper.get("cachedAt") or 0)
    if (time.time() if now is None else now) - cached_at > ttl:
        return None
    return payload


def write_cache(path: Path, payload: dict[str, object], now: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = {
        "schemaVersion": SCHEMA_VERSION,
        "cachedAt": time.time() if now is None else now,
        "payload": payload,
    }
    encoded = json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CACHE_BYTES:
        return
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".lyrics-", delete=False
        ) as temporary:
            temporary.write(encoded)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def fetch_with_cache(
    metadata: dict[str, object],
    cache_directory: Path,
    refresh: bool = False,
    fetcher: Callable[..., dict[str, object]] = fetch_remote,
    source: str = "netease",
) -> dict[str, object]:
    path = cache_directory / f"{cache_key(metadata, source)}.json"
    if not refresh:
        cached = read_cache(path)
        if cached is not None:
            result = dict(cached)
            result["cached"] = True
            return result
    result = fetcher(metadata, source)
    write_cache(path, result)
    result = dict(result)
    result["cached"] = False
    return result


# Readings over the words ----------------------------------------------------
#
# A romanization is one space-separated syllable per sound. Chinese has one per
# character, so the two simply pair up. Japanese kanji have any number, but the
# kana around them have exactly one each and a known spelling, so they pin the
# syllables down and each run of kanji takes whatever lies between them.

HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿々〆]")
KANA_READINGS: dict[str, tuple[str, ...]] = {}
for _row in (
    "あa いi うu えe おo かka きki くku けke こko がga ぎgi ぐgu げge ごgo "
    "さsa しshi,si すsu せse そso ざza じji,zi ずzu ぜze ぞzo "
    "たta ちchi,ti つtsu,tu てte とto だda ぢji,di づzu,du でde どdo "
    "なna にni ぬnu ねne のno はha,wa ひhi ふfu,hu へhe,e ほho "
    "ばba びbi ぶbu べbe ぼbo ぱpa ぴpi ぷpu ぺpe ぽpo まma みmi むmu めme もmo "
    "やya ゆyu よyo らra りri るru れre ろro わwa をwo,o んn,nn,m ゔvu "
    "ぁa ぃi ぅu ぇe ぉo ゃya ゅyu ょyo ゎwa"
).split():
    KANA_READINGS[_row[0]] = tuple(_row[1:].split(","))
SMALL_Y = {"ゃ": "a", "ゅ": "u", "ょ": "o"}
SMALL_VOWELS = {"ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o"}
MAX_KANJI_SYLLABLES = 4


def hiragana(character: str) -> str:
    code = ord(character)
    return chr(code - 0x60) if 0x30A1 <= code <= 0x30F6 else character


def kana_readings(key: str) -> set[str]:
    """The ways a romanization may spell one mora: a kana, a kana with a small
    ya/yu/yo or vowel after it, or either behind a small tsu."""
    if key.startswith("っ"):
        rest = kana_readings(key[1:])
        return {word[0] + word for word in rest if word[0] not in "aeiou"} | {"t" + word for word in rest if word.startswith("ch")}
    if len(key) == 1:
        return set(KANA_READINGS.get(key, ()))
    head, tail = key[0], key[1:]
    readings = set()
    for base in KANA_READINGS.get(head, ()):
        if tail in SMALL_Y and base.endswith("i"):
            stem = base[:-1]
            readings.add(stem + "y" + SMALL_Y[tail])
            if stem.endswith("h") or stem == "j":
                readings.add(stem + SMALL_Y[tail])
        elif tail in SMALL_VOWELS:
            readings.add(base[:-1] + SMALL_VOWELS[tail])
            readings.add(base[:-1] + "w" + SMALL_VOWELS[tail])
    return readings


def reading_units(text: str, group_han: bool) -> list[list[str]]:
    """Split a line into [surface, kind, key]: han, kana, long (a lengthening
    mark or a trailing small tsu, which may or may not be spelled), latin, or
    gap (spaces and punctuation, never spelled)."""
    units: list[list[str]] = []
    for character in text:
        key = hiragana(character)
        last = units[-1] if units else None
        if HAN_RE.match(character):
            if group_han and last and last[1] == "han":
                last[0] += character
                last[2] += character
            else:
                units.append([character, "han", character])
        elif key in KANA_READINGS or key == "っ":
            if last and last[1] == "kana" and (key in SMALL_Y or key in SMALL_VOWELS or last[2] == "っ"):
                last[0] += character
                last[2] += key
            else:
                units.append([character, "kana", key])
        elif character == "ー" and last:
            units.append([character, "long", key])
        elif character.isascii() and character.isalnum():
            if last and last[1] == "latin":
                last[0] += character
            else:
                units.append([character, "latin", character.lower()])
        elif last and last[1] == "gap":
            last[0] += character
        else:
            units.append([character, "gap", ""])
    for unit in units:
        if unit[1] == "kana" and unit[2] == "っ":
            unit[1] = "long"
    return units


def ruby_segments(text: object, romanization: object) -> list[list[str]] | None:
    """Pair each part of a line with the syllables that spell it, or None when
    they cannot be paired with confidence."""
    line = str(text or "")
    tokens = [re.sub(r"[^a-z]", "", token.lower()) for token in str(romanization or "").split()]
    tokens = [token for token in tokens if token]
    if not line or not tokens or len(line) > 120 or len(tokens) > 120:
        return None
    japanese = any(hiragana(character) in KANA_READINGS for character in line)
    units = reading_units(line, group_han=japanese)
    sounding = sum(1 for unit in units if unit[1] in ("han", "kana", "latin"))
    if sounding == 0:
        return None
    count, width = len(units), len(tokens)
    infinity = float("inf")
    # cost[i][j]: fewest mismatches pairing the first i units with j syllables.
    cost = [[infinity] * (width + 1) for _ in range(count + 1)]
    step: list[list[tuple[int, int] | None]] = [[None] * (width + 1) for _ in range(count + 1)]
    cost[0][0] = 0
    for index, (_, kind, key) in enumerate(units):
        for used in range(width + 1):
            here = cost[index][used]
            if here == infinity:
                continue
            options: list[tuple[int, float]] = []
            if kind == "gap":
                options.append((0, 0))
            elif kind == "long":
                options += [(0, 0), (1, 0.5)]
            elif kind == "kana":
                options.append((1, 0 if used < width and tokens[used] in kana_readings(key) else 1))
            elif kind == "latin":
                options += [(1, 0 if used < width and tokens[used][0] == key[0] else 1), (0, 1)]
            else:
                most = MAX_KANJI_SYLLABLES * len(key) if japanese else 1
                options += [(taken, 0) for taken in range(1, most + 1)]
            for taken, penalty in options:
                if used + taken > width:
                    continue
                if here + penalty < cost[index + 1][used + taken]:
                    cost[index + 1][used + taken] = here + penalty
                    step[index + 1][used + taken] = (used, taken)
    if cost[count][width] > max(1, sounding // 8):
        return None
    segments: list[list[str]] = []
    used = width
    for index in range(count, 0, -1):
        previous, taken = step[index][used]  # type: ignore[misc]
        surface, kind, _ = units[index - 1]
        separator = "" if japanese else " "
        segments.append([surface, separator.join(tokens[previous:previous + taken])])
        used = previous
    segments.reverse()
    return segments if len(segments) > 1 else None


def add_ruby(payload: dict[str, object]) -> dict[str, object]:
    lines = payload.get("lines")
    for line in lines if isinstance(lines, list) else []:
        if isinstance(line, dict) and line.get("romanization"):
            segments = ruby_segments(line.get("text"), line.get("romanization"))
            if segments:
                line["ruby"] = segments
    return payload



def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch", help="Fetch lyrics as one JSON object")
    fetch.add_argument("--title", required=True)
    fetch.add_argument("--artist", default="")
    fetch.add_argument("--album", default="")
    fetch.add_argument("--duration", type=float, default=0)
    fetch.add_argument("--cache-dir", default="")
    fetch.add_argument("--refresh", action="store_true")
    fetch.add_argument("--source", choices=SOURCES, default="netease",
                       help="Source asked first; the other is the fallback")
    fetch.add_argument("--item-id", default="",
                       help="Jellyfin item id from the player, for the library's own lyrics")
    fetch.add_argument("--choose", type=int, default=-1,
                       help="Remember this NetEase song id for the track (0 forgets the choice)")
    search = subparsers.add_parser("search", help="Search NetEase songs as one JSON object")
    search.add_argument("keyword")
    return root


def main(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    if options.command == "search":
        try:
            results = search_results(options.keyword) if clean_metadata(options.keyword) else []
            print(json.dumps({"schemaVersion": SCHEMA_VERSION, "ok": True, "results": results},
                             ensure_ascii=False, separators=(",", ":")))
            return 0
        except LyricsError as error:
            print(json.dumps({"schemaVersion": SCHEMA_VERSION, "ok": False, "error": str(error), "results": []},
                             ensure_ascii=False, separators=(",", ":")))
            return 1
    metadata = {
        "title": clean_metadata(options.title),
        "artist": clean_metadata(options.artist),
        "album": clean_metadata(options.album),
        "duration": finite_float(options.duration),
        "itemId": options.item_id.lower() if ITEM_ID_RE.fullmatch(options.item_id.lower()) else "",
    }
    try:
        if options.choose >= 0:
            write_choice(metadata, options.choose)
        metadata["choice"] = read_choice(metadata)
        result = add_ruby(fetch_with_cache(metadata, cache_root(options.cache_dir),
                                           options.refresh or options.choose >= 0, source=options.source))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except LyricsError as error:
        print(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "ok": False,
                    "status": "error",
                    "error": str(error),
                    "lines": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

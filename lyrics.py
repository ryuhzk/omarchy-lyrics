#!/usr/bin/env python3
"""Fetch, validate, parse, and cache synced lyrics for the Omarchy plugin.

Two sources: NetEase Cloud Music, which also carries a translation and a
romanization for many songs, and LRCLIB. The preferred one is asked first and
the other only when it has nothing.
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
CACHE_VERSION = 3
API_BASE_URL = "https://lrclib.net/api"
NETEASE_SEARCH_URL = "https://music.163.com/api/search/get"
NETEASE_LYRIC_URL = "https://music.163.com/api/song/lyric"
NETEASE_SEARCH_LIMIT = 10
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
    lines = parse_lrc(synced)
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


def netease_search(metadata: dict[str, object], opener: Callable[..., Any]) -> list[dict[str, object]]:
    title = clean_metadata(metadata.get("title"))
    artist = clean_metadata(metadata.get("artist"))
    keyword = f"{title} {artist}".strip()
    body = urllib.parse.urlencode(
        {"s": keyword, "type": 1, "offset": 0, "limit": NETEASE_SEARCH_LIMIT}
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


def strip_leading_credits(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    start = 0
    while start < len(lines) and CREDIT_RE.match(str(lines[start].get("text", ""))):
        start += 1
    return lines[start:]


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
        query = urllib.parse.urlencode({"id": song["id"], "lv": -1, "kv": -1, "tv": -1, "rv": -1})
        request = urllib.request.Request(f"{NETEASE_LYRIC_URL}?{query}", headers=netease_headers())
        payload = netease_payload(song, read_json(request, opener))
        if payload is not None:
            return payload
    return payload_from_track(None)


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
        if payload.get("status") == "instrumental" or (payload.get("status") == "ready" and payload.get("lines")):
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
    return root


def main(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    metadata = {
        "title": clean_metadata(options.title),
        "artist": clean_metadata(options.artist),
        "album": clean_metadata(options.album),
        "duration": finite_float(options.duration),
    }
    try:
        result = fetch_with_cache(metadata, cache_root(options.cache_dir), options.refresh,
                                  source=options.source)
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

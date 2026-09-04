#!/usr/bin/env python3
"""Fetch, validate, parse, and cache LRCLIB lyrics for the Omarchy plugin."""

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
API_BASE_URL = "https://lrclib.net/api"
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
        "instrumental": instrumental,
        "plainLyrics": plain,
        "lines": lines,
    }


def fetch_remote(
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

    search_params: dict[str, object]
    if artist:
        search_params = {"track_name": title, "artist_name": artist, "album_name": album}
    else:
        search_params = {"q": title}
    candidates = request_json(f"{base}/search", search_params, opener)
    return payload_from_track(choose_candidate(candidates, metadata))


def cache_root(override: str = "") -> Path:
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "omarchy-lyrics"


def cache_key(metadata: dict[str, object]) -> str:
    stable = json.dumps(
        {
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
    fetcher: Callable[[dict[str, object]], dict[str, object]] = fetch_remote,
) -> dict[str, object]:
    path = cache_directory / f"{cache_key(metadata)}.json"
    if not refresh:
        cached = read_cache(path)
        if cached is not None:
            result = dict(cached)
            result["cached"] = True
            return result
    result = fetcher(metadata)
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
        result = fetch_with_cache(metadata, cache_root(options.cache_dir), options.refresh)
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

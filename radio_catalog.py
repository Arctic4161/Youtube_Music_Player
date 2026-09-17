"""YouTube recommendation and transient stream resolution for Radio mode."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

import yt_dlp
from yt_dlp.networking import Request

from download_config import build_radio_stream_options, configured_proxy
from radio_logic import RadioTrack, parse_related_tracks, is_valid_video_id


class RadioCatalogError(RuntimeError):
    """A recoverable discovery or stream-resolution failure."""


@dataclass(frozen=True)
class ResolvedRadioStream:
    """Transient direct stream data.  Callers must not persist or log ``url``."""

    url: str
    headers: dict[str, str]
    duration: float = 0.0
    proxy_url: str = ""


def _next_api_key() -> str:
    try:
        from youtubesearchpython.core.constants import searchKey

        return str(searchKey)
    except Exception as exc:  # pragma: no cover - package is a declared dependency
        raise RadioCatalogError("YouTube search support is unavailable.") from exc


def fetch_related_tracks(
    video_id: str,
    *,
    exclude_ids: Iterable[str] = (),
    limit: int = 20,
    timeout_seconds: float = 15.0,
) -> list[RadioTrack]:
    """Read related videos from YouTube's web client, with no catalog account."""

    payload = {
        "context": {
            "client": {
                "clientName": "MWEB",
                "clientVersion": "2.20241202.07.00",
                "hl": "en",
                "gl": "US",
            }
        },
        "videoId": video_id,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; Mobile; rv:135.0) "
            "Gecko/135.0 Firefox/135.0"
        ),
        "Origin": "https://m.youtube.com",
        "Referer": f"https://m.youtube.com/watch?v={video_id}",
    }
    try:
        headers["Content-Type"] = "application/json"
        with yt_dlp.YoutubeDL({
            "proxy": configured_proxy(), "socket_timeout": timeout_seconds,
            "cachedir": False, "quiet": True,
        }) as ydl:
            with ydl.urlopen(Request(
                f"https://www.youtube.com/youtubei/v1/next?key={_next_api_key()}",
                data=json.dumps(payload).encode("utf-8"), headers=headers,
            )) as response:
                result = json.loads(response.read())
        return parse_related_tracks(
            result,
            exclude_ids=exclude_ids,
            limit=limit,
        )
    except Exception as exc:
        raise RadioCatalogError("Unable to fetch related Radio tracks.") from exc


def search_fallback_tracks(
    title: str,
    *,
    exclude_ids: Iterable[str] = (),
    limit: int = 20,
) -> list[RadioTrack]:
    """Use yt-dlp search so discovery shares its native SOCKS support."""

    query = str(title or "").strip()
    if not query:
        return []
    try:
        with yt_dlp.YoutubeDL({
            "proxy": configured_proxy(), "socket_timeout": 15,
            "extract_flat": True, "skip_download": True, "cachedir": False,
            "quiet": True, "no_warnings": True,
        }) as ydl:
            result = ydl.extract_info(f"ytsearch{max(1, limit)}:{query}", download=False)
    except Exception as exc:
        raise RadioCatalogError("Unable to find fallback Radio tracks.") from exc
    excluded = set(exclude_ids)
    tracks = []
    for entry in (result or {}).get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        video_id = str(entry.get("id") or "")
        title = str(entry.get("title") or "").strip()
        if not is_valid_video_id(video_id) or not title or video_id in excluded:
            continue
        excluded.add(video_id)
        tracks.append(RadioTrack(video_id, title, entry.get("thumbnail")))
        if len(tracks) >= max(1, limit):
            break
    return tracks


def resolve_radio_stream(video_id: str) -> ResolvedRadioStream:
    """Resolve one playable URL without downloading audio or writing yt-dlp cache."""

    options = build_radio_stream_options(video_id=video_id)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
            )
    except Exception as exc:
        raise RadioCatalogError("Unable to resolve this Radio stream.") from exc
    if not isinstance(info, Mapping):
        raise RadioCatalogError("YouTube returned no stream information.")
    url = str(info.get("url") or "").strip()
    if not url:
        requested = info.get("requested_formats")
        if isinstance(requested, list) and requested:
            first = requested[0]
            if isinstance(first, Mapping):
                url = str(first.get("url") or "").strip()
    if not url:
        raise RadioCatalogError("YouTube returned no playable Radio URL.")
    raw_headers = info.get("http_headers")
    stream_headers = dict(options["http_headers"])
    if isinstance(raw_headers, Mapping):
        stream_headers.update(
            {
                str(key): str(value)
                for key, value in raw_headers.items()
                if key and value
            }
        )
    try:
        duration = max(0.0, float(info.get("duration") or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    return ResolvedRadioStream(
        url=url, headers=stream_headers, duration=duration, proxy_url=options["proxy"]
    )

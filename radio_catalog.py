"""YouTube recommendation and transient stream resolution for Radio mode."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json

import yt_dlp
from yt_dlp.networking import Request

from download_config import build_radio_stream_options, configured_proxy
from radio_logic import (
    RadioTrack, is_valid_video_id, parse_music_search_tracks, parse_related_tracks,
)


class RadioCatalogError(RuntimeError):
    """A recoverable discovery or stream-resolution failure."""


@dataclass(frozen=True)
class ResolvedRadioStream:
    """Transient direct stream data.  Callers must not persist or log ``url``."""

    url: str
    headers: dict[str, str]
    duration: float = 0.0
    proxy_url: str = ""


# YouTube Music's Songs search filter. The parser still checks each item's type:
# filtered responses can include other shelves or change shape without notice.
_MUSIC_SONGS_SEARCH_PARAMS = "EgWKAQIIAWoMEA4QChADEAQQCRAF"


def _request_music(
    endpoint: str,
    body: Mapping[str, object],
    *,
    timeout_seconds: float = 15.0,
) -> Mapping[str, object]:
    """Read anonymous Music metadata using Radio's existing SOCKS transport."""

    payload = {
        **body,
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": "1." + datetime.now(timezone.utc).strftime("%Y%m%d") + ".01.00",
                "hl": "en",
                "gl": "US",
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://music.youtube.com",
        "Referer": "https://music.youtube.com/",
    }
    try:
        with yt_dlp.YoutubeDL({
            "proxy": configured_proxy(), "socket_timeout": timeout_seconds,
            "cachedir": False, "quiet": True,
        }) as ydl:
            with ydl.urlopen(Request(
                f"https://music.youtube.com/youtubei/v1/{endpoint}?alt=json",
                data=json.dumps(payload).encode("utf-8"), headers=headers,
            )) as response:
                result = json.loads(response.read())
    except Exception as exc:
        raise RadioCatalogError("Unable to fetch YouTube Music tracks.") from exc
    if not isinstance(result, Mapping) or "error" in result:
        raise RadioCatalogError("YouTube Music returned no usable response.")
    return result


def fetch_related_tracks(
    video_id: str,
    *,
    exclude_ids: Iterable[str] = (),
    limit: int = 20,
    timeout_seconds: float = 15.0,
) -> list[RadioTrack]:
    """Read Music radio and admit only identified songs and official videos."""

    if not is_valid_video_id(video_id):
        raise RadioCatalogError("Invalid Radio seed video ID.")
    video_id = str(video_id).strip()
    result = _request_music(
        "next",
        {
            "videoId": video_id,
            "playlistId": "RDAMVM" + video_id,
            "params": "wAEB",
            "enablePersistentPlaylistPanel": True,
            "isAudioOnly": True,
            "tunerSettingValue": "AUTOMIX_SETTING_NORMAL",
        },
        timeout_seconds=timeout_seconds,
    )
    return parse_related_tracks(result, exclude_ids=exclude_ids, limit=limit)


def search_fallback_tracks(
    title: str,
    *,
    exclude_ids: Iterable[str] = (),
    limit: int = 20,
) -> list[RadioTrack]:
    """Keep fallback discovery inside Music with the same strict item filter."""

    query = str(title or "").strip()
    if not query:
        return []
    result = _request_music(
        "search", {"query": query, "params": _MUSIC_SONGS_SEARCH_PARAMS},
    )
    return parse_music_search_tracks(result, exclude_ids=exclude_ids, limit=limit)


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

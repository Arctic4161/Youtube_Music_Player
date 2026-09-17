"""Pure yt-dlp option construction shared by the Android service."""

from __future__ import annotations

from collections.abc import Callable
import os
from typing import Any
from urllib.parse import urlparse

DEFAULT_YTDLP_PROXY = "socks5://193.25.215.182:22222"


def configured_proxy() -> str:
    """Use the same configured route for downloads and Radio."""
    return os.environ.get("YMP_YTDLP_PROXY", "").strip() or DEFAULT_YTDLP_PROXY


def _origin(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def build_yt_dlp_options(
    *,
    audio_path: str,
    page_url: str,
    cache_dir: str,
    logger: Any,
    proxy_url: str | None = None,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Return secure yt-dlp options for one audio download."""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; rv:135.0) "
            "Gecko/20100101 Firefox/135.0"
        ),
        "Referer": page_url,
        "Accept-Language": "en-US,en;q=0.9",
    }
    origin = _origin(page_url)
    if origin:
        headers["Origin"] = origin

    options: dict[str, Any] = {
        # android_vr now hides most HTTPS audio formats unless a GVS PO token
        # is supplied. visionos does not require the JS player and still
        # exposes regular M4A audio streams for anonymous downloads.
        "extractor_args": {"youtube": {"player_client": ["visionos"]}},
        "outtmpl": {"default": audio_path},
        "overwrites": False,
        # A restarted sticky service must continue yt-dlp's existing .part file.
        "continuedl": True,
        # The rest of the application treats this path as an MP4/M4A container.
        # Do not fall back to WebM/Opus while retaining a misleading .m4a suffix.
        "format": "m4a",
        "ignoreerrors": True,
        "cachedir": cache_dir,
        "retries": 20,
        "sleep_interval": 1,
        "max_sleep_interval": 10,
        "restrictfilenames": True,
        "forceipv4": True,
        "logger": logger,
        "user_agent": headers["User-Agent"],
        "referer": headers["Referer"],
        "http_headers": headers,
    }
    options["proxy"] = proxy_url or configured_proxy()
    if progress_hook is not None:
        options["progress_hooks"] = [progress_hook]
    return options


def build_radio_stream_options(*, video_id: str) -> dict[str, Any]:
    """Resolve temporary Radio streams through the shared download proxy."""

    page_url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; rv:135.0) "
            "Gecko/20100101 Firefox/135.0"
        ),
        "Referer": page_url,
        "Origin": "https://www.youtube.com",
        "Accept-Language": "en-US,en;q=0.9",
    }
    return {
        "extractor_args": {"youtube": {"player_client": ["visionos"]}},
        "format": "bestaudio[ext=m4a][protocol=https]",
        "proxy": configured_proxy(),
        "socket_timeout": 20,
        "noplaylist": True,
        "skip_download": True,
        "cachedir": False,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "http_headers": headers,
        "user_agent": headers["User-Agent"],
        "referer": headers["Referer"],
    }

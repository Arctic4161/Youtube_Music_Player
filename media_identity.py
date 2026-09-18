"""Stable filenames and display names for downloaded YouTube media."""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import parse_qs, urlparse

from utils import safe_filename

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_IDENTITY_SUFFIX_RE = re.compile(r"\s+\[([A-Za-z0-9_-]{6,64})\]$")
_AUDIO_EXTENSIONS = (".m4a", ".mp3", ".aac", ".flac", ".ogg", ".wav")


def youtube_video_id(url: str, explicit_id: object = None) -> str | None:
    """Return a validated YouTube video ID from a result or URL."""

    candidate = str(explicit_id or "").strip()
    if _VIDEO_ID_RE.fullmatch(candidate):
        return candidate

    parsed = urlparse(str(url or "").strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
                candidate = parts[1]
    elif host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]

    candidate = str(candidate or "").strip()
    return candidate if _VIDEO_ID_RE.fullmatch(candidate) else None


def stable_media_id(url: str, explicit_id: object = None) -> str:
    """Return the YouTube ID, or a deterministic URL identity as a fallback."""

    if video_id := youtube_video_id(url, explicit_id):
        return video_id
    normalized_url = str(url or "").strip()
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:12]
    return f"url-{digest}"


def media_stem(title: str, media_id: str) -> str:
    """Build a collision-resistant filename stem while keeping a readable title."""

    clean_id = stable_media_id("", media_id)
    reserved = len(clean_id) + 3
    clean_title = safe_filename(title, max_len=max(1, 120 - reserved))
    return f"{clean_title} [{clean_id}]"


def audio_filename(title: str, media_id: str) -> str:
    return f"{media_stem(title, media_id)}.m4a"


def find_existing_audio(directory: str, title: str, media_id: str) -> str | None:
    """Find a downloaded audio file before the caller starts a new download."""

    root = str(directory or "")
    clean_id = str(media_id or "").strip()
    if not root or not clean_id or not os.path.isdir(root):
        return None

    expected = os.path.join(root, audio_filename(title, clean_id))
    if os.path.isfile(expected):
        return expected

    suffix = f"[{stable_media_id('', clean_id)}]".casefold()
    matches: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                _stem, extension = os.path.splitext(entry.name)
                if extension.casefold() not in _AUDIO_EXTENSIONS:
                    continue
                if _stem.casefold().endswith(suffix):
                    matches.append(entry.path)
    except OSError:
        pass
    if matches:
        return sorted(matches, key=lambda path: os.path.basename(path).casefold())[0]

    legacy_stem = safe_filename(title)
    for extension in _AUDIO_EXTENSIONS:
        legacy_path = os.path.join(root, f"{legacy_stem}{extension}")
        if os.path.isfile(legacy_path):
            return legacy_path
    return None


def display_title_from_stem(stem: str) -> str:
    """Hide the identity suffix in the UI without changing the stored filename."""

    return _IDENTITY_SUFFIX_RE.sub("", str(stem or "")).strip()

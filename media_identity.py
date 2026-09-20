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


def _download_artifact_stems(stem: str, names: list[str]):
    """Include completed audio, resumable parts, and the matching cover."""
    for name in names:
        actual_stem, tail = name[:len(stem)], name[len(stem):].casefold()
        if actual_stem.casefold() == stem.casefold() and (
            tail in {".m4a", ".jpg"} or tail.startswith(".m4a.")
        ):
            yield actual_stem


def download_audio_path(directory: str, title: str, media_id: str) -> str:
    """Choose a stable output without sharing a case-different video's files."""
    clean_id = stable_media_id("", media_id)
    suffix = f" [{clean_id}]"
    stem = media_stem(title, clean_id)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        names = []
    normal = list(_download_artifact_stems(stem, names))
    conflicts = any(not actual.endswith(suffix) for actual in normal)
    if normal and not conflicts:
        return os.path.join(directory, f"{stem}.m4a")

    # Only collisions change the usual filename. Keep the exact ID suffix so
    # existing lookup and Radio metadata continue to recognize either name.
    marker = "~" + hashlib.sha256(clean_id.encode("utf-8")).hexdigest()[:12]
    title_limit = max(1, 120 - len(marker) - len(suffix) - 1)
    alternate = f"{stem[:-len(suffix)][:title_limit].rstrip()} {marker}{suffix}"
    alternate_artifacts = list(_download_artifact_stems(alternate, names))
    if conflicts or alternate_artifacts:
        if any(not actual.endswith(suffix) for actual in alternate_artifacts):
            raise FileExistsError("Another video owns the alternate download filename.")
        return os.path.join(directory, f"{alternate}.m4a")
    return os.path.join(directory, f"{stem}.m4a")


def validate_download_audio_path(audio_path: str, media_id: str) -> None:
    """Reject a replaced/case-conflicting file before reusing or tagging it."""
    stem, extension = os.path.splitext(os.path.basename(audio_path))
    suffix = f" [{stable_media_id('', media_id)}]"
    if extension.casefold() != ".m4a" or not stem.endswith(suffix):
        raise ValueError("The download filename does not match the selected video.")
    names = os.listdir(os.path.dirname(audio_path))
    if any(not actual.endswith(suffix)
           for actual in _download_artifact_stems(stem, names)):
        raise FileExistsError("Another video owns this download filename.")


def find_existing_audio(directory: str, title: str, media_id: str) -> str | None:
    """Find a downloaded audio file before the caller starts a new download."""

    root = str(directory or "")
    clean_id = str(media_id or "").strip()
    if not root or not clean_id or not os.path.isdir(root):
        return None

    expected = os.path.join(root, audio_filename(title, clean_id))
    # Inspect the actual directory entry even for the expected path: Windows
    # file existence checks ignore case, but YouTube video IDs do not.
    suffix = f"[{stable_media_id('', clean_id)}]"
    matches: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                _stem, extension = os.path.splitext(entry.name)
                if extension.casefold() not in _AUDIO_EXTENSIONS:
                    continue
                if _stem.endswith(suffix):
                    if entry.name.casefold() == os.path.basename(expected).casefold():
                        return entry.path
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

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Optional

from playlist_manager import PlaylistManager as _OriginalPlaylistManager
from utils import get_app_writable_dir

try:
    from storage_access import DownloadsAccess
except Exception:
    DownloadsAccess = None


DEFAULT_REL_SUBDIR = "Download/Youtube Music Player/Downloaded/Played"
PLAYLIST_FILENAME = "playlists.json"


def _is_android() -> bool:
    return sys.platform == "android"


def _strip_download_prefix(p: str) -> str:
    low = p.lstrip("/").replace("\\", "/")
    if low.lower().startswith("download/"):
        return low.split("/", 1)[1]
    if low.lower().startswith("downloads/"):
        return low.split("/", 1)[1]
    if low.startswith("storage/emulated/0/"):
        rest = low[len("storage/emulated/0/") :]
        if rest.lower().startswith("download/"):
            return rest.split("/", 1)[1]
    return low


class PlaylistManagerSAF:
    """
    Wrapper around the original PlaylistManager that:
      - Ensures first-run writes happen under an app-private dir (no crash on Android).
      - If the user grants SAF access to Downloads, mirrors playlist JSON to
        public Downloads:<DEFAULT_REL_SUBDIR>/<PLAYLIST_FILENAME>.
      - Delegates all other attributes/methods to the inner manager.
    """

    def __init__(self, *args, **kwargs):
        self._downloads = DownloadsAccess() if DownloadsAccess else None
        self._rel_subdir = kwargs.pop("rel_subdir", DEFAULT_REL_SUBDIR)
        rel_inside_downloads = _strip_download_prefix(self._rel_subdir)
        self._relative_json = os.path.join(rel_inside_downloads, PLAYLIST_FILENAME)
        spath = kwargs.get("storage_path")
        if _is_android() and (
            isinstance(spath, str)
            and (
                "/storage/emulated/0/Download/" in spath
                or spath.lower().lstrip("/").startswith(("download/", "downloads/"))
            )
        ):
            base = get_app_writable_dir("")
            tail = (
                spath.split("/storage/emulated/0/", 1)[-1]
                if "/storage/emulated/0/" in spath
                else _strip_download_prefix(spath)
            )
            new_path = os.path.join(base, tail)
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            kwargs["storage_path"] = new_path

        self._inner = _OriginalPlaylistManager(*args, **kwargs)

        if _is_android():
            with contextlib.suppress(Exception):
                inner_path = getattr(self._inner, "storage_path", None)
                if (
                    isinstance(inner_path, str)
                    and "/storage/emulated/0/Download/" in inner_path
                ):
                    base2 = get_app_writable_dir("")
                    tail2 = inner_path.split("/storage/emulated/0/", 1)[-1]
                    new_path2 = os.path.join(base2, tail2)
                    os.makedirs(os.path.dirname(new_path2), exist_ok=True)
                    self._inner.storage_path = new_path2

    def _saf_granted(self) -> bool:
        try:
            return bool(self._downloads and getattr(self._downloads, "tree_uri", None))
        except Exception:
            return False

    def _saf_exists(self) -> bool:
        """Return True if a playlists.json exists in Downloads SAF tree."""
        if not self._saf_granted():
            return False
        da = self._downloads
        if hasattr(da, "exists"):
            try:
                return bool(da.exists(self._relative_json))
            except Exception:
                return False

        try:
            if hasattr(da, "read_bytes"):
                data = da.read_bytes(self._relative_json)
                return data is not None
            if hasattr(da, "read_text"):
                txt = da.read_text(self._relative_json)
                return txt is not None
        except Exception:
            return False
        return False

    def _saf_write_bytes(self, data: bytes) -> None:
        if not self._saf_granted():
            return
        da = self._downloads
        with contextlib.suppress(Exception):
            if hasattr(da, "write_bytes"):
                da.write_bytes(self._relative_json, data)
                return
            if hasattr(da, "write_text"):
                da.write_text(self._relative_json, data.decode("utf-8"))
                return

    def _saf_read_bytes(self) -> Optional[bytes]:
        if not self._saf_granted():
            return None
        da = self._downloads
        try:
            if hasattr(da, "read_bytes"):
                return da.read_bytes(self._relative_json)
            if hasattr(da, "read_text"):
                txt = da.read_text(self._relative_json)
                return txt.encode("utf-8") if txt is not None else None
        except Exception:
            return None
        return None

    def _inner_json_path(self) -> Optional[str]:
        return getattr(self._inner, "storage_path", None)

    def load(self) -> None:
        """
        On Android with SAF granted:
          - If a playlists.json exists in Downloads, copy it into the inner
            app-private storage and then call inner.load().
        Otherwise: delegate to inner.load().
        """
        if _is_android() and self._saf_exists():
            with contextlib.suppress(Exception):
                data = self._saf_read_bytes()
                dst = self._inner_json_path()
                if data and dst:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(data)
        return self._inner.load()

    def save(self) -> None:
        """
        Always save via the inner manager (app-private, safe).
        If SAF is granted, mirror the resulting JSON into the Downloads SAF tree.
        """
        self._inner.save()

        if not _is_android() or not self._saf_granted():
            return

        src = self._inner_json_path()
        if not src or not os.path.exists(src):
            return

        with contextlib.suppress(Exception):
            with open(src, "rb") as f:
                data = f.read()
            self._saf_write_bytes(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_inner", "_downloads", "_rel_subdir", "_relative_json"}:
            return super().__setattr__(name, value)
        setattr(self._inner, name, value)

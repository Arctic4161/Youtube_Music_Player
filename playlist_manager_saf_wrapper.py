from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any

# Import the user's original manager without modifying it.
from playlist_manager import PlaylistManager as _OriginalPlaylistManager
from utils import get_app_writable_dir

try:
    from storage_access import DownloadsAccess
except Exception:
    DownloadsAccess = None  # type: ignore

DEFAULT_REL_SUBDIR = "Download/Youtube Music Player/Downloaded/Played"
PLAYLIST_FILENAME = "playlists.json"


class PlaylistManagerSAF:
    """Wrapper that adds public Downloads (SAF) support on Android without changing the original class.
    - On Android + SAF granted: load/save via SAF into public Downloads.
    - Otherwise: delegate to the original PlaylistManager unchanged.
    """

    def __init__(self, *args, **kwargs):
        # Create the original manager exactly as the app does today.
        self._inner = _OriginalPlaylistManager(*args, **kwargs)

        # Prepare SAF helpers/paths
        self._downloads = DownloadsAccess() if DownloadsAccess else None
        self._rel_subdir = kwargs.get(
            "rel_subdir", getattr(self._inner, "rel_subdir", DEFAULT_REL_SUBDIR)
        )
        self._relative_json = os.path.join(
            (
                self._rel_subdir.split("/", 1)[1]
                if self._rel_subdir.lower().startswith(("download/", "downloads/"))
                else self._rel_subdir
            ),
            PLAYLIST_FILENAME,
        )

        # If Android and caller passed a public Downloads path, remap to app files dir (so it works without SAF)
        if sys.platform == "android":
            spath = getattr(self._inner, "storage_path", None)
            if isinstance(spath, str) and spath.startswith(
                "/storage/emulated/0/Download/"
            ):
                base = get_app_writable_dir("")
                new_path = os.path.join(
                    base, spath[len("/storage/emulated/0/Download/") :]
                )
                with contextlib.suppress(Exception):
                    self._inner.storage_path = new_path  # keep inner consistent

    # ---- SAF-aware load/save wrappers ----
    def load(self) -> None:
        if sys.platform == "android" and self._downloads and self._downloads.tree_uri:
            with contextlib.suppress(Exception):
                if txt := self._downloads.read_text(self._relative_json):
                    raw = json.loads(txt)
                    # Use the inner's public API to populate data if available
                    # Fallback: assign attributes if the inner exposes them.
                    if hasattr(self._inner, "load_from_dict"):
                        self._inner.load_from_dict(raw)
                        return
                    # Generic fallback for common shapes:
                    if isinstance(getattr(self._inner, "data", None), dict):
                        self._inner.data["playlists"] = raw.get("playlists", [])
                        self._inner.data["active_playlist_id"] = raw.get(
                            "active_playlist_id"
                        )
                        return
        # Otherwise, just call the original behavior
        return self._inner.load()

    def save(self) -> None:
        if sys.platform == "android" and self._downloads and self._downloads.tree_uri:
            try:
                # Prefer a serializer if it exists on the inner manager
                if hasattr(self._inner, "to_dict"):
                    serial = self._inner.to_dict()
                else:
                    # Generic fallback
                    data = getattr(self._inner, "data", None)
                    if isinstance(data, dict):
                        serial = {
                            "playlists": data.get("playlists", []),
                            "active_playlist_id": data.get("active_playlist_id"),
                        }
                    else:
                        serial = {}
                text = json.dumps(serial, ensure_ascii=False, indent=2)
                self._downloads.write_text(self._relative_json, text)
                return
            except Exception:
                pass
        # Fallback to original behavior
        return self._inner.save()

    # ---- Delegate everything else transparently ----
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_inner", "_downloads", "_rel_subdir", "_relative_json"}:
            return super().__setattr__(name, value)
        setattr(self._inner, name, value)

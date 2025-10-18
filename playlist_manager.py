from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from utils import get_app_writable_dir, safe_filename

DEFAULT_REL_SUBDIR = "Downloaded/Played"
PLAYLIST_FILENAME = "playlists.json"


@dataclass
class Track:
    title: str
    path: str
    duration: float = 0.0
    thumb: Optional[str] = None


@dataclass
class Playlist:
    id: str
    name: str
    tracks: List[Track] = field(default_factory=list)


class PlaylistManager:
    """
    Updated to use app-specific storage by default (Android scoped storage-safe)
    Usage:
        pm = PlaylistManager()
    """

    def __init__(
        self, storage_path: Optional[str] = None, rel_subdir: str = DEFAULT_REL_SUBDIR
    ):
        if not storage_path:
            root = get_app_writable_dir(rel_subdir)
            storage_path = os.path.join(root, PLAYLIST_FILENAME)

        self.storage_path = storage_path
        self.data: Dict = {
            "playlists": [],
            "active_playlist_id": None,
        }
        self.load()

    def to_dict(self) -> dict:
        return {
            "playlists": [
                {"id": p.id, "name": p.name, "tracks": [asdict(t) for t in p.tracks]}
                for p in self.data["playlists"]
            ],
            "active_playlist_id": self.data["active_playlist_id"],
        }

    def load_from_dict(self, raw: dict) -> None:
        playlists = []
        for p in raw.get("playlists", []):
            tracks = [Track(**t) for t in p.get("tracks", [])]
            playlists.append(
                Playlist(
                    id=p.get("id", str(uuid.uuid4())),
                    name=p.get("name", "Untitled"),
                    tracks=tracks,
                )
            )
        self.create_and_save_playlist(playlists, raw)

    def load(self) -> None:
        """
        Load playlists and rebuild absolute media/cover paths from the current
        app sandbox ('Downloaded/Played') if stored paths are relative.
        """
        root = get_app_writable_dir("Downloaded/Played")

        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {}
        else:
            raw = {}

        playlists: List[Playlist] = []
        for p in raw.get("playlists", []):
            tr = []
            for t in p.get("tracks", []):
                raw_path = t.get("path", "") or ""
                if raw_path and not os.path.isabs(raw_path):
                    raw_path = os.path.normpath(os.path.join(root, raw_path))

                if raw_path and not os.path.exists(raw_path):
                    alt = os.path.join(root, os.path.basename(raw_path))
                    if os.path.exists(alt):
                        raw_path = os.path.normpath(alt)

                name = t.get("title") or os.path.splitext(os.path.basename(raw_path))[0]
                if (os.sep in name) or name.lower().endswith(
                    (".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg")
                ):
                    name = os.path.splitext(os.path.basename(name))[0]

                dur = t.get("duration", 0.0)

                thumb = t.get("thumb")
                if thumb and not os.path.isabs(thumb):
                    thumb_abs = os.path.normpath(os.path.join(root, thumb))
                    thumb = thumb_abs if os.path.exists(thumb_abs) else thumb

                if (not thumb) and raw_path:
                    base, _ = os.path.splitext(raw_path)
                    cand = f"{base}.jpg"
                    if os.path.exists(cand):
                        thumb = cand
                    else:
                        cand2 = os.path.join(root, f"{os.path.basename(base)}.jpg")
                        if os.path.exists(cand2):
                            thumb = cand2

                tr.append(Track(title=name, path=raw_path, duration=dur, thumb=thumb))

            pl = Playlist(
                id=p.get("id", str(uuid.uuid4())),
                name=p.get("name", "Untitled"),
                tracks=tr,
            )
            playlists.append(pl)

        self.create_and_save_playlist(playlists, raw)

    def create_and_save_playlist(self, playlists, raw):
        self.data["playlists"] = playlists
        self.data["active_playlist_id"] = raw.get("active_playlist_id")
        if not playlists:
            self.create_playlist("Favorites")
        if not self.data["active_playlist_id"] and self.data["playlists"]:
            self.data["active_playlist_id"] = self.data["playlists"][0].id
            self.save()

    def save(self) -> None:
        """
        Save playlists with media/cover paths stored as RELATIVE paths
        (when they live under the current sandbox), so they remain valid
        across reinstalls/updates that change the sandbox root.
        """
        root = get_app_writable_dir("Downloaded/Played")
        root_norm = os.path.normcase(os.path.normpath(root))

        def _to_rel(pth: Optional[str]) -> Optional[str]:
            if not pth:
                return pth
            try:
                np = os.path.normpath(pth)
                if (
                    os.path.normcase(np).startswith(root_norm + os.sep)
                    or os.path.normcase(np) == root_norm
                ):
                    return os.path.relpath(np, root)
                return pth
            except Exception:
                return pth

        serial = {
            "playlists": [],
            "active_playlist_id": self.data["active_playlist_id"],
        }
        for p in self.data["playlists"]:
            tracks = []
            for t in p.tracks:
                td = asdict(t)
                td["path"] = _to_rel(td.get("path"))
                td["thumb"] = _to_rel(td.get("thumb"))
                tracks.append(td)
            serial["playlists"].append({"id": p.id, "name": p.name, "tracks": tracks})

        tmp = f"{self.storage_path}.tmp"
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serial, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.storage_path)

    def _find(self, pid: str) -> Optional[Playlist]:
        return next((p for p in self.data["playlists"] if p.id == pid), None)

    def list_playlists(self) -> List[Playlist]:
        return list(self.data["playlists"])

    def active_playlist(self) -> Optional[Playlist]:
        return (
            self._find(self.data["active_playlist_id"])
            if self.data["active_playlist_id"]
            else None
        )

    def set_active(self, pid: str) -> None:
        if self._find(pid):
            self.data["active_playlist_id"] = pid
            self.save()

    def create_playlist(self, name: str) -> str:
        pid = str(uuid.uuid4())
        self.data["playlists"].append(Playlist(id=pid, name=name, tracks=[]))
        if not self.data["active_playlist_id"]:
            self.data["active_playlist_id"] = pid
        self.save()
        return pid

    def rename_playlist(self, pid: str, new_name: str) -> None:
        if p := self._find(pid):
            p.name = (new_name or "").strip() or p.name
            self.save()

    def delete_playlist(self, pid: str) -> None:
        self.data["playlists"] = [p for p in self.data["playlists"] if p.id != pid]
        if self.data["active_playlist_id"] == pid:
            self.data["active_playlist_id"] = (
                self.data["playlists"][0].id if self.data["playlists"] else None
            )
        self.save()

    def add_tracks(self, pid: str, paths: List[str]) -> None:
        """
        Add tracks by filesystem path, storing RELATIVE paths for anything under
        the app sandbox so the JSON survives reinstalls / sandbox changes.
        Also skips duplicates by normalized path and within the import batch.
        """
        p = self._find(pid)
        if not p:
            return
        root = get_app_writable_dir("Downloaded/Played")
        root_norm = os.path.normcase(os.path.normpath(root))

        def _norm(pth: str) -> str:
            try:
                return os.path.normcase(os.path.normpath(pth))
            except Exception:
                return pth

        def _to_rel_if_in_sandbox(pth: str) -> str:
            try:
                np = os.path.normpath(pth)
                np_norm = os.path.normcase(np)
                if np_norm == root_norm or np_norm.startswith(root_norm + os.sep):
                    return os.path.relpath(np, root)
                return pth
            except Exception:
                return pth

        existing = {
            _norm(getattr(t, "path", "")) for t in p.tracks if getattr(t, "path", "")
        }
        seen_batch = set()
        added_any = False

        for path in paths or []:
            if not path:
                continue

            base_name = os.path.basename(path)
            name_wo_ext, ext = os.path.splitext(base_name)

            safe_name = safe_filename(name_wo_ext)
            title = safe_name

            npath = _norm(path)
            if not npath or npath in existing or npath in seen_batch:
                continue

            rel_or_abs = _to_rel_if_in_sandbox(path)

            thumb = None
            with contextlib.suppress(Exception):
                base, _ = os.path.splitext(path)
                cand = f"{base}.jpg"
                if os.path.exists(cand):
                    thumb = _to_rel_if_in_sandbox(cand)

            p.tracks.append(Track(title=title, path=rel_or_abs, thumb=thumb))
            existing.add(npath)
            seen_batch.add(npath)
            added_any = True

        if added_any:
            self.save()

    def remove_track(self, pid: str, index: int) -> None:
        p = self._find(pid)
        if p and 0 <= index < len(p.tracks):
            p.tracks.pop(index)
            self.save()

    def move_track(self, pid: str, from_idx: int, to_idx: int) -> None:
        p = self._find(pid)
        if not p:
            return
        tracks = p.tracks
        if 0 <= from_idx < len(tracks) and 0 <= to_idx < len(tracks):
            item = tracks.pop(from_idx)
            tracks.insert(to_idx, item)
            self.save()

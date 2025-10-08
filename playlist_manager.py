
from __future__ import annotations
import json, uuid, os
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional

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
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.data: Dict = {
            "playlists": [],
            "active_playlist_id": None,
        }
        self.load()

    # ---------- persistence ----------
    def load(self) -> None:
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
                # Tolerate missing fields
                tr.append(Track(
                    title=t.get("title", os.path.splitext(os.path.basename(t.get("path","")))[0]),
                    path=t.get("path", ""),
                    duration=t.get("duration", 0.0),
                    thumb=t.get("thumb")
                ))
            pl = Playlist(
                id=p.get("id", str(uuid.uuid4())),
                name=p.get("name", "Untitled"),
                tracks=tr,
            )
            playlists.append(pl)

        self.data["playlists"] = playlists
        self.data["active_playlist_id"] = raw.get("active_playlist_id")

        # If nothing exists, seed a default playlist
        if not playlists:
            self.create_playlist("Favorites")

        if not self.data["active_playlist_id"] and self.data["playlists"]:
            self.data["active_playlist_id"] = self.data["playlists"][0].id
            self.save()

    def save(self) -> None:
        serial = {
            "playlists": [
                {
                    "id": p.id,
                    "name": p.name,
                    "tracks": [asdict(t) for t in p.tracks],
                }
                for p in self.data["playlists"]
            ],
            "active_playlist_id": self.data["active_playlist_id"],
        }
        tmp = self.storage_path + ".tmp"
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serial, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.storage_path)

    # ---------- helpers ----------
    def _find(self, pid: str) -> Optional[Playlist]:
        for p in self.data["playlists"]:
            if p.id == pid:
                return p
        return None

    def list_playlists(self) -> List[Playlist]:
        return list(self.data["playlists"])

    def active_playlist(self) -> Optional[Playlist]:
        return self._find(self.data["active_playlist_id"]) if self.data["active_playlist_id"] else None

    def set_active(self, pid: str) -> None:
        if self._find(pid):
            self.data["active_playlist_id"] = pid
            self.save()

    # ---------- playlist CRUD ----------
    def create_playlist(self, name: str) -> str:
        pid = str(uuid.uuid4())
        self.data["playlists"].append(Playlist(id=pid, name=name, tracks=[]))
        if not self.data["active_playlist_id"]:
            self.data["active_playlist_id"] = pid
        self.save()
        return pid

    def rename_playlist(self, pid: str, new_name: str) -> None:
        p = self._find(pid)
        if p:
            p.name = (new_name or "").strip() or p.name
            self.save()

    def delete_playlist(self, pid: str) -> None:
        self.data["playlists"] = [p for p in self.data["playlists"] if p.id != pid]
        if self.data["active_playlist_id"] == pid:
            self.data["active_playlist_id"] = self.data["playlists"][0].id if self.data["playlists"] else None
        self.save()

    # ---------- tracks ----------
    def add_tracks(self, pid: str, paths: List[str]) -> None:
        """Add tracks by filesystem path, **skipping duplicates by normalized path**.
        A duplicate is any path that, after os.path.normpath + normcase, already exists in the playlist.
        Also skips duplicates within the same import batch.
        """
        p = self._find(pid)
        if not p:
            return

        def _norm(pth: str) -> str:
            try:
                return os.path.normcase(os.path.normpath(pth))
            except Exception:
                return pth

        existing = set(_norm(getattr(t, "path", "")) for t in p.tracks if getattr(t, "path", ""))
        seen_batch = set()

        added_any = False
        for path in paths or []:
            if not path:
                continue
            npath = _norm(path)
            if not npath or npath in existing or npath in seen_batch:
                continue
            title = os.path.splitext(os.path.basename(path))[0]
            p.tracks.append(Track(title=title, path=path))
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

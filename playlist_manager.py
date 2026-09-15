from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from os.path import basename, exists, isabs, join, normcase, normpath
from typing import Dict, List, Optional

from utils import get_app_writable_dir, safe_filename

DEFAULT_REL_SUBDIR = "Downloaded/Played"
PLAYLIST_FILENAME = "playlists.json"
PLAYLIST_BACKUP_SUFFIX = ".bak"


def _empty_playlist_payload() -> dict:
    return {"playlists": [], "active_playlist_id": None}


def _validate_playlist_payload(raw: object) -> dict:
    """Reject malformed-but-valid JSON before it reaches the model loader."""

    if not isinstance(raw, dict):
        raise ValueError("playlist data must be an object")
    playlists = raw.get("playlists", [])
    active_id = raw.get("active_playlist_id")
    if not isinstance(playlists, list):
        raise ValueError("playlists must be a list")
    if active_id is not None and not isinstance(active_id, str):
        raise ValueError("active_playlist_id must be a string or null")
    for playlist in playlists:
        if not isinstance(playlist, dict):
            raise ValueError("each playlist must be an object")
        if "id" in playlist and not isinstance(playlist["id"], str):
            raise ValueError("playlist id must be a string")
        if "name" in playlist and not isinstance(playlist["name"], str):
            raise ValueError("playlist name must be a string")
        tracks = playlist.get("tracks", [])
        if not isinstance(tracks, list):
            raise ValueError("playlist tracks must be a list")
        for track in tracks:
            if not isinstance(track, dict):
                raise ValueError("each track must be an object")
            for key in ("title", "path", "thumb", "video_id"):
                value = track.get(key)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"track {key} must be a string or null")
            duration = track.get("duration", 0.0)
            try:
                duration_value = float(duration)
            except (TypeError, ValueError) as exc:
                raise ValueError("track duration must be numeric") from exc
            if not math.isfinite(duration_value) or duration_value < 0.0:
                raise ValueError("track duration must be finite and non-negative")
    return raw


@dataclass
class Track:
    title: str
    path: str
    duration: float = 0.0
    thumb: Optional[str] = None
    video_id: Optional[str] = None


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
        self.load_error: Optional[str] = None
        self.recovered_from_backup = False
        self.corrupt_backup_path: Optional[str] = None
        self._corrupt_source_pending = False
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
        raw = _validate_playlist_payload(raw)
        playlists = []
        for p in raw.get("playlists", []):
            tracks = [
                Track(
                    title=str(t.get("title") or ""),
                    path=str(t.get("path") or ""),
                    duration=float(t.get("duration", 0.0)),
                    thumb=t.get("thumb"),
                    video_id=t.get("video_id"),
                )
                for t in p.get("tracks", [])
            ]
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

        backup_path = f"{self.storage_path}{PLAYLIST_BACKUP_SUFFIX}"
        raw = _empty_playlist_payload()
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    raw = _validate_playlist_payload(json.load(f))
            except Exception as exc:
                self.load_error = str(exc)
                self._corrupt_source_pending = True
                if os.path.exists(backup_path):
                    try:
                        with open(backup_path, "r", encoding="utf-8") as f:
                            raw = _validate_playlist_payload(json.load(f))
                        self.recovered_from_backup = True
                    except Exception:
                        raw = _empty_playlist_payload()
        elif os.path.exists(backup_path):
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    raw = _validate_playlist_payload(json.load(f))
                self.recovered_from_backup = True
            except Exception as exc:
                self.load_error = str(exc)
                raw = _empty_playlist_payload()

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

                dur = float(t.get("duration", 0.0))

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

                tr.append(
                    Track(
                        title=name,
                        path=raw_path,
                        duration=dur,
                        thumb=thumb,
                        video_id=t.get("video_id"),
                    )
                )

            pl = Playlist(
                id=p.get("id", str(uuid.uuid4())),
                name=p.get("name", "Untitled"),
                tracks=tr,
            )
            playlists.append(pl)

        self._set_loaded_data(playlists, raw)

    def _set_loaded_data(self, playlists: List[Playlist], raw: dict) -> None:
        self.data["playlists"] = playlists
        pid = raw.get("active_playlist_id")
        self.data["active_playlist_id"] = (
            pid if any(p.id == pid for p in playlists) else None
        )

    def create_and_save_playlist(self, playlists, raw):
        self._set_loaded_data(playlists, raw)
        self.save()

    def _preserve_corrupt_source(self) -> None:
        if not self._corrupt_source_pending or not os.path.exists(self.storage_path):
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        preserved = f"{self.storage_path}.corrupt-{stamp}"
        shutil.copy2(self.storage_path, preserved)
        self.corrupt_backup_path = preserved
        self._corrupt_source_pending = False

    def _backup_current_valid_file(self) -> None:
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as fh:
                _validate_playlist_payload(json.load(fh))
        except Exception:
            return
        backup_path = f"{self.storage_path}{PLAYLIST_BACKUP_SUFFIX}"
        backup_tmp = f"{backup_path}.tmp"
        shutil.copy2(self.storage_path, backup_tmp)
        os.replace(backup_tmp, backup_path)

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
        os.makedirs(os.path.dirname(self.storage_path) or ".", exist_ok=True)
        self._preserve_corrupt_source()
        self._backup_current_valid_file()
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
        pid = self.data.get("active_playlist_id")
        return self._find(pid) if pid else None

    def set_active(self, pid: str) -> None:
        if self._find(pid):
            self.data["active_playlist_id"] = pid
            self.save()

    def clear_active(self) -> None:
        self.data["active_playlist_id"] = None
        self.save()

    def create_playlist(self, name: str) -> str:
        pid = str(uuid.uuid4())
        self.data["playlists"].append(Playlist(id=pid, name=name, tracks=[]))
        self.save()
        return pid

    def rename_playlist(self, pid: str, new_name: str) -> None:
        if p := self._find(pid):
            p.name = (new_name or "").strip() or p.name
            self.save()

    def delete_playlist(self, pid: str) -> None:
        self.data["playlists"] = [p for p in self.data["playlists"] if p.id != pid]
        if self.data.get("active_playlist_id") == pid:
            self.data["active_playlist_id"] = None
        self.save()

    def add_tracks(
        self,
        pid: str,
        paths: List[str],
        *,
        video_id: Optional[str] = None,
    ) -> None:
        p = self._find(pid)
        if not p:
            return

        root = get_app_writable_dir("Downloaded/Played")
        root_norm = os.path.normcase(os.path.normpath(root))

        def _abs_norm(pth: str) -> str:
            """Return a normcased absolute path; resolve rel paths under sandbox."""
            np = os.path.normpath(pth or "")
            if np and not os.path.isabs(np):
                np = os.path.join(root, np)
            return os.path.normcase(os.path.normpath(np))

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
            _abs_norm(getattr(t, "path", ""))
            for t in p.tracks
            if getattr(t, "path", "")
        }
        seen_batch = set()
        added_any = False

        for path in paths or []:
            if not path:
                continue

            base_name = os.path.basename(path)
            name_wo_ext, _ = os.path.splitext(base_name)
            safe_name = safe_filename(name_wo_ext)
            title = safe_name

            key = _abs_norm(path)
            if not key or key in seen_batch:
                continue
            if key in existing:
                if video_id:
                    for existing_track in p.tracks:
                        if _abs_norm(existing_track.path) == key and not existing_track.video_id:
                            existing_track.video_id = video_id
                            added_any = True
                            break
                continue

            rel_or_abs = _to_rel_if_in_sandbox(path)

            thumb = None
            with contextlib.suppress(Exception):
                base, _ = os.path.splitext(path)
                cand = f"{base}.jpg"
                if os.path.exists(cand):
                    thumb = _to_rel_if_in_sandbox(cand)

            p.tracks.append(
                Track(
                    title=title,
                    path=rel_or_abs,
                    thumb=thumb,
                    video_id=video_id,
                )
            )
            existing.add(key)
            seen_batch.add(key)
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

    def _looks_like_playlist_json(self, name: str) -> bool:
        n = (name or "").lower()
        return n.endswith(".json") and ("playlist" in n)

    def _find_legacy_candidates(self) -> list[str]:
        candidates = []
        for d in (
            get_app_writable_dir("Downloaded/Backups"),
            get_app_writable_dir("Downloaded"),
        ):
            with contextlib.suppress(Exception):
                candidates.extend(
                    os.path.normpath(os.path.join(d, fn))
                    for fn in os.listdir(d)
                    if self._looks_like_playlist_json(fn)
                )
        uniq = []
        seen = set()
        for p in candidates:
            if p not in seen:
                seen.add(p)
                uniq.append(p)

        with contextlib.suppress(Exception):
            uniq.sort(key=os.path.getmtime)
        return uniq

    def write_canonical_json(self, filename: str = "playlist.json") -> str:
        """Write current playlists to Downloads/Backups/<filename> atomically."""
        export_dir = get_app_writable_dir("Downloaded/Backups")
        os.makedirs(export_dir, exist_ok=True)
        path = os.path.join(export_dir, filename)
        data = self.export_dict() if hasattr(self, "export_dict") else self.to_dict()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    def consolidate_legacy_jsons(self, remove_originals: bool = True) -> Optional[str]:
        """
        Import+merge every legacy playlist*.json in Downloads/Backups and Downloads,
        then write a single canonical Downloads/Backups/playlist.json.
        Returns the canonical path or None if nothing was consolidated.
        """
        files = self._find_legacy_candidates()
        if not files:
            return None

        try:
            existing_count = len(self.data.get("playlists", []))
        except Exception:
            existing_count = 0

        first = True
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print("Skip invalid playlist JSON:", path, e)
                continue

            if first and existing_count == 0:
                if hasattr(self, "import_from_dict"):
                    self.import_from_dict(raw, merge=False)
                else:
                    self.load_from_dict(raw)
                first = False
                existing_count = 1
            elif hasattr(self, "import_from_dict"):
                self.import_from_dict(raw, merge=True)
            else:
                self.load_from_dict(raw)

        canonical = self.write_canonical_json("playlist.json")

        if remove_originals:
            archive_root = os.path.join(
                get_app_writable_dir("Downloaded/Backups"), "Imported_Archive"
            )
            os.makedirs(archive_root, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            archive_dir = os.path.join(archive_root, f"run_{ts}")
            os.makedirs(archive_dir, exist_ok=True)
            for p in files:
                if os.path.normpath(p) == os.path.normpath(canonical):
                    continue
                with contextlib.suppress(Exception):
                    shutil.move(p, os.path.join(archive_dir, os.path.basename(p)))
        return canonical

    def auto_consolidate_if_needed(self) -> Optional[str]:
        """
        Run consolidation once if either there are no playlists OR no sentinel exists.
        Keeps a sentinel in Backups to avoid repeating work on every launch.
        """
        sentinel_dir = get_app_writable_dir("Downloaded/Backups")
        os.makedirs(sentinel_dir, exist_ok=True)
        sentinel = os.path.join(sentinel_dir, ".auto_consolidate_done")

        try:
            playlists_count = len(self.data.get("playlists", []))
        except Exception:
            playlists_count = 0

        should = (playlists_count == 0) or (not os.path.exists(sentinel))
        if not should:
            return None

        out = self.consolidate_legacy_jsons(remove_originals=True)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("done")
        return out


def _pm_export_dict(self) -> dict:
    data = self.to_dict()
    with contextlib.suppress(Exception):
        data.setdefault("meta", {})
        data["meta"]["schema"] = "youtube-music-player.playlists.v1"
        data["meta"]["exported_at"] = datetime.now(timezone.utc).isoformat()
    return data


PlaylistManager.export_dict = _pm_export_dict


def _pm_import_from_dict(self, raw: dict, merge: bool = False) -> None:
    try:
        root = get_app_writable_dir("Downloaded/Played")
    except Exception:
        root = os.getcwd()

    def _abs_norm(pth: str) -> str:
        np = normpath(pth or "")
        if np and not os.path.isabs(np):
            np = join(root, np)
        return normcase(normpath(np))

    if not merge:
        return self.load_from_dict(raw)

    incoming: List[Playlist] = []
    for p in raw.get("playlists", []):
        tr = []
        for t in p.get("tracks", []):
            raw_path = t.get("path") or ""
            if raw_path and not isabs(raw_path):
                raw_path = normpath(join(root, raw_path))
            name = t.get("title") or os.path.splitext(basename(raw_path))[0]
            dur = t.get("duration", 0.0)
            thumb = t.get("thumb")
            if thumb and not isabs(thumb):
                thumb_abs = normpath(join(root, thumb))
                thumb = thumb_abs if exists(thumb_abs) else thumb
            tr.append(
                Track(
                    title=name,
                    path=raw_path,
                    duration=dur,
                    thumb=thumb,
                    video_id=t.get("video_id"),
                )
            )
        incoming.append(
            Playlist(
                id=p.get("id", str(uuid.uuid4())),
                name=p.get("name", "Untitled"),
                tracks=tr,
            )
        )

    existing_by_name = {
        (p.name or "").strip().lower(): p for p in self.data["playlists"]
    }
    for inc in incoming:
        key = (inc.name or "Untitled").strip().lower()
        if key not in existing_by_name:
            self.data["playlists"].append(
                Playlist(id=str(uuid.uuid4()), name=inc.name, tracks=list(inc.tracks))
            )
            existing_by_name[key] = self.data["playlists"][-1]
        else:
            dst = existing_by_name[key]
            seen = {
                _abs_norm(getattr(t, "path", ""))
                for t in dst.tracks
                if getattr(t, "path", "")
            }
            seen_basenames = {
                os.path.basename(getattr(t, "path", ""))
                for t in dst.tracks
                if getattr(t, "path", "")
            }
            for t in inc.tracks:
                keyp = _abs_norm(getattr(t, "path", ""))
                base = os.path.basename(getattr(t, "path", ""))
                if keyp and keyp not in seen and base not in seen_basenames:
                    dst.tracks.append(t)
                    seen.add(keyp)
                    seen_basenames.add(base)

    if not self.data.get("active_playlist_id"):
        pid = raw.get("active_playlist_id")
        if pid and any(p.id == pid for p in self.data["playlists"]):
            self.data["active_playlist_id"] = pid
    self.save()


PlaylistManager.import_from_dict = _pm_import_from_dict


def _pm_write_canonical_json(self, filename: str = "playlist.json") -> str:
    pm = self
    export_dir = get_app_writable_dir("Downloaded/Backups")
    os.makedirs(export_dir, exist_ok=True)
    path = os.path.join(export_dir, filename)
    data = pm.export_dict() if hasattr(pm, "export_dict") else pm.to_dict()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


PlaylistManager.write_canonical_json = _pm_write_canonical_json


def _pm_consolidate_legacy_jsons(
    self, remove_originals: bool = True, canonical_name: str = "playlist.json"
) -> str | None:
    pm = self

    def looks_like_playlist_json(name: str) -> bool:
        n = (name or "").lower()
        return n.endswith(".json") and ("playlist" in n)

    search_dirs = [
        get_app_writable_dir("Downloaded/Backups"),
        get_app_writable_dir("Downloaded"),
    ]
    candidates = []
    for d in search_dirs:
        with contextlib.suppress(Exception):
            for fn in os.listdir(d):
                if looks_like_playlist_json(fn):
                    candidates.append(os.path.join(d, fn))

    uniq = []
    seen = set()
    for pth in candidates:
        np = os.path.normpath(pth)
        if np not in seen:
            seen.add(np)
            uniq.append(np)
    try:
        uniq.sort(key=os.path.getmtime)
    except Exception:
        uniq.sort()

    if not uniq:
        return None

    try:
        existing_count = len(pm.data.get("playlists", []))
    except Exception:
        existing_count = 0
    first = True
    for path in uniq:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print("Skip invalid JSON:", path, e)
            continue

        if first and existing_count == 0:
            if hasattr(pm, "import_from_dict"):
                pm.import_from_dict(raw, merge=False)
            else:
                pm.load_from_dict(raw)
            first = False
            existing_count = 1
        else:
            if hasattr(pm, "import_from_dict"):
                pm.import_from_dict(raw, merge=True)
            else:
                pm.load_from_dict(raw)

    canonical = pm.write_canonical_json(canonical_name)

    if remove_originals:
        archive_root = os.path.join(
            get_app_writable_dir("Downloaded/Backups"), "Imported_Archive"
        )
        os.makedirs(archive_root, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        archive_dir = os.path.join(archive_root, f"run_{ts}")
        os.makedirs(archive_dir, exist_ok=True)
        for pth in uniq:
            if os.path.normpath(pth) == os.path.normpath(canonical):
                continue
            with contextlib.suppress(Exception):
                shutil.move(pth, os.path.join(archive_dir, os.path.basename(pth)))

    return canonical


PlaylistManager.consolidate_legacy_jsons = _pm_consolidate_legacy_jsons


def _pm_try_auto_import_legacy(self) -> str | None:
    pm = self
    sentinel_dir = get_app_writable_dir("Downloaded/Backups")
    os.makedirs(sentinel_dir, exist_ok=True)
    sentinel = os.path.join(sentinel_dir, ".auto_import_done")

    try:
        playlists_count = len(pm.data.get("playlists", []))
    except Exception:
        playlists_count = 0

    should = (playlists_count == 0) or (not os.path.exists(sentinel))
    if not should:
        return None

    out = pm.consolidate_legacy_jsons(remove_originals=True)
    with open(sentinel, "w", encoding="utf-8") as f:
        f.write("done")
    return out


PlaylistManager.try_auto_import_legacy = _pm_try_auto_import_legacy

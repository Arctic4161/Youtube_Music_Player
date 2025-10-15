from __future__ import annotations

import contextlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Optional, Tuple

from kivy.clock import Clock
from mutagen.mp4 import MP4

import utils
from utils import get_app_writable_dir

ProgressCb = Optional[Callable[[int, int, str], None]]
DoneCb = Optional[Callable[[dict], None]]


def start_mediastore_recovery_background(
    store,
    *,
    relative_path_prefix: str = "Music/YouTube Music Player/",
    dest_subdir: str = "Downloaded/Played",
    overwrite: bool = False,
    request_permission: bool = True,
    extract_covers: bool = True,
    max_workers: int = 2,
    max_items: Optional[int] = None,
    on_progress: ProgressCb = None,
    on_done: DoneCb = None,
    once_store_key: str = "media_recovery_done",
    force: bool = False,
) -> bool:
    """
    Kick off MediaStore recovery in background (Android only).
    Returns True if a background job was started (False if not needed or not Android).
    """
    if utils.get_platform() != "android":
        return False

    with contextlib.suppress(Exception):
        already = (
            (store is not None)
            and store.exists(once_store_key)
            and store.get(once_store_key).get("done", False)
        )
    if already and not force:
        _dispatch_done(
            on_done,
            {
                "found": 0,
                "copied": 0,
                "skipped": 0,
                "covers": 0,
                "errors": 0,
                "paths": [],
                "already_done": True,
            },
        )
        return False

    if request_permission:
        _request_read_media_audio_permission()

    rows = list(_query_mediastore_audio(relative_path_prefix))
    if not rows:
        rows = list(_query_mediastore_audio("Music/"))
    total = len(rows)
    if total == 0:
        _dispatch_done(
            on_done,
            {
                "found": 0,
                "copied": 0,
                "skipped": 0,
                "covers": 0,
                "errors": 0,
                "paths": [],
            },
        )
        return True

    dest_root = get_app_writable_dir(dest_subdir)
    os.makedirs(dest_root, exist_ok=True)

    if max_items:
        rows = rows[:max_items]
        total = len(rows)

    lock = threading.Lock()
    summary = {
        "found": total,
        "copied": 0,
        "skipped": 0,
        "covers": 0,
        "errors": 0,
        "paths": [],
    }
    done_count = 0

    def _progress(name: str):
        nonlocal done_count
        done_count += 1
        if on_progress:
            _dispatch_progress(on_progress, done_count, total, name)

    def _work(row: Tuple[str, object]) -> None:
        display_name, content_uri = row
        name = (display_name or "unnamed.m4a").strip()
        base, ext = os.path.splitext(name)
        safe_base = _safe_base(base)
        dest_audio = os.path.join(dest_root, safe_base + (ext or ".m4a"))
        dest_cover = os.path.join(dest_root, f"{safe_base}.jpg")

        try:
            if not overwrite and os.path.exists(dest_audio):
                with lock:
                    summary["skipped"] += 1
                _progress(name)
                return

            if not _copy_from_mediastore_to_private(content_uri, dest_audio):
                with lock:
                    summary["errors"] += 1
                _progress(name)
                return

            with lock:
                summary["copied"] += 1
                summary["paths"].append(dest_audio)

            if extract_covers and _extract_cover_jpg(dest_audio, dest_cover):
                with lock:
                    summary["covers"] += 1

        except Exception as e:
            print(f"[recovery-bg] worker failed: {e}")
            with lock:
                summary["errors"] += 1
        finally:
            _progress(name)

    def _runner():
        try:
            workers = max(1, min(max_workers, 4))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_work, r) for r in rows]
                for fut in as_completed(futures):
                    with contextlib.suppress(Exception):
                        fut.result()
        finally:
            with contextlib.suppress(Exception):
                if store is not None:
                    store.put(
                        once_store_key,
                        done=True,
                        ts=int(time.time()),
                        copied=summary.get("copied", 0),
                        found=summary.get("found", 0),
                    )
            _dispatch_done(on_done, summary)

    threading.Thread(target=_runner, daemon=True).start()
    return True


def _dispatch_progress(cb: ProgressCb, done: int, total: int, name: str):
    if not cb:
        return
    if Clock:
        Clock.schedule_once(lambda *_: cb(done, total, name), 0)
    else:
        cb(done, total, name)


def _dispatch_done(cb: DoneCb, summary: dict):
    if not cb:
        return
    if Clock:
        Clock.schedule_once(lambda *_: cb(summary), 0)
    else:
        cb(summary)


def _request_read_media_audio_permission():
    with contextlib.suppress(Exception):
        from android.permissions import Permission, request_permissions

        request_permissions(
            [Permission.READ_MEDIA_AUDIO, Permission.READ_EXTERNAL_STORAGE]
        )


def _query_mediastore_audio(relative_path_prefix: str) -> Iterable[Tuple[str, object]]:
    rows: list[Tuple[str, object]] = []
    try:
        from jnius import autoclass

        MediaStore = autoclass("android.provider.MediaStore")
        ContentUris = autoclass("android.content.ContentUris")
        PythonActivity = autoclass("org.kivy.android.PythonActivity")

        activity = PythonActivity.mActivity
        cr = activity.getContentResolver()
        uri = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI

        projection = ["_id", "display_name", "mime_type", "relative_path"]
        selection = "mime_type LIKE ? AND relative_path LIKE ?"
        args = ["audio/%", f"{relative_path_prefix}%"]

        cursor = cr.query(uri, projection, selection, args, None)
        try:
            if cursor is None:
                return rows
            id_idx = cursor.getColumnIndexOrThrow("_id")
            name_idx = cursor.getColumnIndexOrThrow("display_name")
            while cursor.moveToNext():
                _id = cursor.getLong(id_idx)
                name = cursor.getString(name_idx)
                content_uri = ContentUris.withAppendedId(uri, _id)
                rows.append((name, content_uri))
        finally:
            cursor.close()
    except Exception as e:
        print(f"[recovery-bg] query failed: {e}")
    return rows


def _copy_from_mediastore_to_private(content_uri, dest_path: str) -> bool:
    try:
        from jnius import autoclass

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        cr = activity.getContentResolver()
        ins = cr.openInputStream(content_uri)
        if ins is None:
            return False

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as out:
            buf = bytearray(1024 * 1024)
            while True:
                n = ins.read(buf)
                if n <= 0:
                    break
                out.write(memoryview(buf)[:n])
        ins.close()
        return True
    except Exception as e:
        print(f"[recovery-bg] copy failed: {e}")
        return False


def _extract_cover_jpg(audio_path: str, dest_cover_path: str) -> bool:
    if MP4 is None:
        return False
    try:
        if not os.path.exists(audio_path):
            return False
        mp4 = MP4(audio_path)
        covr = mp4.tags.get("covr")
        if not covr:
            return False
        data = bytes(covr[0])
        os.makedirs(os.path.dirname(dest_cover_path), exist_ok=True)
        with open(dest_cover_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"[recovery-bg] cover extract failed: {e}")
        return False


def _safe_base(name: str) -> str:
    try:
        return utils.safe_filename(name)
    except Exception:
        return (
            "".join(ch for ch in name if ch.isalnum() or ch in (" ", "-", "_")).strip()
            or "file"
        )

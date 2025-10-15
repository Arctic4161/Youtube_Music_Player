import contextlib
import os
import re
import sys
import time
from os import environ
from pathlib import Path


def get_platform():
    kivy_build = environ.get("KIVY_BUILD", "")
    if kivy_build in {"android", "ios"}:
        return kivy_build
    elif "P4A_BOOTSTRAP" in environ or "ANDROID_ARGUMENT" in environ:
        return "android"
    else:
        return None


def _desktop_downloads_dir() -> str:
    """
    Return the OS "Downloads" folder on desktop platforms.
    - Windows: uses Known Folders API (FOLDERID_Downloads).
    - macOS/Linux: uses ~/Downloads if present, else ~.
    """
    if sys.platform.startswith("win"):
        with contextlib.suppress(Exception):
            return find_real_downloads()
        return os.path.join(os.path.expanduser("~"), "Downloads")
    cand = os.path.join(os.path.expanduser("~"), "Downloads")
    return cand if os.path.isdir(cand) else os.path.expanduser("~")


def find_real_downloads():
    import ctypes
    from ctypes import wintypes as wt

    _FID = wt.GUID("{374DE290-123F-4565-9164-39C4925E467B}")
    buf = wt.LPWSTR()
    ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(_FID), 0, None, ctypes.byref(buf)
    )
    return buf.value


def safe_filename(name: str, default_prefix="track", max_len=120) -> str:
    if not name:
        name = ""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:max_len].rstrip(" .") or f"{default_prefix}_{int(time.time())}"
    return name


def get_app_writable_dir(subpath: str = "") -> str:
    sub = (subpath or "").strip().lstrip("/").replace("\\", "/")

    if get_platform() == "android":
        return android_write_directory(sub)

    try:
        dest = Path(_desktop_downloads_dir()) / "YouTube Music Player"
        dest = dest / sub if sub else dest
        dest.mkdir(parents=True, exist_ok=True)
        return str(dest)
    except Exception:
        dest = (
            os.path.expanduser(os.path.join("~", sub))
            if sub
            else os.path.expanduser("~")
        )
        os.makedirs(dest, exist_ok=True)
        return dest


def android_write_directory(sub: str) -> str:
    """
    Resolve an app-writable directory on Android that works from both the Activity
    and the Service. If sub starts with 'Download', use the app's Downloads sandbox.
    Always creates the directory.
    """

    sub = (sub or "").strip().lstrip("/").replace("\\", "/")
    from jnius import autoclass

    ctx = None
    try:
        PythonService = autoclass("org.kivy.android.PythonService")
        ctx = PythonService.mService
    except Exception:
        pass
    if ctx is None:
        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            ctx = PythonActivity.mActivity
        except Exception:
            pass
    if ctx is None:
        try:
            ActivityThread = autoclass("android.app.ActivityThread")
            app = ActivityThread.currentApplication()
            ctx = app if app else None
        except Exception:
            ctx = None
    if ctx is None:
        raise RuntimeError("No Android Context available (service/activity).")

    Environment = autoclass("android.os.Environment")

    base_dir = ctx.getExternalFilesDir(None)
    base = (
        base_dir.getAbsolutePath() if base_dir else ctx.getFilesDir().getAbsolutePath()
    )
    if sub and sub.split("/", 1)[0].lower().startswith("download"):
        dl = ctx.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
        if dl:
            base = dl.getAbsolutePath()

    dest = os.path.join(base, "YouTube Music Player", sub) if sub else base
    os.makedirs(dest, exist_ok=True)
    return dest

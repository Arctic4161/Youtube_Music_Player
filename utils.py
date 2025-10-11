import contextlib
import os
import sys
from os import environ


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


def get_app_writable_dir(subdir: str = "") -> str:
    """
    Android -> app-specific files dir (scoped storage safe).
      /storage/emulated/0/Android/data/<package>/files   (preferred)
      or internal: /data/user/0/<package>/files
    Desktop -> if subdir starts with 'Download/' or 'Downloads/', use the OS Downloads folder,
               otherwise use the user's home directory.
    Ensures the directory exists and returns the full path.
    """
    if get_platform() == "android":
        try:
            from android import mActivity

            ctx = mActivity.getApplicationContext()
            ext = ctx.getExternalFilesDir(None)
            base = ext.getAbsolutePath() if ext else ctx.getFilesDir().getAbsolutePath()
        except Exception:
            base = os.path.expanduser("~")
    elif subdir.lower().startswith(("download/", "downloads/")):
        base = _desktop_downloads_dir()
        parts = subdir.split("/", 1)
        subdir = parts[1] if len(parts) > 1 else ""
    else:
        base = os.path.expanduser("~")

    path = os.path.join(base, subdir) if subdir else base
    os.makedirs(path, exist_ok=True)
    return path

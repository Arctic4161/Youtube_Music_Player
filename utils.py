import contextlib
import os
import sys
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


def get_app_writable_dir(subpath: str = "") -> str:
    """
    Return an app-writable directory. On Android, this is the app-specific external
    files dir (optionally under its Downloads bucket). On desktop, it maps to
    ~/Downloads (or ~/Documents if you prefer).
    """
    sub = (
        (subpath or "").strip().lstrip("/")
    )  # prevent absolute paths like "/Download/.."

    if get_platform() == "android":
        return android_write_directory(sub)
    home = Path.home()

    downloads = (
        home / "Downloads"
        if (home / "Downloads").exists()
        else home  # fallback if Downloads doesn't exist
    )
    dest = downloads / sub if sub else downloads
    dest.mkdir(parents=True, exist_ok=True)
    return str(dest)


def android_write_directory(sub):
    from jnius import autoclass

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Environment = autoclass("android.os.Environment")
    activity = PythonActivity.mActivity

    base = (
        base_dir.getAbsolutePath()
        if (base_dir := activity.getExternalFilesDir(None))
        else activity.getFilesDir().getAbsolutePath()
    )
    if sub and sub.split("/", 1)[0].lower().startswith("download"):
        if dl := activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS):
            base = dl.getAbsolutePath()

    dest = os.path.join(base, sub) if sub else base
    os.makedirs(dest, exist_ok=True)
    return dest

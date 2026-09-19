import contextlib
import os
import re
import sys
import time
from os import environ
from functools import lru_cache
from pathlib import Path

_TEMP_DOWNLOAD_SUFFIXES = (".part", ".webm", ".ytdl")


def cleanup_download_artifacts(directory: str) -> tuple[str, ...]:
    """Remove interrupted-download artifacts only from one managed directory."""

    root = Path(directory)
    if not root.is_dir():
        return ()
    removed: list[str] = []
    for candidate in root.iterdir():
        if not candidate.is_file():
            continue
        if not candidate.name.casefold().endswith(_TEMP_DOWNLOAD_SUFFIXES):
            continue
        try:
            candidate.unlink()
        except OSError:
            continue
        removed.append(str(candidate))
    return tuple(removed)


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


def find_real_downloads() -> str:
    """Resolve the redirected Windows Downloads folder and release its buffer."""
    import ctypes
    from ctypes import wintypes as wt
    from uuid import UUID

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder_id = GUID.from_buffer_copy(
        UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
    )
    # Each call owns its function signatures, including its GUID type.
    shell = ctypes.WinDLL("shell32")
    ole = ctypes.WinDLL("ole32")
    resolve = shell.SHGetKnownFolderPath
    resolve.argtypes = [ctypes.POINTER(GUID), wt.DWORD, wt.HANDLE,
                       ctypes.POINTER(wt.LPWSTR)]
    resolve.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_int32
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    initialized = ole.CoInitializeEx(None, 2)
    # RPC_E_CHANGED_MODE means this thread already has a different COM apartment.
    if initialized < 0 and initialized != -2147417850:
        raise OSError(f"COM initialization failed: 0x{initialized & 0xffffffff:08x}")
    buffer = wt.LPWSTR()
    try:
        result = resolve(ctypes.byref(folder_id), 0, None, ctypes.byref(buffer))
        if result < 0 or not buffer.value:
            raise OSError(f"Downloads lookup failed: 0x{result & 0xffffffff:08x}")
        return buffer.value
    finally:
        if buffer:
            ole.CoTaskMemFree(ctypes.cast(buffer, ctypes.c_void_p))
        if initialized >= 0:
            ole.CoUninitialize()


@lru_cache(maxsize=1)
def _desktop_app_root() -> Path:
    """Keep an existing library at the old fallback location; never move files."""
    resolved = Path(_desktop_downloads_dir()) / "YouTube Music Player"
    if sys.platform.startswith("win"):
        legacy = Path.home() / "Downloads" / "YouTube Music Player"
        played = legacy / "Downloaded" / "Played"
        try:
            has_library = played.is_dir() and any(
                item.is_file() and (
                    item.suffix.lower() in {".m4a", ".mp3", ".aac", ".flac", ".ogg", ".wav", ".part"}
                    or item.name in {"playlists.json", "playlists.json.bak"}
                )
                for item in played.iterdir()
            )
            if has_library:
                return legacy
        except OSError:
            # Retain the established library location when it cannot be inspected.
            if played.is_dir():
                return legacy
    return resolved


def safe_filename(name: str, default_prefix="track", max_len=120) -> str:
    if not name:
        name = ""
    name = re.sub(r"['\u2018\u2019]", "", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:max_len].rstrip(" .") or f"{default_prefix}_{int(time.time())}"
    return name


def get_app_writable_dir(subpath: str = "") -> str:
    sub = (subpath or "").strip().lstrip("/").replace("\\", "/")

    if get_platform() == "android":
        return android_write_directory(sub)

    try:
        dest = _desktop_app_root()
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
    with contextlib.suppress(Exception):
        PythonService = autoclass("org.kivy.android.PythonService")
        ctx = PythonService.mService
    if ctx is None:
        with contextlib.suppress(Exception):
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            ctx = PythonActivity.mActivity
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

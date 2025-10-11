from __future__ import annotations

import contextlib
import json
from typing import Callable, Optional, Sequence
import utils


def _sdk_int() -> int:
    if utils.get_platform() == "android":
        return 0
    try:
        from jnius import autoclass

        VERSION = autoclass("android.os.Build$VERSION")
        return int(VERSION.SDK_INT)
    except Exception:
        return 0


class DownloadsAccess:
    """
    Manages:
      - Runtime permission requests appropriate to the device's Android version.
      - SAF folder selection for the public Downloads directory.
      - Simple read/write helpers in the selected Downloads tree.
    """

    def __init__(self, settings_path: str = "storage_settings.json"):
        self.settings_path = settings_path
        self.tree_uri: Optional[str] = None
        self._load_settings()

    def request_runtime_permissions(
            self, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Request best-effort runtime permissions:
          - Android 13+ (SDK >= 33): READ_MEDIA_AUDIO/IMAGES/VIDEO
          - Android 10-12 (SDK 29-32): READ_EXTERNAL_STORAGE (WRITE is ignored on 30+)
          - Android 9 and below (SDK <= 28): READ/WRITE_EXTERNAL_STORAGE
        """
        if utils.get_platform() == "android":
            if callback:
                callback(True)
            return

        sdk = _sdk_int()
        try:
            from android.permissions import (
                Permission,
                request_permissions,
            )
        except Exception:
            if callback:
                callback(False)
            return

        if sdk >= 33:
            perms = [
                Permission.READ_MEDIA_AUDIO,
                Permission.READ_MEDIA_IMAGES,
                Permission.READ_MEDIA_VIDEO,
            ]
        elif sdk >= 30:
            perms = [Permission.READ_EXTERNAL_STORAGE]
        else:
            perms = [
                Permission.READ_EXTERNAL_STORAGE,
                Permission.WRITE_EXTERNAL_STORAGE,
            ]

        def _cb(perms_result):
            ok = all(bool(x) for x in perms_result) if perms_result else False
            if callback:
                callback(ok)

        try:
            request_permissions(perms, _cb)
        except TypeError:
            try:
                request_permissions(perms)
                if callback:
                    callback(True)
            except Exception:
                if callback:
                    callback(False)

    def show_permission_popup(
            self, on_result: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Opens a system picker to grant access to the public 'Downloads' folder.
        Persists the tree URI if selected.
        """
        if utils.get_platform() == "android":
            if on_result:
                on_result(True)
            return

        try:
            from androidstorage4kivy import SharedStorage

            ss = SharedStorage()
            if uri := ss.open_document_tree(initial_uri="downloads"):
                self.tree_uri = uri
                with contextlib.suppress(Exception):
                    ss.persist_uri_permissions(uri)
                self._save_settings()
                if on_result:
                    on_result(True)
            elif on_result:
                on_result(False)
        except Exception:
            if on_result:
                on_result(False)

    def exists(self, relative_path: str) -> bool:
        if utils.get_platform() == "android" or not self.tree_uri:
            return False
        try:
            from androidstorage4kivy import SharedStorage

            ss = SharedStorage()
            return bool(ss.exists(self.tree_uri, relative_path))
        except Exception:
            return False

    def read_bytes(self, relative_path: str) -> Optional[bytes]:
        if utils.get_platform() == "android" or not self.tree_uri:
            return None
        try:
            from androidstorage4kivy import SharedStorage

            ss = SharedStorage()
            return ss.read_bytes(self.tree_uri, relative_path)
        except Exception:
            return None

    def write_bytes(self, relative_path: str, data: bytes) -> bool:
        if utils.get_platform() == "android" or not self.tree_uri:
            return False
        try:
            from androidstorage4kivy import SharedStorage

            ss = SharedStorage()
            ss.write_bytes(self.tree_uri, relative_path, data)
            return True
        except Exception:
            return False

    def read_text(self, relative_path: str) -> Optional[str]:
        b = self.read_bytes(relative_path)
        return b.decode("utf-8") if b is not None else None

    def write_text(self, relative_path: str, text: str) -> bool:
        return self.write_bytes(relative_path, text.encode("utf-8"))

    def _load_settings(self) -> None:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.tree_uri = data.get("downloads_tree_uri") or None
        except Exception:
            self.tree_uri = None

    def _save_settings(self) -> None:
        data = {"downloads_tree_uri": self.tree_uri}
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

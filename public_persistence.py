import contextlib
import json
import os


def _is_android():
    try:
        from kivy.utils import platform

        return platform == "android"
    except Exception:
        return False


def publish_playlists_json(
    playlists_dict: dict,
    subdir: str = "Documents/YouTube Music Player",
    filename: str = "playlists.json",
) -> str:
    """
    Publish playlists.json to shared storage via MediaStore (Downloads).
    Returns a content:// URI string on Android; raises if not running on Android.
    """
    if not _is_android():
        raise RuntimeError(
            "publish_playlists_json is Android-only. Use desktop export to user folder instead."
        )

    from androidstorage4kivy import SharedStorage

    ss = SharedStorage()
    cache_dir = ss.get_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    private_tmp = os.path.join(cache_dir, filename)

    with open(private_tmp, "w", encoding="utf-8") as f:
        json.dump(playlists_dict, f, ensure_ascii=False, indent=2)

    public_uri = None
    try:
        public_uri = ss.copy_to_shared(
            private_file=private_tmp, rel_path=f"Download/{subdir}"
        )
    except TypeError:
        public_uri = ss.copy_to_shared(private_file=private_tmp)

    with contextlib.suppress(Exception):
        os.remove(private_tmp)

    return str(public_uri)


def try_restore_playlists(
    store,
    manager,
    subdir: str = "Documents/YouTube Music Player",
    filename: str = "playlists.json",
) -> bool:
    """
    Attempts to restore playlists from shared storage.
    Order:
      1) If store has a remembered URI at key 'public_playlists_uri', open and load it.
      2) Else, scan Downloads/<subdir>/ for playlists.json and load it.
      3) Else, return False.
    On success, manager is populated and its own save() may be called by the caller.
    """
    if not _is_android():
        return False

    from androidstorage4kivy import SharedStorage

    ss = SharedStorage()
    uri = None
    try:
        if store and store.exists("public_playlists_uri"):
            uri = store.get("public_playlists_uri").get("uri")
    except Exception:
        uri = None

    def _load_from_uri(u: str):
        try:
            with ss.open(u, "r") as f:
                blob = f.read()
            data = json.loads(blob.decode("utf-8"))
            if hasattr(manager, "load_from_dict"):
                manager.load_from_dict(data)
            elif hasattr(manager, "replace_all"):
                pls = data.get("playlists", [])
                manager.replace_all(pls)
            else:
                manager._data = data
            return True
        except Exception:
            return False

    if uri and _load_from_uri(uri):
        return True

    with contextlib.suppress(Exception):
        target_rel = f"Download/{subdir}".rstrip("/")
        entries = ss.listdir(target_rel)
        for e in entries:
            if e.name == filename and _load_from_uri(e.uri):
                if store:
                    with contextlib.suppress(Exception):
                        store.put("public_playlists_uri", uri=str(e.uri))
                return True
    return False


def wire_public_export(
    manager,
    store,
    subdir: str = "Documents/YouTube Music Player",
    remember_uri: bool = True,
):
    """
    Wraps manager.save() so every time it saves internally, we also publish playlists.json
    to shared storage. We don't modify manager internals; we just replace the bound method.
    """
    if not hasattr(manager, "save"):
        raise AttributeError("Manager has no save() to wrap.")

    original_save = manager.save

    def wrapped_save(*args, **kwargs):
        res = original_save(*args, **kwargs)
        with contextlib.suppress(Exception):
            if hasattr(manager, "to_dict"):
                data = manager.to_dict()
            elif hasattr(manager, "_to_dict"):
                data = manager._to_dict()
            else:
                data = getattr(manager, "data", None) or getattr(manager, "_data", None)
                if data is None:
                    return res
            uri = publish_playlists_json(data, subdir=subdir, filename="playlists.json")
            if remember_uri and store:
                with contextlib.suppress(Exception):
                    store.put("public_playlists_uri", uri=str(uri))
        return res

    manager.save = wrapped_save

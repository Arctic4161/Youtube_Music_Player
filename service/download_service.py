"""Short-lived Android foreground service for user-requested downloads."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from oscpy.server import OSCThreadServer

from service.main import Gui_sounds
from service_lifecycle import clear_download_cancellation
from utils import get_app_writable_dir


DOWNLOAD_OSC_PORT = 3001
_ACTIVE_DOWNLOAD_FILENAME = ".active_download.json"
_DOWNLOAD_WAKE = None


def _android_service_instance():
    from jnius import autoclass

    return autoclass("org.kivy.android.PythonService").mService


def _active_download_path() -> Path:
    """Return the app-private checkpoint for the one supported download job."""

    return Path(get_app_writable_dir("Downloaded")) / _ACTIVE_DOWNLOAD_FILENAME


def _request_id(payload: str) -> str | None:
    """Accept only a complete, request-correlated download payload."""

    try:
        request = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(request, dict):
        return None
    required = ("request_id", "url", "title", "download_dir")
    if any(not str(request.get(key) or "").strip() for key in required):
        return None
    return str(request["request_id"])


def _persist_active_download(payload: str) -> str | None:
    """Atomically save a resumable request before its worker is launched."""

    request_id = _request_id(payload)
    if request_id is None:
        return None
    path = _active_download_path()
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    return request_id


def _load_active_download() -> str | None:
    """Load a validated checkpoint after Android restarts the sticky service."""

    try:
        payload = _active_download_path().read_text(encoding="utf-8")
    except OSError:
        return None
    return payload if _request_id(payload) is not None else None


def _clear_active_download(request_id: str) -> None:
    """Remove only the checkpoint belonging to the terminal job."""

    path = _active_download_path()
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError:
        return
    if _request_id(payload) != request_id:
        return
    with contextlib.suppress(OSError):
        path.unlink()


def acquire_download_wakelock() -> bool:
    """Keep the CPU running while a foreground download has active work."""

    global _DOWNLOAD_WAKE
    if _DOWNLOAD_WAKE is not None:
        with contextlib.suppress(Exception):
            if _DOWNLOAD_WAKE.isHeld():
                return True
    try:
        service = _android_service_instance()
        context = service.getApplicationContext()
        from jnius import autoclass

        PowerManager = autoclass("android.os.PowerManager")
        manager = context.getSystemService(context.POWER_SERVICE)
        wake = manager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "youtubemusicplayer:download",
        )
        wake.setReferenceCounted(False)
        wake.acquire()
        _DOWNLOAD_WAKE = wake
        return True
    except Exception as exc:
        print(f"[download] wake lock unavailable: {exc}")
        return False


def release_download_wakelock() -> None:
    """Release the download CPU lock when the job reaches a terminal state."""

    global _DOWNLOAD_WAKE
    try:
        if _DOWNLOAD_WAKE is not None and _DOWNLOAD_WAKE.isHeld():
            _DOWNLOAD_WAKE.release()
    except Exception as exc:
        print(f"[download] wake lock release failed: {exc}")
    finally:
        _DOWNLOAD_WAKE = None


def run_download_service(payload: str | None = None) -> None:
    """Run or recover one download request, then stop at a terminal result."""

    controller = Gui_sounds()
    server = OSCThreadServer(encoding="utf8")
    request_id = None
    completed = False
    try:
        server.listen("localhost", port=DOWNLOAD_OSC_PORT, default=True)
        server.bind("/cancel_download", controller.cancel_download)
        server.bind("/download_status", controller.report_download_status)
        request_payload = payload
        if request_payload is None:
            request_payload = os.environ.get("PYTHON_SERVICE_ARGUMENT", "")
        if not request_payload.strip():
            request_payload = _load_active_download() or ""
            if request_payload:
                print("[download] resuming the last active request")
        request_id = _persist_active_download(request_payload)
        if request_id is None:
            controller.download_yt(request_payload)
            return
        acquire_download_wakelock()
        controller.download_yt(request_payload)
        with controller._download_lock:
            worker = controller._download_thread
        if worker is not None:
            worker.join()
            completed = True
        else:
            # download_yt already reported its synchronous launch failure.
            completed = True
    finally:
        if completed and request_id is not None:
            _clear_active_download(request_id)
            with contextlib.suppress(OSError):
                clear_download_cancellation(get_app_writable_dir("Downloaded"), request_id)
        release_download_wakelock()
        with contextlib.suppress(Exception):
            server.stop_all()
        with contextlib.suppress(Exception):
            service = _android_service_instance()
            service.stopForeground(True)
            service.stopSelf()


if __name__ == "__main__":
    run_download_service()

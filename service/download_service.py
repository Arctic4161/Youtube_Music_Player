"""Short-lived Android foreground service for user-requested downloads."""

from __future__ import annotations

import contextlib
import os

from oscpy.server import OSCThreadServer

from service.main import Gui_sounds


DOWNLOAD_OSC_PORT = 3001


def _android_service_instance():
    from jnius import autoclass

    return autoclass("org.kivy.android.PythonService").mService


def run_download_service(payload: str | None = None) -> None:
    """Run one download request, accept cancellation, then stop the service."""

    controller = Gui_sounds()
    server = OSCThreadServer(encoding="utf8")
    try:
        server.listen("localhost", port=DOWNLOAD_OSC_PORT, default=True)
        server.bind("/cancel_download", controller.cancel_download)
        request_payload = (
            payload
            if payload is not None
            else os.environ.get("PYTHON_SERVICE_ARGUMENT", "")
        )
        controller.download_yt(request_payload)
        with controller._download_lock:
            worker = controller._download_thread
        if worker is not None:
            worker.join()
    finally:
        with contextlib.suppress(Exception):
            server.stop_all()
        with contextlib.suppress(Exception):
            service = _android_service_instance()
            service.stopForeground(True)
            service.stopSelf()


if __name__ == "__main__":
    run_download_service()

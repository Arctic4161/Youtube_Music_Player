"""Small platform adapters used only by temporary Radio streams."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Mapping

import certifi

from download_config import configured_proxy
from radio_proxy import RadioProxy


_ANDROID_MEDIA3_BRIDGE_TYPE = None


class RadioPlayerError(RuntimeError):
    """Raised when the selected Radio backend cannot load or play a stream."""


def _header_value(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.casefold() == name.casefold():
            return str(value)
    return ""


def preload_android_media3_bridge() -> None:
    """Resolve the app bridge before Radio work moves onto a Python thread.

    Android's JNI ``FindClass`` only sees app classes when called from the
    service's Java-owned startup thread. Stream resolution happens later on a
    Python worker, where the same lookup is limited to framework classes.
    """

    global _ANDROID_MEDIA3_BRIDGE_TYPE
    if _ANDROID_MEDIA3_BRIDGE_TYPE is None:
        from jnius import autoclass

        _ANDROID_MEDIA3_BRIDGE_TYPE = autoclass(
            "com.youtubemusicplayer.bridge.RadioMedia3Player"
        )


def _android_media3_bridge_type():
    preload_android_media3_bridge()
    return _ANDROID_MEDIA3_BRIDGE_TYPE


class FFPyRadioPlayer:
    """Raw ffpyplayer wrapper; unlike Kivy SoundFFPy, resume keeps position."""

    loop = False

    def __init__(self, url: str, headers: Mapping[str, str], *, proxy_url: str = "") -> None:
        try:
            from ffpyplayer.player import MediaPlayer
        except ImportError as exc:  # pragma: no cover - depends on package install
            raise RadioPlayerError("ffpyplayer is not installed for Radio playback.") from exc

        header_lines = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        ff_opts = {"autoexit": True, "paused": True}
        lib_opts = {"tls_verify": "1", "ca_file": certifi.where()}
        if header_lines:
            lib_opts["headers"] = header_lines
        user_agent = _header_value(headers, "User-Agent")
        if user_agent:
            lib_opts["user_agent"] = user_agent
        referer = _header_value(headers, "Referer")
        if referer:
            lib_opts["referer"] = referer
        self._proxy = None
        # Native decoder threads can report errors during MediaPlayer creation.
        self._failed = threading.Event()
        try:
            self._proxy = RadioProxy(url, proxy_url or configured_proxy())
            lib_opts["http_proxy"] = self._proxy.url
            self._player = MediaPlayer(
                url, ff_opts=ff_opts, lib_opts=lib_opts, loglevel="quiet",
                callback=self._on_player_event,
            )
        except Exception as exc:  # pragma: no cover - backend-specific failure
            if self._proxy is not None:
                self._proxy.close()
            raise RadioPlayerError(f"Unable to create ffpyplayer stream: {exc}") from exc
        self._closed = threading.Event()
        self._released = threading.Event()
        self._player_lock = threading.RLock()
        self._close_scheduled = False
        self._state = "stop"
        # ffpyplayer 4.5.3 can terminate the Windows process when get_pts()
        # is called for an audio-only network stream. Keep a small logical
        # playback clock instead of crossing that native position API.
        self._position = 0.0
        self._position_started_at: float | None = None
        self.length = 0.0
        self._pump_thread = threading.Thread(
            target=self._pump,
            name="RadioFFPyPump",
            daemon=True,
        )
        self._pump_thread.start()
        self._wait_for_duration()

    @property
    def state(self) -> str:
        if self._failed.is_set():
            raise RadioPlayerError("Desktop Radio playback failed.")
        return self._state

    def _on_player_event(self, selector: str, _value) -> None:
        if selector in {"read:error", "audio:error", "video:error"}:
            self._failed.set()

    def _pump(self) -> None:
        max_position = 0.0
        while not self._closed.is_set():
            if self._failed.is_set():
                return
            try:
                with self._player_lock:
                    if self._closed.is_set():
                        return
                    _frame, wait = self._player.get_frame()
                    max_position = max(max_position, self.get_pos())
                    metadata = self._player.get_metadata() or {}
                    duration = metadata.get("duration")
                    if duration:
                        self.length = max(0.0, float(duration))
                if wait == "eof":
                    with self._player_lock:
                        max_position = max(max_position, self._freeze_position_locked())
                        self._state = "stop"
                    if max_position <= 0.0:
                        self._failed.set()
                    return
                if wait == "paused":
                    self._closed.wait(0.05)
                elif isinstance(wait, (int, float)) and wait > 0:
                    self._closed.wait(min(float(wait), 0.25))
                else:
                    self._closed.wait(0.01)
            except Exception:
                self._state = "stop"
                self._failed.set()
                return

    def _wait_for_duration(self) -> None:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not self.length:
            with self._player_lock:
                with contextlib.suppress(Exception):
                    metadata = self._player.get_metadata() or {}
                    self.length = max(0.0, float(metadata.get("duration") or 0.0))
            if not self.length:
                time.sleep(0.05)

    def play(self) -> None:
        with self._player_lock:
            if self._closed.is_set():
                return
            position = self._current_position_locked()
            self._player.set_pause(False)
            self._position = position
            self._position_started_at = time.monotonic()
            self._state = "play"

    def stop(self) -> None:
        with self._player_lock:
            if self._closed.is_set():
                return
            position = self._current_position_locked()
            self._player.set_pause(True)
            self._position = position
            self._position_started_at = None
            self._state = "stop"

    def unload(self) -> None:
        self._closed.set()
        self._proxy.close()
        with contextlib.suppress(Exception):
            with self._player_lock:
                self._freeze_position_locked()
                self._state = "stop"
                self._player.set_pause(True)
        if self._pump_thread is not threading.current_thread():
            self._pump_thread.join(timeout=2.0)
        if self._pump_thread.is_alive():
            with self._player_lock:
                if not self._close_scheduled:
                    self._close_scheduled = True
                    threading.Thread(
                        target=self._close_after_pump,
                        name="RadioFFPyClose",
                        daemon=True,
                    ).start()
            return
        self._close_player()

    def _close_after_pump(self) -> None:
        """Never release native decoder memory while ``get_frame`` is active."""

        self._pump_thread.join()
        self._close_player()

    def _close_player(self) -> None:
        if self._released.is_set():
            return
        with self._player_lock:
            if self._released.is_set():
                return
            with contextlib.suppress(Exception):
                self._player.close_player()
            self._released.set()

    def seek(self, position: float) -> None:
        with self._player_lock:
            if self._closed.is_set():
                return
            target = max(0.0, float(position))
            duration = max(0.0, float(self.length or 0.0))
            if duration:
                target = min(target, duration)
            self._player.seek(target, relative=False, accurate=True)
            self._position = target
            self._position_started_at = (
                time.monotonic() if self._state == "play" else None
            )

    def get_pos(self) -> float:
        with self._player_lock:
            return self._current_position_locked()

    def _current_position_locked(self) -> float:
        position = max(0.0, float(getattr(self, "_position", 0.0) or 0.0))
        started_at = getattr(self, "_position_started_at", None)
        if started_at is not None:
            position += max(0.0, time.monotonic() - float(started_at))
        duration = max(0.0, float(getattr(self, "length", 0.0) or 0.0))
        return min(position, duration) if duration else position

    def _freeze_position_locked(self) -> float:
        position = self._current_position_locked()
        self._position = position
        self._position_started_at = None
        return position


class AndroidMedia3RadioPlayer:
    """Pyjnius wrapper around the Java Media3 player kept in ``android_src``."""

    loop = False

    def __init__(self, url: str, headers: Mapping[str, str], context, *, proxy_url: str = "") -> None:
        self._proxy = None
        try:
            bridge_type = _android_media3_bridge_type()
            self._bridge = bridge_type(context)
            self._proxy = RadioProxy(url, proxy_url or configured_proxy())
            self._bridge.load(
                url,
                _header_value(headers, "User-Agent"),
                _header_value(headers, "Referer"),
                _header_value(headers, "Origin"),
                self._proxy.port,
            )
        except Exception as exc:  # pragma: no cover - Android runtime only
            if self._proxy is not None:
                self._proxy.close()
            bridge = getattr(self, "_bridge", None)
            if bridge is not None:
                with contextlib.suppress(Exception):
                    bridge.release()
            raise RadioPlayerError(f"Unable to create Media3 Radio stream: {exc}") from exc
        self.length = 0.0

    @property
    def state(self) -> str:
        try:
            return "play" if self._bridge.isPlaybackActive() else "stop"
        except Exception as exc:
            diagnostic = getattr(
                getattr(self, "_proxy", None), "diagnostic", "no-tunnel-diagnostic"
            )
            raise RadioPlayerError(
                f"Android Radio playback failed: {exc}; tunnel={diagnostic}"
            ) from exc

    def _refresh_length(self) -> None:
        with contextlib.suppress(Exception):
            self.length = max(0.0, float(self._bridge.durationSeconds() or 0.0))

    @property
    def volume(self) -> float:
        return float(self._bridge.getVolume())

    @volume.setter
    def volume(self, value: float) -> None:
        self._bridge.setVolume(max(0.0, min(1.0, float(value))))

    def play(self) -> None:
        self._bridge.play()
        self._refresh_length()

    def stop(self) -> None:
        self._bridge.pause()

    def unload(self) -> None:
        self._proxy.close()
        self._bridge.release()

    def seek(self, position: float) -> None:
        self._bridge.seekSeconds(max(0.0, float(position)))

    def get_pos(self) -> float:
        self._refresh_length()
        with contextlib.suppress(Exception):
            return max(0.0, float(self._bridge.positionSeconds() or 0.0))
        return 0.0


def create_radio_player(url: str, headers: Mapping[str, str], *, platform: str, context=None, proxy_url: str = ""):
    """Create the Radio-only backend for the current runtime platform."""

    if platform == "android":
        if context is None:
            raise RadioPlayerError("Android Radio playback needs the service context.")
        return AndroidMedia3RadioPlayer(url, headers, context, proxy_url=proxy_url)
    return FFPyRadioPlayer(url, headers, proxy_url=proxy_url)

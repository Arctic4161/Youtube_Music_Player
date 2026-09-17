import contextlib
import json
import os
import sys
import time
import uuid
from runpy import run_path

import utils
from media_identity import audio_filename, display_title_from_stem, stable_media_id
from playback_logic import (
    DownloadRequestTracker,
    PlaybackSnapshot,
    PlaybackStatus,
    normalize_playback_status,
)
from service_lifecycle import start_android_service, stop_android_service
from search_logic import SearchResult, parse_search_results
from timer_lifecycle import cancel_event, replace_event
from ui_scaling import bounded_ui_scale
from utils import cleanup_download_artifacts, get_app_writable_dir

kivy_home = get_app_writable_dir("Downloaded")
os.makedirs(kivy_home, exist_ok=True)
os.environ["KIVY_HOME"] = kivy_home
os.environ["KIVY_NO_CONSOLELOG"] = "1"

if utils.get_platform() != "android":
    os.environ["KIVY_AUDIO"] = "gstplayer"
else:
    from android.permissions import Permission, check_permission, request_permissions
    from android.runnable import run_on_ui_thread
    from jnius import PythonJavaClass, autoclass, java_method


def _android_music_service_handles():
    """Return the generated foreground-service class and Activity context."""

    service_class = autoclass(
        "com.youtubemusicplayer.youtubemusicplayer.ServiceMusicservice"
    )
    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    return service_class, activity


def _android_download_service_handles():
    """Return the generated data-sync service class and Activity context."""

    service_class = autoclass(
        "com.youtubemusicplayer.youtubemusicplayer.ServiceDownloadservice"
    )
    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    return service_class, activity
from kivy.config import Config

if utils.get_platform() == "android":
    Config.set("input", "mtdev_%(name)s", "probesysfs,provider=mtdev")
    Config.set("input", "hid_%(name)s", "probesysfs,provider=hidinput")
    Config.set("postproc", "double_tap_distance", "20")
else:
    Config.set("input", "mouse", "mouse,disable_multitouch")
    Config.set("input", "wm_touch", "")
    Config.set("input", "wm_pen", "")

from threading import Thread

from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.factory import Factory
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import (
    BooleanProperty,
    NumericProperty,
    ObjectProperty,
    StringProperty,
)
from kivy.resources import resource_add_path, resource_find
from kivy.storage.jsonstore import JsonStore
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.utils import platform
from kivymd.app import MDApp
from kivymd.toast import toast
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDFlatButton
from kivymd.uix.dialog import MDDialog
from kivymd.uix.floatlayout import MDFloatLayout
from kivymd.uix.gridlayout import MDGridLayout
from kivymd.uix.slider import MDSlider
from kivymd.uix.textfield import MDTextField
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer


_android_insets_listener = None


if utils.get_platform() == "android":

    class _AndroidWindowInsetsListener(PythonJavaClass):
        """Keep Kivy content clear of enforced Android system-bar insets."""

        __javainterfaces__ = ["android/view/View$OnApplyWindowInsetsListener"]
        __javacontext__ = "app"

        def __init__(self, inset_type):
            super().__init__()
            self._inset_type = inset_type

        @java_method(
            "(Landroid/view/View;Landroid/view/WindowInsets;)"
            "Landroid/view/WindowInsets;"
        )
        def onApplyWindowInsets(self, view, window_insets):
            safe = window_insets.getInsets(self._inset_type)
            view.setPadding(safe.left, safe.top, safe.right, safe.bottom)
            return window_insets


    @run_on_ui_thread
    def _apply_android_system_bar_insets():
        """Install inset padding only where Android enforces edge-to-edge."""

        global _android_insets_listener
        BuildVersion = autoclass("android.os.Build$VERSION")
        if int(BuildVersion.SDK_INT) < 35:
            return
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        AndroidId = autoclass("android.R$id")
        content_view = activity.findViewById(AndroidId.content)
        if content_view is None:
            return
        InsetType = autoclass("android.view.WindowInsets$Type")
        inset_type = InsetType.systemBars() | InsetType.displayCutout()
        _android_insets_listener = _AndroidWindowInsetsListener(inset_type)
        content_view.setOnApplyWindowInsetsListener(_android_insets_listener)
        content_view.requestApplyInsets()

else:

    def _apply_android_system_bar_insets():
        """No-op on desktop platforms."""

        return None


from youtube_search_compat import create_video_search

from playlist_manager import PlaylistManager

DOWNLOAD_INACTIVITY_TIMEOUT_SECONDS = 120.0
SERVICE_RECONNECT_RETRY_SECONDS = 0.5
SERVICE_RECONNECT_TIMEOUT_SECONDS = 10.0


def default_cover_path():
    candidates = [
        "music.png",
        "music.ico",
    ]
    for rel in candidates:
        if getattr(sys, "frozen", False):
            resource_add_path(sys._MEIPASS)
        if p := resource_find(rel):
            return p
    return


class RecycleViewRow(BoxLayout):
    text = StringProperty()
    filename = StringProperty()


class PlaylistTrackRow(MDBoxLayout):
    text = StringProperty("")
    index = NumericProperty(-1)
    playlist_id = StringProperty("")

    def on_touch_down(self, touch):
        if "button" in touch.profile and touch.button != "left":
            return super().on_touch_down(touch)
        if getattr(touch, "is_mouse_scrolling", False) or touch.ud.get("was_scroll"):
            with contextlib.suppress(Exception):
                touch.ud["was_scroll"] = True
            return super().on_touch_down(touch)
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        d = self.ids.get("delete_btn", None)
        if d and d.collide_point(*touch.pos):
            self._touch_started_on_child = True
            self._touch_started_on_delete = True
            return True
        return True

    def on_touch_up(self, touch):
        if getattr(touch, "is_mouse_scrolling", False) or touch.ud.get("was_scroll"):
            with contextlib.suppress(Exception):
                touch.ud["was_scroll"] = True
            return super().on_touch_up(touch)
        if getattr(self, "_touch_started_on_delete", False):
            self._touch_started_on_delete = False
            d = self.ids.get("delete_btn", None)
            if d and d.collide_point(*touch.pos):
                with contextlib.suppress(Exception):
                    MDApp.get_running_app().root._playlist_remove_track(int(self.index))
                return True
            return True

        if self.collide_point(*touch.pos):
            with contextlib.suppress(Exception):
                MDApp.get_running_app().root._playlist_play_index(
                    int(self.index),
                    self.playlist_id,
                )
            return True

        return super().on_touch_up(touch)


class MySlider(MDSlider):
    sound = ObjectProperty()

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            setattr(GUILayout, "is_scrubbing", True)
            touch.grab(self)
            super().on_touch_down(touch)
            return True
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            try:
                secs = float(self.value)
            except Exception:
                secs = 0.0

            with contextlib.suppress(Exception):
                self.set_gui_to_play_from_touchup()

            Clock.schedule_once(
                lambda dt: GUILayout.send("seek_seconds", str(int(secs))), 0
            )
            Clock.schedule_once(lambda dt: GUILayout.send("iamawake", "ping"), 0.05)
            super().on_touch_up(touch)
            Clock.schedule_once(lambda dt: setattr(GUILayout, "is_scrubbing", False), 0)

            return True
        return super().on_touch_up(touch)

    def on_touch_cancel(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            Clock.schedule_once(lambda dt: setattr(GUILayout, "is_scrubbing", False), 0)
            super().on_touch_cancel(touch)
            return True
        return super().on_touch_cancel(touch)

    def set_gui_to_play_from_touchup(self):
        app = MDApp.get_running_app()
        root = app.root
        root.paused = False
        with contextlib.suppress(Exception):
            GUILayout.playing_song = True
        app.root.ids.play_btt.disabled = True
        app.root.ids.play_btt.opacity = 0
        app.root.ids.pause_btt.disabled = False
        app.root.ids.pause_btt.opacity = 1
        root.ids.song_position.opacity = 1
        root.ids.song_max.opacity = 1


class GUILayout(MDFloatLayout, MDGridLayout):
    store = ObjectProperty(None)
    gui_reset = False
    is_scrubbing = False
    service = None
    service_started = False
    download_client = None
    get_update_slider = None
    service_playback_status = PlaybackStatus.IDLE.value
    screen2_is_downloads = BooleanProperty(True)
    image_path = default_cover_path()
    set_local_download = get_app_writable_dir("Downloaded/Played")
    os.makedirs(set_local_download, exist_ok=True)

    def _start_music_service_user_initiated(self):
        if utils.get_platform() != "android":
            return
        try:
            # python-for-android service starts are idempotent. Calling this on each
            # foreground reconnect recovers a service Android killed independently.
            service_class, activity = _android_music_service_handles()
            GUILayout.service_started = start_android_service(
                service_class,
                context=activity,
                argument="musicservice",
            )
            print("[service] started via generated ServiceMusicservice")
            return
        except Exception as e:
            print("[service] generated service start unavailable:", e)

    def _ensure_music_service(self):
        """Start the service after the GUI's OSC listener is ready."""

        if utils.get_platform() == "android":
            self._start_music_service_user_initiated()
            return
        service = GUILayout.service
        if service is not None and service.is_alive():
            return
        GUILayout.service = Thread(
            target=run_path,
            args=[os.path.join(os.path.dirname(__file__), "./service/main.py")],
            kwargs={"run_name": "__main__"},
            daemon=True,
        )
        GUILayout.service.start()

    def _cancel_service_reconnect(self):
        cancel_event(getattr(self, "_service_reconnect_retry", None))
        cancel_event(getattr(self, "_service_reconnect_timeout", None))
        self._service_reconnect_retry = None
        self._service_reconnect_timeout = None
        self._service_reconnect_request_id = None

    def _send_service_reconnect(self, _dt=0):
        request_id = getattr(self, "_service_reconnect_request_id", None)
        if request_id is None:
            return False
        with contextlib.suppress(Exception):
            GUILayout.send("iamawake", json.dumps({"request_id": request_id}))
        return True

    def _service_reconnect_timed_out(self, _dt):
        if getattr(self, "_service_reconnect_request_id", None) is None:
            return
        self._cancel_service_reconnect()
        GUILayout.service_playback_status = PlaybackStatus.IDLE.value
        self._reset_to_startup_gui()

        def _show_timeout(_timeout_dt):
            with contextlib.suppress(Exception):
                self.ids.info.text = "Playback service did not respond."

        Clock.schedule_once(_show_timeout, 0)

    @mainthread
    def begin_service_reconnect(self, *_args):
        """Start or rediscover the service and request its complete state."""

        self._cancel_service_reconnect()
        self._ensure_music_service()
        self._service_reconnect_request_id = uuid.uuid4().hex
        GUILayout.service_playback_status = "pending"
        cancel_event(GUILayout.get_update_slider)
        GUILayout.get_update_slider = None
        with contextlib.suppress(Exception):
            if GUILayout.slider is not None:
                GUILayout.slider.disabled = True
            self.ids.next_btt.disabled = True
            self.ids.previous_btt.disabled = True
        self._send_service_reconnect()
        self._service_reconnect_retry = Clock.schedule_interval(
            self._send_service_reconnect,
            SERVICE_RECONNECT_RETRY_SECONDS,
        )
        self._service_reconnect_timeout = Clock.schedule_once(
            self._service_reconnect_timed_out,
            SERVICE_RECONNECT_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _format_playback_time(seconds: float) -> str:
        text = time.strftime("%H:%M:%S", time.gmtime(max(0.0, seconds)))
        return text[3:] if text.startswith("00:") else text

    def _show_snapshot_track(self, snapshot: PlaybackSnapshot):
        if not snapshot.track_name:
            return
        if snapshot.playback_mode == "radio":
            self.stream = None
            cover_path = snapshot.cover_path or default_cover_path()
            self.set_local = cover_path
            self.settitle = snapshot.track_name
            with contextlib.suppress(Exception):
                self.ids.imageView.source = str(cover_path)
                self.ids.song_title.text = (
                    f"{self.settitle[:51]}..."
                    if len(self.settitle) > 51
                    else self.settitle
                )
            return
        filename = os.path.basename(snapshot.track_name)
        title, _extension = os.path.splitext(filename)
        self.stream = os.path.join(self.set_local_download, filename)
        derived_cover = os.path.join(self.set_local_download, f"{title}.jpg")
        cover_path = snapshot.cover_path or derived_cover
        if not os.path.exists(cover_path):
            cover_path = default_cover_path()
        self.set_local = cover_path
        self.settitle = display_title_from_stem(title)
        with contextlib.suppress(Exception):
            self.ids.imageView.source = str(cover_path)
            if utils.get_platform() == "android":
                self.ids.imageView.size_hint_x = 0.7
                self.ids.imageView.size_hint_y = 0.7
            self.ids.song_title.text = (
                f"{self.settitle[:51]}..."
                if len(self.settitle) > 51
                else self.settitle
            )

    def _show_snapshot_timeline(self, snapshot: PlaybackSnapshot):
        if GUILayout.slider is None:
            self.make_slider()
        duration = max(0.0, snapshot.duration)
        position = max(0.0, snapshot.position)
        if duration > 0.0:
            position = min(position, duration)
        self.length = duration
        self.song_pos = position
        GUILayout.slider.max = max(duration, 1.0)
        GUILayout.slider.value = position
        self.ids.song_position.text = self._format_playback_time(position)
        self.ids.song_max.text = self._format_playback_time(
            max(0.0, duration - position)
        )
        self.ids.song_position.opacity = 1
        self.ids.song_max.opacity = 1
        self.fileosc_loaded = True

    def _apply_playback_snapshot(self, snapshot: PlaybackSnapshot):
        status = snapshot.status
        GUILayout.service_playback_status = status.value
        self._apply_radio_state_values(
            active=snapshot.playback_mode == "radio",
            available=snapshot.radio_available,
        )
        self.repeat_selected = snapshot.repeat_enabled
        self.shuffle_selected = snapshot.shuffle_enabled
        # Any loaded service queue owns Next/Previous. A one-song queue has no
        # destination, but it must never fall back to browsing search results.
        self.playlist_mode = (
            snapshot.playback_mode == "radio" or snapshot.queue_size >= 1
        )

        with contextlib.suppress(Exception):
            self.ids.repeat_btt.text_color = (
                1 if snapshot.repeat_enabled else 0,
                0,
                0,
                1,
            )
            self.ids.shuffle_btt.text_color = (
                1 if snapshot.shuffle_enabled else 0,
                0,
                0,
                1,
            )

        if status is PlaybackStatus.IDLE:
            self.repeat_selected = False
            self.shuffle_selected = False
            self.playlist_mode = False
            with contextlib.suppress(Exception):
                self.ids.repeat_btt.text_color = 0, 0, 0, 1
                self.ids.shuffle_btt.text_color = 0, 0, 0, 1
            self._reset_to_startup_gui()
            return

        self._show_snapshot_track(snapshot)
        if snapshot.track_name:
            self._show_snapshot_timeline(snapshot)

        has_queue_navigation = (
            snapshot.playback_mode == "radio" or snapshot.queue_size >= 2
        )
        is_radio = snapshot.playback_mode == "radio"
        is_loading = status is PlaybackStatus.LOADING
        self.paused = status is PlaybackStatus.PAUSED
        GUILayout.playing_song = status is PlaybackStatus.PLAYING

        with contextlib.suppress(Exception):
            self.ids.play_btt.disabled = status is not PlaybackStatus.PAUSED
            self.ids.play_btt.opacity = 1 if status is PlaybackStatus.PAUSED else 0
            self.ids.pause_btt.disabled = status is not PlaybackStatus.PLAYING
            self.ids.pause_btt.opacity = 1 if status is PlaybackStatus.PLAYING else 0
            self.ids.next_btt.disabled = is_loading or not has_queue_navigation
            self.ids.previous_btt.disabled = is_loading or not has_queue_navigation
            self.ids.next_btt.opacity = 1 if has_queue_navigation else 0
            self.ids.previous_btt.opacity = 1 if has_queue_navigation else 0
            self.ids.repeat_btt.disabled = is_loading
            self.ids.repeat_btt.opacity = 1
            self.ids.shuffle_btt.disabled = (
                is_loading or is_radio or not has_queue_navigation
            )
            self.ids.shuffle_btt.opacity = (
                1 if has_queue_navigation and not is_radio else 0
            )
            if is_loading and not self.ids.info.text:
                self.ids.info.text = "Preparing audio..."
            elif not is_loading:
                self.ids.info.text = ""
            if GUILayout.slider is not None:
                GUILayout.slider.disabled = is_loading
                GUILayout.slider.opacity = 1 if snapshot.track_name else 0

        cancel_event(GUILayout.get_update_slider)
        GUILayout.get_update_slider = None
        if status is PlaybackStatus.PLAYING:
            GUILayout.get_update_slider = replace_event(
                GUILayout.get_update_slider,
                lambda: Clock.schedule_interval(self.wait_update_slider, 1),
            )

    @mainthread
    def apply_playback_snapshot(self, *values):
        parts = []
        for value in values:
            if isinstance(value, (bytes, bytearray)):
                parts.append(bytes(value).decode("utf-8", "ignore"))
            else:
                parts.append(str(value))
        snapshot = PlaybackSnapshot.from_json("".join(parts))
        if snapshot is None:
            return
        active_request = getattr(self, "_service_reconnect_request_id", None)
        if active_request is not None and snapshot.request_id != active_request:
            return
        if active_request is None and snapshot.request_id:
            return
        self._cancel_service_reconnect()
        if utils.get_platform() == "android":
            GUILayout.service_started = True
        self._apply_playback_snapshot(snapshot)

    def _apply_radio_state_values(self, *, active: bool, available: bool) -> None:
        self.radio_active = bool(active)
        self.radio_available = bool(available)
        with contextlib.suppress(Exception):
            radio_btt = self.ids.radio_btt
            radio_btt.icon = "stop-circle-outline" if active else "radio"
            radio_btt.tooltip_text = "Stop Radio" if active else "Start Radio"
            radio_btt.disabled = not (active or available)
            radio_btt.opacity = 1 if (active or available) else 0

    @mainthread
    def apply_radio_state(self, *values):
        try:
            raw = "".join(
                bytes(value).decode("utf-8", "ignore")
                if isinstance(value, (bytes, bytearray))
                else str(value)
                for value in values
            )
            state = json.loads(raw)
            if not isinstance(state, dict):
                return
        except (TypeError, ValueError):
            return
        self._apply_radio_state_values(
            active=bool(state.get("active")),
            available=bool(state.get("available")),
        )

    def reset_for_new_query(self):
        """Remove the current result view before a replacement search starts."""
        self.stop()
        self._reset_to_startup_gui()
        app = MDApp.get_running_app()
        app.root.ids.info.text = ""
        app.root.ids.song_position.text = ""
        app.root.ids.song_max.text = ""
        app.root.ids.imageView.source = default_cover_path()
        app.root.ids.song_position.opacity = 0
        app.root.ids.song_max.opacity = 0
        if GUILayout.slider is not None:
            with contextlib.suppress(Exception):
                GUILayout.slider.value = 0
                GUILayout.slider.disabled = True
                GUILayout.slider.opacity = 0

    def on_song_not_found(self, *val):
        missing = "".join(val).strip() or "Selected track"

        def _do(dt):
            try:
                dlg = MDDialog(
                    title="Song not found",
                    text=f'"{missing}" could not be found. It may have been moved or deleted.',
                    buttons=[MDFlatButton(text="OK")],
                )
                btn = dlg.buttons[0]
                btn.bind(on_release=lambda *_: dlg.dismiss())
                dlg.open()
            except Exception:
                with contextlib.suppress(Exception):
                    toast("Song not found")
            self._reset_to_startup_gui()

        Clock.schedule_once(_do, 0)

    def check_are_we_playing(self, *val):
        raw = "".join(val)
        status = normalize_playback_status(raw)
        if status is not None:
            GUILayout.service_playback_status = status.value

    @mainthread
    def _reset_to_startup_gui(self):
        self.gui_reset = True
        self.paused = False
        GUILayout.playing_song = False
        self._apply_radio_state_values(active=False, available=False)
        try:
            app = MDApp.get_running_app()
            root = app.root
        except Exception:
            return
        with contextlib.suppress(Exception):
            root.ids.imageView.source = os.path.join(
                os.path.dirname(__file__), "music.png"
            )
        root.ids.song_title.text = ""
        root.ids.info.text = ""
        root.ids.song_position.text = ""
        root.ids.song_max.text = ""
        root.ids.song_position.opacity = 0
        root.ids.song_max.opacity = 0
        with contextlib.suppress(Exception):
            root.ids.play_btt.opacity = 0
            root.ids.play_btt.disabled = True
            root.ids.pause_btt.opacity = 0
            root.ids.pause_btt.disabled = True
            root.ids.next_btt.opacity = 0
            root.ids.next_btt.disabled = True
            root.ids.previous_btt.opacity = 0
            root.ids.previous_btt.disabled = True
            root.ids.repeat_btt.disabled = True
            root.ids.repeat_btt.opacity = 0
            root.ids.shuffle_btt.disabled = True
            root.ids.shuffle_btt.opacity = 0
        if GUILayout.slider is not None:
            with contextlib.suppress(Exception):
                GUILayout.slider.disabled = True
                GUILayout.slider.opacity = 0

    def set_gui_conditions_from_none(self):
        self.set_gui_conditions(0, True, True, 0)
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.previous_btt.opacity = 0
            MDApp.get_running_app().root.ids.next_btt.opacity = 0
            MDApp.get_running_app().root.ids.next_btt.disabled = True
            MDApp.get_running_app().root.ids.previous_btt.disabled = True
            MDApp.get_running_app().root.ids.song_position.opacity = 0
            MDApp.get_running_app().root.ids.song_max.opacity = 0
        self._reset_to_startup_gui()

    def set_gui_conditions(self, arg0, arg1, arg2, arg3):
        MDApp.get_running_app().root.ids.play_btt.opacity = arg0
        MDApp.get_running_app().root.ids.play_btt.disabled = arg1
        MDApp.get_running_app().root.ids.pause_btt.disabled = arg2
        MDApp.get_running_app().root.ids.pause_btt.opacity = arg3

    def stop(self):
        if hasattr(self, "_cancel_download_wait"):
            self._cancel_download_wait()
        if GUILayout.slider is not None:
            GUILayout.slider.disabled = True
            GUILayout.slider.opacity = 0
        self.paused = False
        self.stream = None
        MDApp.get_running_app().root.ids.song_position.text = ""
        MDApp.get_running_app().root.ids.song_max.text = ""
        if self.gui_reset is False:
            MDApp.get_running_app().root.ids.play_btt.opacity = 1
            MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        MDApp.get_running_app().root.ids.repeat_btt.disabled = True
        with contextlib.suppress(Exception):
            GUILayout.send("stop", "stop music")
        cancel_event(GUILayout.get_update_slider)
        GUILayout.get_update_slider = None
        cancel_event(getattr(self, "loadingosctimer", None))
        self.loadingosctimer = None
        GUILayout.playing_song = False
        GUILayout.service_playback_status = PlaybackStatus.IDLE.value

    def next(self):
        self.set_next_previous_bttns()
        if self.playlist_mode:
            GUILayout.send("next", self.playlist_mode)
        else:
            self.count = self.count + 1
            self.retrieve_text()

    def previous(self):
        self.set_next_previous_bttns()
        if self.playlist_mode:
            GUILayout.send("previous", self.playlist_mode)
        else:
            self.count = self.count - 1
            self.retrieve_text()

    def toggle_radio(self):
        if getattr(self, "radio_active", False):
            GUILayout.send("stop_radio", "")
        elif getattr(self, "radio_available", False):
            GUILayout.send("start_radio", "")

    def set_next_previous_bttns(self):
        self.paused = False
        GUILayout.playing_song = True
        GUILayout.get_update_slider = replace_event(
            GUILayout.get_update_slider,
            lambda: Clock.schedule_interval(GUILayout.wait_update_slider, 1),
        )
        app = MDApp.get_running_app()
        app.root.ids.play_btt.disabled = True
        app.root.ids.play_btt.opacity = 0
        app.root.ids.pause_btt.disabled = False
        app.root.ids.pause_btt.opacity = 1

    @staticmethod
    def send(message_type, message):
        if message_type == "seek_seconds":
            try:
                secs = float(message)
            except Exception:
                secs = 0.0
            GUILayout.client.send_message("/seek_seconds", [secs])
            return

        message = f"{message}"
        if message_type == "load":
            GUILayout.client.send_message("/load", message)
        elif message_type == "play":
            GUILayout.client.send_message("/play", message)
        elif message_type == "pause":
            GUILayout.client.send_message("/pause", message)
        elif message_type == "next":
            GUILayout.client.send_message("/next", message)
        elif message_type == "stop":
            GUILayout.client.send_message("/stop", message)
        elif message_type == "playlist":
            GUILayout.client.send_message("/playlist", [message])
        elif message_type == "navigation_mode":
            GUILayout.client.send_message("/navigation_mode", message)
        elif message_type == "update_load_fs":
            GUILayout.client.send_message("/update_load_fs", message)
        elif message_type == "previous":
            GUILayout.client.send_message("/previous", message)
        elif message_type == "start_radio":
            GUILayout.client.send_message("/start_radio", message)
        elif message_type == "stop_radio":
            GUILayout.client.send_message("/stop_radio", message)
        elif message_type == "iamawake":
            GUILayout.client.send_message("/iamawake", message)
        elif message_type == "loop":
            GUILayout.client.send_message("/loop", message)
        elif message_type == "shuffle":
            GUILayout.client.send_message("/shuffle", message)
        elif message_type == "get_update_slider":
            GUILayout.client.send_message("/get_update_slider", message)
        elif message_type == "downloadyt":
            if utils.get_platform() == "android":
                service_class, activity = _android_download_service_handles()
                start_android_service(
                    service_class,
                    context=activity,
                    argument=message,
                )
            else:
                GUILayout.client.send_message("/downloadyt", [message])
        elif message_type == "cancel_download":
            client = (
                GUILayout.download_client
                if utils.get_platform() == "android"
                else GUILayout.client
            )
            if client is not None:
                client.send_message("/cancel_download", [message])

    def _active_playlist_song_names(self):
        names = []
        pname = "Downloaded"
        with contextlib.suppress(Exception):
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            if ap:
                pname = ap.name or "Playlist"
                if ap.tracks:
                    names = [os.path.basename(t.path) for t in ap.tracks if t.path]
        if not names:
            try:
                names = self.get_play_list() if pname == "Downloaded" else []
            except Exception:
                names = []
        return names, pname

    def _send_active_playlist_to_service(self):
        songs, _ = self._active_playlist_song_names()
        payload = json.dumps(songs)
        GUILayout.send("playlist", payload)
        if getattr(GUILayout, "playing_song", False):
            if songs:
                has_queue_navigation = len(songs) >= 2
                self.set_playlist(
                    True,
                    not has_queue_navigation,
                    1 if has_queue_navigation else 0,
                )
            else:
                self.set_playlist(False, True, 0)

    def _update_active_playlist_badge(self):
        with contextlib.suppress(Exception):
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            name = ap.name if ap else "Downloads"
            MDApp.get_running_app().root.ids.active_playlist_badge.text = (
                f"Playlist: {name}"
            )

    def on_kv_post(self, base_widget):
        try:
            storage = (
                os.path.join(
                    get_app_writable_dir("Downloaded/Played"),
                    "playlists.json",
                )
                if utils.get_platform() == "android"
                else os.path.normpath(
                    os.path.join(self.set_local_download, "playlists.json")
                )
            )
        except Exception:
            storage = os.path.join(os.getcwd(), "playlists.json")
        self._playlist_manager = PlaylistManager(storage_path=storage)
        with contextlib.suppress(Exception):
            self.ids.imageView.source = default_cover_path()
        try:
            self.library_tab = Factory.LibraryTab()
            self.ids.bottom_nav.add_widget(self.library_tab)
        except Exception as e:
            print("Failed to attach Library tab:", e)
            self.library_tab = None
            return
        self.refresh_playlist()

    @mainthread
    def _controls(self, action: str, *args):
        if action != "enable_play":
            return
        ids = self.ids
        ids.play_btt.disabled = False
        ids.play_btt.opacity = 1
        ids.pause_btt.disabled = True
        ids.pause_btt.opacity = 0
        ids.next_btt.disabled = False
        ids.previous_btt.disabled = False
        ids.song_pos_lbl.opacity = 1
        ids.song_max_lbl.opacity = 1

    def _playlist_refresh_sidebar(self):
        if not getattr(self, "library_tab", None):
            return
        active = self._playlist_manager.active_playlist()
        data = [
            {
                "pid": p.id,
                "name": p.name,
                "selected": bool(active and active.id == p.id),
            }
            for p in self._playlist_manager.list_playlists()
        ]
        self.library_tab.ids.rv_playlists.data = data
        if not self.screen2_is_downloads:
            self.library_tab.ids.active_playlist_name.text = (
                active.name if active else "Tracks"
            )

    def _playlist_on_select(self, pid: str):
        self.screen2_is_downloads = False
        self._playlist_manager.set_active(pid)
        self.refresh_playlist()
        self._send_active_playlist_to_service()
        self.second_screen2()

    def refresh_playlist(self):
        self._playlist_refresh_sidebar()
        self._playlist_refresh_tracks()
        self._update_active_playlist_badge()

    def _playlist_open_menu(self, pid: str, name: str):
        content = MDTextField(text=name, hint_text="Rename playlist")
        dlg = MDDialog(
            title="Playlist options",
            type="custom",
            content_cls=content,
            buttons=[
                MDFlatButton(
                    text="Delete",
                    on_release=lambda *_: (self._playlist_delete(pid), dlg.dismiss()),
                ),
                MDFlatButton(
                    text="Save",
                    on_release=lambda *_: (
                        self._playlist_rename(pid, content.text),
                        dlg.dismiss(),
                    ),
                ),
                MDFlatButton(text="Close", on_release=lambda *_: dlg.dismiss()),
            ],
        )
        dlg.open()

    def _playlist_prompt_new(self):
        content = MDTextField(hint_text="Playlist name")
        dlg = MDDialog(
            title="New playlist",
            type="custom",
            content_cls=content,
            buttons=[
                MDFlatButton(
                    text="Create",
                    on_release=lambda *_: (
                        self._playlist_create(content.text),
                        dlg.dismiss(),
                    ),
                ),
                MDFlatButton(text="Cancel", on_release=lambda *_: dlg.dismiss()),
            ],
        )
        dlg.open()

    def _playlist_create(self, name: str):
        name = (name or "").strip() or "Untitled"
        pid = self._playlist_manager.create_playlist(name)
        self._playlist_manager.set_active(pid)
        self._playlist_refresh_sidebar()
        self._playlist_refresh_tracks()
        with contextlib.suppress(Exception):
            toast(f'Created "{name}"')
        self.set_active_playlist_send_to_service()

    def _playlist_rename(self, pid: str, new_name: str):
        self._playlist_manager.rename_playlist(
            pid, (new_name or "").strip() or "Untitled"
        )
        self._playlist_refresh_sidebar()
        with contextlib.suppress(Exception):
            toast("Renamed")
        self._update_active_playlist_badge()
        self.second_screen2()

    def _playlist_delete(self, pid: str):
        was_active = False
        try:
            ap = self._playlist_manager.active_playlist()
            was_active = bool(ap and getattr(ap, "id", None) == pid)
        except Exception:
            was_active = False
        self._playlist_manager.delete_playlist(pid)
        self._playlist_refresh_sidebar()
        if was_active:
            self.second_screen()
            self.screen2_is_downloads = True

            with contextlib.suppress(Exception):
                if getattr(self, "library_tab", None):
                    self.library_tab.ids.active_playlist_name.text = "Tracks"
                    self.library_tab.ids.rv_tracks.data = []
        else:
            self._playlist_refresh_tracks()
        with contextlib.suppress(Exception):
            toast("Deleted")
        self._update_active_playlist_badge()
        self._send_active_playlist_to_service()

    def set_active_playlist_send_to_service(self):
        self._update_active_playlist_badge()
        self._send_active_playlist_to_service()
        self.second_screen2()

    def _playlist_refresh_tracks(self):
        if not getattr(self, "library_tab", None):
            return

        ap = None
        with contextlib.suppress(Exception):
            ap = self._playlist_manager.active_playlist()
        if not ap:
            self.library_tab.ids.active_playlist_name.text = "Tracks"
            self.library_tab.ids.rv_tracks.data = []
            return
        # A restored active playlist can populate this panel before the user
        # taps its row. Keep the screen mode aligned with what is visible.
        self.screen2_is_downloads = False
        self.library_tab.ids.active_playlist_name.text = ap.name or "Playlist"
        rows = [
            {
                "text": display_title_from_stem(t.title),
                "index": idx,
                "playlist_id": ap.id,
            }
            for idx, t in enumerate(ap.tracks or [])
        ]
        self.library_tab.ids.rv_tracks.data = rows

    def _playlist_import_selective(self):
        """
        Open a responsive dialog listing .m4a files in Downloads with checkboxes
        so the user can choose which ones to add to the active playlist.
        """
        active = (
            getattr(self, "_playlist_manager", None).active_playlist()
            if hasattr(self, "_playlist_manager")
            else None
        )
        if not active:
            with contextlib.suppress(Exception):
                toast("No active playlist")
            return

        try:
            names = [
                fn
                for fn in os.listdir(self.set_local_download)
                if fn.lower().endswith(".m4a")
            ]
        except Exception:
            names = []

        if not names:
            with contextlib.suppress(Exception):
                toast("No .m4a files found in your downloads folder")
            return

        visible_h = max(dp(180), min(Window.height * 0.60, dp(420)))
        small_screen = Window.height < dp(640)
        row_h = dp(36) if small_screen else dp(40)

        container = MDBoxLayout(
            orientation="vertical",
            spacing=dp(8),
            padding=[dp(8), dp(8), dp(8), dp(4)],
            adaptive_height=True,
        )
        filter_box = MDTextField(
            hint_text="Filter by name…",
            helper_text="Type to filter the list",
            helper_text_mode="on_focus",
            size_hint_x=1,
        )
        container.add_widget(filter_box)

        scroll = ScrollView(size_hint=(1, None), height=visible_h)
        grid = MDGridLayout(
            cols=1, adaptive_height=True, spacing=dp(6), size_hint_y=None
        )
        grid.bind(minimum_height=grid.setter("height"))

        try:
            full_list = [os.path.join(self.set_local_download, n) for n in names]
            names_sorted = [
                os.path.basename(i)
                for i in sorted(full_list, key=os.path.getmtime, reverse=True)
            ]
        except Exception:
            names_sorted = sorted(names)

        self._import_items = []
        self._all_rows = []

        for fn in names_sorted:
            row = MDBoxLayout(
                orientation="horizontal",
                size_hint_y=None,
                height=row_h,
                spacing=dp(10),
                padding=[dp(2), 0, dp(2), 0],
            )
            cb = Factory.MDCheckbox(size_hint=(None, None), size=(dp(24), dp(24)))
            lbl = Factory.MDLabel(
                text=fn[:-4], halign="left", shorten=True, shorten_from="right"
            )
            row.add_widget(cb)
            row.add_widget(lbl)
            grid.add_widget(row)
            self._import_items.append((cb, fn))
            self._all_rows.append((row, cb, fn, lbl))
        scroll.add_widget(grid)
        container.add_widget(scroll)

        def _apply_filter(q_text):
            q = (q_text or "").strip().lower()
            for row, _cb, fn, lbl in self._all_rows:
                visible = (q in fn.lower()) or (q in lbl.text.lower())
                row.opacity = 1 if visible else 0
                row.height = row_h if visible else 0
                row.disabled = not visible

        filter_box.bind(text=lambda _w, v: _apply_filter(v))

        self._import_dialog = MDDialog(
            title="Select tracks to import",
            type="custom",
            content_cls=container,
            size_hint=(None, None),
            width=min(Window.width * 0.90, dp(560)),
            buttons=[
                MDFlatButton(
                    text="Select All",
                    on_release=lambda *_: [
                        setattr(cb, "active", True) for cb, _ in self._import_items
                    ],
                ),
                MDFlatButton(
                    text="Add Selected",
                    on_release=lambda *_: self._confirm_import_selected(),
                ),
                MDFlatButton(
                    text="Cancel", on_release=lambda *_: self._import_dialog.dismiss()
                ),
            ],
        )
        self._import_dialog.open()

    def _confirm_import_selected(self):
        """Collect selected files and add them to the active playlist; refresh UI and service."""
        apm = getattr(self, "_playlist_manager", None)
        active = apm.active_playlist() if apm else None
        if not active:
            with contextlib.suppress(Exception):
                toast("No active playlist")
            return

        try:
            selected = [
                fn
                for cb, fn in getattr(self, "_import_items", [])
                if getattr(cb, "active", False)
            ]
        except Exception:
            selected = []

        if not selected:
            with contextlib.suppress(Exception):
                toast("No tracks selected")
            return

        paths = [os.path.join(self.set_local_download, fn) for fn in selected]
        before = len(active.tracks)
        with contextlib.suppress(Exception):
            apm.add_tracks(active.id, paths)

        added = max(0, len(apm.active_playlist().tracks) - before) if apm else 0
        skipped = max(0, len(paths) - added)

        with contextlib.suppress(Exception):
            self._playlist_refresh_tracks()
        with contextlib.suppress(Exception):
            self._send_active_playlist_to_service()
        with contextlib.suppress(Exception):
            self.second_screen2()

        with contextlib.suppress(Exception):
            toast(
                f"Imported {added} track(s)"
                + (f", skipped {skipped} duplicate(s)" if skipped else "")
            )

        with contextlib.suppress(Exception):
            self._import_dialog.dismiss()

    def _playlist_remove_track(self, index: int):
        active = self._playlist_manager.active_playlist()
        if not active:
            return
        self._playlist_manager.remove_track(active.id, index)
        self._playlist_refresh_tracks()
        with contextlib.suppress(Exception):
            toast("Removed")
        self._send_active_playlist_to_service()
        self.second_screen2()

    def _playlist_move_up(self, index: int):
        """Move the track at `index` up by one within the active playlist."""
        with contextlib.suppress(Exception):
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            if not ap or not (0 <= index < len(ap.tracks)):
                return
            to_idx = max(0, index - 1)
            if to_idx == index:
                return
            apm.move_track(ap.id, index, to_idx)
            with contextlib.suppress(Exception):
                self._playlist_refresh_tracks()
                self._send_active_playlist_to_service()
            self.second_screen2()

    def _playlist_move_down(self, index: int):
        """Move the track at `index` down by one within the active playlist."""
        with contextlib.suppress(Exception):
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            if not ap or not (0 <= index < len(ap.tracks)):
                return
            to_idx = min(len(ap.tracks) - 1, index + 1)
            if to_idx == index:
                return
            apm.move_track(ap.id, index, to_idx)
            with contextlib.suppress(Exception):
                self._playlist_refresh_tracks()
                self._send_active_playlist_to_service()
            self.second_screen2()

    def _playlist_play_index(self, index: int, playlist_id: str = ""):
        manager = self._playlist_manager
        active = manager.active_playlist()
        if playlist_id and (not active or active.id != playlist_id):
            manager.set_active(playlist_id)
            active = manager.active_playlist()
        if not active or not (0 <= index < len(active.tracks)):
            return
        # Track rows are actionable on their own; selecting the matching row
        # in the left pane first is not required.
        self.screen2_is_downloads = False
        names = [os.path.basename(t.path) for t in active.tracks]
        if len(names) >= 2:
            self.set_playlist(True, False, 1)
        else:
            self.set_playlist(False, True, 0)
        with contextlib.suppress(Exception):
            self._send_active_playlist_to_service()
        self.getting_song(names[index])
        with contextlib.suppress(Exception):
            self.change_screen_item("Screen 1")

    def _is_last_index(self, index: int) -> bool:
        try:
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            if not ap:
                return False
            return index >= (len(ap.tracks) - 1) if ap.tracks else True
        except Exception:
            return False

    def __draw_shadow__(self, origin, end, context=None):
        pass

    def __init__(self, **kwargs):
        super(GUILayout, self).__init__(**kwargs)
        self.download_tracker = DownloadRequestTracker(
            timeout_seconds=DOWNLOAD_INACTIVITY_TIMEOUT_SECONDS
        )
        self.settitle = None
        self.fileosc_loaded = None
        self.length = None
        self.filetoplay = None
        self.popup = None
        self.play_btt = ObjectProperty(None)
        if self._has_active_playlist():
            self.second_screen2()
        else:
            self.second_screen(clear_active=False)
        self.pause_btt = ObjectProperty(None)
        self.paused = False
        self.stream = None
        self.count = 0
        self.results_loaded = False
        self.video_search = None
        self.result = None
        self.result1: list[SearchResult] = []
        self.selected_video_id = None
        self._search_generation = 0
        self.playlist_mode = False
        self.radio_active = False
        self.radio_available = False
        self.repeat_selected = False
        self.shuffle_selected = False
        self._service_reconnect_request_id = None
        self._service_reconnect_retry = None
        self._service_reconnect_timeout = None
        self.server = server = OSCThreadServer(encoding="utf8")
        server.listen(
            address=b"localhost",
            port=3002,
            default=True,
        )
        server.bind("/set_slider", self.set_slider)
        server.bind("/song_pos", self.update_slider)
        server.bind("/normalize", self.normalize_slider)
        server.bind("/update_image", self.update_image)
        server.bind("/reset_gui", self.reset_gui)
        server.bind("/file_is_downloaded", self.file_is_downloaded)
        server.bind("/data_info", self.update_info)
        server.bind("/download_progress", self.download_progress)
        server.bind("/download_result", self.download_result)
        server.bind("/are_we", self.check_are_we_playing)
        server.bind("/playback_snapshot", self.apply_playback_snapshot)
        server.bind("/radio_state", self.apply_radio_state)
        server.bind("/song_not_found", self.on_song_not_found)
        server.bind("/controls", self._controls)
        GUILayout.client = OSCClient("localhost", 3000, encoding="utf8")
        GUILayout.download_client = OSCClient("localhost", 3001, encoding="utf8")
        GUILayout.song_local = [0]
        GUILayout.slider = None
        GUILayout.playing_song = False
        GUILayout.service_playback_status = PlaybackStatus.IDLE.value
        self.loadingosctimer = None
        self.loadingfiletimer = None
        GUILayout.get_update_slider = None

    def _has_active_playlist(self) -> bool:
        try:
            ap = self._playlist_manager.active_playlist()
            return bool(ap and ap.tracks)
        except Exception:
            return False

    def _active_playlist_title(self) -> str:
        try:
            ap = self._playlist_manager.active_playlist()
            return f"Tracks — {ap.name}" if ap and ap.tracks else "No active playlist"
        except Exception:
            return "No active playlist"

    def second_screen(self, *, clear_active: bool = True):
        if clear_active:
            self._playlist_manager.clear_active()
        self.screen2_is_downloads = True
        songs = self.get_play_list()
        uniq = list(dict.fromkeys(songs))
        self.ids.rv.data = [
            {
                "text": display_title_from_stem(os.path.splitext(str(name))[0]),
                "filename": str(name),
            }
            for name in uniq
        ]
        with contextlib.suppress(Exception):
            self.ids.play_list.text = "Current Playlist: Downloaded"
        with contextlib.suppress(Exception):
            self._update_active_playlist_badge()
            self._send_active_playlist_to_service()

    def change_screen_item(self, nav_item):
        if not getattr(self, "screen2_is_downloads", False):
            self.second_screen2()
        self.ids.bottom_nav.switch_tab(nav_item)

    def second_screen2(self):
        songs, pname = self._active_playlist_song_names()
        try:
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
        except Exception:
            ap = None

        if not ap:
            self.second_screen(clear_active=False)
            return

        uniq = list(dict.fromkeys(songs))
        self.ids.rv.data = [
            {
                "text": display_title_from_stem(os.path.splitext(str(name))[0]),
                "filename": str(name),
            }
            for name in uniq
        ]
        with contextlib.suppress(Exception):
            self.ids.play_list.text = f"Current Playlist: {ap.name or 'Playlist'}"
        self.screen2_is_downloads = False

    def message_box(self, message):
        """Options popup (Page 2): MDDialog version, same logic (Yes deletes)."""

        box = BoxLayout(orientation="vertical", padding=10)
        body = Factory.MDLabel(
            text="Delete this track from disk?", theme_text_color="Secondary"
        )
        box.add_widget(body)

        self._current_dialog = MDDialog(
            title="Delete",
            type="custom",
            content_cls=box,
            size_hint=(None, None),
            width=min(MDApp.get_running_app().root.width * 0.90, dp(560)),
            buttons=[
                MDFlatButton(
                    text="NO",
                    on_release=lambda *_: (
                        self._current_dialog.dismiss()
                        if getattr(self, "_current_dialog", None)
                        else None
                    ),
                ),
                MDFlatButton(
                    text="Yes",
                    on_release=lambda *_: self.remove_track(message)
                    or self._current_dialog.dismiss(),
                ),
            ],
        )
        self._current_dialog.open()

    def remove_track(self, message):
        """
        Delete media/cover on disk and remove the item from the active playlist.
        Works whether `message` is a filename string (Page 2) or a dict with
        'path' / 'cover_path' (Page 3).

        If the target is the CURRENTLY PLAYING track, do NOT delete.
        Show a toast + dialog and return early.
        """
        try:
            track_path = getattr(self, "selected_track_path", None)
            cover_path = getattr(self, "selected_cover_path", None)

            if not track_path and isinstance(message, dict):
                track_path = message.get("path")
                cover_path = message.get("cover_path")

            if not track_path and isinstance(message, str):
                base = os.path.splitext(os.path.basename(message))[0]
                folder = get_app_writable_dir("Downloaded/Played")
                track_path = os.path.join(folder, f"{base}.m4a")
                cover_path = os.path.join(folder, f"{base}.jpg")

            if not track_path:
                toast("No track selected.")
                return

            current_base = (
                os.path.basename(self.stream) if getattr(self, "stream", None) else None
            )
            target_base = os.path.basename(track_path)
            if current_base and target_base and current_base == target_base:
                with contextlib.suppress(Exception):
                    toast("Can't delete the current track. Stop playback first.")
                return

            for p in (track_path, cover_path):
                if p and os.path.exists(p):
                    with contextlib.suppress(Exception):
                        os.remove(p)

            pm = getattr(self, "_playlist_manager", None)
            ap = pm.active_playlist() if pm else None
            if ap:
                target = os.path.normpath(os.path.realpath(track_path))
                remove_index = None
                for idx, t in enumerate(list(ap.tracks) if ap.tracks else []):
                    p = getattr(t, "path", None) or (
                        t.get("path") if isinstance(t, dict) else None
                    )
                    if not p:
                        continue
                    rp = os.path.normpath(os.path.realpath(p))
                    if rp == target or os.path.basename(rp) == os.path.basename(target):
                        remove_index = idx
                        break
                if remove_index is not None:
                    with contextlib.suppress(Exception):
                        pm.remove_track(ap.id, remove_index)

            with contextlib.suppress(Exception):
                self._playlist_refresh_tracks()
                if getattr(self, "library_tab", None):
                    self.library_tab.ids.rv_tracks.refresh_from_data()

            with contextlib.suppress(Exception):
                self.second_screen2()
                self.ids.rv.refresh_from_data()

            if dlg := getattr(self, "dialog", None):
                with contextlib.suppress(Exception):
                    dlg.dismiss()
            with contextlib.suppress(Exception):
                self._send_active_playlist_to_service()
            toast("Track deleted and removed from playlist.")
        except Exception as e:
            print("Error in remove_track:", e)
            toast("Delete failed.")

    def getting_song(self, message):
        try:
            if getattr(self, "screen2_is_downloads", False):
                songs = self.get_play_list() or []
                GUILayout.send("playlist", json.dumps(songs))
            else:
                self._send_active_playlist_to_service()
        except Exception:
            self._send_active_playlist_to_service()
        GUILayout.send("update_load_fs", "update_load_fs")
        if GUILayout.playing_song:
            self.stop()
        self.stream = os.path.join(
            get_app_writable_dir("Downloaded/Played"),
            f"{message}",
        )
        self.set_local = os.path.join(
            get_app_writable_dir("Downloaded/Played"),
            f"{message[:-4]}.jpg",
        )
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if utils.get_platform() == "android":
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7
        stored_stem = os.path.splitext(os.path.basename(message))[0]
        self.settitle = display_title_from_stem(stored_stem)
        if len(self.settitle) > 51:
            settitle1 = f"{self.settitle[:51]}..."
        else:
            settitle1 = self.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1

        if GUILayout.slider is None:
            self.make_slider()
        self.playlist_mode = True
        self.playing()

    @mainthread
    def update_image(self, *val):
        raw = "".join(val).strip()
        if raw.startswith("['") and raw.endswith("']"):
            raw = raw[2:-2]
        elif raw.startswith('["') and raw.endswith('"]'):
            raw = raw[2:-2]
        if (raw.startswith("'") and raw.endswith("'")) or (
            raw.startswith('"') and raw.endswith('"')
        ):
            raw = raw[1:-1]
        filename = os.path.basename(raw)
        base, ext = os.path.splitext(filename)
        if not ext:
            ext = ".m4a"
            filename = base + ext
        self.stream = os.path.join(self.set_local_download, filename)
        self.set_local = os.path.join(self.set_local_download, f"{base}.jpg")
        if not os.path.exists(self.set_local):
            self.set_local = default_cover_path()
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if utils.get_platform() == "android":
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7

        self.settitle = display_title_from_stem(base)
        settitle1 = (
            f"{self.settitle[:51]}..." if len(self.settitle) > 51 else self.settitle
        )
        MDApp.get_running_app().root.ids.song_title.text = settitle1

    def make_slider(self):
        if GUILayout.slider is None:
            GUILayout.slider = MySlider(
                orientation="horizontal",
                min=0,
                max=100,
                value=0,
                pos_hint={"center_x": 0.50, "center_y": 0.3},
                size_hint_x=0.6,
                size_hint_y=0.1,
                opacity=0,
                disabled=True,
                step=1,
            )
        MDApp.get_running_app().root.ids.screen_1.add_widget(GUILayout.slider)
        MDApp.get_running_app().root.ids.play_btt.opacity = 0
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1

    def get_play_list(self):
        name_list = os.listdir(self.set_local_download)
        full_list = [os.path.join(self.set_local_download, i) for i in name_list]
        time_sorted_list = sorted(full_list, key=os.path.getmtime)
        time_sorted_list.reverse()
        return [os.path.basename(i) for i in time_sorted_list if i.endswith("m4a")]

    def new_search(self):
        self._start_music_service_user_initiated()
        # A search replaces, rather than augments, the current result set. Clear
        # this state before the worker starts so stale results cannot be shown
        # while the new request is still in flight.
        self.result1 = []
        self.results_loaded = False
        self.count = 0
        self.selected_video_id = None
        self.setytlink = None
        self.settitle = ""
        self.reset_for_new_query()
        if GUILayout.slider is not None:
            GUILayout.slider.disabled = True
            GUILayout.slider.opacity = 0
        self.playlist_mode = False
        GUILayout.send("navigation_mode", "search")
        self.retrieve_text()

    def retrieve_text(self):
        # Invalidate any older worker even when this request exits early (for
        # example, when both the search field and current title are empty).
        self._search_generation += 1
        generation = self._search_generation
        GUILayout.send("update_load_fs", "update_load_fs")
        self.paused = False
        self.gui_reset = True
        self.stop()
        if self.results_loaded:
            self._show_search_result()
            return

        ids = MDApp.get_running_app().root.ids
        search_text = str(ids.input_box.text or "").strip()
        if not search_text:
            search_text = str(ids.song_title.text or "").strip()
        if not search_text:
            return

        ids.info.text = "Searching..."
        ids.play_btt.disabled = True
        ids.play_btt.opacity = 0
        ids.next_btt.disabled = True
        ids.previous_btt.disabled = True
        Thread(
            target=self._run_search,
            args=(generation, search_text),
            name=f"VideoSearch-{generation}",
            daemon=True,
        ).start()

    def _run_search(self, generation: int, search_text: str):
        results: list[SearchResult] = []
        error = ""
        try:
            search = create_video_search(search_text)
            results = parse_search_results(search.result())
        except Exception as exc:
            error = str(exc) or "Search request failed."
            print(f"[search] {error}")
        Clock.schedule_once(
            lambda _dt, g=generation, r=results, e=error: (
                self._receive_search_results(g, r, e)
            ),
            0,
        )

    @mainthread
    def _receive_search_results(
        self,
        generation: int,
        results: list[SearchResult],
        error: str,
    ):
        if generation != self._search_generation:
            return
        if error or not results:
            self.results_loaded = False
            self.result1 = []
            self._reset_to_startup_gui()
            self.ids.info.text = (
                "Error searching music. Check your connection and try again."
                if error
                else "No matching music was found."
            )
            return
        self.result1 = results
        self.results_loaded = True
        self.count %= len(results)
        self._show_search_result()

    def _show_search_result(self):
        if not self.result1:
            self._reset_to_startup_gui()
            self.ids.info.text = "No matching music was found."
            return
        if GUILayout.slider is None:
            GUILayout.slider = MySlider(
                orientation="horizontal",
                min=0,
                max=100,
                value=0,
                pos_hint={"center_x": 0.50, "center_y": 0.3},
                size_hint_x=0.6,
                size_hint_y=0.1,
                opacity=0,
                disabled=True,
                step=1,
            )
            MDApp.get_running_app().root.ids.screen_1.add_widget(GUILayout.slider)
        self.count %= len(self.result1)
        result = self.result1[self.count]
        self.setytlink = result.link
        self.selected_video_id = result.media_id
        self.set_local = result.thumbnail_url or default_cover_path()
        self.settitle = utils.safe_filename(result.title)
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if utils.get_platform() == "android":
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7
        if len(self.settitle) > 51:
            settitle1 = f"{self.settitle[:51]}..."
        else:
            settitle1 = self.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1
        self.settitle = utils.safe_filename(self.settitle)
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1
        MDApp.get_running_app().root.ids.info.text = ""

    def error_reset(self, msg):
        self._reset_to_startup_gui()
        MDApp.get_running_app().root.ids.imageView.source = default_cover_path()
        if msg == "search":
            MDApp.get_running_app().root.ids.info.text = "Error Searching Music"
        elif msg == "download":
            MDApp.get_running_app().root.ids.info.text = "Error downloading Music"
        self.paused = False

    @mainthread
    def download_yt(self, request_id):
        MDApp.get_running_app().root.ids.next_btt.disabled = True
        MDApp.get_running_app().root.ids.previous_btt.disabled = True
        MDApp.get_running_app().root.ids.info.text = "Downloading audio... Please wait"
        payload = json.dumps(
            {
                "request_id": request_id,
                "url": self.setytlink,
                "title": self.settitle,
                "video_id": self.selected_video_id,
                "thumbnail_url": self.set_local,
                "download_dir": self.set_local_download,
            }
        )
        try:
            GUILayout.send("downloadyt", payload)
        except Exception as exc:
            self.download_result(
                json.dumps(
                    {
                        "request_id": request_id,
                        "status": "error",
                        "message": f"Unable to start download service: {exc}",
                    }
                )
            )

    @mainthread
    def file_is_downloaded(self, *val):
        """Accept legacy service results during the playback protocol transition."""
        maybe = "".join(val)
        if maybe == "nope":
            message = self.ids.info.text or "Download failed. Tap Play to retry."
            self._restore_after_download_failure(message)
        elif maybe == "yep":
            self._complete_download_success()

    @mainthread
    def download_progress(self, *val):
        try:
            payload = json.loads("".join(val))
            request_id = str(payload.get("request_id") or "")
            message = str(payload.get("message") or "")
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
            return
        if not self.download_tracker.touch(request_id, now=time.monotonic()):
            return
        self.ids.info.text = "Downloading audio... Please wait\n" f"{message}"

    @mainthread
    def download_result(self, *val):
        try:
            payload = json.loads("".join(val))
            request_id = str(payload.get("request_id") or "")
            status = str(payload.get("status") or "")
            message = str(payload.get("message") or "")
            audio_path = payload.get("audio_path")
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
            return
        if not self.download_tracker.accepts(request_id):
            return
        if status != "success":
            self._restore_after_download_failure(
                message or "Download failed. Tap Play to retry."
            )
            return
        expected = os.path.normcase(os.path.realpath(self.filetoplay or ""))
        reported = os.path.normcase(os.path.realpath(str(audio_path or "")))
        if not expected or reported != expected or not os.path.isfile(expected):
            self._restore_after_download_failure(
                "Download finished without the expected audio file. Tap Play to retry."
            )
            return
        self._complete_download_success()

    @mainthread
    def update_info(self, *val):
        msg = "".join(val)
        if self.download_tracker.active_id:
            self.download_tracker.touch(
                self.download_tracker.active_id,
                now=time.monotonic(),
            )
            MDApp.get_running_app().root.ids.info.text = (
                "Downloading audio... Please wait\n" f"{msg}"
            )
            return
        MDApp.get_running_app().root.ids.info.text = msg

    def _cancel_download_wait(self, *, notify_service: bool = True):
        cancel_event(self.loadingfiletimer)
        self.loadingfiletimer = None
        tracker = getattr(self, "download_tracker", None)
        if tracker is not None:
            request_id = tracker.active_id
            tracker.cancel()
            if notify_service and request_id:
                with contextlib.suppress(Exception):
                    GUILayout.send(
                        "cancel_download",
                        json.dumps({"request_id": request_id}),
                    )

    def _restore_after_download_failure(self, message):
        self._cancel_download_wait()
        self.paused = False
        GUILayout.playing_song = False
        GUILayout.service_playback_status = PlaybackStatus.IDLE.value
        ids = MDApp.get_running_app().root.ids
        ids.info.text = str(message)
        ids.play_btt.disabled = False
        ids.play_btt.opacity = 1
        ids.pause_btt.disabled = True
        ids.pause_btt.opacity = 0
        ids.next_btt.disabled = False
        ids.previous_btt.disabled = False

    def _complete_download_success(self):
        self._cancel_download_wait(notify_service=False)
        if not self.filetoplay or not os.path.isfile(self.filetoplay):
            self._restore_after_download_failure(
                "Downloaded audio file was not found. Tap Play to retry."
            )
            return
        self.sync_playlist_set_load()
        # The downloaded file is now part of the active playlist, or the
        # Downloads queue when no playlist is selected.
        self.playlist_mode = True
        self.play_it()

    def _check_download_timeout(self, dt):
        if self.download_tracker.active_id is None:
            return False
        if not self.download_tracker.expired(now=time.monotonic()):
            return True
        self._restore_after_download_failure(
            "The download stopped responding. Tap Play to retry."
        )
        return False

    def checkfile(self):
        if getattr(self, "radio_active", False):
            # Radio has no local file. Let the service resume its stream and
            # publish the resulting playback state back to the GUI.
            GUILayout.send("play", "play")
            return
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.info.text = ""
        MDApp.get_running_app().root.ids.song_position.text = ""
        MDApp.get_running_app().root.ids.song_max.text = ""
        title = str(self.settitle or "").strip()
        if not title:
            self._restore_after_download_failure("No track is selected.")
            return
        media_id = str(getattr(self, "selected_video_id", "") or "").strip()
        if not media_id:
            media_id = stable_media_id(str(getattr(self, "setytlink", "") or ""))
            self.selected_video_id = media_id
        filename = audio_filename(title, media_id)
        self.filetoplay = os.path.join(self.set_local_download, filename)
        if os.path.isfile(self.filetoplay):
            self._complete_download_success()
            return
        if not getattr(self, "setytlink", None):
            self._restore_after_download_failure(
                "The selected track is not available to download."
            )
            return
        self._cancel_download_wait()
        request_id = uuid.uuid4().hex
        self.download_tracker.begin(request_id, now=time.monotonic())
        GUILayout.service_playback_status = PlaybackStatus.LOADING.value
        self.download_yt(request_id)
        self.loadingfiletimer = replace_event(
            self.loadingfiletimer,
            lambda: Clock.schedule_interval(self._check_download_timeout, 1),
        )

    def set_title_refresh_playlist(self, apm, ap):
        full_path = self.filetoplay
        if not full_path:
            return
        apm.add_tracks(
            ap.id,
            [full_path],
            video_id=getattr(self, "selected_video_id", None),
        )
        with contextlib.suppress(Exception):
            self._playlist_refresh_tracks()

    def sync_playlist_set_load(self):
        with contextlib.suppress(Exception):
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            if ap:
                self.set_title_refresh_playlist(apm, ap)
        with contextlib.suppress(Exception):
            self.refresh_playlist()
        with contextlib.suppress(Exception):
            self.second_screen2()
        with contextlib.suppress(Exception):
            self._send_active_playlist_to_service()

    def play_it(self):
        self.stream = self.filetoplay
        MDApp.get_running_app().root.ids.info.text = ""
        self.playing()
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False

    def playing(self):
        GUILayout.get_update_slider = replace_event(
            GUILayout.get_update_slider,
            lambda: Clock.schedule_interval(self.wait_update_slider, 1),
        )
        if not getattr(self, "screen2_is_downloads", False):
            self._send_active_playlist_to_service()
        self.second_screen2()
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.disabled = False
        MDApp.get_running_app().root.ids.play_btt.opacity = 0
        MDApp.get_running_app().root.ids.pause_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        MDApp.get_running_app().root.ids.song_position.opacity = 1
        MDApp.get_running_app().root.ids.song_max.opacity = 1
        GUILayout.playing_song = True
        GUILayout.service_playback_status = PlaybackStatus.PLAYING.value
        if self.paused is False:
            GUILayout.service_playback_status = PlaybackStatus.LOADING.value
            self.loadingosctimer = replace_event(
                self.loadingosctimer,
                lambda: Clock.schedule_interval(self.waitingforoscload, 1),
            )
            self.load_file()
        else:
            GUILayout.send("play", "play")

    @staticmethod
    def wait_update_slider(dt):
        GUILayout.send("get_update_slider", "tick")

    def waitingforoscload(self, dt):
        if self.fileosc_loaded is True:
            self.fileosc_loaded = False
            self.updating_gui_slider()
            cancel_event(self.loadingosctimer)
            self.loadingosctimer = None

    def updating_gui_slider(self):
        self.count = 0
        GUILayout.slider.disabled = False
        GUILayout.slider.opacity = 1
        GUILayout.slider.value = 0
        GUILayout.slider.max = self.length
        MDApp.get_running_app().root.ids.repeat_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1

    def set_playlist(self, arg0, arg1, arg2):
        self.playlist_mode = arg0
        MDApp.get_running_app().root.ids.shuffle_btt.disabled = arg1
        MDApp.get_running_app().root.ids.shuffle_btt.opacity = arg2

    def load_file(self):
        GUILayout.send("load", self.stream)

    @mainthread
    def set_slider(self, *val):
        self.length = float("".join(val))
        GUILayout.slider.max = self.length
        ty_res = time.gmtime(self.length)
        res = time.strftime("%H:%M:%S", ty_res)
        if str(res[:2]) == "00":
            res = res[3:]
        MDApp.get_running_app().root.ids.song_max.text = str(res)
        MDApp.get_running_app().root.ids.song_position.text = (
            "00:00:00" if str(res).count(":") == 2 else "00:00"
        )

        self.fileosc_loaded = True

    @mainthread
    def update_slider(self, *val):
        self.song_pos = float("".join(val))
        if GUILayout.slider is None or self.length is None:
            return
        if not getattr(GUILayout, "is_scrubbing", False):
            GUILayout.slider.value = self.song_pos
        settext = max(0.0, self.length - self.song_pos)
        ty_res = time.gmtime(settext)
        res = time.strftime("%H:%M:%S", ty_res)
        if str(res[:2]) == "00":
            res = res[3:]
        adding_value = time.gmtime(self.song_pos)
        res1 = time.strftime("%H:%M:%S", adding_value)
        if str(res1[:2]) == "00":
            res1 = res1[3:]
        MDApp.get_running_app().root.ids.song_position.text = str(res1)
        MDApp.get_running_app().root.ids.song_max.text = str(res)

    def normalize_slider(self):
        seekingsound = float(GUILayout.slider.value)
        GUILayout.send("seek_seconds", str(seekingsound))

    def repeat_songs_check(self):
        if self.repeat_selected is True:
            self.set_loop(False, 0, "False")
        else:
            self.set_loop(True, 1, "True")

    def set_loop(self, arg0, arg1, arg2):
        self.repeat_selected = arg0
        MDApp.get_running_app().root.ids.repeat_btt.text_color = arg1, 0, 0, 1
        GUILayout.send("loop", arg2)

    def shuffle_song_check(self):
        if self.shuffle_selected is True:
            self.shuffle_selected = False
            GUILayout.send("shuffle", "False")
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        else:
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 1, 0, 0, 1
            self.shuffle_selected = True
            GUILayout.send("shuffle", "True")

    def pause(self):
        self.paused = True
        GUILayout.playing_song = False
        GUILayout.service_playback_status = PlaybackStatus.PAUSED.value
        cancel_event(GUILayout.get_update_slider)
        GUILayout.get_update_slider = None
        GUILayout.send("pause", "pause")
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0

    @mainthread
    def reset_gui(self, *val):
        """Reset GUI to a clean idle state when the last track finishes."""
        self.gui_reset = True
        self.paused = False
        self.playlist_mode = False
        GUILayout.playing_song = False
        GUILayout.service_playback_status = PlaybackStatus.IDLE.value
        with contextlib.suppress(Exception):
            cancel_event(GUILayout.get_update_slider)
            GUILayout.get_update_slider = None
        cancel_event(getattr(self, "loadingosctimer", None))
        self.loadingosctimer = None
        Clock.schedule_once(lambda dt: self.set_gui_conditions_from_none(), 0)

    def _playlist_open_reorder_dialog(self):
        """
        Open a modal dialog to reorder the ACTIVE playlist using up/down arrows.
        Applies the order using PlaylistManager.move_track(...) on Save.
        """
        import functools

        from kivy.uix.scrollview import ScrollView
        from kivymd.uix.boxlayout import MDBoxLayout
        from kivymd.uix.button import MDFlatButton, MDIconButton
        from kivymd.uix.dialog import MDDialog
        from kivymd.uix.gridlayout import MDGridLayout
        from kivymd.uix.label import MDLabel

        apm = getattr(self, "_playlist_manager", None)
        ap = apm.active_playlist() if apm else None
        if not ap or not ap.tracks:
            with contextlib.suppress(Exception):
                toast("No active playlist to reorder")
            return
        model = [(t.title, getattr(t, "path", None)) for t in ap.tracks]
        visible_h = max(dp(220), min(Window.height * 0.65, dp(520)))
        row_h = dp(48)

        container = MDBoxLayout(
            orientation="vertical",
            spacing=dp(8),
            padding=[dp(8), dp(6), dp(8), dp(2)],
            adaptive_height=True,
        )

        scroll = ScrollView(size_hint=(1, None), height=visible_h)
        grid = MDGridLayout(
            cols=1, adaptive_height=True, spacing=dp(6), size_hint_y=None
        )
        grid.bind(minimum_height=grid.setter("height"))
        scroll.add_widget(grid)
        container.add_widget(scroll)

        state = {"model": model, "grid": grid}

        def _refresh_grid():
            grid = state["grid"]
            grid.clear_widgets()
            m = state["model"]
            for idx, (title, key) in enumerate(m):
                row = MDBoxLayout(
                    orientation="horizontal",
                    size_hint_y=None,
                    height=row_h,
                    padding=[dp(6), 0, dp(6), 0],
                    spacing=dp(8),
                )

                lbl_idx = MDLabel(
                    text=f"{idx+1}.",
                    size_hint_x=None,
                    width=dp(28),
                    halign="right",
                    valign="center",
                )

                lbl_title = MDLabel(
                    text=title or "(untitled)",
                    halign="left",
                    shorten=True,
                    shorten_from="right",
                )

                btn_up = MDIconButton(
                    icon="chevron-up",
                    on_release=functools.partial(_move_item, idx, -1),
                )
                btn_dn = MDIconButton(
                    icon="chevron-down",
                    on_release=functools.partial(_move_item, idx, +1),
                )

                btn_up.disabled = idx == 0
                btn_dn.disabled = idx == len(m) - 1

                row.add_widget(lbl_idx)
                row.add_widget(lbl_title)
                row.add_widget(btn_up)
                row.add_widget(btn_dn)
                grid.add_widget(row)

        def _move_item(idx, delta, *_args):
            m = state["model"]
            j = idx + delta
            if 0 <= idx < len(m) and 0 <= j < len(m):
                m[idx], m[j] = m[j], m[idx]
                _refresh_grid()

        def _apply_and_close(_btn):
            try:
                desired_keys = [k for (_title, k) in state["model"]]
                cur_keys = [getattr(t, "path", None) for t in ap.tracks]

                for i, want in enumerate(desired_keys):
                    if want not in cur_keys:
                        continue
                    cur_pos = cur_keys.index(want)
                    if cur_pos != i:
                        with contextlib.suppress(Exception):
                            apm.move_track(ap.id, cur_pos, i)
                        item = cur_keys.pop(cur_pos)
                        cur_keys.insert(i, item)

                with contextlib.suppress(Exception):
                    self._playlist_refresh_tracks()
                with contextlib.suppress(Exception):
                    self._send_active_playlist_to_service()
                with contextlib.suppress(Exception):
                    self.second_screen2()

                with contextlib.suppress(Exception):
                    toast("Playlist order updated")
            finally:
                with contextlib.suppress(Exception):
                    dlg.dismiss()

        dlg = MDDialog(
            title="Reorder tracks",
            type="custom",
            content_cls=container,
            size_hint=(None, None),
            width=min(Window.width * 0.92, dp(620)),
            buttons=[
                MDFlatButton(text="Cancel", on_release=lambda *_: dlg.dismiss()),
                MDFlatButton(text="Save", on_release=_apply_and_close),
            ],
        )

        _refresh_grid()
        dlg.open()

    def on_app_close(self):
        """Cancel owned callbacks and stop local OSC activity on application exit."""

        self._cancel_service_reconnect()
        cancel_event(GUILayout.get_update_slider)
        GUILayout.get_update_slider = None
        cancel_event(getattr(self, "loadingfiletimer", None))
        self.loadingfiletimer = None
        cancel_event(getattr(self, "loadingosctimer", None))
        self.loadingosctimer = None
        with contextlib.suppress(Exception):
            GUILayout.send("stop", "app closing")
        with contextlib.suppress(Exception):
            self.server.stop_all()


class Musicapp(MDApp):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Do not let the launcher's working directory select a different
        # musicapp.kv when development and publisher trees are both present.
        self.kv_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "musicapp.kv"
        )
        store_path = os.path.join(get_app_writable_dir("Downloaded"), "app_state.json")
        self._store = JsonStore(store_path)
        self._update_ui_scale()
        Window.bind(size=lambda *a: self._update_ui_scale())

    def _update_ui_scale(self):
        w, h = Window.system_size
        self.ui_scale = bounded_ui_scale(w, h)

    def spx(self, value: float) -> float:
        """Scaled sp() you can call from KV: app.spx(14) etc."""
        return sp(value) * float(self.ui_scale)

    def get_thumb_path(self, title: str) -> str:
        """Return a valid local path for a track thumbnail, or the default cover."""
        base = get_app_writable_dir("Downloaded/Played")
        os.makedirs(base, exist_ok=True)
        stem = os.path.splitext(os.path.basename(title))[0]
        fname = f"{utils.safe_filename(stem)}.jpg"
        path = os.path.join(base, fname)
        return path if os.path.exists(path) else default_cover_path()

    def on_stop(self):
        """Clean up an intentional desktop exit without stopping Android playback."""

        if utils.get_platform() == "android":
            # Android calls on_stop when its activity is reclaimed or recreated.
            # The foreground playback service belongs to a different process and
            # must continue in that case.
            self._cancel_resume_handshake()
            self._cancel_deferred_notification_permission()
            return
        self._cleanup_on_exit()

    def on_start(self):
        """Connect after build(), when the GUI OSC listener is available."""

        _apply_android_system_bar_insets()
        self._schedule_resume_handshake()
        self._defer_android_notification_permission()

    def _cancel_resume_handshake(self):
        cancel_event(getattr(self, "_resume_handshake_event", None))
        self._resume_handshake_event = None

    def _schedule_resume_handshake(self, *_args):
        """Coalesce Android start/resume callbacks into one surface-safe restore."""

        self._resume_handshake_event = replace_event(
            getattr(self, "_resume_handshake_event", None),
            lambda: Clock.schedule_once(self._resume_handshake, 0.15),
        )

    def _cancel_deferred_notification_permission(self):
        if not getattr(self, "_notification_permission_frame_pending", False):
            return
        with contextlib.suppress(Exception):
            Window.unbind(on_flip=self._request_android_notification_permission)
        self._notification_permission_frame_pending = False

    def _defer_android_notification_permission(self):
        """Ask only after the initial Kivy frame, never while its surface starts."""

        if (
            utils.get_platform() != "android"
            or getattr(self, "_notification_permission_requested", False)
            or getattr(self, "_notification_permission_frame_pending", False)
        ):
            return
        self._notification_permission_frame_pending = True
        Window.bind(on_flip=self._request_android_notification_permission)

    def _request_android_notification_permission(self, *_args):
        """Request Android notification permission once, only when it is missing."""

        self._cancel_deferred_notification_permission()
        if (
            utils.get_platform() != "android"
            or getattr(self, "_notification_permission_requested", False)
        ):
            return
        self._notification_permission_requested = True
        with contextlib.suppress(Exception):
            if check_permission(Permission.POST_NOTIFICATIONS):
                return
            request_permissions([Permission.POST_NOTIFICATIONS])

    def _cleanup_on_exit(self):
        """Centralized shutdown path; safe to call multiple times."""
        if getattr(self, "_cleanup_complete", False):
            return
        root = getattr(self, "root", None)
        if root and hasattr(root, "on_app_close"):
            try:
                root.on_app_close()
            except Exception as e:
                print("on_app_close error:", e)
        self.stop_service()
        self._cleanup_complete = True

    def _on_keyboard(self, window, key, scancode, codepoint, modifier):
        if key in (27, 1001):
            self._cleanup_on_exit()
            return False
        return False

    def _on_request_close(self, *args):
        self._cleanup_on_exit()
        return False

    def build(self):
        Window.bind(on_request_close=self._on_request_close)
        if not self._store.exists("init_done"):
            self._store.put("init_done", value=True)
        self.title = "Youtube Music Player"
        icon = default_cover_path()
        self.icon = icon
        self.theme_cls.theme_style = "Light"
        self.theme_cls.primary_palette = "Green"
        self.theme_cls.primary_hue = "500"
        if getattr(sys, "frozen", False):
            resource_add_path(sys._MEIPASS)
        try:
            p = resource_find("library_tab.kv")
            Builder.load_file(p)
        except Exception as _e:
            print("library_tab.kv load error:", _e)
        return GUILayout(store=self._store)

    def stop_service(self):
        if utils.get_platform() == "android":
            try:
                service_class, activity = _android_music_service_handles()
                GUILayout.service_started = stop_android_service(
                    service_class,
                    context=activity,
                    started=GUILayout.service_started,
                )
            except Exception as exc:
                print("[service] stop failed:", exc)
            return
        if platform in ("linux", "linux2", "macosx", "win"):
            return
        raise NotImplementedError("service stop not implemented on this platform")

    def on_pause(self):
        self._cancel_resume_handshake()
        root = getattr(self, "root", None)
        if root and hasattr(root, "_cancel_service_reconnect"):
            root._cancel_service_reconnect()
        cancel_event(GUILayout.get_update_slider)
        GUILayout.get_update_slider = None
        return True

    def on_resume(self):
        """
        Resume flow (thread-safe):
          - Hop to main thread and run a handshake that asks the service
            for state and updates the UI safely.
        """
        self._schedule_resume_handshake()
        return None

    @mainthread
    def _resume_handshake(self, *args):
        """Re-sync this GUI process with the persistent playback service."""

        self._resume_handshake_event = None
        self.root.begin_service_reconnect()
        # The first call can land before SDL recreates the Android surface.
        # Repeat after it has had a frame to become drawable.
        for delay in (0, 0.25, 1.0):
            Clock.schedule_once(self._request_canvas_redraw, delay)

    @staticmethod
    def _request_canvas_redraw(_dt):
        with contextlib.suppress(Exception):
            Window.canvas.ask_update()


if __name__ == "__main__":
    # Android's download service can outlive this GUI and still own .part files.
    if utils.get_platform() != "android":
        cleanup_download_artifacts(get_app_writable_dir("Downloaded/Played"))
    app = Musicapp()
    try:
        app.run()
    finally:
        if utils.get_platform() != "android":
            app.stop_service()

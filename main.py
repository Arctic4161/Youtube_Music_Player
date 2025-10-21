import contextlib
import json
import os
import sys
import time
from runpy import run_path

import utils
from utils import get_app_writable_dir

kivy_home = get_app_writable_dir("Downloaded")
os.makedirs(kivy_home, exist_ok=True)
os.environ["KIVY_HOME"] = kivy_home
os.environ["KIVY_NO_CONSOLELOG"] = "1"

if utils.get_platform() != "android":
    os.environ["KIVY_AUDIO"] = "gstplayer"
else:
    import android
    from android.permissions import Permission, request_permissions

    request_permissions(
        [
            Permission.READ_MEDIA_AUDIO,
            Permission.READ_EXTERNAL_STORAGE,
            Permission.POST_NOTIFICATIONS,
        ]
    )
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
from youtubesearchpython import VideosSearch

from playlist_manager import PlaylistManager


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


class PlaylistTrackRow(MDBoxLayout):
    text = StringProperty("")

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
            return super().on_touch_down(touch)
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
                MDApp.get_running_app().root._playlist_play_index(int(self.index))
            return True

        return super().on_touch_up(touch)


class MySlider(MDSlider):
    sound = ObjectProperty()

    def on_touch_down(self, touch):
        if "button" in touch.profile and touch.button != "left":
            return super().on_touch_down(touch)

        if getattr(touch, "is_mouse_scrolling", True) or touch.ud.get("was_scroll"):
            with contextlib.suppress(Exception):
                touch.ud["was_scroll"] = True
            return super().on_touch_down(touch)

        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        d = self.ids.get("delete_btn", None)
        if d and d.collide_point(*touch.pos):
            self._touch_started_on_delete = True
            return True

        self._touch_started_on_delete = False
        touch.ud.pop("was_scroll", None)
        touch.ud["down_pos"] = touch.pos
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if self.collide_point(*touch.pos) and "down_pos" in touch.ud:
            x0, y0 = touch.ud["down_pos"]
            dx = touch.x - x0
            dy = touch.y - y0
            if (dx * dx + dy * dy) > (dp(6) ** 2):
                touch.ud["was_scroll"] = True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.ud.get("was_scroll"):
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
                MDApp.get_running_app().root._playlist_play_index(int(self.index))
            return True

        return super().on_touch_up(touch)

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
    screen2_is_downloads = BooleanProperty(True)
    image_path = default_cover_path()
    set_local_download = get_app_writable_dir("Downloaded/Played")
    os.makedirs(set_local_download, exist_ok=True)

    def _start_music_service_user_initiated(self):
        if utils.get_platform() != "android":
            return
        try:
            GUILayout.service = android.start_service(
                "Music Player",
                "Playing in background",
                "musicservice",
            )
            print("[service] started via android.start_service")
            return
        except Exception as e:
            print("[service] android.start_service unavailable:", e)

    def reset_for_new_query(self):
        """Clear time + status and hide the slider before a new search/load kicks off."""
        self.stop()
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
        if raw in {"True", "False", "None"}:
            GUILayout.check_are_paused = raw
        else:
            GUILayout.check_are_paused = (
                "True"
                if raw.lower() == "true"
                else "False" if raw.lower() == "false" else "None"
            )

    def set_gui_from_check(self, dt):
        if GUILayout.check_are_paused == "False":
            self.set_gui_conditions(0, True, False, 1)
        elif GUILayout.check_are_paused == "None":
            self.set_gui_conditions_from_none()
        elif GUILayout.check_are_paused == "True":
            self.set_gui_conditions(1, False, True, 0)

    @mainthread
    def _reset_to_startup_gui(self):
        self.gui_reset = True
        self.paused = False
        GUILayout.playing_song = False
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
        if GUILayout.playing_song is True:
            GUILayout.send("stop", "stop music")
        GUILayout.get_update_slider.cancel()

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

    def set_next_previous_bttns(self):
        self.paused = False
        GUILayout.playing_song = True
        GUILayout.get_update_slider = Clock.schedule_interval(
            GUILayout.wait_update_slider, 1
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
        elif message_type == "update_load_fs":
            GUILayout.client.send_message("/update_load_fs", message)
        elif message_type == "previous":
            GUILayout.client.send_message("/previous", message)
        elif message_type == "iamawake":
            GUILayout.client.send_message("/iamawake", message)
        elif message_type == "loop":
            GUILayout.client.send_message("/loop", message)
        elif message_type == "shuffle":
            GUILayout.client.send_message("/shuffle", message)
        elif message_type == "get_update_slider":
            GUILayout.client.send_message("/get_update_slider", message)
        elif message_type == "downloadyt":
            GUILayout.client.send_message("/downloadyt", [message])
        elif message_type == "iampaused":
            GUILayout.client.send_message("/iampaused", message)

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
        if len(songs) >= 2 and getattr(GUILayout, "playing_song", False):
            self.set_playlist(True, False, 1)
        elif getattr(GUILayout, "playing_song", False):
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
        data = [
            {"pid": p.id, "name": p.name}
            for p in self._playlist_manager.list_playlists()
        ]
        self.library_tab.ids.rv_playlists.data = data
        if not self.screen2_is_downloads:
            active = self._playlist_manager.active_playlist()
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
        self.library_tab.ids.active_playlist_name.text = ap.name or "Playlist"
        rows = [
            {"text": t.title, "index": idx} for idx, t in enumerate(ap.tracks or [])
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

    def _playlist_play_index(self, index: int):
        active = self._playlist_manager.active_playlist()
        if not active or not (0 <= index < len(active.tracks)):
            return
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
        self.settitle = None
        self.fileosc_loaded = None
        self.length = None
        self.filetoplay = None
        self.popup = None
        self.play_btt = ObjectProperty(None)
        self.second_screen()
        self.pause_btt = ObjectProperty(None)
        self.paused = False
        self.stream = None
        self.count = 0
        self.results_loaded = False
        self.video_search = None
        self.result = None
        self.result1 = {}
        self.file_loaded = False
        self.playlist_mode = False
        self.repeat_selected = False
        self.shuffle_selected = False
        if utils.get_platform() == "android":
            self._start_music_service_user_initiated()
        else:
            GUILayout.service = Thread(
                target=run_path,
                args=[os.path.join(os.path.dirname(__file__), "./service/main.py")],
                kwargs={"run_name": "__main__"},
                daemon=True,
            )
            GUILayout.service.start()
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
        server.bind("/are_we", self.check_are_we_playing)
        server.bind("/song_not_found", self.on_song_not_found)
        server.bind("/controls", self._controls)
        GUILayout.client = OSCClient("localhost", 3000, encoding="utf8")
        GUILayout.song_local = [0]
        GUILayout.slider = None
        GUILayout.playing_song = False
        GUILayout.check_are_paused = "None"
        self.loadingosctimer = Clock.schedule_interval(self.waitingforoscload, 1)
        GUILayout.get_update_slider = Clock.schedule_interval(
            self.wait_update_slider, 1
        )

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

    def second_screen(self):
        self._playlist_manager.clear_active()
        self.screen2_is_downloads = True
        songs = self.get_play_list()
        uniq = list(dict.fromkeys(songs))
        self.ids.rv.data = [{"text": str(x[:-4])} for x in uniq]
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
            self.second_screen()
            return

        uniq = list(dict.fromkeys(songs))
        self.ids.rv.data = [{"text": str(x[:-4])} for x in uniq]
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
        self.settitle = message[:-4]
        if len(self.settitle) > 51:
            settitle1 = f"{self.settitle[:51]}..."
        else:
            settitle1 = self.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1

        if GUILayout.slider is None:
            self.make_slider()
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
            try:
                jpgs = [
                    f
                    for f in os.listdir(self.set_local_download)
                    if f.lower().endswith(".jpg")
                ]
                for jpg in jpgs:
                    img_base, _ = os.path.splitext(jpg)
                    if img_base in base or base in img_base:
                        old_audio = self.stream
                        new_audio = os.path.join(
                            self.set_local_download, f"{img_base}.m4a"
                        )
                        if os.path.exists(old_audio) and not os.path.exists(new_audio):
                            os.rename(old_audio, new_audio)
                        self.stream = new_audio
                        self.set_local = os.path.join(
                            self.set_local_download, f"{img_base}.jpg"
                        )
                        base = img_base
                        break
            except Exception as e:
                print(f"[ui] optional rename/repair failed: {e}")
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if utils.get_platform() == "android":
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7

        self.settitle = base
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
        self.reset_for_new_query()
        if GUILayout.slider is not None:
            GUILayout.slider.disabled = True
            GUILayout.slider.opacity = 0
        self.playlist_mode = False
        self.count = 0
        self.results_loaded = False
        self.retrieve_text()

    def retrieve_text(self):
        GUILayout.send("update_load_fs", "update_load_fs")
        self.paused = False
        self.gui_reset = True
        self.stop()
        if self.results_loaded is False:
            try:
                search_text = str(MDApp.get_running_app().root.ids.input_box.text)
                if search_text != "":
                    self.video_search = VideosSearch(search_text)
                elif MDApp.get_running_app().root.ids.song_title.text != "":
                    self.video_search = VideosSearch(
                        MDApp.get_running_app().root.ids.song_title.text
                    )
                else:
                    return
            except TypeError as e:
                self.error_reset("search")
                return
            self.result = self.video_search.result()
            self.result1 = self.result["result"]
            self.results_loaded = True
            if MDApp.get_running_app().root.ids.input_box.text == "":
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
        if self.results_loaded is False:
            self.video_search = VideosSearch(
                MDApp.get_running_app().root.ids.input_box.text
            )
            self.result = self.video_search.result()
            self.result1 = self.result["result"]
            self.results_loaded = True
        try:
            resultdict = self.result1[self.count]
        except IndexError:
            self.count = 0
            resultdict = self.result1[self.count]
        self.setytlink = resultdict["link"]
        thumbnail = resultdict["thumbnails"]
        self.set_local = thumbnail[0]["url"]
        self.settitle = utils.safe_filename(resultdict["title"])
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

    def error_reset(self, msg):
        MDApp.get_running_app().root.ids.imageView.source = default_cover_path()
        if msg == "search":
            MDApp.get_running_app().root.ids.info.text = "Error Searching Music"
        elif msg == "download":
            MDApp.get_running_app().root.ids.info.text = "Error downloading Music"
        self._reset_to_startup_gui()
        self.paused = False
        self.file_loaded = False

    @mainthread
    def download_yt(self):
        MDApp.get_running_app().root.ids.next_btt.disabled = True
        MDApp.get_running_app().root.ids.previous_btt.disabled = True
        MDApp.get_running_app().root.ids.info.text = "Downloading audio... Please wait"
        payload = json.dumps(
            [self.setytlink, self.settitle, self.set_local, self.set_local_download]
        )
        GUILayout.send("downloadyt", payload)

    @mainthread
    def file_is_downloaded(self, *val):
        maybe = "".join(val)
        if maybe == "nope":
            self.error_reset("download")
        elif maybe == "yep":
            self.sync_playlist_set_load()

    @mainthread
    def update_info(self, *val):
        msg = "".join(val)
        MDApp.get_running_app().root.ids.info.text = (
            "Downloading audio... Please wait\n" f"{msg}"
        )

    def checkfile(self):
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.info.text = ""
        MDApp.get_running_app().root.ids.song_position.text = ""
        MDApp.get_running_app().root.ids.song_max.text = ""
        self.filetoplay = (
            f"{self.set_local_download}//{self.settitle}"
            if self.settitle.strip()[-4:] == ".m4a"
            else f"{self.set_local_download}//{self.settitle.strip()}.m4a"
        )
        if os.path.isfile(self.filetoplay):
            self.sync_playlist_set_load()
        else:
            self.file_loaded = False
            loadingfile = Thread(target=self.download_yt, daemon=True)
            loadingfile.start()
        self.loadingfiletimer = Clock.schedule_interval(self.waitingforload, 1)
        self.loadingfiletimer()

    def set_title_refresh_playlist(self, apm, ap):
        title = utils.safe_filename((self.settitle or "").strip())
        fname = title if title.endswith(".m4a") else f"{title}.m4a"
        full_path = os.path.join(self.set_local_download, fname)

        apm.add_tracks(ap.id, [full_path])
        with contextlib.suppress(Exception):
            self._playlist_refresh_tracks()

    def sync_playlist_set_load(self):
        self.file_loaded = True
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

    def waitingforload(self, dt):
        if self.file_loaded is True:
            self.file_loaded = False
            self.play_it()
            self.loadingfiletimer.cancel()
        elif self.file_loaded is False:
            pass
        elif self.file_loaded is None:
            self.file_loaded = False
            self.stop()
            self.loadingfiletimer.cancel()

    def play_it(self):
        self.stream = self.filetoplay
        MDApp.get_running_app().root.ids.info.text = ""
        self.playing()
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False

    def playing(self):
        GUILayout.get_update_slider = Clock.schedule_interval(
            self.wait_update_slider, 1
        )
        if not getattr(self, "_screen2_is_downloads", False):
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
        if self.paused is False:
            self.load_file()
            self.loadingosctimer()
        else:
            GUILayout.send("play", "play")

    @staticmethod
    def wait_update_slider(dt):
        GUILayout.send("get_update_slider", "tick")

    def waitingforoscload(self, dt):
        if self.fileosc_loaded is True:
            self.fileosc_loaded = False
            self.updating_gui_slider()
            self.loadingosctimer.cancel()

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
        if not getattr(GUILayout, "is_scrubbing", False):
            GUILayout.slider.value = self.song_pos
        settext = self.length - self.song_pos
        ty_res = time.gmtime(settext)
        res = time.strftime("%H:%M:%S", ty_res)
        if str(res[:2]) == "00":
            res = res[3:]
        adding_value = time.gmtime(self.song_pos)
        res1 = time.strftime("%H:%M:%S", adding_value)
        if str(res1[:2]) == "00":
            res1 = res1[3:]
            MDApp.get_running_app().root.ids.song_position.pos_hint = {
                "center_x": 0.60,
                "center_y": 0.3,
            }
        else:
            MDApp.get_running_app().root.ids.song_position.pos_hint = {
                "center_x": 0.56,
                "center_y": 0.3,
            }
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
        GUILayout.get_update_slider.cancel()
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
        with contextlib.suppress(Exception):
            GUILayout.get_update_slider.cancel()
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


class Musicapp(MDApp):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        store_path = os.path.join(get_app_writable_dir("Downloaded"), "app_state.json")
        self._store = JsonStore(store_path)
        self._update_ui_scale()
        Window.bind(size=lambda *a: self._update_ui_scale())

    def _update_ui_scale(self):
        w, h = Window.size
        base = 411.0
        raw = (min(w, h) / base) if base else 1.0
        self.ui_scale = max(0.85, min(1.35, raw))

    def spx(self, value: float) -> float:
        """Scaled sp() you can call from KV: app.spx(14) etc."""
        return sp(value) * float(self.ui_scale)

    def get_thumb_path(self, title: str) -> str:
        """Return a valid local path for a track thumbnail, or the default cover."""
        base = get_app_writable_dir("Downloaded/Played")
        os.makedirs(base, exist_ok=True)
        fname = f"{utils.safe_filename(title)}.jpg"
        path = os.path.join(base, fname)
        return path if os.path.exists(path) else default_cover_path()

    def on_stop(self):
        """Called by Kivy when the app is closing."""
        self._cleanup_on_exit()

    def _cleanup_on_exit(self):
        """Centralized shutdown path; safe to call multiple times."""
        gs = getattr(self, "gui_sounds", None)
        if gs and hasattr(gs, "on_app_close"):
            try:
                gs.on_app_close()
            except Exception as e:
                print("on_app_close error:", e)

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
        if GUILayout.service:
            if utils.get_platform() == "android":
                GUILayout.service.stop(GUILayout.service_activity)
            elif platform in ("linux", "linux2", "macosx", "win"):
                return
            else:
                raise NotImplementedError(
                    "service start not implemented on this platform"
                )
            GUILayout.service = None

    def on_pause(self):
        GUILayout.get_update_slider.cancel()
        GUILayout.send("iampaused", ":(")
        return True

    def on_resume(self):
        """
        Resume flow (thread-safe):
          - Hop to main thread and run a handshake that asks the service
            for state and updates the UI safely.
        """
        Clock.schedule_once(self._resume_handshake, 0)
        return None

    @mainthread
    def _resume_handshake(self, *args):
        """Run on the main thread: safely re-sync UI with playback state."""
        root = self.root

        GUILayout.send("iamawake", "hello")

        with contextlib.suppress(Exception):
            if GUILayout.slider is not None:
                GUILayout.slider.disabled = True
        with contextlib.suppress(Exception):
            if hasattr(root.ids, "next_btt"):
                root.ids.next_btt.disabled = True
            if hasattr(root.ids, "previous_btt"):
                root.ids.previous_btt.disabled = True
        with contextlib.suppress(Exception):
            if getattr(root, "refresh_playlist", None):
                Clock.schedule_once(lambda dt: root.refresh_playlist(), 0)
            if getattr(root, "_send_active_playlist_to_service", None):
                Clock.schedule_once(
                    lambda dt: root._send_active_playlist_to_service(), 0
                )
        with contextlib.suppress(Exception):
            if getattr(root, "get_update_slider", None):
                root.get_update_slider.cancel()
        with contextlib.suppress(Exception):
            root.get_update_slider = Clock.schedule_interval(root.wait_update_slider, 1)

        def _apply_playing_state():
            with contextlib.suppress(Exception):
                if getattr(GUILayout, "slider", None) is not None:
                    GUILayout.slider.disabled = False
                    GUILayout.slider.opacity = 1
            with contextlib.suppress(Exception):
                if hasattr(root.ids, "next_btt"):
                    root.ids.next_btt.disabled = False
                    root.ids.next_btt.opacity = 1
                if hasattr(root.ids, "previous_btt"):
                    root.ids.previous_btt.disabled = False
                    root.ids.previous_btt.opacity = 1
            with contextlib.suppress(Exception):
                if hasattr(root.ids, "song_position"):
                    root.ids.song_position.opacity = 1
                if hasattr(root.ids, "song_max"):
                    root.ids.song_max.opacity = 1
            GUILayout.check_are_paused = "False"
            root.set_gui_from_check(0)

        def _apply_no_track_state():
            with contextlib.suppress(Exception):
                if GUILayout.slider is not None:
                    GUILayout.slider.disabled = True
                    GUILayout.slider.opacity = 0
            with contextlib.suppress(Exception):
                if hasattr(root.ids, "next_btt"):
                    root.ids.next_btt.disabled = True
                    root.ids.next_btt.opacity = 0
                if hasattr(root.ids, "previous_btt"):
                    root.ids.previous_btt.disabled = True
                    root.ids.previous_btt.opacity = 0
            with contextlib.suppress(Exception):
                root.set_gui_conditions_from_none()

        def _apply_paused_state():
            with contextlib.suppress(Exception):
                if GUILayout.slider is not None:
                    GUILayout.slider.disabled = False
                    GUILayout.slider.opacity = 1
            with contextlib.suppress(Exception):
                if hasattr(root.ids, "next_btt"):
                    root.ids.next_btt.disabled = False
                    root.ids.next_btt.opacity = 1
                if hasattr(root.ids, "previous_btt"):
                    root.ids.previous_btt.disabled = False
                    root.ids.previous_btt.opacity = 1
            with contextlib.suppress(Exception):
                if hasattr(root.ids, "song_position"):
                    root.ids.song_position.opacity = 1
                if hasattr(root.ids, "song_max"):
                    root.ids.song_max.opacity = 1

            GUILayout.check_are_paused = "True"
            root.set_gui_from_check(0)

        poll_ev = {"ev": None}
        timeout_ev = {"ev": None}

        def _poll(dt):
            paused = getattr(GUILayout, "check_are_paused", "None")
            if paused == "False":
                _stop_all()
                _apply_playing_state()
            elif paused == "True":
                _stop_all()
                _apply_paused_state()
            else:
                return

        def _stop_all():
            with contextlib.suppress(Exception):
                Clock.unschedule(poll_ev["ev"])
            with contextlib.suppress(Exception):
                Clock.unschedule(timeout_ev["ev"])

        def _timeout(dt):
            _stop_all()
            _apply_no_track_state()

        poll_ev["ev"] = Clock.schedule_interval(_poll, 0.25)
        timeout_ev["ev"] = Clock.schedule_once(_timeout, 10.0)
        Clock.schedule_once(lambda dt: Window.canvas.ask_update(), 0)


if __name__ == "__main__":
    python_files = [
        file
        for file in os.listdir()
        if file.endswith(".webm") or file.endswith(".ytdl") or file.endswith(".part")
    ]
    for file in python_files:
        with contextlib.suppress(PermissionError):
            os.remove(file)
    Musicapp().run()
    Musicapp().stop_service()

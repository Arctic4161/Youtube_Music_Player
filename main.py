import contextlib
import os
import sys
import time
from runpy import run_path

import utils
from media_recovery import start_mediastore_recovery_background
from utils import get_app_writable_dir

if utils.get_platform() != "android":
    os.environ["KIVY_AUDIO"] = "gstplayer"
else:
    from jnius import autoclass
from kivy.storage.jsonstore import JsonStore

from public_persistence import try_restore_playlists, wire_public_export

kivy_home = get_app_writable_dir("Downloaded")
os.makedirs(kivy_home, exist_ok=True)
os.environ["KIVY_HOME"] = kivy_home
# os.environ["KIVY_NO_CONSOLELOG"] = "1"

from threading import Thread

from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.factory import Factory
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import ObjectProperty, StringProperty
from kivy.resources import resource_add_path, resource_find
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
        if p := resource_find(rel):
            return p
    return


class RecycleViewRow(BoxLayout):
    text = StringProperty()


class MySlider(MDSlider):
    sound = ObjectProperty()

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            GUILayout.is_scrubbing = True
        return super(MySlider, self).on_touch_down(touch)

    def on_touch_up(self, touch):
        if self.collide_point(*touch.pos):
            try:
                secs = float(self.value)
            except Exception:
                secs = 0.0
            with contextlib.suppress(Exception):
                self.set_gui_to_play_from_touchup()
            with contextlib.suppress(Exception):
                GUILayout.get_update_slider.cancel()

            GUILayout.get_update_slider = Clock.schedule_interval(
                GUILayout.wait_update_slider, 1
            )

            GUILayout.send("play", "play")
            Clock.schedule_once(
                lambda dt: GUILayout.send("seek_seconds", str(int(secs))), 0
            )
            Clock.schedule_once(lambda dt: GUILayout.send("iamawake", "ping"), 0.05)
            Clock.schedule_once(
                lambda dt: setattr(GUILayout, "is_scrubbing", False), 0.1
            )

        return super(MySlider, self).on_touch_up(touch)

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


class GUILayout(MDFloatLayout, MDGridLayout):
    store = ObjectProperty(None)
    gui_reset = False
    is_scrubbing = False
    image_path = default_cover_path()
    set_local_download = get_app_writable_dir("Downloaded/Played")
    os.makedirs(set_local_download, exist_ok=True)

    def _start_music_service_user_initiated(self):
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        ContextCompat = autoclass("androidx.core.content.ContextCompat")
        Intent = autoclass("android.content.Intent")
        mActivity = PythonActivity.mActivity
        pkg = mActivity.getPackageName()
        SvcClass = autoclass(f"{pkg}.ServiceMusicservice")
        intent = Intent(mActivity, SvcClass)
        ContextCompat.startForegroundService(mActivity, intent)

    def reset_for_new_query(self):
        """Clear time + status and hide the slider before a new search/load kicks off."""
        app = MDApp.get_running_app()
        root = app.root

        with contextlib.suppress(Exception):
            root.ids.info.text = ""

        with contextlib.suppress(Exception):
            root.ids.song_position.text = ""
            root.ids.song_max.text = ""

        with contextlib.suppress(Exception):
            self.ids.cover.source = default_cover_path()

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
            with contextlib.suppress(Exception):
                self._reset_to_startup_gui()

        Clock.schedule_once(_do, 0)

    def check_are_we_playing(self, *val):
        GUILayout.check_are_play = "".join(val)

    def set_gui_from_check(self, dt):
        if GUILayout.check_are_play == "False":
            self.set_gui_conditions(0, True, False, 1)
            GUILayout.gui_resume_check.cancel()
        elif GUILayout.check_are_play == "None":
            self.set_gui_conditions_from_none()
        elif GUILayout.check_are_play == "True":
            self.set_gui_conditions(1, False, True, 0)
            GUILayout.gui_resume_check.cancel()

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
        with contextlib.suppress(Exception):
            root.ids.song_title.text = ""
            root.ids.info.text = ""
            root.ids.song_position.text = ""
            root.ids.song_max.text = ""
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
        self._reset_to_startup_gui()
        with contextlib.suppress(Exception):
            GUILayout.gui_resume_check.cancel()

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
        if self.playlist:
            GUILayout.send("next", self.playlist)
        else:
            self.count = self.count + 1
            self.retrieve_text()

    def previous(self):
        self.set_next_previous_bttns()
        if self.playlist:
            GUILayout.send("previous", self.playlist)
        else:
            self.count = self.count - 1
            self.retrieve_text()

    def set_next_previous_bttns(self):
        self.paused = False
        GUILayout.playing_song = True
        with contextlib.suppress(Exception):
            GUILayout.get_update_slider.cancel()
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
            GUILayout.client.send_message("/playlist", message)
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
            GUILayout.client.send_message("/downloadyt", message)
        elif message_type == "iampaused":
            GUILayout.client.send_message("/iampaused", message)

    def _active_playlist_song_names(self):
        names = []
        pname = "Downloads"
        with contextlib.suppress(Exception):
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            if ap and ap.tracks:
                names = [os.path.basename(t.path) for t in ap.tracks if t.path]
                pname = ap.name or "Playlist"
        if not names:
            try:
                names = self.get_play_list()
            except Exception:
                names = []
        return names, pname

    def _send_active_playlist_to_service(self):
        with contextlib.suppress(Exception):
            with contextlib.suppress(Exception):
                songs, _ = self._active_playlist_song_names()
                GUILayout.send("playlist", songs)
        if not getattr(GUILayout, "playing_song", False):
            return
        songs, _ = self._active_playlist_song_names()
        if len(songs) >= 2:
            self.set_playlist(True, False, 1)
            GUILayout.send("playlist", songs)
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
        with contextlib.suppress(Exception):
            self.ids.cover.source = default_cover_path()
        try:
            self.library_tab = Factory.LibraryTab()
            self.ids.bottom_nav.add_widget(self.library_tab)
        except Exception as e:
            print("Failed to attach Library tab:", e)
            self.library_tab = None
            return
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
        wire_public_export(
            self._playlist_manager, self.store, subdir="Documents/YouTube Music Player"
        )
        self.refresh_playlist()

    def _playlist_refresh_sidebar(self):
        if not getattr(self, "library_tab", None):
            return
        data = [
            {"pid": p.id, "name": p.name}
            for p in self._playlist_manager.list_playlists()
        ]
        self.library_tab.ids.rv_playlists.data = data
        active = self._playlist_manager.active_playlist()
        self.library_tab.ids.active_playlist_name.text = (
            active.name if active else "Tracks"
        )

    def _playlist_on_select(self, pid: str):
        self._playlist_manager.set_active(pid)
        self.refresh_playlist()
        self._send_active_playlist_to_service()

        active = self._playlist_manager.active_playlist()
        if active and active.tracks:
            song_title = f"{active.tracks[0].title}.m4a"
            self.getting_song(song_title)
        self.second_screen2()
        with contextlib.suppress(Exception):
            self.change_screen_item("Screen 1")

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
        self._playlist_manager.delete_playlist(pid)
        self._playlist_refresh_sidebar()
        self._playlist_refresh_tracks()
        with contextlib.suppress(Exception):
            toast("Deleted")
        self.set_active_playlist_send_to_service()

    def set_active_playlist_send_to_service(self):
        self._update_active_playlist_badge()
        self._send_active_playlist_to_service()
        self.second_screen2()

    def _playlist_refresh_tracks(self):
        if not getattr(self, "library_tab", None):
            return
        active = self._playlist_manager.active_playlist()
        self.library_tab.ids.active_playlist_name.text = (
            active.name if active else "Tracks"
        )
        self.library_tab.ids.rv_tracks.data = []
        if not active:
            return
        rows = [{"text": t.title, "index": idx} for idx, t in enumerate(active.tracks)]
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
            GUILayout.send("playlist", names)
        else:
            self.set_playlist(False, True, 0)
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
        self.fire_off_stop = False
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
        self.playlist = False
        self.repeat_selected = False
        self.shuffle_selected = False
        if platform in ("linux", "macosx", "win"):
            GUILayout.service = Thread(
                target=run_path,
                args=[os.path.join(os.path.dirname(__file__), "service_main.py")],
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
        if not hasattr(self, "check_are_we_playing") or not callable(
            getattr(self, "check_are_we_playing", None)
        ):

            def _fallback_are_we(*val):
                with contextlib.suppress(Exception):
                    GUILayout.check_are_play = "".join(val)

            self.check_are_we_playing = _fallback_are_we
        server.bind("/set_slider", self.set_slider)
        server.bind("/song_pos", self.update_slider)
        server.bind("/normalize", self.normalize_slider)
        server.bind("/update_image", self.update_image)
        server.bind("/reset_gui", self.reset_gui)
        server.bind("/file_is_downloaded", self.file_is_downloaded)
        server.bind("/data_info", self.update_info)
        server.bind("/are_we", self.check_are_we_playing)
        server.bind("/song_not_found", self.on_song_not_found)
        GUILayout.client = OSCClient("localhost", 3000, encoding="utf8")
        GUILayout.song_local = [0]
        GUILayout.slider = None
        GUILayout.playing_song = False
        GUILayout.check_are_play = None
        self.loadingosctimer = Clock.schedule_interval(self.waitingforoscload, 1)
        self.fire_stop = Clock.schedule_interval(self.checking_for_stop, 1)
        GUILayout.gui_resume_check = Clock.schedule_interval(self.set_gui_from_check, 1)
        GUILayout.get_update_slider = Clock.schedule_interval(
            self.wait_update_slider, 1
        )

    def second_screen(self):
        songs = self.get_play_list()
        self.ids.rv.data = [{"text": str(x[:-4])} for x in songs]

    def change_screen_item(self, nav_item):
        self.second_screen2()
        self.ids.bottom_nav.switch_tab(nav_item)

    def second_screen2(self):
        songs, pname = self._active_playlist_song_names()
        self.ids.rv.data = [{"text": str(x[:-4])} for x in songs]
        try:
            self.ids.play_list.text = f"Current Playlist: {pname}"
        except Exception as e:
            print(e)

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
                    text="Yes", on_release=lambda *_: self.remove_track(message)
                ),
            ],
        )
        self._current_dialog.open()

    def remove_track(self, message):
        """
        Deletes the selected track's media/cover from disk AND removes it
        from the active playlist JSON so UI and data stay in sync.
        """
        try:
            track_path = getattr(self, "selected_track_path", None)
            cover_path = getattr(self, "selected_cover_path", None)
            if not track_path and isinstance(message, dict):
                track_path = message.get("path")
                cover_path = message.get("cover_path")

            if not track_path:
                toast("No track selected.")
                return

            import os

            for p in (track_path, cover_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception as e:
                        print(f"Warning: could not delete {p}: {e}")
            pm = getattr(self, "playlists", None)
            pid = getattr(self, "active_playlist_id", None)

            if pm and pid is not None:
                import os

                norm_target = os.path.normpath(os.path.realpath(track_path))

                try:
                    tracks = pm.get_tracks(pid)
                except Exception:
                    tracks = []

                remove_index = None
                for idx, item in enumerate(tracks):
                    if isinstance(item, dict):
                        p = item.get("path") or item.get("file") or item.get("src")
                    else:
                        p = str(item)
                    if not p:
                        continue
                    if os.path.normpath(os.path.realpath(p)) == norm_target:
                        remove_index = idx
                        break

                if remove_index is not None:
                    try:
                        pm.remove_track(pid, remove_index)
                        pm.save()
                    except Exception as e:
                        print(f"Warning: playlist removal failed: {e}")
                else:
                    print(
                        "Note: track not found in playlist; data may already be in sync."
                    )

            with contextlib.suppress(Exception):
                self._reload_playlist_view()
            with contextlib.suppress(Exception):
                self._apply_idle_ui_state()
            if dlg := getattr(self, "dialog", None):
                with contextlib.suppress(Exception):
                    dlg.dismiss()
            toast("Track deleted and removed from playlist.")
        except Exception as e:
            print("Error in remove_track:", e)
            toast("Delete failed.")

    def getting_song(self, message):
        self.reset_for_new_query()
        with contextlib.suppress(Exception):
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
        self.reset_for_new_query()
        self.shuffle_selected = False
        MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        if GUILayout.slider is not None:
            GUILayout.slider.disabled = True
            GUILayout.slider.opacity = 0
        self.playlist = False
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
                if MDApp.get_running_app().root.ids.input_box.text != "":

                    self.video_search = VideosSearch(
                        MDApp.get_running_app().root.ids.input_box.text
                    )
                elif MDApp.get_running_app().root.ids.song_title.text != "":
                    self.video_search = VideosSearch(
                        MDApp.get_running_app().root.ids.song_title.text
                    )
                else:
                    return
            except TypeError as e:
                print(e)
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
        self.settitle = resultdict["title"]
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
            MDApp.get_running_app().root.ids.play_btt.opacity = 0
            MDApp.get_running_app().root.ids.play_btt.disabled = True
        elif msg == "download":
            MDApp.get_running_app().root.ids.info.text = "Error downloading Music"
            MDApp.get_running_app().root.ids.play_btt.opacity = 1
            MDApp.get_running_app().root.ids.play_btt.disabled = False
        if GUILayout.slider is not None:
            GUILayout.slider.disabled = True
            GUILayout.slider.opacity = 0
        MDApp.get_running_app().root.ids.song_position.text = ""
        MDApp.get_running_app().root.ids.song_max.text = ""
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        MDApp.get_running_app().root.ids.repeat_btt.disabled = True
        GUILayout.get_update_slider.cancel()
        self.paused = False
        self.file_loaded = False

    def download_yt(self):
        if utils.get_platform() == "android":
            self._start_music_service_user_initiated()
        MDApp.get_running_app().root.ids.next_btt.disabled = True
        MDApp.get_running_app().root.ids.previous_btt.disabled = True
        MDApp.get_running_app().root.ids.info.text = "Downloading audio... Please wait"
        GUILayout.send(
            "downloadyt",
            [self.setytlink, self.settitle, self.set_local, self.set_local_download],
        )

    def file_is_downloaded(self, *val):
        maybe = "".join(val)
        if maybe == "nope":
            self.error_reset("download")
        elif maybe == "yep":
            self.sync_playlist_set_load()

    def update_info(self, *val):
        msg = "".join(val)
        MDApp.get_running_app().root.ids.info.text = (
            "Downloading audio... Please wait\n" f"{msg[11:]}"
        )

    def checkfile(self):
        self.fire_stop()
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
        title = (self.settitle or "").strip()
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
        if utils.get_platform() == "android":
            self._start_music_service_user_initiated()
        GUILayout.get_update_slider = Clock.schedule_interval(
            self.wait_update_slider, 1
        )
        self.second_screen2()
        songs = self.get_play_list()
        if len(songs) >= 2:
            self.set_playlist(True, False, 1)
            GUILayout.send("playlist", songs)
        else:
            self.set_playlist(False, True, 0)
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.disabled = False
        MDApp.get_running_app().root.ids.play_btt.opacity = 0
        MDApp.get_running_app().root.ids.pause_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        GUILayout.playing_song = True
        if self.paused is False:
            self.load_file()
            self.loadingosctimer()
        else:
            GUILayout.send("play", "play")

    @staticmethod
    def wait_update_slider(dt):
        GUILayout.send("get_update_slider", "play")

    def waitingforoscload(self, dt):
        if self.fileosc_loaded is True:
            self.fileosc_loaded = False
            self.updating_gui_slider()
            self.loadingosctimer.cancel()

    def checking_for_stop(self, dt):
        if self.fire_off_stop is True:
            self.fire_off_stop = False
            self.stop()
            self.fire_stop.cancel()

    def updating_gui_slider(self):
        self.playlist = True
        self.count = 0
        GUILayout.slider.disabled = False
        GUILayout.slider.opacity = 1
        GUILayout.slider.value = 0
        GUILayout.slider.max = self.length
        MDApp.get_running_app().root.ids.repeat_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1

    def set_playlist(self, arg0, arg1, arg2):
        self.playlist = arg0
        MDApp.get_running_app().root.ids.shuffle_btt.disabled = arg1
        MDApp.get_running_app().root.ids.shuffle_btt.opacity = arg2

    def load_file(self):
        GUILayout.send("load", self.stream)

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
        GUILayout.send("seek_seconds", str(int(seekingsound)))

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

    def reset_gui(self, *val):
        GUILayout.playing_song = False
        self.fire_off_stop = True

        with contextlib.suppress(Exception):
            self._reset_to_startup_gui()
        Clock.schedule_once(lambda dt: self._reset_to_startup_gui(), 0)


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

    def on_start(self):
        if utils.get_platform() != "android":
            return

        def _progress(done, total, name):

            with contextlib.suppress(Exception):
                pct = int((done / total) * 100)
                print(f"[recovery] {done}/{total} • {pct}% • {name}")
                toast(f"Restoring {done}/{total}")

        def _done(summary):
            print("[recovery] done:", summary)
            with contextlib.suppress(Exception):
                self._playlist_manager = PlaylistManager()
                try_restore_playlists(
                    self._store,
                    self._playlist_manager,
                    subdir="Documents/YouTube Music Player",
                )
                self._playlist_manager.save()
                if getattr(self, "root", None) and hasattr(
                    self.root, "refresh_playlist"
                ):
                    self.root.refresh_playlist()
                toast(f"Recovered {summary.get('copied', 0)}/{summary.get('found', 0)}")

        started = start_mediastore_recovery_background(
            self._store,
            relative_path_prefix="Music/YouTube Music Player/",
            dest_subdir="Downloaded/Played",
            overwrite=False,
            request_permission=True,
            extract_covers=True,
            max_workers=2,
            max_items=None,
            on_progress=_progress,
            on_done=_done,
            once_store_key="media_recovery_done",
            force=False,
        )
        print("[recovery] started:", started)

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
        Resume flow:
          1) Start safe (everything disabled/idle)
          2) Ping service ("iamawake" + "areweplaying")
          3) Poll for answer and set GUI accordingly
          4) Re-enable slider/controls only if actually playing
        """
        app = self
        root = app.root

        GUILayout.send("iamawake", "hello")

        with contextlib.suppress(Exception):
            root.ids.slider.disabled = True
        with contextlib.suppress(Exception):
            if hasattr(root.ids, "next_btt"):
                root.ids.next_btt.disabled = True
            if hasattr(root.ids, "previous_btt"):
                root.ids.previous_btt.disabled = True

        if getattr(root, "set_gui_from_none", None):
            with contextlib.suppress(Exception):
                root.set_gui_from_none()

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

        with contextlib.suppress(Exception):
            GUILayout.check_are_play = "None"
        GUILayout.send("areweplaying", "")

        def _apply_playing_state():
            with contextlib.suppress(Exception):
                root.ids.slider.disabled = False
            with contextlib.suppress(Exception):
                if hasattr(root.ids, "next_btt"):
                    root.ids.next_btt.disabled = False
                if hasattr(root.ids, "previous_btt"):
                    root.ids.previous_btt.disabled = False
            if getattr(root, "set_gui_conditions", None):
                with contextlib.suppress(Exception):
                    root.set_gui_conditions(1, False, True, 0)
            elif getattr(root, "set_gui_from_check", None):
                with contextlib.suppress(Exception):
                    root.set_gui_from_check(0)

        def _apply_idle_state():
            with contextlib.suppress(Exception):
                root.ids.slider.disabled = True
            with contextlib.suppress(Exception):
                if hasattr(root.ids, "next_btt"):
                    root.ids.next_btt.disabled = True
                if hasattr(root.ids, "previous_btt"):
                    root.ids.previous_btt.disabled = True
            if getattr(root, "set_gui_from_none", None):
                with contextlib.suppress(Exception):
                    root.set_gui_from_none()

        poll_ev = {"ev": None}
        timeout_ev = {"ev": None}

        def _poll(dt):
            state = getattr(GUILayout, "check_are_play", "None")
            if state == "True":
                with contextlib.suppress(Exception):
                    Clock.unschedule(poll_ev["ev"])
                with contextlib.suppress(Exception):
                    Clock.unschedule(timeout_ev["ev"])
                _apply_playing_state()
            elif state == "False":
                with contextlib.suppress(Exception):
                    Clock.unschedule(poll_ev["ev"])
                with contextlib.suppress(Exception):
                    Clock.unschedule(timeout_ev["ev"])
                _apply_idle_state()

        def _timeout(dt):
            with contextlib.suppress(Exception):
                Clock.unschedule(poll_ev["ev"])
            _apply_idle_state()

        poll_ev["ev"] = Clock.schedule_interval(_poll, 0.25)
        timeout_ev["ev"] = Clock.schedule_once(_timeout, 5.0)


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

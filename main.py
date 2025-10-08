import contextlib
import os

from kivy.factory import Factory
from kivy.lang import Builder
from kivymd.toast import toast
from kivymd.uix.button import MDFlatButton
from kivymd.uix.dialog import MDDialog
from kivymd.uix.textfield import MDTextField

import utils
from playlist_manager import PlaylistManager

if utils.get_platform() == "android":
    from android.permissions import Permission, request_permissions
    from android.storage import primary_external_storage_path
    from jnius import autoclass

    request_permissions(
        [
            Permission.INTERNET,
            Permission.FOREGROUND_SERVICE,
            Permission.MEDIA_CONTENT_CONTROL,
            Permission.WRITE_EXTERNAL_STORAGE,
            Permission.READ_EXTERNAL_STORAGE,
            Permission.READ_MEDIA_AUDIO,
            Permission.READ_MEDIA_IMAGES,
        ]
    )
    os.makedirs(
        os.path.normpath(
            os.path.join(
                primary_external_storage_path(),
                "Download",
                "Youtube Music Player",
                "Downloaded",
                "Played",
            )
        ),
        exist_ok=True,
    )
else:
    os.makedirs(
        os.path.normpath(
            os.path.join(
                os.path.expanduser("~/Documents"), "Youtube Music Player", "Downloaded"
            )
        ),
        exist_ok=True,
    )
    os.environ["KIVY_HOME"] = os.path.join(
        os.path.expanduser("~/Documents"), "Youtube Music Player", "Downloaded"
    )
    os.environ["KIVY_AUDIO"] = "gstplayer"
os.environ["KIVY_NO_CONSOLELOG"] = "1"
import time
from threading import Thread

from kivy import platform
from kivy.clock import Clock
from kivy.properties import ObjectProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivymd.app import MDApp
from kivymd.uix.floatlayout import MDFloatLayout
from kivymd.uix.gridlayout import MDGridLayout
from kivymd.uix.slider import MDSlider
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer
from pytube.helpers import safe_filename
from youtubesearchpython import VideosSearch


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
            from kivy.clock import Clock

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

    def _active_playlist_song_names(self):
        names = []
        pname = "Downloads"
        with contextlib.suppress(Exception):
            apm = getattr(self, "_playlist_manager", None)
            ap = apm.active_playlist() if apm else None
            if ap and ap.tracks:
                names = [f"{t.title}.m4a" for t in ap.tracks]
                pname = ap.name or "Playlist"
        if not names:
            try:
                names = self.get_play_list()
            except Exception:
                names = []
        return names, pname

    def _send_active_playlist_to_service(self):
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
        try:
            self.library_tab = Factory.LibraryTab()
            self.ids.bottom_nav.add_widget(self.library_tab)
        except Exception as e:
            print("Failed to attach Library tab:", e)
            self.library_tab = None
            return
        try:
            storage = os.path.normpath(
                os.path.join(self.set_local_download, "playlists.json")
            )
        except Exception:
            storage = os.path.join(os.getcwd(), "playlists.json")
        self._playlist_manager = PlaylistManager(storage_path=storage)
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
        self._update_active_playlist_badge()
        self._send_active_playlist_to_service()
        self.second_screen2()

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

    def _playlist_import(self):
        active = self._playlist_manager.active_playlist()
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

        paths = [os.path.join(self.set_local_download, fn) for fn in names]
        before = len(active.tracks)
        self._playlist_manager.add_tracks(active.id, paths)
        added = max(0, len(active.tracks) - before)
        skipped = max(0, len(paths) - added)
        self._playlist_refresh_tracks()
        self._send_active_playlist_to_service()
        with contextlib.suppress(Exception):
            toast(
                f"Imported {added} track(s)"
                + (f", skipped {skipped} duplicate(s)" if skipped else "")
            )
        self.second_screen2()

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

    def _playlist_move_track(self, index: int, delta: int):
        apm = self._playlist_manager
        pl = apm.active_playlist() if apm else None
        if not pl:
            return
        new_idx = max(0, min(len(pl.tracks) - 1, index + delta))
        if new_idx == index:
            return
        apm.move_track(pl.id, index, new_idx)
        self._playlist_refresh_tracks()
        self._send_active_playlist_to_service()
        self.second_screen2()

    def _playlist_play_index(self, index: int):
        active = self._playlist_manager.active_playlist()
        if not active or not (0 <= index < len(active.tracks)):
            return
        names = [f"{t.title}.m4a" for t in active.tracks]
        if len(names) >= 2:
            self.set_playlist(True, False, 1)
            GUILayout.send("playlist", names)
        else:
            self.set_playlist(False, True, 0)
        self.getting_song(names[index])
        with contextlib.suppress(Exception):
            self.change_screen_item("Screen 1")

    is_scrubbing = False
    image_path = StringProperty(os.path.join(os.path.dirname(__file__), "music.png"))
    if platform != "android":
        set_local_download = os.path.normpath(
            os.path.join(
                os.path.expanduser("~/Documents"),
                "Youtube Music Player",
                "Downloaded",
                "Played",
            )
        )
    else:
        set_local_download = os.path.normpath(
            os.path.join(
                primary_external_storage_path(),
                "Download",
                "Youtube Music Player",
                "Downloaded",
                "Played",
            )
        )
    os.makedirs(set_local_download, exist_ok=True)

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
        if platform == "android":
            from android import mActivity

            context = mActivity.getApplicationContext()
            SERVICE_NAME = f"{str(context.getPackageName())}.ServiceMusicservice"
            GUILayout.service_activity = autoclass(
                "org.kivy.android.PythonActivity"
            ).mActivity
            service = autoclass(SERVICE_NAME)
            service.start(GUILayout.service_activity, "")
            GUILayout.service = service
        elif platform in ("linux", "linux2", "macos", "win"):
            from runpy import run_path
            from threading import Thread

            GUILayout.service = Thread(
                target=run_path,
                args=[
                    os.path.join(
                        os.path.dirname(__file__), "service", "service_main.py"
                    )
                ],
                kwargs={"run_name": "__main__"},
                daemon=True,
            )
            GUILayout.service.start()
        else:
            raise NotImplementedError("service start not implemented on this platform")
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
        box = BoxLayout(orientation="vertical", padding=10)
        btn1 = Button(text="Yes")
        btn2 = Button(text="NO")
        box.add_widget(btn1)
        box.add_widget(btn2)
        self.popup = Popup(
            title="Delete",
            title_size=30,
            title_align="center",
            content=box,
            size_hint=(None, None),
            size=(315, 315),
            auto_dismiss=True,
        )
        btn2.bind(on_press=self.popup.dismiss)
        btn1.bind(on_press=lambda x: self.remove_track(message))
        self.popup.open()

    def remove_track(self, message):
        song = f"{self.set_local_download}//{message}"
        image = f"{self.set_local_download}//{message[:-4]}.jpg"
        with contextlib.suppress(PermissionError, FileNotFoundError):
            os.remove(song)
            os.remove(image)
        self.popup.dismiss()
        self.second_screen2()

    def getting_song(self, message):
        with contextlib.suppress(Exception):
            self._send_active_playlist_to_service()
        GUILayout.send("update_load_fs", "update_load_fs")
        if GUILayout.playing_song:
            self.stop()
        self.stream = f"{self.set_local_download}//{message}"
        self.set_local = f"{self.set_local_download}//{message[:-4]}.jpg"
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if platform == "android":
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
        self.stream = f"{self.set_local_download}//{filename}"
        self.set_local = f"{self.set_local_download}//{base}.jpg"
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
                        new_audio = f"{self.set_local_download}//{img_base}.m4a"
                        if os.path.exists(old_audio) and not os.path.exists(new_audio):
                            os.rename(old_audio, new_audio)
                        self.stream = new_audio
                        self.set_local = f"{self.set_local_download}//{img_base}.jpg"
                        base = img_base
                        break
            except Exception as e:
                print(f"[ui] optional rename/repair failed: {e}")
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if platform == "android":
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
        self.stop()
        if self.results_loaded is False:
            try:
                if MDApp.get_running_app().root.ids.input_box.text != "":
                    self.video_search = VideosSearch(
                        MDApp.get_running_app().root.ids.input_box.text
                    )
                else:
                    self.video_search = VideosSearch(
                        MDApp.get_running_app().root.ids.song_title.text
                    )
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
            if platform == "android":
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7
        if len(self.settitle) > 51:
            settitle1 = f"{self.settitle[:51]}..."
        else:
            settitle1 = self.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1
        self.settitle = safe_filename(self.settitle)
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
        print(msg)
        MDApp.get_running_app().root.ids.imageView.source = os.path.join(
            os.path.dirname(__file__), "music.png"
        )
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
        MDApp.get_running_app().root.ids.next_btt.disabled = True
        MDApp.get_running_app().root.ids.previous_btt.disabled = True
        MDApp.get_running_app().root.ids.info.text = "Downloading audio... Please wait"
        GUILayout.send(
            "downloadyt",
            [self.setytlink, self.settitle, self.set_local, self.set_local_download],
        )

    def file_is_downloaded(self, *val):
        maybe = "".join(val)
        if maybe == "yep":
            self.file_loaded = True
        elif maybe == "nope":
            self.error_reset("download")

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
            self.file_loaded = True
        else:
            self.file_loaded = False
            loadingfile = Thread(target=self.download_yt, daemon=True)
            loadingfile.start()
        self.loadingfiletimer = Clock.schedule_interval(self.waitingforload, 1)
        self.loadingfiletimer()

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
        from kivy.clock import Clock

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

    def set_gui_conditions_from_none(self):
        self.set_gui_conditions(0, True, True, 0)
        MDApp.get_running_app().root.ids.previous_btt.opacity = 0
        MDApp.get_running_app().root.ids.next_btt.opacity = 0
        MDApp.get_running_app().root.ids.next_btt.disabled = True
        MDApp.get_running_app().root.ids.previous_btt.disabled = True
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
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        MDApp.get_running_app().root.ids.repeat_btt.disabled = True
        if GUILayout.playing_song is True:
            GUILayout.send("stop", "stop music")
        GUILayout.get_update_slider.cancel()

    def next(self):
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

        if self.playlist:
            GUILayout.send("next", self.playlist)
        else:
            self.count = self.count + 1
            self.retrieve_text()

    def previous(self):
        self.paused = False
        GUILayout.playing_song = True
        with contextlib.suppress(Exception):
            GUILayout.get_update_slider.cancel()
        from kivy.clock import Clock

        GUILayout.get_update_slider = Clock.schedule_interval(
            GUILayout.wait_update_slider, 1
        )
        app = MDApp.get_running_app()
        app.root.ids.play_btt.disabled = True
        app.root.ids.play_btt.opacity = 0
        app.root.ids.pause_btt.disabled = False
        app.root.ids.pause_btt.opacity = 1

        if self.playlist:
            GUILayout.send("previous", self.playlist)
        else:
            self.count = self.count - 1
            self.retrieve_text()

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


class Musicapp(MDApp):
    def build(self):
        self.title = "Youtube Music Player"
        icon = os.path.join(os.path.dirname(__file__), "music.png")
        self.icon = icon
        self.theme_cls.theme_style = "Light"
        self.theme_cls.primary_palette = "Green"
        self.theme_cls.primary_hue = "500"
        try:
            Builder.load_file("library_tab.kv")
        except Exception as _e:
            print("library_tab.kv load error:", _e)

        return GUILayout()

    def stop_service(self):
        if GUILayout.service:
            if platform == "android":
                GUILayout.service.stop(GUILayout.service_activity)
            elif platform in ("linux", "linux2", "macos", "win"):
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
        GUILayout.get_update_slider = Clock.schedule_interval(
            GUILayout.wait_update_slider, 1
        )
        GUILayout.gui_resume_check()
        GUILayout.send("iamawake", "Heelloo")


if __name__ == "__main__":
    python_files = [
        file
        for file in os.listdir()
        if file.endswith(".webm") or file.endswith(".ytdl") or file.endswith(".part")
    ]
    # Delete old undownloaded files
    for file in python_files:
        with contextlib.suppress(PermissionError):
            os.remove(file)
    Musicapp().run()
    Musicapp().stop_service()

import contextlib
import os
import shutil
import requests
import utils
if utils.get_platform() == 'android':
    os.environ['KIVY_AUDIO'] = 'android'
    from android.permissions import request_permissions, Permission
    request_permissions([Permission.INTERNET])
else:
    os.environ["KIVY_NO_CONSOLELOG"] = "1"
    os.environ['KIVY_AUDIO'] = 'gstplayer'
from kivy.config import Config
Config.set('kivy', 'keyboard_mode', 'system')
Config.set('input', 'mouse', 'mouse,disable_multitouch')
from kivy.core.audio import SoundLoader
from kivy import platform
import random
import time
import yt_dlp
from threading import Thread
from kivy.clock import Clock
from kivy.properties import StringProperty, ObjectProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivymd.app import MDApp
from kivymd.uix.floatlayout import MDFloatLayout
from kivymd.uix.gridlayout import MDGridLayout
from kivymd.uix.slider import MDSlider
from pytube.helpers import safe_filename
from youtubesearchpython import VideosSearch
#pyinstaller/installer
os.environ['GST_PLUGIN_PATH_1_0'] = os.path.dirname(__file__)
os.environ['GST_PLUGIN_SYSTEM_PATH_1_0'] = os.path.dirname(__file__)

class CustomLogger:
    def debug(self, msg):
        # For compatibility with youtube-dl, both debug and info are passed into debug
        # You can distinguish them by the prefix '[debug] '
        if not msg.startswith('[debug] '):
            self.info(msg)

    def info(self, msg):
        if "[download]" in msg:
            MDApp.get_running_app().root.ids.info.text = "Downloading and Extracting audio... Please wait\n" \
                                                         f"{msg[11:]}"
    def error(self, msg):
        print(msg)

    def warning(self, msg):
        print(msg)


class RecycleViewRow(BoxLayout):
    text = StringProperty()


class MySlider(MDSlider):
    sound = ObjectProperty()

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            GUILayout.updater.cancel()  # Stop further event propagation
        return super(MySlider, self).on_touch_down(touch)

    def on_touch_up(self, touch):
        if self.collide_point(*touch.pos):
            # adjust position of sound
            seeking_sound = GUILayout.slider.max * GUILayout.slider.value_normalized
            GUILayout.song_local = [seeking_sound]
            GUILayout.song_position = GUILayout.sound.seek(seeking_sound)

            # if sound is stopped, restart it
            if GUILayout.sound.state == 'stop':
                GUILayout.sound.play()
                GUILayout.sound.seek(seeking_sound)
            # return the saved return value
            GUILayout.updater()
        return super(MySlider, self).on_touch_up(touch)


class GUILayout(MDFloatLayout, MDGridLayout):
    updater = None
    slider = None
    sound = None
    image_path = StringProperty(os.path.join(os.path.dirname(__file__), 'music.png'))
    directory = os.getcwd()
    if platform != "android":
        set_local_download = os.path.join(os.path.expanduser('~/Documents'), 'Youtube Music Player', 'Downloaded',
                                               'Played')
    else:
        set_local_download = f'{directory}//Downloaded//Played'
    set_local_cache = f'{directory}//Downloaded'
    os.makedirs(set_local_download, exist_ok=True)
    history = f'{set_local_download}//history_log'
    with open(history, 'w') as f:
        f.write('')

    def __draw_shadow__(self, origin, end, context=None):
        pass

    def __init__(self, **kwargs):
        super(GUILayout, self).__init__(**kwargs)
        self.popup = None
        self.play_btt = ObjectProperty(None)
        self.second_screen()
        self.pause_btt = ObjectProperty(None)
        self.song_position = ObjectProperty(None)
        self.song_local = [0]
        GUILayout.slider = None
        self.set_by_slider = False
        self.link = None
        self.paused = False
        self.set_local = None
        self.stream = None
        self.settitle = None
        self.repeat_selected = False
        GUILayout.updater = None
        GUILayout.sound = None
        self.count = 0
        self.results_loaded = False
        self.video_search = None
        self.result = None
        self.result1 = {}
        self.file_loaded = False
        self.playlist = False
        self.shuffle_selected = False
        self.song_change = True

    def second_screen(self):
        songs = self.get_play_list()
        songs.reverse()
        self.ids.rv.data = [{'text': str(x[:-4])} for x in songs]

    def change_screen_item(self, nav_item):
        self.ids.bottom_nav.switch_tab(nav_item)

    def second_screen2(self):
        songs = self.get_play_list()
        songs.reverse()
        MDApp.get_running_app().root.ids.rv.data = [{'text': str(x[:-4])} for x in songs]
        if len(songs) >= 2:
            MDApp.get_running_app().root.ids.shuffle_btt.disabled = False
            MDApp.get_running_app().root.ids.shuffle_btt.opacity = 1
        else:
            MDApp.get_running_app().root.ids.shuffle_btt.disabled = True
            MDApp.get_running_app().root.ids.shuffle_btt.opacity = 0

    def message_box(self, message):
        box = BoxLayout(orientation='vertical', padding=10)
        btn1 = Button(text="Yes")
        btn2 = Button(text="NO")
        box.add_widget(btn1)
        box.add_widget(btn2)
        self.popup = Popup(title='Delete', title_size=30,
                                title_align='center', content=box,
                                size_hint=(None, None), size=(315, 315),
                                auto_dismiss=True)
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
        if self.sound is not None:
            self.stop()
        self.playlist = True
        self.stream = f"{self.set_local_download}//{message}"
        self.set_local = f"{self.set_local_download}//{message[:-4]}.jpg"
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if platform == 'android':
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7
        self.settitle = message[:-4]
        if len(self.settitle) > 51:
            settitle1 = f"{self.settitle[:51]}..."
        else:
            settitle1 = self.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1
        if self.slider is None:
            self.make_slider()
        self.playing()

    def make_slider(self):
        GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0, sound=self.sound,
                                    pos_hint={'center_x': 0.50, 'center_y': 0.3}, size_hint_x=0.6,
                                    size_hint_y=0.1,
                                    opacity=0, disabled=True, step=1)
        MDApp.get_running_app().root.ids.screen_1.add_widget(self.slider)
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1

    def retrieving_song(self):
        previous_songs = []
        with open(self.history, 'r') as f:
            previous_songs.extend(line.strip("\n") for line in f)
        previous_songs.reverse()
        current_song = os.path.basename(self.stream)
        songs = self.get_play_list()
        if len(songs) >= 2:
            if self.shuffle_selected is True:
                songs.remove(current_song)
                next_song = random.choice(songs)
                if (
                        self.song_change is True
                        or self.slider.value <= 5
                        and not previous_songs
                ):
                    self.getting_song(next_song)
                elif self.slider.value > 5:
                    self.play_next_song()
                else:
                    current_song_in_list = previous_songs.index(current_song) + 1
                    if current_song_in_list >= len(previous_songs):
                        self.play_next_song()
                    else:
                        next_song = previous_songs[current_song_in_list]
                        self.getting_song(next_song)
            elif self.song_change is False:
                index_next_song = songs.index(current_song) - 1
                index_next_song = max(index_next_song, 0)
                next_song = songs[index_next_song]
                if self.slider.value > 5:
                    self.play_next_song()
                else:
                    self.getting_song(next_song)
            else:
                index_next_song = songs.index(current_song) + 1
                if index_next_song >= len(songs):
                    index_next_song = 0
                next_song = songs[index_next_song]
                self.getting_song(next_song)

    def get_play_list(self):
        name_list = os.listdir(self.set_local_download)
        full_list = [os.path.join(self.set_local_download, i) for i in name_list]
        time_sorted_list = sorted(full_list, key=os.path.getmtime)
        return [os.path.basename(i) for i in time_sorted_list if i.endswith("m4a")]

    def play_next_song(self):
        self.count = self.count + 1
        GUILayout.sound.play()
        self.slider.max = GUILayout.sound.length
        self.slider.disabled = False
        self.slider.opacity = 1

    def new_search(self):
        self.shuffle_selected = False
        MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        self.playlist = False
        self.count = 0
        self.results_loaded = False
        self.retrieve_text()


    def retrieve_text(self):
        self.paused = False
        self.stop()
        if self.results_loaded is False:
            self.video_search = VideosSearch(MDApp.get_running_app().root.ids.input_box.text, timeout=20)
            self.result = self.video_search.result()
            self.result1 = self.result["result"]
            self.results_loaded = True
            if MDApp.get_running_app().root.ids.input_box.text == '':
                return
        if self.slider is None:
            GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0, sound=self.sound,
                                        pos_hint={'center_x': 0.50, 'center_y': 0.3}, size_hint_x=0.6, size_hint_y=0.1,
                                        opacity=0, disabled=True, step=1)
            MDApp.get_running_app().root.ids.screen_1.add_widget(self.slider)
        if self.results_loaded is False:
            self.video_search = VideosSearch(MDApp.get_running_app().root.ids.input_box.text, timeout=20)
            self.result = self.video_search.result()
            self.result1 = self.result["result"]
            self.results_loaded = True
        try:
            resultdict = self.result1[self.count]
        except IndexError:
            self.count = 0
            resultdict = self.result1[self.count]
        self.setytlink = resultdict['link']
        thumbnail = resultdict['thumbnails']
        self.set_local = thumbnail[0]['url']
        self.settitle = resultdict['title']
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
        self.link = self.set_local
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1

    def loadfile(self):
        try:
            self.download_yt()
        except Exception:
            MDApp.get_running_app().root.ids.info.text = "Error Downloading Music"
            MDApp.get_running_app().root.ids.next_btt.disabled = False
            MDApp.get_running_app().root.ids.previous_btt.disabled = False
            MDApp.get_running_app().root.ids.play_btt.disabled = False
            self.file_loaded = False

    def download_yt(self):
        MDApp.get_running_app().root.ids.next_btt.disabled = True
        MDApp.get_running_app().root.ids.previous_btt.disabled = True
        ydl_opts = (
            {
                "format": 'm4a/bestaudio',
                'logger': CustomLogger(),
                'ignoreerrors': True,
                'cachedir': self.set_local_cache,
                "retries": 20,
            })
        MDApp.get_running_app().root.ids.info.text = "Downloading and Extracting audio... Please wait"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(self.setytlink)
        files = [file for file in os.listdir() if file.endswith(".m4a")]
        for f in files:
            shutil.move(f, f"{self.set_local_download}//{self.settitle}.m4a")
        img_data = requests.get(self.set_local).content
        with open(f"{self.set_local_download}//{self.settitle}.jpg", 'wb') as handler:
            handler.write(img_data)
        self.file_loaded = True

    def checkfile(self):
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        self.filetoplay = (
                f"{self.set_local_download}//{self.settitle}"
                if self.settitle.strip()[-4:] == ".m4a"
                else f"{self.set_local_download}//{self.settitle.strip()}.m4a"
            )
        if os.path.isfile(self.filetoplay):
            self.file_loaded = True
        else:
            self.file_loaded = False
            loadingfile = Thread(target=self.loadfile)
            loadingfile.start()
        self.loadingfiletimer = Clock.schedule_interval(self.waitingforload, 1)
        self.loadingfiletimer()

    def waitingforload(self, dt):
        if self.file_loaded is True:
            self.play_it()
        elif self.file_loaded is False:
            pass
        elif self.file_loaded is None:
            self.file_loaded = False
            self.stop()
            self.loadingfiletimer.cancel()

    def play_it(self):
        self.stream = self.filetoplay
        self.file_loaded = False
        self.loadingfiletimer.cancel()
        MDApp.get_running_app().root.ids.info.text = ''
        self.playing()
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False

    def playing(self):
        self.second_screen2()
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.opacity = 0
        MDApp.get_running_app().root.ids.pause_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.disabled = False
        if self.paused is False:
            self.load_file()
        with open(self.history, 'a') as f:
            f.write(os.path.basename(self.stream) + '\n')
        if self.playlist is False:
            self.playlist = True
            self.count = 0
        GUILayout.sound.bind(state=self.check_for_pause)
        self.slider.disabled = False
        self.slider.opacity = 1
        self.slider.value = 0
        self.slider.max = GUILayout.sound.length
        GUILayout.sound.play()
        MDApp.get_running_app().root.ids.repeat_btt.disabled = False

    def load_file(self):
        GUILayout.sound = SoundLoader.load(self.stream)
        ty_res = time.gmtime(GUILayout.sound.length)
        res = time.strftime("%H:%M:%S", ty_res)
        if str(res[:2]) == '00':
            res = res[3:]
        MDApp.get_running_app().root.ids.song_max.text = str(res)
        MDApp.get_running_app().root.ids.song_position.text = (
            "00:00:00" if str(res).count(':') == 2 else "00:00"
        )

    def check_for_pause(self, arg, arg0):
        if self.paused and arg0 == "play":
            try:
                GUILayout.sound.seek(self.song_local[0])
            except TypeError:
                seekingsound = self.slider.max * self.slider.value_normalized
                GUILayout.sound.seek(seekingsound)
            self.paused = False
        GUILayout.updater = Clock.create_trigger(self.update_slider)
        GUILayout.updater()

    def update_slider(self, dt):
        if GUILayout.sound is None:
            return
        if GUILayout.sound.state == 'stop':
            return
        GUILayout.updater()
        self.slider.value = GUILayout.sound.get_pos()
        settext = GUILayout.sound.length - GUILayout.sound.get_pos()
        ty_res = time.gmtime(settext)
        res = time.strftime("%H:%M:%S", ty_res)
        if str(res[:2]) == '00':
            res = res[3:]
        adding_value = time.gmtime(GUILayout.sound.get_pos())
        res1 = time.strftime("%H:%M:%S", adding_value)
        if str(res1[:2]) == '00':
            res1 = res1[3:]
            MDApp.get_running_app().root.ids.song_position.pos_hint = {'center_x': 0.60, 'center_y': 0.3}
        else:
            MDApp.get_running_app().root.ids.song_position.pos_hint = {'center_x': 0.56, 'center_y': 0.3}
        MDApp.get_running_app().root.ids.song_position.text = str(res1)
        MDApp.get_running_app().root.ids.song_max.text = str(res)
        settingvalue = self.slider.max - self.slider.value
        if settingvalue <= 1.5 and (self.repeat_selected is False and GUILayout.sound.loop is False):
            self.next()

    def repeat_songs_check(self):
        if self.repeat_selected is True:
            self.repeat_selected = False
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 0, 0, 0, 1
            self.repeat_song()
        elif self.repeat_selected is False:
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 1, 0, 0, 1
            self.repeat_selected = True
            self.repeat_song()

    def repeat_song(self):
        if self.repeat_selected is True:
            GUILayout.sound.loop = True
        elif self.repeat_selected is False:
            GUILayout.sound.loop = False

    def shuffle_song_check(self):
        if self.shuffle_selected is True:
            self.shuffle_selected = False
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        elif self.shuffle_selected is False:
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 1, 0, 0, 1
            self.shuffle_selected = True

    def pause(self):
        self.paused = True
        self.song_local = [GUILayout.sound.get_pos()]
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        GUILayout.sound.stop()

    def stop(self):
        if self.sound is None:
            return
        self.slider.disabled = True
        self.slider.opacity = 0
        self.paused = False
        MDApp.get_running_app().root.ids.song_position.text = ''
        MDApp.get_running_app().root.ids.song_max.text = ''
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.repeat_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        if GUILayout.sound.state == 'play':
            GUILayout.sound.stop()
            GUILayout.sound = None

    def next(self):
        self.slider.disabled = True
        self.slider.opacity = 0
        self.paused = False
        if self.sound is not None:
            self.stop()
        self.count = self.count + 1
        if self.playlist is False:
            self.count = min(self.count, 18)
            self.retrieve_text()
        else:
            self.song_change = True
            self.retrieving_song()

    def previous(self):
        self.paused = False
        if self.sound is not None:
            if GUILayout.sound.get_pos() >= 20:
                GUILayout.sound.seek(0)
            else:
                self.stop()
        self.count = self.count - 1
        self.count = max(self.count, 0)
        if self.playlist is False:
            self.retrieve_text()
        else:
            self.song_change = False
            self.retrieving_song()


class Musicapp(MDApp):
    def build(self):
        self.title = 'Youtube Music Player'
        icon = os.path.join(os.path.dirname(__file__), 'music.png')
        self.icon = icon
        self.theme_cls.theme_style = "Light"
        self.theme_cls.primary_palette = "Green"
        self.theme_cls.primary_hue = "500"
        return GUILayout()


if __name__ == '__main__':
    python_files = [file for file in os.listdir() if file.endswith(".webm") or file.endswith(".ytdl") or
                    file.endswith(".part")]
    # Delete old undownloaded files
    for file in python_files:
        os.remove(file)
    Musicapp().run()

import contextlib
import os
import shutil
import requests
import utils
os.environ["KIVY_HOME"] = f'{os.getcwd()}//Downloaded'
if utils.get_platform() == 'android':
    os.environ['KIVY_AUDIO'] = 'android'
    from android.permissions import request_permissions, Permission
    request_permissions([Permission.INTERNET])
else:
    os.environ['KIVY_AUDIO'] = 'gstplayer'
os.environ["KIVY_NO_CONSOLELOG"] = "1"
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


class CustomLogger:
    def debug(self, msg):
        # For compatibility with youtube-dl, both debug and info are passed into debug
        # You can distinguish them by the prefix '[debug] '
        if not msg.startswith('[debug] '):
            self.info(msg)

    def info(self, msg):
        if "[download]" in msg:
            MDApp.get_running_app().root.ids.info.text = "Downloading audio... Please wait\n" \
                                                         f"%{msg[11:]}"
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
            if GUILayout.sound.state != 'play':
                GUILayout.sound.play()
            GUILayout.sound.seek(seeking_sound)
            MDApp.get_running_app().root.ids.play_btt.disabled = True
            MDApp.get_running_app().root.ids.play_btt.opacity = 0
            MDApp.get_running_app().root.ids.pause_btt.opacity = 1
            MDApp.get_running_app().root.ids.pause_btt.disabled = False
            GUILayout.updater()
        return super(MySlider, self).on_touch_up(touch)


class GUILayout(MDFloatLayout, MDGridLayout):
    image_path = StringProperty(f'{os.getcwd()}//music.png')
    directory = os.getcwd()
    if platform != "android":
        set_local_download = os.path.join(os.path.expanduser('~/Documents'), 'Youtube Music Player', 'Downloaded',
                                          'Played')
    else:
        set_local_download = f'{directory}//Downloaded//Played'
    set_local_cache = f'{directory}//Downloaded'
    os.makedirs(set_local_download, exist_ok=True)

    def __draw_shadow__(self, origin, end, context=None):
        pass

    def __init__(self, **kwargs):
        super(GUILayout, self).__init__(**kwargs)
        self.popup = None
        self.play_btt = ObjectProperty(None)
        self.second_screen()
        self.pause_btt = ObjectProperty(None)
        self.song_position = ObjectProperty(None)
        self.set_by_slider = False
        self.link = None
        self.paused = False
        self.set_local = None
        self.stream = None
        self.settitle = None
        self.count = 0
        self.previous_songs = []
        self.results_loaded = False
        self.video_search = None
        self.result = None
        self.result1 = {}
        self.file_loaded = False
        self.playlist = False
        self.song_change = True
        self.repeat_selected = False
        self.shuffle_selected = False
        GUILayout.song_local = [0]
        GUILayout.slider = None
        GUILayout.sound = None
        GUILayout.updater = Clock.create_trigger(self.update_slider)

    def on_pause(self):
        return True

    def on_resume(self):
        pass

    def second_screen(self):
        songs = self.get_play_list()
        self.ids.rv.data = [{'text': str(x[:-4])} for x in songs]

    def change_screen_item(self, nav_item):
        self.ids.bottom_nav.switch_tab(nav_item)

    def second_screen2(self):
        songs = self.get_play_list()
        MDApp.get_running_app().root.ids.rv.data = [{'text': str(x[:-4])} for x in songs]

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
        if GUILayout.sound is not None:
            self.stop()
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
        if GUILayout.slider is None:
            self.make_slider()
        self.playing()

    def make_slider(self):
        GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0, sound=GUILayout.sound,
                                    pos_hint={'center_x': 0.50, 'center_y': 0.3}, size_hint_x=0.6,
                                    size_hint_y=0.1,
                                    opacity=0, disabled=True, step=1)
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

    def retrieving_song(self):
        current_song = os.path.basename(self.stream)
        songs = self.get_play_list()
        if self.song_change is True:
            if self.shuffle_selected is True:
                songs.remove(current_song)
                next_song = random.choice(songs)
                self.getting_song(next_song)
            elif len(songs) >= 2:
                try:
                    index_next_song = songs.index(current_song) + 1
                    try:
                        next_song = songs[index_next_song]
                    except IndexError:
                        next_song = songs[0]
                    self.getting_song(next_song)
                except ValueError:
                    self.count = self.count + 1
                    self.retrieve_text()
            else:
                self.playlist = False
                self.count = self.count + 1
                self.retrieve_text()
        elif self.song_change is False:
            if len(songs) >= 2:
                if GUILayout.slider.value > 5:
                    self.playing()
                else:
                    self.check_against_previous(current_song, songs)
            else:
                self.playlist = False
                self.count = self.count - 1
                self.retrieve_text()

    def check_against_previous(self, current_song, songs):
        current_song_in_list = self.previous_songs.index(current_song) - 1
        next_song = self.previous_songs[current_song_in_list]
        if current_song_in_list <= 0:
            self.previous_songs = []
            try:
                index_next_song = songs.index(current_song) - 1
                next_song = songs[index_next_song]
            except ValueError:
                next_song = songs[0]
        self.getting_song(next_song)

    def get_play_list(self):
        name_list = os.listdir(self.set_local_download)
        full_list = [os.path.join(self.set_local_download, i) for i in name_list]
        time_sorted_list = sorted(full_list, key=os.path.getmtime)
        time_sorted_list.reverse()
        return [os.path.basename(i) for i in time_sorted_list if i.endswith("m4a")]

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
            try:
                MDApp.get_running_app().root.ids.info.text = ""
                if MDApp.get_running_app().root.ids.input_box.text != '':
                    self.video_search = VideosSearch(MDApp.get_running_app().root.ids.input_box.text)
                else:
                    self.video_search = VideosSearch(MDApp.get_running_app().root.ids.song_title.text)
            except TypeError:
                MDApp.get_running_app().root.ids.info.text = "Error Searching Music"
                MDApp.get_running_app().root.ids.next_btt.disabled = False
                MDApp.get_running_app().root.ids.previous_btt.disabled = False
                MDApp.get_running_app().root.ids.play_btt.opacity = 1
                MDApp.get_running_app().root.ids.play_btt.disabled = False
                MDApp.get_running_app().root.ids.pause_btt.disabled = True
                MDApp.get_running_app().root.ids.pause_btt.opacity = 0
                self.file_loaded = False
                return
            self.result = self.video_search.result()
            self.result1 = self.result["result"]
            self.results_loaded = True
            if MDApp.get_running_app().root.ids.input_box.text == '':
                return
        if GUILayout.slider is None:
            GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0, sound=GUILayout.sound,
                                        pos_hint={'center_x': 0.50, 'center_y': 0.3}, size_hint_x=0.6, size_hint_y=0.1,
                                        opacity=0, disabled=True, step=1)
            MDApp.get_running_app().root.ids.screen_1.add_widget(GUILayout.slider)
        if self.results_loaded is False:
            self.video_search = VideosSearch(MDApp.get_running_app().root.ids.input_box.text)
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
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1

    def loadfile(self):
        try:
            self.download_yt()
        except Exception:
            MDApp.get_running_app().root.ids.info.text = "Error Downloading Music"
            MDApp.get_running_app().root.ids.next_btt.disabled = False
            MDApp.get_running_app().root.ids.previous_btt.disabled = False
            MDApp.get_running_app().root.ids.play_btt.opacity = 1
            MDApp.get_running_app().root.ids.play_btt.disabled = False
            MDApp.get_running_app().root.ids.pause_btt.disabled = True
            MDApp.get_running_app().root.ids.pause_btt.opacity = 0
            self.file_loaded = False

    def download_yt(self):
        MDApp.get_running_app().root.ids.next_btt.disabled = True
        MDApp.get_running_app().root.ids.previous_btt.disabled = True
        ydl_opts = (
            {"proxies": {"socks5": "173.249.7.118:2276"},
             "format": 'm4a/bestaudio',
             'logger': CustomLogger(),
             'ignoreerrors': True,
             'cachedir': self.set_local_cache,
             "retries": 20,
             })
        MDApp.get_running_app().root.ids.info.text = "Downloading audio... Please wait"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(self.setytlink)
        files = [file for file in os.listdir() if file.endswith(".m4a")]
        for f in files:
            shutil.move(f, f"{self.set_local_download}//{self.settitle}.m4a")
        proxies = {"socks5": "173.249.7.118:2276"}
        img_data = requests.get(self.set_local, proxies=proxies).content
        with open(f"{self.set_local_download}//{self.settitle}.jpg", 'wb') as handler:
            handler.write(img_data)
        self.file_loaded = True

    def checkfile(self):
        GUILayout.updater.cancel()
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
        MDApp.get_running_app().root.ids.info.text = ''
        self.playing()
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False

    def playing(self):
        self.second_screen2()
        songs = self.get_play_list()
        if len(songs) >= 2:
            self.set_playlist(True, False, 1)
        else:
            self.set_playlist(False, True, 0)
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.disabled = False
        MDApp.get_running_app().root.ids.play_btt.opacity = 0
        MDApp.get_running_app().root.ids.pause_btt.opacity = 1
        if self.paused is False:
            self.load_file()
        self.previous_songs.append(os.path.basename(self.stream))
        self.playlist = True
        self.count = 0
        GUILayout.slider.disabled = False
        GUILayout.slider.opacity = 1
        GUILayout.slider.value = 0
        GUILayout.slider.max = GUILayout.sound.length
        GUILayout.updater()
        GUILayout.sound.play()
        if GUILayout.song_local[0] > 0:
            self.check_for_pause()
        MDApp.get_running_app().root.ids.repeat_btt.disabled = False

    def set_playlist(self, arg0, arg1, arg2):
        self.playlist = arg0
        MDApp.get_running_app().root.ids.shuffle_btt.disabled = arg1
        MDApp.get_running_app().root.ids.shuffle_btt.opacity = arg2

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

    def check_for_pause(self):
        if self.paused:
            try:
                GUILayout.sound.seek(GUILayout.song_local[0])
            except TypeError:
                seekingsound = GUILayout.slider.max * GUILayout.slider.value_normalized
                GUILayout.sound.seek(seekingsound)
            self.paused = False

    def update_slider(self, dt):
        if GUILayout.sound is None:
            return
        if GUILayout.sound.state == 'stop':
            return
        GUILayout.updater()
        GUILayout.slider.value = GUILayout.sound.get_pos()
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
        settingvalue = GUILayout.slider.max - GUILayout.slider.value
        if settingvalue <= 1.5 and (self.repeat_selected is False and GUILayout.sound.loop is False):
            self.next()

    def repeat_songs_check(self):
        if self.repeat_selected is True:
            self.repeat_selected = False
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 0, 0, 0, 1
        else:
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 1, 0, 0, 1
            self.repeat_selected = True
        self.repeat_song()

    def shuffle_song_check(self):
        if self.shuffle_selected is True:
            self.shuffle_selected = False
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        else:
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 1, 0, 0, 1
            self.shuffle_selected = True

    def repeat_song(self):
        if self.repeat_selected is True:
            GUILayout.sound.loop = True
        elif self.repeat_selected is False:
            GUILayout.sound.loop = False

    def pause(self):
        self.paused = True
        GUILayout.song_local = [GUILayout.sound.get_pos()]
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        GUILayout.sound.stop()
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0

    def stop(self):
        if GUILayout.sound is None:
            return
        GUILayout.slider.disabled = True
        GUILayout.slider.opacity = 0
        self.paused = False
        MDApp.get_running_app().root.ids.song_position.text = ''
        MDApp.get_running_app().root.ids.song_max.text = ''
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        MDApp.get_running_app().root.ids.repeat_btt.disabled = True
        if GUILayout.sound.state == 'play':
            GUILayout.sound.stop()
            GUILayout.sound.unload()
            GUILayout.sound = None

    def next(self):
        self.paused = False
        if GUILayout.sound is None:
            self.count = self.count + 1
            if self.playlist is False:
                self.retrieve_text()
        else:
            self.check_song_change(True)

    def previous(self):
        self.paused = False
        if GUILayout.sound is None:
            self.count = self.count - 1
            if self.playlist is False:
                self.retrieve_text()
        elif GUILayout.sound.get_pos() >= 20:
            GUILayout.sound.seek(0)
        else:
            self.check_song_change(False)

    def check_song_change(self, arg0):
        self.stop()
        self.song_change = arg0
        self.retrieving_song()


class Musicapp(MDApp):
    def build(self):
        self.title = 'Youtube Music Player'
        icon = f'{os.getcwd()}//music.png'
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

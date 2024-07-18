import contextlib
import os

os.environ["KIVY_NO_CONSOLELOG"] = "1"
os.environ['KIVY_IMAGE'] = 'pil'
os.environ['KIVY_AUDIO'] = 'gstplayer'

#pyinstaller/installer
os.environ['GST_PLUGIN_PATH_1_0'] = os.path.dirname(__file__)
os.environ['GST_PLUGIN_SYSTEM_PATH_1_0'] = os.path.dirname(__file__)

import random
import shutil
import time
import urllib.request
from threading import Thread
from kivy.clock import Clock
from kivy.config import Config
Config.set('input', 'mouse', 'mouse,disable_multitouch')
Config.set('kivy', 'keyboard_mode', 'system')
from kivy.core.audio import SoundLoader
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
import yt_dlp


class RecycleViewRow(BoxLayout):
    text = StringProperty()


class MySlider(MDSlider):
    sound = ObjectProperty(None)

    def on_touch_up(self, touch):
        if touch.grab_current != self:
            return super(MySlider, self).on_touch_up(touch)
        # call super method and save its return
        ret_val = super(MySlider, self).on_touch_up(touch)
        # adjust position of sound
        seekingsound = GUILayout.slider.max * GUILayout.slider.value_normalized
        GUILayout.song_position = self.sound.seek(seekingsound)

        # if sound is stopped, restart it
        if GUILayout.playingstate == 'stop':
            GUILayout.playing()
        # return the saved return value
        return ret_val


class GUILayout(MDFloatLayout, MDGridLayout):
    def __draw_shadow__(self, origin, end, context=None):
        pass

    def __init__(self, **kwargs):
        super(GUILayout, self).__init__(**kwargs)
        self.second_screen()

    play_btt = ObjectProperty(None)
    pause_btt = ObjectProperty(None)
    song_position = ObjectProperty(None)
    image_path = StringProperty(os.path.join(os.path.dirname(__file__), 'music.png'))
    slider = None
    setbyslider = False
    link = None
    paused = False
    setlocal = None
    stream = None
    settitle = None
    repeatselected = False
    directory = os.getcwd()
    setlocaldownload = f'{directory}//downloaded//Played'
    os.makedirs(setlocaldownload, exist_ok=True)
    history = f'{setlocaldownload}//history_log'
    with open(history, 'w') as f:
        f.write('')
    updater = None
    sound = None
    count = 0
    resultsloaded = False
    videossearch = None
    result = None
    result1 = {}
    fileloaded = False
    playlist = False
    shuffleselected = False
    songchange = True

    def second_screen(self):
        name_list = os.listdir(self.setlocaldownload)
        full_list = [os.path.join(self.setlocaldownload, i) for i in name_list]
        time_sorted_list = sorted(full_list, key=os.path.getmtime)
        songs = [os.path.basename(i) for i in time_sorted_list if i.endswith("mp3")]
        songs.reverse()
        self.ids.rv.data = [{'text': str(x[:-4])} for x in songs]

    def change_screen_item(self, nav_item):
        self.ids.bottom_nav.switch_tab(nav_item)

    @staticmethod
    def second_screen2():
        name_list = os.listdir(GUILayout.setlocaldownload)
        full_list = [os.path.join(GUILayout.setlocaldownload, i) for i in name_list]
        time_sorted_list = sorted(full_list, key=os.path.getmtime)
        songs = [os.path.basename(i) for i in time_sorted_list if i.endswith("mp3")]
        songs.reverse()
        MDApp.get_running_app().root.ids.rv.data = [{'text': str(x[:-4])} for x in songs]
        if len(songs) >= 2:
            MDApp.get_running_app().root.ids.shuffle_btt.disabled = False
            MDApp.get_running_app().root.ids.shuffle_btt.opacity = 1
        else:
            MDApp.get_running_app().root.ids.shuffle_btt.disabled = True
            MDApp.get_running_app().root.ids.shuffle_btt.opacity = 0

    @staticmethod
    def message_box(message):
        box = BoxLayout(orientation='vertical', padding=10)
        btn1 = Button(text="Yes")
        btn2 = Button(text="NO")
        box.add_widget(btn1)
        box.add_widget(btn2)
        GUILayout.popup = Popup(title='Delete', title_size=30,
                                title_align='center', content=box,
                                size_hint=(None, None), size=(315, 315),
                                auto_dismiss=True)
        btn2.bind(on_press=GUILayout.popup.dismiss)
        btn1.bind(on_press=lambda x: GUILayout.remove_track(message))
        GUILayout.popup.open()

    @staticmethod
    def remove_track(message):
        song = f"{GUILayout.setlocaldownload}//{message}"
        image = f"{GUILayout.setlocaldownload}//{message[:-4]}.jpg"
        with contextlib.suppress(PermissionError, FileNotFoundError):
            os.remove(song)
            os.remove(image)
        GUILayout.popup.dismiss()
        GUILayout.second_screen2()

    @staticmethod
    def getting_song(message):
        if GUILayout.sound is not None:
            GUILayout.stop()
        GUILayout.playlist = True
        GUILayout.stream = f"{GUILayout.setlocaldownload}//{message}"
        GUILayout.setlocal = f"{GUILayout.setlocaldownload}//{message[:-4]}.jpg"
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(GUILayout.setlocal)
            if os.name == 'posix':
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7
        GUILayout.settitle = message[:-4]
        if len(GUILayout.settitle) > 51:
            settitle1 = f"{GUILayout.settitle[:51]}..."
        else:
            settitle1 = GUILayout.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1
        if GUILayout.slider is None:
            GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0, sound=GUILayout.sound,
                                        pos_hint={'center_x': 0.50, 'center_y': 0.3}, size_hint_x=0.6,
                                        size_hint_y=0.1,
                                        opacity=0, disabled=True, step=1)
            MDApp.get_running_app().root.ids.screen_1.add_widget(GUILayout.slider)
            MDApp.get_running_app().root.ids.play_btt.disabled = False
            MDApp.get_running_app().root.ids.next_btt.disabled = False
            MDApp.get_running_app().root.ids.previous_btt.disabled = False
            MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
            MDApp.get_running_app().root.ids.play_btt.opacity = 1
            MDApp.get_running_app().root.ids.next_btt.opacity = 1
            MDApp.get_running_app().root.ids.previous_btt.opacity = 1
        GUILayout.playing()

    @staticmethod
    def retrieving_song():
        previous_songs = []
        with open(GUILayout.history, 'r') as f:
            previous_songs.extend(line.strip("\n") for line in f)
        previous_songs.reverse()
        current_song = os.path.basename(GUILayout.stream)
        name_list = os.listdir(GUILayout.setlocaldownload)
        full_list = [os.path.join(GUILayout.setlocaldownload, i) for i in name_list]
        time_sorted_list = sorted(full_list, key=os.path.getmtime)
        songs = [os.path.basename(i) for i in time_sorted_list if i.endswith("mp3")]
        if len(songs) >= 2:
            if GUILayout.shuffleselected is True:
                songs.remove(current_song)
                next_song = random.choice(songs)
                if (
                    GUILayout.songchange is True
                    or GUILayout.slider.value <= 5
                    and not previous_songs
                ):
                    GUILayout.getting_song(next_song)
                elif GUILayout.slider.value > 5:
                    GUILayout.count = GUILayout.count + 1
                    GUILayout.sound.seek(0)
                    GUILayout.sound.play()
                else:
                    currentsong_inlist = previous_songs.index(current_song) + 1
                    if currentsong_inlist >= len(previous_songs):
                        GUILayout.count = GUILayout.count + 1
                        GUILayout.sound.seek(0)
                        GUILayout.sound.play()
                    else:
                        next_song = previous_songs[currentsong_inlist]
                        GUILayout.getting_song(next_song)
            elif GUILayout.songchange is False:
                indexnextsong = songs.index(current_song) - 1
                indexnextsong = max(indexnextsong, 0)
                next_song = songs[indexnextsong]
                if GUILayout.slider.value > 5:
                    GUILayout.count = GUILayout.count + 1
                    GUILayout.sound.seek(0)
                    GUILayout.sound.play()
                else:
                    GUILayout.getting_song(next_song)
            else:
                indexnextsong = songs.index(current_song) + 1
                if indexnextsong >= len(songs):
                    indexnextsong = 0
                next_song = songs[indexnextsong]
                GUILayout.getting_song(next_song)

    def newsearch(self):
        GUILayout.shuffleselected = False
        MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        GUILayout.playlist = False
        GUILayout.count = 0
        GUILayout.resultsloaded = False
        self.retrieve_text()

    @staticmethod
    def retrieve_text():
        GUILayout.paused = False
        GUILayout.stop()
        if GUILayout.resultsloaded is False:
            GUILayout.videossearch = VideosSearch(MDApp.get_running_app().root.ids.input_box.text, limit=20)
            GUILayout.result = GUILayout.videossearch.result()
            GUILayout.result1 = GUILayout.result["result"]
            GUILayout.resultsloaded = True
            if MDApp.get_running_app().root.ids.input_box.text == '':
                return
        if GUILayout.slider is None:
            GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0, sound=GUILayout.sound,
                                        pos_hint={'center_x': 0.50, 'center_y': 0.3}, size_hint_x=0.6, size_hint_y=0.1,
                                        opacity=0, disabled=True, step=1)
            MDApp.get_running_app().root.ids.screen_1.add_widget(GUILayout.slider)
        if GUILayout.resultsloaded is False:
            GUILayout.videossearch = VideosSearch(MDApp.get_running_app().root.ids.input_box.text, limit=20)
            GUILayout.result = GUILayout.videossearch.result()
            GUILayout.result1 = GUILayout.result["result"]
            GUILayout.resultsloaded = True
        resultdict = GUILayout.result1[GUILayout.count]
        GUILayout.setytlink = resultdict['link']
        thumbnail = resultdict['thumbnails']
        GUILayout.setlocal = thumbnail[0]['url']
        GUILayout.settitle = resultdict['title']
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(GUILayout.setlocal)
            if os.name == 'posix':
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7
        if len(GUILayout.settitle) > 51:
            settitle1 = f"{GUILayout.settitle[:51]}..."
        else:
            settitle1 = GUILayout.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1
        GUILayout.settitle = safe_filename(GUILayout.settitle)
        GUILayout.link = GUILayout.setlocal
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False
        MDApp.get_running_app().root.ids.repeat_btt.opacity = 1
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.next_btt.opacity = 1
        MDApp.get_running_app().root.ids.previous_btt.opacity = 1

    @staticmethod
    def loadfile():
        try:
            MDApp.get_running_app().root.ids.song_position.text = (
                "Downloading and Extracting audio"
            )
            # Remove FFmpeg location if running from IDE. Make sure FFmpeg and FFprobe are on Path
            ydl_opts = {'ffmpeg_location': os.path.join(os.path.dirname(__file__), 'prerequisites', 'bin'),
                'format': 'bestaudio',
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }],
                    }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download(GUILayout.setytlink)
            files = [f for f in os.listdir('.') if os.path.isfile(f)]
            for f in files:
                if f.endswith(".mp3"):
                    shutil.move(f, f"{GUILayout.setlocaldownload}//{GUILayout.settitle}.mp3")
            urllib.request.urlretrieve(GUILayout.setlocal,
                                       f"{GUILayout.setlocaldownload}//{GUILayout.settitle}.jpg")
            GUILayout.fileloaded = True
        except Exception:
            GUILayout.fileloaded = False

    @staticmethod
    def checkfile():
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        GUILayout.filetoplay = (
            f"{GUILayout.setlocaldownload}//{GUILayout.settitle}"
            if GUILayout.settitle.strip()[-4:] == ".mp3"
            else f"{GUILayout.setlocaldownload}//{GUILayout.settitle.strip()}.mp3"
        )
        if os.path.isfile(GUILayout.filetoplay):
            GUILayout.fileloaded = True
        else:
            loadingfile = Thread(target=GUILayout.loadfile)
            loadingfile.start()
        GUILayout.loadingfiletimer = Clock.schedule_interval(GUILayout.waitingforload, 1)

    @staticmethod
    def waitingforload(dt):
        if GUILayout.fileloaded is True:
            GUILayout.stream = GUILayout.filetoplay
            GUILayout.fileloaded = False
            GUILayout.loadingfiletimer.cancel()
            GUILayout.playing()
        elif GUILayout.fileloaded is False:
            pass
        elif GUILayout.fileloaded is None:
            GUILayout.fileloaded = False
            GUILayout.loadingfiletimer.cancel()
            GUILayout.stop_playing()

    @staticmethod
    def playing():
        GUILayout.second_screen2()
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.opacity = 0
        MDApp.get_running_app().root.ids.pause_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.disabled = False
        if GUILayout.paused is False:
            GUILayout.sound = SoundLoader.load(GUILayout.stream)
            GUILayout.slider.sound = GUILayout.sound
            GUILayout.slider.disabled = False
            GUILayout.slider.opacity = 1
            GUILayout.slider.max = GUILayout.sound.length
            ty_res = time.gmtime(GUILayout.sound.length)
            res = time.strftime("%H:%M:%S", ty_res)
            if str(res[:2]) == '00':
                res = res[3:]
            MDApp.get_running_app().root.ids.song_max.text = str(res)
            if str(res).count(':') == 2:
                MDApp.get_running_app().root.ids.song_position.text = "00:00:00"
            else:
                MDApp.get_running_app().root.ids.song_position.text = "00:00"
        if GUILayout.songchange is True:
            with open(GUILayout.history, 'a') as f:
                f.write(os.path.basename(GUILayout.stream) + '\n')
        if GUILayout.playlist is False:
            GUILayout.playlist = True
            GUILayout.count = 0
        GUILayout.sound.play()
        GUILayout.playingstate = "play"
        MDApp.get_running_app().root.ids.repeat_btt.disabled = False
        if GUILayout.paused:
            try:
                GUILayout.sound.seek(GUILayout.song_position)
            except TypeError:
                seekingsound = GUILayout.slider.max * GUILayout.slider.value_normalized
                GUILayout.sound.seek(seekingsound)
            GUILayout.paused = False
        GUILayout.updater = Clock.create_trigger(GUILayout.update_slider)
        GUILayout.updater()

    @staticmethod
    def update_slider(dt):
        if GUILayout.playingstate != 'play':
            return
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
        GUILayout.updater()
        settingvalue = GUILayout.slider.max - GUILayout.slider.value
        if settingvalue <= 1.5:
            GUILayout.playingstate = "stop"
            GUILayout.next()

    @staticmethod
    def repeat_songs_check():
        if GUILayout.repeatselected is True:
            GUILayout.repeatselected = False
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 0, 0, 0, 1
            GUILayout.repeat_song()
        elif GUILayout.repeatselected is False:
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 1, 0, 0, 1
            GUILayout.repeatselected = True
            GUILayout.repeat_song()

    @staticmethod
    def repeat_song():
        if GUILayout.repeatselected is True:
            GUILayout.sound.loop = True
        elif GUILayout.repeatselected is False:
            GUILayout.sound.loop = False

    @staticmethod
    def shuffle_song_check():
        if GUILayout.shuffleselected is True:
            GUILayout.shuffleselected = False
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        elif GUILayout.shuffleselected is False:
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 1, 0, 0, 1
            GUILayout.shuffleselected = True

    @staticmethod
    def pause():
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        GUILayout.song_position = GUILayout.sound.get_pos()
        GUILayout.sound.stop()
        GUILayout.playingstate = "stop"
        GUILayout.paused = True

    @staticmethod
    def stop():
        if GUILayout.sound is None:
            return
        GUILayout.slider.disabled = True
        GUILayout.slider.opacity = 0
        GUILayout.paused = False
        MDApp.get_running_app().root.ids.song_position.text = ''
        MDApp.get_running_app().root.ids.song_max.text = ''
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.repeat_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0
        if GUILayout.playingstate == 'play':
            GUILayout.sound.stop()
        GUILayout.sound.unload()
        GUILayout.playingstate = "stop"
        GUILayout.sound = None

    @staticmethod
    def next():
        GUILayout.paused = False
        if GUILayout.sound is not None:
            GUILayout.stop()
        GUILayout.count = GUILayout.count + 1
        if GUILayout.playlist is False:
            GUILayout.count = min(GUILayout.count, 18)
            GUILayout.retrieve_text()
        else:
            GUILayout.songchange = True
            GUILayout.retrieving_song()

    def previous(self):
        GUILayout.paused = False
        if GUILayout.sound.get_pos() >= 20:
            self.sound.seek(0)
        else:
            if GUILayout.sound is not None:
                GUILayout.stop()
            GUILayout.count = GUILayout.count - 1
            GUILayout.count = max(GUILayout.count, 0)
            if GUILayout.playlist is False:
                self.retrieve_text()
            else:
                GUILayout.songchange = False
                GUILayout.retrieving_song()


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
    Musicapp().run()
import contextlib
import os
import utils

if utils.get_platform() == 'android':
    from android.permissions import request_permissions, Permission
    from jnius import autoclass
    from android.storage import primary_external_storage_path
    request_permissions([Permission.INTERNET, Permission.FOREGROUND_SERVICE, Permission.MEDIA_CONTENT_CONTROL,
                         Permission.WRITE_EXTERNAL_STORAGE, Permission.READ_EXTERNAL_STORAGE,
                         Permission.READ_MEDIA_AUDIO, Permission.READ_MEDIA_IMAGES])
    os.makedirs(os.path.normpath(os.path.join(primary_external_storage_path(), 'Download', 'Youtube Music Player',
                                              'Downloaded', 'Played')), exist_ok=True)
else:
    os.makedirs(os.path.normpath(os.path.join(os.path.expanduser('~/Documents'), 'Youtube Music Player', 'Downloaded'))
                , exist_ok=True)
    os.environ["KIVY_HOME"] = os.path.join(os.path.expanduser('~/Documents'), 'Youtube Music Player', 'Downloaded')
    os.environ['KIVY_AUDIO'] = 'gstplayer'
os.environ["KIVY_NO_CONSOLELOG"] = "1"
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer
from kivy import platform
import time
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


class RecycleViewRow(BoxLayout):
    text = StringProperty()


class MySlider(MDSlider):
    sound = ObjectProperty()

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            GUILayout.get_update_slider.cancel()  # Stop further event propagation
        return super(MySlider, self).on_touch_down(touch)

    def on_touch_up(self, touch):
        if self.collide_point(*touch.pos):
            # adjust position of sound
            seeking_sound = GUILayout.slider.max * GUILayout.slider.value_normalized
            GUILayout.send('normalized', str(seeking_sound))
            GUILayout.get_update_slider()
            MDApp.get_running_app().root.ids.play_btt.disabled = True
            MDApp.get_running_app().root.ids.play_btt.opacity = 0
            MDApp.get_running_app().root.ids.pause_btt.opacity = 1
            MDApp.get_running_app().root.ids.pause_btt.disabled = False
        return super(MySlider, self).on_touch_up(touch)


class GUILayout(MDFloatLayout, MDGridLayout):
    image_path = StringProperty(os.path.join(os.path.dirname(__file__), 'music.png'))
    if platform != "android":
        set_local_download = os.path.normpath(os.path.join(os.path.expanduser('~/Documents'), 'Youtube Music Player',
                                                           'Downloaded', 'Played'))
    else:
        set_local_download = os.path.normpath(os.path.join(primary_external_storage_path(), 'Download',
                                                           'Youtube Music Player', 'Downloaded', 'Played'))
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
        if platform == 'android':
            from android import mActivity
            context = mActivity.getApplicationContext()
            SERVICE_NAME = f'{str(context.getPackageName())}.ServiceMusicservice'
            GUILayout.service_activity = autoclass('org.kivy.android.PythonActivity').mActivity
            service = autoclass(SERVICE_NAME)
            service.start(GUILayout.service_activity, '')
            GUILayout.service = service
        elif platform in ('linux', 'linux2', 'macos', 'win'):
            from runpy import run_path
            from threading import Thread
            GUILayout.service = Thread(
                target=run_path,
                args=[os.path.join(os.path.dirname(__file__), 'service', 'main.py')],
                kwargs={'run_name': '__main__'},
                daemon=True
            )
            GUILayout.service.start()
        else:
            raise NotImplementedError(
                "service start not implemented on this platform"
            )
        self.server = server = OSCThreadServer(encoding='utf8')
        server.listen(
            address=b'localhost',
            port=3002,
            default=True,
        )
        server.bind(u'/set_slider', self.set_slider)
        server.bind(u'/song_pos', self.update_slider)
        server.bind(u'/normalize', self.normalize_slider)
        server.bind(u'/update_image', self.update_image)
        server.bind(u'/reset_gui', self.reset_gui)
        server.bind(u'/file_is_downloaded', self.file_is_downloaded)
        server.bind(u'/data_info', self.update_info)
        server.bind(u'/are_we', self.check_are_we_playing)
        GUILayout.client = OSCClient(u'localhost', 3000, encoding='utf8')
        GUILayout.song_local = [0]
        GUILayout.slider = None
        GUILayout.playing_song = False
        GUILayout.check_are_play = None
        self.loadingosctimer = Clock.schedule_interval(self.waitingforoscload, 1)
        self.fire_stop = Clock.schedule_interval(self.checking_for_stop, 1)
        GUILayout.gui_resume_check = Clock.schedule_interval(self.set_gui_from_check, 1)
        GUILayout.get_update_slider = Clock.schedule_interval(self.wait_update_slider, 1)

    @staticmethod
    def set_gui_resume(arg0, arg1, arg2, arg3):
        MDApp.get_running_app().root.ids.play_btt.opacity = arg0
        MDApp.get_running_app().root.ids.play_btt.disabled = arg1
        MDApp.get_running_app().root.ids.pause_btt.disabled = arg2
        MDApp.get_running_app().root.ids.pause_btt.opacity = arg3
        MDApp.get_running_app().root.ids.next_btt.disabled = arg2
        MDApp.get_running_app().root.ids.previous_btt.disabled = arg2

    def second_screen(self):
        songs = self.get_play_list()
        self.ids.rv.data = [{'text': str(x[:-4])} for x in songs]

    def change_screen_item(self, nav_item):
        self.second_screen2()
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
        GUILayout.send('update_load_fs', 'update_load_fs')
        if GUILayout.playing_song:
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

    def update_image(self, *val):
        message = ''.join(val)
        self.stream = f"{self.set_local_download}//{message[2:-6]}"
        self.set_local = f"{self.set_local_download}//{message[2:-6]}.jpg"
        with contextlib.suppress(Exception):
            MDApp.get_running_app().root.ids.imageView.source = str(self.set_local)
            if platform == 'android':
                MDApp.get_running_app().root.ids.imageView.size_hint_x = 0.7
                MDApp.get_running_app().root.ids.imageView.size_hint_y = 0.7
        self.settitle = message[2:-6]
        if len(self.settitle) > 51:
            settitle1 = f"{self.settitle[:51]}..."
        else:
            settitle1 = self.settitle
        MDApp.get_running_app().root.ids.song_title.text = settitle1

    def make_slider(self):
        if GUILayout.slider is None:
            GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0,
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
        GUILayout.send('update_load_fs', 'update_load_fs')
        self.paused = False
        self.stop()
        if self.results_loaded is False:
            try:
                if MDApp.get_running_app().root.ids.input_box.text != '':
                    self.video_search = VideosSearch(MDApp.get_running_app().root.ids.input_box.text)
                else:
                    self.video_search = VideosSearch(MDApp.get_running_app().root.ids.song_title.text)
            except TypeError:
                self.error_reset("search")
                return
            self.result = self.video_search.result()
            self.result1 = self.result["result"]
            self.results_loaded = True
            if MDApp.get_running_app().root.ids.input_box.text == '':
                return
        if GUILayout.slider is None:
            GUILayout.slider = MySlider(orientation="horizontal", min=0, max=100, value=0,
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
        MDApp.get_running_app().root.ids.imageView.source = os.path.join(os.path.dirname(__file__), 'music.png')
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
        MDApp.get_running_app().root.ids.song_position.text = ''
        MDApp.get_running_app().root.ids.song_max.text = ''
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
        GUILayout.send('downloadyt', [self.setytlink, self.settitle, self.set_local, self.set_local_download])

    def file_is_downloaded(self, *val):
        maybe = ''.join(val)
        if maybe == 'yep':
            self.file_loaded = True
        elif maybe == 'nope':
            self.error_reset("download")

    def update_info(self, *val):
        msg = ''.join(val)
        MDApp.get_running_app().root.ids.info.text = "Downloading audio... Please wait\n" \
                                                     f"{msg[11:]}"

    def checkfile(self):
        self.fire_stop()
        MDApp.get_running_app().root.ids.play_btt.disabled = True
        MDApp.get_running_app().root.ids.info.text = ""
        MDApp.get_running_app().root.ids.song_position.text = ''
        MDApp.get_running_app().root.ids.song_max.text = ''
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
        MDApp.get_running_app().root.ids.info.text = ''
        self.playing()
        MDApp.get_running_app().root.ids.next_btt.disabled = False
        MDApp.get_running_app().root.ids.previous_btt.disabled = False

    def playing(self):
        GUILayout.get_update_slider()
        self.second_screen2()
        songs = self.get_play_list()
        if len(songs) >= 2:
            self.set_playlist(True, False, 1)
            GUILayout.send('playlist', songs)
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
            GUILayout.send('play', "play")

    @staticmethod
    def wait_update_slider(dt):
        GUILayout.send('get_update_slider', "play")

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
        self.length = float(''.join(val))
        GUILayout.slider.max = self.length
        ty_res = time.gmtime(self.length)
        res = time.strftime("%H:%M:%S", ty_res)
        if str(res[:2]) == '00':
            res = res[3:]
        MDApp.get_running_app().root.ids.song_max.text = str(res)
        MDApp.get_running_app().root.ids.song_position.text = (
            "00:00:00" if str(res).count(':') == 2 else "00:00"
        )

        self.fileosc_loaded = True

    def update_slider(self, *val):
        self.song_pos = float(''.join(val))
        GUILayout.slider.value = self.song_pos
        settext = self.length - self.song_pos
        ty_res = time.gmtime(settext)
        res = time.strftime("%H:%M:%S", ty_res)
        if str(res[:2]) == '00':
            res = res[3:]
        adding_value = time.gmtime(self.song_pos)
        res1 = time.strftime("%H:%M:%S", adding_value)
        if str(res1[:2]) == '00':
            res1 = res1[3:]
            MDApp.get_running_app().root.ids.song_position.pos_hint = {'center_x': 0.60, 'center_y': 0.3}
        else:
            MDApp.get_running_app().root.ids.song_position.pos_hint = {'center_x': 0.56, 'center_y': 0.3}
        MDApp.get_running_app().root.ids.song_position.text = str(res1)
        MDApp.get_running_app().root.ids.song_max.text = str(res)

    def normalize_slider(self):
        seekingsound = GUILayout.slider.max * GUILayout.slider.value_normalized
        GUILayout.send('normalized', str(seekingsound))

    def repeat_songs_check(self):
        if self.repeat_selected is True:
            self.repeat_selected = False
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 0, 0, 0, 1
            GUILayout.send('loop', 'False')
        else:
            MDApp.get_running_app().root.ids.repeat_btt.text_color = 1, 0, 0, 1
            self.repeat_selected = True
            GUILayout.send('loop', 'True')

    def shuffle_song_check(self):
        if self.shuffle_selected is True:
            self.shuffle_selected = False
            GUILayout.send('shuffle', 'False')
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 0, 0, 0, 1
        else:
            MDApp.get_running_app().root.ids.shuffle_btt.text_color = 1, 0, 0, 1
            self.shuffle_selected = True
            GUILayout.send('shuffle', 'True')

    def pause(self):
        self.paused = True
        GUILayout.playing_song = False
        GUILayout.get_update_slider.cancel()
        GUILayout.send('pause', 'pause')
        MDApp.get_running_app().root.ids.pause_btt.disabled = True
        MDApp.get_running_app().root.ids.play_btt.disabled = False
        MDApp.get_running_app().root.ids.play_btt.opacity = 1
        MDApp.get_running_app().root.ids.pause_btt.opacity = 0

    def reset_gui(self, *val):
        GUILayout.playing_song = False
        self.fire_off_stop = True

    def check_are_we_playing(self, *val):
        GUILayout.check_are_play = ''.join(val)

    def set_gui_from_check(self, dt):
        if GUILayout.check_are_play == 'False':
            GUILayout.set_gui_resume(0, True, False, 1)
            GUILayout.gui_resume_check.cancel()
        elif GUILayout == 'True':
            GUILayout.set_gui_resume(1, False, True, 0)
            GUILayout.gui_resume_check.cancel()
        elif GUILayout.set_gui_resume is None:
            pass

    def stop(self):
        if GUILayout.slider is not None:
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
        if GUILayout.playing_song is True:
            GUILayout.send('stop', 'stop music')
        GUILayout.get_update_slider.cancel()

    def next(self):
        self.paused = False
        if self.playlist:
            GUILayout.send('next', self.playlist)
        else:
            self.count = self.count + 1
            self.retrieve_text()

    def previous(self):
        self.paused = False
        if self.playlist:
            GUILayout.send('previous', self.playlist)
        else:
            self.count = self.count - 1
            self.retrieve_text()

    @staticmethod
    def send(message_type, message):
        message = f'{message}'
        if message_type == "load":
            GUILayout.client.send_message(u'/load', message)
        elif message_type == "play":
            GUILayout.client.send_message(u'/play', message)
        elif message_type == "normalized":
            GUILayout.client.send_message(u'/normalized', f'u{message}')
        elif message_type == "pause":
            GUILayout.client.send_message(u'/pause', message)
        elif message_type == "next":
            GUILayout.client.send_message(u'/next', message)
        elif message_type == "stop":
            GUILayout.client.send_message(u'/stop', message)
        elif message_type == "playlist":
            GUILayout.client.send_message(u'/playlist', message)
        elif message_type == "update_load_fs":
            GUILayout.client.send_message(u'/update_load_fs', message)
        elif message_type == "previous":
            GUILayout.client.send_message(u'/previous', message)
        elif message_type == "iamawake":
            GUILayout.client.send_message(u'/iamawake', message)
        elif message_type == "loop":
            GUILayout.client.send_message(u'/loop', f'u{message}')
        elif message_type == "shuffle":
            GUILayout.client.send_message(u'/shuffle', message)
        elif message_type == "get_update_slider":
            GUILayout.client.send_message(u'/get_update_slider', message)
        elif message_type == "downloadyt":
            GUILayout.client.send_message(u'/downloadyt', message)
        elif message_type == "iampaused":
            GUILayout.client.send_message(u'/iampaused', message)


class Musicapp(MDApp):
    def build(self):
        self.title = 'Youtube Music Player'
        icon = os.path.join(os.path.dirname(__file__), 'music.png')
        self.icon = icon
        self.theme_cls.theme_style = "Light"
        self.theme_cls.primary_palette = "Green"
        self.theme_cls.primary_hue = "500"
        return GUILayout()

    def stop_service(self):
        if GUILayout.service:
            if platform == "android":
                GUILayout.service.stop(GUILayout.service_activity)
            elif platform in ('linux', 'linux2', 'macos', 'win'):
                # thread is deamon should stop when main thread ends
                return
            else:
                raise NotImplementedError(
                    "service start not implemented on this platform"
                )
            GUILayout.service = None

    def on_pause(self):
        GUILayout.send('iampaused', ':(')
        return True

    def on_resume(self):
        GUILayout.gui_resume_check()
        GUILayout.send('iamawake', 'Heelloo')


if __name__ == '__main__':
    python_files = [file for file in os.listdir() if file.endswith(".webm") or file.endswith(".ytdl") or
                    file.endswith(".part")]
    # Delete old undownloaded files
    for file in python_files:
        with contextlib.suppress(PermissionError):
            os.remove(file)
    Musicapp().run()
    Musicapp().stop_service()

import contextlib
import os.path
import random
import shutil
import time
from threading import Thread
import requests
import yt_dlp
import utils

if utils.get_platform() == 'android':
    # for running on android service
    os.environ['KIVY_AUDIO'] = 'android'
    from jnius import autoclass
    PythonService = autoclass('org.kivy.android.PythonService')
    autoclass('org.jnius.NativeInvocationHandler')
else:
    os.environ['KIVY_AUDIO'] = 'gstplayer'
from kivy.core.audio import SoundLoader
from oscpy.server import OSCThreadServer
from oscpy.client import OSCClient

CLIENT = OSCClient('localhost', 3002, encoding="utf-8")


class CustomLogger:
    def debug(self, msg):
        # For compatibility with youtube-dl, both debug and info are passed into debug
        # You can distinguish them by the prefix '[debug] '
        if not msg.startswith('[debug] '):
            self.info(msg)

    def info(self, msg):
        if "[download]" in msg:
            Gui_sounds.send('data_info', msg)

    def error(self, msg):
        print(msg)

    def warning(self, msg):
        print(msg)


class Gui_sounds():
    length = None
    previous_songs = []
    set_local = None
    load_from_service = False
    if utils.get_platform() != "android":
        set_local_download = os.path.join(os.path.expanduser('~/Documents'), 'Youtube Music Player', 'Downloaded',
                                          'Played')
    else:
        set_local_download = f'{os.getcwd()}//Downloaded//Played'
    shuffle_selected = False
    playlist = False
    song_change = False
    file_to_load = None
    song_local = None
    sound = None
    paused = False
    checking_it = None

    @staticmethod
    def load(*val):
        Gui_sounds.stop()
        Gui_sounds.file_to_load = ''.join(val)
        Gui_sounds.file_to_load = os.path.normpath(Gui_sounds.file_to_load)
        Gui_sounds.sound = SoundLoader.load(Gui_sounds.file_to_load)
        Gui_sounds.length = Gui_sounds.sound.length
        Gui_sounds.send("set_slider", str(Gui_sounds.length))
        if Gui_sounds.load_from_service:
            Gui_sounds.send("update_image", [Gui_sounds.set_local])
        Gui_sounds.play()

    def download_yt(self, *val):
        setytlink, settitle, set_local, set_local_download = ''.join(val).strip("']").split("', '")
        ydl_opts = (
            {"proxies": {"socks5": "173.249.7.118:2276"},
             "format": 'm4a/bestaudio',
             'logger': CustomLogger(),
             'ignoreerrors': True,
             'cachedir': f'{os.getcwd()}//Downloaded',
             "retries": 20,
             })
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download(setytlink)
            files = [file for file in os.listdir() if file.endswith(".m4a")]
            for f in files:
                shutil.move(f, f"{set_local_download}//{settitle}.m4a")
            proxies = {"socks5": "173.249.7.118:2276"}
            img_data = requests.get(set_local, proxies=proxies).content
            with open(f"{set_local_download}//{settitle}.jpg", 'wb') as handler:
                handler.write(img_data)
            Gui_sounds.send("file_is_downloaded", "yep")
        except Exception as e:
            Gui_sounds.send("file_is_downloaded", e)

    def update_load_fs(self, *val):
        Gui_sounds.load_from_service = False

    @staticmethod
    def play(*val):
        if Gui_sounds.song_local and Gui_sounds.song_local[0] > 0:
            Gui_sounds.check_for_pause()
        else:
            Gui_sounds.paused = False
            Gui_sounds.song_local = None
            Gui_sounds.previous_songs.append(os.path.basename(Gui_sounds.file_to_load))
            Gui_sounds.sound.play()
            Gui_sounds.previous = False
            if Gui_sounds.checking_it is None:
                Gui_sounds.checking_it = Thread(target=Gui_sounds.check_for_next, daemon=True)
                Gui_sounds.checking_it.start()


    @staticmethod
    def check_for_next():
        while True:
            if (Gui_sounds.sound is not None and (Gui_sounds.paused is False or Gui_sounds.sound.state == 'play')
                    and Gui_sounds.length - Gui_sounds.sound.get_pos() <= 1):
                if Gui_sounds.previous is True:
                    continue
                if Gui_sounds.playlist is False or Gui_sounds.playlist == 'False':
                    Gui_sounds.send("reset_gui", "reset_gui")
                    Gui_sounds.checking_it = None
                    break
                Gui_sounds.next()
            time.sleep(1)

    def update_slider(self, *val):
        with contextlib.suppress(AttributeError):
            pos = int(Gui_sounds.sound.get_pos())
            Gui_sounds.send("song_pos", str(pos))

    @staticmethod
    def check_for_pause():
        if Gui_sounds.paused:
            try:
                Gui_sounds.sound.play()
                Gui_sounds.sound.seek(Gui_sounds.song_local[0])
            except TypeError:
                Gui_sounds.send("normalize", "normalize please")
        else:
            Gui_sounds.sound.play()
        Gui_sounds.previous = False
        Gui_sounds.paused = False
        Gui_sounds.song_local = None

    def normalized(self, *val):
        seekingsound = float(''.join(val))
        with contextlib.suppress(AttributeError):
            if Gui_sounds.sound.state != "play":
                Gui_sounds.sound.play()
                Gui_sounds.previous = False
            Gui_sounds.sound.seek(seekingsound)

    def pause(self, *val):
        Gui_sounds.paused = True
        Gui_sounds.song_local = [Gui_sounds.sound.get_pos()]
        Gui_sounds.sound.stop()

    @staticmethod
    def stop(*val):
        if Gui_sounds.sound is not None and Gui_sounds.sound.state == "play":
            Gui_sounds.sound.stop()
            Gui_sounds.sound.unload()
            Gui_sounds.sound = None

    @staticmethod
    def next(*val):
        Gui_sounds.paused = False
        Gui_sounds.previous = False
        Gui_sounds.load_from_service = True
        Gui_sounds.check_song_change(True)

    def previous(self, *val):
        Gui_sounds.paused = False
        Gui_sounds.previous = True
        Gui_sounds.load_from_service = True
        if Gui_sounds.sound.get_pos() >= 20:
            Gui_sounds.sound.seek(0)
        else:
            Gui_sounds.check_song_change(False)

    @staticmethod
    def check_song_change(arg0):
        Gui_sounds.song_change = arg0
        if Gui_sounds.song_change is True and (Gui_sounds.sound is not None and Gui_sounds.sound.state == 'play'):
            Gui_sounds.stop()
        Gui_sounds.retrieving_song()

    @staticmethod
    def retrieving_song():
        current_song = os.path.basename(Gui_sounds.file_to_load)
        songs = Gui_sounds.playlist
        with contextlib.suppress(TypeError):
            if Gui_sounds.song_change is True:
                if Gui_sounds.shuffle_selected is True:
                    songs.remove(current_song)
                    next_song = random.choice(songs)
                    Gui_sounds.getting_song(next_song)
                elif len(songs) >= 2:
                    with contextlib.suppress(ValueError):
                        index_next_song = songs.index(current_song) + 1
                        try:
                            next_song = songs[index_next_song]
                        except IndexError:
                            next_song = songs[0]
                        Gui_sounds.getting_song(next_song)
            elif Gui_sounds.song_change is False:
                if len(songs) >= 2:
                    if Gui_sounds.sound is not None:
                        Gui_sounds.check_against_previous(current_song, songs)
                else:
                    Gui_sounds.playlist = False

    @staticmethod
    def check_against_previous(current_song, songs):
        current_song_in_list = Gui_sounds.previous_songs.index(current_song) - 1
        next_song = Gui_sounds.previous_songs[current_song_in_list]
        if current_song_in_list <= -1:
            Gui_sounds.previous_songs = []
            try:
                index_next_song = songs.index(current_song) - 1
                next_song = songs[index_next_song]
            except ValueError:
                next_song = songs[0]
        Gui_sounds.getting_song(next_song)

    @staticmethod
    def getting_song(message):
        Gui_sounds.stream = f"{Gui_sounds.set_local_download}//{message}"
        Gui_sounds.set_local = message
        Gui_sounds.load(Gui_sounds.stream)

    def play_list(self, *val):
        Gui_sounds.playlist = ''.join(val[1:-1]).strip("'").split("', '")

    def refresh_gui(self, *val):
        Gui_sounds.send("update_image", [Gui_sounds.set_local])

    def loop(self, *val):
        Gui_sounds.looping_bool = ''.join(val)
        Gui_sounds.sound.loop = Gui_sounds.looping_bool == 'True'

    @staticmethod
    def shuffle(*val):
        Gui_sounds.shuffle_bool = ''.join(val)
        if Gui_sounds.shuffle_bool == 'True':
            Gui_sounds.shuffle_selected = True
        else:
            Gui_sounds.shuffle_selected = False

    @staticmethod
    def send(message_type, message):
        message = f'{message}'
        if message_type == "normalize":
            CLIENT.send_message(u'/normalize', message)
        elif message_type == "song_pos":
            CLIENT.send_message(u'/song_pos', message)
        elif message_type == "set_slider":
            CLIENT.send_message(u'/set_slider', message)
        elif message_type == "update_image":
            CLIENT.send_message(u'/update_image', message)
        elif message_type == "reset_gui":
            CLIENT.send_message(u'/reset_gui', message)
        elif message_type == "file_is_downloaded":
            CLIENT.send_message(u'/file_is_downloaded', message)
        elif message_type == "data_info":
            CLIENT.send_message(u'/data_info', message)


if __name__ == '__main__':
    SERVER = OSCThreadServer(encoding='utf8')
    SERVER.listen('localhost', port=3000, default=True)
    SERVER.bind(u'/load', Gui_sounds.load)
    SERVER.bind(u'/play', Gui_sounds.play)
    SERVER.bind(u'/normalized', Gui_sounds.normalized)
    SERVER.bind(u'/pause', Gui_sounds.pause)
    SERVER.bind(u'/stop', Gui_sounds.stop)
    SERVER.bind(u'/next', Gui_sounds.next)
    SERVER.bind(u'/previous', Gui_sounds.previous)
    SERVER.bind(u'/playlist', Gui_sounds.play_list)
    SERVER.bind(u'/update_load_fs', Gui_sounds.update_load_fs)
    SERVER.bind(u'/iamawake', Gui_sounds.refresh_gui)
    SERVER.bind(u'/loop', Gui_sounds.loop)
    SERVER.bind(u'/shuffle', Gui_sounds.shuffle)
    SERVER.bind(u'/get_update_slider', Gui_sounds.update_slider)
    SERVER.bind(u'/downloadyt', Gui_sounds.download_yt)
    while True:
        time.sleep(1)

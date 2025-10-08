import contextlib
import os.path
import random
import time
from threading import Thread
import requests
import yt_dlp
import utils

if utils.get_platform() == 'android':
    # for running on android service
    os.environ['KIVY_AUDIO'] = 'android'
    from jnius import autoclass
    from android.storage import primary_external_storage_path
    PythonService = autoclass('org.kivy.android.PythonService')
    autoclass('org.jnius.NativeInvocationHandler')
else:
    os.environ['KIVY_AUDIO'] = 'gstplayer'
from kivy.core.audio import SoundLoader
from oscpy.server import OSCThreadServer
from oscpy.client import OSCClient

CLIENT = OSCClient('localhost', 3002, encoding="utf-8")


# Foreground service helper for Android
def _start_in_foreground_if_android():
    try:
        if utils.get_platform() == 'android':
            from jnius import autoclass, cast
            PythonService = autoclass('org.kivy.android.PythonService').mService
            Context = autoclass('android.content.Context')
            NotificationManager = autoclass('android.app.NotificationManager')
            Build = autoclass('android.os.Build')
            String = autoclass('java.lang.String')
            NotificationCompat = autoclass('androidx.core.app.NotificationCompat')
            NotificationChannel = autoclass('android.app.NotificationChannel')

            nm = cast(NotificationManager, PythonService.getSystemService(Context.NOTIFICATION_SERVICE))
            channel_id = String('music_playback')

            if Build.VERSION.SDK_INT >= 26:
                channel = NotificationChannel(channel_id, String('Playback'), NotificationManager.IMPORTANCE_LOW)
                nm.createNotificationChannel(channel)

            builder = NotificationCompat.Builder(PythonService, channel_id)\
                .setContentTitle('Playing music')\
                .setContentText('Your music continues in the background')\
                .setSmallIcon(PythonService.getApplicationInfo().icon)\
                .setOngoing(True)

            notification = builder.build()
            PythonService.startForeground(1, notification)
    except Exception as e:
        print(f'[service] start_in_foreground failed: {e}')



class CustomLogger:
    def debug(self, msg):
        # For compatibility with youtube-dl, both debug and info are passed into debug
        # You can distinguish them by the prefix '[debug] '
        if not msg.startswith('[debug] '):
            self.info(msg)

    def info(self, msg):
        if "[download]" in msg and "Destination:" not in msg:
            Gui_sounds.send('data_info', msg)

    def error(self, msg):
        print(msg)

    def warning(self, msg):
        print(msg)


class Gui_sounds():
    sounds = None
    length = None
    previous_songs = []
    set_local = None
    load_from_service = False
    if utils.get_platform() != "android":
        set_local_download = os.path.join(os.path.expanduser('~/Documents'), 'Youtube Music Player', 'Downloaded',
                                          'Played')
    else:
        set_local_download = os.path.normpath(os.path.join(primary_external_storage_path(), 'Download',
                                                           'Youtube Music Player', 'Downloaded', 'Played'))
    cache_dire = os.path.join(os.getcwd(), 'Downloaded')
    os.makedirs(cache_dire, exist_ok=True)
    shuffle_selected = False
    playlist = []
    song_change = False
    file_to_load = None
    song_local = None
    sound = None
    paused = False
    checking_it = None
    main_paused = True
    previous = False
    looping_bool = False
    shuffle_bool = 'False'
    shuffle_bag = []  # remaining songs in the current true-shuffle cycle
    _bag_source_len = 0  # tracks length of playlist used to build the bag

    @staticmethod
    def load(*val):
        Gui_sounds.stop()
        Gui_sounds.file_to_load = ''.join(val)
        Gui_sounds.file_to_load = os.path.normpath(Gui_sounds.file_to_load)
        Gui_sounds.sound = SoundLoader.load(Gui_sounds.file_to_load)
        if not Gui_sounds.sound:
            Gui_sounds.send("reset_gui", "reset_gui")
            return
        # Apply persistent loop preference to newly loaded sounds
        try:
            Gui_sounds.sound.loop = (str(Gui_sounds.looping_bool) == 'True')
        except Exception:
            pass
        Gui_sounds.length = Gui_sounds.sound.length or 0
        Gui_sounds.send("set_slider", str(Gui_sounds.length))
        if Gui_sounds.load_from_service:
            Gui_sounds.send("update_image", Gui_sounds.set_local)
        Gui_sounds.play()

    def download_yt(self, *val):
        setytlink, settitle, set_local, set_local_download = ''.join(val).strip("']").split("', '")
        ydl_opts = {
            "outtmpl": {"default": os.path.join(set_local_download, f"{settitle}.%(ext)s")},
            "overwrites": True,

            "format": "m4a/bestaudio",
            "ignoreerrors": True,
            "cachedir": Gui_sounds.cache_dire,
            "retries": 20,
            "restrictfilenames": True,
            "writelog": os.path.join(Gui_sounds.cache_dire, "yt_download.log")
        }
        try:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download(setytlink)
            except Exception as e:
                Gui_sounds.send("error_reset", f"download failed: {e}")
                return
            try:
                img_data = requests.get(set_local, timeout=30).content
            except Exception as e:
                img_data = None
                print(f"[service] thumbnail fetch failed: {e}")
            if img_data:
                with open(os.path.join(set_local_download, f"{settitle}.jpg"), 'wb') as handler:
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
            _start_in_foreground_if_android()
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
                if Gui_sounds.previous is True or Gui_sounds.sound.loop is True:
                    continue
                if Gui_sounds.playlist is False or Gui_sounds.playlist == 'False':
                    Gui_sounds.send("reset_gui", "reset_gui")
                    Gui_sounds.checking_it = None
                    break
                Gui_sounds.next()
            elif Gui_sounds.main_paused and Gui_sounds.sound is not None:
                if Gui_sounds.length - Gui_sounds.sound.get_pos() <= 1:
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

    def normalized_1(self, *val):
        seekingsound = float(''.join(val))
        with contextlib.suppress(AttributeError):
            try:
                if Gui_sounds.sound.state != "play":
                    Gui_sounds.sound.play()
            except Exception:
                with contextlib.suppress(Exception):
                    Gui_sounds.sound.play()
            Gui_sounds.previous = False
            Gui_sounds.paused = False
            Gui_sounds.song_local = None
            Gui_sounds.sound.seek(seekingsound)
            with contextlib.suppress(Exception):
                Gui_sounds.send("song_pos", str(int(seekingsound)))

    @staticmethod
    def seek_seconds(*val):
        """Seek to an absolute position (seconds). Accepts int/float/str/bytes/tuple payloads."""
        if not val:
            return
        v = val[0] if len(val) == 1 else val
        try:
            if isinstance(v, (int, float)):
                secs = float(v)
            elif isinstance(v, (bytes, bytearray)):
                secs = float(v.decode('utf-8', 'ignore'))
            elif isinstance(v, str):
                secs = float(v.strip())
            else:
                vv = v[0]
                if isinstance(vv, (bytes, bytearray)):
                    secs = float(vv.decode('utf-8', 'ignore'))
                else:
                    secs = float(vv)
        except Exception:
            return

        with contextlib.suppress(AttributeError):
            just_started = False
            try:
                if Gui_sounds.sound.state != "play":
                    Gui_sounds.sound.play()
                    just_started = True
            except Exception:
                with contextlib.suppress(Exception):
                    Gui_sounds.sound.play()
                    just_started = True

            # Clear pause/previous flags so the player state is consistent
            Gui_sounds.previous = False
            Gui_sounds.paused = False
            Gui_sounds.song_local = None

            # Some backends need a tiny moment after play() before seek()
            if just_started:
                import time as _time
                _time.sleep(0.05)

            # Clamp within known track length if we have it
            try:
                if Gui_sounds.length is not None:
                    secs = max(0.0, min(float(secs), float(Gui_sounds.length) - 0.1))
            except Exception:
                pass
            # Seek in seconds
            Gui_sounds.sound.seek(secs)
            # Acknowledge new position to GUI
            with contextlib.suppress(Exception):
                Gui_sounds.send("song_pos", str(int(secs)))

    def pause(self, *val):
        Gui_sounds.paused = True
        Gui_sounds.song_local = [Gui_sounds.sound.get_pos()]
        Gui_sounds.sound.stop()

    def pause_val(self, *val):
        Gui_sounds.main_paused = True
        Gui_sounds.previous = False
        Gui_sounds.looping_bool = False
        Gui_sounds.shuffle_bool = 'False'

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

    def previous_bttn(self, *val):
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
                    # True shuffle: play every track once before reshuffling
                    try:
                        current = os.path.basename(Gui_sounds.file_to_load) if Gui_sounds.file_to_load else None
                    except Exception:
                        current = None
                    # Rebuild bag if the playlist changed or bag is empty
                    need_rebuild = (not Gui_sounds.shuffle_bag) or (Gui_sounds._bag_source_len != len(songs)) \
                        or any(item not in songs for item in Gui_sounds.shuffle_bag)
                    if need_rebuild:
                        # Prefer excluding the current so we never immediately repeat
                        Gui_sounds._rebuild_shuffle_bag(exclude_current=current if len(songs) > 1 else None)
                    # If bag is still empty (e.g., single-track playlist), fall back to current
                    try:
                        next_song = Gui_sounds.shuffle_bag.pop() if Gui_sounds.shuffle_bag else (current or (songs[0] if songs else None))
                    except Exception:
                        next_song = current or (songs[0] if songs else None)
                    if next_song:
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
        try:
            current_idx = Gui_sounds.previous_songs.index(current_song) - 1
            next_song = Gui_sounds.previous_songs[current_idx]
        except ValueError:
            current_idx = -1
            next_song = None
        if current_idx <= -1 or next_song is None:
            Gui_sounds.previous_songs = []
            try:
                index_next_song = songs.index(current_song) - 1
                next_song = songs[index_next_song]
            except (ValueError, IndexError):
                next_song = songs[0] if songs else None
        if next_song:
            Gui_sounds.getting_song(next_song)

    @staticmethod
    def getting_song(message):
        Gui_sounds.stream = os.path.join(Gui_sounds.set_local_download, message)
        Gui_sounds.set_local = message
        Gui_sounds.load(Gui_sounds.stream)

    def play_list(self, *val):
        Gui_sounds.playlist = ''.join(val[1:-1]).strip("'").split("', '")

    def refresh_gui(self, *val):
        Gui_sounds.main_paused = False
        if Gui_sounds.load_from_service:
            Gui_sounds.send("update_image", Gui_sounds.set_local)
        if Gui_sounds.sound is None:
            Gui_sounds.send("are_we", "None")
        else:
            Gui_sounds.send("are_we", Gui_sounds.paused)
    @staticmethod
    def loop(*val):
        Gui_sounds.looping_bool = ''.join(val)
        try:
            if Gui_sounds.sound is not None:
                Gui_sounds.sound.loop = (Gui_sounds.looping_bool == 'True')
        except Exception:
            pass

    @staticmethod
    def shuffle(*val):
        Gui_sounds.shuffle_bool = ''.join(val)
        if Gui_sounds.shuffle_bool == 'True':
            Gui_sounds.shuffle_selected = True
            # initialize a fresh shuffle bag, avoid repeating current immediately
            try:
                current = os.path.basename(Gui_sounds.file_to_load) if Gui_sounds.file_to_load else None
            except Exception:
                current = None
            Gui_sounds._rebuild_shuffle_bag(exclude_current=current)
        else:
            Gui_sounds.shuffle_selected = False
            Gui_sounds.shuffle_bag = []
            Gui_sounds._bag_source_len = 0

    @staticmethod
    def _rebuild_shuffle_bag(exclude_current=None):
        """Build a new randomized bag of songs for true shuffle.
        exclude_current: optionally avoid picking the currently playing song first.
        """
        try:
            songs = Gui_sounds.playlist if isinstance(Gui_sounds.playlist, list) else []
        except Exception:
            songs = []
        if not songs:
            Gui_sounds.shuffle_bag = []
            Gui_sounds._bag_source_len = 0
            return
        bag = [s for s in songs if s != exclude_current] if exclude_current else list(songs)
        random.shuffle(bag)
        Gui_sounds.shuffle_bag = bag
        Gui_sounds._bag_source_len = len(songs)

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
        elif message_type == "are_we":
            CLIENT.send_message(u'/are_we', message)


if __name__ == '__main__':
    SERVER = OSCThreadServer(encoding='utf8')
    SERVER.listen('localhost', port=3000, default=True)
    SERVER.bind(u'/load', Gui_sounds.load)
    SERVER.bind(u'/play', Gui_sounds.play)
    SERVER.bind(u'/pause', Gui_sounds.pause)
    SERVER.bind(u'/stop', Gui_sounds.stop)
    SERVER.bind(u'/next', Gui_sounds.next)
    SERVER.bind(u'/previous', Gui_sounds.previous_bttn)
    SERVER.bind(u'/playlist', Gui_sounds.play_list)
    SERVER.bind(u'/update_load_fs', Gui_sounds.update_load_fs)
    SERVER.bind(u'/iamawake', Gui_sounds.refresh_gui)
    SERVER.bind(u'/loop', Gui_sounds.loop)
    SERVER.bind(u'/shuffle', Gui_sounds.shuffle)
    SERVER.bind(u'/get_update_slider', Gui_sounds.update_slider)
    SERVER.bind(u'/downloadyt', Gui_sounds.download_yt)
    SERVER.bind(u'/iampaused', Gui_sounds.pause_val)
    SERVER.bind(u'/seek_seconds', Gui_sounds.seek_seconds)
    while True:
        time.sleep(1)

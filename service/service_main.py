import contextlib
import os.path
import random
import time
from threading import Thread

import requests
import yt_dlp

import utils
from storage_access import DownloadsAccess
from utils import get_app_writable_dir

if utils.get_platform() == "android":
    os.environ["KIVY_AUDIO"] = "android"
    from jnius import autoclass, cast

    PythonService = autoclass("org.kivy.android.PythonService")
    autoclass("org.jnius.NativeInvocationHandler")
else:
    os.environ["KIVY_AUDIO"] = "gstplayer"
from kivy.core.audio import SoundLoader
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer
_WAKE = None
_last_fg_ts = 0

CLIENT = OSCClient("localhost", 3002, encoding="utf-8")


def start_foreground_heartbeat():
    if utils.get_platform() != "android":
        return

    def _beat():
        while True:
            ensure_foreground()
            time.sleep(45)

    Thread(target=_beat, daemon=True).start()


def ensure_foreground():
    """Promote PythonService to a true Android foreground service (no AndroidX)."""
    global _last_fg_ts
    if utils.get_platform() != "android":
        return
    if time.time() - _last_fg_ts < 5:
        return
    _last_fg_ts = time.time()

    def _start_foreground():
        try:
            Context = autoclass("android.content.Context")
            NotificationManager = autoclass("android.app.NotificationManager")
            NotificationChannel = autoclass("android.app.NotificationChannel")
            NotificationBuilder = autoclass("android.app.Notification$Builder")
            VERSION = autoclass("android.os.Build$VERSION")
            PythonServiceClass = autoclass("org.kivy.android.PythonService")

            svc = None
            for _ in range(20):
                try:
                    svc = PythonServiceClass.mService
                    if svc:
                        break
                except Exception:
                    pass
                time.sleep(0.25)
            if not svc:
                print("[service] ensure_foreground: mService not ready; aborting")
                return

            nm = cast(NotificationManager, svc.getSystemService(Context.NOTIFICATION_SERVICE))
            channel_id = "music_playback"

            if VERSION.SDK_INT >= 26:
                try:
                    channel = NotificationChannel(channel_id, "Playback", NotificationManager.IMPORTANCE_LOW)
                    nm.createNotificationChannel(channel)
                except Exception:
                    pass
                builder = NotificationBuilder(svc, channel_id)
            else:
                builder = NotificationBuilder(svc)

            builder.setContentTitle("Playing music")
            builder.setContentText("Your music continues in the background")
            builder.setSmallIcon(svc.getApplicationInfo().icon)
            builder.setOngoing(True)

            notification = builder.build()
            svc.startForeground(1, notification)
            print("[service] ensure_foreground: promoted to foreground")
        except Exception as e:
            print("[service] ensure_foreground failed:", e)

    Thread(target=_start_foreground, daemon=True).start()





def acquire_wakelock():
    """Keep CPU awake while audio is playing (Android only)."""
    global _WAKE
    try:
        if _WAKE:
            return
        # If we’re not running as a PythonService on Android, do nothing
        try:
            service = autoclass("org.kivy.android.PythonService").mService
        except Exception:
            return
        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        pm = service.getSystemService(Context.POWER_SERVICE)
        wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "youtubemusic:player")
        wl.setReferenceCounted(False)
        wl.acquire()
        _WAKE = wl
    except Exception as e:
        print("[service] wakelock acquire failed:", e)


def release_wakelock():
    """Release CPU lock when paused/stopped."""
    global _WAKE
    try:
        if _WAKE and _WAKE.isHeld():
            _WAKE.release()
    except Exception as e:
        print("[service] wakelock release failed:", e)
    _WAKE = None

with contextlib.suppress(Exception):
    ensure_foreground()
with contextlib.suppress(Exception):
    start_foreground_heartbeat()

class CustomLogger:
    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self.info(msg)

    def info(self, msg):
        if "[download]" in msg and "Destination:" not in msg:
            Gui_sounds.send("data_info", msg)

    def error(self, msg):
        print(msg)

    def warning(self, msg):
        print(msg)


class Gui_sounds:
    sounds = None
    length = None
    previous_songs = []
    set_local = None
    load_from_service = False
    set_local_download = get_app_writable_dir(
        "Downloaded/Played"
    )
    cache_dire = os.path.join(os.getcwd(), "Downloaded")
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
    shuffle_bool = "False"
    shuffle_bag = []
    _bag_source_len = 0

    @staticmethod
    def load(*val):
        Gui_sounds.stop()
        Gui_sounds.file_to_load = "".join(val)
        Gui_sounds.file_to_load = os.path.normpath(Gui_sounds.file_to_load)
        if not os.path.isfile(Gui_sounds.file_to_load):
            with contextlib.suppress(Exception):
                Gui_sounds.send(
                    "song_not_found", os.path.basename(Gui_sounds.file_to_load)
                )
            Gui_sounds.send("reset_gui", "reset_gui")
            return
        Gui_sounds.sound = SoundLoader.load(Gui_sounds.file_to_load)
        if not Gui_sounds.sound:
            Gui_sounds.send("reset_gui", "reset_gui")
            return

        with contextlib.suppress(Exception):
            Gui_sounds.sound.loop = str(Gui_sounds.looping_bool) == "True"
        Gui_sounds.length = Gui_sounds.sound.length or 0
        Gui_sounds.send("set_slider", str(Gui_sounds.length))
        if Gui_sounds.load_from_service:
            Gui_sounds.send("update_image", Gui_sounds.set_local)
        Gui_sounds.play()

    def download_yt(self, *val):
        setytlink, settitle, set_local, set_local_download = (
            "".join(val).strip("']").split("', '")
        )
        ydl_opts = {
            "outtmpl": {
                "default": (
                    os.path.join(
                        get_app_writable_dir(
                            "Downloaded/Played"
                        ),
                        f"{settitle}.%(ext)s",
                    )
                    if utils.get_platform() == "android"
                    else os.path.join(set_local_download, f"{settitle}.%(ext)s")
                )
            },
            "proxies": {"all": "socks5://154.38.180.176:443"},
            "overwrites": True,
            "format": "m4a/bestaudio",
            "ignoreerrors": True,
            "cachedir": Gui_sounds.cache_dire,
            "retries": 20,
            "restrictfilenames": True,
            "writelog": os.path.join(Gui_sounds.cache_dire, "yt_download.log"),
            "forceipv4": True,
            "nocheckcertificate": True,
            "logger": CustomLogger(),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([setytlink])
        except Exception as e:
            Gui_sounds.send("error_reset", f"download failed: {e}")
            return
        try:
            img_data = requests.get(set_local, timeout=30).content
        except Exception as e:
            img_data = None
            print(f"[service] thumbnail fetch failed: {e}")
        if img_data:
            if utils.get_platform() == "android":
                try:
                    da = DownloadsAccess()
                    if da and da.tree_uri:
                        da.write_bytes(
                            f"Downloaded/Played/{settitle}.jpg",
                            img_data,
                        )
                        try:
                            priv_dir = get_app_writable_dir(
                                "Downloaded/Played"
                            )
                            thumb_src = os.path.join(priv_dir, f"{settitle}.jpg")
                            if os.path.exists(thumb_src):
                                os.remove(thumb_src)
                        except Exception as e:
                            print("[service] thumb cleanup failed:", e)
                    else:
                        priv_dir = get_app_writable_dir(
                            "Downloaded/Played"
                        )
                        with open(
                            os.path.join(priv_dir, f"{settitle}.jpg"), "wb"
                        ) as handler:
                            handler.write(img_data)
                except Exception:
                    with open(
                        os.path.join(set_local_download, f"{settitle}.jpg"), "wb"
                    ) as handler:
                        handler.write(img_data)
            else:
                with open(
                    os.path.join(set_local_download, f"{settitle}.jpg"), "wb"
                ) as handler:
                    handler.write(img_data)
        with contextlib.suppress(Exception):
            if utils.get_platform() == "android":
                da = DownloadsAccess()
                if da and da.tree_uri:
                    self.set_download_directory(settitle, da)
        Gui_sounds.send("file_is_downloaded", "yep")

    def set_download_directory(self, settitle, da):
        priv_dir = get_app_writable_dir(
            "Downloaded/Played"
        )
        m4a_path = os.path.join(priv_dir, f"{settitle}.m4a")
        alt_path = None
        if not os.path.exists(m4a_path):
            for fn in os.listdir(priv_dir):
                if fn.startswith(f"{settitle}."):
                    alt_path = os.path.join(priv_dir, fn)
                    break
        file_src_path = m4a_path if os.path.exists(m4a_path) else alt_path
        if file_src_path and os.path.exists(file_src_path):
            with open(file_src_path, "rb") as fsrc:
                data = fsrc.read()
            da.write_bytes(
                f"Downloaded/Played/{os.path.basename(file_src_path)}",
                data,
            )
            try:
                if file_src_path and os.path.exists(file_src_path):
                    os.remove(file_src_path)
                    priv_dir = os.path.dirname(file_src_path)
                    base = os.path.basename(file_src_path)
                    for name in os.listdir(priv_dir):
                        if name == f"{base}.part" or (name.startswith(base) and name.endswith(".part")):
                            with contextlib.suppress(Exception):
                                os.remove(os.path.join(priv_dir, name))
            except Exception as e:
                print("[service] audio cleanup failed:", e)

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
            ensure_foreground()
            acquire_wakelock()
            Gui_sounds.sound.play()
            Gui_sounds.previous = False
            if Gui_sounds.checking_it is None:
                Gui_sounds.checking_it = Thread(
                    target=Gui_sounds.check_for_next, daemon=True
                )
                Gui_sounds.checking_it.start()

    @staticmethod
    def check_for_next():
        while True:
            if (
                Gui_sounds.sound is not None
                and (Gui_sounds.paused is False or Gui_sounds.sound.state == "play")
                and Gui_sounds.length - Gui_sounds.sound.get_pos() <= 1
            ):
                if Gui_sounds.previous is True or Gui_sounds.sound.loop is True:
                    continue
                if Gui_sounds.playlist is False or Gui_sounds.playlist == "False":
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
                ensure_foreground()
                acquire_wakelock()
                Gui_sounds.sound.play()
                Gui_sounds.sound.seek(Gui_sounds.song_local[0])
            except TypeError:
                Gui_sounds.send("normalize", "normalize please")
        else:
            ensure_foreground()
            acquire_wakelock()
            Gui_sounds.sound.play()
        Gui_sounds.previous = False
        Gui_sounds.paused = False
        Gui_sounds.song_local = None

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
                secs = float(v.decode("utf-8", "ignore"))
            elif isinstance(v, str):
                secs = float(v.strip())
            else:
                vv = v[0]
                if isinstance(vv, (bytes, bytearray)):
                    secs = float(vv.decode("utf-8", "ignore"))
                else:
                    secs = float(vv)
        except Exception:
            return

        with contextlib.suppress(AttributeError):
            just_started = False
            try:
                if Gui_sounds.sound.state != "play":
                    ensure_foreground()
                    acquire_wakelock()
                    Gui_sounds.sound.play()
                    just_started = True
            except Exception:
                with contextlib.suppress(Exception):
                    ensure_foreground()
                    acquire_wakelock()
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
            with contextlib.suppress(Exception):
                if Gui_sounds.length is not None:
                    secs = max(0.0, min(float(secs), float(Gui_sounds.length) - 0.1))
            # Seek in seconds
            Gui_sounds.sound.seek(secs)
            # Acknowledge new position to GUI
            with contextlib.suppress(Exception):
                Gui_sounds.send("song_pos", str(int(secs)))

    def pause(self, *val):
        Gui_sounds.paused = True
        Gui_sounds.song_local = [Gui_sounds.sound.get_pos()]
        release_wakelock()
        Gui_sounds.sound.stop()

    def pause_val(self, *val):
        Gui_sounds.main_paused = True
        Gui_sounds.previous = False
        Gui_sounds.looping_bool = False
        Gui_sounds.shuffle_bool = "False"

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
        if Gui_sounds.song_change is True and (
            Gui_sounds.sound is not None and Gui_sounds.sound.state == "play"
        ):
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
                        current = (
                            os.path.basename(Gui_sounds.file_to_load)
                            if Gui_sounds.file_to_load
                            else None
                        )
                    except Exception:
                        current = None
                    # Rebuild bag if the playlist changed or bag is empty
                    need_rebuild = (
                        (not Gui_sounds.shuffle_bag)
                        or (Gui_sounds._bag_source_len != len(songs))
                        or any(item not in songs for item in Gui_sounds.shuffle_bag)
                    )
                    if need_rebuild:
                        # Prefer excluding the current so we never immediately repeat
                        Gui_sounds._rebuild_shuffle_bag(
                            exclude_current=current if len(songs) > 1 else None
                        )
                    # If bag is still empty (e.g., single-track playlist), fall back to current
                    try:
                        next_song = (
                            Gui_sounds.shuffle_bag.pop()
                            if Gui_sounds.shuffle_bag
                            else (current or (songs[0] if songs else None))
                        )
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
        Gui_sounds.playlist = "".join(val[1:-1]).strip("'").split("', '")

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
        Gui_sounds.looping_bool = "".join(val)
        with contextlib.suppress(Exception):
            if Gui_sounds.sound is not None:
                Gui_sounds.sound.loop = Gui_sounds.looping_bool == "True"

    @staticmethod
    def shuffle(*val):
        Gui_sounds.shuffle_bool = "".join(val)
        if Gui_sounds.shuffle_bool == "True":
            Gui_sounds.shuffle_selected = True
            # initialize a fresh shuffle bag, avoid repeating current immediately
            try:
                current = (
                    os.path.basename(Gui_sounds.file_to_load)
                    if Gui_sounds.file_to_load
                    else None
                )
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
        bag = (
            [s for s in songs if s != exclude_current]
            if exclude_current
            else list(songs)
        )
        random.shuffle(bag)
        Gui_sounds.shuffle_bag = bag
        Gui_sounds._bag_source_len = len(songs)

    @staticmethod
    def send(message_type, message):
        message = f"{message}"
        if message_type == "normalize":
            CLIENT.send_message("/normalize", message)
        elif message_type == "song_pos":
            CLIENT.send_message("/song_pos", message)
        elif message_type == "set_slider":
            CLIENT.send_message("/set_slider", message)
        elif message_type == "update_image":
            CLIENT.send_message("/update_image", message)
        elif message_type == "reset_gui":
            CLIENT.send_message("/reset_gui", message)
        elif message_type == "file_is_downloaded":
            CLIENT.send_message("/file_is_downloaded", message)
        elif message_type == "data_info":
            CLIENT.send_message("/data_info", message)
        elif message_type == "are_we":
            CLIENT.send_message("/are_we", message)
        elif message_type == "song_not_found":
            CLIENT.send_message("/song_not_found", message)
            CLIENT.send_message("/are_we", message)


if __name__ == "__main__":
    SERVER = OSCThreadServer(encoding="utf8")
    SERVER.listen("localhost", port=3000, default=True)
    SERVER.bind("/load", Gui_sounds.load)
    SERVER.bind("/play", Gui_sounds.play)
    SERVER.bind("/pause", Gui_sounds.pause)
    SERVER.bind("/stop", Gui_sounds.stop)
    SERVER.bind("/next", Gui_sounds.next)
    SERVER.bind("/previous", Gui_sounds.previous_bttn)
    SERVER.bind("/playlist", Gui_sounds.play_list)
    SERVER.bind("/update_load_fs", Gui_sounds.update_load_fs)
    SERVER.bind("/iamawake", Gui_sounds.refresh_gui)
    SERVER.bind("/loop", Gui_sounds.loop)
    SERVER.bind("/shuffle", Gui_sounds.shuffle)
    SERVER.bind("/get_update_slider", Gui_sounds.update_slider)
    SERVER.bind("/downloadyt", Gui_sounds.download_yt)
    SERVER.bind("/iampaused", Gui_sounds.pause_val)
    SERVER.bind("/seek_seconds", Gui_sounds.seek_seconds)
    while True:
        time.sleep(1)

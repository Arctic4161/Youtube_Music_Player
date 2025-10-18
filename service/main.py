import contextlib
import json
import os
import os.path
import random
import threading
import time as _time

import requests
import yt_dlp
from yt_dlp import DownloadError

import utils
from utils import get_app_writable_dir

if utils.get_platform() == "android":
    os.environ["KIVY_AUDIO"] = "android"
    os.environ.setdefault("KIVY_WINDOW", "mock")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    from jnius import autoclass, cast
else:
    os.environ["KIVY_AUDIO"] = "gstplayer"

from kivy.core.audio import SoundLoader
from mutagen.mp4 import MP4, MP4Cover
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer

_WAKE = None
_AUDIO_FOCUS_REQ = None

CLIENT = OSCClient("localhost", 3002, encoding="utf-8")


def request_audio_focus_plain(ctx):
    """Request permanent media focus while playing (no listener)."""
    global _AUDIO_FOCUS_REQ
    AudioManager = autoclass("android.media.AudioManager")
    AudioAttributes = autoclass("android.media.AudioAttributes")
    AttrBuilder = autoclass("android.media.AudioAttributes$Builder")
    AudioFocusRequest = autoclass("android.media.AudioFocusRequest")
    AFRBuilder = autoclass("android.media.AudioFocusRequest$Builder")

    attrs = (
        AttrBuilder()
        .setUsage(AudioAttributes.USAGE_MEDIA)
        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
        .build()
    )

    afr = AFRBuilder(AudioManager.AUDIOFOCUS_GAIN).setAudioAttributes(attrs).build()

    am = cast("android.media.AudioManager", ctx.getSystemService(ctx.AUDIO_SERVICE))
    ok = False
    try:
        ok = am.requestAudioFocus(afr) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        if ok:
            _AUDIO_FOCUS_REQ = afr
    except Exception:
        ok = True
    return ok


def abandon_audio_focus(ctx):
    """Give focus back when paused/stopped."""
    global _AUDIO_FOCUS_REQ
    if not _AUDIO_FOCUS_REQ:
        return
    AudioManager = autoclass("android.media.AudioManager")
    am = cast("android.media.AudioManager", ctx.getSystemService(ctx.AUDIO_SERVICE))
    with contextlib.suppress(Exception):
        am.abandonAudioFocusRequest(_AUDIO_FOCUS_REQ)
    _AUDIO_FOCUS_REQ = None


def wait_for_service_ctx(timeout_s: float = 6.0):
    PythonService = autoclass("org.kivy.android.PythonService")
    autoclass("org.jnius.NativeInvocationHandler")
    t0 = _time.time()
    while _time.time() - t0 < timeout_s:
        if svc := PythonService.mService:
            return svc.getApplicationContext(), svc
        _time.sleep(0.05)
    return None, None


def ensure_foreground(ctx, svc):
    NotificationManager = autoclass("android.app.NotificationManager")
    NotificationChannel = autoclass("android.app.NotificationChannel")
    channel_id = "music_fg"
    nm = cast(
        "android.app.NotificationManager",
        ctx.getSystemService(ctx.NOTIFICATION_SERVICE),
    )
    ch = NotificationChannel(channel_id, "Playback", NotificationManager.IMPORTANCE_LOW)
    nm.createNotificationChannel(ch)

    try:
        NotificationBuilder = autoclass("androidx.core.app.NotificationCompat$Builder")
        b = NotificationBuilder(ctx, channel_id)
    except Exception:
        NotificationBuilder = autoclass("android.app.Notification$Builder")
        b = NotificationBuilder(ctx, channel_id)

    b.setSmallIcon(ctx.getApplicationInfo().icon)
    b.setContentTitle("Playing")
    b.setContentText("Music Player is active")
    b.setOngoing(True)
    svc.startForeground(1, b.build())


def setup_media_session(ctx):
    MediaSession = autoclass("android.media.session.MediaSession")
    PlaybackState = autoclass("android.media.session.PlaybackState")
    Builder = autoclass("android.media.session.PlaybackState$Builder")
    session = MediaSession(ctx, "MusicPlayer")
    state = (
        Builder()
        .setActions(
            PlaybackState.ACTION_PLAY
            | PlaybackState.ACTION_PAUSE
            | PlaybackState.ACTION_PLAY_PAUSE
            | PlaybackState.ACTION_STOP
        )
        .setState(PlaybackState.STATE_PLAYING, 0, 1.0)
        .build()
    )
    session.setPlaybackState(state)
    session.setActive(True)
    return session


def acquire_wakelock():
    global _WAKE
    if utils.get_platform() != "android":
        return
    PythonService = autoclass("org.kivy.android.PythonService")
    svc = PythonService.mService
    if not svc:
        return
    Context = autoclass("android.content.Context")
    PowerManager = autoclass("android.os.PowerManager")
    pm = svc.getSystemService(Context.POWER_SERVICE)
    wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "player:wakelock")
    wl.setReferenceCounted(False)
    wl.acquire()
    _WAKE = wl


def release_wakelock():
    """Release CPU lock when paused/stopped."""
    global _WAKE
    try:
        if _WAKE and _WAKE.isHeld():
            _WAKE.release()
    except Exception as e:
        print("[service] wakelock release failed:", e)
    _WAKE = None


def embed_cover_art_m4a_jpeg(
    m4a_path: str,
    jpeg_bytes: bytes,
    title: str | None = None,
    artist: str | None = None,
) -> bool:
    try:
        audio = MP4(m4a_path)
        audio["covr"] = [MP4Cover(jpeg_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
        if title:
            audio["\xa9nam"] = [title]
        if artist:
            audio["\xa9ART"] = [artist]
        audio.save()
        return True
    except Exception as e:
        print(f"[service] embed cover failed: {e}")
        return False


def _resolve_cover_for_audio(audio_path: str) -> str | None:
    """
    Given /path/to/Foo.m4a try to return a working cover path:
    - <sandbox>/Downloaded/Played/Foo.jpg
    - <old_dir>/Foo.jpg    (if still present)
    """
    name, _ = os.path.splitext(os.path.basename(audio_path))
    cand = os.path.join(get_app_writable_dir("Downloaded/Played"), f"{name}.jpg")
    if os.path.exists(cand):
        return cand

    old = os.path.join(os.path.dirname(audio_path), f"{name}.jpg")
    return old if os.path.exists(old) else None


class CustomLogger:
    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self.info(msg)

    def info(self, msg):
        if "[download]" in msg and "Destination:" not in msg:
            Gui_sounds.send("data_info", msg)

    def error(self, msg):
        with contextlib.suppress(Exception):
            Gui_sounds.send("data_info", msg)
        print(msg)

    def warning(self, msg):
        print(msg)


class Gui_sounds:
    sounds = None
    length = None
    previous_songs = []
    set_local = None
    load_from_service = False
    set_local_download = get_app_writable_dir("Downloaded/Played")
    cache_dire = get_app_writable_dir("Downloaded")
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._next_thread = None
        self._next_thread_stop = threading.Event()
        if utils.get_platform() == "android":
            self.PlaybackState = autoclass("android.media.session.PlaybackState")
            self.B = autoclass("android.media.session.PlaybackState$Builder")

    def start_next_monitor(self):
        """Start (or restart) the background loop that monitors for track end/next."""
        self.stop_next_monitor()
        self._next_thread_stop.clear()
        self._next_thread = threading.Thread(
            target=self._check_for_next_loop, name="NextMonitor", daemon=True
        )
        self._next_thread.start()

    def stop_next_monitor(self, join_timeout: float = 1.5):
        """Signal the loop to stop and join the thread if it exists."""
        with contextlib.suppress(Exception):
            self._next_thread_stop.set()
            t = self._next_thread
            self._next_thread = None
            if t and t.is_alive():
                t.join(timeout=join_timeout)

    def _check_for_next_loop(self):
        """
        Internal loop that replaces while True: ... in check_for_next().
        It checks the stop flag every tick so it can shut down cleanly.
        """
        while not self._next_thread_stop.is_set():
            try:
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
                _time.sleep(1)
            except Exception as e:
                print("Next-monitor loop error:", e)
            self._next_thread_stop.wait(0.25)

    @staticmethod
    def load(*val):
        """
        Load a track robustly:
        - Rebuild absolute path from the current app sandbox ("Downloaded/Played")
          so stale absolute paths from older installs still work.
        - Fall back to the originally provided path if needed.
        - Keep existing GUI and length setup.
        """

        GS.stop()
        Gui_sounds.file_to_load = "".join(val)
        Gui_sounds.file_to_load = os.path.normpath(Gui_sounds.file_to_load)
        base_dir = Gui_sounds.set_local_download
        candidate = os.path.join(base_dir, os.path.basename(Gui_sounds.file_to_load))

        path_to_try = (
            candidate if os.path.isfile(candidate) else Gui_sounds.file_to_load
        )

        if not os.path.isfile(path_to_try):
            name, ext = os.path.splitext(os.path.basename(path_to_try))
            if not ext:
                for e in (".m4a", ".mp3", ".aac", ".flac", ".ogg", ".wav"):
                    p2 = os.path.join(base_dir, name + e)
                    if os.path.isfile(p2):
                        path_to_try = p2
                        break

        if not os.path.isfile(path_to_try):
            with contextlib.suppress(Exception):
                Gui_sounds.send(
                    "song_not_found", os.path.basename(Gui_sounds.file_to_load)
                )
            Gui_sounds.send("reset_gui", "reset_gui")
            return

        Gui_sounds.file_to_load = path_to_try
        Gui_sounds.sound = SoundLoader.load(Gui_sounds.file_to_load)
        if not Gui_sounds.sound:
            Gui_sounds.send("reset_gui", "reset_gui")
            return

        with contextlib.suppress(Exception):
            Gui_sounds.sound.loop = str(Gui_sounds.looping_bool) == "True"
        Gui_sounds.length = Gui_sounds.sound.length or 0
        Gui_sounds.send("set_slider", str(Gui_sounds.length))
        if Gui_sounds.load_from_service:
            with contextlib.suppress(Exception):
                if cover := _resolve_cover_for_audio(Gui_sounds.file_to_load):
                    Gui_sounds.send("update_image", cover)
        GS.play()

    def download_yt(self, payload_str):
        setytlink, settitle, set_local, set_local_download = json.loads(payload_str)
        os.makedirs(set_local_download, exist_ok=True)
        safe_title = utils.safe_filename(settitle)

        audio_path = os.path.join(set_local_download, f"{safe_title}.m4a")
        cover_path = os.path.join(set_local_download, f"{safe_title}.jpg")

        ydl_opts = {
            "outtmpl": {"default": audio_path},
            "proxies": {"all": "socks5://154.38.180.176:443"},
            "overwrites": True,
            "format": "m4a/bestaudio",
            "ignoreerrors": True,
            "cachedir": Gui_sounds.cache_dire,
            "retries": 20,
            "restrictfilenames": True,
            "forceipv4": True,
            "nocheckcertificate": True,
            "logger": CustomLogger(),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([setytlink])
        except DownloadError as e:
            msg = str(e)
            if "Requested format is not available" in msg:
                Gui_sounds.send(
                    "data_info", "M4A not available right now. Tap Play to retry."
                )
            else:
                Gui_sounds.send("data_info", f"Download failed: {msg}")
            Gui_sounds.send("controls", "enable_play")
            return

        except Exception as e:
            Gui_sounds.send("data_info", f"Download error: {e}")
            Gui_sounds.send("controls", "enable_play")
            return
        if not os.path.exists(audio_path):
            Gui_sounds.send("error_reset", "downloaded file not found (.m4a)")
            return
        img_data = None
        try:
            resp = requests.get(set_local, timeout=30)
            resp.raise_for_status()
            img_data = resp.content
        except Exception as e:
            print(f"[service] thumbnail fetch failed: {e}")

        if img_data:
            try:
                with open(cover_path, "wb") as fh:
                    fh.write(img_data)
            except Exception as e:
                print(f"[service] thumbnail save failed: {e}")

            try:
                if embed_cover_art_m4a_jpeg(audio_path, img_data, title=settitle):
                    print("[service] embedded cover art into m4a")
                    if utils.get_platform() == "android":
                        with contextlib.suppress(Exception):
                            Gui_sounds.send("data_info", "Embedded album art")
            except Exception as e:
                print(f"[service] embed cover failed: {e}")

        Gui_sounds.send("file_is_downloaded", "yep")

    def update_load_fs(self, *val):
        Gui_sounds.load_from_service = False

    def play(self, *val):
        if utils.get_platform() == "android":
            acquire_wakelock()
            ctx, _ = wait_for_service_ctx()
            if ctx:
                request_audio_focus_plain(ctx)
            if "_SESSION" in globals():
                state = (
                    self.B().setState(self.PlaybackState.STATE_PLAYING, 0, 1.0).build()
                )
                _SESSION.setPlaybackState(state)
        if Gui_sounds.song_local and Gui_sounds.song_local[0] > 0:
            Gui_sounds.check_for_pause()
        else:
            Gui_sounds.paused = False
            Gui_sounds.song_local = None
            Gui_sounds.previous_songs.append(os.path.basename(Gui_sounds.file_to_load))
            Gui_sounds.sound.play()
            Gui_sounds.previous = False
            if Gui_sounds.checking_it is None:
                self.start_next_monitor()

    def update_slider(self, *val):
        with contextlib.suppress(AttributeError):
            pos = int(Gui_sounds.sound.get_pos())
            Gui_sounds.send("song_pos", str(pos))

    @staticmethod
    def check_for_pause():
        if Gui_sounds.paused:
            try:
                acquire_wakelock()
                Gui_sounds.sound.play()
                Gui_sounds.sound.seek(Gui_sounds.song_local[0])
            except TypeError:
                Gui_sounds.send("normalize", "normalize please")
        else:
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
                    acquire_wakelock()
                    Gui_sounds.sound.play()
                    just_started = True
            except Exception:
                with contextlib.suppress(Exception):
                    acquire_wakelock()
                    Gui_sounds.sound.play()
                    just_started = True

            Gui_sounds.previous = False
            Gui_sounds.paused = False
            Gui_sounds.song_local = None

            if just_started:
                _time.sleep(0.05)

            with contextlib.suppress(Exception):
                if Gui_sounds.length is not None:
                    secs = max(0.0, min(float(secs), float(Gui_sounds.length) - 0.1))

            Gui_sounds.sound.seek(secs)

            with contextlib.suppress(Exception):
                Gui_sounds.send("song_pos", str(int(secs)))

    def pause(self, *val):
        Gui_sounds.paused = True
        Gui_sounds.song_local = [Gui_sounds.sound.get_pos()]
        self.stop_next_monitor()
        Gui_sounds.sound.stop()
        if utils.get_platform() == "android":
            release_wakelock()
            ctx, _ = wait_for_service_ctx()
            if ctx:
                abandon_audio_focus(ctx)
            if "_SESSION" in globals():
                state = (
                    self.B().setState(self.PlaybackState.STATE_PAUSED, 0, 0.0).build()
                )
                _SESSION.setPlaybackState(state)

    def pause_val(self, *val):
        Gui_sounds.main_paused = True
        Gui_sounds.previous = False
        Gui_sounds.looping_bool = False
        Gui_sounds.shuffle_bool = "False"

    def stop(self, *val):
        if Gui_sounds.sound is not None and Gui_sounds.sound.state == "play":
            Gui_sounds.sound.stop()
            Gui_sounds.sound.unload()
            Gui_sounds.sound = None
        self.stop_next_monitor()
        if utils.get_platform() == "android":
            release_wakelock()
            ctx, _ = wait_for_service_ctx()
            if ctx:
                abandon_audio_focus(ctx)

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
        if Gui_sounds.sound and Gui_sounds.sound.get_pos() >= 20:
            Gui_sounds.sound.seek(0)
        else:
            Gui_sounds.check_song_change(False)

    @staticmethod
    def check_song_change(arg0):
        Gui_sounds.song_change = arg0
        if Gui_sounds.song_change is True and (
            Gui_sounds.sound is not None and Gui_sounds.sound.state == "play"
        ):
            GS.stop()
        Gui_sounds.retrieving_song()

    @staticmethod
    def retrieving_song():
        current_song = os.path.basename(Gui_sounds.file_to_load)
        songs = Gui_sounds.playlist
        with contextlib.suppress(TypeError):
            if Gui_sounds.song_change is True:
                if Gui_sounds.shuffle_selected is True:

                    try:
                        current = (
                            os.path.basename(Gui_sounds.file_to_load)
                            if Gui_sounds.file_to_load
                            else None
                        )
                    except Exception:
                        current = None

                    need_rebuild = (
                        (not Gui_sounds.shuffle_bag)
                        or (Gui_sounds._bag_source_len != len(songs))
                        or any(item not in songs for item in Gui_sounds.shuffle_bag)
                    )
                    if need_rebuild:
                        Gui_sounds._rebuild_shuffle_bag(
                            exclude_current=current if len(songs) > 1 else None
                        )

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
        elif message_type == "error_reset":
            CLIENT.send_message("/error_reset", message)


GS = Gui_sounds()

if __name__ == "__main__":
    if utils.get_platform() == "android":
        ctx, svc = wait_for_service_ctx()
        if ctx and svc:
            ensure_foreground(ctx, svc)
            _SESSION = setup_media_session(ctx)
        else:
            print("[service] no service ctx; cannot start foreground")
    SERVER = OSCThreadServer(encoding="utf8")
    SERVER.listen("localhost", port=3000, default=True)
    SERVER.bind("/load", GS.load)
    SERVER.bind("/play", GS.play)
    SERVER.bind("/pause", GS.pause)
    SERVER.bind("/stop", GS.stop)
    SERVER.bind("/next", GS.next)
    SERVER.bind("/previous", GS.previous_bttn)
    SERVER.bind("/playlist", GS.play_list)
    SERVER.bind("/update_load_fs", GS.update_load_fs)
    SERVER.bind("/iamawake", GS.refresh_gui)
    SERVER.bind("/loop", GS.loop)
    SERVER.bind("/shuffle", GS.shuffle)
    SERVER.bind("/get_update_slider", GS.update_slider)
    SERVER.bind("/downloadyt", GS.download_yt)
    SERVER.bind("/iampaused", GS.pause_val)
    SERVER.bind("/seek_seconds", GS.seek_seconds)
    while True:
        _time.sleep(1)

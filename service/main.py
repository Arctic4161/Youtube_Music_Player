import ast
import contextlib
import json
import os
import os.path
import threading
import time as _time
from functools import wraps

import requests
import yt_dlp
from yt_dlp import DownloadError

import utils
from download_config import build_yt_dlp_options
from media_identity import display_title_from_stem, media_stem, stable_media_id
from playback_logic import (
    NavigationAction,
    PlaybackQueue,
    PlaybackSnapshot,
    PlaybackStatus,
    android_playback_state,
    clamp_seek,
    has_reached_end,
    pause_sound,
    resume_sound,
    safe_sound_position,
    stop_and_unload,
)
from utils import get_app_writable_dir

if utils.get_platform() == "android":
    os.environ["KIVY_AUDIO"] = "android"
    os.environ.setdefault("KIVY_WINDOW", "mock")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    from jnius import PythonJavaClass, autoclass, cast, java_method
else:
    os.environ["KIVY_AUDIO"] = "gstplayer"

from kivy.core.audio import SoundLoader
from mutagen.mp4 import MP4, MP4Cover
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer

_WAKE = None
_AUDIO_FOCUS_REQ = None
_AUDIO_FOCUS_LISTENER = None
_SESSION = None
_SESSION_CALLBACK = None
_NOTIFICATION_MANAGER = None
_NOTIFICATION_BUILDER = None
_SERVICE_CONTEXT = None
_SERVICE_INSTANCE = None
_CONTROL_RECEIVER = None
_FOREGROUND_ACTIVE = False
_IDLE_DEMOTION_TIMER = None
_FOREGROUND_IDLE_DELAY_S = 5.0
_FOREGROUND_PAUSED_DELAY_S = 10 * 60.0

CLIENT = OSCClient("localhost", 3002, encoding="utf-8")


def audio_focus_action(focus_change, AudioManager):
    """Translate Android focus constants into service playback actions."""

    if focus_change == AudioManager.AUDIOFOCUS_GAIN:
        return "gain"
    if focus_change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK:
        return "duck"
    if focus_change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT:
        return "pause"
    if focus_change == AudioManager.AUDIOFOCUS_LOSS:
        return "stop"
    return "ignore"


if utils.get_platform() == "android":

    class AudioFocusChangeListener(PythonJavaClass):
        __javainterfaces__ = [
            "android/media/AudioManager$OnAudioFocusChangeListener"
        ]
        __javacontext__ = "app"

        def __init__(self, controller):
            super().__init__()
            self.controller = controller
            self.focus_granted = False
            self.resume_on_gain = False
            self.ducked_volume = None

        def _restore_volume(self):
            if self.ducked_volume is None:
                return
            sound = Gui_sounds.sound
            if sound is not None:
                with contextlib.suppress(Exception):
                    sound.volume = self.ducked_volume
            self.ducked_volume = None

        @java_method("(I)V")
        def onAudioFocusChange(self, focus_change):
            AudioManager = autoclass("android.media.AudioManager")
            action = audio_focus_action(focus_change, AudioManager)
            if action == "duck":
                sound = Gui_sounds.sound
                if sound is not None and self.ducked_volume is None:
                    with contextlib.suppress(Exception):
                        self.ducked_volume = float(sound.volume)
                        sound.volume = min(self.ducked_volume, 0.2)
                return
            self._restore_volume()
            if action == "pause":
                self.focus_granted = False
                self.resume_on_gain = self.controller.status is PlaybackStatus.PLAYING
                self.controller.pause(abandon_focus=False)
            elif action == "stop":
                self.focus_granted = False
                self.resume_on_gain = False
                self.controller.pause()
            elif action == "gain":
                self.focus_granted = True
                if self.resume_on_gain:
                    self.resume_on_gain = False
                    self.controller.play()


def audio_focus_is_granted(request, listener):
    """Return whether the cached Android request still owns playback focus."""

    return request is not None and bool(
        listener is not None and getattr(listener, "focus_granted", False)
    )


def playback_locked(method):
    """Serialize service commands with end-of-track transitions."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)

    return wrapped


def request_audio_focus(ctx, controller):
    """Request permanent media focus and listen for interruptions."""
    global _AUDIO_FOCUS_LISTENER, _AUDIO_FOCUS_REQ
    if _AUDIO_FOCUS_REQ is not None:
        return audio_focus_is_granted(_AUDIO_FOCUS_REQ, _AUDIO_FOCUS_LISTENER)
    AudioManager = autoclass("android.media.AudioManager")
    AudioAttributes = autoclass("android.media.AudioAttributes")
    AttrBuilder = autoclass("android.media.AudioAttributes$Builder")
    AFRBuilder = autoclass("android.media.AudioFocusRequest$Builder")
    Handler = autoclass("android.os.Handler")
    Looper = autoclass("android.os.Looper")

    attrs = (
        AttrBuilder()
        .setUsage(AudioAttributes.USAGE_MEDIA)
        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
        .build()
    )

    listener = AudioFocusChangeListener(controller)
    afr = (
        AFRBuilder(AudioManager.AUDIOFOCUS_GAIN)
        .setAudioAttributes(attrs)
        .setOnAudioFocusChangeListener(listener, Handler(Looper.getMainLooper()))
        .build()
    )

    am = cast("android.media.AudioManager", ctx.getSystemService(ctx.AUDIO_SERVICE))
    ok = False
    try:
        ok = am.requestAudioFocus(afr) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        if ok:
            listener.focus_granted = True
            _AUDIO_FOCUS_REQ = afr
            _AUDIO_FOCUS_LISTENER = listener
    except Exception as exc:
        print(f"[service] audio focus request failed: {exc}")
    return ok


def abandon_audio_focus(ctx):
    """Give focus back when paused/stopped."""
    global _AUDIO_FOCUS_LISTENER, _AUDIO_FOCUS_REQ
    listener = _AUDIO_FOCUS_LISTENER
    if listener is not None:
        listener.focus_granted = False
        with contextlib.suppress(Exception):
            listener._restore_volume()
    if not _AUDIO_FOCUS_REQ:
        _AUDIO_FOCUS_LISTENER = None
        return
    am = cast("android.media.AudioManager", ctx.getSystemService(ctx.AUDIO_SERVICE))
    with contextlib.suppress(Exception):
        am.abandonAudioFocusRequest(_AUDIO_FOCUS_REQ)
    _AUDIO_FOCUS_REQ = None
    _AUDIO_FOCUS_LISTENER = None


def wait_for_service_ctx(timeout_s: float = 6.0):
    PythonService = autoclass("org.kivy.android.PythonService")
    autoclass("org.jnius.NativeInvocationHandler")
    t0 = _time.time()
    while _time.time() - t0 < timeout_s:
        if svc := PythonService.mService:
            return svc.getApplicationContext(), svc
        _time.sleep(0.05)
    return None, None


def register_notification_control_receiver(ctx) -> bool:
    """Register notification actions while the playback service is alive."""

    global _CONTROL_RECEIVER
    if _CONTROL_RECEIVER is not None:
        return True
    try:
        Context = autoclass("android.content.Context")
        BuildVersion = autoclass("android.os.Build$VERSION")
        IntentFilter = autoclass("android.content.IntentFilter")
        ControlReceiver = autoclass(
            "com.youtubemusicplayer.bridge.MusicControlReceiver"
        )
        receiver = ControlReceiver()
        intent_filter = IntentFilter()
        for action in (
            ControlReceiver.ACTION_PREVIOUS,
            ControlReceiver.ACTION_TOGGLE,
            ControlReceiver.ACTION_NEXT,
        ):
            intent_filter.addAction(action)
        if BuildVersion.SDK_INT >= 33:
            ctx.registerReceiver(
                receiver,
                intent_filter,
                Context.RECEIVER_NOT_EXPORTED,
            )
        else:
            ctx.registerReceiver(receiver, intent_filter)
    except Exception as exc:
        print(f"[service] notification control registration failed: {exc}")
        return False
    _CONTROL_RECEIVER = receiver
    return True


def ensure_foreground(ctx, svc, session=None):
    global _FOREGROUND_ACTIVE
    global _NOTIFICATION_BUILDER, _NOTIFICATION_MANAGER
    global _SERVICE_CONTEXT, _SERVICE_INSTANCE
    cancel_idle_foreground_demotion()
    Intent = autoclass("android.content.Intent")
    Notification = autoclass("android.app.Notification")
    NotificationManager = autoclass("android.app.NotificationManager")
    NotificationChannel = autoclass("android.app.NotificationChannel")
    NotificationBuilder = autoclass("android.app.Notification$Builder")
    NotificationActionBuilder = autoclass("android.app.Notification$Action$Builder")
    Icon = autoclass("android.graphics.drawable.Icon")
    PendingIntent = autoclass("android.app.PendingIntent")
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    JavaString = autoclass("java.lang.String")
    ControlReceiver = autoclass(
        "com.youtubemusicplayer.bridge.MusicControlReceiver"
    )
    AndroidDrawable = autoclass("android.R$drawable")
    channel_id = "music_fg"
    nm = cast(
        "android.app.NotificationManager",
        ctx.getSystemService(ctx.NOTIFICATION_SERVICE),
    )
    ch = NotificationChannel(channel_id, "Playback", NotificationManager.IMPORTANCE_LOW)
    nm.createNotificationChannel(ch)
    register_notification_control_receiver(ctx)

    b = NotificationBuilder(ctx, channel_id)

    b.setSmallIcon(ctx.getApplicationInfo().icon)
    b.setContentTitle("Music Player")
    b.setContentText("Ready")
    b.setCategory(Notification.CATEGORY_TRANSPORT)
    b.setVisibility(Notification.VISIBILITY_PUBLIC)
    b.setOngoing(True)
    b.setOnlyAlertOnce(True)
    launch_intent = Intent(ctx, PythonActivity)
    launch_intent.setFlags(
        Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP
    )
    launch_flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
    b.setContentIntent(PendingIntent.getActivity(ctx, 0, launch_intent, launch_flags))
    for request_code, icon, title, action in (
        (
            1,
            AndroidDrawable.ic_media_previous,
            "Previous",
            ControlReceiver.ACTION_PREVIOUS,
        ),
        (
            2,
            AndroidDrawable.ic_media_play,
            "Play or pause",
            ControlReceiver.ACTION_TOGGLE,
        ),
        (
            3,
            AndroidDrawable.ic_media_next,
            "Next",
            ControlReceiver.ACTION_NEXT,
        ),
    ):
        # Pyjnius 1.7 resolves this overload correctly only with the precise
        # Icon/CharSequence signature; it does not coerce the older int icon
        # constructor used by Python-for-Android's former Pyjnius release.
        action_icon = Icon.createWithResource(ctx, int(icon))
        control_intent = Intent(action)
        control_intent.setPackage(ctx.getPackageName())
        control_pending_intent = PendingIntent.getBroadcast(
            ctx,
            request_code,
            control_intent,
            launch_flags,
        )
        b.addAction(
            NotificationActionBuilder(
                action_icon,
                JavaString(title),
                control_pending_intent,
            ).build()
        )
    if session is not None:
        try:
            NotificationStyle = autoclass(
                "com.youtubemusicplayer.bridge.MusicNotificationStyle"
            )
            NotificationStyle.applyMediaStyle(b, session)
        except Exception as exc:
            # The actions above remain usable if a future platform rejects the
            # optional visual enhancement.
            print("[notification] MediaStyle unavailable:", exc)
    _NOTIFICATION_MANAGER = nm
    _NOTIFICATION_BUILDER = b
    _SERVICE_CONTEXT = ctx
    _SERVICE_INSTANCE = svc
    svc.startForeground(1, b.build())
    _FOREGROUND_ACTIVE = True


def ensure_foreground_for_audio_focus(ctx, svc) -> bool:
    """Promote a demoted Android playback service before requesting focus."""

    if ctx is None or svc is None:
        return False
    if _FOREGROUND_ACTIVE:
        return True
    try:
        ensure_foreground(ctx, svc, _SESSION)
    except Exception as exc:
        print(f"[service] foreground restore failed: {exc}")
    return _FOREGROUND_ACTIVE


def cancel_idle_foreground_demotion():
    global _IDLE_DEMOTION_TIMER
    timer = _IDLE_DEMOTION_TIMER
    _IDLE_DEMOTION_TIMER = None
    if timer is not None:
        timer.cancel()


def foreground_demotion_delay(status: PlaybackStatus):
    """Return the foreground grace period for non-playing states."""

    if status is PlaybackStatus.IDLE:
        return _FOREGROUND_IDLE_DELAY_S
    if status is PlaybackStatus.PAUSED:
        return _FOREGROUND_PAUSED_DELAY_S
    return None


def demote_foreground_if_idle():
    """Remove foreground state after an idle or paused grace period."""

    global _FOREGROUND_ACTIVE, _IDLE_DEMOTION_TIMER
    _IDLE_DEMOTION_TIMER = None
    if not _FOREGROUND_ACTIVE or GS.status not in (
        PlaybackStatus.IDLE,
        PlaybackStatus.PAUSED,
    ):
        return False
    if _SERVICE_INSTANCE is None:
        return False
    try:
        _SERVICE_INSTANCE.stopForeground(True)
        if _NOTIFICATION_MANAGER is not None:
            _NOTIFICATION_MANAGER.cancel(1)
    except Exception as exc:
        print(f"[service] foreground demotion failed: {exc}")
        return False
    _FOREGROUND_ACTIVE = False
    return True


def schedule_idle_foreground_demotion(delay_s: float | None = None):
    global _IDLE_DEMOTION_TIMER
    cancel_idle_foreground_demotion()
    if delay_s is None:
        delay_s = _FOREGROUND_IDLE_DELAY_S
    timer = threading.Timer(
        delay_s,
        demote_foreground_if_idle,
    )
    timer.daemon = True
    _IDLE_DEMOTION_TIMER = timer
    timer.start()


def _build_media_session_state(
    PlaybackState,
    Builder,
    status,
    position_seconds=0.0,
    *,
    can_skip: bool = False,
):
    state_name, speed = android_playback_state(status)
    actions = (
        PlaybackState.ACTION_PLAY
        | PlaybackState.ACTION_PAUSE
        | PlaybackState.ACTION_PLAY_PAUSE
        | PlaybackState.ACTION_STOP
        | PlaybackState.ACTION_SEEK_TO
    )
    if can_skip:
        actions |= (
            PlaybackState.ACTION_SKIP_TO_NEXT
            | PlaybackState.ACTION_SKIP_TO_PREVIOUS
        )
    position_ms = max(0, int(float(position_seconds or 0.0) * 1000.0))
    return (
        Builder()
        .setActions(actions)
        .setState(getattr(PlaybackState, state_name), position_ms, speed)
        .build()
    )


def update_foreground_notification(status: PlaybackStatus, track_name: str | None):
    if _NOTIFICATION_BUILDER is None or _NOTIFICATION_MANAGER is None:
        return
    title = {
        PlaybackStatus.IDLE: "Music Player",
        PlaybackStatus.LOADING: "Preparing audio",
        PlaybackStatus.PLAYING: "Playing",
        PlaybackStatus.PAUSED: "Paused",
    }[status]
    stem = os.path.splitext(os.path.basename(track_name or ""))[0]
    detail = display_title_from_stem(stem) or (
        "Ready" if status is PlaybackStatus.IDLE else "Music Player"
    )
    try:
        _NOTIFICATION_BUILDER.setContentTitle(title)
        _NOTIFICATION_BUILDER.setContentText(detail)
        _NOTIFICATION_BUILDER.setOngoing(True)
        _NOTIFICATION_MANAGER.notify(1, _NOTIFICATION_BUILDER.build())
    except Exception as exc:
        print(f"[service] notification update failed: {exc}")


def update_media_session_metadata(session, track_name, duration_seconds):
    if session is None:
        return
    MediaMetadata = autoclass("android.media.MediaMetadata")
    MetadataBuilder = autoclass("android.media.MediaMetadata$Builder")
    stem = os.path.splitext(os.path.basename(track_name or ""))[0]
    title = display_title_from_stem(stem) or "Music Player"
    builder = MetadataBuilder().putString(MediaMetadata.METADATA_KEY_TITLE, title)
    builder.putString(MediaMetadata.METADATA_KEY_DISPLAY_TITLE, title)
    if duration_seconds:
        builder.putLong(
            MediaMetadata.METADATA_KEY_DURATION,
            max(0, int(float(duration_seconds) * 1000.0)),
        )
    cover_path = os.path.splitext(track_name or "")[0] + ".jpg"
    if os.path.isfile(cover_path):
        with contextlib.suppress(Exception):
            BitmapFactory = autoclass("android.graphics.BitmapFactory")
            bitmap = BitmapFactory.decodeFile(cover_path)
            if bitmap is not None:
                builder.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, bitmap)
    session.setMetadata(builder.build())


def setup_media_session(ctx):
    global _SESSION_CALLBACK
    MediaSession = autoclass("android.media.session.MediaSession")
    MediaCallback = autoclass(
        "com.youtubemusicplayer.bridge.MusicMediaSessionCallback"
    )
    PlaybackState = autoclass("android.media.session.PlaybackState")
    Builder = autoclass("android.media.session.PlaybackState$Builder")
    Handler = autoclass("android.os.Handler")
    Looper = autoclass("android.os.Looper")
    session = MediaSession(ctx, "MusicPlayer")
    state = _build_media_session_state(
        PlaybackState,
        Builder,
        PlaybackStatus.IDLE,
    )
    session.setPlaybackState(state)
    _SESSION_CALLBACK = MediaCallback(3000)
    session.setCallback(_SESSION_CALLBACK, Handler(Looper.getMainLooper()))
    session.setFlags(
        MediaSession.FLAG_HANDLES_MEDIA_BUTTONS
        | MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS
    )
    session.setActive(True)
    return session


def acquire_wakelock():
    global _WAKE
    if utils.get_platform() != "android":
        return
    if _WAKE is not None:
        with contextlib.suppress(Exception):
            if _WAKE.isHeld():
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
    jpeg_bytes: bytes | None,
    title: str | None = None,
    artist: str | None = None,
    video_id: str | None = None,
) -> bool:
    try:
        audio = MP4(m4a_path)
        if jpeg_bytes:
            audio["covr"] = [MP4Cover(jpeg_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
        if title:
            audio["\xa9nam"] = [title]
        if artist:
            audio["\xa9ART"] = [artist]
        if video_id:
            audio["----:com.apple.iTunes:YouTubeVideoID"] = [
                video_id.encode("utf-8")
            ]
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
    def __init__(self, request_id: str, send_callback):
        self.request_id = request_id
        self._send_callback = send_callback

    def _progress(self, message: str):
        payload = json.dumps({"request_id": self.request_id, "message": message})
        self._send_callback("download_progress", payload)

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self.info(msg)

    def info(self, msg):
        if "[download]" in msg and "Destination:" not in msg:
            self._progress(msg)

    def error(self, msg):
        with contextlib.suppress(Exception):
            self._progress(msg)
        print(msg)

    def warning(self, msg):
        print(msg)


class Gui_sounds:
    sounds = None
    length = None
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
    main_paused = False
    previous = False
    loop_enabled = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue: PlaybackQueue[str] = PlaybackQueue()
        self.local_navigation_enabled = False
        self.status = PlaybackStatus.IDLE
        self._state_lock = threading.RLock()
        self._next_thread: threading.Thread | None = None
        self._next_thread_stop: threading.Event | None = None
        self._download_lock = threading.Lock()
        self._download_generation = 0
        self._download_thread: threading.Thread | None = None
        self._download_cancel: threading.Event | None = None
        self._download_request_id: str | None = None
        if utils.get_platform() == "android":
            self.PlaybackState = autoclass("android.media.session.PlaybackState")
            self.B = autoclass("android.media.session.PlaybackState$Builder")

    def _set_status(self, status: PlaybackStatus, *, notify: bool = True):
        self.status = status
        self._sync_android_system_state(status)
        if notify:
            with contextlib.suppress(Exception):
                self.send("are_we", status.value)

    def _sync_android_system_state(self, status: PlaybackStatus):
        if utils.get_platform() != "android":
            return
        position = safe_sound_position(Gui_sounds.sound)
        if status is PlaybackStatus.PAUSED and Gui_sounds.song_local:
            with contextlib.suppress(TypeError, ValueError, IndexError):
                position = float(Gui_sounds.song_local[0])
        demotion_delay = foreground_demotion_delay(status)
        if demotion_delay is not None:
            schedule_idle_foreground_demotion(demotion_delay)
        else:
            cancel_idle_foreground_demotion()
            if (
                not _FOREGROUND_ACTIVE
                and _SERVICE_CONTEXT is not None
                and _SERVICE_INSTANCE is not None
            ):
                ensure_foreground(_SERVICE_CONTEXT, _SERVICE_INSTANCE, _SESSION)
        if _SESSION is not None:
            try:
                update_media_session_metadata(
                    _SESSION,
                    Gui_sounds.file_to_load,
                    Gui_sounds.length,
                )
            except Exception as exc:
                print(f"[service] media metadata update failed: {exc}")
            try:
                state = _build_media_session_state(
                    self.PlaybackState,
                    self.B,
                    status,
                    position,
                    can_skip=(
                        self.local_navigation_enabled and len(self.queue.items) >= 2
                    ),
                )
                _SESSION.setPlaybackState(state)
            except Exception as exc:
                print(f"[service] playback state update failed: {exc}")
        update_foreground_notification(status, Gui_sounds.file_to_load)

    def start_next_monitor(self):
        """Start (or restart) the background loop that monitors for track end/next."""
        self.stop_next_monitor()
        stop_event = threading.Event()
        self._next_thread_stop = stop_event
        self._next_thread = threading.Thread(
            target=self._check_for_next_loop,
            args=(stop_event,),
            name="NextMonitor",
            daemon=True,
        )
        self._next_thread.start()

    def stop_next_monitor(self, join_timeout: float = 1.5):
        """Signal the loop to stop and join the thread if it exists."""
        stop_event = self._next_thread_stop
        self._next_thread_stop = None
        if stop_event is not None:
            stop_event.set()
        t = self._next_thread
        self._next_thread = None
        if t and t is not threading.current_thread() and t.is_alive():
            with contextlib.suppress(Exception):
                t.join(timeout=join_timeout)

    def _check_for_next_loop(self, stop_event: threading.Event):
        """
        End-of-track monitor (service-side, GUI-independent).
        """
        tick = 0.25
        end_fired = False
        previous_state = ""
        max_position = 0.0

        while not stop_event.is_set():
            try:
                snd = getattr(Gui_sounds, "sound", None)
                length = float(getattr(Gui_sounds, "length", 0.0) or 0.0)
                pos = safe_sound_position(snd)
                max_position = max(max_position, pos)
                paused = bool(getattr(Gui_sounds, "paused", False))
                state = getattr(snd, "state", "") if snd else ""
                endish = has_reached_end(
                    position=pos,
                    duration=length,
                    backend_state=state,
                    previous_backend_state=previous_state,
                    max_position=max_position,
                    paused=paused,
                    looping=bool(getattr(snd, "loop", False)) if snd else False,
                )
                if endish and not end_fired:
                    end_fired = True
                    self._advance_after_end(stop_event)
                    return
                previous_state = state
                stop_event.wait(tick)
            except Exception as e:
                print("Next-monitor loop error:", e)
                stop_event.wait(tick)

    @playback_locked
    def _advance_after_end(self, source_event: threading.Event | None = None):
        if source_event is not None and self._next_thread_stop is not source_event:
            return
        if self.status is not PlaybackStatus.PLAYING:
            return
        target = self.queue.next()
        if target is None:
            self.stop()
            self.send("reset_gui", "reset_gui")
            return
        self.getting_song(target, record_history=False)

    def load(self, *val):
        """
        Load a track robustly:
        - Rebuild absolute path from the current app sandbox ("Downloaded/Played")
          so stale absolute paths from older installs still work.
        - Fall back to the originally provided path if needed.
        - Keep existing GUI and length setup.
        """

        requested_path = "".join(val)
        self._load_path(requested_path, record_history=True)

    @playback_locked
    def _load_path(self, requested_path: str, *, record_history: bool):
        self.stop(notify=False)
        Gui_sounds.file_to_load = os.path.normpath(requested_path)
        self._set_status(PlaybackStatus.LOADING)
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
                self.send(
                    "song_not_found", os.path.basename(Gui_sounds.file_to_load)
                )
            self._set_status(PlaybackStatus.IDLE)
            self.send("reset_gui", "reset_gui")
            return
        Gui_sounds.file_to_load = path_to_try
        selected = os.path.basename(path_to_try)
        self.queue.select(selected, record_history=record_history)
        Gui_sounds.sound = SoundLoader.load(Gui_sounds.file_to_load)
        if not Gui_sounds.sound:
            self._set_status(PlaybackStatus.IDLE)
            self.send("data_info", "Unable to load the selected audio file.")
            self.send("reset_gui", "reset_gui")
            return
        self.local_navigation_enabled = True
        self.set_loop(self.loop_enabled)
        Gui_sounds.length = Gui_sounds.sound.length or 0
        self.send("set_slider", str(Gui_sounds.length))
        if Gui_sounds.load_from_service:
            with contextlib.suppress(Exception):
                if cover := _resolve_cover_for_audio(Gui_sounds.file_to_load):
                    self.send("update_image", cover)
        self.play()

    def download_yt(self, payload_str):
        request_id = ""
        try:
            raw = payload_str[0] if isinstance(payload_str, (list, tuple)) else payload_str
            request = json.loads(raw)
            if isinstance(request, dict):
                request_id = str(request.get("request_id") or "")
                setytlink = str(request["url"])
                settitle = str(request["title"])
                video_id = stable_media_id(setytlink, request.get("video_id"))
                set_local = str(request.get("thumbnail_url") or "")
                set_local_download = str(request["download_dir"])
            else:
                setytlink, settitle, set_local, set_local_download = request
                video_id = stable_media_id(setytlink)
            if not request_id or not setytlink or not settitle:
                raise ValueError("request_id, url, and title are required")
        except Exception as exc:
            self._send_download_result(
                request_id,
                status="error",
                message=f"Invalid download request: {exc}",
            )
            return

        with self._download_lock:
            active = self._download_thread
            if active is not None and active.is_alive():
                self._send_download_result(
                    request_id,
                    status="error",
                    message="Another download is already in progress.",
                    set_idle=False,
                )
                return
            self._download_generation += 1
            generation = self._download_generation
            cancel_event = threading.Event()
            worker = threading.Thread(
                target=self._download_worker,
                args=(
                    generation,
                    cancel_event,
                    request_id,
                    setytlink,
                    settitle,
                    video_id,
                    set_local,
                    set_local_download,
                ),
                name=f"Download-{generation}",
                daemon=True,
            )
            self._download_cancel = cancel_event
            self._download_request_id = request_id
            self._download_thread = worker
        self._set_status(PlaybackStatus.LOADING)
        try:
            worker.start()
        except Exception as exc:
            self._release_download_job(generation)
            self._send_download_result(
                request_id,
                status="error",
                message=f"Unable to start download: {exc}",
            )

    def _download_is_current(self, generation: int, request_id: str) -> bool:
        with self._download_lock:
            return (
                generation == self._download_generation
                and request_id == self._download_request_id
            )

    def _release_download_job(self, generation: int) -> None:
        with self._download_lock:
            if generation != self._download_generation:
                return
            self._download_thread = None
            self._download_cancel = None
            self._download_request_id = None

    def _report_download_job(
        self,
        generation: int,
        request_id: str,
        *,
        status: str,
        message: str,
        audio_path: str | None = None,
    ) -> None:
        if not self._download_is_current(generation, request_id):
            return
        self._send_download_result(
            request_id,
            status=status,
            message=message,
            audio_path=audio_path,
            set_idle=self.status is PlaybackStatus.LOADING,
        )

    def _download_worker(
        self,
        generation: int,
        cancel_event: threading.Event,
        request_id: str,
        setytlink: str,
        settitle: str,
        video_id: str,
        thumbnail_url: str,
        set_local_download: str,
    ) -> None:
        stem = media_stem(settitle, video_id)
        audio_path = os.path.join(set_local_download, f"{stem}.m4a")
        cover_path = os.path.join(set_local_download, f"{stem}.jpg")

        def progress_hook(_progress: dict) -> None:
            if cancel_event.is_set():
                raise DownloadError("Download cancelled.")

        try:
            os.makedirs(set_local_download, exist_ok=True)
            ydl_opts = build_yt_dlp_options(
                audio_path=audio_path,
                page_url=setytlink,
                cache_dir=Gui_sounds.cache_dire,
                logger=CustomLogger(request_id, self.send),
                proxy_url=os.environ.get("YMP_YTDLP_PROXY", "").strip() or None,
                progress_hook=progress_hook,
            )
            if cancel_event.is_set():
                raise DownloadError("Download cancelled.")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([setytlink])
            if cancel_event.is_set():
                raise DownloadError("Download cancelled.")
            if not os.path.exists(audio_path):
                self._report_download_job(
                    generation,
                    request_id,
                    status="error",
                    message="Downloaded audio file was not created.",
                )
                return

            img_data = None
            if thumbnail_url:
                try:
                    resp = requests.get(thumbnail_url, timeout=30)
                    resp.raise_for_status()
                    img_data = resp.content
                except Exception as exc:
                    print(f"[service] thumbnail fetch failed: {exc}")
            if cancel_event.is_set():
                raise DownloadError("Download cancelled.")

            if img_data:
                try:
                    with open(cover_path, "wb") as fh:
                        fh.write(img_data)
                except Exception as exc:
                    print(f"[service] thumbnail save failed: {exc}")
            if embed_cover_art_m4a_jpeg(
                audio_path,
                img_data,
                title=settitle,
                video_id=video_id,
            ):
                print("[service] embedded media metadata into m4a")

            self._report_download_job(
                generation,
                request_id,
                status="success",
                message="Download complete.",
                audio_path=audio_path,
            )
        except DownloadError as exc:
            msg = str(exc)
            if cancel_event.is_set():
                status = "cancelled"
                message = "Download cancelled."
            elif "Requested format is not available" in msg:
                status = "error"
                message = "M4A not available right now. Tap Play to retry."
            else:
                status = "error"
                message = f"Download failed: {msg}"
            self._report_download_job(
                generation,
                request_id,
                status=status,
                message=message,
            )
        except Exception as exc:
            self._report_download_job(
                generation,
                request_id,
                status="error",
                message=f"Download error: {exc}",
            )
        finally:
            self._release_download_job(generation)

    def cancel_download(self, *values) -> None:
        raw = values[0] if len(values) == 1 else values
        if isinstance(raw, (list, tuple)) and raw:
            raw = raw[0]
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "ignore")
        try:
            payload = json.loads(str(raw))
            request_id = str(payload.get("request_id") or "")
        except (AttributeError, TypeError, ValueError):
            return
        with self._download_lock:
            if request_id != self._download_request_id:
                return
            if self._download_cancel is not None:
                self._download_cancel.set()

    def _send_download_result(
        self,
        request_id: str,
        *,
        status: str,
        message: str,
        audio_path: str | None = None,
        set_idle: bool = True,
    ):
        if set_idle:
            self._set_status(PlaybackStatus.IDLE)
        if request_id:
            payload = {
                "request_id": request_id,
                "status": status,
                "message": message,
            }
            if audio_path:
                payload["audio_path"] = audio_path
            self.send("download_result", json.dumps(payload))
            return
        if status == "success":
            self.send("file_is_downloaded", "yep")
        else:
            self.send("data_info", message)
            self.send("file_is_downloaded", "nope")

    def update_load_fs(self, *val):
        Gui_sounds.load_from_service = False

    @playback_locked
    def play(self, *val):
        sound = Gui_sounds.sound
        if sound is None:
            self.stop_next_monitor()
            self._set_status(PlaybackStatus.IDLE)
            return

        paused_position = None
        if Gui_sounds.song_local:
            with contextlib.suppress(TypeError, ValueError, IndexError):
                paused_position = float(Gui_sounds.song_local[0])

        if utils.get_platform() == "android":
            ctx, svc = wait_for_service_ctx()
            if (
                not ensure_foreground_for_audio_focus(ctx, svc)
                or not request_audio_focus(ctx, self)
            ):
                Gui_sounds.paused = True
                Gui_sounds.song_local = [
                    paused_position
                    if paused_position is not None
                    else safe_sound_position(sound)
                ]
                self._set_status(PlaybackStatus.PAUSED)
                self.send(
                    "data_info",
                    "Audio is in use by another app. Playback remains paused.",
                )
                release_wakelock()
                return
            acquire_wakelock()
        if not resume_sound(sound, paused_position):
            self.stop()
            self.send("data_info", "Unable to start audio playback.")
            self.send("reset_gui", "reset_gui")
            return
        Gui_sounds.paused = False
        Gui_sounds.song_local = None
        Gui_sounds.previous = False
        self._set_status(PlaybackStatus.PLAYING)
        self.start_next_monitor()

    def update_slider(self, *val):
        if Gui_sounds.sound is not None:
            self.send("song_pos", str(int(safe_sound_position(Gui_sounds.sound))))

    @playback_locked
    def seek_seconds(self, *val):
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

        sound = Gui_sounds.sound
        if sound is None:
            self._set_status(PlaybackStatus.IDLE)
            return

        secs = clamp_seek(secs, Gui_sounds.length)
        was_playing = self.status is PlaybackStatus.PLAYING
        if not was_playing:
            self.stop_next_monitor()
            with contextlib.suppress(Exception):
                sound.seek(secs)
            Gui_sounds.previous = False
            Gui_sounds.paused = self.status is PlaybackStatus.PAUSED
            Gui_sounds.song_local = [secs]
            self.send("song_pos", str(int(secs)))
            return

        just_started = getattr(sound, "state", "") != "play"
        if just_started and utils.get_platform() == "android":
            ctx, svc = wait_for_service_ctx()
            if (
                not ensure_foreground_for_audio_focus(ctx, svc)
                or not request_audio_focus(ctx, self)
            ):
                Gui_sounds.paused = True
                Gui_sounds.song_local = [secs]
                self._set_status(PlaybackStatus.PAUSED)
                self.send(
                    "data_info",
                    "Audio is in use by another app. Playback remains paused.",
                )
                release_wakelock()
                return
            acquire_wakelock()
        if just_started and not resume_sound(sound):
            self.stop()
            self.send("data_info", "Unable to resume audio playback.")
            return
        if just_started:
            _time.sleep(0.05)
        with contextlib.suppress(Exception):
            sound.seek(secs)
        Gui_sounds.previous = False
        Gui_sounds.paused = False
        Gui_sounds.song_local = None
        self._set_status(PlaybackStatus.PLAYING)
        self.start_next_monitor()
        self.send("song_pos", str(int(secs)))

    @playback_locked
    def pause(self, *val, abandon_focus: bool = True):
        self.stop_next_monitor()
        paused_position = pause_sound(Gui_sounds.sound)
        if paused_position is None:
            self._set_status(PlaybackStatus.IDLE)
            return
        Gui_sounds.paused = True
        Gui_sounds.song_local = [paused_position]
        self._set_status(PlaybackStatus.PAUSED)
        if utils.get_platform() == "android":
            release_wakelock()
            if abandon_focus:
                ctx, _ = wait_for_service_ctx()
                if ctx:
                    abandon_audio_focus(ctx)

    @playback_locked
    def toggle(self, *val):
        if self.status is PlaybackStatus.PLAYING:
            self.pause()
        else:
            self.play()

    def pause_val(self, *val):
        Gui_sounds.main_paused = True

    @playback_locked
    def stop(self, *val, notify: bool = True):
        self.stop_next_monitor()
        stop_and_unload(Gui_sounds.sound)
        Gui_sounds.sound = None
        Gui_sounds.paused = False
        Gui_sounds.song_local = None
        Gui_sounds.length = None
        Gui_sounds.file_to_load = None
        self._set_status(PlaybackStatus.IDLE, notify=notify)
        if utils.get_platform() == "android":
            release_wakelock()
            ctx, _ = wait_for_service_ctx()
            if ctx:
                abandon_audio_focus(ctx)

    @playback_locked
    def next(self, *val):
        if not self.local_navigation_enabled or not self.queue.items:
            return
        Gui_sounds.paused = False
        Gui_sounds.previous = False
        Gui_sounds.load_from_service = True
        target = self.queue.next()
        if target is not None:
            self.getting_song(target, record_history=False)

    @playback_locked
    def previous_bttn(self, *val):
        if not self.local_navigation_enabled or not self.queue.items:
            return
        Gui_sounds.paused = False
        Gui_sounds.previous = True
        Gui_sounds.load_from_service = True
        decision = self.queue.previous(safe_sound_position(Gui_sounds.sound))
        if decision.action is NavigationAction.RESTART:
            if Gui_sounds.sound is not None:
                with contextlib.suppress(Exception):
                    Gui_sounds.sound.seek(0.0)
                self.send("song_pos", "0")
            return
        if decision.action is NavigationAction.PLAY and decision.track is not None:
            self.getting_song(decision.track, record_history=False)

    def getting_song(self, message, *, record_history: bool = False):
        Gui_sounds.stream = os.path.join(Gui_sounds.set_local_download, message)
        Gui_sounds.set_local = message
        self._load_path(Gui_sounds.stream, record_history=record_history)

    @playback_locked
    def play_list(self, payload):
        items = None
        if isinstance(payload, (list, tuple)):
            if len(payload) == 1:
                p0 = payload[0]
                if isinstance(p0, (list, tuple)):
                    items = list(p0)
                else:
                    s = (
                        p0.decode("utf-8", "ignore")
                        if isinstance(p0, (bytes, bytearray))
                        else str(p0)
                    ).strip()
                    with contextlib.suppress(Exception):
                        val = json.loads(s)
                        if isinstance(val, (list, tuple)):
                            items = list(val)
                    if items is None:
                        with contextlib.suppress(Exception):
                            val = ast.literal_eval(s)
                            if isinstance(val, (list, tuple)):
                                items = list(val)
                    if items is None:
                        items = [s] if s else []
            else:
                items = list(payload)

        if items is None:
            s = (
                payload.decode("utf-8", "ignore")
                if isinstance(payload, (bytes, bytearray))
                else str(payload)
            ).strip()
            with contextlib.suppress(Exception):
                val = json.loads(s)
                if isinstance(val, (list, tuple)):
                    items = list(val)
        if items is None:
            with contextlib.suppress(Exception):
                val = ast.literal_eval(s)
                if isinstance(val, (list, tuple)):
                    items = list(val)
        if items is None:
            items = [s] if s else []
        Gui_sounds.playlist = [os.path.basename(str(x)) for x in items]
        self.queue.set_items(Gui_sounds.playlist)
        self._sync_android_system_state(self.status)

    @playback_locked
    def set_navigation_mode(self, *val):
        mode = "".join(
            bytes(item).decode("utf-8", "ignore")
            if isinstance(item, (bytes, bytearray))
            else str(item)
            for item in val
        ).strip().lower()
        self.local_navigation_enabled = mode == "local" and bool(self.queue.items)
        self._sync_android_system_state(self.status)

    @staticmethod
    def _refresh_request_id(values) -> str:
        parts = []
        for value in values:
            if isinstance(value, (bytes, bytearray)):
                parts.append(bytes(value).decode("utf-8", "ignore"))
            else:
                parts.append(str(value))
        raw = "".join(parts).strip()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("request_id") or "")

    def _snapshot(self, request_id: str) -> PlaybackSnapshot:
        has_loaded_track = Gui_sounds.sound is not None or self.status in {
            PlaybackStatus.PLAYING,
            PlaybackStatus.PAUSED,
        }
        track_name = self.queue.current if has_loaded_track else None
        if track_name is None and has_loaded_track:
            current_path = Gui_sounds.file_to_load
            if current_path:
                track_name = os.path.basename(str(current_path))

        position = safe_sound_position(Gui_sounds.sound)
        if self.status is PlaybackStatus.PAUSED and Gui_sounds.song_local:
            with contextlib.suppress(TypeError, ValueError, IndexError):
                position = float(Gui_sounds.song_local[0])

        cover_path = None
        if track_name:
            audio_path = os.path.join(Gui_sounds.set_local_download, track_name)
            cover_path = _resolve_cover_for_audio(audio_path)

        queue_size = (
            len(self.queue.items or Gui_sounds.playlist)
            if self.local_navigation_enabled
            else 0
        )
        return PlaybackSnapshot(
            request_id=request_id,
            status=self.status,
            track_name=track_name,
            cover_path=cover_path,
            duration=Gui_sounds.length or 0.0,
            position=position,
            repeat_enabled=self.loop_enabled,
            shuffle_enabled=self.queue.shuffle_enabled,
            queue_size=queue_size,
        )

    @playback_locked
    def refresh_gui(self, *val):
        Gui_sounds.main_paused = False
        request_id = self._refresh_request_id(val)
        self.send("playback_snapshot", self._snapshot(request_id).to_json())
        if Gui_sounds.load_from_service:
            self.send("update_image", Gui_sounds.set_local)
        self.send("are_we", self.status.value)

    def set_loop(self, want: bool):
        self.loop_enabled = want
        if self.sound is not None:
            with contextlib.suppress(Exception):
                self.sound.loop = self.loop_enabled

    def on_loop_msg(self, *val):
        raw = "".join(val)
        want = raw.strip().lower() in {"1", "true", "yes", "on"}
        self.set_loop(want)

    @playback_locked
    def shuffle(self, *val):
        want = "".join(val).strip().lower() in {"1", "true", "yes", "on"}
        Gui_sounds.shuffle_selected = want
        self.queue.set_shuffle(want)

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
        elif message_type == "download_progress":
            CLIENT.send_message("/download_progress", message)
        elif message_type == "download_result":
            CLIENT.send_message("/download_result", message)
        elif message_type == "are_we":
            CLIENT.send_message("/are_we", message)
        elif message_type == "playback_snapshot":
            CLIENT.send_message("/playback_snapshot", message)
        elif message_type == "song_not_found":
            CLIENT.send_message("/song_not_found", message)
            CLIENT.send_message("/are_we", "None")
        elif message_type == "error_reset":
            CLIENT.send_message("/error_reset", message)


GS = Gui_sounds()

if __name__ == "__main__":
    SERVER = OSCThreadServer(encoding="utf8")
    SERVER.listen("localhost", port=3000, default=True)
    SERVER.bind("/load", GS.load)
    SERVER.bind("/play", GS.play)
    SERVER.bind("/pause", GS.pause)
    SERVER.bind("/toggle", GS.toggle)
    SERVER.bind("/stop", GS.stop)
    SERVER.bind("/next", GS.next)
    SERVER.bind("/previous", GS.previous_bttn)
    SERVER.bind("/playlist", GS.play_list)
    SERVER.bind("/navigation_mode", GS.set_navigation_mode)
    SERVER.bind("/update_load_fs", GS.update_load_fs)
    SERVER.bind("/iamawake", GS.refresh_gui)
    SERVER.bind("/loop", GS.on_loop_msg)
    SERVER.bind("/shuffle", GS.shuffle)
    SERVER.bind("/get_update_slider", GS.update_slider)
    if utils.get_platform() != "android":
        SERVER.bind("/downloadyt", GS.download_yt)
        SERVER.bind("/cancel_download", GS.cancel_download)
    SERVER.bind("/iampaused", GS.pause_val)
    SERVER.bind("/seek_seconds", GS.seek_seconds)
    if utils.get_platform() == "android":
        ctx, svc = wait_for_service_ctx()
        if ctx and svc:
            _SESSION = setup_media_session(ctx)
            ensure_foreground(ctx, svc, _SESSION)
            GS._sync_android_system_state(GS.status)
        else:
            print("[service] no service ctx; cannot start foreground")
    while True:
        _time.sleep(1)

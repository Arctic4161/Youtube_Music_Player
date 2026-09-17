"""Pure playback state, queue navigation, and audio-handle helpers."""

from __future__ import annotations

import contextlib
import json
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Protocol, TypeVar

Song = TypeVar("Song")
SeekValue = str | int | float

PREVIOUS_RESTART_SECONDS = 5.0
END_POSITION_TOLERANCE_SECONDS = 0.75


class PlaybackStatus(str, Enum):
    """Shared UI/service playback states."""

    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


def android_playback_state(status: PlaybackStatus) -> tuple[str, float]:
    """Map app state to an Android PlaybackState constant name and speed."""

    return {
        PlaybackStatus.IDLE: ("STATE_STOPPED", 0.0),
        PlaybackStatus.LOADING: ("STATE_BUFFERING", 0.0),
        PlaybackStatus.PLAYING: ("STATE_PLAYING", 1.0),
        PlaybackStatus.PAUSED: ("STATE_PAUSED", 0.0),
    }[status]


class NavigationAction(str, Enum):
    """An action selected by queue navigation."""

    NONE = "none"
    PLAY = "play"
    RESTART = "restart"


@dataclass(frozen=True)
class NavigationDecision(Generic[Song]):
    """The result of a Previous-button decision."""

    action: NavigationAction
    track: Song | None = None


@dataclass
class DownloadRequestTracker:
    """Own one download request and reject stale progress/results."""

    timeout_seconds: float
    active_id: str | None = None
    last_activity: float | None = None

    def begin(self, request_id: str, *, now: float) -> None:
        self.active_id = request_id
        self.last_activity = now

    def accepts(self, request_id: str) -> bool:
        return bool(request_id) and request_id == self.active_id

    def touch(self, request_id: str, *, now: float) -> bool:
        if not self.accepts(request_id):
            return False
        self.last_activity = now
        return True

    def expired(self, *, now: float) -> bool:
        if self.active_id is None or self.last_activity is None:
            return False
        return now - self.last_activity >= self.timeout_seconds

    def cancel(self) -> None:
        self.active_id = None
        self.last_activity = None


def _non_negative_float(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0.0 else 0.0


def _snapshot_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PlaybackSnapshot:
    """Complete service state used to rebuild a newly created GUI process."""

    request_id: str
    status: PlaybackStatus
    track_name: str | None = None
    cover_path: str | None = None
    duration: float = 0.0
    position: float = 0.0
    repeat_enabled: bool = False
    shuffle_enabled: bool = False
    queue_size: int = 0
    playback_mode: str = "local"
    radio_available: bool = False

    def to_json(self) -> str:
        duration = _non_negative_float(self.duration)
        position = _non_negative_float(self.position)
        if duration > 0.0:
            position = min(position, duration)
        return json.dumps(
            {
                "version": 2,
                "request_id": self.request_id,
                "status": self.status.value,
                "track_name": self.track_name,
                "cover_path": self.cover_path,
                "duration": duration,
                "position": position,
                "repeat_enabled": self.repeat_enabled,
                "shuffle_enabled": self.shuffle_enabled,
                "queue_size": max(0, int(self.queue_size)),
                "playback_mode": self.playback_mode,
                "radio_available": self.radio_available,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: object) -> PlaybackSnapshot | None:
        if isinstance(payload, (bytes, bytearray)):
            raw = bytes(payload).decode("utf-8", "ignore")
        elif isinstance(payload, str):
            raw = payload
        else:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        status = normalize_playback_status(data.get("status"))
        if status is None:
            return None
        request_id = str(data.get("request_id") or "")
        track_raw = data.get("track_name")
        cover_raw = data.get("cover_path")
        track_name = str(track_raw) if track_raw else None
        cover_path = str(cover_raw) if cover_raw else None
        duration = _non_negative_float(data.get("duration"))
        position = _non_negative_float(data.get("position"))
        if duration > 0.0:
            position = min(position, duration)
        try:
            queue_size = max(0, int(data.get("queue_size") or 0))
        except (TypeError, ValueError):
            queue_size = 0
        playback_mode = str(data.get("playback_mode") or "local").strip().lower()
        if playback_mode not in {"local", "radio"}:
            playback_mode = "local"
        return cls(
            request_id=request_id,
            status=status,
            track_name=track_name,
            cover_path=cover_path,
            duration=duration,
            position=position,
            repeat_enabled=_snapshot_bool(data.get("repeat_enabled")),
            shuffle_enabled=_snapshot_bool(data.get("shuffle_enabled")),
            queue_size=queue_size,
            playback_mode=playback_mode,
            radio_available=_snapshot_bool(data.get("radio_available")),
        )


@dataclass
class PlaybackQueue(Generic[Song]):
    """Ordered queue, shuffle bag, and actual listening history."""

    items: list[Song] = field(default_factory=list)
    current: Song | None = None
    history: list[Song] = field(default_factory=list)
    shuffle_enabled: bool = False
    shuffle_remaining: list[Song] = field(default_factory=list)
    randomize: Callable[[list[Song]], None] = field(
        default=random.shuffle,
        repr=False,
        compare=False,
    )

    def set_items(self, items: Sequence[Song]) -> None:
        """Replace the queue while retaining only still-valid history."""

        self.items = list(items)
        self.history = [item for item in self.history if item in self.items]
        self.shuffle_remaining = [
            item for item in self.shuffle_remaining if item in self.items
        ]
        if self.shuffle_enabled and not self.shuffle_remaining:
            self._rebuild_shuffle()

    def select(self, track: Song, *, record_history: bool = True) -> Song:
        """Select a track and optionally record the old current track."""

        if record_history and self.current is not None and self.current != track:
            self.history.append(self.current)
        self.current = track
        with contextlib.suppress(ValueError):
            self.shuffle_remaining.remove(track)
        return track

    def set_shuffle(self, enabled: bool) -> None:
        self.shuffle_enabled = enabled
        if enabled:
            self._rebuild_shuffle()
        else:
            self.shuffle_remaining.clear()

    def next(self) -> Song | None:
        """Choose the next track, wrapping multi-track queues."""

        if len(self.items) < 2:
            return None
        if self.shuffle_enabled:
            if not self.shuffle_remaining:
                self._rebuild_shuffle()
            target = self.shuffle_remaining.pop() if self.shuffle_remaining else None
        else:
            target = next_song(self.items, self.current, wrap=True)
        if target is None:
            return None
        return self.select(target)

    def previous(
        self,
        position: float,
        *,
        restart_seconds: float = PREVIOUS_RESTART_SECONDS,
    ) -> NavigationDecision[Song]:
        """Restart after the threshold; otherwise follow listening history."""

        if self.current is not None and position >= restart_seconds:
            return NavigationDecision(NavigationAction.RESTART, self.current)

        while self.history:
            target = self.history.pop()
            if target != self.current and target in self.items:
                self.current = target
                with contextlib.suppress(ValueError):
                    self.shuffle_remaining.remove(target)
                return NavigationDecision(NavigationAction.PLAY, target)

        if len(self.items) < 2:
            return NavigationDecision(NavigationAction.NONE, self.current)
        fallback_target = previous_song(self.items, self.current)
        if fallback_target is None:
            return NavigationDecision(NavigationAction.NONE, self.current)
        self.current = fallback_target
        with contextlib.suppress(ValueError):
            self.shuffle_remaining.remove(fallback_target)
        return NavigationDecision(NavigationAction.PLAY, fallback_target)

    def _rebuild_shuffle(self) -> None:
        self.shuffle_remaining = [
            item for item in self.items if self.current is None or item != self.current
        ]
        self.randomize(self.shuffle_remaining)


class SoundHandle(Protocol):
    state: str
    length: float
    loop: bool

    def play(self) -> object: ...

    def stop(self) -> object: ...

    def unload(self) -> object: ...

    def seek(self, position: float) -> object: ...

    def get_pos(self) -> float: ...


def normalize_playback_status(value: object) -> PlaybackStatus | None:
    """Normalize current and legacy service state payloads."""

    raw = str(value).strip().lower()
    mapping = {
        "idle": PlaybackStatus.IDLE,
        "none": PlaybackStatus.IDLE,
        "loading": PlaybackStatus.LOADING,
        "playing": PlaybackStatus.PLAYING,
        "false": PlaybackStatus.PLAYING,
        "paused": PlaybackStatus.PAUSED,
        "true": PlaybackStatus.PAUSED,
    }
    return mapping.get(raw)


def next_song(
    songs: Sequence[Song],
    current_song: Song | None,
    *,
    wrap: bool = True,
) -> Song | None:
    """Return the next song without treating a one-track queue as repeat."""

    if len(songs) < 2:
        return None
    try:
        current_index = songs.index(current_song)
    except ValueError:
        return songs[0]
    next_index = current_index + 1
    if next_index < len(songs):
        return songs[next_index]
    return songs[0] if wrap else None


def previous_song(songs: Sequence[Song], current_song: Song | None) -> Song | None:
    """Return the prior song, wrapping from the first item to the last."""

    if not songs:
        return None
    try:
        current_index = songs.index(current_song)
    except ValueError:
        return songs[0]
    return songs[current_index - 1]


def clamp_seek(position: SeekValue, duration: SeekValue | None) -> float:
    """Convert and clamp an absolute seek position."""

    try:
        seconds = max(0.0, float(position))
    except (TypeError, ValueError):
        seconds = 0.0
    try:
        length = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        length = 0.0
    if length > 0.0:
        return min(seconds, max(0.0, length - 0.1))
    return seconds


def safe_sound_position(sound: SoundHandle | None) -> float:
    """Return a non-negative position from an optional audio handle."""

    if sound is None:
        return 0.0
    try:
        return max(0.0, float(sound.get_pos() or 0.0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def pause_sound(sound: SoundHandle | None) -> float | None:
    """Stop an active handle while retaining its resumable position."""

    if sound is None:
        return None
    position = safe_sound_position(sound)
    try:
        sound.stop()
    except Exception:
        return None
    return position


def resume_sound(sound: SoundHandle | None, position: float | None = None) -> bool:
    """Start an optional handle and restore a paused position when supplied."""

    if sound is None:
        return False
    try:
        sound.play()
    except Exception:
        return False
    if position is not None:
        try:
            sound.seek(clamp_seek(position, getattr(sound, "length", None)))
        except Exception:
            pass
    return True


def stop_and_unload(sound: SoundHandle | None) -> None:
    """Best-effort stop and unload, regardless of the current backend state."""

    if sound is None:
        return
    try:
        sound.stop()
    except Exception:
        pass
    try:
        sound.unload()
    except Exception:
        pass


def has_reached_end(
    *,
    position: float,
    duration: float,
    backend_state: str,
    previous_backend_state: str,
    max_position: float,
    paused: bool,
    looping: bool,
) -> bool:
    """Detect a real end without interpreting an ordinary position stall as EOF."""

    if paused or looping or max_position <= 0.0:
        return False
    near_end = duration > 0.0 and position >= max(
        0.0,
        duration - END_POSITION_TOLERANCE_SECONDS,
    )
    backend_finished = previous_backend_state == "play" and backend_state != "play"
    return near_end or backend_finished

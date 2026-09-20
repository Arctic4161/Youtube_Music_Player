"""Pure state and response parsing for the temporary Radio playback mode."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from playback_logic import NavigationAction, NavigationDecision


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


@dataclass(frozen=True)
class RadioTrack:
    """One YouTube recommendation held only for the active Radio session."""

    video_id: str
    title: str
    thumbnail_url: str | None = None


@dataclass(frozen=True)
class RadioSeed:
    """The local track and position to restore when Radio is stopped."""

    video_id: str
    path: str
    title: str
    cover_path: str | None
    position: float
    artist: str | None = None


@dataclass
class RadioSession:
    """A non-persistent, de-duplicated Radio queue and listening history."""

    active: bool = False
    seed: RadioSeed | None = None
    current: RadioTrack | None = None
    pending: list[RadioTrack] = field(default_factory=list)
    history: list[RadioTrack] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    generation: int = 0

    def start(self, seed: RadioSeed) -> int:
        self.generation += 1
        self.active = True
        self.seed = seed
        self.current = None
        self.pending.clear()
        self.history.clear()
        self.seen_ids = {seed.video_id}
        return self.generation

    def stop(self) -> RadioSeed | None:
        seed = self.seed
        self.generation += 1
        self.active = False
        self.seed = None
        self.current = None
        self.pending.clear()
        self.history.clear()
        self.seen_ids.clear()
        return seed

    def add_candidates(self, tracks: Iterable[RadioTrack]) -> int:
        """Append valid, not-yet-played candidates and return the number added."""

        if not self.active:
            return 0
        added = 0
        for track in tracks:
            if not is_valid_video_id(track.video_id) or track.video_id in self.seen_ids:
                continue
            title = str(track.title or "").strip()
            if not title:
                continue
            self.pending.append(
                RadioTrack(
                    video_id=track.video_id,
                    title=title,
                    thumbnail_url=track.thumbnail_url or None,
                )
            )
            self.seen_ids.add(track.video_id)
            added += 1
        return added

    def next(self) -> RadioTrack | None:
        """Move to the next unseen recommendation without wrapping history."""

        if not self.active:
            return None
        if not self.pending:
            return None
        if self.current is not None:
            self.history.append(self.current)
        self.current = self.pending.pop(0)
        return self.current

    def discard_current(self) -> None:
        """Drop an unplayable candidate without adding it to listening history."""

        self.current = None

    def previous(self, position: float, *, restart_seconds: float = 5.0) -> NavigationDecision[RadioTrack]:
        """Restart the current Radio track or return to actual listening history."""

        if not self.active or self.current is None:
            return NavigationDecision(NavigationAction.NONE)
        if position >= restart_seconds:
            return NavigationDecision(NavigationAction.RESTART, self.current)
        if not self.history:
            return NavigationDecision(NavigationAction.NONE, self.current)
        previous = self.history.pop()
        self.pending.insert(0, self.current)
        self.current = previous
        return NavigationDecision(NavigationAction.PLAY, previous)

    @property
    def queue_size(self) -> int:
        return int(self.current is not None) + len(self.pending)

    @property
    def needs_refill(self) -> bool:
        return self.active and len(self.pending) <= 5


def is_valid_video_id(value: object) -> bool:
    return bool(_VIDEO_ID_RE.fullmatch(str(value or "").strip()))


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    simple = value.get("simpleText") or value.get("content")
    if isinstance(simple, str):
        return simple.strip()
    runs = value.get("runs")
    if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes)):
        return "".join(
            str(item.get("text") or "")
            for item in runs
            if isinstance(item, Mapping)
        ).strip()
    return ""


def _nested(mapping: Mapping[str, object], *keys: str) -> object:
    current: object = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_thumbnail(node: Mapping[str, object]) -> str | None:
    candidates = (
        _nested(node, "thumbnail", "thumbnails"),
        _nested(node, "thumbnail", "musicThumbnailRenderer", "thumbnail", "thumbnails"),
        _nested(node, "contentImage", "thumbnailViewModel", "image", "sources"),
        _nested(node, "contentImage", "collectionThumbnailViewModel", "primaryThumbnail", "thumbnailViewModel", "image", "sources"),
    )
    for items in candidates:
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in reversed(items):
            if isinstance(item, Mapping):
                url = str(item.get("url") or "").strip()
                if url:
                    return url
    return None


_MUSIC_VIDEO_TYPES = frozenset({"MUSIC_VIDEO_TYPE_ATV", "MUSIC_VIDEO_TYPE_OMV"})


def _music_renderer_nodes(
    value: object, renderer_name: str,
) -> Iterable[Mapping[str, object]]:
    """Read owning music rows, never a wrapper's alternate video counterpart."""

    if isinstance(value, Mapping):
        wrapper = value.get("playlistPanelVideoWrapperRenderer")
        if isinstance(wrapper, Mapping):
            yield from _music_renderer_nodes(wrapper.get("primaryRenderer"), renderer_name)
            return
        renderer = value.get(renderer_name)
        if isinstance(renderer, Mapping):
            yield renderer
            return
        for child in value.values():
            yield from _music_renderer_nodes(child, renderer_name)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _music_renderer_nodes(child, renderer_name)


def _music_track(node: Mapping[str, object], *, search: bool) -> RadioTrack | None:
    if (
        "unplayableText" in node
        or node.get("isPlayable") is False
        or node.get("musicItemRendererDisplayPolicy")
        == "MUSIC_ITEM_RENDERER_DISPLAY_POLICY_GREY_OUT"
    ):
        return None
    if search:
        endpoint = _nested(
            node, "overlay", "musicItemThumbnailOverlayRenderer", "content",
            "musicPlayButtonRenderer", "playNavigationEndpoint", "watchEndpoint",
        )
        columns = node.get("flexColumns")
        if not isinstance(columns, list) or not columns or not isinstance(columns[0], Mapping):
            return None
        title = _text(_nested(columns[0], "musicResponsiveListItemFlexColumnRenderer", "text"))
        row_id = _nested(node, "playlistItemData", "videoId")
    else:
        endpoint = _nested(node, "navigationEndpoint", "watchEndpoint")
        title = _text(node.get("title"))
        row_id = node.get("videoId")
        if not is_valid_video_id(row_id):
            return None
    if not isinstance(endpoint, Mapping):
        return None
    video_type = _nested(
        endpoint, "watchEndpointMusicSupportedConfigs", "watchEndpointMusicConfig", "musicVideoType",
    )
    if not isinstance(video_type, str) or video_type not in _MUSIC_VIDEO_TYPES:
        return None
    video_id = str(endpoint.get("videoId") or "").strip()
    # A menu's related song must not classify a different row as music.
    if row_id is not None and str(row_id).strip() != video_id:
        return None
    if not is_valid_video_id(video_id) or not title:
        return None
    return RadioTrack(video_id, title, _first_thumbnail(node))


def _parse_music_tracks(
    payload: object, *, search: bool, exclude_ids: Iterable[str], limit: int,
) -> list[RadioTrack]:
    excluded = {str(item).strip() for item in exclude_ids}
    renderer_name = "musicResponsiveListItemRenderer" if search else "playlistPanelVideoRenderer"
    tracks: list[RadioTrack] = []
    for node in _music_renderer_nodes(payload, renderer_name):
        track = _music_track(node, search=search)
        if track is None or track.video_id in excluded:
            continue
        excluded.add(track.video_id)
        tracks.append(track)
        if len(tracks) >= max(1, limit):
            break
    return tracks


def parse_related_tracks(
    payload: object,
    *,
    exclude_ids: Iterable[str] = (),
    limit: int = 20,
) -> list[RadioTrack]:
    """Admit Music radio songs/videos; unknown and non-music rows fail closed."""

    return _parse_music_tracks(payload, search=False, exclude_ids=exclude_ids, limit=limit)


def parse_music_search_tracks(
    payload: object,
    *,
    exclude_ids: Iterable[str] = (),
    limit: int = 20,
) -> list[RadioTrack]:
    """Apply the radio music policy to each fallback search row as well."""

    return _parse_music_tracks(payload, search=True, exclude_ids=exclude_ids, limit=limit)


def tracks_from_search_results(
    results: object,
    *,
    exclude_ids: Iterable[str] = (),
    limit: int = 20,
) -> list[RadioTrack]:
    """Normalize the existing search package's fallback result shape."""

    raw_items = results.get("result") if isinstance(results, Mapping) else None
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return []
    excluded = {str(item).strip() for item in exclude_ids}
    tracks: list[RadioTrack] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        video_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not is_valid_video_id(video_id) or not title or video_id in excluded:
            continue
        thumbnail_url = None
        thumbnails = item.get("thumbnails")
        if isinstance(thumbnails, Sequence) and not isinstance(thumbnails, (str, bytes)):
            for thumbnail in reversed(thumbnails):
                if isinstance(thumbnail, Mapping) and thumbnail.get("url"):
                    thumbnail_url = str(thumbnail["url"])
                    break
        excluded.add(video_id)
        tracks.append(RadioTrack(video_id, title, thumbnail_url))
        if len(tracks) >= max(1, limit):
            break
    return tracks

"""Pure validation for results returned by youtube-search-python."""

from __future__ import annotations

from dataclasses import dataclass

from media_identity import stable_media_id


@dataclass(frozen=True)
class SearchResult:
    title: str
    link: str
    thumbnail_url: str
    media_id: str


def parse_search_results(payload: object) -> list[SearchResult]:
    """Return only complete, playable search results from a library payload."""

    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("result")
    if not isinstance(raw_results, list):
        return []

    parsed: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        link = str(item.get("link") or "").strip()
        thumbnails = item.get("thumbnails")
        thumbnail_url = ""
        if isinstance(thumbnails, list):
            for thumbnail in thumbnails:
                if isinstance(thumbnail, dict) and thumbnail.get("url"):
                    thumbnail_url = str(thumbnail["url"]).strip()
                    break
        if not title or not link:
            continue
        parsed.append(
            SearchResult(
                title=title,
                link=link,
                thumbnail_url=thumbnail_url,
                media_id=stable_media_id(link, item.get("id")),
            )
        )
    return parsed

"""HTTPX 0.28 compatibility for the pinned YouTube search dependency."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _legacy_proxy_url(proxies: object) -> str | None:
    """Return the HTTPS proxy from youtube-search-python's legacy mapping."""

    if not isinstance(proxies, Mapping):
        return None
    for scheme in ("https://", "http://"):
        value = proxies.get(scheme)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _post_with_current_httpx(
    httpx_module: Any,
    *,
    url: str,
    headers: dict[str, str],
    data: Any,
    timeout: float | None,
    proxies: object,
) -> Any:
    """Send a search request without the removed ``proxies=`` HTTPX argument."""

    proxy = _legacy_proxy_url(proxies)
    if proxy:
        # HTTPX 0.28 accepts one explicit proxy per client.  The search endpoint
        # is HTTPS, so prefer the legacy HTTPS proxy when both are configured.
        with httpx_module.Client(proxy=proxy, trust_env=False) as client:
            return client.post(url, headers=headers, json=data, timeout=timeout)
    return httpx_module.post(
        url,
        headers=headers,
        json=data,
        timeout=timeout,
        trust_env=True,
    )


def _install_httpx_compatibility() -> None:
    """Patch the pinned dependency's synchronous request method once."""

    from youtubesearchpython.core.constants import userAgent
    from youtubesearchpython.core.requests import RequestCore

    if getattr(RequestCore.syncPostRequest, "_ymp_httpx_028_compatible", False):
        return

    def sync_post_request(request_core: Any) -> Any:
        import httpx

        return _post_with_current_httpx(
            httpx,
            url=request_core.url,
            headers={"User-Agent": userAgent},
            data=request_core.data,
            timeout=request_core.timeout,
            proxies=request_core.proxy,
        )

    sync_post_request._ymp_httpx_028_compatible = True
    RequestCore.syncPostRequest = sync_post_request


def create_video_search(query: str, *, timeout: int = 15) -> Any:
    """Create a video search using the current HTTPX request API."""

    _install_httpx_compatibility()
    from youtubesearchpython import VideosSearch

    return VideosSearch(query, timeout=timeout)

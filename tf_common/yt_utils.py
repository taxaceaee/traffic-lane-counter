"""Canonical YouTube helpers for the tf_* runtime."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from typing import Any

logger = logging.getLogger("trafficflow.yt_utils")

DEFAULT_FORMAT = "best[height<=720]"
MAX_RETRIES = 3
RETRY_DELAY_S = 5.0
_LIVE_STREAM_CACHE_TTL_S = 300.0
_stream_info_cache: dict[str, tuple[dict[str, Any], float]] = {}
_stream_info_cache_lock = threading.Lock()


def build_ydl_opts(
    overrides: dict[str, Any] | None = None,
    quiet: bool = True,
) -> dict[str, Any]:
    """Build a yt-dlp option set with optional cookie support."""
    opts: dict[str, Any] = {
        "quiet": quiet,
        "no_warnings": quiet,
        "extract_flat": False,
    }

    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE")
    if cookies_file:
        cookies_path = os.path.expanduser(cookies_file)
        if os.path.isfile(cookies_path):
            opts["cookiefile"] = cookies_path
            logger.info("YouTube cookies from file: %s", cookies_path)
        else:
            logger.warning(
                "YOUTUBE_COOKIES_FILE=%s set but file not found",
                cookies_path,
            )

    cookies_browser = os.environ.get("YOUTUBE_COOKIES_FROM_BROWSER")
    if cookies_browser and cookies_browser.strip():
        browser = cookies_browser.strip()
        # yt-dlp's Python API expects (browser, profile, keyring, container),
        # while the environment-friendly form is "chrome:Default".
        browser_name, separator, profile = browser.partition(":")
        opts["cookiesfrombrowser"] = (
            browser_name,
            profile if separator and profile else None,
            None,
            None,
        )
        logger.info("YouTube cookies from browser: %s", browser)

    # YouTube now requires a JS challenge solver for full extraction. Node is
    # already part of the local development toolchain, so enable it when
    # present. The remote EJS component is allowed by default because yt-dlp
    # ships the solver core but not the frequently updated challenge library.
    node_path = shutil.which("node")
    if node_path:
        opts["js_runtimes"] = {"node": {"path": node_path}}
    remote_components = os.environ.get("YTDLP_REMOTE_COMPONENTS", "ejs:github")
    opts["remote_components"] = [
        item.strip() for item in remote_components.split(",") if item.strip()
    ]
    pot_port = os.environ.get("YTDLP_BGUTIL_PORT", "4416").strip()
    if pot_port != "4416":
        opts.setdefault("extractor_args", {}).setdefault(
            "youtubepot-bgutilhttp", {}
        )["base_url"] = [f"http://127.0.0.1:{pot_port}"]

    if overrides:
        opts.update(overrides)
    return opts


def resolve_stream_info(
    url: str,
    fmt: str = DEFAULT_FORMAT,
    retries: int = MAX_RETRIES,
    use_cache: bool = False,
    allow_stale_cache: bool = True,
) -> dict[str, Any]:
    """Resolve a YouTube URL to direct stream metadata.

    ``use_cache`` is intended for live-reader restarts. A config reload should
    not discard a still-valid HLS URL and immediately trigger another YouTube
    page extraction, which is especially prone to transient anti-bot failures.
    Periodic refreshes can bypass this cache by leaving it disabled.
    """
    import yt_dlp

    cache_key = f"{url}\x00{fmt}"
    if use_cache:
        with _stream_info_cache_lock:
            cached = _stream_info_cache.get(cache_key)
            if cached is not None and time.monotonic() - cached[1] < _LIVE_STREAM_CACHE_TTL_S:
                logger.info("Using cached YouTube live stream URL for %s", url)
                return dict(cached[0])

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with yt_dlp.YoutubeDL(build_ydl_opts({"format": fmt})) as ydl:
                info = ydl.extract_info(url, download=False)

            resolved = {
                "url": _pick_stream_url(info),
                "width": info.get("width", 0) or 0,
                "height": info.get("height", 0) or 0,
                "fps": float(info.get("fps", 25.0) or 25.0),
                "duration": info.get("duration"),
            }
            if not resolved["url"]:
                raise ValueError("yt-dlp returned no stream URL")
            with _stream_info_cache_lock:
                _stream_info_cache[cache_key] = (dict(resolved), time.monotonic())
            return resolved
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning(
                    "yt-dlp resolve attempt %s/%s failed: %s - retrying in %.1fs",
                    attempt,
                    retries,
                    exc,
                    RETRY_DELAY_S,
                )
                time.sleep(RETRY_DELAY_S)

    # A short-lived extractor failure must not take down a stream that was
    # already resolved successfully. The reader will still refresh normally
    # when its periodic refresh path explicitly bypasses this cache.
    if allow_stale_cache:
        with _stream_info_cache_lock:
            cached = _stream_info_cache.get(cache_key)
            if cached is not None and time.monotonic() - cached[1] < _LIVE_STREAM_CACHE_TTL_S:
                logger.warning(
                    "yt-dlp could not resolve %s; reusing cached live URL: %s",
                    url,
                    last_exc,
                )
                return dict(cached[0])

    raise ValueError(
        f"Failed to resolve stream info after {retries} retries: {last_exc}"
    ) from last_exc


def resolve_stream_url(
    url: str,
    fmt: str = DEFAULT_FORMAT,
    retries: int = MAX_RETRIES,
) -> str:
    """Resolve a YouTube URL to a direct media URL."""
    return resolve_stream_info(url, fmt, retries)["url"]


def download_video(url: str, output_path: str, fmt: str = DEFAULT_FORMAT) -> None:
    """Download a YouTube URL to a local file."""
    import yt_dlp

    with yt_dlp.YoutubeDL(
        build_ydl_opts({"format": fmt, "outtmpl": output_path})
    ) as ydl:
        ydl.download([url])


def _pick_stream_url(info: dict[str, Any]) -> str:
    url = info.get("url")
    if url:
        return url

    requested = info.get("requested_formats")
    if requested:
        for fmt in requested:
            if fmt.get("vcodec", "none") != "none":
                candidate = fmt.get("url")
                if candidate:
                    return candidate
        candidate = requested[0].get("url")
        if candidate:
            return candidate

    formats = info.get("formats")
    if formats:
        for fmt in formats:
            if fmt.get("vcodec", "none") != "none" and fmt.get("url"):
                return fmt["url"]

    return info.get("webpage_url", "")

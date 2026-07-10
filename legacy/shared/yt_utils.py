"""Shared YouTube utilities: URL resolution, cookie support, retry logic.

All yt-dlp calls throughout the codebase go through this module so that
cookie handling and retry behaviour is consistent across all components.

Cookie sources (checked in order):
  1. Env ``YOUTUBE_COOKIES_FILE`` — path to a Netscape-format cookie file.
  2. Env ``YOUTUBE_COOKIES_FROM_BROWSER`` — e.g. ``chrome``, ``firefox``, ``brave``.
"""

import logging
import os
import time
from typing import Any

logger = logging.getLogger("trafficflow.yt_utils")

DEFAULT_FORMAT = "best[height<=720]"
MAX_RETRIES = 3
RETRY_DELAY_S = 5.0


# ---------------------------------------------------------------------------
# yt-dlp option builder
# ---------------------------------------------------------------------------

def build_ydl_opts(overrides: dict[str, Any] | None = None, quiet: bool = True) -> dict[str, Any]:
    """Build a yt-dlp options dict with cookie support from env vars.

    Environment variables read:

    ===============================  ============================================
    ``YOUTUBE_COOKIES_FILE``         Path to Netscape-format cookie file  (``cookiefile``).
    ``YOUTUBE_COOKIES_FROM_BROWSER`` Browser name for cookie extraction  (``cookiesfrombrowser``).
    ===============================  ============================================

    If neither is set the opts dict is returned as-is (no auth).
    """
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
        opts["cookiesfrombrowser"] = (browser,)
        logger.info("YouTube cookies from browser: %s", browser)

    if overrides:
        opts.update(overrides)
    return opts


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def resolve_stream_info(
    url: str,
    fmt: str = DEFAULT_FORMAT,
    retries: int = MAX_RETRIES,
) -> dict:
    """Resolve a YouTube URL to stream metadata via yt-dlp with retry.

    Parameters
    ----------
    url:
        YouTube watch URL or video id.
    fmt:
        yt-dlp format string (default ``best[height<=720]``).
    retries:
        How many times to retry on failure (default 3).

    Returns
    -------
    dict with keys ``url``, ``width``, ``height``, ``fps``, ``duration``.

    Raises
    ------
    ValueError
        After exhausting all retries.
    """
    import yt_dlp  # noqa: F811

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
            return resolved

        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                msg = (
                    f"yt-dlp resolve attempt {attempt}/{retries} failed: {exc}"
                    f" — retrying in {RETRY_DELAY_S}s"
                )
                logger.warning(msg)
                time.sleep(RETRY_DELAY_S)

    raise ValueError(
        f"Failed to resolve stream info after {retries} retries: {last_exc}"
    ) from last_exc


def resolve_stream_url(
    url: str,
    fmt: str = DEFAULT_FORMAT,
    retries: int = MAX_RETRIES,
) -> str:
    """Resolve a YouTube URL to a direct stream URL (convenience wrapper)."""
    return resolve_stream_info(url, fmt, retries)["url"]


def download_video(url: str, output_path: str, fmt: str = DEFAULT_FORMAT) -> None:
    """Download a YouTube video to a local file via yt-dlp (with cookie support)."""
    import yt_dlp  # noqa: F811

    with yt_dlp.YoutubeDL(
        build_ydl_opts({"format": fmt, "outtmpl": output_path})
    ) as ydl:
        ydl.download([url])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pick_stream_url(info: dict) -> str:
    """Extract the best stream URL from a yt-dlp info dict."""
    url = info.get("url")
    if url:
        return url

    requested = info.get("requested_formats")
    if requested:
        for fmt in requested:
            if fmt.get("vcodec", "none") != "none":
                u = fmt.get("url")
                if u:
                    return u
        u = requested[0].get("url")
        if u:
            return u

    formats = info.get("formats")
    if formats:
        for fmt in formats:
            if fmt.get("vcodec", "none") != "none" and fmt.get("url"):
                return fmt["url"]

    return info.get("webpage_url", "")

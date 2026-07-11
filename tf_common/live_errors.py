"""Structured diagnostics for live camera source failures.

The live UI needs more than the last exception string: operators need a
stable error code, a safe explanation, concrete remediation steps, and a
repeatable verification method.  This module deliberately never exposes
yt-dlp's raw exception text to the browser because extractor errors can
contain source URLs or implementation details.
"""

from __future__ import annotations

import os
import shlex
from typing import Any


def _diagnostic(
    *,
    code: str,
    severity: str,
    title: str,
    message: str,
    cause: str,
    fix_steps: list[str],
    verify_steps: list[str],
    source_type: str,
    source: str | None = None,
    retryable: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
        "cause": cause,
        "fix_steps": fix_steps,
        "verify_steps": verify_steps,
        "source_type": source_type,
        "retryable": retryable,
    }
    if source_type in {"youtube", "youtube_live"} and source:
        command = [
            ".venv/bin/yt-dlp",
            "--simulate",
            "--no-playlist",
            "--js-runtimes",
            "node",
            "--remote-components",
            os.environ.get("YTDLP_REMOTE_COMPONENTS", "ejs:github"),
        ]
        browser = os.environ.get("YOUTUBE_COOKIES_FROM_BROWSER", "").strip()
        cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
        if browser:
            command.extend(["--cookies-from-browser", browser])
        elif cookies_file:
            command.extend(["--cookies", cookies_file])
        command.extend(["--format", "best[height<=720]", source])
        result["verification_command"] = " ".join(shlex.quote(part) for part in command)
    return result


def diagnose_stream_error(
    exc: BaseException,
    *,
    source_type: str = "video",
    source: str | None = None,
) -> dict[str, Any]:
    """Map a reader/extractor exception to safe operator-facing diagnostics."""
    text = str(exc).casefold()
    is_youtube = source_type in {"youtube", "youtube_live"}

    if is_youtube and any(
        marker in text
        for marker in (
            "sign in to confirm you’re not a bot",
            "sign in to confirm you're not a bot",
            "confirm you are not a bot",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "not a bot",
        )
    ):
        return _diagnostic(
            code="YOUTUBE_ANTIBOT_BLOCKED",
            severity="critical",
            title="YouTube blocked yt-dlp (anti-bot)",
            message="YouTube yêu cầu xác minh người dùng và đã từ chối yt-dlp.",
            cause=(
                "Nguồn YouTube đang yêu cầu đăng nhập/xác minh bot; đây là lỗi "
                "từ extractor hoặc chính sách YouTube, không phải lỗi model/ROI."
            ),
            fix_steps=[
                "Chạy npm run dev để tự bật Node/EJS và PO-token provider cục bộ.",
                "Cập nhật yt-dlp trên đúng môi trường API: .venv/bin/python -m pip install -U yt-dlp.",
                "Nếu máy không có Chrome profile đang đăng nhập, đặt YOUTUBE_COOKIES_FILE=/đường/dẫn/cookies.txt.",
                "Khởi động lại API sau khi cấu hình cookies; không đưa cookies vào git hoặc frontend.",
                "Nếu vẫn bị chặn, dùng URL live khác hoặc nguồn RTSP/HLS được cấp phép thay cho YouTube extractor.",
            ],
            verify_steps=[
                "Bấm Verify source trong Live Monitoring/Alerts để chạy kiểm tra extractor mới nhất.",
                "Hoặc chạy verification command bên dưới ngay trên máy đang chạy API.",
            ],
            source_type=source_type,
            source=source,
        )

    if is_youtube and "no video formats found" in text:
        return _diagnostic(
            code="YOUTUBE_NO_FORMATS",
            severity="critical",
            title="YouTube returned no playable format",
            message="yt-dlp không tìm thấy format video có thể phát cho nguồn này.",
            cause="Video có thể đã kết thúc, bị giới hạn quyền riêng tư/khu vực, hoặc extractor không còn tương thích.",
            fix_steps=[
                "Xác nhận URL vẫn mở được bằng trình duyệt trên cùng máy chạy API.",
                "Cập nhật yt-dlp và kiểm tra lại cookies nếu video yêu cầu đăng nhập.",
                "Chọn một nguồn live công khai hoặc chuyển sang RTSP/HLS ổn định.",
            ],
            verify_steps=[
                "Bấm Verify source hoặc chạy verification command bên dưới.",
                "Nếu trình duyệt mở được nhưng yt-dlp không mở được, kiểm tra cookies và phiên bản yt-dlp.",
            ],
            source_type=source_type,
            source=source,
        )

    if is_youtube and any(
        marker in text for marker in ("timed out", "timeout", "network", "connection reset")
    ):
        return _diagnostic(
            code="YOUTUBE_NETWORK_ERROR",
            severity="warning",
            title="Cannot reach YouTube",
            message="Không thể kết nối tới YouTube để lấy stream URL.",
            cause="Kết nối mạng, DNS, proxy hoặc rate limit đang ngăn API gọi YouTube.",
            fix_steps=[
                "Kiểm tra DNS/proxy/firewall trên máy chạy API.",
                "Chạy lại verification command sau vài giây để phân biệt lỗi tạm thời.",
                "Nếu chạy trong container, kiểm tra container có outbound HTTPS hay không.",
            ],
            verify_steps=[
                "Bấm Verify source để thử lại trong cùng runtime của API.",
                "Kiểm tra log API và mã HTTP của YouTube nếu lỗi lặp lại.",
            ],
            source_type=source_type,
            source=source,
        )

    if "could not open" in text or "failed to open" in text:
        return _diagnostic(
            code="STREAM_OPEN_FAILED",
            severity="critical",
            title="Stream could not be opened",
            message="Backend đã tìm thấy nguồn nhưng không mở được luồng video.",
            cause="URL hết hạn, codec/FFmpeg không tương thích, hoặc camera không cho phép kết nối.",
            fix_steps=[
                "Kiểm tra URL/credentials và thử nguồn trực tiếp trên máy chạy API.",
                "Với YouTube, Verify source sẽ lấy lại URL HLS mới trước khi thử lại.",
                "Với RTSP, kiểm tra FFmpeg, firewall và thông tin đăng nhập camera.",
            ],
            verify_steps=[
                "Bấm Retry stream sau khi sửa nguồn.",
                "Theo dõi Input/Process/Output FPS; chỉ xem là hoạt động khi Process và Output > 0.",
            ],
            source_type=source_type,
            source=source,
        )

    return _diagnostic(
        code="STREAM_SOURCE_UNAVAILABLE",
        severity="critical",
        title="Camera source unavailable",
        message="Backend không đọc được frame từ camera source.",
        cause="Nguồn video đang offline, sai cấu hình hoặc đang trong quá trình reconnect.",
        fix_steps=[
            "Kiểm tra source và quyền truy cập trên đúng máy chạy API.",
            "Giữ cửa sổ Live Monitoring mở để pipeline tự reconnect.",
            "Nếu lỗi kéo dài, dùng Retry stream sau khi source đã hoạt động lại.",
        ],
        verify_steps=[
            "Xem status chuyển sang active và Process/Output FPS lớn hơn 0.",
            "Mở Alerts để kiểm tra alert đã được resolve sau khi có frame mới.",
        ],
        source_type=source_type,
        source=source,
    )

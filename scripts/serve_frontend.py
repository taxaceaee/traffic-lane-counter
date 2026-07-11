"""Serve the standalone frontend with runtime config injection."""

from __future__ import annotations

import http.server
import os
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "services" / "frontend"


def write_runtime_config() -> None:
    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
    config_path = FRONTEND_DIR / "config.js"
    config_path.write_text(
        "\n".join(
            [
                "window.__TRAFFICFLOW_CONFIG__ = {",
                f'    API_BASE_URL: "{api_base_url}",',
                "};",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> None:
    host = os.getenv("FRONTEND_HOST", "0.0.0.0")  # noqa: S104 - container default, configurable
    port = int(os.getenv("FRONTEND_PORT", "3000"))
    write_runtime_config()
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args,
        directory=str(FRONTEND_DIR),
        **kwargs,
    )
    with http.server.ThreadingHTTPServer((host, port), handler) as httpd:
        print(
            f"Serving frontend at http://{host}:{port} with API_BASE_URL={os.getenv('API_BASE_URL', 'http://localhost:8000')}",
            flush=True,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping frontend server...", flush=True)


if __name__ == "__main__":
    main()

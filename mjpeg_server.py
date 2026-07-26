"""
mjpeg_server.py — Lightweight MJPEG-over-HTTP server.

Serves a live video stream as multipart JPEG over HTTP.
Any web browser or VLC player can open the URL to view the stream.

Usage (standalone):
    Not typically run directly. Used by app.py when Internet Mode is enabled.

Architecture:
    - Runs a stdlib http.server on a background thread.
    - SenderBackend pushes JPEG bytes via update_frame().
    - All connected HTTP clients receive the MJPEG multipart stream.
"""

import io
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler


# Shared state: latest JPEG frame + condition variable for notification
_frame_lock = threading.Lock()
_frame_condition = threading.Condition(_frame_lock)
_latest_frame: bytes | None = None


def update_frame(jpeg_bytes: bytes) -> None:
    """Called by SenderBackend to push the latest JPEG frame."""
    global _latest_frame
    with _frame_condition:
        _latest_frame = jpeg_bytes
        _frame_condition.notify_all()


class _MJPEGHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for MJPEG stream and landing page."""

    def log_message(self, format, *args):
        # Suppress default stderr logging
        pass

    def do_GET(self):
        if self.path == "/stream":
            self._serve_mjpeg()
        else:
            self._serve_landing_page()

    def _serve_landing_page(self):
        """Serves a simple HTML page with the embedded video stream."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Video Stream</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0a0a0f;
            color: #e0e0e0;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
        }
        .container {
            text-align: center;
            max-width: 960px;
            width: 100%;
            padding: 20px;
        }
        h1 {
            font-size: 1.4rem;
            font-weight: 600;
            margin-bottom: 16px;
            color: #4ade80;
        }
        .dot {
            display: inline-block;
            width: 10px; height: 10px;
            background: #ef4444;
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 1.5s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }
        img {
            width: 100%;
            max-width: 900px;
            border-radius: 12px;
            border: 2px solid #1e293b;
            box-shadow: 0 8px 32px rgba(0,0,0,0.5);
        }
        .footer {
            margin-top: 16px;
            font-size: 0.8rem;
            color: #64748b;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1><span class="dot"></span>LIVE</h1>
        <img src="/stream" alt="Live Video Stream" />
        <p class="footer">Live Video Relay &mdash; powered by Cloudflare Tunnel</p>
    </div>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_mjpeg(self):
        """Serves an MJPEG multipart stream."""
        self.send_response(200)
        boundary = "frame"
        self.send_header(
            "Content-Type",
            f"multipart/x-mixed-replace; boundary={boundary}"
        )
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                with _frame_condition:
                    # Wait for a new frame (timeout to check connection)
                    _frame_condition.wait(timeout=2.0)
                    frame = _latest_frame

                if frame is None:
                    continue

                try:
                    self.wfile.write(f"--{boundary}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break

                # ~20 fps max for HTTP viewers
                time.sleep(0.05)
        except Exception:
            pass


class MJPEGServer:
    """
    Manages an MJPEG HTTP server on a background thread.

    Usage:
        server = MJPEGServer(port=8080)
        server.start()
        # ... push frames with update_frame(jpeg_bytes) ...
        server.stop()
    """

    def __init__(self, port: int = 8080):
        self.port = port
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._httpd = HTTPServer(("0.0.0.0", self.port), _MJPEGHandler)
        self._httpd.timeout = 1
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        if self._httpd:
            self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None

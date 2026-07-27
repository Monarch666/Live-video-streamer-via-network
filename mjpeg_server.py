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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


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
        html = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Video Relay - Multi-View Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0d1117;
            color: #c9d1d9;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
            min-height: 100vh;
            padding: 24px;
            display: flex;
            flex-direction: column;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #21262d;
            padding-bottom: 16px;
            margin-bottom: 24px;
            flex-shrink: 0;
        }
        .logo-section {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .logo {
            font-size: 1.5rem;
            font-weight: 700;
            color: #58a6ff;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .logo .pulse-dot {
            width: 10px;
            height: 10px;
            background: #3fb950;
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 8px #3fb950;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.2); opacity: 0.5; }
        }
        .subtitle {
            font-size: 0.85rem;
            color: #8b949e;
        }
        .controls-panel {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 24px;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            align-items: center;
            flex-shrink: 0;
        }
        .input-group {
            display: flex;
            gap: 8px;
            flex-grow: 1;
            min-width: 300px;
        }
        input {
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 6px;
            color: #c9d1d9;
            padding: 8px 12px;
            font-size: 0.9rem;
            outline: none;
            transition: border-color 0.2s;
        }
        input:focus {
            border-color: #58a6ff;
        }
        input.url-input {
            flex-grow: 2;
        }
        input.name-input {
            flex-grow: 1;
            max-width: 200px;
        }
        button {
            background: #21262d;
            border: 1px solid #30363d;
            border-radius: 6px;
            color: #c9d1d9;
            padding: 8px 16px;
            font-size: 0.9rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 6px;
            user-select: none;
        }
        button:hover {
            background: #30363d;
            border-color: #8b949e;
        }
        button.primary {
            background: #238636;
            border-color: #2ea043;
            color: #ffffff;
        }
        button.primary:hover {
            background: #2ea043;
            border-color: #3fb950;
        }
        .grid-container {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
            gap: 20px;
            flex-grow: 1;
        }
        .grid-container.single-item {
            grid-template-columns: minmax(300px, 800px);
            justify-content: center;
            align-content: start;
        }
        .feed-card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            position: relative;
            aspect-ratio: 16/9;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            transition: transform 0.2s ease, border-color 0.2s ease;
        }
        .feed-card:hover {
            border-color: #444c56;
        }
        .feed-header {
            padding: 8px 12px;
            background: #21262d;
            border-bottom: 1px solid #30363d;
            display: flex;
            justify-content: space-between;
            align-items: center;
            z-index: 10;
        }
        .feed-title-container {
            display: flex;
            align-items: center;
            gap: 8px;
            min-width: 0;
        }
        .feed-dot {
            width: 8px;
            height: 8px;
            background: #2ea043;
            border-radius: 50%;
            flex-shrink: 0;
        }
        .feed-title {
            font-size: 0.9rem;
            font-weight: 600;
            color: #f0f6fc;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .feed-actions {
            display: flex;
            gap: 6px;
        }
        .action-icon {
            background: none;
            border: none;
            padding: 4px 6px;
            color: #8b949e;
            cursor: pointer;
            border-radius: 4px;
            font-size: 0.85rem;
            transition: background-color 0.2s, color 0.2s;
        }
        .action-icon:hover {
            background: #30363d;
            color: #f0f6fc;
        }
        .action-icon.remove:hover {
            background: #da3633;
            color: #ffffff;
        }
        .feed-view {
            flex-grow: 1;
            background: #000;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            position: relative;
        }
        .feed-img {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }
        .feed-overlay {
            position: absolute;
            bottom: 8px;
            left: 8px;
            background: rgba(13, 17, 23, 0.75);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            color: #8b949e;
            pointer-events: none;
            border: 1px solid #30363d;
        }
        .feed-card.fullscreen {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            z-index: 9999;
            width: 100vw;
            height: 100vh;
            aspect-ratio: auto;
            border-radius: 0;
        }
        .footer {
            margin-top: 24px;
            font-size: 0.8rem;
            color: #484f58;
            text-align: center;
            flex-shrink: 0;
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-section">
            <div class="logo">
                <span class="pulse-dot"></span> Live Video Relay Dashboard
            </div>
            <div class="subtitle">Multi-View Streaming console</div>
        </div>
    </header>

    <div class="controls-panel">
        <div class="input-group">
            <input type="text" id="streamUrl" class="url-input" placeholder="Paste TryCloudflare or LAN stream URL (e.g. https://xxx.trycloudflare.com)" />
            <input type="text" id="streamName" class="name-input" placeholder="Camera Name (e.g. Side Gate)" />
        </div>
        <button class="primary" onclick="addStream()">➕ Add Feed</button>
    </div>

    <div id="grid" class="grid-container"></div>

    <p class="footer">Live Video Relay &mdash; powered by Cloudflare Tunnel</p>

    <script>
        let feeds = [];

        function initFeeds() {
            const saved = localStorage.getItem('mjpeg_feeds');
            if (saved) {
                try {
                    feeds = JSON.parse(saved);
                } catch(e) {
                    feeds = [];
                }
            }
            
            // Always ensure the primary feed (current host) is present in the list
            const currentOrigin = window.location.origin;
            const primaryUrl = currentOrigin + '/stream';
            const hasPrimary = feeds.some(f => f.url === primaryUrl || f.url === '/stream' || f.isPrimary);
            
            if (!hasPrimary) {
                // Remove any old primary feed before inserting the correct new one
                feeds = feeds.filter(f => !f.isPrimary);
                feeds.unshift({
                    url: '/stream',
                    name: 'Local Feed (This PC)',
                    isPrimary: true
                });
            }
            
            saveFeeds();
            renderGrid();
        }

        function saveFeeds() {
            localStorage.setItem('mjpeg_feeds', JSON.stringify(feeds));
        }

        function normalizeUrl(url) {
            url = url.trim();
            if (!url) return '';
            
            // If it's a local path, keep it
            if (url.startsWith('/')) return url;
            
            // Prepend https:// if no protocol is defined
            if (!/^https?:\/\//i.test(url)) {
                url = 'https://' + url;
            }
            
            // Automatically append /stream to Cloudflare URLs or clean domains if missing
            try {
                const parsed = new URL(url);
                if (parsed.pathname === '/' || parsed.pathname === '') {
                    parsed.pathname = '/stream';
                }
                return parsed.toString();
            } catch(e) {
                return url;
            }
        }

        function addStream() {
            const urlInput = document.getElementById('streamUrl');
            const nameInput = document.getElementById('streamName');
            
            const rawUrl = urlInput.value.trim();
            const rawName = nameInput.value.trim();
            
            if (!rawUrl) {
                alert('Please enter a stream URL');
                return;
            }
            
            const normalizedUrl = normalizeUrl(rawUrl);
            const name = rawName || `Camera ${feeds.length + 1}`;
            
            // Prevent duplicate URLs
            if (feeds.some(f => f.url === normalizedUrl)) {
                alert('This stream URL is already added.');
                return;
            }
            
            feeds.push({
                url: normalizedUrl,
                name: name,
                isPrimary: false
            });
            
            saveFeeds();
            renderGrid();
            
            // Clear inputs
            urlInput.value = '';
            nameInput.value = '';
        }

        function removeStream(index) {
            feeds.splice(index, 1);
            saveFeeds();
            renderGrid();
        }

        function toggleFullscreen(cardId) {
            const card = document.getElementById(cardId);
            if (card) {
                card.classList.toggle('fullscreen');
                const btn = card.querySelector('.fullscreen-btn');
                if (card.classList.contains('fullscreen')) {
                    btn.innerHTML = '🗗 Exit Fullscreen';
                } else {
                    btn.innerHTML = '🗖 Fullscreen';
                }
            }
        }

        function renderGrid() {
            const grid = document.getElementById('grid');
            grid.innerHTML = '';
            
            if (feeds.length === 1) {
                grid.classList.add('single-item');
            } else {
                grid.classList.remove('single-item');
            }
            
            feeds.forEach((feed, index) => {
                const cardId = `feed-card-${index}`;
                const card = document.createElement('div');
                card.className = 'feed-card';
                card.id = cardId;
                
                card.innerHTML = `
                    <div class="feed-header">
                        <div class="feed-title-container">
                            <span class="feed-dot"></span>
                            <span class="feed-title" title="${feed.name}">${feed.name}</span>
                        </div>
                        <div class="feed-actions">
                            <button class="action-icon fullscreen-btn" onclick="toggleFullscreen('${cardId}')">🗖 Fullscreen</button>
                            ${!feed.isPrimary ? `<button class="action-icon remove" onclick="removeStream(${index})">❌ Remove</button>` : ''}
                        </div>
                    </div>
                    <div class="feed-view">
                        <img class="feed-img" src="${feed.url}" alt="${feed.name}" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'640\\' height=\\'480\\' viewBox=\\'0 0 640 480\\'><rect width=\\'100%\\' height=\\'100%\\' fill=\\'%23000000\\'/><text x=\\'50%\\' y=\\'50%\\' font-family=\\'-apple-system,BlinkMacSystemFont,sans-serif\\' font-size=\\'1.2rem\\' fill=\\'%23da3633\\' text-anchor=\\'middle\\' dominant-baseline=\\'middle\\'>Feed Offline / Loading Failed</text></svg>';" />
                        <div class="feed-overlay">${feed.url}</div>
                    </div>
                `;
                grid.appendChild(card);
            });
        }

        // Initialize grid on load
        window.onload = initFeeds;
    </script>
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
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _MJPEGHandler)
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

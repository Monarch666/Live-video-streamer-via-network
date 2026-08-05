"""
app.py — Unified Live Video Relay Desktop Application
======================================================

Launch this SAME app on both PCs:

  PC1 (Camera PC): Click  "📷 Share My Camera"
                   → App shows your IP:PORT (e.g. 192.168.1.10:9000)
                   → Share that address with the viewer
                   → Your camera streams to anyone who connects

  PC2 (Viewer):    Click  "👁 Watch Stream"
                   → Type in the IP and Port shown on PC1
                   → Click Connect — live video appears immediately

Works on the same LAN with no extra setup.
For different networks, PC1 needs a public IP or port-forwarding
on the chosen port (default 9000).

Dependencies:
    pip install opencv-python Pillow websockets
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import Optional, Set

import cv2
import numpy as np
from PIL import Image, ImageTk
import websockets
import json

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from protocol import (
    pack_frame, read_frame, now_ms,
    MSG_REGISTER_RECEIVER, MSG_REGISTER_OK, MSG_REGISTER_FAIL,
    MSG_VIDEO_FRAME, MSG_PING, MSG_PONG,
    WRITE_BUFFER_LIMIT,
)
from cloudflared_tunnel import CloudflareTunnel
from mjpeg_server import MJPEGServer, update_frame as mjpeg_update_frame


# ═══════════════════════════════════════════════════════════════════
# THEME
# ═══════════════════════════════════════════════════════════════════

BG        = "#0d1117"       # main background
CARD      = "#161b22"       # card / frame background
PANEL     = "#21262d"       # inner panels
BORDER    = "#30363d"       # borders / dividers
ACCENT    = "#58a6ff"       # blue accent
ACCENT_H  = "#79c0ff"       # hover blue
GREEN     = "#3fb950"       # success / connected
RED       = "#f85149"       # error / disconnected
YELLOW    = "#e3b341"       # warning / waiting
FG        = "#e6edf3"       # primary text
FG2       = "#8b949e"       # secondary text
FG3       = "#484f58"       # muted text

_SYSTEM_FONT = "Segoe UI" if sys.platform == "win32" else (
    "SF Pro Display" if sys.platform == "darwin" else "Ubuntu"
)

def _font(size: int, weight: str = "normal") -> tuple:
    return (_SYSTEM_FONT, size, weight)


# ═══════════════════════════════════════════════════════════════════
# NETWORK HELPERS
# ═══════════════════════════════════════════════════════════════════

def get_local_ip() -> str:
    """Return the machine's LAN IP (best-effort)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ═══════════════════════════════════════════════════════════════════
# SENDER BACKEND (TCP server + webcam capture)
# ═══════════════════════════════════════════════════════════════════

class SenderBackend:
    def __init__(
        self,
        port: int = 9000,
        camera_index: int = 0,
        width: int = 640,
        height: int = 480,
        fps: int = 15,
        jpeg_quality: int = 70,
    ):
        self.port = port
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = jpeg_quality

        self.preview_queue: queue.Queue = queue.Queue(maxsize=2)
        self.status_queue: queue.Queue = queue.Queue()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="sender-asyncio"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and not self._loop.is_closed():
            if hasattr(self, "_main_task") and not self._main_task.done():
                self._loop.call_soon_threadsafe(self._main_task.cancel)
        if self._thread:
            self._thread.join(timeout=4)

    def _run_loop(self) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._main_task = self._loop.create_task(self._serve())
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.status_queue.put(("error", str(exc)))
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        cap = await loop.run_in_executor(None, self._open_camera)
        if cap is None:
            return

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cam"
        )
        receivers: Set[asyncio.StreamWriter] = set()
        viewers_lock = asyncio.Lock()

        async def handle_receiver(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                msg_type, _, _, _, _ = await asyncio.wait_for(read_frame(reader), timeout=5.0)
                if msg_type != MSG_REGISTER_RECEIVER:
                    return
                writer.write(pack_frame(MSG_REGISTER_OK, 1, 0, now_ms(), b""))
                await writer.drain()
                async with viewers_lock:
                    receivers.add(writer)
                    self.status_queue.put(("viewers", len(receivers)))
                while not self._stop_event.is_set():
                    try:
                        msg_type, _, _, _, _ = await asyncio.wait_for(read_frame(reader), timeout=20.0)
                        if msg_type == MSG_PING:
                            writer.write(pack_frame(MSG_PONG, 1, 0, now_ms(), b""))
                            await writer.drain()
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
            except Exception:
                pass
            finally:
                async with viewers_lock:
                    receivers.discard(writer)
                    self.status_queue.put(("viewers", len(receivers)))
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        try:
            server = await asyncio.start_server(handle_receiver, "0.0.0.0", self.port)
        except OSError as exc:
            self.status_queue.put(("error", f"Cannot bind port {self.port}: {exc}"))
            cap.release()
            return

        self.status_queue.put(("ready", None))
        frame_interval = 1.0 / self.fps
        seq_num = 0
        frames_in_window = 0
        window_start = loop.time()

        try:
            async with server:
                while not self._stop_event.is_set():
                    t0 = loop.time()
                    try:
                        ok, frame = await loop.run_in_executor(executor, cap.read)
                    except Exception:
                        break
                    if not ok or frame is None:
                        await asyncio.sleep(0.05)
                        continue
                    jpeg = await loop.run_in_executor(executor, self._encode_jpeg, frame)
                    if jpeg is None:
                        continue
                    try:
                        self.preview_queue.put_nowait(frame)
                    except queue.Full:
                        pass
                    mjpeg_update_frame(jpeg)
                    if receivers:
                        ts = now_ms()
                        try:
                            data = pack_frame(MSG_VIDEO_FRAME, 1, seq_num, ts, jpeg)
                        except ValueError:
                            seq_num = 0
                            data = pack_frame(MSG_VIDEO_FRAME, 1, seq_num, ts, jpeg)
                        seq_num = (seq_num + 1) & 0xFFFFFFFF
                        dead: Set[asyncio.StreamWriter] = set()
                        async with viewers_lock:
                            snapshot = set(receivers)
                        for w in snapshot:
                            try:
                                if w.transport.get_write_buffer_size() > WRITE_BUFFER_LIMIT:
                                    continue
                                w.write(data)
                                frames_in_window += 1
                            except Exception:
                                dead.add(w)
                        if dead:
                            async with viewers_lock:
                                receivers -= dead
                                self.status_queue.put(("viewers", len(receivers)))
                    now = loop.time()
                    if now - window_start >= 1.0:
                        fps_actual = frames_in_window / (now - window_start)
                        self.status_queue.put(("fps", round(fps_actual, 1)))
                        frames_in_window = 0
                        window_start = now
                    elapsed = loop.time() - t0
                    sleep_time = frame_interval - elapsed
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)
        finally:
            executor.shutdown(wait=True)
            cap.release()

    def _open_camera(self) -> Optional[cv2.VideoCapture]:
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.camera_index, backend)
        if not cap.isOpened():
            self.status_queue.put(("error", f"Camera {self.camera_index} not available."))
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return cap

    def _encode_jpeg(self, frame: np.ndarray) -> Optional[bytes]:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        return buf.tobytes() if ok else None


# ═══════════════════════════════════════════════════════════════════
# RECEIVER BACKEND (TCP client)
# ═══════════════════════════════════════════════════════════════════

class ReceiverBackend:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.frame_queue: queue.Queue = queue.Queue(maxsize=3)
        self.status_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="receiver-asyncio")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(self._loop)])
        if self._thread:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._main_task = self._loop.create_task(self._connect_and_receive())
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.status_queue.put(("error", str(exc)))
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _connect_and_receive(self) -> None:
        if "trycloudflare.com" in self.host or self.host.startswith("http"):
            await self._receive_mjpeg_http()
            return

        self.status_queue.put(("connecting", None))
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=5.0)
        except Exception as e:
            self.status_queue.put(("error", f"Connection failed: {e}"))
            return

        writer.write(pack_frame(MSG_REGISTER_RECEIVER, 0, 0, now_ms(), b"live"))
        await writer.drain()
        try:
            msg_type, _, _, _, payload = await asyncio.wait_for(read_frame(reader), timeout=5.0)
            if msg_type != MSG_REGISTER_OK:
                self.status_queue.put(("error", "Registration failed"))
                writer.close()
                return
        except Exception:
            self.status_queue.put(("error", "Handshake timeout"))
            writer.close()
            return

        self.status_queue.put(("connected", None))
        frames_in_window = 0
        window_start = time.time()
        latency_samples = []
        keepalive_task = asyncio.create_task(self._keepalive(writer))

        try:
            while not self._stop_event.is_set():
                try:
                    msg_type, _, _, ts, payload = await asyncio.wait_for(read_frame(reader), timeout=15.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.IncompleteReadError:
                    break

                if msg_type == MSG_VIDEO_FRAME:
                    latency_samples.append(now_ms() - ts)
                    arr = np.frombuffer(payload, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        try:
                            self.frame_queue.put_nowait(rgb)
                        except queue.Full:
                            pass
                        frames_in_window += 1

                    now = time.time()
                    if now - window_start >= 1.0:
                        fps = frames_in_window / (now - window_start)
                        avg_lat = sum(latency_samples) // len(latency_samples) if latency_samples else 0
                        self.status_queue.put(("fps", round(fps, 1)))
                        self.status_queue.put(("latency", avg_lat))
                        frames_in_window = 0
                        latency_samples.clear()
                        window_start = now
        finally:
            keepalive_task.cancel()
            writer.close()
            self.status_queue.put(("disconnected", None))

    async def _keepalive(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                writer.write(pack_frame(MSG_PING, 0, 0, now_ms(), b""))
                await writer.drain()
            except Exception:
                break

    async def _receive_mjpeg_http(self) -> None:
        self.status_queue.put(("connecting", None))
        url = self.host if self.host.startswith("http") else f"https://{self.host}"
        if "/video_feed" not in url: url = f"{url.rstrip('/')}/video_feed"
        
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        cap = await loop.run_in_executor(executor, lambda: cv2.VideoCapture(url))
        if not cap.isOpened():
            self.status_queue.put(("error", f"Cannot open stream: {url}"))
            return

        self.status_queue.put(("connected", None))
        frames_in_window = 0
        window_start = time.time()

        try:
            while not self._stop_event.is_set():
                ok, frame = await loop.run_in_executor(executor, cap.read)
                if not ok or frame is None:
                    await asyncio.sleep(0.01)
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                try: self.frame_queue.put_nowait(rgb)
                except queue.Full: pass
                frames_in_window += 1
                now = time.time()
                if now - window_start >= 1.0:
                    self.status_queue.put(("fps", round(frames_in_window / (now - window_start), 1)))
                    frames_in_window = 0
                    window_start = now
        finally:
            cap.release()
            self.status_queue.put(("disconnected", None))


# ═══════════════════════════════════════════════════════════════════
# GLOBAL BRIDGE BACKEND
# ═══════════════════════════════════════════════════════════════════

class GlobalBridgeBackend:
    def __init__(self, port: int = 9001):
        self.port = port
        self.frame_queue: queue.Queue = queue.Queue(maxsize=30)
        self.status_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tunnel: Optional[CloudflareTunnel] = None
        self._decode_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="bridge-decode"
        )

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="bridge-asyncio")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._tunnel: self._tunnel.stop()
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread: self._thread.join(timeout=2)
        self._decode_executor.shutdown(wait=False)

    @staticmethod
    def _decode_jpeg_to_rgb(raw_bytes: bytes):
        """Decode JPEG bytes to RGB numpy array — runs in thread pool."""
        arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        self.status_queue.put(("connecting", None))
        self._tunnel = CloudflareTunnel(local_port=self.port, protocol="http")
        self._tunnel.start()
        
        def poll_tunnel():
            try:
                event, val = self._tunnel.status_queue.get_nowait()
                if event == "connected":
                    url, _ = val
                    self.status_queue.put(("ready", url.replace("https://", "wss://")))
                elif event == "error":
                    self.status_queue.put(("error", f"Tunnel failed: {val}"))
            except queue.Empty: pass
            if not self._stop_event.is_set(): self._loop.call_later(0.5, poll_tunnel)

        self._loop.call_soon(poll_tunnel)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            self.status_queue.put(("error", str(e)))

    async def _serve(self) -> None:
        async with websockets.serve(
            self._handle,
            "0.0.0.0",
            self.port,
            max_size=2 * 1024 * 1024,      # 2 MB max frame
            compression=None,               # disable permessage-deflate
            ping_interval=None,             # no ping overhead
            ping_timeout=None,
        ):
            await asyncio.Future()

    async def _handle(self, ws):
        addr = ws.remote_address
        self.status_queue.put(("connected", f"{addr[0]}:{addr[1]}"))
        f_count = 0
        w_start = time.time()
        loop = asyncio.get_running_loop()
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    # Decode JPEG in thread pool so the event loop stays free
                    # to receive the next WebSocket message immediately
                    rgb = await loop.run_in_executor(
                        self._decode_executor,
                        self._decode_jpeg_to_rgb,
                        msg,
                    )
                    if rgb is not None:
                        # Always keep latest frame: drop oldest if full
                        if self.frame_queue.full():
                            try: self.frame_queue.get_nowait()
                            except queue.Empty: pass
                        try: self.frame_queue.put_nowait(rgb)
                        except queue.Full: pass
                        f_count += 1
                now = time.time()
                if now - w_start >= 1.0:
                    self.status_queue.put(("fps", round(f_count/(now-w_start), 1)))
                    f_count = 0; w_start = now
        except (websockets.exceptions.ConnectionClosed, asyncio.exceptions.IncompleteReadError):
            pass # Graceful cleanup for network drops
        except Exception as e:
            self.status_queue.put(("error", f"Bridge error: {str(e)[:50]}"))
        finally:
            self.status_queue.put(("disconnected", None))


# ═══════════════════════════════════════════════════════════════════
# UI WIDGETS
# ═══════════════════════════════════════════════════════════════════

class RoundedButton(tk.Button):
    def __init__(self, parent, text="", command=None, bg_color=ACCENT, fg_color=BG, hover_color=ACCENT_H, font_spec=None, **kwargs):
        for key in ("bd", "highlightthickness", "width", "height", "radius"): kwargs.pop(key, None)
        super().__init__(parent, text=text, command=command, bg=bg_color, fg=fg_color, font=font_spec or _font(12, "bold"), relief=tk.FLAT, bd=0, highlightthickness=0, cursor="hand2", activebackground=hover_color, activeforeground=fg_color, padx=14, pady=7, **kwargs)
        self._bg = bg_color; self._hover = hover_color
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))
    def configure_text(self, text: str) -> None: self.configure(text=text)

class StatusDot(tk.Label):
    _COLORS = {"ok": GREEN, "waiting": YELLOW, "error": RED, "idle": FG3}
    def __init__(self, parent, size=10, **kwargs):
        for key in ("bd", "highlightthickness"): kwargs.pop(key, None)
        super().__init__(parent, text="●", font=(_SYSTEM_FONT, size), bg=CARD, bd=0, **kwargs)
        self.set("idle")
    def set(self, state): self.configure(fg=self._COLORS.get(state, FG3))

class VideoCanvas(tk.Label):
    def __init__(self, parent, bg="#000000", **kwargs):
        super().__init__(parent, bg=bg, bd=0, **kwargs)
        self._photo = None; self.show_placeholder()
    def show_frame(self, rgb):
        try:
            w = self.winfo_width()
            h = self.winfo_height() or int(w * 3 / 4)
            if w > 10 and h > 10:
                # cv2.resize with INTER_NEAREST is ~50x faster than PIL.resize
                rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_NEAREST)
            # Direct fromarray → PhotoImage, no PIL resize step
            self._photo = ImageTk.PhotoImage(Image.fromarray(rgb), master=self)
            self.configure(image=self._photo)
        except Exception: pass
    def show_placeholder(self, msg="Waiting for stream…"):
        img = Image.new("RGB", (640, 400), "#0a0a0a")
        from PIL import ImageDraw; ImageDraw.Draw(img).text((320, 200), msg, fill="#484f58", anchor="mm")
        self._photo = ImageTk.PhotoImage(img, master=self)
        self.configure(image=self._photo)


# ═══════════════════════════════════════════════════════════════════
# SCREENS
# ═══════════════════════════════════════════════════════════════════

class HomeScreen(tk.Frame):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self._app = app; self._build()
    def _build(self):
        header = tk.Frame(self, bg=BG); header.pack(pady=(60, 0))
        tk.Label(header, text="📡 Live Video Relay", font=_font(30, "bold"), fg=FG, bg=BG).pack()
        tk.Label(header, text="Direct camera sharing over your network", font=_font(13), fg=FG2, bg=BG).pack(pady=6)
        row = tk.Frame(self, bg=BG); row.pack(pady=40)
        self._card(row, "📷", "Share Camera", "Stream webcam", "Start Sharing", GREEN, "#56d364", self._app.show_sender).pack(side=tk.LEFT, padx=12)
        self._card(row, "👁", "Watch Stream", "Connect or Host", "Connect", ACCENT, ACCENT_H, self._app.show_receiver).pack(side=tk.LEFT, padx=12)
        self._card(row, "🖥️", "Multi-Bridge Grid", "4-Camera Command Center", "Open Grid", YELLOW, "#f0c674", self._app.show_multibridge).pack(side=tk.LEFT, padx=12)
    def _card(self, p, e, t, s, bt, bc, bh, cmd):
        f = tk.Frame(p, bg=CARD, width=260, height=310); f.pack_propagate(False)
        tk.Label(f, text=e, font=_font(48), bg=CARD).pack(pady=(30, 4))
        tk.Label(f, text=t, font=_font(16, "bold"), bg=CARD, fg=FG).pack()
        tk.Label(f, text=s, font=_font(11), bg=CARD, fg=FG2).pack(pady=(6, 20))
        RoundedButton(f, text=bt, command=cmd, bg_color=bc, hover_color=bh, fg_color="#fff").pack()
        return f

def _sep(p, orient="h", color=BORDER, thickness=1, **kwargs):
    tk.Frame(p, bg=color, height=thickness if orient=="h" else None, width=thickness if orient=="v" else None).pack(**kwargs)

class SenderScreen(tk.Frame):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self._app = app; self._running = False; self._backend = None; self._tunnel = None; self._mjpeg = None
        self._public_url = ""
        self._build()
    def _build(self):
        bar = tk.Frame(self, bg=CARD, height=52); bar.pack(fill=tk.X); bar.pack_propagate(False)
        RoundedButton(bar, text="← Back", command=self._back, bg_color=PANEL, hover_color=BORDER, fg_color=FG).pack(side=tk.LEFT, padx=16)
        tk.Label(bar, text="📷 Share My Camera", font=_font(14, "bold"), fg=FG, bg=CARD).pack(side=tk.LEFT)
        self._dot = StatusDot(bar, size=12); self._dot.pack(side=tk.LEFT, padx=8)
        self._status_lbl = tk.Label(bar, text="Ready", font=_font(11), fg=FG2, bg=CARD); self._status_lbl.pack(side=tk.LEFT)
        self._copy_btn = RoundedButton(bar, text="📋 Copy Link", command=self._copy_public_url, bg_color=PANEL, hover_color=BORDER, fg_color=ACCENT)
        self._copy_btn.pack(side=tk.RIGHT, padx=16)
        self._copy_btn.pack_forget() # Show only when tunnel URL is ready

        _sep(self, fill=tk.X)
        body = tk.Frame(self, bg=BG); body.pack(fill=tk.BOTH, expand=True)
        self._video = VideoCanvas(body); self._video.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right = tk.Frame(body, bg=CARD, width=280); right.pack(side=tk.RIGHT, fill=tk.Y); right.pack_propagate(False)
        addr = tk.Frame(right, bg=CARD); addr.pack(fill=tk.X, padx=20, pady=20)
        self._local_ip = get_local_ip()
        tk.Label(addr, text="Local IP: " + self._local_ip, font=_font(13, "bold"), fg=ACCENT, bg=CARD).pack()
        self._port_var = tk.StringVar(value="9000"); tk.Entry(addr, textvariable=self._port_var, width=6).pack(pady=5)
        self._start_btn = RoundedButton(right, text="▶ Start", command=self._start, bg_color=GREEN, fg_color="#fff"); self._start_btn.pack(pady=10)
        self._internet_var = tk.BooleanVar(value=True)
        tk.Checkbutton(right, text="Internet Mode", variable=self._internet_var, bg=CARD, fg=FG, selectcolor=BG).pack()
        self._v_val = tk.Label(right, text="Viewers: 0", bg=CARD, fg=FG2); self._v_val.pack()
        self._f_val = tk.Label(right, text="FPS: 0", bg=CARD, fg=FG2); self._f_val.pack()

    def _copy_public_url(self):
        if self._public_url:
            self.clipboard_clear()
            self.clipboard_append(self._public_url)
            self.update()
            messagebox.showinfo("Link Copied!", f"Public Internet Stream Link copied to clipboard:\n\n{self._public_url}\n\nPaste this in any browser worldwide to watch!")

    def _start(self):
        if self._running: return
        self._backend = SenderBackend(port=int(self._port_var.get()))
        self._backend.start(); self._running = True
        self._start_btn.configure_text("● Streaming"); self._dot.set("waiting")
        if self._internet_var.get():
            self._mjpeg = MJPEGServer(port=8080); self._mjpeg.start()
            self._tunnel = CloudflareTunnel(local_port=8080); self._tunnel.start()
        self._poll()

    def _poll(self):
        if not self._running: return
        try:
            while True:
                ev, val = self._backend.status_queue.get_nowait()
                if ev == "ready": self._dot.set("ok"); self._status_lbl.config(text="Live")
                elif ev == "viewers": self._v_val.config(text=f"Viewers: {val}")
                elif ev == "fps": self._f_val.config(text=f"FPS: {val}")
        except queue.Empty: pass
        try:
            frame = self._backend.preview_queue.get_nowait()
            self._video.show_frame(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except queue.Empty: pass
        if self._tunnel:
            try:
                ev, val = self._tunnel.status_queue.get_nowait()
                if ev == "connected":
                    self._public_url = val[0]
                    self._status_lbl.config(text=f"Public: {val[0]}")
                    self._copy_btn.pack(side=tk.RIGHT, padx=16)
            except queue.Empty: pass
        self.after(30, self._poll)

    def _back(self):
        self._running = False
        self._public_url = ""
        if self._backend: self._backend.stop()
        if self._tunnel: self._tunnel.stop()
        if self._mjpeg: self._mjpeg.stop()
        self._app.show_home()

class ReceiverScreen(tk.Frame):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self._app = app; self._connected = False; self._be = None; self._bridge_be = None
        self._mode_var = tk.StringVar(value="lan")
        self._bridge_url = ""
        self._build()

    def _build(self):
        bar = tk.Frame(self, bg=CARD, height=52); bar.pack(fill=tk.X); bar.pack_propagate(False)
        RoundedButton(bar, text="← Back", command=self._back, bg_color=PANEL, hover_color=BORDER, fg_color=FG).pack(side=tk.LEFT, padx=16)
        tk.Label(bar, text="👁 Watch Stream", font=_font(14, "bold"), fg=FG, bg=CARD).pack(side=tk.LEFT)
        self._dot = StatusDot(bar, size=12); self._dot.pack(side=tk.LEFT, padx=8)
        self._status_lbl = tk.Label(bar, text="Idle", font=_font(11), fg=FG2, bg=CARD); self._status_lbl.pack(side=tk.LEFT)
        _sep(self, fill=tk.X)
        
        mode_f = tk.Frame(self, bg=CARD); mode_f.pack(fill=tk.X, padx=20, pady=5)
        tk.Radiobutton(mode_f, text="LAN Mode", variable=self._mode_var, value="lan", bg=CARD, fg=FG, selectcolor=BG, command=self._on_mode).pack(side=tk.LEFT)
        tk.Radiobutton(mode_f, text="Global Bridge", variable=self._mode_var, value="bridge", bg=CARD, fg=FG, selectcolor=BG, command=self._on_mode).pack(side=tk.LEFT, padx=20)
        
        self._lan_f = tk.Frame(self, bg=CARD); self._lan_f.pack(fill=tk.X, padx=20)
        self._ip_v = tk.StringVar(); tk.Entry(self._lan_f, textvariable=self._ip_v, width=20).pack(side=tk.LEFT)
        self._port_v = tk.StringVar(value="9000"); tk.Entry(self._lan_f, textvariable=self._port_v, width=6).pack(side=tk.LEFT, padx=5)
        self._conn_btn = RoundedButton(self._lan_f, text="Connect", command=self._connect); self._conn_btn.pack(side=tk.LEFT)

        self._bridge_f = tk.Frame(self, bg=CARD); self._bridge_f.pack_forget()
        tk.Label(self._bridge_f, text="Bridge Port:", bg=CARD, fg=FG2, font=_font(10)).pack(side=tk.LEFT, padx=(0, 4))
        self._bridge_port_v = tk.StringVar(value="9001")
        tk.Entry(self._bridge_f, textvariable=self._bridge_port_v, width=6).pack(side=tk.LEFT, padx=5)
        self._bridge_btn = RoundedButton(self._bridge_f, text="Host Bridge", command=self._host_bridge); self._bridge_btn.pack(side=tk.LEFT)
        self._url_lbl = tk.Label(self._bridge_f, text="", fg=ACCENT, bg=CARD, font=_font(10, "bold")); self._url_lbl.pack(side=tk.LEFT, padx=10)
        self._copy_bridge_btn = RoundedButton(self._bridge_f, text="📋 Copy Bridge Link", command=self._copy_bridge_url, bg_color=PANEL, hover_color=BORDER, fg_color=ACCENT)
        self._copy_bridge_btn.pack(side=tk.LEFT, padx=10)
        self._copy_bridge_btn.pack_forget()

        self._video = VideoCanvas(self); self._video.pack(fill=tk.BOTH, expand=True)

    def _copy_bridge_url(self):
        if self._bridge_url:
            self.clipboard_clear()
            self.clipboard_append(self._bridge_url)
            self.update()
            messagebox.showinfo("Link Copied!", f"Global Bridge WebSocket URL copied to clipboard:\n\n{self._bridge_url}\n\nPaste this into your Android phone app to send video from anywhere!")

    def _on_mode(self):
        if self._mode_var.get() == "lan": self._lan_f.pack(fill=tk.X, padx=20); self._bridge_f.pack_forget()
        else: self._bridge_f.pack(fill=tk.X, padx=20); self._lan_f.pack_forget()

    def _connect(self):
        if self._connected: return
        self._be = ReceiverBackend(self._ip_v.get(), int(self._port_v.get()))
        self._be.start(); self._connected = True; self._poll()

    def _host_bridge(self):
        if self._connected: return
        try:
            port = int(self._bridge_port_v.get())
        except ValueError:
            port = 9001
        self._bridge_be = GlobalBridgeBackend(port=port)
        self._bridge_be.start(); self._connected = True; self._poll()

    def _poll(self):
        if not self._connected: return
        be = self._be or self._bridge_be
        if not be: return
        try:
            while True:
                ev, val = be.status_queue.get_nowait()
                if ev == "connected": self._dot.set("ok"); self._status_lbl.config(text="Connected")
                elif ev == "ready":
                    self._bridge_url = val
                    self._url_lbl.config(text=val)
                    self._copy_bridge_btn.pack(side=tk.LEFT, padx=10)
                elif ev == "fps": self._status_lbl.config(text=f"Receiving @ {val} FPS")
        except queue.Empty: pass
        latest_frame = None
        while True:
            try:
                latest_frame = be.frame_queue.get_nowait()
            except queue.Empty:
                break
        if latest_frame is not None:
            self._video.show_frame(latest_frame)
        self.after(15, self._poll)

    def _back(self):
        self._connected = False
        if self._be: self._be.stop(); self._be = None
        if self._bridge_be: self._bridge_be.stop(); self._bridge_be = None
        self._app.show_home()


# ═══════════════════════════════════════════════════════════════════
# MULTI-BRIDGE COMMAND CENTER (4-CAMERA PC GRID)
# ═══════════════════════════════════════════════════════════════════

class MultiBridgeCell(tk.Frame):
    def __init__(self, parent, title="Camera Feed", default_port=9001):
        super().__init__(parent, bg=CARD, bd=1, relief=tk.SOLID)
        self.port = default_port
        self.backend: Optional[GlobalBridgeBackend] = None
        self.bridge_url = ""
        self.running = False
        
        # Header
        top = tk.Frame(self, bg=PANEL, height=36); top.pack(fill=tk.X); top.pack_propagate(False)
        self.dot = StatusDot(top, size=10); self.dot.pack(side=tk.LEFT, padx=8)
        tk.Label(top, text=title, font=_font(11, "bold"), fg=FG, bg=PANEL).pack(side=tk.LEFT)
        
        tk.Label(top, text="Port:", font=_font(9), fg=FG2, bg=PANEL).pack(side=tk.LEFT, padx=(10, 2))
        self.port_var = tk.StringVar(value=str(default_port))
        tk.Entry(top, textvariable=self.port_var, width=5).pack(side=tk.LEFT)

        self.btn_host = RoundedButton(top, text="Host", command=self.toggle_host, bg_color=GREEN, fg_color="#fff")
        self.btn_host.pack(side=tk.LEFT, padx=6)

        self.url_lbl = tk.Label(top, text="", fg=ACCENT, bg=PANEL, font=_font(9, "bold"))
        self.url_lbl.pack(side=tk.LEFT, padx=4)

        self.btn_copy = RoundedButton(top, text="📋 Copy", command=self.copy_url, bg_color=CARD, hover_color=BORDER, fg_color=ACCENT)
        self.btn_copy.pack(side=tk.LEFT)
        self.btn_copy.pack_forget()

        self.fps_lbl = tk.Label(top, text="Idle", font=_font(9), fg=FG2, bg=PANEL)
        self.fps_lbl.pack(side=tk.RIGHT, padx=8)

        # Video Canvas
        self.video = VideoCanvas(self)
        self.video.pack(fill=tk.BOTH, expand=True)

    def toggle_host(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        try:
            p = int(self.port_var.get())
        except ValueError:
            p = self.port
        self.backend = GlobalBridgeBackend(port=p)
        self.backend.start()
        self.running = True
        self.btn_host.configure_text("Stop")
        self.btn_host.configure(bg=RED)
        self.dot.set("waiting")
        self.poll()

    def stop(self):
        self.running = False
        if self.backend:
            self.backend.stop()
            self.backend = None
        self.btn_host.configure_text("Host")
        self.btn_host.configure(bg=GREEN)
        self.btn_copy.pack_forget()
        self.url_lbl.config(text="")
        self.dot.set("idle")
        self.fps_lbl.config(text="Idle")

    def copy_url(self):
        if self.bridge_url:
            self.clipboard_clear()
            self.clipboard_append(self.bridge_url)
            self.update()
            messagebox.showinfo("Link Copied!", f"Global Bridge Link copied:\n\n{self.bridge_url}\n\nPaste this in your phone app!")

    def poll(self):
        if not self.running or not self.backend: return
        try:
            while True:
                ev, val = self.backend.status_queue.get_nowait()
                if ev == "connected":
                    self.dot.set("ok")
                elif ev == "ready":
                    self.bridge_url = val
                    self.url_lbl.config(text=val)
                    self.btn_copy.pack(side=tk.LEFT, padx=4)
                elif ev == "fps":
                    self.fps_lbl.config(text=f"{val} FPS")
                elif ev == "error":
                    self.dot.set("error")
                    self.fps_lbl.config(text="Error")
        except queue.Empty: pass

        latest_frame = None
        while True:
            try:
                latest_frame = self.backend.frame_queue.get_nowait()
            except queue.Empty:
                break
        if latest_frame is not None:
            self.video.show_frame(latest_frame)

        self.after(15, self.poll)


class MultiBridgeScreen(tk.Frame):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self._app = app
        self._cells = []
        self._build()

    def _build(self):
        bar = tk.Frame(self, bg=CARD, height=52); bar.pack(fill=tk.X); bar.pack_propagate(False)
        RoundedButton(bar, text="← Back", command=self._back, bg_color=PANEL, hover_color=BORDER, fg_color=FG).pack(side=tk.LEFT, padx=16)
        tk.Label(bar, text="🖥️ Multi-Bridge Command Center (4-Camera Grid)", font=_font(14, "bold"), fg=FG, bg=CARD).pack(side=tk.LEFT)
        _sep(self, fill=tk.X)

        grid_frame = tk.Frame(self, bg=BG); grid_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        grid_frame.columnconfigure(0, weight=1); grid_frame.columnconfigure(1, weight=1)
        grid_frame.rowconfigure(0, weight=1); grid_frame.rowconfigure(1, weight=1)

        ports = [9001, 9002, 9003, 9004]
        for idx in range(4):
            r, c = divmod(idx, 2)
            cell = MultiBridgeCell(grid_frame, title=f"Camera {idx+1}", default_port=ports[idx])
            cell.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
            self._cells.append(cell)

    def _back(self):
        for cell in self._cells:
            cell.stop()
        self._app.show_home()


class VideoRelayApp(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("Live Video Relay"); self.geometry("960x640"); self.configure(bg=BG)
        self._screen = None; self.show_home()
    def _switch(self, s):
        if self._screen: self._screen.destroy()
        self._screen = s; s.pack(fill=tk.BOTH, expand=True)
    def show_home(self): self._switch(HomeScreen(self))
    def show_sender(self): self._switch(SenderScreen(self))
    def show_receiver(self): self._switch(ReceiverScreen(self))
    def show_multibridge(self): self._switch(MultiBridgeScreen(self))

if __name__ == "__main__": VideoRelayApp().mainloop()

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
    pip install opencv-python Pillow
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

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from protocol import (
    pack_frame, read_frame, now_ms,
    MSG_REGISTER_RECEIVER, MSG_REGISTER_OK, MSG_REGISTER_FAIL,
    MSG_VIDEO_FRAME, MSG_PING, MSG_PONG,
    WRITE_BUFFER_LIMIT,
)


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
# SENDER BACKEND  (TCP server + webcam capture)
# ═══════════════════════════════════════════════════════════════════

class SenderBackend:
    """
    Runs a TCP server on a background asyncio thread.
    Captures webcam frames, broadcasts to all connected viewers, and
    pushes preview frames to preview_queue for the UI to display.

    Status events posted to status_queue:
        ("ready",   None)          — server is up
        ("error",   message:str)   — fatal error
        ("viewers", count:int)     — viewer count changed
        ("fps",     fps:float)     — current send FPS (every second)
    """

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

    # ── public interface ───────────────────────────────────────────

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

    # ── internal ───────────────────────────────────────────────────

    def _run_loop(self) -> None:
        # Windows requires ProactorEventLoop for asyncio TCP servers in threads.
        # On Linux/macOS the default SelectorEventLoop is used automatically.
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

        # Open webcam
        cap = await loop.run_in_executor(None, self._open_camera)
        if cap is None:
            return

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cam"
        )

        # Set of connected receiver writers
        receivers: Set[asyncio.StreamWriter] = set()
        viewers_lock = asyncio.Lock()

        # ── per-receiver connection handler ────────────────────────
        async def handle_receiver(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            addr = writer.get_extra_info("peername", ("?", "?"))
            try:
                # Expect REGISTER_RECEIVER
                msg_type, _, _, _, _ = await asyncio.wait_for(
                    read_frame(reader), timeout=5.0
                )
                if msg_type != MSG_REGISTER_RECEIVER:
                    return
                # Acknowledge
                writer.write(pack_frame(MSG_REGISTER_OK, 1, 0, now_ms(), b""))
                await writer.drain()

                async with viewers_lock:
                    receivers.add(writer)
                    self.status_queue.put(("viewers", len(receivers)))

                # Stay alive — handle PING while camera loop sends frames
                while not self._stop_event.is_set():
                    try:
                        msg_type, _, _, _, _ = await asyncio.wait_for(
                            read_frame(reader), timeout=20.0
                        )
                        if msg_type == MSG_PING:
                            writer.write(
                                pack_frame(MSG_PONG, 1, 0, now_ms(), b"")
                            )
                            await writer.drain()
                    except asyncio.TimeoutError:
                        continue  # keepalive grace period
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

        # ── start TCP server ───────────────────────────────────────
        try:
            server = await asyncio.start_server(
                handle_receiver, "0.0.0.0", self.port
            )
        except OSError as exc:
            self.status_queue.put(("error", f"Cannot bind port {self.port}: {exc}"))
            cap.release()
            return

        self.status_queue.put(("ready", None))

        # ── camera capture + broadcast loop ───────────────────────
        frame_interval = 1.0 / self.fps
        seq_num = 0
        frames_in_window = 0
        window_start = loop.time()

        try:
            async with server:
                while not self._stop_event.is_set():
                    t0 = loop.time()

                    # Blocking camera read — offloaded to thread
                    try:
                        ok, frame = await loop.run_in_executor(executor, cap.read)
                    except Exception:
                        break

                    if not ok or frame is None:
                        await asyncio.sleep(0.05)
                        continue

                    # JPEG encode (CPU-bound — offloaded)
                    jpeg = await loop.run_in_executor(
                        executor, self._encode_jpeg, frame
                    )
                    if jpeg is None:
                        continue

                    # Push preview to UI queue (non-blocking, drop if full)
                    try:
                        self.preview_queue.put_nowait(frame)
                    except queue.Full:
                        pass

                    # Broadcast to all connected receivers
                    if receivers:
                        ts = now_ms()
                        try:
                            # TODO(security): encrypt jpeg here before packing
                            data = pack_frame(
                                MSG_VIDEO_FRAME, 1, seq_num, ts, jpeg
                            )
                        except ValueError:
                            # Payload exceeded MAX_PAYLOAD. This used to raise
                            # uncaught and kill the entire backend thread (camera
                            # closed, "Sender Error" dialog). Now we just skip
                            # this one frame and keep streaming.
                            seq_num = (seq_num + 1) & 0xFFFFFFFF
                            continue
                        seq_num = (seq_num + 1) & 0xFFFFFFFF

                        dead: Set[asyncio.StreamWriter] = set()
                        async with viewers_lock:
                            snapshot = set(receivers)

                        for w in snapshot:
                            try:
                                buf = w.transport.get_write_buffer_size()
                            except Exception:
                                buf = 0
                            if buf > WRITE_BUFFER_LIMIT:
                                continue
                            try:
                                w.write(data)
                                frames_in_window += 1
                            except Exception:
                                dead.add(w)

                        if dead:
                            async with viewers_lock:
                                receivers -= dead
                                self.status_queue.put(("viewers", len(receivers)))

                    # Report FPS every second
                    now = loop.time()
                    if now - window_start >= 1.0:
                        fps_actual = frames_in_window / (now - window_start)
                        self.status_queue.put(("fps", round(fps_actual, 1)))
                        frames_in_window = 0
                        window_start = now

                    # Pace to target FPS
                    elapsed = loop.time() - t0
                    sleep_time = frame_interval - elapsed
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)

        finally:
            cap.release()
            executor.shutdown(wait=False)

    def _open_camera(self) -> Optional[cv2.VideoCapture]:
        # On Windows, DirectShow (CAP_DSHOW) opens cameras faster and more
        # reliably than the default MSMF backend.  CAP_DSHOW is a no-op on
        # Linux/macOS where OpenCV falls back to V4L2 / AVFoundation.
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.camera_index, backend)
        if not cap.isOpened():
            self.status_queue.put((
                "error",
                f"Camera index {self.camera_index} not available.\n"
                "Check that a webcam is connected and not in use by another app."
            ))
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return cap

    def _encode_jpeg(self, frame: np.ndarray) -> Optional[bytes]:
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        )
        return buf.tobytes() if ok else None


# ═══════════════════════════════════════════════════════════════════
# RECEIVER BACKEND  (TCP client)
# ═══════════════════════════════════════════════════════════════════

class ReceiverBackend:
    """
    Connects to a SenderBackend server in a background asyncio thread.
    Pushes decoded frames to frame_queue for the UI to display.

    Status events:
        ("connecting",  None)
        ("connected",   None)
        ("error",       message:str)
        ("disconnected",None)
        ("fps",         fps:float)
        ("latency",     ms:int)
    """

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
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="receiver-asyncio"
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
        # Same Windows ProactorEventLoop requirement as SenderBackend.
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
        self.status_queue.put(("connecting", None))
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5.0
            )
        except asyncio.TimeoutError:
            self.status_queue.put(("error", f"Connection timed out ({self.host}:{self.port})"))
            return
        except (ConnectionRefusedError, OSError) as exc:
            self.status_queue.put(("error", f"Cannot connect: {exc}"))
            return

        # Register as receiver
        stream_name = "live"
        writer.write(
            pack_frame(MSG_REGISTER_RECEIVER, 0, 0, now_ms(), stream_name.encode())
        )
        await writer.drain()

        try:
            msg_type, _, _, _, payload = await asyncio.wait_for(
                read_frame(reader), timeout=5.0
            )
        except asyncio.TimeoutError:
            self.status_queue.put(("error", "No response from sender"))
            writer.close()
            return

        if msg_type == MSG_REGISTER_FAIL:
            reason = payload.decode("utf-8", errors="replace")
            self.status_queue.put(("error", f"Sender refused: {reason}"))
            writer.close()
            return
        if msg_type != MSG_REGISTER_OK:
            self.status_queue.put(("error", f"Unexpected response: 0x{msg_type:02X}"))
            writer.close()
            return

        self.status_queue.put(("connected", None))

        # Receive loop
        frames_in_window = 0
        window_start = asyncio.get_running_loop().time()
        latency_samples: list[int] = []

        keepalive_task = asyncio.create_task(self._keepalive(writer))

        try:
            while not self._stop_event.is_set():
                try:
                    msg_type, _, _, timestamp_ms, payload = await asyncio.wait_for(
                        read_frame(reader), timeout=15.0
                    )
                except asyncio.TimeoutError:
                    continue
                except asyncio.IncompleteReadError:
                    break

                if msg_type == MSG_VIDEO_FRAME:
                    local_ms = now_ms()
                    latency = local_ms - timestamp_ms
                    latency_samples.append(latency)

                    # TODO(security): decrypt payload here before decoding
                    arr = np.frombuffer(payload, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        # Convert BGR→RGB for PIL
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        try:
                            self.frame_queue.put_nowait(rgb)
                        except queue.Full:
                            pass
                        frames_in_window += 1

                    now = asyncio.get_running_loop().time()
                    if now - window_start >= 1.0:
                        fps_actual = frames_in_window / (now - window_start)
                        avg_lat = sum(latency_samples) // len(latency_samples) if latency_samples else 0
                        self.status_queue.put(("fps", round(fps_actual, 1)))
                        self.status_queue.put(("latency", avg_lat))
                        frames_in_window = 0
                        latency_samples.clear()
                        window_start = now

                elif msg_type == MSG_PONG:
                    pass

        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self.status_queue.put(("disconnected", None))

    async def _keepalive(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                writer.write(pack_frame(MSG_PING, 0, 0, now_ms(), b""))
                await writer.drain()
            except Exception:
                break


# ═══════════════════════════════════════════════════════════════════
# UI WIDGETS  (reusable building blocks)
# ═══════════════════════════════════════════════════════════════════

class RoundedButton(tk.Button):
    """
    A styled flat button with hover effect.

    Drop-in replacement for the old Canvas-based version.  Uses tk.Button
    which is fully initialized before any method is called, avoiding the
    Python 3.14 TclError: invalid command name bug that affects Canvas
    subclasses when draw() is called inside __init__.

    API is identical: configure_text(text) updates the label.
    The `width`, `height`, and `radius` parameters are accepted but ignored
    so call sites don't need to change.
    """

    def __init__(
        self,
        parent,
        text: str = "",
        command=None,
        bg_color: str = ACCENT,
        fg_color: str = BG,
        hover_color: str = ACCENT_H,
        font_spec: tuple = None,
        width: int = 160,    # kept for API compat
        height: int = 44,   # kept for API compat
        radius: int = 10,   # kept for API compat
        **kwargs,
    ):
        # Strip Canvas-specific kwargs that tk.Button doesn't accept
        for key in ("bd", "highlightthickness"):
            kwargs.pop(key, None)

        super().__init__(
            parent,
            text=text,
            command=command,
            bg=bg_color,
            fg=fg_color,
            font=font_spec or _font(12, "bold"),
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            activebackground=hover_color,
            activeforeground=fg_color,
            padx=14,
            pady=7,
            **kwargs,
        )
        self._bg    = bg_color
        self._hover = hover_color
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))

    def configure_text(self, text: str) -> None:
        """Update the button label (mirrors old Canvas API)."""
        self.configure(text=text)


class StatusDot(tk.Label):
    """
    A colored filled-circle indicator showing connection state.

    Uses a Unicode bullet (●) coloured via `fg` instead of Canvas drawing,
    avoiding the Python 3.14 TclError that affects Canvas.delete() called
    inside __init__.
    """

    _COLORS = {
        "ok":      GREEN,
        "waiting": YELLOW,
        "error":   RED,
        "idle":    FG3,
    }

    def __init__(self, parent, size: int = 10, **kwargs):
        # Remove Canvas-only kwargs that tk.Label doesn't accept
        for key in ("bd", "highlightthickness"):
            kwargs.pop(key, None)
        super().__init__(
            parent,
            text="●",
            font=(_SYSTEM_FONT, size),
            bg=CARD,
            bd=0,
            **kwargs,
        )
        self.set("idle")

    def set(self, state: str) -> None:
        color = self._COLORS.get(state, FG3)
        self.configure(fg=color)


class VideoCanvas(tk.Label):
    """A Label that displays PIL Images — used for video frames."""

    def __init__(self, parent, bg: str = "#000000", **kwargs):
        super().__init__(parent, bg=bg, bd=0, **kwargs)
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._placeholder: Optional[ImageTk.PhotoImage] = None
        self._build_placeholder()
        self.configure(image=self._placeholder)

    def _build_placeholder(self) -> None:
        img = Image.new("RGB", (640, 480), "#0a0a0a")
        self._placeholder = ImageTk.PhotoImage(img)

    def show_frame(self, rgb_array: np.ndarray) -> None:
        """Display an RGB numpy array as the current frame."""
        try:
            pil_img = Image.fromarray(rgb_array)
            # Resize to fit if the canvas has a known size
            if self.winfo_width() > 10:
                w = self.winfo_width()
                h = self.winfo_height() or int(w * 3 / 4)
                pil_img = pil_img.resize((w, h), Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(pil_img)
            self.configure(image=self._photo)
        except Exception:
            pass

    def show_placeholder(self, message: str = "Waiting for stream…") -> None:
        img = Image.new("RGB", (640, 400), "#0a0a0a")
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        draw.text((320, 200), message, fill="#484f58", anchor="mm")
        self._placeholder = ImageTk.PhotoImage(img)
        self.configure(image=self._placeholder)


# ═══════════════════════════════════════════════════════════════════
# SCREENS
# ═══════════════════════════════════════════════════════════════════

class HomeScreen(tk.Frame):
    def __init__(self, app: "VideoRelayApp"):
        super().__init__(app, bg=BG)
        self._app = app
        self._build()

    def _build(self) -> None:
        # ── Header ────────────────────────────────────────────────
        header = tk.Frame(self, bg=BG)
        header.pack(pady=(60, 0))

        tk.Label(
            header, text="📡  Live Video Relay",
            font=_font(30, "bold"), fg=FG, bg=BG,
        ).pack()
        tk.Label(
            header,
            text="Direct camera sharing over your local network",
            font=_font(13), fg=FG2, bg=BG,
        ).pack(pady=(6, 0))

        # ── Cards row ─────────────────────────────────────────────
        cards_row = tk.Frame(self, bg=BG)
        cards_row.pack(pady=50)

        self._build_card(
            cards_row,
            emoji="📷",
            title="Share My Camera",
            subtitle="Stream your webcam\nto another PC",
            btn_text="  Start Sharing  ",
            btn_color=GREEN,
            btn_hover="#56d364",
            command=self._app.show_sender,
        ).pack(side=tk.LEFT, padx=20)

        self._build_card(
            cards_row,
            emoji="👁",
            title="Watch Stream",
            subtitle="Connect to a camera\nshared on the network",
            btn_text="  Connect  ",
            btn_color=ACCENT,
            btn_hover=ACCENT_H,
            command=self._app.show_receiver,
        ).pack(side=tk.LEFT, padx=20)

        # ── Footer note ────────────────────────────────────────────
        tk.Label(
            self,
            text="Works on the same LAN without any extra configuration.",
            font=_font(10), fg=FG3, bg=BG,
        ).pack(pady=(0, 30))

    def _build_card(
        self, parent, emoji, title, subtitle,
        btn_text, btn_color, btn_hover, command
    ) -> tk.Frame:
        card = tk.Frame(
            parent, bg=CARD, bd=0, relief=tk.FLAT,
            width=280, height=320,
        )
        card.pack_propagate(False)

        tk.Label(card, text=emoji, font=_font(52), bg=CARD, fg=FG).pack(pady=(36, 6))
        tk.Label(card, text=title, font=_font(17, "bold"), bg=CARD, fg=FG).pack()
        tk.Label(
            card, text=subtitle, font=_font(11), bg=CARD, fg=FG2,
            justify=tk.CENTER,
        ).pack(pady=(8, 24))

        RoundedButton(
            card, text=btn_text, command=command,
            bg_color=btn_color, hover_color=btn_hover,
            fg_color="#ffffff",
            width=180, height=42, radius=10,
            font_spec=_font(12, "bold"),
        ).pack()

        return card


# ── Separator helper ──────────────────────────────────────────────

def _sep(parent, orient="h", color=BORDER, thickness=1, **pack_kwargs):
    if orient == "h":
        f = tk.Frame(parent, bg=color, height=thickness)
    else:
        f = tk.Frame(parent, bg=color, width=thickness)
    f.pack(**pack_kwargs)
    return f


class SenderScreen(tk.Frame):
    """
    Sender mode UI.
    Left panel: live camera preview.
    Right panel: connection address, status, stats.
    """

    def __init__(self, app: "VideoRelayApp"):
        super().__init__(app, bg=BG)
        self._app = app
        self._backend: Optional[SenderBackend] = None
        self._running = False

        # Stats
        self._fps: float = 0.0
        self._viewers: int = 0

        self._build()

    def _build(self) -> None:
        # ── Top bar ───────────────────────────────────────────────
        topbar = tk.Frame(self, bg=CARD, height=52)
        topbar.pack(fill=tk.X)
        topbar.pack_propagate(False)

        RoundedButton(
            topbar, text="← Back", command=self._on_back,
            bg_color=PANEL, hover_color=BORDER, fg_color=FG,
            width=100, height=34, radius=8, font_spec=_font(11),
        ).pack(side=tk.LEFT, padx=16, pady=9)

        tk.Label(
            topbar, text="📷  Share My Camera",
            font=_font(14, "bold"), fg=FG, bg=CARD,
        ).pack(side=tk.LEFT, padx=4)

        self._status_dot = StatusDot(topbar, size=12)
        self._status_dot.pack(side=tk.LEFT, padx=8)

        self._status_lbl = tk.Label(
            topbar, text="Starting…", font=_font(11), fg=FG2, bg=CARD
        )
        self._status_lbl.pack(side=tk.LEFT)

        _sep(self, color=BORDER, fill=tk.X)

        # ── Body ──────────────────────────────────────────────────
        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        # Left — video preview
        left = tk.Frame(body, bg="#000000")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._video = VideoCanvas(left)
        self._video.pack(fill=tk.BOTH, expand=True)

        # Divider
        tk.Frame(body, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

        # Right — info panel
        right = tk.Frame(body, bg=CARD, width=280)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        self._build_info_panel(right)

    def _build_info_panel(self, parent: tk.Frame) -> None:
        # ── Address section ───────────────────────────────────────
        addr_frame = tk.Frame(parent, bg=CARD)
        addr_frame.pack(fill=tk.X, padx=20, pady=(28, 0))

        tk.Label(
            addr_frame, text="Share this address",
            font=_font(11, "bold"), fg=FG2, bg=CARD,
        ).pack(anchor=tk.W)

        ip_box = tk.Frame(addr_frame, bg=PANEL, bd=0)
        ip_box.pack(fill=tk.X, pady=(10, 0), ipady=14)

        self._port_var = tk.StringVar(value="9000")
        self._local_ip = get_local_ip()

        self._ip_lbl = tk.Label(
            ip_box,
            text=self._local_ip,
            font=(_SYSTEM_FONT, 18, "bold"), fg=ACCENT, bg=PANEL,
        )
        self._ip_lbl.pack(pady=(12, 0))

        self._port_lbl = tk.Label(
            ip_box, text="Port: 9000",
            font=_font(11), fg=FG2, bg=PANEL,
        )
        self._port_lbl.pack(pady=(2, 12))

        copy_btn = RoundedButton(
            addr_frame, text="📋  Copy Address",
            command=self._copy_address,
            bg_color=PANEL, hover_color=BORDER, fg_color=FG,
            width=200, height=36, radius=8, font_spec=_font(11),
        )
        copy_btn.pack(pady=(10, 0))
        self._copy_btn = copy_btn

        _sep(parent, color=BORDER, fill=tk.X, pady=(24, 0))

        # ── Port config ───────────────────────────────────────────
        port_frame = tk.Frame(parent, bg=CARD)
        port_frame.pack(fill=tk.X, padx=20, pady=(16, 0))

        tk.Label(
            port_frame, text="Port",
            font=_font(11), fg=FG2, bg=CARD,
        ).pack(side=tk.LEFT)

        port_entry = tk.Entry(
            port_frame, textvariable=self._port_var,
            font=_font(11), width=7,
            bg=PANEL, fg=FG, insertbackground=FG,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightcolor=ACCENT, highlightbackground=BORDER,
        )
        port_entry.pack(side=tk.LEFT, padx=(10, 0))

        cam_frame = tk.Frame(parent, bg=CARD)
        cam_frame.pack(fill=tk.X, padx=20, pady=(10, 0))

        tk.Label(
            cam_frame, text="Camera",
            font=_font(11), fg=FG2, bg=CARD,
        ).pack(side=tk.LEFT)

        self._cam_var = tk.StringVar(value="0")
        cam_entry = tk.Entry(
            cam_frame, textvariable=self._cam_var,
            font=_font(11), width=4,
            bg=PANEL, fg=FG, insertbackground=FG,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightcolor=ACCENT, highlightbackground=BORDER,
        )
        cam_entry.pack(side=tk.LEFT, padx=(10, 0))

        self._start_btn = RoundedButton(
            parent, text="▶  Start",
            command=self._start_streaming,
            bg_color=GREEN, hover_color="#56d364", fg_color="#ffffff",
            width=200, height=40, radius=10, font_spec=_font(12, "bold"),
        )
        self._start_btn.pack(pady=(18, 0))

        _sep(parent, color=BORDER, fill=tk.X, pady=(24, 0))

        # ── Stats ─────────────────────────────────────────────────
        stats = tk.Frame(parent, bg=CARD)
        stats.pack(fill=tk.X, padx=20, pady=(16, 0))

        def _stat_row(label: str) -> tk.Label:
            row = tk.Frame(stats, bg=CARD)
            row.pack(fill=tk.X, pady=4)
            tk.Label(row, text=label, font=_font(11), fg=FG2, bg=CARD).pack(side=tk.LEFT)
            val = tk.Label(row, text="—", font=_font(11, "bold"), fg=FG, bg=CARD)
            val.pack(side=tk.RIGHT)
            return val

        self._viewers_val  = _stat_row("Viewers")
        self._fps_val      = _stat_row("FPS sent")
        self._frames_val   = _stat_row("Quality")

    # ── Actions ────────────────────────────────────────────────────

    def _start_streaming(self) -> None:
        if self._running:
            return
        try:
            port = int(self._port_var.get())
            cam = int(self._cam_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Port and Camera must be numbers.")
            return

        self._port_lbl.configure(text=f"Port: {port}")
        self._backend = SenderBackend(
            port=port, camera_index=cam,
            width=1280, height=720, fps=20, jpeg_quality=75,
        )
        self._backend.start()
        self._running = True
        self._start_btn.configure_text("● Streaming")
        self._status_dot.set("waiting")
        self._status_lbl.configure(text="Opening camera…")
        self._poll_status()
        self._poll_preview()

    def _copy_address(self) -> None:
        port = self._port_var.get()
        addr = f"{self._local_ip}:{port}"
        self._app.clipboard_clear()
        self._app.clipboard_append(addr)
        self._copy_btn.configure_text("✓  Copied!")
        self.after(2000, lambda: self._copy_btn.configure_text("📋  Copy Address"))

    def _on_back(self) -> None:
        self._running = False
        if self._backend:
            self._backend.stop()
            self._backend = None
        self._app.show_home()

    # ── Polling loops ──────────────────────────────────────────────

    def _poll_status(self) -> None:
        if not self._running:
            return
        be = self._backend
        if be is None:
            return
        try:
            while True:
                event, value = be.status_queue.get_nowait()
                if event == "ready":
                    self._status_dot.set("ok")
                    self._status_lbl.configure(text="Streaming — waiting for viewers")
                elif event == "error":
                    self._status_dot.set("error")
                    self._status_lbl.configure(text=f"Error: {value[:60]}")
                    messagebox.showerror("Sender Error", value, parent=self._app)
                    self._running = False
                    return
                elif event == "viewers":
                    self._viewers = value
                    self._viewers_val.configure(
                        text=str(value),
                        fg=GREEN if value > 0 else FG,
                    )
                    if value > 0:
                        self._status_lbl.configure(
                            text=f"{value} viewer{'s' if value != 1 else ''} connected"
                        )
                    else:
                        self._status_lbl.configure(text="Streaming — waiting for viewers")
                elif event == "fps":
                    self._fps = value
                    self._fps_val.configure(text=f"{value}")
        except queue.Empty:
            pass
        self.after(250, self._poll_status)

    def _poll_preview(self) -> None:
        if not self._running:
            return
        be = self._backend
        if be is None:
            return
        try:
            frame = be.preview_queue.get_nowait()
            # BGR → RGB for PIL
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            self._video.show_frame(rgb)
        except queue.Empty:
            pass
        self.after(33, self._poll_preview)  # ~30 fps display


class ReceiverScreen(tk.Frame):
    """
    Receiver mode UI.
    Top: connection form.
    Main: full-screen video display.
    Bottom bar: stats.
    """

    def __init__(self, app: "VideoRelayApp"):
        super().__init__(app, bg=BG)
        self._app = app
        self._backend: Optional[ReceiverBackend] = None
        self._connected = False

        self._fps: float = 0.0
        self._latency: int = 0

        self._build()

    def _build(self) -> None:
        # ── Top bar ───────────────────────────────────────────────
        topbar = tk.Frame(self, bg=CARD, height=52)
        topbar.pack(fill=tk.X)
        topbar.pack_propagate(False)

        RoundedButton(
            topbar, text="← Back", command=self._on_back,
            bg_color=PANEL, hover_color=BORDER, fg_color=FG,
            width=100, height=34, radius=8, font_spec=_font(11),
        ).pack(side=tk.LEFT, padx=16, pady=9)

        tk.Label(
            topbar, text="👁  Watch Stream",
            font=_font(14, "bold"), fg=FG, bg=CARD,
        ).pack(side=tk.LEFT, padx=4)

        self._status_dot = StatusDot(topbar, size=12)
        self._status_dot.pack(side=tk.LEFT, padx=8)

        self._status_lbl = tk.Label(
            topbar, text="Not connected", font=_font(11), fg=FG2, bg=CARD
        )
        self._status_lbl.pack(side=tk.LEFT)

        _sep(self, color=BORDER, fill=tk.X)

        # ── Connect form ──────────────────────────────────────────
        form = tk.Frame(self, bg=CARD, height=64)
        form.pack(fill=tk.X)
        form.pack_propagate(False)

        tk.Label(form, text="Address:", font=_font(11), fg=FG2, bg=CARD).pack(
            side=tk.LEFT, padx=(24, 6), pady=16
        )

        self._ip_var = tk.StringVar()
        ip_entry = tk.Entry(
            form, textvariable=self._ip_var,
            font=_font(13), width=18,
            bg=PANEL, fg=FG, insertbackground=FG,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightcolor=ACCENT, highlightbackground=BORDER,
        )
        ip_entry.pack(side=tk.LEFT, ipady=6, padx=(0, 16))
        ip_entry.bind("<Return>", lambda e: self._connect())

        tk.Label(form, text="Port:", font=_font(11), fg=FG2, bg=CARD).pack(
            side=tk.LEFT, padx=(0, 6)
        )

        self._port_var = tk.StringVar(value="9000")
        port_entry = tk.Entry(
            form, textvariable=self._port_var,
            font=_font(13), width=7,
            bg=PANEL, fg=FG, insertbackground=FG,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightcolor=ACCENT, highlightbackground=BORDER,
        )
        port_entry.pack(side=tk.LEFT, ipady=6, padx=(0, 16))
        port_entry.bind("<Return>", lambda e: self._connect())

        self._connect_btn = RoundedButton(
            form, text="Connect",
            command=self._connect,
            bg_color=ACCENT, hover_color=ACCENT_H, fg_color=BG,
            width=120, height=38, radius=9, font_spec=_font(12, "bold"),
        )
        self._connect_btn.pack(side=tk.LEFT, padx=(0, 8), pady=13)

        self._disconnect_btn = RoundedButton(
            form, text="Disconnect",
            command=self._disconnect,
            bg_color=RED, hover_color="#ff6b6b", fg_color="#ffffff",
            width=120, height=38, radius=9, font_spec=_font(12, "bold"),
        )
        # hidden until connected

        _sep(self, color=BORDER, fill=tk.X)

        # ── Video area ────────────────────────────────────────────
        video_frame = tk.Frame(self, bg="#000000")
        video_frame.pack(fill=tk.BOTH, expand=True)

        self._video = VideoCanvas(video_frame)
        self._video.pack(fill=tk.BOTH, expand=True)

        _sep(self, color=BORDER, fill=tk.X)

        # ── Stats bar ─────────────────────────────────────────────
        bar = tk.Frame(self, bg=CARD, height=34)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        self._fps_lbl = tk.Label(
            bar, text="FPS: —", font=_font(10), fg=FG2, bg=CARD
        )
        self._fps_lbl.pack(side=tk.LEFT, padx=20, pady=8)

        self._lat_lbl = tk.Label(
            bar, text="Latency: —",
            font=_font(10), fg=FG2, bg=CARD,
        )
        self._lat_lbl.pack(side=tk.LEFT, padx=20)

        tk.Label(
            bar,
            text="⚠ Latency is accurate only when both PCs clocks are NTP-synced",
            font=_font(9), fg=FG3, bg=CARD,
        ).pack(side=tk.RIGHT, padx=16)

    # ── Actions ────────────────────────────────────────────────────

    def _connect(self) -> None:
        ip = self._ip_var.get().strip()
        port_str = self._port_var.get().strip()
        if not ip:
            messagebox.showwarning("Missing IP", "Enter the sender's IP address.", parent=self._app)
            return
        try:
            port = int(port_str)
        except ValueError:
            messagebox.showwarning("Invalid Port", "Port must be a number.", parent=self._app)
            return

        if self._backend:
            self._backend.stop()
            self._backend = None

        self._video.show_placeholder("Connecting…")
        self._status_dot.set("waiting")
        self._status_lbl.configure(text=f"Connecting to {ip}:{port}…")
        self._connect_btn.configure_text("Connecting…")

        self._backend = ReceiverBackend(host=ip, port=port)
        self._backend.start()
        self._connected = True

        self._disconnect_btn.pack(side=self._connect_btn.winfo_parent() and tk.LEFT, padx=(0, 8), pady=13)
        self._poll_status()
        self._poll_frames()

    def _disconnect(self) -> None:
        self._connected = False
        if self._backend:
            self._backend.stop()
            self._backend = None
        self._status_dot.set("idle")
        self._status_lbl.configure(text="Disconnected")
        self._connect_btn.configure_text("Connect")
        self._video.show_placeholder("Disconnected — enter an IP to reconnect")
        self._disconnect_btn.pack_forget()

    def _on_back(self) -> None:
        self._connected = False
        if self._backend:
            self._backend.stop()
            self._backend = None
        self._app.show_home()

    # ── Polling loops ──────────────────────────────────────────────

    def _poll_status(self) -> None:
        if not self._connected:
            return
        be = self._backend
        if be is None:
            return
        try:
            while True:
                event, value = be.status_queue.get_nowait()
                if event == "connecting":
                    self._status_dot.set("waiting")
                    self._status_lbl.configure(text="Connecting…")
                elif event == "connected":
                    self._status_dot.set("ok")
                    self._status_lbl.configure(text="Connected — receiving stream")
                    self._connect_btn.configure_text("Connected ✓")
                    # Show disconnect button
                    self._disconnect_btn.pack(
                        side=tk.LEFT, padx=(0, 8), pady=13,
                        after=self._connect_btn,
                    )
                elif event == "error":
                    self._status_dot.set("error")
                    self._status_lbl.configure(text=f"Error: {value[:60]}")
                    self._video.show_placeholder(f"⚠ {value}")
                    self._connect_btn.configure_text("Connect")
                    self._connected = False
                    return
                elif event == "disconnected":
                    self._status_dot.set("error")
                    self._status_lbl.configure(text="Sender disconnected")
                    self._video.show_placeholder("Sender disconnected.\nEnter IP to reconnect.")
                    self._connect_btn.configure_text("Connect")
                    self._connected = False
                    return
                elif event == "fps":
                    self._fps = value
                    self._fps_lbl.configure(text=f"FPS: {value}")
                elif event == "latency":
                    self._latency = value
                    color = GREEN if abs(value) < 150 else (YELLOW if abs(value) < 500 else RED)
                    self._lat_lbl.configure(text=f"Latency: {value:+d} ms", fg=color)
        except queue.Empty:
            pass
        self.after(200, self._poll_status)

    def _poll_frames(self) -> None:
        if not self._connected:
            return
        be = self._backend
        if be is None:
            return
        # Drain queue, only display the newest frame
        frame = None
        try:
            while True:
                frame = be.frame_queue.get_nowait()
        except queue.Empty:
            pass

        if frame is not None:
            self._video.show_frame(frame)

        self.after(30, self._poll_frames)  # ~33 fps display refresh


# ═══════════════════════════════════════════════════════════════════
# MAIN APP WINDOW
# ═══════════════════════════════════════════════════════════════════

class VideoRelayApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Live Video Relay")
        self.configure(bg=BG)
        self.geometry("960x640")
        self.minsize(800, 520)

        # Dark title bar on Windows 10/11 via DWM API (undocumented but stable).
        # Silently ignored on Linux and macOS.
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4
                )
            except Exception:
                pass  # older Windows / unavailable DWM — not critical

        self._current_screen: Optional[tk.Frame] = None
        self.show_home()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _switch(self, screen: tk.Frame) -> None:
        if self._current_screen:
            self._current_screen.destroy()
        self._current_screen = screen
        screen.pack(fill=tk.BOTH, expand=True)

    def show_home(self)     -> None: self._switch(HomeScreen(self))
    def show_sender(self)   -> None: self._switch(SenderScreen(self))
    def show_receiver(self) -> None: self._switch(ReceiverScreen(self))

    def _on_close(self) -> None:
        # Let the current screen clean up
        if isinstance(self._current_screen, (SenderScreen, ReceiverScreen)):
            self._current_screen._on_back()
        self.destroy()


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    app = VideoRelayApp()
    app.mainloop()


if __name__ == "__main__":
    main()

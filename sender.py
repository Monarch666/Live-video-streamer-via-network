"""
sender.py — Webcam capture and streaming sender for the Live Video Relay.

Runs on PC1 (any network).  Captures webcam frames, encodes as JPEG, and
streams them through the relay server to all registered receivers.

Architecture:
  • Webcam capture runs in a ThreadPoolExecutor (cv2.VideoCapture.read() blocks).
  • asyncio event loop handles all network I/O.
  • A thread-safe asyncio.Queue bridges the two worlds.
  • Frames are dropped (not buffered) when the relay's write buffer is backed up.

Usage:
    python sender.py \\
        --relay-host <VPS_IP> \\
        --relay-port 9000 \\
        --stream-name mystream \\
        --camera-index 0 \\
        --width 640 --height 480 \\
        --fps 15 \\
        --jpeg-quality 70
"""

import argparse
import asyncio
import concurrent.futures
import logging
import sys
import time
import os

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from protocol import (
    WRITE_BUFFER_LIMIT,
    MSG_REGISTER_SENDER,
    MSG_REGISTER_OK,
    MSG_REGISTER_FAIL,
    MSG_VIDEO_FRAME,
    MSG_PING,
    MSG_PONG,
    now_ms,
    pack_frame,
    read_frame,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SENDER] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("sender")

# ---------------------------------------------------------------------------
# Global stats (updated from async context, read by stats printer)
# ---------------------------------------------------------------------------

_stats_sent: int = 0
_stats_dropped: int = 0
_stats_last_reset: float = time.monotonic()
_stats_sent_in_window: int = 0


def _reset_stats_window() -> None:
    global _stats_last_reset, _stats_sent_in_window
    _stats_last_reset = time.monotonic()
    _stats_sent_in_window = 0


# ---------------------------------------------------------------------------
# Webcam capture (blocking — runs in thread pool)
# ---------------------------------------------------------------------------

def _open_camera(index: int, width: int, height: int) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera index {index}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    log.info("Camera opened: index=%d, resolution=%dx%d", index, actual_w, actual_h)
    return cap


def _capture_one_frame(cap: cv2.VideoCapture) -> bytes | None:
    """
    Capture a single frame and JPEG-encode it.
    Runs in the thread pool — must NOT touch asyncio primitives.
    Returns JPEG bytes or None on capture failure.
    """
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    return frame  # return raw frame; encoding happens in async context for clarity


def _jpeg_encode(frame: np.ndarray, quality: int) -> bytes | None:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        log.warning("JPEG encode failed — dropping frame")
        return None
    return buf.tobytes()


# ---------------------------------------------------------------------------
# Stats printer task
# ---------------------------------------------------------------------------

async def _stats_printer(interval: float = 5.0) -> None:
    global _stats_sent, _stats_dropped, _stats_last_reset, _stats_sent_in_window
    while True:
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - _stats_last_reset
        fps = _stats_sent_in_window / elapsed if elapsed > 0 else 0.0
        log.info(
            "Stats — sent: %d  dropped: %d  current FPS: %.1f",
            _stats_sent,
            _stats_dropped,
            fps,
        )
        _stats_sent_in_window = 0
        _stats_last_reset = time.monotonic()


# ---------------------------------------------------------------------------
# PING keepalive task
# ---------------------------------------------------------------------------

async def _keepalive(writer: asyncio.StreamWriter, interval: float = 10.0) -> None:
    """Send PING every *interval* seconds to keep the connection alive."""
    while True:
        await asyncio.sleep(interval)
        try:
            data = pack_frame(MSG_PING, 0, 0, now_ms(), b"")
            writer.write(data)
            await writer.drain()
        except Exception:
            break  # connection lost — let the main loop handle it


# ---------------------------------------------------------------------------
# Reader task (handles PONG, unexpected messages from relay)
# ---------------------------------------------------------------------------

async def _reader_task(reader: asyncio.StreamReader) -> None:
    """
    Consume incoming messages from the relay (e.g. PONG).
    Runs as a concurrent task so the send loop is never stalled waiting for data.
    """
    try:
        while True:
            msg_type, stream_id, seq_num, ts, payload = await read_frame(reader)
            if msg_type == MSG_PONG:
                pass  # keepalive acknowledged
            else:
                log.debug("Unexpected msg from relay: 0x%02X", msg_type)
    except asyncio.IncompleteReadError:
        pass  # normal close
    except Exception as exc:
        log.debug("Reader task exiting: %s", exc)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

async def _register(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stream_name: str,
) -> int:
    """
    Send REGISTER_SENDER and wait for REGISTER_OK or REGISTER_FAIL.
    Returns the assigned stream_id on success.
    Raises RuntimeError on failure.
    """
    payload = stream_name.encode("utf-8")
    writer.write(pack_frame(MSG_REGISTER_SENDER, 0, 0, now_ms(), payload))
    await writer.drain()

    msg_type, stream_id, _, _, resp_payload = await read_frame(reader)
    if msg_type == MSG_REGISTER_OK:
        log.info("Registered as sender for stream '%s' (id=%d)", stream_name, stream_id)
        return stream_id
    elif msg_type == MSG_REGISTER_FAIL:
        reason = resp_payload.decode("utf-8", errors="replace")
        raise RuntimeError(f"Relay rejected registration: {reason}")
    else:
        raise RuntimeError(f"Unexpected response: 0x{msg_type:02X}")


# ---------------------------------------------------------------------------
# Main send loop for one connection session
# ---------------------------------------------------------------------------

async def _send_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    frame_queue: "asyncio.Queue[bytes | None]",
    stream_id: int,
    fps: int,
) -> None:
    """
    Pull JPEG frames from *frame_queue* and send them to the relay.
    Returns when the queue yields None (shutdown signal) or a write error occurs.
    """
    global _stats_sent, _stats_dropped, _stats_sent_in_window

    seq_num: int = 0
    frame_interval = 1.0 / fps

    # Start keepalive and reader tasks
    keepalive_task = asyncio.create_task(_keepalive(writer))
    reader_task = asyncio.create_task(_reader_task(reader))

    try:
        while True:
            deadline = asyncio.get_event_loop().time() + frame_interval
            try:
                jpeg_bytes = await asyncio.wait_for(
                    frame_queue.get(), timeout=frame_interval * 2
                )
            except asyncio.TimeoutError:
                continue

            if jpeg_bytes is None:
                break  # shutdown

            # Check backpressure before writing
            try:
                buf_size = writer.transport.get_write_buffer_size()
            except Exception:
                buf_size = 0

            if buf_size > WRITE_BUFFER_LIMIT:
                _stats_dropped += 1
                log.debug("Send buffer full (%d B) — dropping frame", buf_size)
            else:
                ts = now_ms()
                try:
                    data = pack_frame(MSG_VIDEO_FRAME, stream_id, seq_num, ts, jpeg_bytes)
                except ValueError as exc:
                    # Payload exceeded MAX_PAYLOAD (an unusually complex frame).
                    # Drop just this one frame — do not let it take down the
                    # whole session, which used to cause a full reconnect.
                    _stats_dropped += 1
                    log.debug("Dropping oversized frame: %s", exc)
                    seq_num = (seq_num + 1) & 0xFFFFFFFF
                else:
                    try:
                        # TODO(security): Encryption hook — `data` contains the packed
                        # frame.  For plaintext MVP we write directly.  To add
                        # encryption, replace `writer.write(data)` with
                        # `writer.write(encrypt_transport(data, session_key))`.
                        writer.write(data)
                        # Do not await drain() — we rely on OS buffering + backpressure
                        # check above to avoid stalling the capture loop.
                    except Exception as exc:
                        log.warning("Write error: %s", exc)
                        break

                    seq_num = (seq_num + 1) & 0xFFFFFFFF
                    _stats_sent += 1
                    _stats_sent_in_window += 1

            # Pace to target FPS
            now = asyncio.get_event_loop().time()
            sleep_time = deadline - now
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    finally:
        keepalive_task.cancel()
        reader_task.cancel()
        try:
            await asyncio.gather(keepalive_task, reader_task, return_exceptions=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Webcam producer (runs in asyncio, offloads blocking read to thread pool)
# ---------------------------------------------------------------------------

async def _webcam_producer(
    cap: cv2.VideoCapture,
    queue: "asyncio.Queue[bytes | None]",
    jpeg_quality: int,
    executor: concurrent.futures.ThreadPoolExecutor,
) -> None:
    """
    Continuously capture frames from *cap* and push JPEG bytes onto *queue*.
    Drops frames rather than filling the queue unboundedly.
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            frame = await loop.run_in_executor(executor, _capture_one_frame, cap)
        except Exception as exc:
            log.error("Camera read error: %s", exc)
            await asyncio.sleep(0.5)
            continue

        if frame is None:
            log.warning("Camera returned empty frame — retrying")
            await asyncio.sleep(0.05)
            continue

        # Encode JPEG in executor (CPU bound)
        try:
            jpeg = await loop.run_in_executor(
                executor, _jpeg_encode, frame, jpeg_quality
            )
        except Exception as exc:
            log.warning("JPEG encode error: %s", exc)
            continue

        if jpeg is None:
            continue

        # Non-blocking put — drop the frame if queue is full (backpressure)
        try:
            queue.put_nowait(jpeg)
        except asyncio.QueueFull:
            global _stats_dropped
            _stats_dropped += 1


# ---------------------------------------------------------------------------
# Top-level main with reconnect loop
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    cap = _open_camera(args.camera_index, args.width, args.height)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="cam")

    # Queue between webcam producer and send session (bounded to ~1 frame)
    frame_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)

    # Start webcam producer (runs forever in the background)
    producer_task = asyncio.create_task(
        _webcam_producer(cap, frame_queue, args.jpeg_quality, executor),
        name="webcam-producer",
    )
    # Start stats printer
    stats_task = asyncio.create_task(_stats_printer(), name="stats-printer")

    try:
        while True:
            log.info(
                "Connecting to relay %s:%d ...", args.relay_host, args.relay_port
            )
            try:
                reader, writer = await asyncio.open_connection(
                    args.relay_host, args.relay_port
                )
            except (ConnectionRefusedError, OSError) as exc:
                log.error("Connection failed: %s — retrying in 3 s", exc)
                await asyncio.sleep(3)
                continue

            try:
                stream_id = await _register(reader, writer, args.stream_name)
            except RuntimeError as exc:
                log.error("Registration failed: %s", exc)
                writer.close()
                await asyncio.sleep(3)
                continue
            except Exception as exc:
                log.error("Unexpected registration error: %s", exc)
                writer.close()
                await asyncio.sleep(3)
                continue

            try:
                await _send_session(reader, writer, frame_queue, stream_id, args.fps)
            except Exception as exc:
                log.error("Session error: %s", exc)
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

            log.info("Disconnected from relay — reconnecting in 3 s ...")
            await asyncio.sleep(3)

    except asyncio.CancelledError:
        pass
    finally:
        producer_task.cancel()
        stats_task.cancel()
        await asyncio.gather(producer_task, stats_task, return_exceptions=True)
        cap.release()
        executor.shutdown(wait=False)
        log.info("Sender shut down.")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Live Video Relay — Sender",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--relay-host", required=True, help="Relay server IP or hostname")
    p.add_argument("--relay-port", type=int, default=9000, help="Relay server port")
    p.add_argument("--stream-name", required=True, help="Unique stream identifier string")
    p.add_argument("--camera-index", type=int, default=0, help="OpenCV camera index")
    p.add_argument("--width", type=int, default=640, help="Capture width")
    p.add_argument("--height", type=int, default=480, help="Capture height")
    p.add_argument("--fps", type=int, default=15, help="Target frames per second")
    p.add_argument("--jpeg-quality", type=int, default=70, help="JPEG quality (1-100)")
    return p


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        log.info("Sender stopped by user.")

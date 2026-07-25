"""
receiver.py — Video frame receiver and display for the Live Video Relay.

Runs on PC2 (any network).  Connects to the relay, subscribes to a stream by
name, and displays incoming JPEG frames in an OpenCV window.

Threading model:
  • cv2.imshow() and cv2.waitKey() MUST run on the main OS thread (Qt/GTK
    requirement on all platforms).
  • asyncio networking runs on a dedicated background thread.
  • A thread-safe queue.Queue bridges the two: the asyncio thread pushes
    decoded numpy frames; the main thread calls imshow/waitKey in a tight loop.

Usage:
    python receiver.py \\
        --relay-host <VPS_IP> \\
        --relay-port 9000 \\
        --stream-name mystream

Exit: press 'q' in the display window, or Ctrl+C.
"""

import argparse
import asyncio
import logging
import os
import queue
import sys
import threading
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from protocol import (
    MSG_REGISTER_RECEIVER,
    MSG_REGISTER_OK,
    MSG_REGISTER_FAIL,
    MSG_VIDEO_FRAME,
    MSG_PING,
    MSG_PONG,
    MSG_PEER_DISCONNECTED,
    now_ms,
    pack_frame,
    read_frame,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RECV] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("receiver")

# ---------------------------------------------------------------------------
# Sentinel objects for the frame queue
# ---------------------------------------------------------------------------

class _PeerDisconnected:
    pass


class _Shutdown:
    pass


SENTINEL_PEER_DISC = _PeerDisconnected()
SENTINEL_SHUTDOWN = _Shutdown()

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

_stats_recv: int = 0
_stats_last_reset: float = time.monotonic()
_stats_recv_in_window: int = 0


# ---------------------------------------------------------------------------
# Stats printer (runs inside the asyncio thread)
# ---------------------------------------------------------------------------

async def _stats_printer(interval: float = 5.0) -> None:
    global _stats_recv, _stats_last_reset, _stats_recv_in_window
    while True:
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - _stats_last_reset
        fps = _stats_recv_in_window / elapsed if elapsed > 0 else 0.0
        log.info(
            "Stats — received: %d  current FPS: %.1f",
            _stats_recv,
            fps,
        )
        _stats_recv_in_window = 0
        _stats_last_reset = time.monotonic()


# ---------------------------------------------------------------------------
# PING keepalive (asyncio thread)
# ---------------------------------------------------------------------------

async def _keepalive(writer: asyncio.StreamWriter, interval: float = 10.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            writer.write(pack_frame(MSG_PING, 0, 0, now_ms(), b""))
            await writer.drain()
        except Exception:
            break


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

async def _register(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stream_name: str,
) -> int:
    payload = stream_name.encode("utf-8")
    writer.write(pack_frame(MSG_REGISTER_RECEIVER, 0, 0, now_ms(), payload))
    await writer.drain()

    msg_type, stream_id, _, _, resp_payload = await read_frame(reader)
    if msg_type == MSG_REGISTER_OK:
        log.info("Registered as receiver for stream '%s' (id=%d)", stream_name, stream_id)
        return stream_id
    elif msg_type == MSG_REGISTER_FAIL:
        reason = resp_payload.decode("utf-8", errors="replace")
        raise RuntimeError(f"Relay rejected registration: {reason}")
    else:
        raise RuntimeError(f"Unexpected response: 0x{msg_type:02X}")


# ---------------------------------------------------------------------------
# Receive loop (asyncio thread)
# ---------------------------------------------------------------------------

async def _receive_loop(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    frame_q: "queue.Queue",
    stream_name: str,
) -> None:
    """
    Read frames from the relay and push decoded numpy arrays into *frame_q*.
    Runs until connection is lost or shutdown.
    """
    global _stats_recv, _stats_recv_in_window

    keepalive_task = asyncio.create_task(_keepalive(writer))

    try:
        while True:
            try:
                msg_type, stream_id, seq_num, timestamp_ms, payload = await read_frame(reader)
            except asyncio.IncompleteReadError:
                log.info("Relay connection closed.")
                break
            except ValueError as exc:
                log.warning("Protocol error: %s", exc)
                break

            if msg_type == MSG_VIDEO_FRAME:
                # Decode JPEG → numpy array
                # NOTE: end-to-end latency = now_ms() - timestamp_ms works only when
                # both PCs have reasonably synced clocks (NTP).  Latency numbers will
                # be meaningless / negative if clocks are skewed.  This is a known
                # MVP limitation; clock sync is out of scope.
                local_recv_ms = now_ms()
                latency_ms = local_recv_ms - timestamp_ms

                # TODO(security): Decryption hook — `payload` here contains the raw
                # bytes received from the relay.  For plaintext MVP we pass directly
                # to cv2.imdecode.  To add decryption, replace `raw_jpeg = payload`
                # with `raw_jpeg = decrypt(payload, session_key)`.
                raw_jpeg = payload

                arr = np.frombuffer(raw_jpeg, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    log.warning("Failed to decode JPEG (seq=%d) — dropping", seq_num)
                    continue

                # Annotate frame with stats (optional — helps debugging)
                cv2.putText(
                    frame,
                    f"lat: {latency_ms:+d}ms  seq: {seq_num}",
                    (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (0, 255, 80),
                    1,
                    cv2.LINE_AA,
                )

                # Push frame — non-blocking, drop if display thread is behind
                try:
                    frame_q.put_nowait((frame, latency_ms))
                except queue.Full:
                    pass  # display thread can't keep up — drop this frame

                _stats_recv += 1
                _stats_recv_in_window += 1

            elif msg_type == MSG_PEER_DISCONNECTED:
                log.warning(
                    "Sender disconnected for stream '%s' — waiting for reconnect ...",
                    stream_name,
                )
                print(
                    "\n[RECEIVER] Sender disconnected — keeping window open, "
                    "waiting for sender to reconnect ...\n"
                )
                frame_q.put(SENTINEL_PEER_DISC)

            elif msg_type == MSG_PONG:
                pass  # keepalive acknowledged

            else:
                log.debug("Unhandled message: 0x%02X", msg_type)

    finally:
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Asyncio thread entry point (runs the event loop + reconnect logic)
# ---------------------------------------------------------------------------

def _asyncio_thread(
    relay_host: str,
    relay_port: int,
    stream_name: str,
    frame_q: "queue.Queue",
    stop_event: threading.Event,
) -> None:
    """
    Blocking function that runs an asyncio event loop.
    Handles connect → register → receive loop → reconnect.
    """

    async def _run() -> None:
        stats_task = asyncio.create_task(_stats_printer())
        try:
            while not stop_event.is_set():
                log.info("Connecting to relay %s:%d ...", relay_host, relay_port)
                try:
                    reader, writer = await asyncio.open_connection(relay_host, relay_port)
                except (ConnectionRefusedError, OSError) as exc:
                    log.error("Connection failed: %s — retrying in 3 s", exc)
                    await asyncio.sleep(3)
                    continue

                try:
                    await _register(reader, writer, stream_name)
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

                log.info(
                    "Waiting for sender to stream '%s' ...", stream_name
                )
                print(f"\n[RECEIVER] Waiting for sender on stream '{stream_name}' ...\n")

                try:
                    await _receive_loop(reader, writer, frame_q, stream_name)
                except Exception as exc:
                    log.error("Receive error: %s", exc)
                finally:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

                if not stop_event.is_set():
                    log.info("Disconnected from relay — reconnecting in 3 s ...")
                    await asyncio.sleep(3)
        finally:
            stats_task.cancel()
            frame_q.put(SENTINEL_SHUTDOWN)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Main — display loop on main thread
# ---------------------------------------------------------------------------

def main(args: argparse.Namespace) -> None:
    frame_q: queue.Queue = queue.Queue(maxsize=4)  # small buffer — freshness over completeness
    stop_event = threading.Event()

    # Start asyncio networking on a background thread
    net_thread = threading.Thread(
        target=_asyncio_thread,
        args=(args.relay_host, args.relay_port, args.stream_name, frame_q, stop_event),
        daemon=True,
        name="asyncio-net",
    )
    net_thread.start()

    window_title = f"Live Stream — {args.stream_name}"
    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)

    last_frame: np.ndarray | None = None
    last_latency: int = 0
    waiting_placeholder_shown = False

    log.info(
        "Display window opened. Press 'q' to quit. Waiting for frames ..."
    )

    try:
        while True:
            # cv2.waitKey MUST be called on the main thread; 30 ms gives ~33 fps max UI.
            key = cv2.waitKey(30) & 0xFF
            if key == ord("q"):
                log.info("'q' pressed — exiting.")
                break

            # Drain the queue (take the most recent frame, discard stale ones)
            item = None
            try:
                while True:
                    item = frame_q.get_nowait()
            except queue.Empty:
                pass

            if item is None:
                # Nothing new — redisplay last frame or show placeholder
                if last_frame is not None:
                    cv2.imshow(window_title, last_frame)
                elif not waiting_placeholder_shown:
                    placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
                    cv2.putText(
                        placeholder,
                        "Waiting for stream ...",
                        (120, 240),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.0,
                        (180, 180, 180),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.imshow(window_title, placeholder)
                    waiting_placeholder_shown = True
                continue

            if isinstance(item, _Shutdown):
                log.info("Network thread shut down — exiting display loop.")
                break

            if isinstance(item, _PeerDisconnected):
                # Show disconnected overlay on last frame
                if last_frame is not None:
                    overlay = last_frame.copy()
                    cv2.putText(
                        overlay,
                        "Sender disconnected — waiting ...",
                        (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.8,
                        (0, 80, 255),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.imshow(window_title, overlay)
                continue

            # Regular frame
            frame, latency_ms = item
            last_frame = frame
            last_latency = latency_ms
            waiting_placeholder_shown = False
            cv2.imshow(window_title, frame)

    except KeyboardInterrupt:
        log.info("Ctrl+C — exiting.")
    finally:
        stop_event.set()
        cv2.destroyAllWindows()
        net_thread.join(timeout=3)
        log.info("Receiver shut down.")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Live Video Relay — Receiver",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--relay-host", required=True, help="Relay server IP or hostname")
    p.add_argument("--relay-port", type=int, default=9000, help="Relay server port")
    p.add_argument("--stream-name", required=True, help="Stream name to subscribe to")
    return p


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    main(args)

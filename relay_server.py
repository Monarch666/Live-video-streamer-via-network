"""
relay_server.py — Cross-Network Live Video Relay Server.

Runs on a VPS with a public IP.  Accepts TCP connections from senders and
receivers, matches them by stream name, and forwards video frames.

Design decisions:
  • Pure asyncio — no threads, no external deps beyond stdlib.
  • All state is in-memory; resets on restart (by design for this MVP).
  • One slow receiver never stalls other receivers or the sender: frames are
    dropped if a receiver's write-buffer exceeds WRITE_BUFFER_LIMIT.
  • The relay is stream-name agnostic — it never inspects frame payloads.

Usage:
    python relay_server.py [--host 0.0.0.0] [--port 9000]
"""

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

# Ensure the relay_server can be run from the project root regardless of CWD.
import os
sys.path.insert(0, os.path.dirname(__file__))

from protocol import (
    WRITE_BUFFER_LIMIT,
    MSG_REGISTER_SENDER,
    MSG_REGISTER_RECEIVER,
    MSG_REGISTER_OK,
    MSG_REGISTER_FAIL,
    MSG_VIDEO_FRAME,
    MSG_PING,
    MSG_PONG,
    MSG_PEER_DISCONNECTED,
    MSG_NAMES,
    now_ms,
    pack_frame,
    read_frame,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RELAY] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("relay")


# ---------------------------------------------------------------------------
# Stream State
# ---------------------------------------------------------------------------

@dataclass
class StreamState:
    name: str
    stream_id: int
    sender: Optional[asyncio.StreamWriter] = None
    receivers: Set[asyncio.StreamWriter] = field(default_factory=set)


# Global registry: stream_name -> StreamState
streams: Dict[str, StreamState] = {}
_next_stream_id: int = 1


def _alloc_stream_id() -> int:
    global _next_stream_id
    sid = _next_stream_id
    _next_stream_id += 1
    return sid


def _get_or_create_stream(name: str) -> StreamState:
    if name not in streams:
        streams[name] = StreamState(name=name, stream_id=_alloc_stream_id())
        log.info("Created stream '%s' (id=%d)", name, streams[name].stream_id)
    return streams[name]


def _peer_addr(writer: asyncio.StreamWriter) -> str:
    try:
        return "{}:{}".format(*writer.get_extra_info("peername", ("?", "?")))
    except Exception:
        return "<unknown>"


# ---------------------------------------------------------------------------
# Frame forwarding helpers
# ---------------------------------------------------------------------------

async def _send_control(
    writer: asyncio.StreamWriter,
    msg_type: int,
    stream_id: int = 0,
    payload: bytes = b"",
) -> None:
    """Send a control message (REGISTER_OK, REGISTER_FAIL, PONG, etc.)."""
    data = pack_frame(msg_type, stream_id, 0, now_ms(), payload)
    writer.write(data)
    await writer.drain()


async def _forward_frame_to_receiver(
    raw_frame: bytes,
    receiver: asyncio.StreamWriter,
    stream_name: str,
) -> bool:
    """
    Forward a pre-packed frame to one receiver.

    Returns True if the frame was written, False if it was dropped due to
    backpressure.  Dropping never raises — we just move on.
    """
    try:
        buf_size = receiver.transport.get_write_buffer_size()
    except Exception:
        buf_size = 0

    if buf_size > WRITE_BUFFER_LIMIT:
        log.debug(
            "Dropping frame for receiver %s (stream='%s'): buffer %d B > limit %d B",
            _peer_addr(receiver),
            stream_name,
            buf_size,
            WRITE_BUFFER_LIMIT,
        )
        return False

    try:
        receiver.write(raw_frame)
        # NOTE: we intentionally do NOT await drain() here so that one
        # slow receiver cannot pause the forwarding loop.  The OS TCP
        # buffer + our buffer-size check above bound the queue.
    except Exception as exc:
        log.warning("Write error to %s: %s", _peer_addr(receiver), exc)
        return False

    return True


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    addr = _peer_addr(writer)
    log.info("New connection from %s", addr)

    # Role for this connection (set after first REGISTER_* message)
    role: Optional[str] = None      # "sender" or "receiver"
    stream_state: Optional[StreamState] = None

    try:
        while True:
            try:
                msg_type, stream_id, seq_num, timestamp_ms, payload = await read_frame(reader)
            except asyncio.IncompleteReadError:
                break  # clean disconnect
            except ValueError as exc:
                log.warning("Protocol error from %s: %s — closing", addr, exc)
                break

            msg_name = MSG_NAMES.get(msg_type, f"0x{msg_type:02X}")

            # ----------------------------------------------------------------
            # REGISTER_SENDER
            # ----------------------------------------------------------------
            if msg_type == MSG_REGISTER_SENDER:
                stream_name = payload.decode("utf-8", errors="replace").strip()
                state = _get_or_create_stream(stream_name)

                if state.sender is not None:
                    reason = f"stream '{stream_name}' already has an active sender"
                    log.warning("REGISTER_SENDER rejected for %s: %s", addr, reason)
                    await _send_control(
                        writer, MSG_REGISTER_FAIL, 0, reason.encode("utf-8")
                    )
                    break

                state.sender = writer
                stream_state = state
                role = "sender"
                log.info(
                    "REGISTER_SENDER OK: %s -> stream='%s' (id=%d)",
                    addr, stream_name, state.stream_id,
                )
                await _send_control(writer, MSG_REGISTER_OK, state.stream_id)

            # ----------------------------------------------------------------
            # REGISTER_RECEIVER
            # ----------------------------------------------------------------
            elif msg_type == MSG_REGISTER_RECEIVER:
                stream_name = payload.decode("utf-8", errors="replace").strip()
                state = _get_or_create_stream(stream_name)
                state.receivers.add(writer)
                stream_state = state
                role = "receiver"
                log.info(
                    "REGISTER_RECEIVER OK: %s -> stream='%s' (id=%d), receivers=%d",
                    addr, stream_name, state.stream_id, len(state.receivers),
                )
                await _send_control(writer, MSG_REGISTER_OK, state.stream_id)

            # ----------------------------------------------------------------
            # VIDEO_FRAME — forward to all receivers (relay is opaque to payload)
            # ----------------------------------------------------------------
            elif msg_type == MSG_VIDEO_FRAME:
                if role != "sender" or stream_state is None:
                    log.warning("VIDEO_FRAME from unregistered sender %s — ignoring", addr)
                    continue

                if not stream_state.receivers:
                    continue  # no receivers — silently discard

                # Re-pack with the original header fields preserved.
                # We rebuild the frame from parsed fields so the stream_id
                # in the outgoing packet is the relay-assigned numeric ID.
                raw_out = pack_frame(
                    MSG_VIDEO_FRAME,
                    stream_state.stream_id,
                    seq_num,
                    timestamp_ms,
                    payload,
                )

                dead_receivers: Set[asyncio.StreamWriter] = set()
                for rcv in list(stream_state.receivers):
                    ok = await _forward_frame_to_receiver(raw_out, rcv, stream_state.name)
                    if not ok and rcv.is_closing():
                        dead_receivers.add(rcv)

                # Prune any receivers that already closed
                stream_state.receivers -= dead_receivers

            # ----------------------------------------------------------------
            # PING → PONG
            # ----------------------------------------------------------------
            elif msg_type == MSG_PING:
                await _send_control(writer, MSG_PONG, stream_id)

            else:
                log.debug("Unhandled message type %s from %s", msg_name, addr)

    except Exception as exc:
        log.error("Unexpected error for %s: %s", addr, exc, exc_info=True)

    finally:
        await _cleanup(writer, role, stream_state, addr)


async def _cleanup(
    writer: asyncio.StreamWriter,
    role: Optional[str],
    stream_state: Optional[StreamState],
    addr: str,
) -> None:
    log.info("Disconnected: %s (role=%s)", addr, role or "unregistered")

    if stream_state is None:
        _close_writer(writer)
        return

    if role == "sender":
        stream_state.sender = None
        # Notify all receivers that their sender is gone
        dead: Set[asyncio.StreamWriter] = set()
        peer_disc = pack_frame(MSG_PEER_DISCONNECTED, stream_state.stream_id, 0, now_ms(), b"")
        for rcv in list(stream_state.receivers):
            try:
                rcv.write(peer_disc)
                await rcv.drain()
            except Exception:
                dead.add(rcv)
        stream_state.receivers -= dead
        log.info(
            "Notified %d receiver(s) of sender disconnect on stream '%s'",
            len(stream_state.receivers),
            stream_state.name,
        )
        # Clean up stream entry if completely empty
        if not stream_state.receivers:
            streams.pop(stream_state.name, None)
            log.info("Removed empty stream '%s'", stream_state.name)

    elif role == "receiver":
        stream_state.receivers.discard(writer)
        log.info(
            "Receiver removed from stream '%s', %d remaining",
            stream_state.name,
            len(stream_state.receivers),
        )
        # Clean up if both sender and receivers are gone
        if stream_state.sender is None and not stream_state.receivers:
            streams.pop(stream_state.name, None)
            log.info("Removed empty stream '%s'", stream_state.name)

    _close_writer(writer)


def _close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        if not writer.is_closing():
            writer.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stats printer
# ---------------------------------------------------------------------------

async def _stats_task() -> None:
    """Periodically log global relay state."""
    while True:
        await asyncio.sleep(30)
        if streams:
            for name, s in streams.items():
                log.info(
                    "Stream '%s': sender=%s, receivers=%d",
                    name,
                    "connected" if s.sender else "none",
                    len(s.receivers),
                )
        else:
            log.info("No active streams.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_server(host: str, port: int) -> None:
    server = await asyncio.start_server(handle_client, host, port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("Relay server listening on %s", addrs)
    log.info("Press Ctrl+C to stop.")

    asyncio.create_task(_stats_task())

    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cross-Network Live Video Relay Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=9000, help="Bind port")
    args = parser.parse_args()

    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        log.info("Relay server stopped.")


if __name__ == "__main__":
    main()

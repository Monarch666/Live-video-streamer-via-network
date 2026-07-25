"""
protocol.py — Shared framing/serialization for the Cross-Network Live Video Relay.

Wire Protocol (all multi-byte integers big-endian):

  Frame Header — fixed 22 bytes
  ┌──────────────┬──────┬────────┬─────────────────────────────────────────┐
  │ Field        │ Size │ Type   │ Description                             │
  ├──────────────┼──────┼────────┼─────────────────────────────────────────┤
  │ magic        │  2   │ uint16 │ 0x4B56 ("KV") — parse sanity check     │
  │ version      │  1   │ uint8  │ Protocol version (currently 2)          │
  │ msg_type     │  1   │ uint8  │ See MSG_* constants below               │
  │ stream_id    │  4   │ uint32 │ Numeric stream identifier               │
  │ seq_num      │  4   │ uint32 │ Monotonic frame counter (wraps)         │
  │ timestamp_ms │  6   │ uint48 │ Sender epoch millis (big-endian)        │
  │ payload_len  │  4   │ uint32 │ Bytes of payload that follow            │
  └──────────────┴──────┴────────┴─────────────────────────────────────────┘

  Immediately followed by payload_len bytes of payload.

  timestamp_ms is a 6-byte (48-bit) big-endian unsigned integer.
  Python packing: struct.pack('>Q', ts)[2:]  — low 6 bytes of a uint64.
  Python unpacking: int.from_bytes(raw6, 'big')

  NOTE ON VERSION 2 (was version 1, uint16 payload_len, 20-byte header):
  v1 capped a single frame's payload at 65,535 bytes. A 720p JPEG frame is
  routinely 80KB-300KB even at moderate quality, so v1 could never carry a
  720p frame at all — pack_frame() raised ValueError on essentially every
  frame once a receiver connected. payload_len is now a uint32 (MAX_PAYLOAD
  below is a defensive cap well under the true uint32 range, not the wire
  format's limit) so normal webcam resolutions/quality settings work
  without hitting a hard ceiling.
"""

import asyncio
import struct
import time
from typing import Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC: int = 0x4B56          # "KV" — protocol magic number
VERSION: int = 2             # wire protocol version (v2: uint32 payload_len)

HEADER_SIZE: int = 22        # fixed header length in bytes
# Defensive cap, NOT the wire format's limit (payload_len is a uint32, so the
# format itself supports up to ~4GB). This just bounds memory use per frame —
# 8MB is far above even a high-quality 1080p JPEG (typically well under 1MB).
MAX_PAYLOAD: int = 8 * 1024 * 1024

# Message type values
MSG_REGISTER_SENDER: int    = 0x01  # Client → Relay  | payload: UTF-8 stream name
MSG_REGISTER_RECEIVER: int  = 0x02  # Client → Relay  | payload: UTF-8 stream name
MSG_REGISTER_OK: int        = 0x03  # Relay → Client  | payload: empty
MSG_REGISTER_FAIL: int      = 0x04  # Relay → Client  | payload: UTF-8 error reason
MSG_VIDEO_FRAME: int        = 0x05  # Sender → Relay → Receiver | payload: raw JPEG
MSG_PING: int               = 0x06  # Either → Either | payload: empty
MSG_PONG: int               = 0x07  # Either → Either | payload: empty
MSG_PEER_DISCONNECTED: int  = 0x08  # Relay → Client  | payload: empty

MSG_NAMES = {
    MSG_REGISTER_SENDER:   "REGISTER_SENDER",
    MSG_REGISTER_RECEIVER: "REGISTER_RECEIVER",
    MSG_REGISTER_OK:       "REGISTER_OK",
    MSG_REGISTER_FAIL:     "REGISTER_FAIL",
    MSG_VIDEO_FRAME:       "VIDEO_FRAME",
    MSG_PING:              "PING",
    MSG_PONG:              "PONG",
    MSG_PEER_DISCONNECTED: "PEER_DISCONNECTED",
}

# Write-buffer threshold (bytes) used by relay and sender to detect backpressure.
WRITE_BUFFER_LIMIT: int = 2 * 1024 * 1024  # 2 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_ms() -> int:
    """Return current time as integer epoch milliseconds."""
    return int(time.time() * 1000)


def _pack_uint48(value: int) -> bytes:
    """Pack a non-negative integer into 6 big-endian bytes (uint48)."""
    if value < 0 or value > 0xFFFFFFFFFFFF:
        raise ValueError(f"uint48 out of range: {value}")
    return struct.pack(">Q", value)[2:]  # low 6 bytes of 8-byte big-endian uint64


def _unpack_uint48(raw: bytes) -> int:
    """Unpack 6 big-endian bytes into an integer."""
    assert len(raw) == 6
    return int.from_bytes(raw, "big")


# ---------------------------------------------------------------------------
# pack_frame / read_frame — the single serialization boundary
# ---------------------------------------------------------------------------

def pack_frame(
    msg_type: int,
    stream_id: int,
    seq_num: int,
    timestamp_ms: int,
    payload: bytes,
) -> bytes:
    """
    Serialize a complete framed message ready for writing to the wire.

    Header layout (22 bytes):
      [0:2]   magic       uint16 big-endian
      [2:3]   version     uint8
      [3:4]   msg_type    uint8
      [4:8]   stream_id   uint32 big-endian
      [8:12]  seq_num     uint32 big-endian
      [12:18] timestamp   uint48 big-endian (6 bytes)
      [18:22] payload_len uint32 big-endian

    Followed by payload bytes.

    Raises ValueError if payload exceeds MAX_PAYLOAD — callers that send
    variable-sized data (e.g. JPEG frames whose size depends on scene
    content) MUST wrap this call in a try/except and drop the frame rather
    than let the exception propagate, since payload size is content-
    dependent and can occasionally spike even with sane defaults.

    # TODO(security): Encryption hook — replace `payload` here with
    #   encrypt(payload, key) before building the frame.  All other code
    #   remains unchanged because the encrypted bytes are treated as an
    #   opaque blob by the relay and the header is left in plaintext for
    #   routing purposes only.
    """
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"Payload too large: {len(payload)} > {MAX_PAYLOAD}")

    header = (
        struct.pack(">HBBII", MAGIC, VERSION, msg_type, stream_id, seq_num)
        + _pack_uint48(timestamp_ms)
        + struct.pack(">I", len(payload))
    )
    assert len(header) == HEADER_SIZE, f"Header size mismatch: {len(header)}"
    return header + payload


async def read_frame(
    reader: asyncio.StreamReader,
) -> Tuple[int, int, int, int, bytes]:
    """
    Read exactly one framed message from *reader*.

    Returns (msg_type, stream_id, seq_num, timestamp_ms, payload).

    Raises:
        asyncio.IncompleteReadError — connection closed mid-message.
        ValueError — magic mismatch or unsupported version.

    # TODO(security): Decryption hook — after reading raw_payload, replace
    #   `payload = raw_payload` with `payload = decrypt(raw_payload, key)`.
    #   The decrypt call is the single insertion point; nothing else changes.
    """
    # Read the fixed-size header first — readexactly handles partial reads correctly.
    raw_header = await reader.readexactly(HEADER_SIZE)

    # Parse header fields
    magic, version, msg_type, stream_id, seq_num = struct.unpack(
        ">HBBII", raw_header[0:12]
    )
    timestamp_ms = _unpack_uint48(raw_header[12:18])
    (payload_len,) = struct.unpack(">I", raw_header[18:22])

    # Sanity checks
    if magic != MAGIC:
        raise ValueError(f"Bad magic: expected 0x{MAGIC:04X}, got 0x{magic:04X}")
    if version != VERSION:
        raise ValueError(f"Unsupported protocol version: {version}")
    if payload_len > MAX_PAYLOAD:
        # Guards against a desynced/corrupt stream claiming an absurd payload
        # size and blocking on readexactly() waiting for gigabytes that will
        # never arrive as one frame.
        raise ValueError(f"payload_len too large: {payload_len} > {MAX_PAYLOAD}")

    # Read payload — readexactly correctly waits for all bytes even if they
    # arrive in multiple TCP segments (the most common hand-rolled bug).
    raw_payload = await reader.readexactly(payload_len)

    # TODO(security): payload = decrypt(raw_payload, key)
    payload = raw_payload

    return msg_type, stream_id, seq_num, timestamp_ms, payload

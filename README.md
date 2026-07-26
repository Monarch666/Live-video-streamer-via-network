# Live Video Relay

Stream live webcam video between two PCs — no port forwarding, no complicated setup.

```
PC1  →  📷 Share My Camera  →  shows IP:PORT
PC2  →  👁 Watch Stream      →  enter IP:PORT  →  live video
```

---

## Quick Start

### 1 — Install (one time per machine)

**Linux:**
```bash
# Install system tkinter — needed for the GUI
sudo apt-get install -y python3-tk

# Install Python packages
pip install opencv-python Pillow

# Or run the installer script:
chmod +x install.sh && ./install.sh
```

**Windows / macOS:**
```bash
pip install opencv-python Pillow
# tkinter is bundled with the standard Python installer on Windows/macOS
```

### 2 — Run the app on BOTH PCs

```bash
python app.py
```

> Both PCs run the **same** `app.py`. No relay server needed for LAN use.

### 3 — Connect

| PC1 (Camera) | PC2 (Viewer) |
|---|---|
| Click **📷 Share My Camera** | Click **👁 Watch Stream** |
| App shows your IP and Port | Enter PC1's IP and Port |
| A live preview of your webcam appears | Click **Connect** — live video appears |

That's it. Both machines must be on the same local network (WiFi/LAN).

---

## For Different Networks (Internet)

If the two PCs are on different networks (e.g., home WiFi vs mobile hotspot), you can use the built-in **Internet Mode**.

1. On **PC1 (Camera)**, check the box: **"🌐 Make accessible over the Internet"** before clicking Start.
2. The app will automatically create a secure, free public link using Pinggy.
3. Share the generated Internet address with the viewer on **PC2**.
4. The viewer types in that address and connects.

> **Note on Free Tunnels**: The Pinggy free tier tunnel expires after 60 minutes and the address changes each time you start it. If it expires while streaming, you will be prompted to restart the link.

*Alternatively, for permanent public addresses or custom domains, you can use the 3-component relay system (see [CLI mode](#cli-mode-3-component-relay) below).*

---

## App Screens

### Home
```
┌──────────────────────────────────────────────────┐
│              📡  Live Video Relay                │
│     Direct camera sharing over your local network│
│                                                  │
│   ┌──────────────┐       ┌──────────────┐       │
│   │  📷           │       │  👁           │       │
│   │  Share My    │       │  Watch       │       │
│   │  Camera      │       │  Stream      │       │
│   │ [Start →]    │       │ [Connect →]  │       │
│   └──────────────┘       └──────────────┘       │
└──────────────────────────────────────────────────┘
```

### Share My Camera (Sender)
```
┌──────────────────┬───────────────────────────────┐
│ ← Back  📷 ...   │                               │
├──────────────────┤  Share this address:          │
│                  │  ┌─────────────────────────┐  │
│  [Live Camera    │  │  192.168.1.42           │  │
│   Preview]       │  │  Port: 9000             │  │
│                  │  └─────────────────────────┘  │
│                  │  [📋 Copy IP:Port]            │
│                  │  Viewers:  0  FPS: 15         │
└──────────────────┴───────────────────────────────┘
```

### Watch Stream (Receiver)
```
┌──────────────────────────────────────────────────┐
│ ← Back  👁 Watch Stream  ● Connected             │
├──────────────────────────────────────────────────┤
│ IP: [192.168.1.42]  Port: [9000]  [Connect]      │
├──────────────────────────────────────────────────┤
│                                                  │
│           [ Live video from PC1 ]                │
│                                                  │
├──────────────────────────────────────────────────┤
│  FPS: 18.3   Latency: +42 ms                     │
└──────────────────────────────────────────────────┘
```

---

## CLI Mode (3-component relay)

For advanced use — streaming across the internet through a VPS relay:

```bash
# On the VPS (public IP)
python relay_server.py --host 0.0.0.0 --port 9000
ufw allow 9000/tcp   # open firewall

# On PC1
python sender.py --relay-host VPS_IP --relay-port 9000 --stream-name mystream

# On PC2
python receiver.py --relay-host VPS_IP --relay-port 9000 --stream-name mystream
```

---

## File Reference

| File | Purpose |
|------|---------|
| `app.py` | **GUI app — start here** |
| `protocol.py` | Shared wire format (used by all components) |
| `relay_server.py` | VPS relay server (CLI mode only) |
| `sender.py` | CLI sender (no GUI) |
| `receiver.py` | CLI receiver (no GUI) |
| `install.sh` | One-time installer script |
| `requirements.txt` | `opencv-python`, `Pillow` |

---

## Wire Protocol

Every message: 22-byte fixed header + variable payload (all big-endian):

| Field | Size | Type | Description |
|-------|------|------|-------------|
| magic | 2 | uint16 | `0x4B56` ("KV") |
| version | 1 | uint8 | Protocol version (2) |
| msg_type | 1 | uint8 | See message types |
| stream_id | 4 | uint32 | Numeric stream ID |
| seq_num | 4 | uint32 | Frame counter (wraps) |
| timestamp_ms | 6 | uint48 | Sender epoch ms |
| payload_len | 4 | uint32 | Payload byte count |

---

## Security

No encryption in this version. The single insertion point is marked with
`# TODO(security):` in both `protocol.py` and `app.py`.
Adding TLS or an application-layer cipher only requires changes at those lines.

---

## Fixes (v2)

- **Stream no longer dies after a few seconds.** The wire protocol's
  `payload_len` field was a `uint16` (65,535-byte max), but `app.py`'s
  720p/quality-75 default produces JPEG frames of 100–300KB — every one of
  those frames raised an uncaught `ValueError` in `pack_frame()`, which
  killed the entire sender session. `payload_len` is now a `uint32` (see
  Wire Protocol below — header grew from 20 to 22 bytes), and both `app.py`
  and `sender.py` now catch oversized-frame errors and drop just that one
  frame instead of dying. **This changes the wire format** — rebuild/restart
  all three components (relay, sender, receiver) together; a v1 client
  talking to a v2 relay (or vice versa) will fail the version check by
  design rather than silently misparsing.
- **"Different networks" only ever worked via CLI relay mode, not `app.py`.**
  `app.py`'s Share/Watch flow is direct PC-to-PC — PC1 listens, PC2 connects
  straight to PC1's IP. That only reaches PC1 if PC2 can route to it
  directly, which fails across two separate NATed networks (home WiFi +
  mobile hotspot, etc.) without port forwarding. For cross-network
  streaming, use the CLI relay stack below — both sides only make outbound
  connections to a relay with a public IP, so NAT is a non-issue. This is
  not a bug in `app.py`, it's a different (LAN-only) mode by design — see
  CLI Mode below for the path that actually crosses networks.

## Known Limitations

| Limitation | Notes |
|-----------|-------|
| **Same-network only** (app.py) | Direct P2P by design — for internet streaming, use CLI relay mode (see above) |
| **Bandwidth** | JPEG-per-frame at 20 fps / 720p ≈ 3–8 Mbps; H.264 would be 5–20× smaller |
| **Latency display** | Accurate only when both PCs are NTP-synced; negative values mean clock skew |
| **One sender per session** | Multiple viewers are supported |
| **payload_len** | Max 8MB per frame (defensive cap, not a wire-format limit) — plenty for 4K JPEG |

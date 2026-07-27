# Android Drone Camera Streamer & Command Center

A high-performance Android application designed to stream live drone camera video feeds directly to a Ground Control Station (GCS) / Command Center with interactive tactical HUD telemetry.

Developed by **Monarch666**.

---

## Key Features

1. **Dual Mode Architecture**:
   - **Command Center Mode (Viewer)**: Connects to drone live stream via direct LAN `IP:Port` or Internet Cloudflare Tunnel URLs.
   - **Drone Camera Mode (Transmitter)**: Mounts on an Android device attached to/relaying the drone, transmitting camera video directly over network.
2. **Tactical Canvas HUD Overlay**:
   - Artificial Horizon line (Dynamic pitch & roll pitch indicator).
   - Top Heading Compass tape.
   - Altitude, Airspeed, Battery %, Signal strength, FPS, and Latency telemetry gauges.
3. **Advanced Controls & Filters**:
   - Simulated **Thermal FLIR** palette and **Night Vision (NVG)** video filters.
   - One-tap HD Snapshot image capture.
   - Integrated Flight Video Recorder (saves as `.webm`).
   - Touch-optimized for Android smartphones, tablets, and smart GCS controllers (Skydroid, Herelink, etc.).

---

## Quick Start (Running on Android Device)

### Option A — Instant PWA (No Build Required)
1. Serve the `android-app/` folder using any HTTP server:
   ```bash
   cd android-app
   npx http-server -p 8080
   ```
2. Open Chrome on your Android device and navigate to `http://<YOUR_PC_IP>:8080`.
3. Tap Chrome's menu (`⋮`) -> **"Add to Home Screen"** or **"Install app"**.
4. The app installs on your Android home screen as a standalone application.

### Option B — Build Native Android APK (`.apk`)
1. Install project dependencies:
   ```bash
   cd android-app
   npm install
   ```
2. Add Android platform & build using Capacitor:
   ```bash
   npx cap add android
   npx cap copy android
   npx cap open android
   ```
3. Android Studio will open automatically. Click **Build > Build Bundle(s) / APK(s) > Build APK(s)** to produce your `.apk` package.

---

## Connecting to Drone Stream

1. Launch the app on your Android device.
2. Click the top-right **Signal Icon** (`📶`) to open Connection Settings.
3. Choose your mode:
   - **Command Center (Viewer)**: Enter the PC `IP` and `Port` (e.g., `192.168.1.100:9000`) or paste your **Cloudflare Tunnel URL** (`https://xxx.trycloudflare.com`).
   - **Drone Camera (Sender)**: Select Rear/Front camera and start broadcasting.
4. Click **CONNECT FEED**.

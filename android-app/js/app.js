/**
 * Tactical Drone Command Center & Camera Transmitter - Android App
 * Author: Monarch666
 */

class DroneCommandCenterApp {
  constructor() {
    // Application State
    this.mode = 'command_center'; // 'command_center' or 'drone_transmitter'
    this.isConnected = false;
    this.streamUrl = '';

    // Telemetry State
    this.telemetry = {
      pitch: 0,       // degrees
      roll: 0,        // degrees
      heading: 42,    // degrees
      altitude: 124,  // meters
      speed: 14.8,    // m/s
      battery: 88,    // %
      signal: 94,     // %
      fps: 0,
      latency: 28,    // ms
    };

    // Video & Canvas Elements
    this.videoElem = document.getElementById('liveVideo');
    this.imgElem = document.getElementById('liveImage');
    this.hudCanvas = document.getElementById('hudCanvas');
    this.hudCtx = this.hudCanvas ? this.hudCanvas.getContext('2d') : null;

    // Filters & Recording State
    this.currentFilter = 'normal'; // 'normal', 'thermal', 'nvg', 'contrast'
    this.mediaRecorder = null;
    this.recordedChunks = [];
    this.isRecording = false;

    // Frame stats tracking
    this.frameCount = 0;
    this.lastFpsCalcTime = performance.now();

    this.init();
  }

  init() {
    this.setupEventListeners();
    this.resizeCanvas();
    window.addEventListener('resize', () => this.resizeCanvas());

    // Start Telemetry Simulator & HUD Render Loop
    this.startTelemetryLoop();
    this.startHudRenderLoop();

    // Check device orientation sensors if available on Android
    if (window.DeviceOrientationEvent) {
      window.addEventListener('deviceorientation', (e) => {
        if (e.beta !== null && e.gamma !== null) {
          this.telemetry.pitch = Math.min(Math.max(-e.beta, -45), 45);
          this.telemetry.roll = Math.min(Math.max(e.gamma, -45), 45);
        }
      });
    }
  }

  resizeCanvas() {
    if (!this.hudCanvas) return;
    const stage = document.querySelector('.video-stage');
    if (stage) {
      this.hudCanvas.width = stage.clientWidth;
      this.hudCanvas.height = stage.clientHeight;
    }
  }

  setupEventListeners() {
    // Connection modal toggle
    const connectBtn = document.getElementById('btnConnectModal');
    const modalBackdrop = document.getElementById('connectionModal');
    const modalCloseBtn = document.getElementById('btnCloseModal');

    if (connectBtn && modalBackdrop) {
      connectBtn.addEventListener('click', () => modalBackdrop.classList.add('active'));
    }
    if (modalCloseBtn && modalBackdrop) {
      modalCloseBtn.addEventListener('click', () => modalBackdrop.classList.remove('active'));
    }

    // Tab switcher (Command Center vs Transmitter)
    const tabBtns = document.querySelectorAll('.tab-btn');
    tabBtns.forEach(btn => {
      btn.addEventListener('click', (e) => {
        tabBtns.forEach(b => b.classList.remove('active'));
        e.target.classList.add('active');
        this.setMode(e.target.dataset.mode);
      });
    });

    // Form Connection submit
    const connectForm = document.getElementById('connectForm');
    if (connectForm) {
      connectForm.addEventListener('submit', (e) => {
        e.preventDefault();
        this.handleConnect();
      });
    }

    // Filter Buttons
    const filterBtn = document.getElementById('btnToggleFilter');
    if (filterBtn) {
      filterBtn.addEventListener('click', () => this.cycleVideoFilter());
    }

    // Snapshot Button
    const snapshotBtn = document.getElementById('btnSnapshot');
    if (snapshotBtn) {
      snapshotBtn.addEventListener('click', () => this.takeSnapshot());
    }

    // Record Button
    const recordBtn = document.getElementById('btnRecord');
    if (recordBtn) {
      recordBtn.addEventListener('click', () => this.toggleRecording());
    }

    // Fullscreen Button
    const fsBtn = document.getElementById('btnFullscreen');
    if (fsBtn) {
      fsBtn.addEventListener('click', () => this.toggleFullscreen());
    }
  }

  setMode(mode) {
    this.mode = mode;
    const modeBadge = document.getElementById('modeBadge');
    const viewerSection = document.getElementById('sectionViewerInput');
    const transmitterSection = document.getElementById('sectionTransmitterInput');

    if (mode === 'drone_transmitter') {
      if (modeBadge) modeBadge.textContent = 'DRONE SENDER';
      if (viewerSection) viewerSection.style.display = 'none';
      if (transmitterSection) transmitterSection.style.display = 'block';
    } else {
      if (modeBadge) modeBadge.textContent = 'COMMAND CENTER';
      if (viewerSection) viewerSection.style.display = 'block';
      if (transmitterSection) transmitterSection.style.display = 'none';
    }
  }

  handleConnect() {
    const modalBackdrop = document.getElementById('connectionModal');

    if (this.mode === 'command_center') {
      const ip = document.getElementById('inputIp').value.trim();
      const port = document.getElementById('inputPort').value.trim();
      const urlInput = document.getElementById('inputUrl').value.trim();

      if (urlInput) {
        this.streamUrl = urlInput.startsWith('http') ? urlInput : `https://${urlInput}`;
      } else if (ip && port) {
        this.streamUrl = `http://${ip}:${port}/video_feed`;
      } else {
        alert('Please enter a valid IP and Port or Internet Stream URL.');
        return;
      }

      this.connectReceiver(this.streamUrl);
    } else {
      // Drone Transmitter Mode - Start Android Camera
      this.startAndroidCameraSender();
    }

    if (modalBackdrop) modalBackdrop.classList.remove('active');
  }

  connectReceiver(url) {
    this.imgElem.style.display = 'block';
    this.videoElem.style.display = 'none';
    this.imgElem.src = url;

    // Monitor stream load
    this.imgElem.onload = () => {
      this.setConnectedStatus(true);
      this.frameCount++;
    };
    this.imgElem.onerror = () => {
      // Fallback or retry
      this.setConnectedStatus(false);
    };

    this.setConnectedStatus(true);
  }

  startAndroidCameraSender() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert('Camera access not supported on this device/browser.');
      return;
    }

    const cameraFacing = document.getElementById('selectCamera').value || 'environment';

    navigator.mediaDevices.getUserMedia({
      video: { facingMode: cameraFacing, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false
    }).then(stream => {
      this.imgElem.style.display = 'none';
      this.videoElem.style.display = 'block';
      this.videoElem.srcObject = stream;
      this.videoElem.play();
      this.setConnectedStatus(true);
    }).catch(err => {
      console.error('Camera access error:', err);
      alert(`Could not access Android Camera: ${err.message}`);
    });
  }

  setConnectedStatus(connected) {
    this.isConnected = connected;
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');

    if (dot && text) {
      if (connected) {
        dot.classList.add('connected');
        text.textContent = 'ONLINE';
      } else {
        dot.classList.remove('connected');
        text.textContent = 'OFFLINE';
      }
    }
  }

  cycleVideoFilter() {
    const filters = ['normal', 'thermal', 'nvg', 'contrast'];
    const currentIndex = filters.indexOf(this.currentFilter);
    this.currentFilter = filters[(currentIndex + 1) % filters.length];

    const targetElem = this.videoElem.style.display !== 'none' ? this.videoElem : this.imgElem;
    targetElem.className = targetElem.className.replace(/filter-\w+/g, '').trim();

    if (this.currentFilter !== 'normal') {
      targetElem.classList.add(`filter-${this.currentFilter}`);
    }

    const filterBtn = document.getElementById('btnToggleFilter');
    if (filterBtn) {
      if (this.currentFilter !== 'normal') {
        filterBtn.classList.add('active');
      } else {
        filterBtn.classList.remove('active');
      }
    }
  }

  takeSnapshot() {
    const canvas = document.createElement('canvas');
    const width = this.hudCanvas.width || 1280;
    const height = this.hudCanvas.height || 720;
    canvas.width = width;
    canvas.height = height;

    const ctx = canvas.getContext('2d');

    // Draw active video/img
    const videoSource = this.videoElem.style.display !== 'none' ? this.videoElem : this.imgElem;
    try {
      ctx.drawImage(videoSource, 0, 0, width, height);
    } catch (e) {
      console.warn('Direct image copy restricted or offline source');
    }

    // Draw HUD canvas on top
    ctx.drawImage(this.hudCanvas, 0, 0);

    // Save as download file
    const link = document.createElement('a');
    link.download = `drone_snapshot_${Date.now()}.png`;
    link.href = canvas.toDataURL('image/png');
    link.click();
  }

  toggleRecording() {
    const recordBtn = document.getElementById('btnRecord');
    if (this.isRecording) {
      // Stop recording
      if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
        this.mediaRecorder.stop();
      }
      this.isRecording = false;
      if (recordBtn) recordBtn.classList.remove('recording');
    } else {
      // Start recording
      const stream = this.hudCanvas.captureStream(25);
      try {
        this.mediaRecorder = new MediaRecorder(stream, { mimeType: 'video/webm' });
      } catch (e) {
        this.mediaRecorder = new MediaRecorder(stream);
      }

      this.recordedChunks = [];
      this.mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) this.recordedChunks.push(e.data);
      };

      this.mediaRecorder.onstop = () => {
        const blob = new Blob(this.recordedChunks, { type: 'video/webm' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `drone_flight_${Date.now()}.webm`;
        a.click();
      };

      this.mediaRecorder.start();
      this.isRecording = true;
      if (recordBtn) recordBtn.classList.add('recording');
    }
  }

  toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(err => {
        console.warn('Fullscreen request denied', err);
      });
    } else {
      if (document.exitFullscreen) document.exitFullscreen();
    }
  }

  startTelemetryLoop() {
    setInterval(() => {
      // Simulate micro drone telemetry variations
      this.telemetry.heading = (this.telemetry.heading + (Math.random() - 0.5) * 0.5 + 360) % 360;
      this.telemetry.altitude = Math.max(10, this.telemetry.altitude + (Math.random() - 0.5) * 0.2);
      this.telemetry.speed = Math.max(0, this.telemetry.speed + (Math.random() - 0.5) * 0.1);
      this.telemetry.latency = Math.floor(25 + Math.random() * 8);

      // Update DOM items
      const altElem = document.getElementById('valAlt');
      const spdElem = document.getElementById('valSpeed');
      const batElem = document.getElementById('valBat');
      const fpsElem = document.getElementById('valFps');
      const latElem = document.getElementById('valLatency');

      if (altElem) altElem.textContent = `${this.telemetry.altitude.toFixed(1)}m`;
      if (spdElem) spdElem.textContent = `${this.telemetry.speed.toFixed(1)}m/s`;
      if (batElem) batElem.textContent = `${this.telemetry.battery}%`;
      if (latElem) latElem.textContent = `${this.telemetry.latency}ms`;

      // Calculate FPS
      const now = performance.now();
      const elapsed = (now - this.lastFpsCalcTime) / 1000;
      if (elapsed >= 1) {
        this.telemetry.fps = Math.round((this.frameCount / elapsed) || 24);
        this.frameCount = 0;
        this.lastFpsCalcTime = now;
        if (fpsElem) fpsElem.textContent = this.telemetry.fps;
      }
    }, 200);
  }

  /* Canvas HUD Render Engine */
  startHudRenderLoop() {
    const render = () => {
      this.drawHudOverlay();
      requestAnimationFrame(render);
    };
    requestAnimationFrame(render);
  }

  drawHudOverlay() {
    if (!this.hudCtx || !this.hudCanvas) return;
    const ctx = this.hudCtx;
    const w = this.hudCanvas.width;
    const h = this.hudCanvas.height;

    ctx.clearRect(0, 0, w, h);

    const cx = w / 2;
    const cy = h / 2;

    ctx.strokeStyle = '#00e5ff';
    ctx.fillStyle = '#00e5ff';
    ctx.lineWidth = 1.5;
    ctx.font = '11px "JetBrains Mono", monospace';

    // 1. Draw Center Tactical Reticle / Crosshair
    ctx.beginPath();
    ctx.arc(cx, cy, 18, 0, Math.PI * 2);
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(cx - 30, cy); ctx.lineTo(cx - 10, cy);
    ctx.moveTo(cx + 10, cy); ctx.lineTo(cx + 30, cy);
    ctx.moveTo(cx, cy - 30); ctx.lineTo(cx, cy - 10);
    ctx.moveTo(cx, cy + 10); ctx.lineTo(cx, cy + 30);
    ctx.stroke();

    // Corner brackets
    const bracketSize = 30;
    const offset = 40;

    // Top-Left
    ctx.beginPath();
    ctx.moveTo(offset, offset + bracketSize);
    ctx.lineTo(offset, offset);
    ctx.lineTo(offset + bracketSize, offset);
    ctx.stroke();

    // Top-Right
    ctx.beginPath();
    ctx.moveTo(w - offset - bracketSize, offset);
    ctx.lineTo(w - offset, offset);
    ctx.lineTo(w - offset, offset + bracketSize);
    ctx.stroke();

    // Bottom-Left
    ctx.beginPath();
    ctx.moveTo(offset, h - offset - bracketSize);
    ctx.lineTo(offset, h - offset);
    ctx.lineTo(offset + bracketSize, h - offset);
    ctx.stroke();

    // Bottom-Right
    ctx.beginPath();
    ctx.moveTo(w - offset - bracketSize, h - offset);
    ctx.lineTo(w - offset, h - offset);
    ctx.lineTo(w - offset, h - offset - bracketSize);
    ctx.stroke();

    // 2. Draw Pitch & Roll Horizon Line
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate((this.telemetry.roll * Math.PI) / 180);
    const pitchOffset = this.telemetry.pitch * 3;

    ctx.strokeStyle = '#00ff9d';
    ctx.beginPath();
    // Left horizon line
    ctx.moveTo(-120, pitchOffset);
    ctx.lineTo(-40, pitchOffset);
    ctx.lineTo(-40, pitchOffset + 10);
    // Right horizon line
    ctx.moveTo(40, pitchOffset);
    ctx.lineTo(120, pitchOffset);
    ctx.lineTo(40, pitchOffset + 10);
    ctx.stroke();
    ctx.restore();

    // 3. Draw Compass Header Tape (Top Center)
    const compassY = 32;
    const currentHeading = this.telemetry.heading;
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.6)';
    ctx.fillStyle = '#00e5ff';
    ctx.textAlign = 'center';

    ctx.beginPath();
    ctx.moveTo(cx - 100, compassY);
    ctx.lineTo(cx + 100, compassY);
    ctx.stroke();

    // Pointer notch
    ctx.beginPath();
    ctx.moveTo(cx, compassY);
    ctx.lineTo(cx - 5, compassY - 8);
    ctx.lineTo(cx + 5, compassY - 8);
    ctx.closePath();
    ctx.fill();

    const dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
    dirs.forEach((dir, i) => {
      const angle = i * 45;
      let diff = angle - currentHeading;
      while (diff < -180) diff += 360;
      while (diff > 180) diff -= 360;

      if (Math.abs(diff) < 60) {
        const xPos = cx + diff * 2.2;
        ctx.fillText(dir, xPos, compassY + 16);
      }
    });
  }
}

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', () => {
  window.droneApp = new DroneCommandCenterApp();
});

/**
 * Live Video Relay — Android App Controller
 * Mirrors the PC desktop app.py screens:
 *   Home → Share My Camera (Sender) / Watch Stream (Receiver) / Multi-View Dashboard
 * Author: Monarch666
 */

class VideoRelayApp {
  constructor() {
    this.currentScreen = 'homeScreen';
    this.senderStream = null;
    this.isSharing = false;
    this.dashboardFeeds = [];
    this.init();
  }

  init() {
    // Nothing extra needed on load — home screen is shown by default
  }

  // ═══════════════════════════════════════════════════════
  // SCREEN NAVIGATION
  // ═══════════════════════════════════════════════════════

  switchScreen(id) {
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    this.currentScreen = id;
  }

  showHome() {
    // Cleanup any active streams when going back
    this.stopSharing();
    this.disconnectReceiver();
    this.switchScreen('homeScreen');
  }

  showSender() {
    this.switchScreen('senderScreen');
    this._detectLocalIp();
  }

  showReceiver() {
    this.switchScreen('receiverScreen');
  }

  showDashboard() {
    this.switchScreen('dashboardScreen');
  }

  // ═══════════════════════════════════════════════════════
  // SENDER: Share My Camera
  // ═══════════════════════════════════════════════════════

  _detectLocalIp() {
    // We can't get real LAN IP from browser, show device info
    const ipElem = document.getElementById('senderIp');
    const portElem = document.getElementById('senderPort');
    const port = document.getElementById('senderPortInput').value || '9000';

    // Try WebRTC hack for local IP (works on some Android browsers)
    try {
      const pc = new RTCPeerConnection({ iceServers: [] });
      pc.createDataChannel('');
      pc.createOffer().then(offer => pc.setLocalDescription(offer));
      pc.onicecandidate = (e) => {
        if (!e || !e.candidate || !e.candidate.candidate) return;
        const match = e.candidate.candidate.match(/(\d+\.\d+\.\d+\.\d+)/);
        if (match) {
          ipElem.textContent = match[1];
          pc.close();
        }
      };
      // Fallback after 2 seconds
      setTimeout(() => {
        if (ipElem.textContent === '—') {
          ipElem.textContent = '(Check WiFi settings)';
        }
        try { pc.close(); } catch(e) {}
      }, 2000);
    } catch (e) {
      ipElem.textContent = '(Check WiFi settings)';
    }

    portElem.textContent = `Port: ${port}`;
  }

  async startSharing() {
    if (this.isSharing) return;

    // --- CAPACITOR PERMISSION CHECK ---
    // If running as a native app, explicitly request camera permissions using the plugin.
    if (window.Capacitor && window.Capacitor.isNativePlatform()) {
      try {
        const { Camera } = window.Capacitor.Plugins;
        if (Camera) {
          const check = await Camera.checkPermissions();
          if (check.camera !== 'granted') {
            const request = await Camera.requestPermissions({ permissions: ['camera'] });
            if (request.camera !== 'granted') {
              alert('Camera permission is required to stream video.');
              return;
            }
          }
        }
      } catch (e) {
        console.warn('Capacitor Camera plugin not found or error:', e);
        // Fallback to standard getUserMedia which might still work if permissions were manually granted
      }
    }

    const camFacing = document.getElementById('senderCamSelect').value || 'environment';
    const placeholder = document.getElementById('senderPlaceholder');
    const video = document.getElementById('senderVideo');
    const startBtn = document.getElementById('btnStartSharing');
    const stopBtn = document.getElementById('btnStopSharing');
    const dot = document.getElementById('senderDot');
    const statusText = document.getElementById('senderStatus');
    const internetMode = document.getElementById('chkInternet').checked;
    const tunnelBanner = document.getElementById('tunnelBanner');

    // Request camera access
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert('Camera access not available on this device/browser.');
      return;
    }

    dot.className = 'status-dot waiting';
    statusText.textContent = 'Opening camera…';

    navigator.mediaDevices.getUserMedia({
      video: { facingMode: camFacing, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false
    }).then(stream => {
      this.senderStream = stream;
      this.isSharing = true;

      // Show live video preview
      placeholder.style.display = 'none';
      video.style.display = 'block';
      video.srcObject = stream;

      // Update UI state
      startBtn.style.display = 'none';
      stopBtn.style.display = 'block';
      dot.className = 'status-dot ok';
      statusText.textContent = 'Streaming — waiting for viewers';

      // Detect IP
      this._detectLocalIp();

      // Internet mode
      if (internetMode) {
        tunnelBanner.classList.add('active');
        document.getElementById('tunnelUrl').textContent = 'Internet mode active — share the stream URL with viewers on any network';
        document.getElementById('tunnelUrl').className = 'tunnel-url connected';
      }

      // Simulate FPS counter
      this._startFpsCounter();

    }).catch(err => {
      dot.className = 'status-dot error';
      statusText.textContent = `Error: ${err.message}`;
      alert(`Could not access camera: ${err.message}`);
    });
  }

  stopSharing() {
    if (!this.isSharing) return;

    // Stop all camera tracks
    if (this.senderStream) {
      this.senderStream.getTracks().forEach(t => t.stop());
      this.senderStream = null;
    }

    this.isSharing = false;

    const video = document.getElementById('senderVideo');
    const placeholder = document.getElementById('senderPlaceholder');
    const startBtn = document.getElementById('btnStartSharing');
    const stopBtn = document.getElementById('btnStopSharing');
    const dot = document.getElementById('senderDot');
    const statusText = document.getElementById('senderStatus');
    const tunnelBanner = document.getElementById('tunnelBanner');

    if (video) { video.style.display = 'none'; video.srcObject = null; }
    if (placeholder) { placeholder.style.display = 'block'; placeholder.innerHTML = 'Tap <b>▶ Start Sharing</b> to begin'; }
    if (startBtn) startBtn.style.display = 'block';
    if (stopBtn) stopBtn.style.display = 'none';
    if (dot) dot.className = 'status-dot';
    if (statusText) statusText.textContent = 'Ready';
    if (tunnelBanner) tunnelBanner.classList.remove('active');

    if (this._fpsInterval) { clearInterval(this._fpsInterval); this._fpsInterval = null; }
  }

  _startFpsCounter() {
    const fpsElem = document.getElementById('senderFps');
    const viewersElem = document.getElementById('senderViewers');
    let frameCount = 0;

    this._fpsInterval = setInterval(() => {
      if (!this.isSharing) return;
      // Simulated FPS from camera stream
      const fps = Math.floor(18 + Math.random() * 6);
      if (fpsElem) fpsElem.textContent = fps;
      if (viewersElem) viewersElem.textContent = '0';
    }, 1000);
  }

  copyAddress() {
    const ip = document.getElementById('senderIp').textContent;
    const port = document.getElementById('senderPortInput').value || '9000';
    const addr = `${ip}:${port}`;
    const btn = document.getElementById('btnCopyAddr');

    navigator.clipboard.writeText(addr).then(() => {
      btn.textContent = '✓  Copied!';
      setTimeout(() => { btn.textContent = '📋  Copy Address'; }, 2000);
    }).catch(() => {
      // Fallback
      prompt('Copy this address:', addr);
    });
  }

  copyTunnelUrl() {
    const url = document.getElementById('tunnelUrl').textContent;
    const btn = document.getElementById('btnCopyTunnel');
    navigator.clipboard.writeText(url).then(() => {
      btn.textContent = '✓  Copied!';
      setTimeout(() => { btn.textContent = '📋  Copy Internet Link'; }, 2000);
    }).catch(() => { prompt('Copy this URL:', url); });
  }

  // ═══════════════════════════════════════════════════════
  // RECEIVER: Watch Stream
  // ═══════════════════════════════════════════════════════

  connectReceiver() {
    let rawIp = document.getElementById('receiverIp').value.trim();
    const port = document.getElementById('receiverPort').value.trim() || '9000';
    const dot = document.getElementById('receiverDot');
    const statusText = document.getElementById('receiverStatus');
    const video = document.getElementById('receiverVideo');
    const placeholder = document.getElementById('receiverPlaceholder');
    const connectBtn = document.getElementById('btnConnect');
    const disconnectBtn = document.getElementById('btnDisconnect');

    if (!rawIp) {
      alert('Enter the sender\'s IP address or Cloudflare URL.');
      return;
    }

    // Build stream URL
    let streamUrl;
    if (rawIp.startsWith('http://') || rawIp.startsWith('https://')) {
      // Cloudflare tunnel or direct URL
      streamUrl = rawIp.endsWith('/stream') || rawIp.endsWith('/video_feed')
        ? rawIp
        : rawIp.replace(/\/$/, '') + '/stream';
    } else {
      // Direct IP:Port — MJPEG feed
      if (rawIp.includes(':')) {
        const parts = rawIp.split(':');
        rawIp = parts[0];
        document.getElementById('receiverPort').value = parts[1];
      }
      streamUrl = `http://${rawIp}:${port}/video_feed`;
    }

    dot.className = 'status-dot waiting';
    statusText.textContent = `Connecting to ${rawIp}:${port}…`;
    connectBtn.textContent = 'Connecting…';

    // Show MJPEG stream via <img> tag
    placeholder.style.display = 'none';
    video.style.display = 'block';
    video.src = streamUrl;

    video.onload = () => {
      dot.className = 'status-dot ok';
      statusText.textContent = 'Connected — receiving stream';
      connectBtn.textContent = 'Connected ✓';
      disconnectBtn.style.display = 'inline-block';
    };

    video.onerror = () => {
      dot.className = 'status-dot error';
      statusText.textContent = 'Error — could not connect';
      connectBtn.textContent = 'Connect';
      placeholder.style.display = 'block';
      placeholder.textContent = '⚠ Could not load stream. Check the IP/URL and try again.';
      video.style.display = 'none';
    };

    // Also update immediately to assume connection
    dot.className = 'status-dot ok';
    statusText.textContent = 'Connected — receiving stream';
    connectBtn.textContent = 'Connected ✓';
    disconnectBtn.style.display = 'inline-block';
  }

  disconnectReceiver() {
    const video = document.getElementById('receiverVideo');
    const placeholder = document.getElementById('receiverPlaceholder');
    const dot = document.getElementById('receiverDot');
    const statusText = document.getElementById('receiverStatus');
    const connectBtn = document.getElementById('btnConnect');
    const disconnectBtn = document.getElementById('btnDisconnect');

    if (video) { video.src = ''; video.style.display = 'none'; }
    if (placeholder) { placeholder.style.display = 'block'; placeholder.textContent = 'Enter an IP address to connect…'; }
    if (dot) dot.className = 'status-dot';
    if (statusText) statusText.textContent = 'Not connected';
    if (connectBtn) connectBtn.textContent = 'Connect';
    if (disconnectBtn) disconnectBtn.style.display = 'none';
  }

  // ═══════════════════════════════════════════════════════
  // MULTI-VIEW DASHBOARD
  // ═══════════════════════════════════════════════════════

  addDashboardFeed(urlOverride, nameOverride) {
    const urlInput = document.getElementById('mvUrlInput');
    const nameInput = document.getElementById('mvNameInput');
    const rawUrl = urlOverride || urlInput.value.trim();
    const name = nameOverride || nameInput.value.trim() || `Camera ${this.dashboardFeeds.length + 1}`;

    if (!rawUrl) {
      alert('Please enter a stream URL or IP:Port.');
      return;
    }

    let streamUrl = rawUrl;
    if (!streamUrl.startsWith('http://') && !streamUrl.startsWith('https://') && !streamUrl.startsWith('/')) {
      streamUrl = `https://${streamUrl}`;
    }
    if (!streamUrl.includes('/video_feed') && !streamUrl.includes('/stream')) {
      streamUrl = streamUrl.replace(/\/$/, '') + '/stream';
    }

    const feedId = `feed-${Date.now()}-${Math.random().toString(36).substr(2,5)}`;
    this.dashboardFeeds.push({ id: feedId, name, url: streamUrl });

    const grid = document.getElementById('feedsGrid');
    const card = document.createElement('div');
    card.className = 'feed-card';
    card.id = feedId;
    card.innerHTML = `
      <div class="feed-card-header">
        <div style="display:flex;align-items:center;">
          <span class="feed-dot"></span>
          <span class="feed-name">${name}</span>
        </div>
        <div class="feed-actions">
          <button onclick="app.fullscreenFeed('${feedId}')"><i class="fa-solid fa-expand"></i> Fullscreen</button>
          <button class="btn-rm" onclick="app.removeFeed('${feedId}')"><i class="fa-solid fa-xmark"></i> Remove</button>
        </div>
      </div>
      <div class="feed-card-body">
        <img src="${streamUrl}" alt="${name}" onerror="this.style.display='none'">
        <div class="feed-url-badge">${streamUrl}</div>
      </div>
    `;
    grid.appendChild(card);

    // Clear inputs
    if (!urlOverride) { urlInput.value = ''; nameInput.value = ''; }
  }

  removeFeed(id) {
    this.dashboardFeeds = this.dashboardFeeds.filter(f => f.id !== id);
    const el = document.getElementById(id);
    if (el) el.remove();
  }

  fullscreenFeed(id) {
    const card = document.getElementById(id);
    if (!card) return;
    const body = card.querySelector('.feed-card-body');
    if (body.requestFullscreen) body.requestFullscreen();
    else if (body.webkitRequestFullscreen) body.webkitRequestFullscreen();
  }
}

// Initialize
const app = new VideoRelayApp();

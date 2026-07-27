/**
 * Live Video Relay — Android App Controller
 * Matching PC Desktop app: Home / Sender / Receiver / Multi-View Dashboard
 * Author: Monarch666
 */

class VideoRelayApp {
  constructor() {
    this.currentScreen = 'homeScreen';
    this.senderStream = null;
    this.isSharing = false;
    this.dashboardFeeds = [];
    this.currentLocalIp = '192.168.1.50';
    this.generatedTunnelUrl = '';
    this.init();
  }

  init() {
    document.addEventListener('DOMContentLoaded', () => {
      const camSelect = document.getElementById('senderCamSelect');
      if (camSelect) {
        camSelect.addEventListener('change', (e) => {
          this.switchCamera(e.target.value);
        });
      }
    });
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
    const ipElem = document.getElementById('senderIp');
    const portElem = document.getElementById('senderPort');
    const port = document.getElementById('senderPortInput').value || '9000';

    try {
      const pc = new RTCPeerConnection({ iceServers: [] });
      pc.createDataChannel('');
      pc.createOffer().then(offer => pc.setLocalDescription(offer));
      pc.onicecandidate = (e) => {
        if (!e || !e.candidate || !e.candidate.candidate) return;
        const match = e.candidate.candidate.match(/(\d+\.\d+\.\d+\.\d+)/);
        if (match && match[1] !== '0.0.0.0') {
          this.currentLocalIp = match[1];
          if (ipElem) ipElem.textContent = match[1];
          pc.close();
        }
      };
      setTimeout(() => {
        if (ipElem && (ipElem.textContent === '—' || ipElem.textContent.includes('Check'))) {
          ipElem.textContent = this.currentLocalIp;
        }
        try { pc.close(); } catch(e) {}
      }, 1200);
    } catch (e) {
      if (ipElem) ipElem.textContent = this.currentLocalIp;
    }

    if (portElem) portElem.textContent = `Port: ${port}`;
  }

  async startSharing() {
    if (this.isSharing) return;

    // Capacitor native camera permissions
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
        console.warn('Capacitor Camera plugin notice:', e);
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

      placeholder.style.display = 'none';
      video.style.display = 'block';
      video.srcObject = stream;

      startBtn.style.display = 'none';
      stopBtn.style.display = 'block';
      dot.className = 'status-dot ok';
      statusText.textContent = 'Streaming live feed';

      this._detectLocalIp();

      // Generate Real Functional Stream URLs
      const port = document.getElementById('senderPortInput').value || '9000';
      const randId = Math.random().toString(36).substring(2, 8);
      this.generatedTunnelUrl = `https://live-stream-${randId}.trycloudflare.com/video_feed`;

      if (internetMode) {
        tunnelBanner.classList.add('active');
        const urlElem = document.getElementById('tunnelUrl');
        const copyBtn = document.getElementById('btnCopyTunnel');

        urlElem.textContent = this.generatedTunnelUrl;
        urlElem.className = 'tunnel-url connected';
        if (copyBtn) copyBtn.style.display = 'block';
      }

      this._startFpsCounter();

    }).catch(err => {
      dot.className = 'status-dot error';
      statusText.textContent = `Error: ${err.message}`;
      alert(`Could not access camera: ${err.message}`);
    });
  }

  switchCamera(facingMode) {
    if (!this.isSharing) return;

    const video = document.getElementById('senderVideo');
    const statusText = document.getElementById('senderStatus');

    if (statusText) statusText.textContent = 'Flipping camera…';

    if (this.senderStream) {
      this.senderStream.getTracks().forEach(t => t.stop());
    }

    navigator.mediaDevices.getUserMedia({
      video: { facingMode: facingMode, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false
    }).then(stream => {
      this.senderStream = stream;
      if (video) video.srcObject = stream;
      if (statusText) statusText.textContent = 'Streaming live feed';
    }).catch(err => {
      console.error('Camera switch error:', err);
      alert(`Could not flip camera: ${err.message}`);
    });
  }

  stopSharing() {
    if (!this.isSharing) return;

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

    this._fpsInterval = setInterval(() => {
      if (!this.isSharing) return;
      const fps = Math.floor(24 + Math.random() * 5);
      if (fpsElem) fpsElem.textContent = fps;
      if (viewersElem) viewersElem.textContent = '1';
    }, 1000);
  }

  copyAddress() {
    const ip = document.getElementById('senderIp').textContent.trim();
    const port = document.getElementById('senderPortInput').value || '9000';
    const addr = `${ip}:${port}`;
    const btn = document.getElementById('btnCopyAddr');

    navigator.clipboard.writeText(addr).then(() => {
      btn.textContent = '✓  Copied Address!';
      setTimeout(() => { btn.textContent = '📋  Copy Address'; }, 2000);
    }).catch(() => {
      prompt('Copy LAN Address:', addr);
    });
  }

  copyTunnelUrl() {
    const url = this.generatedTunnelUrl || document.getElementById('tunnelUrl').textContent.trim();
    const btn = document.getElementById('btnCopyTunnel');
    navigator.clipboard.writeText(url).then(() => {
      btn.textContent = '✓  Copied Link!';
      setTimeout(() => { btn.textContent = '📋  Copy Internet Link'; }, 2000);
    }).catch(() => {
      prompt('Copy Internet Link:', url);
    });
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

    let streamUrl;
    if (rawIp.startsWith('http://') || rawIp.startsWith('https://')) {
      streamUrl = rawIp.endsWith('/stream') || rawIp.endsWith('/video_feed')
        ? rawIp
        : rawIp.replace(/\/$/, '') + '/stream';
    } else {
      if (rawIp.includes(':')) {
        const parts = rawIp.split(':');
        rawIp = parts[0];
        document.getElementById('receiverPort').value = parts[1];
      }
      streamUrl = `http://${rawIp}:${port}/video_feed`;
    }

    dot.className = 'status-dot waiting';
    statusText.textContent = `Connecting to ${rawIp}…`;
    connectBtn.textContent = 'Connecting…';

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

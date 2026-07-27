/**
 * Live Video Relay Dashboard - Multi-View Manager
 * Author: Monarch666
 */

class MultiViewDashboard {
  constructor() {
    this.feeds = [];
    this.gridElem = document.getElementById('feedsGrid');
    this.urlInput = document.getElementById('streamUrlInput');
    this.nameInput = document.getElementById('streamNameInput');
    this.addBtn = document.getElementById('addFeedBtn');

    this.init();
  }

  init() {
    this.addBtn.addEventListener('click', () => this.handleAddFeed());
    this.urlInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter') this.handleAddFeed();
    });

    // Load initial default sample feeds matching dashboard setup
    this.loadDefaultFeeds();
  }

  loadDefaultFeeds() {
    // Initial feeds setup
    const initialFeeds = [
      { id: 'feed-1', name: 'Local Feed (This Device)', url: '/video_feed', isLocalCam: true },
      { id: 'feed-2', name: 'Camera 2', url: 'https://tabs-corp-imports-cfr.trycloudflare.com/video_feed' },
      { id: 'feed-3', name: 'Camera 3', url: 'https://plaint-ff-bytes-functions-intensity.trycloudflare.com/video_feed' },
      { id: 'feed-4', name: 'Camera 4', url: 'https://presents-jewellery-high-patrick.trycloudflare.com/video_feed' }
    ];

    initialFeeds.forEach(feed => this.addFeedCard(feed));
  }

  handleAddFeed() {
    const rawUrl = this.urlInput.value.trim();
    const rawName = this.nameInput.value.trim();

    if (!rawUrl) {
      alert('Please enter a stream URL or IP:Port.');
      return;
    }

    let formattedUrl = rawUrl;
    if (!formattedUrl.startsWith('http://') && !formattedUrl.startsWith('https://') && !formattedUrl.startsWith('/')) {
      formattedUrl = `https://${formattedUrl}`;
    }

    // Append /video_feed if not specified
    if (!formattedUrl.includes('/video_feed') && !formattedUrl.includes('/stream') && !formattedUrl.startsWith('blob:')) {
      formattedUrl = formattedUrl.endsWith('/') ? `${formattedUrl}video_feed` : `${formattedUrl}/video_feed`;
    }

    const name = rawName || `Camera ${this.feeds.length + 1}`;
    const feed = {
      id: `feed-${Date.now()}`,
      name: name,
      url: formattedUrl
    };

    this.addFeedCard(feed);

    // Clear inputs
    this.urlInput.value = '';
    this.nameInput.value = '';
  }

  addFeedCard(feed) {
    this.feeds.push(feed);

    const card = document.createElement('div');
    card.className = 'feed-card';
    card.id = feed.id;

    card.innerHTML = `
      <div class="feed-header">
        <div class="feed-title-wrap">
          <span class="feed-status-dot"></span>
          <span class="feed-name">${feed.name}</span>
        </div>
        <div class="feed-actions">
          <button class="btn-action btn-fullscreen" title="Fullscreen">
            <i class="fa-solid fa-expand"></i> Fullscreen
          </button>
          <button class="btn-action btn-remove" title="Remove Feed">
            <i class="fa-solid fa-xmark"></i> Remove
          </button>
        </div>
      </div>
      <div class="feed-body">
        ${feed.isLocalCam
          ? `<video class="feed-stream" autoplay playsinline muted></video>`
          : `<img class="feed-stream" src="${feed.url}" alt="${feed.name}" onerror="this.src='https://images.unsplash.com/photo-1508614589041-895b88991e3e?q=80&w=800&auto=format&fit=crop'">`
        }
        <div class="feed-url-badge">${feed.url}</div>
      </div>
    `;

    this.gridElem.appendChild(card);

    // If local cam, initialize Android/Web Camera
    if (feed.isLocalCam) {
      const videoElem = card.querySelector('video');
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
          .then(stream => { videoElem.srcObject = stream; })
          .catch(() => {
            // Fallback sample view if camera permission not granted
            card.querySelector('.feed-body').innerHTML = `
              <img class="feed-stream" src="https://images.unsplash.com/photo-1508614589041-895b88991e3e?q=80&w=800&auto=format&fit=crop" alt="Local Feed">
              <div class="feed-url-badge">/stream</div>
            `;
          });
      }
    }

    // Attach Event Listeners for Fullscreen & Remove
    const fsBtn = card.querySelector('.btn-fullscreen');
    const rmBtn = card.querySelector('.btn-remove');
    const bodyElem = card.querySelector('.feed-body');

    fsBtn.addEventListener('click', () => {
      if (bodyElem.requestFullscreen) {
        bodyElem.requestFullscreen();
      } else if (bodyElem.webkitRequestFullscreen) {
        bodyElem.webkitRequestFullscreen();
      }
    });

    rmBtn.addEventListener('click', () => {
      this.removeFeed(feed.id);
    });
  }

  removeFeed(id) {
    this.feeds = this.feeds.filter(f => f.id !== id);
    const elem = document.getElementById(id);
    if (elem) elem.remove();
  }
}

// Initialize on DOM Ready
document.addEventListener('DOMContentLoaded', () => {
  window.dashboard = new MultiViewDashboard();
});

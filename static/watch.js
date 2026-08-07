// ============================================
// Watch Page - Live stream viewer
// ============================================

// Chaturbate + Stripchat private/non-public show statuses.
const PRIVATE_STATUSES = [
  'private', 'p2p', 'group', 'ticket', 'premium', 'spy',
  'virtualprivate', 'virtual_private', 'true_private', 'private_spy',
  'password_protected', 'password protected', 'hidden'
];

function isPrivateRoomStatus(rs) {
  var status = String(rs || '').toLowerCase();
  if (!status) return false;
  if (PRIVATE_STATUSES.indexOf(status) !== -1) return true;
  return /private|p2p|group|ticket|premium|spy/.test(status);
}

let currentUsername = '';
let currentSourceType = '';
let isFollowing = false;
let isAutoRecord = false;
let isModelTracked = false;
let profilePlaybackVolume = null;
let volumeSaveTimeout = null;
let hlsPlayer = null;
let ivsPlayer = null;
let ivsPlayerLoadPromise = null;
let streamLoaded = false;
let currentStreamUrl = '';
let currentStreamType = '';
let requestedStreamQuality = -1;
// All sites: Auto (ABR) + fixed heights only. No unlabeled placeholder labels.
let serverQualityOptions = [1080, 720, 480];
let statusCheckInterval = null;
let streamUptimeInterval = null;
let streamStartedAt = '';
let streamProblemStatusTimeout = null;
let streamStartPromise = null;
// User clicked pause — do not auto-resume on HLS level reloads / autoplay retries.
let userPausedLive = false;
// Keep Discover-card identity across incomplete stream payloads (no displayName/face).
let lastWatchDisplayName = '';
let lastWatchProfileImageUrl = '';
let lastWatchFollowers = undefined;
let lastWatchChannelUrl = '';

function sourceQuery(extraParams) {
  var params = new URLSearchParams();
  if (currentSourceType) params.set('source', currentSourceType);
  Object.keys(extraParams || {}).forEach(function(key) {
    var value = extraParams[key];
    if (value !== null && value !== undefined && value !== '') {
      params.set(key, String(value));
    }
  });
  var query = params.toString();
  return query ? ('?' + query) : '';
}

function formatStreamUptime(startedAt) {
  if (!startedAt) return '';
  var startMs = Date.parse(startedAt);
  if (!Number.isFinite(startMs)) return '';
  var totalSec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
  var hours = Math.floor(totalSec / 3600);
  var minutes = Math.floor((totalSec % 3600) / 60);
  var seconds = totalSec % 60;
  return hours + ':' + String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
}

function stopStreamUptimeTicker() {
  if (streamUptimeInterval) {
    clearInterval(streamUptimeInterval);
    streamUptimeInterval = null;
  }
}

function updateStreamUptimeDisplay() {
  var uptimeEl = document.getElementById('streamUptime');
  var uptimeValue = document.getElementById('streamUptimeValue');
  if (!uptimeEl || !uptimeValue) return;
  var source = (currentSourceType || '').toLowerCase();
  var text = source === 'twitch' ? formatStreamUptime(streamStartedAt) : '';
  if (!text) {
    uptimeEl.style.display = 'none';
    uptimeValue.textContent = '';
    stopStreamUptimeTicker();
    return;
  }
  uptimeValue.textContent = text;
  uptimeEl.style.display = '';
  uptimeEl.setAttribute('aria-label', 'Stream uptime ' + text);
}

function setStreamUptime(startedAt, isLive) {
  streamStartedAt = isLive ? String(startedAt || '').trim() : '';
  updateStreamUptimeDisplay();
  stopStreamUptimeTicker();
  if (streamStartedAt && (currentSourceType || '').toLowerCase() === 'twitch' && isLive) {
    streamUptimeInterval = setInterval(updateStreamUptimeDisplay, 1000);
  }
}

function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function goBackFromWatch() {
  if (window.history.length > 1) {
    window.history.back();
    return;
  }

  window.location.href = '/';
}

// ============================================
// Extract username from URL
// ============================================
function getUsername() {
  var parts = window.location.pathname.split('/');
  // /watch/{username}
  return parts[2] || '';
}

// ============================================
// Initialize page
// ============================================
async function initWatch() {
  currentUsername = getUsername();
  if (!currentUsername) {
    document.getElementById('watchUsername').textContent = 'Error: No username';
    return;
  }

  // Lit le source_type depuis l'URL (?source=twitch|chaturbate) pour les modèles qui ne
  // sont pas encore dans le cache SQLite — évite le fallback par défaut vers
  // Chaturbate qui marque les CAM4 comme Offline.
  try {
    currentSourceType = new URLSearchParams(window.location.search).get('source') || '';
  } catch (e) {
    currentSourceType = '';
  }
  // Set platform label immediately from ?source= so first paint is never stale "URL:".
  setWatchChannelLabel(currentSourceType);

  lastWatchDisplayName = '';
  lastWatchProfileImageUrl = '';
  lastWatchFollowers = undefined;
  lastWatchChannelUrl = '';

  document.title = currentUsername + ' - HXYLIVE';
  document.getElementById('watchUsername').textContent = currentUsername;

  await loadProfileVolume();
  setupVolumePersistence();

  // loadModelStatus must run first: fills displayName / avatar and resolves sourceType.
  await loadModelStatus();
  await Promise.all([
    loadFollowStatus(),
    loadTrackStatus(),
  ]);

  // Start periodic status check
  statusCheckInterval = setInterval(loadModelStatus, 30000);
}

// ============================================
// Load model status and start stream
// ============================================
async function loadModelStatus() {
  try {
    var res = await fetch('/api/model/' + currentUsername + '/status' + sourceQuery());
    if (!res.ok) return;
    var data = await res.json();

    var statusDot = document.getElementById('statusDot');
    var statusText = document.getElementById('statusText');
    var viewerCount = document.getElementById('viewerCount');
    var viewerNum = document.getElementById('viewerNum');
    var offlineOverlay = document.getElementById('offlineOverlay');
    var offlineTitle = document.getElementById('offlineTitle');
    var offlineText = document.getElementById('offlineText');
    var offlineIcon = document.getElementById('offlineIcon');

    var retryBtn = document.getElementById('retryBtn');
    var priv = isPrivateRoomStatus(data.roomStatus);
    if (data.sourceType) currentSourceType = data.sourceType;
    updateWatchIdentity(data);

    if (priv) {
      statusDot.className = 'status-dot private';
      statusText.textContent = 'Private';
      viewerCount.style.display = 'none';
      setStreamUptime('', false);
      if (offlineIcon) offlineIcon.innerHTML = '&#128274;';
      if (offlineTitle) offlineTitle.textContent = 'Model is in a Private Show';
      if (offlineText) offlineText.textContent = formatPrivateText(data.roomStatus);
      if (retryBtn) retryBtn.style.display = 'none';
      offlineOverlay.style.display = 'flex';
      stopStream();
      return;
    }

    if (data.isOnline) {
      statusDot.className = 'status-dot online';
      statusText.textContent = 'Live';
      viewerCount.style.display = '';
      viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
      setStreamUptime(data.startedAt || data.started_at || '', true);
      offlineOverlay.style.display = 'none';

      // Start stream if not already playing
      if (!hasActiveStream()) {
        startStream();
      }
    } else {
      if (hasActiveStream()) {
        // The status API can briefly report offline while the HLS stream is
        // still healthy. Keep the existing player alive instead of pausing it.
        statusDot.className = 'status-dot online';
        statusText.textContent = 'Live';
        viewerCount.style.display = '';
        viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
        setStreamUptime(data.startedAt || data.started_at || streamStartedAt, true);
        offlineOverlay.style.display = 'none';
        return;
      }

      // Status says offline, but try loading the stream anyway
      // The status API can return false negatives (rate limiting, cache miss)
      if (!hasActiveStream()) {
        var loaded = await tryLoadStream();
        if (loaded) {
          statusDot.className = 'status-dot online';
          statusText.textContent = 'Live';
          viewerCount.style.display = '';
          viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
          setStreamUptime(data.startedAt || data.started_at || '', true);
          offlineOverlay.style.display = 'none';
          return;
        }
      }

      statusDot.className = 'status-dot offline';
      statusText.textContent = 'Offline';
      viewerCount.style.display = 'none';
      setStreamUptime('', false);
      if (offlineIcon) offlineIcon.innerHTML = '&#128308;';
      if (offlineTitle) offlineTitle.textContent = 'Model is Offline';
      if (offlineText) offlineText.textContent = 'This model is currently not streaming.';
      if (retryBtn) retryBtn.style.display = 'inline-flex';
      offlineOverlay.style.display = 'flex';

      // Stop stream if playing
      stopStream();
    }
  } catch (e) {
    console.error('Error loading model status:', e);
  }
}

function scheduleStatusRefreshAfterStreamProblem() {
  if (streamProblemStatusTimeout) return;
  streamProblemStatusTimeout = setTimeout(function() {
    streamProblemStatusTimeout = null;
    loadModelStatus();
  }, 250);
}

function watchProviderLabel(sourceType) {
  var t = String(sourceType || 'chaturbate').toLowerCase();
  var labels = {
    chaturbate: 'Chaturbate',
    twitch: 'Twitch',
    bilibili: 'Bilibili',
    stripchat: 'Stripchat'
  };
  if (labels[t]) return labels[t];
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : 'Chaturbate';
}

function setWatchChannelLabel(sourceType) {
  var channelLine = document.getElementById('watchChannelLine');
  var channelLabel = document.getElementById('watchChannelLabel')
    || (channelLine && channelLine.querySelector('.watch-meta-label'));
  if (channelLabel) {
    channelLabel.textContent = watchProviderLabel(sourceType || currentSourceType) + ':';
  }
}

function updateWatchIdentity(data) {
  var avatar = document.getElementById('watchAvatar');
  var placeholder = document.getElementById('watchAvatarPlaceholder');
  var followers = document.getElementById('watchFollowersValue');
  var channel = document.getElementById('watchChannelUrl');
  var channelLine = document.getElementById('watchChannelLine');
  var nameEl = document.getElementById('watchUsername');
  // Stream responses often only have username + cover thumbnail. Never let those
  // wipe the Discover-card face / display_name that status already loaded.
  var incomingName = String(
    (data && (data.displayName || data.display_name)) || ''
  ).trim();
  if (incomingName && incomingName !== currentUsername) {
    lastWatchDisplayName = incomingName;
  } else if (incomingName && !lastWatchDisplayName) {
    lastWatchDisplayName = incomingName;
  }
  var displayName = lastWatchDisplayName || currentUsername;

  var incomingImage = String(
    (data && (data.profileImageUrl || data.profile_image_url)) || ''
  ).trim();
  // Live covers are not face photos — keep letter avatar (Discover/Media-style).
  function isLiveCoverAvatarUrl(url) {
    var u = String(url || '').trim().toLowerCase();
    if (!u) return false;
    if (/thumb\.live\.mmcdn\.com\/riw\//i.test(u)) return true;
    if (/doppiocdn\./i.test(u) && /\/snapshot\//i.test(u)) return true;
    if (/(doppiocdn\.|static-proxy\.strpst\.com)/i.test(u) && /\/previews\//i.test(u)) return true;
    return false;
  }
  if (isLiveCoverAvatarUrl(incomingImage)) {
    incomingImage = '';
  }
  if (incomingImage) {
    lastWatchProfileImageUrl = incomingImage;
  } else if (isLiveCoverAvatarUrl(lastWatchProfileImageUrl)) {
    lastWatchProfileImageUrl = '';
  }
  var imageUrl = lastWatchProfileImageUrl;

  if (nameEl && displayName) {
    nameEl.textContent = displayName;
    document.title = displayName + ' - HXYLIVE';
  }
  var letter = String(displayName || currentUsername || '?').trim().charAt(0).toUpperCase() || '?';
  if (placeholder) {
    var mark = placeholder.querySelector('span');
    if (mark) mark.textContent = letter;
  }
  if (avatar) {
    avatar.setAttribute('referrerpolicy', 'no-referrer');
    avatar.onload = function() {
      avatar.style.display = 'block';
      if (placeholder) placeholder.style.display = 'none';
    };
    avatar.onerror = function() {
      avatar.removeAttribute('src');
      avatar.style.display = 'none';
      if (placeholder) placeholder.style.display = 'flex';
    };
    if (imageUrl) {
      if (placeholder) placeholder.style.display = 'none';
      avatar.style.display = 'block';
      if (avatar.getAttribute('src') !== imageUrl) {
        avatar.src = imageUrl;
      }
    } else {
      avatar.removeAttribute('src');
      avatar.style.display = 'none';
      if (placeholder) placeholder.style.display = 'flex';
    }
  }
  if (followers && data && Object.prototype.hasOwnProperty.call(data, 'followers')) {
    var followersLine = document.getElementById('watchFollowers');
    if (String(currentSourceType || data.sourceType || '').toLowerCase() === 'stripchat') {
      lastWatchFollowers = null;
      if (followersLine) followersLine.style.display = 'none';
    } else {
      if (followersLine) followersLine.style.display = '';
      lastWatchFollowers = data.followers;
      followers.textContent = lastWatchFollowers === null || lastWatchFollowers === undefined
        ? 'unavailable'
        : Number(lastWatchFollowers || 0).toLocaleString();
    }
  }
  if (data && (data.sourceType || currentSourceType)) {
    setWatchChannelLabel(data.sourceType || currentSourceType);
  }
  if (channel && channelLine && data && data.channelUrl) {
    lastWatchChannelUrl = data.channelUrl;
    channel.href = lastWatchChannelUrl;
    channel.textContent = lastWatchChannelUrl;
    setWatchChannelLabel(currentSourceType || data.sourceType);
    channelLine.style.display = '';
  }
}

function applyLiveMetadata(data) {
  if (!data) return;
  if (data.sourceType) {
    currentSourceType = data.sourceType;
  }
  updateWatchIdentity(data);
}

function formatPrivateText(rs) {
  var s = (rs || '').toLowerCase();
  if (s === 'group') return 'The model is currently in a group show.';
  if (s === 'p2p' || s === 'virtualprivate' || s === 'virtual_private') {
    return 'The model is in a private (p2p) show.';
  }
  if (s === 'ticket' || s === 'premium') return 'The model is in a ticket / premium show.';
  if (s === 'spy' || s === 'private_spy') return 'The model is in a private show (spy mode).';
  if (s === 'password_protected' || s === 'password protected') return 'The room is password-protected.';
  if (s === 'hidden') return 'The room is hidden from public viewers.';
  if (s === 'true_private') return 'The model is in a true private show.';
  return 'The model is in a private session.';
}

function showStreamLoading() {
  var offlineOverlay = document.getElementById('offlineOverlay');
  var offlineIcon = document.getElementById('offlineIcon');
  var offlineTitle = document.getElementById('offlineTitle');
  var offlineText = document.getElementById('offlineText');
  var retryBtn = document.getElementById('retryBtn');
  if (offlineIcon) offlineIcon.innerHTML = '&#9203;';
  if (offlineTitle) offlineTitle.textContent = 'Starting live';
  if (offlineText) offlineText.textContent = 'Connecting to the live video...';
  if (retryBtn) retryBtn.style.display = 'none';
  if (offlineOverlay) offlineOverlay.style.display = 'flex';
}

async function readStreamError(res) {
  try {
    var data = await res.json();
    if (data && data.detail) return String(data.detail);
    if (data && data.message) return String(data.message);
  } catch (e) {
    // Fall through to the generic HTTP status below.
  }
  return 'Live stream request failed (' + res.status + ').';
}

async function fetchStreamPayload(showErrors, quality) {
  var query = {};
  if (quality !== undefined && quality !== null && Number(quality) > 0) {
    query.quality = Number(quality);
  }
  var res = await fetch('/api/model/' + currentUsername + '/stream' + sourceQuery(query));
  if (!res.ok) {
    var detail = await readStreamError(res);
    if (showErrors) showLivePlayerError(detail);
    return null;
  }
  var data = await res.json();
  if (!data.streamUrl) {
    if (showErrors) showLivePlayerError('The live stream did not return a playable URL.');
    return null;
  }
  if (Array.isArray(data.qualityOptions) && data.qualityOptions.length) {
    serverQualityOptions = data.qualityOptions
      .map(function(item) { return Number(item); })
      .filter(function(item) { return item > 0; });
  }
  return data;
}

// ============================================
// Try loading stream (returns true if successful)
// ============================================
async function tryLoadStream() {
  try {
    if (streamStartPromise) return await streamStartPromise;
    var data = await fetchStreamPayload(false);
    if (!data) return false;

    // Stream URL is available - start playing
    applyLiveMetadata(data);
    startStreamWithUrl(data.streamUrl, data.streamType);
    return true;
  } catch (e) {
    return false;
  }
}

// ============================================
// Start HLS stream
// ============================================
async function startStream() {
  if (streamStartPromise) return streamStartPromise;
  streamStartPromise = (async function() {
    try {
      showStreamLoading();
      var data = await fetchStreamPayload(true);
      if (!data) return false;
      var streamUrl = data.streamUrl;

      applyLiveMetadata(data);
      startStreamWithUrl(streamUrl, data.streamType);
      return true;
    } catch (e) {
      console.error('Error starting stream:', e);
      showLivePlayerError('This live stream cannot be loaded right now.');
      return false;
    } finally {
      streamStartPromise = null;
    }
  })();
  return streamStartPromise;
}

function startStreamWithUrl(streamUrl, streamType, forceReload) {
  var video = document.getElementById('videoPlayer');
  if (!video) return;

  streamType = String(streamType || '').toLowerCase();
  if (!forceReload && hasActiveStream() && currentStreamUrl === streamUrl && currentStreamType === streamType) {
    return;
  }

  stopStream(false);
  currentStreamUrl = streamUrl;
  currentStreamType = streamType;
  streamLoaded = true;
  userPausedLive = false;
  prepareMutedAutoplay(video);
  var offlineOverlay = document.getElementById('offlineOverlay');
  if (offlineOverlay) offlineOverlay.style.display = 'none';

  if (isIvsStream(streamUrl, streamType)) {
    startIvsStream(streamUrl, video);
  } else if (isNativeVideoStream(streamUrl)) {
    resetQualitySelector();
    video.src = streamUrl;
    video.addEventListener('loadedmetadata', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.addEventListener('canplay', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.load();
    startMutedAutoplay(video);
  } else if (window.Hls && window.Hls.isSupported()) {
    hlsPlayer = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 90,
    });
    hlsPlayer.loadSource(streamUrl);
    hlsPlayer.attachMedia(video);
    hlsPlayer.on(Hls.Events.MANIFEST_PARSED, function() {
      updateQualitySelector();
      startMutedAutoplay(video);
    });
    // Live playlists reload often. Do not force play() on every LEVEL_LOADED —
    // that was undoing intentional pause clicks after ~1 segment.
    hlsPlayer.on(Hls.Events.ERROR, function(event, data) {
      if (data.fatal) {
        console.error('HLS fatal error: ' + [
          data.type,
          data.details || '',
          data.reason || '',
          data.error ? data.error.message : '',
          data.response ? data.response.code : '',
          data.url || ''
        ].filter(Boolean).join(' | '));
        scheduleStatusRefreshAfterStreamProblem();
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            hlsPlayer.startLoad();
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hlsPlayer.recoverMediaError();
            break;
          default:
            stopStream(false);
            setTimeout(startStream, 5000);
            break;
        }
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Safari native HLS
    resetQualitySelector();
    video.src = streamUrl;
    video.addEventListener('loadedmetadata', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.addEventListener('canplay', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.load();
    startMutedAutoplay(video);
  } else {
    console.error('HLS playback is not supported in this browser');
    stopStream(false);
    var offlineOverlay = document.getElementById('offlineOverlay');
    var offlineIcon = document.getElementById('offlineIcon');
    var offlineTitle = document.getElementById('offlineTitle');
    var offlineText = document.getElementById('offlineText');
    var retryBtn = document.getElementById('retryBtn');
    if (offlineIcon) offlineIcon.innerHTML = '&#9888;';
    if (offlineTitle) offlineTitle.textContent = 'Live player unavailable';
    if (offlineText) offlineText.textContent = 'This browser cannot load the HLS player. Check the network connection and retry.';
    if (retryBtn) retryBtn.style.display = 'inline-flex';
    if (offlineOverlay) offlineOverlay.style.display = 'flex';
  }
}

function isIvsStream(streamUrl, streamType) {
  var url = String(streamUrl || '').toLowerCase();
  return streamType === 'ivs' || url.indexOf('playback.live-video.net') !== -1;
}

function loadIvsPlayerApi() {
  if (window.IVSPlayer) return Promise.resolve(window.IVSPlayer);
  if (ivsPlayerLoadPromise) return ivsPlayerLoadPromise;

  ivsPlayerLoadPromise = new Promise(function(resolve, reject) {
    var script = document.createElement('script');
    script.src = '/vendor/amazon-ivs-player.min.js';
    script.async = true;
    script.onload = function() {
      if (window.IVSPlayer) {
        resolve(window.IVSPlayer);
      } else {
        reject(new Error('IVS player did not initialize'));
      }
    };
    script.onerror = function() {
      reject(new Error('IVS player failed to load'));
    };
    document.head.appendChild(script);
  });

  return ivsPlayerLoadPromise;
}

function showLivePlayerError(message) {
  stopStream(false);
  var offlineOverlay = document.getElementById('offlineOverlay');
  var offlineIcon = document.getElementById('offlineIcon');
  var offlineTitle = document.getElementById('offlineTitle');
  var offlineText = document.getElementById('offlineText');
  var retryBtn = document.getElementById('retryBtn');
  if (offlineIcon) offlineIcon.innerHTML = '&#9888;';
  if (offlineTitle) offlineTitle.textContent = 'Live player unavailable';
  if (offlineText) offlineText.textContent = message || 'This live stream cannot be loaded right now.';
  if (retryBtn) retryBtn.style.display = 'inline-flex';
  if (offlineOverlay) offlineOverlay.style.display = 'flex';
}

function startIvsStream(streamUrl, video) {
  resetQualitySelector();
  loadIvsPlayerApi().then(function(IVSPlayer) {
    if (!IVSPlayer || !IVSPlayer.isPlayerSupported) {
      throw new Error('IVS playback is not supported in this browser');
    }
    ivsPlayer = IVSPlayer.create();
    ivsPlayer.attachHTMLVideoElement(video);
    if (typeof ivsPlayer.setLiveLowLatencyEnabled === 'function') {
      ivsPlayer.setLiveLowLatencyEnabled(false);
    }
    if (IVSPlayer.PlayerEventType && typeof ivsPlayer.addEventListener === 'function') {
      ivsPlayer.addEventListener(IVSPlayer.PlayerEventType.READY, function() {
        startMutedAutoplay(video);
      });
      ivsPlayer.addEventListener(IVSPlayer.PlayerEventType.ERROR, function(error) {
        console.error('IVS playback error:', error);
        scheduleStatusRefreshAfterStreamProblem();
      });
    }
    ivsPlayer.load(streamUrl);
    ivsPlayer.play();
    startMutedAutoplay(video);
  }).catch(function(error) {
    console.error('IVS player error:', error);
    showLivePlayerError('This live requires the IVS player and could not be loaded.');
  });
}

function isNativeVideoStream(streamUrl) {
  var url = String(streamUrl || '').toLowerCase();
  return url.indexOf('/streams/browser/') !== -1 || /\.webm(?:$|[?#])/.test(url);
}

function prepareMutedAutoplay(video) {
  video.autoplay = true;
  video.muted = true;
  video.defaultMuted = true;
  video.playsInline = true;
  video.setAttribute('autoplay', '');
  video.setAttribute('muted', '');
  video.setAttribute('playsinline', '');
  video.setAttribute('webkit-playsinline', '');
}

function applyPreferredLiveAudio(video) {
  if (!video) return;
  var volume = getSavedProfileVolume();
  if (volume === null || volume <= 0) volume = 1;
  video.volume = volume;
  video.muted = false;
  video.defaultMuted = false;
  video.removeAttribute('muted');
}

function invalidateLiveAutoplayRetries(video) {
  if (!video) return;
  video.dataset.autoplayToken = '';
}

function pauseLivePlayback() {
  userPausedLive = true;
  var video = document.getElementById('videoPlayer');
  invalidateLiveAutoplayRetries(video);
  if (video) video.pause();
  if (ivsPlayer && typeof ivsPlayer.pause === 'function') {
    try { ivsPlayer.pause(); } catch (e) {}
  }
  updateLiveControls();
}

function resumeLivePlayback() {
  userPausedLive = false;
  var video = document.getElementById('videoPlayer');
  if (ivsPlayer && typeof ivsPlayer.play === 'function') {
    try { ivsPlayer.play(); } catch (e) {}
  }
  if (video) {
    video.play().catch(function() {});
  }
  updateLiveControls();
}

function startMutedAutoplay(video) {
  // Prefer unmuted playback at the saved/default volume (100%).
  // If the browser blocks unmuted autoplay, fall back to muted play and keep
  // volume at the preferred level so one unmute click is full volume.
  if (!video || userPausedLive) return;

  video.autoplay = true;
  video.playsInline = true;
  video.setAttribute('autoplay', '');
  video.setAttribute('playsinline', '');
  video.setAttribute('webkit-playsinline', '');
  applyPreferredLiveAudio(video);

  var token = Date.now().toString(36) + Math.random().toString(36).slice(2);
  video.dataset.autoplayToken = token;

  var delays = [0, 200, 600, 1200, 2500];
  function finishControls() {
    if (typeof updateLiveControls === 'function') {
      try { updateLiveControls(); } catch (e) {}
    }
  }

  function attemptUnmuted(index) {
    if (userPausedLive || video.dataset.autoplayToken !== token) return;
    applyPreferredLiveAudio(video);

    var promise = video.play();
    if (!promise || typeof promise.then !== 'function') {
      finishControls();
      return;
    }

    promise.then(function() {
      if (userPausedLive || video.dataset.autoplayToken !== token) return;
      applyPreferredLiveAudio(video);
      finishControls();
    }).catch(function() {
      if (userPausedLive || video.dataset.autoplayToken !== token) return;
      if (index < delays.length - 1) {
        setTimeout(function() {
          attemptUnmuted(index + 1);
        }, delays[index + 1]);
        return;
      }
      attemptMutedFallback();
    });
  }

  function attemptMutedFallback() {
    if (userPausedLive || video.dataset.autoplayToken !== token) return;
    prepareMutedAutoplay(video);
    video.volume = getSavedProfileVolume();
    var promise = video.play();
    if (!promise || typeof promise.then !== 'function') {
      finishControls();
      return;
    }
    promise.then(function() {
      if (userPausedLive || video.dataset.autoplayToken !== token) return;
      // Some browsers allow unmuting right after muted autoplay starts.
      applyPreferredLiveAudio(video);
      var unmutePlay = video.play();
      if (unmutePlay && typeof unmutePlay.catch === 'function') {
        unmutePlay.catch(function() {
          video.volume = getSavedProfileVolume();
          // Stay muted; volume remains at preferred level.
        }).then(finishControls, finishControls);
      } else {
        finishControls();
      }
    }).catch(function(error) {
      console.warn('Live autoplay did not start:', error);
      finishControls();
    });
  }

  attemptUnmuted(0);
}

function hasActiveStream() {
  return streamLoaded || !!hlsPlayer || !!ivsPlayer;
}

function stopStream(clearVideo) {
  var video = document.getElementById('videoPlayer');
  if (hlsPlayer) {
    hlsPlayer.destroy();
    hlsPlayer = null;
  }
  if (ivsPlayer) {
    try {
      if (typeof ivsPlayer.pause === 'function') ivsPlayer.pause();
      if (typeof ivsPlayer.delete === 'function') ivsPlayer.delete();
    } catch (e) {}
    ivsPlayer = null;
  }
  streamLoaded = false;
  currentStreamUrl = '';
  currentStreamType = '';
  resetQualitySelector();

  if (clearVideo !== false && video) {
    video.removeAttribute('src');
    video.load();
  }
}

function resetQualitySelector() {
  var select = document.getElementById('watchQualitySelect');
  if (!select) return;
  select.innerHTML = '<option value="-1">Auto</option>';
  select.disabled = true;
}

function watchQualityHeights() {
  var defaults = [1080, 720, 480];
  var heights = (serverQualityOptions || [])
    .map(function(item) { return Number(item); })
    .filter(function(item) { return item === 1080 || item === 720 || item === 480; });
  return heights.length ? heights : defaults;
}

function findHlsLevelForHeight(height) {
  if (!hlsPlayer || !Array.isArray(hlsPlayer.levels) || !height) return -1;
  var target = Number(height);
  var bestIndex = -1;
  var bestHeight = -1;
  hlsPlayer.levels.forEach(function(level, index) {
    var levelHeight = Number(level && level.height) || 0;
    if (!levelHeight || levelHeight > target) return;
    if (levelHeight >= bestHeight) {
      bestHeight = levelHeight;
      bestIndex = index;
    }
  });
  return bestIndex;
}

function updateQualitySelector() {
  var select = document.getElementById('watchQualitySelect');
  if (!select) return;
  if (!hlsPlayer || !hlsPlayer.levels || !hlsPlayer.levels.length) {
    resetQualitySelector();
    return;
  }

  // Every site uses the same menu: Auto (network ABR) + 1080p/720p/480p.
  // Do not enumerate raw HLS levels (unlabeled rungs used to show as Source).
  var html = '<option value="-1">Auto</option>';
  watchQualityHeights().forEach(function(height) {
    var levelIndex = findHlsLevelForHeight(height);
    if (levelIndex >= 0 && Number(hlsPlayer.levels[levelIndex].height) === height) {
      html += '<option value="' + levelIndex + '">' + height + 'p</option>';
    } else {
      html += '<option value="server:' + height + '">' + height + 'p</option>';
    }
  });
  select.innerHTML = html;
  select.disabled = false;

  if (requestedStreamQuality > 0) {
    var exactLevel = findHlsLevelForHeight(requestedStreamQuality);
    if (
      exactLevel >= 0
      && Number(hlsPlayer.levels[exactLevel].height) === requestedStreamQuality
      && optionExists(select, String(exactLevel))
    ) {
      select.value = String(exactLevel);
      return;
    }
    var serverValue = 'server:' + requestedStreamQuality;
    select.value = optionExists(select, serverValue) ? serverValue : '-1';
    return;
  }

  // Auto = ABR (hls.js picks by bandwidth). Independent of any fixed rung.
  select.value = '-1';
  if (typeof hlsPlayer.currentLevel === 'number') {
    hlsPlayer.currentLevel = -1;
  }
}

function optionExists(select, value) {
  for (var i = 0; i < select.options.length; i++) {
    if (select.options[i].value === value) return true;
  }
  return false;
}

async function changeQuality(levelValue) {
  if (String(levelValue).indexOf('server:') === 0) {
    requestedStreamQuality = parseInt(String(levelValue).slice(7), 10);
    var data = await fetchStreamPayload(true, requestedStreamQuality);
    if (data) {
      applyLiveMetadata(data);
      startStreamWithUrl(data.streamUrl, data.streamType, true);
    }
    return;
  }
  var level = parseInt(levelValue, 10);
  if (Number.isNaN(level) || level < 0) {
    requestedStreamQuality = -1;
    if (hlsPlayer && hlsPlayer.levels && hlsPlayer.levels.length > 1) {
      hlsPlayer.currentLevel = -1;
      return;
    }
    var automatic = await fetchStreamPayload(true, null);
    if (automatic) {
      applyLiveMetadata(automatic);
      startStreamWithUrl(automatic.streamUrl, automatic.streamType, true);
    }
    return;
  }
  if (!hlsPlayer) return;
  requestedStreamQuality = Number(hlsPlayer.levels[level] && hlsPlayer.levels[level].height) || -1;
  hlsPlayer.currentLevel = level;
}

// ============================================
// Retry loading stream (called from retry button)
// ============================================
async function retryStream() {
  var retryBtn = document.getElementById('retryBtn');
  if (retryBtn) {
    retryBtn.disabled = true;
    retryBtn.textContent = 'Retrying...';
  }

  await loadModelStatus();

  if (retryBtn) {
    retryBtn.disabled = false;
    retryBtn.textContent = 'Retry';
  }
}

// ============================================
// Follow status
// ============================================
function followBasePath() {
  // Route vers le bon service selon la plateforme.
  return '/api/providers/' + encodeURIComponent(currentSourceType || 'chaturbate');
}

async function loadFollowStatus() {
  try {
    var res = await fetch(followBasePath() + '/is-following/' + currentUsername);
    if (!res.ok) return;
    var data = await res.json();
    isFollowing = data.isFollowing;
    updateFollowButton();
    document.getElementById('followBtn').style.display = 'inline-flex';
  } catch (e) {
    console.error('Error loading follow status:', e);
  }
}

function updateFollowButton() {
  var btn = document.getElementById('followBtn');
  var text = document.getElementById('followText');

  if (isFollowing) {
    btn.classList.add('active');
    if (text) text.textContent = 'Unfollow';
  } else {
    btn.classList.remove('active');
    if (text) text.textContent = 'Follow';
  }
}

async function toggleFollow() {
  var btn = document.getElementById('followBtn');
  btn.disabled = true;

  try {
    var endpoint = isFollowing
      ? followBasePath() + '/unfollow/' + currentUsername
      : followBasePath() + '/follow/' + currentUsername;

    var res = await fetch(endpoint, { method: 'POST' });
    if (res.ok) {
      isFollowing = !isFollowing;
      updateFollowButton();
      showNotification(
        isFollowing ? 'Now following ' + currentUsername : 'Unfollowed ' + currentUsername,
        'success'
      );
    } else {
      showNotification('Failed to update follow status', 'error');
    }
  } catch (e) {
    console.error('Error toggling follow:', e);
    showNotification('Connection error', 'error');
  } finally {
    btn.disabled = false;
  }
}

// ============================================
// Recording profile link
// ============================================
var recordingProfiles = [];
var recordingProfileSearch = '';

function currentChannelUrl() {
  var source = (currentSourceType || 'chaturbate').toLowerCase();
  if (source === 'twitch') return 'https://www.twitch.tv/' + encodeURIComponent(currentUsername);
  if (source === 'bilibili') return 'https://live.bilibili.com/' + encodeURIComponent(currentUsername);
  return 'https://chaturbate.com/' + encodeURIComponent(currentUsername) + '/';
}

async function loadTrackStatus() {
  try {
    var res = await fetch('/api/media-library?limit=1', { cache: 'no-store' });
    if (!res.ok) return;
    var data = await res.json();
    var profiles = data.profiles || [];
    var found = null;
    for (var i = 0; i < profiles.length; i++) {
      var sources = profiles[i].streamSources || profiles[i].stream_sources || [];
      var currentSource = (currentSourceType || 'chaturbate').toLowerCase();
      for (var j = 0; j < sources.length; j++) {
        var sourceType = (sources[j].sourceType || sources[j].source_type || 'chaturbate').toLowerCase();
        var channelUsername = sources[j].channelUsername || sources[j].channel_username || '';
        if (channelUsername === currentUsername && sourceType === currentSource) {
          found = profiles[i];
          break;
        }
      }
      if (found) break;
    }

    isModelTracked = !!found;
    isAutoRecord = !!found;
    updateRecordButton();
    document.getElementById('recordBtn').style.display = 'inline-flex';
  } catch (e) {
    console.error('Error loading recording profile status:', e);
  }
}

function updateRecordButton() {
  var btn = document.getElementById('recordBtn');
  var text = document.getElementById('recordText');

  if (isAutoRecord) {
    btn.classList.add('active');
    if (text) text.textContent = 'Recording set';
  } else {
    btn.classList.remove('active');
    if (text) text.textContent = 'Set recording';
  }
}

function normalizeProfileUsername(value) {
  return String(value || '')
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, '-')
    .replace(/^[._-]+|[._-]+$/g, '');
}

function profileLabel(profile) {
  return profile.displayName || profile.display_name || profile.username || '';
}

function sourceCountLabel(profile) {
  var sources = profile.streamSources || profile.stream_sources || [];
  if (!sources.length) return 'No source yet';
  return sources.length === 1 ? '1 source' : sources.length + ' sources';
}

async function loadRecordingProfiles() {
  try {
    var res = await fetch('/api/media-library?limit=1', { cache: 'no-store' });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) throw new Error(data.detail || 'Profiles unavailable');
    recordingProfiles = data.profiles || [];
    renderRecordingProfileList();
  } catch (e) {
    console.error('Error loading recording profiles:', e);
    recordingProfiles = [];
    renderRecordingProfileList(e.message || 'Profiles unavailable');
  }
}

function renderRecordingProfileList(errorMessage) {
  var list = document.getElementById('recordingProfileList');
  if (!list) return;
  if (errorMessage) {
    list.innerHTML = '<div class="watch-recording-profile">' + escapeHtml(errorMessage) + '</div>';
    return;
  }
  var query = recordingProfileSearch.trim().toLowerCase();
  var profiles = recordingProfiles.filter(function(profile) {
    if (!query) return true;
    return [
      profile.username,
      profile.displayName || profile.display_name,
      profile.firstName || profile.first_name,
      profile.lastName || profile.last_name
    ].join(' ').toLowerCase().indexOf(query) !== -1;
  });
  if (!profiles.length) {
    list.innerHTML = '<div class="watch-recording-profile">No profile found</div>';
    return;
  }
  list.innerHTML = profiles.map(function(profile) {
    return '<button class="watch-recording-profile" type="button" data-profile="' + escapeHtml(profile.username) + '">' +
      '<strong>' + escapeHtml(profileLabel(profile)) + '</strong>' +
      '<span>' + escapeHtml(sourceCountLabel(profile)) + '</span>' +
    '</button>';
  }).join('');
}

function setRecordingMode(mode) {
  var existingTab = document.getElementById('recordingExistingTab');
  var createTab = document.getElementById('recordingCreateTab');
  var existingPane = document.getElementById('recordingExistingPane');
  var createPane = document.getElementById('recordingCreatePane');
  var create = mode === 'create';
  if (existingTab) existingTab.classList.toggle('active', !create);
  if (createTab) createTab.classList.toggle('active', create);
  if (existingPane) existingPane.style.display = create ? 'none' : '';
  if (createPane) createPane.style.display = create ? '' : 'none';
}

function openRecordingModal() {
  var modal = document.getElementById('recordingModal');
  var usernameInput = document.getElementById('recordingCreateUsername');
  var displayNameInput = document.getElementById('recordingCreateDisplayName');
  if (usernameInput) usernameInput.value = normalizeProfileUsername(currentUsername);
  // Prefer live uname (e.g. Bilibili) over numeric room id for the profile label.
  if (displayNameInput) displayNameInput.value = lastWatchDisplayName || currentUsername;
  setRecordingMode('existing');
  if (modal) {
    modal.style.display = 'flex';
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('watch-recording-open');
  }
  loadRecordingProfiles();
}

function closeRecordingModal() {
  var modal = document.getElementById('recordingModal');
  if (modal) {
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('watch-recording-open');
  }
}

async function linkRecordingProfile(profileUsername, createProfile, displayName) {
  var btn = document.getElementById('recordBtn');
  if (btn) btn.disabled = true;
  try {
    var res = await fetch('/api/media-profiles/link-live', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        profileUsername: profileUsername,
        createProfile: !!createProfile,
        displayName: displayName || profileUsername,
        liveUsername: currentUsername,
        sourceType: currentSourceType || 'chaturbate',
        channelUrl: currentChannelUrl(),
        autoRecord: true
      })
    });

    if (res.ok) {
      isModelTracked = true;
      isAutoRecord = true;
      updateRecordButton();
      closeRecordingModal();
      showNotification('Recording configured', 'success');
      await loadTrackStatus();
    } else {
      var data = await res.json().catch(function() { return {}; });
      showNotification(data.detail || 'Failed to set recording', 'error');
    }
  } catch (e) {
    console.error('Error setting recording:', e);
    showNotification('Connection error', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function submitCreateRecordingProfile(ev) {
  if (ev) ev.preventDefault();
  var username = normalizeProfileUsername(document.getElementById('recordingCreateUsername').value);
  var displayName = document.getElementById('recordingCreateDisplayName').value.trim() || username;
  if (!username) {
    showNotification('Profile username is required', 'error');
    return;
  }
  await linkRecordingProfile(username, true, displayName);
}

document.addEventListener('click', function(ev) {
  var profileButton = ev.target.closest('.watch-recording-profile[data-profile]');
  if (profileButton) {
    linkRecordingProfile(profileButton.dataset.profile, false, '');
    return;
  }
  var modal = document.getElementById('recordingModal');
  if (modal && ev.target === modal) closeRecordingModal();
});

document.addEventListener('input', function(ev) {
  if (ev.target && ev.target.id === 'recordingProfileSearch') {
    recordingProfileSearch = ev.target.value || '';
    renderRecordingProfileList();
  }
});

document.addEventListener('keydown', function(ev) {
  if (ev.key === 'Escape') closeRecordingModal();
});

// ============================================
// Volume persistence (across sessions)
// ============================================
function normalizeVolume(value) {
  if (value === null || value === undefined || value === '') return null;
  var volume = Number(value);
  if (!Number.isFinite(volume)) return null;
  return Math.min(1, Math.max(0, volume));
}

async function loadProfileVolume() {
  try {
    var res = await fetch('/api/models/' + encodeURIComponent(currentUsername) + '/volume');
    if (!res.ok) return;

    var data = await res.json();
    var saved = normalizeVolume(data.volume);
    if (saved !== null) {
      profilePlaybackVolume = saved;
      localStorage.setItem('video_volume_' + currentUsername, String(saved));
      return;
    }

    var profileVolume = getLocalVolume('video_volume_' + currentUsername);
    if (profileVolume !== null) {
      saveProfileVolume(profileVolume);
    }
  } catch (e) {
    console.warn('Could not load saved profile volume:', e);
  }
}

function persistProfileVolume(volume) {
  volumeSaveTimeout = null;
  fetch('/api/models/' + encodeURIComponent(currentUsername) + '/volume', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ volume: volume }),
    keepalive: true
  }).catch(function(e) {
    console.warn('Could not save profile volume:', e);
  });
}

function saveProfileVolume(volume) {
  var normalized = normalizeVolume(volume);
  if (normalized === null) return;

  profilePlaybackVolume = normalized;
  localStorage.setItem('video_volume_' + currentUsername, String(normalized));

  if (volumeSaveTimeout) {
    clearTimeout(volumeSaveTimeout);
  }
  volumeSaveTimeout = setTimeout(function() {
    persistProfileVolume(normalized);
  }, 250);
}

function getLocalVolume(key) {
  var saved = localStorage.getItem(key);
  return saved === null ? null : normalizeVolume(saved);
}

function getSavedProfileVolume() {
  if (profilePlaybackVolume !== null) return profilePlaybackVolume;

  var profileVolume = getLocalVolume('video_volume_' + currentUsername);
  if (profileVolume !== null) return profileVolume;

  var legacyGlobalVolume = getLocalVolume('video_volume_global');
  if (legacyGlobalVolume !== null) return legacyGlobalVolume;

  return 1;
}

function setupVolumePersistence() {
  var video = document.getElementById('videoPlayer');
  if (!video) return;

  video.volume = getSavedProfileVolume();

  if (video.dataset.volumePersistenceReady === 'true') return;
  video.dataset.volumePersistenceReady = 'true';

  // Persist on change
  video.addEventListener('volumechange', function() {
    if (!video.muted || video.volume === 0) {
      saveProfileVolume(video.volume);
    }
  });
}

function setupLiveControls() {
  var video = document.getElementById('videoPlayer');
  var container = document.querySelector('.watch-player-container');
  var playBtn = document.getElementById('livePlayBtn');
  var muteBtn = document.getElementById('liveMuteBtn');
  var fullscreenBtn = document.getElementById('liveFullscreenBtn');
  var volumeSlider = document.getElementById('liveVolumeSlider');
  var qualitySelect = document.getElementById('watchQualitySelect');
  if (!video) return;

  video.controls = false;
  video.playbackRate = 1;

  if (playBtn) {
    playBtn.addEventListener('click', function() {
      if (video.paused) {
        resumeLivePlayback();
      } else {
        pauseLivePlayback();
      }
    });
  }

  if (muteBtn) {
    muteBtn.addEventListener('click', function() {
      if (video.muted || video.volume === 0) {
        video.muted = false;
        if (video.volume === 0) video.volume = getSavedProfileVolume();
      } else {
        video.muted = true;
      }
    });
  }

  if (volumeSlider) {
    volumeSlider.value = String(video.volume);
    volumeSlider.addEventListener('input', function() {
      var volume = parseFloat(volumeSlider.value);
      if (Number.isNaN(volume)) return;
      video.volume = volume;
      video.muted = volume === 0;
      saveProfileVolume(volume);
    });
  }

  if (qualitySelect) {
    qualitySelect.addEventListener('change', function() {
      changeQuality(qualitySelect.value).catch(function(error) {
        console.error('Error changing live quality:', error);
      });
    });
  }

  if (fullscreenBtn && container) {
    fullscreenBtn.addEventListener('click', function() {
      if (document.fullscreenElement) {
        document.exitFullscreen().catch(function() {});
      } else if (container.requestFullscreen) {
        container.requestFullscreen().catch(function() {});
      }
    });
  }

  video.addEventListener('click', function(event) {
    if (event.target === video) {
      if (video.paused) {
        resumeLivePlayback();
      } else {
        pauseLivePlayback();
      }
    }
  });
  video.addEventListener('play', updateLiveControls);
  video.addEventListener('pause', updateLiveControls);
  video.addEventListener('volumechange', updateLiveControls);
  video.addEventListener('ratechange', function() {
    if (video.playbackRate !== 1) video.playbackRate = 1;
  });

  updateLiveControls();
  resetQualitySelector();
}

function updateLiveControls() {
  var video = document.getElementById('videoPlayer');
  var playIcon = document.getElementById('livePlayIcon');
  var muteIcon = document.getElementById('liveMuteIcon');
  var volumeSlider = document.getElementById('liveVolumeSlider');
  if (!video) return;

  if (playIcon) {
    playIcon.innerHTML = video.paused ? '&#9654;' : '&#10074;&#10074;';
  }
  if (muteIcon) {
    muteIcon.innerHTML = (video.muted || video.volume === 0) ? '&#128263;' : '&#128266;';
  }
  if (volumeSlider && document.activeElement !== volumeSlider) {
    volumeSlider.value = String(video.volume);
  }
}

// ============================================
// Notifications
// ============================================
function showNotification(message, type) {
  type = type || 'success';
  var notif = document.createElement('div');
  var bgColor = type === 'success' ? '#10b981' : '#ef4444';
  notif.style.cssText = 'position:fixed;top:20px;right:20px;background:' + bgColor + ';color:white;padding:1rem 1.5rem;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.3);z-index:9999;font-weight:500;animation:slideIn 0.3s ease-out;';
  notif.textContent = message;
  document.body.appendChild(notif);

  setTimeout(function() {
    notif.style.opacity = '0';
    notif.style.transform = 'translateX(100px)';
    notif.style.transition = 'all 0.3s ease-out';
    setTimeout(function() { notif.remove(); }, 300);
  }, 3000);
}

// ============================================
// Cleanup
// ============================================
window.addEventListener('beforeunload', function() {
  if (statusCheckInterval) clearInterval(statusCheckInterval);
  stopStreamUptimeTicker();
  if (streamProblemStatusTimeout) clearTimeout(streamProblemStatusTimeout);
  if (volumeSaveTimeout && profilePlaybackVolume !== null) {
    clearTimeout(volumeSaveTimeout);
    persistProfileVolume(profilePlaybackVolume);
  }
  if (hlsPlayer) {
    hlsPlayer.destroy();
    hlsPlayer = null;
  }
});

// ============================================
// Initialization
// ============================================
window.addEventListener('DOMContentLoaded', function() {
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }';
  document.head.appendChild(style);

  var video = document.getElementById('videoPlayer');
  if (video) {
    video.addEventListener('error', function() {
      streamLoaded = false;
      currentStreamUrl = '';
      scheduleStatusRefreshAfterStreamProblem();
    });
    video.addEventListener('ended', function() {
      streamLoaded = false;
      currentStreamUrl = '';
      scheduleStatusRefreshAfterStreamProblem();
    });
    video.addEventListener('stalled', function() {
      scheduleStatusRefreshAfterStreamProblem();
    });
  }

  setupLiveControls();
  initWatch();
});

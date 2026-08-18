// ============================================
// Media Page - profile-first media catalogue
// ============================================

(function() {
  'use strict';

  var state = {
    items: [],
    vpsItems: [],
    macFiles: [],
    profiles: [],
    kind: 'video',
    selectedProfile: '',
    filterProfile: '',
    filterSource: 'all',
    filterLiveStatus: 'all',
    profileSearch: '',
    videoSearch: '',
    sort: 'newest',
    loading: false,
    profileRefreshing: false,
    pendingDelete: null,
    pendingBothChoices: {},
    pendingProfileDelete: null,
    currentViewerItem: null,
    profileSettings: null,
    viewerSaveInterval: null,
    viewerNextTimer: null,
    viewerNextCountdownTimer: null,
    loadController: null,
    loadRequestId: 0,
    creatingProfile: false,
    resolvingProfileImage: false,
    localSessionId: '',
    macHelperAvailable: false,
    macHelperDirect: false,
    syncStatuses: {},
    syncScannedAt: 0,
    deviceFilter: 'all',
    selectedItemIds: {},
    macScanInProgress: false,
    mediaPage: 1,
    profilePage: 1
  };
  var MEDIA_PAGE_SIZE = 12;
  var PROFILE_PAGE_ROWS = 2;
  var MAC_HELPER_BASE = 'http://127.0.0.1:17899';
  var fileWatchTimer = null;
  var fileWatchTick = null;
  var fileWatchGeneration = 0;
  var FILE_WATCH_MAX_MS = 6 * 60 * 60 * 1000;

  var PROFILE_SOURCE_OPTIONS = [
    { value: 'twitch', label: 'Twitch', domains: ['twitch.tv', 'www.twitch.tv'] },
    { value: 'bilibili', label: 'Bilibili', domains: ['live.bilibili.com', 'bilibili.com', 'www.bilibili.com'] },
    { value: 'chaturbate', label: 'Chaturbate', domains: ['chaturbate.com'] },
    { value: 'stripchat', label: 'Stripchat', domains: ['stripchat.com', 'www.stripchat.com'] }
  ];
  var PROFILE_LIVE_STATUS_OPTIONS = [
    { value: 'all', label: 'All' },
    { value: 'live', label: 'Live' },
    { value: 'private', label: 'Private' },
    { value: 'locked', label: 'Locked' },
    { value: 'offline', label: 'Offline' }
  ];
  var PROFILE_GLOBAL_LIVE_STATUS_OPTIONS = [
    { value: 'live', label: 'Live' },
    { value: 'private', label: 'Private' },
    { value: 'locked', label: 'Locked' },
    { value: 'offline', label: 'Offline' }
  ];

  var profileSearchTimer = null;
  var videoSearchTimer = null;
  var mediaVolumeUsername = '';
  var mediaPlaybackVolume = null;
  var mediaVolumeSaveTimeout = null;
  var mediaVolumeLoadRequestId = 0;

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function pad2(number) {
    return String(number).padStart(2, '0');
  }

  /** Standard datetime: 2026-08-02 21:21:42 */
  function formatDateTimeSeconds(timestamp) {
    if (!timestamp) return '';
    try {
      var value = new Date(Number(timestamp) * 1000);
      if (isNaN(value.getTime())) return '';
      return value.getFullYear() + '-' +
        pad2(value.getMonth() + 1) + '-' +
        pad2(value.getDate()) + ' ' +
        pad2(value.getHours()) + ':' +
        pad2(value.getMinutes()) + ':' +
        pad2(value.getSeconds());
    } catch (e) {
      return '';
    }
  }

  function formatDate(timestamp) {
    return formatDateTimeSeconds(timestamp) || '-';
  }

  /** Absolute last-live label for Media profile cards. */
  function formatLastLive(timestamp) {
    var absolute = formatDateTimeSeconds(timestamp);
    return absolute ? ('Last live · ' + absolute) : '';
  }

  /** Relative helper kept for title/tooltip compatibility. */
  function formatLastSeen(timestamp) {
    var stamp = Number(timestamp || 0);
    if (!stamp) return '';
    var now = Math.floor(Date.now() / 1000);
    var diff = now - stamp;
    if (diff < 60) return 'Last seen just now';
    if (diff < 3600) return 'Last seen ' + Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return 'Last seen ' + Math.floor(diff / 3600) + 'h ago';
    if (diff < 604800) return 'Last seen ' + Math.floor(diff / 86400) + 'd ago';
    var absolute = formatDateTimeSeconds(stamp);
    return absolute ? ('Last seen ' + absolute) : '';
  }

  /** Parse many on-disk / legacy stems into standard datetime text. */
  function standardDateTimeFromText(raw) {
    var text = String(raw || '').trim();
    if (!text) return '';
    var stem = text.replace(/\.[^.]+$/, '');
    var m = stem.match(/(?:^|[_\s-])(\d{4}-\d{2}-\d{2})[ T_](\d{2})[:.\-](\d{2})[:.\-](\d{2})(?:$|[_\s.-])/);
    if (!m) m = stem.match(/^(\d{4}-\d{2}-\d{2})[ T_](\d{2})[:.\-](\d{2})[:.\-](\d{2})$/);
    if (m) return m[1] + ' ' + m[2] + ':' + m[3] + ':' + m[4];
    m = stem.match(/(?:^|[_\s-])(\d{4}-\d{2}-\d{2})_(\d{6})(?:$|[_\s.-])/);
    if (!m) m = stem.match(/^(\d{4}-\d{2}-\d{2})_(\d{6})$/);
    if (m) {
      return m[1] + ' ' + m[2].slice(0, 2) + ':' + m[2].slice(2, 4) + ':' + m[2].slice(4, 6);
    }
    m = stem.match(/(?:^|[_\s-])(\d{8})[_-](\d{6})(?:$|[_\s.-])/);
    if (!m) m = stem.match(/^(\d{8})[_-](\d{6})$/);
    if (m) {
      return m[1].slice(0, 4) + '-' + m[1].slice(4, 6) + '-' + m[1].slice(6, 8) + ' ' +
        m[2].slice(0, 2) + ':' + m[2].slice(2, 4) + ':' + m[2].slice(4, 6);
    }
    m = stem.match(/^(\d{4}-\d{2}-\d{2})$/);
    if (m) return m[1] + ' 00:00:00';
    return '';
  }

  function formatDurationClock(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var remaining = total % 60;
    return hours + ':' + String(minutes).padStart(2, '0') + ':' + String(remaining).padStart(2, '0');
  }

  function formatQuality(resolution) {
    var match = String(resolution || '').match(/^\d+x(\d+)$/i);
    return match ? match[1] + 'p' : (resolution || '-');
  }

  function parseMediaResolution(resolution) {
    var match = String(resolution || '').match(/^(\d+)\s*[xX]\s*(\d+)$/);
    if (!match) return null;
    var width = parseInt(match[1], 10);
    var height = parseInt(match[2], 10);
    if (!(width > 0) || !(height > 0)) return null;
    return { width: width, height: height };
  }

  function applyMediaVideoBoxSize(video, item) {
    if (!video) return;
    var size = parseMediaResolution(item && item.resolution);
    if (!size && video.videoWidth > 0 && video.videoHeight > 0) {
      size = { width: video.videoWidth, height: video.videoHeight };
    }
    if (!size) {
      size = { width: 1920, height: 1080 };
    }
    video.width = size.width;
    video.height = size.height;
    video.style.aspectRatio = size.width + ' / ' + size.height;
  }

  function formatType(item) {
    if (item.type === 'image') return 'Photo';
    if (item.type === 'audio') return 'Audio';
    return 'Video';
  }

  function formatBytesShort(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    var units = ['KB', 'MB', 'GB', 'TB'];
    var i = -1;
    do {
      n /= 1024;
      i++;
    } while (n >= 1024 && i < units.length - 1);
    return (i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
  }

  function intSize(value) {
    return parseInt(value, 10) || 0;
  }

  function parseMacFileMeta(file) {
    var relative = String((file && file.filename) || '').replace(/\\/g, '/').replace(/^\/+/, '');
    var parts = relative.split('/').filter(Boolean);
    var basename = parts.length ? parts[parts.length - 1] : relative;
    var username = parts.length > 1 ? parts[0] : '';
    var title = standardDateTimeFromText(basename) || basename.replace(/\.[^.]+$/, '') || 'Mac video';
    var createdAt = 0;
    var parsed = Date.parse(String(title).replace(' ', 'T'));
    if (!isNaN(parsed)) createdAt = Math.floor(parsed / 1000);
    return {
      relative: relative,
      basename: basename,
      username: username,
      title: title,
      createdAt: createdAt
    };
  }

  function macThumbUrl(file, relative) {
    if (!state.localSessionId) return '';
    var params = new URLSearchParams();
    params.set('localSessionId', state.localSessionId);
    if (relative) params.set('relativePath', relative);
    var rid = String((file && file.recordingId) || '').trim();
    if (rid) params.set('recordingId', rid);
    return MAC_HELPER_BASE + '/thumb?' + params.toString();
  }

  function locationLabel(locations) {
    if (locations === 'both') return 'BOTH';
    if (locations === 'mac') return 'MAC';
    if (locations === 'vps') return 'VPS';
    return '';
  }

  /** Standard video name: 2026-08-02 21:21:42 */
  function displayMediaTitle(item) {
    if (!item) return '';
    var fromCreated = formatDateTimeSeconds(item.createdAt);
    if (fromCreated) return fromCreated;
    var fromText = standardDateTimeFromText(item.title || item.filename || item.macRelativePath || '');
    if (fromText) return fromText;
    return String(item.title || item.filename || '').trim();
  }

  /** Standard id + datetime: Nancy-A1 2026-08-02 21:21:42 */
  function displayMediaIdTitle(item) {
    var when = displayMediaTitle(item);
    var username = String((item && item.username) || '').trim();
    if (username && when) return username + ' ' + when;
    return when || username || '';
  }

  function itemHasMac(item) {
    return !!(item && (item.onMac || item.locations === 'mac' || item.locations === 'both'));
  }

  function itemHasVps(item) {
    return !!(item && (item.onVps || item.locations === 'vps' || item.locations === 'both') && !item.isMacOnly);
  }

  function itemIsBoth(item) {
    return !!(item && (item.locations === 'both' || (itemHasVps(item) && itemHasMac(item))));
  }

  function itemIsDownloadable(item) {
    return !!(item && itemHasVps(item) && item.type === 'video' && item.syncStatus !== 'synced');
  }

  function itemIsSelectable(item) {
    return !!(item && item.type === 'video' && (itemHasVps(item) || itemHasMac(item)));
  }

  function sortCatalogItems(items) {
    var list = (items || []).slice();
    var sortKey = state.sort || 'newest';
    list.sort(function(a, b) {
      if (sortKey === 'oldest') return intSize(a.createdAt) - intSize(b.createdAt);
      if (sortKey === 'largest') return intSize(b.size) - intSize(a.size);
      if (sortKey === 'smallest') return intSize(a.size) - intSize(b.size);
      if (sortKey === 'name') {
        return String(a.title || a.filename || '').localeCompare(String(b.title || b.filename || ''));
      }
      return intSize(b.createdAt) - intSize(a.createdAt);
    });
    return list;
  }

  function catalogMatchesFilters(item) {
    if (!item) return false;
    if (state.filterProfile && String(item.username || '') !== String(state.filterProfile)) return false;
    var query = String(state.videoSearch || '').trim().toLowerCase();
    if (query) {
      var hay = [
        item.filename,
        item.title,
        item.username,
        item.macRelativePath
      ].join(' ').toLowerCase();
      if (hay.indexOf(query) === -1) return false;
    }
    return true;
  }

  function findMacFileForVpsItem(item, macFiles, used) {
    var rid = String((item && item.recordingId) || '').trim();
    var i;
    if (rid) {
      for (i = 0; i < macFiles.length; i++) {
        if (used[i]) continue;
        if (String(macFiles[i].recordingId || '').trim() === rid) {
          used[i] = true;
          return macFiles[i];
        }
      }
    }
    var fname = String((item && item.filename) || '');
    var size = intSize(item && item.size);
    for (i = 0; i < macFiles.length; i++) {
      if (used[i]) continue;
      var macName = String(macFiles[i].filename || '').split('/').pop();
      if (macName === fname && intSize(macFiles[i].size) === size) {
        used[i] = true;
        return macFiles[i];
      }
    }
    return null;
  }

  function annotateVpsItemWithMac(item, macFile) {
    var syncStatus = state.syncStatuses[item.id] || 'unknown';
    if (macFile) {
      if (syncStatus === 'unknown') {
        syncStatus = intSize(macFile.size) === intSize(item.size) ? 'synced' : 'incomplete';
      }
      item.onVps = true;
      item.onMac = true;
      item.locations = 'both';
      item.syncStatus = syncStatus;
      item.macRelativePath = String(macFile.filename || '');
      item.macSize = intSize(macFile.size);
      item.isMacOnly = false;
    } else {
      item.onVps = true;
      item.onMac = false;
      item.locations = 'vps';
      item.syncStatus = state.macHelperAvailable && state.syncScannedAt
        ? (syncStatus === 'unknown' ? 'not_synced' : syncStatus)
        : syncStatus;
      item.macRelativePath = '';
      item.macSize = 0;
      item.isMacOnly = false;
    }
    return item;
  }

  function buildUnifiedCatalog() {
    var macFiles = state.macFiles || [];
    var used = {};
    var unified = [];

    (state.vpsItems || []).forEach(function(item) {
      var copy = Object.assign({}, item);
      var macFile = findMacFileForVpsItem(item, macFiles, used);
      annotateVpsItemWithMac(copy, macFile);
      unified.push(copy);
    });

    macFiles.forEach(function(file, index) {
      if (used[index]) return;
      var meta = parseMacFileMeta(file);
      var rid = String(file.recordingId || '').trim();
      var id = rid ? ('mac:' + rid) : ('mac:' + meta.relative);
      var duration = numberOrZero(file.durationSeconds || file.duration);
      var resolution = String(file.resolution || '').trim();
      unified.push({
        id: id,
        type: 'video',
        recordingId: rid,
        filename: meta.basename,
        title: meta.title,
        username: meta.username,
        size: intSize(file.size),
        sizeFormatted: formatBytesShort(file.size),
        createdAt: meta.createdAt,
        duration: duration,
        resolution: resolution,
        thumbnail: macThumbUrl(file, meta.relative),
        url: '',
        browserPlayable: false,
        onVps: false,
        onMac: true,
        locations: 'mac',
        syncStatus: 'synced',
        macRelativePath: meta.relative,
        macSize: intSize(file.size),
        isMacOnly: true,
        isWatched: false
      });
    });

    return sortCatalogItems(unified.filter(catalogMatchesFilters));
  }

  function rebuildVisibleItems() {
    var device = state.deviceFilter || 'all';
    var macFiles = state.macFiles || [];
    var used = {};

    if (device === 'vps') {
      state.items = (state.vpsItems || []).map(function(item) {
        var copy = Object.assign({}, item);
        var macFile = findMacFileForVpsItem(item, macFiles, used);
        return annotateVpsItemWithMac(copy, macFile);
      }).filter(catalogMatchesFilters);
    } else {
      state.items = buildUnifiedCatalog().filter(function(item) {
        if (device === 'mac') return itemHasMac(item);
        if (device === 'both') return itemIsBoth(item);
        return true;
      });
    }

    // Drop stale selections that are no longer visible/downloadable.
    Object.keys(state.selectedItemIds).forEach(function(id) {
      if (!itemById(id)) delete state.selectedItemIds[id];
    });

    renderRecentSection(state.items.length);
    updateMacToolbar();
  }

  function numberOrZero(value) {
    var num = Number(value);
    return Number.isFinite(num) && num > 0 ? num : 0;
  }

  function normalizeVolume(value) {
    if (value === null || value === undefined || value === '') return null;
    var volume = Number(value);
    if (!Number.isFinite(volume)) return null;
    return Math.min(1, Math.max(0, volume));
  }

  function getLocalVolume(key) {
    var saved = localStorage.getItem(key);
    return saved === null ? null : normalizeVolume(saved);
  }

  function persistMediaProfileVolume(username, volume) {
    mediaVolumeSaveTimeout = null;
    if (!username) return;

    fetch('/api/models/' + encodeURIComponent(username) + '/volume', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ volume: volume }),
      keepalive: true
    }).catch(function(e) {
      console.warn('Could not save media profile volume:', e);
    });
  }

  function flushMediaProfileVolume() {
    if (mediaVolumeSaveTimeout && mediaPlaybackVolume !== null) {
      clearTimeout(mediaVolumeSaveTimeout);
      persistMediaProfileVolume(mediaVolumeUsername, mediaPlaybackVolume);
    }
  }

  function saveMediaProfileVolume(username, volume) {
    var normalized = normalizeVolume(volume);
    if (!username || normalized === null) return;

    mediaVolumeUsername = username;
    mediaPlaybackVolume = normalized;
    localStorage.setItem('video_volume_' + username, String(normalized));

    if (mediaVolumeSaveTimeout) {
      clearTimeout(mediaVolumeSaveTimeout);
    }
    mediaVolumeSaveTimeout = setTimeout(function() {
      persistMediaProfileVolume(username, normalized);
    }, 250);
  }

  function getSavedMediaProfileVolume(username) {
    if (mediaVolumeUsername === username && mediaPlaybackVolume !== null) {
      return mediaPlaybackVolume;
    }

    var profileVolume = getLocalVolume('video_volume_' + username);
    if (profileVolume !== null) return profileVolume;

    var legacyGlobalVolume = getLocalVolume('video_volume_global');
    if (legacyGlobalVolume !== null) return legacyGlobalVolume;

    return 0.5;
  }

  function loadMediaProfileVolume(video, item) {
    var username = item && item.username ? item.username : '';
    if (!video || !username) return;

    var pendingVolume = (
      mediaVolumeSaveTimeout &&
      mediaVolumeUsername === username &&
      mediaPlaybackVolume !== null
    ) ? mediaPlaybackVolume : null;
    flushMediaProfileVolume();
    if (pendingVolume !== null) {
      mediaVolumeUsername = username;
      mediaPlaybackVolume = pendingVolume;
      video.volume = pendingVolume;
      return;
    }

    mediaVolumeUsername = username;
    mediaPlaybackVolume = null;

    var requestId = ++mediaVolumeLoadRequestId;
    fetch('/api/models/' + encodeURIComponent(username) + '/volume', { cache: 'no-store' })
      .then(function(res) {
        if (!res.ok) return null;
        return res.json().catch(function() { return null; });
      })
      .then(function(data) {
        if (requestId !== mediaVolumeLoadRequestId || state.currentViewerItem !== item || !video.isConnected) {
          return;
        }

        var saved = normalizeVolume(data && data.volume);
        if (saved !== null) {
          mediaPlaybackVolume = saved;
          localStorage.setItem('video_volume_' + username, String(saved));
          video.volume = saved;
          return;
        }

        var profileVolume = getLocalVolume('video_volume_' + username);
        if (profileVolume !== null) {
          video.volume = profileVolume;
          saveMediaProfileVolume(username, profileVolume);
        }
      })
      .catch(function(e) {
        console.warn('Could not load media profile volume:', e);
      });
  }

  function setupMediaProfileVolume(video, item) {
    var username = item && item.username ? item.username : '';
    if (!video || !username) return;

    video.volume = getSavedMediaProfileVolume(username);
    loadMediaProfileVolume(video, item);

    video.addEventListener('volumechange', function() {
      if (!video.muted || video.volume === 0) {
        saveMediaProfileVolume(username, video.volume);
      }
    });
  }

  function mediaPlaybackDuration(item) {
    return numberOrZero(item && item.playbackDuration) || numberOrZero(item && item.duration);
  }

  function mediaPlaybackProgress(item) {
    if (!item || item.type !== 'video') return 0;
    var explicitProgress = Number(item.playbackProgress);
    if (Number.isFinite(explicitProgress) && explicitProgress > 0) {
      return Math.max(0, Math.min(100, Math.round(explicitProgress)));
    }
    var duration = mediaPlaybackDuration(item);
    var position = numberOrZero(item.playbackPosition);
    if (!duration || !position) return 0;
    return Math.max(0, Math.min(100, Math.round((position / duration) * 100)));
  }

  function mediaWatchedThreshold(item) {
    var threshold = Number(item && item.watchedThreshold);
    if (!Number.isFinite(threshold)) return 90;
    return Math.max(0, Math.min(100, threshold));
  }

  function updateMediaPlaybackState(item, position, duration, data) {
    if (!item || item.type !== 'video') return;
    var savedDuration = numberOrZero(duration) || mediaPlaybackDuration(item);
    var savedPosition = numberOrZero(position);
    item.playbackPosition = savedPosition;
    item.playbackDuration = savedDuration;

    if (data && typeof data.progress === 'number') {
      item.playbackProgress = data.progress;
    } else if (savedDuration > 0 && savedPosition > 0) {
      item.playbackProgress = Math.max(0, Math.min(100, Math.round((savedPosition / savedDuration) * 100)));
    } else {
      item.playbackProgress = 0;
    }

    if (data && typeof data.watchedThreshold === 'number') {
      item.watchedThreshold = data.watchedThreshold;
    }
    if (data && data.watchedAt) {
      item.watchedAt = data.watchedAt;
    }
    if (data && typeof data.isWatched === 'boolean') {
      item.isWatched = data.isWatched;
      return;
    }

    var threshold = mediaWatchedThreshold(item);
    if (savedDuration > 0 && savedPosition > 0 && item.playbackProgress >= threshold) {
      item.isWatched = true;
      if (!item.watchedAt) item.watchedAt = Math.floor(Date.now() / 1000);
    }
  }

  function refreshMediaCard(item) {
    if (!item || !item.id) return;
    var cards = document.querySelectorAll('.media-card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.mediaId === item.id) {
        cards[i].outerHTML = renderCard(item);
        return;
      }
    }
  }

  function itemById(id) {
    for (var i = 0; i < state.items.length; i++) {
      if (state.items[i].id === id) return state.items[i];
    }
    return null;
  }

  function profileByUsername(username) {
    for (var i = 0; i < state.profiles.length; i++) {
      if (state.profiles[i].username === username) return state.profiles[i];
    }
    return null;
  }

  function profileLabel(profile) {
    if (!profile) return state.selectedProfile || state.filterProfile || '';
    return profile.displayName || profile.username || '';
  }

  function providerLabel(sourceType) {
    var t = String(sourceType || '').toLowerCase();
    for (var i = 0; i < PROFILE_SOURCE_OPTIONS.length; i++) {
      if (PROFILE_SOURCE_OPTIONS[i].value === t) return PROFILE_SOURCE_OPTIONS[i].label;
    }
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : 'Chaturbate';
  }

  function profileExists(username) {
    return !!(username && profileByUsername(username));
  }

  function profileSourceTypes(profile) {
    var types = [];
    if (!profile) return types;
    var primary = String(profile.sourceType || profile.source_type || '').toLowerCase();
    if (primary) types.push(primary);
    var streamSources = profile.streamSources || profile.stream_sources || [];
    for (var i = 0; i < streamSources.length; i++) {
      var source = streamSources[i] || {};
      var token = String(source.sourceType || source.source_type || '').toLowerCase();
      if (token && types.indexOf(token) === -1) types.push(token);
    }
    return types;
  }

  function profileMatchesSourceFilter(profile, source) {
    var needle = String(source || 'all').toLowerCase();
    if (!needle || needle === 'all') return true;
    return profileSourceTypes(profile).indexOf(needle) !== -1;
  }

  function profileRoomStatus(profile) {
    return String(
      (profile && (profile.roomStatus || profile.room_status)) || ''
    ).trim().toLowerCase().replace(/\s+/g, '_');
  }

  function profileIsLocked(profile) {
    return ['password_protected', 'hidden'].indexOf(profileRoomStatus(profile)) !== -1;
  }

  function profileIsPrivate(profile) {
    var roomStatus = profileRoomStatus(profile);
    return [
      'private', 'p2p', 'p2pvoice', 'p2p_voice', 'group', 'groupshow', 'group_show',
      'ticket', 'ticketshow', 'ticket_show', 'premium', 'spy',
      'virtualprivate', 'virtual_private', 'true_private', 'private_spy',
      'privateshow', 'private_show'
    ].indexOf(roomStatus) !== -1
      || /private|p2p|group|ticket|premium|spy/.test(roomStatus);
  }

  function profileIsOfflineStatus(profile) {
    var roomStatus = profileRoomStatus(profile);
    return [
      'off', 'offline', 'away', 'idle', 'inactive', 'not_live'
    ].indexOf(roomStatus) !== -1;
  }

  function profileLiveBucket(profile) {
    if (profileIsLocked(profile)) return 'locked';
    if (profileIsPrivate(profile)) return 'private';
    // Stripchat may still send isOnline=true with status "off".
    if (profileIsOfflineStatus(profile)) return 'offline';
    if (profile && profile.isOnline) return 'live';
    return 'offline';
  }

  function profileStatusDetail(profile, bucket) {
    var status = profileRoomStatus(profile);
    var labels = {
      p2p: 'P2P', p2pvoice: 'P2P Voice', p2p_voice: 'P2P Voice',
      group: 'Group', groupshow: 'Group', group_show: 'Group',
      ticket: 'Ticket', ticketshow: 'Ticket', ticket_show: 'Ticket',
      premium: 'Premium', spy: 'Spy',
      virtualprivate: 'Virtual Private', virtual_private: 'Virtual Private',
      true_private: 'Private', private_spy: 'Private Spy',
      password_protected: 'Password', hidden: 'Hidden',
      away: 'Away', idle: 'Idle', inactive: 'Inactive'
    };
    if (!status || status === 'public' || status === 'private' || status === 'offline' || status === 'off') return '';
    if (bucket === 'offline' && status === 'not_live') return '';
    return labels[status] || '';
  }

  function profileMatchesLiveStatus(profile, status) {
    var needle = String(status || 'all').toLowerCase();
    if (!needle || needle === 'all') return true;
    return profileLiveBucket(profile) === needle;
  }

  function visibleProfiles() {
    var query = String(state.profileSearch || '').trim().toLowerCase();
    var source = String(state.filterSource || 'all').toLowerCase();
    var liveStatus = String(state.filterLiveStatus || 'all').toLowerCase();
    return state.profiles.filter(function(profile) {
      if (!profileMatchesSourceFilter(profile, source)) return false;
      if (!profileMatchesLiveStatus(profile, liveStatus)) return false;
      if (!query) return true;
      return [
        profile.username,
        profile.displayName,
        profile.channelUsername,
        profile.channelUrl,
        providerLabel(profile.sourceType || profile.source_type)
      ].some(function(value) {
        return String(value || '').toLowerCase().indexOf(query) !== -1;
      });
    });
  }

  function profileGridColumns() {
    var width = window.innerWidth || document.documentElement.clientWidth || 1200;
    if (width <= 720) return 1;
    if (width <= 1180) return 2;
    return 3;
  }

  function profilePageSize() {
    return Math.max(1, profileGridColumns() * PROFILE_PAGE_ROWS);
  }

  function renderProfileSourceFilters() {
    var row = $('mediaProfileSourceFilters');
    if (!row) return;
    var activeSource = String(state.filterSource || 'all').toLowerCase();
    var activeStatus = String(state.filterLiveStatus || 'all').toLowerCase();
    var allSitesActive = activeSource === 'all' && activeStatus === 'all';
    var buttons = [
      '<button type="button" class="media-profile-source-btn' +
      (allSitesActive ? ' active' : '') +
      '" data-source="all">All</button>'
    ];
    PROFILE_SOURCE_OPTIONS.forEach(function(option) {
      buttons.push(
        '<button type="button" class="media-profile-source-btn' +
        (activeSource === option.value ? ' active' : '') +
        '" data-source="' + escapeHtml(option.value) + '">' +
        escapeHtml(option.label) +
        '</button>'
      );
    });
    // Cross-site live-status shortcuts on the same website row.
    buttons.push('<span class="media-profile-website-sep" aria-hidden="true"></span>');
    PROFILE_GLOBAL_LIVE_STATUS_OPTIONS.forEach(function(option) {
      var active = activeSource === 'all' && activeStatus === option.value;
      buttons.push(
        '<button type="button" class="media-profile-source-btn media-profile-global-status-btn' +
        (active ? ' active' : '') +
        '" data-global-live-status="' + escapeHtml(option.value) + '">' +
        escapeHtml(option.label) +
        '</button>'
      );
    });
    row.innerHTML = buttons.join('');
  }

  function renderProfileStatusFilters() {
    var row = $('mediaProfileStatusFilters');
    if (!row) return;
    var source = String(state.filterSource || 'all').toLowerCase();
    // Per-website status chips only after a concrete website is chosen.
    if (!source || source === 'all') {
      row.hidden = true;
      row.innerHTML = '';
      return;
    }
    row.hidden = false;
    var active = String(state.filterLiveStatus || 'all').toLowerCase();
    var buttons = PROFILE_LIVE_STATUS_OPTIONS.map(function(option) {
      return (
        '<button type="button" class="media-profile-status-btn' +
        (active === option.value ? ' active' : '') +
        '" data-live-status="' + escapeHtml(option.value) + '">' +
        escapeHtml(option.label) +
        '</button>'
      );
    });
    row.innerHTML = buttons.join('');
  }

  function selectedProfileStillVisible() {
    if (!state.filterProfile) return true;
    var selected = profileByUsername(state.filterProfile);
    if (!selected) return false;
    if (!profileMatchesSourceFilter(selected, state.filterSource)) return false;
    if (!profileMatchesLiveStatus(selected, state.filterLiveStatus)) return false;
    return true;
  }

  function applyProfileFilters(options) {
    options = options || {};
    if (Object.prototype.hasOwnProperty.call(options, 'source')) {
      state.filterSource = options.source;
    }
    if (Object.prototype.hasOwnProperty.call(options, 'liveStatus')) {
      state.filterLiveStatus = options.liveStatus;
    }
    state.profilePage = 1;
    renderProfileSourceFilters();
    renderProfileStatusFilters();
    if (!selectedProfileStillVisible()) {
      selectProfile('', false);
      return;
    }
    renderProfileCarousel();
  }

  function setProfileSourceFilter(source) {
    var next = String(source || 'all').trim().toLowerCase() || 'all';
    if (next !== 'all') {
      var known = PROFILE_SOURCE_OPTIONS.some(function(option) { return option.value === next; });
      if (!known) next = 'all';
    }
    // Website chips reset live-status; second row then starts at All.
    applyProfileFilters({ source: next, liveStatus: 'all' });
  }

  function setGlobalLiveStatusFilter(status) {
    var next = String(status || 'all').trim().toLowerCase() || 'all';
    var known = PROFILE_GLOBAL_LIVE_STATUS_OPTIONS.some(function(option) {
      return option.value === next;
    });
    if (!known) {
      applyProfileFilters({ source: 'all', liveStatus: 'all' });
      return;
    }
    // Cross-site Online / Offline / Private on the website row.
    applyProfileFilters({ source: 'all', liveStatus: next });
  }

  function setProfileLiveStatusFilter(status) {
    var next = String(status || 'all').trim().toLowerCase() || 'all';
    var known = PROFILE_LIVE_STATUS_OPTIONS.some(function(option) { return option.value === next; });
    if (!known) next = 'all';
    applyProfileFilters({ liveStatus: next });
  }

  function mediaCountLabel(count, singular, plural) {
    count = Number(count) || 0;
    return count + ' ' + (count === 1 ? singular : plural);
  }

  function formatProfileMediaCounts(profile) {
    return mediaCountLabel(profile && profile.videos, 'video', 'videos');
  }

  function isLiveCoverAvatarUrl(url) {
    var u = String(url || '').trim().toLowerCase();
    if (!u) return false;
    // Chaturbate live webcam frame / Stripchat snapshot or room preview.
    if (/thumb\.live\.mmcdn\.com\/riw\//i.test(u)) return true;
    if (/doppiocdn\./i.test(u) && /\/snapshot\//i.test(u)) return true;
    if (/(doppiocdn\.|static-proxy\.strpst\.com)/i.test(u) && /\/previews\//i.test(u)) return true;
    return false;
  }

  function profileImageUrl(profile) {
    if (!profile) return '';
    var url = String(profile.profileImageUrl || profile.profile_image_url || '').trim();
    // Never use live covers as the circular face — show letter avatar instead.
    if (isLiveCoverAvatarUrl(url)) return '';
    return url;
  }

  function firstLetter(value) {
    value = String(value || '?').trim();
    return (value.charAt(0) || '?').toUpperCase();
  }

  function splitLines(value) {
    return String(value || '')
      .split(/\r?\n/)
      .map(function(line) { return line.trim(); })
      .filter(Boolean);
  }

  function joinLines(value) {
    if (Array.isArray(value)) return value.join('\n');
    return String(value || '');
  }

  function normalizeProfileUsername(value) {
    return String(value || '')
      .trim()
      .replace(/[^A-Za-z0-9_.-]+/g, '-')
      .replace(/^[._-]+|[._-]+$/g, '');
  }

  function channelUsernameFromUrl(value) {
    try {
      var url = new URL(String(value || '').trim());
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
      var ignored = { b: true, chat: true, en: true, fr: true, room: true, rooms: true, videochat: true };
      var parts = url.pathname.split('/').map(function(part) {
        return decodeURIComponent(part || '').trim().replace(/^@+/, '');
      }).filter(Boolean);
      for (var i = 0; i < parts.length; i++) {
        if (!ignored[parts[i].toLowerCase()]) return normalizeProfileUsername(parts[i]);
      }
      return parts.length ? normalizeProfileUsername(parts[parts.length - 1]) : '';
    } catch (e) {
      return '';
    }
  }

  function sourceTypeFromUrl(value) {
    try {
      var url = new URL(String(value || '').trim());
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
      var host = (url.hostname || '').toLowerCase().replace(/\.$/, '');
      for (var i = 0; i < PROFILE_SOURCE_OPTIONS.length; i++) {
        var option = PROFILE_SOURCE_OPTIONS[i];
        for (var j = 0; j < option.domains.length; j++) {
          var domain = option.domains[j].toLowerCase();
          if (host === domain || host.slice(-(domain.length + 1)) === '.' + domain) {
            return option.value;
          }
        }
      }
    } catch (e) {
      return '';
    }
    return '';
  }

  function buildQuery(forceLiveRefresh) {
    var params = new URLSearchParams();
    params.set('kind', state.kind);
    params.set('sort', state.sort);
    params.set('metadata', 'lazy');
    params.set('live', 'true');
    if (forceLiveRefresh) params.set('live_refresh', 'true');
    params.set('limit', '1000');
    if (state.filterProfile) params.set('username', state.filterProfile);
    if (state.videoSearch) params.set('search', state.videoSearch);
    return params.toString();
  }

  async function loadMediaLibrary(options) {
    options = options || {};
    var requestId = state.loadRequestId + 1;
    state.loadRequestId = requestId;

    if (state.loadController && typeof state.loadController.abort === 'function') {
      state.loadController.abort();
    }
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    state.loadController = controller;
    state.loading = true;
    setStorageRefreshBusy(true);
    renderLoading();

    try {
      var fetchOptions = { cache: 'no-store' };
      if (controller) fetchOptions.signal = controller.signal;
      var res = await fetch('/api/media-library?' + buildQuery(!!options.forceLiveRefresh), fetchOptions);
      if (!res.ok) throw new Error('Failed to load media library');
      var data = await res.json();
      if (requestId !== state.loadRequestId) return;
      state.vpsItems = (data.items || []).map(function(item) {
        item.syncStatus = state.syncStatuses[item.id] || 'unknown';
        return item;
      });
      applyProfilesPayload(data.profiles || []);
      renderStats(data.libraryStats || data.stats || {}, data.storage || {});
      renderProfileCarousel();
      rebuildVisibleItems();
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      if (requestId !== state.loadRequestId) return;
      console.error('Error loading media library:', e);
      renderError();
    } finally {
      if (requestId === state.loadRequestId) {
        state.loading = false;
        state.loadController = null;
        setStorageRefreshBusy(false);
      }
    }
  }

  function applyProfilesPayload(profiles) {
    state.profiles = profiles || [];
    if (state.selectedProfile && !profileExists(state.selectedProfile)) state.selectedProfile = '';
    if (state.filterProfile && !profileExists(state.filterProfile)) state.filterProfile = '';
    if (state.filterProfile && !selectedProfileStillVisible()) {
      state.filterProfile = '';
      state.selectedProfile = '';
    }
  }

  async function refreshProfiles() {
    if (state.profileRefreshing) return;
    state.profileRefreshing = true;
    setProfileRefreshBusy(true);
    try {
      var params = new URLSearchParams();
      params.set('kind', 'all');
      params.set('metadata', 'lazy');
      params.set('live', 'true');
      params.set('live_refresh', 'true');
      params.set('limit', '1');
      var res = await fetch('/api/media-library?' + params.toString(), { cache: 'no-store' });
      if (!res.ok) throw new Error('Failed to refresh profiles');
      var data = await res.json();
      applyProfilesPayload(data.profiles || []);
      renderProfileCarousel();
    } catch (e) {
      console.error('Error refreshing profiles:', e);
      showToast(e.message || 'Profile refresh failed', 'error');
    } finally {
      state.profileRefreshing = false;
      setProfileRefreshBusy(false);
    }
  }

  function setRefreshBusy(id, busy) {
    var btn = $(id);
    if (!btn) return;
    btn.disabled = !!busy;
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function setStorageRefreshBusy(busy) {
    setRefreshBusy('mediaStorageRefreshBtn', busy);
  }

  function setProfileRefreshBusy(busy) {
    setRefreshBusy('mediaProfileRefreshBtn', busy);
  }

  function renderLoading() {
    var grid = $('mediaGrid');
    var meta = $('mediaResultMeta');
    var rail = $('mediaProfileRail');
    var pagination = $('mediaPagination');
    var profilePagination = $('mediaProfilePagination');
    if (pagination) pagination.hidden = true;
    if (profilePagination) profilePagination.hidden = true;
    if (meta) meta.textContent = 'Loading...';
    if (!state.profiles.length && rail) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading profiles...</p></div>';
    }
    if (grid) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading media...</p></div>';
    }
  }

  function renderError() {
    var grid = $('mediaGrid');
    var rail = $('mediaProfileRail');
    var meta = $('mediaResultMeta');
    if (meta) meta.textContent = 'Unable to load media';
    if (rail && !state.profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Profiles unavailable</p></div>';
    }
    if (grid) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Media unavailable</p></div>';
    }
  }

  function formatDiskPercent(value) {
    if (value == null || value === '') return '-';
    return String(value) + '%';
  }

  function renderStats(stats, storage) {
    storage = storage || {};
    if ($('mediaRecordingFolderSize')) $('mediaRecordingFolderSize').textContent = storage.recordingFolderFormatted || '-';
    if ($('mediaProcessingSize')) $('mediaProcessingSize').textContent = storage.processingFormatted || '0 B';
    if ($('mediaUntrackedSize')) $('mediaUntrackedSize').textContent = storage.untrackedFormatted || '0 B';
    if ($('mediaDiskUsed')) $('mediaDiskUsed').textContent = storage.diskUsedFormatted || '-';
    if ($('mediaDiskUsedPercent')) $('mediaDiskUsedPercent').textContent = formatDiskPercent(storage.diskUsedPercent);
    if ($('mediaDiskFree')) $('mediaDiskFree').textContent = storage.diskFreeFormatted || '-';
    if ($('mediaDiskFreePercent')) $('mediaDiskFreePercent').textContent = formatDiskPercent(storage.diskFreePercent);
  }

  function renderProfilePagination(totalPages) {
    var pagination = $('mediaProfilePagination');
    var numbers = $('mediaProfilePageNumbers');
    if (!pagination) return;
    pagination.hidden = totalPages <= 1;
    if (!numbers) return;
    var pageButtons = [];
    for (var page = 1; page <= totalPages; page++) {
      pageButtons.push(
        '<button class="media-page-number' + (page === state.profilePage ? ' active' : '') +
        '" type="button" data-profile-page="' + page + '" aria-label="Profile page ' + page +
        '" aria-current="' + (page === state.profilePage ? 'page' : 'false') + '">' + page + '</button>'
      );
    }
    numbers.innerHTML = pageButtons.join('');
  }

  function setProfilePage(page) {
    var profiles = visibleProfiles();
    var pageSize = profilePageSize();
    var totalPages = Math.max(1, Math.ceil(profiles.length / pageSize));
    state.profilePage = Math.min(totalPages, Math.max(1, Number(page) || 1));
    renderProfileCarousel();
    var section = document.querySelector('.media-profiles-section');
    if (section && section.scrollIntoView) {
      section.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function jumpProfilePageToUsername(username) {
    if (!username) return;
    var profiles = visibleProfiles();
    var index = -1;
    for (var i = 0; i < profiles.length; i++) {
      if (profiles[i].username === username) {
        index = i;
        break;
      }
    }
    if (index < 0) return;
    var pageSize = profilePageSize();
    state.profilePage = Math.floor(index / pageSize) + 1;
  }

  function renderProfileCarousel() {
    var rail = $('mediaProfileRail');
    var meta = $('mediaProfileMeta');
    if (!rail) return;
    var profiles = visibleProfiles();
    var sourceFilteredCount = state.profiles.filter(function(profile) {
      return profileMatchesSourceFilter(profile, state.filterSource)
        && profileMatchesLiveStatus(profile, state.filterLiveStatus);
    }).length;
    var pageSize = profilePageSize();
    var totalPages = Math.max(1, Math.ceil(profiles.length / pageSize) || 1);
    state.profilePage = Math.min(Math.max(1, state.profilePage), totalPages);
    var hasNarrowingFilter = !!(
      state.profileSearch
      || (state.filterSource && state.filterSource !== 'all')
      || (state.filterLiveStatus && state.filterLiveStatus !== 'all')
    );

    if (meta) {
      var count = profiles.length;
      var countLabel = count === 1 ? '1 profile' : count + ' profiles';
      if (hasNarrowingFilter) {
        meta.textContent = countLabel + ' of ' + (
          state.profileSearch ? sourceFilteredCount : state.profiles.length
        );
        if (!state.profileSearch) {
          meta.textContent = countLabel + ' of ' + state.profiles.length;
        }
      } else {
        meta.textContent = countLabel;
      }
    }

    if (!state.profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No profiles found</p></div>';
      renderProfilePagination(1);
      return;
    }

    if (!profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No profiles match this filter</p></div>';
      renderProfilePagination(1);
      return;
    }

    var start = (state.profilePage - 1) * pageSize;
    var pageProfiles = profiles.slice(start, start + pageSize);
    var cards = pageProfiles.map(renderProfileCard);
    // Pin "All streamers" as the first card under the website All row (page 1 only).
    if (state.profilePage === 1) {
      cards.unshift(renderAllProfilesCard());
    }
    rail.innerHTML = cards.join('');
    renderProfilePagination(totalPages);
  }

  function totalProfileVideoCount(profiles) {
    return (profiles || []).reduce(function(sum, profile) {
      return sum + Number(profile && profile.videos || 0);
    }, 0);
  }

  function renderAllProfilesCard() {
    var active = !state.selectedProfile && !state.filterProfile;
    var profiles = visibleProfiles();
    var profileCount = profiles.length;
    var videoCount = totalProfileVideoCount(profiles);
    var profileLabelText = profileCount === 1 ? '1 profile' : profileCount + ' profiles';
    var videoLabel = mediaCountLabel(videoCount, 'video', 'videos');
    return '' +
      '<article class="media-profile-card media-all-profiles-card' + (active ? ' active' : '') +
      '" role="button" tabindex="0" data-profile="" data-all-profiles="1" title="Show recordings for all streamers">' +
        '<div class="media-profile-info">' +
          '<div class="media-profile-avatar media-all-profiles-icon">' +
            '<span aria-hidden="true">&#128193;</span>' +
          '</div>' +
          '<div class="media-profile-copy">' +
            '<div class="media-profile-name">All streamers</div>' +
            '<div class="media-profile-counts">' + escapeHtml(profileLabelText) + '</div>' +
            '<div class="media-profile-counts">' + escapeHtml(videoLabel) + '</div>' +
          '</div>' +
        '</div>' +
      '</article>';
  }

  function renderProfileCard(profile) {
    var active = state.selectedProfile === profile.username;
    var name = profileLabel(profile);
    var firstStreamSource = (profile.streamSources || profile.stream_sources || [])[0] || {};
    var sourceType = profile.sourceType || profile.source_type || firstStreamSource.sourceType || firstStreamSource.source_type || '';
    var image = '';
    var profileImage = profileImageUrl(profile);
    if (profileImage) {
      image = '<img src="' + escapeHtml(profileImage) + '" alt="' + escapeHtml(name) + '" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'; this.parentElement.classList.add(\'missing-thumb\');">';
    }

    var countLabel = formatProfileMediaCounts(profile);
    var channelUrl = profile.channelUrl || firstStreamSource.channelUrl || firstStreamSource.channel_url || '';
    var channelUsername = profile.channelUsername || profile.channel_username || firstStreamSource.channelUsername || firstStreamSource.channel_username || profile.username;
    var watchUrl = '/watch/' + encodeURIComponent(channelUsername) + (sourceType ? '?source=' + encodeURIComponent(sourceType) : '');
    var followerText = profile.followers === null || profile.followers === undefined
      ? 'Followers unavailable'
      : Number(profile.followers || 0).toLocaleString() + ' followers';
    var hideFollowers = String(sourceType || '').toLowerCase() === 'stripchat';
    var liveBucket = profileLiveBucket(profile);
    var liveClass = liveBucket;
    var liveDetail = profileStatusDetail(profile, liveBucket);
    var liveText = liveBucket === 'live'
      ? 'Live · ' + Number(profile.viewers || 0).toLocaleString() + ' watching'
      : liveBucket.charAt(0).toUpperCase() + liveBucket.slice(1);
    if (liveDetail) liveText += ' · ' + liveDetail;
    var lastLiveStamp = Number(
      profile.lastLiveAt
      || profile.last_live_at
      || profile.lastSeenOnlineAt
      || profile.last_seen_online_at
      || profile.latestVideoAt
      || profile.latestAt
      || 0
    );
    // Show whenever we have a stamp so Offline cards get history and Live cards
    // still surface the tracked moment (updates while online, freezes when offline).
    var lastLiveText = lastLiveStamp ? formatLastLive(lastLiveStamp) : '';
    var lastLiveRelative = lastLiveText ? formatLastSeen(lastLiveStamp) : '';
    var lastLiveHtml = lastLiveText
      ? '<span class="offline media-profile-last-live"' +
          (lastLiveRelative ? ' title="' + escapeHtml(lastLiveRelative) + '"' : '') +
        '>' + escapeHtml(lastLiveText) + '</span>'
      : '';

    return '' +
      '<article class="media-profile-card' + (active ? ' active' : '') + '" role="button" tabindex="0" data-profile="' + escapeHtml(profile.username) + '">' +
        '<div class="media-profile-info">' +
          '<div class="media-profile-avatar-column">' +
            '<div class="media-profile-avatar">' +
              image +
              '<div class="media-profile-placeholder"><span>' + escapeHtml(firstLetter(name)) + '</span></div>' +
            '</div>' +
            '<div class="media-profile-counts media-profile-avatar-counts">' + escapeHtml(countLabel) + '</div>' +
          '</div>' +
          '<div class="media-profile-copy">' +
            '<div class="media-profile-name">' + escapeHtml(name) + '</div>' +
            '<div class="media-profile-live-meta">' +
              '<span class="' + liveClass + '">' + escapeHtml(liveText) + '</span>' +
              lastLiveHtml +
            '</div>' +
            (hideFollowers ? '' : '<div class="media-profile-followers">' + escapeHtml(followerText) + '</div>') +
            (channelUrl ? '<div class="media-profile-channel-line"><span>' + escapeHtml(providerLabel(sourceType)) + ':</span> <a class="media-profile-channel-url" href="' + escapeHtml(channelUrl) + '" target="_blank" rel="noopener" data-profile-action="channel">' + escapeHtml(channelUrl) + '</a></div>' : '') +
          '</div>' +
          '<div class="media-profile-actions">' +
            '<button class="media-profile-follow-btn' + (profile.isFollowing ? ' is-enabled' : '') + '" type="button" title="' + (profile.isFollowing ? 'Unfollow' : 'Follow') + ' ' + escapeHtml(profile.username) + '" aria-label="' + (profile.isFollowing ? 'Unfollow' : 'Follow') + ' ' + escapeHtml(profile.username) + '" data-profile-action="follow" data-profile="' + escapeHtml(profile.username) + '" data-source="' + escapeHtml(sourceType) + '">' +
              (profile.isFollowing ? 'Unfollow' : 'Follow') +
            '</button>' +
            '<button class="media-profile-record-btn' + (profile.autoRecord ? ' is-enabled' : '') + '" type="button" title="' + (profile.autoRecord ? 'Pause recording' : 'Enable recording') + '" aria-label="' + (profile.autoRecord ? 'Pause recording' : 'Enable recording') + ' for ' + escapeHtml(profile.username) + '" aria-pressed="' + (profile.autoRecord ? 'true' : 'false') + '" data-profile-action="record" data-profile="' + escapeHtml(profile.username) + '">' + (profile.autoRecord ? 'Recording on' : 'Recording off') + '</button>' +
            '<a class="media-profile-watch-btn" href="' + escapeHtml(watchUrl) + '" data-profile-action="watch">Watch live</a>' +
            '<button class="media-profile-menu-btn" type="button" title="Profile settings" aria-label="Profile settings" data-profile-action="settings" data-profile="' + escapeHtml(profile.username) + '">&#8942;</button>' +
          '</div>' +
        '</div>' +
      '</article>';
  }

  async function toggleProfileFollow(username, sourceType, button) {
    var profile = profileByUsername(username);
    if (!profile || !username || !sourceType || (button && button.disabled)) return;
    var following = !!profile.isFollowing;
    if (button) button.disabled = true;
    try {
      var action = following ? 'unfollow' : 'follow';
      var res = await fetch('/api/providers/' + encodeURIComponent(sourceType) + '/' + action + '/' + encodeURIComponent(username), {
        method: 'POST'
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || data.message || 'Follow update failed');
      profile.isFollowing = !following;
      renderProfileCarousel();
      showToast(profile.isFollowing ? 'Followed ' + username : 'Unfollowed ' + username, 'success');
    } catch (e) {
      showToast(e.message || 'Follow update failed', 'error');
      if (button) button.disabled = false;
    }
  }

  async function toggleProfileRecording(username, button) {
    var profile = profileByUsername(username);
    if (!profile || !username || (button && button.disabled)) return;
    var enabled = !profile.autoRecord;
    if (button) button.disabled = true;
    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(username) + '/auto-record', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ autoRecord: enabled })
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Recording update failed');
      profile.autoRecord = !!data.autoRecord;
      if (data.profile) {
        profile.streamSources = data.profile.streamSources || profile.streamSources;
        profile.stream_sources = data.profile.stream_sources || profile.stream_sources;
        profile.recordQuality = data.profile.recordQuality || profile.recordQuality;
        profile.retentionDays = data.profile.retentionDays == null ? profile.retentionDays : data.profile.retentionDays;
      }
      renderProfileCarousel();
      showToast(profile.autoRecord ? 'Recording enabled for ' + username : 'Recording paused for ' + username, 'success');
    } catch (e) {
      showToast(e.message || 'Recording update failed', 'error');
      if (button) button.disabled = false;
    }
  }

  function syncProfileSelectionUI() {
    document.querySelectorAll('.media-profile-card').forEach(function(card) {
      if (card.getAttribute('data-all-profiles') === '1') {
        card.classList.toggle('active', !state.selectedProfile && !state.filterProfile);
        return;
      }
      card.classList.toggle('active', card.dataset.profile === state.selectedProfile);
    });
  }

  function renderRecentSection(total) {
    renderRecentTitle();
    renderGrid(total);
    syncFilterControls();
  }

  function renderRecentTitle() {
    var title = $('mediaRecentTitle');
    var meta = $('mediaResultMeta');
    var profileId = state.filterProfile || 'All videos';
    var countText = state.items.length === 1 ? '1 matching video' : state.items.length + ' matching videos';
    if (title) {
      title.innerHTML =
        '<span class="media-heading-profile">' + escapeHtml(profileId) + '</span>' +
        '<span class="media-heading-count">: ' + escapeHtml(countText) + '</span>';
    }
    if (meta) {
      meta.hidden = true;
      meta.textContent = '';
    }
  }

  function renderGrid(total) {
    var grid = $('mediaGrid');
    if (!grid) return;
    renderRecentTitle();

    if (!state.items.length) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No media found</p></div>';
      renderMediaPagination(1);
      return;
    }

    var totalPages = Math.max(1, Math.ceil(state.items.length / MEDIA_PAGE_SIZE));
    state.mediaPage = Math.min(Math.max(1, state.mediaPage), totalPages);
    var start = (state.mediaPage - 1) * MEDIA_PAGE_SIZE;
    grid.innerHTML = state.items.slice(start, start + MEDIA_PAGE_SIZE).map(renderCard).join('');
    renderMediaPagination(totalPages);
  }

  function renderMediaPagination(totalPages) {
    var pagination = $('mediaPagination');
    var numbers = $('mediaPageNumbers');
    if (!pagination) return;
    pagination.hidden = totalPages <= 1;
    if (numbers) {
      var pageButtons = [];
      for (var page = 1; page <= totalPages; page++) {
        pageButtons.push(
          '<button class="media-page-number' + (page === state.mediaPage ? ' active' : '') +
          '" type="button" data-page="' + page + '" aria-label="Page ' + page +
          '" aria-current="' + (page === state.mediaPage ? 'page' : 'false') + '">' + page + '</button>'
        );
      }
      numbers.innerHTML = pageButtons.join('');
    }
  }

  function setMediaPage(page) {
    var totalPages = Math.max(1, Math.ceil(state.items.length / MEDIA_PAGE_SIZE));
    state.mediaPage = Math.min(totalPages, Math.max(1, Number(page) || 1));
    renderGrid(state.items.length);
    var results = document.querySelector('.media-recent-section');
    if (results && results.scrollIntoView) {
      results.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function renderCard(item) {
    var thumb = '';
    if (item.thumbnail) {
      thumb = '<img src="' + escapeHtml(item.thumbnail) + '" alt="' + escapeHtml(item.title || item.filename) + '" loading="lazy" onerror="this.style.display=\'none\'; this.parentElement.classList.add(\'missing-thumb\');">';
    }

    var progress = mediaPlaybackProgress(item);
    var marker = item.type === 'image' ? '&#128247;' : item.type === 'audio' ? '&#9835;' : '&#9654;';
    var cardTitle = displayMediaTitle(item);
    var badges = [];
    if (item.isImported) badges.push('<span class="media-tag">Imported</span>');
    if (item.type === 'video' && itemHasVps(item) && !item.browserPlayable) {
      badges.push('<span class="media-tag">Original</span>');
    }
    var locTag = '';
    if (item.type === 'video') {
      var locKey = item.locations || '';
      var locText = locationLabel(locKey);
      if (locText) {
        locTag =
          '<span class="media-tag media-loc-tag loc-' + escapeHtml(locKey) +
          ' sync-' + escapeHtml(item.syncStatus || 'unknown') + '">' +
          escapeHtml(locText) + '</span>';
      }
    }
    // Quality only when WxH is known (VPS ffprobe / Mac Helper mdls·mp4·ffprobe).
    var qualityText = item.type === 'video' ? formatQuality(item.resolution) : '';
    var showQuality = !!(qualityText && qualityText !== '-');
    var selected = !!state.selectedItemIds[item.id];
    var selectable = itemIsSelectable(item);
    var bodyAction = selectable
      ? ' data-media-action="select-card" data-media-id="' + escapeHtml(item.id) + '"'
      : '';
    // Folder key stays username (Bilibili room id); show human label when known.
    var profileForItem = profileByUsername(item.username);
    var profileIdLabel = profileForItem ? profileLabel(profileForItem) : (item.username || '');

    return '' +
      '<article class="media-card' + (item.isWatched ? ' watched' : '') + (selected ? ' selected' : '') +
      (item.isMacOnly ? ' mac-only' : '') + '" role="button" tabindex="0" data-media-id="' + escapeHtml(item.id) + '" title="' + escapeHtml(itemHasMac(item) ? 'Click to open on Mac · ⌘/Ctrl-click to play online' : 'Click to play') + '">' +
        '<div class="media-card-thumb">' +
          thumb +
          '<div class="media-card-placeholder"><span aria-hidden="true">' + marker + '</span></div>' +
          (item.type === 'video' && progress > 0 ? '<div class="media-playback-progress" aria-hidden="true"><div style="width:' + progress + '%"></div></div>' : '') +
        '</div>' +
        '<div class="media-card-body"' + bodyAction + '>' +
          '<div class="media-card-title-row">' +
            '<div class="media-card-title" title="' + escapeHtml(item.macRelativePath || item.filename || cardTitle) + '">' +
              escapeHtml(cardTitle) + '</div>' +
          '</div>' +
          '<div class="media-card-meta">' +
            '<div class="media-card-profile-row">' +
              '<button class="media-card-profile-id" type="button" data-media-action="profile" data-profile="' + escapeHtml(item.username || '') + '" title="Show recordings for ' + escapeHtml(profileIdLabel || item.username || '') + '">' + escapeHtml(profileIdLabel || '') + '</button>' +
              locTag +
            '</div>' +
            (numberOrZero(item.duration)
              ? '<span class="media-card-detail-row">Duration: <strong class="media-duration-value">' + escapeHtml(formatDurationClock(item.duration)) + '</strong></span>'
              : '') +
            '<span class="media-card-detail-row">Recorded: ' + escapeHtml(item.createdAt ? formatDate(item.createdAt) : (cardTitle || '-')) + '</span>' +
            '<span class="media-card-detail-row">Size: ' + escapeHtml(item.sizeFormatted || formatBytesShort(item.size) || '-') + '</span>' +
            (showQuality
              ? '<div class="media-card-quality-row">' +
                  '<span>Quality: ' + escapeHtml(qualityText) + '</span>' +
                '</div>'
              : '') +
            (item.macRelativePath
              ? '<button type="button" class="media-mac-path" data-media-action="reveal-local" data-media-id="' +
                escapeHtml(item.id) + '" title="Show in Finder">' +
                'Mac: ' + escapeHtml(item.macRelativePath) + '</button>'
              : '') +
          '</div>' +
          (badges.length ? '<div class="media-badges">' + badges.join('') + '</div>' : '') +
        '</div>' +
      '</article>';
  }

  function clearViewerSaveInterval() {
    if (state.viewerSaveInterval) {
      clearInterval(state.viewerSaveInterval);
      state.viewerSaveInterval = null;
    }
  }

  function clearViewerNextPrompt() {
    if (state.viewerNextTimer) {
      clearTimeout(state.viewerNextTimer);
      state.viewerNextTimer = null;
    }
    if (state.viewerNextCountdownTimer) {
      clearInterval(state.viewerNextCountdownTimer);
      state.viewerNextCountdownTimer = null;
    }
    var stage = $('mediaViewerStage');
    var prompt = stage ? stage.querySelector('.media-next-prompt') : null;
    if (prompt) prompt.remove();
  }

  function currentVideoPlaylist() {
    return state.items.filter(function(item) {
      return item && item.type === 'video' && itemHasVps(item) && item.url;
    });
  }

  function nextVideoItem(item) {
    if (!item) return null;
    var videos = currentVideoPlaylist();
    for (var i = 0; i < videos.length; i++) {
      if (videos[i].id === item.id) {
        return videos[i + 1] || null;
      }
    }
    return null;
  }

  function previousVideoItem(item) {
    if (!item) return null;
    var videos = currentVideoPlaylist();
    for (var i = 0; i < videos.length; i++) {
      if (videos[i].id === item.id) {
        return videos[i - 1] || null;
      }
    }
    return null;
  }

  function playNextVideo(nextItem) {
    if (!nextItem) return;
    clearViewerNextPrompt();
    openViewer(nextItem);
  }

  function updateViewerNav(item) {
    var prev = $('mediaViewerPrev');
    var next = $('mediaViewerNext');
    var previousItem = item && item.type === 'video' ? previousVideoItem(item) : null;
    var nextItem = item && item.type === 'video' ? nextVideoItem(item) : null;
    if (prev) {
      prev.disabled = !previousItem;
      prev.dataset.mediaId = previousItem ? previousItem.id : '';
    }
    if (next) {
      next.disabled = !nextItem;
      next.dataset.mediaId = nextItem ? nextItem.id : '';
    }
  }

  function showNextPrompt(item) {
    if (!item || !state.currentViewerItem || state.currentViewerItem.id !== item.id) return;
    var nextItem = nextVideoItem(item);
    if (!nextItem) return;
    var stage = $('mediaViewerStage');
    if (!stage) return;

    clearViewerNextPrompt();
    var countdown = 5;
    var prompt = document.createElement('div');
    prompt.className = 'media-next-prompt';
    prompt.innerHTML = '' +
      '<div>' +
        '<div class="media-next-kicker">Up next</div>' +
        '<h3>' + escapeHtml(displayMediaTitle(nextItem)) + '</h3>' +
        '<p><span data-next-countdown>' + countdown + '</span>s until next video</p>' +
      '</div>' +
      '<div class="media-next-actions">' +
        '<button type="button" data-next-action="stay">Stay</button>' +
        '<button type="button" data-next-action="next">Next</button>' +
      '</div>';
    stage.appendChild(prompt);

    prompt.addEventListener('click', function(ev) {
      var action = ev.target.closest('[data-next-action]');
      if (!action) return;
      if (action.dataset.nextAction === 'next') {
        playNextVideo(nextItem);
      } else {
        clearViewerNextPrompt();
      }
    });

    state.viewerNextCountdownTimer = setInterval(function() {
      countdown -= 1;
      var countNode = prompt.querySelector('[data-next-countdown]');
      if (countNode) countNode.textContent = String(Math.max(0, countdown));
    }, 1000);
    state.viewerNextTimer = setTimeout(function() {
      playNextVideo(nextItem);
    }, countdown * 1000);
  }

  function videoPlaybackDuration(video, item) {
    return numberOrZero(video && video.duration) || mediaPlaybackDuration(item);
  }

  function videoPlaybackPosition(video, duration) {
    if (!video) return 0;
    if (video.ended && duration > 0) return duration;
    return numberOrZero(video.currentTime);
  }

  function saveMediaPlaybackPosition(video, item, options) {
    options = options || {};
    if (!video || !item || item.type !== 'video' || !item.recordingId) {
      return Promise.resolve(null);
    }

    var duration = videoPlaybackDuration(video, item);
    var position = videoPlaybackPosition(video, duration);
    if (position <= 0 && !options.force) return Promise.resolve(null);

    updateMediaPlaybackState(item, position, duration);
    if (options.updateCard !== false) refreshMediaCard(item);

    var now = Date.now();
    if (!options.force && item._mediaPlaybackSavedAt && now - item._mediaPlaybackSavedAt < 10000) {
      return Promise.resolve(null);
    }
    item._mediaPlaybackSavedAt = now;

    return fetch('/api/playback-position/' + encodeURIComponent(item.recordingId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        position: position,
        duration: duration,
        username: item.username || ''
      })
    }).then(function(res) {
      if (!res.ok) return null;
      return res.json().catch(function() { return null; });
    }).then(function(data) {
      if (data) {
        updateMediaPlaybackState(item, position, duration, data);
        if (options.updateCard !== false) refreshMediaCard(item);
      }
      return data;
    }).catch(function() {
      return null;
    });
  }

  async function loadMediaPlaybackPosition(video, item) {
    if (!video || !item || item.type !== 'video' || !item.recordingId) return;
    var duration = videoPlaybackDuration(video, item);
    try {
      var res = await fetch('/api/playback-position/' + encodeURIComponent(item.recordingId), { cache: 'no-store' });
      if (!res.ok) return;
      var data = await res.json();
      updateMediaPlaybackState(item, data.position, data.duration || duration, data);
      refreshMediaCard(item);

      var seekDuration = videoPlaybackDuration(video, item);
      var position = numberOrZero(data.position);
      if (!item.isWatched && position > 5 && seekDuration > 0 && position < seekDuration - 3) {
        video.currentTime = position;
      }
    } catch (e) {}
  }

  function setupMediaVideoPlayback(video, item) {
    if (!video || !item || item.type !== 'video' || !item.recordingId) return;

    clearViewerSaveInterval();
    video.addEventListener('loadedmetadata', function() {
      loadMediaPlaybackPosition(video, item);
    });
    video.addEventListener('timeupdate', function() {
      var previousProgress = mediaPlaybackProgress(item);
      var duration = videoPlaybackDuration(video, item);
      var position = videoPlaybackPosition(video, duration);
      updateMediaPlaybackState(item, position, duration);
      var nextProgress = mediaPlaybackProgress(item);
      if (nextProgress > 0 && nextProgress !== previousProgress) refreshMediaCard(item);
    });
    video.addEventListener('pause', function() {
      saveMediaPlaybackPosition(video, item, { force: true });
    });
    video.addEventListener('play', function() {
      clearViewerNextPrompt();
    });
    video.addEventListener('ended', function() {
      saveMediaPlaybackPosition(video, item, { force: true }).then(function() {
        showNextPrompt(item);
      });
    });
    state.viewerSaveInterval = setInterval(function() {
      if (!video.paused && !video.ended) {
        saveMediaPlaybackPosition(video, item);
      }
    }, 15000);
  }

  function openViewer(item) {
    if (!item) return;

    var viewer = $('mediaViewer');
    var stage = $('mediaViewerStage');
    var title = $('mediaViewerTitle');
    var deleteBtn = $('mediaViewerDelete');
    if (!viewer || !stage) return;

    clearViewerNextPrompt();
    state.currentViewerItem = item;
    title.textContent = displayMediaTitle(item);
    if (deleteBtn) deleteBtn.dataset.mediaId = item.id;
    updateViewerNav(item);

    stage.innerHTML = '';
    var mediaNode;
    if (item.type === 'image') {
      mediaNode = document.createElement('img');
      mediaNode.src = item.url;
      mediaNode.alt = item.title || item.filename;
      stage.appendChild(mediaNode);
    } else if (item.type === 'audio') {
      mediaNode = document.createElement('audio');
      mediaNode.src = item.url;
      mediaNode.controls = true;
      mediaNode.autoplay = true;
      stage.appendChild(mediaNode);
    } else {
      mediaNode = document.createElement('video');
      mediaNode.src = item.url;
      mediaNode.controls = true;
      mediaNode.autoplay = true;
      mediaNode.playsInline = true;
      applyMediaVideoBoxSize(mediaNode, item);
      mediaNode.addEventListener('loadedmetadata', function() {
        applyMediaVideoBoxSize(mediaNode, item);
      });
      setupMediaProfileVolume(mediaNode, item);
      stage.appendChild(mediaNode);
      setupMediaVideoPlayback(mediaNode, item);
      if (!item.browserPlayable) {
        var note = document.createElement('div');
        note.className = 'media-viewer-note';
        note.textContent = 'Original format. Your browser may not play it directly.';
        stage.appendChild(note);
      }
    }

    viewer.style.display = 'flex';
    viewer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('media-viewer-open');
  }

  function closeViewer() {
    var viewer = $('mediaViewer');
    var stage = $('mediaViewerStage');
    if (!viewer || !stage) return;

    var active = stage.querySelector('video, audio');
    clearViewerSaveInterval();
    clearViewerNextPrompt();
    if (active && active.tagName && active.tagName.toLowerCase() === 'video') {
      saveMediaPlaybackPosition(active, state.currentViewerItem, { force: true });
      flushMediaProfileVolume();
    }
    if (active) active.pause();
    stage.innerHTML = '';
    state.currentViewerItem = null;
    updateViewerNav(null);
    viewer.style.display = 'none';
    viewer.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('media-viewer-open');
  }

  function showToast(message, type) {
    if (typeof window.showNotification === 'function') {
      window.showNotification(message, type || 'success');
      return;
    }
    var existing = document.querySelector('.media-toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'media-toast ' + (type || 'success');
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(function() {
      toast.remove();
    }, 2600);
  }

  function classifyDeleteItems(items) {
    var vpsOnly = [];
    var macOnly = [];
    var both = [];
    (items || []).forEach(function(item) {
      if (!item) return;
      if (item.locations === 'both' || (itemHasVps(item) && itemHasMac(item))) both.push(item);
      else if (itemHasMac(item) && !itemHasVps(item)) macOnly.push(item);
      else if (itemHasVps(item)) vpsOnly.push(item);
    });
    return { vpsOnly: vpsOnly, macOnly: macOnly, both: both };
  }

  function readBothDeleteChoicesFromDom() {
    var choices = {};
    var body = $('mediaDeleteBothBody');
    if (!body) return choices;
    body.querySelectorAll('tr[data-media-id]').forEach(function(row) {
      var id = row.getAttribute('data-media-id');
      if (!id) return;
      var vps = row.querySelector('input[data-side="vps"]');
      var mac = row.querySelector('input[data-side="mac"]');
      choices[id] = {
        vps: !!(vps && vps.checked),
        mac: !!(mac && mac.checked)
      };
    });
    return choices;
  }

  function setBothDeleteSide(side, checked) {
    var body = $('mediaDeleteBothBody');
    if (!body) return;
    body.querySelectorAll('input[data-side="' + side + '"]').forEach(function(input) {
      if (input.disabled) return;
      input.checked = !!checked;
    });
    state.pendingBothChoices = readBothDeleteChoicesFromDom();
    syncBatchDeleteConfirmState();
  }

  function syncBatchDeleteConfirmState() {
    var items = state.pendingDelete || [];
    var groups = classifyDeleteItems(items);
    var choices = Object.keys(state.pendingBothChoices || {}).length
      ? state.pendingBothChoices
      : readBothDeleteChoicesFromDom();
    state.pendingBothChoices = choices;
    var helperReady = !!(state.localSessionId && state.macHelperAvailable);
    var macDeletes = groups.macOnly.length;
    var vpsDeletes = groups.vpsOnly.length;
    groups.both.forEach(function(item) {
      var choice = choices[item.id] || { vps: true, mac: false };
      if (choice.vps) vpsDeletes += 1;
      if (choice.mac) macDeletes += 1;
    });
    var confirm = $('mediaDeleteConfirm');
    if (!confirm) return;
    var needsHelper = macDeletes > 0;
    var nothingSelected = (vpsDeletes + macDeletes) === 0;
    confirm.disabled = nothingSelected || (needsHelper && !helperReady);
    confirm.textContent = 'Delete';
    if (nothingSelected) {
      confirm.title = 'Select at least one side to delete';
    } else if (needsHelper && !helperReady) {
      confirm.title = 'Start the HXYLIVE Mac helper to delete Mac copies';
    } else {
      confirm.removeAttribute('title');
    }
  }

  function renderBothDeleteTable(bothItems) {
    var panel = $('mediaDeleteBothPanel');
    var body = $('mediaDeleteBothBody');
    var title = $('mediaDeleteBothTitle');
    var dialog = document.querySelector('#mediaDeleteModal .media-delete-dialog');
    if (!panel || !body) return;
    if (!bothItems.length) {
      panel.hidden = true;
      body.innerHTML = '';
      if (dialog) dialog.classList.remove('has-both-choices');
      return;
    }
    panel.hidden = false;
    if (dialog) dialog.classList.add('has-both-choices');
    if (title) title.textContent = 'BOTH videos (' + bothItems.length + ') — choose sides';
    var helperReady = !!(state.localSessionId && state.macHelperAvailable);
    body.innerHTML = bothItems.map(function(item) {
      var choice = state.pendingBothChoices[item.id] || { vps: true, mac: false };
      var name = displayMediaIdTitle(item) || item.filename || item.id;
      return '' +
        '<tr data-media-id="' + escapeHtml(item.id) + '">' +
          '<td><div class="media-delete-both-name" title="' + escapeHtml(item.macRelativePath || item.filename || name) + '">' +
            escapeHtml(name) + '</div></td>' +
          '<td class="media-delete-check-cell">' +
            '<input type="checkbox" data-side="vps"' + (choice.vps ? ' checked' : '') +
            ' aria-label="Delete ' + escapeHtml(name) + ' from VPS"></td>' +
          '<td class="media-delete-check-cell">' +
            '<input type="checkbox" data-side="mac"' + (choice.mac ? ' checked' : '') +
            (helperReady ? '' : ' disabled') +
            ' aria-label="Delete ' + escapeHtml(name) + ' from Mac"></td>' +
        '</tr>';
    }).join('');
  }

  function openBatchDeleteConfirm() {
    var items = Object.keys(state.selectedItemIds).map(itemById).filter(itemIsSelectable);
    if (!items.length) return;
    var groups = classifyDeleteItems(items);
    var helperReady = !!(state.localSessionId && state.macHelperAvailable);
    if (groups.macOnly.length && !helperReady && !groups.vpsOnly.length && !groups.both.length) {
      showToast('Start the HXYLIVE Mac helper to delete Mac copies', 'error');
      return;
    }
    state.pendingDelete = items;
    state.pendingBothChoices = {};
    groups.both.forEach(function(item) {
      state.pendingBothChoices[item.id] = { vps: true, mac: false };
    });

    var modal = $('mediaDeleteModal');
    var target = $('mediaDeleteTarget');
    var message = $('mediaDeleteMessage');
    var confirm = $('mediaDeleteConfirm');
    var lines = [];
    if (groups.vpsOnly.length) {
      lines.push(groups.vpsOnly.length + ' VPS-only video(s) will be deleted from VPS');
    }
    if (groups.macOnly.length) {
      lines.push(groups.macOnly.length + ' Mac-only video(s) will be deleted from Mac');
    }
    if (!lines.length && groups.both.length) {
      lines.push('No VPS-only or Mac-only videos in this selection');
    }
    if (target) target.textContent = lines.join('\n') || (items.length + ' selected video(s)');
    if (message) {
      if (groups.both.length) {
        message.textContent = 'VPS-only and Mac-only files delete on their own device. For BOTH files, pick VPS and/or Mac below (VPS checked by default).';
      } else if (groups.macOnly.length && !groups.vpsOnly.length) {
        message.textContent = 'This removes the selected files from the Mac folder.';
      } else if (groups.vpsOnly.length && !groups.macOnly.length) {
        message.textContent = 'This removes the selected files from the VPS records folder.';
      } else {
        message.textContent = 'This removes the selected files from their unique device.';
      }
    }
    renderBothDeleteTable(groups.both);
    if (confirm) {
      confirm.dataset.idleLabel = 'Delete';
      confirm.textContent = 'Delete';
    }
    syncBatchDeleteConfirmState();
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    }
  }

  function closeDeleteConfirm() {
    var modal = $('mediaDeleteModal');
    var panel = $('mediaDeleteBothPanel');
    var body = $('mediaDeleteBothBody');
    var dialog = document.querySelector('#mediaDeleteModal .media-delete-dialog');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-delete-open');
    }
    if (panel) panel.hidden = true;
    if (body) body.innerHTML = '';
    if (dialog) dialog.classList.remove('has-both-choices');
    state.pendingDelete = null;
    state.pendingBothChoices = {};
  }

  async function deleteSelectedMacCopies(items) {
    var macItems = (items || []).filter(itemHasMac);
    if (!macItems.length) return { deletedCount: 0 };
    if (!state.localSessionId || !state.macHelperAvailable) {
      throw new Error('Start the HXYLIVE Mac helper to delete Mac copies');
    }
    var res = await fetch(MAC_HELPER_BASE + '/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        localSessionId: state.localSessionId,
        items: macItems.map(function(item) {
          return {
            relativePath: item.macRelativePath || '',
            recordingId: item.recordingId || ''
          };
        })
      }),
      cache: 'no-store'
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      throw new Error(data.error || data.detail || 'Mac delete failed');
    }
    return data;
  }

  function buildDeletePlan(items) {
    var groups = classifyDeleteItems(items);
    var choices = readBothDeleteChoicesFromDom();
    state.pendingBothChoices = choices;
    var vpsItems = groups.vpsOnly.slice();
    var macItems = groups.macOnly.slice();
    groups.both.forEach(function(item) {
      var choice = choices[item.id] || { vps: true, mac: false };
      if (choice.vps) vpsItems.push(item);
      if (choice.mac) macItems.push(item);
    });
    return { vpsItems: vpsItems, macItems: macItems };
  }

  async function confirmDeleteMedia() {
    var items = state.pendingDelete;
    if (!items || !items.length) return;
    var plan = buildDeletePlan(items);
    var vpsItems = plan.vpsItems;
    var macItems = plan.macItems;
    if (!vpsItems.length && !macItems.length) {
      showToast('Select at least one side to delete', 'error');
      return;
    }
    var confirm = $('mediaDeleteConfirm');
    function setBusy(busy) {
      if (confirm) {
        confirm.disabled = !!busy;
        confirm.textContent = busy ? 'Deleting...' : (confirm.dataset.idleLabel || 'Delete');
      }
      var body = $('mediaDeleteBothBody');
      if (body) {
        body.querySelectorAll('input[type="checkbox"]').forEach(function(input) {
          input.disabled = !!busy || (input.getAttribute('data-side') === 'mac' &&
            !(state.localSessionId && state.macHelperAvailable));
        });
      }
      ['mediaDeleteSelectAllVps', 'mediaDeleteSelectAllMac', 'mediaDeleteClearAllMac'].forEach(function(id) {
        var btn = $(id);
        if (btn) btn.disabled = !!busy;
      });
    }
    if (confirm) confirm.dataset.idleLabel = 'Delete';
    setBusy(true);

    try {
      if (macItems.length && (!state.localSessionId || !state.macHelperAvailable)) {
        throw new Error('Start the HXYLIVE Mac helper to delete Mac copies');
      }
      var deletedCount = 0;
      if (vpsItems.length) {
        var res = await fetch('/api/media-library/batch-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ itemIds: vpsItems.map(function(item) { return item.id; }) })
        });
        var data = await res.json().catch(function() { return {}; });
        if (!res.ok || data.success === false) {
          throw new Error(data.detail || data.message || 'VPS delete failed');
        }
        deletedCount += Number(data.deletedCount || vpsItems.length) || 0;
      }

      if (macItems.length) {
        var macResult = await deleteSelectedMacCopies(macItems);
        deletedCount += Number(macResult.deletedCount || 0) || 0;
      }

      if (state.currentViewerItem && items.some(function(item) { return item.id === state.currentViewerItem.id; })) {
        closeViewer();
      }
      closeDeleteConfirm();
      state.selectedItemIds = {};
      var notes = [];
      if (vpsItems.length) notes.push(vpsItems.length + ' VPS');
      if (macItems.length) notes.push(macItems.length + ' Mac');
      showToast(
        Math.max(deletedCount, vpsItems.length + macItems.length) + ' deletion(s)' +
        (notes.length ? ' (' + notes.join(', ') + ')' : ''),
        'success'
      );
      if (macItems.length) {
        await scanMacAndRefresh(true);
      } else {
        await loadMediaLibrary();
      }
    } catch (e) {
      console.error('Error deleting media:', e);
      showToast(e.message || 'Delete failed', 'error');
      setBusy(false);
      syncBatchDeleteConfirmState();
    }
  }

  function ensureSelectOption(select, value) {
    if (!select || !value) return;
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) return;
    }
    var option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  }

  function setField(id, value) {
    var field = $(id);
    if (!field) return;
    field.value = value == null ? '' : value;
  }

  function fieldValue(id) {
    var field = $(id);
    return field ? field.value.trim() : '';
  }

  function qualityOptionsHtml(value) {
    var values = ['best', '2160p', '1440p', '1080p', '720p', '480p', '360p'];
    if (value && values.indexOf(value) === -1) values.push(value);
    return values.map(function(option) {
      return '<option value="' + escapeHtml(option) + '"' + (option === value ? ' selected' : '') + '>' + escapeHtml(option === 'best' ? 'Best' : option) + '</option>';
    }).join('');
  }

  function sourceOptionsHtml(value) {
    var options = PROFILE_SOURCE_OPTIONS.map(function(option) {
      return { value: option.value, label: option.label };
    });
    var found = options.some(function(option) { return option.value === value; });
    if (value && !found) options.push({ value: value, label: value });
    return options.map(function(option) {
      return '<option value="' + escapeHtml(option.value) + '"' + (option.value === value ? ' selected' : '') + '>' + escapeHtml(option.label) + '</option>';
    }).join('');
  }

  function normalizeProfileSource(source, fallbackUsername) {
    source = source || {};
    var channelUrl = source.channelUrl || source.channel_url || '';
    var sourceType = (source.sourceType || source.source_type || '').toString().trim().toLowerCase();
    var urlSourceType = sourceTypeFromUrl(channelUrl);
    if (urlSourceType && (!sourceType || sourceType === 'chaturbate')) sourceType = urlSourceType;
    if (!sourceType) sourceType = 'chaturbate';
    var retention = parseInt(source.retentionDays == null ? source.retention_days : source.retentionDays, 10);
    if (Number.isNaN(retention)) retention = 30;
    retention = Math.max(0, Math.min(365, retention));
    return {
      sourceType: sourceType,
      channelUsername: source.channelUsername || source.channel_username || source.username || fallbackUsername || '',
      channelUrl: channelUrl,
      recordQuality: source.recordQuality || source.record_quality || 'best',
      retentionDays: retention,
      autoRecord: !!(source.autoRecord != null ? source.autoRecord : source.auto_record)
    };
  }

  function profileSourcesFromProfile(profile) {
    profile = profile || {};
    var sources = Array.isArray(profile.streamSources) ? profile.streamSources : profile.stream_sources;
    if (Array.isArray(sources) && sources.length) {
      return sources.map(function(source) {
        return normalizeProfileSource(source, profile.username || state.selectedProfile || '');
      });
    }
    if (!profile.username && state.creatingProfile) {
      return [normalizeProfileSource({
        sourceType: 'chaturbate',
        channelUsername: '',
        recordQuality: 'best',
        retentionDays: 30,
        autoRecord: false
      }, '')];
    }
    return [normalizeProfileSource({
      sourceType: profile.sourceType || profile.source_type || 'chaturbate',
      channelUsername: profile.username || state.selectedProfile || '',
      channelUrl: Array.isArray(profile.streamUrls) && profile.streamUrls.length ? profile.streamUrls[0] : '',
      recordQuality: profile.recordQuality || 'best',
      retentionDays: profile.retentionDays == null ? 30 : profile.retentionDays,
      autoRecord: !!profile.autoRecord
    }, profile.username || state.selectedProfile || '')];
  }

  function renderProfileSources(sources) {
    var list = $('profileSourcesList');
    if (!list) return;
    sources = Array.isArray(sources) && sources.length ? sources : [normalizeProfileSource({}, '')];
    list.innerHTML = sources.map(function(source, index) {
      source = normalizeProfileSource(source, '');
      return '' +
        '<div class="media-profile-source-row" data-source-index="' + index + '">' +
          '<input data-source-field="channelUsername" type="hidden" value="' + escapeHtml(source.channelUsername) + '">' +
          '<label>Source<select data-source-field="sourceType">' + sourceOptionsHtml(source.sourceType) + '</select></label>' +
          '<label>URL<input data-source-field="channelUrl" type="url" autocomplete="off" value="' + escapeHtml(source.channelUrl) + '" placeholder="https://..."></label>' +
          '<label>Quality<select data-source-field="recordQuality">' + qualityOptionsHtml(source.recordQuality) + '</select></label>' +
          '<label>Retention<input data-source-field="retentionDays" type="number" min="0" max="365" value="' + escapeHtml(source.retentionDays) + '"></label>' +
          '<label class="media-settings-check"><input data-source-field="autoRecord" type="checkbox"' + (source.autoRecord ? ' checked' : '') + '><span>Auto-record</span></label>' +
          '<button class="media-source-remove-btn" data-source-action="remove" type="button" title="Remove source" aria-label="Remove source">&#215;</button>' +
        '</div>';
    }).join('');
    syncLegacyStreamFields(sources);
  }

  function readProfileSources() {
    var rows = Array.prototype.slice.call(document.querySelectorAll('.media-profile-source-row'));
    return rows.map(function(row) {
      function get(field) {
        var el = row.querySelector('[data-source-field="' + field + '"]');
        if (!el) return '';
        if (el.type === 'checkbox') return !!el.checked;
        return el.value.trim();
      }
      var channelUrl = get('channelUrl');
      var selectedSource = (get('sourceType') || '').toLowerCase();
      var urlSource = sourceTypeFromUrl(channelUrl);
      if (urlSource && (!selectedSource || selectedSource === 'chaturbate')) selectedSource = urlSource;
      var retention = parseInt(get('retentionDays'), 10);
      if (Number.isNaN(retention)) retention = 30;
      return {
        sourceType: selectedSource || 'chaturbate',
        channelUsername: channelUsernameFromUrl(channelUrl) || normalizeProfileUsername(get('channelUsername')),
        channelUrl: channelUrl,
        recordQuality: get('recordQuality') || 'best',
        retentionDays: Math.max(0, Math.min(365, retention)),
        autoRecord: !!get('autoRecord')
      };
    }).filter(function(source) {
      return source.channelUsername || source.channelUrl;
    });
  }

  function syncLegacyStreamFields(sources) {
    sources = Array.isArray(sources) ? sources : [];
    var first = normalizeProfileSource(sources[0] || {}, state.selectedProfile || '');
    var quality = $('profileRecordQuality');
    var retention = $('profileRetentionDays');
    var source = $('profileSourceType');
    var auto = $('profileAutoRecord');
    ensureSelectOption(quality, first.recordQuality || 'best');
    if (quality) quality.value = first.recordQuality || 'best';
    if (retention) retention.value = first.retentionDays == null ? 30 : first.retentionDays;
    ensureSelectOption(source, first.sourceType || 'chaturbate');
    if (source) source.value = first.sourceType || 'chaturbate';
    if (auto) auto.checked = !!first.autoRecord;
  }

  function addProfileSource(source) {
    var sources = readProfileSources();
    sources.push(normalizeProfileSource(source || {
      sourceType: 'chaturbate',
      channelUsername: '',
      recordQuality: 'best',
      retentionDays: 30,
      autoRecord: false
    }, state.selectedProfile || ''));
    renderProfileSources(sources);
  }

  function fillProfileSettings(profile) {
    state.profileSettings = profile;
    state.creatingProfile = !profile || !profile.username;
    profile = profile || {};
    var subtitle = $('mediaProfileSettingsSubtitle');
    var title = $('mediaProfileSettingsTitle');
    if (title) title.textContent = state.creatingProfile ? 'New profile' : 'Profile settings';
    if (subtitle) subtitle.textContent = state.creatingProfile ? 'Create a local media profile' : profile.username;

    var usernameField = $('profileUsernameField');
    var usernameInput = $('profileUsername');
    if (usernameField) usernameField.style.display = state.creatingProfile ? 'grid' : 'none';
    if (usernameInput) {
      usernameInput.value = profile.username || '';
      usernameInput.disabled = !state.creatingProfile;
    }

    setField('profileDisplayName', profile.displayName || '');
    setField('profileFirstName', profile.firstName || '');
    setField('profileLastName', profile.lastName || '');
    setField('profileBirthDate', profile.birthDate || profile.birth_date || '');
    setField('profileImageUrl', profile.profileImageUrl || profile.profile_image_url || '');
    setField('profileImageSourceUrl', profile.profileImageSourceUrl || profile.profile_image_source_url || '');
    setField('profileAge', profile.age == null ? '' : profile.age);
    setField('profileAliases', profile.aliases || '');
    setField('profileTags', profile.tags || '');
    setField('profileAddress', profile.address || '');
    setField('profileCity', profile.city || '');
    setField('profileRegion', profile.region || '');
    setField('profilePostalCode', profile.postalCode || '');
    setField('profileCountry', profile.country || '');
    setField('profileSocialUrls', joinLines(profile.socialUrls));
    setField('profileStreamUrls', joinLines(profile.streamUrls));
    setField('profileProfileUrls', joinLines(profile.profileUrls));
    setField('profileNotes', profile.notes || '');
    var quality = $('profileRecordQuality');
    ensureSelectOption(quality, profile.recordQuality || 'best');
    if (quality) quality.value = profile.recordQuality || 'best';
    setField('profileRetentionDays', profile.retentionDays == null ? 30 : profile.retentionDays);
    var source = $('profileSourceType');
    ensureSelectOption(source, profile.sourceType || profile.source_type || 'chaturbate');
    if (source) source.value = profile.sourceType || profile.source_type || 'chaturbate';
    var auto = $('profileAutoRecord');
    if (auto) auto.checked = !!profile.autoRecord;
    renderProfileSources(profileSourcesFromProfile(profile));

    var deleteBtn = $('mediaProfileDeleteBtn');
    if (deleteBtn) deleteBtn.style.display = state.creatingProfile ? 'none' : '';
  }

  async function openProfileSettings(profileUsername) {
    if (profileUsername) {
      state.selectedProfile = profileUsername;
      syncProfileSelectionUI();
    }
    if (!state.selectedProfile) return;
    var modal = $('mediaProfileSettingsModal');
    var save = $('mediaProfileSettingsSave');
    if (save) save.disabled = true;

    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(state.selectedProfile), { cache: 'no-store' });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Profile unavailable');
      fillProfileSettings(data);
      if (modal) {
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('media-profile-settings-open');
      }
    } catch (e) {
      console.error('Error loading profile settings:', e);
      showToast(e.message || 'Profile unavailable', 'error');
    } finally {
      if (save) save.disabled = false;
    }
  }

  function closeProfileSettings() {
    var modal = $('mediaProfileSettingsModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-profile-settings-open');
    }
    state.creatingProfile = false;
  }

  async function resolveProfileImage() {
    if (state.resolvingProfileImage) return;
    var username = state.creatingProfile ? normalizeProfileUsername(fieldValue('profileUsername')) : state.selectedProfile;
    if (!username) {
      showToast('Username is required', 'error');
      return;
    }

    var button = $('profileResolveImageBtn');
    state.resolvingProfileImage = true;
    if (button) {
      button.disabled = true;
      button.textContent = 'Fetching...';
    }

    var query = fieldValue('profileDisplayName') ||
      [fieldValue('profileFirstName'), fieldValue('profileLastName')].filter(Boolean).join(' ') ||
      username;
    var payload = {
      query: query,
      profileImageUrl: fieldValue('profileImageUrl'),
      sourceUrl: fieldValue('profileImageSourceUrl'),
      profileUrls: splitLines(fieldValue('profileProfileUrls'))
    };

    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(username) + '/profile-image/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Profile image unavailable');
      var profile = data.profile || {};
      setField('profileImageUrl', profile.profileImageUrl || profile.profile_image_url || '');
      setField('profileImageSourceUrl', profile.profileImageSourceUrl || profile.profile_image_source_url || payload.sourceUrl || '');
      state.profileSettings = profile;
      if (state.creatingProfile) state.selectedProfile = username;
      await loadMediaLibrary();
      showToast('Profile image updated');
    } catch (e) {
      console.error('Error resolving profile image:', e);
      showToast(e.message || 'Profile image unavailable', 'error');
    } finally {
      state.resolvingProfileImage = false;
      if (button) {
        button.disabled = false;
        button.textContent = 'Fetch Babepedia image';
      }
    }
  }

  async function saveProfileSettings(ev) {
    if (ev) ev.preventDefault();
    var username = state.creatingProfile ? normalizeProfileUsername(fieldValue('profileUsername')) : state.selectedProfile;
    if (!username) {
      showToast('Username is required', 'error');
      return;
    }
    var save = $('mediaProfileSettingsSave');
    if (save) {
      save.disabled = true;
      save.textContent = 'Saving...';
    }

    var ageValue = fieldValue('profileAge');
    var age = ageValue ? parseInt(ageValue, 10) : null;
    if (Number.isNaN(age)) age = null;

    var profileSources = readProfileSources();
    syncLegacyStreamFields(profileSources);
    var retention = parseInt(fieldValue('profileRetentionDays'), 10);
    if (Number.isNaN(retention)) retention = 30;
    retention = Math.max(0, Math.min(365, retention));
    var auto = $('profileAutoRecord');
    var source = $('profileSourceType');
    var payload = {
      displayName: fieldValue('profileDisplayName'),
      firstName: fieldValue('profileFirstName'),
      lastName: fieldValue('profileLastName'),
      birthDate: fieldValue('profileBirthDate'),
      profileImageUrl: fieldValue('profileImageUrl'),
      profileImageSourceUrl: fieldValue('profileImageSourceUrl'),
      age: age,
      aliases: fieldValue('profileAliases'),
      tags: fieldValue('profileTags'),
      address: fieldValue('profileAddress'),
      city: fieldValue('profileCity'),
      region: fieldValue('profileRegion'),
      postalCode: fieldValue('profilePostalCode'),
      country: fieldValue('profileCountry'),
      socialUrls: splitLines(fieldValue('profileSocialUrls')),
      streamUrls: splitLines(fieldValue('profileStreamUrls')),
      profileUrls: splitLines(fieldValue('profileProfileUrls')),
      notes: fieldValue('profileNotes'),
      recordQuality: fieldValue('profileRecordQuality') || 'best',
      retentionDays: retention,
      sourceType: source ? source.value : 'chaturbate',
      autoRecord: auto ? auto.checked : false,
      streamSources: profileSources
    };

    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(username), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok || data.success === false) {
        throw new Error(data.detail || data.message || 'Save failed');
      }
      showToast('Settings saved', 'success');
      closeProfileSettings();
      state.selectedProfile = username;
      await loadMediaLibrary();
    } catch (e) {
      console.error('Error saving profile settings:', e);
      showToast(e.message || 'Save failed', 'error');
    } finally {
      if (save) {
        save.disabled = false;
        save.textContent = 'Save';
      }
    }
  }

  function openProfileDeleteConfirm() {
    if (!state.selectedProfile) return;
    var profile = state.profileSettings || profileByUsername(state.selectedProfile) || { username: state.selectedProfile };
    state.pendingProfileDelete = profile;
    var target = $('mediaProfileDeleteTarget');
    var confirm = $('mediaProfileDeleteConfirm');
    var modal = $('mediaProfileDeleteModal');
    if (target) target.textContent = profileLabel(profile) + ' / ' + profile.username;
    if (confirm) {
      confirm.disabled = false;
      confirm.textContent = 'Delete profile';
    }
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    }
  }

  function closeProfileDeleteConfirm() {
    var modal = $('mediaProfileDeleteModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-delete-open');
    }
    state.pendingProfileDelete = null;
  }

  async function confirmDeleteProfile() {
    var profile = state.pendingProfileDelete;
    if (!profile || !profile.username) return;
    var confirm = $('mediaProfileDeleteConfirm');
    if (confirm) {
      confirm.disabled = true;
      confirm.textContent = 'Deleting...';
    }

    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(profile.username), { method: 'DELETE' });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok || data.success === false) {
        throw new Error(data.detail || data.message || 'Delete failed');
      }
      closeProfileDeleteConfirm();
      closeProfileSettings();
      if (state.filterProfile === profile.username) state.filterProfile = '';
      state.selectedProfile = '';
      state.profileSettings = null;
      showToast('Profile deleted', 'success');
      await loadMediaLibrary();
    } catch (e) {
      console.error('Error deleting profile:', e);
      showToast(e.message || 'Delete failed', 'error');
      if (confirm) {
        confirm.disabled = false;
        confirm.textContent = 'Delete profile';
      }
    }
  }

  function syncFilterControls() {
    var buttons = document.querySelectorAll('.media-kind-btn');
    buttons.forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.kind === state.kind);
    });

    document.querySelectorAll('#mediaSortButtons [data-sort]').forEach(function(button) {
      button.classList.toggle('active', button.dataset.sort === state.sort);
    });

    document.querySelectorAll('#mediaDeviceFilters [data-device]').forEach(function(button) {
      var device = button.dataset.device || 'all';
      button.classList.toggle('active', device === (state.deviceFilter || 'all'));
      if (device === 'mac' || device === 'both') {
        button.disabled = !state.macHelperAvailable && !state.macScanInProgress;
      } else {
        button.disabled = false;
      }
    });
  }

  function updateMacToolbar() {
    var status = $('mediaMacSyncStatus');
    var rescan = $('mediaRescanMacBtn');
    var selectAll = $('mediaSelectAllBtn');
    var invertSelection = $('mediaInvertSelectionBtn');
    var download = $('mediaDownloadSelectedBtn');
    var deleteSelected = $('mediaDeleteSelectedBtn');
    var selectedCount = Object.keys(state.selectedItemIds).length;
    var selectableCount = state.items.filter(itemIsSelectable).length;
    var downloadableSelected = Object.keys(state.selectedItemIds).filter(function(id) {
      return itemIsDownloadable(itemById(id));
    }).length;
    var deletableSelected = Object.keys(state.selectedItemIds).filter(function(id) {
      return itemIsSelectable(itemById(id));
    }).length;
    if (status) {
      status.textContent = state.macScanInProgress ? 'Scanning Mac folder...' : 'Refresh Mac folder';
    }
    if (rescan) rescan.disabled = false;
    if (selectAll) selectAll.disabled = selectableCount === 0;
    if (invertSelection) invertSelection.disabled = selectableCount === 0;
    if (download) {
      download.textContent = 'Download selected (' + downloadableSelected + ')';
      download.disabled = !state.macHelperAvailable || downloadableSelected === 0;
    }
    if (deleteSelected) {
      deleteSelected.textContent = 'Delete selected (' + deletableSelected + ')';
      deleteSelected.disabled = deletableSelected === 0;
    }
    syncFilterControls();
  }

  function applySyncStatusesToItems() {
    rebuildVisibleItems();
  }

  async function scanMacAndRefresh(silent, forceLiveRefresh) {
    if (state.macScanInProgress) {
      if (!silent) showToast('Mac folder scan already running', 'info');
      return;
    }
    state.macScanInProgress = true;
    var status = $('mediaMacSyncStatus');
    var toolbar = $('mediaMacToolbar');
    if (toolbar) {
      toolbar.classList.add('scanning');
      toolbar.setAttribute('aria-busy', 'true');
    }
    if (status) status.textContent = 'Scanning local video folder...';
    try {
      var localRes = await fetch(MAC_HELPER_BASE + '/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        cache: 'no-store'
      });
      if (!localRes.ok) throw new Error('Local Mac helper did not accept the scan');
      var snapshot = await localRes.json();
      state.localSessionId = snapshot.localSessionId;
      state.macHelperAvailable = true;
      state.macFiles = Array.isArray(snapshot.files) ? snapshot.files : [];
      var localCount = state.macFiles.length;
      var compareRes = await fetch('/api/mac/sync-snapshot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          localSessionId: snapshot.localSessionId,
          files: state.macFiles
        }),
        cache: 'no-store'
      });
      if (!compareRes.ok) {
        var compareDetail = 'VPS could not compare the Mac folder';
        try {
          var compareBody = await compareRes.json();
          if (compareBody && compareBody.detail) compareDetail = String(compareBody.detail);
        } catch (_) {}
        throw new Error(compareDetail);
      }
      var compared = await compareRes.json();
      state.syncStatuses = compared.statuses || {};
      state.syncScannedAt = compared.scannedAt || Math.floor(Date.now() / 1000);
      // Update badges immediately so Refresh cannot look like a no-op.
      applySyncStatusesToItems();
      if (!silent) {
        showToast(
          'Mac scan: ' + localCount + ' local file(s) → ' +
          (compared.synced || 0) + ' on Mac, ' +
          (compared.notSynced || 0) + ' missing',
          'success'
        );
      }
    } catch (e) {
      state.macHelperAvailable = false;
      state.localSessionId = '';
      state.macFiles = [];
      state.syncStatuses = {};
      state.syncScannedAt = 0;
      if (state.deviceFilter === 'mac' || state.deviceFilter === 'both') {
        state.deviceFilter = state.deviceFilter === 'both' ? 'all' : 'vps';
      }
      applySyncStatusesToItems();
      if (!silent) showToast(e.message || 'Start the HXYLIVE Mac helper, then rescan', 'error');
    }
    try {
      updateMacToolbar();
      await loadMediaLibrary({ forceLiveRefresh: !!forceLiveRefresh });
    } finally {
      state.macScanInProgress = false;
      if (toolbar) {
        toolbar.classList.remove('scanning');
        toolbar.setAttribute('aria-busy', 'false');
      }
      if (status) status.textContent = 'Refresh Mac folder';
    }
  }

  function toggleDownloadSelection(itemId, checked) {
    var item = itemById(itemId);
    if (checked && !itemIsSelectable(item)) return;
    if (checked) state.selectedItemIds[itemId] = true;
    else delete state.selectedItemIds[itemId];
    if (item) refreshMediaCard(item);
    updateMacToolbar();
  }

  function selectAllVisibleVideos() {
    state.selectedItemIds = {};
    state.items.forEach(function(item) {
      if (itemIsSelectable(item)) state.selectedItemIds[item.id] = true;
    });
    renderGrid(state.items.length);
    updateMacToolbar();
  }

  function invertVisibleVideoSelection() {
    var inverted = {};
    state.items.forEach(function(item) {
      if (itemIsSelectable(item) && !state.selectedItemIds[item.id]) inverted[item.id] = true;
    });
    state.selectedItemIds = inverted;
    renderGrid(state.items.length);
    updateMacToolbar();
  }

  async function openLocally(item, options) {
    if (!item || !itemHasMac(item)) return;
    if (!state.localSessionId || !state.macHelperAvailable) {
      showToast('Start the HXYLIVE Mac helper to open local files', 'error');
      return;
    }
    var reveal = !!(options && options.reveal);
    try {
      var res = await fetch(MAC_HELPER_BASE + '/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          localSessionId: state.localSessionId,
          relativePath: item.macRelativePath || '',
          recordingId: item.recordingId || '',
          reveal: reveal
        }),
        cache: 'no-store'
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.error || (reveal ? 'Could not show folder' : 'Could not open local file'));
      var shown = data.relativePath || item.macRelativePath || item.filename;
      showToast((reveal ? 'Showed in Finder: ' : 'Opened on Mac: ') + shown, 'success');
    } catch (e) {
      showToast(e.message || (reveal ? 'Show folder failed' : 'Local open failed'), 'error');
    }
  }

  function activateMediaItem(item, options) {
    if (!item) return;
    var preferOnline = !!(options && options.online);
    if (preferOnline) {
      if (itemHasVps(item) && item.url) {
        openViewer(item);
        return;
      }
      showToast('Online playback needs a VPS copy of this video', 'error');
      return;
    }
    if (itemHasMac(item)) {
      openLocally(item);
      return;
    }
    if (itemHasVps(item)) openViewer(item);
  }

  async function setDeviceFilter(device) {
    var next = String(device || 'all').toLowerCase();
    if (next !== 'all' && next !== 'mac' && next !== 'vps' && next !== 'both') next = 'all';
    state.deviceFilter = next;
    state.mediaPage = 1;
    syncFilterControls();
    if ((next === 'mac' || next === 'all' || next === 'both') && (!state.macHelperAvailable || !state.syncScannedAt)) {
      if (!state.macScanInProgress) {
        await scanMacAndRefresh(true);
      }
      if (!state.macHelperAvailable && !state.macScanInProgress) {
        if (next === 'mac') {
          showToast('Start the HXYLIVE Mac helper to browse Mac videos', 'error');
          state.deviceFilter = 'vps';
        } else if (next === 'both') {
          showToast('Start the HXYLIVE Mac helper to browse Both videos', 'error');
          state.deviceFilter = 'all';
        } else {
          showToast('Mac helper unavailable — showing VPS library in All', 'info');
        }
      }
    }
    rebuildVisibleItems();
  }

  function itemStillWaitingForMac(id) {
    return (state.syncStatuses || {})[id] !== 'synced';
  }

  function recordingIdsForMediaItems(ids) {
    var found = [];
    (ids || []).forEach(function(id) {
      var item = itemById(id);
      var rid = String((item && item.recordingId) || '').trim();
      if (rid) found.push(rid);
    });
    return found;
  }

  function stopWatchingFiledDownloads() {
    fileWatchGeneration += 1;
    fileWatchTick = null;
    if (fileWatchTimer) {
      clearTimeout(fileWatchTimer);
      fileWatchTimer = null;
    }
  }

  function watchFiledDownloads(ids) {
    stopWatchingFiledDownloads();
    var generation = fileWatchGeneration;
    var startedAt = Date.now();
    var recordingIds = recordingIdsForMediaItems(ids);

    async function tick() {
      if (generation !== fileWatchGeneration) return;
      if (Date.now() - startedAt > FILE_WATCH_MAX_MS) {
        fileWatchTick = null;
        fileWatchTimer = null;
        return;
      }
      var filedNow = false;
      if (recordingIds.length && state.localSessionId) {
        var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        var abortTimer = setTimeout(function() {
          if (controller) controller.abort();
        }, 28000);
        try {
          var fetchOptions = {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              localSessionId: state.localSessionId,
              recordingIds: recordingIds,
              timeoutMs: 25000
            }),
            cache: 'no-store'
          };
          if (controller) fetchOptions.signal = controller.signal;
          var res = await fetch(MAC_HELPER_BASE + '/wait-filed', fetchOptions);
          var data = await res.json().catch(function() { return {}; });
          filedNow = !!(res.ok && data && Array.isArray(data.filed) && data.filed.length);
        } catch (_) {
          filedNow = false;
        } finally {
          clearTimeout(abortTimer);
        }
      }
      if (generation !== fileWatchGeneration) return;
      if (filedNow) {
        try { await scanMacAndRefresh(true); } catch (_) {}
        if (generation !== fileWatchGeneration) return;
        if (!ids.filter(itemStillWaitingForMac).length) {
          fileWatchTick = null;
          fileWatchTimer = null;
          showToast(ids.length + ' video(s) filed on Mac', 'success');
          return;
        }
      }
      fileWatchTimer = setTimeout(tick, filedNow ? 200 : 800);
    }

    fileWatchTick = tick;
    fileWatchTimer = setTimeout(tick, 200);
  }

  async function downloadSelectedToMac() {
    var ids = Object.keys(state.selectedItemIds).filter(function(id) {
      return itemIsDownloadable(itemById(id));
    });
    if (!ids.length || !state.localSessionId) return;
    var button = $('mediaDownloadSelectedBtn');
    if (button) button.disabled = true;
    try {
      var createRes = await fetch('/api/mac/download-jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ localSessionId: state.localSessionId, itemIds: ids })
      });
      if (!createRes.ok) throw new Error('Could not create the VPS download batch');
      var job = await createRes.json();
      var dispatchRes = await fetch(MAC_HELPER_BASE + '/dispatch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          localSessionId: state.localSessionId,
          jobId: job.jobId,
          vpsBase: window.location.origin
        })
      });
      if (!dispatchRes.ok) {
        var dispatchErr = 'Mac helper could not start the Chrome download batch';
        try {
          var dispatchBody = await dispatchRes.json();
          if (dispatchBody && dispatchBody.error) dispatchErr = dispatchBody.error;
        } catch (_) {}
        throw new Error(dispatchErr);
      }
      showToast(ids.length + ' download(s) started in Chrome; helper will file them', 'success');
      state.selectedItemIds = {};
      watchFiledDownloads(ids);
    } catch (e) {
      showToast(e.message || 'Download batch failed', 'error');
    }
    updateMacToolbar();
    renderGrid(state.items.length);
  }

  function setKind(kind) {
    state.kind = kind || 'all';
    state.mediaPage = 1;
    loadMediaLibrary();
  }

  function selectProfile(profile, shouldScroll) {
    var next = profile || '';
    // Clicking the active profile again clears the video filter (back to all streamers).
    if (next && state.selectedProfile === next) next = '';
    state.selectedProfile = next;
    state.filterProfile = next;
    state.mediaPage = 1;
    if (next) jumpProfilePageToUsername(next);
    else state.profilePage = 1;
    syncProfileSelectionUI();
    renderProfileCarousel();
    if (shouldScroll) {
      var results = document.querySelector('.media-recent-section');
      if (results && results.scrollIntoView) {
        results.scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
    }
    loadMediaLibrary();
  }

  function setFilterProfile(profile) {
    state.filterProfile = profile || '';
    state.selectedProfile = profile || '';
    loadMediaLibrary();
  }

  function bindEvents() {
    var storageRefresh = $('mediaStorageRefreshBtn');
    if (storageRefresh) {
      storageRefresh.addEventListener('click', function() {
        if (state.loading) return;
        loadMediaLibrary();
      });
    }

    var profileRefresh = $('mediaProfileRefreshBtn');
    if (profileRefresh) {
      profileRefresh.addEventListener('click', function() {
        if (state.profileRefreshing) return;
        refreshProfiles();
      });
    }

    var videoSearch = $('mediaVideoSearchInput');
    if (videoSearch) {
      videoSearch.addEventListener('input', function() {
        clearTimeout(videoSearchTimer);
        videoSearchTimer = setTimeout(function() {
          state.videoSearch = videoSearch.value.trim();
          state.mediaPage = 1;
          loadMediaLibrary();
        }, 180);
      });
    }

    var search = $('mediaSearchInput');
    if (search) {
      search.addEventListener('input', function() {
        clearTimeout(profileSearchTimer);
        profileSearchTimer = setTimeout(function() {
          state.profileSearch = search.value.trim();
          state.profilePage = 1;
          renderProfileCarousel();
        }, 180);
      });
    }

    var sourceFilters = $('mediaProfileSourceFilters');
    if (sourceFilters) {
      sourceFilters.addEventListener('click', function(event) {
        var globalStatus = event.target.closest('[data-global-live-status]');
        if (globalStatus) {
          setGlobalLiveStatusFilter(globalStatus.dataset.globalLiveStatus || 'all');
          return;
        }
        var button = event.target.closest('[data-source]');
        if (!button) return;
        setProfileSourceFilter(button.dataset.source || 'all');
      });
    }

    var statusFilters = $('mediaProfileStatusFilters');
    if (statusFilters) {
      statusFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-live-status]');
        if (!button) return;
        setProfileLiveStatusFilter(button.dataset.liveStatus || 'all');
      });
    }

    var profilePageNumbers = $('mediaProfilePageNumbers');
    if (profilePageNumbers) {
      profilePageNumbers.addEventListener('click', function(event) {
        var button = event.target.closest('[data-profile-page]');
        if (button) setProfilePage(button.dataset.profilePage);
      });
    }

    var profilePageResizeTimer = null;
    window.addEventListener('resize', function() {
      clearTimeout(profilePageResizeTimer);
      profilePageResizeTimer = setTimeout(function() {
        if (!state.profiles.length) return;
        renderProfileCarousel();
      }, 160);
    });

    var sortButtons = $('mediaSortButtons');
    if (sortButtons) {
      sortButtons.addEventListener('click', function(event) {
        var button = event.target.closest('[data-sort]');
        if (!button) return;
        state.sort = button.dataset.sort || 'newest';
        state.mediaPage = 1;
        loadMediaLibrary();
      });
    }

    var deviceFilters = $('mediaDeviceFilters');
    if (deviceFilters) {
      deviceFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-device]');
        if (!button || button.disabled) return;
        setDeviceFilter(button.dataset.device || 'all');
      });
    }
    var macToolbar = $('mediaMacToolbar');
    if (macToolbar) {
      macToolbar.addEventListener('click', function() { scanMacAndRefresh(false); });
      macToolbar.addEventListener('keydown', function(event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          scanMacAndRefresh(false);
        }
      });
    }
    var downloadSelected = $('mediaDownloadSelectedBtn');
    if (downloadSelected) downloadSelected.addEventListener('click', downloadSelectedToMac);
    document.addEventListener('visibilitychange', function() {
      if (document.hidden || !fileWatchTick) return;
      if (fileWatchTimer) clearTimeout(fileWatchTimer);
      fileWatchTimer = setTimeout(fileWatchTick, 400);
    });
    var deleteSelected = $('mediaDeleteSelectedBtn');
    if (deleteSelected) deleteSelected.addEventListener('click', openBatchDeleteConfirm);
    var selectAll = $('mediaSelectAllBtn');
    if (selectAll) selectAll.addEventListener('click', selectAllVisibleVideos);
    var invertSelection = $('mediaInvertSelectionBtn');
    if (invertSelection) invertSelection.addEventListener('click', invertVisibleVideoSelection);

    var mediaPageNumbers = $('mediaPageNumbers');
    if (mediaPageNumbers) {
      mediaPageNumbers.addEventListener('click', function(event) {
        var button = event.target.closest('[data-page]');
        if (button) setMediaPage(button.dataset.page);
      });
    }

    document.querySelectorAll('.media-kind-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        setKind(btn.dataset.kind || 'all');
      });
    });

    var rail = $('mediaProfileRail');
    if (rail) {
      rail.addEventListener('click', function(ev) {
        var directLink = ev.target.closest('[data-profile-action="channel"], [data-profile-action="watch"]');
        if (directLink) {
          ev.stopPropagation();
          return;
        }
        var follow = ev.target.closest('[data-profile-action="follow"]');
        if (follow) {
          ev.preventDefault();
          ev.stopPropagation();
          toggleProfileFollow(follow.dataset.profile || '', follow.dataset.source || '', follow);
          return;
        }
        var record = ev.target.closest('[data-profile-action="record"]');
        if (record) {
          ev.preventDefault();
          ev.stopPropagation();
          toggleProfileRecording(record.dataset.profile || '', record);
          return;
        }
        var settings = ev.target.closest('[data-profile-action="settings"]');
        if (settings) {
          ev.preventDefault();
          ev.stopPropagation();
          openProfileSettings(settings.dataset.profile || '');
          return;
        }
        var card = ev.target.closest('.media-profile-card');
        if (!card) return;
        selectProfile(card.dataset.profile || '', true);
      });
      rail.addEventListener('keydown', function(ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        if (ev.target.closest('[data-profile-action="channel"], [data-profile-action="watch"], [data-profile-action="settings"], [data-profile-action="follow"], [data-profile-action="record"]')) return;
        var card = ev.target.closest('.media-profile-card');
        if (!card) return;
        ev.preventDefault();
        selectProfile(card.dataset.profile || '', true);
      });
    }

    var grid = $('mediaGrid');
    if (grid) {
      grid.addEventListener('click', function(ev) {
        var action = ev.target.closest('[data-media-action]');
        if (action) {
          if (action.dataset.mediaAction === 'profile') {
            ev.preventDefault();
            ev.stopPropagation();
            selectProfile(action.dataset.profile || '', true);
            return;
          }
          if (action.dataset.mediaAction === 'reveal-local') {
            ev.preventDefault();
            ev.stopPropagation();
            openLocally(itemById(action.dataset.mediaId), { reveal: true });
            return;
          }
          if (action.dataset.mediaAction === 'select-card' || action.dataset.mediaAction === 'select') {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.metaKey || ev.ctrlKey) {
              activateMediaItem(itemById(action.dataset.mediaId), { online: true });
              return;
            }
            toggleDownloadSelection(action.dataset.mediaId, !state.selectedItemIds[action.dataset.mediaId]);
            return;
          }
          return;
        }
        var card = ev.target.closest('.media-card');
        if (card) {
          activateMediaItem(itemById(card.dataset.mediaId), {
            online: !!(ev.metaKey || ev.ctrlKey)
          });
        }
      });
      grid.addEventListener('keydown', function(ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        var profileLink = ev.target.closest('[data-media-action="profile"]');
        if (profileLink) {
          ev.preventDefault();
          ev.stopPropagation();
          selectProfile(profileLink.dataset.profile || '', true);
          return;
        }
        var revealLocal = ev.target.closest('[data-media-action="reveal-local"]');
        if (revealLocal) {
          ev.preventDefault();
          openLocally(itemById(revealLocal.dataset.mediaId), { reveal: true });
          return;
        }
        var card = ev.target.closest('.media-card');
        if (!card) return;
        ev.preventDefault();
        activateMediaItem(itemById(card.dataset.mediaId));
      });
    }

    var close = $('mediaViewerClose');
    if (close) {
      close.addEventListener('click', closeViewer);
      close.addEventListener('keydown', function(event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          closeViewer();
        }
      });
    }

    var viewerPrev = $('mediaViewerPrev');
    if (viewerPrev) {
      viewerPrev.addEventListener('click', function() {
        var previousItem = previousVideoItem(state.currentViewerItem);
        if (previousItem) openViewer(previousItem);
      });
    }

    var viewerNext = $('mediaViewerNext');
    if (viewerNext) {
      viewerNext.addEventListener('click', function() {
        var nextItem = nextVideoItem(state.currentViewerItem);
        if (nextItem) openViewer(nextItem);
      });
    }

    var deleteCancel = $('mediaDeleteCancel');
    if (deleteCancel) deleteCancel.addEventListener('click', closeDeleteConfirm);

    var deleteConfirm = $('mediaDeleteConfirm');
    if (deleteConfirm) deleteConfirm.addEventListener('click', function() {
      confirmDeleteMedia();
    });
    var deleteSelectAllVps = $('mediaDeleteSelectAllVps');
    if (deleteSelectAllVps) deleteSelectAllVps.addEventListener('click', function() {
      setBothDeleteSide('vps', true);
    });
    var deleteSelectAllMac = $('mediaDeleteSelectAllMac');
    if (deleteSelectAllMac) deleteSelectAllMac.addEventListener('click', function() {
      setBothDeleteSide('mac', true);
    });
    var deleteClearAllMac = $('mediaDeleteClearAllMac');
    if (deleteClearAllMac) deleteClearAllMac.addEventListener('click', function() {
      setBothDeleteSide('mac', false);
    });
    var deleteBothBody = $('mediaDeleteBothBody');
    if (deleteBothBody) {
      deleteBothBody.addEventListener('change', function(ev) {
        var input = ev.target && ev.target.closest ? ev.target.closest('input[type="checkbox"]') : null;
        if (!input) return;
        state.pendingBothChoices = readBothDeleteChoicesFromDom();
        syncBatchDeleteConfirmState();
      });
    }

    var profileSettingsForm = $('mediaProfileSettingsForm');
    if (profileSettingsForm) profileSettingsForm.addEventListener('submit', saveProfileSettings);

    var profileResolveImage = $('profileResolveImageBtn');
    if (profileResolveImage) profileResolveImage.addEventListener('click', resolveProfileImage);

    var profileSettingsClose = $('mediaProfileSettingsClose');
    if (profileSettingsClose) profileSettingsClose.addEventListener('click', closeProfileSettings);

    var profileSettingsCancel = $('mediaProfileSettingsCancel');
    if (profileSettingsCancel) profileSettingsCancel.addEventListener('click', closeProfileSettings);

    var profileDelete = $('mediaProfileDeleteBtn');
    if (profileDelete) profileDelete.addEventListener('click', openProfileDeleteConfirm);

    var profileAddSource = $('profileAddSourceBtn');
    if (profileAddSource) profileAddSource.addEventListener('click', function() {
      addProfileSource();
    });

    var profileSourcesList = $('profileSourcesList');
    if (profileSourcesList) {
      profileSourcesList.addEventListener('click', function(ev) {
        var remove = ev.target.closest('[data-source-action="remove"]');
        if (!remove) return;
        var rows = Array.prototype.slice.call(document.querySelectorAll('.media-profile-source-row'));
        if (rows.length <= 1) {
          rows[0].querySelectorAll('input').forEach(function(input) {
            if (input.type === 'checkbox') input.checked = false;
            else input.value = '';
          });
          return;
        }
        var row = remove.closest('.media-profile-source-row');
        if (row) row.remove();
        syncLegacyStreamFields(readProfileSources());
      });
      profileSourcesList.addEventListener('change', function() {
        syncLegacyStreamFields(readProfileSources());
      });
      profileSourcesList.addEventListener('input', function() {
        syncLegacyStreamFields(readProfileSources());
      });
    }

    var profileDeleteCancel = $('mediaProfileDeleteCancel');
    if (profileDeleteCancel) profileDeleteCancel.addEventListener('click', closeProfileDeleteConfirm);

    var profileDeleteConfirm = $('mediaProfileDeleteConfirm');
    if (profileDeleteConfirm) profileDeleteConfirm.addEventListener('click', confirmDeleteProfile);

    var viewer = $('mediaViewer');
    if (viewer) {
      viewer.addEventListener('click', function(ev) {
        if (ev.target === viewer) closeViewer();
      });
    }

    var deleteModal = $('mediaDeleteModal');
    if (deleteModal) {
      deleteModal.addEventListener('click', function(ev) {
        if (ev.target === deleteModal) closeDeleteConfirm();
      });
    }

    var profileSettingsModal = $('mediaProfileSettingsModal');
    if (profileSettingsModal) {
      profileSettingsModal.addEventListener('click', function(ev) {
        if (ev.target === profileSettingsModal) closeProfileSettings();
      });
    }

    var profileDeleteModal = $('mediaProfileDeleteModal');
    if (profileDeleteModal) {
      profileDeleteModal.addEventListener('click', function(ev) {
        if (ev.target === profileDeleteModal) closeProfileDeleteConfirm();
      });
    }

    document.addEventListener('keydown', function(ev) {
      if (ev.key === 'Escape') {
        if (state.pendingProfileDelete) {
          closeProfileDeleteConfirm();
        } else if (state.pendingDelete) {
          closeDeleteConfirm();
        } else if ($('mediaProfileSettingsModal') && $('mediaProfileSettingsModal').style.display !== 'none') {
          closeProfileSettings();
        } else {
          closeViewer();
        }
        return;
      }
      if ((ev.key === 'Delete' || ev.key === 'Backspace') && !state.pendingDelete && !state.pendingProfileDelete) {
        if (ev.target && ev.target.closest && ev.target.closest('input, textarea, select, [contenteditable="true"]')) {
          return;
        }
        if (!Object.keys(state.selectedItemIds).some(function(id) { return itemIsSelectable(itemById(id)); })) {
          return;
        }
        ev.preventDefault();
        openBatchDeleteConfirm();
      }
    });

    window.addEventListener('beforeunload', flushMediaProfileVolume);
  }

  document.addEventListener('DOMContentLoaded', function() {
    bindEvents();
    renderProfileSourceFilters();
    renderProfileStatusFilters();
    syncFilterControls();
    scanMacAndRefresh(true, true);
  });
})();

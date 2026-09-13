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
    recordingEnabled: true,
    librarySpaceBlocked: false,
    stagingBlocked: false,
    defaultMonthlyQuotaGb: 100,
    kind: 'video',
    selectedProfile: '',
    filterProfile: '',
    filterSources: [],
    filterLiveStatuses: [],
    filterRecordings: [],
    filterSource: 'all',
    filterLiveStatus: 'all',
    filterRecording: 'all',
    profileSearch: '',
    dateFrom: { y: '', m: '', d: '' },
    dateTo: { y: '', m: '', d: '' },
    dateBounds: { min: null, max: null },
    sortField: 'name',
    sortDir: 'desc',
    loading: false,
    profileRefreshing: false,
    pendingDelete: null,
    pendingProfileDelete: null,
    processingFiles: [],
    processingSelected: {},
    processingLoading: false,
    processingDeleting: false,
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
    deviceFilters: ['vps', 'mac'],
    selectedItemIds: {},
    selectedStreamerIds: {},
    selectedProjectIds: {},
    videoViewMode: 'projects',
    projects: [],
    projectDetailId: null,
    filterProjectStatus: 'all',
    filterCompletedSync: 'all',
    filterConcatStatus: 'all',
    macScanInProgress: false,
    mediaPage: 1,
    profilePage: 1,
    localProfileLiveMeta: {},
    localProfileAvatarInflight: {},
    timelineDays: 7,
    timelineLoading: false,
    timelineData: null,
    timelineLoadTimer: null,
    tasksOpen: false,
    tasksTab: 'download',
    tasksLoading: false,
    tasksError: '',
    tasksDownload: { projects: [] },
    tasksConcat: { current: null, queue: [] },
    tasksConvert: { items: [] },
    tasksSelected: {},
    tasksExpanded: {},
    tasksPollTimer: null,
    tasksDrag: null
  };
  var TASKS_POLL_MS = 1500;
  var GAP_BUFFER_MINUTES_OPTIONS = [15, 30, 60, 90];
  var MEDIA_PAGE_SIZE = 12;
  var PROFILE_PAGE_ROWS = 3;
  var toolbarCompact = false;
  var MAC_HELPER_BASE = 'http://127.0.0.1:17899';
  var fileWatchTimer = null;
  var fileWatchTick = null;
  var fileWatchGeneration = 0;
  var FILE_WATCH_MAX_MS = 6 * 60 * 60 * 1000;

  var PROFILE_SOURCE_OPTIONS = [
    { value: 'twitch', label: 'Twitch', domains: ['twitch.tv', 'www.twitch.tv'] },
    { value: 'youtube', label: 'YouTube', domains: ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'music.youtube.com'] },
    { value: 'bilibili', label: 'Bilibili', domains: ['live.bilibili.com', 'bilibili.com', 'www.bilibili.com'] },
    { value: 'chaturbate', label: 'Chaturbate', domains: ['chaturbate.com'] },
    { value: 'stripchat', label: 'Stripchat', domains: ['stripchat.com', 'www.stripchat.com'] }
  ];
  var KNOWN_SOURCE_MARKERS = PROFILE_SOURCE_OPTIONS.map(function(option) {
    return option.value;
  });

  function normalizeSourceMarker(value) {
    var source = String(value || '').trim().toLowerCase();
    if (!source) return '';
    if (source === 'yt' || source === 'youtube.com') source = 'youtube';
    if (source === 'cb') source = 'chaturbate';
    if (source === 'sc') source = 'stripchat';
    return KNOWN_SOURCE_MARKERS.indexOf(source) !== -1 ? source : '';
  }

  /** Parse ``username(source)`` from a Mac folder or filename stem. */
  function parseIdentityWithSource(value) {
    var raw = String(value || '').trim().replace(/\\/g, '/');
    if (raw.indexOf('/') !== -1) {
      var parts = raw.split('/').filter(Boolean);
      raw = parts.length ? parts[parts.length - 1] : '';
    }
    if (!raw) return { username: '', sourceType: '' };
    if (raw.indexOf('.') !== -1 && raw.charAt(raw.length - 1) !== ')') {
      var splitAt = raw.lastIndexOf('.');
      var ext = raw.slice(splitAt + 1);
      if (ext && ext.length <= 5 && /^[A-Za-z0-9]+$/.test(ext)) {
        raw = raw.slice(0, splitAt);
      }
    }
    var match = raw.match(/^(.+)\(([A-Za-z0-9_-]+)\)(?:$|(?=[_\s.-]))/i);
    if (!match) return { username: raw, sourceType: '' };
    var sourceType = normalizeSourceMarker(match[2]);
    var username = String(match[1] || '').trim();
    if (!username || !sourceType) return { username: raw, sourceType: '' };
    return { username: username, sourceType: sourceType };
  }

  function channelUrlForSource(sourceType, username) {
    var source = normalizeSourceMarker(sourceType);
    var user = String(username || '').trim().replace(/^@+/, '');
    if (!source || !user) return '';
    var encoded = encodeURIComponent(user);
    if (source === 'chaturbate') return 'https://chaturbate.com/' + encoded + '/';
    if (source === 'stripchat') return 'https://stripchat.com/' + encoded;
    if (source === 'twitch') return 'https://www.twitch.tv/' + encoded;
    if (source === 'bilibili') return 'https://live.bilibili.com/' + encoded;
    if (source === 'youtube') {
      if (user.indexOf('UC') === 0 && user.length >= 24) {
        return 'https://www.youtube.com/channel/' + encoded;
      }
      return 'https://www.youtube.com/@' + encoded;
    }
    return '';
  }
  var PROFILE_LIVE_STATUS_OPTIONS = [
    { value: 'live', label: 'Live' },
    { value: 'private', label: 'Private' },
    { value: 'locked', label: 'Locked' },
    { value: 'offline', label: 'Offline' }
  ];
  var PROFILE_RECORDING_OPTIONS = [
    { value: 'on', label: 'Recording' },
    { value: 'off', label: 'Not recording' }
  ];

  var profileSearchTimer = null;
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

  /** Absolute last-live label for Media profile cards (local TZ, no year/seconds). */
  function formatLastLive(timestamp) {
    if (!timestamp) return '';
    try {
      var value = new Date(Number(timestamp) * 1000);
      if (isNaN(value.getTime())) return '';
      var absolute =
        pad2(value.getMonth() + 1) + '-' +
        pad2(value.getDate()) + ' ' +
        pad2(value.getHours()) + ':' +
        pad2(value.getMinutes());
      return 'Last live · ' + absolute;
    } catch (e) {
      return '';
    }
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

  function formatFps(fps) {
    var value = Number(fps);
    if (!(value > 0)) return '';
    if (Math.abs(value - Math.round(value)) < 0.05) {
      return String(Math.round(value)) + ' fps';
    }
    return (Math.round(value * 100) / 100).toFixed(2).replace(/\.?0+$/, '') + ' fps';
  }

  function formatBitrate(bitrate) {
    var value = Number(bitrate);
    if (!(value > 0)) return '';
    if (value >= 1000000) {
      var mbps = value / 1000000;
      var rounded = mbps >= 10 ? Math.round(mbps) : Math.round(mbps * 10) / 10;
      return String(rounded).replace(/\.0$/, '') + ' Mbps';
    }
    if (value >= 1000) {
      return String(Math.round(value / 1000)) + ' kbps';
    }
    return String(Math.round(value)) + ' bps';
  }

  function estimateBitrateBps(size, durationSeconds) {
    var bytes = numberOrZero(size);
    var duration = numberOrZero(durationSeconds);
    if (!(bytes > 0) || !(duration > 0)) return 0;
    return Math.max(1, Math.round(bytes * 8 / duration));
  }

  function formatVideoStreamMeta(item) {
    var parts = [];
    var qualityText = item && item.type === 'video' ? formatQuality(item.resolution) : '';
    if (qualityText && qualityText !== '-') parts.push(qualityText);
    var fpsText = formatFps(item && item.fps);
    if (fpsText) parts.push(fpsText);
    var bitrate = numberOrZero(item && item.bitrate);
    if (!(bitrate > 0)) {
      bitrate = estimateBitrateBps(item && item.size, item && item.duration);
    }
    var bitrateText = formatBitrate(bitrate);
    if (bitrateText) parts.push(bitrateText);
    return parts.join(' · ');
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
    var folderIdentity = parts.length > 1
      ? parseIdentityWithSource(parts[0])
      : { username: '', sourceType: '' };
    var fileIdentity = parseIdentityWithSource(basename);
    var username = folderIdentity.username || fileIdentity.username || '';
    var sourceType = folderIdentity.sourceType || fileIdentity.sourceType || '';
    var title = standardDateTimeFromText(basename) || basename.replace(/\.[^.]+$/, '') || 'Mac video';
    var createdAt = 0;
    var parsed = Date.parse(String(title).replace(' ', 'T'));
    if (!isNaN(parsed)) createdAt = Math.floor(parsed / 1000);
    return {
      relative: relative,
      basename: basename,
      username: username,
      sourceType: sourceType,
      title: title,
      createdAt: createdAt
    };
  }

  /** Video counts by streamer from VPS items + unmatched Mac-only files. */
  function resolveLocalIdentityToProfileUsername(username, sourceType) {
    var raw = String(username || '').trim();
    if (!raw) return '';
    if (profileByUsername(raw)) return raw;
    var source = normalizeSourceMarker(sourceType);
    var needle = raw.toLowerCase();
    var profiles = state.profiles || [];
    var match = null;
    for (var i = 0; i < profiles.length; i++) {
      var profile = profiles[i];
      if (!profile || profile.fromMacLocal) continue;
      var display = String(profile.displayName || profile.display_name || '').trim().toLowerCase();
      if (!display || display !== needle) continue;
      var types = profileSourceTypes(profile);
      if (source && types.length && types.indexOf(source) === -1) continue;
      // Bilibili Mac folders use the display name; the Media card key is the room id.
      if (source === 'bilibili' || types.indexOf('bilibili') !== -1) {
        if (/^\d+$/.test(String(profile.username || ''))) return profile.username;
      }
      if (!match) match = profile.username;
    }
    return match || raw;
  }

  function localVideoStatsByUsername() {
    var stats = {};
    function bump(username, size, createdAt, sourceType) {
      var key = resolveLocalIdentityToProfileUsername(username, sourceType);
      if (!key) return;
      if (!stats[key]) {
        stats[key] = { videos: 0, totalSize: 0, latestVideoAt: 0, sourceType: '' };
      }
      stats[key].videos += 1;
      stats[key].totalSize += intSize(size);
      var at = intSize(createdAt);
      if (at >= stats[key].latestVideoAt) stats[key].latestVideoAt = at;
      var source = normalizeSourceMarker(sourceType);
      if (source && !stats[key].sourceType) stats[key].sourceType = source;
    }

    var macFiles = state.macFiles || [];
    var used = {};
    (state.vpsItems || []).forEach(function(item) {
      if (!item) return;
      if (item.type && item.type !== 'video') return;
      var identity = parseIdentityWithSource(item.username);
      bump(
        identity.username || item.username,
        item.size,
        item.createdAt,
        item.sourceType || item.source_type || identity.sourceType
      );
      findMacFileForVpsItem(item, macFiles, used);
    });
    macFiles.forEach(function(file, index) {
      if (used[index]) return;
      var meta = parseMacFileMeta(file);
      bump(meta.username, file.size, meta.createdAt, meta.sourceType);
    });
    return stats;
  }

  function makeLocalVideoProfile(username, info) {
    var videos = Number((info && info.videos) || 0);
    var latest = Number((info && info.latestVideoAt) || 0) || null;
    var sourceType = normalizeSourceMarker(info && info.sourceType) || '';
    var channelUrl = channelUrlForSource(sourceType, username);
    var streamSources = sourceType
      ? [{
          sourceType: sourceType,
          source_type: sourceType,
          channelUsername: username,
          channel_username: username,
          channelUrl: channelUrl,
          channel_url: channelUrl,
          autoRecord: false
        }]
      : [];
    var profile = {
      username: username,
      displayName: username,
      videos: videos,
      total: videos,
      images: 0,
      audio: 0,
      totalSize: Number((info && info.totalSize) || 0),
      latestVideoAt: latest,
      latestAt: latest,
      lastLiveAt: latest,
      last_live_at: latest,
      isFollowing: false,
      autoRecord: false,
      empty: videos === 0,
      folderExists: false,
      sourceType: sourceType,
      source_type: sourceType,
      channelUsername: username,
      channelUrl: channelUrl,
      isOnline: false,
      viewers: 0,
      followers: null,
      streamSources: streamSources,
      stream_sources: streamSources,
      fromMacLocal: true
    };
    applyLocalVideoLiveMeta(profile, state.localProfileLiveMeta[username]);
    return profile;
  }

  function profileSortRank(profile) {
    if (profileLiveBucket(profile) === 'live') return 0;
    if (profile && profile.autoRecord) return 1;
    return 2;
  }

  function profileSortName(profile) {
    return String((profile && (profile.displayName || profile.username)) || '').toLowerCase();
  }

  function sortMediaProfilesInPlace(profiles) {
    (profiles || []).sort(function(a, b) {
      var rankDiff = profileSortRank(a) - profileSortRank(b);
      if (rankDiff) return rankDiff;
      var aName = profileSortName(a);
      var bName = profileSortName(b);
      if (aName !== bName) return aName < bName ? -1 : 1;
      var aUser = String((a && a.username) || '').toLowerCase();
      var bUser = String((b && b.username) || '').toLowerCase();
      return aUser < bUser ? -1 : (aUser > bUser ? 1 : 0);
    });
  }

  /**
   * Unfollowed (white-ID) streamer cards for anyone who has local/Mac videos
   * but is not yet in the Media profile list from follows / VPS folders.
   */
  function ensureProfilesForLocalVideos() {
    var stats = localVideoStatsByUsername();
    var changed = false;
    var kept = [];
    (state.profiles || []).forEach(function(profile) {
      if (!profile || !profile.username) return;
      if (profile.fromMacLocal && !stats[profile.username]) {
        changed = true;
        return;
      }
      kept.push(profile);
    });
    if (kept.length !== (state.profiles || []).length) state.profiles = kept;

    Object.keys(stats).forEach(function(username) {
      var info = stats[username];
      var existing = profileByUsername(username);
      if (!existing) {
        state.profiles.push(makeLocalVideoProfile(username, info));
        changed = true;
        return;
      }
      if (Number(existing.videos || 0) !== info.videos) {
        existing.videos = info.videos;
        existing.total = Math.max(Number(existing.total || 0), info.videos);
        existing.empty = info.videos === 0;
        changed = true;
      }
      if (info.sourceType && !normalizeSourceMarker(existing.sourceType || existing.source_type)) {
        existing.sourceType = info.sourceType;
        existing.source_type = info.sourceType;
        existing.channelUrl = existing.channelUrl || channelUrlForSource(info.sourceType, username);
        if (!(existing.streamSources || existing.stream_sources || []).length) {
          existing.streamSources = [{
            sourceType: info.sourceType,
            source_type: info.sourceType,
            channelUsername: username,
            channel_username: username,
            channelUrl: existing.channelUrl,
            channel_url: existing.channelUrl,
            autoRecord: false
          }];
          existing.stream_sources = existing.streamSources;
        }
        changed = true;
      }
      if (info.latestVideoAt) {
        var prevLatest = Number(existing.latestVideoAt || existing.latestAt || 0);
        if (info.latestVideoAt > prevLatest) {
          existing.latestVideoAt = info.latestVideoAt;
          if (!existing.latestAt || info.latestVideoAt > Number(existing.latestAt || 0)) {
            existing.latestAt = info.latestVideoAt;
          }
          if (!existing.lastLiveAt && !existing.last_live_at) {
            existing.lastLiveAt = info.latestVideoAt;
            existing.last_live_at = info.latestVideoAt;
          }
          changed = true;
        }
      }
    });

    if (hydrateLocalVideoProfileAvatars()) changed = true;
    if (changed) sortMediaProfilesInPlace(state.profiles);
    persistLocalOnlyProfileCards();
    return changed;
  }

  var ensuredLocalCards = {};

  /** Fire-and-forget ensure-card so Mac/local phantom profiles persist in DB. */
  function persistLocalOnlyProfileCards() {
    (state.profiles || []).forEach(function(profile) {
      if (!profile || !profile.fromMacLocal || !profile.username) return;
      var sourceType = normalizeSourceMarker(profile.sourceType || profile.source_type);
      if (!sourceType) return;
      var key = String(profile.username).toLowerCase() + '|' + sourceType;
      if (ensuredLocalCards[key]) return;
      ensuredLocalCards[key] = true;
      var channelUrl = profile.channelUrl || profile.channel_url ||
        channelUrlForSource(sourceType, profile.username);
      fetch('/api/media-profiles/ensure-card', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: profile.username,
          sourceType: sourceType,
          displayName: profile.displayName || profile.display_name || profile.username,
          profileImageUrl: profile.profileImageUrl || profile.profile_image_url || '',
          channelUrl: channelUrl,
          autoRecord: false
        })
      }).catch(function() {});
    });
  }

  function macThumbUrl(file, relative) {
    if (!state.localSessionId || !state.macHelperAvailable) return '';
    var params = new URLSearchParams();
    params.set('localSessionId', state.localSessionId);
    if (relative) params.set('relativePath', relative);
    var rid = String((file && file.recordingId) || '').trim();
    if (rid) params.set('recordingId', rid);
    if (state.macHelperDirect) return MAC_HELPER_BASE + '/thumb?' + params.toString();
    return '/api/mac/helper/thumb?' + params.toString();
  }

  function locationLabel(locations) {
    if (locations === 'mac') return 'MAC';
    if (locations === 'vps') return 'VPS';
    return '';
  }

  /** Standard video name: 2026-08-02 21:21:42 · P2 */
  function partLabelFromMediaText(text) {
    var match = String(text || '').match(/_part(\d{3,})/i);
    if (!match) return '';
    return 'P' + String(parseInt(match[1], 10));
  }

  function displayMediaTitle(item) {
    if (!item) return '';
    var fromCreated = formatDateTimeSeconds(item.createdAt);
    var fromText = standardDateTimeFromText(item.title || item.filename || item.macRelativePath || '');
    var base = fromCreated || fromText || String(item.title || item.filename || '').trim();
    var partLabel = partLabelFromMediaText(
      [item.filename, item.macRelativePath, item.title].filter(Boolean).join('\n')
    );
    if (base && partLabel) return base + ' · ' + partLabel;
    return base || partLabel;
  }

  /** Standard id + datetime: Nancy-A1 2026-08-02 21:21:42 */
  function displayMediaIdTitle(item) {
    var when = displayMediaTitle(item);
    var username = String((item && item.username) || '').trim();
    var profile = username ? profileByUsername(username) : null;
    var label = profile ? profileLabel(profile) : '';
    if (label && when) return label + ' ' + when;
    return when || label || '';
  }

  function itemDayParts(item) {
    var stamp = intSize(item && item.createdAt);
    if (stamp > 0) {
      try {
        var value = new Date(stamp * 1000);
        if (!isNaN(value.getTime())) {
          return { y: value.getFullYear(), m: value.getMonth() + 1, d: value.getDate() };
        }
      } catch (e) {}
    }
    var text = displayMediaTitle(item) || String((item && (item.title || item.filename)) || '');
    var match = String(text).match(/(20\d{2}|19\d{2})[^\d]?(\d{1,2})[^\d]?(\d{1,2})/);
    if (!match) return null;
    var y = Number(match[1]);
    var m = Number(match[2]);
    var d = Number(match[3]);
    if (!y || m < 1 || m > 12 || d < 1 || d > 31) return null;
    return { y: y, m: m, d: d };
  }

  function dayKey(parts) {
    if (!parts) return 0;
    return (Number(parts.y) * 10000) + (Number(parts.m) * 100) + Number(parts.d);
  }

  function emptyDateOverride() {
    return { y: '', m: '', d: '' };
  }

  function copyDateParts(parts) {
    if (!parts) return null;
    return { y: Number(parts.y), m: Number(parts.m), d: Number(parts.d) };
  }

  function ymdFromInputs(override, fallback) {
    var y = String((override && override.y) || '').trim();
    var m = String((override && override.m) || '').trim();
    var d = String((override && override.d) || '').trim();
    return {
      y: y ? Number(y) : Number(fallback && fallback.y),
      m: m ? Number(m) : Number(fallback && fallback.m),
      d: d ? Number(d) : Number(fallback && fallback.d)
    };
  }

  function dateOverrideActive(override) {
    return !!(override && (String(override.y || '').trim() || String(override.m || '').trim() || String(override.d || '').trim()));
  }

  function dateFilterIsNarrowing() {
    return dateOverrideActive(state.dateFrom) || dateOverrideActive(state.dateTo);
  }

  function daysInMonth(year, month) {
    return new Date(year, month, 0).getDate();
  }

  function clampNumber(value, min, max) {
    var n = Number(value);
    if (!Number.isFinite(n)) n = min;
    if (Number.isFinite(min)) n = Math.max(min, n);
    if (Number.isFinite(max)) n = Math.min(max, n);
    return n;
  }

  function partLimits(bound, part) {
    var bounds = state.dateBounds || {};
    var min = copyDateParts(bounds.min);
    var max = copyDateParts(bounds.max);
    if (!min || !max) {
      var now = new Date();
      min = { y: now.getFullYear(), m: 1, d: 1 };
      max = { y: now.getFullYear(), m: now.getMonth() + 1, d: now.getDate() };
    }
    var otherBound = bound === 'from' ? 'to' : 'from';
    var selfOverride = bound === 'from' ? state.dateFrom : state.dateTo;
    var otherOverride = bound === 'from' ? state.dateTo : state.dateFrom;
    var selfFallback = bound === 'from' ? min : max;
    var otherFallback = bound === 'from' ? max : min;
    var self = ymdFromInputs(selfOverride, selfFallback);
    var other = ymdFromInputs(otherOverride, otherFallback);

    var minY = min.y;
    var maxY = max.y;
    if (bound === 'from') maxY = Math.min(maxY, other.y || maxY);
    else minY = Math.max(minY, other.y || minY);

    if (part === 'y') return { min: minY, max: maxY, width: 4 };

    var year = clampNumber(self.y, minY, maxY);
    var minM = 1;
    var maxM = 12;
    if (year === min.y) minM = Math.max(minM, min.m);
    if (year === max.y) maxM = Math.min(maxM, max.m);
    if (bound === 'from' && year === other.y) maxM = Math.min(maxM, other.m || maxM);
    if (bound === 'to' && year === other.y) minM = Math.max(minM, other.m || minM);
    if (part === 'm') return { min: minM, max: Math.max(minM, maxM), width: 2 };

    var month = clampNumber(self.m, minM, Math.max(minM, maxM));
    var minD = 1;
    var maxD = daysInMonth(year, month);
    if (year === min.y && month === min.m) minD = Math.max(minD, min.d);
    if (year === max.y && month === max.m) maxD = Math.min(maxD, max.d);
    if (bound === 'from' && year === other.y && month === other.m) {
      maxD = Math.min(maxD, other.d || maxD);
    }
    if (bound === 'to' && year === other.y && month === other.m) {
      minD = Math.max(minD, other.d || minD);
    }
    return { min: minD, max: Math.max(minD, maxD), width: 2 };
  }

  function clampDateDigitValue(bound, part, raw) {
    var digits = String(raw || '').replace(/\D/g, '');
    var limits = partLimits(bound, part);
    if (!digits) return '';
    var value = Number(digits);
    if (!Number.isFinite(value)) return '';
    if (digits.length < limits.width) {
      var maxPrefix = Number(String(limits.max).slice(0, digits.length));
      if (Number.isFinite(maxPrefix) && value > maxPrefix) {
        return String(limits.max).slice(0, digits.length);
      }
      return digits;
    }
    return String(clampNumber(value, limits.min, limits.max));
  }

  function finalizeDateDigitValue(bound, part, raw) {
    var digits = String(raw || '').replace(/\D/g, '');
    if (!digits) return '';
    var limits = partLimits(bound, part);
    var value = Number(digits);
    if (!Number.isFinite(value)) return '';
    if (value >= limits.min && value <= limits.max) return String(value);
    if (digits.length < limits.width) return '';
    return String(clampNumber(value, limits.min, limits.max));
  }

  function syncDateRangePlaceholders() {
    var root = $('mediaDateRange');
    if (!root) return;
    var bounds = state.dateBounds || {};
    var min = bounds.min;
    var max = bounds.max;
    root.querySelectorAll('.media-date-digit').forEach(function(input) {
      var bound = input.getAttribute('data-bound') || 'from';
      var part = input.getAttribute('data-part') || 'y';
      var fallback = bound === 'from' ? min : max;
      var placeholder = fallback ? String(fallback[part] || '') : '';
      if (part !== 'y' && placeholder) placeholder = pad2(Number(placeholder));
      input.placeholder = placeholder;
      var override = bound === 'from' ? state.dateFrom : state.dateTo;
      var current = String((override && override[part]) || '');
      if (input.value !== current) input.value = current;
      input.classList.toggle('has-override', !!current);
    });
  }

  function readDateOverridesFromInputs() {
    var root = $('mediaDateRange');
    if (!root) return;
    var nextFrom = emptyDateOverride();
    var nextTo = emptyDateOverride();
    root.querySelectorAll('.media-date-digit').forEach(function(input) {
      var bound = input.getAttribute('data-bound') || 'from';
      var part = input.getAttribute('data-part') || 'y';
      var clamped = finalizeDateDigitValue(bound, part, input.value);
      input.value = clamped;
      if (bound === 'from') nextFrom[part] = clamped;
      else nextTo[part] = clamped;
    });
    state.dateFrom = nextFrom;
    state.dateTo = nextTo;
  }

  function applyDateRangeFilter(force) {
    readDateOverridesFromInputs();
    syncDateRangePlaceholders();
    state.mediaPage = 1;
    rebuildVisibleItems();
  }

  function refreshDateBoundsFromPool(items) {
    var min = null;
    var max = null;
    (items || []).forEach(function(item) {
      var parts = itemDayParts(item);
      if (!parts) return;
      if (!min || dayKey(parts) < dayKey(min)) min = parts;
      if (!max || dayKey(parts) > dayKey(max)) max = parts;
    });
    if (!min || !max) {
      var now = new Date();
      min = { y: now.getFullYear(), m: now.getMonth() + 1, d: now.getDate() };
      max = copyDateParts(min);
    }
    state.dateBounds = { min: min, max: max };
    // Drop overrides that fall outside the new pool.
    ['y', 'm', 'd'].forEach(function(part) {
      if (state.dateFrom[part]) {
        state.dateFrom[part] = clampDateDigitValue('from', part, state.dateFrom[part]);
      }
      if (state.dateTo[part]) {
        state.dateTo[part] = clampDateDigitValue('to', part, state.dateTo[part]);
      }
    });
    syncDateRangePlaceholders();
  }

  function itemMatchesDateFilter(item) {
    if (!dateFilterIsNarrowing()) return true;
    var parts = itemDayParts(item);
    if (!parts) return false;
    var bounds = state.dateBounds || {};
    var from = ymdFromInputs(state.dateFrom, bounds.min);
    var to = ymdFromInputs(state.dateTo, bounds.max);
    var key = dayKey(parts);
    return key >= dayKey(from) && key <= dayKey(to);
  }

  function itemHasMac(item) {
    return !!(item && (item.onMac || item.locations === 'mac'));
  }

  function itemHasVps(item) {
    return !!(item && (item.onVps || item.locations === 'vps') && !item.isMacOnly);
  }

  function itemIsDownloadable(item) {
    return !!(item && itemHasVps(item) && item.type === 'video' && item.syncStatus !== 'synced');
  }

  function itemIsSelectable(item) {
    return !!(item && item.type === 'video' && (itemHasVps(item) || itemHasMac(item)));
  }

  function sortNameKey(item) {
    return displayMediaTitle(item) || String((item && (item.title || item.filename)) || '');
  }

  function sortCatalogItems(items) {
    var list = (items || []).slice();
    var field = state.sortField === 'size' ? 'size' : 'name';
    var desc = state.sortDir !== 'asc';
    list.sort(function(a, b) {
      var cmp = 0;
      if (field === 'size') {
        cmp = intSize(a.size) - intSize(b.size);
      } else {
        cmp = sortNameKey(a).localeCompare(sortNameKey(b));
        if (!cmp) cmp = intSize(a.createdAt) - intSize(b.createdAt);
      }
      return desc ? -cmp : cmp;
    });
    return list;
  }

  function deviceFilterMode() {
    var selected = Array.isArray(state.deviceFilters) ? state.deviceFilters : [];
    var hasVps = selected.indexOf('vps') !== -1;
    var hasMac = selected.indexOf('mac') !== -1;
    if (hasVps && hasMac) return 'all';
    if (hasMac) return 'mac';
    return 'vps';
  }

  function normalizeDeviceFilters(list) {
    var next = [];
    (Array.isArray(list) ? list : []).forEach(function(value) {
      var token = String(value || '').toLowerCase();
      if ((token === 'vps' || token === 'mac') && next.indexOf(token) === -1) next.push(token);
    });
    if (!next.length) next = ['vps', 'mac'];
    return next;
  }

  function catalogMatchesFilters(item, options) {
    options = options || {};
    if (!item) return false;
    var selected = selectedStreamerList();
    if (selected.length) {
      if (selected.indexOf(String(item.username || '')) === -1) return false;
    } else if (state.filterProfile && String(item.username || '') !== String(state.filterProfile)) {
      return false;
    }
    if (!options.ignoreDate && !itemMatchesDateFilter(item)) return false;
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
    // Date-renamed Motrix copies: match only when this byte size is unique on both sides.
    if (size > 0) {
      var macHits = [];
      var vpsSameSize = 0;
      for (i = 0; i < macFiles.length; i++) {
        if (used[i]) continue;
        if (intSize(macFiles[i].size) === size) macHits.push(i);
      }
      (state.vpsItems || []).forEach(function(other) {
        if (!other || (other.type && other.type !== 'video')) return;
        if (intSize(other.size) === size) vpsSameSize += 1;
      });
      if (macHits.length === 1 && vpsSameSize === 1) {
        used[macHits[0]] = true;
        return macFiles[macHits[0]];
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
      // Complete Mac copies are canonical; VPS rows are omitted from the catalog
      // while the server purges the staging file. Incomplete matches stay on VPS.
      item.onVps = true;
      item.onMac = syncStatus === 'synced';
      item.locations = 'vps';
      item.syncStatus = syncStatus;
      item.macRelativePath = syncStatus === 'synced' ? String(macFile.filename || '') : '';
      item.macSize = syncStatus === 'synced' ? intSize(macFile.size) : 0;
      item.isMacOnly = false;
      if (syncStatus === 'synced') {
        var macRes = String(macFile.resolution || '').trim();
        if (macRes && !String(item.resolution || '').trim()) {
          item.resolution = macRes;
        }
        var macDuration = numberOrZero(macFile.durationSeconds || macFile.duration);
        if (macDuration && !numberOrZero(item.duration)) {
          item.duration = macDuration;
        }
        var macFps = Number(macFile.fps);
        if (macFps > 0 && !(Number(item.fps) > 0)) {
          item.fps = macFps;
        }
        var macBitrate = numberOrZero(macFile.bitrate);
        if (macBitrate > 0 && !(numberOrZero(item.bitrate) > 0)) {
          item.bitrate = macBitrate;
        }
      }
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

  function releaseMacFileUse(macFiles, used, macFile) {
    if (!macFile) return;
    for (var i = 0; i < macFiles.length; i++) {
      if (macFiles[i] === macFile) {
        used[i] = false;
        return;
      }
    }
  }

  function buildUnifiedCatalog(options) {
    options = options || {};
    var macFiles = state.macFiles || [];
    var used = {};
    var unified = [];

    (state.vpsItems || []).forEach(function(item) {
      var copy = Object.assign({}, item);
      var identity = parseIdentityWithSource(copy.username);
      if (identity.username) copy.username = identity.username;
      if (identity.sourceType && !normalizeSourceMarker(copy.sourceType || copy.source_type)) {
        copy.sourceType = identity.sourceType;
        copy.source_type = identity.sourceType;
      }
      var macFile = findMacFileForVpsItem(copy, macFiles, used);
      annotateVpsItemWithMac(copy, macFile);
      // Synced VPS copies are being purged; show the Mac card only.
      if (macFile && copy.syncStatus === 'synced') {
        releaseMacFileUse(macFiles, used, macFile);
        return;
      }
      unified.push(copy);
    });

    macFiles.forEach(function(file, index) {
      if (used[index]) return;
      var meta = parseMacFileMeta(file);
      var rid = String(file.recordingId || '').trim();
      var id = rid ? ('mac:' + rid) : ('mac:' + meta.relative);
      var duration = numberOrZero(file.durationSeconds || file.duration);
      var resolution = String(file.resolution || '').trim();
      var fps = Number(file.fps);
      var bitrate = numberOrZero(file.bitrate);
      var profileUsername = resolveLocalIdentityToProfileUsername(meta.username, meta.sourceType);
      unified.push({
        id: id,
        type: 'video',
        recordingId: rid,
        filename: meta.basename,
        title: meta.title,
        username: profileUsername || meta.username,
        sourceType: meta.sourceType || '',
        source_type: meta.sourceType || '',
        size: intSize(file.size),
        sizeFormatted: formatBytesShort(file.size),
        createdAt: meta.createdAt,
        duration: duration,
        resolution: resolution,
        fps: fps > 0 ? fps : null,
        bitrate: bitrate > 0 ? bitrate : null,
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

    return sortCatalogItems(unified.filter(function(item) {
      return catalogMatchesFilters(item, options);
    }));
  }

  function collectDevicePool(options) {
    options = options || {};
    var device = deviceFilterMode();
    var macFiles = state.macFiles || [];
    var used = {};

    if (device === 'vps') {
      return (state.vpsItems || []).map(function(item) {
        var copy = Object.assign({}, item);
        var identity = parseIdentityWithSource(copy.username);
        if (identity.username) copy.username = identity.username;
        if (identity.sourceType && !normalizeSourceMarker(copy.sourceType || copy.source_type)) {
          copy.sourceType = identity.sourceType;
          copy.source_type = identity.sourceType;
        }
        var macFile = findMacFileForVpsItem(copy, macFiles, used);
        return annotateVpsItemWithMac(copy, macFile);
      }).filter(function(item) {
        return catalogMatchesFilters(item, options) && item.syncStatus !== 'synced';
      });
    }

    return buildUnifiedCatalog(options).filter(function(item) {
      if (device === 'mac') return itemHasMac(item);
      return true;
    });
  }

  function rebuildVisibleItems() {
    var pool = collectDevicePool({ ignoreDate: true });
    refreshDateBoundsFromPool(pool);
    state.items = sortCatalogItems(pool.filter(function(item) {
      return catalogMatchesFilters(item);
    }));

    // Drop stale selections that are no longer visible/downloadable.
    Object.keys(state.selectedItemIds).forEach(function(id) {
      if (!itemById(id)) delete state.selectedItemIds[id];
    });
    pruneProjectSelection();

    renderRecentSection(state.items.length);
    updateMacToolbar();
  }

  function projectById(projectId) {
    var id = String(projectId || '');
    for (var i = 0; i < (state.projects || []).length; i++) {
      if (String(state.projects[i].projectId || '') === id) return state.projects[i];
    }
    return null;
  }

  function projectMemberItemIds(project) {
    var ids = [];
    ((project && project.members) || []).forEach(function(member) {
      var id = String((member && (member.itemId || member.item_id)) || '').trim();
      if (id) ids.push(id);
    });
    return ids;
  }

  function pruneProjectSelection() {
    Object.keys(state.selectedProjectIds || {}).forEach(function(projectId) {
      if (!projectById(projectId)) delete state.selectedProjectIds[projectId];
    });
  }

  function projectIsCompleted(project) {
    if (!project) return false;
    if (project.localComplete) return true;
    var status = String(project.status || '');
    return status === 'ready' || status === 'local_complete' ||
      status === 'concatenated' || status === 'concat_failed';
  }

  function projectIsInProgress(project) {
    if (!project) return false;
    var status = String(project.status || '');
    return status === 'open' || status === 'awaiting_gap' || status === 'syncing';
  }

  function normalizeConcatStatus(value) {
    var status = String(value || 'none').toLowerCase();
    if (status === 'queued' || status === 'running' || status === 'concatenating') {
      return 'concatenating';
    }
    if (status === 'concat_failed' || status === 'failed' || status === 'error') {
      return 'failed';
    }
    if (status === 'concatenated' || status === 'done') return 'concatenated';
    if (!status || status === 'none') return 'none';
    return status;
  }

  function projectMatchesFilters(project) {
    if (!project) return false;
    var selected = selectedStreamerList();
    if (selected.length && selected.indexOf(String(project.username || '')) === -1) {
      return false;
    }
    var statusFilter = state.filterProjectStatus || 'all';
    if (statusFilter === 'in-progress' && !projectIsInProgress(project)) return false;
    if (statusFilter === 'completed') {
      if (!projectIsCompleted(project)) return false;
      var syncFilter = state.filterCompletedSync || 'all';
      if (syncFilter === 'synced' && !project.localComplete) return false;
      if (syncFilter === 'not-synced' && project.localComplete) return false;
    }
    var concatFilter = state.filterConcatStatus || 'all';
    if (concatFilter !== 'all') {
      if (normalizeConcatStatus(project.concatStatus) !== concatFilter) return false;
    }
    return true;
  }

  function visibleProjects() {
    return sortProjects((state.projects || []).filter(projectMatchesFilters));
  }

  function sortProjects(projects) {
    var list = (projects || []).slice();
    var desc = state.sortDir !== 'asc';
    list.sort(function(a, b) {
      var cmp = 0;
      if (state.sortField === 'size') {
        cmp = intSize(a.totalSize) - intSize(b.totalSize);
      } else {
        cmp = intSize(a.startedAt) - intSize(b.startedAt);
        if (!cmp) {
          cmp = String(a.title || '').localeCompare(String(b.title || ''));
        }
      }
      return desc ? -cmp : cmp;
    });
    return list;
  }

  async function loadRecordingProjects() {
    try {
      var params = new URLSearchParams();
      var usernames = selectedStreamerList();
      if (usernames.length) params.set('usernames', usernames.join(','));
      var res = await fetch('/api/recording-projects?' + params.toString(), { cache: 'no-store' });
      if (!res.ok) throw new Error('Failed to load recording projects');
      var data = await res.json();
      state.projects = Array.isArray(data.projects) ? data.projects : [];
      pruneProjectSelection();
      if (state.projectDetailId && !projectById(state.projectDetailId)) {
        state.projectDetailId = null;
      }
      if (state.videoViewMode === 'projects') {
        renderRecentSection(state.items.length);
        updateMacToolbar();
      }
    } catch (e) {
      console.error('Error loading recording projects:', e);
      state.projects = [];
    }
  }

  function formatProjectSizeLine(project) {
    var synced = formatBytesShort(project && project.syncedSize);
    var total = formatBytesShort(project && project.totalSize);
    var syncedCount = Number((project && project.syncedCount) || 0);
    var memberCount = Number((project && project.memberCount) || 0);
    return (synced || '0 B') + '/' + (total || '0 B') +
      ' (' + syncedCount + '/' + memberCount + ')';
  }

  function projectStatusBadges(project) {
    var badges = [];
    var status = String((project && project.status) || '');
    if (status) {
      badges.push('<span class="media-tag">' + escapeHtml(status.replace(/_/g, ' ')) + '</span>');
    }
    var concat = normalizeConcatStatus(project && project.concatStatus);
    if (concat && concat !== 'none') {
      badges.push('<span class="media-tag">' + escapeHtml(concat) + '</span>');
    }
    if (project && project.localComplete) {
      badges.push('<span class="media-tag">fully synced</span>');
    }
    return badges;
  }

  function projectHasMacMember(project) {
    return ((project && project.members) || []).some(function(member) {
      var sync = String((member && (member.syncStatus || member.sync_status || member.location)) || '');
      return sync === 'synced' || sync === 'mac';
    });
  }

  function firstMacMemberItem(project) {
    var members = (project && project.members) || [];
    for (var i = 0; i < members.length; i++) {
      var member = members[i];
      var itemId = String((member && (member.itemId || member.item_id)) || '');
      var item = itemId ? itemById(itemId) : null;
      if (item && itemHasMac(item)) return item;
      var sync = String((member && (member.syncStatus || member.location)) || '');
      if (sync === 'synced' || sync === 'mac') {
        var filename = String((member && member.filename) || '');
        var relative = '';
        (state.macFiles || []).some(function(file) {
          var name = String(file.filename || '');
          if (name === filename || name.split('/').pop() === filename) {
            relative = name;
            return true;
          }
          return false;
        });
        if (relative || filename) {
          return {
            macRelativePath: relative || filename,
            recordingId: (member && (member.recordingId || member.recording_id)) || '',
            onMac: true,
            locations: 'mac'
          };
        }
      }
    }
    return null;
  }

  function renderProjectCard(project) {
    var projectId = String((project && project.projectId) || '');
    var selected = !!state.selectedProjectIds[projectId];
    var badges = projectStatusBadges(project);
    var canReveal = projectHasMacMember(project);
    var profile = profileByUsername(project.username);
    var profileLabelText = profile ? profileLabel(profile) : (project.username || '');
    return '' +
      '<article class="media-project-card' + (selected ? ' selected' : '') +
      '" role="button" tabindex="0" data-project-id="' + escapeHtml(projectId) +
      '" data-media-action="open-project" title="Open project">' +
        '<button class="media-card-profile-id" type="button" data-media-action="profile" data-profile="' +
          escapeHtml(project.username || '') + '" title="Show recordings for ' +
          escapeHtml(profileLabelText) + '">' + escapeHtml(profileLabelText) + '</button>' +
        '<h3 class="media-project-card-title">' + escapeHtml(project.title || projectId) + '</h3>' +
        (project.spanLine
          ? '<div class="media-project-card-line">' + escapeHtml(project.spanLine) + '</div>'
          : '') +
        '<div class="media-project-card-line">' + escapeHtml(formatProjectSizeLine(project)) + '</div>' +
        (project.clipStructure
          ? '<div class="media-project-card-line">' + escapeHtml(project.clipStructure) + '</div>'
          : '') +
        (badges.length ? '<div class="media-project-card-badges">' + badges.join('') + '</div>' : '') +
        '<div class="media-project-card-footer">' +
          '<button type="button" class="media-action-btn" data-media-action="select-project" data-project-id="' +
            escapeHtml(projectId) + '">' + (selected ? 'Selected' : 'Select') + '</button>' +
          (canReveal
            ? '<button type="button" class="media-card-footer-btn media-card-open-folder" data-media-action="reveal-project" data-project-id="' +
              escapeHtml(projectId) + '" title="Show in Finder">Open folder</button>'
            : '') +
        '</div>' +
      '</article>';
  }

  function hideProjectDetail() {
    var detail = $('mediaProjectDetail');
    var grid = $('mediaGrid');
    if (detail) {
      detail.hidden = true;
      detail.innerHTML = '';
    }
    if (grid) grid.hidden = false;
  }

  function renderProjectDetail(projectId) {
    var detail = $('mediaProjectDetail');
    var grid = $('mediaGrid');
    var project = projectById(projectId);
    if (!detail || !project) {
      state.projectDetailId = null;
      hideProjectDetail();
      return;
    }
    state.projectDetailId = String(project.projectId || projectId);
    if (grid) grid.hidden = true;
    detail.hidden = false;
    var memberCards = ((project.members) || []).map(function(member) {
      var itemId = String((member && (member.itemId || member.item_id)) || '');
      var item = itemId ? itemById(itemId) : null;
      if (item) return renderCard(item);
      var sync = String((member && (member.syncStatus || member.location)) || 'vps');
      var title = displayMediaTitle({
        filename: member.filename,
        title: member.filename,
        createdAt: member.startedAt || member.started_at
      }) || String(member.filename || '');
      var selected = itemId && !!state.selectedItemIds[itemId];
      return '' +
        '<article class="media-card' + (selected ? ' selected' : '') + '" data-media-id="' +
          escapeHtml(itemId) + '">' +
          '<div class="media-card-body"' +
            (itemId ? ' data-media-action="select-card" data-media-id="' + escapeHtml(itemId) + '"' : '') + '>' +
            '<div class="media-card-title">' + escapeHtml(title) + '</div>' +
            '<div class="media-card-meta">' +
              '<span class="media-card-detail-row">Sync: ' + escapeHtml(sync) + '</span>' +
              '<span class="media-card-detail-row">Size: ' +
                escapeHtml(formatBytesShort(member.sizeBytes || member.size_bytes) || '-') + '</span>' +
            '</div>' +
          '</div>' +
        '</article>';
    }).join('');
    detail.innerHTML =
      '<div class="media-project-detail-head">' +
        '<button type="button" class="media-action-btn media-project-detail-back" data-media-action="project-back">Back</button>' +
        '<div class="media-project-detail-copy">' +
          '<h3 class="media-project-detail-title">' + escapeHtml(project.title || project.projectId) + '</h3>' +
          '<div class="media-project-detail-meta">' +
            escapeHtml([project.spanLine, formatProjectSizeLine(project), project.clipStructure]
              .filter(Boolean).join(' · ')) +
          '</div>' +
          '<div class="media-project-card-badges">' + projectStatusBadges(project).join('') + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="media-project-detail-grid">' +
        (memberCards || '<div class="empty-message"><p>No fragments in this project</p></div>') +
      '</div>';
  }

  function setVideoViewMode(mode) {
    state.videoViewMode = mode === 'fragments' ? 'fragments' : 'projects';
    if (state.videoViewMode === 'fragments') {
      state.projectDetailId = null;
      hideProjectDetail();
    }
    state.mediaPage = 1;
    syncProjectFilterControls();
    renderRecentSection(state.items.length);
    updateMacToolbar();
  }

  function openProjectDetail(projectId) {
    if (!projectId) return;
    state.projectDetailId = String(projectId);
    renderProjectDetail(state.projectDetailId);
    updateMacToolbar();
  }

  function closeProjectDetail() {
    state.projectDetailId = null;
    hideProjectDetail();
    renderRecentSection(state.items.length);
    updateMacToolbar();
  }

  function selectProject(projectId, selected) {
    var project = projectById(projectId);
    if (!project) return;
    if (selected) state.selectedProjectIds[projectId] = true;
    else delete state.selectedProjectIds[projectId];
    projectMemberItemIds(project).forEach(function(itemId) {
      if (selected) {
        if (itemIsSelectable(itemById(itemId)) || !itemById(itemId)) {
          state.selectedItemIds[itemId] = true;
        }
      } else {
        delete state.selectedItemIds[itemId];
      }
    });
    if (state.projectDetailId) renderProjectDetail(state.projectDetailId);
    else renderRecentSection(state.items.length);
    updateMacToolbar();
  }

  function selectedConcatableProjectIds() {
    return Object.keys(state.selectedProjectIds || {}).filter(function(projectId) {
      var project = projectById(projectId);
      return !!(project && project.localComplete &&
        normalizeConcatStatus(project.concatStatus) !== 'concatenated' &&
        normalizeConcatStatus(project.concatStatus) !== 'concatenating');
    });
  }

  function syncProjectFilterControls() {
    var projectRow = $('mediaProjectFilterRow');
    if (projectRow) projectRow.hidden = state.videoViewMode !== 'projects';
    document.querySelectorAll('#mediaViewToggle [data-view-mode]').forEach(function(button) {
      button.classList.toggle('active', button.dataset.viewMode === state.videoViewMode);
    });
    document.querySelectorAll('#mediaProjectStatusFilters [data-project-status]').forEach(function(button) {
      button.classList.toggle('active', button.dataset.projectStatus === state.filterProjectStatus);
    });
    var completedFilters = $('mediaCompletedSyncFilters');
    if (completedFilters) {
      completedFilters.hidden = state.filterProjectStatus !== 'completed';
    }
    document.querySelectorAll('#mediaCompletedSyncFilters [data-completed-sync]').forEach(function(button) {
      button.classList.toggle('active', button.dataset.completedSync === state.filterCompletedSync);
    });
    document.querySelectorAll('#mediaConcatStatusFilters [data-concat-status]').forEach(function(button) {
      button.classList.toggle('active', button.dataset.concatStatus === state.filterConcatStatus);
    });
  }

  async function concatSelectedProjects() {
    var ids = selectedConcatableProjectIds();
    if (!ids.length) return;
    var button = $('mediaConcatSelectedBtn');
    if (button) button.disabled = true;
    try {
      var res = await fetch('/api/recording-projects/concat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectIds: ids })
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || data.error || 'Concat queue failed');
      var queued = Array.isArray(data.queued) ? data.queued.length : 0;
      showToast(queued ? (queued + ' project(s) queued for concat') : 'No projects queued', 'success');
      await loadRecordingProjects();
    } catch (e) {
      showToast(e.message || 'Concat queue failed', 'error');
    }
    updateMacToolbar();
  }

  function formatSpeedBps(bps) {
    var n = Number(bps);
    if (!Number.isFinite(n) || n <= 0) return '';
    return formatBytesShort(n) + '/s';
  }

  function tasksSelectionKey(kind, id) {
    return String(kind || '') + ':' + String(id || '');
  }

  function clearTasksSelection() {
    state.tasksSelected = {};
  }

  function isTasksSelected(kind, id) {
    return !!state.tasksSelected[tasksSelectionKey(kind, id)];
  }

  function setTasksSelected(kind, id, selected) {
    var key = tasksSelectionKey(kind, id);
    if (selected) state.tasksSelected[key] = true;
    else delete state.tasksSelected[key];
  }

  function toggleTasksSelected(kind, id) {
    setTasksSelected(kind, id, !isTasksSelected(kind, id));
  }

  function selectedTasksOfKind(kind) {
    var prefix = String(kind || '') + ':';
    return Object.keys(state.tasksSelected || {}).filter(function(key) {
      return key.indexOf(prefix) === 0;
    }).map(function(key) {
      return key.slice(prefix.length);
    }).filter(Boolean);
  }

  function stopTasksPolling() {
    if (state.tasksPollTimer) {
      clearTimeout(state.tasksPollTimer);
      state.tasksPollTimer = null;
    }
  }

  function scheduleTasksPoll() {
    stopTasksPolling();
    if (!state.tasksOpen) return;
    if (document.visibilityState !== 'visible') return;
    state.tasksPollTimer = setTimeout(function() {
      state.tasksPollTimer = null;
      refreshTasksPanel(false);
    }, TASKS_POLL_MS);
  }

  function openTasksModal(tab) {
    state.tasksOpen = true;
    state.tasksTab = tab || state.tasksTab || 'download';
    clearTasksSelection();
    var modal = $('mediaTasksModal');
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-tasks-open');
    }
    syncTasksTabUi();
    refreshTasksPanel(true);
  }

  function closeTasksModal() {
    state.tasksOpen = false;
    stopTasksPolling();
    state.tasksDrag = null;
    clearTasksSelection();
    var modal = $('mediaTasksModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-tasks-open');
    }
  }

  function setTasksTab(tab) {
    var next = tab === 'concat' || tab === 'convert' ? tab : 'download';
    if (state.tasksTab === next) return;
    state.tasksTab = next;
    clearTasksSelection();
    syncTasksTabUi();
    renderTasksPanel();
    if (state.tasksOpen) refreshTasksPanel(true);
  }

  function syncTasksTabUi() {
    document.querySelectorAll('#mediaTasksTabs [data-tasks-tab]').forEach(function(button) {
      var active = button.dataset.tasksTab === state.tasksTab;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    var toolbar = $('mediaTasksToolbar');
    if (toolbar) toolbar.hidden = state.tasksTab === 'convert';
  }

  function taskStatusLabel(raw) {
    var status = String(raw || '').trim().toLowerCase();
    if (!status) return '';
    if (status === 'running' || status === 'downloading' || status === 'active') return 'Downloading';
    if (status === 'queued' || status === 'pending' || status === 'waiting') return 'Queued';
    if (status === 'paused') return 'Paused';
    if (status === 'done' || status === 'completed' || status === 'synced') return 'Done';
    if (status === 'failed' || status === 'error' || status === 'concat_failed') return 'Failed';
    if (status === 'concatenating') return 'Concatenating';
    if (status === 'converting') return 'Converting';
    return status.charAt(0).toUpperCase() + status.slice(1);
  }

  function taskIsReorderable(status) {
    var s = String(status || '').toLowerCase();
    return s === 'queued' || s === 'paused' || s === 'pending' || s === 'waiting' || !s;
  }

  function normalizeDownloadProject(raw, index) {
    raw = raw || {};
    var projectId = String(raw.projectId || raw.project_id || raw.id || ('project-' + index));
    var fragments = raw.fragments || raw.tasks || raw.items || raw.files || [];
    if (!Array.isArray(fragments)) fragments = [];
    var status = raw.status || raw.state || '';
    var downloaded = intSize(raw.downloadedBytes != null ? raw.downloadedBytes : raw.downloaded_bytes);
    var total = intSize(raw.totalBytes != null ? raw.totalBytes : raw.total_bytes);
    var percent = raw.percent != null ? Number(raw.percent) : (total > 0 ? (downloaded / total) * 100 : null);
    var speed = raw.speedBps != null ? raw.speedBps : (raw.speed_bps != null ? raw.speed_bps : raw.speed);
    return {
      projectId: projectId,
      name: raw.name || raw.title || raw.displayName || raw.label || projectId,
      username: raw.username || raw.streamer || '',
      status: status,
      paused: !!(raw.paused || String(status).toLowerCase() === 'paused'),
      speedBps: speed,
      downloadedBytes: downloaded,
      totalBytes: total,
      percent: percent,
      reorderable: raw.reorderable != null ? !!raw.reorderable : taskIsReorderable(status),
      fragments: fragments.map(function(frag, fragIndex) {
        frag = frag || {};
        var taskId = String(frag.taskId || frag.task_id || frag.id || frag.itemId || frag.item_id || (projectId + '-frag-' + fragIndex));
        var fDownloaded = intSize(frag.downloadedBytes != null ? frag.downloadedBytes : frag.downloaded_bytes);
        var fTotal = intSize(frag.totalBytes != null ? frag.totalBytes : (frag.total_bytes != null ? frag.total_bytes : frag.size));
        var fPercent = frag.percent != null ? Number(frag.percent) : (fTotal > 0 ? (fDownloaded / fTotal) * 100 : null);
        var fStatus = frag.status || frag.state || status;
        return {
          taskId: taskId,
          itemId: String(frag.itemId || frag.item_id || ''),
          name: frag.name || frag.title || frag.filename || frag.label || taskId,
          status: fStatus,
          paused: !!(frag.paused || String(fStatus).toLowerCase() === 'paused'),
          speedBps: frag.speedBps != null ? frag.speedBps : (frag.speed_bps != null ? frag.speed_bps : frag.speed),
          downloadedBytes: fDownloaded,
          totalBytes: fTotal,
          percent: fPercent,
          reorderable: frag.reorderable != null ? !!frag.reorderable : taskIsReorderable(fStatus)
        };
      })
    };
  }

  function normalizeDownloadTasksPayload(data) {
    var projects = [];
    if (!data || typeof data !== 'object') return { projects: projects };
    if (Array.isArray(data.projects)) projects = data.projects;
    else if (Array.isArray(data.queue)) projects = data.queue;
    else if (Array.isArray(data.items)) projects = data.items;
    else if (Array.isArray(data)) projects = data;
    return { projects: projects.map(normalizeDownloadProject) };
  }

  function normalizeConcatTask(raw, index) {
    raw = raw || {};
    var projectId = String(raw.projectId || raw.project_id || raw.id || ('concat-' + index));
    var status = raw.status || raw.concatStatus || raw.concat_status || raw.state || '';
    var step = raw.fragmentIndex != null ? intSize(raw.fragmentIndex)
      : (raw.fragment_index != null ? intSize(raw.fragment_index)
        : (raw.step != null ? intSize(raw.step) : (raw.current != null ? intSize(raw.current) : 0)));
    var total = raw.fragmentTotal != null ? intSize(raw.fragmentTotal)
      : (raw.fragment_total != null ? intSize(raw.fragment_total)
        : (raw.total != null ? intSize(raw.total) : (raw.memberCount != null ? intSize(raw.memberCount) : 0)));
    return {
      projectId: projectId,
      name: raw.name || raw.title || raw.displayName || raw.label || projectId,
      username: raw.username || '',
      status: status,
      paused: !!(raw.paused || String(status).toLowerCase() === 'paused'),
      fragmentIndex: step,
      fragmentTotal: total,
      reorderable: raw.reorderable != null ? !!raw.reorderable : taskIsReorderable(status)
    };
  }

  function normalizeConcatTasksPayload(data) {
    if (!data || typeof data !== 'object') return { current: null, queue: [] };
    var current = null;
    var queue = [];
    if (data.current) current = normalizeConcatTask(data.current, 0);
    if (Array.isArray(data.queue)) queue = data.queue.map(normalizeConcatTask);
    else if (Array.isArray(data.items)) {
      var items = data.items.map(normalizeConcatTask);
      var running = items.filter(function(item) {
        var s = String(item.status || '').toLowerCase();
        return s === 'running' || s === 'concatenating' || s === 'active';
      });
      queue = items.filter(function(item) {
        return running.indexOf(item) === -1;
      });
      if (!current && running.length) current = running[0];
    } else if (Array.isArray(data)) {
      queue = data.map(normalizeConcatTask);
    }
    return { current: current, queue: queue };
  }

  function normalizeConvertTasksPayload(data) {
    var items = [];
    if (!data || typeof data !== 'object') return { items: items };
    if (Array.isArray(data.items)) items = data.items;
    else if (Array.isArray(data.tasks)) items = data.tasks;
    else if (Array.isArray(data.queue)) items = data.queue;
    else if (Array.isArray(data)) items = data;
    return {
      items: items.map(function(raw, index) {
        raw = raw || {};
        return {
          id: String(raw.id || raw.taskId || raw.task_id || raw.recordingId || raw.filename || ('convert-' + index)),
          name: raw.name || raw.title || raw.filename || raw.label || ('Convert #' + (index + 1)),
          username: raw.username || raw.streamer || '',
          status: raw.status || raw.state || raw.stage || '',
          stage: raw.stage || raw.phase || ''
        };
      })
    };
  }

  async function fetchTasksJson(path) {
    var res = await fetch(path, { cache: 'no-store' });
    if (res.status === 404) {
      return { ok: false, missing: true, data: null, status: 404 };
    }
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      var detail = (data && (data.detail || data.message || data.error)) || ('HTTP ' + res.status);
      return { ok: false, missing: false, data: data, status: res.status, error: String(detail) };
    }
    return { ok: true, missing: false, data: data, status: res.status };
  }

  async function postTasksAction(kind, action, body) {
    var res = await fetch('/api/tasks/' + encodeURIComponent(kind) + '/' + encodeURIComponent(action), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    var data = await res.json().catch(function() { return {}; });
    if (res.status === 404) throw new Error('Tasks API not available yet');
    if (!res.ok) throw new Error(data.detail || data.message || data.error || (action + ' failed'));
    return data;
  }

  function tasksProgressMeta(entity) {
    var parts = [];
    var speed = formatSpeedBps(entity && entity.speedBps);
    if (speed) parts.push(speed);
    var downloaded = intSize(entity && entity.downloadedBytes);
    var total = intSize(entity && entity.totalBytes);
    if (downloaded || total) {
      parts.push((formatBytesShort(downloaded) || '0 B') + ' / ' + (total ? formatBytesShort(total) : '?'));
    }
    var percent = entity && entity.percent;
    if (percent != null && Number.isFinite(Number(percent))) {
      parts.push(Math.max(0, Math.min(100, Math.round(Number(percent)))) + '%');
    }
    var status = taskStatusLabel(entity && entity.status);
    if (status) parts.push(status);
    return parts.join(' · ');
  }

  function concatStepLabel(task) {
    var index = intSize(task && task.fragmentIndex);
    var total = intSize(task && task.fragmentTotal);
    if (total > 0) return index + '/' + total;
    if (index > 0) return String(index);
    return '';
  }

  function updateTasksActionButtons() {
    var pauseBtn = $('mediaTasksPauseBtn');
    var resumeBtn = $('mediaTasksResumeBtn');
    var deleteBtn = $('mediaTasksDeleteBtn');
    var selectAllBtn = $('mediaTasksSelectAllBtn');
    var mutable = state.tasksTab === 'download' || state.tasksTab === 'concat';
    var selectedCount = 0;
    if (state.tasksTab === 'download') {
      selectedCount = selectedTasksOfKind('project').length + selectedTasksOfKind('fragment').length;
    } else if (state.tasksTab === 'concat') {
      selectedCount = selectedTasksOfKind('concat').length;
    }
    if (selectAllBtn) selectAllBtn.disabled = !mutable;
    if (pauseBtn) pauseBtn.disabled = !mutable || selectedCount === 0;
    if (resumeBtn) resumeBtn.disabled = !mutable || selectedCount === 0;
    if (deleteBtn) deleteBtn.disabled = !mutable || selectedCount === 0;
  }

  function renderTasksEmpty(message) {
    return '<div class="media-tasks-empty">' + escapeHtml(message || 'No tasks') + '</div>';
  }

  function renderDownloadTasksHtml() {
    var projects = (state.tasksDownload && state.tasksDownload.projects) || [];
    if (!projects.length) {
      return renderTasksEmpty(state.tasksError || 'Download queue is empty');
    }
    return projects.map(function(project) {
      var expanded = state.tasksExpanded[project.projectId] !== false;
      var selected = isTasksSelected('project', project.projectId);
      var meta = tasksProgressMeta(project);
      var canDrag = !!project.reorderable;
      var rows = (project.fragments || []).map(function(frag) {
        var fragSelected = isTasksSelected('fragment', frag.taskId);
        var fragMeta = tasksProgressMeta(frag);
        return '' +
          '<div class="media-tasks-row media-tasks-fragment' + (fragSelected ? ' is-selected' : '') + '"' +
            ' data-task-kind="fragment" data-task-id="' + escapeHtml(frag.taskId) + '"' +
            ' data-project-id="' + escapeHtml(project.projectId) + '"' +
            (frag.reorderable ? ' draggable="true"' : '') + '>' +
            '<button type="button" class="media-tasks-name" data-tasks-select="fragment" data-task-id="' + escapeHtml(frag.taskId) + '">' +
              escapeHtml(frag.name) +
            '</button>' +
            '<span class="media-tasks-meta">' + escapeHtml(fragMeta) + '</span>' +
          '</div>';
      }).join('');
      return '' +
        '<div class="media-tasks-project' + (selected ? ' is-selected' : '') + '"' +
          ' data-task-kind="project" data-project-id="' + escapeHtml(project.projectId) + '"' +
          (canDrag ? ' draggable="true"' : '') + '>' +
          '<div class="media-tasks-row media-tasks-project-row">' +
            '<button type="button" class="media-tasks-expand" data-tasks-expand="' + escapeHtml(project.projectId) + '" aria-expanded="' + (expanded ? 'true' : 'false') + '">' +
              (expanded ? '▾' : '▸') +
            '</button>' +
            '<button type="button" class="media-tasks-name" data-tasks-select="project" data-task-id="' + escapeHtml(project.projectId) + '">' +
              escapeHtml(project.name) +
              (project.username ? (' · ' + escapeHtml(project.username)) : '') +
            '</button>' +
            '<span class="media-tasks-meta">' + escapeHtml(meta) + '</span>' +
          '</div>' +
          (expanded ? ('<div class="media-tasks-fragments">' + (rows || renderTasksEmpty('No fragments')) + '</div>') : '') +
        '</div>';
    }).join('');
  }

  function renderConcatTaskRow(task, isCurrent) {
    if (!task) return '';
    var selected = isTasksSelected('concat', task.projectId);
    var status = taskStatusLabel(task.status);
    var step = concatStepLabel(task);
    var metaParts = [];
    if (isCurrent) metaParts.push('Current');
    if (status) metaParts.push(status);
    if (step) metaParts.push(step);
    return '' +
      '<div class="media-tasks-row media-tasks-concat-row' + (selected ? ' is-selected' : '') + (isCurrent ? ' is-current' : '') + '"' +
        ' data-task-kind="concat" data-project-id="' + escapeHtml(task.projectId) + '"' +
        (task.reorderable && !isCurrent ? ' draggable="true"' : '') + '>' +
        '<button type="button" class="media-tasks-name" data-tasks-select="concat" data-task-id="' + escapeHtml(task.projectId) + '">' +
          escapeHtml(task.name) +
          (task.username ? (' · ' + escapeHtml(task.username)) : '') +
        '</button>' +
        '<span class="media-tasks-meta">' + escapeHtml(metaParts.join(' · ')) + '</span>' +
      '</div>';
  }

  function renderConcatTasksHtml() {
    var current = state.tasksConcat && state.tasksConcat.current;
    var queue = (state.tasksConcat && state.tasksConcat.queue) || [];
    if (!current && !queue.length) {
      return renderTasksEmpty(state.tasksError || 'Concat queue is empty');
    }
    var html = '';
    if (current) {
      html += '<div class="media-tasks-section-label">Current</div>' + renderConcatTaskRow(current, true);
    }
    html += '<div class="media-tasks-section-label">Queue</div>';
    if (!queue.length) html += renderTasksEmpty('No queued concat tasks');
    else html += queue.map(function(task) { return renderConcatTaskRow(task, false); }).join('');
    return html;
  }

  function renderConvertTasksHtml() {
    var items = (state.tasksConvert && state.tasksConvert.items) || [];
    if (!items.length) {
      return renderTasksEmpty(state.tasksError || 'No convert tasks');
    }
    return items.map(function(item) {
      var metaParts = [];
      var status = taskStatusLabel(item.status || item.stage);
      if (item.username) metaParts.push(item.username);
      if (status) metaParts.push(status);
      return '' +
        '<div class="media-tasks-row media-tasks-convert-row">' +
          '<span class="media-tasks-name media-tasks-name-readonly">' + escapeHtml(item.name) + '</span>' +
          '<span class="media-tasks-meta">' + escapeHtml(metaParts.join(' · ')) + '</span>' +
        '</div>';
    }).join('');
  }

  function renderTasksPanel() {
    var body = $('mediaTasksBody');
    var status = $('mediaTasksStatus');
    if (!body) return;
    if (state.tasksLoading && !(
      (state.tasksTab === 'download' && state.tasksDownload.projects.length) ||
      (state.tasksTab === 'concat' && (state.tasksConcat.current || state.tasksConcat.queue.length)) ||
      (state.tasksTab === 'convert' && state.tasksConvert.items.length)
    )) {
      body.innerHTML = renderTasksEmpty('Loading…');
    } else if (state.tasksTab === 'download') {
      body.innerHTML = renderDownloadTasksHtml();
    } else if (state.tasksTab === 'concat') {
      body.innerHTML = renderConcatTasksHtml();
    } else {
      body.innerHTML = renderConvertTasksHtml();
    }
    if (status) {
      if (state.tasksError) status.textContent = state.tasksError;
      else if (state.tasksLoading) status.textContent = 'Refreshing…';
      else status.textContent = '';
    }
    updateTasksActionButtons();
  }

  async function refreshTasksPanel(force) {
    if (!state.tasksOpen) return;
    if (document.visibilityState !== 'visible' && !force) {
      stopTasksPolling();
      return;
    }
    var tab = state.tasksTab || 'download';
    state.tasksLoading = true;
    if (force) renderTasksPanel();
    try {
      var result = await fetchTasksJson('/api/tasks/' + encodeURIComponent(tab));
      if (!state.tasksOpen) return;
      if (result.missing) {
        state.tasksError = 'Tasks API not available yet';
        if (tab === 'download') state.tasksDownload = { projects: [] };
        if (tab === 'concat') state.tasksConcat = { current: null, queue: [] };
        if (tab === 'convert') state.tasksConvert = { items: [] };
      } else if (!result.ok) {
        state.tasksError = result.error || 'Failed to load tasks';
      } else {
        state.tasksError = '';
        if (tab === 'download') state.tasksDownload = normalizeDownloadTasksPayload(result.data);
        else if (tab === 'concat') state.tasksConcat = normalizeConcatTasksPayload(result.data);
        else state.tasksConvert = normalizeConvertTasksPayload(result.data);
      }
    } catch (e) {
      if (!state.tasksOpen) return;
      state.tasksError = (e && e.message) || 'Failed to load tasks';
    } finally {
      state.tasksLoading = false;
      if (state.tasksOpen) {
        renderTasksPanel();
        scheduleTasksPoll();
      }
    }
  }

  function selectAllTasksInTab() {
    clearTasksSelection();
    if (state.tasksTab === 'download') {
      ((state.tasksDownload && state.tasksDownload.projects) || []).forEach(function(project) {
        setTasksSelected('project', project.projectId, true);
        (project.fragments || []).forEach(function(frag) {
          setTasksSelected('fragment', frag.taskId, true);
        });
      });
    } else if (state.tasksTab === 'concat') {
      var current = state.tasksConcat && state.tasksConcat.current;
      if (current) setTasksSelected('concat', current.projectId, true);
      ((state.tasksConcat && state.tasksConcat.queue) || []).forEach(function(task) {
        setTasksSelected('concat', task.projectId, true);
      });
    }
    renderTasksPanel();
  }

  function collectDownloadMutationIds() {
    return {
      projectIds: selectedTasksOfKind('project'),
      taskIds: selectedTasksOfKind('fragment')
    };
  }

  async function mutateSelectedTasks(action) {
    if (state.tasksTab === 'convert') return;
    try {
      if (state.tasksTab === 'download') {
        var ids = collectDownloadMutationIds();
        if (!ids.projectIds.length && !ids.taskIds.length) return;
        await postTasksAction('download', action, ids);
      } else {
        var projectIds = selectedTasksOfKind('concat');
        if (!projectIds.length) return;
        await postTasksAction('concat', action, { projectIds: projectIds });
      }
      clearTasksSelection();
      await refreshTasksPanel(true);
    } catch (e) {
      showToast((e && e.message) || (action + ' failed'), 'error');
    }
  }

  function downloadProjectOrder() {
    return ((state.tasksDownload && state.tasksDownload.projects) || []).map(function(project) {
      return project.projectId;
    });
  }

  function downloadFragmentOrder(projectId) {
    var project = null;
    ((state.tasksDownload && state.tasksDownload.projects) || []).some(function(item) {
      if (item.projectId === projectId) {
        project = item;
        return true;
      }
      return false;
    });
    return ((project && project.fragments) || []).map(function(frag) {
      return frag.taskId;
    });
  }

  function concatQueueOrder() {
    return ((state.tasksConcat && state.tasksConcat.queue) || []).map(function(task) {
      return task.projectId;
    });
  }

  function moveIdInOrder(order, fromId, toId) {
    var list = (order || []).slice();
    var fromIndex = list.indexOf(fromId);
    var toIndex = list.indexOf(toId);
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return null;
    list.splice(fromIndex, 1);
    toIndex = list.indexOf(toId);
    list.splice(toIndex, 0, fromId);
    return list;
  }

  async function applyTasksReorder(payload) {
    try {
      if (state.tasksTab === 'download') await postTasksAction('download', 'reorder', payload);
      else if (state.tasksTab === 'concat') await postTasksAction('concat', 'reorder', payload);
      await refreshTasksPanel(true);
    } catch (e) {
      showToast((e && e.message) || 'Reorder failed', 'error');
    }
  }

  function onTasksBodyClick(event) {
    var expand = event.target.closest('[data-tasks-expand]');
    if (expand) {
      var projectId = expand.getAttribute('data-tasks-expand') || '';
      if (!projectId) return;
      var currentlyExpanded = state.tasksExpanded[projectId] !== false;
      state.tasksExpanded[projectId] = !currentlyExpanded;
      renderTasksPanel();
      return;
    }
    var selectBtn = event.target.closest('[data-tasks-select]');
    if (!selectBtn || state.tasksTab === 'convert') return;
    var kind = selectBtn.getAttribute('data-tasks-select') || '';
    var id = selectBtn.getAttribute('data-task-id') || '';
    if (!kind || !id) return;
    event.preventDefault();
    toggleTasksSelected(kind, id);
    renderTasksPanel();
  }

  function onTasksDragStart(event) {
    var row = event.target.closest('[draggable="true"]');
    if (!row || state.tasksTab === 'convert') return;
    var kind = row.getAttribute('data-task-kind') || '';
    var projectId = row.getAttribute('data-project-id') || '';
    var taskId = row.getAttribute('data-task-id') || '';
    state.tasksDrag = {
      kind: kind,
      projectId: projectId,
      taskId: taskId || projectId
    };
    row.classList.add('is-dragging');
    try {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', state.tasksDrag.taskId);
    } catch (e) {}
  }

  function onTasksDragOver(event) {
    if (!state.tasksDrag) return;
    var row = event.target.closest('[draggable="true"]');
    if (!row) return;
    var kind = row.getAttribute('data-task-kind') || '';
    if (kind !== state.tasksDrag.kind) return;
    if (kind === 'fragment' && row.getAttribute('data-project-id') !== state.tasksDrag.projectId) return;
    event.preventDefault();
    row.classList.add('is-drop-target');
  }

  function onTasksDragLeave(event) {
    var row = event.target.closest('.is-drop-target');
    if (row) row.classList.remove('is-drop-target');
  }

  async function onTasksDrop(event) {
    var drag = state.tasksDrag;
    var row = event.target.closest('[draggable="true"]');
    document.querySelectorAll('#mediaTasksBody .is-drop-target, #mediaTasksBody .is-dragging').forEach(function(node) {
      node.classList.remove('is-drop-target', 'is-dragging');
    });
    state.tasksDrag = null;
    if (!drag || !row) return;
    event.preventDefault();
    var kind = row.getAttribute('data-task-kind') || '';
    if (kind !== drag.kind) return;
    var targetProjectId = row.getAttribute('data-project-id') || '';
    var targetId = row.getAttribute('data-task-id') || targetProjectId;
    if (kind === 'project') {
      var projectOrder = moveIdInOrder(downloadProjectOrder(), drag.projectId, targetProjectId);
      if (projectOrder) await applyTasksReorder({ projectIds: projectOrder });
      return;
    }
    if (kind === 'fragment') {
      if (targetProjectId !== drag.projectId) return;
      var fragOrder = moveIdInOrder(downloadFragmentOrder(drag.projectId), drag.taskId, targetId);
      if (fragOrder) await applyTasksReorder({ projectId: drag.projectId, taskIds: fragOrder });
      return;
    }
    if (kind === 'concat') {
      var concatOrder = moveIdInOrder(concatQueueOrder(), drag.projectId, targetProjectId);
      if (concatOrder) await applyTasksReorder({ projectIds: concatOrder });
    }
  }

  function onTasksDragEnd() {
    state.tasksDrag = null;
    document.querySelectorAll('#mediaTasksBody .is-drop-target, #mediaTasksBody .is-dragging').forEach(function(node) {
      node.classList.remove('is-drop-target', 'is-dragging');
    });
  }

  function showProjectTasks() {
    openTasksModal('download');
  }

  async function openStreamerMacFolder(username) {
    var user = String(username || '').trim();
    if (!user) return;
    if (!state.macHelperAvailable || !state.localSessionId) {
      showToast('Start the HXYLIVE Mac helper to open local folders', 'error');
      return;
    }
    var match = null;
    (state.macFiles || []).some(function(file) {
      var relative = String((file && file.filename) || '').replace(/\\/g, '/');
      var top = relative.split('/')[0] || '';
      var identity = parseIdentityWithSource(top);
      var resolved = resolveLocalIdentityToProfileUsername(identity.username || top, identity.sourceType);
      if (resolved === user || (identity.username || top) === user) {
        match = {
          macRelativePath: relative,
          recordingId: file.recordingId || '',
          onMac: true,
          locations: 'mac'
        };
        return true;
      }
      return false;
    });
    if (!match) {
      showToast('No Mac videos found for this streamer yet', 'info');
      return;
    }
    await openLocally(match, { reveal: true });
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
    return String(profile.displayName || profile.display_name || '').trim()
      || String(profile.username || '').trim();
  }

  function profileStatusClass(profile) {
    // Follow flag feeds Sync reconcile; ID text stays one color (cards are not live remote truth).
    if (profile && profile.isFollowing) return ' is-followed';
    return ' is-unfollowed';
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

  function profileMatchesSourceFilter(profile, sources) {
    var selected = Array.isArray(sources) ? sources : [];
    if (!selected.length) return true;
    var types = profileSourceTypes(profile);
    return selected.some(function(source) {
      return types.indexOf(String(source || '').toLowerCase()) !== -1;
    });
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

  function profileMatchesLiveStatus(profile, statuses) {
    var selected = Array.isArray(statuses) ? statuses : [];
    if (!selected.length) return true;
    var bucket = profileLiveBucket(profile);
    return selected.indexOf(bucket) !== -1;
  }

  function profileMatchesRecordingFilter(profile, recordings) {
    var selected = Array.isArray(recordings) ? recordings : [];
    if (!selected.length) return true;
    var on = !!(profile && profile.autoRecord);
    return selected.some(function(value) {
      if (value === 'on') return on;
      if (value === 'off') return !on;
      return false;
    });
  }

  function visibleProfiles() {
    var query = String(state.profileSearch || '').trim().toLowerCase();
    return state.profiles.filter(function(profile) {
      if (!profileMatchesSourceFilter(profile, state.filterSources)) return false;
      if (!profileMatchesLiveStatus(profile, state.filterLiveStatuses)) return false;
      if (!profileMatchesRecordingFilter(profile, state.filterRecordings)) return false;
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
    // Keep in sync with .media-profile-carousel breakpoints in styles.css (5×3 = 15 slots).
    var width = window.innerWidth || document.documentElement.clientWidth || 1200;
    if (width <= 720) return 1;
    if (width <= 980) return 2;
    if (width <= 1100) return 3;
    if (width <= 1400) return 4;
    return 5;
  }

  function profilePageSize() {
    return Math.max(1, profileGridColumns() * PROFILE_PAGE_ROWS);
  }

  /** Page 1 reserves one slot for the pinned All streamers card. */
  function profileFirstPageStreamerCount(pageSize) {
    return Math.max(1, (Number(pageSize) || profilePageSize()) - 1);
  }

  function profileTotalPages(profileCount, pageSize) {
    var count = Number(profileCount) || 0;
    var size = Math.max(1, Number(pageSize) || profilePageSize());
    if (count <= 0) return 1;
    var first = profileFirstPageStreamerCount(size);
    if (count <= first) return 1;
    return 1 + Math.ceil((count - first) / size);
  }

  function profilePageSlice(profiles, page, pageSize) {
    var list = profiles || [];
    var size = Math.max(1, Number(pageSize) || profilePageSize());
    var first = profileFirstPageStreamerCount(size);
    var pageNum = Math.max(1, Number(page) || 1);
    if (pageNum <= 1) return list.slice(0, first);
    var offset = first + (pageNum - 2) * size;
    return list.slice(offset, offset + size);
  }

  function toggleFilterValue(list, value, knownValues) {
    var next = String(value || '').trim().toLowerCase();
    if (!next || knownValues.indexOf(next) === -1) return list.slice();
    var copy = list.slice();
    var index = copy.indexOf(next);
    if (index >= 0) copy.splice(index, 1);
    else copy.push(next);
    return copy;
  }

  function syncLegacyFilterAliases() {
    state.filterSource = state.filterSources.length === 1 ? state.filterSources[0] : 'all';
    state.filterLiveStatus = state.filterLiveStatuses.length === 1 ? state.filterLiveStatuses[0] : 'all';
    state.filterRecording = state.filterRecordings.length === 1 ? state.filterRecordings[0] : 'all';
  }

  function renderFilterButtons(options, selectedValues, dataAttr, buttonClass) {
    var selected = Array.isArray(selectedValues) ? selectedValues : [];
    var allActive = !selected.length;
    return options.map(function(option) {
      var active = allActive || selected.indexOf(option.value) !== -1;
      return (
        '<button type="button" class="' + buttonClass +
        (active && !allActive ? ' active' : '') +
        (allActive ? ' is-all' : '') +
        '" ' + dataAttr + '="' + escapeHtml(option.value) +
        '" title="' + escapeHtml(option.label) + '">' +
        escapeHtml(option.label) +
        '</button>'
      );
    }).join('');
  }

  function renderProfileSourceFilters() {
    var row = $('mediaProfileSourceFilters');
    if (!row) return;
    row.innerHTML = renderFilterButtons(
      PROFILE_SOURCE_OPTIONS,
      state.filterSources,
      'data-source',
      'media-profile-source-btn'
    );
  }

  function renderProfileStatusFilters() {
    var row = $('mediaProfileStatusFilters');
    if (!row) return;
    row.hidden = false;
    row.innerHTML = renderFilterButtons(
      PROFILE_LIVE_STATUS_OPTIONS,
      state.filterLiveStatuses,
      'data-live-status',
      'media-profile-status-btn'
    );
  }

  function renderProfileRecordingFilters() {
    var row = $('mediaProfileRecordingFilters');
    if (!row) return;
    row.innerHTML = renderFilterButtons(
      PROFILE_RECORDING_OPTIONS,
      state.filterRecordings,
      'data-recording-filter',
      'media-profile-status-btn'
    );
  }

  function pruneInvisibleStreamerSelection() {
    var keep = {};
    visibleProfiles().forEach(function(profile) {
      if (profile && profile.username && state.selectedStreamerIds[profile.username]) {
        keep[profile.username] = true;
      }
    });
    var changed = Object.keys(state.selectedStreamerIds).length !== Object.keys(keep).length;
    state.selectedStreamerIds = keep;
    if (!Object.keys(keep).length) state.filterProfile = '';
    else if (Object.keys(keep).length === 1) state.filterProfile = Object.keys(keep)[0];
    else state.filterProfile = '';
    return changed;
  }

  function applyProfileFilters(options) {
    options = options || {};
    if (Object.prototype.hasOwnProperty.call(options, 'sources')) {
      state.filterSources = options.sources.slice();
    }
    if (Object.prototype.hasOwnProperty.call(options, 'liveStatuses')) {
      state.filterLiveStatuses = options.liveStatuses.slice();
    }
    if (Object.prototype.hasOwnProperty.call(options, 'recordings')) {
      state.filterRecordings = options.recordings.slice();
    }
    syncLegacyFilterAliases();
    state.profilePage = 1;
    renderProfileSourceFilters();
    renderProfileStatusFilters();
    renderProfileRecordingFilters();
    renderProfileRecordingFilters();
    pruneInvisibleStreamerSelection();
    renderProfileCarousel();
    rebuildVisibleItems();
  }

  function setProfileSourceFilter(source) {
    var known = PROFILE_SOURCE_OPTIONS.map(function(option) { return option.value; });
    applyProfileFilters({
      sources: toggleFilterValue(state.filterSources, source, known)
    });
  }

  function setProfileLiveStatusFilter(status) {
    var known = PROFILE_LIVE_STATUS_OPTIONS.map(function(option) { return option.value; });
    applyProfileFilters({
      liveStatuses: toggleFilterValue(state.filterLiveStatuses, status, known)
    });
  }

  function setRecordingFilter(recording) {
    var known = PROFILE_RECORDING_OPTIONS.map(function(option) { return option.value; });
    applyProfileFilters({
      recordings: toggleFilterValue(state.filterRecordings, recording, known)
    });
  }

  function resetProfileFilters() {
    state.profileSearch = '';
    var search = $('mediaSearchInput');
    if (search) search.value = '';
    applyProfileFilters({ sources: [], liveStatuses: [], recordings: [] });
    clearStreamerSelection();
    clearVideoSelection();
  }

  function preferredSyncSource() {
    if (state.filterSources.length === 1) return state.filterSources[0];
    return 'all';
  }

  function syncCandidateSources() {
    if (state.filterSources.length) {
      return state.filterSources.filter(function(value) {
        return !!MEDIA_SYNC_CAPABLE_SOURCES[value];
      });
    }
    return syncableSourcesForMedia('all');
  }

  function closeSyncSitePicker() {
    var modal = $('mediaSyncSiteModal');
    if (!modal) return;
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('media-delete-open');
  }

  function openSyncSitePicker(sources) {
    return new Promise(function(resolve) {
      var modal = $('mediaSyncSiteModal');
      var list = $('mediaSyncSiteList');
      var cancel = $('mediaSyncSiteCancel');
      var hint = $('mediaSyncSiteHint');
      if (!modal || !list) {
        resolve('');
        return;
      }
      if (hint) {
        hint.textContent = sources.length > 1
          ? 'Several websites are available. Pick one site to reconcile.'
          : 'Pick a site to reconcile.';
      }
      list.innerHTML = sources.map(function(value) {
        return '<button type="button" class="media-picker-option" role="option" data-sync-source="' +
          escapeHtml(value) + '">' + escapeHtml(providerLabel(value)) + '</button>';
      }).join('');
      function finish(value) {
        list.removeEventListener('click', onPick);
        if (cancel) cancel.removeEventListener('click', onCancel);
        modal.removeEventListener('click', onBackdrop);
        closeSyncSitePicker();
        resolve(value || '');
      }
      function onPick(event) {
        var button = event.target.closest('[data-sync-source]');
        if (!button) return;
        finish(button.getAttribute('data-sync-source') || '');
      }
      function onCancel() { finish(''); }
      function onBackdrop(event) {
        if (event.target === modal) finish('');
      }
      list.addEventListener('click', onPick);
      if (cancel) cancel.addEventListener('click', onCancel);
      modal.addEventListener('click', onBackdrop);
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    });
  }

  async function pickSyncSourceInteractive() {
    var sources = syncCandidateSources();
    if (!sources.length) return '';
    if (sources.length === 1) return sources[0];
    return openSyncSitePicker(sources);
  }

  function closeStreamerSettingsPicker() {
    var modal = $('mediaStreamerSettingsPickModal');
    if (!modal) return;
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('media-delete-open');
  }

  function openStreamerSettingsPicker(usernames) {
    return new Promise(function(resolve) {
      var modal = $('mediaStreamerSettingsPickModal');
      var list = $('mediaStreamerSettingsPickList');
      var cancel = $('mediaStreamerSettingsPickCancel');
      if (!modal || !list) {
        resolve('');
        return;
      }
      list.innerHTML = usernames.map(function(username) {
        var profile = profileByUsername(username) || {};
        var label = profileLabel(profile) || username;
        return '<button type="button" class="media-picker-option" role="option" data-settings-username="' +
          escapeHtml(username) + '">' +
          '<span class="media-picker-option-title">' + escapeHtml(label) + '</span>' +
          '</button>';
      }).join('');
      function finish(value) {
        list.removeEventListener('click', onPick);
        if (cancel) cancel.removeEventListener('click', onCancel);
        modal.removeEventListener('click', onBackdrop);
        closeStreamerSettingsPicker();
        resolve(value || '');
      }
      function onPick(event) {
        var button = event.target.closest('[data-settings-username]');
        if (!button) return;
        finish(button.getAttribute('data-settings-username') || '');
      }
      function onCancel() { finish(''); }
      function onBackdrop(event) {
        if (event.target === modal) finish('');
      }
      list.addEventListener('click', onPick);
      if (cancel) cancel.addEventListener('click', onCancel);
      modal.addEventListener('click', onBackdrop);
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    });
  }

  async function openStreamerSettingsFromToolbar() {
    var selected = selectedStreamerList();
    if (!selected.length) {
      showToast('Select at least one streamer card first', 'info');
      return;
    }
    var username = selected[0];
    if (selected.length > 1) {
      username = await openStreamerSettingsPicker(selected);
      if (!username) return;
    }
    openProfileSettings(username);
  }

  var MEDIA_SYNC_CAPABLE_SOURCES = {
    twitch: true,
    chaturbate: true,
    stripchat: true,
    bilibili: true,
    youtube: true
  };
  var reconcileBusy = false;
  var profileCardPointer = { x: 0, y: 0, moved: false };
  var syncLogEntries = [];
  var SYNC_LOG_LIMIT = 80;

  function formatSyncLogTime(ts) {
    try {
      return new Date(ts || Date.now()).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
    } catch (e) {
      return '';
    }
  }

  function renderSyncLogToolbar() {
    var textEl = $('headerMediaLogText') || $('mediaFollowSyncLogText');
    var btn = $('headerMediaLogBtn') || $('mediaFollowSyncLogBtn');
    var latest = syncLogEntries.length ? syncLogEntries[0] : null;
    if (textEl) {
      textEl.textContent = latest ? latest.message : 'No activity yet';
    }
    if (btn) {
      btn.classList.toggle('is-error', !!(latest && latest.type === 'error'));
      btn.classList.toggle('is-success', !!(latest && latest.type === 'success'));
      btn.classList.toggle('is-info', !!(latest && latest.type === 'info'));
      btn.title = latest
        ? (formatSyncLogTime(latest.at) + ' — ' + latest.message)
        : 'Open Media activity log (recording, sync, download, and Mac helper)';
      btn.hidden = false;
      btn.removeAttribute('style');
      btn.setAttribute('aria-hidden', 'false');
    }
  }

  function renderSyncLogModalList() {
    var list = $('mediaSyncLogList');
    if (!list) return;
    if (!syncLogEntries.length) {
      list.innerHTML = '<div class="media-sync-log-empty">No Media activity yet.</div>';
      return;
    }
    list.innerHTML = syncLogEntries.map(function(entry) {
      return '<div class="media-sync-log-entry is-' + escapeHtml(entry.type || 'info') + '">' +
        '<span class="media-sync-log-time">' + escapeHtml(formatSyncLogTime(entry.at)) + '</span>' +
        '<span class="media-sync-log-message">' + escapeHtml(entry.message || '') + '</span>' +
      '</div>';
    }).join('');
  }

  function appendSyncLog(message, type) {
    var msg = String(message || '').trim();
    if (!msg) return;
    syncLogEntries.unshift({
      at: Date.now(),
      message: msg,
      type: type || 'info'
    });
    if (syncLogEntries.length > SYNC_LOG_LIMIT) {
      syncLogEntries.length = SYNC_LOG_LIMIT;
    }
    renderSyncLogToolbar();
    var modal = $('mediaSyncLogModal');
    if (modal && modal.style.display !== 'none') renderSyncLogModalList();
  }

  var appendMediaLog = appendSyncLog;

  function openSyncLogModal() {
    var modal = $('mediaSyncLogModal');
    renderSyncLogModalList();
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
    }
    document.body.classList.add('media-delete-open');
  }

  function closeSyncLogModal() {
    var modal = $('mediaSyncLogModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
    }
    if (!$('mediaFollowSyncModal') || $('mediaFollowSyncModal').style.display === 'none') {
      document.body.classList.remove('media-delete-open');
    }
  }

  function clearSyncLog() {
    syncLogEntries = [];
    renderSyncLogToolbar();
    renderSyncLogModalList();
  }

  function isProviderAuthError(err) {
    var msg = String((err && err.message) || err || '').toLowerCase();
    var status = Number((err && err.status) || 0);
    if (status === 401 || status === 403) return true;
    return /not logged in|login required|session expired|session missing|imported .+ session is not logged in|unauthorized|authentication/.test(msg);
  }

  function sleepMs(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
  }

  async function postProviderSessionFromMedia(source, payload) {
    var res = await fetch('/api/providers/' + encodeURIComponent(source) + '/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      cache: 'no-store'
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok || data.success === false) {
      throw new Error(data.detail || data.error || 'Session import failed');
    }
    return data;
  }

  async function fetchChromeProviderCookiesFromMedia(source) {
    var controller = new AbortController();
    var timer = setTimeout(function() { controller.abort(); }, 90000);
    try {
      var res = await fetch(MAC_HELPER_BASE + '/provider-cookies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sourceType: source }),
        cache: 'no-store',
        signal: controller.signal
      });
      var data = await res.json().catch(function() { return {}; });
      if (res.ok && data.ok && (data.cookieHeader || (data.cookies && data.cookies.length))) {
        return data;
      }
      var err = new Error(data.error || 'Mac helper could not read Chrome cookies');
      err.status = res.status;
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  async function queueChromeProviderImportFromMedia(source) {
    var snapRes = await fetch('/api/mac/helper/snapshot', { cache: 'no-store' });
    var snap = await snapRes.json().catch(function() { return {}; });
    if (!snapRes.ok || !snap || !snap.available || !snap.localSessionId) {
      throw new Error('Start the HXYLIVE Mac helper, log into this site in Google Chrome, then retry');
    }
    var res = await fetch('/api/mac/helper/import-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        localSessionId: snap.localSessionId,
        sourceType: source
      }),
      cache: 'no-store'
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      throw new Error(data.detail || data.error || 'Start the HXYLIVE Mac helper, then retry');
    }
    return data;
  }

  async function waitForQueuedChromeImportFromMedia(commandId) {
    var deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      var res = await fetch('/api/mac/helper/import-session/' + encodeURIComponent(commandId), {
        cache: 'no-store'
      });
      var data = await res.json().catch(function() { return {}; });
      if (res.status === 404) {
        throw new Error(data.detail || 'Import result expired; retry Sync');
      }
      if (!res.ok) {
        throw new Error(data.detail || data.error || 'Chrome import failed');
      }
      if (data.status === 'done') {
        if (data.success) return data;
        throw new Error(data.detail || 'Log into this site in Google Chrome, then retry Sync');
      }
      await sleepMs(700);
    }
    throw new Error('Chrome import timed out. Allow Keychain access if macOS asked, then retry.');
  }

  async function importProviderSessionFromChromeForSync(source) {
    var label = providerLabel(source);
    appendSyncLog('Connecting ' + label + ' — importing session from Chrome…', 'info');
    try {
      var cookies = await fetchChromeProviderCookiesFromMedia(source);
      await postProviderSessionFromMedia(source, {
        cookieHeader: cookies.cookieHeader || '',
        cookies: cookies.cookies || [],
        userAgent: cookies.userAgent || '',
        username: cookies.username || ''
      });
      appendSyncLog(label + ' connected — session imported', 'success');
      return true;
    } catch (directErr) {
      if (directErr && directErr.status) {
        appendSyncLog(label + ' connection failed: ' + (directErr.message || 'import error'), 'error');
        throw directErr;
      }
      appendSyncLog(label + ' direct cookie read unavailable — queuing Mac helper import…', 'info');
    }
    var queued = await queueChromeProviderImportFromMedia(source);
    await waitForQueuedChromeImportFromMedia(queued.commandId);
    appendSyncLog(label + ' connected — session imported via Mac helper', 'success');
    return true;
  }

  function profileCardHasTextSelection(card) {
    if (!card || !window.getSelection) return false;
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !String(selection).trim()) return false;
    if (selection.rangeCount < 1) return false;
    try {
      var node = selection.getRangeAt(0).commonAncestorContainer;
      return !!(node && card.contains(node.nodeType === 1 ? node : node.parentElement));
    } catch (e) {
      return false;
    }
  }

  function syncableSourcesForMedia(requested) {
    var source = String(requested || '').trim().toLowerCase();
    if (!source || source === 'all') {
      return PROFILE_SOURCE_OPTIONS
        .map(function(option) { return option.value; })
        .filter(function(value) { return !!MEDIA_SYNC_CAPABLE_SOURCES[value]; });
    }
    if (!MEDIA_SYNC_CAPABLE_SOURCES[source]) return [];
    return [source];
  }

  var reconcileState = {
    sourceType: '',
    filter: '',
    cardOnly: [],
    remoteOnly: [],
    both: [],
    summary: '',
    loading: false
  };

  function closeFollowReconcile() {
    var modal = $('mediaFollowSyncModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
    }
    if (!$('mediaSyncLogModal') || $('mediaSyncLogModal').style.display === 'none') {
      document.body.classList.remove('media-delete-open');
    }
    reconcileState.sourceType = '';
    reconcileState.filter = '';
    reconcileState.cardOnly = [];
    reconcileState.remoteOnly = [];
    reconcileState.both = [];
    reconcileState.summary = '';
    reconcileState.loading = false;
  }

  function reconcileBucketRows(bucket) {
    if (bucket === 'cardOnly') return reconcileState.cardOnly;
    if (bucket === 'remoteOnly') return reconcileState.remoteOnly;
    if (bucket === 'both') return reconcileState.both;
    return [];
  }

  function reconcileSelectedRows(bucket) {
    return reconcileBucketRows(bucket).filter(function(row) { return !!row.selected; });
  }

  function reconcileDisplayName(item) {
    return String((item && (item.displayName || item.username)) || '').trim();
  }

  function reconcileShowUsername(item) {
    var displayName = reconcileDisplayName(item);
    var username = String((item && item.username) || '').trim();
    if (!username) return '';
    if (displayName && displayName.toLowerCase() === username.toLowerCase()) return '';
    return username;
  }

  function reconcileChannelUrl(item) {
    var url = String((item && (item.channelUrl || item.channel_url)) || '').trim();
    if (!url) {
      url = channelUrlForSource(reconcileState.sourceType, item && item.username);
    }
    if (!url) return '';
    var source = normalizeSourceMarker(reconcileState.sourceType);
    if (source === 'youtube' && !/\/live\/?$/i.test(url)) {
      url = url.replace(/\/+$/, '') + '/live';
    }
    return url;
  }

  function reconcileAvatarHtml(item, channelUrl) {
    var label = reconcileDisplayName(item) || String(item.username || '');
    var imageUrl = String((item && (item.profileImageUrl || item.profile_image_url)) || '').trim();
    var avatarInner;
    if (imageUrl) {
      avatarInner = '<div class="media-follow-sync-avatar">' +
        '<img src="' + escapeHtml(imageUrl) + '" alt="" loading="lazy" referrerpolicy="no-referrer" ' +
        'onerror="this.style.display=\'none\'; this.parentElement.classList.add(\'missing-thumb\');">' +
        '<div class="media-follow-sync-avatar-placeholder" aria-hidden="true"><span>' + escapeHtml(firstLetter(label)) + '</span></div>' +
        '</div>';
    } else {
      avatarInner = '<div class="media-follow-sync-avatar missing-thumb">' +
        '<div class="media-follow-sync-avatar-placeholder"><span>' + escapeHtml(firstLetter(label)) + '</span></div>' +
        '</div>';
    }
    if (!channelUrl) return avatarInner;
    return '<a class="media-follow-sync-open" href="' + escapeHtml(channelUrl) +
      '" target="_blank" rel="noopener" data-reconcile-open-channel="1" title="Open on site">' +
      avatarInner + '</a>';
  }

  function updateReconcileStatus() {
    var resultBox = $('mediaFollowSyncResult');
    var summary = String(reconcileState.summary || '').trim();
    if (!resultBox) return;
    if (summary) {
      var failedOnly = /failed/i.test(summary) && !/succeeded/i.test(summary);
      var mixed = /succeeded/i.test(summary) && /failed/i.test(summary);
      resultBox.hidden = false;
      resultBox.className = 'media-reconcile-result' +
        (failedOnly ? ' is-error' : (mixed ? ' is-mixed' : ' is-success'));
      resultBox.innerHTML =
        '<span class="media-reconcile-result-label">Last action</span>' +
        '<span class="media-reconcile-result-text">' + escapeHtml(summary) + '</span>';
    } else {
      resultBox.hidden = true;
      resultBox.className = 'media-reconcile-result';
      resultBox.innerHTML = '';
    }
  }

  function selectedReconcileUsernames() {
    var keys = {};
    ['cardOnly', 'remoteOnly', 'both'].forEach(function(bucket) {
      reconcileBucketRows(bucket).forEach(function(row) {
        if (row && row.selected && row.username) {
          keys[String(row.username).toLowerCase()] = true;
        }
      });
    });
    return keys;
  }

  function mapReconcileRows(items, selectedKeys) {
    selectedKeys = selectedKeys || {};
    return (items || []).map(function(item) {
      var username = String(item.username || '').trim();
      return {
        username: username,
        profileUsername: String(item.profileUsername || item.profile_username || item.username || '').trim(),
        displayName: String(item.displayName || item.display_name || item.username || '').trim(),
        profileImageUrl: String(item.profileImageUrl || item.profile_image_url || '').trim(),
        channelUrl: String(item.channelUrl || item.channel_url || '').trim(),
        isOnline: !!item.isOnline,
        viewers: item.viewers,
        autoRecord: !!item.autoRecord,
        selected: !!(username && selectedKeys[username.toLowerCase()]),
        result: '',
        resultOk: null
      };
    }).filter(function(item) { return !!item.username; });
  }

  function renderReconcileRow(item, bucket, index) {
    var displayName = reconcileDisplayName(item);
    var label = displayName.toLowerCase();
    var needle = String(reconcileState.filter || '').trim().toLowerCase();
    if (needle && label.indexOf(needle) === -1) return '';
    var metaParts = [];
    if (item.isOnline) metaParts.push('live');
    var resultHtml = item.result
      ? '<span class="media-reconcile-row-result' +
          (item.resultOk === false ? ' is-error' : (item.resultOk ? ' is-ok' : '')) +
        '">' + escapeHtml(item.result) + '</span>'
      : '';
    var channelUrl = reconcileChannelUrl(item);
    var nameText = escapeHtml(displayName || item.username);
    var nameHtml = channelUrl
      ? '<a class="media-follow-sync-card-name media-follow-sync-open" href="' + escapeHtml(channelUrl) +
        '" target="_blank" rel="noopener" data-reconcile-open-channel="1" title="Open on site">' +
        nameText + '</a>'
      : '<span class="media-follow-sync-card-name">' + nameText + '</span>';
    return '<div class="media-follow-sync-card' + (item.selected ? ' is-selected' : '') +
      '" role="option" tabindex="0" aria-selected="' + (item.selected ? 'true' : 'false') +
      '" data-reconcile-bucket="' + escapeHtml(bucket) +
      '" data-reconcile-index="' + index + '">' +
      reconcileAvatarHtml(item, channelUrl) +
      '<span class="media-follow-sync-card-text">' +
      nameHtml +
      (metaParts.length || resultHtml
        ? '<span class="media-follow-sync-card-meta">' +
            (metaParts.length
              ? '<span class="media-follow-sync-card-status">' + escapeHtml(metaParts.join(' · ')) + '</span>'
              : '') +
            resultHtml +
          '</span>'
        : '') +
      '</span></div>';
  }

  function renderReconcileSection(bucket, title, actions) {
    var rows = reconcileBucketRows(bucket);
    var selectedCount = reconcileSelectedRows(bucket).length;
    var listHtml = '';
    rows.forEach(function(item, index) {
      listHtml += renderReconcileRow(item, bucket, index);
    });
    if (!listHtml && String(reconcileState.filter || '').trim()) {
      listHtml = '<div class="media-follow-sync-empty">No channels match this filter.</div>';
    } else if (!listHtml) {
      listHtml = '<div class="media-follow-sync-empty">None</div>';
    }
    var actionHtml = (actions || []).map(function(action) {
      var label = action.label + ' (' + selectedCount + ')';
      return '<button type="button" class="media-delete-cancel" data-reconcile-action="' +
        escapeHtml(action.id) + '" data-reconcile-bucket="' + escapeHtml(bucket) + '"' +
        (selectedCount ? '' : ' disabled') + '>' +
        escapeHtml(label) + '</button>';
    }).join('');
    return '<section class="media-reconcile-section" data-bucket="' + escapeHtml(bucket) + '">' +
      '<div class="media-reconcile-section-head">' +
        '<h3>' + escapeHtml(title) + ' (' + rows.length + ')</h3>' +
        '<div class="media-reconcile-section-actions">' + actionHtml + '</div>' +
      '</div>' +
      '<div class="media-follow-sync-list media-reconcile-list" role="listbox" aria-multiselectable="true">' +
        listHtml +
      '</div>' +
    '</section>';
  }

  function reconcileSectionActions(bucket) {
    if (bucket === 'cardOnly') {
      return [
        { id: 'delete-cards', label: 'Delete cards' },
        { id: 'follow', label: 'Follow on site' }
      ];
    }
    if (bucket === 'remoteOnly') {
      return [
        { id: 'create-cards', label: 'Create cards' },
        { id: 'unfollow', label: 'Unfollow on site' }
      ];
    }
    if (bucket === 'both') {
      return [
        { id: 'unfollow', label: 'Unfollow on site' },
        { id: 'delete-cards', label: 'Delete cards' }
      ];
    }
    return [];
  }

  function updateReconcileSectionActionLabels(bucket) {
    var sections = $('mediaFollowSyncSections');
    if (!sections) return;
    var selectedCount = reconcileSelectedRows(bucket).length;
    reconcileSectionActions(bucket).forEach(function(action) {
      var btn = sections.querySelector(
        '[data-reconcile-action="' + action.id + '"][data-reconcile-bucket="' + bucket + '"]'
      );
      if (!btn) return;
      btn.textContent = action.label + ' (' + selectedCount + ')';
      btn.disabled = !selectedCount;
    });
  }

  function renderFollowReconcile() {
    var sections = $('mediaFollowSyncSections');
    if (!sections) return;
    var scrollByBucket = {};
    sections.querySelectorAll('.media-reconcile-section[data-bucket]').forEach(function(section) {
      var bucket = section.getAttribute('data-bucket') || '';
      var list = section.querySelector('.media-reconcile-list');
      if (bucket && list) scrollByBucket[bucket] = list.scrollTop;
    });
    var outerScroll = sections.scrollTop;
    sections.innerHTML =
      renderReconcileSection('cardOnly', 'Has card, not followed on site', reconcileSectionActions('cardOnly')) +
      renderReconcileSection('remoteOnly', 'Followed on site, no card', reconcileSectionActions('remoteOnly')) +
      renderReconcileSection('both', 'Has card and followed', reconcileSectionActions('both'));
    sections.scrollTop = outerScroll;
    Object.keys(scrollByBucket).forEach(function(bucket) {
      var list = sections.querySelector(
        '.media-reconcile-section[data-bucket="' + bucket + '"] .media-reconcile-list'
      );
      if (list) list.scrollTop = scrollByBucket[bucket];
    });
    updateReconcileStatus();
  }

  function applyReconcileSnapshot(sourceType, data, options) {
    options = options || {};
    var selectedKeys = options.preserveSelection ? selectedReconcileUsernames() : {};
    reconcileState.sourceType = sourceType;
    reconcileState.cardOnly = mapReconcileRows(data && data.cardOnly, selectedKeys);
    reconcileState.remoteOnly = mapReconcileRows(data && data.remoteOnly, selectedKeys);
    reconcileState.both = mapReconcileRows(data && data.both, selectedKeys);
    if (!options.preserveSelection) reconcileState.summary = '';
    renderFollowReconcile();
  }

  async function fetchFollowReconcile(sourceType) {
    var res = await fetch(
      '/api/providers/' + encodeURIComponent(sourceType) + '/following/reconcile',
      { method: 'GET', cache: 'no-store' }
    );
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      var err = new Error(data.detail || data.message || ('Failed to load reconcile for ' + providerLabel(sourceType)));
      err.status = res.status;
      throw err;
    }
    if (data.trusted === false) {
      throw new Error(data.skippedReason || data.message || ('Reconcile skipped for ' + providerLabel(sourceType)));
    }
    return data;
  }

  async function openFollowReconcile(sourceType) {
    var source = normalizeSourceMarker(sourceType) || String(sourceType || '').trim().toLowerCase();
    if (!source || !MEDIA_SYNC_CAPABLE_SOURCES[source]) {
      appendSyncLog(
        providerLabel(source) + ' does not support follow sync. Import from Chrome in Settings if needed.',
        'error'
      );
      return;
    }
    if (reconcileBusy) {
      appendSyncLog('Follow sync already running', 'info');
      return;
    }
    reconcileBusy = true;
    reconcileState.loading = true;
    var label = providerLabel(source);
    try {
      appendSyncLog('Checking ' + label + ' connection…', 'info');
      var data;
      try {
        data = await fetchFollowReconcile(source);
        appendSyncLog(label + ' connected — loading reconcile', 'success');
      } catch (firstErr) {
        if (!isProviderAuthError(firstErr)) throw firstErr;
        appendSyncLog(
          label + ' not connected: ' + (firstErr.message || 'login required') + ' — trying Import from Chrome…',
          'info'
        );
        await importProviderSessionFromChromeForSync(source);
        appendSyncLog('Retrying ' + label + ' reconcile after import…', 'info');
        data = await fetchFollowReconcile(source);
        appendSyncLog(label + ' connected — reconcile ready', 'success');
      }
      var title = $('mediaFollowSyncTitle');
      var hint = $('mediaFollowSyncHint');
      var search = $('mediaFollowSyncSearch');
      if (title) title.textContent = 'Reconcile ' + label;
      if (hint) {
        hint.textContent =
          'Compare local cards with remote follows. Lists refresh after each successful action.';
      }
      if (search) search.value = '';
      reconcileState.filter = '';
      applyReconcileSnapshot(source, data);
      var modal = $('mediaFollowSyncModal');
      if (modal) {
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
      }
      document.body.classList.add('media-delete-open');
      if (search) search.focus();
    } catch (e) {
      appendSyncLog(e.message || 'Follow reconcile failed', 'error');
      closeFollowReconcile();
    } finally {
      reconcileBusy = false;
      reconcileState.loading = false;
    }
  }

  async function refreshFollowReconcileQuiet() {
    var source = reconcileState.sourceType;
    if (!source) return;
    try {
      var data = await fetchFollowReconcile(source);
      var keepSummary = reconcileState.summary;
      applyReconcileSnapshot(source, data, { preserveSelection: true });
      reconcileState.summary = keepSummary;
      updateReconcileStatus();
    } catch (e) {
      appendSyncLog(e.message || 'Reconcile refresh failed', 'error');
    }
  }

  async function startMediaFollowSync(preferredSource) {
    var requested = String(preferredSource || '').trim().toLowerCase();
    if (!requested) {
      var candidates = syncCandidateSources();
      if (!candidates.length) {
        appendSyncLog('No sync-capable site is available', 'error');
        return;
      }
      if (candidates.length === 1) {
        requested = candidates[0];
      } else {
        requested = await pickSyncSourceInteractive();
        if (!requested) return;
      }
    }
    if (!MEDIA_SYNC_CAPABLE_SOURCES[requested]) {
      appendSyncLog(
        providerLabel(requested) + ' does not support follow sync. Import from Chrome in Settings if needed.',
        'error'
      );
      return;
    }
    openFollowReconcile(requested);
  }

  function consumeReconcileQuery() {
    try {
      var params = new URLSearchParams(window.location.search || '');
      var raw = params.get('reconcile');
      if (!raw) return;
      var source = normalizeSourceMarker(raw) || String(raw).trim().toLowerCase();
      params.delete('reconcile');
      var query = params.toString();
      var next = window.location.pathname + (query ? '?' + query : '') + (window.location.hash || '');
      window.history.replaceState({}, '', next);
      if (source) openFollowReconcile(source);
    } catch (e) {}
  }

  async function reconcileCreateCard(row) {
    var sourceType = reconcileState.sourceType;
    var res = await fetch('/api/media-profiles/ensure-card', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: row.username,
        sourceType: sourceType,
        displayName: row.displayName || row.username,
        profileImageUrl: row.profileImageUrl || '',
        channelUrl: row.channelUrl || channelUrlForSource(sourceType, row.username),
        autoRecord: false
      })
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) throw new Error(data.detail || data.message || 'Create card failed');
    return data;
  }

  async function reconcileDeleteCard(row) {
    var profileUsername = String(row.profileUsername || row.username || '').trim();
    if (!profileUsername) throw new Error('Missing profile username');
    var res = await fetch('/api/media-profiles/' + encodeURIComponent(profileUsername), {
      method: 'DELETE'
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) throw new Error(data.detail || data.message || 'Delete card failed');
    return data;
  }

  async function reconcileFollowRemote(row, follow) {
    var action = follow ? 'follow' : 'unfollow';
    var username = String((row && row.username) || '').trim();
    if (!username) throw new Error('Missing username');
    var sourceType = reconcileState.sourceType;
    // Twitch GQL mutations need browser integrity; VPS always fails. Prefer
    // localhost Mac Helper, then VPS→Helper command queue (CORS-safe).
    if (sourceType === 'twitch') {
      try {
        await ensureMacHelperSession({ force: true });
        var helperData = await helperPostLocal('/provider/follow', {
          localSessionId: state.localSessionId,
          sourceType: 'twitch',
          username: username,
          follow: !!follow
        }, 45000);
        if (!helperData || helperData.ok === false) {
          throw new Error((helperData && helperData.error) || (action + ' failed on Mac'));
        }
        var confirmRes = await fetch(
          '/api/providers/twitch/' + action + '/' + encodeURIComponent(username),
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ remoteCompleted: true })
          }
        );
        var confirmData = await confirmRes.json().catch(function() { return {}; });
        if (!confirmRes.ok) {
          throw new Error(confirmData.detail || confirmData.message || ('Local ' + action + ' sync failed'));
        }
        return confirmData;
      } catch (helperErr) {
        appendSyncLog(
          'Mac Helper direct Twitch ' + action + ' unavailable — queuing via VPS (' +
          ((helperErr && helperErr.message) || 'error') + ')',
          'info'
        );
        return await queueTwitchFollowViaMacHelper(username, follow);
      }
    }
    var res = await fetch(
      '/api/providers/' + encodeURIComponent(sourceType) + '/' +
        action + '/' + encodeURIComponent(username),
      { method: 'POST' }
    );
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) throw new Error(data.detail || data.message || (action + ' failed'));
    return data;
  }

  async function queueTwitchFollowViaMacHelper(username, follow) {
    var action = follow ? 'follow' : 'unfollow';
    var snapRes = await fetch('/api/mac/helper/snapshot', { cache: 'no-store' });
    var snap = await snapRes.json().catch(function() { return {}; });
    if (!snapRes.ok || !snap || !snap.available || !snap.localSessionId) {
      throw new Error('Start the HXYLIVE Mac helper, then retry Twitch ' + action);
    }
    state.localSessionId = snap.localSessionId;
    state.macHelperAvailable = true;
    var res = await fetch('/api/mac/helper/provider-follow', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        localSessionId: snap.localSessionId,
        sourceType: 'twitch',
        username: username,
        follow: !!follow
      }),
      cache: 'no-store'
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      throw new Error(data.detail || data.error || ('Could not queue Twitch ' + action));
    }
    var commandId = data.commandId || '';
    if (!commandId) throw new Error('Mac Helper did not return a follow command id');
    appendSyncLog('Queued Twitch ' + action + ' for ' + username + ' on Mac Helper…', 'info');
    return await waitForQueuedTwitchFollow(commandId, action);
  }

  async function waitForQueuedTwitchFollow(commandId, action) {
    var deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      var res = await fetch('/api/mac/helper/provider-follow/' + encodeURIComponent(commandId), {
        cache: 'no-store'
      });
      var data = await res.json().catch(function() { return {}; });
      if (res.status === 404) {
        throw new Error(data.detail || 'Follow result expired; retry Sync');
      }
      if (!res.ok) {
        throw new Error(data.detail || data.error || ('Twitch ' + action + ' failed'));
      }
      if (data.status === 'done') {
        if (data.success) return data;
        throw new Error(data.detail || ('Twitch ' + action + ' failed on Mac'));
      }
      await sleepMs(700);
    }
    throw new Error('Twitch ' + action + ' timed out waiting for Mac Helper');
  }

  async function ensureMacHelperSession(options) {
    options = options || {};
    if (!options.force && state.localSessionId && state.macHelperAvailable) {
      return state.localSessionId;
    }
    var timeout = helperRequestTimeout(2500);
    try {
      var res = await fetch(MAC_HELPER_BASE + '/health', {
        cache: 'no-store',
        signal: timeout.signal
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok || !data.localSessionId) {
        throw new Error('Start the HXYLIVE Mac helper, then retry');
      }
      state.localSessionId = data.localSessionId;
      state.macHelperAvailable = true;
      return state.localSessionId;
    } finally {
      timeout.clear();
    }
  }

  async function runReconcileAction(actionId, bucket) {
    var rows = reconcileSelectedRows(bucket);
    if (!rows.length) {
      appendSyncLog('Select at least one channel first', 'info');
      return;
    }
    if (reconcileBusy) {
      appendSyncLog('Follow sync already running', 'info');
      return;
    }
    reconcileBusy = true;
    var ok = 0;
    var failed = 0;
    try {
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        try {
          if (actionId === 'create-cards') {
            await reconcileCreateCard(row);
            row.result = 'Card created';
            row.resultOk = true;
          } else if (actionId === 'delete-cards') {
            await reconcileDeleteCard(row);
            row.result = 'Card deleted';
            row.resultOk = true;
          } else if (actionId === 'follow') {
            await reconcileFollowRemote(row, true);
            row.result = 'Followed on site';
            row.resultOk = true;
          } else if (actionId === 'unfollow') {
            await reconcileFollowRemote(row, false);
            row.result = 'Unfollowed on site';
            row.resultOk = true;
          } else {
            continue;
          }
          ok += 1;
        } catch (e) {
          row.result = e.message || 'Action failed';
          row.resultOk = false;
          failed += 1;
        }
      }
      reconcileState.summary =
        (ok ? ok + ' succeeded' : '') +
        (ok && failed ? ', ' : '') +
        (failed ? failed + ' failed' : '');
      renderFollowReconcile();
      if (ok) {
        await loadMediaLibrary({ forceLiveRefresh: true });
        await refreshFollowReconcileQuiet();
      }
      appendSyncLog(
        reconcileState.summary || 'No changes',
        failed && !ok ? 'error' : (failed ? 'info' : 'success')
      );
    } finally {
      reconcileBusy = false;
    }
  }

  function bindFollowReconcileControls() {
    var sections = $('mediaFollowSyncSections');
    var search = $('mediaFollowSyncSearch');
    var cancelBtn = $('mediaFollowSyncCancel');
    var modal = $('mediaFollowSyncModal');
    var syncBtn = $('mediaFollowSyncBtn');
    var logBtn = $('headerMediaLogBtn') || $('mediaFollowSyncLogBtn');
    var logModal = $('mediaSyncLogModal');
    var logClose = $('mediaSyncLogClose');
    var logClear = $('mediaSyncLogClear');
    if (sections) {
      function toggleReconcileCard(card) {
        if (!card || !sections.contains(card)) return;
        var bucket = card.getAttribute('data-reconcile-bucket') || '';
        var index = parseInt(card.getAttribute('data-reconcile-index'), 10);
        var rows = reconcileBucketRows(bucket);
        if (!isFinite(index) || !rows[index]) return;
        rows[index].selected = !rows[index].selected;
        // Update selection in place — full re-render resets list scroll and
        // focus-driven scrollIntoView jumps the "Followed on site" pane.
        card.classList.toggle('is-selected', !!rows[index].selected);
        card.setAttribute('aria-selected', rows[index].selected ? 'true' : 'false');
        updateReconcileSectionActionLabels(bucket);
      }
      sections.addEventListener('mousedown', function(event) {
        if (event.button !== 0) return;
        var openLink = event.target && event.target.closest
          ? event.target.closest('[data-reconcile-open-channel]')
          : null;
        if (openLink && sections.contains(openLink)) return;
        var card = event.target && event.target.closest
          ? event.target.closest('[data-reconcile-bucket][data-reconcile-index]')
          : null;
        if (!card || !sections.contains(card)) return;
        // Keep focus from scrolling overflow lists when selecting a row.
        event.preventDefault();
      });
      sections.addEventListener('click', function(event) {
        var actionBtn = event.target && event.target.closest
          ? event.target.closest('[data-reconcile-action]')
          : null;
        if (actionBtn && sections.contains(actionBtn)) {
          event.preventDefault();
          if (actionBtn.disabled) return;
          runReconcileAction(
            actionBtn.getAttribute('data-reconcile-action') || '',
            actionBtn.getAttribute('data-reconcile-bucket') || ''
          );
          return;
        }
        var openLink = event.target && event.target.closest
          ? event.target.closest('[data-reconcile-open-channel]')
          : null;
        if (openLink && sections.contains(openLink)) {
          // Avatar / name open the live site; do not toggle selection.
          event.stopPropagation();
          return;
        }
        var card = event.target && event.target.closest
          ? event.target.closest('[data-reconcile-bucket][data-reconcile-index]')
          : null;
        if (!card || !sections.contains(card)) return;
        event.preventDefault();
        toggleReconcileCard(card);
      });
      sections.addEventListener('keydown', function(event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        var openLink = event.target && event.target.closest
          ? event.target.closest('[data-reconcile-open-channel]')
          : null;
        if (openLink) return;
        var card = event.target && event.target.closest
          ? event.target.closest('[data-reconcile-bucket][data-reconcile-index]')
          : null;
        if (!card || !sections.contains(card) || event.target !== card) return;
        event.preventDefault();
        toggleReconcileCard(card);
      });
    }
    if (search) {
      search.addEventListener('input', function() {
        reconcileState.filter = search.value || '';
        renderFollowReconcile();
      });
    }
    if (cancelBtn) cancelBtn.addEventListener('click', closeFollowReconcile);
    if (syncBtn) {
      syncBtn.addEventListener('click', function() {
        startMediaFollowSync();
      });
    }
    function bindHeaderLogButton() {
      var btn = $('headerMediaLogBtn');
      if (!btn || btn.dataset.logBound === '1') return;
      btn.dataset.logBound = '1';
      btn.addEventListener('click', function(event) {
        event.preventDefault();
        openSyncLogModal();
      });
      btn.hidden = false;
      btn.removeAttribute('style');
      btn.setAttribute('aria-hidden', 'false');
    }
    bindHeaderLogButton();
    document.addEventListener('hxylive:header-ready', bindHeaderLogButton);
    if (logBtn && logBtn.id !== 'headerMediaLogBtn') {
      logBtn.addEventListener('click', openSyncLogModal);
    }
    if (logClose) logClose.addEventListener('click', closeSyncLogModal);
    if (logClear) logClear.addEventListener('click', clearSyncLog);
    if (modal) {
      modal.addEventListener('click', function(event) {
        if (event.target === modal) closeFollowReconcile();
      });
    }
    if (logModal) {
      logModal.addEventListener('click', function(event) {
        if (event.target === logModal) closeSyncLogModal();
      });
    }
    renderSyncLogToolbar();
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
    // Chaturbate live webcam (/riw/) and room posters (/ri/) — both look like
    // live screenshots, not the model's self-set summary/face photo.
    if (/thumb\.live\.mmcdn\.com\/riw\//i.test(u)) return true;
    if (/thumb\.live\.mmcdn\.com\/ri\//i.test(u)) return true;
    if (/roomimg\.stream\.highwebmedia\.com\/ri\//i.test(u)) return true;
    if (/i\.ytimg\.com\//i.test(u) || /ytimg\.com\/vi\//i.test(u)) return true;
    if (/doppiocdn\./i.test(u) && /\/snapshot\//i.test(u)) return true;
    if (/(doppiocdn\.|static-proxy\.strpst\.com)/i.test(u) && /\/previews\//i.test(u)) return true;
    // Twitch Helix stream preview frame — not the profile face photo.
    if (/previews-ttv/i.test(u) || /live_user_/i.test(u)) return true;
    return false;
  }

  function rewriteChaturbatePosterUrl(url) {
    var raw = String(url || '').trim();
    if (!raw) return '';
    // Legacy roomimg host has an expired browser cert and 301s to mmcdn /ri/.
    var match = raw.match(/^https?:\/\/roomimg\.stream\.highwebmedia\.com\/ri\/([A-Za-z0-9_]+)\.jpg(?:\?.*)?$/i);
    if (match) return 'https://thumb.live.mmcdn.com/ri/' + match[1].toLowerCase() + '.jpg';
    return raw;
  }

  function isRecordingThumbAvatarUrl(url) {
    var u = String(url || '');
    return /\/thumb\?/i.test(u) || /\/api\/mac\/helper\/thumb\?/i.test(u);
  }

  function profileImageUrl(profile) {
    if (!profile) return '';
    var url = rewriteChaturbatePosterUrl(
      String(profile.profileImageUrl || profile.profile_image_url || '').trim()
    );
    if (url.indexOf('http://') === 0) url = 'https://' + url.slice(7);
    // Never use live covers or recording thumbs as the circular face.
    if (isLiveCoverAvatarUrl(url) || isRecordingThumbAvatarUrl(url)) return '';
    return url;
  }

  function localVideoAvatarUrl(meta) {
    var url = rewriteChaturbatePosterUrl(
      String((meta && (meta.profileImageUrl || meta.profile_image_url)) || '').trim()
    );
    if (!url || isLiveCoverAvatarUrl(url) || isRecordingThumbAvatarUrl(url)) return '';
    return url;
  }

  function localLiveMetaIsUseful(meta, sourceType) {
    if (!meta || typeof meta !== 'object') return false;
    if (localVideoAvatarUrl(meta)) return true;
    // Chaturbate: followers alone are not enough — keep retrying until a face lands.
    if (normalizeSourceMarker(sourceType) === 'chaturbate') return false;
    if (meta.followers !== null && meta.followers !== undefined) return true;
    // Stripchat never exposes follower counts; channel URL means the lookup ran.
    if (normalizeSourceMarker(sourceType) === 'stripchat' && (meta.channelUrl || meta.channel_url)) {
      return true;
    }
    return false;
  }

  function applyLocalVideoLiveMeta(profile, meta) {
    if (!profile || !meta) return false;
    var changed = false;
    var image = localVideoAvatarUrl(meta);
    if (image && profileImageUrl(profile) !== image) {
      profile.profileImageUrl = image;
      profile.profile_image_url = image;
      changed = true;
    }
    var displayName = String(meta.displayName || meta.display_name || '').trim();
    var currentName = String(profile.displayName || profile.display_name || '').trim();
    if (displayName && (!currentName || currentName === profile.username) && currentName !== displayName) {
      profile.displayName = displayName;
      changed = true;
    }
    if (typeof meta.isOnline === 'boolean' && !!profile.isOnline !== meta.isOnline) {
      profile.isOnline = meta.isOnline;
      profile.viewers = meta.isOnline ? Number(meta.viewers || 0) : 0;
      changed = true;
    } else if (meta.isOnline && Number(meta.viewers || 0) !== Number(profile.viewers || 0)) {
      profile.viewers = Number(meta.viewers || 0);
      changed = true;
    }
    if (meta.followers !== undefined && meta.followers !== profile.followers) {
      profile.followers = meta.followers;
      changed = true;
    }
    var roomStatus = String(meta.roomStatus || meta.room_status || '').trim();
    if (roomStatus && String(profile.roomStatus || profile.room_status || '') !== roomStatus) {
      profile.roomStatus = roomStatus;
      profile.room_status = roomStatus;
      changed = true;
    }
    var channelUrl = String(meta.channelUrl || meta.channel_url || '').trim();
    if (channelUrl && !profile.channelUrl) {
      profile.channelUrl = channelUrl;
      changed = true;
    }
    var sourceType = normalizeSourceMarker(meta.sourceType || meta.source_type);
    if (sourceType && !normalizeSourceMarker(profile.sourceType || profile.source_type)) {
      profile.sourceType = sourceType;
      profile.source_type = sourceType;
      if (!profile.channelUrl) profile.channelUrl = channelUrlForSource(sourceType, profile.username);
      changed = true;
    }
    return changed;
  }

  function fetchLocalVideoLiveMeta(profile) {
    var sourceType = normalizeSourceMarker(profile.sourceType || profile.source_type);
    var statusUrl = '/api/model/' + encodeURIComponent(profile.username) + '/status';
    if (sourceType) statusUrl += '?source=' + encodeURIComponent(sourceType);
    var statusPromise = fetch(statusUrl, { cache: 'no-store', credentials: 'same-origin' })
      .then(function(res) {
        if (!res.ok) throw new Error('status');
        return res.json();
      });
    // Chaturbate Media/Watch face photos come from chatvideocontext; Discover
    // exposes the same images via profile-images — merge so local cards match.
    if (sourceType === 'chaturbate' || !sourceType) {
      var imagesUrl = '/api/discover/profile-images?source=chaturbate&usernames=' +
        encodeURIComponent(String(profile.username || '').toLowerCase());
      var imagesPromise = fetch(imagesUrl, { cache: 'no-store', credentials: 'same-origin' })
        .then(function(res) { return res.ok ? res.json() : { images: {} }; })
        .then(function(data) { return data.images || {}; })
        .catch(function() { return {}; });
      return Promise.all([statusPromise, imagesPromise]).then(function(pair) {
        var status = pair[0] && typeof pair[0] === 'object' ? pair[0] : {};
        var images = pair[1] || {};
        var key = String(profile.username || '').toLowerCase();
        var face = String(images[key] || images[profile.username] || '').trim();
        if (face && !localVideoAvatarUrl(status)) {
          status.profileImageUrl = face;
          status.profile_image_url = face;
        }
        return status;
      });
    }
    return statusPromise;
  }

  function chaturbateAvatarLookupKey(profile) {
    if (!profile) return '';
    var candidates = [
      profile.channelUsername || profile.channel_username,
      profile.username
    ];
    for (var i = 0; i < candidates.length; i++) {
      var key = String(candidates[i] || '')
        .trim()
        .toLowerCase()
        .replace(/^@+/, '')
        .replace(/\s+/g, '');
      if (key && /^[a-z0-9_]+$/.test(key)) return key;
    }
    return '';
  }

  function profileNeedsChaturbateAvatar(profile) {
    if (!profile || !profile.username) return false;
    if (profileImageUrl(profile)) return false;
    var sourceType = normalizeSourceMarker(profile.sourceType || profile.source_type);
    var sources = profile.streamSources || profile.stream_sources || [];
    var first = sources[0] || {};
    if (!sourceType) {
      sourceType = normalizeSourceMarker(first.sourceType || first.source_type) || 'chaturbate';
    }
    return !sourceType || sourceType === 'chaturbate';
  }

  function applyChaturbateAvatarMap(images) {
    if (!images || typeof images !== 'object') return false;
    var changed = false;
    (state.profiles || []).forEach(function(profile) {
      if (!profileNeedsChaturbateAvatar(profile)) return;
      var key = chaturbateAvatarLookupKey(profile);
      if (!key) return;
      var face = rewriteChaturbatePosterUrl(String(images[key] || '').trim());
      if (!face || isLiveCoverAvatarUrl(face) || isRecordingThumbAvatarUrl(face)) return;
      if (profileImageUrl(profile) === face) return;
      profile.profileImageUrl = face;
      profile.profile_image_url = face;
      changed = true;
    });
    return changed;
  }

  var missingChaturbateAvatarInflight = false;
  var chaturbateAvatarAttempted = {};

  function fillMissingChaturbateAvatars(force) {
    if (missingChaturbateAvatarInflight) return;
    if (force) chaturbateAvatarAttempted = {};
    var need = [];
    var seen = {};
    (state.profiles || []).forEach(function(profile) {
      if (!profileNeedsChaturbateAvatar(profile)) return;
      var key = chaturbateAvatarLookupKey(profile);
      if (!key || seen[key]) return;
      if (!force && chaturbateAvatarAttempted[key]) return;
      seen[key] = true;
      need.push(key);
    });
    if (!need.length) return;
    need.forEach(function(key) { chaturbateAvatarAttempted[key] = true; });
    missingChaturbateAvatarInflight = true;
    // Batch like Discover so followed and Mac-local cards both get faces.
    var url = '/api/discover/profile-images?source=chaturbate&usernames=' +
      encodeURIComponent(need.join(','));
    fetch(url, { cache: 'no-store', credentials: 'same-origin' })
      .then(function(res) { return res.ok ? res.json() : { images: {} }; })
      .then(function(data) {
        if (applyChaturbateAvatarMap(data && data.images)) {
          renderProfileCarousel();
        }
      })
      .catch(function() { /* keep letter avatars */ })
      .then(function() {
        missingChaturbateAvatarInflight = false;
      });
  }

  function hydrateLocalVideoProfileAvatars(force) {
    var changed = false;
    (state.profiles || []).forEach(function(profile) {
      if (!profile || !profile.fromMacLocal || !profile.username) return;
      var sourceType = normalizeSourceMarker(profile.sourceType || profile.source_type);
      if (force) delete state.localProfileLiveMeta[profile.username];
      var cached = state.localProfileLiveMeta[profile.username];
      if (cached) {
        if (applyLocalVideoLiveMeta(profile, cached)) changed = true;
        if (localLiveMetaIsUseful(cached, sourceType) || (cached._attempts || 0) >= 3) return;
        if ((Date.now() - Number(cached._fetchedAt || 0)) < 20000) return;
      }
      if (state.localProfileAvatarInflight[profile.username]) return;
      state.localProfileAvatarInflight[profile.username] = true;
      fetchLocalVideoLiveMeta(profile)
        .then(function(data) {
          delete state.localProfileAvatarInflight[profile.username];
          if (!data || typeof data !== 'object') data = {};
          data._fetchedAt = Date.now();
          data._attempts = Number((cached && cached._attempts) || 0) + 1;
          state.localProfileLiveMeta[profile.username] = data;
          var current = profileByUsername(profile.username);
          if (current && applyLocalVideoLiveMeta(current, data)) renderProfileCarousel();
        })
        .catch(function() {
          delete state.localProfileAvatarInflight[profile.username];
        });
    });
    fillMissingChaturbateAvatars(!!force);
    return changed;
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
      var host = (url.hostname || '').toLowerCase().replace(/\.$/, '');
      if (host === 'youtu.be' || host === 'www.youtu.be' || host === 'youtube.com' || host.slice(-12) === '.youtube.com') {
        var ytParts = url.pathname.split('/').map(function(part) {
          return decodeURIComponent(part || '').trim();
        }).filter(Boolean);
        var videoId = url.searchParams.get('v') || '';
        if (host === 'youtu.be' || host === 'www.youtu.be') {
          videoId = (ytParts[0] || '').split('?')[0];
        } else if (ytParts[0] === 'embed' && ytParts[1]) {
          videoId = ytParts[1];
        } else if (ytParts[0] === 'live' && ytParts[1] && ytParts[1] !== 'live') {
          videoId = ytParts[1];
        }
        if (videoId) return videoId.replace(/[^A-Za-z0-9_-]/g, '');
        if (ytParts[0] === 'channel' && ytParts[1]) return ytParts[1];
        if (ytParts[0] && ytParts[0].charAt(0) === '@') return ytParts[0].replace(/^@+/, '');
        if ((ytParts[0] === 'c' || ytParts[0] === 'user') && ytParts[1]) return ytParts[1];
      }
      var ignored = { b: true, chat: true, en: true, fr: true, room: true, rooms: true, videochat: true, watch: true };
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
    // Client-side sort; ask the API for a stable newest feed.
    params.set('sort', 'newest');
    params.set('metadata', 'lazy');
    params.set('live', 'true');
    if (forceLiveRefresh) params.set('live_refresh', 'true');
    params.set('limit', '1000');
    // Multi-select video filter is applied client-side from selectedStreamerIds.
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
      await loadRecordingProjects();
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
    ensureProfilesForLocalVideos();
    if (state.selectedProfile && !profileExists(state.selectedProfile)) state.selectedProfile = '';
    Object.keys(state.selectedStreamerIds || {}).forEach(function(username) {
      if (!profileExists(username)) delete state.selectedStreamerIds[username];
    });
    pruneInvisibleStreamerSelection();
    fillMissingChaturbateAvatars();
    scheduleTimelineReload();
  }

  async function refreshProfiles() {
    if (state.profileRefreshing) return;
    state.profileRefreshing = true;
    setProfileRefreshBusy(true);
    try {
      state.localProfileLiveMeta = {};
      state.localProfileAvatarInflight = {};
      var params = new URLSearchParams();
      params.set('kind', 'all');
      params.set('metadata', 'lazy');
      params.set('live', 'true');
      params.set('live_refresh', 'true');
      params.set('limit', '1');
      var res = await fetch('/api/media-library?' + params.toString(), { cache: 'no-store', credentials: 'same-origin' });
      if (!res.ok) throw new Error('Failed to refresh profiles');
      var data = await res.json();
      applyProfilesPayload(data.profiles || []);
      hydrateLocalVideoProfileAvatars(true);
      fillMissingChaturbateAvatars(true);
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
    if (meta) meta.textContent = ': Loading...';
    if (!state.profiles.length && rail) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading profiles...</p></div>';
    }
    var profileMeta = $('mediaProfileMeta');
    if (profileMeta && !state.profiles.length) profileMeta.textContent = ': Loading...';
    if (grid) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading media...</p></div>';
    }
    // Keep pagination chrome stable while data loads (never blank / never hide).
    ensurePaginationSkeleton();
  }

  function ensurePaginationSkeleton() {
    var profileNumbers = $('mediaProfilePageNumbers');
    if (profileNumbers && !profileNumbers.querySelector('[data-profile-page]')) {
      renderProfilePagination(Math.max(1, state.profilePage || 1));
    } else {
      var profileNav = $('mediaProfilePagination');
      if (profileNav) profileNav.hidden = false;
      var profilePrev = $('mediaStreamerPrevPageBtn');
      var profileNext = $('mediaStreamerNextPageBtn');
      if (profilePrev) profilePrev.disabled = state.profilePage <= 1;
      if (profileNext) profileNext.disabled = true;
    }
    var mediaNumbers = $('mediaPageNumbers');
    if (mediaNumbers && !mediaNumbers.querySelector('[data-page]')) {
      renderMediaPagination(Math.max(1, state.mediaPage || 1));
    } else {
      var mediaNav = $('mediaPagination');
      if (mediaNav) mediaNav.hidden = false;
      var mediaPrev = $('mediaVideoPrevPageBtn');
      var mediaNext = $('mediaVideoNextPageBtn');
      if (mediaPrev) mediaPrev.disabled = state.mediaPage <= 1;
      if (mediaNext) mediaNext.disabled = true;
    }
  }

  function renderError() {
    var grid = $('mediaGrid');
    var rail = $('mediaProfileRail');
    var meta = $('mediaResultMeta');
    if (meta) meta.textContent = ': Unable to load media';
    if (rail && !state.profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Profiles unavailable</p></div>';
    }
    var profileMeta = $('mediaProfileMeta');
    if (profileMeta && !state.profiles.length) profileMeta.textContent = ': Unable to load streamers';
    if (grid) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Media unavailable</p></div>';
    }
  }

  function renderDiskBreakdown(storage) {
    var bar = $('mediaDiskBar');
    if (!bar) return;

    var mediaBytes = Math.max(0, Number(storage.visibleMediaBytes) || 0);
    var processingBytes = Math.max(0, Number(storage.processingBytes) || 0);
    var otherBytes = Math.max(0, Number(storage.untrackedBytes) || 0);
    var folderBytes = Math.max(0, Number(storage.recordingFolderBytes) || (mediaBytes + processingBytes + otherBytes));
    var diskUsed = Math.max(0, Number(storage.diskUsedBytes) || 0);
    var diskFree = Math.max(0, Number(storage.diskFreeBytes) || 0);
    var usable = Math.max(0, Number(storage.diskUsableBytes) || (diskUsed + diskFree));
    var systemBytes = Math.max(0, diskUsed - folderBytes);
    if (usable <= 0) {
      bar.innerHTML = '';
      return;
    }

    // Clamp folder parts into used so the bar never exceeds usable.
    var folderParts = mediaBytes + processingBytes + otherBytes;
    if (folderParts > diskUsed && folderParts > 0) {
      var scale = diskUsed / folderParts;
      mediaBytes = Math.floor(mediaBytes * scale);
      processingBytes = Math.floor(processingBytes * scale);
      otherBytes = Math.max(0, diskUsed - mediaBytes - processingBytes - systemBytes);
    }
    systemBytes = Math.max(0, diskUsed - mediaBytes - processingBytes - otherBytes);

    var freePct = Number(storage.diskFreePercent);
    if (!isFinite(freePct) && usable > 0) {
      freePct = Math.round((diskFree / usable) * 1000) / 10;
    }
    var freeSize = storage.diskFreeFormatted || formatBytesShort(diskFree);
    var freePctText = isFinite(freePct) ? (String(freePct) + '%') : '';
    var freeTitle = 'Free ' + freeSize + (freePctText ? (' · ' + freePctText) : '');

    var segments = [
      {
        bytes: systemBytes,
        cls: 'media-storage-segment-system',
        label: 'System',
        text: 'System ' + formatBytesShort(systemBytes),
        html: null
      },
      {
        bytes: mediaBytes,
        cls: 'media-storage-segment-media',
        label: 'Media',
        text: 'Media ' + (storage.visibleMediaFormatted || formatBytesShort(mediaBytes)),
        html: null
      },
      {
        bytes: processingBytes,
        cls: 'media-storage-segment-processing',
        label: 'Processing',
        text: 'Processing ' + (storage.processingFormatted || formatBytesShort(processingBytes)),
        html: null
      },
      {
        bytes: otherBytes,
        cls: 'media-storage-segment-other',
        label: 'Other',
        text: 'Other ' + (storage.untrackedFormatted || formatBytesShort(otherBytes)),
        html: null
      },
      {
        bytes: diskFree,
        cls: 'media-storage-segment-free',
        label: 'Free',
        text: freeTitle,
        html: freePctText
          ? ('Free ' + escapeHtml(freeSize) + ' · <span class="media-storage-free-pct">' + escapeHtml(freePctText) + '</span>')
          : ('Free ' + escapeHtml(freeSize))
      }
    ];
    bar.innerHTML = segments.map(function(seg) {
      if (seg.bytes <= 0) return '';
      var pct = (seg.bytes / usable) * 100;
      if (pct > 0 && pct < 0.4) pct = 0.4;
      var showLabel = pct >= 8;
      var title = escapeHtml(seg.text);
      var labelInner = seg.html || escapeHtml(seg.text);
      var labelHtml = showLabel
        ? ('<span class="media-storage-segment-label">' + labelInner + '</span>')
        : '';
      return '<div class="media-storage-segment ' + seg.cls + '" style="width:' + pct.toFixed(2) + '%" title="' + title + '">' + labelHtml + '</div>';
    }).join('');
  }

  function renderStats(stats, storage) {
    storage = storage || {};
    var usedPct = Number(storage.diskUsedPercent);
    if (!isFinite(usedPct)) usedPct = 0;
    usedPct = Math.max(0, Math.min(100, usedPct));

    var diskBar = $('mediaDiskBar');
    if (diskBar) {
      diskBar.setAttribute('aria-valuenow', String(Math.round(usedPct)));
    }

    renderDiskBreakdown(storage);
  }

  function selectedProcessingPaths() {
    return Object.keys(state.processingSelected).filter(function(path) {
      return !!state.processingSelected[path];
    });
  }

  function syncProcessingDeleteState() {
    var selectedCount = selectedProcessingPaths().length;
    var deleteBtn = $('mediaProcessingDelete');
    var selectAll = $('mediaProcessingSelectAll');
    var summary = $('mediaProcessingSummary');
    var total = state.processingFiles.length;
    var allSelected = total > 0 && selectedCount === total;
    if (deleteBtn) {
      deleteBtn.disabled = state.processingDeleting || state.processingLoading || selectedCount === 0;
      deleteBtn.textContent = selectedCount
        ? ('Delete selected (' + selectedCount + ')')
        : 'Delete selected';
    }
    if (selectAll) {
      selectAll.disabled = state.processingDeleting || state.processingLoading || total === 0;
      selectAll.textContent = allSelected ? 'Clear selection' : 'Select all';
    }
    if (summary) {
      if (state.processingLoading) {
        summary.textContent = 'Loading...';
      } else if (!state.processingFiles.length) {
        summary.textContent = 'No temporary files';
      } else {
        summary.textContent = state.processingFiles.length + ' file(s)';
      }
    }
  }

  function renderProcessingFiles() {
    var body = $('mediaProcessingBody');
    if (!body) return;
    if (state.processingLoading) {
      body.innerHTML = '<tr><td colspan="3" class="media-processing-empty">Loading...</td></tr>';
      syncProcessingDeleteState();
      return;
    }
    if (!state.processingFiles.length) {
      body.innerHTML = '<tr><td colspan="3" class="media-processing-empty">No processing or temporary files found.</td></tr>';
      syncProcessingDeleteState();
      return;
    }
    body.innerHTML = state.processingFiles.map(function(file) {
      var checked = !!state.processingSelected[file.path];
      return '<tr class="media-processing-row' + (checked ? ' is-selected' : '') + '"' +
        ' data-processing-path="' + escapeHtml(file.path) + '"' +
        ' role="option" tabindex="0" aria-selected="' + (checked ? 'true' : 'false') + '"' +
        ' aria-label="Select ' + escapeHtml(file.filename) + '">' +
        '<td><div class="media-processing-file"><strong>' + escapeHtml(file.filename) + '</strong>' +
        '<span>' + escapeHtml(file.path) + '</span></div></td>' +
        '<td class="media-processing-col-size">' + escapeHtml(file.sizeFormatted || formatBytesShort(file.size || 0)) + '</td>' +
        '<td class="media-processing-col-modified">' +
          escapeHtml(formatDateTimeSeconds(file.modifiedAt) || '-') + '</td>' +
        '</tr>';
    }).join('');
    syncProcessingDeleteState();
  }

  async function loadProcessingFiles() {
    state.processingLoading = true;
    renderProcessingFiles();
    try {
      var res = await fetch('/api/media-library/processing-files', { cache: 'no-store', credentials: 'same-origin' });
      if (!res.ok) throw new Error('Unable to load temporary files');
      var data = await res.json();
      state.processingFiles = Array.isArray(data.files) ? data.files : [];
      var pathEl = $('mediaProcessingPath');
      if (pathEl) pathEl.textContent = data.recordsRoot ? ('Folder: ' + data.recordsRoot) : '';
      var keepSelected = {};
      state.processingFiles.forEach(function(file) {
        if (state.processingSelected[file.path]) keepSelected[file.path] = true;
      });
      state.processingSelected = keepSelected;
    } catch (error) {
      state.processingFiles = [];
      var body = $('mediaProcessingBody');
      if (body) {
        body.innerHTML = '<tr><td colspan="3" class="media-processing-empty">' +
          escapeHtml(error.message || 'Unable to load temporary files') + '</td></tr>';
      }
    } finally {
      state.processingLoading = false;
      renderProcessingFiles();
    }
  }

  function openProcessingModal() {
    var modal = $('mediaProcessingModal');
    if (!modal) return;
    state.processingSelected = {};
    state.processingDeleting = false;
    modal.style.display = 'flex';
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('media-delete-open');
    loadProcessingFiles();
  }

  function closeProcessingModal() {
    var modal = $('mediaProcessingModal');
    if (!modal) return;
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('media-delete-open');
    state.processingDeleting = false;
    syncProcessingDeleteState();
  }

  async function deleteSelectedProcessingFiles() {
    var paths = selectedProcessingPaths();
    if (!paths.length || state.processingDeleting) return;
    var count = paths.length;
    var message = 'Delete ' + count + ' temporary file(s) from the VPS? This cannot be undone.';
    var ok = await (window.hxyliveConfirm
      ? window.hxyliveConfirm(message, {
          title: 'Delete temporary files',
          confirmLabel: 'Delete selected (' + count + ')',
          danger: true
        })
      : Promise.resolve(window.confirm(message)));
    if (!ok) return;
    state.processingDeleting = true;
    syncProcessingDeleteState();
    try {
      var res = await fetch('/api/media-library/processing-files/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ paths: paths })
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Delete failed');
      showToast('Deleted ' + (data.deletedCount || paths.length) + ' temporary file(s)', 'success');
      state.processingSelected = {};
      await loadProcessingFiles();
      await loadMediaLibrary();
    } catch (error) {
      showToast(error.message || 'Delete failed', 'error');
    } finally {
      state.processingDeleting = false;
      syncProcessingDeleteState();
    }
  }

  function renderProfilePagination(totalPages) {
    var pagination = $('mediaProfilePagination');
    var numbers = $('mediaProfilePageNumbers');
    var prevBtn = $('mediaStreamerPrevPageBtn');
    var nextBtn = $('mediaStreamerNextPageBtn');
    if (!pagination) return;
    pagination.hidden = false;
    var pages = Math.max(1, Number(totalPages) || 1);
    if (prevBtn) prevBtn.disabled = state.profilePage <= 1;
    if (nextBtn) nextBtn.disabled = state.profilePage >= pages;
    if (!numbers) return;
    var pageButtons = [];
    for (var page = 1; page <= pages; page++) {
      pageButtons.push(
        '<button class="media-page-number' + (page === state.profilePage ? ' active' : '') +
        '" type="button" data-profile-page="' + page + '" aria-label="Profile page ' + page +
        '" aria-current="' + (page === state.profilePage ? 'page' : 'false') + '">' + page + '</button>'
      );
    }
    numbers.innerHTML = pageButtons.join('');
  }

  function shiftProfilePage(delta) {
    var profiles = visibleProfiles();
    var pageSize = profilePageSize();
    var totalPages = profileTotalPages(profiles.length, pageSize);
    setProfilePage(state.profilePage + (Number(delta) || 0));
    return totalPages;
  }

  function setProfilePage(page) {
    var profiles = visibleProfiles();
    var pageSize = profilePageSize();
    var totalPages = profileTotalPages(profiles.length, pageSize);
    state.profilePage = Math.min(totalPages, Math.max(1, Number(page) || 1));
    renderProfileCarousel();
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
    var first = profileFirstPageStreamerCount(pageSize);
    if (index < first) state.profilePage = 1;
    else state.profilePage = 2 + Math.floor((index - first) / pageSize);
  }

  function renderProfileCarousel() {
    var rail = $('mediaProfileRail');
    var meta = $('mediaProfileMeta');
    if (!rail) return;
    var profiles = visibleProfiles();
    var sourceFilteredCount = state.profiles.filter(function(profile) {
      return profileMatchesSourceFilter(profile, state.filterSources)
        && profileMatchesLiveStatus(profile, state.filterLiveStatuses)
        && profileMatchesRecordingFilter(profile, state.filterRecordings);
    }).length;
    var pageSize = profilePageSize();
    var totalPages = profileTotalPages(profiles.length, pageSize);
    state.profilePage = Math.min(Math.max(1, state.profilePage), totalPages);
    var hasNarrowingFilter = !!(
      state.profileSearch
      || state.filterSources.length
      || state.filterLiveStatuses.length
      || state.filterRecordings.length
    );

    if (meta) {
      var count = profiles.length;
      var countLabel = count === 1 ? '1 streamer' : count + ' streamers';
      var metaText = countLabel;
      if (hasNarrowingFilter) {
        metaText = countLabel + ' of ' + (
          state.profileSearch ? sourceFilteredCount : state.profiles.length
        );
        if (!state.profileSearch) {
          metaText = countLabel + ' of ' + state.profiles.length;
        }
      }
      meta.textContent = ': ' + metaText;
      meta.hidden = false;
    }

    if (!state.profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No streamers found. Create a card from Discover, sync a site, or scan local videos.</p></div>';
      renderProfilePagination(1);
      syncStreamerBatchActions();
      return;
    }

    if (!profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No streamers match this filter</p></div>';
      renderProfilePagination(1);
      syncStreamerBatchActions();
      return;
    }

    var pageProfiles = profilePageSlice(profiles, state.profilePage, pageSize);
    var cards = pageProfiles.map(renderProfileCard);
    // Pin "All streamers" as the first card (page 1 only); page size already reserves that slot.
    if (state.profilePage === 1) {
      cards.unshift(renderAllProfilesCard());
    }
    rail.innerHTML = cards.join('');
    renderProfilePagination(totalPages);
    syncStreamerBatchActions();
  }

  function totalProfileVideoCount(profiles) {
    return (profiles || []).reduce(function(sum, profile) {
      return sum + Number(profile && profile.videos || 0);
    }, 0);
  }

  function renderAllProfilesCard() {
    var active = !selectedStreamerList().length;
    var profiles = visibleProfiles();
    var profileCount = profiles.length;
    var videoCount = totalProfileVideoCount(profiles);
    var profileLabelText = profileCount === 1 ? '1 streamer' : profileCount + ' streamers';
    var videoLabel = mediaCountLabel(videoCount, 'video', 'videos');
    return '' +
      '<article class="media-profile-card media-all-profiles-card' + (active ? ' is-selected' : '') +
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

  function selectedStreamerList() {
    return Object.keys(state.selectedStreamerIds || {}).filter(function(username) {
      return !!state.selectedStreamerIds[username] && !!profileByUsername(username);
    });
  }

  function syncStreamerBatchActions() {
    var selected = selectedStreamerList();
    var startRecordCount = 0;
    var recordingCount = 0;
    selected.forEach(function(username) {
      var profile = profileByUsername(username);
      if (!profile) return;
      if (profile.autoRecord) recordingCount += 1;
      else startRecordCount += 1;
    });
    var clearBtn = $('mediaStreamerClearSelectionBtn');
    var startBtn = $('mediaStreamerStartRecordBtn');
    var stopBtn = $('mediaStreamerStopRecordBtn');
    var deleteBtn = $('mediaStreamerDeleteBtn');
    var settingsBtn = $('mediaStreamerSettingsBtn');
    if (clearBtn) clearBtn.disabled = !selected.length;
    if (settingsBtn) settingsBtn.disabled = !selected.length;
    if (startBtn) {
      startBtn.disabled = !startRecordCount;
      startBtn.textContent = 'Turn on recording (' + startRecordCount + ')';
    }
    if (stopBtn) {
      stopBtn.disabled = !recordingCount;
      stopBtn.textContent = 'Turn off recording (' + recordingCount + ')';
    }
    if (deleteBtn) {
      deleteBtn.disabled = !selected.length;
      deleteBtn.textContent = 'Delete cards (' + selected.length + ')';
    }
  }

  function syncStreamerFilterState() {
    var selected = selectedStreamerList();
    if (selected.length === 1) state.filterProfile = selected[0];
    else state.filterProfile = '';
  }

  function applyStreamerSelectionFilter(shouldScroll) {
    syncStreamerFilterState();
    state.mediaPage = 1;
    if (state.projectDetailId) {
      state.projectDetailId = null;
      hideProjectDetail();
    }
    syncStreamerBatchActions();
    // Toggle classes in place — full carousel re-render would wipe text selection.
    syncProfileSelectionUI();
    rebuildVisibleItems();
    loadRecordingProjects();
    scheduleTimelineReload();
    if (shouldScroll) {
      var results = document.querySelector('.media-recent-section');
      if (results && results.scrollIntoView) {
        results.scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
    }
  }

  function toggleStreamerSelection(username, selected) {
    if (!username) return;
    if (selected) state.selectedStreamerIds[username] = true;
    else delete state.selectedStreamerIds[username];
    applyStreamerSelectionFilter(false);
  }

  function pageProfiles() {
    var profiles = visibleProfiles();
    var pageSize = profilePageSize();
    var totalPages = profileTotalPages(profiles.length, pageSize);
    var page = Math.min(Math.max(1, state.profilePage), totalPages);
    return profilePageSlice(profiles, page, pageSize);
  }

  function selectAllFilteredStreamers() {
    visibleProfiles().forEach(function(profile) {
      if (profile && profile.username) state.selectedStreamerIds[profile.username] = true;
    });
    applyStreamerSelectionFilter(false);
  }

  function selectAllVisibleStreamers() {
    pageProfiles().forEach(function(profile) {
      if (profile && profile.username) state.selectedStreamerIds[profile.username] = true;
    });
    // Keep the Streamers section in view — do not jump to the video grid.
    applyStreamerSelectionFilter(false);
  }

  function clearStreamerSelection() {
    state.selectedStreamerIds = {};
    applyStreamerSelectionFilter(false);
  }

  function profileRecordingBlocked(profile) {
    if (state.recordingEnabled === false) return true;
    if (state.librarySpaceBlocked || state.stagingBlocked) return true;
    if (profile && (profile.recordingAllowed === false || profile.recording_allowed === false)) return true;
    if (profile && (profile.quotaExceeded || profile.quota_exceeded)) return true;
    var reason = profile && (profile.recordingBlockedReason || profile.recording_blocked_reason);
    return reason === 'global_pause' || reason === 'quota_exceeded' || reason === 'library_disk_low' || reason === 'staging_cleaning';
  }

  function profileRecordingBlockedTitle(profile) {
    if (state.librarySpaceBlocked) {
      return 'Library disk space is too low; Auto Record is disabled until you free space and turn it back on';
    }
    if (state.stagingBlocked) {
      return 'SSD staging is cleaning; captures are paused until staging is empty';
    }
    if (state.recordingEnabled === false) {
      return 'Project recording paused (global Auto Record is off)';
    }
    var reason = (profile && (profile.recordingBlockedReason || profile.recording_blocked_reason)) || '';
    if (reason === 'quota_exceeded' || (profile && (profile.quotaExceeded || profile.quota_exceeded))) {
      return 'Monthly recording quota reached for this cycle';
    }
    if (reason === 'global_pause') {
      return 'Project recording paused (global Auto Record is off)';
    }
    return 'Recording not allowed by project right now';
  }

  function renderProfileCard(profile) {
    var selected = !!state.selectedStreamerIds[profile.username];
    var blocked = profileRecordingBlocked(profile);
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
    var lastLiveText = lastLiveStamp ? formatLastLive(lastLiveStamp) : '';
    var lastLiveRelative = lastLiveText ? formatLastSeen(lastLiveStamp) : '';
    var lastLiveHtml = lastLiveText
      ? '<span class="offline media-profile-last-live"' +
          (lastLiveRelative ? ' title="' + escapeHtml(lastLiveRelative) + '"' : '') +
        '>' + escapeHtml(lastLiveText) + '</span>'
      : '';
    var nameTitle = name;
    var nameClass = 'media-profile-name' + profileStatusClass(profile);
    var countClass = 'media-profile-counts media-profile-avatar-counts';
    var liveWatchHtml = '<a class="media-profile-live-link ' + liveClass + '" href="' + escapeHtml(watchUrl) +
      '" data-profile-action="watch" title="Watch live">' + escapeHtml(liveText) + '</a>';
    var cardTitle = 'Click to select' + (profile.autoRecord ? ' (recording on)' : '') +
      (blocked ? ' — ' + profileRecordingBlockedTitle(profile) : '');
    return '' +
      '<article class="media-profile-card' +
        (selected ? ' is-selected' : '') +
        (profile.autoRecord ? ' is-recording' : '') +
        (blocked ? ' is-recording-blocked' : '') +
        '" role="button" tabindex="0" data-profile="' + escapeHtml(profile.username) +
        '" aria-pressed="' + (selected ? 'true' : 'false') +
        '" title="' + escapeHtml(cardTitle) + '">' +
        '<div class="media-profile-info">' +
          '<div class="media-profile-avatar-column">' +
            '<div class="media-profile-avatar">' +
              image +
              '<div class="media-profile-placeholder"><span>' + escapeHtml(firstLetter(name)) + '</span></div>' +
            '</div>' +
            '<div class="' + countClass + ' media-profile-videos-link" data-profile-action="open-videos-folder" data-profile="' +
              escapeHtml(profile.username) + '" title="Open Mac folder for this streamer">' + escapeHtml(countLabel) + '</div>' +
          '</div>' +
          '<div class="media-profile-copy">' +
            '<div class="' + nameClass + '" title="' + escapeHtml(nameTitle) + '">' +
              '<span class="media-profile-name-text">' + escapeHtml(name) + '</span>' +
            '</div>' +
            '<div class="media-profile-live-meta">' +
              liveWatchHtml +
            '</div>' +
            (lastLiveHtml ? '<div class="media-profile-last-live-row">' + lastLiveHtml + '</div>' : '') +
            (hideFollowers ? '' : '<div class="media-profile-followers">' + escapeHtml(followerText) + '</div>') +
            (channelUrl
              ? '<div class="media-profile-channel-line">' +
                  '<a class="media-profile-channel-url" href="' + escapeHtml(channelUrl) +
                  '" target="_blank" rel="noopener" data-profile-action="channel">' +
                  escapeHtml(providerLabel(sourceType) || 'Channel') +
                  '</a></div>'
              : '') +
          '</div>' +
        '</div>' +
      '</article>';
  }

  function recordingPayloadFromProfile(profile, autoRecord) {
    var username = String((profile && profile.username) || '').trim();
    var sources = (profile && (profile.streamSources || profile.stream_sources)) || [];
    var first = sources[0] || {};
    var sourceType = normalizeSourceMarker(
      (profile && (profile.sourceType || profile.source_type)) ||
      first.sourceType || first.source_type ||
      'chaturbate'
    ) || 'chaturbate';
    var channelUsername = String(
      first.channelUsername || first.channel_username ||
      (profile && (profile.channelUsername || profile.channel_username)) ||
      username
    ).trim() || username;
    var channelUrl = String(
      first.channelUrl || first.channel_url ||
      (profile && (profile.channelUrl || profile.channel_url)) ||
      channelUrlForSource(sourceType, channelUsername) ||
      ''
    ).trim();
    return {
      autoRecord: !!autoRecord,
      sourceType: sourceType,
      channelUsername: channelUsername,
      channelUrl: channelUrl,
      displayName: (profile && (profile.displayName || profile.display_name)) || username,
      profileImageUrl: (profile && (profile.profileImageUrl || profile.profile_image_url)) || ''
    };
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
        body: JSON.stringify(recordingPayloadFromProfile(profile, enabled))
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Recording update failed');
      profile.autoRecord = !!data.autoRecord;
      profile.fromMacLocal = false;
      if (data.profile) {
        profile.streamSources = data.profile.streamSources || profile.streamSources;
        profile.stream_sources = data.profile.stream_sources || profile.stream_sources;
        profile.recordQuality = data.profile.recordQuality || profile.recordQuality;
        profile.retentionDays = data.profile.retentionDays == null ? profile.retentionDays : data.profile.retentionDays;
        profile.sourceType = data.profile.sourceType || data.profile.source_type || profile.sourceType;
        profile.source_type = profile.sourceType;
        if (data.profile.recordingAllowed != null) profile.recordingAllowed = data.profile.recordingAllowed;
        if (data.profile.recordingBlockedReason != null) profile.recordingBlockedReason = data.profile.recordingBlockedReason;
        if (data.profile.quotaExceeded != null) profile.quotaExceeded = data.profile.quotaExceeded;
        if (data.profile.monthlyQuotaGb !== undefined) profile.monthlyQuotaGb = data.profile.monthlyQuotaGb;
        if (data.profile.monthlyQuotaUsedBytes != null) profile.monthlyQuotaUsedBytes = data.profile.monthlyQuotaUsedBytes;
      }
      renderProfileCarousel();
      showToast(profile.autoRecord ? 'Recording enabled for ' + username : 'Recording paused for ' + username, 'success');
    } catch (e) {
      showToast(e.message || 'Recording update failed', 'error');
      if (button) button.disabled = false;
    }
  }

  async function bulkStartRecordingSelectedStreamers() {
    var selected = selectedStreamerList().filter(function(username) {
      var profile = profileByUsername(username);
      return profile && !profile.autoRecord;
    });
    if (!selected.length) return;
    var items = selected.map(function(username) {
      var profile = profileByUsername(username) || { username: username };
      var payload = recordingPayloadFromProfile(profile, true);
      return {
        username: username,
        sourceType: payload.sourceType,
        channelUsername: payload.channelUsername,
        channelUrl: payload.channelUrl,
        displayName: payload.displayName,
        profileImageUrl: payload.profileImageUrl
      };
    });
    try {
      var res = await fetch('/api/media-profiles/bulk/auto-record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: items, autoRecord: true })
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Bulk recording update failed');
      var failed = (data.results || []).filter(function(row) { return !row.success; });
      if (!(data.updated || 0)) {
        var detail = (failed[0] && failed[0].detail) || 'No streamers could enable recording';
        throw new Error(detail);
      }
      if (failed.length) {
        showToast(
          'Recording turned on for ' + (data.updated || 0) +
            '; failed ' + failed.length + (failed[0].detail ? ' — ' + failed[0].detail : ''),
          'info'
        );
      } else {
        showToast('Recording turned on for ' + (data.updated || 0) + ' streamer(s)', 'success');
      }
      await loadMediaLibrary();
    } catch (e) {
      showToast(e.message || 'Bulk recording update failed', 'error');
    }
  }

  async function bulkStopRecordingSelectedStreamers() {
    var selected = selectedStreamerList().filter(function(username) {
      var profile = profileByUsername(username);
      return profile && profile.autoRecord;
    });
    if (!selected.length) return;
    try {
      var res = await fetch('/api/media-profiles/bulk/auto-record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ usernames: selected, autoRecord: false })
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Bulk recording update failed');
      showToast('Recording turned off for ' + (data.updated || 0) + ' streamer(s)', 'success');
      await loadMediaLibrary();
    } catch (e) {
      showToast(e.message || 'Bulk recording update failed', 'error');
    }
  }

  function openStreamerBulkDeleteConfirm() {
    var selected = selectedStreamerList();
    if (!selected.length) return;
    var modal = $('mediaStreamerBulkDeleteModal');
    var target = $('mediaStreamerBulkDeleteTarget');
    var confirmBtn = $('mediaStreamerBulkDeleteConfirm');
    if (target) {
      target.textContent = selected.length === 1
        ? selected[0]
        : (selected.length + ' cards: ' + selected.slice(0, 8).join(', ') + (selected.length > 8 ? '…' : ''));
    }
    if (confirmBtn) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Delete cards';
    }
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    }
  }

  function closeStreamerBulkDeleteConfirm() {
    var modal = $('mediaStreamerBulkDeleteModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-delete-open');
    }
  }

  async function confirmStreamerBulkDelete() {
    var selected = selectedStreamerList();
    if (!selected.length) return;
    var confirmBtn = $('mediaStreamerBulkDeleteConfirm');
    if (confirmBtn) {
      confirmBtn.disabled = true;
      confirmBtn.textContent = 'Deleting...';
    }
    var serverUsernames = [];
    var macLocalUsernames = [];
    selected.forEach(function(username) {
      var profile = profileByUsername(username);
      if (profile && profile.fromMacLocal) macLocalUsernames.push(username);
      else serverUsernames.push(username);
    });
    try {
      var deletedCount = 0;
      var failedDetail = '';
      if (serverUsernames.length) {
        var res = await fetch('/api/media-profiles/bulk/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ usernames: serverUsernames, confirm: true })
        });
        var data = await res.json().catch(function() { return {}; });
        if (!res.ok) throw new Error(data.detail || 'Bulk delete failed');
        deletedCount += Number(data.deleted || 0);
        var failed = (data.results || []).filter(function(row) { return row && !row.success; });
        if (failed.length && failed[0].detail) failedDetail = failed[0].detail;
      }
      if (macLocalUsernames.length) {
        var macItems = [];
        (state.items || []).forEach(function(item) {
          if (!item || !item.isMacOnly) return;
          if (macLocalUsernames.indexOf(String(item.username || '')) === -1) return;
          macItems.push(item);
        });
        // Also collect any Mac files for these folder identities even if not in visible items.
        (state.macFiles || []).forEach(function(file) {
          var meta = parseMacFileMeta(file);
          var key = resolveLocalIdentityToProfileUsername(meta.username, meta.sourceType) || meta.username;
          if (macLocalUsernames.indexOf(key) === -1 && macLocalUsernames.indexOf(meta.username) === -1) return;
          var already = macItems.some(function(item) {
            return String(item.macRelativePath || '') === String(meta.relative || '');
          });
          if (already) return;
          macItems.push({
            macRelativePath: meta.relative,
            recordingId: file.recordingId || '',
            username: key || meta.username,
            isMacOnly: true
          });
        });
        if (macItems.length) {
          try {
            var macResult = await deleteSelectedMacCopies(macItems);
            deletedCount += Number(macResult.deletedCount || 0) || macLocalUsernames.length;
          } catch (macErr) {
            // Card can still disappear from the UI; Mac files may remain if helper is down.
            failedDetail = failedDetail || (macErr && macErr.message) || 'Mac delete failed';
            deletedCount += macLocalUsernames.length;
          }
        } else {
          deletedCount += macLocalUsernames.length;
        }
        state.profiles = (state.profiles || []).filter(function(profile) {
          return !(profile && profile.fromMacLocal && macLocalUsernames.indexOf(profile.username) !== -1);
        });
      }
      closeStreamerBulkDeleteConfirm();
      state.selectedStreamerIds = {};
      selected.forEach(function(username) {
        if (state.filterProfile === username) state.filterProfile = '';
        if (state.selectedProfile === username) state.selectedProfile = '';
      });
      if (deletedCount <= 0 && failedDetail) {
        showToast(failedDetail || 'Bulk delete failed', 'error');
      } else {
        showToast('Deleted ' + deletedCount + ' streamer card(s)', 'success');
      }
      await loadMediaLibrary();
    } catch (e) {
      showToast(e.message || 'Bulk delete failed', 'error');
      if (confirmBtn) {
        confirmBtn.disabled = false;
        confirmBtn.textContent = 'Delete cards';
      }
    }
  }

  function syncProfileSelectionUI() {
    document.querySelectorAll('.media-profile-card').forEach(function(card) {
      if (card.getAttribute('data-all-profiles') === '1') {
        card.classList.toggle('is-selected', !selectedStreamerList().length);
        return;
      }
      var username = card.dataset.profile || '';
      var selected = !!(username && state.selectedStreamerIds[username]);
      card.classList.toggle('is-selected', selected);
      card.setAttribute('aria-pressed', selected ? 'true' : 'false');
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
    var selected = selectedStreamerList();
    var profileId = 'All videos';
    if (selected.length === 1) {
      profileId = profileLabel(profileByUsername(selected[0])) || selected[0];
    } else if (selected.length > 1) {
      profileId = selected.length + ' streamers';
    }
    var countText;
    if (state.videoViewMode === 'projects' && !state.projectDetailId) {
      var projectCount = visibleProjects().length;
      countText = projectCount === 1 ? '1 matching project' : projectCount + ' matching projects';
    } else {
      countText = state.items.length === 1 ? '1 matching video' : state.items.length + ' matching videos';
    }
    if (meta) {
      meta.textContent = ': ' + countText;
      meta.hidden = false;
    }
    if (title) {
      var profileEl = title.querySelector('.media-heading-profile');
      if (profileEl) profileEl.textContent = profileId;
      else {
        title.innerHTML =
          '<span class="media-heading-profile">' + escapeHtml(profileId) + '</span>' +
          '<span class="media-heading-count" id="mediaResultMeta">: ' + escapeHtml(countText) + '</span>';
      }
    }
  }

  function renderGrid(total) {
    var grid = $('mediaGrid');
    if (!grid) return;
    renderRecentTitle();
    syncProjectFilterControls();

    if (state.videoViewMode === 'projects') {
      if (state.projectDetailId) {
        renderProjectDetail(state.projectDetailId);
        return;
      }
      hideProjectDetail();
      var projects = visibleProjects();
      if (!projects.length) {
        grid.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No projects found</p></div>';
        renderMediaPagination(1);
        return;
      }
      var totalPages = Math.max(1, Math.ceil(projects.length / MEDIA_PAGE_SIZE));
      state.mediaPage = Math.min(Math.max(1, state.mediaPage), totalPages);
      var start = (state.mediaPage - 1) * MEDIA_PAGE_SIZE;
      grid.innerHTML = projects.slice(start, start + MEDIA_PAGE_SIZE).map(renderProjectCard).join('');
      renderMediaPagination(totalPages);
      return;
    }

    hideProjectDetail();
    if (!state.items.length) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No media found</p></div>';
      renderMediaPagination(1);
      return;
    }

    var fragmentPages = Math.max(1, Math.ceil(state.items.length / MEDIA_PAGE_SIZE));
    state.mediaPage = Math.min(Math.max(1, state.mediaPage), fragmentPages);
    var fragmentStart = (state.mediaPage - 1) * MEDIA_PAGE_SIZE;
    grid.innerHTML = state.items.slice(fragmentStart, fragmentStart + MEDIA_PAGE_SIZE).map(renderCard).join('');
    renderMediaPagination(fragmentPages);
  }

  function renderMediaPagination(totalPages) {
    var pagination = $('mediaPagination');
    var numbers = $('mediaPageNumbers');
    var prev = $('mediaVideoPrevPageBtn');
    var next = $('mediaVideoNextPageBtn');
    if (!pagination) return;
    pagination.hidden = false;
    var pages = Math.max(1, Number(totalPages) || 1);
    if (numbers) {
      var pageButtons = [];
      for (var page = 1; page <= pages; page++) {
        pageButtons.push(
          '<button class="media-page-number' + (page === state.mediaPage ? ' active' : '') +
          '" type="button" data-page="' + page + '" aria-label="Page ' + page +
          '" aria-current="' + (page === state.mediaPage ? 'page' : 'false') + '">' + page + '</button>'
        );
      }
      numbers.innerHTML = pageButtons.join('');
    }
    if (prev) prev.disabled = state.mediaPage <= 1;
    if (next) next.disabled = state.mediaPage >= pages;
  }

  function setMediaPage(page) {
    var listLength = state.videoViewMode === 'projects' && !state.projectDetailId
      ? visibleProjects().length
      : state.items.length;
    var totalPages = Math.max(1, Math.ceil(listLength / MEDIA_PAGE_SIZE));
    state.mediaPage = Math.min(totalPages, Math.max(1, Number(page) || 1));
    renderGrid(state.items.length);
  }

  function shiftMediaPage(delta) {
    setMediaPage(state.mediaPage + (Number(delta) || 0));
  }

  function retryMacThumb(img) {
    if (!img) return;
    var n = Number(img.dataset.retry || 0);
    if (n >= 5) {
      img.style.display = 'none';
      if (img.parentElement) img.parentElement.classList.add('missing-thumb');
      return;
    }
    img.dataset.retry = String(n + 1);
    setTimeout(function() {
      var url = String(img.getAttribute('src') || '').replace(/&_r=\d+/g, '');
      if (!url) return;
      img.src = url + (url.indexOf('?') === -1 ? '?' : '&') + '_r=' + Date.now();
    }, 2500 * (n + 1));
  }
  window.hxyliveRetryMacThumb = retryMacThumb;

  function renderCard(item) {
    var thumb = '';
    if (item.thumbnail) {
      var macCover = String(item.thumbnail).indexOf('/thumb?') !== -1 ||
        String(item.thumbnail).indexOf('/api/mac/helper/thumb?') !== -1;
      thumb = '<img src="' + escapeHtml(item.thumbnail) + '" alt="' + escapeHtml(item.title || item.filename) +
        '" loading="lazy" onerror="' +
        (macCover
          ? 'window.hxyliveRetryMacThumb && window.hxyliveRetryMacThumb(this)'
          : 'this.style.display=\'none\'; this.parentElement.classList.add(\'missing-thumb\');') +
        '">';
    }

    var progress = mediaPlaybackProgress(item);
    var marker = item.type === 'image' ? '&#128247;' : item.type === 'audio' ? '&#9835;' : '&#9654;';
    var cardTitle = displayMediaTitle(item);
    var badges = [];
    if (item.isImported) badges.push('<span class="media-tag">Imported</span>');
    if (item.type === 'video' && itemHasVps(item) && !item.browserPlayable) {
      badges.push('<span class="media-tag">Original</span>');
    }
    // Quality only when WxH is known (VPS ffprobe / Mac Helper mdls·mp4·ffprobe).
    var qualityText = item.type === 'video' ? formatVideoStreamMeta(item) : '';
    var showQuality = !!(qualityText);
    var selected = !!state.selectedItemIds[item.id];
    var selectable = itemIsSelectable(item);
    var bodyAction = selectable
      ? ' data-media-action="select-card" data-media-id="' + escapeHtml(item.id) + '"'
      : '';
    // Folder key stays username (Bilibili room id); show human label when known.
    var profileForItem = profileByUsername(item.username);
    var profileIdLabel = profileForItem ? profileLabel(profileForItem) : (item.username || '');
    var canRevealFolder = !!(item.macRelativePath && itemHasMac(item));
    var footerParts = [];
    if (itemHasVps(item)) {
      footerParts.push(
        '<span class="media-card-footer-btn media-card-vps-folder" data-media-action="vps-label" data-media-id="' +
        escapeHtml(item.id) + '" title="Select this VPS video" role="button" tabindex="0">VPS</span>'
      );
    }
    if (canRevealFolder) {
      footerParts.push(
        '<button type="button" class="media-card-footer-btn media-card-open-folder" data-media-action="reveal-local" data-media-id="' +
        escapeHtml(item.id) + '" title="Show in Finder">Open folder</button>'
      );
    }

    return '' +
      '<article class="media-card' + (item.isWatched ? ' watched' : '') + (selected ? ' selected' : '') +
      (item.isMacOnly ? ' mac-only' : '') + '" role="button" tabindex="0" data-media-id="' + escapeHtml(item.id) +
      '" title="' + escapeHtml(itemHasMac(item) ? 'Click to open on Mac' : 'Click to play') + '">' +
        '<div class="media-card-thumb">' +
          thumb +
          '<div class="media-card-placeholder"><span aria-hidden="true">' + marker + '</span></div>' +
          (item.type === 'video' && progress > 0 ? '<div class="media-playback-progress" aria-hidden="true"><div style="width:' + progress + '%"></div></div>' : '') +
        '</div>' +
        '<div class="media-card-body"' + bodyAction + '>' +
          '<button class="media-card-profile-id" type="button" data-media-action="profile" data-profile="' + escapeHtml(item.username || '') + '" title="Show recordings for ' + escapeHtml(profileIdLabel || item.username || '') + '">' + escapeHtml(profileIdLabel || '') + '</button>' +
          '<div class="media-card-title-row">' +
            '<div class="media-card-title" title="' + escapeHtml(item.macRelativePath || item.filename || cardTitle) + '">' +
              escapeHtml(cardTitle) + '</div>' +
          '</div>' +
          '<div class="media-card-meta">' +
            (numberOrZero(item.duration)
              ? '<span class="media-card-detail-row">Duration: <strong class="media-duration-value">' + escapeHtml(formatDurationClock(item.duration)) + '</strong></span>'
              : '') +
            '<span class="media-card-detail-row">Size: ' + escapeHtml(item.sizeFormatted || formatBytesShort(item.size) || '-') + '</span>' +
            (showQuality
              ? '<div class="media-card-quality-row">' +
                  '<span>Quality: ' + escapeHtml(qualityText) + '</span>' +
                '</div>'
              : '') +
          '</div>' +
          (badges.length ? '<div class="media-badges">' + badges.join('') + '</div>' : '') +
        '</div>' +
        (footerParts.length
          ? '<div class="media-card-footer">' + footerParts.join('') + '</div>'
          : '') +
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
    var kind = type || 'success';
    if (kind === 'error' || kind === 'success' || kind === 'info') {
      appendMediaLog(message, kind);
    } else {
      appendMediaLog(message, 'info');
    }
    if (typeof window.showNotification === 'function') {
      window.showNotification(message, kind);
      return;
    }
    var existing = document.querySelector('.media-toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'media-toast ' + kind;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(function() {
      toast.remove();
    }, 2600);
  }

  function classifyDeleteItems(items) {
    var vpsOnly = [];
    var macOnly = [];
    (items || []).forEach(function(item) {
      if (!item) return;
      if (itemHasMac(item)) macOnly.push(item);
      if (itemHasVps(item)) vpsOnly.push(item);
    });
    return { vpsOnly: vpsOnly, macOnly: macOnly };
  }

  function countDeleteLocations(items) {
    var macCount = 0;
    var vpsCount = 0;
    (items || []).forEach(function(item) {
      if (!item) return;
      if (itemHasMac(item)) macCount += 1;
      if (itemHasVps(item)) vpsCount += 1;
    });
    return { macCount: macCount, vpsCount: vpsCount };
  }

  function syncBatchDeleteConfirmState() {
    var items = state.pendingDelete || [];
    var counts = countDeleteLocations(items);
    var helperReady = !!(state.localSessionId && state.macHelperAvailable);
    var macDeletes = counts.macCount;
    var vpsDeletes = counts.vpsCount;
    var confirm = $('mediaDeleteConfirm');
    if (!confirm) return;
    var needsHelper = macDeletes > 0;
    var nothingSelected = (vpsDeletes + macDeletes) === 0;
    confirm.disabled = nothingSelected || (needsHelper && !helperReady);
    confirm.textContent = 'Delete';
    if (nothingSelected) {
      confirm.title = 'Select at least one video to delete';
    } else if (needsHelper && !helperReady) {
      confirm.title = 'Start the HXYLIVE Mac helper to delete Mac copies';
    } else {
      confirm.removeAttribute('title');
    }
  }

  function openBatchDeleteConfirm() {
    var items = Object.keys(state.selectedItemIds).map(itemById).filter(itemIsSelectable);
    if (!items.length) return;
    var counts = countDeleteLocations(items);
    var helperReady = !!(state.localSessionId && state.macHelperAvailable);
    if (counts.macCount && !helperReady && !counts.vpsCount) {
      showToast('Start the HXYLIVE Mac helper to delete Mac copies', 'error');
      return;
    }
    state.pendingDelete = items;

    var modal = $('mediaDeleteModal');
    var target = $('mediaDeleteTarget');
    var message = $('mediaDeleteMessage');
    var confirm = $('mediaDeleteConfirm');
    var lines = [];
    lines.push(counts.macCount + ' Mac file(s) will be deleted');
    lines.push(counts.vpsCount + ' VPS file(s) will be deleted');
    if (target) target.textContent = lines.join('\n');
    if (message) {
      message.textContent = 'This will delete ' + counts.macCount + ' Mac file(s) and ' +
        counts.vpsCount + ' VPS file(s).';
    }
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
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-delete-open');
    }
    state.pendingDelete = null;
  }

  async function deleteSelectedMacCopies(items) {
    var macItems = (items || []).filter(itemHasMac);
    if (!macItems.length) return { deletedCount: 0 };
    if (!state.localSessionId || !state.macHelperAvailable) {
      throw new Error('Start the HXYLIVE Mac helper to delete Mac copies');
    }
    var res = await helperPostOrQueue('/delete', '/api/mac/helper/delete', {
      localSessionId: state.localSessionId,
      items: macItems.map(function(item) {
        return {
          relativePath: item.macRelativePath || '',
          recordingId: item.recordingId || ''
        };
      })
    }, 2500);
    if (res && res.status === 'queued') {
      await new Promise(function(resolve) { setTimeout(resolve, 1500); });
    }
    return res;
  }

  function buildDeletePlan(items) {
    var groups = classifyDeleteItems(items);
    return { vpsItems: groups.vpsOnly.slice(), macItems: groups.macOnly.slice() };
  }

  async function confirmDeleteMedia() {
    var items = state.pendingDelete;
    if (!items || !items.length) return;
    var plan = buildDeletePlan(items);
    var vpsItems = plan.vpsItems;
    var macItems = plan.macItems;
    if (!vpsItems.length && !macItems.length) {
      showToast('Select at least one video to delete', 'error');
      return;
    }
    var confirm = $('mediaDeleteConfirm');
    function setBusy(busy) {
      if (confirm) {
        confirm.disabled = !!busy;
        confirm.textContent = busy ? 'Deleting...' : (confirm.dataset.idleLabel || 'Delete');
      }
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
    var rawQuota = source.monthlyQuotaGb;
    if (rawQuota === undefined) rawQuota = source.monthly_quota_gb;
    var monthlyQuotaGb = null;
    if (rawQuota !== null && rawQuota !== undefined && rawQuota !== '') {
      monthlyQuotaGb = parseInt(rawQuota, 10);
      if (Number.isNaN(monthlyQuotaGb)) monthlyQuotaGb = null;
      else monthlyQuotaGb = Math.max(0, Math.min(10000, monthlyQuotaGb));
    }
    return {
      sourceType: sourceType,
      channelUsername: source.channelUsername || source.channel_username || source.username || fallbackUsername || '',
      channelUrl: channelUrl,
      recordQuality: source.recordQuality || source.record_quality || 'best',
      retentionDays: retention,
      monthlyQuotaGb: monthlyQuotaGb,
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
      var autoOn = !!source.autoRecord;
      return '' +
        '<div class="media-profile-source-row" data-source-index="' + index + '">' +
          '<div class="media-ps-source-top">' +
            '<input data-source-field="channelUsername" type="hidden" value="' + escapeHtml(source.channelUsername) + '">' +
            '<label class="media-ps-field">Source<select data-source-field="sourceType">' + sourceOptionsHtml(source.sourceType) + '</select></label>' +
            '<button class="media-source-remove-btn" data-source-action="remove" type="button" title="Remove source" aria-label="Remove source">&#215;</button>' +
          '</div>' +
          '<label class="media-ps-field media-ps-field-url">URL<input data-source-field="channelUrl" type="url" autocomplete="off" value="' + escapeHtml(source.channelUrl) + '" placeholder="https://..."></label>' +
          '<div class="media-ps-source-meta">' +
            '<label class="media-ps-field">Quality<select data-source-field="recordQuality">' + qualityOptionsHtml(source.recordQuality) + '</select></label>' +
            '<label class="media-ps-field">Retention (days)<input data-source-field="retentionDays" type="number" min="0" max="365" value="' + escapeHtml(source.retentionDays) + '"></label>' +
            '<label class="media-ps-field" title="0 means unlimited. Uses the Settings default when left at that value.">Monthly quota (GB)<input data-source-field="monthlyQuotaGb" type="number" min="0" max="10000" value="' +
              escapeHtml(source.monthlyQuotaGb == null ? state.defaultMonthlyQuotaGb : source.monthlyQuotaGb) + '"></label>' +
            '<button type="button" class="media-settings-toggle' + (autoOn ? ' is-active' : '') +
              '" data-source-field="autoRecord" aria-pressed="' + (autoOn ? 'true' : 'false') +
              '"><span class="media-settings-toggle-indicator" aria-hidden="true"></span>' +
              '<span class="media-settings-toggle-copy">' +
                '<span class="media-settings-toggle-title">Auto-record</span>' +
                '<span class="media-settings-toggle-state">' + (autoOn ? 'On' : 'Off') + '</span>' +
              '</span></button>' +
          '</div>' +
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
        if (el.getAttribute('aria-pressed') != null) {
          return el.getAttribute('aria-pressed') === 'true' || el.classList.contains('is-active');
        }
        return el.value.trim();
      }
      var channelUrl = get('channelUrl');
      var selectedSource = (get('sourceType') || '').toLowerCase();
      var urlSource = sourceTypeFromUrl(channelUrl);
      if (urlSource && (!selectedSource || selectedSource === 'chaturbate')) selectedSource = urlSource;
      var retention = parseInt(get('retentionDays'), 10);
      if (Number.isNaN(retention)) retention = 30;
      var quotaRaw = get('monthlyQuotaGb');
      var monthlyQuotaGb = parseInt(quotaRaw, 10);
      if (Number.isNaN(monthlyQuotaGb)) {
        monthlyQuotaGb = state.defaultMonthlyQuotaGb;
      } else {
        monthlyQuotaGb = Math.max(0, Math.min(10000, monthlyQuotaGb));
      }
      return {
        sourceType: selectedSource || 'chaturbate',
        channelUsername: channelUsernameFromUrl(channelUrl) || normalizeProfileUsername(get('channelUsername')),
        channelUrl: channelUrl,
        recordQuality: get('recordQuality') || 'best',
        retentionDays: Math.max(0, Math.min(365, retention)),
        monthlyQuotaGb: monthlyQuotaGb,
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
    if (title) title.textContent = state.creatingProfile ? 'New streamer' : 'Profile settings';
    if (subtitle) subtitle.textContent = state.creatingProfile ? 'Create a local Media card' : (profile.username || '');

    var usernameField = $('profileUsernameField');
    var usernameInput = $('profileUsername');
    if (usernameField) usernameField.style.display = state.creatingProfile ? 'grid' : 'none';
    if (usernameInput) {
      usernameInput.value = profile.username || '';
      usernameInput.disabled = !state.creatingProfile;
    }

    setField('profileDisplayName', profile.displayName || '');
    var gapSelect = $('profileGapBufferMinutes');
    if (gapSelect) {
      var gapRaw = profile.gap_buffer_minutes != null ? profile.gap_buffer_minutes : profile.gapBufferMinutes;
      if (gapRaw == null || gapRaw === '') {
        gapSelect.value = '';
      } else {
        var gap = parseInt(gapRaw, 10);
        if (GAP_BUFFER_MINUTES_OPTIONS.indexOf(gap) === -1) gapSelect.value = '';
        else gapSelect.value = String(gap);
      }
    }
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

  function fillProfileSettingsFromLocal(profile) {
    if (!profile) return;
    fillProfileSettings({
      username: profile.username,
      displayName: profile.displayName || profile.display_name || profile.username,
      channelUsername: profile.channelUsername || profile.channel_username || profile.username,
      channelUrl: profile.channelUrl || profile.channel_url || '',
      sourceType: profile.sourceType || profile.source_type || 'chaturbate',
      source_type: profile.sourceType || profile.source_type || 'chaturbate',
      autoRecord: !!profile.autoRecord,
      recordQuality: profile.recordQuality || 'best',
      retentionDays: profile.retentionDays == null ? 30 : profile.retentionDays,
      streamSources: profile.streamSources || profile.stream_sources || [],
      stream_sources: profile.streamSources || profile.stream_sources || [],
      profileImageUrl: profile.profileImageUrl || profile.profile_image_url || '',
      gap_buffer_minutes: profile.gap_buffer_minutes != null ? profile.gap_buffer_minutes : profile.gapBufferMinutes,
      gapBufferMinutes: profile.gapBufferMinutes != null ? profile.gapBufferMinutes : profile.gap_buffer_minutes
    });
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
      if (!res.ok) {
        var local = profileByUsername(state.selectedProfile);
        if (local && local.fromMacLocal) {
          fillProfileSettingsFromLocal(local);
        } else {
          throw new Error(data.detail || 'Profile unavailable');
        }
      } else {
        fillProfileSettings(data);
      }
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

    var profileSources = readProfileSources();
    syncLegacyStreamFields(profileSources);
    var retention = parseInt(fieldValue('profileRetentionDays'), 10);
    if (Number.isNaN(retention)) retention = 30;
    retention = Math.max(0, Math.min(365, retention));
    var auto = $('profileAutoRecord');
    var source = $('profileSourceType');
    var gapSelect = $('profileGapBufferMinutes');
    var gapMinutes = null;
    if (gapSelect && String(gapSelect.value || '').trim() !== '') {
      gapMinutes = parseInt(gapSelect.value, 10);
      if (GAP_BUFFER_MINUTES_OPTIONS.indexOf(gapMinutes) === -1) gapMinutes = null;
    }
    var existing = state.profileSettings || profileByUsername(username) || {};
    var payload = {
      displayName: fieldValue('profileDisplayName') || existing.displayName || existing.display_name || username,
      firstName: existing.firstName || existing.first_name || '',
      lastName: existing.lastName || existing.last_name || '',
      birthDate: existing.birthDate || existing.birth_date || '',
      profileImageUrl: existing.profileImageUrl || existing.profile_image_url || '',
      profileImageSourceUrl: existing.profileImageSourceUrl || existing.profile_image_source_url || '',
      age: existing.age == null ? null : existing.age,
      aliases: existing.aliases || '',
      tags: existing.tags || '',
      address: existing.address || '',
      city: existing.city || '',
      region: existing.region || '',
      postalCode: existing.postalCode || existing.postal_code || '',
      country: existing.country || '',
      socialUrls: existing.socialUrls || existing.social_urls || [],
      streamUrls: existing.streamUrls || existing.stream_urls || [],
      profileUrls: existing.profileUrls || existing.profile_urls || [],
      notes: existing.notes || '',
      recordQuality: fieldValue('profileRecordQuality') || 'best',
      retentionDays: retention,
      sourceType: source ? source.value : 'chaturbate',
      autoRecord: auto ? auto.checked : false,
      streamSources: profileSources,
      gap_buffer_minutes: gapMinutes,
      gapBufferMinutes: gapMinutes
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
        save.textContent = 'Save changes';
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
      confirm.textContent = 'Delete card';
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
        confirm.textContent = 'Delete card';
      }
    }
  }

  function syncFilterControls() {
    var buttons = document.querySelectorAll('.media-kind-btn');
    buttons.forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.kind === state.kind);
    });

    var sortField = state.sortField === 'size' ? 'size' : 'name';
    var sortDesc = state.sortDir !== 'asc';
    document.querySelectorAll('#mediaSortButtons [data-sort-field]').forEach(function(button) {
      var field = button.dataset.sortField || 'name';
      var active = field === sortField;
      button.classList.toggle('active', active);
      var desc = active ? sortDesc : true;
      if (field === 'name') {
        button.textContent = desc ? 'Newest first' : 'Oldest first';
      } else {
        button.textContent = desc ? 'Largest first' : 'Smallest first';
      }
    });

    var selectedDevices = normalizeDeviceFilters(state.deviceFilters);
    state.deviceFilters = selectedDevices;
    document.querySelectorAll('#mediaDeviceFilters [data-device]').forEach(function(button) {
      var device = button.dataset.device || '';
      button.classList.toggle('active', selectedDevices.indexOf(device) !== -1);
      if (device === 'mac') {
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
    var selectPage = $('mediaSelectPageBtn');
    var clearSelection = $('mediaClearSelectionBtn');
    var download = $('mediaDownloadSelectedBtn');
    var deleteSelected = $('mediaDeleteSelectedBtn');
    var concatSelected = $('mediaConcatSelectedBtn');
    var selectedCount = Object.keys(state.selectedItemIds).length;
    var selectableCount = state.videoViewMode === 'projects' && !state.projectDetailId
      ? visibleProjects().length
      : state.items.filter(itemIsSelectable).length;
    var downloadableSelected = Object.keys(state.selectedItemIds).filter(function(id) {
      return itemIsDownloadable(itemById(id));
    }).length;
    var deletableSelected = Object.keys(state.selectedItemIds).filter(function(id) {
      return itemIsSelectable(itemById(id));
    }).length;
    var concatCount = selectedConcatableProjectIds().length;
    if (status) {
      status.textContent = state.macScanInProgress ? 'Scanning Mac folder...' : 'Refresh Mac folder';
    }
    if (rescan) rescan.disabled = false;
    if (selectAll) selectAll.disabled = selectableCount === 0;
    if (selectPage) selectPage.disabled = selectableCount === 0;
    if (clearSelection) clearSelection.disabled = selectedCount === 0 && !Object.keys(state.selectedProjectIds || {}).length;
    if (download) {
      download.textContent = 'Download selected (' + downloadableSelected + ')';
      download.disabled = !state.macHelperAvailable || downloadableSelected === 0;
    }
    if (deleteSelected) {
      deleteSelected.textContent = 'Delete selected (' + deletableSelected + ')';
      deleteSelected.disabled = deletableSelected === 0;
    }
    if (concatSelected) {
      concatSelected.textContent = 'Concat selected (' + concatCount + ')';
      concatSelected.disabled = concatCount === 0;
    }
    syncFilterControls();
    syncProjectFilterControls();
  }

  function applySyncStatusesToItems() {
    rebuildVisibleItems();
  }

  function applyMacSnapshot(snapshot, direct) {
    state.localSessionId = snapshot.localSessionId || '';
    state.macHelperAvailable = !!state.localSessionId;
    state.macHelperDirect = !!direct;
    state.macFiles = Array.isArray(snapshot.files) ? snapshot.files : [];
    state.syncStatuses = snapshot.statuses || {};
    state.syncScannedAt = snapshot.scannedAt || Math.floor(Date.now() / 1000);
    if (ensureProfilesForLocalVideos()) {
      renderProfileCarousel();
    }
  }

  function helperRequestTimeout(ms) {
    var controller = new AbortController();
    var timer = setTimeout(function() { controller.abort(); }, ms);
    return {
      signal: controller.signal,
      clear: function() { clearTimeout(timer); }
    };
  }

  async function fetchLocalMacScan(timeoutMs) {
    var timeout = helperRequestTimeout(timeoutMs || 2000);
    try {
      var localRes = await fetch(MAC_HELPER_BASE + '/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        cache: 'no-store',
        signal: timeout.signal
      });
      if (!localRes.ok) throw new Error('Local Mac helper did not accept the scan');
      return await localRes.json();
    } finally {
      timeout.clear();
    }
  }

  async function helperPostLocal(path, body, timeoutMs) {
    var timeout = helperRequestTimeout(timeoutMs || 2000);
    try {
      var res = await fetch(MAC_HELPER_BASE + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        cache: 'no-store',
        signal: timeout.signal
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.error || 'Mac helper request failed');
      return data;
    } finally {
      timeout.clear();
    }
  }

  async function helperPostOrQueue(path, vpsPath, body, timeoutMs) {
    try {
      return await helperPostLocal(path, body, timeoutMs);
    } catch (_) {
      var res = await fetch(vpsPath, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        cache: 'no-store'
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) {
        throw new Error(data.detail || data.error || 'Start the HXYLIVE Mac helper, then retry');
      }
      return data;
    }
  }

  async function fetchVpsMacSnapshot() {
    var remoteRes = await fetch('/api/mac/helper/snapshot', { cache: 'no-store' });
    if (!remoteRes.ok) throw new Error('VPS could not read the Mac helper snapshot');
    var remote = await remoteRes.json();
    if (!remote || !remote.available || !remote.localSessionId) {
      throw new Error('Start the HXYLIVE Mac helper, then rescan');
    }
    return remote;
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
      var localCount = 0;
      var compared = null;
      try {
        var snapshot = await fetchLocalMacScan();
        try {
          var compareRes = await fetch('/api/mac/sync-snapshot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              localSessionId: snapshot.localSessionId,
              files: Array.isArray(snapshot.files)
                ? snapshot.files.map(function(file) {
                    return {
                      recordingId: file.recordingId || null,
                      filename: file.filename || '',
                      size: intSize(file.size),
                      durationSeconds: numberOrZero(file.durationSeconds || file.duration) || null,
                      resolution: String(file.resolution || '').trim() || null,
                      fps: Number(file.fps) > 0 ? Number(file.fps) : null,
                      bitrate: numberOrZero(file.bitrate) || null
                    };
                  })
                : []
            }),
            cache: 'no-store'
          });
          if (compareRes.ok) {
            compared = await compareRes.json();
          }
        } catch (_) {
          compared = null;
        }
        // Keep a successful local scan even when VPS compare fails.
        applyMacSnapshot({
          localSessionId: snapshot.localSessionId,
          files: snapshot.files,
          statuses: (compared && compared.statuses) || {},
          scannedAt: (compared && compared.scannedAt) || Math.floor(Date.now() / 1000)
        }, true);
        localCount = state.macFiles.length;
      } catch (_) {
        var remote = null;
        var lastErr = null;
        var attempt = 0;
        while (attempt < 3) {
          try {
            remote = await fetchVpsMacSnapshot();
            break;
          } catch (err) {
            lastErr = err;
            attempt += 1;
            if (attempt < 3) {
              await new Promise(function(resolve) { setTimeout(resolve, 1000); });
            }
          }
        }
        if (!remote) throw lastErr || new Error('Start the HXYLIVE Mac helper, then rescan');
        applyMacSnapshot(remote, false);
        localCount = state.macFiles.length;
        compared = remote;
      }
      applySyncStatusesToItems();
      if (!silent) {
        var purged = (compared && compared.purgedVps) || 0;
        showToast(
          'Mac scan: ' + localCount + ' local file(s), ' +
          ((compared && compared.notSynced) || 0) + ' still on VPS' +
          (purged ? ', ' + purged + ' freed from VPS' : ''),
          'success'
        );
      }
    } catch (e) {
      state.macHelperAvailable = false;
      state.macHelperDirect = false;
      state.localSessionId = '';
      state.macFiles = [];
      state.syncStatuses = {};
      state.syncScannedAt = 0;
      if (deviceFilterMode() === 'mac') {
        state.deviceFilters = ['vps'];
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
    if (checked && item && !itemIsSelectable(item)) return;
    if (checked) state.selectedItemIds[itemId] = true;
    else delete state.selectedItemIds[itemId];
    if (checked) {
      (state.projects || []).forEach(function(project) {
        var ids = projectMemberItemIds(project);
        if (ids.indexOf(String(itemId)) !== -1) {
          state.selectedProjectIds[project.projectId] = true;
        }
      });
    } else {
      (state.projects || []).forEach(function(project) {
        var ids = projectMemberItemIds(project);
        if (ids.indexOf(String(itemId)) === -1) return;
        var anySelected = ids.some(function(id) { return !!state.selectedItemIds[id]; });
        if (!anySelected) delete state.selectedProjectIds[project.projectId];
      });
    }
    // Toggle class in place — replacing outerHTML drops :hover and re-lifts.
    var card = document.querySelector('.media-card[data-media-id="' + String(itemId).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]');
    if (card) {
      card.classList.toggle('selected', !!checked);
    } else if (item) {
      refreshMediaCard(item);
    }
    updateMacToolbar();
  }

  function selectAllVisibleVideos() {
    if (state.videoViewMode === 'projects' && !state.projectDetailId) {
      visibleProjects().forEach(function(project) {
        selectProject(project.projectId, true);
      });
      return;
    }
    state.selectedItemIds = {};
    state.items.forEach(function(item) {
      if (itemIsSelectable(item)) state.selectedItemIds[item.id] = true;
    });
    renderGrid(state.items.length);
    updateMacToolbar();
  }

  function selectAllPageVideos() {
    if (state.videoViewMode === 'projects' && !state.projectDetailId) {
      var start = (state.mediaPage - 1) * MEDIA_PAGE_SIZE;
      visibleProjects().slice(start, start + MEDIA_PAGE_SIZE).forEach(function(project) {
        selectProject(project.projectId, true);
      });
      return;
    }
    var pageStart = (state.mediaPage - 1) * MEDIA_PAGE_SIZE;
    state.items.slice(pageStart, pageStart + MEDIA_PAGE_SIZE).forEach(function(item) {
      if (itemIsSelectable(item)) state.selectedItemIds[item.id] = true;
    });
    renderGrid(state.items.length);
    updateMacToolbar();
  }

  function clearVideoSelection() {
    state.selectedItemIds = {};
    state.selectedProjectIds = {};
    if (state.projectDetailId) renderProjectDetail(state.projectDetailId);
    else renderGrid(state.items.length);
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
      var data = await helperPostOrQueue('/open', '/api/mac/helper/open', {
        localSessionId: state.localSessionId,
        relativePath: item.macRelativePath || '',
        recordingId: item.recordingId || '',
        reveal: reveal
      }, 2500);
      var shown = data.relativePath || item.macRelativePath || item.filename;
      showToast((reveal ? 'Showed in Finder: ' : 'Opened on Mac: ') + shown, 'success');
    } catch (e) {
      showToast(e.message || (reveal ? 'Show folder failed' : 'Local open failed'), 'error');
    }
  }

  function activateMediaItem(item) {
    if (!item) return;
    if (itemHasMac(item)) {
      openLocally(item);
      return;
    }
    if (itemHasVps(item)) openViewer(item);
  }

  function setSortField(field) {
    var next = field === 'size' ? 'size' : 'name';
    if (state.sortField === next) {
      state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      state.sortField = next;
      state.sortDir = 'desc';
    }
    state.mediaPage = 1;
    syncFilterControls();
    rebuildVisibleItems();
  }

  function resetVideoFilters() {
    state.dateFrom = emptyDateOverride();
    state.dateTo = emptyDateOverride();
    state.sortField = 'name';
    state.sortDir = 'desc';
    state.deviceFilters = ['vps', 'mac'];
    state.filterProjectStatus = 'all';
    state.filterCompletedSync = 'all';
    state.filterConcatStatus = 'all';
    state.mediaPage = 1;
    syncDateRangePlaceholders();
    syncFilterControls();
    syncProjectFilterControls();
    rebuildVisibleItems();
    clearStreamerSelection();
    clearVideoSelection();
  }

  function applyToolbarCompactMode() {
    document.body.classList.toggle('media-toolbar-compact', !!toolbarCompact);
    var streamerBar = document.querySelector('.media-streamer-toolbar');
    var videoBar = document.querySelector('.media-video-toolbar');
    if (streamerBar) streamerBar.setAttribute('data-compact', toolbarCompact ? '1' : '0');
    if (videoBar) videoBar.setAttribute('data-compact', toolbarCompact ? '1' : '0');
  }

  function toggleToolbarCompactMode() {
    toolbarCompact = !toolbarCompact;
    applyToolbarCompactMode();
  }

  async function setDeviceFilter(device) {
    var token = String(device || '').toLowerCase();
    if (token !== 'vps' && token !== 'mac') return;
    var selected = normalizeDeviceFilters(state.deviceFilters);
    var index = selected.indexOf(token);
    if (index >= 0) {
      if (selected.length <= 1) return;
      selected.splice(index, 1);
    } else {
      selected.push(token);
    }
    state.deviceFilters = normalizeDeviceFilters(selected);
    state.mediaPage = 1;
    syncFilterControls();
    var mode = deviceFilterMode();
    if ((mode === 'mac' || mode === 'all') && (!state.macHelperAvailable || !state.syncScannedAt)) {
      if (!state.macScanInProgress) {
        await scanMacAndRefresh(true);
      }
      if (!state.macHelperAvailable && !state.macScanInProgress) {
        if (mode === 'mac') {
          showToast('Start the HXYLIVE Mac helper to browse Mac videos', 'error');
          state.deviceFilters = ['vps'];
        } else {
          showToast('Mac helper unavailable — showing VPS library', 'info');
          state.deviceFilters = ['vps'];
        }
        syncFilterControls();
      }
    }
    rebuildVisibleItems();
  }

  function itemStillWaitingForMac(id) {
    var item = itemById(id);
    // After a successful Mac sync the VPS copy is purged, so the VPS row is gone.
    if (!item || !itemHasVps(item)) return false;
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

  async function refreshMacListFromHelperSnapshot() {
    var remote = await fetchVpsMacSnapshot();
    applyMacSnapshot(remote, state.macHelperDirect);
    applySyncStatusesToItems();
    updateMacToolbar();
  }

  async function refreshMacListAfterFile() {
    var purged = 0;
    try {
      var snapshot = await fetchLocalMacScan(15000);
      var compareRes = await fetch('/api/mac/sync-snapshot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          localSessionId: snapshot.localSessionId,
          files: Array.isArray(snapshot.files) ? snapshot.files : []
        }),
        cache: 'no-store'
      });
      if (!compareRes.ok) throw new Error('VPS could not compare the Mac folder');
      var compared = await compareRes.json();
      purged = Number(compared.purgedVps || 0) || 0;
      applyMacSnapshot({
        localSessionId: snapshot.localSessionId,
        files: snapshot.files,
        statuses: compared.statuses,
        scannedAt: compared.scannedAt
      }, true);
    } catch (_) {
      await refreshMacListFromHelperSnapshot();
    }
    if (purged > 0) {
      await loadMediaLibrary();
    } else {
      applySyncStatusesToItems();
    }
    updateMacToolbar();
    return purged;
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
        var timeout = helperRequestTimeout(28000);
        try {
          var res = await fetch(MAC_HELPER_BASE + '/wait-filed', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              localSessionId: state.localSessionId,
              recordingIds: recordingIds,
              timeoutMs: 25000
            }),
            cache: 'no-store',
            signal: timeout.signal
          });
          var data = await res.json().catch(function() { return {}; });
          filedNow = !!(res.ok && data && Array.isArray(data.filed) && data.filed.length);
        } catch (_) {
          filedNow = false;
        } finally {
          timeout.clear();
        }
      }
      if (generation !== fileWatchGeneration) return;
      if (filedNow) {
        var purged = 0;
        try { purged = await refreshMacListAfterFile() || 0; } catch (_) {}
        if (generation !== fileWatchGeneration) return;
        if (!ids.filter(itemStillWaitingForMac).length) {
          fileWatchTick = null;
          fileWatchTimer = null;
          showToast(
            ids.length + ' video(s) filed on Mac' +
            (purged ? ', ' + purged + ' freed from VPS' : ''),
            'success'
          );
          return;
        }
      }
      fileWatchTimer = setTimeout(tick, filedNow ? 200 : 800);
    }

    fileWatchTick = tick;
    fileWatchTimer = setTimeout(tick, 200);
  }

  function downloadableSelectedIds() {
    return Object.keys(state.selectedItemIds).filter(function(id) {
      return itemIsDownloadable(itemById(id));
    });
  }

  function openDownloadMethodModal() {
    var ids = downloadableSelectedIds();
    if (!ids.length || !state.localSessionId || !state.macHelperAvailable) {
      if (!state.macHelperAvailable) {
        showToast('Start the HXYLIVE Mac helper to download to this Mac', 'error');
      }
      return;
    }
    var modal = $('mediaDownloadMethodModal');
    var target = $('mediaDownloadMethodTarget');
    var motrix = $('mediaDownloadWithMotrix');
    var chrome = $('mediaDownloadWithChrome');
    if (target) {
      target.textContent = ids.length === 1
        ? '1 video selected'
        : (ids.length + ' videos selected');
    }
    if (motrix) {
      motrix.disabled = false;
      motrix.dataset.idleLabel = motrix.dataset.idleLabel || 'Motrix';
    }
    if (chrome) {
      chrome.disabled = false;
      chrome.dataset.idleLabel = chrome.dataset.idleLabel || 'Google Chrome';
    }
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    }
  }

  function closeDownloadMethodModal() {
    var modal = $('mediaDownloadMethodModal');
    var motrix = $('mediaDownloadWithMotrix');
    var chrome = $('mediaDownloadWithChrome');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-delete-open');
    }
    if (motrix) motrix.disabled = false;
    if (chrome) chrome.disabled = false;
  }

  function normalizeDownloadMethod(method) {
    var value = String(method || '').trim().toLowerCase();
    return value === 'motrix' ? 'motrix' : 'chrome';
  }

  async function downloadSelectedToMac(method) {
    var downloadMethod = normalizeDownloadMethod(method);
    var ids = downloadableSelectedIds();
    if (!ids.length || !state.localSessionId) return;
    var button = $('mediaDownloadSelectedBtn');
    var motrixBtn = $('mediaDownloadWithMotrix');
    var chromeBtn = $('mediaDownloadWithChrome');
    if (button) button.disabled = true;
    if (motrixBtn) motrixBtn.disabled = true;
    if (chromeBtn) chromeBtn.disabled = true;
    try {
      var createRes = await fetch('/api/mac/download-jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          localSessionId: state.localSessionId,
          itemIds: ids,
          method: downloadMethod
        })
      });
      if (!createRes.ok) {
        var createDetail = await createRes.json().catch(function() { return {}; });
        throw new Error(createDetail.detail || createDetail.message || 'Could not create the VPS download batch');
      }
      var job = await createRes.json();
      var timeout = helperRequestTimeout(2500);
      try {
        if (job.jobId) {
          await fetch(MAC_HELPER_BASE + '/dispatch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              localSessionId: state.localSessionId,
              jobId: job.jobId,
              vpsBase: window.location.origin,
              method: downloadMethod
            }),
            cache: 'no-store',
            signal: timeout.signal
          });
        }
      } catch (_) {
        // Helper claims scheduler jobs from VPS heartbeat when localhost is unreachable.
      } finally {
        timeout.clear();
      }
      closeDownloadMethodModal();
      var label = downloadMethod === 'motrix' ? 'Motrix' : 'Chrome';
      var added = Number(job.addedCount != null ? job.addedCount : ids.length) || ids.length;
      showToast(added + ' download(s) queued for ' + label + ' (scheduler fills free slots)', 'success');
      state.selectedItemIds = {};
      state.selectedProjectIds = {};
      watchFiledDownloads(ids);
    } catch (e) {
      showToast(e.message || 'Download batch failed', 'error');
      if (motrixBtn) motrixBtn.disabled = false;
      if (chromeBtn) chromeBtn.disabled = false;
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
    if (!next) {
      clearStreamerSelection();
      if (shouldScroll) {
        var results = document.querySelector('.media-recent-section');
        if (results && results.scrollIntoView) {
          results.scrollIntoView({ block: 'start', behavior: 'smooth' });
        }
      }
      return;
    }
    // Video-card profile links focus one streamer; card clicks toggle via toggleStreamerSelection.
    state.selectedStreamerIds = {};
    state.selectedStreamerIds[next] = true;
    jumpProfilePageToUsername(next);
    applyStreamerSelectionFilter(!!shouldScroll);
  }

  function setFilterProfile(profile) {
    selectProfile(profile || '', false);
  }

  function timelineSelectedUsernames() {
    return selectedStreamerList().slice().sort(function(a, b) {
      return a.toLowerCase().localeCompare(b.toLowerCase());
    });
  }

  function scheduleTimelineReload() {
    if (state.timelineLoadTimer) {
      clearTimeout(state.timelineLoadTimer);
      state.timelineLoadTimer = null;
    }
    state.timelineLoadTimer = setTimeout(function() {
      state.timelineLoadTimer = null;
      loadTimeline(true);
    }, 120);
  }

  function setTimelineRangeButtons() {
    var root = $('mediaTimelineRangeButtons');
    if (!root) return;
    root.querySelectorAll('[data-timeline-days]').forEach(function(btn) {
      var days = Number(btn.getAttribute('data-timeline-days') || 0);
      btn.classList.toggle('active', days === state.timelineDays);
    });
  }

  function formatTimelineTick(ts, spanSeconds) {
    var date = new Date(ts * 1000);
    if (spanSeconds > 10 * 86400) {
      return (date.getMonth() + 1) + '/' + date.getDate();
    }
    if (spanSeconds > 2 * 86400) {
      return (date.getMonth() + 1) + '/' + date.getDate() + ' ' +
        String(date.getHours()).padStart(2, '0') + ':00';
    }
    return String(date.getHours()).padStart(2, '0') + ':' +
      String(date.getMinutes()).padStart(2, '0');
  }

  function renderTimelineChart(data) {
    var svg = $('mediaTimelineSvg');
    var empty = $('mediaTimelineEmpty');
    var meta = $('mediaTimelineMeta');
    var selected = timelineSelectedUsernames();
    if (!svg || !empty) return;

    if (!selected.length) {
      svg.hidden = true;
      empty.hidden = false;
      empty.textContent = 'Select streamer cards above to show their live vs recorded coverage.';
      if (meta) meta.textContent = ': Select streamers above';
      return;
    }

    var streamers = (data && data.streamers) || [];
    var colorByUser = {};
    streamers.forEach(function(row) {
      colorByUser[String(row.username || '').toLowerCase()] = row.color || '#22d3ee';
    });
    selected.forEach(function(name, index) {
      var key = name.toLowerCase();
      if (!colorByUser[key]) {
        var palette = ['#22d3ee', '#f472b6', '#a3e635', '#fb923c', '#38bdf8', '#e879f9', '#facc15', '#4ade80'];
        colorByUser[key] = palette[index % palette.length];
      }
    });

    var fromTs = Number((data && data.from) || 0);
    var toTs = Number((data && data.to) || Math.floor(Date.now() / 1000));
    if (toTs <= fromTs) toTs = fromTs + 3600;
    var span = toTs - fromTs;

    // One shared bar height for track / live / recorded — only fill style differs.
    var labelWidth = 168;
    var rightPad = 18;
    var topPad = 34;
    var rowHeight = 52;
    var barHeight = 18;
    var width = Math.max(640, (svg.parentElement && svg.parentElement.clientWidth) || 720);
    var height = topPad + selected.length * rowHeight + 10;
    var plotWidth = Math.max(200, width - labelWidth - rightPad);
    var plotLeft = labelWidth;

    function xFor(ts) {
      var clamped = Math.max(fromTs, Math.min(toTs, Number(ts) || fromTs));
      return plotLeft + ((clamped - fromTs) / span) * plotWidth;
    }

    var liveByUser = {};
    var recByUser = {};
    selected.forEach(function(name) {
      liveByUser[name.toLowerCase()] = [];
      recByUser[name.toLowerCase()] = [];
    });
    ((data && data.live) || []).forEach(function(seg) {
      var key = String(seg.username || '').toLowerCase();
      if (!liveByUser[key]) return;
      liveByUser[key].push(seg);
    });
    ((data && data.recordings) || []).forEach(function(seg) {
      var key = String(seg.username || '').toLowerCase();
      if (!recByUser[key]) return;
      recByUser[key].push(seg);
    });

    var parts = [];
    parts.push('<rect x="0" y="0" width="' + width + '" height="' + height + '" fill="transparent"></rect>');
    parts.push(
      '<line x1="' + plotLeft + '" y1="' + (topPad - 10) + '" x2="' + plotLeft +
      '" y2="' + height + '" stroke="rgba(148,163,184,0.22)" stroke-width="1"></line>'
    );

    var tickCount = Math.min(8, Math.max(4, Math.floor(plotWidth / 90)));
    for (var t = 0; t <= tickCount; t++) {
      var ts = fromTs + (span * t / tickCount);
      var x = xFor(ts);
      parts.push(
        '<line x1="' + x + '" y1="' + (topPad - 8) + '" x2="' + x + '" y2="' + height +
        '" stroke="rgba(148,163,184,0.12)" stroke-width="1"></line>'
      );
      parts.push(
        '<text x="' + x + '" y="18" fill="rgba(186,198,214,0.95)" font-size="12" font-weight="500" text-anchor="middle">' +
        escapeHtml(formatTimelineTick(ts, span)) + '</text>'
      );
    }

    selected.forEach(function(name, index) {
      var key = name.toLowerCase();
      var color = colorByUser[key] || '#22d3ee';
      var y = topPad + index * rowHeight;
      var mid = y + rowHeight / 2;
      var barY = mid - barHeight / 2;
      var profile = profileByUsername(name) || {};
      var label = String(profile.displayName || name);
      var maxChars = 16;
      var shortLabel = label.length > maxChars ? label.slice(0, maxChars - 1) + '…' : label;

      if (index > 0) {
        parts.push(
          '<line x1="0" y1="' + y + '" x2="' + width + '" y2="' + y +
          '" stroke="rgba(148,163,184,0.12)" stroke-width="1"></line>'
        );
      }

      parts.push(
        '<text x="14" y="' + (mid + 6) + '" fill="' + color +
        '" font-size="15" font-weight="700" letter-spacing="0.01em">' +
        escapeHtml(shortLabel) + '</text>'
      );

      // Full-day track (idle / no live): same height as live + recorded overlays.
      parts.push(
        '<rect x="' + plotLeft + '" y="' + barY + '" width="' + plotWidth +
        '" height="' + barHeight + '" rx="5" fill="rgba(148,163,184,0.14)" ' +
        'stroke="rgba(148,163,184,0.28)" stroke-width="1"></rect>'
      );

      (liveByUser[key] || []).forEach(function(seg) {
        var x1 = xFor(seg.start);
        var x2 = xFor(seg.end);
        var w = Math.max(2, x2 - x1);
        parts.push(
          '<rect x="' + x1 + '" y="' + barY + '" width="' + w + '" height="' + barHeight +
          '" rx="5" fill="' + color + '" fill-opacity="0.34">' +
          '<title>' + escapeHtml(label) + ' live&#10;' +
          escapeHtml(new Date(seg.start * 1000).toLocaleString()) + ' → ' +
          escapeHtml(new Date(seg.end * 1000).toLocaleString()) +
          (seg.open ? ' (still live)' : '') + '</title></rect>'
        );
      });

      (recByUser[key] || []).forEach(function(seg) {
        var x1 = xFor(seg.start);
        var x2 = xFor(seg.end);
        var w = Math.max(2, x2 - x1);
        var locs = (seg.locations || [seg.location || 'vps']).join('+');
        parts.push(
          '<rect x="' + x1 + '" y="' + barY + '" width="' + w + '" height="' + barHeight +
          '" rx="5" fill="' + color + '" fill-opacity="0.96">' +
          '<title>' + escapeHtml(label) + ' recorded (' + escapeHtml(locs) + ')&#10;' +
          escapeHtml(new Date(seg.start * 1000).toLocaleString()) + ' → ' +
          escapeHtml(new Date(seg.end * 1000).toLocaleString()) + '</title></rect>'
        );
      });
    });

    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    svg.innerHTML = parts.join('');
    svg.hidden = false;
    empty.hidden = true;

    var liveCount = ((data && data.live) || []).length;
    var recCount = ((data && data.recordings) || []).length;
    if (meta) {
      meta.textContent = ': ' + selected.length + ' streamer' + (selected.length === 1 ? '' : 's') +
        ', ' + liveCount + ' live · ' + recCount + ' recorded';
    }
  }

  async function loadTimeline(force) {
    var selected = timelineSelectedUsernames();
    var meta = $('mediaTimelineMeta');
    if (!selected.length) {
      state.timelineData = null;
      renderTimelineChart(null);
      return;
    }
    if (state.timelineLoading && !force) return;
    state.timelineLoading = true;
    if (meta) meta.textContent = ': Loading...';
    try {
      var params = new URLSearchParams();
      params.set('days', String(state.timelineDays || 7));
      params.set('usernames', selected.join(','));
      var res = await fetch('/api/media-timeline?' + params.toString(), {
        cache: 'no-store',
        credentials: 'same-origin'
      });
      if (!res.ok) throw new Error('Timeline request failed');
      var data = await res.json();
      state.timelineData = data;
      renderTimelineChart(data);
    } catch (e) {
      console.error('Timeline load failed:', e);
      var empty = $('mediaTimelineEmpty');
      var svg = $('mediaTimelineSvg');
      if (svg) svg.hidden = true;
      if (empty) {
        empty.hidden = false;
        empty.textContent = e.message || 'Unable to load timeline';
      }
      if (meta) meta.textContent = ': Error';
      showToast(e.message || 'Timeline load failed', 'error');
    } finally {
      state.timelineLoading = false;
    }
  }

  function bindTimelineEvents() {
    setTimelineRangeButtons();

    var rangeRoot = $('mediaTimelineRangeButtons');
    if (rangeRoot) {
      rangeRoot.addEventListener('click', function(ev) {
        var btn = ev.target.closest('[data-timeline-days]');
        if (!btn) return;
        state.timelineDays = Number(btn.getAttribute('data-timeline-days') || 7);
        setTimelineRangeButtons();
        loadTimeline(true);
      });
    }

    var refreshBtn = $('mediaTimelineRefreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', function() {
        loadTimeline(true);
      });
    }

    window.addEventListener('resize', function() {
      if (state.timelineData) renderTimelineChart(state.timelineData);
    });
  }

  function bindEvents() {
    var storageRefresh = $('mediaStorageRefreshBtn');
    if (storageRefresh) {
      storageRefresh.addEventListener('click', function() {
        if (state.loading) return;
        loadMediaLibrary();
      });
    }

    var processingBrowse = $('mediaProcessingBrowseBtn');
    if (processingBrowse) {
      processingBrowse.addEventListener('click', function() {
        openProcessingModal();
      });
    }

    var processingCancel = $('mediaProcessingCancel');
    if (processingCancel) processingCancel.addEventListener('click', closeProcessingModal);

    var processingDelete = $('mediaProcessingDelete');
    if (processingDelete) processingDelete.addEventListener('click', deleteSelectedProcessingFiles);

    var processingSelectAll = $('mediaProcessingSelectAll');
    if (processingSelectAll) {
      processingSelectAll.addEventListener('click', function() {
        if (state.processingDeleting || state.processingLoading) return;
        var total = state.processingFiles.length;
        if (!total) return;
        var selectedCount = selectedProcessingPaths().length;
        var selectAll = selectedCount !== total;
        state.processingSelected = {};
        if (selectAll) {
          state.processingFiles.forEach(function(file) {
            state.processingSelected[file.path] = true;
          });
        }
        renderProcessingFiles();
      });
    }

    var processingBody = $('mediaProcessingBody');
    if (processingBody) {
      function toggleProcessingRow(row) {
        if (!row || state.processingDeleting || state.processingLoading) return;
        var path = row.getAttribute('data-processing-path') || '';
        if (!path) return;
        if (state.processingSelected[path]) delete state.processingSelected[path];
        else state.processingSelected[path] = true;
        var selected = !!state.processingSelected[path];
        row.classList.toggle('is-selected', selected);
        row.setAttribute('aria-selected', selected ? 'true' : 'false');
        syncProcessingDeleteState();
      }
      processingBody.addEventListener('click', function(event) {
        var row = event.target.closest('tr.media-processing-row[data-processing-path]');
        if (!row || !processingBody.contains(row)) return;
        toggleProcessingRow(row);
      });
      processingBody.addEventListener('keydown', function(event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        var row = event.target.closest('tr.media-processing-row[data-processing-path]');
        if (!row || !processingBody.contains(row)) return;
        event.preventDefault();
        toggleProcessingRow(row);
      });
    }

    var processingModal = $('mediaProcessingModal');
    if (processingModal) {
      processingModal.addEventListener('click', function(event) {
        if (event.target === processingModal) closeProcessingModal();
      });
    }

    var profileRefresh = $('mediaProfileRefreshBtn');
    if (profileRefresh) {
      profileRefresh.addEventListener('click', function() {
        if (state.profileRefreshing) return;
        refreshProfiles();
      });
    }

    var dateRange = $('mediaDateRange');
    if (dateRange) {
      var dateDigits = dateRange.querySelectorAll('.media-date-digit');
      dateDigits.forEach(function(input, index) {
        input.addEventListener('input', function() {
          var bound = input.getAttribute('data-bound') || 'from';
          var part = input.getAttribute('data-part') || 'y';
          var clamped = clampDateDigitValue(bound, part, input.value);
          if (input.value !== clamped) input.value = clamped;
          input.classList.toggle('has-override', !!String(input.value || '').trim());
        });
        input.addEventListener('keydown', function(event) {
          if (event.key === 'Enter') {
            event.preventDefault();
            applyDateRangeFilter(true);
            return;
          }
          if (event.key !== 'Tab') return;
          event.preventDefault();
          var nextIndex = event.shiftKey
            ? (index - 1 + dateDigits.length) % dateDigits.length
            : (index + 1) % dateDigits.length;
          dateDigits[nextIndex].focus();
          dateDigits[nextIndex].select();
        });
        input.addEventListener('blur', function() {
          window.setTimeout(function() {
            var active = document.activeElement;
            if (active && dateRange.contains(active)) return;
            applyDateRangeFilter(true);
          }, 0);
        });
      });
    }

    var search = $('mediaSearchInput');
    if (search) {
      function commitStreamerSearch() {
        state.profileSearch = search.value.trim();
        state.profilePage = 1;
        renderProfileCarousel();
      }
      search.addEventListener('keydown', function(event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          commitStreamerSearch();
        }
      });
      search.addEventListener('blur', commitStreamerSearch);
    }

    var sourceFilters = $('mediaProfileSourceFilters');
    if (sourceFilters) {
      sourceFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-source]');
        if (!button) return;
        setProfileSourceFilter(button.dataset.source || '');
      });
    }

    var statusFilters = $('mediaProfileStatusFilters');
    if (statusFilters) {
      statusFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-live-status]');
        if (!button) return;
        setProfileLiveStatusFilter(button.dataset.liveStatus || '');
      });
    }

    var recordingFilters = $('mediaProfileRecordingFilters');
    if (recordingFilters) {
      recordingFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-recording-filter]');
        if (!button) return;
        setRecordingFilter(button.dataset.recordingFilter || '');
      });
    }

    var filterReset = $('mediaStreamerFilterResetBtn');
    if (filterReset) filterReset.addEventListener('click', resetProfileFilters);

    var profilePageNumbers = $('mediaProfilePageNumbers');
    if (profilePageNumbers) {
      profilePageNumbers.addEventListener('click', function(event) {
        var button = event.target.closest('[data-profile-page]');
        if (button) setProfilePage(button.dataset.profilePage);
      });
    }
    var profilePrev = $('mediaStreamerPrevPageBtn');
    if (profilePrev) profilePrev.addEventListener('click', function() { shiftProfilePage(-1); });
    var profileNext = $('mediaStreamerNextPageBtn');
    if (profileNext) profileNext.addEventListener('click', function() { shiftProfilePage(1); });
    var videoPrev = $('mediaVideoPrevPageBtn');
    if (videoPrev) videoPrev.addEventListener('click', function() { shiftMediaPage(-1); });
    var videoNext = $('mediaVideoNextPageBtn');
    if (videoNext) videoNext.addEventListener('click', function() { shiftMediaPage(1); });

      document.addEventListener('keydown', function(event) {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      var target = event.target;
      if (target) {
        var tag = String(target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable) return;
      }
      var key = String(event.key || '').toLowerCase();
      if (key === 'e') {
        event.preventDefault();
        shiftProfilePage(1);
      } else if (key === 'q') {
        event.preventDefault();
        shiftProfilePage(-1);
      } else if (key === 'd') {
        event.preventDefault();
        shiftMediaPage(1);
      } else if (key === 'a') {
        event.preventDefault();
        shiftMediaPage(-1);
      } else if (key === 'f') {
        event.preventDefault();
        toggleToolbarCompactMode();
      }
    });

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
        var button = event.target.closest('[data-sort-field]');
        if (!button) return;
        setSortField(button.dataset.sortField || 'name');
      });
    }

    var deviceFilters = $('mediaDeviceFilters');
    if (deviceFilters) {
      deviceFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-device]');
        if (!button || button.disabled) return;
        setDeviceFilter(button.dataset.device || '');
      });
    }
    var videoFilterReset = $('mediaVideoFilterResetBtn');
    if (videoFilterReset) videoFilterReset.addEventListener('click', resetVideoFilters);
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
    if (downloadSelected) downloadSelected.addEventListener('click', openDownloadMethodModal);
    var downloadMethodCancel = $('mediaDownloadMethodCancel');
    if (downloadMethodCancel) downloadMethodCancel.addEventListener('click', closeDownloadMethodModal);
    var downloadWithMotrix = $('mediaDownloadWithMotrix');
    if (downloadWithMotrix) {
      downloadWithMotrix.addEventListener('click', function() { downloadSelectedToMac('motrix'); });
    }
    var downloadWithChrome = $('mediaDownloadWithChrome');
    if (downloadWithChrome) {
      downloadWithChrome.addEventListener('click', function() { downloadSelectedToMac('chrome'); });
    }
    var downloadMethodModal = $('mediaDownloadMethodModal');
    if (downloadMethodModal) {
      downloadMethodModal.addEventListener('click', function(ev) {
        if (ev.target === downloadMethodModal) closeDownloadMethodModal();
      });
    }
    document.addEventListener('visibilitychange', function() {
      if (document.hidden || !fileWatchTick) {
        if (document.hidden) stopTasksPolling();
        return;
      }
      if (fileWatchTimer) clearTimeout(fileWatchTimer);
      fileWatchTimer = setTimeout(fileWatchTick, 400);
      if (state.tasksOpen) refreshTasksPanel(true);
    });
    var deleteSelected = $('mediaDeleteSelectedBtn');
    if (deleteSelected) deleteSelected.addEventListener('click', openBatchDeleteConfirm);
    var selectAll = $('mediaSelectAllBtn');
    if (selectAll) selectAll.addEventListener('click', selectAllVisibleVideos);
    var selectPage = $('mediaSelectPageBtn');
    if (selectPage) selectPage.addEventListener('click', selectAllPageVideos);
    var clearSelection = $('mediaClearSelectionBtn');
    if (clearSelection) clearSelection.addEventListener('click', clearVideoSelection);

    var tasksBtn = $('mediaTasksBtn');
    if (tasksBtn) tasksBtn.addEventListener('click', showProjectTasks);
    var tasksClose = $('mediaTasksClose');
    if (tasksClose) tasksClose.addEventListener('click', closeTasksModal);
    var tasksCloseFooter = $('mediaTasksCloseFooter');
    if (tasksCloseFooter) tasksCloseFooter.addEventListener('click', closeTasksModal);
    var tasksModal = $('mediaTasksModal');
    if (tasksModal) {
      tasksModal.addEventListener('click', function(ev) {
        if (ev.target === tasksModal) closeTasksModal();
      });
    }
    var tasksTabs = $('mediaTasksTabs');
    if (tasksTabs) {
      tasksTabs.addEventListener('click', function(event) {
        var button = event.target.closest('[data-tasks-tab]');
        if (!button) return;
        setTasksTab(button.dataset.tasksTab || 'download');
      });
    }
    var tasksSelectAll = $('mediaTasksSelectAllBtn');
    if (tasksSelectAll) tasksSelectAll.addEventListener('click', selectAllTasksInTab);
    var tasksPause = $('mediaTasksPauseBtn');
    if (tasksPause) tasksPause.addEventListener('click', function() { mutateSelectedTasks('pause'); });
    var tasksResume = $('mediaTasksResumeBtn');
    if (tasksResume) tasksResume.addEventListener('click', function() { mutateSelectedTasks('resume'); });
    var tasksDelete = $('mediaTasksDeleteBtn');
    if (tasksDelete) tasksDelete.addEventListener('click', function() { mutateSelectedTasks('delete'); });
    var tasksBody = $('mediaTasksBody');
    if (tasksBody) {
      tasksBody.addEventListener('click', onTasksBodyClick);
      tasksBody.addEventListener('dragstart', onTasksDragStart);
      tasksBody.addEventListener('dragover', onTasksDragOver);
      tasksBody.addEventListener('dragleave', onTasksDragLeave);
      tasksBody.addEventListener('drop', onTasksDrop);
      tasksBody.addEventListener('dragend', onTasksDragEnd);
    }

    var streamerSelectAll = $('mediaStreamerSelectAllBtn');
    if (streamerSelectAll) streamerSelectAll.addEventListener('click', selectAllFilteredStreamers);
    var streamerSelectPage = $('mediaStreamerSelectPageBtn');
    if (streamerSelectPage) streamerSelectPage.addEventListener('click', selectAllVisibleStreamers);
    var streamerClear = $('mediaStreamerClearSelectionBtn');
    if (streamerClear) streamerClear.addEventListener('click', clearStreamerSelection);
    var streamerStartRecord = $('mediaStreamerStartRecordBtn');
    if (streamerStartRecord) streamerStartRecord.addEventListener('click', bulkStartRecordingSelectedStreamers);
    var streamerStopRecord = $('mediaStreamerStopRecordBtn');
    if (streamerStopRecord) streamerStopRecord.addEventListener('click', bulkStopRecordingSelectedStreamers);
    var streamerDelete = $('mediaStreamerDeleteBtn');
    if (streamerDelete) streamerDelete.addEventListener('click', openStreamerBulkDeleteConfirm);
    var streamerSettings = $('mediaStreamerSettingsBtn');
    if (streamerSettings) streamerSettings.addEventListener('click', openStreamerSettingsFromToolbar);
    var streamerBulkDeleteCancel = $('mediaStreamerBulkDeleteCancel');
    if (streamerBulkDeleteCancel) streamerBulkDeleteCancel.addEventListener('click', closeStreamerBulkDeleteConfirm);
    var streamerBulkDeleteConfirm = $('mediaStreamerBulkDeleteConfirm');
    if (streamerBulkDeleteConfirm) streamerBulkDeleteConfirm.addEventListener('click', confirmStreamerBulkDelete);
    var streamerBulkDeleteModal = $('mediaStreamerBulkDeleteModal');
    if (streamerBulkDeleteModal) {
      streamerBulkDeleteModal.addEventListener('click', function(ev) {
        if (ev.target === streamerBulkDeleteModal) closeStreamerBulkDeleteConfirm();
      });
    }
    bindFollowReconcileControls();

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
        var openFolder = ev.target.closest('[data-profile-action="open-videos-folder"]');
        if (openFolder) {
          ev.preventDefault();
          ev.stopPropagation();
          openStreamerMacFolder(openFolder.dataset.profile || '');
          return;
        }
        var directLink = ev.target.closest('[data-profile-action="channel"], [data-profile-action="watch"]');
        if (directLink) {
          ev.stopPropagation();
          return;
        }
        var card = ev.target.closest('.media-profile-card');
        if (!card) return;
        // Drag-to-select text should not toggle the streamer card.
        if (profileCardPointer.moved || profileCardHasTextSelection(card)) {
          profileCardPointer.moved = false;
          return;
        }
        if (card.getAttribute('data-all-profiles') === '1') {
          clearStreamerSelection();
          return;
        }
        var username = card.dataset.profile || '';
        if (!username) return;
        toggleStreamerSelection(username, !state.selectedStreamerIds[username]);
      });
      rail.addEventListener('pointerdown', function(ev) {
        if (ev.button !== 0) return;
        profileCardPointer.x = ev.clientX;
        profileCardPointer.y = ev.clientY;
        profileCardPointer.moved = false;
        if (ev.target.closest('[data-profile-action="channel"], [data-profile-action="watch"], [data-profile-action="open-videos-folder"]')) return;
      });
      rail.addEventListener('pointermove', function(ev) {
        if (profileCardPointer.moved) return;
        var dx = Math.abs(ev.clientX - profileCardPointer.x);
        var dy = Math.abs(ev.clientY - profileCardPointer.y);
        if (dx > 4 || dy > 4) profileCardPointer.moved = true;
      });
      rail.addEventListener('keydown', function(ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        if (ev.target.closest('[data-profile-action="channel"], [data-profile-action="watch"], [data-profile-action="open-videos-folder"]')) return;
        var card = ev.target.closest('.media-profile-card');
        if (!card) return;
        ev.preventDefault();
        if (card.getAttribute('data-all-profiles') === '1') {
          clearStreamerSelection();
          return;
        }
        var username = card.dataset.profile || '';
        if (!username) return;
        toggleStreamerSelection(username, !state.selectedStreamerIds[username]);
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
          if (action.dataset.mediaAction === 'select-project') {
            ev.preventDefault();
            ev.stopPropagation();
            var selectPid = action.dataset.projectId || '';
            selectProject(selectPid, !state.selectedProjectIds[selectPid]);
            return;
          }
          if (action.dataset.mediaAction === 'reveal-project') {
            ev.preventDefault();
            ev.stopPropagation();
            var revealItem = firstMacMemberItem(projectById(action.dataset.projectId));
            if (revealItem) openLocally(revealItem, { reveal: true });
            return;
          }
          if (action.dataset.mediaAction === 'reveal-local') {
            ev.preventDefault();
            ev.stopPropagation();
            openLocally(itemById(action.dataset.mediaId), { reveal: true });
            return;
          }
          if (action.dataset.mediaAction === 'vps-label') {
            // Same selection affordance as clicking the video info body.
            ev.preventDefault();
            ev.stopPropagation();
            var vpsCard = action.closest('.media-card');
            var vpsId = (vpsCard && vpsCard.dataset.mediaId) || action.dataset.mediaId || '';
            if (vpsId && itemIsSelectable(itemById(vpsId))) {
              toggleDownloadSelection(vpsId, !state.selectedItemIds[vpsId]);
            }
            return;
          }
          if (action.dataset.mediaAction === 'select-card' || action.dataset.mediaAction === 'select') {
            ev.preventDefault();
            ev.stopPropagation();
            toggleDownloadSelection(action.dataset.mediaId, !state.selectedItemIds[action.dataset.mediaId]);
            return;
          }
          if (action.dataset.mediaAction !== 'open-project') return;
        }
        if (ev.target.closest('.media-card-footer') || ev.target.closest('.media-project-card-footer')) return;
        var projectCard = ev.target.closest('.media-project-card');
        if (projectCard) {
          openProjectDetail(projectCard.dataset.projectId || '');
          return;
        }
        var card = ev.target.closest('.media-card');
        if (card) {
          activateMediaItem(itemById(card.dataset.mediaId));
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
        var projectCardKey = ev.target.closest('.media-project-card');
        if (projectCardKey) {
          ev.preventDefault();
          openProjectDetail(projectCardKey.dataset.projectId || '');
          return;
        }
        var revealLocal = ev.target.closest('[data-media-action="reveal-local"]');
        if (revealLocal) {
          ev.preventDefault();
          openLocally(itemById(revealLocal.dataset.mediaId), { reveal: true });
          return;
        }
        if (ev.target.closest('[data-media-action="vps-label"]')) {
          ev.preventDefault();
          var vpsCard = ev.target.closest('.media-card');
          var vpsId = (vpsCard && vpsCard.dataset.mediaId) || '';
          if (vpsId && itemIsSelectable(itemById(vpsId))) {
            toggleDownloadSelection(vpsId, !state.selectedItemIds[vpsId]);
          }
          return;
        }
        if (ev.target.closest('.media-card-footer') || ev.target.closest('.media-project-card-footer')) return;
        var card = ev.target.closest('.media-card');
        if (!card) return;
        ev.preventDefault();
        activateMediaItem(itemById(card.dataset.mediaId));
      });
    }

    var projectDetail = $('mediaProjectDetail');
    if (projectDetail) {
      projectDetail.addEventListener('click', function(ev) {
        var action = ev.target.closest('[data-media-action]');
        if (!action) {
          var detailCard = ev.target.closest('.media-card');
          if (detailCard) activateMediaItem(itemById(detailCard.dataset.mediaId));
          return;
        }
        if (action.dataset.mediaAction === 'project-back') {
          ev.preventDefault();
          closeProjectDetail();
          return;
        }
        if (action.dataset.mediaAction === 'select-card' || action.dataset.mediaAction === 'select') {
          ev.preventDefault();
          toggleDownloadSelection(action.dataset.mediaId, !state.selectedItemIds[action.dataset.mediaId]);
          return;
        }
        if (action.dataset.mediaAction === 'reveal-local') {
          ev.preventDefault();
          openLocally(itemById(action.dataset.mediaId), { reveal: true });
          return;
        }
        if (action.dataset.mediaAction === 'profile') {
          ev.preventDefault();
          selectProfile(action.dataset.profile || '', true);
        }
      });
    }

    var viewToggle = $('mediaViewToggle');
    if (viewToggle) {
      viewToggle.addEventListener('click', function(event) {
        var button = event.target.closest('[data-view-mode]');
        if (!button) return;
        setVideoViewMode(button.dataset.viewMode || 'projects');
      });
    }
    var projectStatusFilters = $('mediaProjectStatusFilters');
    if (projectStatusFilters) {
      projectStatusFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-project-status]');
        if (!button) return;
        state.filterProjectStatus = button.dataset.projectStatus || 'all';
        if (state.filterProjectStatus !== 'completed') state.filterCompletedSync = 'all';
        state.mediaPage = 1;
        syncProjectFilterControls();
        renderRecentSection(state.items.length);
        updateMacToolbar();
      });
    }
    var completedSyncFilters = $('mediaCompletedSyncFilters');
    if (completedSyncFilters) {
      completedSyncFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-completed-sync]');
        if (!button) return;
        state.filterCompletedSync = button.dataset.completedSync || 'all';
        state.mediaPage = 1;
        syncProjectFilterControls();
        renderRecentSection(state.items.length);
        updateMacToolbar();
      });
    }
    var concatStatusFilters = $('mediaConcatStatusFilters');
    if (concatStatusFilters) {
      concatStatusFilters.addEventListener('click', function(event) {
        var button = event.target.closest('[data-concat-status]');
        if (!button) return;
        state.filterConcatStatus = button.dataset.concatStatus || 'all';
        state.mediaPage = 1;
        syncProjectFilterControls();
        renderRecentSection(state.items.length);
        updateMacToolbar();
      });
    }
    var concatSelectedBtn = $('mediaConcatSelectedBtn');
    if (concatSelectedBtn) concatSelectedBtn.addEventListener('click', concatSelectedProjects);

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

    var profileSettingsForm = $('mediaProfileSettingsForm');
    if (profileSettingsForm) profileSettingsForm.addEventListener('submit', saveProfileSettings);

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
        var toggle = ev.target.closest('[data-source-field="autoRecord"]');
        if (toggle && profileSourcesList.contains(toggle)) {
          ev.preventDefault();
          var next = !(toggle.getAttribute('aria-pressed') === 'true' || toggle.classList.contains('is-active'));
          toggle.setAttribute('aria-pressed', next ? 'true' : 'false');
          toggle.classList.toggle('is-active', next);
          var stateEl = toggle.querySelector('.media-settings-toggle-state');
          if (stateEl) stateEl.textContent = next ? 'On' : 'Off';
          syncLegacyStreamFields(readProfileSources());
          return;
        }
        var remove = ev.target.closest('[data-source-action="remove"]');
        if (!remove) return;
        var rows = Array.prototype.slice.call(document.querySelectorAll('.media-profile-source-row'));
        if (rows.length <= 1) {
          rows[0].querySelectorAll('input, select').forEach(function(input) {
            if (input.type === 'checkbox') input.checked = false;
            else if (input.type !== 'hidden') input.value = '';
          });
          var autoToggle = rows[0].querySelector('[data-source-field="autoRecord"]');
          if (autoToggle) {
            autoToggle.setAttribute('aria-pressed', 'false');
            autoToggle.classList.remove('is-active');
            var offState = autoToggle.querySelector('.media-settings-toggle-state');
            if (offState) offState.textContent = 'Off';
          }
          syncLegacyStreamFields(readProfileSources());
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
        } else if (state.tasksOpen) {
          closeTasksModal();
        } else if ($('mediaProcessingModal') && $('mediaProcessingModal').style.display !== 'none') {
          closeProcessingModal();
        } else if ($('mediaDownloadMethodModal') && $('mediaDownloadMethodModal').style.display !== 'none') {
          closeDownloadMethodModal();
        } else if ($('mediaProfileSettingsModal') && $('mediaProfileSettingsModal').style.display !== 'none') {
          closeProfileSettings();
        } else if ($('mediaSyncLogModal') && $('mediaSyncLogModal').style.display !== 'none') {
          closeSyncLogModal();
        } else if ($('mediaFollowSyncModal') && $('mediaFollowSyncModal').style.display !== 'none') {
          closeFollowReconcile();
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
    applyToolbarCompactMode();
    bindEvents();
    bindTimelineEvents();
    renderProfileSourceFilters();
    renderProfileStatusFilters();
    renderProfileRecordingFilters();
    syncFilterControls();
    syncDateRangePlaceholders();
    renderSyncLogToolbar();
    fetch('/api/settings/recording', { cache: 'no-store' })
      .then(function(res) { return res.ok ? res.json() : null; })
      .then(function(data) {
        if (!data) return;
        state.recordingEnabled = data.recording_enabled !== false;
        state.librarySpaceBlocked = !!data.library_space_blocked;
        state.stagingBlocked = !!(data.staging_blocked || data.staging_cleaning);
        var quotaDefault = parseInt(data.default_monthly_quota_gb, 10);
        if (!Number.isNaN(quotaDefault)) {
          state.defaultMonthlyQuotaGb = Math.max(0, Math.min(10000, quotaDefault));
        }
        renderProfileCarousel();
      })
      .catch(function() {});
    document.addEventListener('hxylive:recording-enabled', function(event) {
      var detail = (event && event.detail) || {};
      var enabled = detail.enabled !== false;
      state.recordingEnabled = enabled;
      if (detail.library_space_blocked != null) {
        state.librarySpaceBlocked = !!detail.library_space_blocked;
      }
      if (detail.staging_blocked != null) {
        state.stagingBlocked = !!detail.staging_blocked;
      }
      (state.profiles || []).forEach(function(profile) {
        if (!profile) return;
        var blocked = !enabled || state.librarySpaceBlocked || state.stagingBlocked;
        profile.recordingAllowed = !blocked && !profile.quotaExceeded && !profile.quota_exceeded;
        profile.recordingBlockedReason = state.librarySpaceBlocked
          ? 'library_disk_low'
          : (state.stagingBlocked
            ? 'staging_cleaning'
            : (!enabled
              ? 'global_pause'
              : (profile.quotaExceeded || profile.quota_exceeded ? 'quota_exceeded' : null)));
      });
      renderProfileCarousel();
    });
    scanMacAndRefresh(true, true);
    consumeReconcileQuery();
  });
})();

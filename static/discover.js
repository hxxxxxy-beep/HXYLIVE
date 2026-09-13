// ============================================
// Discover Page - Browse live Chaturbate models
// ============================================

// Render a small platform badge overlaid on the thumbnail.
function renderPlatformBadge(sourceType) {
  var t = (sourceType || '').toLowerCase();
  var label = providerLabel(t);
  var cls = 'platform-badge platform-' + (t || 'unknown');
  return '<span class="' + cls + '" title="' + label + '">' + label + '</span>';
}

// State
let currentPage = 1;
let totalPages = 1;
// Always a single website (default Twitch). Categories load for that site.
let currentSource = 'twitch';
// A core category selection (generic — not gender-only).
let selectedCategoryKey = 'all';
let selectedCategoryType = 'all';
let selectedCategoryRequestParam = null;
let selectedCategoryRequestValue = null;
let selectedSecondaryFilters = {};
// Compat mirror of gender request value for older helpers / URL gender=.
let currentGender = '';
let currentSearch = '';
let activeTags = [];
let searchTimeout = null;
let discoverProviders = [];
let providerCapsBySource = {};
let discoverRequestSeq = 0;
let isDiscoverLoading = false;
let discoverHasMore = true;
let loadedModelKeys = new Set();
let infiniteScrollObserver = null;
const DISCOVER_PAGE_LIMIT = 24;
const DISCOVER_ALLOWED_SOURCES = { twitch: true, chaturbate: true, bilibili: true, stripchat: true, youtube: true };
const DISCOVER_DEFAULT_SOURCE = 'twitch';
// Same-page refill when a provider returns empty models with has_more=true.
const DISCOVER_EMPTY_PAGE_RETRY_MAX = 2;
// Empty tag-filtered provider pages: advance a few pages looking for matches.
const DISCOVER_TAG_EMPTY_PAGE_ADVANCE_MAX = 8;
let emptyPageRetryCount = 0;
// Last successful /api/discover paint (ms). Used to detect stale roster after
// bfcache restore or a long backgrounded tab — thumbnails alone do not refresh usernames.
let lastDiscoverFetchAt = 0;
let discoverHiddenAt = 0;
// Local streamer cards and auto-record channels, loaded before the first render.
let cardSet = new Set();
let recordingSet = new Set();
// channel key → media profile username (may differ when sources are merged).
let recordingProfileByKey = {};
let createCardPending = null;
// Dynamic categories (only available=true + readiness=verified).
let categoriesRequestSeq = 0;
let categoriesAbortController = null;
let formalCategories = [];
let categoriesLoadState = 'idle'; // idle | loading | ready | error
let categoriesStatusMessage = '';

function categoryHelpers() {
  return (typeof DiscoverCategories !== 'undefined' && DiscoverCategories)
    ? DiscoverCategories
    : null;
}

function categoryFiltersHost() {
  return document.getElementById('categoryFilters');
}

function categoryRowHost() {
  return document.getElementById('categoryRow');
}

function hasSelectedSource() {
  return !!(currentSource && DISCOVER_ALLOWED_SOURCES[currentSource]);
}

function normalizeDiscoverSource(value) {
  var key = String(value || '').trim().toLowerCase();
  // Legacy source=all / missing / invalid → default single site (Twitch).
  if (!key || key === 'all') return DISCOVER_DEFAULT_SOURCE;
  if (DISCOVER_ALLOWED_SOURCES[key]) return key;
  return DISCOVER_DEFAULT_SOURCE;
}

function setCategoryRowVisible(visible) {
  var row = categoryRowHost();
  var host = categoryFiltersHost();
  if (row) {
    row.hidden = !visible;
  } else if (host) {
    host.hidden = !visible;
  }
}

function resetCategorySelectionToAll() {
  selectedCategoryKey = 'all';
  selectedCategoryType = 'all';
  selectedCategoryRequestParam = null;
  selectedCategoryRequestValue = null;
  selectedSecondaryFilters = {};
  currentGender = '';
}

function applySelectedCategoryItem(item) {
  var helpers = categoryHelpers();
  var normalized = helpers && helpers.normalizeCategoryItem
    ? helpers.normalizeCategoryItem(item)
    : {
        canonical_key: 'all',
        category_type: 'all',
        request_param: null,
        request_value: null
      };
  selectedCategoryKey = normalized.canonical_key || 'all';
  selectedCategoryType = normalized.category_type || 'all';
  selectedCategoryRequestParam = normalized.request_param;
  selectedCategoryRequestValue = normalized.request_value;
  // Compat: only mirror real gender mappings into currentGender.
  if (selectedCategoryType === 'gender' && selectedCategoryRequestParam === 'gender') {
    currentGender = String(selectedCategoryRequestValue || '');
  } else {
    currentGender = '';
  }
}

function normalizePositiveInt(value, fallback) {
  var parsed = parseInt(value, 10);
  return parsed > 0 ? parsed : fallback;
}

function parseTagParam(value) {
  return String(value || '')
    .split(',')
    .map(function(tag) { return tag.trim().toLowerCase(); })
    .filter(Boolean)
    .filter(function(tag, index, tags) { return tags.indexOf(tag) === index; });
}

function readDiscoverStateFromUrl() {
  var params = new URLSearchParams(window.location.search || '');
  // Legacy gender= / game_id= are validated against formal category mapping after categories load.
  var gender = String(params.get('gender') || '').trim().toLowerCase();
  var gameId = String(params.get('game_id') || '').trim();
  var parentAreaId = String(params.get('parent_area_id') || '').trim();
  var liveQuery = String(params.get('live_query') || '').trim().toLowerCase();
  var videoCategoryId = String(params.get('video_category_id') || '').trim();
  currentPage = 1;
  // Default Twitch when URL has no/legacy source=. Single-site only.
  currentSource = normalizeDiscoverSource(params.get('source'));
  resetCategorySelectionToAll();
  if (String(currentSource || '').toLowerCase() === 'youtube') {
    // YouTube Discover ignores category URL params (handle search only).
  } else if (hasSelectedSource() && liveQuery && /^[a-z][a-z0-9]{0,31}$/.test(liveQuery)) {
    selectedCategoryKey = 'live_query:' + liveQuery;
    selectedCategoryType = 'content';
    selectedCategoryRequestParam = 'live_query';
    selectedCategoryRequestValue = liveQuery;
    currentGender = '';
  } else if (hasSelectedSource() && gameId && /^\d+$/.test(gameId)) {
    selectedCategoryKey = 'game:' + gameId;
    selectedCategoryType = 'content';
    selectedCategoryRequestParam = 'game_id';
    selectedCategoryRequestValue = gameId;
    currentGender = '';
  } else if (hasSelectedSource() && parentAreaId && /^\d+$/.test(parentAreaId)) {
    selectedCategoryKey = 'parent_area:' + parentAreaId;
    selectedCategoryType = 'content';
    selectedCategoryRequestParam = 'parent_area_id';
    selectedCategoryRequestValue = parentAreaId;
    currentGender = '';
  } else if (hasSelectedSource() && videoCategoryId && /^\d+$/.test(videoCategoryId)) {
    selectedCategoryKey = 'video_category:' + videoCategoryId;
    selectedCategoryType = 'content';
    selectedCategoryRequestParam = 'video_category_id';
    selectedCategoryRequestValue = videoCategoryId;
    currentGender = '';
  } else if (hasSelectedSource() && gender && gender !== 'all') {
    // Temporary hint until categories API resolves the formal item.
    currentGender = gender;
    selectedCategoryKey = gender;
    selectedCategoryType = 'gender';
    selectedCategoryRequestParam = 'gender';
    selectedCategoryRequestValue = gender;
  }
  currentSearch = String(params.get('search') || '').trim();
  activeTags = parseTagParam(params.get('tags'));
  selectedSecondaryFilters = {};
  emptyPageRetryCount = 0;
  loadedModelKeys = new Set();
  discoverHasMore = true;
}

function discoverStateParams() {
  var params = new URLSearchParams();
  if (hasSelectedSource()) params.set('source', currentSource);
  var applied = applyCategoryRequestToDiscover();
  if (hasSelectedSource() && applied.ok && applied.gender) params.set('gender', applied.gender);
  if (hasSelectedSource() && applied.ok && applied.game_id) params.set('game_id', applied.game_id);
  if (hasSelectedSource() && applied.ok && applied.parent_area_id) {
    params.set('parent_area_id', applied.parent_area_id);
  }
  if (hasSelectedSource() && applied.ok && applied.video_category_id) {
    params.set('video_category_id', applied.video_category_id);
  }
  if (hasSelectedSource() && applied.ok && applied.live_query) {
    params.set('live_query', applied.live_query);
  }
  if (currentSearch) params.set('search', currentSearch);
  if (activeTags.length) params.set('tags', activeTags.join(','));
  return params;
}

function applyCategoryRequestToDiscover() {
  var helpers = categoryHelpers();
  if (!helpers || !helpers.applyCategoryRequest) {
    return { ok: true, gender: currentGender || '', game_id: '', parent_area_id: '', video_category_id: '', live_query: '' };
  }
  return helpers.applyCategoryRequest({
    selectedCategoryKey: selectedCategoryKey,
    selectedCategoryType: selectedCategoryType,
    selectedCategoryRequestParam: selectedCategoryRequestParam,
    selectedCategoryRequestValue: selectedCategoryRequestValue
  });
}

function syncDiscoverStateToUrl() {
  if (!window.history || !window.location) return;
  var params = discoverStateParams();
  var nextUrl = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
  var currentUrl = window.location.pathname + window.location.search;
  if (nextUrl !== currentUrl) {
    window.history.replaceState({}, '', nextUrl);
  }
}

function applyDiscoverStateToControls() {
  var searchInput = document.getElementById('searchInput');
  if (searchInput) searchInput.value = currentSearch;
  updateDiscoverSearchChrome();

  document.querySelectorAll('.discover-source-btn').forEach(function(button) {
    var btnSource = normalizeDiscoverSource(button.getAttribute('data-source'));
    button.classList.toggle('active', btnSource === currentSource);
  });

  var youtubeSource = String(currentSource || '').toLowerCase() === 'youtube';
  setCategoryRowVisible(
    !youtubeSource &&
    hasSelectedSource() && (
      formalCategories.length > 0 ||
      categoriesLoadState === 'loading' ||
      categoriesLoadState === 'error'
    )
  );
  syncCategoryPillActiveState();
  renderActiveTagFilters();
}

function updateDiscoverSearchChrome() {
  var searchInput = document.getElementById('searchInput');
  var tagWrap = document.querySelector('.tag-input-container');
  var activeTags = document.getElementById('activeTagFilters');
  var youtubeSource = String(currentSource || '').toLowerCase() === 'youtube';
  if (searchInput) {
    searchInput.placeholder = youtubeSource
      ? 'YouTube @handle or channel URL…'
      : 'Search models...';
  }
  if (tagWrap) {
    // Author CSS sets display:flex on .tag-input-container and can override [hidden].
    tagWrap.hidden = youtubeSource;
    tagWrap.style.display = youtubeSource ? 'none' : '';
  }
  if (youtubeSource && activeTags) {
    activeTags.style.display = 'none';
    activeTags.innerHTML = '';
  }
}

function syncCategoryPillActiveState() {
  var host = categoryFiltersHost();
  if (!host) return;
  var pills = host.querySelectorAll('.filter-pill');
  pills.forEach(function(pill) {
    var key = pill.getAttribute('data-canonical') || '';
    pill.classList.toggle('active', key === selectedCategoryKey);
  });
}

function resetDiscoverListState() {
  currentPage = 1;
  emptyPageRetryCount = 0;
  discoverHasMore = true;
  loadedModelKeys = new Set();
}

function clearCategoryButtons() {
  var host = categoryFiltersHost();
  if (host) host.innerHTML = '';
  formalCategories = [];
  setCategoryRowVisible(false);
}

function setCategoryStatus(message, isError) {
  categoriesStatusMessage = message || '';
  var el = document.getElementById('categoryStatus');
  if (!el) return;
  if (!categoriesStatusMessage) {
    el.textContent = '';
    el.hidden = true;
    el.classList.remove('is-error');
    el.removeAttribute('style');
    return;
  }
  el.hidden = false;
  el.classList.toggle('is-error', !!isError);
  el.textContent = categoriesStatusMessage;
  el.style.flexBasis = '100%';
  el.style.fontSize = '0.8rem';
  el.style.opacity = isError ? '0.95' : '0.75';
  el.style.color = isError ? '#ef4444' : 'inherit';
  el.style.marginTop = '0.15rem';
}

function renderCategoryButtons(items, options) {
  options = options || {};
  var helpers = categoryHelpers();
  var host = categoryFiltersHost();
  if (!host) return;
  var incoming = Array.isArray(items) ? items.slice() : [];
  // Site-wide: never render unsupported/unavailable/invalid-mapping as grey pills.
  formalCategories = incoming.filter(function(item) {
    if (helpers && helpers.isFormalCategoryItem && !helpers.isFormalCategoryItem(item)) {
      return false;
    }
    if (helpers && helpers.evaluateCategoryRequestSupport) {
      return !!helpers.evaluateCategoryRequestSupport(item).supported;
    }
    return true;
  });
  if (!formalCategories.length && helpers) {
    formalCategories = helpers.safeFallbackItemsForSource
      ? helpers.safeFallbackItemsForSource(currentSource)
      : helpers.safeAllFallbackItems();
  }
  host.innerHTML = formalCategories.map(function(item) {
    var normalized = helpers && helpers.normalizeCategoryItem
      ? helpers.normalizeCategoryItem(item)
      : item;
    var canonical = String(normalized.canonical_key || normalized.canonical_category || 'all').toLowerCase();
    var ctype = String(normalized.category_type || 'all');
    var reqParam = normalized.request_param == null ? '' : String(normalized.request_param);
    var reqValue = normalized.request_value == null ? '' : String(normalized.request_value);
    var label = String(normalized.display_label || normalized.label || canonical || 'All');
    var active = canonical === selectedCategoryKey ? ' active' : '';
    // Unexecutable categories are omitted above (no grey disabled pills).
    return '<button type="button" class="filter-pill' + active +
      '" data-canonical="' + escapeHtml(canonical) +
      '" data-category-type="' + escapeHtml(ctype) +
      '" data-request-param="' + escapeHtml(reqParam) +
      '" data-request-value="' + escapeHtml(reqValue) +
      '" data-gender="' + escapeHtml(ctype === 'gender' ? reqValue : '') + '">' +
      escapeHtml(label) + '</button>';
  }).join('');
  // Second-row category pills only after a website is selected and formal items exist.
  setCategoryRowVisible(hasSelectedSource() && formalCategories.length > 0);
  if (options.errorMessage) {
    setCategoryStatus(options.errorMessage, true);
  } else if (options.loadingMessage) {
    setCategoryStatus(options.loadingMessage, false);
  } else {
    setCategoryStatus('', false);
  }
  syncCategoryPillActiveState();
}

function applyFormalCategories(formalItems, preferred) {
  var helpers = categoryHelpers();
  var fallback = helpers && helpers.safeFallbackItemsForSource
    ? helpers.safeFallbackItemsForSource(currentSource)
    : (helpers ? helpers.safeAllFallbackItems() : [{
        canonical_key: 'all',
        canonical_category: 'all',
        display_label: 'All',
        category_type: 'all',
        request_param: null,
        request_value: null,
        available: true,
        readiness: 'verified'
      }]);
  var items = formalItems && formalItems.length ? formalItems : fallback;
  var preferredWithSource = preferred && typeof preferred === 'object'
    ? Object.assign({ source: currentSource }, preferred)
    : { source: currentSource, canonical_key: preferred };
  var selected = helpers && helpers.selectDefaultCategory
    ? helpers.selectDefaultCategory(items, preferredWithSource)
    : (helpers ? helpers.normalizeCategoryItem(items[0]) : items[0]);
  applySelectedCategoryItem(selected);
  renderCategoryButtons(items);
  return selected;
}

async function loadCategoriesForSource(source, options) {
  options = options || {};
  var helpers = categoryHelpers();
  var sourceKeyValue = String(source || '').trim().toLowerCase();
  if (!DISCOVER_ALLOWED_SOURCES[sourceKeyValue]) {
    clearCategoryButtons();
    categoriesLoadState = 'idle';
    setCategoryStatus('', false);
    return { stale: false, skipped: true, formal: [] };
  }
  var preferred = options.preferred;
  if (preferred === undefined) {
    preferred = {
      canonical_key: selectedCategoryKey,
      gender: currentGender,
      selectedCategoryKey: selectedCategoryKey,
      selectedCategoryRequestValue: selectedCategoryRequestValue
    };
  }

  var requestSeq = ++categoriesRequestSeq;
  if (categoriesAbortController && typeof categoriesAbortController.abort === 'function') {
    try { categoriesAbortController.abort(); } catch (e) { /* ignore */ }
  }
  categoriesAbortController = (typeof AbortController !== 'undefined')
    ? new AbortController()
    : null;

  clearCategoryButtons();
  categoriesLoadState = 'loading';
  // Show the category row immediately after a website is chosen (loading state).
  // YouTube has no category pills — skip the loading row entirely.
  if (sourceKeyValue === 'youtube') {
    setCategoryRowVisible(false);
    categoriesLoadState = 'ready';
    resetCategorySelectionToAll();
    selectedSecondaryFilters = {};
    setCategoryStatus('', false);
    return { stale: false, formal: [], skipped: true };
  }
  setCategoryRowVisible(true);
  var loadingHost = categoryFiltersHost();
  if (loadingHost) {
    loadingHost.innerHTML =
      '<span class="category-loading" aria-live="polite">Loading categories…</span>';
  }
  setCategoryStatus('', false);
  // Drop prior source category so /api/discover is not called with a stale filter.
  resetCategorySelectionToAll();
  selectedSecondaryFilters = {};

  function isCurrentRequest() {
    if (!helpers) return requestSeq === categoriesRequestSeq && sourceKeyValue === currentSource;
    return helpers.shouldApplyCategoriesResponse(
      requestSeq, categoriesRequestSeq, sourceKeyValue, currentSource
    );
  }

  function applySafeAllError() {
    categoriesLoadState = 'error';
    var fallback = helpers && helpers.safeFallbackItemsForSource
      ? helpers.safeFallbackItemsForSource(sourceKeyValue)
      : (helpers
        ? helpers.safeAllFallbackItems()
        : [{
            canonical_key: 'all',
            canonical_category: 'all',
            display_label: 'All',
            category_type: 'all',
            request_param: null,
            request_value: null,
            available: true,
            readiness: 'verified'
          }]);
    var preferredKey = (helpers && helpers.preferredDefaultForSource
      ? helpers.preferredDefaultForSource(sourceKeyValue)
      : { canonical_key: 'all' }).canonical_key;
    // YouTube has no category pills — keep the row hidden without a fake ASMR fallback.
    if (!fallback.length) {
      clearCategoryButtons();
      setCategoryRowVisible(false);
      setCategoryStatus('', false);
      return { stale: false, error: true, formal: [] };
    }
    var fallbackLabel = String((fallback[0] && fallback[0].display_label) || preferredKey || 'default');
    // Never restore the fixed five-key row — source-safe fallback + light error/retry.
    applyFormalCategories(fallback, { canonical_key: preferredKey, source: sourceKeyValue });
    setCategoryStatus(
      'Categories unavailable. Showing ' + fallbackLabel + '. Tap to retry.',
      true
    );
    var statusEl = document.getElementById('categoryStatus');
    if (statusEl) {
      statusEl.style.cursor = 'pointer';
      statusEl.onclick = function() {
        loadCategoriesForSource(currentSource, {
          preferred: helpers && helpers.preferredDefaultForSource
            ? helpers.preferredDefaultForSource(currentSource)
            : { canonical_key: 'all' }
        })
          .then(function(result) {
            if (!result || result.stale) return;
            resetDiscoverListState();
            fetchDiscover();
          });
      };
    }
    return { stale: false, error: true, formal: fallback };
  }

  if (!helpers) {
    var noHelpers = applySafeAllError();
    return noHelpers;
  }

  try {
    var fetchOpts = {};
    if (categoriesAbortController) fetchOpts.signal = categoriesAbortController.signal;
    fetchOpts.cache = 'no-store';
    var res = await fetch(
      '/api/discover/categories?source=' + encodeURIComponent(sourceKeyValue),
      fetchOpts
    );
    if (!isCurrentRequest()) {
      return { stale: true };
    }
    if (!res.ok) {
      throw new Error('categories_http_' + res.status);
    }
    var payload = await res.json();
    if (!isCurrentRequest()) {
      return { stale: true };
    }
    var formal = helpers.filterFormalCategoriesFromPayload(payload);
    // Defense: never render unavailable_categories even if a buggy payload mixes them.
    categoriesLoadState = 'ready';
    var statusElOk = document.getElementById('categoryStatus');
    if (statusElOk) {
      statusElOk.onclick = null;
      statusElOk.style.cursor = '';
    }
    applyFormalCategories(formal, preferred);
    return { stale: false, formal: formal, payload: payload };
  } catch (err) {
    if (err && err.name === 'AbortError') {
      return { stale: true };
    }
    if (!isCurrentRequest()) {
      return { stale: true };
    }
    var errResult = applySafeAllError();
    return errResult;
  }
}

function sourceKey(username, sourceType) {
  return String(sourceType || 'chaturbate').toLowerCase() + ':' + String(username || '').toLowerCase();
}

function providerLabel(sourceType) {
  var meta = discoverProviders.find(function(p) { return p.sourceType === sourceType; });
  if (meta && meta.displayName) return meta.displayName;
  var t = (sourceType || '').toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

function fallbackThumbnailUrl(username, sourceType, opts) {
  var source = (sourceType || 'chaturbate').toLowerCase();
  var offline = !!(opts && opts.offline);
  // Offline CB `riw/*.jpg` is often a gray Chaturbate logo stub (~3KB). Prefer a
  // neutral placeholder so onerror does not replace a good summary_card photo
  // with that logo, and so offline cards without a photo still look intentional.
  if (source === 'chaturbate' && !offline) {
    return 'https://thumb.live.mmcdn.com/riw/' + encodeURIComponent(username || '') + '.jpg';
  }
  var label = providerLabel(source) || 'Live';
  var title = username || label;
  var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="220" viewBox="0 0 320 220">' +
    '<rect fill="#151a24" width="320" height="220"/>' +
    '<rect fill="#2b3342" x="28" y="28" width="264" height="164" rx="8"/>' +
    '<text x="50%" y="46%" dominant-baseline="middle" text-anchor="middle" fill="#f8fafc" font-family="system-ui, -apple-system, sans-serif" font-size="20" font-weight="700">' + escapeHtml(label) + '</text>' +
    '<text x="50%" y="60%" dominant-baseline="middle" text-anchor="middle" fill="#cbd5e1" font-family="system-ui, -apple-system, sans-serif" font-size="15">' + escapeHtml(title) + '</text>' +
    '</svg>';
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}

function thumbnailUrlForModel(model, sourceType) {
  var source = (sourceType || 'chaturbate').toLowerCase();
  var thumbnail = String(model.thumbnail || '').trim();
  var profile = String(
    model.profile_image_url || model.profileImageUrl || model.avatar_url || model.avatarUrl || ''
  ).trim();
  var isOnline = model.is_online;
  if (isOnline === undefined) isOnline = model.isOnline;
  if (isOnline === undefined) isOnline = true;
  var roomStatus = String(model.room_status || model.roomStatus || '').toLowerCase();
  if (
    ['off', 'offline', 'away', 'idle', 'inactive', 'not_live', 'not live'].indexOf(roomStatus) !== -1
  ) {
    isOnline = false;
  }
  var offline = !isOnline;
  // Offline CB roomlist stubs (`riw/*.jpg`) are often a gray logo; prefer a real
  // profile/summary image when the API still returns the stub path.
  if (source === 'chaturbate' && offline && /thumb\.live\.mmcdn\.com\/riw\//i.test(thumbnail)) {
    if (profile && !/thumb\.live\.mmcdn\.com\/riw\//i.test(profile)) {
      return profile;
    }
    return fallbackThumbnailUrl(model.username, source, { offline: true });
  }
  // Offline Stripchat doppiocdn snapshots frequently 404; prefer avatar/preview.
  if (source === 'stripchat' && offline && /img\.doppiocdn\.net\/snapshot\//i.test(thumbnail)) {
    if (profile && !/img\.doppiocdn\.net\/snapshot\//i.test(profile)) {
      return profile;
    }
  }
  // Offline /users payloads may only hydrate avatarUrl; use it as the card cover.
  if (!thumbnail && profile) {
    return profile;
  }
  return thumbnail || fallbackThumbnailUrl(model.username, source, { offline: offline });
}

function channelUrlForModel(model, sourceType) {
  var explicit = String(model.channel_url || model.channelUrl || '').trim();
  if (explicit) return explicit;
  var username = encodeURIComponent(model.username || '');
  var rawName = String(model.username || '').trim();
  if (sourceType === 'youtube') {
    if (/^UC[\w-]{22}$/.test(rawName)) {
      return 'https://www.youtube.com/channel/' + encodeURIComponent(rawName) + '/live';
    }
    return 'https://www.youtube.com/@' + encodeURIComponent(rawName.replace(/^@+/, '')) + '/live';
  }
  var templates = {
    twitch: 'https://www.twitch.tv/',
    bilibili: 'https://live.bilibili.com/',
    chaturbate: 'https://chaturbate.com/',
    stripchat: 'https://stripchat.com/'
  };
  return templates[sourceType] ? templates[sourceType] + username : '';
}

async function loadCardAndRecordingSets() {
  try {
    var res = await fetch('/api/media-library?limit=1&metadata=lazy', { cache: 'no-store' });
    if (!res.ok) return;
    var data = await res.json();
    var nextCards = new Set();
    var nextRecording = new Set();
    var profileMap = {};
    (data.profiles || []).forEach(function(profile) {
      var profileUsername = profile.username || '';
      var profileRecording = !!profile.autoRecord;
      var sources = profile.streamSources || profile.stream_sources || [];
      sources.forEach(function(source) {
        var channelUsername = source.channelUsername || source.channel_username || '';
        var sourceType = source.sourceType || source.source_type || 'chaturbate';
        if (!channelUsername) return;
        var key = sourceKey(channelUsername, sourceType);
        profileMap[key] = profileUsername;
        nextCards.add(key);
        var sourceRecording = source.autoRecord != null ? !!source.autoRecord : !!source.auto_record;
        if (profileRecording || sourceRecording) nextRecording.add(key);
      });
      if (profileUsername && !sources.length) {
        var fallbackKey = sourceKey(profileUsername, profile.sourceType || profile.source_type || 'chaturbate');
        profileMap[fallbackKey] = profileUsername;
        nextCards.add(fallbackKey);
        if (profileRecording) nextRecording.add(fallbackKey);
      }
    });
    cardSet = nextCards;
    recordingSet = nextRecording;
    recordingProfileByKey = profileMap;
  } catch (e) {
    // Card/recording badges stay inactive if the library lookup fails.
  }
}

async function loadDiscoverProviders() {
  var sourceFilters = document.getElementById('sourceFilters');
  var preferredOrder = ['twitch', 'youtube', 'bilibili', 'chaturbate', 'stripchat'];
  function renderSourceFilters(providerBySource) {
    if (!sourceFilters) return;
    providerBySource = providerBySource || {};
    var fallbackLabels = {
      twitch: 'Twitch',
      youtube: 'YouTube',
      bilibili: 'Bilibili',
      chaturbate: 'Chaturbate',
      stripchat: 'Stripchat'
    };
    var buttons = [];
    preferredOrder.forEach(function(source) {
      var provider = providerBySource[source];
      var label = provider
        ? (provider.displayName || provider.sourceType)
        : fallbackLabels[source];
      buttons.push(
        '<button type="button" class="discover-source-btn' +
        (currentSource === source ? ' active' : '') +
        '" data-source="' + escapeHtml(source) + '">' + escapeHtml(label) + '</button>'
      );
    });
    sourceFilters.innerHTML = buttons.join('');
  }

  // Keep the website selector usable even while provider status is loading or
  // when that optional status request fails.
  renderSourceFilters({});
  try {
    var res = await fetch('/api/providers', { cache: 'no-store' });
    if (!res.ok) return;
    var data = await res.json();
    discoverProviders = (data.providers || []).filter(function(provider) {
      return provider.enabled !== false && provider.capabilities && provider.capabilities.can_discover;
    });
    providerCapsBySource = {};
    discoverProviders.forEach(function(provider) {
      providerCapsBySource[provider.sourceType] = provider.capabilities || {};
    });
    var providerBySource = {};
    discoverProviders.forEach(function(provider) {
      providerBySource[provider.sourceType] = provider;
    });
    renderSourceFilters(providerBySource);
  } catch (e) {
    // Discover remains available with the default provider list.
  }
}

// ============================================
// Fetch discover data
// ============================================
function buildDiscoverParams(page) {
  var params = new URLSearchParams({
    page: page || currentPage,
    limit: DISCOVER_PAGE_LIMIT
  });
  // Always send a concrete provider source= (no aggregate All UI).
  if (hasSelectedSource()) params.set('source', currentSource);
  var applied = applyCategoryRequestToDiscover();
  if (!applied.ok) {
    // Safety: never emit gender=<non-gender>. Caller should have blocked setCategory.
    return params;
  }
  if (hasSelectedSource()) {
    if (applied.gender) params.set('gender', applied.gender);
    if (applied.game_id) params.set('game_id', applied.game_id);
    if (applied.parent_area_id) params.set('parent_area_id', applied.parent_area_id);
    if (applied.video_category_id) params.set('video_category_id', applied.video_category_id);
    if (applied.live_query) params.set('live_query', applied.live_query);
  }
  if (currentSearch) params.set('search', currentSearch);
  if (activeTags.length > 0) params.set('tags', activeTags.join(','));
  return params;
}


async function fetchDiscover(options) {
  options = options || {};
  var append = options.append === true;
  var refillSamePage = options.refillSamePage === true;
  // YouTube Discover is exact @handle search only — never pull a browse catalogue.
  if (
    String(currentSource || '').toLowerCase() === 'youtube' &&
    !String(currentSearch || '').trim()
  ) {
    discoverHasMore = false;
    setPaginationLoading(false);
    updateDiscoverLoadStatus();
    renderEmpty([], { youtubeHandleHint: true });
    return;
  }
  // Append advances page; same-page refill must NOT page+1.
  var requestedPage;
  if (refillSamePage) {
    requestedPage = Math.max(1, currentPage);
  } else if (append) {
    requestedPage = currentPage + 1;
  } else {
    requestedPage = 1;
  }

  var requestSeq = ++discoverRequestSeq;
  var scheduleSamePageRefill = false;
  var scheduleTagPageAdvance = false;
  if (!append && !refillSamePage) {
    currentPage = 1;
    discoverHasMore = true;
    loadedModelKeys = new Set();
    emptyPageRetryCount = 0;
  }
  syncDiscoverStateToUrl();
  setPaginationLoading(true);
  var grid = document.getElementById('discoverGrid');
  var hasCards = !!(grid && grid.querySelector('.discover-card'));
  if (!append && !refillSamePage) {
    grid.innerHTML = '<div class="empty-message discover-loading-message"><div class="icon">&#9203;</div><p>Loading models...</p></div>';
    updateDiscoverLoadStatus();
  } else if (!hasCards) {
    // Auto-retry / append with an empty grid: keep a single in-grid loader.
    // Do not stack "No models found" with a bottom "Loading more..." status.
    if (grid && !grid.querySelector('.discover-loading-message')) {
      grid.innerHTML = '<div class="empty-message discover-loading-message"><div class="icon">&#9203;</div><p>Loading models...</p></div>';
    }
    updateDiscoverLoadStatus();
  } else {
    updateDiscoverLoadStatus('Loading more...');
  }

  var params = buildDiscoverParams(requestedPage);

  try {
    var res = await fetch('/api/discover?' + params.toString(), { cache: 'no-store' });
    if (requestSeq !== discoverRequestSeq) return;
    if (res.ok) {
      var data = await res.json();
      if (requestSeq !== discoverRequestSeq) return;
      lastDiscoverFetchAt = Date.now();

      var unsupported = data.supported === false;
      var models = data.models || [];
      var modelsEmpty = !models.length;
      currentPage = Number(data.page || requestedPage);
      totalPages = Number(data.total_pages || currentPage);
      if (unsupported) {
        // Stop infinite scroll; never treat unsupported as empty live inventory pagination.
        discoverHasMore = false;
        emptyPageRetryCount = 0;
      } else if (String(currentSource || '').toLowerCase() === 'youtube') {
        // Exact handle lookup returns at most one card — never infinite-scroll.
        discoverHasMore = false;
        emptyPageRetryCount = 0;
      } else {
        discoverHasMore = typeof data.has_more === 'boolean'
          ? data.has_more
          : currentPage < totalPages;
        if (modelsEmpty && discoverHasMore) {
          if (activeTags.length > 0) {
            if (emptyPageRetryCount < DISCOVER_TAG_EMPTY_PAGE_ADVANCE_MAX) {
              emptyPageRetryCount += 1;
              scheduleTagPageAdvance = true;
            } else {
              discoverHasMore = false;
              emptyPageRetryCount = 0;
            }
          } else if (emptyPageRetryCount < DISCOVER_EMPTY_PAGE_RETRY_MAX) {
            emptyPageRetryCount += 1;
            scheduleSamePageRefill = true;
          } else {
            discoverHasMore = false;
            emptyPageRetryCount = 0;
          }
        } else if (!modelsEmpty) {
          emptyPageRetryCount = 0;
        } else {
          emptyPageRetryCount = 0;
          discoverHasMore = false;
        }
      }
      // Keep the in-grid loader while auto-retrying empty pages so we never flash
      // "No models found" together with a bottom loading / end-of-list status.
      var deferEmptyForRetry = modelsEmpty && !append && !refillSamePage && (
        scheduleTagPageAdvance || scheduleSamePageRefill
      );
      if (!deferEmptyForRetry) {
        // Refill/advance must not wipe existing cards; if the grid still shows
        // the empty/loading placeholder and models arrived, replace instead of appending.
        var renderAppend = append || refillSamePage;
        if ((append || refillSamePage) && !modelsEmpty && grid && !grid.querySelector('.discover-card')) {
          renderAppend = false;
        }
        // Final empty result with no cards must replace the in-grid loader.
        // Otherwise refillSamePage keeps append=true and never calls renderEmpty.
        if (modelsEmpty && grid && !grid.querySelector('.discover-card')) {
          renderAppend = false;
        }
        // Never re-sort models client-side — append API order only.
        renderGrid(
          models,
          data.provider_statuses || [],
          renderAppend,
          { unsupported: unsupported }
        );
      }
      updateDiscoverLoadStatus();
    } else {
      if (requestSeq !== discoverRequestSeq) return;
      if (!append && !refillSamePage) {
        grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Failed to load models.</p></div>';
      }
      discoverHasMore = false;
      emptyPageRetryCount = 0;
      updateDiscoverLoadStatus('Failed to load more');
    }
  } catch (e) {
    if (requestSeq !== discoverRequestSeq) return;
    console.error('Error loading discover:', e);
    if (!append && !refillSamePage) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Connection error.</p></div>';
    }
    discoverHasMore = false;
    emptyPageRetryCount = 0;
    updateDiscoverLoadStatus('Connection error');
  } finally {
    if (requestSeq === discoverRequestSeq) {
      setPaginationLoading(false);
      if (scheduleTagPageAdvance && discoverHasMore) {
        // Tag filter emptied this provider page; try the next page for matches.
        fetchDiscover({ append: true });
      } else if (scheduleSamePageRefill && discoverHasMore) {
        // Await prior request completion (we are in finally); then refill same page.
        fetchDiscover({ refillSamePage: true });
      } else {
        maybeLoadNextDiscoverPage();
      }
    }
  }
}

// ============================================
// Get number of columns in a CSS grid
// ============================================
function getGridColumnCount(grid) {
  var cols = getComputedStyle(grid).gridTemplateColumns;
  if (cols && cols !== 'none') {
    return cols.split(' ').length;
  }
  // Fallback: estimate from container width and min column size (280px + 24px gap)
  var width = grid.clientWidth;
  return Math.max(1, Math.floor((width + 24) / (280 + 24)));
}

// ============================================
// Render model grid
// ============================================
function renderEmpty(providerStatuses, options) {
  var grid = document.getElementById('discoverGrid');
  var opts = options || {};
  var status = null;
  if (hasSelectedSource()) {
    status = (providerStatuses || []).find(function(item) {
      return item.source_type === currentSource;
    });
  }
  if (!status && providerStatuses && providerStatuses.length === 1) {
    status = providerStatuses[0];
  }

  var title = 'No models found';
  var detail = '';
  var action = '';
  if (opts.youtubeHandleHint) {
    title = 'Search a YouTube @handle';
    detail = 'Paste a channel URL or the part after @ (example: AngelicLofiASMR).';
  } else if (opts.unsupported) {
    // Distinct from live inventory empty — do not reuse "No models found".
    title = 'Category not supported on this platform';
    detail = 'This category is not available in the current HXYLIVE integration for this platform.';
  } else if (status && status.status === 'unsupported') {
    title = 'Category not supported on this platform';
    detail = status.detail || 'This category is not available in the current HXYLIVE integration for this platform.';
  } else if (status && status.status === 'auth_required') {
    title = (status.display_name || providerLabel(status.source_type)) + ' needs a connection';
    detail = status.detail || 'Connect this provider before loading live models.';
    action = '<button class="btn-primary empty-action" onclick="window.location.href=\'/settings\'">Open Settings</button>';
  } else if (status && status.detail) {
    title = (status.display_name || providerLabel(status.source_type)) + ' is not available';
    detail = status.detail;
  }

  grid.innerHTML = '<div class="empty-message"><div class="icon">&#128269;</div><p>' + escapeHtml(title) + '</p>' +
    (detail ? '<span class="empty-detail">' + escapeHtml(detail) + '</span>' : '') +
    action +
    '</div>';
}

function renderGrid(models, providerStatuses, append, options) {
  var grid = document.getElementById('discoverGrid');
  var opts = options || {};

  if (!models.length && !append) {
    renderEmpty(providerStatuses || [], opts);
    return 0;
  }

  var uniqueModels = models.filter(function(model) {
    var cardSource = String(model.source_type || model.platform || 'chaturbate').toLowerCase();
    var key = sourceKey(model.username, cardSource);
    if (loadedModelKeys.has(key)) return false;
    loadedModelKeys.add(key);
    return true;
  });

  var html = uniqueModels.map(function(model) {
    var cardSource = String(model.source_type || model.platform || 'chaturbate').toLowerCase();
    var thumbUrl = thumbnailUrlForModel(model, cardSource);
    var isOnline = model.is_online;
    if (isOnline === undefined) isOnline = model.isOnline;
    if (isOnline === undefined) isOnline = true;
    isOnline = !!isOnline;
    var earlyRoomStatus = String(model.room_status || model.roomStatus || '').toLowerCase();
    // Stripchat uses short "off" for offline; do not keep a stale isOnline flag.
    if (
      ['off', 'offline', 'away', 'idle', 'inactive', 'not_live', 'not live'].indexOf(earlyRoomStatus) !== -1
    ) {
      isOnline = false;
    }
    var fallbackThumbUrl = fallbackThumbnailUrl(model.username, cardSource, { offline: !isOnline });
    var tagsHtml = '';
    if (model.tags && model.tags.length > 0) {
      // Show every tag; active filters first so matches stay visible / highlighted.
      var displayTags = pickDiscoverDisplayTags(model.tags, activeTags);
      tagsHtml = '<div class="discover-tags">' + displayTags.map(function(t) {
        var isActive = activeTags.some(function(a) {
          return String(a).toLowerCase() === String(t).toLowerCase();
        });
        var cls = 'discover-tag' + (isActive ? ' discover-tag-active' : '');
        return '<span class="' + cls + '" onclick="event.stopPropagation(); addTagFilter(\'' + escapeInlineJs(t) + '\')">' + escapeHtml(t) + '</span>';
      }).join('') + '</div>';
    }
    var channelUrl = channelUrlForModel(model, cardSource);
    var displayLabel = String(model.display_name || model.displayName || model.username || '').trim() || model.username;
    var avatarUrl = String(
      model.profile_image_url || model.profileImageUrl || model.avatar_url || model.avatarUrl || ''
    ).trim();
    // Catalogue Stripchat avatars on doppiocdn often 404; Media/Watch use static-proxy.
    if (cardSource === 'stripchat' && avatarUrl) {
      avatarUrl = avatarUrl.replace(
        /^https?:\/\/(?:img\.)?doppiocdn\.[^/]+(\/avatars\/)/i,
        'https://static-proxy.strpst.com$1'
      );
    }
    // Live webcam/snapshot/preview covers are card thumbs, not face photos.
    if (
      /thumb\.live\.mmcdn\.com\/riw\//i.test(avatarUrl) ||
      (/doppiocdn\./i.test(avatarUrl) && /\/snapshot\//i.test(avatarUrl)) ||
      (/(doppiocdn\.|static-proxy\.strpst\.com)/i.test(avatarUrl) && /\/previews\//i.test(avatarUrl)) ||
      /previews-ttv/i.test(avatarUrl) ||
      /live_user_/i.test(avatarUrl)
    ) {
      avatarUrl = '';
    }
    // Never use live/room covers as circular faces (letter avatar instead).
    // Bilibili room covers are keyframes, not UP face photos.
    if (
      !avatarUrl &&
      cardSource !== 'stripchat' &&
      cardSource !== 'chaturbate' &&
      cardSource !== 'bilibili' &&
      cardSource !== 'youtube' &&
      thumbUrl &&
      !/thumb\.live\.mmcdn\.com\/riw\//i.test(thumbUrl) &&
      !(/doppiocdn\./i.test(thumbUrl) && /\/snapshot\//i.test(thumbUrl)) &&
      !/previews-ttv/i.test(thumbUrl) &&
      !/live_user_/i.test(thumbUrl)
    ) {
      avatarUrl = thumbUrl;
    }
    var avatarLetter = String(displayLabel || model.username || '?').trim().charAt(0).toUpperCase() || '?';
    var avatarPlaceholder =
      '<span class="discover-avatar-placeholder" aria-hidden="true"><span>' +
      escapeHtml(avatarLetter) +
      '</span></span>';
    var avatarHtml = avatarUrl
      ? '<img class="discover-avatar" src="' + escapeHtml(avatarUrl) + '" alt="" loading="lazy" referrerpolicy="no-referrer" ' +
        'onerror="this.style.display=\'none\'; var p=this.nextElementSibling; if(p){p.style.display=\'flex\';}" />' +
        '<span class="discover-avatar-placeholder" style="display:none" aria-hidden="true"><span>' +
        escapeHtml(avatarLetter) +
        '</span></span>'
      : avatarPlaceholder;
    var followerText = model.followers === null || model.followers === undefined
      ? 'unavailable'
      : Number(model.followers || 0).toLocaleString();
    var hideFollowers = cardSource === 'stripchat';
    var streamAvailable = model.stream_available !== false;
    var cardCaps = providerCapsBySource[cardSource] || {};
    var canRecord = cardCaps.can_record !== false && (cardCaps.can_stream !== false || cardCaps.can_record === true);
    var cardKey = sourceKey(model.username, cardSource);
    var hasCard = cardSet.has(cardKey);
    var isRecording = recordingSet.has(cardKey);
    var actionBtn = '';
    if (hasCard && canRecord) {
      actionBtn = '<button type="button" class="discover-card-btn record-btn' + (isRecording ? ' active' : '') + '" ' +
        'title="' + (isRecording ? 'Turn off recording' : 'Turn on recording') + ' for ' + escapeHtml(displayLabel) + '" ' +
        'onclick="event.stopPropagation(); toggleRecordingOnCard(\'' + escapeInlineJs(model.username) + '\', \'' + escapeInlineJs(cardSource) + '\', \'' + escapeInlineJs(displayLabel) + '\', this)">' +
        (isRecording ? 'Recording' : 'Record') +
      '</button>';
    } else if (!hasCard && (canRecord || cardCaps.can_stream !== false)) {
      actionBtn = '<button type="button" class="discover-card-btn create-card-btn" ' +
        'data-source="' + escapeHtml(cardSource) + '" data-display="' + escapeHtml(displayLabel) + '" ' +
        'title="Create a local streamer card for ' + escapeHtml(displayLabel) + '" ' +
        'onclick="event.stopPropagation(); openCreateCardDialog(\'' + escapeInlineJs(model.username) + '\', \'' + escapeInlineJs(cardSource) + '\', \'' + escapeInlineJs(displayLabel) + '\', this)">' +
        'Create card' +
      '</button>';
    }
    var cardActions = actionBtn
      ? '<div class="discover-card-actions">' + actionBtn + '</div>'
      : '';
    // YouTube: open the site live room (no in-app /watch). Other providers: watch page
    // (offline overlay OK). Providers that cannot stream stay non-clickable.
    var opensExternalLive = cardSource === 'youtube' && !!channelUrl;
    var canOpenWatch = !opensExternalLive && cardCaps.can_stream !== false;
    var cardClass = 'discover-card' + (streamAvailable ? '' : ' is-discover-only');
    var cardAction = opensExternalLive
      ? ' onclick="openExternalLive(\'' + escapeInlineJs(channelUrl) + '\')"'
      : (canOpenWatch
        ? ' onclick="openWatch(\'' + escapeInlineJs(model.username) + '\', \'' + escapeInlineJs(cardSource) + '\')"'
        : ' title="Live playback is not available for this provider yet"');
    var uptimeHtml = '';
    var roomStatus = String(model.room_status || model.roomStatus || '').toLowerCase();
    var isPrivate = [
      'private', 'p2p', 'group', 'ticket', 'premium', 'spy',
      'virtualprivate', 'virtual_private', 'true_private', 'private_spy',
      'password_protected', 'password protected', 'hidden'
    ].indexOf(roomStatus) !== -1
      || /private|p2p|group|ticket|premium|spy/.test(roomStatus);
    var liveLabel = isPrivate ? 'Private' : (isOnline ? 'Live' : 'Offline');
    var liveDotClass = isPrivate ? 'private' : (isOnline ? 'online' : 'offline');
    var viewersLabel = isPrivate ? 'Private' : Number(model.viewers || 0).toLocaleString();

    if ((cardSource === 'twitch' || cardSource === 'youtube') && isOnline) {
      var startedAt = model.started_at || model.startedAt || '';
      var uptimeText = formatStreamUptime(startedAt);
      if (uptimeText) {
        uptimeHtml = '<div class="discover-meta-line discover-uptime-line" data-started-at="' + escapeHtml(startedAt) + '">' +
          '<span class="discover-uptime" aria-hidden="true">' + escapeHtml(uptimeText) + '</span>' +
        '</div>';
      }
    }

    return '<div class="' + cardClass + '" data-username="' + escapeHtml(model.username) + '" data-source="' + escapeHtml(cardSource) + '"' + cardAction + '>' +
      '<div class="discover-card-thumb">' +
        '<img src="' + escapeHtml(thumbUrl) + '" alt="' + escapeHtml(displayLabel) + '" ' +
          'referrerpolicy="no-referrer" ' +
          'onerror="this.onerror=null;this.src=\'' + escapeInlineJs(fallbackThumbUrl) + '\'" loading="lazy" />' +
      '</div>' +
      '<div class="discover-card-info">' +
        cardActions +
        '<div class="discover-identity">' +
          avatarHtml +
          '<span class="discover-username">' + escapeHtml(displayLabel) + '</span>' +
        '</div>' +
        '<div class="discover-live-status">' +
          '<span class="status-dot ' + liveDotClass + '"></span>' +
          '<span>' + liveLabel + '</span>' +
        '</div>' +
        uptimeHtml +
        '<div class="discover-meta-line discover-viewers-line">' +
          '<span class="discover-meta-label">Viewers:</span>' +
          '<span>' + escapeHtml(viewersLabel) + '</span>' +
        '</div>' +
        (hideFollowers ? '' : (
        '<div class="discover-meta-line discover-followers">' +
          '<span class="discover-meta-label">Followers:</span>' +
          '<span>' + escapeHtml(followerText) + '</span>' +
        '</div>')) +
        (channelUrl ? '<div class="discover-channel-line"><span>' + escapeHtml(providerLabel(cardSource)) + ':</span> <a class="discover-channel-url" href="' + escapeHtml(channelUrl) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + escapeHtml(channelUrl) + '</a></div>' : '') +
        tagsHtml +
      '</div>' +
    '</div>';
  }).join('');
  if (append) {
    grid.insertAdjacentHTML('beforeend', html);
  } else {
    grid.innerHTML = html;
  }
  fillMissingChaturbateAvatars(uniqueModels);
  return uniqueModels.length;
}

function fillMissingChaturbateAvatars(models) {
  var need = [];
  var seen = {};
  (models || []).forEach(function(model) {
    var src = String(model.source_type || model.platform || '').toLowerCase();
    if (src !== 'chaturbate') return;
    var username = String(model.username || '').trim();
    if (!username || seen[username.toLowerCase()]) return;
    var avatar = String(model.profile_image_url || model.profileImageUrl || '').trim();
    if (avatar && !/thumb\.live\.mmcdn\.com\/riw\//i.test(avatar)) return;
    seen[username.toLowerCase()] = true;
    need.push(username);
  });
  if (!need.length) return;

  var chunks = [];
  for (var i = 0; i < need.length; i += 8) {
    chunks.push(need.slice(i, i + 8));
  }

  function applyImages(images) {
    Object.keys(images || {}).forEach(function(username) {
      var url = String(images[username] || '').trim();
      if (!url || /thumb\.live\.mmcdn\.com\/riw\//i.test(url)) return;
      var card = document.querySelector(
        '.discover-card[data-source="chaturbate"][data-username="' +
          String(username).replace(/\\/g, '\\\\').replace(/"/g, '\\"') +
          '"]'
      );
      if (!card) return;
      var identity = card.querySelector('.discover-identity');
      if (!identity) return;
      var img = identity.querySelector('.discover-avatar');
      var placeholder = identity.querySelector('.discover-avatar-placeholder');
      if (!img) {
        img = document.createElement('img');
        img.className = 'discover-avatar';
        img.alt = '';
        img.loading = 'lazy';
        img.setAttribute('referrerpolicy', 'no-referrer');
        img.onerror = function() {
          img.style.display = 'none';
          if (placeholder) placeholder.style.display = 'flex';
        };
        identity.insertBefore(img, identity.firstChild);
      }
      if (img.getAttribute('src') !== url) {
        img.style.display = '';
        if (placeholder) placeholder.style.display = 'none';
        img.src = url;
      }
      for (var mi = 0; mi < (models || []).length; mi++) {
        if (String(models[mi].username || '').toLowerCase() === String(username).toLowerCase()) {
          models[mi].profile_image_url = url;
          models[mi].profileImageUrl = url;
          break;
        }
      }
    });
  }

  chunks.reduce(function(prev, chunk) {
    return prev.then(function() {
      return fetch(
        '/api/discover/profile-images?source=chaturbate&usernames=' +
          encodeURIComponent(chunk.join(',')),
        { cache: 'no-store' }
      )
        .then(function(res) { return res.ok ? res.json() : { images: {} }; })
        .then(function(data) { applyImages(data.images || {}); })
        .catch(function() {});
    });
  }, Promise.resolve());
}

// ============================================
// Open live room (in-app watch or external site)
// ============================================
function openWatch(username, sourceType) {
  // Discover must not route YouTube into the in-app live room.
  if (String(sourceType || '').toLowerCase() === 'youtube') {
    openExternalLive(channelUrlForModel({ username: username }, 'youtube'));
    return;
  }
  var qs = sourceType ? ('?source=' + encodeURIComponent(sourceType)) : '';
  window.location.href = '/watch/' + encodeURIComponent(username) + qs;
}

function openExternalLive(url) {
  var target = String(url || '').trim();
  if (!target) return;
  window.open(target, '_blank', 'noopener');
}

// ============================================
// Create card / recording actions on Discover cards.
// ============================================
function updateDiscoverRecordButton(btn, isRecording, username) {
  if (!btn) return;
  btn.classList.toggle('active', !!isRecording);
  btn.classList.remove('create-card-btn');
  btn.classList.add('record-btn');
  btn.textContent = isRecording ? 'Recording' : 'Record';
  btn.title = (isRecording ? 'Turn off recording' : 'Turn on recording') + ' for ' + username;
  btn.setAttribute(
    'onclick',
    "event.stopPropagation(); toggleRecordingOnCard('" +
      escapeInlineJs(username) + "', '" +
      escapeInlineJs(btn.getAttribute('data-source') || '') + "', '" +
      escapeInlineJs(btn.getAttribute('data-display') || username) + "', this)"
  );
}

function closeCreateCardDialog() {
  createCardPending = null;
  var modal = document.getElementById('discoverCreateCardModal');
  if (modal) {
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
  }
}

function openCreateCardDialog(username, sourceType, displayName, btn) {
  if (!username || (btn && (btn.classList.contains('busy') || btn.disabled))) return;
  createCardPending = {
    username: username,
    sourceType: sourceType || 'chaturbate',
    displayName: displayName || username,
    btn: btn || null
  };
  var modal = document.getElementById('discoverCreateCardModal');
  var title = document.getElementById('discoverCreateCardTitle');
  var hint = document.getElementById('discoverCreateCardHint');
  if (title) title.textContent = 'Create card for ' + (displayName || username);
  if (hint) {
    hint.textContent = 'Create a local streamer card. Recording is optional and independent.';
  }
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'discoverCreateCardModal';
    modal.className = 'hxylive-confirm-modal';
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML =
      '<div class="hxylive-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="discoverCreateCardTitle">' +
        '<h2 id="discoverCreateCardTitle">Create card</h2>' +
        '<p id="discoverCreateCardHint"></p>' +
        '<div class="hxylive-confirm-actions discover-create-card-actions">' +
          '<button type="button" class="hxylive-confirm-ok" id="discoverCreateCardRecord">Create and record</button>' +
          '<button type="button" class="hxylive-confirm-ok" id="discoverCreateCardOnly">Create card only</button>' +
          '<button type="button" class="hxylive-confirm-cancel" id="discoverCreateCardCancel">Cancel</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', function(ev) {
      if (ev.target === modal) closeCreateCardDialog();
    });
    document.getElementById('discoverCreateCardCancel').addEventListener('click', closeCreateCardDialog);
    document.getElementById('discoverCreateCardOnly').addEventListener('click', function() {
      confirmCreateCard(false);
    });
    document.getElementById('discoverCreateCardRecord').addEventListener('click', function() {
      confirmCreateCard(true);
    });
  }
  if (title) title.textContent = 'Create card for ' + (displayName || username);
  if (hint) {
    hint.textContent = 'Create a local streamer card. Recording is optional and independent.';
  }
  modal.style.display = 'flex';
  modal.setAttribute('aria-hidden', 'false');
}

async function confirmCreateCard(withRecording) {
  var pending = createCardPending;
  if (!pending || !pending.username) {
    closeCreateCardDialog();
    return;
  }
  var username = pending.username;
  var sourceType = pending.sourceType || 'chaturbate';
  var displayName = pending.displayName || username;
  var btn = pending.btn;
  var key = sourceKey(username, sourceType);
  closeCreateCardDialog();
  if (btn) {
    btn.classList.add('busy');
    btn.disabled = true;
  }
  try {
    var res = await fetch('/api/media-profiles/ensure-card', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: username,
        sourceType: sourceType,
        channelUsername: username,
        channelUrl: channelUrlForModel({ username: username }, sourceType),
        displayName: displayName,
        autoRecord: !!withRecording
      })
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) throw new Error(data.detail || 'Failed to create card');
    cardSet.add(key);
    recordingProfileByKey[key] = username;
    if (withRecording) recordingSet.add(key);
    if (btn) {
      btn.setAttribute('data-source', sourceType);
      btn.setAttribute('data-display', displayName);
      updateDiscoverRecordButton(btn, !!withRecording, username);
    }
    showNotification(
      withRecording
        ? 'Card created and recording enabled for ' + username
        : 'Card created for ' + username,
      'success'
    );
  } catch (e) {
    showNotification(e.message || 'Failed to create card', 'error');
  } finally {
    if (btn) {
      btn.classList.remove('busy');
      btn.disabled = false;
    }
  }
}

async function toggleRecordingOnCard(username, sourceType, displayName, btn) {
  if (!username || !btn || btn.classList.contains('busy') || btn.disabled) return;
  var key = sourceKey(username, sourceType);
  var wasRecording = recordingSet.has(key);
  var profileUsername = recordingProfileByKey[key] || username;
  var enabled = !wasRecording;
  var source = sourceType || 'chaturbate';
  var channelUrl = channelUrlForModel({ username: username }, source);

  btn.classList.add('busy');
  btn.disabled = true;
  try {
    var res = await fetch('/api/media-profiles/' + encodeURIComponent(profileUsername) + '/auto-record', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        autoRecord: enabled,
        sourceType: source,
        channelUsername: username,
        channelUrl: channelUrl,
        displayName: displayName || username
      })
    });
    var data = await res.json().catch(function() { return {}; });
    if (res.status === 404 && enabled) {
      res = await fetch('/api/media-profiles/ensure-card', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: username,
          sourceType: source,
          channelUsername: username,
          channelUrl: channelUrl,
          displayName: displayName || username,
          autoRecord: true
        })
      });
      data = await res.json().catch(function() { return {}; });
      recordingProfileByKey[key] = username;
      cardSet.add(key);
    }
    if (res.ok) {
      if (enabled) recordingSet.add(key);
      else recordingSet.delete(key);
      updateDiscoverRecordButton(btn, enabled, username);
      showNotification(
        enabled ? 'Recording enabled for ' + username : 'Recording paused for ' + username,
        'success'
      );
    } else {
      showNotification(data.detail || 'Failed to update recording', 'error');
    }
  } catch (e) {
    showNotification(e.message || 'Connection error', 'error');
  } finally {
    btn.classList.remove('busy');
    btn.disabled = false;
  }
}

// ============================================
// Tag filtering
// ============================================
function pickDiscoverDisplayTags(tags, preferred, limit) {
  // limit omitted/null/0 → show all tags. Positive limit caps the preview.
  var hasCap = limit != null && Number(limit) > 0;
  var max = hasCap ? Math.max(1, Number(limit)) : Number.POSITIVE_INFINITY;
  var raw = Array.isArray(tags) ? tags : [];
  var prefer = (preferred || []).map(function(t) {
    return String(t || '').trim().toLowerCase();
  }).filter(Boolean);
  var seen = {};
  var out = [];
  function pushTag(tag) {
    var label = String(tag || '').trim();
    if (!label) return;
    var key = label.toLowerCase();
    if (seen[key]) return;
    seen[key] = true;
    out.push(label);
  }
  // 1) Active filters that this model actually has (preserve filter chip order).
  prefer.forEach(function(needle) {
    if (out.length >= max) return;
    for (var i = 0; i < raw.length; i++) {
      if (String(raw[i] || '').trim().toLowerCase() === needle) {
        pushTag(raw[i]);
        break;
      }
    }
  });
  // 2) Remaining tags in original order (all of them when uncapped).
  for (var j = 0; j < raw.length && out.length < max; j++) {
    pushTag(raw[j]);
  }
  return out;
}

function addTagFilter(tag) {
  tag = tag.toLowerCase().trim();
  if (!tag || activeTags.indexOf(tag) !== -1) return;
  activeTags.push(tag);
  resetDiscoverListState();
  renderActiveTagFilters();
  fetchDiscover();
}

function removeTagFilter(tag) {
  activeTags = activeTags.filter(function(t) { return t !== tag; });
  resetDiscoverListState();
  renderActiveTagFilters();
  fetchDiscover();
}

function clearAllTags() {
  activeTags = [];
  resetDiscoverListState();
  renderActiveTagFilters();
  fetchDiscover();
}

function renderActiveTagFilters() {
  var container = document.getElementById('activeTagFilters');
  if (!container) return;

  if (activeTags.length === 0) {
    container.style.display = 'none';
    return;
  }

  container.style.display = 'flex';
  container.innerHTML = activeTags.map(function(tag) {
    return '<span class="active-tag-chip">' +
      escapeHtml(tag) +
      '<button onclick="event.stopPropagation(); removeTagFilter(\'' + escapeInlineJs(tag) + '\')">&times;</button>' +
    '</span>';
  }).join('') +
  '<button class="clear-tags-btn" onclick="clearAllTags()">Clear all</button>';
}

function handleTagInput(e) {
  // Ignore Enter while an IME composition session is confirming (Chinese etc.).
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter') {
    var input = document.getElementById('tagInput');
    var val = input.value.trim();
    if (val) {
      addTagFilter(val);
      input.value = '';
    }
  }
}

// ============================================
// Escape HTML helper
// ============================================
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

let discoverUptimeInterval = null;

function refreshDiscoverUptimeDisplays() {
  var nodes = document.querySelectorAll('.discover-uptime-line[data-started-at]');
  for (var i = 0; i < nodes.length; i++) {
    var line = nodes[i];
    var startedAt = line.getAttribute('data-started-at') || '';
    var text = formatStreamUptime(startedAt);
    var valueEl = line.querySelector('.discover-uptime');
    if (!text) {
      line.style.display = 'none';
      if (valueEl) valueEl.textContent = '';
      continue;
    }
    line.style.display = '';
    if (valueEl) valueEl.textContent = text;
  }
}

function startDiscoverUptimeTicker() {
  if (discoverUptimeInterval) return;
  refreshDiscoverUptimeDisplays();
  discoverUptimeInterval = setInterval(refreshDiscoverUptimeDisplays, 1000);
}

function escapeHtml(text) {
  if (!text) return '';
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

function escapeInlineJs(value) {
  return String(value == null ? '' : value)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, '\\x27')
    .replace(/"/g, '\\x22')
    .replace(/&/g, '\\x26')
    .replace(/</g, '\\x3c')
    .replace(/>/g, '\\x3e')
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

// ============================================
// Pagination
// ============================================
function setPaginationLoading(isLoading) {
  isDiscoverLoading = isLoading;
}

function updateDiscoverLoadStatus(message) {
  var status = document.getElementById('discoverLoadStatus');
  if (!status) return;
  // YouTube Discover is 0-or-1 card — never paginate or show end-of-list copy.
  if (String(currentSource || '').toLowerCase() === 'youtube') {
    discoverHasMore = false;
    status.dataset.mode = '';
    status.innerHTML = '';
    status.textContent = '';
    status.style.display = 'none';
    return;
  }
  var grid = document.getElementById('discoverGrid');
  var hasCards = !!(grid && grid.querySelector('.discover-card'));
  // Empty / loading placeholders already explain the state — hide the footer.
  if (!hasCards) {
    status.dataset.mode = '';
    status.innerHTML = '';
    status.textContent = '';
    status.style.display = 'none';
    return;
  }
  if (message === 'Loading more...') {
    status.dataset.mode = 'loading-more';
    status.innerHTML = '<div class="icon" aria-hidden="true">&#9203;</div><p>' + message + '</p>';
    status.style.display = 'flex';
    return;
  }
  status.dataset.mode = '';
  status.innerHTML = '';
  if (message) {
    status.textContent = message;
    status.style.display = 'block';
    return;
  }
  status.textContent = discoverHasMore ? 'Scroll for more' : 'You have reached the end';
  status.style.display = 'block';
}

function loadNextDiscoverPage() {
  if (isDiscoverLoading || !discoverHasMore) return;
  fetchDiscover({ append: true });
}

function maybeLoadNextDiscoverPage() {
  if (isDiscoverLoading || !discoverHasMore) return;
  var status = document.getElementById('discoverLoadStatus');
  if (status && status.getBoundingClientRect().top <= window.innerHeight + 500) {
    loadNextDiscoverPage();
  }
}

function setupInfiniteDiscoverScroll() {
  var status = document.getElementById('discoverLoadStatus');
  if (!status) return;
  if ('IntersectionObserver' in window) {
    infiniteScrollObserver = new IntersectionObserver(function(entries) {
      if (entries.some(function(entry) { return entry.isIntersecting; })) {
        loadNextDiscoverPage();
      }
    }, { rootMargin: '600px 0px' });
    infiniteScrollObserver.observe(status);
  } else {
    window.addEventListener('scroll', maybeLoadNextDiscoverPage, { passive: true });
  }
}

// ============================================
// Filter handlers
// ============================================
function setCategory(itemOrKey, btn) {
  var helpers = categoryHelpers();
  var item = null;
  if (itemOrKey && typeof itemOrKey === 'object') {
    item = itemOrKey;
  } else if (helpers && helpers.findFormalByKey) {
    item = helpers.findFormalByKey(formalCategories, itemOrKey || 'all');
  }
  if (!item && helpers) {
    item = helpers.safeFallbackItemsForSource
      ? helpers.safeFallbackItemsForSource(currentSource)[0]
      : helpers.safeAllFallbackItems()[0];
  }
  if (!item) return;

  var gate = helpers && helpers.evaluateCategoryRequestSupport
    ? helpers.evaluateCategoryRequestSupport(item)
    : { supported: true, normalized: item };
  if (!gate.supported) {
    // Block send — never silently degrade to gender=<value>.
    setCategoryStatus(gate.message || 'This category filter is not wired yet', true);
    syncCategoryPillActiveState();
    return;
  }

  applySelectedCategoryItem(gate.normalized || item);
  resetDiscoverListState();
  selectedSecondaryFilters = {};
  setCategoryStatus('', false);
  if (btn && btn.classList) {
    var host = categoryFiltersHost();
    if (host) {
      host.querySelectorAll('.filter-pill').forEach(function(pill) {
        pill.classList.remove('active');
      });
    }
    btn.classList.add('active');
  } else {
    syncCategoryPillActiveState();
  }
  fetchDiscover();
}

/** Compat wrapper — resolves gender value to a formal category item. */
function setGender(gender, btn) {
  var helpers = categoryHelpers();
  var item = null;
  if (helpers && helpers.findFormalByGenderValue) {
    item = helpers.findFormalByGenderValue(formalCategories, gender);
  }
  if (!item && helpers && helpers.findFormalByKey) {
    item = helpers.findFormalByKey(formalCategories, gender || 'all');
  }
  setCategory(item || { canonical_key: 'all', category_type: 'all', request_param: null, request_value: null }, btn);
}

async function setSource(sourceType) {
  var next = normalizeDiscoverSource(sourceType);
  currentSource = next;
  // Drop prior source category immediately — do not keep old filters across sources.
  resetCategorySelectionToAll();
  selectedSecondaryFilters = {};
  resetDiscoverListState();
  clearCategoryButtons();
  applyDiscoverStateToControls();
  syncDiscoverStateToUrl();
  var preferred = categoryHelpers() && categoryHelpers().preferredDefaultForSource
    ? categoryHelpers().preferredDefaultForSource(currentSource)
    : { canonical_key: 'all' };
  var result = await loadCategoriesForSource(currentSource, {
    preferred: preferred
  });
  if (result && result.stale) return;
  resetDiscoverListState();
  fetchDiscover();
}

function searchModels(query) {
  currentSearch = query;
  resetDiscoverListState();
  fetchDiscover();
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
// Auto-refresh live thumbnails
// ============================================
// Every 30s: update cover/avatar URLs in place for the current page (all
// sources). Do NOT wipe/rebuild the card grid while the user is browsing —
// Chaturbate roomlist order churn would otherwise flash-reload every tick.
// Full roster reload only when returning to a stale tab / bfcache restore.
const DISCOVER_HIDDEN_REFETCH_MS = 30000;

function discoverRosterIsFirstPageOnly() {
  if (currentPage > 1) return false;
  var grid = document.getElementById('discoverGrid');
  if (!grid) return false;
  return grid.querySelectorAll('.discover-card').length <= DISCOVER_PAGE_LIMIT;
}

function maybeRefetchStaleDiscoverRoster() {
  if (document.hidden || isDiscoverLoading) return;
  if (!discoverRosterIsFirstPageOnly()) return;
  var age = lastDiscoverFetchAt ? (Date.now() - lastDiscoverFetchAt) : DISCOVER_HIDDEN_REFETCH_MS;
  if (age < DISCOVER_HIDDEN_REFETCH_MS) return;
  fetchDiscover();
}

async function refreshLiveThumbnails() {
  if (document.hidden) return; // pause on background tabs
  var grid = document.getElementById('discoverGrid');
  if (!grid || !grid.querySelector('.discover-card')) return;

  var params = buildDiscoverParams(currentPage);

  try {
    var res = await fetch('/api/discover?' + params.toString(), { cache: 'no-store' });
    if (!res.ok) return;
    var data = await res.json();
    var models = data.models || [];
    models.forEach(function(model) {
      var card = grid.querySelector('.discover-card[data-username="' + CSS.escape(model.username) + '"]');
      if (!card) return;
      var cardSource = String(
        card.getAttribute('data-source') || model.source_type || model.platform || currentSource || 'chaturbate'
      ).toLowerCase();
      // Only the cover plane — never the circular .discover-avatar (CB riw / SC snapshot).
      var thumbImg = card.querySelector('.discover-card-thumb img');
      if (thumbImg) {
        var newThumb = thumbnailUrlForModel(model, cardSource);
        if (thumbImg.getAttribute('src') !== newThumb) thumbImg.src = newThumb;
      }
      var avatarImg = card.querySelector('.discover-identity .discover-avatar');
      if (!avatarImg) return;
      var newAvatar = String(
        model.profile_image_url || model.profileImageUrl || model.avatar_url || model.avatarUrl || ''
      ).trim();
      if (cardSource === 'stripchat' && newAvatar) {
        newAvatar = newAvatar.replace(
          /^https?:\/\/(?:img\.)?doppiocdn\.[^/]+(\/avatars\/)/i,
          'https://static-proxy.strpst.com$1'
        );
      }
      if (
        !newAvatar ||
        /thumb\.live\.mmcdn\.com\/riw\//i.test(newAvatar) ||
        (/doppiocdn\./i.test(newAvatar) && /\/snapshot\//i.test(newAvatar)) ||
        (/(doppiocdn\.|static-proxy\.strpst\.com)/i.test(newAvatar) && /\/previews\//i.test(newAvatar)) ||
        /previews-ttv/i.test(newAvatar) ||
        /live_user_/i.test(newAvatar)
      ) {
        return;
      }
      if (avatarImg.getAttribute('src') !== newAvatar) {
        avatarImg.style.display = '';
        var ph = card.querySelector('.discover-identity .discover-avatar-placeholder');
        if (ph) ph.style.display = 'none';
        avatarImg.src = newAvatar;
      }
    });
  } catch (e) {
    // Silent: will retry on the next tick
  }
}

// ============================================
// Initialization
// ============================================
window.addEventListener('DOMContentLoaded', function() {
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }';
  document.head.appendChild(style);
  startDiscoverUptimeTicker();
  readDiscoverStateFromUrl();

  // Set up search with debounce
  var searchInput = document.getElementById('searchInput');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(function() {
        searchModels(searchInput.value.trim());
      }, 400);
    });
    searchInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        clearTimeout(searchTimeout);
        searchModels(searchInput.value.trim());
      }
    });
  }

  // Set up tag input
  var tagInput = document.getElementById('tagInput');
  if (tagInput) {
    tagInput.addEventListener('keydown', handleTagInput);
  }

  var sourceFilters = document.getElementById('sourceFilters');
  if (sourceFilters) {
    sourceFilters.addEventListener('click', function(event) {
      var button = event.target.closest('.discover-source-btn');
      if (button) setSource(button.getAttribute('data-source'));
    });
  }

  var categoryFilters = categoryFiltersHost();
  if (categoryFilters) {
    categoryFilters.addEventListener('click', function(event) {
      var button = event.target.closest('.filter-pill');
      if (!button || !categoryFilters.contains(button)) return;
      var helpers = categoryHelpers();
      var item = {
        canonical_key: button.getAttribute('data-canonical') || 'all',
        category_type: button.getAttribute('data-category-type') || 'all',
        request_param: button.getAttribute('data-request-param') || null,
        request_value: button.getAttribute('data-request-value') || null,
        display_label: button.textContent || '',
        available: true,
        readiness: 'verified'
      };
      if (item.request_param === '') item.request_param = null;
      if (item.request_value === '') item.request_value = null;
      if (helpers && helpers.normalizeCategoryItem) item = helpers.normalizeCategoryItem(item);
      setCategory(item, button);
    });
  }


  // Boot once from the URL/default source. If the user already clicked another
  // website while providers/follows were loading, setSource owns that load —
  // do not start a second categories request that aborts the first.
  var bootSource = currentSource;
  Promise.all([loadCardAndRecordingSets(), loadDiscoverProviders()]).finally(function() {
    applyDiscoverStateToControls();
    setupInfiniteDiscoverScroll();
    if (currentSource !== bootSource) {
      return;
    }
    var preferredFromUrl = {
      canonical_key: selectedCategoryKey,
      gender: currentGender,
      selectedCategoryKey: selectedCategoryKey,
      selectedCategoryRequestValue: selectedCategoryRequestValue
    };
    loadCategoriesForSource(currentSource, {
      preferred: preferredFromUrl
    }).then(function(result) {
      if (result && result.stale) return;
      if (currentSource !== bootSource) return;
      fetchDiscover();
    });
  });

  window.addEventListener('popstate', function() {
    readDiscoverStateFromUrl();
    applyDiscoverStateToControls();
    loadCategoriesForSource(currentSource, {
      preferred: {
        canonical_key: selectedCategoryKey,
        gender: currentGender,
        selectedCategoryKey: selectedCategoryKey,
        selectedCategoryRequestValue: selectedCategoryRequestValue
      }
    }).then(function(result) {
      if (result && result.stale) return;
      fetchDiscover();
    });
  });

  // Back-forward cache restores the old DOM (and username list) without
  // re-running boot — force a live roomlist reload on first page.
  window.addEventListener('pageshow', function(event) {
    if (!event || !event.persisted) return;
    if (document.hidden || isDiscoverLoading) return;
    if (!discoverRosterIsFirstPageOnly()) return;
    fetchDiscover();
  });
  document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
      discoverHiddenAt = Date.now();
      return;
    }
    var hiddenFor = discoverHiddenAt ? (Date.now() - discoverHiddenAt) : 0;
    if (hiddenFor >= DISCOVER_HIDDEN_REFETCH_MS) {
      maybeRefetchStaleDiscoverRoster();
    }
  });

  // Quiet cover refresh only — does not rebuild the card grid.
  setInterval(refreshLiveThumbnails, 30000);
});

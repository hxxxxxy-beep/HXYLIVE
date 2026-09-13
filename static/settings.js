// ============================================
// Settings Page - Configuration and account management
// ============================================

// ============================================
// Tab Navigation
// ============================================
function initTabs() {
  var navItems = document.querySelectorAll('.settings-nav-item');
  navItems.forEach(function(item) {
    item.addEventListener('click', function() {
      var tabId = this.getAttribute('data-tab');
      // Deactivate all
      navItems.forEach(function(n) { n.classList.remove('active'); });
      document.querySelectorAll('.settings-tab').forEach(function(t) { t.classList.remove('active'); });
      // Activate clicked
      this.classList.add('active');
      var tab = document.getElementById('tab-' + tabId);
      if (tab) tab.classList.add('active');
      // Load stats on first visit
      if (tabId === 'statistics' && !statsLoaded) {
        loadSystemStats();
        statsLoaded = true;
      }
      if (tabId === 'logs' && !logsLoaded) {
        loadLogs();
        logsLoaded = true;
      }
      if (tabId === 'tests' && !testsLoaded) {
        renderTestsList();
        runAllTests();
        testsLoaded = true;
      }
      if (tabId === 'providers') {
        loadProviders();
      }
      if (tabId === 'lifecycle') {
        loadRecordingLifecycle();
      }
      if (tabId === 'processes') {
        startProcessesPolling();
      } else {
        stopProcessesPolling();
      }
    });
  });
}

var statsLoaded = false;
var logsLoaded = false;
var testsLoaded = false;
var logsOffset = 0;
var statsRefreshInterval = null;

// ============================================
// Provider capabilities
// ============================================

async function loadProviders() {
  var list = document.getElementById('providersList');
  if (!list) return;
  list.innerHTML = '<div class="proc-empty">Checking provider sessions...</div>';
  try {
    // verify=1 live-checks each site session so Connected matches syncability.
    var res = await fetch('/api/providers?verify=1', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    renderProviders(data.providers || []);
  } catch (e) {
    list.innerHTML = '<div class="proc-empty">Provider list unavailable</div>';
  }
}

function renderProviders(providers) {
  var list = document.getElementById('providersList');
  if (!list) return;
  providers = providers || [];
  if (!providers.length) {
    list.innerHTML = '<div class="proc-empty">No providers configured.</div>';
    return;
  }

  list.innerHTML = providers.map(function(provider) {
    var source = provider.sourceType;
    var caps = provider.capabilities || {};
    var status = provider.status || {};
    var enabled = provider.enabled !== false;
    var supportsAccount = caps.can_login === true;
    var connected = supportsAccount && status.isLoggedIn === true;
    var capabilities = providerCapabilityChecks(caps);
    var accountControls = supportsAccount ? providerAccountControls(source, status, caps) : '';
    var statusClass = supportsAccount ? providerStatusClass(status) : 'available';
    var statusText = supportsAccount ? providerStatusText(status) : 'Local';
    var enabledControl = providerEnabledControl(source, enabled);

    return '<div class="provider-card ' + (enabled ? '' : 'is-provider-disabled') + '">' +
      '<div class="provider-card-main">' +
        '<div>' +
          '<div class="provider-title">' + escapeHtml(provider.displayName || source) + '</div>' +
          '<div class="provider-muted">' + escapeHtml(providerAvailabilityText(caps)) + '</div>' +
        '</div>' +
        '<div class="provider-card-actions">' +
          enabledControl +
          '<span class="status-indicator ' + statusClass + '">' + escapeHtml(statusText) + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="provider-capability-list">' + capabilities.join('') + '</div>' +
      accountControls +
    '</div>';
  }).join('');
}

function providerEnabledControl(source, enabled) {
  return '<div class="provider-enabled-control">' +
    '<span>Enabled</span>' +
    '<label class="toggle-switch">' +
      '<input type="checkbox" ' + (enabled ? 'checked' : '') +
        ' onchange="toggleProviderEnabled(\'' + escapeInlineJs(source) + '\', this.checked, this)">' +
      '<span class="toggle-slider"></span>' +
    '</label>' +
  '</div>';
}

async function toggleProviderEnabled(source, enabled, input) {
  if (input) input.disabled = true;
  try {
    var res = await fetch('/api/providers/' + encodeURIComponent(source) + '/enabled', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!enabled })
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      if (input) input.checked = !enabled;
      showNotification(data.detail || 'Failed to update provider', 'error');
      return;
    }
    showNotification(data.enabled ? 'Provider enabled' : 'Provider disabled', 'success');
    loadProviders();
  } catch (e) {
    if (input) input.checked = !enabled;
    showNotification('Connection error', 'error');
  } finally {
    if (input) input.disabled = false;
  }
}

function providerStatusClass(status) {
  if (status.isLoggedIn) return 'connected';
  if (providerStatusSessionExpired(status)) return 'disconnected';
  if (status.hasSavedCredentials || status.hasSavedSessionData || status.hasCookies || status.hasLocalStorage) return 'unknown';
  if (status.lastError) return 'disconnected';
  return 'disconnected';
}

function providerStatusText(status) {
  if (status.isLoggedIn) return 'Connected';
  if (providerStatusNeedsSessionImport(status)) return 'Session Required';
  if (providerStatusLoginFailed(status)) return 'Login Failed';
  if (providerStatusSessionExpired(status)) return 'Session Expired';
  if (status.hasSavedCredentials) return 'Credentials Saved';
  if (status.hasSavedSessionData || status.hasCookies || status.hasLocalStorage) return 'Session Saved';
  return 'Not Connected';
}

function providerAvailabilityText(caps) {
  if (caps.can_login && caps.can_sync_following) return 'Live, recording, remote sync and follow';
  if (caps.can_discover && caps.can_record && caps.can_follow) return 'Live, recording and local follows';
  if (caps.can_discover && caps.can_record) return 'Live and recording';
  if (caps.can_discover) return 'Live discovery';
  if (caps.can_record) return 'Recording';
  return 'Registered provider';
}

function providerCapabilityChecks(caps) {
  return [
    providerCapabilityCheck('Discover', !!caps.can_discover),
    providerCapabilityCheck('Record', !!caps.can_record),
    providerCapabilityCheck('Follow / Unfollow', !!caps.can_follow),
    providerCapabilityCheck('Sync', !!caps.can_sync_following)
  ];
}

function providerCapabilityCheck(label, enabled) {
  return '<label class="provider-capability ' + (enabled ? 'is-enabled' : 'is-disabled') + '">' +
    '<span class="provider-capability-icon" aria-hidden="true">' + (enabled ? '&#10003;' : '&#10005;') + '</span>' +
    '<span>' + escapeHtml(label) + '</span>' +
  '</label>';
}

function providerAccountControls(source, status, caps) {
  var connected = status.isLoggedIn === true;
  var canSync = connected && caps.can_sync_following === true;
  var username = status.username ? '<div class="provider-muted">Connected as ' + escapeHtml(status.username) + '</div>' : '';
  var savedMessage = providerSavedCredentialsMessage(status);
  var error = providerStatusError(status);
  var sessionHint = (connected && canSync)
    ? '<div class="provider-muted">Use Sync follows to open Media and reconcile local cards with this site. Follow/Unfollow here also updates the site.</div>'
    : (!connected
    ? '<div class="provider-muted">Log into this site in Google Chrome on this Mac, then Import from Chrome. Use Sync follows afterwards to open Media reconcile.</div>'
    : '');
  var sync = canSync
    ? '<button type="button" class="btn btn-secondary btn-sm" onclick="syncProviderFollowing(\'' + escapeInlineJs(source) + '\')">Sync follows</button>'
    : '';
  var importFromChrome = !connected
    ? '<button type="button" class="btn btn-primary btn-sm" onclick="importProviderSessionFromChrome(\'' + escapeInlineJs(source) + '\')">Import from Chrome</button>'
    : '';
  var logout = connected
    ? '<button type="button" class="btn btn-secondary btn-sm" onclick="logoutProvider(\'' + escapeInlineJs(source) + '\')">Disconnect</button>'
    : '';

  return '<div class="provider-account">' +
    username +
    sessionHint +
    (savedMessage ? '<div class="provider-muted">' + escapeHtml(savedMessage) + '</div>' : '') +
    (error ? '<div class="provider-error">' + escapeHtml(error) + '</div>' : '') +
    '<div class="provider-actions">' + importFromChrome + sync + logout + '</div>' +
  '</div>';
}

function providerSavedCredentialsMessage(status) {
  if (status.isLoggedIn) return '';
  if (providerStatusSessionExpired(status)) {
    return 'Saved session failed live check; Import from Chrome again.';
  }
  if (status.hasSavedCredentials) return 'Saved credentials are stored; reconnect to verify the session.';
  if (status.hasSavedSessionData || status.hasCookies || status.hasLocalStorage) return 'Browser session data is saved but still needs verification.';
  return '';
}

function providerStatusError(status) {
  if (!status || !status.lastError) return '';
  if (providerStatusNeedsSessionImport(status)) {
    return 'Automatic login was blocked; import a verified browser session.';
  }
  if (providerStatusSessionExpired(status)) {
    return 'Session expired or rejected by the site; Import from Chrome again.';
  }
  return providerConnectionError(status.lastError);
}

function providerConnectionError(error) {
  if (!error) return '';
  var text = String(error);
  if (/captcha|2fa|challenge|cloudflare|interaction/i.test(text)) {
    return 'Automatic login was blocked; import a verified browser session.';
  }
  if (/invalid|incorrect|password|credential|login failed/i.test(text)) {
    return 'Automatic account login failed. Check credentials.';
  }
  return text;
}

function providerStatusNeedsSessionImport(status) {
  return !!(status && status.lastError && /captcha|2fa|challenge|cloudflare|interaction/i.test(String(status.lastError)));
}

function providerStatusLoginFailed(status) {
  return !!(status && status.lastError && /invalid|incorrect|password|credential|login failed/i.test(String(status.lastError)));
}

function providerStatusSessionExpired(status) {
  return !!(status && !status.isLoggedIn && status.lastError &&
    /not logged in|session expired|login required|session missing/i.test(String(status.lastError)));
}

var MAC_HELPER_BASE = 'http://127.0.0.1:17899';

function helperRequestTimeout(ms) {
  var controller = new AbortController();
  var timer = setTimeout(function() { controller.abort(); }, ms);
  return {
    signal: controller.signal,
    clear: function() { clearTimeout(timer); }
  };
}

function sleepMs(ms) {
  return new Promise(function(resolve) { setTimeout(resolve, ms); });
}

async function postProviderSession(source, payload) {
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
  showNotification('Provider session imported', 'success');
  loadProviders();
  return data;
}

async function fetchChromeProviderCookies(source) {
  var timeout = helperRequestTimeout(90000);
  try {
    var res = await fetch(MAC_HELPER_BASE + '/provider-cookies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sourceType: source }),
      cache: 'no-store',
      signal: timeout.signal
    });
    var data = await res.json().catch(function() { return {}; });
    if (res.ok && data.ok && (data.cookieHeader || (data.cookies && data.cookies.length))) {
      return data;
    }
    var err = new Error(data.error || 'Mac helper could not read Chrome cookies');
    err.loginUrl = data.loginUrl || '';
    err.status = res.status;
    throw err;
  } finally {
    timeout.clear();
  }
}

async function queueChromeProviderImport(source) {
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

async function waitForQueuedChromeImport(commandId) {
  var deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    var res = await fetch('/api/mac/helper/import-session/' + encodeURIComponent(commandId), {
      cache: 'no-store'
    });
    var data = await res.json().catch(function() { return {}; });
    if (res.status === 404) {
      throw new Error(data.detail || 'Import result expired; retry Import from Chrome');
    }
    if (!res.ok) {
      throw new Error(data.detail || data.error || 'Chrome import failed');
    }
    if (data.status === 'done') {
      if (data.success) {
        showNotification('Provider session imported', 'success');
        loadProviders();
        return data;
      }
      throw new Error(data.detail || 'Log into this site in Google Chrome, then retry Import from Chrome');
    }
    await sleepMs(700);
  }
  throw new Error('Chrome import timed out. Allow Keychain access if macOS asked, then retry.');
}

async function importProviderSessionFromChrome(source) {
  try {
    try {
      var cookies = await fetchChromeProviderCookies(source);
      await postProviderSession(source, {
        cookieHeader: cookies.cookieHeader || '',
        cookies: cookies.cookies || [],
        userAgent: cookies.userAgent || '',
        username: cookies.username || ''
      });
      return;
    } catch (directErr) {
      if (directErr && directErr.status) {
        throw directErr;
      }
    }
    var queued = await queueChromeProviderImport(source);
    await waitForQueuedChromeImport(queued.commandId);
  } catch (e) {
    showNotification(e.message || 'Chrome import failed', 'error');
  }
}

async function logoutProvider(source) {
  try {
    var res = await fetch('/api/providers/' + encodeURIComponent(source) + '/logout', { method: 'POST' });
    if (res.ok) {
      showNotification('Provider disconnected', 'success');
      loadProviders();
    } else {
      showNotification('Disconnect failed', 'error');
    }
  } catch (e) {
    showNotification('Connection error', 'error');
  }
}

async function syncProviderFollowing(source) {
  var site = String(source || '').trim().toLowerCase();
  if (!site) return;
  window.location.href = '/media?reconcile=' + encodeURIComponent(site);
}

// ============================================
// Check FlareSolverr status
// ============================================
async function checkFlareSolverr(manual) {
  var statusEl = document.getElementById('flareStatus');
  var versionRow = document.getElementById('flareVersionRow');
  var versionEl = document.getElementById('flareVersion');
  var urlInput = document.getElementById('flareUrlInput');
  var messageRow = document.getElementById('flareMessageRow');
  var messageEl = document.getElementById('flareMessage');
  var testBtn = document.getElementById('flareTestBtn');

  if (manual && testBtn) {
    testBtn.disabled = true;
    testBtn.textContent = 'Testing...';
  }
  statusEl.className = 'status-indicator unknown';
  statusEl.textContent = 'Checking...';

  try {
    var res = await fetch('/api/chaturbate/status', { cache: 'no-store' });
    if (res.ok) {
      var data = await res.json();

      if (data.flaresolverrAvailable) {
        statusEl.className = 'status-indicator connected';
        statusEl.textContent = 'Healthy';
      } else {
        statusEl.className = 'status-indicator disconnected';
        statusEl.textContent = 'Not Available';
      }

      if (data.flaresolverrMessage) {
        if (messageRow) messageRow.style.display = '';
        if (messageEl) messageEl.textContent = data.flaresolverrMessage;
      }

      if (data.flaresolverrVersion && versionRow && versionEl) {
        versionRow.style.display = '';
        versionEl.textContent = data.flaresolverrVersion;
      }

      if (data.flaresolverrUrl && urlInput && document.activeElement !== urlInput) {
        urlInput.value = data.flaresolverrUrl;
      }

      if (manual) {
        showNotification(
          data.flaresolverrAvailable ? 'FlareSolverr reachable' : 'FlareSolverr: ' + (data.flaresolverrMessage || 'unreachable'),
          data.flaresolverrAvailable ? 'success' : 'error'
        );
      }
    } else {
      statusEl.className = 'status-indicator unknown';
      statusEl.textContent = 'Unknown';
    }
  } catch (e) {
    statusEl.className = 'status-indicator unknown';
    statusEl.textContent = 'Not Available';
    if (messageEl) messageEl.textContent = 'Network error reaching HXYLIVE API';
    if (messageRow) messageRow.style.display = '';
  } finally {
    if (manual && testBtn) {
      testBtn.disabled = false;
      testBtn.textContent = 'Test connection';
    }
  }
}

async function loadFlareSolverrSettings() {
  var urlInput = document.getElementById('flareUrlInput');
  if (!urlInput) return;

  try {
    var res = await fetch('/api/settings/flaresolverr', { cache: 'no-store' });
    if (!res.ok) return;
    var data = await res.json();
    if (data.flaresolverrUrl || data.url) {
      urlInput.value = data.flaresolverrUrl || data.url;
    }
  } catch (e) {
    console.error('Error loading FlareSolverr settings:', e);
  }
}

async function saveFlareSolverrUrl() {
  var urlInput = document.getElementById('flareUrlInput');
  var saveBtn = document.getElementById('flareSaveBtn');
  if (!urlInput) return;

  var url = String(urlInput.value || '').trim();
  if (!url) {
    showNotification('FlareSolverr URL is required', 'error');
    return;
  }

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
  }

  try {
    var res = await fetch('/api/settings/flaresolverr', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url })
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      showNotification(data.detail || 'Unable to save FlareSolverr URL', 'error');
      return;
    }
    urlInput.value = data.flaresolverrUrl || data.url || url;
    showNotification('FlareSolverr URL saved', 'success');
    await checkFlareSolverr(true);
  } catch (e) {
    showNotification('Connection error', 'error');
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  }
}

// ============================================
// Load app version and config
// ============================================
async function loadAppInfo() {
  try {
    var res = await fetch('/api/version');
    if (res.ok) {
      var data = await res.json();
      if (data.output_dir || data.config) {
        var config = data.config || data;
        if (config.output_dir) setText('outputDir', config.output_dir);
        if (config.ffmpeg_path) setText('ffmpegPath', config.ffmpeg_path);
        if (config.check_interval_seconds || config.check_interval) {
          setCheckIntervalInput(config.check_interval_seconds || config.check_interval);
        }
      }
    }
  } catch (e) {
    console.error('Error loading app info:', e);
    var statusEl = document.getElementById('apiStatus');
    if (statusEl) {
      statusEl.className = 'status-indicator disconnected';
      statusEl.textContent = 'Disconnected';
    }
  }
}

// ============================================
// Recording Settings (auto_convert, keep_ts)
// ============================================
function normalizeRetentionDays(value, fallback) {
  var parsed = parseInt(value, 10);
  if (isNaN(parsed)) parsed = fallback;
  return Math.max(0, Math.min(365, parsed));
}

function normalizeMonthlyQuotaGb(value, fallback) {
  var parsed = parseInt(value, 10);
  if (isNaN(parsed)) parsed = fallback;
  return Math.max(0, Math.min(10000, parsed));
}

function normalizeQuotaCycleDay(value, fallback) {
  var parsed = parseInt(value, 10);
  if (isNaN(parsed)) parsed = fallback;
  return Math.max(1, Math.min(28, parsed));
}

function normalizeSegmentSizeMb(value) {
  var parsed = parseInt(value, 10);
  if (isNaN(parsed)) parsed = 0;
  return Math.max(0, parsed);
}

function normalizeCheckIntervalSeconds(value) {
  var parsed = parseInt(value, 10);
  if (isNaN(parsed)) parsed = 120;
  return Math.max(30, Math.min(3600, parsed));
}

function setCheckIntervalInput(value) {
  var input = document.getElementById('checkIntervalInput');
  if (input) input.value = normalizeCheckIntervalSeconds(value);
}

function applyRecordingGateUi(data) {
  var toggle = document.getElementById('recordingEnabledToggle');
  var messageEl = document.getElementById('recordingGateMessage');
  if (!toggle) return;

  var gate = (data && data.recording_gate) || 'on';
  var message = (data && data.recording_gate_message) || '';
  var libraryBlocked = !!(data && data.library_space_blocked);
  var stagingCleaning = !!(data && (data.staging_blocked || data.staging_cleaning));
  var enabled = data && data.recording_enabled !== false;

  if (libraryBlocked) {
    toggle.checked = false;
    toggle.disabled = true;
    message = message || 'Library disk space is too low. Free space, then turn Auto Record back on.';
  } else if (stagingCleaning && enabled) {
    toggle.checked = true;
    toggle.disabled = false;
    message = message || 'Cleaning SSD staging; captures are paused until staging is empty.';
  } else {
    toggle.checked = !!enabled;
    toggle.disabled = false;
    if (gate === 'on') message = '';
  }

  if (messageEl) {
    if (message) {
      messageEl.textContent = message;
      messageEl.style.display = 'block';
    } else {
      messageEl.textContent = '';
      messageEl.style.display = 'none';
    }
  }
}

async function loadRecordingSettings() {
  try {
    var res = await fetch('/api/settings/recording');
    if (res.ok) {
      var data = await res.json();
      var autoConvertToggle = document.getElementById('autoConvertToggle');
      var keepTsToggle = document.getElementById('keepTsToggle');
      var recordingEnabledToggle = document.getElementById('recordingEnabledToggle');
      var gapBufferMinutesSelect = document.getElementById('gapBufferMinutesSelect');
      var showTsToggle = document.getElementById('showTsToggle');
      var autoDeleteToggle = document.getElementById('autoDeleteToggle');
      var autoDeleteThreshold = document.getElementById('autoDeleteThreshold');
      var thresholdRow = document.getElementById('autoDeleteThresholdRow');
      var thresholdValue = document.getElementById('thresholdValue');
      var defaultRetentionInput = document.getElementById('defaultRetentionInput');
      var segmentDurationSelect = document.getElementById('segmentDurationSelect');
      var segmentSizeInput = document.getElementById('segmentSizeInput');
      var filenameFormatSelect = document.getElementById('filenameFormatSelect');
      var checkIntervalInput = document.getElementById('checkIntervalInput');

      if (autoConvertToggle) autoConvertToggle.checked = !!data.auto_convert;
      if (keepTsToggle) keepTsToggle.checked = !!data.keep_ts;
      applyRecordingGateUi(data);
      var stagingStopGbInput = document.getElementById('stagingStopGbInput');
      if (stagingStopGbInput) {
        var stagingGb = parseInt(data.staging_stop_gb, 10);
        if (isNaN(stagingGb)) stagingGb = 5;
        stagingStopGbInput.value = String(Math.max(2, Math.min(20, stagingGb)));
      }
      var libraryStopGbInput = document.getElementById('libraryStopGbInput');
      if (libraryStopGbInput) {
        var libraryGb = parseInt(data.library_stop_gb, 10);
        if (isNaN(libraryGb)) libraryGb = 100;
        libraryStopGbInput.value = String(Math.max(50, Math.min(500, libraryGb)));
      }
      if (gapBufferMinutesSelect) {
        var gap = parseInt(data.gap_buffer_minutes, 10);
        if ([15, 30, 60, 90].indexOf(gap) < 0) gap = 30;
        gapBufferMinutesSelect.value = String(gap);
      }
      var downloadProjectConcurrencySelect = document.getElementById('downloadProjectConcurrencySelect');
      if (downloadProjectConcurrencySelect) {
        var projectConc = parseInt(data.download_project_concurrency, 10);
        if ([1, 2, 3, 4].indexOf(projectConc) < 0) projectConc = 2;
        downloadProjectConcurrencySelect.value = String(projectConc);
      }
      var downloadFileConcurrencySelect = document.getElementById('downloadFileConcurrencySelect');
      if (downloadFileConcurrencySelect) {
        var fileConc = parseInt(data.download_file_concurrency, 10);
        if ([2, 4, 6, 8, 10, 12].indexOf(fileConc) < 0) fileConc = 6;
        downloadFileConcurrencySelect.value = String(fileConc);
      }
      if (showTsToggle) showTsToggle.checked = !!data.show_ts_files;
      if (autoDeleteToggle) {
        autoDeleteToggle.checked = !!data.auto_delete_watched;
        if (thresholdRow) thresholdRow.style.display = data.auto_delete_watched ? 'flex' : 'none';
      }
      if (autoDeleteThreshold) {
        var thresh = data.auto_delete_threshold || 90;
        autoDeleteThreshold.value = thresh;
        if (thresholdValue) thresholdValue.textContent = thresh + '%';
      }
      var maxResSelect = document.getElementById('maxResolutionSelect');
      if (maxResSelect) {
        maxResSelect.value = String(data.max_resolution || 0);
      }
      var defaultResSelect = document.getElementById('defaultResolutionSelect');
      if (defaultResSelect) {
        defaultResSelect.value = String(data.default_resolution || 0);
      }
      var defaultMonthlyQuotaInput = document.getElementById('defaultMonthlyQuotaInput');
      if (defaultMonthlyQuotaInput) {
        defaultMonthlyQuotaInput.value = normalizeMonthlyQuotaGb(data.default_monthly_quota_gb, 100);
      }
      var quotaCycleDayInput = document.getElementById('quotaCycleDayInput');
      if (quotaCycleDayInput) {
        quotaCycleDayInput.value = normalizeQuotaCycleDay(data.quota_cycle_day, 13);
      }
      if (defaultRetentionInput) {
        defaultRetentionInput.value = normalizeRetentionDays(data.default_retention_days, 30);
      }
      if (segmentDurationSelect) {
        segmentDurationSelect.value = String(data.segment_duration_minutes || 0);
      }
      if (segmentSizeInput) {
        segmentSizeInput.value = normalizeSegmentSizeMb(data.segment_size_mb);
      }
      if (filenameFormatSelect) {
        filenameFormatSelect.value = data.filename_format || 'timestamp';
      }
      if (checkIntervalInput) {
        setCheckIntervalInput(data.check_interval_seconds || data.check_interval);
      }
    }
  } catch (e) {
    console.error('Error loading recording settings:', e);
  }
}

async function updateRecordingSetting(key, value) {
  try {
    var body = {};
    body[key] = value;
    var res = await fetch('/api/settings/recording', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    var data = await res.json().catch(function() { return {}; });
    if (res.ok) {
      showNotification('Setting updated', 'success');
      if (key === 'default_retention_days') {
        var defaultRetentionInput = document.getElementById('defaultRetentionInput');
        if (defaultRetentionInput) defaultRetentionInput.value = normalizeRetentionDays(value, 30);
      }
      if (key === 'segment_size_mb') {
        var segmentSizeInput = document.getElementById('segmentSizeInput');
        if (segmentSizeInput) segmentSizeInput.value = normalizeSegmentSizeMb(value);
      }
      if (key === 'check_interval_seconds') {
        setCheckIntervalInput(data.check_interval_seconds || data.check_interval || value);
      }
      if (key === 'staging_stop_gb' || key === 'library_stop_gb') {
        loadRecordingSettings();
      }
      // Toggle threshold row visibility when auto_delete_watched changes
      if (key === 'auto_delete_watched') {
        var thresholdRow = document.getElementById('autoDeleteThresholdRow');
        if (thresholdRow) thresholdRow.style.display = value ? 'flex' : 'none';
      }
      if (key === 'recording_enabled') {
        applyRecordingGateUi(data && Object.keys(data).length ? data : { recording_enabled: !!value });
        try {
          document.dispatchEvent(new CustomEvent('hxylive:recording-enabled', {
            detail: {
              enabled: !!(data.recording_enabled != null ? data.recording_enabled : value),
              gate: data.recording_gate,
              library_space_blocked: !!data.library_space_blocked,
              staging_blocked: !!data.staging_blocked
            }
          }));
        } catch (e) {}
        loadRecordingSettings();
      }
    } else {
      showNotification(data.detail || 'Failed to update setting', 'error');
      loadRecordingSettings();
    }
  } catch (e) {
    console.error('Error updating recording setting:', e);
    showNotification('Connection error', 'error');
    loadRecordingSettings();
  }
}

async function applyDefaultRetentionToModels() {
  var input = document.getElementById('defaultRetentionInput');
  var retentionDays = normalizeRetentionDays(input ? input.value : 30, 30);
  try {
    var res = await fetch('/api/settings/recording', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        default_retention_days: retentionDays,
        apply_default_retention_to_models: true
      })
    });
    var data = await res.json().catch(function() { return {}; });
    if (res.ok) {
      if (input) input.value = normalizeRetentionDays(data.default_retention_days, retentionDays);
      showNotification('Retention applied to ' + (data.applied_retention_models || 0) + ' models', 'success');
    } else {
      showNotification(data.detail || 'Failed to apply retention', 'error');
      loadRecordingSettings();
    }
  } catch (e) {
    console.error('Error applying default retention:', e);
    showNotification('Connection error', 'error');
    loadRecordingSettings();
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
// Tag Blacklist Management
// ============================================

let blacklistedTags = [];

async function loadBlacklistedTags() {
  try {
    var res = await fetch('/api/settings/blacklisted-tags');
    if (res.ok) {
      var data = await res.json();
      blacklistedTags = data.tags || [];
      renderBlacklistedTags();
    }
  } catch (e) {
    console.error('Error loading blacklisted tags:', e);
  }
}

function renderBlacklistedTags() {
  var container = document.getElementById('blacklistedTagsList');
  if (!container) return;

  if (blacklistedTags.length === 0) {
    container.replaceChildren();
    var empty = document.createElement('span');
    empty.style.cssText = 'font-size: 0.85rem; color: var(--text-muted);';
    empty.textContent = 'No blacklisted tags yet.';
    container.appendChild(empty);
    return;
  }

  container.replaceChildren();
  blacklistedTags.forEach(function(tag) {
    var chip = document.createElement('span');
    chip.style.cssText = 'display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.3rem 0.7rem; border-radius: 6px; background: rgba(239, 68, 68, 0.15); color: #f87171; font-size: 0.85rem; font-weight: 500;';
    chip.appendChild(document.createTextNode(String(tag)));

    var removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.style.cssText = 'background: none; border: none; color: #f87171; cursor: pointer; font-size: 1.1rem; padding: 0; line-height: 1;';
    removeButton.textContent = '\u00d7';
    removeButton.setAttribute('aria-label', 'Remove ' + String(tag));
    removeButton.addEventListener('click', function() {
      removeBlacklistedTag(tag);
    });
    chip.appendChild(removeButton);
    container.appendChild(chip);
  });
}

async function addBlacklistedTag() {
  var input = document.getElementById('blacklistInput');
  var tag = input.value.trim().toLowerCase();
  if (!tag) return;
  if (blacklistedTags.indexOf(tag) !== -1) {
    showNotification('Tag already blacklisted', 'error');
    return;
  }

  blacklistedTags.push(tag);
  input.value = '';

  try {
    var res = await fetch('/api/settings/blacklisted-tags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags: blacklistedTags })
    });
    if (res.ok) {
      renderBlacklistedTags();
      showNotification('Tag "' + tag + '" blacklisted', 'success');
    } else {
      blacklistedTags.pop();
      showNotification('Failed to save', 'error');
    }
  } catch (e) {
    blacklistedTags.pop();
    showNotification('Connection error', 'error');
  }
}

async function removeBlacklistedTag(tag) {
  blacklistedTags = blacklistedTags.filter(function(t) { return t !== tag; });

  try {
    var res = await fetch('/api/settings/blacklisted-tags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags: blacklistedTags })
    });
    if (res.ok) {
      renderBlacklistedTags();
      showNotification('Tag removed', 'success');
    }
  } catch (e) {
    showNotification('Connection error', 'error');
  }
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
// System Statistics
// ============================================

function formatBytes(bytes) {
  if (bytes === 0 || bytes == null) return '0 B';
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var i = Math.floor(Math.log(bytes) / Math.log(1024));
  if (i >= units.length) i = units.length - 1;
  return (bytes / Math.pow(1024, i)).toFixed(i > 1 ? 1 : 0) + ' ' + units[i];
}

function formatNumber(num) {
  if (num == null) return '-';
  return num.toLocaleString();
}

function formatPercent(value) {
  if (value == null || isNaN(value)) return '-';
  var fixed = Math.abs(value % 1) > 0 ? value.toFixed(1) : value.toFixed(0);
  return fixed + '%';
}

function setText(id, value) {
  var el = document.getElementById(id);
  if (el) el.textContent = value;
}

function formatUptime(seconds) {
  if (!seconds) return '-';
  var d = Math.floor(seconds / 86400);
  var h = Math.floor((seconds % 86400) / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  var parts = [];
  if (d > 0) parts.push(d + 'd');
  if (h > 0) parts.push(h + 'h');
  parts.push(m + 'm');
  return parts.join(' ');
}

function setGauge(id, percent, color) {
  var el = document.getElementById(id);
  if (!el) return;
  var circumference = 2 * Math.PI * 50; // r=50
  var offset = circumference - (percent / 100) * circumference;
  el.style.strokeDasharray = circumference;
  el.style.strokeDashoffset = offset;
  if (color) el.style.stroke = color;
}

function getGaugeColor(percent) {
  if (percent < 50) return '#10b981';
  if (percent < 75) return '#f59e0b';
  return '#ef4444';
}

async function loadSystemStats() {
  try {
    var res = await fetch('/api/system/stats');
    if (!res.ok) {
      console.error('Failed to load stats:', res.status);
      return;
    }
    var data = await res.json();
    renderStats(data);
  } catch (e) {
    console.error('Error loading system stats:', e);
  }
}

function renderStats(data) {
  var updatedAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  setText('statsUpdatedAt', 'Updated ' + updatedAt);
  setText('statsSummaryDisk', formatPercent(data.disk.percent));
  setText('statsSummaryDiskSub', formatBytes(data.disk.free) + ' free');
  setText('statsSummaryCpu', formatPercent(data.cpu.usage_percent));
  setText('statsSummaryCpuSub', (data.cpu.cores_logical || 0) + ' logical cores');
  setText('statsSummaryRam', formatPercent(data.ram.percent));
  setText('statsSummaryRamSub', formatBytes(data.ram.available) + ' available');
  setText('statsSummaryRec', data.sessions.active_count);
  setText('statsSummaryRecSub', 'active now');

  // --- System Overview ---
  var el;
  el = document.getElementById('stat-uptime');
  if (el) el.textContent = formatUptime(data.process.uptime_seconds);
  el = document.getElementById('stat-pid');
  if (el) el.textContent = data.process.pid;
  el = document.getElementById('stat-threads');
  if (el) el.textContent = data.process.threads;
  el = document.getElementById('stat-active-rec');
  if (el) el.textContent = data.sessions.active_count;

  // --- Disk ---
  var diskPct = data.disk.percent;
  setGauge('disk-gauge', diskPct, getGaugeColor(diskPct));
  el = document.getElementById('disk-gauge-text');
  if (el) el.textContent = formatPercent(diskPct);
  el = document.getElementById('disk-gauge-sub');
  if (el) el.textContent = formatBytes(data.disk.free) + ' free';
  el = document.getElementById('stat-disk-total');
  if (el) el.textContent = formatBytes(data.disk.total);
  el = document.getElementById('stat-disk-used');
  if (el) el.textContent = formatBytes(data.disk.used);
  el = document.getElementById('stat-disk-free');
  if (el) el.textContent = formatBytes(data.disk.free);

  // --- CPU ---
  var cpuPct = data.cpu.usage_percent;
  setGauge('cpu-gauge', cpuPct, getGaugeColor(cpuPct));
  el = document.getElementById('cpu-gauge-text');
  if (el) el.textContent = formatPercent(cpuPct);
  el = document.getElementById('cpu-gauge-sub');
  if (el) el.textContent = (data.cpu.cores_logical || 0) + ' cores';
  el = document.getElementById('stat-cpu-physical');
  if (el) el.textContent = data.cpu.cores_physical || '-';
  el = document.getElementById('stat-cpu-logical');
  if (el) el.textContent = data.cpu.cores_logical || '-';
  el = document.getElementById('stat-cpu-freq');
  if (el) {
    if (data.cpu.frequency && data.cpu.frequency.current) {
      el.textContent = Math.round(data.cpu.frequency.current) + ' MHz';
    } else {
      el.textContent = '-';
    }
  }

  // CPU cores visualization
  var coresEl = document.getElementById('stat-cpu-cores');
  if (coresEl && data.cpu.per_core) {
    coresEl.innerHTML = data.cpu.per_core.map(function(pct, i) {
      var bg = getGaugeColor(pct);
      var alpha = Math.max(0.15, pct / 100);
      return '<div class="stats-core" style="background: ' + bg + '; opacity: ' + (0.3 + alpha * 0.7).toFixed(2) + ';" title="Core ' + i + ': ' + pct.toFixed(0) + '%">' + pct.toFixed(0) + '</div>';
    }).join('');
  }

  // --- RAM ---
  var ramPct = data.ram.percent;
  setGauge('ram-gauge', ramPct, getGaugeColor(ramPct));
  el = document.getElementById('ram-gauge-text');
  if (el) el.textContent = formatPercent(ramPct);
  el = document.getElementById('ram-gauge-sub');
  if (el) el.textContent = formatBytes(data.ram.used) + ' used';
  el = document.getElementById('stat-ram-total');
  if (el) el.textContent = formatBytes(data.ram.total);
  el = document.getElementById('stat-ram-used');
  if (el) el.textContent = formatBytes(data.ram.used);
  el = document.getElementById('stat-ram-available');
  if (el) el.textContent = formatBytes(data.ram.available);

  // --- Storage Breakdown ---
  var storage = data.storage;
  var totalStorage = storage.ts_files.size + storage.mp4_files.size + storage.thumbnails.size + storage.other_files.size;

  // Storage bar
  var barEl = document.getElementById('storage-bar');
  if (barEl && totalStorage > 0) {
    var segments = [
      { size: storage.ts_files.size, color: '#6366f1', label: 'TS' },
      { size: storage.mp4_files.size, color: '#10b981', label: 'MP4' },
      { size: storage.thumbnails.size, color: '#f59e0b', label: 'Thumbs' },
      { size: storage.other_files.size, color: '#6b7280', label: 'Other' },
    ];
    barEl.innerHTML = segments.map(function(s) {
      var pct = (s.size / totalStorage * 100);
      if (pct < 0.5) return '';
      return '<div class="stats-storage-segment" style="width: ' + pct.toFixed(1) + '%; background: ' + s.color + ';" title="' + s.label + ': ' + formatBytes(s.size) + '"></div>';
    }).join('');
  } else if (barEl) {
    barEl.innerHTML = '<div style="height:100%;width:100%;display:flex;align-items:center;justify-content:center;font-size:0.75rem;color:var(--text-muted);">No data</div>';
  }

  el = document.getElementById('stat-ts-info');
  if (el) el.textContent = formatBytes(storage.ts_files.size) + ' (' + storage.ts_files.count + ' files)';
  el = document.getElementById('stat-mp4-info');
  if (el) el.textContent = formatBytes(storage.mp4_files.size) + ' (' + storage.mp4_files.count + ' files)';
  el = document.getElementById('stat-thumb-info');
  if (el) el.textContent = formatBytes(storage.thumbnails.size) + ' (' + storage.thumbnails.count + ' files)';
  el = document.getElementById('stat-other-info');
  if (el) el.textContent = formatBytes(storage.other_files.size) + ' (' + storage.other_files.count + ' files)';
  el = document.getElementById('stat-total-rec-size');
  if (el) el.textContent = formatBytes(storage.total_recordings_size);

  // --- Process Resources ---
  el = document.getElementById('stat-proc-cpu');
  if (el) el.textContent = data.process.cpu_percent.toFixed(1) + '%';
  el = document.getElementById('stat-proc-mem');
  if (el) el.textContent = formatBytes(data.process.memory_rss);
  el = document.getElementById('stat-proc-vms');
  if (el) el.textContent = formatBytes(data.process.memory_vms);
  el = document.getElementById('stat-proc-files');
  if (el) el.textContent = data.process.open_files;
  el = document.getElementById('stat-proc-conn');
  if (el) el.textContent = data.process.connections;

  // --- Network I/O ---
  el = document.getElementById('stat-net-recv');
  if (el) el.textContent = formatBytes(data.network.bytes_recv);
  el = document.getElementById('stat-net-sent');
  if (el) el.textContent = formatBytes(data.network.bytes_sent);
  el = document.getElementById('stat-net-pin');
  if (el) el.textContent = formatNumber(data.network.packets_recv);
  el = document.getElementById('stat-net-pout');
  if (el) el.textContent = formatNumber(data.network.packets_sent);

  // --- Child Processes ---
  var childrenEl = document.getElementById('stats-children-list');
  if (childrenEl) {
    if (data.children.length === 0) {
      childrenEl.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem 0;">No active child processes (ffmpeg, etc.)</p>';
    } else {
      childrenEl.innerHTML = data.children.map(function(c) {
        return '<div class="stats-child-item">' +
          '<span class="stats-child-name" title="' + escapeHtml(c.cmdline) + '">' + escapeHtml(c.name) + ' <span style="color:var(--text-muted);font-size:0.75rem;">PID ' + c.pid + '</span></span>' +
          '<div class="stats-child-meta">' +
            '<span title="CPU">' + c.cpu_percent.toFixed(1) + '% CPU</span>' +
            '<span title="Memory">' + formatBytes(c.memory_rss) + '</span>' +
            '<span style="color:' + (c.status === 'running' ? 'var(--success)' : 'var(--text-muted)') + ';">' + c.status + '</span>' +
          '</div>' +
        '</div>';
      }).join('');
    }
  }

  // --- Top Models by Storage ---
  var topModelsEl = document.getElementById('stats-top-models');
  if (topModelsEl) {
    var models = storage.by_model || [];
    if (models.length === 0) {
      topModelsEl.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem 0;">No recording data yet</p>';
    } else {
      var maxSize = models[0].total_size || 1;
      topModelsEl.innerHTML = models.map(function(m, i) {
        var barPct = (m.total_size / maxSize * 100).toFixed(1);
        return '<div class="stats-model-item">' +
          '<span class="stats-model-rank">' + (i + 1) + '</span>' +
          '<div class="stats-model-info">' +
            '<div class="stats-model-name">' + escapeHtml(m.username) + '</div>' +
            '<div class="stats-model-detail">' + m.ts_count + ' TS, ' + m.mp4_count + ' MP4</div>' +
          '</div>' +
          '<div class="stats-model-bar-bg"><div class="stats-model-bar-fill" style="width:' + barPct + '%;"></div></div>' +
          '<span class="stats-model-size">' + formatBytes(m.total_size) + '</span>' +
        '</div>';
      }).join('');
    }
  }
}

// ============================================
// Tests Center
// ============================================

var testStates = {};

async function fetchJsonNoCache(url) {
  var res = await fetch(url, {
    cache: 'no-store',
    headers: { 'Accept': 'application/json' }
  });
  var text = await res.text();
  var data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      if (res.ok) throw new Error('Invalid JSON from ' + url);
    }
  }
  if (!res.ok) {
    var detail = data && (data.detail || data.error || data.message);
    throw new Error('HTTP ' + res.status + ' on ' + url + (detail ? ': ' + detail : ''));
  }
  return data;
}

async function fetchStatusNoCache(url) {
  var res = await fetch(url, { cache: 'no-store' });
  return { ok: res.ok, status: res.status, url: url };
}

function assertTest(condition, message) {
  if (!condition) throw new Error(message);
}

function testArray(value, label) {
  assertTest(Array.isArray(value), label + ' must be an array');
  return value;
}

function formatTestDuration(ms) {
  if (!Number.isFinite(ms)) return '';
  if (ms < 1000) return Math.round(ms) + 'ms';
  return (ms / 1000).toFixed(1) + 's';
}

function withTestDuration(detail, startedAt) {
  var elapsed = (typeof performance !== 'undefined' && performance.now)
    ? performance.now() - startedAt
    : NaN;
  var duration = formatTestDuration(elapsed);
  return duration ? (detail || '-') + ' - ' + duration : (detail || '-');
}

function testResult(state, detail) {
  return { state: state, detail: detail || '-' };
}

var testDefinitions = [
  {
    id: 'api',
    name: 'API',
    run: async function() {
      var data = await fetchJsonNoCache('/api/version');
      assertTest(data.version, 'Version is missing');
      return testResult('pass', 'v' + (data.version || 'unknown') + ' - ' + (data.output_dir || 'output dir unknown'));
    }
  },
  {
    id: 'routes',
    name: 'App routes',
    run: async function() {
      var root = await fetchStatusNoCache('/');
      var discover = await fetchStatusNoCache('/discover');
      var settings = await fetchStatusNoCache('/settings');
      var dashboard = await fetchStatusNoCache('/dashboard');
      assertTest(root.status === 200, 'Root returned HTTP ' + root.status);
      assertTest(discover.status === 200, 'Discover returned HTTP ' + discover.status);
      assertTest(settings.status === 200, 'Settings returned HTTP ' + settings.status);
      assertTest(dashboard.status === 404, 'Legacy dashboard route still exists');
      return testResult('pass', 'Root, Discover and Settings OK - legacy dashboard removed');
    }
  },
  {
    id: 'providers',
    name: 'Providers status',
    run: async function() {
      var data = await fetchJsonNoCache('/api/providers');
      var providers = testArray(data.providers, 'providers');
      var sourceTypes = providers.map(function(provider) { return provider.sourceType; });
      var required = ['chaturbate', 'twitch', 'bilibili', 'stripchat', 'youtube'];
      var removed = ['streamate', 'flirt4free', 'cam4', 'bongacams', 'myfreecams', 'livejasmin', 'camsoda', 'cams', 'xcams', 'porn'];
      required.forEach(function(source) {
        assertTest(sourceTypes.indexOf(source) !== -1, 'Missing provider ' + source);
      });
      removed.forEach(function(source) {
        assertTest(sourceTypes.indexOf(source) === -1, 'Removed provider still registered: ' + source);
      });
      providers.forEach(function(provider) {
        assertTest(provider.status && typeof provider.status === 'object', 'Missing status for ' + provider.sourceType);
      });
      var discoverable = providers.filter(function(provider) {
        return provider.capabilities && provider.capabilities.can_discover;
      }).length;
      var localFollow = providers.filter(function(provider) {
        return provider.capabilities && provider.capabilities.can_follow;
      }).length;
      var accountLogin = providers.filter(function(provider) {
        return provider.capabilities && provider.capabilities.can_login;
      }).length;
      var remoteSync = providers.filter(function(provider) {
        return provider.capabilities && provider.capabilities.can_sync_following;
      }).length;
      assertTest(accountLogin === 5, 'All five providers should expose account login');
      assertTest(remoteSync === 5, 'All five providers should expose remote sync');
      return testResult('pass', providers.length + ' providers - ' + discoverable + ' discoverable - ' + localFollow + ' follow - ' + remoteSync + ' sync');
    }
  },
  {
    id: 'system',
    name: 'System stats',
    run: async function() {
      var data = await fetchJsonNoCache('/api/system/stats');
      var disk = data.disk || {};
      var cpu = data.cpu || {};
      assertTest(data.disk && typeof disk === 'object', 'Disk stats missing');
      assertTest(data.cpu && typeof cpu === 'object', 'CPU stats missing');
      return testResult('pass', 'Disk ' + (disk.percent != null ? disk.percent + '%' : '-') + ' - CPU ' + (cpu.usage_percent != null ? cpu.usage_percent + '%' : '-'));
    }
  },
  {
    id: 'recording',
    name: 'Recording settings',
    run: async function() {
      var data = await fetchJsonNoCache('/api/settings/recording');
      assertTest(typeof data.auto_convert === 'boolean', 'auto_convert setting missing');
      assertTest(typeof data.keep_ts === 'boolean', 'keep_ts setting missing');
      assertTest(data.mac_auto_sync === false, 'mac_auto_sync must stay false (manual downloads only)');
      assertTest(typeof data.recording_enabled === 'boolean', 'recording_enabled setting missing');
      assertTest(typeof data.default_monthly_quota_gb === 'number', 'default_monthly_quota_gb setting missing');
      assertTest(typeof data.quota_cycle_day === 'number', 'quota_cycle_day setting missing');
      assertTest(typeof data.check_interval_seconds === 'number', 'check_interval_seconds setting missing');
      var maxRes = data.max_resolution ? data.max_resolution + 'p max' : 'best max';
      var defaultRes = data.default_resolution ? data.default_resolution + 'p default' : 'best default';
      var interval = data.check_interval_seconds + 's checks';
      var duration = data.segment_duration_minutes ? data.segment_duration_minutes + 'm parts' : 'duration off';
      var size = data.segment_size_mb ? data.segment_size_mb + 'MB parts' : 'size off';
      var record = data.recording_enabled ? 'autorecord on' : 'autorecord off';
      var quota = data.default_monthly_quota_gb + 'GB / day ' + data.quota_cycle_day;
      return testResult('pass', 'Auto convert ' + (data.auto_convert ? 'on' : 'off') + ' - ' + record + ' - ' + interval + ' - ' + defaultRes + ' - ' + maxRes + ' - ' + quota + ' - ' + duration + ' - ' + size);
    }
  },
  {
    id: 'following',
    name: 'Following cache',
    run: async function() {
      var data = await fetchJsonNoCache('/api/following');
      var models = testArray(data.models, 'models');
      var onlineCount = Number(data.onlineCount || 0);
      var offlineCount = Number(data.offlineCount || 0);
      assertTest(onlineCount + offlineCount === models.length, 'Online/offline counts do not match model count');
      assertTest(data.perSource && typeof data.perSource === 'object', 'Provider login map missing');
      return testResult('pass', models.length + ' follows - ' + onlineCount + ' online - ' + Object.keys(data.perSource).length + ' provider sessions');
    }
  },
  {
    id: 'processes',
    name: 'FFmpeg processes',
    run: async function() {
      var data = await fetchJsonNoCache('/api/processes');
      var totals = data.totals || {};
      assertTest(Array.isArray(data.processes), 'Process list missing');
      assertTest(Number.isFinite(Number(totals.total || 0)), 'Process totals missing');
      return testResult('pass', (totals.active || 0) + ' active / ' + (totals.total || 0) + ' total');
    }
  },
  {
    id: 'flaresolverr',
    name: 'FlareSolverr',
    run: async function() {
      var data = await fetchJsonNoCache('/api/chaturbate/status');
      if (data.flaresolverrAvailable) {
        return testResult('pass', data.flaresolverrVersion ? 'Healthy - ' + data.flaresolverrVersion : 'Healthy');
      }
      return testResult('warn', data.flaresolverrMessage || 'Not available');
    }
  },
  {
    id: 'recordings',
    name: 'Recordings index',
    run: async function() {
      var data = await fetchJsonNoCache('/api/all-recordings?limit=1');
      assertTest(Array.isArray(data.recordings), 'Recordings list missing');
      return testResult('pass', (data.total || 0) + ' recordings - ' + (data.totalSizeFormatted || '0 B'));
    }
  },
  {
    id: 'media-imports',
    name: 'Media imports',
    run: async function() {
      var data = await fetchJsonNoCache('/api/media-imports/status');
      assertTest(typeof data.enabled === 'boolean', 'Media import enabled flag missing');
      assertTest(typeof data.running === 'boolean', 'Media import running flag missing');
      return testResult('pass', data.enabled ? (data.running ? 'Enabled - scanning' : 'Enabled - idle') : 'Disabled');
    }
  },
  {
    id: 'blacklist',
    name: 'Blacklist settings',
    run: async function() {
      var data = await fetchJsonNoCache('/api/settings/blacklisted-tags');
      var tags = testArray(data.tags, 'tags');
      return testResult('pass', tags.length + ' blocked tags');
    }
  }
];

function testStatusClass(state) {
  if (state === 'pass') return 'connected';
  if (state === 'fail') return 'disconnected';
  return 'unknown';
}

function testStatusText(state) {
  if (state === 'pass') return 'Passed';
  if (state === 'warn') return 'Warning';
  if (state === 'fail') return 'Failed';
  if (state === 'running') return 'Running';
  return 'Not run';
}

function renderTestsList() {
  var list = document.getElementById('testsList');
  if (!list) return;

  list.innerHTML = testDefinitions.map(function(test) {
    var state = testStates[test.id] || { state: 'not-run', detail: '-' };
    return '<div class="test-row" id="test-row-' + test.id + '">' +
      '<div>' +
        '<div class="test-name">' + escapeHtml(test.name) + '</div>' +
        '<div class="test-detail" id="test-detail-' + test.id + '">' + escapeHtml(state.detail) + '</div>' +
      '</div>' +
      '<span class="status-indicator ' + testStatusClass(state.state) + '" id="test-status-' + test.id + '">' + testStatusText(state.state) + '</span>' +
      '<button class="btn-secondary test-run-btn" id="test-run-' + test.id + '" onclick="runSingleTestById(\'' + test.id + '\')">Run</button>' +
    '</div>';
  }).join('');
}

function setTestState(id, state, detail) {
  testStates[id] = { state: state, detail: detail || '-' };

  var statusEl = document.getElementById('test-status-' + id);
  var detailEl = document.getElementById('test-detail-' + id);
  var runBtn = document.getElementById('test-run-' + id);
  if (statusEl) {
    statusEl.className = 'status-indicator ' + testStatusClass(state);
    statusEl.textContent = testStatusText(state);
  }
  if (detailEl) {
    detailEl.textContent = detail || '-';
  }
  if (runBtn) {
    runBtn.disabled = state === 'running';
    runBtn.textContent = state === 'running' ? 'Running' : 'Run';
  }
}

function updateTestsSummary() {
  var passed = 0;
  var warned = 0;
  var failed = 0;
  var finished = 0;

  testDefinitions.forEach(function(test) {
    var state = (testStates[test.id] || {}).state;
    if (state === 'pass') passed++;
    if (state === 'warn') warned++;
    if (state === 'fail') failed++;
    if (state === 'pass' || state === 'warn' || state === 'fail') finished++;
  });

  var summaryEl = document.getElementById('testsSummary');
  var overallEl = document.getElementById('testsOverallStatus');
  if (summaryEl) {
    summaryEl.textContent = finished + '/' + testDefinitions.length + ' complete - ' + passed + ' passed - ' + warned + ' warnings - ' + failed + ' failed';
  }
  if (overallEl) {
    var overallState = failed > 0 ? 'fail' : (warned > 0 ? 'warn' : (finished === testDefinitions.length ? 'pass' : 'running'));
    overallEl.className = 'status-indicator ' + testStatusClass(overallState);
    overallEl.textContent = testStatusText(overallState);
  }
}

async function runSingleTest(test) {
  setTestState(test.id, 'running', 'Running...');
  updateTestsSummary();
  var startedAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : NaN;
  try {
    var result = await test.run();
    setTestState(test.id, result.state || 'pass', withTestDuration(result.detail || '-', startedAt));
  } catch (e) {
    setTestState(test.id, 'fail', withTestDuration(e.message || 'Failed', startedAt));
  }
  updateTestsSummary();
}

async function runSingleTestById(id) {
  var test = testDefinitions.find(function(item) { return item.id === id; });
  if (!test) return;
  await runSingleTest(test);
}

async function runAllTests() {
  var btn = document.getElementById('runAllTestsBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Running...';
  }

  renderTestsList();
  testDefinitions.forEach(function(test) {
    setTestState(test.id, 'running', 'Queued...');
  });
  updateTestsSummary();

  for (var i = 0; i < testDefinitions.length; i++) {
    await runSingleTest(testDefinitions[i]);
  }

  if (btn) {
    btn.disabled = false;
    btn.textContent = 'Run all tests';
  }
}

// ============================================
// Initialization
// ============================================
window.addEventListener('DOMContentLoaded', function() {
  // Add animation keyframes
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }';
  document.head.appendChild(style);

  // Initialize tab navigation
  initTabs();

  // Load all data in parallel
  loadProviders();
  loadFlareSolverrSettings();
  checkFlareSolverr();
  loadAppInfo();
  loadBlacklistedTags();
  loadRecordingSettings();

  // Set up blacklist input Enter key
  var blacklistInput = document.getElementById('blacklistInput');
  if (blacklistInput) {
    blacklistInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') addBlacklistedTag();
    });
  }

  // Auto-refresh stats every 30 seconds when on stats tab
  setInterval(function() {
    var statsTab = document.getElementById('tab-statistics');
    if (statsTab && statsTab.classList.contains('active')) {
      loadSystemStats();
    }
  }, 30000);
});

// ============================================
// Logs Viewer
// ============================================

function escapeLogHtml(str) {
  var div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

async function loadLogs() {
  logsOffset = 0;
  var level = document.getElementById('logLevelFilter').value;
  try {
    var url = '/api/logs?limit=200&offset=0';
    if (level) url += '&level=' + level;
    var res = await fetch(url);
    if (!res.ok) return;
    var data = await res.json();
    renderLogs(data.logs, false);
    document.getElementById('logTotal').textContent = data.total;
    logsOffset = data.logs.length;

    // Load error/warning counts
    var errRes = await fetch('/api/logs?level=ERROR&limit=1&offset=0');
    var warnRes = await fetch('/api/logs?level=WARNING&limit=1&offset=0');
    if (errRes.ok) {
      var errData = await errRes.json();
      document.getElementById('logErrors').textContent = errData.total;
    }
    if (warnRes.ok) {
      var warnData = await warnRes.json();
      document.getElementById('logWarnings').textContent = warnData.total;
    }
  } catch (e) {
    console.error('Error loading logs:', e);
  }
}

async function loadMoreLogs() {
  var level = document.getElementById('logLevelFilter').value;
  try {
    var url = '/api/logs?limit=200&offset=' + logsOffset;
    if (level) url += '&level=' + level;
    var res = await fetch(url);
    if (!res.ok) return;
    var data = await res.json();
    if (data.logs.length === 0) {
      document.getElementById('loadMoreLogs').textContent = 'No more logs';
      return;
    }
    renderLogs(data.logs, true);
    logsOffset += data.logs.length;
  } catch (e) {
    console.error('Error loading more logs:', e);
  }
}

function renderLogs(logs, append) {
  var container = document.getElementById('logsContainer');
  if (!append) container.innerHTML = '';

  if (logs.length === 0 && !append) {
    container.innerHTML = '<p style="color: var(--text-muted); padding: 2rem; text-align: center;">No logs found</p>';
    return;
  }

  var html = logs.map(function(log) {
    var time = log.timestamp.split(' ')[1] || log.timestamp;
    return '<div class="log-entry">' +
      '<span class="log-timestamp">' + escapeLogHtml(time) + '</span>' +
      '<span class="log-level log-level-' + log.level + '">' + log.level + '</span>' +
      '<span class="log-module">' + escapeLogHtml(log.module) + '</span>' +
      '<span class="log-message">' + escapeLogHtml(log.message) + '</span>' +
    '</div>';
  }).join('');

  container.insertAdjacentHTML('beforeend', html);
  document.getElementById('loadMoreLogs').textContent = 'Load More';
}

// ============================================
// Processes tab — live ffmpeg process inspector
// ============================================
var processesPollInterval = null;
var processesExpanded = {};   // session_id -> expanded?
var processesLastSnap = {};   // session_id -> last snapshot (for partial updates)
var PROCESSES_REFRESH_MS = 10000;

function startProcessesPolling() {
  if (processesPollInterval) return;
  refreshProcesses();
  processesPollInterval = setInterval(refreshProcesses, PROCESSES_REFRESH_MS);
}

function stopProcessesPolling() {
  if (processesPollInterval) {
    clearInterval(processesPollInterval);
    processesPollInterval = null;
  }
}

function fmtBytes(n) {
  if (n == null) return '-';
  var u = ['B', 'KB', 'MB', 'GB', 'TB'];
  var i = 0;
  var v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(i === 0 ? 0 : 1) + ' ' + u[i];
}

function fmtDuration(secs) {
  if (secs == null) return '-';
  secs = Math.max(0, Math.floor(secs));
  var h = Math.floor(secs / 3600);
  var m = Math.floor((secs % 3600) / 60);
  var s = secs % 60;
  if (h > 0) return h + 'h ' + (m < 10 ? '0' : '') + m + 'm';
  if (m > 0) return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
  return s + 's';
}

function escapeProcHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function statusDotClass(p) {
  if (!p.running || !p.pid) return 'stopped';
  if (p.status === 'Z') return 'zombie';
  if (p.seconds_since_progress != null && p.seconds_since_progress > 120) return 'zombie';
  return '';
}

async function refreshProcesses() {
  var tbody = document.getElementById('proc-tbody');
  if (!tbody) return;
  try {
    var res = await fetch('/api/processes');
    if (!res.ok) {
      tbody.innerHTML = '<tr><td colspan="10" class="proc-empty">Failed to load (' + res.status + ')</td></tr>';
      return;
    }
    var data = await res.json();
    renderProcesses(data);
  } catch (e) {
    console.error('refreshProcesses:', e);
    tbody.innerHTML = '<tr><td colspan="10" class="proc-empty">Connection error</td></tr>';
  }
}

function renderProcesses(data) {
  var procs = (data && data.processes) || [];
  var totals = (data && data.totals) || {};
  var tbody = document.getElementById('proc-tbody');
  var totalsEl = document.getElementById('proc-totals');
  var badgeEl = document.getElementById('processesCountBadge');

  if (badgeEl) {
    badgeEl.textContent = totals.active || 0;
    if (totals.active > 0) badgeEl.classList.add('active');
    else badgeEl.classList.remove('active');
  }

  if (totalsEl) {
    totalsEl.textContent = (totals.active || 0) + ' active / ' + (totals.total || 0) +
      ' · CPU ' + (totals.cpu_percent_sum != null ? totals.cpu_percent_sum.toFixed(1) : '?') + '%' +
      ' · RAM ' + fmtBytes(totals.rss_bytes_sum || 0) +
      ' · ' + (totals.host_cores || '?') + ' cores';
  }

  if (procs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="proc-empty">No active ffmpeg processes</td></tr>';
    processesLastSnap = {};
    return;
  }

  var rows = procs.map(function(p) {
    processesLastSnap[p.session_id] = p;
    var dot = '<span class="proc-status-dot ' + statusDotClass(p) + '" title="' + escapeProcHtml(p.status || '?') + '"></span>';
    var actions =
      '<button class="proc-action-btn"        onclick="event.stopPropagation(); procStop(\'' + escapeInlineJs(p.session_id) + '\')"    title="Stop (graceful)">&#9209;</button>' +
      '<button class="proc-action-btn"        onclick="event.stopPropagation(); procRestart(\'' + escapeInlineJs(p.session_id) + '\')" title="Restart (stop + monitor re-spawns)">&#8635;</button>' +
      '<button class="proc-action-btn danger" onclick="event.stopPropagation(); procKill(\'' + escapeInlineJs(p.session_id) + '\')"    title="Force kill (SIGKILL)">&#9760;</button>' +
      '<button class="proc-action-btn"        onclick="event.stopPropagation(); procPreview(\'' + escapeInlineJs(p.playback_url || '') + '\')" title="Open HLS preview">&#9654;</button>';

    var row = '<tr class="proc-row" data-sid="' + escapeProcHtml(p.session_id) + '" onclick="toggleProcRow(\'' + escapeInlineJs(p.session_id) + '\')">' +
      '<td>' + dot + '</td>' +
      '<td>' + (p.pid != null ? p.pid : '-') + '</td>' +
      '<td>' + escapeProcHtml(p.person || p.name || '-') + '</td>' +
      '<td>' + fmtDuration(p.uptime_seconds) + '</td>' +
      '<td>' + (p.cpu_percent != null ? p.cpu_percent.toFixed(1) : '-') + '</td>' +
      '<td>' + fmtBytes(p.rss_bytes) + '</td>' +
      '<td>' + escapeProcHtml(p.quality || '-') + '</td>' +
      '<td>' + fmtBytes(p.record_size_bytes) + '</td>' +
      '<td>' + (p.segment_count != null ? p.segment_count : '-') + '</td>' +
      '<td class="proc-actions">' + actions + '</td>' +
    '</tr>';
    var detail = '';
    if (processesExpanded[p.session_id]) {
      detail = '<tr class="proc-detail-row" data-sid="' + escapeProcHtml(p.session_id) + '">' +
                 '<td colspan="10" style="padding:0;"><div class="proc-detail" id="proc-detail-' + escapeProcHtml(p.session_id) + '">' +
                   renderProcDetail(p) +
                 '</div></td>' +
               '</tr>';
    }
    return row + detail;
  });

  tbody.innerHTML = rows.join('');

  // Refresh log tail for each currently-expanded row (fire-and-forget)
  Object.keys(processesExpanded).forEach(function(sid) {
    if (processesExpanded[sid]) loadProcessLog(sid);
  });
}

function renderProcDetail(p) {
  var input = p.input_url || '-';
  var inputShort = input.length > 80 ? input.slice(0, 80) + '…' : input;
  var lastData = p.seconds_since_progress != null ? fmtDuration(p.seconds_since_progress) + ' ago' : '-';
  var io = '';
  if (p.io_read_bytes != null || p.io_write_bytes != null) {
    io = '<div class="kv"><span class="k">IO read</span><span class="v">' + fmtBytes(p.io_read_bytes) + '</span></div>' +
         '<div class="kv"><span class="k">IO write</span><span class="v">' + fmtBytes(p.io_write_bytes) + '</span></div>';
  }
  return '' +
    '<h4>Process</h4>' +
    '<div class="kv"><span class="k">Session</span><span class="v">' + escapeProcHtml(p.session_id) + '</span></div>' +
    '<div class="kv"><span class="k">PID</span><span class="v">' + (p.pid != null ? p.pid : '-') + '</span></div>' +
    '<div class="kv"><span class="k">State</span><span class="v">' + escapeProcHtml(p.status || '?') + '</span></div>' +
    '<div class="kv"><span class="k">Started</span><span class="v">' + escapeProcHtml(p.start_date || '-') + '</span></div>' +
    '<div class="kv"><span class="k">Uptime</span><span class="v">' + fmtDuration(p.uptime_seconds) + '</span></div>' +
    '<div class="kv"><span class="k">Threads</span><span class="v">' + (p.num_threads != null ? p.num_threads : '-') + '</span></div>' +
    '<div class="kv"><span class="k">FDs</span><span class="v">' + (p.num_fds != null ? p.num_fds : '-') + '</span></div>' +
    '<div class="kv"><span class="k">Nice</span><span class="v">' + (p.nice != null ? p.nice : '-') + '</span></div>' +
    '<h4>Resources</h4>' +
    '<div class="kv"><span class="k">CPU</span><span class="v">' + (p.cpu_percent != null ? p.cpu_percent.toFixed(1) + ' %' : '-') + '</span></div>' +
    '<div class="kv"><span class="k">RSS</span><span class="v">' + fmtBytes(p.rss_bytes) + '</span></div>' +
    '<div class="kv"><span class="k">VSZ</span><span class="v">' + fmtBytes(p.vsz_bytes) + '</span></div>' +
    '<div class="kv"><span class="k">Written</span><span class="v">' + fmtBytes(p.bytes_written) + '</span></div>' +
    '<div class="kv"><span class="k">Last data</span><span class="v">' + lastData + '</span></div>' +
    io +
    '<h4>Paths</h4>' +
    '<div class="kv"><span class="k">Quality</span><span class="v">' + escapeProcHtml(p.record_quality || '?') + ' &rarr; ' + escapeProcHtml(p.quality || '?') + ' (effective)</span></div>' +
    '<div class="kv"><span class="k">Input m3u8</span><span class="v">' + escapeProcHtml(inputShort) +
      '<button class="copy-btn" onclick="event.stopPropagation(); navigator.clipboard.writeText(\'' + escapeInlineJs(input) + '\').catch(function(){})">copy</button>' +
    '</span></div>' +
    '<div class="kv"><span class="k">Output</span><span class="v">' + escapeProcHtml(p.record_path || '-') + '</span></div>' +
    '<div class="kv"><span class="k">HLS</span><span class="v">' + escapeProcHtml(p.playback_url || '-') + '</span></div>' +
    '<div class="kv"><span class="k">Segments</span><span class="v">' + (p.segment_count != null ? p.segment_count : '-') +
      ' (' + fmtBytes(p.segment_bytes || 0) + ')</span></div>' +
    '<h4>FFmpeg log (tail)</h4>' +
    '<pre class="proc-log" id="proc-log-' + escapeProcHtml(p.session_id) + '">loading…</pre>';
}

function toggleProcRow(sid) {
  processesExpanded[sid] = !processesExpanded[sid];
  refreshProcesses();
}

async function loadProcessLog(sid) {
  var el = document.getElementById('proc-log-' + sid);
  if (!el) return;
  try {
    var res = await fetch('/api/processes/' + encodeURIComponent(sid) + '/log?lines=30');
    if (!res.ok) { el.textContent = '(log unavailable: ' + res.status + ')'; return; }
    var data = await res.json();
    var lines = data.lines || [];
    el.textContent = lines.length ? lines.join('\n') : '(empty)';
  } catch (e) {
    el.textContent = '(log fetch error)';
  }
}

async function procStop(sid) {
  var ok = await (window.hxyliveConfirm
    ? window.hxyliveConfirm(
        'Stop ffmpeg session ' + sid + '? The auto-monitor may re-spawn it shortly if auto-record is on.',
        { title: 'Stop session', confirmLabel: 'Stop', danger: true }
      )
    : Promise.resolve(confirm('Stop ffmpeg session ' + sid + '? The auto-monitor may re-spawn it shortly if auto-record is on.')));
  if (!ok) return;
  try {
    var res = await fetch('/api/processes/' + encodeURIComponent(sid) + '/stop', { method: 'POST' });
    if (res.ok) showNotification('Stopped ' + sid, 'success');
    else        showNotification('Failed to stop', 'error');
  } catch (e) { showNotification('Connection error', 'error'); }
  setTimeout(refreshProcesses, 500);
}

async function procKill(sid) {
  var ok = await (window.hxyliveConfirm
    ? window.hxyliveConfirm(
        'Force-kill (SIGKILL) ffmpeg session ' + sid + '?\n\nUse only if a graceful stop is hanging.',
        { title: 'Force-kill session', confirmLabel: 'Kill', danger: true }
      )
    : Promise.resolve(confirm('Force-kill (SIGKILL) ffmpeg session ' + sid + '?\n\nUse only if a graceful stop is hanging.')));
  if (!ok) return;
  try {
    var res = await fetch('/api/processes/' + encodeURIComponent(sid) + '/kill', { method: 'POST' });
    if (res.ok) showNotification('Killed ' + sid, 'success');
    else        showNotification('Failed to kill', 'error');
  } catch (e) { showNotification('Connection error', 'error'); }
  setTimeout(refreshProcesses, 500);
}

async function procRestart(sid) {
  var ok = await (window.hxyliveConfirm
    ? window.hxyliveConfirm(
        'Restart ffmpeg session ' + sid + '?\n\nThis stops the current process; the auto-monitor will re-spawn it within a few seconds with a freshly-resolved URL.',
        { title: 'Restart session', confirmLabel: 'Restart' }
      )
    : Promise.resolve(confirm('Restart ffmpeg session ' + sid + '?\n\nThis stops the current process; the auto-monitor will re-spawn it within a few seconds with a freshly-resolved URL.')));
  if (!ok) return;
  try {
    var res = await fetch('/api/processes/' + encodeURIComponent(sid) + '/restart', { method: 'POST' });
    if (res.ok) showNotification('Restart queued for ' + sid, 'success');
    else        showNotification('Failed to restart', 'error');
  } catch (e) { showNotification('Connection error', 'error'); }
  setTimeout(refreshProcesses, 1500);
}

function procPreview(url) {
  if (!url) { showNotification('No HLS URL for this session', 'error'); return; }
  // Open in a new tab; the existing /watch page can render arbitrary HLS via Hls.js,
  // but the simplest reliable fallback is opening the m3u8 directly.
  window.open(url, '_blank', 'noopener');
}

async function stopAllProcesses() {
  var snap = Object.keys(processesLastSnap);
  if (snap.length === 0) { showNotification('Nothing to stop', 'success'); return; }
  var ok = await (window.hxyliveConfirm
    ? window.hxyliveConfirm(
        'Stop ALL ' + snap.length + ' ffmpeg sessions?\n\nAuto-monitor may re-spawn them if auto-record is on.',
        { title: 'Stop all sessions', confirmLabel: 'Stop all', danger: true }
      )
    : Promise.resolve(confirm('Stop ALL ' + snap.length + ' ffmpeg sessions?\n\nAuto-monitor may re-spawn them if auto-record is on.')));
  if (!ok) return;
  for (var i = 0; i < snap.length; i++) {
    try {
      await fetch('/api/processes/' + encodeURIComponent(snap[i]) + '/stop', { method: 'POST' });
    } catch (e) {}
  }
  showNotification('Stop requested for ' + snap.length + ' sessions', 'success');
  setTimeout(refreshProcesses, 800);
}

// ============================================
// Recording lifecycle audit (durable JSONL)
// ============================================

var auditSessionsCache = [];
var auditOffset = 0;
var auditTotal = 0;
var auditLoading = false;

function auditEscape(str) {
  var div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML;
}

function auditFormatTime(tsUnix, tsIso) {
  var ms = null;
  if (tsUnix != null && tsUnix !== '') {
    ms = Number(tsUnix) * 1000;
  } else if (tsIso) {
    ms = Date.parse(tsIso);
  }
  if (!ms || isNaN(ms)) return '—';
  var d = new Date(ms);
  var hh = String(d.getHours()).padStart(2, '0');
  var mm = String(d.getMinutes()).padStart(2, '0');
  var ss = String(d.getSeconds()).padStart(2, '0');
  return hh + ':' + mm + ':' + ss;
}

function auditFormatBytes(n) {
  var value = Number(n);
  if (!isFinite(value) || value < 0) return '';
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return (i === 0 ? String(Math.round(value)) : value.toFixed(1)) + ' ' + units[i];
}

function auditFormatDuration(seconds) {
  var total = Math.round(Number(seconds));
  if (!isFinite(total) || total < 0) return '';
  var h = Math.floor(total / 3600);
  var m = Math.floor((total % 3600) / 60);
  var s = total % 60;
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm ' + s + 's';
  return s + 's';
}

function auditBuildUrl(offset) {
  var daysEl = document.getElementById('auditDaysFilter');
  var userEl = document.getElementById('auditUsernameFilter');
  var days = daysEl ? daysEl.value : '1';
  var url = '/api/ops/recording-lifecycle?days=' + encodeURIComponent(days) +
    '&limit=50&offset=' + encodeURIComponent(offset || 0);
  var username = userEl ? String(userEl.value || '').trim() : '';
  if (username) url += '&username=' + encodeURIComponent(username);
  return url;
}

function auditOutcomeLabel(outcome) {
  var map = {
    ok: 'OK',
    failed: 'Failed',
    discarded: 'Discarded',
    interrupted: 'Interrupted',
    recording: 'Recording',
    stopped: 'Stopped',
    deleted: 'Deleted',
    info: 'Info'
  };
  return map[outcome] || outcome;
}

function auditGroupForOutcome(outcome) {
  if (outcome === 'failed' || outcome === 'interrupted' || outcome === 'discarded') {
    return 'attention';
  }
  if (outcome === 'ok' || outcome === 'stopped' || outcome === 'recording') {
    return 'recordings';
  }
  return 'other';
}

function auditGroupTitle(key) {
  if (key === 'attention') return 'Needs attention';
  if (key === 'recordings') return 'Recordings';
  return 'Other';
}

function auditRenderSessionCard(sess, globalIdx) {
  var outcome = String(sess.outcome || 'info');
  var user = sess.username || '—';
  var summary = sess.summary || auditOutcomeLabel(outcome);
  var facts = [];
  if (sess.sourceType) facts.push(sess.sourceType);
  var dur = auditFormatDuration(sess.elapsedSeconds);
  if (dur) facts.push(dur);
  var bytes = auditFormatBytes(sess.bytesWritten);
  if (bytes) facts.push(bytes);
  var timeLabel = auditFormatTime(sess.startedUnix, sess.startedAt);
  if (sess.endedUnix && sess.endedUnix !== sess.startedUnix) {
    timeLabel += '–' + auditFormatTime(sess.endedUnix, sess.endedAt);
  }
  var steps = (sess.steps || []).map(function(step) {
    return '<li class="audit-step">' +
      '<span class="audit-step-time">' + auditEscape(auditFormatTime(step.ts_unix, step.ts)) + '</span>' +
      '<span class="audit-step-text">' + auditEscape(step.text || step.event || '') + '</span>' +
    '</li>';
  }).join('');
  var rawJson = auditEscape(JSON.stringify(sess.steps ? sess.steps.map(function(s) { return s.raw; }) : [], null, 2));
  var rowClass = 'audit-session';
  if (auditGroupForOutcome(outcome) === 'attention') rowClass += ' is-attention';
  if (outcome === 'ok') rowClass += ' is-ok-row';
  var metaHtml = facts.length
    ? '<div class="audit-session-meta">' + auditEscape(facts.join(' · ')) +
      (sess.filename ? '<div class="audit-meta-file">' + auditEscape(sess.filename) + '</div>' : '') +
      '</div>'
    : (sess.filename
      ? '<div class="audit-session-meta"><div class="audit-meta-file">' + auditEscape(sess.filename) + '</div></div>'
      : '');
  return '<div class="' + rowClass + '" data-audit-idx="' + globalIdx + '">' +
    '<button type="button" class="audit-session-summary" onclick="toggleAuditSession(this)">' +
      '<span class="audit-session-main">' +
        '<div class="audit-session-top">' +
          '<span class="audit-session-user">' + auditEscape(user) + '</span>' +
          '<span class="audit-outcome is-' + auditEscape(outcome) + '">' + auditEscape(auditOutcomeLabel(outcome)) + '</span>' +
        '</div>' +
        '<div class="audit-session-summary-text">' + auditEscape(summary) + '</div>' +
        metaHtml +
      '</span>' +
      '<span class="audit-session-time">' + auditEscape(timeLabel) + '</span>' +
    '</button>' +
    '<div class="audit-session-detail">' +
      '<ul class="audit-steps">' + steps + '</ul>' +
      '<button type="button" class="audit-raw-toggle" onclick="toggleAuditRaw(this)">Show raw JSON</button>' +
      '<pre class="audit-raw">' + rawJson + '</pre>' +
    '</div>' +
  '</div>';
}

function renderAuditSessions(sessions) {
  var list = document.getElementById('auditSessionList');
  if (!list) return;
  if (!sessions.length) {
    list.innerHTML = '<div class="audit-empty">No lifecycle sessions in this window.</div>';
    return;
  }
  var groups = { attention: [], recordings: [], other: [] };
  sessions.forEach(function(sess, idx) {
    var key = auditGroupForOutcome(sess.outcome || 'info');
    groups[key].push({ sess: sess, idx: idx });
  });
  var order = ['attention', 'recordings', 'other'];
  var html = '';
  order.forEach(function(key) {
    var items = groups[key];
    if (!items.length) return;
    html += '<section class="audit-group" data-group="' + key + '">' +
      '<div class="audit-group-header"><strong>' + auditEscape(auditGroupTitle(key)) + '</strong>' +
      '<span class="audit-group-count">' + items.length + '</span></div>' +
      '<div class="audit-session-list">' +
      items.map(function(item) { return auditRenderSessionCard(item.sess, item.idx); }).join('') +
      '</div></section>';
  });
  list.innerHTML = html;
}

function toggleAuditSession(btn) {
  var row = btn && btn.closest ? btn.closest('.audit-session') : null;
  if (!row) return;
  row.classList.toggle('is-open');
}

function toggleAuditRaw(btn) {
  var row = btn && btn.closest ? btn.closest('.audit-session') : null;
  if (!row) return;
  var open = row.classList.toggle('is-raw-open');
  btn.textContent = open ? 'Hide raw JSON' : 'Show raw JSON';
}

function updateAuditStats(data) {
  var countEl = document.getElementById('auditSessionCount');
  var eventEl = document.getElementById('auditEventCount');
  if (countEl) countEl.textContent = String(data.total != null ? data.total : (data.count || 0));
  if (eventEl) eventEl.textContent = String(data.scannedEvents != null ? data.scannedEvents : '—');
  var more = document.getElementById('auditLoadMoreBtn');
  if (more) {
    var shown = auditSessionsCache.length;
    var total = Number(data.total || 0);
    more.style.display = shown < total ? 'inline-flex' : 'none';
  }
}

async function loadRecordingLifecycle() {
  if (auditLoading) return;
  auditLoading = true;
  auditOffset = 0;
  auditSessionsCache = [];
  var list = document.getElementById('auditSessionList');
  if (list) list.innerHTML = '<div class="audit-empty">Loading…</div>';
  try {
    var res = await fetch(auditBuildUrl(0), { cache: 'no-store' });
    if (!res.ok) {
      if (list) list.innerHTML = '<div class="audit-empty">Failed to load (' + res.status + ')</div>';
      return;
    }
    var data = await res.json();
    auditSessionsCache = data.sessions || [];
    auditOffset = auditSessionsCache.length;
    auditTotal = data.total || 0;
    renderAuditSessions(auditSessionsCache);
    updateAuditStats(data);
  } catch (e) {
    if (list) list.innerHTML = '<div class="audit-empty">Connection error</div>';
  } finally {
    auditLoading = false;
  }
}

async function loadMoreRecordingLifecycle() {
  if (auditLoading) return;
  if (auditSessionsCache.length >= auditTotal) return;
  auditLoading = true;
  try {
    var res = await fetch(auditBuildUrl(auditOffset), { cache: 'no-store' });
    if (!res.ok) return;
    var data = await res.json();
    var more = data.sessions || [];
    auditSessionsCache = auditSessionsCache.concat(more);
    auditOffset = auditSessionsCache.length;
    auditTotal = data.total || auditTotal;
    renderAuditSessions(auditSessionsCache);
    updateAuditStats(data);
  } catch (e) {
    console.error('audit load more failed', e);
  } finally {
    auditLoading = false;
  }
}

// Reuse the global notification helper if present, else fall back to alert.
if (typeof showNotification !== 'function') {
  // minimal fallback so the file is self-contained even if loaded standalone
  window.showNotification = function(msg) { console.log(msg); };
}

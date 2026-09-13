// Component loader for shared header and footer
(function() {
  function setVersionText(version) {
    const label = `v${version || 'unknown'}`;
    const versionTextEl = document.getElementById('appVersionText');
    const versionEl = document.getElementById('appVersion');

    if (versionTextEl) {
      versionTextEl.textContent = label;
    } else if (versionEl) {
      versionEl.textContent = label;
    }

    if (versionEl && !versionEl.classList.contains('has-update')) {
      versionEl.setAttribute('aria-label', `Current version ${label}`);
    }
  }

  function showUpdateBadge(updateInfo) {
    const versionEl = document.getElementById('appVersion');
    const badge = document.getElementById('appUpdateBadge');
    const versionTextEl = document.getElementById('appVersionText');

    if (!versionEl || !badge) return;

    const currentVersion = versionTextEl ? versionTextEl.textContent : versionEl.textContent;
    const latestVersion = updateInfo && (updateInfo.latest_version || updateInfo.latestVersion);
    const behindBy = updateInfo && Number(updateInfo.behindBy || 0);
    const message = latestVersion
      ? `Update available: v${latestVersion}`
      : behindBy > 0
        ? `${behindBy} update${behindBy === 1 ? '' : 's'} available`
        : 'Update available';

    badge.hidden = false;
    badge.title = message;
    versionEl.classList.add('has-update');
    versionEl.title = message;
    versionEl.setAttribute('aria-label', `${currentVersion}. ${message}`);
  }

  // Load header
  fetch('/static/header.html?v=hxylive-ui10')
    .then(res => res.text())
    .then(html => {
      const placeholder = document.getElementById('header-placeholder');
      if (placeholder) {
        placeholder.outerHTML = html;

        // After header injection, highlight active nav link
        const currentPath = window.location.pathname;
        const navLinks = document.querySelectorAll('.nav-link');
        navLinks.forEach(link => {
          const href = link.getAttribute('href');
          if (currentPath === href || (currentPath === '/' && href === '/media')) {
            link.classList.add('active');
          }
        });

        // Load version
        (async function loadVersion() {
          try {
            const res = await fetch('/api/version');
            const data = await res.json();
            setVersionText(data.version);
            const logoutBtn = document.getElementById('logoutBtn');
            if (logoutBtn && data.authentication_required) {
              logoutBtn.hidden = false;
              logoutBtn.style.display = 'flex';
              logoutBtn.addEventListener('click', async function() {
                logoutBtn.disabled = true;
                try {
                  await fetch('/api/logout', { method: 'POST' });
                } finally {
                  window.location.href = '/login';
                }
              });
            }
          } catch (e) {
            console.error('Error loading version:', e);
          }
        })();

        // Media Log lives in the centered header slot; reveal only on /media.
        // Auto Record is Settings-only (no header toggle).
        (async function bindHeaderMediaLog() {
          var logBtn = document.getElementById('headerMediaLogBtn');
          if (logBtn && window.location.pathname.indexOf('/media') === 0) {
            logBtn.hidden = false;
            logBtn.style.display = 'inline-flex';
            logBtn.setAttribute('aria-hidden', 'false');
          }
          try {
            document.dispatchEvent(new CustomEvent('hxylive:header-ready'));
          } catch (e) {}
        })();

        // Check release-based app updates
        (async function checkAppUpdateStatus() {
          try {
            const res = await fetch('/api/system/check-update', { cache: 'no-store' });
            if (!res.ok) return;

            const data = await res.json();
            if (data.update_available) {
              showUpdateBadge(data);
            }
          } catch (e) {
            console.error('Error checking app updates:', e);
          }
        })();

      }
    })
    .catch(err => console.error('Error loading header:', err));

  function ensureConfirmModal() {
    var existing = document.getElementById('hxyliveConfirmModal');
    if (existing) return existing;
    var modal = document.createElement('div');
    modal.id = 'hxyliveConfirmModal';
    modal.className = 'hxylive-confirm-modal';
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML =
      '<div class="hxylive-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="hxyliveConfirmTitle">' +
        '<h2 id="hxyliveConfirmTitle">Confirm</h2>' +
        '<p id="hxyliveConfirmMessage"></p>' +
        '<div class="hxylive-confirm-actions">' +
          '<button type="button" class="hxylive-confirm-cancel" id="hxyliveConfirmCancel">Cancel</button>' +
          '<button type="button" class="hxylive-confirm-ok" id="hxyliveConfirmOk">Confirm</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modal);
    return modal;
  }

  window.hxyliveConfirm = function(message, options) {
    options = options || {};
    var title = options.title || 'Confirm';
    var confirmLabel = options.confirmLabel || 'Confirm';
    var cancelLabel = options.cancelLabel || 'Cancel';
    var danger = !!options.danger;
    return new Promise(function(resolve) {
      var modal = ensureConfirmModal();
      var titleEl = document.getElementById('hxyliveConfirmTitle');
      var messageEl = document.getElementById('hxyliveConfirmMessage');
      var okBtn = document.getElementById('hxyliveConfirmOk');
      var cancelBtn = document.getElementById('hxyliveConfirmCancel');
      var finished = false;

      function close(result) {
        if (finished) return;
        finished = true;
        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('hxylive-confirm-open');
        modal.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        resolve(result);
      }
      function onOk() { close(true); }
      function onCancel() { close(false); }
      function onBackdrop(ev) {
        if (ev.target === modal) close(false);
      }
      function onKey(ev) {
        if (ev.key === 'Escape') close(false);
        if (ev.key === 'Enter') close(true);
      }

      if (titleEl) titleEl.textContent = title;
      if (messageEl) messageEl.textContent = String(message || '');
      if (okBtn) {
        okBtn.textContent = confirmLabel;
        okBtn.classList.toggle('is-danger', danger);
      }
      if (cancelBtn) cancelBtn.textContent = cancelLabel;
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('hxylive-confirm-open');
      modal.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey);
      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      setTimeout(function() { okBtn && okBtn.focus(); }, 0);
    });
  };

})();

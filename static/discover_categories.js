// Discover dynamic category helpers (A P3 / P3.1).
// Pure logic only — no DOM. Generic category request mapping + safety gate.
(function (root) {
  'use strict';

  // /api/discover accepts gender (+ implicit all) and Twitch game_id (A P5).
  var SUPPORTED_DISCOVER_REQUEST_PARAMS = { gender: true, game_id: true, parent_area_id: true };
  // Twitch / Bilibili have no All pill — these are the Discover defaults.
  var TWITCH_DEFAULT_CATEGORY = {
    canonical_key: 'game:509659',
    canonical_category: 'game:509659',
    display_label: 'ASMR',
    category_type: 'content',
    request_param: 'game_id',
    request_value: '509659',
    filter_scope: 'content',
    available: true,
    readiness: 'verified',
    source_signal_present: true
  };
  var BILIBILI_DEFAULT_CATEGORY = {
    canonical_key: 'parent_area:9',
    canonical_category: 'parent_area:9',
    display_label: 'Virtual streamers',
    category_type: 'content',
    request_param: 'parent_area_id',
    request_value: '9',
    filter_scope: 'content',
    available: true,
    readiness: 'verified',
    source_signal_present: true
  };

  function canonicalKeyOf(item) {
    if (!item || typeof item !== 'object') return 'all';
    var key = item.canonical_key || item.canonical_category || 'all';
    return String(key).trim().toLowerCase() || 'all';
  }

  function isFormalCategoryItem(item) {
    if (!item || typeof item !== 'object') return false;
    if (item.available !== true) return false;
    if (String(item.readiness || '') !== 'verified') return false;
    var key = String(item.canonical_key || item.canonical_category || '').trim();
    if (!key) return false;
    var label = String(item.display_label || item.label || '').trim();
    if (!label && key.toLowerCase() === 'all') label = 'All';
    if (!label) return false;
    // Site-wide: never treat unavailable/unsupported/experimental as formal.
    var readinessLower = String(item.readiness || '').toLowerCase();
    if (
      readinessLower === 'unsupported' ||
      readinessLower === 'not_ready' ||
      readinessLower === 'experimental'
    ) {
      return false;
    }
    return true;
  }

  /**
   * Formal buttons come only from payload.categories.
   * Never use unavailable_categories / diagnostics / capability_evidence.
   * Drop items whose request mapping cannot be executed (no grey unsupported pills).
   */
  function filterFormalCategoriesFromPayload(payload) {
    var list = payload && Array.isArray(payload.categories) ? payload.categories : [];
    return list.filter(function (item) {
      if (!isFormalCategoryItem(item)) return false;
      var gate = evaluateCategoryRequestSupport(item);
      return !!(gate && gate.supported);
    });
  }

  function normalizeCategoryItem(item) {
    var raw = item && typeof item === 'object' ? item : {};
    var key = canonicalKeyOf(raw);
    var ctype = String(raw.category_type || (key === 'all' ? 'all' : 'gender')).toLowerCase();
    var requestParam = raw.request_param;
    var requestValue = raw.request_value;
    var filterScope = raw.filter_scope || 'primary';

    if (requestParam === undefined || requestValue === undefined) {
      // Infer legacy payloads that only had canonical_category.
      if (ctype === 'all' || key === 'all') {
        requestParam = null;
        requestValue = null;
      } else if (ctype === 'gender') {
        requestParam = 'gender';
        requestValue = key;
      } else if (ctype === 'content') {
        if (String(key).indexOf('game:') === 0) {
          requestParam = 'game_id';
          requestValue = String(key).slice(5);
        } else {
          requestParam = 'category';
          requestValue = key;
        }
      } else if (ctype === 'language') {
        requestParam = 'language';
        requestValue = key;
      } else {
        requestParam = requestParam === undefined ? null : requestParam;
        requestValue = requestValue === undefined ? key : requestValue;
      }
    }
    if (requestParam === '') requestParam = null;
    if (requestValue === '') requestValue = null;

    return {
      canonical_key: key,
      canonical_category: key,
      display_label: String(raw.display_label || (key === 'all' ? 'All' : key)),
      category_type: ctype,
      request_param: requestParam,
      request_value: requestValue,
      filter_scope: filterScope,
      available: raw.available === true,
      readiness: String(raw.readiness || ''),
      source_signal_present: !!raw.source_signal_present
    };
  }

  function evaluateCategoryRequestSupport(item) {
    var normalized = normalizeCategoryItem(item);
    var ctype = normalized.category_type;
    var param = normalized.request_param;

    if (ctype === 'all' || (!param && (normalized.request_value == null || normalized.request_value === ''))) {
      return {
        supported: true,
        reason: 'all',
        message: '',
        normalized: normalized,
        discoverGender: '',
        discoverGameId: null,
        discoverParentAreaId: null
      };
    }

    if (ctype === 'gender' && param === 'gender' && normalized.request_value) {
      return {
        supported: true,
        reason: 'gender',
        message: '',
        normalized: normalized,
        discoverGender: String(normalized.request_value).toLowerCase(),
        discoverGameId: null,
        discoverParentAreaId: null
      };
    }

    if (
      (ctype === 'content' || param === 'game_id') &&
      param === 'game_id' &&
      normalized.request_value &&
      /^\d+$/.test(String(normalized.request_value))
    ) {
      return {
        supported: true,
        reason: 'game_id',
        message: '',
        normalized: normalized,
        discoverGender: null,
        discoverGameId: String(normalized.request_value),
        discoverParentAreaId: null
      };
    }

    if (
      (ctype === 'content' || param === 'parent_area_id') &&
      param === 'parent_area_id' &&
      normalized.request_value &&
      /^\d+$/.test(String(normalized.request_value))
    ) {
      return {
        supported: true,
        reason: 'parent_area_id',
        message: '',
        normalized: normalized,
        discoverGender: null,
        discoverGameId: null,
        discoverParentAreaId: String(normalized.request_value)
      };
    }

    // Never silently map content/language/tag into gender=.
    return {
      supported: false,
      reason: 'unsupported_request_mapping',
      message: 'This category filter is not wired yet',
      normalized: normalized,
      discoverGender: null,
      discoverGameId: null,
      discoverParentAreaId: null
    };
  }

  /**
   * Build query fields for /api/discover from selected category state.
   * Returns { ok, gender?, game_id?, error? }. Never sets gender for non-gender types.
   */
  function applyCategoryRequest(selection) {
    var sel = selection || {};
    var synthetic = {
      canonical_key: sel.selectedCategoryKey || sel.canonical_key || 'all',
      category_type: sel.selectedCategoryType || sel.category_type || 'all',
      request_param: sel.selectedCategoryRequestParam !== undefined
        ? sel.selectedCategoryRequestParam
        : sel.request_param,
      request_value: sel.selectedCategoryRequestValue !== undefined
        ? sel.selectedCategoryRequestValue
        : sel.request_value,
      filter_scope: sel.filter_scope || 'primary',
      available: true,
      readiness: 'verified'
    };
    var gate = evaluateCategoryRequestSupport(synthetic);
    if (!gate.supported) {
      return {
        ok: false,
        error: gate.message || 'This category filter is not wired yet',
        gender: null,
        game_id: null,
        parent_area_id: null
      };
    }
    if (gate.discoverGender) {
      return { ok: true, gender: gate.discoverGender, game_id: '', parent_area_id: '' };
    }
    if (gate.discoverGameId) {
      return { ok: true, gender: '', game_id: gate.discoverGameId, parent_area_id: '' };
    }
    if (gate.discoverParentAreaId) {
      return { ok: true, gender: '', game_id: '', parent_area_id: gate.discoverParentAreaId };
    }
    return { ok: true, gender: '', game_id: '', parent_area_id: '' };
  }

  function formalHasCanonical(formalItems, canonical) {
    var want = String(canonical || 'all').trim().toLowerCase() || 'all';
    return (formalItems || []).some(function (item) {
      return canonicalKeyOf(item) === want;
    });
  }

  function findFormalByKey(formalItems, canonical) {
    var want = String(canonical || 'all').trim().toLowerCase() || 'all';
    for (var i = 0; i < (formalItems || []).length; i++) {
      if (canonicalKeyOf(formalItems[i]) === want) return normalizeCategoryItem(formalItems[i]);
    }
    return null;
  }

  function findFormalByGenderValue(formalItems, gender) {
    var want = String(gender || '').trim().toLowerCase();
    if (!want || want === 'all') return findFormalByKey(formalItems, 'all');
    for (var i = 0; i < (formalItems || []).length; i++) {
      var n = normalizeCategoryItem(formalItems[i]);
      if (n.category_type === 'gender' && n.request_param === 'gender' &&
          String(n.request_value || '').toLowerCase() === want) {
        return n;
      }
    }
    return null;
  }

  function preferredDefaultForSource(source) {
    var sourceKey = String(source || '').trim().toLowerCase();
    if (sourceKey === 'twitch') return { canonical_key: TWITCH_DEFAULT_CATEGORY.canonical_key };
    if (sourceKey === 'bilibili') return { canonical_key: BILIBILI_DEFAULT_CATEGORY.canonical_key };
    return { canonical_key: 'all' };
  }

  function safeFallbackItemsForSource(source) {
    var sourceKey = String(source || '').trim().toLowerCase();
    if (sourceKey === 'twitch') return [Object.assign({}, TWITCH_DEFAULT_CATEGORY)];
    if (sourceKey === 'bilibili') return [Object.assign({}, BILIBILI_DEFAULT_CATEGORY)];
    return safeAllFallbackItems();
  }

  /** Prefer preferred key; else All (CB/Stripchat) or first formal content item. */
  function selectDefaultCategory(formalItems, preferred) {
    var items = (formalItems || []).map(normalizeCategoryItem);
    var preferredKey = '';
    var preferredGender = '';
    var preferredSource = '';
    if (typeof preferred === 'string') {
      preferredGender = preferred;
    } else if (preferred && typeof preferred === 'object') {
      preferredKey = preferred.canonical_key || preferred.selectedCategoryKey || '';
      preferredGender = preferred.gender || preferred.selectedCategoryRequestValue || '';
      preferredSource = preferred.source || preferred.source_type || '';
    }

    // Treat stale All preference as "use source default" for Twitch/Bilibili.
    if (
      String(preferredKey || '').toLowerCase() === 'all' &&
      (preferredSource === 'twitch' || preferredSource === 'bilibili')
    ) {
      preferredKey = preferredDefaultForSource(preferredSource).canonical_key;
    }

    var candidate = null;
    if (preferredKey) candidate = findFormalByKey(items, preferredKey);
    if (!candidate && preferredGender) candidate = findFormalByGenderValue(items, preferredGender);
    if (!candidate) candidate = findFormalByKey(items, 'all');
    if (!candidate && items.length) candidate = items[0];
    if (!candidate) {
      return normalizeCategoryItem(safeFallbackItemsForSource(preferredSource)[0]);
    }

    var gate = evaluateCategoryRequestSupport(candidate);
    if (!gate.supported) {
      return findFormalByKey(items, 'all')
        || (items[0] ? items[0] : null)
        || normalizeCategoryItem(safeFallbackItemsForSource(preferredSource)[0]);
    }
    return gate.normalized;
  }

  // Back-compat name used by older tests / callers.
  function selectDefaultGenderParam(formalItems, preferredGender) {
    var selected = selectDefaultCategory(formalItems, preferredGender);
    var applied = applyCategoryRequest({
      selectedCategoryKey: selected.canonical_key,
      selectedCategoryType: selected.category_type,
      selectedCategoryRequestParam: selected.request_param,
      selectedCategoryRequestValue: selected.request_value
    });
    return applied.ok ? (applied.gender || '') : '';
  }

  function categoryToGenderParam(canonical) {
    // Legacy helper — only valid for gender/all keys. Do not use for content/language.
    var key = String(canonical || '').trim().toLowerCase();
    if (!key || key === 'all') return '';
    return key;
  }

  function genderParamToCanonical(gender) {
    var key = String(gender || '').trim().toLowerCase();
    return key || 'all';
  }

  function safeAllFallbackItems() {
    return [{
      canonical_key: 'all',
      canonical_category: 'all',
      display_label: 'All',
      category_type: 'all',
      request_param: null,
      request_value: null,
      filter_scope: 'primary',
      available: true,
      readiness: 'verified',
      source_signal_present: true
    }];
  }

  function shouldApplyCategoriesResponse(requestSeq, latestSeq, requestSource, currentSource) {
    if (requestSeq !== latestSeq) return false;
    var req = String(requestSource || '').trim().toLowerCase();
    var cur = String(currentSource || '').trim().toLowerCase();
    return !!req && req === cur;
  }

  var api = {
    SUPPORTED_DISCOVER_REQUEST_PARAMS: SUPPORTED_DISCOVER_REQUEST_PARAMS,
    TWITCH_DEFAULT_CATEGORY: TWITCH_DEFAULT_CATEGORY,
    BILIBILI_DEFAULT_CATEGORY: BILIBILI_DEFAULT_CATEGORY,
    isFormalCategoryItem: isFormalCategoryItem,
    filterFormalCategoriesFromPayload: filterFormalCategoriesFromPayload,
    normalizeCategoryItem: normalizeCategoryItem,
    evaluateCategoryRequestSupport: evaluateCategoryRequestSupport,
    applyCategoryRequest: applyCategoryRequest,
    selectDefaultCategory: selectDefaultCategory,
    selectDefaultGenderParam: selectDefaultGenderParam,
    categoryToGenderParam: categoryToGenderParam,
    genderParamToCanonical: genderParamToCanonical,
    formalHasCanonical: formalHasCanonical,
    findFormalByKey: findFormalByKey,
    findFormalByGenderValue: findFormalByGenderValue,
    canonicalKeyOf: canonicalKeyOf,
    safeAllFallbackItems: safeAllFallbackItems,
    safeFallbackItemsForSource: safeFallbackItemsForSource,
    preferredDefaultForSource: preferredDefaultForSource,
    shouldApplyCategoriesResponse: shouldApplyCategoriesResponse
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  root.DiscoverCategories = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);

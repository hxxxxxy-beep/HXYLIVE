/**
 * Optional Node runner for static/discover_categories.js
 * Usage: node tests/frontend/discover_categories_logic_test.mjs
 */
import { createRequire } from 'module';
import { fileURLToPath } from 'url';
import path from 'path';
import assert from 'assert';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const api = require(path.join(__dirname, '../../static/discover_categories.js'));

const female = api.applyCategoryRequest({
  selectedCategoryKey: 'female',
  selectedCategoryType: 'gender',
  selectedCategoryRequestParam: 'gender',
  selectedCategoryRequestValue: 'female',
});
assert.strictEqual(female.ok, true);
assert.strictEqual(female.gender, 'female');

const all = api.applyCategoryRequest({
  selectedCategoryKey: 'all',
  selectedCategoryType: 'all',
  selectedCategoryRequestParam: null,
  selectedCategoryRequestValue: null,
});
assert.strictEqual(all.ok, true);
assert.strictEqual(all.gender, '');

const asmr = api.applyCategoryRequest({
  selectedCategoryKey: 'asmr',
  selectedCategoryType: 'content',
  selectedCategoryRequestParam: 'category',
  selectedCategoryRequestValue: 'asmr',
});
assert.strictEqual(asmr.ok, false);
assert.strictEqual(asmr.gender, null);

const bilibiliArea = api.applyCategoryRequest({
  selectedCategoryKey: 'parent_area:9',
  selectedCategoryType: 'content',
  selectedCategoryRequestParam: 'parent_area_id',
  selectedCategoryRequestValue: '9',
});
assert.strictEqual(bilibiliArea.ok, true);
assert.strictEqual(bilibiliArea.parent_area_id, '9');
assert.strictEqual(bilibiliArea.game_id, '');
assert.strictEqual(bilibiliArea.gender, '');
assert.strictEqual(bilibiliArea.video_category_id, '');
assert.strictEqual(bilibiliArea.live_query, '');

const youtubeGaming = api.applyCategoryRequest({
  selectedCategoryKey: 'video_category:20',
  selectedCategoryType: 'content',
  selectedCategoryRequestParam: 'video_category_id',
  selectedCategoryRequestValue: '20',
});
assert.strictEqual(youtubeGaming.ok, true);
assert.strictEqual(youtubeGaming.video_category_id, '20');
assert.strictEqual(youtubeGaming.live_query, '');
assert.strictEqual(youtubeGaming.game_id, '');
assert.strictEqual(youtubeGaming.parent_area_id, '');
assert.strictEqual(youtubeGaming.gender, '');

const youtubeAsmr = api.applyCategoryRequest({
  selectedCategoryKey: 'live_query:asmr',
  selectedCategoryType: 'content',
  selectedCategoryRequestParam: 'live_query',
  selectedCategoryRequestValue: 'asmr',
});
assert.strictEqual(youtubeAsmr.ok, true);
assert.strictEqual(youtubeAsmr.live_query, 'asmr');
assert.strictEqual(youtubeAsmr.video_category_id, '');
assert.strictEqual(youtubeAsmr.game_id, '');
assert.strictEqual(youtubeAsmr.parent_area_id, '');
assert.strictEqual(youtubeAsmr.gender, '');


const english = api.applyCategoryRequest({
  selectedCategoryKey: 'english',
  selectedCategoryType: 'language',
  selectedCategoryRequestParam: 'language',
  selectedCategoryRequestValue: 'english',
});
assert.strictEqual(english.ok, false);

assert.strictEqual(typeof api.parseRankingHints, 'undefined');
assert.strictEqual(typeof api.canOfferViewersDescSort, 'undefined');
assert.strictEqual(typeof api.rankingSortControlsEnabled, 'undefined');

assert.strictEqual(api.preferredDefaultForSource('twitch').canonical_key, 'game:509659');
assert.strictEqual(api.preferredDefaultForSource('bilibili').canonical_key, 'parent_area:9');
assert.strictEqual(api.preferredDefaultForSource('youtube').canonical_key, 'all');
assert.strictEqual(api.preferredDefaultForSource('chaturbate').canonical_key, 'all');

const twitchFallback = api.safeFallbackItemsForSource('twitch');
assert.strictEqual(twitchFallback[0].request_value, '509659');
const biliFallback = api.safeFallbackItemsForSource('bilibili');
assert.strictEqual(biliFallback[0].request_value, '9');
assert.strictEqual(biliFallback[0].display_label, 'Virtual streamers');
const youtubeFallback = api.safeFallbackItemsForSource('youtube');
assert.strictEqual(youtubeFallback.length, 0);

const twitchItems = [
  api.TWITCH_DEFAULT_CATEGORY,
  {
    canonical_key: 'game:509658',
    category_type: 'content',
    request_param: 'game_id',
    request_value: '509658',
    display_label: 'Just Chatting',
    available: true,
    readiness: 'verified',
  },
];
const twitchDefault = api.selectDefaultCategory(twitchItems, {
  canonical_key: 'all',
  source: 'twitch',
});
assert.strictEqual(twitchDefault.canonical_key, 'game:509659');

console.log('discover_categories_logic_test.mjs OK');

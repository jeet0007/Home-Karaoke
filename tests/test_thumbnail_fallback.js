const test = require("node:test");
const assert = require("node:assert/strict");
const ThumbnailFallback = require("../static/thumbnail-fallback.js");

test("buildThumbnailChain orders qualities from highest to lowest", () => {
  assert.deepEqual(ThumbnailFallback.buildThumbnailChain("abc123XYZ_-"), [
    "https://img.youtube.com/vi/abc123XYZ_-/maxresdefault.jpg",
    "https://img.youtube.com/vi/abc123XYZ_-/sddefault.jpg",
    "https://img.youtube.com/vi/abc123XYZ_-/hqdefault.jpg",
    "https://img.youtube.com/vi/abc123XYZ_-/mqdefault.jpg",
  ]);
});

test("buildThumbnailChain returns an empty chain for a missing video id", () => {
  assert.deepEqual(ThumbnailFallback.buildThumbnailChain(""), []);
  assert.deepEqual(ThumbnailFallback.buildThumbnailChain(null), []);
});

test("isPlaceholderImage flags YouTube's grey no-thumbnail graphic", () => {
  assert.equal(ThumbnailFallback.isPlaceholderImage(120, 90), true);
  assert.equal(ThumbnailFallback.isPlaceholderImage(1280, 720), false);
});

function fakeImg() {
  return { src: "", onerror: null, onload: null, naturalWidth: 0, naturalHeight: 0 };
}

test("attachThumbnailFallback starts at maxresdefault", () => {
  const img = fakeImg();
  ThumbnailFallback.attachThumbnailFallback(img, "vid123");
  assert.equal(img.src, "https://img.youtube.com/vi/vid123/maxresdefault.jpg");
});

test("attachThumbnailFallback advances on error through the whole chain", () => {
  const img = fakeImg();
  ThumbnailFallback.attachThumbnailFallback(img, "vid123");

  img.onerror();
  assert.equal(img.src, "https://img.youtube.com/vi/vid123/sddefault.jpg");

  img.onerror();
  assert.equal(img.src, "https://img.youtube.com/vi/vid123/hqdefault.jpg");

  img.onerror();
  assert.equal(img.src, "https://img.youtube.com/vi/vid123/mqdefault.jpg");
});

test("attachThumbnailFallback stops and clears handlers once the chain is exhausted", () => {
  const img = fakeImg();
  ThumbnailFallback.attachThumbnailFallback(img, "vid123");

  img.onerror(); // sddefault
  img.onerror(); // hqdefault
  img.onerror(); // mqdefault
  const lastSrc = img.src;
  img.onerror(); // exhausted

  assert.equal(img.src, lastSrc);
  assert.equal(img.onerror, null);
  assert.equal(img.onload, null);
});

test("attachThumbnailFallback treats a placeholder-sized load as a miss and advances", () => {
  const img = fakeImg();
  let loaded = false;
  ThumbnailFallback.attachThumbnailFallback(img, "vid123", () => {
    loaded = true;
  });

  img.naturalWidth = 120;
  img.naturalHeight = 90;
  img.onload();

  assert.equal(img.src, "https://img.youtube.com/vi/vid123/sddefault.jpg");
  assert.equal(loaded, false);
});

test("attachThumbnailFallback fires onLoad once a real image loads", () => {
  const img = fakeImg();
  let loaded = false;
  ThumbnailFallback.attachThumbnailFallback(img, "vid123", () => {
    loaded = true;
  });

  img.naturalWidth = 1280;
  img.naturalHeight = 720;
  img.onload();

  assert.equal(loaded, true);
  assert.equal(img.onerror, null);
  assert.equal(img.onload, null);
});

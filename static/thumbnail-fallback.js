// Builds and walks the YouTube thumbnail quality fallback chain for a video
// id, since maxresdefault.jpg only exists for uploads the creator (or
// YouTube) generated a high-res thumbnail for - most videos only have the
// lower, always-generated qualities. Exposed as `window.ThumbnailFallback`
// in the browser and via `module.exports` under Node for testing.
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.ThumbnailFallback = factory();
  }
})(typeof window !== "undefined" ? window : this, function () {
  var QUALITIES = ["maxresdefault", "sddefault", "hqdefault", "mqdefault"];

  // YouTube's placeholder image for a quality that doesn't exist for a given
  // video is a 120x90 grey "no thumbnail" graphic served with HTTP 200 (not
  // a 404) for some qualities, so `onerror` alone can't detect a miss - the
  // loaded image's pixel size has to be checked too.
  var PLACEHOLDER_MAX_WIDTH = 120;
  var PLACEHOLDER_MAX_HEIGHT = 90;

  function buildThumbnailChain(videoId) {
    if (!videoId) return [];
    return QUALITIES.map(function (quality) {
      return "https://img.youtube.com/vi/" + encodeURIComponent(videoId) + "/" + quality + ".jpg";
    });
  }

  function isPlaceholderImage(width, height) {
    return width <= PLACEHOLDER_MAX_WIDTH && height <= PLACEHOLDER_MAX_HEIGHT;
  }

  // Wires `imgEl` to walk `videoId`'s fallback chain on load failure (or a
  // placeholder hit), calling `onLoad` once a real thumbnail is showing.
  // Leaves `imgEl` with no `src` if every candidate misses.
  function attachThumbnailFallback(imgEl, videoId, onLoad) {
    var chain = buildThumbnailChain(videoId);
    var index = 0;

    function tryNext() {
      if (index >= chain.length) {
        imgEl.onerror = null;
        imgEl.onload = null;
        return;
      }
      var url = chain[index];
      index += 1;
      imgEl.src = url;
    }

    imgEl.onerror = tryNext;
    imgEl.onload = function () {
      if (isPlaceholderImage(imgEl.naturalWidth, imgEl.naturalHeight)) {
        tryNext();
        return;
      }
      imgEl.onerror = null;
      imgEl.onload = null;
      if (typeof onLoad === "function") onLoad();
    };

    tryNext();
  }

  return {
    buildThumbnailChain: buildThumbnailChain,
    attachThumbnailFallback: attachThumbnailFallback,
    isPlaceholderImage: isPlaceholderImage,
  };
});

// The player-shell overlay: the loading spinner / error / "no backing
// track" messaging shown over the (invisible-video, audio-only) player
// while a song is being resolved. See templates/player.html for the
// element this manipulates.

const overlay = document.getElementById('player-overlay');

export function setOverlayLoading(message) {
  overlay.hidden = false;
  overlay.innerHTML = `
    <div>
      <div class="spinner" aria-hidden="true"></div>
      <p class="overlay-title">Finding a karaoke backing track</p>
      <p class="overlay-copy">${message}</p>
    </div>
  `;
}

export function setOverlayMessage(title, message) {
  overlay.hidden = false;
  overlay.innerHTML = '';

  const wrap = document.createElement('div');
  const titleEl = document.createElement('p');
  const copy = document.createElement('p');

  titleEl.className = 'overlay-title';
  titleEl.textContent = title;
  copy.className = 'overlay-copy';
  copy.textContent = message;

  wrap.append(titleEl, copy);
  overlay.appendChild(wrap);
}

export function setOverlayError(message) {
  setOverlayMessage('Could not load this song', message);
}

export function setOverlayNoBackingTrack(message) {
  setOverlayMessage('No backing track found', message);
}

export function hideOverlay() {
  overlay.hidden = true;
}

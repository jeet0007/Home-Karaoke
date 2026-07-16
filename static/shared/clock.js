// Extrapolates a song position between periodic `playback-position`
// broadcasts (see core/rooms.py's set_now_playing_position) so a phone's
// lyric highlighting doesn't stutter waiting for the next update. The TV
// remains the sole source of truth - this only smooths between the
// updates it sends, the same way the note guide already tolerates a
// little imprecision for a guide track.

export function createRemoteClock() {
  let basePosMs = 0;
  let playing = false;
  let receivedAtMs = performance.now();

  return {
    update(posMs, isPlaying) {
      basePosMs = posMs;
      playing = isPlaying;
      receivedAtMs = performance.now();
    },
    nowMs() {
      if (!playing) return basePosMs;
      return basePosMs + (performance.now() - receivedAtMs);
    },
  };
}

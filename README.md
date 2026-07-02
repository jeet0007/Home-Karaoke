# Home Karaoke Search

A tiny local web app for finding karaoke-quality YouTube videos: search for any song and it automatically ranks results by karaoke signals (karaoke/instrumental/backing-track/no-vocals versions boosted, covers/reactions penalised), served through a minimal dark-themed vanilla JS GUI.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000 in your browser.

The main app binds to `127.0.0.1:5000` by default. Set `APP_PORT` or `APP_HOST` to change that:

```bash
APP_PORT=5050 python app.py
```

On macOS, port 5000 is commonly used by AirPlay Receiver (`ControlCenter`). If startup says port 5000
is already in use, set `APP_PORT=<other port>` and retry, or free port 5000 in System Settings.

## Lyrics

Lyrics and song metadata are served through a [Lyrica](https://github.com/Wilooper/Lyrica) sidecar — a
separate lyrics API we run locally and call over HTTP. It is not vendored in this repo; clone it once:

```bash
git clone https://github.com/Wilooper/Lyrica sidecar/lyrica
```

### Running with the sidecar

```bash
./start.sh
```

This installs Lyrica's own dependencies, starts it on port 5001, waits for it to come up, then starts
this app on port 5000. Set `APP_PORT` or `APP_HOST` to change this app's bind address. Set
`LYRICA_PORT` to change the sidecar's port, or `LYRICA_URL` to point at an already-running / remote
Lyrica instance instead (e.g. `LYRICA_URL=https://wilooper-lyrica.hf.space`).

If `sidecar/lyrica` isn't present, `start.sh` skips it and only starts the main app — `/lyrics` and
`/metadata` will then respond with 404s, but `/search` and `/preview` keep working normally.

### Endpoints

```
GET /lyrics?artist=<str>&title=<str>&duration=<int>
  → 200 {"synced": [{"time_ms": int, "text": str}, ...], "plain": "...", "source": "lrclib"}
  → 404 {"error": "no lyrics found"}
```

`synced` is `[]` when Lyrica has no timestamped (LRC) version for the song, even if `plain` is
non-empty. `duration` is accepted but currently unused — Lyrica's `/lyrics/` endpoint has no way to
disambiguate by track length.

```
GET /metadata?artist=<str>&title=<str>
  → 200 {"cover_art": url, "genre": str, "duration_s": int, "release_date": str, ...}
  → 404 {"error": "no metadata found"}
```

`genre` is derived from Lyrica's `tags` list (comma-joined); other fields Lyrica returns (album,
popularity, links, etc.) are passed through as-is.

## Waveform

The player shows a coarse waveform visualization of the current song, computed server-side and served
from `GET /waveform/<video_id>` (a bucketed peak envelope + duration, cached in memory per video). This
requires `ffmpeg` on the server's `PATH` — it's not vendored, install it separately (e.g. `brew install
ffmpeg` / `apt install ffmpeg`). If `ffmpeg` isn't found, `/waveform` returns a 503 and the player simply
shows no waveform; playback and lyrics are unaffected.

## Notes

- Search is powered by the bundled `binaries/yt-dlp` binary (already included in this repo) — no separate yt-dlp install is required for search itself, though `yt-dlp` is still listed in `requirements.txt` as a Python fallback/dependency.
- `search.py` exposes `KaraokeSearch.search()` (auto-appends "karaoke" to your query) and `KaraokeSearch.search_raw()` (searches the exact query, no karaoke bias) for power users.

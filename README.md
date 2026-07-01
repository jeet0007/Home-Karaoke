# Home Karaoke Search

A tiny local web app for finding karaoke-quality YouTube videos: search for any song and it automatically ranks results by karaoke signals (karaoke/instrumental/backing-track/no-vocals versions boosted, covers/reactions penalised), served through a minimal dark-themed vanilla JS GUI.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000 in your browser.

## Notes

- Search is powered by the bundled `binaries/yt-dlp` binary (already included in this repo) — no separate yt-dlp install is required for search itself, though `yt-dlp` is still listed in `requirements.txt` as a Python fallback/dependency.
- `search.py` exposes `KaraokeSearch.search()` (auto-appends "karaoke" to your query) and `KaraokeSearch.search_raw()` (searches the exact query, no karaoke bias) for power users.

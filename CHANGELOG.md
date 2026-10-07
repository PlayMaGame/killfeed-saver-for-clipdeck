# Changelog

All notable changes to Killfeed Saver.

## [1.1.0] - 2026-10-07

### Added
- Kill Feed dock: an OBS Custom Browser Dock showing live saver status
  (working / not running), the replay-buffer state, and the last 3 clips
  newest-first, each with a one-click "copy path" button. Auto-refreshes every 3s.
- Clipboard auto-open: copy a clip path and it opens automatically (~1s poll).
  Only reacts to .mp4 files under the recordings root; never fires on startup.
- Open-last-clip hotkeys: three OBS frontend hotkeys ("open last clip (1/2/3)")
  to open the 3 most recent clips with the default player.
- Clip path resolver: still finds a clip if a downstream tool (e.g. CLIPDECK)
  renamed or moved it.

### Changed
- Config adds `clips_log_path`, `raw_log_path`, `auto_open_clip`,
  `auto_start_replay`, and the dock/hotkey wiring.

## [1.0.0] - 2026-10-07

### Added
- OCR killfeed reader for the Wardogs killfeed (RapidOCR): detects the player's
  own gamertag and drives Replay Buffer Pro's SaveClip over OBS WebSocket.
- OBS "Game Capture" screenshot capture backend (`capture_backend: "obs"`).
- Fight model with an intro ladder: single kill 5s, multi-kill 10s,
  kill+death (death-first) 15s; kill-first +1 death 3s / +2 kills 10s / +3 kills 15s;
  lone death 3s, self-death 10s.
- MIC-follow clip extension (`mic_tail`, `mic_min`).
- Robust re-read handling: jitter-proof row signatures, seen-row memory,
  detection cooldown, OCR-confidence gate, frame-change gate.
- Kind-aware dedupe (`dedupe_enemy_ratio`), gamertag fuzzy match
  (`gamertag_fuzzy_ratio` 0.90).
- Split logs: clean feed log (your own events + skips), raw OCR log, per-clip log.
- OBS autostart script (spawns/supervises the saver) + a version-snapshot scheme.

### Notes
- No learning/calibration is used; matching relies on static character folds only.

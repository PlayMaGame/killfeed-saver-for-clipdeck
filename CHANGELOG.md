# Changelog

All notable changes to Killfeed Saver.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] - 2026-10-10

### Added
- Clip-extension trace in the raw log (`killfeed_raw.log`). Each line names what
  extended a clip, by how many seconds, and the resulting length, e.g.
  `[CLIP] EXTEND +15.0s (kill x2) -> clip 30s` and
  `[CLIP] EXTEND +1.0s (mic follow, -23.4 dB [mag -31.0]) -> clip 31s`, plus
  `[CLIP] OPEN` / `[CLIP] DONE` bookends per clip. Mic lines show the level that
  triggered them (and the channel-0 magnitude in brackets).

## [1.2.0] - 2026-10-08

### Changed
- `mic_threshold` default raised from 0.01 to 0.03 (about -30 dB), so room noise
  no longer reads as speech — the `mic_tail` dead-air cut now actually triggers.
- The mic intro bonus (`mic_intro_extra`) now applies to **any** clip the mic is
  heard in (kills included), not just deaths — covers mumbling before a kill.
- Multi-kill tail is no longer additive: each *extra* kill **resets** the tail to
  `kill_step` (15s) counted from that kill, instead of `15 x kills` from the
  intro. A later kill can no longer be cut, and close-together kills no longer
  leave a long empty tail.

## [1.1.1] - 2026-10-08

### Added
- `mic_intro_extra`: when the mic is active during a death clip, add that many
  seconds to the intro (default 3).

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

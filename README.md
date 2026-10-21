# Killfeed Saver

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Real-time killfeed OCR saver for Wardogs, using OBS Studio + [Replay Buffer Pro](https://github.com/JoshuaPotter/replay-buffer-pro) for exact-length replay clips.

See [CHANGELOG.md](CHANGELOG.md) for the release history.

## How it works

1. Connects to OBS via obs-websocket 5.x (SaveClip, replay buffer, mic, overlay).
2. Every `poll_interval` sec, **directly grabs only the killfeed region** from the
   screen (`capture_backend`, default `obs`) and OCRs it — no full-frame
   screenshots. Set the region with **Crop X / Y / W / H** in the OBS script
   Properties (or `crop` in config.json); the green box shows it.
3. When it sees your gamertag a **fight** opens and ONE final clip is saved for
   it — anchored before the first event, growing as the fight does:
   - **Kill**: a lone kill gets a 5s outro (`outro`); each *extra* kill **resets**
     the tail to `kill_step` (15s) counted from that kill (not additive), so a
     later kill is never cut.
   - **Self-death**: treated like a kill anchor (10s prelude + 5s outro).
   - **Death**: adds 5s (`death_extra`) of complaining room if it follows a kill.
   - **Lone death** (no kill): a short clip (3s prelude + 5s outro).
   - **Mic active**: the tail follows your voice — the clip closes `mic_tail`
     (2.5s) after the last speech, and the intro gets `mic_intro_extra` (3s).
4. **Rewrite**: if a follow-up kill/death lands after a clip was already saved,
   the script saves a longer clip and deletes the shorter one — a fight always
   ends with a single clip at its final length.
5. The final clip is moved out of the replay buffer directory (`_trimmed` variant
   preferred); kills → `Kills/`, self-deaths → `Self Deaths/`, deaths → `Deaths/`.
   Manual replay saves are moved to `Kills/`.

## Clip rules

| Situation | Final clip |
|-----------|------------|
| 1 kill, no mic | 5s prelude + 5s outro = 10s |
| 1 kill + mic | 8s prelude; tail follows speech (last word + 2.5s) |
| 2+ kills | each extra kill tail = 15s (`kill_step`) from that kill |
| kill + death | death tail = 5s outro + 5s `death_extra` |
| lone death | 3s prelude + 5s outro = 8s |
| self-death | 10s prelude + 5s outro |
| mic in the clip | ladder intro + `mic_intro_extra` (3s) |
| Hard cap | 180s (buffer length) |

After each save the script keeps watching for `watch_gap` (15s) more; any follow-up
action rewrites the clip to a longer version. All timings are editable live in
`config.json` / the OBS script Properties.

Replay Buffer Pro 1.8.0+ exposes an obs-websocket vendor command (`SaveClip`,
arbitrary 1s–6h durations), so every clip is saved at its exact length — no 6-button cap.

## Requirements

- Python 3.9+ (3.11 recommended)
- OBS Studio with obs-websocket 5.x enabled (Settings > Tools > WebSocket Server)
- **Replay Buffer Pro** 1.8.0+ (the `SaveClip` vendor command; earlier versions only had
  6 hotkey-triggerable save buttons and are no longer supported by this script).

## Pairs with CLIPDECK

This tool is the capture side of a two-part workflow and is designed to run
alongside **[CLIPDECK](https://github.com/PlayMaGame/clipdeck)**, a self-hosted
gaming-clip triage deck:

- Killfeed Saver saves exact-length replay clips of your kills/deaths.
- CLIPDECK auto-pulls those clips, rips them into vertical 9:16 videos
  (1080x1920, ~60fps, with a minimap PiP), and serves a phone "deck" where you
  swipe to triage: right = keep, left = delete, up/down = skip, hold for
  slow/fast, tap stars to rank.
- Keeps are archived to `CLIPDECK-Keeps` (most-starred first); sources can be
  removed only after a verified rip, and only when deletion is explicitly
  enabled — so nothing is lost by default.

Because CLIPDECK moves/renames clips, the open-last-clip helper resolves a logged
clip by its core name if the original path no longer exists.

## Setup

1. Install dependencies:

   ```bat
   python -m pip install -r requirements.txt
   ```

2. Create your config by copying the example, then edit it:

   ```bat
   copy config.example.json config.json
   ```

   Set `gamertags` to your own tag, `source_name` to your OBS capture source,
   `crop` to your killfeed region, and the log/folder paths to your machine.
   `config.json` is **git-ignored** (your local, personal file);
   `config.example.json` is the tracked template. Leave `obs_password` empty to
   auto-read the password from OBS's WebSocket plugin config.

3. Apply the crop box to your killfeed area (see `crop` in `config.json`; the
   crop-box overlay shows exactly what OCR reads — see below).

## Name matching

Matching runs on **static character folds only** (no learning/calibration):
exact → canonical (folded) substring → windowed fuzzy vs canonical at 5/6 →
generic 4/6, gated by `gamertag_fuzzy_ratio` (default 0.90) and `min_ocr_conf`.

## Run

```bat
run.bat
```

## Crop-box overlay (see exactly what OCR reads)

Show a live green rectangle in OBS over the exact killfeed region the script
reads, so you can dial the crop box in until it hugs the feed:

1. `run.bat cropbox` — auto-creates a `Killfeed Crop Box` browser source in
   your current scene (topmost, canvas-sized).
2. Adjust **Crop X / Y / W / H** in the OBS script Properties (Tools → Scripts →
   the script). The box **moves live** as you change the numbers (the saver
   watches `config.json` and redraws the overlay).
3. Delete the `Killfeed Crop Box` source anytime in OBS to remove it.

The crop is mapped from the Game Capture source's pixels onto the canvas via its
transform, so it's correct even if the source is scaled or moved.

## Detection cooldown

`detect_cooldown` (default 4.0s): after a kill/death/self event fires, new
events are ignored for that long, so OCR re-reads of the same killfeed row (with
slightly different text each time) don't fire multiple clips/chimes.

## Auto-start with OBS (recommended)

So the saver starts and stops together with OBS and you never have to think about it:

1. In OBS, open **Tools > Scripts > Python Settings** and make sure it points at a
   Python **3.9–3.12** (this repo uses 3.11; OBS needs the matching install).
2. Add **`obs_killfeed_autostart.py`** (this folder) as a script in **Tools > Scripts**.
3. The script already has everything registered; when OBS fully loads it starts
   `killfeed_saver.py` hidden and stops it when OBS exits.

Timing / mic / replay settings are editable live in the script's **Properties**
(prelude, outro, kill step, death/mic extras, watch gap, max clip, mic source,
auto replay) — hit **Restart** (or toggle `enable`) to apply and they are written
back to `config.json`.

Notes:

- The saver's output goes to `killfeed_saver.log` in this folder (overwritten on
  each start). The clean killfeed feed appends to `killfeed_feed.log` (see Config notes).
- Starting OBS from its own bin dir avoids a confusing `Failed to find locale/en-US.ini`
  dialog (OBS resolves its data dir from the working directory); OBS relaunches itself
  from the right folder anyway after the crash-recovery dialog, so your normal shortcut
  is unaffected.

## Kill Feed dock and clip hotkeys

The autostart script regenerates `killfeed_dock.html` every 3 seconds. Start OBS
once so the file exists, then show it inside OBS as a Custom Browser Dock:

1. **View > Docks > Custom Browser Docks...**
2. Name it `Kill Feed` and set the URL to the full path of `killfeed_dock.html`,
   e.g. `file:///D:/Learn-Python/Streaming%202026/killfeed-saver/killfeed_dock.html`
3. Click **Apply**. The dock shows **WORKING** / **NOT RUNNING** (saver heartbeat
   within 8s), the replay-buffer state, and the last 3 clips newest-first.
   **copy** puts a clip's full path on the clipboard.

`auto_open_clip` (default on) watches the clipboard: copy a `.mp4` path that
lives under the recordings root and it opens in your default player about a
second later. It ignores whatever was already on the clipboard at startup.

The script also registers three OBS hotkeys — `Killfeed: open last clip (1)`,
`(2)` and `(3)`. Bind them in **Settings > Hotkeys**; they open the three newest
clips and still find them after CLIPDECK renames or moves the files.

## Config notes

- `crop` is `[x, y, width, height]` and must cover the global killfeed region.
- `white_gate` filters OCR to bright text; tweak for your HUD brightness.
- `enable_deaths` (default true) also watches for your tag in victim position.
- The flank of the gamer-switch in the killfeed determines whether a line is
  "your kill" or "your death" (`killer_max_x`). Set `line_y_tolerance` if rows
  occasionally merge.
- `fuzzy_ratio` handles OCR jitter; `dedupe_window` stops the same kill from
  re-triggering.
- `dedupe_y_tolerance` (px) distinguishes a *new* kill row from the same feed line
  read again: a tag hit whose row is within this Y distance of a recent kill is
  skipped as a duplicate, even when OCR reads the target name differently.
- `rbp_trigger_gap` (s, default 3) spaces out RBP `SaveClip` requests. RBP folds a
  new save into one already in flight when it arrives before OBS starts writing
  (the newest press wins), which would merge two saves into one clip; this guard
  defers any save that would land too soon after the previous one.
- Clip timing keys: `prelude` (10s before a multi-kill / self-death),
  `prelude_single` (5s before a lone kill), `prelude_death` (3s before a lone
  death), `prelude_combo` (15s intro for a kill+death or death-first fight),
  `outro` (5s base tail), `kill_step` (15s tail after each *extra* kill — resets
  per kill, not additive), `death_extra` (5s after a death that follows a kill),
  `watch_gap` (15s follow-up window after a save), `max_clip_seconds` (180s cap).
- `mic_source` (OBS input name, default `MIC`) drives the mic follow through the
  `InputVolumeMeters` websocket event. `mic_threshold` (linear amplitude, default
  0.03 = about -30 dB) is how loud counts as speech; `mic_tail` (2.5s) is the
  dead-air delay that ends the clip; `mic_min` (3s) is a small floor.
- `mic_intro_extra` (default 3) adds a few seconds of **intro** whenever the mic
  is active during a clip — a little more lead-in so what you were mumbling
  before the kill (or ranting about after) is included.
- `gamertag_fuzzy_ratio` (0.67 = ~4/6 chars) makes OCR gamertag matching tolerant
  to character swaps and clipped reads of your tag.
- `feed_log` (default true) appends a clean killfeed feed to `feed_log_path`
  (default `E:/OBS Recording/killfeed_feed.log`) — one line per row, marked
  `<< KILL` / `<< DEATH` / `<< SELF` when your tag matched, or `--` for
  enemy-only (proximity) rows. Watch it live with
  `Get-Content "E:\OBS Recording\killfeed_feed.log" -Wait -Tail 20`. A missed
  death shows up as either a `--` row (your name was misread) or no row at all
  (OCR/timing/crop).
- `raw_log_path` (default `E:/OBS Recording/killfeed_raw.log`) holds every OCR
  row **and** a clip-extension trace, so you can see exactly why a clip got long:
  `[CLIP] OPEN kill | intro 5s -> clip 10s`,
  `[CLIP] EXTEND +15.0s (kill x2) -> clip 30s`,
  `[CLIP] EXTEND +1.0s (mic follow, -23.4 dB [mag -31.0]) -> clip 31s`,
  `[CLIP] DONE kill | 2k/0d | intro 13s | clip 31s`.
  Each line names the trigger, the seconds added and the new length; mic lines
  show the level (what triggered it, and the magnitude in brackets).
- `detect_chime` (default true) plays a two-tone ding whenever your gamertag is
  registered in the killfeed (kill / death / self-death) — instant audio
  confirmation the script sees you. `chime_wav` can point at a custom WAV;
  blank auto-generates `chime.wav` next to the script.
- `clips_log_path` (default `E:/OBS Recording/killfeed_clips.log`) gets one line
  per saved clip. The dock and the open-last-clip hotkeys read this file, so
  keep it next to the clips it describes.
- `auto_start_replay` (default false) starts the replay buffer as soon as the
  saver connects to OBS, so you never forget to arm it.

## Contributing / Contact

Found this useful, or found something broken? Open an issue on GitHub.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 PlayMaGame.
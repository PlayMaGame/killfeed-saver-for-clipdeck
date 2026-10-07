# Killfeed Saver

Real-time killfeed OCR saver for Wardogs, using OBS Studio + [Replay Buffer Pro](https://github.com/JoshuaPotter/replay-buffer-pro) for exact-length replay clips.

## How it works

1. Connects to OBS via obs-websocket 5.x (SaveClip, replay buffer, mic, overlay).
2. Every `poll_interval` sec, **directly grabs only the killfeed region** from the
   screen (`capture_backend`, default `mss`) and OCRs it — no full-frame
   screenshots. Set the region with **Crop X / Y / W / H** in the OBS script
   Properties (or `crop` in config.json); the green box shows it.
3. When it sees your gamertag a **fight** opens and ONE final clip is saved for
   it — anchored 10s before the first kill, growing as the fight does:
   - **Kill**: each kill adds 15s (`kill_step`). 1 kill = 15s clip, 2 = 30s, ...
   - **Self-death**: treated like a kill anchor (10s prelude + 5s outro).
   - **Death**: adds 5s (`death_extra`) of complaining room if it follows a kill.
   - **Lone death** (no kill): a short 10s clip (5s prelude + 5s outro).
   - **Mic active** during a window: +5s (`mic_extra`), once per event, unless it
     was a lone death.
4. **Rewrite**: if a follow-up kill/death lands after a clip was already saved,
   the script saves a longer clip and deletes the shorter one — a fight always
   ends with a single clip at its final length.
5. The final clip is moved out of the replay buffer directory (`_trimmed` variant
   preferred); kills → `Kills/`, self-deaths → `Self Deaths/`, deaths → `Deaths/`.
   Manual replay saves are moved to `Kills/`.

## Clip rules

| Situation | Final clip |
|-----------|------------|
| 1 kill, nothing else | 15s (10 prelude + 5 outro) |
| 1 kill + mic during outro | 20s |
| 1 kill + 1 death | +5s complaining |
| 2 kills | 30s |
| 3 kills | 45s |
| N kills | 15 × N s |
| Self-death | 15s (+5s if mic active) |
| Lone death | 10s (5 prelude + 5 outro) |
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

## Setup

```bat
python -m pip install -r requirements.txt
```

Then apply the crop box to your killfeed area (see `crop` in `config.json`; use
`calibrate` to verify):

```bat
run.bat calibrate --shots 3
```

## Learn my name (auto-tune OCR)

To make the script read your gamertag ~99% reliably even with a full
multilingual killfeed, run one learning session while the game's killfeed is
set to **self-only** (every row is your name = ground truth):

1. Set the game killfeed to show only your kills/deaths.
2. Turn on **`Learn my name`** (OBS script Properties) or set `"learn_mode": true`.
3. Play normally. The script now:
   - **Auto-tunes the reading pattern** — scores `raw`, `up2x`, `up3x`,
     `gray_contrast`, `binarize`, `sharp` on your name and locks in the best
     (`ocr_preprocess`).
   - **Learns a character-confusion map** — e.g. if it reads `TAG_READ` for
     `YOUR_GAMERTAG` it records `i → l`. Persisted as `learned_confusables`
     (`{"i": ["l","1"], "z": ["2"], ...}`), used to fold OCR junk before
     matching. No list of garbled names.
   - **Counts opportunities** (a killfeed row appearing) via a brightness
     "door sensor" — independent of OCR — and scores each as a **hit** (name
     read) or **miss** (row present but name not read). Miss photos are saved
     to `misses_folder` (`E:/OBS Recording/killfeed-misses`) so you can see
     what was missed.
4. After **≥20 opportunities**, if the detection rate is **≥99%** the log prints
   `[learn] READY`. Only then switch the full killfeed back on.
5. Matching afterwards: exact → canonical (static + learned confusions)
   substring → windowed fuzzy vs canonical at 5/6 → generic 4/6.

To start over (font/UI change): set `"clear_learned": true` once — it wipes
`learned_confusables` + `ocr_preprocess` and resets the counters.

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
- Clip timing keys: `prelude` (10s before a kill/self-death), `prelude_death` (5s
  before a lone death), `outro` (5s base tail), `kill_step` (15s per kill),
  `death_extra` (5s after a death that follows a kill), `mic_extra` (5s once per
  event when the mic is active), `watch_gap` (15s follow-up window after a save),
  `max_clip_seconds` (180s hard cap, matches the replay buffer length).
- `mic_source` (OBS input name, default `MIC`) and `mic_threshold` (RMS, default
  0.01) drive the +5s mic boost via the `InputVolumeMeters` websocket event.
- `gamertag_fuzzy_ratio` (0.67 = ~4/6 chars) makes OCR gamertag matching tolerant
  to character swaps and clipped reads of your tag.
- `feed_log` (default true) appends a clean killfeed feed to `feed_log_path`
  (default `E:/OBS Recording/killfeed_feed.log`) — one line per row, marked
  `<< KILL` / `<< DEATH` / `<< SELF` when your tag matched, or `--` for
  enemy-only (proximity) rows. Watch it live with
  `Get-Content "E:\OBS Recording\killfeed_feed.log" -Wait -Tail 20`. A missed
  death shows up as either a `--` row (your name was misread) or no row at all
  (OCR/timing/crop).
- `detect_chime` (default true) plays a two-tone ding whenever your gamertag is
  registered in the killfeed (kill / death / self-death) — instant audio
  confirmation the script sees you. `chime_wav` can point at a custom WAV;
  blank auto-generates `chime.wav` next to the script.
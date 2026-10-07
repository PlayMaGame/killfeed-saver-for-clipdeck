import argparse
import base64
import collections
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import time
import wave
from difflib import SequenceMatcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

try:
    import winsound
except ImportError:
    winsound = None

import cv2
import numpy as np
import websocket

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:
    RapidOCR = None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "gamertags": ["YOUR_GAMERTAG"],
    "source_name": "Game Capture",
    "obs_host": "127.0.0.1",
    "obs_port": 4455,
    "obs_password": "",
    "capture_width": 1280,
    "capture_height": 720,
    "capture_backend": "mss",   # direct screen-region capture: mss (GDI) / dxcam
    "crop": [0, 310, 240, 85],
    "poll_interval": 0.7,

    # Clip model: the clip is a window pinned to the first event.
    "prelude": 10,            # seconds of footage before a multi-kill / self-death
    "prelude_death": 3,       # seconds of footage before a lone death
    "prelude_single": 5,      # seconds of footage before a single kill
    "prelude_combo": 15,      # intro for a fight with a kill AND a death/self-death
    "outro": 5,               # base tail after an event (1 kill => 10 prelude + 5 outro = 15s)
    "kill_step": 15,          # each additional kill adds this much (clip = 15 * kills)
    "death_extra": 5,         # extra tail after a death that follows a kill (complaining)
    "mic_extra": 5,           # (retired) legacy one-shot mic bonus
    "mic_tail": 2.5,          # dead-air seconds after the last speech -> clip ends
    "mic_min": 3,             # small floor: mic clip is at least this long
    "mic_source": "MIC",      # OBS input whose level is watched
    "mic_threshold": 0.01,    # RMS multiplier above which the mic counts as active
    "watch_gap": 15,          # keep watching this long after a save for follow-up action
    "max_clip_seconds": 180,  # hard cap (matches the replay buffer length)

    # OCR / diagnostics
    "gamertag_fuzzy_ratio": 0.90,  # windowed match threshold (near-exact)
    "ocr_preprocess": "gray_contrast",  # reading pattern (raw/up2x/up3x/gray_contrast/...)
    "feed_log": True,              # append your matched events to the feed log
    "feed_log_path": "E:/OBS Recording/killfeed_feed.log",
    "raw_log_path": "E:/OBS Recording/killfeed_raw.log",  # every OCR row (debug)
    "clips_log_path": "E:/OBS Recording/killfeed_clips.log",  # simple per-clip summary
    "detect_chime": True,          # two-tone ding when your tag is detected
    "chime_wav": "",               # custom WAV path (blank = auto-generated chime.wav)

    "rbp_trigger_gap": 3.0,
    "detect_cooldown": 5.0,        # seconds to pause OCR after an event
    "row_dup_ratio": 0.80,        # row-signature similarity to treat as a re-read
    "row_memory_sec": 10,         # how long a row signature is remembered
    "min_ocr_conf": 0.5,          # ignore OCR reads below this confidence
    "frame_change_min": 0.0,      # min fraction of crop pixels changed to run OCR (0 = any change >4px)
    "overlay_source": "Killfeed Crop Box",  # browser-source name for the crop overlay
    "dedupe_window": 10,          # seconds a row's enemy name is remembered
    "dedupe_enemy_ratio": 0.75,   # enemy-name similarity to count as same row
    "dedupe_y_tolerance": 8,
    "fuzzy_ratio": 0.72,
    "line_y_tolerance": 14,
    "enable_deaths": True,
    "auto_start_replay": False,
    "enable_self_deaths": True,
    "self_deaths_folder": "",
    "self_death_keywords": ["Tu", "Suicidio", "Suicide", "Muerte",
                            "Caida", "Fall", "Fuego", "Fire",
                            "Explosion", "Puerta", "Door"],
    "kills_folder": "",
    "deaths_folder": "",
    "white_gate": {"low": [170, 170, 170], "high": [255, 255, 255], "min_pixels": 30},
}

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

KILLER_NAME_MAX_X_RATIO = 130.0 / 240.0

# Log handles, set once in _main_loop:
#   CLIP_LOG -> killfeed_clips.log : one line per saved clip
#   FEED_LOG -> killfeed_feed.log  : YOUR events only (kill/death/self) + skips
#   RAW_LOG  -> killfeed_raw.log   : every OCR row (dev/debug)
CLIP_LOG = None
FEED_LOG = None
RAW_LOG = None
FEED_LOCK = threading.Lock()


def clip_log(msg):
    """Saver stdout (goes to killfeed_saver.log)."""
    print(msg)


def _log_line(f, msg, stamp):
    if f is None:
        return
    try:
        with FEED_LOCK:
            f.write(("[%s] %s" % (time.strftime("%H:%M:%S"), msg) if stamp else msg) + "\n")
            f.flush()
    except Exception:
        pass


def feed_msg(msg):
    """Your-events log: a clip decision line (skip/dupe/open), timestamped."""
    _log_line(FEED_LOG, msg, True)


def clip_summary(msg):
    """Clip log: pre-formatted one-line summary."""
    _log_line(CLIP_LOG, msg, False)

_DISTANCE_TAG_RE = re.compile(r"\[?\d+\s*m\]?", re.IGNORECASE)
_LEADING_CLAN_TAG_RE = re.compile(r"^\[[^\[\]]{1,12}\]\s*")
_NON_WORD_RE = re.compile(r"[^\w]", re.UNICODE)
_ANY_TAG_RE = re.compile(r"\[[^\]]*\]")   # every [...] tag, anywhere
_DIGITS_RE = re.compile(r"\d+")
UNKNOWN_VICTIM = "Enemigo"


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        cfg.update(user)
    return cfg


def update_config_keys(updates):
    """Merge `updates` into config.json (preserving the rest), atomically."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.update(updates)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def resolve_password(cfg):
    if cfg.get("obs_password"):
        return cfg["obs_password"]
    p = os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "plugin_config",
                     "obs-websocket", "config.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("server_enabled", True):
            return d.get("server_password", "")
    return ""


def _pid_alive(pid):
    """True if a process with `pid` is running (Windows)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    except Exception:
        return False


def acquire_single_instance():
    """Ensure only one saver runs: take over a stale lock, or terminate a live
    older instance (orphan from a force-killed OBS) and replace it."""
    lock = os.path.join(HERE, "killfeed_saver.lock")
    if os.path.exists(lock):
        try:
            with open(lock, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            if _pid_alive(pid) and pid != os.getpid():
                print("[info] replacing previous saver pid %d" % pid)
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                time.sleep(0.5)
        except (ValueError, OSError):
            pass
        try:
            os.remove(lock)
        except OSError:
            pass
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        print("[warn] cannot write lock file: %s" % e)
    return lock


# --------------------------------------------------------------------------- #
# OCR text helpers (ported from ClipsKillFeedWardogs core/scanner.py)
# --------------------------------------------------------------------------- #

def normalize_key(name):
    if not name:
        return ""
    key = _NON_WORD_RE.sub("", name.lower())
    if key:
        return key
    return re.sub(r"\s+", "", name.lower())


_UNKNOWN_VICTIM_KEY = normalize_key(UNKNOWN_VICTIM)


def strip_distance_tag(name):
    if not name or not _DISTANCE_TAG_RE.search(name):
        return name
    cleaned = _DISTANCE_TAG_RE.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or name


def strip_clan_tag(name):
    if not name:
        return name
    cleaned = _LEADING_CLAN_TAG_RE.sub("", name).strip()
    return cleaned or name


def names_likely_match(a, b, ratio):
    if not a or not b:
        return False
    if a == b:
        return True
    if a == _UNKNOWN_VICTIM_KEY or b == _UNKNOWN_VICTIM_KEY:
        return True
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        return True
    if len(a) >= 4 and len(b) >= 4 and SequenceMatcher(None, a, b).ratio() >= ratio:
        return True
    return False


def enemy_names_match(a, b, ratio, allow_unknown=True):
    """True if two enemy-name reads are the same enemy (row dedupe). Unlike
    names_likely_match, an unreadable name (empty/unknown) matches anything --
    a row re-read with the enemy dropped is treated as the same row.

    `allow_unknown` gates that wildcard: it is only safe within the same row
    kind, so a self-death (enemy key = the 'Enemigo' placeholder) can never
    swallow a real kill/death that happens to be read next."""
    if not a or not b:
        return allow_unknown
    if a == _UNKNOWN_VICTIM_KEY or b == _UNKNOWN_VICTIM_KEY:
        return allow_unknown
    if a == b:
        return True
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= ratio


# Common one-character OCR swaps on this killfeed font.
OCR_CONFUSABLES = str.maketrans({
    "1": "i", "l": "i", "|": "i", "I": "i",
    "0": "o", "O": "o",
    "2": "z", "Z": "z",
    "5": "s", "S": "s",
    "3": "e",
})


def _confusion_fold():
    """Static OCR confusable fold (char -> canonical)."""
    return dict(OCR_CONFUSABLES)


def canonical_key(name):
    """normalize_key() then fold the static OCR confusable characters."""
    return normalize_key(name).translate(_confusion_fold())


def _best_window(tag, text, tol=2):
    """Best (ratio, window_substring) aligning `tag` within `text`."""
    best = (0.0, "")
    lo = max(1, len(tag) - tol)
    hi = len(tag) + tol
    for w in range(lo, hi + 1):
        for i in range(0, max(1, len(text) - w + 1)):
            sub = text[i:i + w]
            r = SequenceMatcher(None, tag, sub).ratio()
            if r > best[0]:
                best = (r, sub)
    return best


def _best_window_ratio(tag, text, tol=2):
    return _best_window(tag, text, tol)[0]


def match_gamertag(clean, clean_gamertags, fuzzy_ratio=0.90):
    """Return the normalized gamertag `clean` matches, or None.

    Priority: exact tag substring -> canonical (static fold) substring ->
    windowed fuzzy vs the tag at `fuzzy_ratio`."""
    for gt in clean_gamertags:
        if len(gt) >= 2 and gt in clean:
            return gt
    cclean = canonical_key(clean)
    for gt in clean_gamertags:
        if len(gt) >= 2 and canonical_key(gt) in cclean:
            return gt
    for gt in clean_gamertags:
        if len(gt) >= 4 and _best_window_ratio(gt, clean) >= fuzzy_ratio:
            return gt
    return None


def classify_feed_line(line, clean_gamertags, track_deaths, self_keywords,
                       fuzzy_ratio, min_conf=0.0):
    """Return (marker, text, directed) for one killfeed line.

    marker is '<< KILL' / '<< DEATH' / '<< SELF' when the gamertag matched, or
    '--' for an enemy-only (proximity) row. `directed` is the readable
    'killer >> victim' form (or the lone tag) for your-events logging."""
    line_sorted = sorted(line, key=lambda r: min(float(p[0]) for p in r[0]))
    texts = [str(it[1]) for it in line_sorted]
    text = " ".join(t for t in texts if t.strip()) or " ".join(texts)
    parts = [strip_distance_tag(t.strip()) for t in texts if t.strip()]
    directed = ("%s >> %s" % (parts[0], parts[-1])) if len(parts) >= 2 else text
    marker = "--"
    self_keys = set(normalize_key(k) for k in self_keywords)
    for idx, item in enumerate(line_sorted):
        txt = str(item[1])
        if len(item) > 2 and item[2] is not None and item[2] < min_conf:
            continue   # low-confidence OCR read -> don't trust it
        clean = normalize_key(txt)
        gt = match_gamertag(clean, clean_gamertags, fuzzy_ratio)
        if gt is None:
            continue
        if len(line_sorted) == 1:
            if track_deaths:
                marker = "<< SELF"
                directed = str(txt).strip()
            break
        if idx == 0:
            victim_key = normalize_key(strip_clan_tag(texts[-1])) if texts else ""
            if match_gamertag(victim_key, clean_gamertags, fuzzy_ratio) is not None:
                marker = "<< SELF"   # YOUR_TAG >> YOUR_TAG (you killed yourself)
            else:
                marker = "<< KILL"
            break
        if track_deaths:
            killer = strip_clan_tag(parts[0]) if parts else ""
            k_norm = normalize_key(killer)
            if txt.strip() == "Tu" or k_norm == gt or k_norm in self_keys:
                marker = "<< SELF"
            else:
                marker = "<< DEATH"
            break
    return marker, text, directed


def _feed_rows(lines, clean_gamertags, track_deaths, self_keywords, fuzzy_ratio,
               prev_rows, min_conf=0.0):
    """Return (raw_text, clean_text) for the NEW rows in `lines` (rows not already
    logged), and update prev_rows to the current row set so persistent rows
    aren't repeated.

    raw_text = every OCR row (killfeed_raw.log); clean_text = only the rows where
    your tag matched, as 'label   killer >> victim' (killfeed_feed.log)."""
    raw = []
    clean = []
    current = set()
    for line in lines:
        marker, text, directed = classify_feed_line(line, clean_gamertags,
                                                    track_deaths, self_keywords,
                                                    fuzzy_ratio, min_conf)
        row = "%s   %s" % (marker, text)
        current.add(row)
        if row not in prev_rows:
            ts = time.strftime("%H:%M:%S")
            raw.append("[%s] %s" % (ts, row))
            if marker != "--":
                clean.append("[%s] %-5s %s" % (ts, marker.replace("<< ", ""), directed))
    prev_rows.clear()
    prev_rows.update(current)
    return ("".join(l + "\n" for l in raw),
            "".join(l + "\n" for l in clean))


OCR_PREPROCESS_VARIANTS = ("raw", "up2x", "up3x", "gray_contrast", "binarize", "sharp")


def apply_preprocess(crop, variant):
    """Apply an OCR preprocessing variant to the killfeed crop."""
    if variant == "up2x":
        return cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    if variant == "up3x":
        return cv2.resize(crop, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    if variant == "gray_contrast":
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return cv2.cvtColor(clahe.apply(gray), cv2.COLOR_GRAY2BGR)
    if variant == "binarize":
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return cv2.cvtColor(th, cv2.COLOR_GRAY2BGR)
    if variant == "sharp":
        blur = cv2.GaussianBlur(crop, (0, 0), 1.0)
        return cv2.addWeighted(crop, 1.5, blur, -0.5, 0)
    return crop  # raw


def ensure_chime_wav(path):
    """Create a pleasant two-tone ding WAV (E5 -> A5, decaying) if missing."""
    if os.path.exists(path):
        return
    try:
        sample_rate = 44100
        notes = [(659.25, 0.0), (880.0, 0.16)]
        total = 0.55
        frames = bytearray()
        for i in range(int(sample_rate * total)):
            t = i / sample_rate
            val = 0.0
            for freq, start in notes:
                if t >= start:
                    dt = t - start
                    val += math.sin(2.0 * math.pi * freq * dt) * math.exp(-dt * 7.0)
            val = max(-1.0, min(1.0, val * 0.5))
            frames += struct.pack("<h", int(val * 32000))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(bytes(frames))
        print("[info] generated chime: %s" % path)
    except Exception as e:
        print("[warn] could not generate chime wav: %s" % e)


def group_ocr_into_lines(items, y_tolerance):
    def y_center(box):
        return sum(float(p[1]) for p in box) / len(box)

    items_sorted = sorted(items, key=lambda it: y_center(it[0]))
    lines, cur, last_y = [], [], None
    for it in items_sorted:
        y = y_center(it[0])
        if last_y is not None and abs(y - last_y) > y_tolerance:
            lines.append(cur)
            cur = []
        cur.append(it)
        last_y = y
    if cur:
        lines.append(cur)
    return lines


def _row_signature(text):
    """Jitter-proof row identity: drop every [...] tag and all digits, then
    normalize. The same killfeed row re-read with tag/spacing/digit jitter
    collapses to the same value."""
    text = _ANY_TAG_RE.sub(" ", text)
    text = _DIGITS_RE.sub("", text)
    return normalize_key(text)


def row_signature(line):
    return _row_signature(" ".join(str(it[1]) for it in line))


def killfeed_signature(lines):
    """Frame-level signature: the tuple of per-row signatures."""
    return tuple(row_signature(line) for line in lines)


def detect_events_from_lines(lines, clean_gamertags, track_deaths, killer_max_x,
                             self_keywords=(), fuzzy_ratio=0.67, min_conf=0.0):
    events = []
    self_keywords = set(normalize_key(k) for k in self_keywords)
    for line in lines:
        line_sorted = sorted(line, key=lambda r: min(float(p[0]) for p in r[0]))
        line_texts = [str(it[1]) for it in line_sorted]
        for idx, item in enumerate(line_sorted):
            box, txt, _score = item
            if _score is not None and _score < min_conf:
                continue   # low-confidence OCR read -> don't trust it
            clean = normalize_key(str(txt))
            matched = match_gamertag(clean, clean_gamertags, fuzzy_ratio)
            if matched is None:
                continue
            y_center = sum(float(p[1]) for p in box) / len(box)

            # A line showing only your tag (no enemy) is a self/environmental
            # death (suicide, fall, crushed by door, ...).
            if len(line_sorted) == 1:
                if track_deaths:
                    events.append(("self", matched, UNKNOWN_VICTIM, matched, y_center))
                break

            is_your_kill = idx == 0
            parts = [t.strip() for t in line_texts if t.strip()]
            killer = parts[0] if parts else matched
            victim = parts[-1] if len(parts) >= 2 else UNKNOWN_VICTIM
            killer = strip_distance_tag(killer)
            victim = strip_distance_tag(victim)

            if is_your_kill:
                victim_key = normalize_key(strip_clan_tag(victim))
                victim_is_you = match_gamertag(victim_key, clean_gamertags,
                                               fuzzy_ratio) is not None
                if victim_is_you:
                    # YOUR_TAG >> YOUR_TAG -> you killed yourself (self-death)
                    if track_deaths:
                        events.append(("self", matched, UNKNOWN_VICTIM, matched, y_center))
                else:
                    events.append(("kill", matched, killer, victim, y_center))
            elif track_deaths:
                victim = matched
                k_norm = normalize_key(strip_clan_tag(killer))
                if killer == "Tu" or k_norm == matched or k_norm in self_keywords:
                    kind = "self"
                    killer = UNKNOWN_VICTIM
                else:
                    kind = "death"
                events.append((kind, matched, killer, victim, y_center))
            break
    return events


def detect_events(ocr_items, clean_gamertags, track_deaths, killer_max_x, y_tolerance=14,
                  self_keywords=(), fuzzy_ratio=0.67, min_conf=0.0):
    return detect_events_from_lines(
        group_ocr_into_lines(ocr_items, y_tolerance),
        clean_gamertags, track_deaths, killer_max_x, self_keywords, fuzzy_ratio, min_conf)


# --------------------------------------------------------------------------- #
# OBS WebSocket client (obs-websocket 5.x)
# --------------------------------------------------------------------------- #

class OBSClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ws = None
        self._rid = 0
        self.on_replay_saved = None
        self.on_mic_levels = None
        self._events = collections.deque()

    def connect(self):
        password = resolve_password(self.cfg)
        url = "ws://%s:%d" % (self.cfg["obs_host"], self.cfg["obs_port"])
        self.ws = websocket.create_connection(url, timeout=5)
        hello = self._recv(timeout=5)
        auth = None
        if password and hello:
            auth_info = hello.get("d", {}).get("authentication", {})
            salt = auth_info.get("salt")
            challenge = auth_info.get("challenge")
            if salt and challenge:
                secret = base64.b64encode(
                    hashlib.sha256((password + salt).encode("utf-8")).digest()
                ).decode("ascii")
                auth = base64.b64encode(
                    hashlib.sha256((secret + challenge).encode("utf-8")).digest()
                ).decode("ascii")
        sub = sum(1 << i for i in range(11)) | (1 << 16) | (1 << 17) | (1 << 18) | (1 << 19)
        self.ws.send(json.dumps({
            "op": 1,
            "d": {"rpcVersion": 1, "authentication": auth, "eventSubscriptions": sub},
        }))
        ident = self._recv(timeout=5)
        if not ident or ident.get("op") != 2 or ident.get("d", {}).get("negotiatedRpcVersion") is None:
            raise RuntimeError("obs-websocket Identify failed")

    def _recv(self, timeout):
        self.ws.settimeout(timeout)
        try:
            return json.loads(self.ws.recv())
        except websocket.WebSocketTimeoutException:
            return None
        except Exception:
            return None

    def request(self, request_type, request_data=None, timeout=10):
        self._rid += 1
        rid = str(self._rid)
        self.ws.send(json.dumps({
            "op": 6,
            "d": {"requestType": request_type, "requestId": rid,
                  "requestData": request_data or {}},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._recv(timeout=0.5)
            if msg is None:
                continue
            op = msg.get("op")
            d = msg.get("d", {})
            if op == 7 and d.get("requestId") == rid:
                status = d.get("requestStatus", {})
                if not status.get("result", False):
                    raise RuntimeError("%s: %s" % (request_type, status.get("comment", "")))
                return d.get("responseData", {})
            if op == 5:
                self._handle_event(d)
        raise TimeoutError("no response for %s" % request_type)

    def drain(self):
        while True:
            msg = self._recv(timeout=0.02)
            if msg is None:
                break
            if msg.get("op") == 5:
                self._handle_event(msg.get("d", {}))

    def _handle_event(self, d):
        et = d.get("eventType")
        if et == "ReplayBufferSaved":
            path = d.get("eventData", {}).get("savedReplayPath")
            if path and self.on_replay_saved:
                self.on_replay_saved(path)
        elif et == "InputVolumeMeters":
            if self.on_mic_levels:
                self.on_mic_levels(d.get("eventData", {}).get("inputs", []))

    def screenshot(self, source_name, width, height):
        try:
            data = self.request("GetSourceScreenshot", {
                "sourceName": source_name,
                "imageFormat": "jpg",
                "imageWidth": width,
                "imageHeight": height,
                "imageCompressionQuality": 80,
            }, timeout=6)
        except Exception:
            return None
        img = data.get("imageData", "")
        if not img.startswith("data:image"):
            return None
        b64 = img.split(",", 1)[1]
        buf = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)

    def save_clip(self, duration):
        """Save a clip of exactly `duration` seconds through Replay Buffer Pro's
        obs-websocket vendor command (plugin 1.8.0+). Returns the vendor object,
        e.g. {'accepted': True, 'durationSeconds': N, 'clamped': bool} or
        {'accepted': False, 'error': '<reason>'}."""
        try:
            data = self.request("CallVendorRequest", {
                "vendorName": "replay-buffer-pro",
                "requestType": "SaveClip",
                "requestData": {"durationSeconds": int(duration)},
            }, timeout=6)
        except Exception as e:
            print("[warn] save_clip %ds request failed: %s" % (duration, e))
            return {"accepted": False, "error": "request-failed"}
        if isinstance(data, dict) and isinstance(data.get("responseData"), dict):
            data = data["responseData"]
        return data

    def replay_buffer_status(self):
        try:
            return self.request("GetReplayBufferStatus", {}, timeout=5).get("outputActive", False)
        except Exception:
            return False

    def start_replay_buffer(self):
        try:
            self.request("StartReplayBuffer", {}, timeout=5)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Scene / overlay helpers (crop box)
    # ------------------------------------------------------------------ #

    def get_current_scene(self):
        try:
            return self.request("GetCurrentProgramScene", {}, timeout=5).get("currentProgramSceneName")
        except Exception:
            return None

    def get_video_settings(self):
        try:
            return self.request("GetVideoSettings", {}, timeout=5)
        except Exception:
            return {}

    def get_scene_items(self, scene):
        try:
            return self.request("GetSceneItemList", {"sceneName": scene}, timeout=6).get("sceneItems", [])
        except Exception:
            return []

    def get_scene_item_transform(self, scene, item_id):
        try:
            return self.request("GetSceneItemTransform", {
                "sceneName": scene, "sceneItemId": item_id}, timeout=6).get("sceneItemTransform", {})
        except Exception:
            return {}

    def create_browser_source(self, scene, name, file_path, w, h):
        try:
            data = self.request("CreateInput", {
                "sceneName": scene, "inputName": name, "inputKind": "browser_source",
                "inputSettings": _browser_settings(file_path, w, h)}, timeout=8)
            return data.get("sceneItemId")
        except Exception as e:
            print("[warn] create browser source failed: %s" % e)
            return None

    def set_input_settings(self, name, settings):
        try:
            self.request("SetInputSettings", {"inputName": name, "inputSettings": settings},
                         timeout=6)
            return True
        except Exception as e:
            print("[warn] set input settings failed: %s" % e)
            return False

    def set_scene_item_transform(self, scene, item_id, transform):
        try:
            self.request("SetSceneItemTransform", {
                "sceneName": scene, "sceneItemId": item_id,
                "sceneItemTransform": transform}, timeout=6)
        except Exception as e:
            print("[warn] set transform failed: %s" % e)

    def set_scene_item_index(self, scene, item_id, index):
        try:
            self.request("SetSceneItemIndex", {
                "sceneName": scene, "sceneItemId": item_id, "sceneItemIndex": index},
                timeout=6)
        except Exception as e:
            print("[warn] set index failed: %s" % e)


# --------------------------------------------------------------------------- #
# Direct screen-region capture (killfeed only)
# --------------------------------------------------------------------------- #

class ScreenCapture:
    """Grabs just the killfeed region from the screen (mss GDI, dxcam optional),
    so we never screenshot the full frame for OCR."""

    def __init__(self, backend="mss"):
        self.backend = backend
        self._mss = None
        self._dx = None
        if backend == "dxcam":
            try:
                import dxcam
                self._dx = dxcam.create(output_color="BGR")
                if self._dx is None:
                    print("[warn] dxcam failed to create; falling back to mss")
                    self.backend = "mss"
            except Exception as e:
                print("[warn] dxcam unavailable (%s); falling back to mss" % e)
                self.backend = "mss"
        if self.backend == "mss":
            try:
                import mss
                factory = getattr(mss, "MSS", None) or mss.mss
                self._mss = factory()
            except Exception as e:
                print("[warn] mss unavailable: %s" % e)
                self._mss = None

    def grab(self, x, y, w, h):
        """Capture the region (x, y, w, h) in screen pixels; return a BGR ndarray
        or None."""
        if self.backend == "dxcam" and self._dx is not None:
            try:
                frame = self._dx.grab(region=(int(x), int(y), int(x + w), int(y + h)))
                return frame
            except Exception:
                return None
        if self._mss is not None:
            try:
                shot = self._mss.grab({"left": int(x), "top": int(y),
                                       "width": int(w), "height": int(h)})
                img = np.array(shot)
                return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            except Exception:
                return None
        return None


# --------------------------------------------------------------------------- #
# Save state machine (spree grouping + dedupe)
# --------------------------------------------------------------------------- #

CROP_OVERLAY_HTML = os.path.join(HERE, "crop_overlay.html")


def _file_url(path):
    from urllib.parse import quote
    return "file:///" + quote(path.replace("\\", "/"))


def _browser_settings(path, w, h):
    """Browser-source settings that reliably load a local HTML file."""
    return {"url": _file_url(path), "local_file": False, "file": "",
            "width": w, "height": h, "fps": 30,
            "shutdown": False, "restart_when_active": True}


def _overlay_html(box_l, box_t, box_w, box_h, crop, cw, ch):
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="1">
<style>
html,body{margin:0;padding:0;width:%dpx;height:%dpx;background:transparent;overflow:hidden;}
#box{position:absolute;left:%.1fpx;top:%.1fpx;width:%.1fpx;height:%.1fpx;border:3px solid #00ff00;box-sizing:border-box;}
#label{position:absolute;left:%.1fpx;top:%.1fpx;transform:translateY(-20px);color:#00ff00;font:bold 14px Consolas,monospace;background:rgba(0,0,0,0.6);padding:1px 5px;white-space:nowrap;}
</style></head><body>
<div id="box"></div>
<div id="label">OCR crop [%d, %d, %d, %d]</div>
</body></html>""" % (cw, ch, box_l, box_t, box_w, box_h,
                     box_l, box_t, crop[0], crop[1], crop[2], crop[3])


def compute_overlay(cfg, obs, crop, source_name):
    """Generate crop_overlay.html mapping `crop` (source pixels) onto the canvas.

    Returns (html_path, canvas_w, canvas_h) or None."""
    scene = obs.get_current_scene()
    if not scene:
        print("[warn] no current scene; cannot map crop box")
        return None
    items = obs.get_scene_items(scene)
    item = next((it for it in items if it.get("sourceName") == source_name), None)
    if item is None:
        sx = sy = 1.0
        px = py = 0.0
        cl = ct = 0
    else:
        item_id = item.get("sceneItemId")
        sw = float(item.get("sourceWidth") or cfg["capture_width"] or 1)
        sh = float(item.get("sourceHeight") or cfg["capture_height"] or 1)
        t = obs.get_scene_item_transform(scene, item_id)
        if t.get("boundsType") == "OBS_BOUNDS_NONE":
            sx = float(t.get("scaleX") or 1.0)
            sy = float(t.get("scaleY") or 1.0)
        else:
            bw = float(t.get("boundsWidth") or 0)
            bh = float(t.get("boundsHeight") or 0)
            sx = (bw / sw) if (bw and sw) else 1.0
            sy = (bh / sh) if (bh and sh) else 1.0
        px = float(t.get("positionX") or 0.0)
        py = float(t.get("positionY") or 0.0)
        cl = int(t.get("cropLeft") or 0)
        ct = int(t.get("cropTop") or 0)

    vs = obs.get_video_settings()
    canvas_w = int(vs.get("baseWidth") or cfg["capture_width"])
    canvas_h = int(vs.get("baseHeight") or cfg["capture_height"])

    x, y, w, h = crop
    box_l = px + (x - cl) * sx
    box_t = py + (y - ct) * sy
    box_w = w * sx
    box_h = h * sy

    html = _overlay_html(box_l, box_t, box_w, box_h, crop, canvas_w, canvas_h)
    with open(CROP_OVERLAY_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    return CROP_OVERLAY_HTML, canvas_w, canvas_h


def cropbox(cfg):
    """Create/update the live crop-box overlay as a browser source in OBS."""
    obs = OBSClient(cfg)
    obs.connect()
    crop = [int(v) for v in cfg["crop"]]
    res = compute_overlay(cfg, obs, crop, cfg["source_name"])
    if not res:
        print("[cropbox] could not resolve scene/source")
        return
    path, canvas_w, canvas_h = res
    scene = obs.get_current_scene()
    items = obs.get_scene_items(scene)
    existing = next((it for it in items
                     if it.get("sourceName") == cfg.get("overlay_source", "Killfeed Crop Box")), None)
    if existing:
        item_id = existing.get("sceneItemId")
        obs.set_input_settings(existing.get("sourceName"),
                               _browser_settings(path, canvas_w, canvas_h))
    else:
        item_id = obs.create_browser_source(scene, cfg.get("overlay_source", "Killfeed Crop Box"),
                                            path, canvas_w, canvas_h)
    if item_id:
        obs.set_scene_item_transform(scene, item_id, {
            "positionX": 0, "positionY": 0, "scaleX": 1, "scaleY": 1,
            "boundsType": "OBS_BOUNDS_STRETCH",
            "boundsWidth": canvas_w, "boundsHeight": canvas_h})
        idx = len(items) if items else 0
        obs.set_scene_item_index(scene, item_id, max(0, idx))
        print("[cropbox] overlay '%s' ready (%s) crop=%s -> %s"
              % (cfg.get("overlay_source", "Killfeed Crop Box"), "updated" if existing else "created",
                 crop, path))
        _verify_overlay(cfg, obs, crop)
    else:
        print("[cropbox] browser source creation failed; HTML written to %s" % path)


def _verify_overlay(cfg, obs, crop):
    """Screenshot the canvas and confirm the green box actually rendered."""
    import time as _t
    _t.sleep(2.0)  # let the browser source load
    try:
        d = obs.request("GetSourceScreenshot", {
            "sourceName": cfg.get("overlay_source", "Killfeed Crop Box"),
            "imageFormat": "png", "imageWidth": 640, "imageHeight": 360}, timeout=8)
    except Exception as e:
        print("[cropbox] verify screenshot failed: %s" % e)
        return
    img = d.get("imageData", "")
    if not img:
        print("[cropbox] verify: no screenshot (browser source likely blank)")
        return
    import base64
    buf = np.frombuffer(base64.b64decode(img.split(",", 1)[1]), dtype=np.uint8)
    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    g = frame[:, :, 1].astype(int)
    r = frame[:, :, 0].astype(int)
    b = frame[:, :, 2].astype(int)
    mask = (g > 150) & (g > r + 60) & (g > b + 60)
    n = int(mask.sum())
    ys, xs = np.where(mask)
    where = "x %d-%d y %d-%d" % (xs.min(), xs.max(), ys.min(), ys.max()) if n else "-"
    print("[cropbox] verify: green pixels=%d (%s) %s" % (n, where, "OK" if n > 50 else "NOT RENDERED"))


def refresh_overlay_html(cfg, obs, crop, source_name):
    """Regenerate the overlay HTML for a new crop (browser source reloads it)."""
    try:
        compute_overlay(cfg, obs, [int(v) for v in crop], source_name)
    except Exception as e:
        print("[warn] overlay refresh failed: %s" % e)

class Saver:
    """One clip per fight.

    The clip is a window pinned to the first event (10s prelude for kills and
    self-deaths, 5s for a lone death). Each additional kill grows it by
    `kill_step` (15s); a death that follows a kill adds `death_extra`; a mic
    that is active during a window adds `mic_extra` once per event. A follow-up
    event that arrives after a clip was already saved triggers a REWRITE: a
    longer clip is saved and the shorter one deleted, so a fight always ends
    with a single clip at its final length.
    """

    def __init__(self, cfg, obs):
        self.cfg = cfg
        self.obs = obs
        self.pending = collections.deque()
        self.lock = threading.Lock()

        self.fight = None
        self.recent_events = []   # [{"enemy_key": str, "sec": float}] for row dedupe
        self._last_trigger = 0.0
        self._deferred = False
        self.mic_hot = False
        self.mic_last_hot = 0.0
        self._rbp_coalesce_gap = cfg.get("rbp_trigger_gap", 3.0)

        self.prelude = cfg.get("prelude", 10)
        self.prelude_death = cfg.get("prelude_death", 3)
        self.prelude_single = cfg.get("prelude_single", 5)
        self.prelude_combo = cfg.get("prelude_combo", 15)
        self.outro = cfg.get("outro", 5)
        self.kill_step = cfg.get("kill_step", 15)
        self.death_extra = cfg.get("death_extra", 5)
        self.mic_extra = cfg.get("mic_extra", 5)
        self.mic_tail = cfg.get("mic_tail", 2.5)
        self.mic_min = cfg.get("mic_min", 3)
        self.watch_gap = cfg.get("watch_gap", 15)
        self.max_clip = cfg.get("max_clip_seconds", 180)
        self.mic_source = cfg.get("mic_source", "MIC")
        self.mic_threshold = cfg.get("mic_threshold", 0.01)

        self.enable_self_deaths = cfg.get("enable_self_deaths", True)
        self.self_deaths_folder = cfg.get("self_deaths_folder", "")
        self.detect_chime = cfg.get("detect_chime", True)
        self.chime_wav = cfg.get("chime_wav") or os.path.join(HERE, "chime.wav")

    def reload_params(self, cfg):
        """Re-read tunables from a freshly loaded config (live, no restart)."""
        self.cfg = cfg
        self._rbp_coalesce_gap = cfg.get("rbp_trigger_gap", 3.0)
        self.prelude = cfg.get("prelude", 10)
        self.prelude_death = cfg.get("prelude_death", 3)
        self.prelude_single = cfg.get("prelude_single", 5)
        self.prelude_combo = cfg.get("prelude_combo", 15)
        self.outro = cfg.get("outro", 5)
        self.kill_step = cfg.get("kill_step", 15)
        self.death_extra = cfg.get("death_extra", 5)
        self.mic_extra = cfg.get("mic_extra", 5)
        self.mic_tail = cfg.get("mic_tail", 2.5)
        self.mic_min = cfg.get("mic_min", 3)
        self.watch_gap = cfg.get("watch_gap", 15)
        self.max_clip = cfg.get("max_clip_seconds", 180)
        self.mic_source = cfg.get("mic_source", "MIC")
        self.mic_threshold = cfg.get("mic_threshold", 0.01)
        self.enable_self_deaths = cfg.get("enable_self_deaths", True)
        self.self_deaths_folder = cfg.get("self_deaths_folder", "")
        self.detect_chime = cfg.get("detect_chime", True)
        self.chime_wav = cfg.get("chime_wav") or os.path.join(HERE, "chime.wav")

    def _play_chime(self):
        if not self.detect_chime or winsound is None:
            return
        try:
            if os.path.exists(self.chime_wav):
                winsound.PlaySound(self.chime_wav,
                                   winsound.SND_FILENAME | winsound.SND_ASYNC
                                   | winsound.SND_NODEFAULT)
            else:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # OBS callbacks
    # ------------------------------------------------------------------ #

    def on_mic_levels(self, inputs):
        for inp in inputs or []:
            if inp.get("inputName") == self.mic_source:
                levels = inp.get("inputLevelsMul") or []
                flat = []
                for ch in levels:
                    if isinstance(ch, (list, tuple)):
                        flat.extend(ch)
                    else:
                        flat.append(ch)
                if flat and max(flat) > self.mic_threshold:
                    self.mic_hot = True
                    self.mic_last_hot = time.time()
                    return

    def on_replay_saved(self, path):
        with self.lock:
            item = self.pending.popleft() if self.pending else None
        if item is None:
            clip_log("[CLIP] manual replay saved (ignored): %s" % path)
            return
        clip_type = item
        if self.fight is not None and not self.fight["finalized"]:
            self.fight["saved"].append(path)
            clip_log("[CLIP] file written | %s (kept until fight finalizes)" % path)
        else:
            clip_log("[CLIP] file written with no open fight: %s" % path)

    # ------------------------------------------------------------------ #
    # Fight state helpers
    # ------------------------------------------------------------------ #

    def _open(self, now, kind):
        if kind == "death":
            self.fight = {
                "start": now - self.prelude_death,
                "end": now + self.outro,
                "kills": 0, "deaths": 1, "self_seen": False, "has_kills": False,
                "mic_armed": True, "saved": [], "saved_once": False,
                "saved_end": None, "watch_until": None,
                "finalize_after_save": False, "finalized": False,
                "t_open": now,
                "you_read": "", "kill_names": [], "death_name": "", "mic_logged": False,
                "death_first": True,
            }
        else:  # kill or self-death anchor (10s prelude, 15s base)
            self.fight = {
                "start": now - (self.prelude_single if kind == "kill" else self.prelude),
                "end": now + self.outro,
                "kills": 1 if kind == "kill" else 0, "deaths": 0,
                "self_seen": kind == "self", "has_kills": kind == "kill",
                "mic_armed": True, "saved": [], "saved_once": False,
                "saved_end": None, "watch_until": None,
                "finalize_after_save": False, "finalized": False,
                "t_open": now,
                "you_read": "", "kill_names": [], "death_name": "", "mic_logged": False,
                "death_first": kind != "kill",
            }

    def _ensure_intro(self):
        """Set the clip's intro from the fight's content.

        Order matters: a death/self-death BEFORE any kill (posthumous trade)
        rewinds to `prelude_combo` (15s). Otherwise a kill-first fight that then
        lost a death uses a kill-count ladder: 1 kill -> `prelude_death` (3s),
        2 kills -> `prelude` (10s), 3+ kills -> `prelude_combo` (15s cap).
        A kill-only fight keeps `prelude_single` (1 kill) / `prelude` (2+).

        `start` is set to the target exactly while the clip is unsaved (it may
        shrink -- e.g. a single kill opens at 5s but a kill+death must be 3s);
        once a clip has been saved we only extend, forcing a rewrite."""
        f = self.fight
        if f is None or not f["has_kills"]:
            return
        if f.get("death_first"):
            target = self.prelude_combo              # died first, then killed -> 15s
        elif f["deaths"] or f["self_seen"]:
            if f["kills"] >= 3:
                target = self.prelude_combo          # 3+ kills -> 15s (cap)
            elif f["kills"] == 2:
                target = self.prelude                # 2 kills -> 10s
            else:
                target = self.prelude_death          # 1 kill + death -> 3s
        elif f["kills"] >= 2:
            target = self.prelude                    # kill-only multi -> 10s
        else:
            target = self.prelude_single             # kill-only single -> 5s
        want = f["t_open"] - target
        if want == f["start"]:
            return
        if not f["saved_once"]:
            f["start"] = want                        # may extend or shrink pre-save
        elif want < f["start"]:
            f["start"] = want
            f["watch_until"] = None

    def _clip_length(self):
        f = self.fight
        return int(round(min(f["end"] - f["start"], self.max_clip)))

    def _classify(self):
        f = self.fight
        if f["has_kills"]:
            return "kill"
        if f["self_seen"]:
            return "self"
        return "death"

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def on_event(self, kind, matched, killer, victim, y):
        now = time.time()
        if kind == "kill":
            self._on_kill(now, killer, victim, y)
        else:
            if kind == "self" and not self.enable_self_deaths:
                kind = "death"
            self._on_death(now, killer, victim, y, kind)

    # ------------------------------------------------------------------ #
    # Row dedupe (same killfeed row re-read while it lingers/scrolled)
    # ------------------------------------------------------------------ #

    def _find_dupe(self, now, enemy_key, kind):
        window = self.cfg.get("dedupe_window", 10)
        ratio = self.cfg.get("dedupe_enemy_ratio", 0.75)
        self.recent_events = [e for e in self.recent_events
                              if now - e["sec"] <= window]
        for e in self.recent_events:
            if enemy_names_match(e["enemy_key"], enemy_key, ratio,
                                 allow_unknown=(e.get("kind") == kind)):
                return e
        return None

    def _remember(self, now, enemy_key, kind):
        self.recent_events.append({"enemy_key": enemy_key, "sec": now,
                                   "kind": kind})

    def _on_kill(self, now, killer, victim, y):
        enemy_key = normalize_key(strip_clan_tag(victim))
        dupe = self._find_dupe(now, enemy_key, "kill")
        if dupe is not None:
            clip_log("[CLIP] IGNORE dup | kill enemy=%r (seen %.1fs ago) -> no new clip"
                  % (victim, now - dupe["sec"]))
            feed_msg("SKIP    duplicate kill '%s' (seen %.1fs ago) -> no new clip"
                     % (victim, now - dupe["sec"]))
            return
        self.mic_hot = False
        self._remember(now, enemy_key, "kill")
        self._play_chime()
        fresh = self.fight is None
        if fresh:
            self._open(now, "kill")
        else:
            self.fight["kills"] += 1
        self.fight["has_kills"] = True
        self.fight["you_read"] = killer
        if victim and victim not in self.fight["kill_names"]:
            self.fight["kill_names"].append(victim)
        self._ensure_intro()   # widen intro before sizing the end
        if fresh:
            clip_log("[CLIP] START kill | %r -> %r | target %ds"
                  % (killer, victim, self._clip_length()))
        else:
            self.fight["end"] = max(self.fight["end"],
                                    self.fight["start"] + self.kill_step * self.fight["kills"])
            clip_log("[CLIP] EXTEND kill x%d%s | %r -> %r | target %ds"
                  % (self.fight["kills"],
                     " (multi-kill)" if self.fight["kills"] > 1 else "",
                     killer, victim, self._clip_length()))
        self.fight["mic_armed"] = True

    def _on_death(self, now, killer, victim, y, kind="death"):
        enemy_key = normalize_key(strip_clan_tag(killer))
        dupe = self._find_dupe(now, enemy_key, kind)
        if dupe is not None:
            clip_log("[CLIP] IGNORE dup | %s enemy=%r (seen %.1fs ago) -> no new clip"
                  % (kind, killer, now - dupe["sec"]))
            feed_msg("SKIP    duplicate %s '%s' (seen %.1fs ago) -> no new clip"
                     % (kind, killer, now - dupe["sec"]))
            return
        self.mic_hot = False
        self._remember(now, enemy_key, kind)
        self._play_chime()
        if self.fight is None:
            self._open(now, kind)
            clip_log("[CLIP] START %s | killer=%r | target %ds"
                  % (kind, killer, self._clip_length()))
        else:
            if kind == "self":
                self.fight["self_seen"] = True
            else:
                self.fight["deaths"] += 1
            if not self.fight["has_kills"]:
                # a death/self arrived before any kill -> posthumous order
                self.fight["death_first"] = True
            if self.fight["has_kills"]:
                # complaining room after a death that follows a kill
                self.fight["end"] = max(self.fight["end"], now + self.outro + self.death_extra)
            else:
                self.fight["end"] = max(self.fight["end"], now + self.outro)
            clip_log("[CLIP] EXTEND %s | killer=%r | target %ds"
                  % (kind, killer, self._clip_length()))
        self.fight["you_read"] = victim
        if kind != "self" and killer:
            self.fight["death_name"] = killer
        self.fight["mic_armed"] = True
        self._ensure_intro()

    # ------------------------------------------------------------------ #
    # Save / rewrite / finalize
    # ------------------------------------------------------------------ #

    def tick(self):
        now = time.time()
        if self._deferred and now - self._last_trigger >= self._rbp_coalesce_gap:
            self._deferred = False
            if self.fight is not None:
                self._trigger_now(self._classify(), self._clip_length())

        f = self.fight
        if f is None or f["finalized"]:
            return

        # MIC: the clip follows your talking and closes `mic_tail` seconds after
        # the last speech. A small `mic_min` floor keeps a brief blip usable.
        if f["kills"] or f["self_seen"] or f["deaths"]:
            if self.mic_last_hot > 0 and now - self.mic_last_hot <= self.mic_tail:
                want = self.mic_last_hot + self.mic_tail
                if want > f["end"]:
                    f["end"] = want
                    if not f.get("mic_logged"):
                        f["mic_logged"] = True
                        clip_log("[CLIP] EXTEND mic (follow, tail %.1fs) | target %ds"
                                 % (self.mic_tail, self._clip_length()))
            if self.mic_last_hot >= f["t_open"]:
                floor_end = f["t_open"] + self.mic_min
                if floor_end > f["end"]:
                    f["end"] = floor_end

        # Hard cap: once the clip would exceed the buffer, clamp and finalize.
        max_end = f["start"] + self.max_clip
        if f["end"] > max_end:
            f["end"] = max_end
            f["finalize_after_save"] = True

        # Growth past an already-saved clip means a rewrite is pending: stop
        # watching the old follow-up window and wait for the new end instead.
        if f["saved_once"] and f["end"] > f["saved_end"]:
            f["watch_until"] = None

        if f["saved_once"]:
            if f["watch_until"] is None:
                if now >= f["end"] or f["finalize_after_save"]:
                    self._request_save()  # rewrite to a longer clip
            elif now >= f["watch_until"] or f["finalize_after_save"]:
                self._finalize()
        else:
            if now >= f["end"] or f["finalize_after_save"]:
                self._request_save()

    def _request_save(self):
        f = self.fight
        if f is None or f["finalized"]:
            return
        duration = self._clip_length()
        if duration <= 0:
            return
        now = time.time()
        if self._last_trigger > 0 and now - self._last_trigger < self._rbp_coalesce_gap:
            self._deferred = True
            clip_log("[CLIP] deferring %s save (%ds) (coalesce guard)" % (
                self._classify(), duration))
            return
        self._trigger_now(self._classify(), duration)

    def _trigger_now(self, clip_type, duration):
        self._last_trigger = time.time()
        resp = self.obs.save_clip(duration)
        if not isinstance(resp, dict) or not resp.get("accepted"):
            error = (resp or {}).get("error", "no-response") if isinstance(resp, dict) else "no-response"
            clip_log("[CLIP] %s save (%ds) REFUSED: %s" % (clip_type, duration, error))
            return False
        with self.lock:
            self.pending.append(clip_type)
        clip_log("[CLIP] saving %s %ds%s" % (
            clip_type, duration, " (clamped)" if resp.get("clamped") else ""))
        f = self.fight
        if f is not None:
            f["saved_once"] = True
            f["saved_end"] = f["end"]
            f["watch_until"] = time.time() + self.watch_gap
        return True

    def _finalize(self):
        f = self.fight
        if f is None:
            return
        clip_type = self._classify()
        self.fight = None
        if not f["saved"]:
            clip_log("[CLIP] closed with no clip saved")
            return
        final_path = f["saved"][-1]
        if len(f["saved"]) > 1:
            clip_log("[CLIP] REWRITE | %d shorter version(s) replaced by the final clip"
                  % (len(f["saved"]) - 1))
        for old in f["saved"][:-1]:
            self._delete_old_clip(old)
        dur = int(round(f["saved_end"] - f["start"])) if f.get("saved_end") else 0
        clip_log("[CLIP] DONE | %d kill(s) / %d death(s) -> %s %ds"
              % (f["kills"], f["deaths"], clip_type, dur))
        info = {"you": f.get("you_read", ""),
                "enemies": [e for e in f.get("kill_names", []) if e],
                "death": f.get("death_name", "")}
        threading.Thread(target=self._move, args=(clip_type, final_path, info),
                         daemon=True).start()

    def _delete_old_clip(self, path):
        try:
            base, ext = os.path.splitext(path)
            for p in (base + "_trimmed" + ext, path):
                if os.path.exists(p):
                    os.remove(p)
        except OSError:
            pass

    def _move(self, clip_type, path, info=None):
        try:
            d = os.path.dirname(path)
            if clip_type == "kill":
                dest = self.cfg["kills_folder"] or os.path.join(d, "Kills")
            elif clip_type == "self":
                dest = self.self_deaths_folder or os.path.join(d, "Self Deaths")
            else:
                dest = self.cfg["deaths_folder"] or os.path.join(d, "Deaths")
            os.makedirs(dest, exist_ok=True)
            src = self._wait_trimmed(path)
            final = src or path
            if not os.path.exists(final):
                print("[warn] replay file missing: %s" % final)
                return
            base = os.path.basename(final)
            target = os.path.join(dest, base)
            i = 1
            while os.path.exists(target):
                stem, ext = os.path.splitext(base)
                target = os.path.join(dest, "%s_%d%s" % (stem, i, ext))
                i += 1
            shutil.move(final, target)
            clip_log("[CLIP] SAVED %s | -> %s" % (clip_type, target))
            self._write_clip_summary(clip_type, info, d, target)
        except Exception as e:
            print("[error] move failed: %s" % e)

    def _write_clip_summary(self, clip_type, info, root, target):
        info = info or {}
        you = info.get("you") or "?"
        if clip_type == "kill":
            label = "KILL   %s -> %s" % (you, ", ".join(info.get("enemies") or []) or "?")
        elif clip_type == "self":
            label = "SELF   %s" % you
        else:
            label = "DEATH  %s -> %s" % (info.get("death") or "?", you)
        try:
            rel = os.path.relpath(target, root)
        except ValueError:
            rel = os.path.basename(target)
        clip_summary("%s  %s   %s" % (time.strftime("%H:%M:%S"), label, rel))

    def _wait_trimmed(self, path):
        base, ext = os.path.splitext(path)
        trimmed = base + "_trimmed" + ext
        for _ in range(20):
            if os.path.exists(trimmed):
                try:
                    s1 = os.path.getsize(trimmed)
                    time.sleep(0.3)
                    if os.path.exists(trimmed) and os.path.getsize(trimmed) == s1:
                        return trimmed
                except OSError:
                    pass
            time.sleep(0.3)
        return None


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def main():
    lock = acquire_single_instance()
    try:
        _main_loop()
    finally:
        try:
            if lock and os.path.exists(lock):
                os.remove(lock)
        except OSError:
            pass


def _main_loop():
    cfg = load_config()
    if RapidOCR is None:
        print("rapidocr-onnxruntime not installed. Run:  python -m pip install rapidocr-onnxruntime")
        sys.exit(1)

    print("starting ocr engine ...")
    ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1, use_cls=False)

    print("connecting to obs-websocket %s:%d ..." % (cfg["obs_host"], cfg["obs_port"]))
    obs = OBSClient(cfg)
    obs.connect()

    print("clip model: prelude=%ds prelude_death=%ds prelude_single=%ds prelude_combo=%ds outro=%ds kill_step=%ds death_extra=%ds mic_extra=%ds" % (
        cfg.get("prelude", 10), cfg.get("prelude_death", 3), cfg.get("prelude_single", 5),
        cfg.get("prelude_combo", 15), cfg.get("outro", 5), cfg.get("kill_step", 15),
        cfg.get("death_extra", 5), cfg.get("mic_extra", 5)))
    print("watch_gap=%ds  max_clip=%ds  mic_tail=%.1fs  mic_min=%ds  mic_source=%r  mic_threshold=%.3f" % (
        cfg.get("watch_gap", 15), cfg.get("max_clip_seconds", 180),
        cfg.get("mic_tail", 2.5), cfg.get("mic_min", 3),
        cfg.get("mic_source", "MIC"), cfg.get("mic_threshold", 0.01)))
    print("folders: kill -> Kills/  self-death -> Self Deaths/  death -> Deaths/")

    ocr_preprocess = cfg.get("ocr_preprocess") or "raw"
    print("reading pattern: %s  match ratio: %.2f" % (
        ocr_preprocess, cfg.get("gamertag_fuzzy_ratio", 0.90)))

    saver = Saver(cfg, obs)
    obs.on_replay_saved = saver.on_replay_saved
    obs.on_mic_levels = saver.on_mic_levels

    if cfg["auto_start_replay"] and not obs.replay_buffer_status():
        print("[info] starting replay buffer")
        obs.start_replay_buffer()

    gt_pairs = [(normalize_key(t), t.strip()) for t in cfg["gamertags"] if t.strip()]
    clean_gts = [p[0] for p in gt_pairs]
    if not clean_gts:
        print("no gamertags configured")
        sys.exit(1)

    crop_box = [int(v) for v in cfg["crop"]]
    killer_max_x = crop_box[2] * KILLER_NAME_MAX_X_RATIO
    white = cfg["white_gate"]
    low = np.array(white["low"], dtype=np.uint8)
    high = np.array(white["high"], dtype=np.uint8)
    min_pixels = white["min_pixels"]

    screen = ScreenCapture(cfg.get("capture_backend", "mss"))
    if screen._mss is None and screen._dx is None:
        print("[warn] direct screen capture unavailable; falling back to OBS screenshots")

    print("watching killfeed (capture=%s crop=%s, tag=%s)" % (
        cfg.get("capture_backend", "mss"), cfg["crop"], ", ".join(p[1] for p in gt_pairs)))

    last_err = 0
    prev_sig = None
    fail_streak = 0
    no_match_tally = 0
    last_event_time = 0.0
    detect_cooldown = float(cfg.get("detect_cooldown", 5.0))
    fuzzy_ratio = cfg.get("gamertag_fuzzy_ratio", 0.90)
    row_dup_ratio = float(cfg.get("row_dup_ratio", 0.85))
    row_memory_sec = float(cfg.get("row_memory_sec", 10))
    min_conf = float(cfg.get("min_ocr_conf", 0.5))
    frame_change_min = float(cfg.get("frame_change_min", 0.02))
    seen_rows = []   # [[signature, last_seen_time], ...] for re-read suppression
    self_keys = cfg.get("self_death_keywords", ())
    last_cfg_check = 0.0
    last_cfg_mtime = 0.0
    prev_mask = None
    feed = None
    prev_feed_rows = set()
    global FEED_LOG
    if cfg.get("feed_log", True):
        feed_path = cfg.get("feed_log_path") or os.path.join(HERE, "killfeed_feed.log")
        try:
            feed = open(feed_path, "a", encoding="utf-8", errors="replace")
            feed.write("\n===== session %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            feed.flush()
            FEED_LOG = feed
            print("[info] killfeed feed log (your events): %s" % feed_path)
        except Exception as e:
            print("[warn] cannot open feed log (%s): %s" % (feed_path, e))
            feed = None

    raw_feed = None
    global RAW_LOG
    raw_path = cfg.get("raw_log_path") or os.path.join(HERE, "killfeed_raw.log")
    try:
        raw_feed = open(raw_path, "a", encoding="utf-8", errors="replace")
        raw_feed.write("\n===== session %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        raw_feed.flush()
        RAW_LOG = raw_feed
        print("[info] killfeed raw log: %s" % raw_path)
    except Exception as e:
        print("[warn] cannot open raw log (%s): %s" % (raw_path, e))
        raw_feed = None

    global CLIP_LOG
    clips_path = cfg.get("clips_log_path") or os.path.join(HERE, "killfeed_clips.log")
    try:
        CLIP_LOG = open(clips_path, "a", encoding="utf-8", errors="replace")
        CLIP_LOG.write("\n===== session %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        CLIP_LOG.flush()
        print("[info] killfeed clips log: %s" % clips_path)
    except Exception as e:
        print("[warn] cannot open clips log (%s): %s" % (clips_path, e))
        CLIP_LOG = None

    if cfg.get("detect_chime", True):
        ensure_chime_wav(cfg.get("chime_wav") or os.path.join(HERE, "chime.wav"))
    hb_path = os.path.join(HERE, "killfeed_saver.hb")
    last_hb = 0.0
    while True:
        _hbt = time.time()
        if _hbt - last_hb >= 2.0:   # heartbeat for the OBS status dock
            last_hb = _hbt
            try:
                with open(hb_path, "w", encoding="utf-8") as _hf:
                    _hf.write("%.0f" % _hbt)
            except Exception:
                pass
        obs.drain()
        saver.tick()

        if obs.ws is None or not obs.ws.connected:
            print("[info] OBS connection lost, attempting reconnect...")
            try:
                obs.connect()
                fail_streak = 0
            except Exception as e:
                fail_streak += 1
                print("[warn] reconnect failed (%s)" % e)
                time.sleep(min(5.0, 1.0 + fail_streak))
                if fail_streak >= 6:
                    print("[info] cannot reach OBS, exiting")
                    break
                continue

        if screen._mss is not None or screen._dx is not None:
            # Direct region grab: only the killfeed box, full-res, no full frame.
            frame = screen.grab(crop_box[0], crop_box[1], crop_box[2], crop_box[3])
            if frame is not None:
                crop = frame
        else:
            # Fallback: OBS websocket screenshot (full frame, then crop).
            frame = obs.screenshot(cfg["source_name"], cfg["capture_width"], cfg["capture_height"])
            if frame is not None:
                if crop_box[0] + crop_box[2] <= frame.shape[1] and \
                   crop_box[1] + crop_box[3] <= frame.shape[0]:
                    crop = frame[crop_box[1]:crop_box[1] + crop_box[3],
                                 crop_box[0]:crop_box[0] + crop_box[2]]
                else:
                    crop = frame
        if frame is None:
            fail_streak += 1
            now = time.time()
            if now - last_err > 10:
                print("[warn] capture failed (game visible on screen?)")
                last_err = now
            sleep = min(cfg["poll_interval"] * (2 ** min(fail_streak, 3)), 5.0)
        else:
            fail_streak = 0
            sleep = cfg["poll_interval"]
            mask = cv2.inRange(crop, low, high)
            bright = cv2.countNonZero(mask)
            now = time.time()

            # live config: apply crop / cooldown / clip params without restart
            if now - last_cfg_check >= 2.0:
                last_cfg_check = now
                try:
                    mtime = os.path.getmtime(CONFIG_PATH)
                except OSError:
                    mtime = 0
                if mtime != last_cfg_mtime:
                    last_cfg_mtime = mtime
                    new_cfg = load_config()
                    new_crop = new_cfg.get("crop")
                    if new_crop and [int(v) for v in new_crop] != crop_box:
                        crop_box[:] = [int(v) for v in new_crop]
                        killer_max_x = crop_box[2] * KILLER_NAME_MAX_X_RATIO
                        refresh_overlay_html(cfg, obs, list(crop_box), cfg["source_name"])
                    try:
                        detect_cooldown = float(new_cfg.get("detect_cooldown", detect_cooldown))
                    except (TypeError, ValueError):
                        pass
                    saver.reload_params(new_cfg)
                    print("[info] live config applied (cooldown=%.1fs crop=%s)"
                          % (detect_cooldown, crop_box))

            # OCR runs only when past the cooldown (paused after an event) AND the
            # crop changed enough since the last OCR (frozen during pause / jitter).
            if bright > min_pixels and (now - last_event_time >= detect_cooldown):
                changed = True
                if prev_mask is not None:
                    diff = cv2.countNonZero(cv2.bitwise_xor(mask, prev_mask))
                    changed = diff > max(4, int(mask.size * frame_change_min))
                prev_mask = mask
                if changed:
                    res, _ = ocr(apply_preprocess(crop, ocr_preprocess))
                    if res:
                        lines = group_ocr_into_lines(res, cfg["line_y_tolerance"])
                        # feed/raw logs: every read, for OCR debugging
                        if feed is not None or raw_feed is not None:
                            raw_text, clean_text = _feed_rows(
                                lines, clean_gts, cfg["enable_deaths"],
                                self_keys, fuzzy_ratio, prev_feed_rows, min_conf)
                            with FEED_LOCK:
                                if feed is not None and clean_text:
                                    feed.write(clean_text)
                                    feed.flush()
                                if raw_feed is not None and raw_text:
                                    raw_feed.write(raw_text)
                                    raw_feed.flush()
                        # events: skip rows we already handled (row-identity dedupe).
                        # A row is consumed only once it yields an event, so a
                        # garbled read can still clear up on the next poll.
                        for line in lines:
                            rs = row_signature(line)
                            if not rs:
                                continue
                            if any(now - t <= row_memory_sec
                                   and SequenceMatcher(None, s, rs).ratio() >= row_dup_ratio
                                   for s, t in seen_rows):
                                continue   # re-read of a row already processed
                            evs = detect_events_from_lines([line], clean_gts,
                                                           cfg["enable_deaths"],
                                                           killer_max_x, self_keys,
                                                           fuzzy_ratio, min_conf)
                            if evs:
                                seen_rows.append((rs, now))
                                last_event_time = now
                                for ev in evs:
                                    saver.on_event(*ev)
                        seen_rows[:] = [e for e in seen_rows if now - e[1] <= row_memory_sec]

        time.sleep(sleep)


def list_sources(cfg):
    obs = OBSClient(cfg)
    obs.connect()
    try:
        data = obs.request("GetInputList", {}, timeout=6)
        for it in data.get("inputs", []):
            if "game_capture" in it.get("inputKind", "") or "capture" in it.get("inputKind", ""):
                print("%-40s %s" % (it.get("inputName"), it.get("inputKind")))
    except Exception as e:
        print("could not list sources: %s" % e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "calibrate", "sources", "cropbox"])
    ap.add_argument("--shots", type=int, default=3)
    args = ap.parse_args()
    cfg = load_config()
    if args.command == "calibrate":
        calibrate(cfg, args.shots)
    elif args.command == "sources":
        list_sources(cfg)
    elif args.command == "cropbox":
        cropbox(cfg)
    else:
        main()
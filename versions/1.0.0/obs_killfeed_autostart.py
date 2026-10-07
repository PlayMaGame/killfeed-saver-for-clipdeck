"""OBS script: auto-starts killfeed_saver.py with OBS and exposes its timing
settings in Tools > Scripts. Loads the saver as a hidden background process,
stops it when OBS exits, and keeps config.json in sync."""

import json
import os
import re
import shutil
import subprocess
import time

try:
    import obspython as obs
except ImportError:
    obs = None


def _script_dir():
    try:
        return os.path.dirname(os.path.abspath(script_path()))
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


SCRIPT_DIR = _script_dir()
SAVER_PATH = os.path.join(SCRIPT_DIR, "killfeed_saver.py")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "killfeed_saver.log")

CREATE_NO_WINDOW = 0x08000000

_proc = None
_enabled = True
_python_override = ""
_first_update = True   # first script_update = load: sync OBS fields from config.json
_event_cb = None


# --------------------------------------------------------------------------- #
# config.json helpers
# --------------------------------------------------------------------------- #

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_config_if_changed(new_cfg):
    try:
        old = load_config()
    except Exception:
        old = {}
    if old == new_cfg:
        return False
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(new_cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return True


def _parse_int_list(text):
    try:
        vals = [int(x) for x in re.split(r"[,;\s]+", (text or "").strip()) if x]
        return vals if vals else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# python discovery (uses the same install OBS itself uses)
# --------------------------------------------------------------------------- #

def _python_from_ini(ini_path):
    try:
        section = None
        with open(ini_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip().lower()
                    continue
                if section != "python" or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip().lower() == "path64bit":
                    return val.strip().strip('"')
    except Exception:
        return None
    return None


def find_python(override=""):
    if override:
        p = override.strip().strip('"')
        if os.path.isfile(p) and p.lower().endswith(".exe"):
            return p
        if os.path.isdir(p):
            cand = os.path.join(p, "python.exe")
            if os.path.isfile(cand):
                return cand
    appdata = os.environ.get("APPDATA", "")
    for name in ("user.ini", "global.ini"):
        ini = os.path.join(appdata, "obs-studio", name)
        if not os.path.isfile(ini):
            continue
        val = _python_from_ini(ini)
        if not val:
            continue
        if os.path.isfile(val) and val.lower().endswith(".exe"):
            return val
        cand = os.path.join(val, "python.exe")
        if os.path.isfile(cand):
            return cand
    for name in ("python", "python3"):
        w = shutil.which(name)
        if w:
            return w
    return None


# --------------------------------------------------------------------------- #
# spawn / stop the saver
# --------------------------------------------------------------------------- #

def spawn_saver():
    global _proc
    if _proc is not None and _proc.poll() is None:
        return True
    if not os.path.isfile(SAVER_PATH):
        print("[killfeed-autostart] saver not found: %s" % SAVER_PATH)
        return False
    py = find_python(_python_override)
    if not py:
        print("[killfeed-autostart] no python found (set path in OBS Python Settings)")
        return False
    try:
        logf = open(LOG_PATH, "w", encoding="utf-8", errors="replace")
    except Exception as e:
        print("[killfeed-autostart] cannot open log: %s" % e)
        logf = None
    try:
        _proc = subprocess.Popen(
            [py, "-X", "utf8", SAVER_PATH],
            cwd=SCRIPT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        if logf:
            logf.write("[obs] spawned at %s\n[obs] python=%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), py))
            logf.flush()
        print("[killfeed-autostart] started saver pid=%d (%s)" % (_proc.pid, py))
        return True
    except Exception as e:
        print("[killfeed-autostart] spawn failed: %s" % e)
        return False
    finally:
        if logf:
            logf.close()


def stop_saver():
    global _proc
    if _proc is None:
        return
    try:
        if _proc.poll() is None:
            _proc.terminate()
            for _ in range(20):
                if _proc.poll() is not None:
                    break
                time.sleep(0.05)
            if _proc.poll() is None:
                _proc.kill()
        print("[killfeed-autostart] stopped saver pid=%s" % _proc.pid)
    except Exception as e:
        print("[killfeed-autostart] stop failed: %s" % e)
    finally:
        _proc = None


def _frontend_ready():
    if obs is None:
        return False
    try:
        return bool(obs.obs_frontend_is_ready())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# OBS script callbacks
# --------------------------------------------------------------------------- #

def script_description():
    return ("Auto-starts killfeed_saver.py when OBS opens (stops it on exit) "
            "and exposes killfeed timing settings. Saver output: "
            "killfeed_saver.log next to this script.")


def script_defaults(settings):
    if obs is None:
        return
    cfg = load_config()

    def istr(key, fallback):
        try:
            return int(cfg.get(key, fallback))
        except Exception:
            return int(fallback)

    def fnum(key, fallback):
        try:
            return float(cfg.get(key, fallback))
        except Exception:
            return float(fallback)

    def ilist(key, fallback):
        v = cfg.get(key)
        if isinstance(v, list) and v:
            try:
                return [int(x) for x in v]
            except Exception:
                pass
        return list(fallback)

    obs.obs_data_set_default_bool(settings, "kf_enable", True)
    obs.obs_data_set_default_string(settings, "kf_python", "")
    obs.obs_data_set_default_string(
        settings, "kf_gamertags",
        ", ".join(str(t) for t in cfg.get("gamertags", ["YOUR_GAMERTAG"])))
    obs.obs_data_set_default_string(
        settings, "kf_source", str(cfg.get("source_name", "Game Capture")))
    crop = cfg.get("crop", [0, 310, 240, 85])
    obs.obs_data_set_default_int(settings, "kf_crop_x", int(crop[0]))
    obs.obs_data_set_default_int(settings, "kf_crop_y", int(crop[1]))
    obs.obs_data_set_default_int(settings, "kf_crop_w", int(crop[2]))
    obs.obs_data_set_default_int(settings, "kf_crop_h", int(crop[3]))
    obs.obs_data_set_default_double(
        settings, "kf_poll", fnum("poll_interval", 0.7))
    obs.obs_data_set_default_int(settings, "kf_prelude", istr("prelude", 10))
    obs.obs_data_set_default_int(settings, "kf_prelude_death", istr("prelude_death", 5))
    obs.obs_data_set_default_int(settings, "kf_prelude_single", istr("prelude_single", 5))
    obs.obs_data_set_default_int(settings, "kf_prelude_combo", istr("prelude_combo", 15))
    obs.obs_data_set_default_int(settings, "kf_outro", istr("outro", 5))
    obs.obs_data_set_default_int(settings, "kf_kill_step", istr("kill_step", 15))
    obs.obs_data_set_default_int(settings, "kf_death_extra", istr("death_extra", 5))
    obs.obs_data_set_default_int(settings, "kf_mic_extra", istr("mic_extra", 5))
    obs.obs_data_set_default_int(settings, "kf_watch_gap", istr("watch_gap", 15))
    obs.obs_data_set_default_int(settings, "kf_max_clip", istr("max_clip_seconds", 180))
    obs.obs_data_set_default_string(
        settings, "kf_mic_source", str(cfg.get("mic_source", "MIC")))
    obs.obs_data_set_default_double(
        settings, "kf_mic_threshold", fnum("mic_threshold", 0.01))
    obs.obs_data_set_default_double(
        settings, "kf_fuzzy_ratio", fnum("gamertag_fuzzy_ratio", 0.90))
    obs.obs_data_set_default_bool(
        settings, "kf_feed_log", bool(cfg.get("feed_log", True)))
    obs.obs_data_set_default_string(
        settings, "kf_feed_log_path", str(cfg.get("feed_log_path", "E:/OBS Recording/killfeed_feed.log")))
    obs.obs_data_set_default_bool(
        settings, "kf_detect_chime", bool(cfg.get("detect_chime", True)))
    obs.obs_data_set_default_string(
        settings, "kf_chime_wav", str(cfg.get("chime_wav", "")))
    obs.obs_data_set_default_double(settings, "kf_rbp_gap", fnum("rbp_trigger_gap", 3.0))
    obs.obs_data_set_default_int(settings, "kf_dedupe", istr("dedupe_window", 12))
    obs.obs_data_set_default_bool(
        settings, "kf_deaths", bool(cfg.get("enable_deaths", True)))
    obs.obs_data_set_default_bool(
        settings, "kf_self_deaths", bool(cfg.get("enable_self_deaths", True)))
    obs.obs_data_set_default_bool(
        settings, "kf_auto_replay", bool(cfg.get("auto_start_replay", True)))


def script_update(settings):
    global _enabled, _python_override, _first_update
    if obs is None:
        return
    _enabled = bool(obs.obs_data_get_bool(settings, "kf_enable"))
    _python_override = (obs.obs_data_get_string(settings, "kf_python") or "").strip()

    cfg = load_config()
    new = dict(cfg)

    if _first_update:
        # Load: config.json is authoritative. Push the crop from config into
        # OBS's fields so stale saved values can't clobber it later, and skip
        # writing config back.
        _first_update = False
        crop = cfg.get("crop", [0, 0, 0, 0])
        if isinstance(crop, list) and len(crop) == 4:
            obs.obs_data_set_int(settings, "kf_crop_x", int(crop[0]))
            obs.obs_data_set_int(settings, "kf_crop_y", int(crop[1]))
            obs.obs_data_set_int(settings, "kf_crop_w", int(crop[2]))
            obs.obs_data_set_int(settings, "kf_crop_h", int(crop[3]))
        if not _enabled:
            stop_saver()
        elif _frontend_ready() and (_proc is None or _proc.poll() is not None):
            spawn_saver()
        return

    tags = [t.strip() for t in re.split(r"[,;]",
            obs.obs_data_get_string(settings, "kf_gamertags") or "") if t.strip()]
    if tags:
        new["gamertags"] = tags

    src = (obs.obs_data_get_string(settings, "kf_source") or "").strip()
    if src:
        new["source_name"] = src

    cxx = obs.obs_data_get_int(settings, "kf_crop_x")
    cxy = obs.obs_data_get_int(settings, "kf_crop_y")
    cxw = obs.obs_data_get_int(settings, "kf_crop_w")
    cxh = obs.obs_data_get_int(settings, "kf_crop_h")
    if 0 <= cxx and 0 <= cxy and 1 <= cxw <= 4000 and 1 <= cxh <= 4000:
        new["crop"] = [cxx, cxy, cxw, cxh]

    poll = obs.obs_data_get_double(settings, "kf_poll")
    if 0.1 <= poll <= 10.0:
        new["poll_interval"] = round(poll, 3)

    prelude = obs.obs_data_get_int(settings, "kf_prelude")
    if 1 <= prelude <= 300:
        new["prelude"] = prelude

    prelude_death = obs.obs_data_get_int(settings, "kf_prelude_death")
    if 0 <= prelude_death <= 300:
        new["prelude_death"] = prelude_death

    prelude_single = obs.obs_data_get_int(settings, "kf_prelude_single")
    if 0 <= prelude_single <= 300:
        new["prelude_single"] = prelude_single

    prelude_combo = obs.obs_data_get_int(settings, "kf_prelude_combo")
    if 0 <= prelude_combo <= 300:
        new["prelude_combo"] = prelude_combo

    outro = obs.obs_data_get_int(settings, "kf_outro")
    if 0 <= outro <= 300:
        new["outro"] = outro

    ks = obs.obs_data_get_int(settings, "kf_kill_step")
    if 5 <= ks <= 300:
        new["kill_step"] = ks

    de = obs.obs_data_get_int(settings, "kf_death_extra")
    if 0 <= de <= 300:
        new["death_extra"] = de

    me = obs.obs_data_get_int(settings, "kf_mic_extra")
    if 0 <= me <= 300:
        new["mic_extra"] = me

    wg = obs.obs_data_get_int(settings, "kf_watch_gap")
    if 1 <= wg <= 300:
        new["watch_gap"] = wg

    mc = obs.obs_data_get_int(settings, "kf_max_clip")
    if 5 <= mc <= 21600:
        new["max_clip_seconds"] = mc

    ms = (obs.obs_data_get_string(settings, "kf_mic_source") or "").strip()
    if ms:
        new["mic_source"] = ms

    mt = obs.obs_data_get_double(settings, "kf_mic_threshold")
    if 0.0 <= mt <= 1.0:
        new["mic_threshold"] = round(mt, 4)

    fr = obs.obs_data_get_double(settings, "kf_fuzzy_ratio")
    if 0.5 <= fr <= 1.0:
        new["gamertag_fuzzy_ratio"] = round(fr, 2)

    new["feed_log"] = bool(obs.obs_data_get_bool(settings, "kf_feed_log"))

    flp = (obs.obs_data_get_string(settings, "kf_feed_log_path") or "").strip()
    if flp:
        new["feed_log_path"] = flp

    new["detect_chime"] = bool(obs.obs_data_get_bool(settings, "kf_detect_chime"))

    cw = (obs.obs_data_get_string(settings, "kf_chime_wav") or "").strip()
    if cw:
        new["chime_wav"] = cw

    gap = obs.obs_data_get_double(settings, "kf_rbp_gap")
    if 0.2 <= gap <= 30.0:
        new["rbp_trigger_gap"] = round(gap, 2)

    ded = obs.obs_data_get_int(settings, "kf_dedupe")
    if 1 <= ded <= 300:
        new["dedupe_window"] = ded

    new["enable_deaths"] = bool(obs.obs_data_get_bool(settings, "kf_deaths"))
    new["enable_self_deaths"] = bool(obs.obs_data_get_bool(settings, "kf_self_deaths"))
    new["auto_start_replay"] = bool(obs.obs_data_get_bool(settings, "kf_auto_replay"))

    if save_config_if_changed(new):
        print("[killfeed-autostart] config.json updated")

    if not _enabled:
        stop_saver()
    elif _frontend_ready() and (_proc is None or _proc.poll() is not None):
        spawn_saver()


def _on_frontend_event(event):
    if obs is None:
        return
    try:
        if event == obs.OBS_FRONTEND_EVENT_FINISHED_LOADING:
            if _enabled:
                spawn_saver()
        elif event == obs.OBS_FRONTEND_EVENT_EXIT:
            stop_saver()
    except Exception as e:
        print("[killfeed-autostart] event error: %s" % e)


def script_load(settings):
    global _event_cb
    if obs is None:
        return
    _enabled = bool(obs.obs_data_get_bool(settings, "kf_enable"))
    _python_override = (obs.obs_data_get_string(settings, "kf_python") or "").strip()
    _event_cb = _on_frontend_event
    obs.obs_frontend_add_event_callback(_event_cb)
    if _enabled and _frontend_ready():
        spawn_saver()


def script_unload():
    global _event_cb
    if obs is not None and _event_cb is not None:
        try:
            obs.obs_frontend_remove_event_callback(_event_cb)
        except Exception:
            pass
        _event_cb = None
    stop_saver()


def _on_restart_clicked(props, prop):
    stop_saver()
    # A manual restart is an explicit start command: respawn even if the
    # auto-start toggle is off. Retry once in case the old process was still
    # releasing its lock.
    for _attempt in (1, 2):
        if spawn_saver():
            return True
        time.sleep(0.5)
    print("[killfeed-autostart] restart FAILED - see errors above")
    return True


def script_properties():
    if obs is None:
        return None
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(
        props, "kf_enable", "Auto-start Killfeed Saver when OBS opens")
    obs.obs_properties_add_text(
        props, "kf_python",
        "Python path (blank = use OBS Python Settings)", obs.OBS_TEXT_DEFAULT)

    obs.obs_properties_add_text(
        props, "kf_gamertags", "Gamertags (comma separated)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(
        props, "kf_source", "OBS source to capture", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_int(props, "kf_crop_x", "Crop X", 0, 4000, 1)
    obs.obs_properties_add_int(props, "kf_crop_y", "Crop Y", 0, 4000, 1)
    obs.obs_properties_add_int(props, "kf_crop_w", "Crop W", 1, 4000, 1)
    obs.obs_properties_add_int(props, "kf_crop_h", "Crop H", 1, 4000, 1)
    obs.obs_properties_add_float_slider(
        props, "kf_poll", "Poll interval (s)", 0.2, 3.0, 0.1)
    obs.obs_properties_add_int_slider(
        props, "kf_prelude", "Prelude before kill / self-death (s)", 1, 60, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_prelude_death", "Prelude before lone death (s)", 0, 60, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_prelude_single", "Prelude before single kill (s)", 0, 60, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_prelude_combo", "Prelude before kill + death/self combo (s)", 0, 60, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_outro", "Base outro after an event (s)", 0, 60, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_kill_step", "Extra seconds per additional kill", 5, 60, 5)
    obs.obs_properties_add_int_slider(
        props, "kf_death_extra", "Extra seconds after a death (complaining)", 0, 30, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_mic_extra", "Extra seconds once per event if mic active", 0, 30, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_watch_gap", "Watch window after a save (s)", 1, 60, 1)
    obs.obs_properties_add_int_slider(
        props, "kf_max_clip", "Max clip length (s, cap)", 30, 360, 5)
    obs.obs_properties_add_text(
        props, "kf_mic_source", "Mic OBS input name (for +mic boost)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_float_slider(
        props, "kf_mic_threshold", "Mic activity threshold (RMS)", 0.0, 0.1, 0.001)
    obs.obs_properties_add_float_slider(
        props, "kf_fuzzy_ratio", "Gamertag fuzzy match (0.67 = ~4/6 chars)", 0.5, 1.0, 0.01)
    obs.obs_properties_add_bool(
        props, "kf_feed_log",
        "Write clean killfeed feed log (appends)")
    obs.obs_properties_add_text(
        props, "kf_feed_log_path", "Feed log path", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_bool(
        props, "kf_detect_chime", "Chime when your gamertag is detected")
    obs.obs_properties_add_text(
        props, "kf_chime_wav", "Chime WAV path (blank = auto chime.wav)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_float_slider(
        props, "kf_rbp_gap", "RBP coalesce gap (s)", 0.5, 10.0, 0.5)
    obs.obs_properties_add_int_slider(
        props, "kf_dedupe", "Dedupe window (s)", 2, 30, 1)
    obs.obs_properties_add_bool(props, "kf_deaths", "Detect deaths")
    obs.obs_properties_add_bool(
        props, "kf_self_deaths",
        "Separate self/environmental deaths into own folder")
    obs.obs_properties_add_bool(props, "kf_auto_replay", "Auto-start replay buffer")
    obs.obs_properties_add_text(
        props, "kf_info",
        "Timing changes apply after clicking Restart below. "
        "Crop / folders / white gate stay in config.json.",
        obs.OBS_TEXT_INFO)
    obs.obs_properties_add_button(
        props, "kf_restart", "Restart Killfeed Saver (apply changes)",
        _on_restart_clicked)
    return props

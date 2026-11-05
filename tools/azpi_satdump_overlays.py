#!/usr/bin/env python3
"""
Turns on SatDump's country borders and city names for every Meteor picture.

SatDump can draw borders and city names on each picture it makes. When it does, it
saves BOTH pictures: the clean one (..._corrected.png) and a labelled one
(..._corrected_map.png). The publisher posts the labelled one on the Captures page
and keeps the clean one for the Footprint map.

This writes the change into SatDump's own user settings file
(~/.config/satdump/settings.json), on top of whatever is already there. SatDump reads
that file over its built-in settings, so the change survives SatDump updates.

What gets switched on, for every Meteor MSU-MR picture type (221, 124, MCIR, ...):
  * country borders, sand coloured
  * city names (capitals and big regional capitals), white
Also adds cloud enhancements made from the 3.8 um infrared (Cloud Top IR, Enhanced IR,
Color Cloud IR), plus two helper pictures the publisher uses for the rain finder.
What stays OFF on purpose:
  * the station marker (QTH): it would pin the station's exact spot on every public picture
  * lat/lon grid and coastlines

Run it on the Pi as the same user that runs SatDump:
  python3 azpi_satdump_overlays.py            make the change (keeps a backup first)
  python3 azpi_satdump_overlays.py --dry-run  show what would change, change nothing
  python3 azpi_satdump_overlays.py --undo     put the settings back from the backup
  options: --cfg PATH (SatDump's built-in satdump_cfg.json, if it isn't found by itself)
"""
import datetime as dt
import glob
import json
import os
import re
import shutil
import sys

ARGS = sys.argv[1:]
DRY = "--dry-run" in ARGS
BORDERS = [0.91, 0.71, 0.36]       # sand, same as the website
CITIES = [1.0, 1.0, 1.0]
CFG_CANDIDATES = ["/usr/share/satdump/satdump_cfg.json", "/usr/local/share/satdump/satdump_cfg.json",
                  "/opt/satdump/share/satdump/satdump_cfg.json"]


def arg(flag):
    return ARGS[ARGS.index(flag) + 1] if flag in ARGS and ARGS.index(flag) + 1 < len(ARGS) else None


def user_settings_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "satdump", "settings.json")


def load_jsonc(path):
    """JSON that may contain // comments (newer SatDump versions have them)."""
    text = open(path, encoding="utf-8").read()
    try:
        return json.loads(text)
    except ValueError:
        pass
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def find_cfg():
    for p in [arg("--cfg")] + CFG_CANDIDATES + glob.glob("/usr/*/share/satdump/satdump_cfg.json"):
        if p and os.path.isfile(p):
            return p
    return None


def merge(base, diff):
    for k, v in diff.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def overlay_keys():
    return {
        "draw_map_overlay": True,
        "draw_cities_overlay": True,
        "draw_shores_overlay": False,
        "draw_latlon_overlay": False,
        "draw_qth_overlay": False,
        "cities_type": 1,               # capitals + regional capitals
        "cities_scale_rank": 3,
        "cities_size": 32,
        "cities_scale": 32,
        "borders_color": BORDERS,
        "cities_color": CITIES,
    }


def extra_composites(cfg_path):
    """Extra Meteor pictures built from the 3.8 um infrared (channel 4), which is the only
    infrared the Meteors are sending at the moment:
      * three cloud enhancements, coloured by cloud-top temperature with SatDump's own colour scales
      * two helper pictures (temperature and raw) the publisher uses to paint the rain finder;
        these are never posted themselves
    A picture type SatDump can't make (missing colour scale, channel not sent) is simply skipped."""
    res = os.path.join(os.path.dirname(cfg_path), "resources")

    def lut_ok(rel):
        return not os.path.isdir(res) or os.path.isfile(os.path.join(res, rel))

    temp = lambda lo, hi: {"cch4": {"type": "temperature", "min": lo, "max": hi}}
    out = {
        "AZ IR38 Temp": {"equation": "cch4", "calib_cfg": temp(180, 330), "geo_correct": True, "autogen": True},
        "AZ IR38 Raw": {"equation": "ch4", "geo_correct": True, "autogen": True},
    }
    for name, lut, lo, hi in (("Cloud Top IR 3.8", "lut/cal/CloudTopIR.png", 183.15, 323.15),
                              ("Enhanced IR 3.8", "lut/cal/EnhancedIR.png", 200, 300),
                              ("Color Cloud IR 3.8", "lut/cal/Multispectral.png", 180, 320)):
        if not lut_ok(lut):
            print(f"  (skipping {name}: SatDump has no {lut})")
            continue
        entry = {"channels": "cch4", "lut": lut, "calib_cfg": temp(lo, hi), "geo_correct": True, "autogen": True}
        entry.update(overlay_keys())
        out[name] = entry
    return out


def main():
    path = user_settings_path()
    if "--undo" in ARGS:
        backups = sorted(glob.glob(path + ".before-overlays-*"))
        if not backups:
            print("No backup found, nothing to undo.")
            return 1
        if not DRY:
            shutil.copyfile(backups[-1], path)
        print(f"Settings put back from {backups[-1]}")
        return 0

    cfg_path = find_cfg()
    if not cfg_path:
        print("Couldn't find SatDump's satdump_cfg.json. Run again with --cfg /path/to/satdump_cfg.json")
        return 1
    cfg = load_jsonc(cfg_path)
    text = open(cfg_path, encoding="utf-8").read()
    if "draw_map_overlay" not in text:
        print(f"This SatDump ({cfg_path}) is older than 1.1.4 and can't draw borders on its pictures.")
        print("Update SatDump first, then run this again.")
        return 1

    instruments = cfg.get("viewer", {}).get("instruments", {})
    targets = {k: v for k, v in instruments.items() if k.startswith("msu_mr")}
    if not targets:
        print("No Meteor MSU-MR picture settings found in SatDump's config. Nothing changed.")
        return 1

    diff = {"viewer": {"instruments": {}}}
    count = 0
    for inst, block in targets.items():
        comps = block.get("rgb_composites", {})
        out = {}
        for name, comp in comps.items():
            entry = overlay_keys()
            if isinstance(comp.get("project"), dict):        # the 'laid flat as a map' version too
                entry["project"] = {"draw_map_overlay": True, "draw_cities_overlay": True,
                                    "draw_qth_overlay": False, "draw_latlon_overlay": False,
                                    "cities_type": 1, "cities_scale_rank": 3,
                                    "borders_color": BORDERS, "cities_color": CITIES}
            out[name] = entry
            count += 1
        if inst == "msu_mr" and "--no-extras" not in ARGS:
            extra = extra_composites(cfg_path)
            out.update(extra)
            print(f"  + extra pictures from the 3.8 um infrared: {', '.join(extra)}")
        if out:
            diff["viewer"]["instruments"][inst] = {"rgb_composites": out}
        print(f"{inst}: {', '.join(comps) or 'no picture types'}")

    current = load_jsonc(path) if os.path.isfile(path) else {}
    updated = merge(json.loads(json.dumps(current)), diff)
    if DRY:
        print(f"\nWould turn on borders + city names for {count} picture types in {path}")
        print(json.dumps(diff, indent=1)[:3000])
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.before-overlays-{stamp}"
    if os.path.isfile(path):
        shutil.copyfile(path, backup)
    else:
        with open(backup, "w") as fh:          # there was no settings file: undo goes back to empty
            fh.write("{}\n")
    with open(path, "w") as fh:
        json.dump(updated, fh, indent=4)
    print(f"\nBorders + city names are on for {count} picture types ({path}).")
    print("The station marker stays off. The next Meteor pass will come out with both a clean and a labelled picture.")
    for stray in (os.path.expanduser("~/settings.json"), os.path.expanduser("~/raspberry-noaa-v2/settings.json")):
        if os.path.isfile(stray):
            print(f"Note: {stray} exists. SatDump uses a settings.json in the folder it runs from instead of "
                  f"this one, so if it runs from there, copy the change into that file too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

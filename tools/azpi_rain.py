#!/usr/bin/env python3
"""
Rain finder for Meteor passes: paints likely rain onto a pass picture, the way the old
raspberry-noaa-v2 / MeteorDemod setup did.

Rain falls from tall clouds whose tops are very cold. SatDump measures the cloud-top
temperature from the infrared channel (helper picture "AZ IR38 Temp", added by
azpi_satdump_overlays.py). Tops colder than about -32 C (241 K) are coloured, from
pale blue (light rain) through green, yellow, red to purple (heaviest), using
MeteorDemod's rain colour scale.

The Meteors are sending the 3.8 um infrared at the moment. At night that is a true
cloud-top temperature. By day sunlight adds to it, so in daylight only clouds that are
also thick and bright in visible light are coloured, and the result is an estimate.

Day passes: rain painted on the natural-colour (221) picture  -> kind "rain_221"
Night passes: rain painted on the infrared picture             -> kind "rain_ir"

Rain colour scale: MeteorDemod resources/rain.bmp
  Copyright (c) 2020 Digitelektro, MIT License (github.com/Digitelektro/MeteorDemod)

Usage (test): azpi_rain.py PASS_FOLDER OUT.png
"""
import glob
import os
import sys

import numpy as np
from PIL import Image

RAIN_COLOURS = [(30,32,29), (26,22,28), (70,49,80), (109,66,132), (130,70,164), (141,82,180), (148,92,183), (157,93,189), (179,76,203), (213,31,224), (243,14,243), (252,60,254), (252,128,255), (253,177,254), (237,164,204), (205,89,107), (182,23,29), (179,3,3), (187,0,0), (201,2,3), (218,1,5), (232,0,5), (243,5,6), (247,27,7), (251,76,7), (250,128,7), (243,156,10), (232,172,12), (222,182,10), (221,198,8), (231,217,6), (244,237,12), (252,250,22), (255,255,69), (255,255,136), (241,247,159), (185,218,125), (103,179,70), (27,149,20), (0,148,0), (4,169,6), (3,186,6), (3,203,5), (0,228,0), (10,247,11), (63,255,64), (131,255,129), (181,255,176), (170,207,211), (101,114,225), (34,43,233), (9,36,235), (0,67,234), (6,119,234), (5,164,231), (3,191,229), (10,217,229), (40,237,233), (109,248,242), (178,251,250)]
LUT = np.array(RAIN_COLOURS, float)

T_MIN, T_MAX = 180.0, 330.0     # the temperature range the helper picture is saved with (K)
T_RAIN = 241.0                  # colder than this: likely rain (same line SatDump uses for cold cloud tops)
T_HEAVY = 200.0                 # this cold or colder: heaviest colour
RAW_RAIN, RAW_HEAVY = 66.0, 40.0   # rough same lines on the raw 3.8 um channel, if no temperature picture
NIGHT_LEVEL = 20                # natural-colour picture darker than this on average = night pass
DAY_CLOUD = 140                 # by day, only clouds at least this bright in visible light


def find(folder, *names):
    """First picture in the pass's products whose name ends with one of `names` (any case)."""
    files = glob.glob(os.path.join(folder, "**", "*.png"), recursive=True)
    if os.environ.get("AZPI_NO_SATDUMP_LABELS"):          # turned pass: SatDump's names are upside down
        files = [f for f in files if not f.lower().endswith("_map.png")]
    for n in names:
        for f in files:
            if os.path.basename(f).lower().endswith(n.lower()):
                return f
    return None


def load(path):
    im = Image.open(path)
    a = np.asarray(im)
    if a.dtype == np.uint16 or im.mode in ("I;16", "I"):
        return a.astype(float) / 65535.0 * 255.0
    if a.ndim == 3:
        a = a[..., :3].mean(2)
    return a.astype(float)


def match_rows(a, H):
    """Picture types can differ by a few rows; stretch to the base picture's height."""
    if a.shape[0] == H:
        return a
    idx = np.clip(np.round(np.arange(H) * (a.shape[0] - 1) / max(1, H - 1)).astype(int), 0, a.shape[0] - 1)
    return a[idx]


def rain_strength(folder, H, W):
    """0..1 per pixel (1 = heaviest), NaN where no rain."""
    t_path = find(folder, "az_ir38_temp_corrected.png")
    r_path = find(folder, "az_ir38_raw_corrected.png")
    if t_path:
        v = match_rows(load(t_path), H)[:, :W]
        T = T_MIN + v / 255.0 * (T_MAX - T_MIN)
        valid = v > 0.5
        s = (T_RAIN - T) / (T_RAIN - T_HEAVY)
    elif r_path:
        v = match_rows(load(r_path), H)[:, :W]
        valid = v > 8
        s = (RAW_RAIN - v) / (RAW_RAIN - RAW_HEAVY)
    else:
        return None
    s = np.where(valid & (s > 0), np.clip(s, 0, 1), np.nan)
    return s


def make(folder, out_path):
    """Make the rain picture for a pass folder. Returns its kind ('rain_221' / 'rain_ir') or None."""
    clean221 = find(folder, "rgb_221_corrected.png")
    if not clean221:
        return None
    vis = np.asarray(Image.open(clean221).convert("RGB")).astype(float)
    night = vis[vis.mean(2) > 2].mean() < NIGHT_LEVEL if (vis.mean(2) > 2).any() else True
    if night:
        base_path = find(folder, "channel_4_equalized_corrected_map.png", "channel_4_equalized_corrected.png")
        kind = "rain_ir"
    else:
        base_path = find(folder, "rgb_221_corrected_map.png", "rgb_221_corrected.png")
        kind = "rain_221"
    if not base_path:
        return None
    base = np.asarray(Image.open(base_path).convert("RGB")).astype(float)
    H, W = base.shape[:2]
    s = rain_strength(folder, H, W)
    if s is None:
        return None
    if s.shape[1] < W:
        s = np.pad(s, ((0, 0), (0, W - s.shape[1])), constant_values=np.nan)
    rain = ~np.isnan(s)
    if not night:
        bright = match_rows(vis[..., 0], H)[:, :W] > DAY_CLOUD      # thick cloud in visible light
        rain &= bright
    idx = np.clip(np.round(2 + (1 - np.nan_to_num(s)) * 57).astype(int), 2, 59)
    out = base.copy()
    out[rain] = out[rain] * 0.12 + LUT[idx[rain]] * 0.88               # keeps borders faintly visible
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(out_path)
    return kind


if __name__ == "__main__":
    k = make(sys.argv[1], sys.argv[2])
    print(k or "no rain picture (missing infrared helper or 221 picture)")

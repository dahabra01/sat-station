#!/usr/bin/env python3
"""
Cloud enhancements for Meteor passes, coloured by cloud-top temperature.

SatDump makes these from its temperature-calibrated infrared, but it can't calibrate the
3.8 um channel the Meteors are sending at the moment. So the station does the colouring
itself: it takes SatDump's raw 3.8 um helper picture ("AZ IR38 Raw"), turns the raw
brightness into an approximate temperature, and colours it with SatDump's own colour
scales (from the Pi's SatDump install):

  cloudtop     Cloud Top IR       lut/cal/CloudTopIR.png     183-323 K
  enhanced_ir  Enhanced IR        lut/cal/EnhancedIR.png     200-300 K
  color_ir     Colour cloud IR    lut/cal/Multispectral.png  180-320 K

Raw to temperature: two anchors read off a real night pass (5 Nov 2026 files): the
coldest storm tops (raw ~40, ~205 K) and the open sea at night (raw ~97, ~298 K).
At night that is close. By day sunlight makes warm ground and low cloud read far too warm,
so on day passes everything warmer than the sea is squeezed into 298-318 K; the coldest,
highest clouds still come out right.

Borders and city names: SatDump also saves the helper with its borders and names drawn on
(..._corrected_map.png). The pixels where the two differ are the borders and names, and
they're copied onto each enhancement, so it carries the same labels as every other picture.

Usage (test): azpi_enhance.py PASS_FOLDER OUT_DIR [LUT_DIR]
"""
import glob
import os
import sys

import numpy as np
from PIL import Image

RAW_COLD, T_COLD = 40.0, 205.0
RAW_SEA, T_SEA = 97.0, 298.0
ENHANCEMENTS = [("cloudtop", "CloudTopIR.png", 183.15, 323.15),
                ("enhanced_ir", "EnhancedIR.png", 200.0, 300.0),
                ("color_ir", "Multispectral.png", 180.0, 320.0)]
LUT_DIRS = ["/usr/share/satdump/resources/lut/cal", "/usr/local/share/satdump/resources/lut/cal",
            "/opt/satdump/share/satdump/resources/lut/cal"]


def find(folder, name):
    for f in glob.glob(os.path.join(folder, "**", "*.png"), recursive=True):
        if os.path.basename(f).lower().endswith(name):
            return f
    return None


def lut_dir(extra=None):
    for d in ([extra] if extra else []) + LUT_DIRS + glob.glob("/usr/*/share/satdump/resources/lut/cal"):
        if d and os.path.isdir(d):
            return d
    return None


def raw_to_kelvin(raw, valid):
    T = T_COLD + (raw - RAW_COLD) * (T_SEA - T_COLD) / (RAW_SEA - RAW_COLD)
    top = np.percentile(raw[valid], 99.5) if valid.any() else RAW_SEA
    if top > 150:
        # daytime: sunlight pushes warm ground and low cloud far past any real temperature;
        # fold everything warmer than the sea into 298-318 K so land and sea stay visible
        warm = raw > RAW_SEA
        T[warm] = T_SEA + (raw[warm] - RAW_SEA) / (top - RAW_SEA) * 20.0
    return T


def load_grey(path):
    a = np.asarray(Image.open(path))
    if a.dtype == np.uint16:
        a = a / 65535.0 * 255.0
    if a.ndim == 3:
        a = a[..., :3].mean(2)
    return a.astype(float)


def make(folder, out_dir, luts=None):
    """Returns [(kind, png_path), ...] for the enhancements it could make."""
    raw_path = find(folder, "az_ir38_raw_corrected.png")
    d = lut_dir(luts)
    if not raw_path or not d:
        return []
    raw = load_grey(raw_path)
    valid = raw > 8                                     # black = no data
    T = raw_to_kelvin(raw, valid)
    labels = None
    map_path = find(folder, "az_ir38_raw_corrected_map.png")
    if map_path:
        lab = np.asarray(Image.open(map_path).convert("RGB")).astype(int)
        if lab.shape[:2] == raw.shape:
            grey = np.repeat(np.clip(raw, 0, 255).astype(int)[..., None], 3, 2)
            labels = (np.abs(lab - grey).max(2) > 24), lab
    made = []
    for kind, lut_name, lo, hi in ENHANCEMENTS:
        lp = os.path.join(d, lut_name)
        if not os.path.isfile(lp):
            continue
        lut = np.asarray(Image.open(lp).convert("RGB"))[0].astype(np.uint8)
        idx = np.clip(((T - lo) / (hi - lo) * (len(lut) - 1)).round().astype(int), 0, len(lut) - 1)
        out = lut[idx]
        out[~valid] = 0
        if labels is not None:
            m, lab = labels
            out[m] = lab[m]
        p = os.path.join(out_dir, f"azpi_{kind}_{os.getpid()}.png")
        Image.fromarray(out).save(p)
        made.append((kind, p))
    return made


if __name__ == "__main__":
    for k, p in make(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None):
        print(k, p)

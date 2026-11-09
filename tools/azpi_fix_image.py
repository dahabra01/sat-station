#!/usr/bin/env python3
"""
Fix image for publishing (Home Base button, Nov 2026).

Meteor pictures come down in strips a few lines tall. When a strip is lost or
garbled, the picture gets a line across it:
  * a BLUE or YELLOW line = only one of the colour channels lost that strip,
    the other channel(s) came through fine
  * a BLACK or speckled grey line = every channel lost it

What this does to each picture of a pass, without making anything up:
  1. Rebuilds a channel that dropped out from the channels that did come
     through on the same spot (each channel is matched to the others using
     the clean lines around it). This is real data from the satellite.
  2. Short gaps where every channel dropped out (a strip or two) are blended
     from the clean lines just above and below.
  3. Anything bigger, and black bands SatDump already left, are filled (Captain's call,
     9 Nov 2026: no black or gaps in a published picture; a hobby project) with a mirror image
     of the good lines just above and below, meeting in the middle. That part isn't real
     weather; the filled picture goes on the Big Picture map too, as the Captain asked.
     AZPI_FIX_FILL_GAPS=0 turns this off and leaves big gaps black.

SatDump's labelled copies (..._map.png) get the same repair with their
borders and city names put back on top. Laid-flat (projected) pictures can't
be repaired line by line, so they are set aside and not posted while a pass
is fixed. The map helper pictures (AZ ... map) are left alone.

Every original is kept next to the fixed picture as NAME.png.orig, so
  azpi_fix_image.py FOLDER          fix a Home Base pass folder (/srv/passes/<name>)
  azpi_fix_image.py FOLDER --undo   put the originals back
  azpi_fix_image.py IN.png OUT.png  repair one picture (testing)
What was done is written to FOLDER/fix.json.
"""
import glob
import json
import os
import sys
import time

import numpy as np
from PIL import Image

try:
    from scipy import ndimage
except ImportError:                       # scipy is on the Pi (footprint map); keep a slow fallback
    ndimage = None

REF_W = 1568              # full MSU-MR swath width; sizes below are for that width
WIN = 31                  # rows of context for "what this spot normally looks like"
DEV = 48                  # brightness jump (0-255) that marks a garbled spot
DARK = 8                  # a channel this dark where its neighbours aren't = dropped out
LIT = 40                  # ...or where another channel on the same spot is at least this bright
RUN = 40                  # a line has to run at least this far across to count
ROW_SHARE = 0.25          # all-channel gaps: only lines at least this much damaged
MAX_BLEND = 16            # all-channel gaps up to this many lines get blended; bigger = black
FIT_ROWS = 48             # half-height of the window used to match channels to each other
MIN_FIT = 400             # clean pixels needed to trust that match


# ---------------------------------------------------------------- helpers
def _vmedian(a, n):
    if ndimage is not None:
        return ndimage.median_filter(a, size=(n, 1), mode="nearest")
    pad = n // 2
    p = np.pad(a, ((pad, pad), (0, 0)), mode="edge")
    return np.median(np.stack([p[i:i + a.shape[0]] for i in range(n)]), axis=0)


def _hrun(mask, n):
    """Keep only marks that are part of a horizontal line at least n pixels long."""
    if n <= 1:
        return mask
    if ndimage is not None:
        lab, _ = ndimage.label(mask, structure=[[0, 0, 0], [1, 1, 1], [0, 0, 0]])
        sizes = np.bincount(lab.ravel())
        keep = sizes >= n
        keep[0] = False
        return keep[lab]
    out = np.zeros_like(mask)
    for y in np.nonzero(mask.any(1))[0]:
        row = mask[y]
        d = np.diff(np.r_[0, row.astype(np.int8), 0])
        for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
            if e - s >= n:
                out[y, s:e] = True
    return out


def _hline(mask, n, share=0.5):
    """Marks that make up a line: at least `share` of every n-pixel stretch is marked. Catches speckled
    noise lines as well as solid ones; the whole stretch is taken."""
    if ndimage is None or n <= 1:
        return _hrun(mask, n)
    dens = ndimage.uniform_filter1d(mask.astype(np.float32), size=n, axis=1, mode="constant")
    core = dens >= share
    return ndimage.maximum_filter1d(core, size=n, axis=1) & (mask | core |
                                                          ndimage.maximum_filter1d(mask, size=3, axis=1))


def _scale(w):
    k = max(w / REF_W, 0.1)
    return max(5, int(round(WIN * k)) | 1), max(8, int(round(RUN * k))), max(2, int(round(MAX_BLEND * k))), \
        max(8, int(round(FIT_ROWS * k)))


def find_damage(f):
    """f: H x W x C float 0-255 -> per-channel damage mask (H x W x C)."""
    h, w, c = f.shape
    win, run, _, _ = _scale(w)
    bad = np.zeros(f.shape, bool)
    for k in range(c):
        ch = f[..., k]
        ref = _vmedian(ch, win)
        garbled = np.abs(ch - ref) > DEV
        dropped = (ch < DARK) & (ref > DARK + 25)
        cross = np.zeros_like(dropped)
        if c > 1:
            # needs no clean neighbours, so it still works in badly hit stretches: the channels of a
            # Meteor picture rise and fall together, so one reading black while another on the same
            # spot is clearly lit means that channel dropped out there
            others = np.delete(f, k, axis=2).max(2)
            cross = (ch < np.maximum(DARK, 0.2 * others)) & (others > LIT)   # far darker than its partner
        # that rule is specific enough to trust on short dashes too
        bad[..., k] = _hline(garbled | dropped | cross, run) | _hline(cross, max(4, run // 4))
    # grow by a line up and down: the edges of a lost strip are usually smeared too
    grown = bad.copy()
    grown[1:] |= bad[:-1]
    grown[:-1] |= bad[1:]
    # a grown edge only counts where it really looks off
    for k in range(c):
        edge = grown[..., k] & ~bad[..., k]
        ref = _vmedian(f[..., k], win)
        grown[..., k] = bad[..., k] | (edge & (np.abs(f[..., k] - ref) > DEV / 2))
    return grown


def cross_fill(f, bad):
    """Rebuild each damaged channel from the channels that came through on the same pixels."""
    h, w, c = f.shape
    out = f.copy()
    done = np.zeros((h, w), bool)
    if c < 2:
        return out, done
    _, _, _, fit_rows = _scale(w)
    clean_all = ~bad.any(2) & (f.max(2) > 2)
    rows_bad = np.nonzero(bad.any((1, 2)))[0]
    if not len(rows_bad):
        return out, done
    band = max(4, fit_rows // 2)
    received = bad.copy()            # predict only from channels that really came through, never from a rebuilt one
    for b0 in range(rows_bad.min() - rows_bad.min() % band, rows_bad.max() + 1, band):
        b1 = min(h, b0 + band)
        sl_bad = received[b0:b1]
        if not sl_bad.any():
            continue
        for k in range(c):
            target = sl_bad[..., k]
            if not target.any():
                continue
            others = [j for j in range(c) if j != k]
            good_others = ~sl_bad[..., others]                           # which other channels are fine there
            for pattern in {tuple(p) for p in good_others[target].tolist()}:
                use = [o for o, g in zip(others, pattern) if g]
                if not use:
                    continue                                              # nothing came through: blend later
                sel = target & np.all(good_others == np.array(pattern), axis=2)
                coef = None
                for half in (fit_rows, fit_rows * 4, h):
                    r0, r1 = max(0, b0 - half), min(h, b1 + half)
                    m = clean_all[r0:r1]
                    if m.sum() < MIN_FIT:
                        continue
                    X = f[r0:r1][m][:, use]
                    y = f[r0:r1][m][:, k]
                    if len(y) > 60000:
                        idx = np.random.default_rng(0).choice(len(y), 60000, replace=False)
                        X, y = X[idx], y[idx]
                    A = np.c_[np.ones(len(y)), X]
                    coef = np.linalg.lstsq(A, y, rcond=None)[0]
                    break
                if coef is None:
                    continue
                Xs = f[b0:b1][sel][:, use]
                pred = np.clip(coef[0] + Xs @ coef[1:], 0, 255)
                blk = out[b0:b1, :, k]
                blk[sel] = pred
                out[b0:b1, :, k] = blk
                d = done[b0:b1]
                d[sel] = True
                done[b0:b1] = d
                bad[b0:b1, :, k] &= ~sel
    return out, done


def blend_fill(f, bad):
    """Spots where every channel is still damaged: short gaps blended from above/below, long ones black."""
    h, w, c = f.shape
    _, _, max_blend, _ = _scale(w)
    out = f.copy()
    allbad = bad.all(2) if c > 1 else bad[..., 0]
    allbad = allbad | (bad.any(2) & (bad.sum(2) >= max(2, c)))
    # only real lines, not odd specks: the line must cover a fair share of the picture's width
    lit = (f.max(2) > 2)
    width_here = np.maximum(lit.sum(1), 1)
    rowshare = allbad.sum(1) / width_here
    allbad &= (rowshare >= ROW_SHARE)[:, None] | _hrun(allbad, max(8, w // 6))
    blended = np.zeros((h, w), bool)
    blacked = np.zeros((h, w), bool)
    # whole damaged lines: blend or black is decided for the line as a whole, so a badly hit band
    # turns cleanly black instead of being smeared into streaks
    gap_row = rowshare >= ROW_SHARE
    d = np.diff(np.r_[0, gap_row.astype(np.int8), 0])
    for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
        sel = allbad[s:e]
        if e - s <= max_blend and s > 0 and e < h:
            a, b = out[s - 1], out[e]
            t = (np.arange(1, e - s + 1) / (e - s + 1))[:, None, None]
            fill = a[None] * (1 - t) + b[None] * t
            blk = out[s:e]
            blk[sel] = fill[sel]
            out[s:e] = blk
            blended[s:e] |= sel
        else:
            blk = out[s:e]
            blk[sel] = 0
            out[s:e] = blk
            blacked[s:e] |= sel
        allbad[s:e] = False
    # what's left: shorter damaged stretches inside otherwise good lines, column by column
    cols = np.nonzero(allbad.any(0))[0]
    for x in cols:
        col = allbad[:, x]
        d = np.diff(np.r_[0, col.astype(np.int8), 0])
        for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
            n = e - s
            if n <= max_blend and s > 0 and e < h:
                a, b = f[s - 1, x], f[e, x]
                t = (np.arange(1, n + 1) / (n + 1))[:, None]
                out[s:e, x] = a * (1 - t) + b * t
                blended[s:e, x] = True
            else:
                out[s:e, x] = 0
                blacked[s:e, x] = True
    return out, blended, blacked


FILL_GAPS = os.environ.get("AZPI_FIX_FILL_GAPS", "1") != "0"   # 0 = leave big gaps black as before
EDGE_FILL = 0.06          # black bands at the very top/bottom: filled if no taller than this share


HOLE = 30                 # all channels under this = nothing there (SatDump's gaps are 0, edges smear to ~20)


def fill_gaps(f, blacked):
    """Captain's call, 9 Nov 2026: no black bands or gaps in a published picture (hobby project;
    filled pictures go on the Big Picture map too). Every band where nothing came through (ours,
    or already black from SatDump) is filled with a mirror image of the good lines just above and
    just below it, meeting in the middle with a soft join. Only fully good lines are copied."""
    h, w, c = f.shape
    out = f.copy()
    _, run, _, _ = _scale(w)
    dark = f.max(2) < HOLE
    lit_rows = (~dark).sum(1)
    big = lit_rows > 0.2 * w
    lit_w = max(1, int(np.median(lit_rows[big]))) if big.any() else w
    in_pic = lit_rows > 0.2 * lit_w
    hole = blacked | (_hline(dark, run) & in_pic[:, None])          # black stretches inside the picture
    if ndimage is not None:                                          # thin dim lines along old band edges
        rowm = f.mean((1, 2))
        local = ndimage.median_filter(rowm, size=9, mode="nearest")
        hole |= ((rowm < 0.6 * local) & in_pic)[:, None]
    lum = f.mean(2)                                                  # dim streaks inside good lines
    hole |= _hline(lum < 0.65 * _vmedian(lum, _scale(w)[0]), run) & in_pic[:, None]
    grow = hole.copy()                                               # and the smeared line either side
    grow[1:] |= hole[:-1] & dark[1:]
    grow[:-1] |= hole[1:] & dark[:-1]
    hole = grow
    gap = (hole | dark).sum(1) >= 0.6 * lit_w                        # whole lines to fill
    good = np.nonzero(~gap & ~hole.any(1) & in_pic)[0]
    if not len(good):
        return out, np.zeros((h, w), bool)
    first, last = good.min(), good.max()
    filled = np.zeros((h, w), bool)
    d = np.diff(np.r_[0, gap.astype(np.int8), 0])
    for s_, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
        n = e - s_
        above = good[good < s_][::-1]                                # nearest good lines, outward
        below = good[good >= e]
        if (not len(above) or not len(below)) and n > EDGE_FILL * h:
            continue                                                 # long run off the end: night/edge
        i = np.arange(n)
        A = out[above[np.minimum(i, len(above) - 1)]] if len(above) else None
        B = out[below[np.minimum(n - 1 - i, len(below) - 1)]] if len(below) else None
        if A is not None and B is not None:
            mid, soft = (n - 1) / 2.0, max(3.0, n / 6.0)
            wb = np.clip((i - mid) / soft + 0.5, 0, 1)[:, None, None]
            band = A * (1 - wb) + B * wb
        else:
            band = A if A is not None else B
        m = hole[s_:e] | dark[s_:e] | gap[s_:e, None]
        tgt = out[s_:e]
        tgt[m] = band[m]
        out[s_:e] = tgt
        filled[s_:e] |= m
    # short black pieces inside good lines: fill each column between the nearest good pixels
    rest = hole & ~filled
    rest[:first] = False
    rest[last + 1:] = False
    for x in np.nonzero(rest.any(0))[0]:
        col = rest[:, x]
        dd = np.diff(np.r_[0, col.astype(np.int8), 0])
        for s_, e in zip(np.nonzero(dd == 1)[0], np.nonzero(dd == -1)[0]):
            a_ = out[s_ - 1, x] if s_ > 0 else out[min(e, h - 1), x]
            b_ = out[e, x] if e < h else a_
            t = (np.arange(1, e - s_ + 1) / (e - s_ + 1))[:, None]
            out[s_:e, x] = a_ * (1 - t) + b_ * t
            filled[s_:e, x] = True
    return out, filled


def repair(arr):
    """uint8/uint16 picture array -> (fixed array same type, stats dict)."""
    src = np.asarray(arr)
    mono = src.ndim == 2
    alpha = None
    if not mono and src.shape[2] == 4:
        alpha, src = src[..., 3], src[..., :3]
    peak = 65535.0 if src.dtype == np.uint16 else 255.0
    f = src.astype(np.float32) * (255.0 / peak)
    if mono:
        f = f[..., None]
    if f.shape[2] == 3 and np.abs(f[..., 0] - f[..., 1]).mean() < 0.5 and np.abs(f[..., 0] - f[..., 2]).mean() < 0.5:
        # grey picture stored as colour: treat as one channel
        g = f[..., :1]
        g, blended, blacked = blend_fill(g, find_damage(g))
        f = np.repeat(g, 3, axis=2)
        done = np.zeros(blended.shape, bool)
    else:
        bad = find_damage(f)
        f, done = cross_fill(f, bad)
        f, blended, blacked = blend_fill(f, bad)
    cloned = np.zeros(blacked.shape, bool)
    if FILL_GAPS:
        f, cloned = fill_gaps(f, blacked)
        for _ in range(2):                              # anything a first pass couldn't reach
            f, more = fill_gaps(f, np.zeros_like(blacked))
            if not more.any():
                break
            cloned |= more
        blacked = blacked & ~cloned
    lit = max(int((src.reshape(src.shape[0], src.shape[1], -1).max(2) > 0).sum()), 1)
    stats = {"rebuilt_pct": round(100.0 * done.sum() / lit, 1),
             "blended_pct": round(100.0 * blended.sum() / lit, 1),
             "blacked_pct": round(100.0 * blacked.sum() / lit, 1),
             "cloned_pct": round(100.0 * cloned.sum() / lit, 1)}
    stats["blended_pct"] = round(stats["blended_pct"] + stats["cloned_pct"], 1)   # Home Base shows one "blended" figure
    out = np.clip(np.rint(f * (peak / 255.0)), 0, peak).astype(src.dtype)
    if mono:
        out = out[..., 0]
    if alpha is not None:
        out = np.dstack([out, alpha])
    return out, stats


def _open(path):
    im = Image.open(path)
    im.load()
    if im.mode == "P":
        im = im.convert("RGB")
    if im.mode == "LA":
        im = im.convert("RGBA")
    if im.mode == "I;16" or im.mode == "I":
        return np.asarray(im).astype(np.uint16), im.mode
    return np.asarray(im), im.mode


def _save(arr, path, mode_hint=None):
    if arr.dtype == np.uint16:
        Image.fromarray(arr.astype(np.uint16)).save(path)
    else:
        Image.fromarray(arr).save(path)


# ---------------------------------------------------------------- a pass folder
def _pictures(folder):
    for f in sorted(glob.glob(os.path.join(folder, "products", "**", "*.png"), recursive=True)):
        n = os.path.basename(f).lower()
        if "az_" in n and "map_projected" in n:
            continue                                  # map helper: the Big Picture map does its own checks
        yield f


def fix_folder(folder, log=print):
    folder = os.path.realpath(folder)
    if os.path.exists(os.path.join(folder, "fix.json")):
        undo_folder(folder, log=lambda *_: None)      # always repair from the originals
    t0 = time.time()
    pics = list(_pictures(folder))
    labelled = {p for p in pics if p.lower().endswith("_map.png")}
    clean = [p for p in pics if p not in labelled]
    fixed, set_aside, totals = [], [], {"rebuilt_pct": 0.0, "blended_pct": 0.0, "blacked_pct": 0.0}
    main_stats = None
    done_clean = {}
    for p in clean:
        n = os.path.basename(p).lower()
        if "projected" in n:
            os.replace(p, p + ".orig")                # laid flat: can't be repaired line by line
            set_aside.append(os.path.relpath(p, folder))
            continue
        try:
            arr, _ = _open(p)
            out, st = repair(arr)
        except Exception as e:
            log(f"  skipped {os.path.basename(p)} ({e.__class__.__name__}: {e})")
            continue
        os.replace(p, p + ".orig")
        _save(out, p)
        done_clean[p] = (arr, out)
        fixed.append(os.path.relpath(p, folder))
        if "rgb_221_corrected" in n or main_stats is None:
            main_stats = st
        log(f"  {os.path.basename(p)}: {st['rebuilt_pct']}% rebuilt from the other channels, "
            f"{st['blended_pct']}% blended, {st['blacked_pct']}% left black")
    for p in labelled:
        twin = p[:-8] + ".png"                       # X_map.png -> X.png
        if "projected" in os.path.basename(p).lower():
            os.replace(p, p + ".orig")
            set_aside.append(os.path.relpath(p, folder))
            continue
        if twin not in done_clean:
            continue
        try:
            orig_clean, new_clean = done_clean[twin]
            lab, _ = _open(p)
            if lab.shape != orig_clean.shape:
                continue
            marks = np.any(lab != orig_clean, axis=-1) if lab.ndim == 3 else lab != orig_clean
            out = new_clean.copy()
            out[marks] = lab[marks]                  # borders and names back on top
            os.replace(p, p + ".orig")
            _save(out, p)
            fixed.append(os.path.relpath(p, folder))
        except Exception as e:
            log(f"  skipped {os.path.basename(p)} ({e.__class__.__name__}: {e})")
    info = {"fixed_at": int(time.time()), "pictures": fixed, "set_aside": set_aside,
            "main": main_stats or totals, "seconds": round(time.time() - t0, 1)}
    with open(os.path.join(folder, "fix.json"), "w") as fh:
        json.dump(info, fh, indent=1)
    return info


def undo_folder(folder, log=print):
    folder = os.path.realpath(folder)
    n = 0
    for o in glob.glob(os.path.join(folder, "products", "**", "*.png.orig"), recursive=True):
        os.replace(o, o[:-5])
        n += 1
    try:
        os.remove(os.path.join(folder, "fix.json"))
    except FileNotFoundError:
        pass
    log(f"put back {n} original picture(s)")
    return n


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) == 2 and a[0].lower().endswith((".png", ".jpg")):
        arr, _ = _open(a[0])
        out, st = repair(arr)
        _save(out, a[1])
        print(json.dumps(st))
    elif len(a) == 1 and os.path.isdir(a[0]):
        if "--undo" in sys.argv:
            undo_folder(a[0])
        else:
            print(json.dumps(fix_folder(a[0]), indent=1))
    else:
        print(__doc__)
        sys.exit(1)

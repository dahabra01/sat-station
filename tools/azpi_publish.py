#!/usr/bin/env python3
"""
AZPi -> Capt. Abu Zaid Sat Station website publisher.

Runs on the satellite Pi every 15 minutes (from cron). It only does anything
when the Pi is on the home Wi-Fi, so it never uses mobile data in the field.

What it does:
  1. Finds every finished Meteor pass in /srv/images.
  2. Reads the station log to work out each pass's peak signal and how long
     it stayed locked, and the pass database for its height and direction.
  3. Picks the best clean pass of each Dubai day (longest lock wins).
  4. Shrinks that pass's pictures to web size, writes captures/captures.json,
     and pushes it all to the sat-station GitHub repo. GitHub Pages then
     updates the Captures page by itself.

Options:
  --dry-run       show what would happen, change nothing
  --any-network   skip the home Wi-Fi check (testing only)
  --no-push       do everything except git commit/push (testing only)
"""
import datetime as dt
import fcntl
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

# ---------------------------------------------------------------- settings
HOME_IP = "192.168.1.200"          # the Pi's address on home Wi-Fi
IMG_DIR = os.environ.get("AZPI_IMG_DIR", "/srv/images")
LOGS = [os.environ.get("AZPI_LOG", "/var/log/raspberry-noaa-v2/output.log")]
LOGS += [LOGS[0] + ".1"]
DB = os.environ.get("AZPI_DB", os.path.expanduser("~/raspberry-noaa-v2/db/panel.db"))
SITE = os.environ.get("AZPI_SITE", os.path.expanduser("~/sat-station-site"))
OUT_DIR = "captures"                # folder inside the website repo
MIN_LOCK_S = 60                     # a pass needs at least 1 minute of lock to count as clean
SETTLE_S = 300                      # ignore passes whose files changed in the last 5 minutes
IMG_W = 800                         # web picture width
THUMB_W = 480                       # archive card picture width
JPEG_Q = 76
MAX_BYTES = 900 * 1024 * 1024       # keep the captures folder under ~900 MB; oldest days go first
DEPLOY_KEY = os.path.expanduser("~/.ssh/sat_site_deploy")
DUBAI = dt.timezone(dt.timedelta(hours=4))

ARGS = set(sys.argv[1:])
DRY = "--dry-run" in ARGS
ANY_NET = "--any-network" in ARGS
NO_PUSH = "--no-push" in ARGS

FILE_RE = re.compile(r"^(METEOR-M2-\d)-(\d{8})-(\d{6})-(.+)\.(jpg|png)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")   # colour codes satdump writes into the log
LOG_RE = re.compile(
    r"^\[(\d\d:\d\d:\d\d) - (\d\d/\d\d/\d{4})\] \(I\) Progress.*?"
    r"(?:Peak SNR: ([\d.]+)dB|Viterbi : (SYNCED|NOSYNC))"
)


def say(msg):
    print(dt.datetime.now(DUBAI).strftime("%d-%m %H:%M"), msg, flush=True)


# ---------------------------------------------------------------- network
def at_home():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return HOME_IP in out.split()


# ---------------------------------------------------------------- find passes
def find_passes():
    passes = {}
    for path in glob.glob(os.path.join(IMG_DIR, "METEOR-M2-*")):
        m = FILE_RE.match(os.path.basename(path))
        if not m or not os.path.isfile(path):
            continue
        sat, day, hms, rest, ext = m.groups()
        pid = f"{sat}-{day}-{hms}"
        p = passes.setdefault(pid, {
            "id": pid,
            "sat_code": sat,
            "start": dt.datetime.strptime(day + hms, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc),
            "files": [],
            "newest": 0,
        })
        p["files"].append(path)
        p["newest"] = max(p["newest"], os.path.getmtime(path))
    now = dt.datetime.now().timestamp()
    return [p for p in passes.values() if now - p["newest"] > SETTLE_S]


def classify(path):
    """'...-spread_rain_221.jpg' -> ('rain_221', 'spread'); polar -> ('polar', None)"""
    rest = FILE_RE.match(os.path.basename(path)).group(4)
    if rest.startswith("polar"):
        return ("polar", None)
    for proj in ("spread", "equidistant"):
        if rest.startswith(proj + "_"):
            return (rest[len(proj) + 1:], proj)
    return (rest, "other")


# ---------------------------------------------------------------- signal stats from the log
_log_cache = None


def log_lines():
    global _log_cache
    if _log_cache is None:
        rows = []
        for f in reversed(LOGS):
            if not os.path.exists(f):
                continue
            with open(f, errors="ignore") as fh:
                for line in fh:
                    m = LOG_RE.match(ANSI_RE.sub("", line))
                    if not m:
                        continue
                    t, d, snr, vit = m.groups()
                    when = dt.datetime.strptime(d + " " + t, "%d/%m/%Y %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                    rows.append((when, float(snr) if snr else None, vit))
        _log_cache = rows
    return _log_cache


def signal_stats(start):
    end = start + dt.timedelta(minutes=22)
    peak, states = 0.0, []
    for when, snr, vit in log_lines():
        if start <= when <= end:
            if snr is not None:
                peak = max(peak, snr)
            if vit:
                states.append(1 if vit == "SYNCED" else 0)
    timeline = []                      # [[locked 0/1, seconds], ...] in order, 10 s per log line
    for s in states:
        if timeline and timeline[-1][0] == s:
            timeline[-1][1] += 10
        else:
            timeline.append([s, 10])
    return {"peak_snr": round(peak, 1), "lock_s": 10 * sum(states), "timeline": timeline}


# ---------------------------------------------------------------- pass geometry from the database
def pass_geometry(start):
    info = {}
    if not os.path.exists(DB):
        return info
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        t = int(start.timestamp())
        row = con.execute(
            "SELECT * FROM predict_passes WHERE pass_start BETWEEN ? AND ? "
            "ORDER BY ABS(pass_start - ?) LIMIT 1", (t - 300, t + 300, t)).fetchone()
        con.close()
    except Exception as e:
        say(f"note: could not read pass database ({e})")
        return info
    if not row:
        return info
    keys = row.keys()
    pick = {"max_elev": "max_elev", "aos_az": "pass_start_azimuth",
            "max_az": "azimuth_at_max", "direction": "direction", "pass_end": "pass_end"}
    for out, col in pick.items():
        if col in keys and row[col] is not None:
            info[out] = row[col]
    for k in ("max_elev", "aos_az", "max_az"):
        if k in info:
            try:
                info[k] = round(float(info[k]))
            except ValueError:
                info.pop(k)
    if "pass_end" in info:
        try:
            info["pass_end"] = dt.datetime.fromtimestamp(int(info.pop("pass_end")), dt.timezone.utc).isoformat()
        except Exception:
            info.pop("pass_end", None)
    if isinstance(info.get("direction"), str):
        info["direction"] = info["direction"].strip().lower()
    return info


# ---------------------------------------------------------------- pictures
def shrink(src, dst, width):
    if DRY:
        return
    try:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        im.save(dst, "JPEG", quality=JPEG_Q, optimize=True, progressive=True)
    except ImportError:
        subprocess.run(["convert", src, "-resize", f"{width}x>", "-strip",
                        "-quality", str(JPEG_Q), "-interlace", "Plane", dst], check=True)


KIND_ORDER = ["221", "224", "thermal", "67", "rain_221", "rain_224", "rain_67"]


def kind_rank(k):
    return KIND_ORDER.index(k) if k in KIND_ORDER else len(KIND_ORDER)


def build_entry(p, stats, geo):
    folder = os.path.join(OUT_DIR, p["id"])
    target = os.path.join(SITE, folder)
    if not DRY:
        os.makedirs(target, exist_ok=True)
    images, polar = [], None
    for src in sorted(p["files"]):
        kind, proj = classify(src)
        if kind == "polar":
            polar = f"{folder}/polar.png"
            if not DRY:
                shutil.copyfile(src, os.path.join(SITE, polar))
            continue
        if proj not in ("spread", "equidistant"):
            continue
        name = f"{proj}_{kind}.jpg"
        shrink(src, os.path.join(target, name), IMG_W)
        images.append({"kind": kind, "proj": proj, "file": f"{folder}/{name}"})
    images.sort(key=lambda i: (kind_rank(i["kind"]), i["proj"] != "spread"))
    main = next((i for i in images if i["proj"] == "spread"), images[0] if images else None)
    thumb = None
    if main:
        thumb = f"{folder}/thumb.jpg"
        src = next(f for f in p["files"] if classify(f) == (main["kind"], main["proj"]))
        shrink(src, os.path.join(SITE, thumb), THUMB_W)
    num = p["sat_code"].split("-")[-1]
    local = p["start"].astimezone(DUBAI)
    entry = {
        "id": p["id"],
        "sat": f"Meteor-M N2-{num}",
        "day": local.strftime("%Y-%m-%d"),
        "start_utc": p["start"].isoformat(),
        "start_local": local.strftime("%H:%M"),
        "thumb": thumb,
        "polar": polar,
        "images": images,
    }
    entry.update(stats)
    entry.update(geo)
    return entry


# ---------------------------------------------------------------- manifest
def load_manifest(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {"passes": []}


def folder_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def git(*args):
    env = dict(os.environ)
    if os.path.exists(DEPLOY_KEY):
        env["GIT_SSH_COMMAND"] = f"ssh -i {DEPLOY_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return subprocess.run(["git", "-C", SITE, *args], capture_output=True, text=True, env=env, timeout=300)


# ---------------------------------------------------------------- main
def main():
    if not ANY_NET and not at_home():
        say("not on home Wi-Fi, waiting")
        return
    if not os.path.isdir(os.path.join(SITE, ".git")):
        say(f"website repo not found at {SITE}, nothing to do")
        return
    if not DRY and not NO_PUSH:
        r = git("pull", "--rebase", "--quiet")
        if r.returncode:
            say("git pull failed: " + r.stderr.strip())
            return

    man_path = os.path.join(SITE, OUT_DIR, "captures.json")
    manifest = load_manifest(man_path)
    by_day = {e["day"]: e for e in manifest.get("passes", [])}

    best = {}
    for p in find_passes():
        stats = signal_stats(p["start"])
        if stats["lock_s"] < MIN_LOCK_S:
            continue
        day = p["start"].astimezone(DUBAI).strftime("%Y-%m-%d")
        score = (stats["lock_s"], stats["peak_snr"])
        if day not in best or score > best[day][0]:
            best[day] = (score, p, stats)

    changed = []
    for day, (score, p, stats) in sorted(best.items()):
        old = by_day.get(day)
        if old and old["id"] == p["id"]:
            continue
        if old and (old.get("lock_s", 0), old.get("peak_snr", 0)) >= score:
            continue
        if old and not DRY:
            shutil.rmtree(os.path.join(SITE, OUT_DIR, old["id"]), ignore_errors=True)
        say(f"{'would post' if DRY else 'posting'} {p['id']} for {day}: "
            f"locked {stats['lock_s']} s, peak {stats['peak_snr']} dB"
            + (f" (replaces {old['id']})" if old else ""))
        by_day[day] = build_entry(p, stats, pass_geometry(p["start"]))
        changed.append(p["id"])

    if not changed:
        say("nothing new to post")
        return
    if DRY:
        return

    passes = sorted(by_day.values(), key=lambda e: e["start_utc"], reverse=True)
    out_root = os.path.join(SITE, OUT_DIR)
    while len(passes) > 1 and folder_bytes(out_root) > MAX_BYTES:
        gone = passes.pop()
        shutil.rmtree(os.path.join(out_root, gone["id"]), ignore_errors=True)
        say(f"trimmed oldest capture {gone['id']} to stay under the size limit")

    manifest = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "passes": passes}
    os.makedirs(out_root, exist_ok=True)
    with open(man_path, "w") as fh:
        json.dump(manifest, fh, indent=1)

    if NO_PUSH:
        say("--no-push: files written, not sent")
        return
    git("add", "-A", OUT_DIR)
    r = git("commit", "-q", "-m", "New capture: " + ", ".join(changed))
    if r.returncode:
        say("git commit failed: " + (r.stderr or r.stdout).strip())
        return
    r = git("push", "-q")
    say("sent to GitHub" if r.returncode == 0 else "git push failed: " + r.stderr.strip())


if __name__ == "__main__":
    lock = open("/tmp/azpi_publish.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    main()

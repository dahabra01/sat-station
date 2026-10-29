#!/usr/bin/env python3
"""
AZPi -> Capt. Abu Zaid Sat Station website publisher.

Runs on the satellite Pi every 15 minutes (from cron). It only does anything
when the Pi is on the home Wi-Fi, so it never uses mobile data in the field.

What it does:
  1. Notices when the Pi comes back home after a field trip (it keeps a note
     of when it was last away).
  2. Finds every finished Meteor pass recorded since the last review, reads the
     station log for each pass's peak signal and lock time, and the pass
     database for its height and direction.
  3. Picks the best clean pass of each Dubai day (longest lock wins).
  4. ASKS FIRST: sends a message to the Captain's phone (ntfy app) listing what
     was caught, with a preview picture and two buttons, "Post it" and
     "Not now". Nothing goes public until he taps "Post it".
  5. On "Post it": shrinks every picture of that pass to web size, stamps the
     watermark, writes captures/captures.json and pushes it all to the
     sat-station GitHub repo. GitHub Pages then updates the Captures page.
     A second message confirms it with a link.
  If a field trip caught nothing usable, it says so instead of asking.

The ntfy channel name is read from ~/azpi_dongle_watch.py on the Pi, so it
never appears in this public repo. Answers come back on "<channel>-reply".

Options:
  --dry-run       show what would happen, change nothing, send nothing
  --any-network   skip the home Wi-Fi check (testing only)
  --no-push       do everything except git commit/push (testing only)
  --ask-off       old behaviour: post the best pass of each day without asking
  --status        print the review state and exit
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
import secrets
import sys
import time
import urllib.request

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
WATERMARK = "\u00a9 Capt. Abu Zaid Sat Station \u00b7 sat.dahabra.com"
MARK_VERSION = 1                    # bump to re-stamp every posted pass with a new watermark
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"]
DUBAI = dt.timezone(dt.timedelta(hours=4))

# ---- ask-first settings
ASK_FIRST = True                    # False = post without asking (same as --ask-off)
STATE = os.environ.get("AZPI_STATE", os.path.expanduser("~/azpi_publish_state.json"))
NTFY_BASE = os.environ.get("AZPI_NTFY_BASE", "https://ntfy.sh")
NTFY_SRC = os.environ.get("AZPI_NTFY_SRC", os.path.expanduser("~/azpi_dongle_watch.py"))
WAIT_S = int(os.environ.get("AZPI_WAIT_S", 12 * 60))   # how long one run waits for a tap
POLL_S = int(os.environ.get("AZPI_POLL_S", 15))        # how often it checks for the tap
WAIT_WINDOW_S = 6 * 3600            # after this, runs only glance for an answer, no waiting
TRIP_MIN_S = 3600                   # away at least an hour = a field trip (ignores Wi-Fi blips)
PORTAL = f"http://{HOME_IP}:8080/"
SITE_URL = "https://sat.dahabra.com/captures.html"

ARGS = set(sys.argv[1:])
DRY = "--dry-run" in ARGS
ANY_NET = "--any-network" in ARGS
NO_PUSH = "--no-push" in ARGS
if "--ask-off" in ARGS:
    ASK_FIRST = False

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
def stamp(im):
    """Write the watermark along the bottom of the picture, white with a dark outline."""
    from PIL import ImageDraw, ImageFont
    size = max(11, round(im.width * 0.027))
    font = None
    for f in FONTS:
        if os.path.exists(f):
            font = ImageFont.truetype(f, size)
            break
    if font is None:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(im)
    box = d.textbbox((0, 0), WATERMARK, font=font, stroke_width=2)
    w, h = box[2] - box[0], box[3] - box[1]
    x, y = (im.width - w) // 2, im.height - h - max(8, round(im.height * 0.012))
    d.text((x, y), WATERMARK, font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    return im


def shrink(src, dst, width):
    if DRY:
        return
    try:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        stamp(im)
        im.save(dst, "JPEG", quality=JPEG_Q, optimize=True, progressive=True)
    except ImportError:
        size = str(max(11, round(width * 0.027)))
        font = next((f for f in FONTS if os.path.exists(f)), None)
        cmd = ["convert", src, "-resize", f"{width}x>", "-strip", "-gravity", "South", "-pointsize", size]
        if font:
            cmd += ["-font", font]
        cmd += ["-stroke", "black", "-strokewidth", "4", "-fill", "black", "-annotate", "+0+10", WATERMARK,
                "-stroke", "none", "-fill", "white", "-annotate", "+0+10", WATERMARK,
                "-quality", str(JPEG_Q), "-interlace", "Plane", dst]
        subprocess.run(cmd, check=True)


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
        try:
            shrink(src, os.path.join(target, name), IMG_W)
        except Exception as e:
            say(f"skipping broken picture {os.path.basename(src)} ({e.__class__.__name__})")
            continue
        images.append({"kind": kind, "proj": proj, "file": f"{folder}/{name}"})
    images.sort(key=lambda i: (kind_rank(i["kind"]), i["proj"] != "spread"))
    main = next((i for i in images if i["proj"] == "spread"), images[0] if images else None)
    thumb = None
    if main:
        thumb = f"{folder}/thumb.jpg"
        src = next(f for f in p["files"] if classify(f) == (main["kind"], main["proj"]))
        try:
            shrink(src, os.path.join(SITE, thumb), THUMB_W)
        except Exception as e:
            say(f"no thumbnail ({e.__class__.__name__}), using the main picture")
            thumb = main["file"]
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
        "mark": MARK_VERSION,
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


# ---------------------------------------------------------------- review state
def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    if DRY:
        return
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, STATE)


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(s):
    return dt.datetime.fromisoformat(s) if s else None


# ---------------------------------------------------------------- phone messages (ntfy)
def ntfy_topic():
    t = os.environ.get("AZPI_NTFY_TOPIC")
    if t:
        return t
    try:
        with open(NTFY_SRC) as fh:
            m = re.search(r'^NTFY_TOPIC\s*=\s*["\']([\w\-]+)["\']', fh.read(), re.M)
            return m.group(1) if m else None
    except Exception:
        return None


def ntfy_send(title, message, tags="satellite", priority="default", actions=None,
              click=None, attach_path=None):
    """Send one message to the phone. Text is kept to plain ASCII so it survives HTTP headers."""
    topic = ntfy_topic()
    if not topic:
        say("no ntfy channel found, cannot message the phone")
        return False
    if DRY:
        say(f"would message phone: {title} | {message}")
        return True
    url = f"{NTFY_BASE}/{topic}"
    headers = {"Title": title, "Tags": tags, "Priority": priority}
    if actions:
        headers["Actions"] = actions
    if click:
        headers["Click"] = click
    if attach_path and os.path.exists(attach_path):
        with open(attach_path, "rb") as fh:
            body = fh.read()
        headers["Filename"] = "preview.jpg"
        headers["Message"] = message.replace("\n", "\\n")
        method = "PUT"
    else:
        body = message.encode("utf-8")
        method = "POST"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300
    except Exception as e:
        if method == "PUT":
            say(f"preview picture upload failed ({e}), sending text only")
            return ntfy_send(title, message, tags, priority, actions, click, None)
        say(f"phone message failed ({e})")
        return False


def reply_url():
    return f"{NTFY_BASE}/{ntfy_topic()}-reply"


def check_reply(token, since):
    """Look for a 'post <token>' or 'skip <token>' answer sent after `since` (unix time)."""
    url = reply_url() + "/json?poll=1&since=" + str(int(since))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            lines = r.read().decode("utf-8", "ignore").splitlines()
    except Exception as e:
        say(f"could not check for an answer ({e})")
        return None
    answer = None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        words = (ev.get("message") or "").strip().lower().split()
        if ev.get("event", "message") == "message" and len(words) == 2 and words[1] == token \
                and words[0] in ("post", "skip"):
            answer = words[0]          # the latest answer wins
    return answer


def wait_for_reply(token, since):
    deadline = time.time() + WAIT_S
    while True:
        a = check_reply(token, since)
        if a or time.time() >= deadline:
            return a
        time.sleep(POLL_S)


# ---------------------------------------------------------------- describing passes in plain words
def fmt_lock(s):
    m, r = divmod(int(s), 60)
    return f"{m} min" + (f" {r} s" if r else "") if m else f"{r} s"


def short_sat(code):
    return "N2-" + code.split("-")[-1]


def describe(p, stats, geo=None):
    local = p["start"].astimezone(DUBAI)
    txt = f"{short_sat(p['sat_code'])} {local.strftime('%a %d %b %H:%M')}: "
    if stats["lock_s"] <= 0:
        return txt + f"no lock (peak {stats['peak_snr']} dB)"
    txt += f"locked {fmt_lock(stats['lock_s'])}, peak {stats['peak_snr']} dB"
    if geo and geo.get("max_elev") is not None:
        txt += f", max {geo['max_elev']} deg"
    return txt


def readable(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except ImportError:
        return os.path.getsize(path) > 0
    except Exception:
        return False


def picture_count(p):
    return sum(1 for f in p["files"] if classify(f)[1] in ("spread", "equidistant") and readable(f))


# ---------------------------------------------------------------- writing the website
def write_and_push(by_day, changed, msg):
    passes = sorted(by_day.values(), key=lambda e: e["start_utc"], reverse=True)
    out_root = os.path.join(SITE, OUT_DIR)
    while len(passes) > 1 and folder_bytes(out_root) > MAX_BYTES:
        gone = passes.pop()
        shutil.rmtree(os.path.join(out_root, gone["id"]), ignore_errors=True)
        say(f"trimmed oldest capture {gone['id']} to stay under the size limit")
    manifest = {"updated": iso_now(), "passes": passes}
    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, "captures.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    if NO_PUSH:
        say("--no-push: files written, not sent")
        return True
    git("add", "-A", OUT_DIR)
    r = git("commit", "-q", "-m", msg + ": " + ", ".join(changed))
    if r.returncode:
        say("git commit failed: " + (r.stderr or r.stdout).strip())
        return False
    r = git("push", "-q")
    if r.returncode:
        say("git push failed: " + r.stderr.strip())
        return False
    say("sent to GitHub")
    return True


def post_entries(by_day, picks):
    """picks: list of (pass, stats). Each replaces whatever was posted for its day."""
    changed = []
    for p, stats in picks:
        day = p["start"].astimezone(DUBAI).strftime("%Y-%m-%d")
        old = by_day.get(day)
        if old and old["id"] != p["id"] and not DRY:
            shutil.rmtree(os.path.join(SITE, OUT_DIR, old["id"]), ignore_errors=True)
        say(f"{'would post' if DRY else 'posting'} {p['id']} for {day}: locked {stats['lock_s']} s, "
            f"peak {stats['peak_snr']} dB" + (f" (replaces {old['id']})" if old and old["id"] != p["id"] else ""))
        by_day[day] = build_entry(p, stats, pass_geometry(p["start"]))
        changed.append(p["id"])
    return changed


def restamp(by_day, all_passes):
    """Re-stamp already-approved passes when the watermark changes. No question needed."""
    lookup = {p["id"]: p for p in all_passes}
    changed = []
    for day, e in list(by_day.items()):
        if e.get("mark") != MARK_VERSION and e["id"] in lookup:
            say(f"adding new watermark to {e['id']}")
            p = lookup[e["id"]]
            if not DRY:
                by_day[day] = build_entry(p, signal_stats(p["start"]), pass_geometry(p["start"]))
            changed.append(e["id"])
    return changed


def best_per_day(items, by_day):
    """items: list of (pass, stats). Best clean pass per day that beats what is already posted."""
    best = {}
    for p, stats in items:
        if stats["lock_s"] < MIN_LOCK_S:
            continue
        day = p["start"].astimezone(DUBAI).strftime("%Y-%m-%d")
        score = (stats["lock_s"], stats["peak_snr"])
        if day not in best or score > best[day][0]:
            best[day] = (score, p, stats)
    out = []
    for day, (score, p, stats) in sorted(best.items()):
        old = by_day.get(day)
        if old and old["id"] == p["id"]:
            continue
        if old and (old.get("lock_s", 0), old.get("peak_snr", 0)) >= score:
            continue
        out.append((p, stats))
    return out


# ---------------------------------------------------------------- old behaviour: post without asking
def auto_post(by_day, all_passes):
    items = [(p, signal_stats(p["start"])) for p in all_passes]
    changed = restamp(by_day, all_passes) + post_entries(by_day, best_per_day(items, by_day))
    if not changed:
        say("nothing new to post")
    elif not DRY:
        write_and_push(by_day, changed, "New capture")


# ---------------------------------------------------------------- ask first
def ask(st, picks, new_items, came_back):
    token = secrets.token_hex(3)
    n_pic = sum(1 for _, s in new_items if s["lock_s"] >= MIN_LOCK_S)
    head = (f"Welcome back. {len(new_items)} pass{'es' if len(new_items) != 1 else ''} recorded while you were out, "
            if came_back else f"{len(new_items)} new pass{'es' if len(new_items) != 1 else ''} recorded, ")
    head += f"{n_pic} got pictures."
    lines = [head, ""]
    for p, stats in picks:
        geo = pass_geometry(p["start"])
        lines.append(f"BEST - {describe(p, stats, geo)}, {picture_count(p)} pictures")
    others = [(p, s) for p, s in new_items if p["id"] not in {q["id"] for q, _ in picks}]
    for p, s in others[:5]:
        lines.append("also - " + describe(p, s))
    if len(others) > 5:
        lines.append(f"...and {len(others) - 5} more")
    lines += ["", f"Post {'it' if len(picks) == 1 else 'them'} to sat.dahabra.com?"]
    msg = "\n".join(lines)

    preview = None
    p0 = picks[0][0]
    src = next((f for f in sorted(p0["files"]) if classify(f) == ("221", "spread")), None) or \
        next((f for f in sorted(p0["files"]) if classify(f)[1] == "spread"), None)
    if src and not DRY:
        preview = os.path.join("/tmp", "azpi_preview.jpg")
        try:
            shrink(src, preview, THUMB_W)
        except Exception as e:
            say(f"no preview picture ({e})")
            preview = None

    ru = reply_url()
    label = "Post it" if len(picks) == 1 else f"Post all {len(picks)}"
    actions = (f"http, {label}, {ru}, method=POST, body=post {token}, clear=true; "
               f"http, Not now, {ru}, method=POST, body=skip {token}, clear=true")
    title = f"AZPi: post {len(picks)} new capture{'s' if len(picks) != 1 else ''}?"
    asked_at = time.time()
    if not ntfy_send(title, msg, tags="satellite,camera", priority="high", actions=actions,
                     click=PORTAL, attach_path=preview):
        return False
    say(f"asked the Captain about {', '.join(p['id'] for p, _ in picks)} (answer code {token})")
    st["pending"] = {"token": token, "asked_at": asked_at,
                     "pass_ids": [p["id"] for p, _ in picks],
                     "seen_ids": [p["id"] for p, _ in new_items]}
    save_state(st)
    return True


def act_on_answer(st, answer, by_day, all_passes):
    pend = st["pending"]
    lookup = {p["id"]: p for p in all_passes}
    seen = [lookup[i] for i in pend["seen_ids"] if i in lookup]
    if answer == "post":
        picks = [(lookup[i], signal_stats(lookup[i]["start"])) for i in pend["pass_ids"] if i in lookup]
        changed = post_entries(by_day, picks)
        ok = bool(changed) and write_and_push(by_day, changed, "New capture (approved)")
        if ok:
            first = picks[0][0]
            n = sum(picture_count(p) for p, _ in picks)
            ntfy_send("AZPi: posted to sat.dahabra.com",
                      f"Done. {len(picks)} pass{'es' if len(picks) != 1 else ''}, {n} pictures, watermarked. "
                      "The Captures page updates in about a minute.",
                      tags="white_check_mark", click=f"{SITE_URL}#{first['id']}")
        else:
            ntfy_send("AZPi: posting failed",
                      "The pictures could not be sent to GitHub. It will ask again on the next run.",
                      tags="warning", priority="high")
            st["pending"] = None              # ask again next run
            save_state(st)
            return
    else:
        say("the Captain said not now, nothing posted")
    newest = max([p["start"] for p in seen] or [parse_iso(st["reviewed_until"])])
    st["reviewed_until"] = max(newest, parse_iso(st["reviewed_until"])).isoformat()
    st["pending"] = None
    save_state(st)


def ask_first(st, by_day, all_passes):
    # first ever run: everything up to the newest posted pass counts as already reviewed
    if not st.get("reviewed_until"):
        posted = [e["start_utc"] for e in by_day.values()]
        st["reviewed_until"] = max(posted) if posted else iso_now()
        save_state(st)
    reviewed = parse_iso(st["reviewed_until"])
    away = parse_iso(st.get("away_since"))
    came_back = bool(away) and (dt.datetime.now(dt.timezone.utc) - away).total_seconds() >= TRIP_MIN_S

    new = sorted((p for p in all_passes if p["start"] > reviewed), key=lambda p: p["start"])
    new_items = [(p, signal_stats(p["start"])) for p in new]
    picks = best_per_day(new_items, by_day)

    # re-stamps never need a question
    rs = restamp(by_day, all_passes)
    if rs and not DRY:
        write_and_push(by_day, rs, "New watermark")

    pend = st.get("pending")
    if pend:
        fresh = [p for p in new if p["id"] not in pend["seen_ids"]]
        if fresh and {p["id"] for p, _ in picks} != set(pend["pass_ids"]) and picks:
            say("new passes arrived while waiting, asking again with the updated list")
            st["pending"] = None
        else:
            recent = time.time() - pend["asked_at"] < WAIT_WINDOW_S
            answer = (wait_for_reply if recent and not DRY else check_reply)(pend["token"], pend["asked_at"] - 5)
            if answer:
                act_on_answer(st, answer, by_day, all_passes)
            else:
                say("still waiting for the Captain's answer")
            return

    if not new:
        if came_back:
            ntfy_send("AZPi: back home", "Welcome back. No passes were recorded while you were out.",
                      tags="satellite")
            st["away_since"] = None
            save_state(st)
        else:
            say("nothing new to review")
        return

    if picks:
        if ask(st, picks, new_items, came_back):
            st["away_since"] = None
            save_state(st)
            if not DRY:
                answer = wait_for_reply(st["pending"]["token"], st["pending"]["asked_at"] - 5)
                if answer:
                    act_on_answer(st, answer, by_day, all_passes)
                else:
                    say("no answer yet, will keep checking")
        return

    # nothing worth posting
    if came_back:
        best = max(new_items, key=lambda it: (it[1]["lock_s"], it[1]["peak_snr"]))
        n_lock = sum(1 for _, s in new_items if s["lock_s"] >= MIN_LOCK_S)
        why = ("none locked long enough for pictures" if n_lock == 0
               else "none beat what is already posted for those days")
        ntfy_send("AZPi: back home, nothing to post",
                  f"Welcome back. {len(new_items)} pass{'es' if len(new_items) != 1 else ''} recorded, {why}.\n"
                  f"Best was {describe(*best)}.", tags="satellite")
        st["away_since"] = None
    else:
        say(f"{len(new)} new pass(es), none worth posting")
    st["reviewed_until"] = max(p["start"] for p in new).isoformat()
    save_state(st)


# ---------------------------------------------------------------- main
def main():
    st = load_state()
    if "--status" in ARGS:
        print(json.dumps(st, indent=1))
        return
    if not ANY_NET and not at_home():
        if not st.get("away_since"):
            st["away_since"] = iso_now()
            save_state(st)
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

    manifest = load_manifest(os.path.join(SITE, OUT_DIR, "captures.json"))
    by_day = {e["day"]: e for e in manifest.get("passes", [])}
    all_passes = find_passes()

    if ASK_FIRST:
        ask_first(st, by_day, all_passes)
    else:
        auto_post(by_day, all_passes)


if __name__ == "__main__":
    lock = open("/tmp/azpi_publish.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    main()

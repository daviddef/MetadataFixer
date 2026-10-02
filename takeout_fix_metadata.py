#!/usr/bin/env python3
"""Apply Google Takeout .json sidecar metadata back onto the photos/videos.

Google Photos Takeout splits an export across many "Takeout N" folders / zips.
The same album folder ("Photos from 2012") shows up in several of them, and a
photo's .json is not guaranteed to sit next to the photo. So this script:

  1. walks every folder under ROOT (extract all your zips into one parent
     folder, e.g. ~/Takeouts/Takeout 1, Takeout 2, ...),
  2. indexes every JSON sidecar by the media filename it describes,
  3. matches each photo/video to its JSON (same folder name first, then
     anywhere in the tree), and
  4. writes date taken, GPS, description, people and favourite rating into the
     file with exiftool (RAW files get an .xmp sidecar instead of being edited),
     and sets the file modified time to the date taken.

Requires: Python 3.8+ and exiftool (macOS: `brew install exiftool`).

Usage:
  python3 takeout_fix_metadata.py ~/Takeouts --dry-run      # preview, writes only the report
  python3 takeout_fix_metadata.py ~/Takeouts --out ~/Photos # copy to a new library, fixed (safest)
  python3 takeout_fix_metadata.py ~/Takeouts                # fix in place (work on a copy!)

By default existing EXIF values are kept and only missing tags are filled in.
Use --overwrite to replace them with Google's values.
"""
import argparse
import atexit
import csv
import json
import unicodedata
import os
import re
import shutil
import hashlib
import subprocess
import sys
import tempfile
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp",
             ".tif", ".tiff", ".bmp", ".avif"}
RAW_EXT = {".nef", ".cr2", ".cr3", ".arw", ".dng", ".orf", ".raf", ".rw2",
           ".pef", ".srw", ".nrw"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".m2ts",
             ".wmv", ".mpg", ".mpeg"}
MEDIA_EXT = IMAGE_EXT | RAW_EXT | VIDEO_EXT
# exiftool cannot write these reliably; they only get the file mtime fixed.
NO_WRITE_EXT = {".avi", ".mkv", ".wmv", ".mpg", ".mpeg", ".mts", ".m2ts", ".bmp"}

SUPP = ".supplemental-metadata"
DUP_RE = re.compile(r"\((\d+)\)$")
NAME_LIMIT = 46  # Takeout truncates the base name of the json to 46 chars


def norm(s):
    return s.lower()


def json_key(json_path):
    """Return the lower-cased media filename a sidecar describes: 'img.jpg' or 'img.jpg(1)'."""
    base = json_path.name[:-5]  # drop .json
    dup = ""
    m = DUP_RE.search(base)
    if m:
        dup, base = m.group(0), base[:m.start()]
    # ".supplemental-metadata" may be truncated to any prefix of itself
    for k in range(len(SUPP), 1, -1):
        if base.endswith(SUPP[:k]):
            base = base[:-k]
            break
    else:
        if base.endswith(".") and os.path.splitext(base[:-1])[1].lower() in MEDIA_EXT | {".json"}:
            base = base[:-1]                      # Google cut the name right after the extension, leaving a lone dot
    # a duplicate marker can also sit before the suffix: "img(1).jpg.json" is not
    # used by Google, but "img.jpg(1).json" is, and is handled above.
    return norm(base + dup)


def media_candidates(name):
    """Sidecar keys that could describe this media filename, best match first."""
    stem, ext = os.path.splitext(name)
    names = [name]
    low = stem.lower()
    for suf in EDIT_SUFFIXES:                      # an edited copy shares the original's json (in any of Google's languages)
        if low.endswith(suf):
            names.append(stem[: -len(suf)] + ext)
            break
    out = []
    for n in names:
        s, ext2 = os.path.splitext(n)
        m = DUP_RE.search(s)
        variants = [n]
        if m:  # IMG(1).jpg  ->  IMG.jpg(1)
            plain = s[:m.start()] + ext2
            variants.append(plain + m.group(0))
            variants.append(plain[:NAME_LIMIT] + m.group(0))      # truncated name, marker kept
        for v in variants:
            out.append(v)
        if len(n) > NAME_LIMIT:
            out.append(n[:NAME_LIMIT])
    seen, res = set(), []
    for v in out:
        if norm(v) not in seen:
            seen.add(norm(v))
            res.append(norm(v))
    return res


SCAN_SKIP_DIRS = {"_original_videos", "_duplicates", "_merge_conflicts", "_unrecognised", "_older_formats", "_similar_set_aside", ".metadatafixer_stage"}


def scan(root):
    media, sidecars = [], []
    scan.noext = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP_DIRS]
        for f in files:
            p = Path(dirpath) / f
            ext = p.suffix.lower()
            if "." not in f and not f.startswith("._"):
                scan.noext += 1
            if ext == ".json":
                sidecars.append(p)
            elif ext in MEDIA_EXT:
                media.append(p)
    return media, sidecars


def build_index(sidecars):
    by_folder = defaultdict(list)  # (folder_name, key) -> [paths]
    by_key = defaultdict(list)     # key -> [paths]
    by_stem = defaultdict(list)    # (folder_name, stem) -> [paths]  (live-photo fallback)
    for p in sidecars:
        key = json_key(p)
        folder = p.parent.name
        by_folder[(folder, key)].append(p)
        by_key[key].append(p)
        if not DUP_RE.search(key):
            by_stem[(folder, os.path.splitext(key)[0])].append(p)
    return by_folder, by_key, by_stem


def load_json(p):
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(d, dict) and ("photoTakenTime" in d or "creationTime" in d):
        return d
    return None


def find_sidecar(m, idx):
    by_folder, by_key, by_stem = idx
    folder = m.parent.name
    cands = media_candidates(m.name)
    for k in cands:
        found = by_folder.get((folder, k))
        if found:
            local = [p for p in found if p.parent == m.parent]  # sidecar next to the photo wins
            if local:
                return local[0], "folder"
            return (found[0], "folder") if len(found) == 1 else (found, "tree-ambiguous")
    for k in cands:
        if by_key.get(k):
            found = by_key[k]
            return (found[0], "tree") if len(found) == 1 else (found, "tree-ambiguous")
    stem = norm(os.path.splitext(m.name)[0])
    if by_stem.get((folder, stem)):
        return by_stem[(folder, stem)][0], "stem"
    return None, None


def ts(d, field):
    try:
        return int(d[field]["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None


def build_args(d, ext, overwrite, skip=()):
    """exiftool arguments for one sidecar. Returns (args, taken_epoch)."""
    is_video = ext in VIDEO_EXT
    taken = ts(d, "photoTakenTime") or ts(d, "creationTime")
    a = []
    if taken and "date" not in skip:
        dt = datetime.fromtimestamp(taken, timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
        if is_video:
            a += ["-api", "QuickTimeUTC=1",
                  f"-QuickTime:CreateDate={dt}", f"-QuickTime:ModifyDate={dt}",
                  f"-QuickTime:MediaCreateDate={dt}", f"-QuickTime:TrackCreateDate={dt}"]
        else:
            a += [f"-AllDates={dt}", f"-XMP:DateCreated={dt}"]
    geo = d.get("geoData") or {}
    if not (geo.get("latitude") or geo.get("longitude")):
        geo = d.get("geoDataExif") or {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    if (lat is None) != (lon is None):
        lat = lon = None
    if "gps" in skip:
        lat = lon = None
    if (lat or lon) and is_video:
        alt = geo.get("altitude") or 0
        coord = f"{lat}, {lon}, {alt}"
        a += [f"-Keys:GPSCoordinates={coord}", f"-UserData:GPSCoordinates={coord}"]
    elif lat or lon:
        alt = geo.get("altitude") or 0
        a += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
              f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
              f"-GPSAltitude={abs(alt)}", f"-GPSAltitudeRef={1 if alt < 0 else 0}"]
    desc = (d.get("description") or "").strip()
    if desc and "desc" not in skip:
        a += [f"-XMP-dc:Description={desc}"]
        if not is_video:
            a += [f"-ImageDescription={desc}"]
    for person in d.get("people") or []:
        name = (person or {}).get("name")
        if name:
            a += [f"-XMP-iptcExt:PersonInImage-={name}", f"-XMP-iptcExt:PersonInImage+={name}"]  # no repeats
    if d.get("favorited"):
        a += ["-XMP:Rating=5"]
    return a, taken


class ExifTool:
    """One long-lived exiftool process (avoids paying its start-up cost for every file)."""

    def __init__(self):
        self.p = subprocess.Popen(["exiftool", "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def alive(self):
        return self.p.poll() is None

    def run(self, args):
        """Run one exiftool command. Returns (exit_status, output_lines)."""
        a = ["-charset", "utf8", "-charset", "filename=utf8", "-echo4", "STATUS=${status}"]
        a += [str(x) for x in args] + ["-execute"]
        self.p.stdin.write(("\n".join(a) + "\n").encode("utf-8", "surrogateescape"))
        self.p.stdin.flush()
        lines, status = [], 1
        while True:
            raw = self.p.stdout.readline()
            if not raw:
                raise RuntimeError("exiftool stopped unexpectedly")
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.strip() == "{ready}":
                return status, lines
            if line.startswith("STATUS="):
                try:
                    status = int(line[7:])
                except ValueError:
                    status = 1
            else:
                lines.append(line)

    def close(self):
        try:
            self.p.stdin.write(b"-stay_open\nFalse\n")
            self.p.stdin.flush()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


_tools = threading.local()
_all_tools, _all_lock = [], threading.Lock()


def tool():
    t = getattr(_tools, "t", None)
    if t is None or not t.alive():
        t = ExifTool()
        _tools.t = t
        with _all_lock:
            _all_tools.append(t)
    return t


def close_all():
    with _all_lock:
        for t in _all_tools:
            t.close()
        _all_tools.clear()


atexit.register(close_all)

_ASSIGN = re.compile(r"^-[A-Za-z0-9_:-]+\+?=")


def _escape_assign(arg):
    """With -E, exiftool reads values HTML-escaped; this keeps newlines and &<> intact."""
    m = _ASSIGN.match(arg)
    if not m:
        return arg
    v = arg[m.end():].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return m.group(0) + v.replace("\r", "").replace("\n", "&#xa;")


def _message(lines):
    return " | ".join(l.strip() for l in lines if l.strip().startswith(("Error", "Warning"))) or " ".join(
        l.strip() for l in lines if l.strip())


def run_exiftool(target, args, overwrite, sidecar_for_raw=False):
    flags = ["-m", "-P", "-E"]
    tag_args = list(args)
    if sidecar_for_raw:
        xmp = str(target.with_suffix(".xmp"))
        # xmp can't hold EXIF-group tags; map them onto XMP equivalents
        tag_args = [c.replace("-AllDates=", "-XMP:DateCreated=") for c in tag_args]
        tag_args = [c for c in tag_args if not c.startswith(("-ImageDescription", "-GPSAltitudeRef",
                                                             "-GPSLatitudeRef", "-GPSLongitudeRef"))]
        tag_args = [c.replace("-GPSLatitude=", "-XMP:GPSLatitude=").replace("-GPSLongitude=", "-XMP:GPSLongitude=")
                    .replace("-GPSAltitude=", "-XMP:GPSAltitude=") for c in tag_args]
        flags += ["-overwrite_original"] if Path(xmp).exists() else ["-o", xmp]
    else:
        flags += ["-overwrite_original"]
    if not overwrite:
        flags += ["-wm", "cg"]  # create missing tags, never replace existing ones
    cmd = flags + [_escape_assign(c) for c in tag_args]
    status, lines = tool().run(cmd + [str(target)])
    msg = _message(lines)
    if status != 0 and not sidecar_for_raw and "looks more like" in msg:
        return _retry_real_type(target, cmd, msg)
    return status == 0, msg


def _retry_real_type(target, cmd, first_msg):
    """Google sometimes saves JPEGs as .HEIC (etc). Write via a temp name with the real extension."""
    _, lines = tool().run(["-s3", "-FileTypeExtension", str(target)])
    real = (lines[0].strip().lower() if lines else "")
    if not real or " " in real:
        return False, first_msg
    tmp = target.with_name(target.stem + ".__fix__." + real)
    target.rename(tmp)
    try:
        status, out = tool().run(cmd + [str(tmp)])
    finally:
        tmp.rename(target)
    return status == 0, _message(out)


def unique_dest(dest):
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1



def read_existing(path, is_video=False):
    """Existing date / GPS / description / rating in a file, for before-and-after counting. When the file cannot be
    read at all the result carries "_failed": True, so callers never mistake "unreadable" for "empty"."""
    try:
        _, lines = tool().run(["-j", "-n", "-api", "QuickTimeUTC=1", "-DateTimeOriginal", "-CreateDate",
                               "-QuickTime:CreateDate", "-XMP:DateCreated", "-GPSLatitude", "-GPSLongitude", "-Keys:GPSCoordinates",
                               "-ImageDescription", "-XMP-dc:Description", "-XMP:Rating", str(path)])
    except (OSError, RuntimeError, UnicodeError):
        return {"_failed": True}
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("["))
        d = json.loads("\n".join(lines[start:]))[0]
    except (StopIteration, ValueError, IndexError):
        return {"_failed": True}
    lat, lon = d.get("GPSLatitude"), d.get("GPSLongitude")
    kc = d.get("Keys:GPSCoordinates") or d.get("GPSCoordinates")
    if isinstance(kc, str):
        parts = kc.replace(",", " ").split()
        if len(parts) >= 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
            except ValueError:
                pass
    cands = ((d.get("QuickTime:CreateDate"), d.get("CreateDate"), d.get("DateTimeOriginal"), d.get("DateCreated")) if is_video
             else (d.get("DateTimeOriginal"), d.get("CreateDate"), d.get("DateCreated")))
    date = str(next((c for c in cands if c and not str(c).startswith("0000")), "") or "")
    desc = str(d.get("ImageDescription") or d.get("Description") or "").strip()
    return {"date": date, "lat": lat, "lon": lon, "desc": desc, "rating": d.get("Rating")}


def _epoch(date_str):
    try:
        return datetime.strptime(date_str[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def classify(d, ext, ex, overwrite, date_policy="earlier"):
    """Per-field outcome: none / added / same / replaced / kept.

    date_policy decides a disagreement between the photo's own date and Google's:
    earlier = keep whichever is earlier (default), photo = keep the photo's, google = use Google's."""
    differ = "replaced" if overwrite else "kept"
    out = {}
    taken = ts(d, "photoTakenTime") or ts(d, "creationTime")
    out["_taken_final"] = taken
    if not taken:
        out["date"] = "none"
    else:
        dt = datetime.fromtimestamp(taken, timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
        e = ex.get("date", "")
        if not e or e.startswith("0000"):
            out["date"] = "added"
        elif e[:19] == dt:
            out["date"] = "same"
        elif date_policy == "google":
            out["date"] = "replaced"
        elif date_policy == "photo":
            out["date"] = "kept"
        else:  # earlier wins: Google's date is often the (later) upload or re-save time
            out["date"] = "replaced" if dt < e[:19] else "kept"
        if out["date"] in ("kept", "same") and e and not e.startswith("0000"):
            out["_taken_final"] = _epoch(e) or taken
        out["date_before"] = "" if e.startswith("0000") else e[:19]
        out["date_google"] = dt + " UTC"
        up = ts(d, "creationTime")
        if up and abs(up - taken) <= 300:
            out["date_note"] = "Google's date equals its upload time, so it may not be when the photo was taken"
    geo = d.get("geoData") or {}
    if not (geo.get("latitude") or geo.get("longitude")):
        geo = d.get("geoDataExif") or {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    if (lat is None) != (lon is None):
        lat = lon = None
    if not (lat or lon):
        out["gps"] = "none"
    elif ex.get("lat") is None or ex.get("lon") is None:
        out["gps"] = "added"
        out["gps_google"] = f"{lat:.6f}, {lon:.6f}"
    else:
        near = abs(ex["lat"] - lat) < 5e-4 and abs(ex["lon"] - lon) < 5e-4
        out["gps"] = "same" if near else differ
        out["gps_before"] = f"{ex['lat']:.6f}, {ex['lon']:.6f}"
        out["gps_google"] = f"{lat:.6f}, {lon:.6f}"
    desc = (d.get("description") or "").strip()
    if not desc:
        out["desc"] = "none"
    else:
        e = ex.get("desc", "")
        out["desc"] = "added" if not e else ("same" if e == desc else differ)
        out["desc_before"], out["desc_google"] = e, desc
    names = [(p or {}).get("name") for p in d.get("people") or []]
    out["people"] = "; ".join(n for n in names if n)
    out["favourite"] = "yes" if d.get("favorited") else ""
    return out



def pick_closest(m, candidates):
    """Several sidecars share this filename (numbering restarts): choose the one nearest the file's own date."""
    ex = read_existing(m, m.suffix.lower() in VIDEO_EXT).get("date", "")
    try:
        ref = datetime.strptime(ex[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        ref = m.stat().st_mtime
    best, gap = candidates[0], None
    for c in candidates:
        d = load_json(c)
        t = d and (ts(d, "photoTakenTime") or ts(d, "creationTime"))
        if t and (gap is None or abs(t - ref) < gap):
            best, gap = c, abs(t - ref)
    return best


REPORT_FIELDS = ["file", "sidecar", "match", "status", "detail", "live", "output",
                 "date", "date_before", "date_google", "date_note",
                 "gps", "gps_before", "gps_google", "gps_guess", "date_fix", "date_flag",
                 "desc", "desc_before", "desc_google",
                 "people", "favourite"]

STILL_EXT = {".heic", ".heif", ".jpg", ".jpeg"}


def live_id(video):
    """Apple ContentIdentifier of the still image that shares this video's name, or (None, reason)."""
    for ext in (".HEIC", ".heic", ".JPG", ".jpg", ".JPEG", ".jpeg", ".HEIF", ".heif"):
        still = video.with_suffix(ext)
        if still.exists():
            r = subprocess.run(["exiftool", "-s3", "-ContentIdentifier", str(still)],
                               capture_output=True, text=True)
            cid = r.stdout.strip()
            return (cid, "") if cid else (None, "no-id")
    return None, "no-still"


def pair_live(target, cid):
    """Write the still's ContentIdentifier into the video and make it a .MOV."""
    ok, msg = run_exiftool(target, [f"-Keys:ContentIdentifier={cid}"], True)
    if not ok:
        return target, msg
    if target.suffix.lower() != ".mov":
        new = unique_dest(target.with_suffix(".MOV"))
        target.rename(new)
        target = new
    return target, ""


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def keep_rank(p):
    """Which copy of an exact duplicate to keep: year folders before albums, then lowest Takeout number."""
    m = re.search(r"Takeout (\d+)", str(p))
    return (0 if p.parent.name.startswith("Photos from") else 1, int(m.group(1)) if m else 0, str(p))


def _quick_sig(path):
    """Size plus the first and last 64 KB: cheap, and rules out almost every non-duplicate before a full hash."""
    sz = os.path.getsize(path)
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        h.update(fh.read(65536))
        if sz > 131072:
            fh.seek(sz - 65536)
            h.update(fh.read(65536))
    return (sz, h.hexdigest())


def plan_duplicates(media, progress=None):
    """Find byte-identical files. Returns ({duplicate_path: kept_path}, bytes_saved)."""
    by_size = defaultdict(list)
    for m in media:
        try:
            by_size[m.stat().st_size].append(m)
        except OSError:
            pass
    candidates = [g for sz, g in by_size.items() if sz and len(g) > 1]
    todo, done = sum(len(g) for g in candidates), 0
    dupes, saved = {}, 0
    for g in candidates:
        by_quick = defaultdict(list)
        for m in g:
            try:
                by_quick[_quick_sig(m)].append(m)
            except OSError:
                pass                                   # vanished or unreadable: leave it alone
            done += 1
            if progress:
                progress(done, todo)
        for q in by_quick.values():
            if len(q) < 2:
                continue
            by_hash = defaultdict(list)
            for m in q:
                try:
                    by_hash[file_hash(m)].append(m)
                except OSError:
                    pass
            for same in by_hash.values():
                if len(same) > 1:
                    same.sort(key=keep_rank)
                    for other in same[1:]:
                        dupes[str(other)] = str(same[0])
                        try:
                            saved += other.stat().st_size
                        except OSError:
                            pass
    return dupes, saved


STILL_SUFFIXES = (".HEIC", ".heic", ".JPG", ".jpg", ".JPEG", ".jpeg", ".HEIF", ".heif")


def read_content_ids(paths, progress=None, chunk=400):
    """ContentIdentifier of many files using a few exiftool runs instead of one per file."""
    ids, done = {}, 0
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        fd_, arg_ = tempfile.mkstemp(suffix=".args")
        os.close(fd_)
        arg = Path(arg_)
        try:
            arg.write_text("\n".join(str(p) for p in part), encoding="utf-8", errors="surrogateescape")
            r = subprocess.run(["exiftool", "-charset", "filename=utf8", "-j", "-ContentIdentifier",
                                "-@", str(arg)], capture_output=True, text=True)
            try:
                for item in json.loads(r.stdout or "[]"):
                    cid = item.get("ContentIdentifier")
                    if cid:
                        ids[item["SourceFile"]] = str(cid)
            except ValueError:
                pass
        finally:
            arg.unlink(missing_ok=True)
        done += len(part)
        if progress:
            progress("live", done, len(paths))
    return ids


def plan_live(media, progress=None):
    """Read each Live Photo video's still-image ID up front (before any file is moved)."""
    pairs = {}
    for m in media:
        if m.suffix.lower() in (".mp4", ".mov"):
            for ext in STILL_SUFFIXES:
                still = m.with_suffix(ext)
                if still.exists():
                    pairs[str(m)] = still
                    break
    ids = read_content_ids(sorted({str(p) for p in pairs.values()}), progress)
    plan = {}
    for m in media:
        if m.suffix.lower() in (".mp4", ".mov"):
            still = pairs.get(str(m))
            if not still:
                plan[str(m)] = (None, "no-still")
            else:
                cid = ids.get(str(still))
                plan[str(m)] = (cid, "") if cid else (None, "no-id")
    return plan


# ---- Persistent copying and moving: external drives hiccup, so we wait, retry, resume and never give up quietly --------
import errno

RESIL = {"stop": None, "say": None, "tries": 6, "delays": (3, 8, 20, 45, 90, 180), "chunk": 4 << 20, "gentle": 0.0,
         "retries": 0, "bad_run": 0, "abort": False, "wait_drive": 600, "events": []}
TRANSIENT = {errno.EIO, errno.ENXIO, errno.ENODEV, errno.ETIMEDOUT, errno.EBUSY, errno.EAGAIN, errno.EINTR, errno.ENOTCONN,
             errno.ESTALE, errno.EHOSTDOWN, errno.ECONNRESET, errno.ENETDOWN, errno.ENOENT, errno.EPIPE, errno.EPROTO, getattr(errno, "EREMOTEIO", -1)}


def reset_resilience(stop=None, say=None, tries=6):
    """Called by a job when it starts. stop() raises when the user pressed Stop; say(text) shows a retry message."""
    RESIL.update(stop=stop, say=say, tries=tries, gentle=0.0, retries=0, bad_run=0, abort=False, events=[])


def _say(msg):
    f = RESIL.get("say")
    if f:
        try:
            f(msg)
        except Exception:
            pass


def _nap(seconds):
    """Sleep in small steps so Stop is honoured at once."""
    end = time.time() + seconds
    while time.time() < end:
        f = RESIL.get("stop")
        if f:
            f()
        time.sleep(min(0.5, max(0.0, end - time.time())))


def _transient(e):
    return isinstance(e, OSError) and (e.errno in TRANSIENT or e.errno is None)


def _wait_for(path):
    """If the drive holding `path` has gone away, wait for it to come back (up to ten minutes)."""
    d = os.path.dirname(os.path.abspath(path))
    waited = 0
    while not os.path.isdir(d) and waited < RESIL["wait_drive"]:
        _say("The drive is not answering. Waiting for it to come back (%ds)... reconnect the cable if you can" % waited)
        _nap(5)
        waited += 5
    return os.path.isdir(d)


def _copy_resumable(src, part, chunk):
    size = os.path.getsize(src)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have > size:
        have = 0
    have = max(0, have - chunk)                              # the last piece may be damaged: copy it again
    with open(src, "rb") as s, open(part, "r+b" if os.path.exists(part) else "wb") as d:
        s.seek(have)
        d.seek(have)
        d.truncate(have)
        stopf = RESIL.get("stop")
        while True:
            buf = s.read(chunk)
            if not buf:
                break
            d.write(buf)
            if RESIL["gentle"]:
                time.sleep(RESIL["gentle"])
            if stopf:
                stopf()
        d.flush()
    if os.path.getsize(part) != size:
        raise OSError(errno.EIO, "the copy is incomplete (%d of %d bytes)" % (os.path.getsize(part), size))
    try:
        shutil.copystat(src, part)
    except OSError:
        pass


def safe_copy(src, dest):
    """Copy one file to `dest` through dest.part. Retries with growing pauses, resumes a partly copied file, slows down after
    errors and waits for a disconnected drive. Raises OSError only when it has really tried."""
    src, dest = str(src), str(dest)
    part = dest + ".part"
    if RESIL["abort"]:
        raise OSError(errno.EIO, "stopped earlier: the drive is not responding (reconnect it, then press Continue)")
    last = None
    for attempt in range(1, RESIL["tries"] + 1):
        try:
            if not os.path.exists(src) and not _wait_for(src):
                raise OSError(errno.ENOENT, "the source drive is not connected")
            _copy_resumable(src, part, RESIL["chunk"] if not RESIL["gentle"] else 256 * 1024)
            os.replace(part, dest)
            RESIL["bad_run"] = 0
            if RESIL["gentle"] > 0.01:
                RESIL["gentle"] = RESIL["gentle"] / 2                  # the drive is behaving again: speed back up
            else:
                RESIL["gentle"] = 0.0
            return
        except OSError as e:
            last = e
            if not _transient(e) or attempt == RESIL["tries"]:
                break
            RESIL["retries"] += 1
            RESIL["gentle"] = max(RESIL["gentle"], 0.02) * 2 if RESIL["gentle"] else 0.02      # give the drive room to breathe
            delay = RESIL["delays"][min(attempt - 1, len(RESIL["delays"]) - 1)]
            if len(RESIL["events"]) < 50:
                RESIL["events"].append("%s: %s (try %d)" % (os.path.basename(src), e.strerror or e, attempt))
            _say("The drive hiccuped on %s (%s). Pausing %ds, then trying again (%d of %d)" % (os.path.basename(src), e.strerror or e, delay, attempt, RESIL["tries"] - 1))
            _nap(delay)
    try:
        if os.path.exists(part):
            os.unlink(part)
    except OSError:
        pass
    RESIL["bad_run"] += 1
    if RESIL["bad_run"] >= 3:
        RESIL["abort"] = True                                  # stop hammering a drive that has stopped answering
    raise last


def safe_move(src, dest):
    """Move a file or folder. Same drive: a rename. Across drives: a verified resumable copy, and only then the original is removed."""
    src, dest = str(src), str(dest)
    try:
        return os.rename(src, dest)
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTEMPTY) and not _transient(e):
            raise
    if os.path.isdir(src) and not os.path.islink(src):
        os.makedirs(dest, exist_ok=True)
        for name in sorted(os.listdir(src)):
            safe_move(os.path.join(src, name), os.path.join(dest, name))
        os.rmdir(src)
        return
    if os.path.exists(dest):
        raise FileExistsError(errno.EEXIST, "already exists", dest)
    safe_copy(src, dest)
    for attempt in range(3):                                   # the copy is complete and checked: now remove the original
        try:
            os.unlink(src)
            return
        except FileNotFoundError:
            return
        except OSError:
            _nap(2)
    # the original could not be removed: the copy is safe, so report it rather than fail the file
    RESIL["events"].append("%s: copied, but the original could not be removed" % os.path.basename(src))


def safe_copytree(src, dest):
    os.makedirs(dest, exist_ok=True)
    for name in sorted(os.listdir(src)):
        sp, dp = os.path.join(src, name), os.path.join(dest, name)
        if os.path.islink(sp):
            os.symlink(os.readlink(sp), dp)
        elif os.path.isdir(sp):
            safe_copytree(sp, dp)
        else:
            safe_copy(sp, dp)


def place_file(src, dest, move):
    """Copy or move one file into place through a temporary .part name. The real name only ever holds a complete file."""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    if move:
        try:
            os.rename(str(src), str(part))
            os.replace(part, dest)
            return
        except OSError:
            pass
        safe_move(src, dest)
    else:
        safe_copy(src, dest)


MANIFEST = ".metadatafixer_fix.jsonl"   # one progress log per job type, so Sort or Merge never makes Fix skip files
MANIFEST_SORT = ".metadatafixer_sort.jsonl"
MANIFEST_MERGE = ".metadatafixer_merge.jsonl"


def load_manifest(out_root, name=None):
    done = {}
    try:
        with open(Path(out_root) / (name or MANIFEST), encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    done[d["src"]] = d["dest"]
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return done


def record_progress(args, src, dest):
    """Remember that src was placed at dest, so an interrupted run can resume without duplicating."""
    root = getattr(args, "out_root", None)
    if not root or getattr(args, "dry_run", False):
        return
    with args.lock:
        with open(Path(root) / getattr(args, "manifest_file", MANIFEST), "a", encoding="utf-8") as fh:
            key = getattr(args, "manifest_ns", "") + str(src)
            fh.write(json.dumps({"src": key, "dest": str(dest)}, ensure_ascii=False) + "\n")
        args.manifest[key] = str(dest)


def already_done(m, args):
    d = (getattr(args, "manifest", None) or {}).get(getattr(args, "manifest_ns", "") + str(m))
    return d if d and Path(d).exists() else None


def guarded(fn):
    """One bad file must never stop the whole run: turn any failure into an error row."""
    def run(m, idx, args, out_root):
        try:
            return fn(m, idx, args, out_root)
        except Exception as e:
            if type(e).__name__ == "Cancelled":
                raise                                    # the user pressed Stop
            row = {k: "" for k in REPORT_FIELDS}
            row["file"] = str(m)
            row["status"] = "copy-error" if isinstance(e, OSError) else "error"
            row["detail"] = f"{type(e).__name__}: {e}"[:300]
            return row
    return run


def prepare(args, media, progress=None):
    """progress(stage, done, total) is called as work proceeds."""
    args.lock, args.claimed = threading.Lock(), set()
    root = getattr(args, "out_root", None)
    args.manifest = (load_manifest(root, getattr(args, "manifest_file", MANIFEST))
                     if root and not getattr(args, "dry_run", False) else {})
    dedupe_progress = (lambda d, t: progress("dedupe", d, t)) if progress else None
    args.dupes, args.dupe_bytes = (plan_duplicates(media, dedupe_progress) if getattr(args, "dedupe", False) else ({}, 0))
    args.live_plan = plan_live(media, progress) if getattr(args, "pair_live", False) else {}


def _ckey(p):
    """Names that a Mac sees as the same file (IMG.JPG and img.jpg, composed and decomposed accents)."""
    return unicodedata.normalize("NFC", str(p)).casefold()


def claim_dest(dest, args):
    """Reserve a unique output path (thread-safe, also avoids clashes between files in a dry run)."""
    with args.lock:
        cand, i = dest, 0
        while _ckey(cand) in args.claimed or cand.exists():
            i += 1
            cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        args.claimed.add(_ckey(cand))
        return cand


_TAKEOUT_WRAP = re.compile(r"^(takeout( \d+)?|google photos)$", re.I)


def rel_parent(m, roots):
    """Folder path of m below whichever chosen root contains it, ignoring Takeout's wrapper folders."""
    best = None
    for r in roots:
        try:
            rel = m.parent.relative_to(r)
        except ValueError:
            continue
        if best is None or len(Path(r).parts) > len(Path(best[0]).parts):
            best = (r, rel)
    if best is None:
        return Path(m.parent.name)
    parts = list(best[1].parts)
    while parts and _TAKEOUT_WRAP.match(parts[0]):
        parts.pop(0)
    return Path(*parts) if parts else Path()


def dest_dir_for(m, taken, args, out_root):
    """Folders with the same path (below the chosen folders, Takeout wrappers ignored) merge into one."""
    roots = getattr(args, "roots", None)
    if roots:
        return out_root / rel_parent(m, roots)
    return out_root / m.parent.name


def prune_empty_dirs(roots):
    """After moving files out, remove folders left empty (a lone .DS_Store counts as empty)."""
    removed = 0
    for root in roots:
        for dirpath, _, _ in os.walk(root, topdown=False):
            if Path(dirpath) == Path(root):
                continue
            try:
                names = os.listdir(dirpath)
                if all(n in (".DS_Store", "Thumbs.db") for n in names):
                    for n in names:
                        os.remove(os.path.join(dirpath, n))
                    os.rmdir(dirpath)
                    removed += 1
            except OSError:
                pass
    return removed


_ND = [
    re.compile(r"(?<!\d)(19[89]\d|20\d{2})(\d{2})(\d{2})[_ T-]?(\d{2})(\d{2})(\d{2})"),                       # 20190704_123456, PXL_20210512_153045123
    re.compile(r"(?<!\d)(19[89]\d|20\d{2})[-_.](\d{2})[-_.](\d{2})(?:\s+at\s+|[ _T-]+)(\d{2})[-_.:](\d{2})[-_.:](\d{2})"),  # 2019-07-04 12.34.56, Screenshot 2019-07-04 at 12.34.56
    re.compile(r"(?<!\d)(19[89]\d|20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)()()()"),                                  # IMG-20190704-WA0001, 2019-07-04
]


def date_from_name(name):
    """A date written in a file name (IMG_20190704_123456.jpg, Screenshot 2019-07-04 at 12.34.56.png). Returns epoch (the
    wall-clock time as written, treated as UTC so exiftool stores the same digits) or None."""
    stem = os.path.splitext(name)[0]
    now = time.time() + 86400
    for rx in _ND:
        for m in rx.finditer(stem):
            y, mo, d, hh, mi, ss = (int(x) if x else 0 for x in m.groups())
            if not (1 <= mo <= 12 and 1 <= d <= 31 and hh < 24 and mi < 60 and ss < 60):
                continue
            if not m.group(4):
                hh = 12                                        # date only: noon, so no time zone can move it to another day
            try:
                t = datetime(y, mo, d, hh, mi, ss, tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if t <= now:
                return int(t)
    return None


# ---- Date sanity and best-guess places ------------------------------------------------------------------------------
# A folder called "2017" or "Johannesburg" tells us a lot about the pictures in it. Used only to FILL gaps or flag oddities.
GAZETTEER_TEXT = """
c|South Africa|rsa,za|-30.56|22.94
c|Japan|nippon|36.20|138.25
c|United States|usa,america,united states of america|39.83|-98.58
c|United Kingdom|uk,great britain,britain,england,scotland,wales,northern ireland|54.0|-2.5
c|France||46.6|2.2
c|Germany|deutschland|51.16|10.45
c|Italy|italia|42.8|12.5
c|Spain|espana|40.4|-3.7
c|Portugal||39.5|-8.0
c|Netherlands|holland|52.2|5.3
c|Belgium||50.6|4.7
c|Switzerland||46.8|8.2
c|Austria||47.5|14.5
c|Greece||39.0|22.0
c|Ireland||53.4|-8.2
c|Iceland||64.9|-18.6
c|Norway||61.0|9.0
c|Sweden||62.0|15.0
c|Finland||64.0|26.0
c|Denmark||56.0|10.0
c|Poland||52.0|19.4
c|Czech Republic|czechia|49.8|15.5
c|Hungary||47.2|19.5
c|Croatia||45.1|15.2
c|Slovenia||46.1|14.8
c|Romania||45.9|25.0
c|Bulgaria||42.7|25.5
c|Turkey|turkiye|39.0|35.0
c|Egypt||26.8|30.8
c|Morocco||31.8|-7.1
c|Tunisia||34.0|9.0
c|Kenya||0.2|37.9
c|Tanzania||-6.4|34.9
c|Uganda||1.4|32.3
c|Rwanda||-1.9|29.9
c|Ethiopia||9.1|40.5
c|Namibia||-22.0|17.0
c|Botswana||-22.3|24.7
c|Zimbabwe||-19.0|29.2
c|Zambia||-13.1|27.8
c|Mozambique||-18.7|35.5
c|Madagascar||-18.8|47.0
c|Mauritius||-20.3|57.6
c|Seychelles||-4.7|55.5
c|Lesotho||-29.6|28.2
c|Eswatini|swaziland|-26.5|31.5
c|Ghana||7.9|-1.0
c|Nigeria||9.1|8.7
c|Senegal||14.5|-14.5
c|Mexico||23.6|-102.5
c|Canada||56.1|-106.3
c|Brazil|brasil|-14.2|-51.9
c|Argentina||-38.4|-63.6
c|Chile||-35.7|-71.5
c|Peru||-9.2|-75.0
c|Colombia||4.6|-74.3
c|Costa Rica||9.7|-83.8
c|Panama||8.5|-80.8
c|Cuba||21.5|-77.8
c|Jamaica||18.1|-77.3
c|Bahamas||25.0|-77.4
c|Australia||-25.3|133.8
c|New Zealand|nz|-41.0|174.0
c|Fiji||-17.7|178.1
c|China||35.9|104.2
c|Hong Kong|hongkong|22.3|114.2
c|Taiwan||23.7|121.0
c|South Korea|korea|36.5|127.9
c|Thailand||15.9|100.99
c|Vietnam||14.1|108.3
c|Cambodia||12.6|104.99
c|Laos||19.9|102.5
c|Malaysia||4.2|102.0
c|Singapore||1.35|103.82
c|Indonesia||-0.8|113.9
c|Philippines||12.9|121.8
c|India||20.6|78.96
c|Nepal||28.4|84.1
c|Sri Lanka||7.9|80.8
c|Maldives||3.2|73.2
c|Bhutan||27.5|90.4
c|United Arab Emirates|uae|24.0|54.0
c|Qatar||25.3|51.2
c|Oman||21.5|55.9
c|Israel||31.0|34.9
c|Saudi Arabia||23.9|45.1
c|Mongolia||46.9|103.8
c|Russia||61.5|105.3
c|Ukraine||48.4|31.2
c|Estonia||58.6|25.0
c|Latvia||56.9|24.6
c|Lithuania||55.2|23.9
c|Malta||35.9|14.4
c|Cyprus||35.1|33.4
c|Luxembourg||49.8|6.1
c|Bali||-8.4|115.2
c|Patagonia||-49.3|-72.0
c|Tasmania||-42.0|146.6
c|Hawaii||20.8|-156.3
c|Alaska||64.2|-149.5
c|Lapland||68.0|25.0
c|Tuscany||43.4|11.2
c|Provence||43.9|5.9
c|Algarve||37.1|-8.2
c|Kruger||-23.99|31.55
c|Garden Route||-33.97|22.4
c|Drakensberg||-29.2|29.4
c|Serengeti||-2.3|34.8
c|Victoria Falls||-17.92|25.86
c|Zanzibar||-6.17|39.2
t|Johannesburg|joburg,jozi,jhb|-26.2041|28.0473
t|Cape Town|capetown|-33.9249|18.4241
t|Durban||-29.8587|31.0218
t|Pretoria|tshwane|-25.7479|28.2293
t|Port Elizabeth|gqeberha|-33.9608|25.6022
t|Bloemfontein||-29.0852|26.1596
t|Stellenbosch||-33.9321|18.8602
t|Knysna||-34.0367|23.0471
t|Sun City||-25.3333|27.0944
t|Sandton||-26.1076|28.0567
t|Hermanus||-34.4187|19.2345
t|Franschhoek||-33.9072|19.1191
t|Sodwana||-27.5333|32.6833
t|Nairobi||-1.2921|36.8219
t|Mombasa||-4.0435|39.6682
t|Cairo||30.0444|31.2357
t|Marrakech|marrakesh|31.6295|-7.9811
t|Casablanca||33.5731|-7.5898
t|Lagos||6.5244|3.3792
t|Accra||5.6037|-0.187
t|Windhoek||-22.5609|17.0658
t|Dubai||25.2048|55.2708
t|Abu Dhabi||24.4539|54.3773
t|Doha||25.2854|51.531
t|Istanbul||41.0082|28.9784
t|Tel Aviv||32.0853|34.7818
t|Jerusalem||31.7683|35.2137
t|Tokyo||35.6762|139.6503
t|Kyoto||35.0116|135.7681
t|Osaka||34.6937|135.5023
t|Hiroshima||34.3853|132.4553
t|Nara||34.6851|135.8050
t|Sapporo||43.0618|141.3545
t|Okinawa||26.2124|127.6809
t|Yokohama||35.4437|139.638
t|Seoul||37.5665|126.978
t|Busan||35.1796|129.0756
t|Beijing|peking|39.9042|116.4074
t|Shanghai||31.2304|121.4737
t|Shenzhen||22.5431|114.0579
t|Guangzhou||23.1291|113.2644
t|Chengdu||30.5728|104.0668
t|Taipei||25.033|121.5654
t|Bangkok||13.7563|100.5018
t|Phuket||7.8804|98.3923
t|Chiang Mai||18.7883|98.9853
t|Hanoi||21.0285|105.8542
t|Ho Chi Minh|saigon|10.8231|106.6297
t|Siem Reap|angkor|13.3633|103.8564
t|Kuala Lumpur||3.139|101.6869
t|Jakarta||-6.2088|106.8456
t|Ubud||-8.5069|115.2625
t|Manila||14.5995|120.9842
t|Delhi|new delhi|28.6139|77.209
t|Mumbai|bombay|19.076|72.8777
t|Goa||15.2993|74.124
t|Jaipur||26.9124|75.7873
t|Agra||27.1767|78.0081
t|Kathmandu||27.7172|85.324
t|Colombo||6.9271|79.8612
t|Sydney||-33.8688|151.2093
t|Melbourne||-37.8136|144.9631
t|Brisbane||-27.4698|153.0251
t|Perth||-31.9505|115.8605
t|Cairns||-16.9186|145.7781
t|Gold Coast||-28.0167|153.4
t|Adelaide||-34.9285|138.6007
t|Auckland||-36.8485|174.7633
t|Wellington||-41.2866|174.7756
t|Queenstown||-45.0312|168.6626
t|Christchurch||-43.5321|172.6362
t|London||51.5074|-0.1278
t|Edinburgh||55.9533|-3.1883
t|Manchester||53.4808|-2.2426
t|Dublin||53.3498|-6.2603
t|Paris||48.8566|2.3522
t|Lyon||45.764|4.8357
t|Marseille||43.2965|5.3698
t|Bordeaux||44.8378|-0.5792
t|Berlin||52.52|13.405
t|Munich|munchen|48.1351|11.582
t|Hamburg||53.5511|9.9937
t|Frankfurt||50.1109|8.6821
t|Amsterdam||52.3676|4.9041
t|Brussels||50.8503|4.3517
t|Zurich||47.3769|8.5417
t|Geneva||46.2044|6.1432
t|Vienna|wien|48.2082|16.3738
t|Prague||50.0755|14.4378
t|Budapest||47.4979|19.0402
t|Warsaw||52.2297|21.0122
t|Krakow||50.0647|19.945
t|Rome|roma|41.9028|12.4964
t|Florence|firenze|43.7696|11.2558
t|Venice|venezia|45.4408|12.3155
t|Milan|milano|45.4642|9.19
t|Naples|napoli|40.8518|14.2681
t|Amalfi||40.634|14.6027
t|Madrid||40.4168|-3.7038
t|Barcelona||41.3851|2.1734
t|Seville|sevilla|37.3891|-5.9845
t|Lisbon|lisboa|38.7223|-9.1393
t|Porto||41.1579|-8.6291
t|Athens||37.9838|23.7275
t|Santorini||36.3932|25.4615
t|Mykonos||37.4467|25.3289
t|Dubrovnik||42.6507|18.0944
t|Reykjavik||64.1466|-21.9426
t|Oslo||59.9139|10.7522
t|Stockholm||59.3293|18.0686
t|Copenhagen||55.6761|12.5683
t|Helsinki||60.1699|24.9384
t|Moscow||55.7558|37.6173
t|St Petersburg|saint petersburg|59.9311|30.3609
t|New York|nyc,new york city|40.7128|-74.006
t|Los Angeles||34.0522|-118.2437
t|San Francisco||37.7749|-122.4194
t|Las Vegas||36.1699|-115.1398
t|Chicago||41.8781|-87.6298
t|Miami||25.7617|-80.1918
t|Orlando||28.5383|-81.3792
t|Washington dc|washington d c|38.9072|-77.0369
t|Boston||42.3601|-71.0589
t|Seattle||47.6062|-122.3321
t|San Diego||32.7157|-117.1611
t|New Orleans||29.9511|-90.0715
t|Honolulu||21.3069|-157.8583
t|Grand Canyon||36.1069|-112.1129
t|Yellowstone||44.428|-110.5885
t|Yosemite||37.8651|-119.5383
t|Toronto||43.6532|-79.3832
t|Vancouver||49.2827|-123.1207
t|Montreal||45.5017|-73.5673
t|Banff||51.1784|-115.5708
t|Mexico City||19.4326|-99.1332
t|Cancun||21.1619|-86.8515
t|Havana||23.1136|-82.3666
t|Rio de Janeiro|rio|-22.9068|-43.1729
t|Sao Paulo||-23.5505|-46.6333
t|Buenos Aires||-34.6037|-58.3816
t|Santiago||-33.4489|-70.6693
t|Machu Picchu||-13.1631|-72.545
t|Cusco|cuzco|-13.5319|-71.9675
t|Bogota||4.711|-74.0721
c|Jordan||31.2|36.5
"""
_GAZ = {"idx": None, "maxn": 1}


def _gaz_index():
    if _GAZ["idx"] is None:
        idx = {}
        for line in GAZETTEER_TEXT.strip().splitlines():
            kind, name, aliases, lat, lon = line.split("|")
            for a in [name] + [x for x in aliases.split(",") if x]:
                key = " ".join(re.findall(r"[a-z0-9]+", _fold(a)))
                if key:
                    idx.setdefault(key, (name, float(lat), float(lon), kind))
                    _GAZ["maxn"] = max(_GAZ["maxn"], len(key.split()))
        _GAZ["idx"] = idx
    return _GAZ["idx"]


def _path_names(path, roots, limit=None):
    """Folder names from the one holding the file outwards, down to (and including) the folder the user added."""
    p = Path(path)
    names = []
    rts = [Path(r) for r in (roots or [])]
    root = next((r for r in rts if r == p.parent or r in p.parents), None)
    cur = p.parent
    while True:
        names.append(cur.name)
        if root is None and limit is None and False:
            break
        if (limit and len(names) >= limit) or (root is not None and cur == root) or cur.parent == cur:
            break
        cur = cur.parent
    return [n for n in names if n]


def guess_place(path, roots=None, limit=None):
    """A best-guess place from the folder names: {"place","lat","lon","kind","folder"} or None. Nearest folder wins; a city beats a country."""
    idx = _gaz_index()
    for folder in _path_names(path, roots, limit):
        words = re.findall(r"[a-z0-9]+", _fold(PAREN_RE.sub("", folder)))
        best = None
        for n in range(min(_GAZ["maxn"], len(words)), 0, -1):          # the longest name first: "New York" before "York"
            hits = [idx[k] for k in (" ".join(words[i:i + n]) for i in range(len(words) - n + 1)) if k in idx]
            if hits:
                best = next((h for h in hits if h[3] == "t"), hits[0])    # a city beats a country
                break
        if best:
            return {"place": best[0], "lat": best[1], "lon": best[2], "kind": "city" if best[3] == "t" else "country", "folder": folder}
    return None


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
           "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
_YMD = re.compile(r"(?<!\d)((?:19|20)\d\d)[-_. /]?(0[1-9]|1[0-2])[-_. /]?(0[1-9]|[12]\d|3[01])(?!\d)")
_YM = re.compile(r"(?<!\d)((?:19|20)\d\d)[-_. /](0[1-9]|1[0-2])(?!\d)")
_MY = re.compile(r"(?<![a-z])(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*[\s,._-]*((?:19|20)\d\d)(?!\d)", re.I)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")


def date_hint_from_name(name, now=None):
    """What a folder name says about when its pictures were taken: {"year","month","day","precision","epoch"} or None."""
    now = now or time.time()
    this_year = time.gmtime(now).tm_year

    def mk(y, mo, d, prec):
        try:
            t = datetime(y, mo or 7, d or (15 if mo else 1), 12, 0, 0, tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
        if y > this_year or t > now + 86400:
            return None
        return {"year": y, "month": mo, "day": d, "precision": prec, "epoch": int(t)}
    years = set(_YEAR.findall(name))
    if len(years) > 1 and not _YMD.search(name):
        return None                                           # "2015-2017": a range says nothing precise
    m = _YMD.search(name)
    if m:
        return mk(int(m.group(1)), int(m.group(2)), int(m.group(3)), "day")
    m = _YM.search(name)
    if m:
        return mk(int(m.group(1)), int(m.group(2)), None, "month")
    m = _MY.search(name)
    if m:
        return mk(int(m.group(2)), _MONTHS.get(m.group(1).lower()) or _MONTHS.get(m.group(1).lower()[:3]), None, "month")
    m = _YEAR.search(name)
    if m:
        return mk(int(m.group(1)), None, None, "year")
    return None


def date_hint_from_path(path, roots=None, now=None, limit=None):
    for folder in _path_names(path, roots, limit):
        h = date_hint_from_name(folder, now)
        if h:
            h["folder"] = folder
            return h
    return None


def date_problem(hint, cur, now=None):
    """None, or why a photo's date looks wrong: "missing", "future", "year" (does not fit the folder's year) or "month"."""
    now = now or time.time()
    if cur is None:
        return "missing"
    if cur > now + 2 * 86400:
        return "future"
    if not hint:
        return None
    t = time.gmtime(cur)
    y = hint["year"]
    if hint["precision"] == "year":
        lo = datetime(y, 1, 1, tzinfo=timezone.utc).timestamp() - 2 * 86400
        hi = datetime(y + 1, 1, 1, tzinfo=timezone.utc).timestamp() + 2 * 86400
        return None if lo <= cur < hi else "year"
    if t.tm_year != y and abs(cur - hint["epoch"]) > 45 * 86400:
        return "year"
    if hint["month"] and t.tm_year == y and abs(t.tm_mon - hint["month"]) > 1 and abs(cur - hint["epoch"]) > 45 * 86400:
        return "month"
    return None


def date_args(epoch, is_video):
    dt = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    if is_video:
        return ["-api", "QuickTimeUTC=1", f"-QuickTime:CreateDate={dt}", f"-QuickTime:ModifyDate={dt}",
                f"-QuickTime:MediaCreateDate={dt}", f"-QuickTime:TrackCreateDate={dt}"]
    return [f"-AllDates={dt}", f"-XMP:DateCreated={dt}"]


def strip_date_args(args):
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "-api" and i + 1 < len(args) and args[i + 1].startswith("QuickTimeUTC"):
            skip = True
            continue
        if a.startswith(("-AllDates=", "-XMP:DateCreated=", "-QuickTime:CreateDate=", "-QuickTime:ModifyDate=", "-QuickTime:MediaCreateDate=", "-QuickTime:TrackCreateDate=")):
            continue
        out.append(a)
    return out


def gps_args(lat, lon, is_video, guessed=True):
    if is_video:
        c = f"{lat}, {lon}, 0"
        return [f"-Keys:GPSCoordinates={c}", f"-UserData:GPSCoordinates={c}"]
    a = [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}", f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    if guessed:
        a += [f"-XMP-dc:Subject-={GUESS_TAG}", f"-XMP-dc:Subject+={GUESS_TAG}"]
    return a


GUESS_TAG = "Backstory: location guessed from folder name"


def apply_sanity(m, args, ext, d, ex_now, final_taken, row, exif_args, taken):
    """Folder-name date and place helpers for one file. Returns (exif_args, taken, final_taken, used, ex_now). Fills gaps; fixes
    a wrong year only in 'fix' mode; never touches a location that exists."""
    fd = getattr(args, "folder_dates", "") or ""
    gg = bool(getattr(args, "guess_gps", False))
    is_video = ext in VIDEO_EXT
    used = False
    if not ex_now:
        ex_now = read_existing(m, is_video)
    if ex_now.get("_failed"):
        return exif_args, taken, final_taken, False, ex_now
    now = time.time()
    cur = final_taken or (_epoch(ex_now.get("date", "")) if ex_now.get("date") and not ex_now["date"].startswith("0000") else None)
    hint = date_hint_from_path(m, getattr(args, "roots", None))
    prob = date_problem(hint, cur, now)
    if prob in ("future", "year", "month") and not (fd == "fix" and prob in ("future", "year") and hint):
        row["date_flag"] = prob                                  # reported, not changed
        row["date_note"] = {"future": "this photo's date is in the future, so it is wrong",
                            "year": "the date does not fit the year in the folder name ('%s')" % (hint or {}).get("folder", ""),
                            "month": "the date is a few months away from the folder name ('%s')" % (hint or {}).get("folder", "")}[prob]
    if fd and hint and ((prob == "missing") or (fd == "fix" and prob in ("future", "year"))) and ext not in NO_WRITE_EXT:
        before = ex_now.get("date", "") if prob != "missing" else ""
        exif_args = strip_date_args(exif_args) + date_args(hint["epoch"], is_video)
        taken = final_taken = hint["epoch"]
        row["date"] = "added" if prob == "missing" else "replaced"
        row["date_before"] = (str(before) or "")[:19] if prob != "missing" else ""
        row["date_fix"] = "filled" if prob == "missing" else "corrected"
        row["date_google"] = time.strftime("%Y:%m:%d %H:%M:%S", time.gmtime(hint["epoch"])) + " (from the folder name)"
        row["date_flag"] = prob if prob != "missing" else ""
        row["date_note"] = "date set from the folder name '%s'%s" % (hint["folder"], "" if prob == "missing" else " (the photo said %s)" % (str(before)[:10] or "a wrong date"))
        used = True
    if gg and ex_now.get("lat") is None and not (row.get("gps") in ("added", "replaced", "same", "kept")):
        pl = guess_place(m, getattr(args, "roots", None))
        if pl:
            exif_args = exif_args + gps_args(pl["lat"], pl["lon"], is_video, ext not in NO_WRITE_EXT and not is_video)
            row["gps"], row["gps_google"], row["gps_guess"] = "added", "%.4f, %.4f" % (pl["lat"], pl["lon"]), "%s (%s, from folder '%s')" % (pl["place"], pl["kind"], pl["folder"])
            used = True
    return exif_args, taken, final_taken, used, ex_now


def process(m, idx, args, out_root):
    sc, how = find_sidecar(m, idx)
    if how == "tree-ambiguous":
        sc = sc[0] if m_is_dupe(m, args) else pick_closest(m, sc)
    row = {"file": str(m), "sidecar": str(sc) if sc else "", "match": how or "", "status": "",
           **{k: "" for k in REPORT_FIELDS if k not in ("file", "sidecar", "match", "status")}}
    kept = (getattr(args, "dupes", None) or {}).get(str(m))
    if kept:
        row["status"], row["detail"] = "duplicate", f"identical to {kept}"
        return row
    skipped = (getattr(args, "skip", None) or {}).get(str(m))
    if skipped:
        row["status"], row["detail"] = "left-out", skipped
        return row
    prev = already_done(m, args)
    if prev:
        row["status"], row["output"] = "already-done", prev
        return row
    ext = m.suffix.lower()
    cid = None
    if getattr(args, "pair_live", False) and ext in (".mp4", ".mov"):
        cid, why = (args.live_plan.get(str(m)) or live_id(m))
        row["live"] = "paired" if cid else why
    if sc and how == "tree":                 # a match found only by file name, in another folder: sanity-check it against the photo's own date
        dj = load_json(sc)
        tj = (ts(dj, "photoTakenTime") or ts(dj, "creationTime")) if dj else None
        have_ = read_existing(m, ext in VIDEO_EXT).get("date", "")
        ee = _epoch(have_) if have_ else None
        if tj and ee and abs(tj - ee) > 3 * 86400:
            sc, row["sidecar"], row["match"] = None, "", "tree-rejected"
            row["detail"] = "a same-named info file in another folder was ignored: its date is far from this photo's own date"
    d = load_json(sc) if sc else None
    status0 = "no-json" if not sc else "bad-json"
    read_failed = False
    if d is None and getattr(args, "name_dates", False) and ext not in NO_WRITE_EXT:
        nt = date_from_name(m.name)
        if nt:
            ex0 = read_existing(m, ext in VIDEO_EXT)
            have = ex0.get("date", "")
            if not ex0.get("_failed") and (not have or have.startswith("0000")):          # only fills a missing date, never changes one
                d = {"photoTakenTime": {"timestamp": str(nt)}}
                row["match"] = "filename-date"
                row["detail"] = "date taken from the file name"
    skip, final_taken = set(), None
    ex_now = {}
    if d and ext not in NO_WRITE_EXT:
        ex_now = read_existing(m, ext in VIDEO_EXT)
        if ex_now.get("_failed"):          # never write over something we could not read
            d, read_failed = None, True
            row["detail"] = "could not read the file's existing information, so it was left untouched"
        else:
            cl = classify(d, ext, ex_now, args.overwrite, getattr(args, "date_policy", "earlier"))
            final_taken = cl.pop("_taken_final", None)
            row.update(cl)
            skip = {k for k in ("date", "gps", "desc") if row.get(k) in ("same", "kept", "none")}
    exif_args, taken = build_args(d, ext, args.overwrite, skip) if d else ([], None)
    if ex_now.get("rating") and "-XMP:Rating=5" in exif_args:
        exif_args.remove("-XMP:Rating=5")          # a rating you gave yourself is not replaced
    sanity = False
    carry = (getattr(args, "carry", None) or {}).get(str(m))
    if carry and ext not in NO_WRITE_EXT and not read_failed:
        if not ex_now:
            ex_now = read_existing(m, ext in VIDEO_EXT)
        if not ex_now.get("_failed"):
            if "lat" in carry and ex_now.get("lat") is None:
                exif_args += gps_args(carry["lat"], carry["lon"], ext in VIDEO_EXT, False)
                row["gps"], row["gps_google"], row["gps_guess"] = "added", "%.5f, %.5f" % (float(carry["lat"]), float(carry["lon"])), "copied from a near-identical duplicate that was left out"
                sanity = True
            if "desc" in carry and not ex_now.get("desc") and ext not in VIDEO_EXT:
                exif_args += [f"-XMP-dc:Description={carry['desc']}", f"-ImageDescription={carry['desc']}"]
                sanity = True
            if carry.get("keywords") and ext not in VIDEO_EXT:
                for k in carry["keywords"]:
                    exif_args += [f"-XMP-dc:Subject-={k}", f"-XMP-dc:Subject+={k}"]
                sanity = True
            if sanity:
                row["detail"] = (row.get("detail") or "") + " kept the best copy and carried over what the left-out copy had"
                if not d:
                    d = {"_sanity": True}
    if ext not in NO_WRITE_EXT and not read_failed and (ex_now or getattr(args, "folder_dates", "") or getattr(args, "guess_gps", False)):
        exif_args, taken, final_taken, san2, ex_now = apply_sanity(m, args, ext, d, ex_now, final_taken, row, exif_args, taken)
        sanity = sanity or san2
        if sanity and not d:
            d = {"_sanity": True}
    if final_taken:
        taken = final_taken  # file times follow the date that actually won
    dest, at_dest = None, False
    if out_root:
        first = dest_dir_for(m, taken, args, out_root) / m.name
        adopt = False
        if str(first) != str(m):
            try:  # an identical copy was already placed here (for example by Sort): fix that one instead of copying again
                adopt = first.exists() and first.stat().st_size == m.stat().st_size and file_hash(first) == file_hash(m)
            except OSError:
                adopt = False
        if str(first) == str(m):  # already where it belongs (output folder is one of the sources): fix it in place
            dest, at_dest = m, True
            with args.lock:
                args.claimed.add(_ckey(m))
        elif adopt:
            with args.lock:                           # only adopt a copy nobody else is working on right now
                if _ckey(first) in args.claimed:
                    adopt = False
                else:
                    args.claimed.add(_ckey(first))
            if adopt:
                dest, at_dest = first, True
                row["detail"] = "identical copy already in the destination: fixed there"
            else:
                dest = claim_dest(first, args)
        else:
            dest = claim_dest(first, args)
        row["output"] = str(dest)
    if args.dry_run:
        row["status"] = "would-update" if d else status0
        row["detail"] = " ".join(exif_args)[:200] if d else ""
        return row
    target = dest if (out_root and at_dest) else m
    if out_root and not at_dest:
        dest.parent.mkdir(parents=True, exist_ok=True)
        place_file(m, dest, getattr(args, "move", False))
        target = dest
    if d and exif_args and ext not in NO_WRITE_EXT:
        ok, msg = run_exiftool(target, exif_args, True, sidecar_for_raw=ext in RAW_EXT)
        row["status"] = "updated" if ok else "exiftool-error"
        row["detail"] = msg[:300]
    elif d:
        row["status"] = "mtime-only"
    else:
        row["status"] = "exiftool-error" if read_failed else status0
    if cid:
        before_pair = target
        target, err = pair_live(target, cid)
        if err:
            row["live"] = "pair-error"
            row["detail"] = (row["detail"] + " " + err).strip()[:300]
        else:
            row["output"] = str(target)
            if target != before_pair:  # renamed to .MOV in place: keep its Google info file attached by name
                for sfx in (".json", ".supplemental-metadata.json"):
                    oj, nj = Path(str(before_pair) + sfx), Path(str(target) + sfx)
                    if oj.exists() and not nj.exists():
                        try:
                            oj.rename(nj)
                        except OSError:
                            pass
    if taken:
        try:
            os.utime(target, (taken, taken))
        except OSError:
            pass
    if out_root:
        record_progress(args, m, target)
    return row


def sort_one(m, idx, args, out_root):
    """Sorting only: merge same-named folders and skip duplicates. No metadata is read or written."""
    row = {k: "" for k in REPORT_FIELDS}
    row["file"] = str(m)
    kept = (getattr(args, "dupes", None) or {}).get(str(m))
    if kept:
        row["status"], row["detail"] = "duplicate", f"identical to {kept}"
        return row
    prev = already_done(m, args)
    if prev:
        row["status"], row["output"] = "already-done", prev
        return row
    first = dest_dir_for(m, None, args, out_root) / m.name
    if str(first) == str(m):  # sorting in place and this file is already where it belongs
        row["status"], row["output"] = "already-done", str(m)
        return row
    try:
        if first.exists() and first.stat().st_size == m.stat().st_size and file_hash(first) == file_hash(m):
            row["status"], row["output"] = "already-done", str(first)
            if not args.dry_run:
                record_progress(args, m, first)
            return row
    except OSError:
        pass
    dest = claim_dest(first, args)
    row["output"] = str(dest)
    sc = None
    if getattr(args, "bring_json", False):
        sc, how = find_sidecar(m, idx)
        if how == "tree-ambiguous":
            sc = pick_closest(m, sc)
        if sc:
            row["sidecar"], row["match"] = str(sc), how
    if args.dry_run:
        row["status"] = "would-place"
        return row
    dest.parent.mkdir(parents=True, exist_ok=True)
    place_file(m, dest, getattr(args, "move", False))
    if sc:  # keep the info file next to its photo so metadata can be fixed afterwards
        jd = dest.with_name(dest.name + ".json")
        if not jd.exists():
            safe_copy(sc, jd)
    row["status"] = "placed"
    record_progress(args, m, dest)
    return row


def m_is_dupe(m, args):
    return str(m) in (getattr(args, "dupes", None) or {})



# ---------------------------------------------------------------- Part 4: convert old videos to MP4
CONVERT_FIELDS = ["file", "status", "mode", "output", "size_before", "size_after", "original", "detail"]
LEGACY_EXT = (".avi", ".mov", ".mpg", ".mpeg", ".wmv", ".3gp", ".flv", ".mkv", ".mts", ".m2ts", ".vob")
DEFAULT_EXT = (".avi", ".mpg", ".mpeg", ".wmv", ".3gp", ".flv", ".mkv", ".mts", ".m2ts", ".vob")
ORIGINALS_DIR = "_original_videos"
QUALITY_CRF = {"veryhigh": 16, "high": 20, "small": 24}


def have_ffmpeg():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe_video(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None
    streams = d.get("streams", [])
    v = next((x for x in streams if x.get("codec_type") == "video"
              and not x.get("disposition", {}).get("attached_pic")), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    fmt = d.get("format", {})
    try:
        dur = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return {"vcodec": v and v.get("codec_name"), "acodec": a and a.get("codec_name"),
            "duration": dur, "has_video": v is not None, "w": (v or {}).get("width") or 0, "h": (v or {}).get("height") or 0}


def is_live_video(path):
    """A .MOV sitting next to a still with the same name is almost certainly an iPhone Live Photo video."""
    return any(path.with_suffix(e).exists() for e in (".HEIC", ".heic", ".HEIF", ".heif", ".JPG", ".jpg", ".JPEG", ".jpeg"))


def convert_mode(info):
    """remux = rewrap, nothing re-encoded (lossless, fast); audio = video kept, audio converted to AAC;
    encode = convert video to H.264 and audio to AAC."""
    if info["vcodec"] in ("h264", "hevc"):
        return "remux" if info["acodec"] in (None, "aac", "mp3", "ac3") else "audio"
    return "encode"


def _dur_ok(a, b):
    return b > 0 and (a <= 0 or abs(a - b) <= max(1.0, 0.02 * a))


def scan_legacy(roots, exts):
    out = []
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != ORIGINALS_DIR]
            for f in files:
                if Path(f).suffix.lower() in exts and not f.startswith("._"):
                    out.append((Path(dirpath) / f, Path(root)))
    return out


def sample_encode_ratio(src, info, crf, seg=8.0, should_stop=None):
    """Estimate how big the re-encoded video will be relative to the original, by test-encoding a short
    sample from the middle with the real settings. Returns output_bytes / input_bytes_for_that_part, or None."""
    dur = info.get("duration") or 0
    try:
        total = Path(src).stat().st_size
    except OSError:
        return None
    if dur < 1 or total <= 0:
        return None
    seg = min(seg, dur)
    start = max(0.0, dur / 2 - seg / 2)
    fd, tmp = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        vf = ("yadif=deint=interlaced," if info.get("vcodec") == "mpeg2video" else "") + "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{seg:.2f}", "-i", str(src),
               "-map", "0:v:0", "-map", "0:a?", "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-f", "mp4", tmp]
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        out = os.path.getsize(tmp)
        if r.returncode != 0 or out <= 0:
            return None
        return out / (total * seg / dur)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def count_legacy(roots, exts=LEGACY_EXT):
    """Quick census of convertible videos per type: count, bytes, and how many .mov are Live Photo videos."""
    out = {}
    for p, _ in scan_legacy(roots, set(exts)):
        e = p.suffix.lower()
        d = out.setdefault(e, {"n": 0, "bytes": 0, "live": 0})
        d["n"] += 1
        try:
            d["bytes"] += p.stat().st_size
        except OSError:
            pass
        if e == ".mov" and is_live_video(p):
            d["live"] += 1
    return out


def _run_ffmpeg(src, part, info, mode, crf, duration, progress, should_stop=None):
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a?"]
    if mode == "remux":
        cmd += ["-c", "copy"] + (["-tag:v", "hvc1"] if info["vcodec"] == "hevc" else [])
    elif mode == "audio":
        cmd += ["-c:v", "copy"] + (["-tag:v", "hvc1"] if info["vcodec"] == "hevc" else []) + ["-c:a", "aac", "-b:a", "192k"]
    else:
        vf = ("yadif=deint=interlaced," if info["vcodec"] == "mpeg2video" else "") + "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "medium",
                "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-map_metadata", "0", "-movflags", "+faststart+use_metadata_tags", "-f", "mp4",
            "-progress", "pipe:1", "-nostats", str(part)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    errs, speed = [], None
    for line in p.stdout:
        line = line.strip()
        if should_stop and should_stop():
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            return False, "cancelled"
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                secs = int(line.split("=")[1]) / 1e6
                if progress and duration:
                    progress(min(secs, duration), speed)
            except ValueError:
                pass
        elif line.startswith("speed="):
            try:
                speed = float(line.split("=")[1].strip().rstrip("x"))
            except ValueError:
                pass
        elif "=" not in line and line:
            errs.append(line)
    p.wait()
    return p.returncode == 0, " ".join(errs[-3:])


def _copy_video_tags(src, dest):
    """Best effort: carry dates and GPS across with exiftool (ffmpeg keeps most, not all, of them)."""
    try:
        tool().run(["-m", "-P", "-overwrite_original", "-api", "QuickTimeUTC=1", "-TagsFromFile", str(src),
                    "-QuickTime:CreateDate", "-QuickTime:ModifyDate", "-QuickTime:MediaCreateDate",
                    "-QuickTime:TrackCreateDate", "-Keys:GPSCoordinates", "-UserData:GPSCoordinates", str(dest)])
    except (OSError, RuntimeError):
        pass


def convert_file(src, root, opts, progress=None):
    """Convert one .avi/.mov to .mp4. opts: crf, action (keep|move|delete), include_live, dry_run."""
    row = {k: "" for k in CONVERT_FIELDS}
    row["file"] = str(src)
    try:
        row["size_before"] = src.stat().st_size
    except OSError as e:
        row["status"], row["detail"] = "failed", str(e)
        return row
    if src.suffix.lower() == ".mov" and not opts.get("include_live") and is_live_video(src):
        row["status"], row["detail"] = "skipped-live", "Live Photo video: kept as .MOV so Photos keeps the pair"
        return row
    info = probe_video(src)
    if not info or not info["has_video"]:
        row["status"], row["detail"] = "unreadable", "ffprobe could not read a video stream"
        return row
    row["mode"] = convert_mode(info)
    final, resumed = src.with_suffix(".mp4"), False
    if final.exists():
        oi = probe_video(final)
        try:
            same_stamp = abs(final.stat().st_mtime - src.stat().st_mtime) < 2.5       # a conversion copies the original's time
        except OSError:
            same_stamp = False
        if oi and oi["has_video"] and same_stamp and _dur_ok(info["duration"], oi["duration"]):
            resumed = True
        else:
            final = unique_dest(src.with_name(src.stem + "_converted.mp4"))
    row["output"] = str(final)
    if opts.get("dry_run"):
        row["status"] = "would-finish" if resumed else "would-convert"
        return row
    if not resumed:
        part = src.with_name(src.name + ".part")
        ok, err = _run_ffmpeg(src, part, info, row["mode"], opts.get("crf", 20), info["duration"], progress, opts.get("should_stop"))
        if err == "cancelled":
            part.unlink(missing_ok=True)
            row["status"], row["detail"] = "cancelled", "stopped by you; run again to continue"
            return row
        oi = probe_video(part) if ok and part.exists() else None
        if not (oi and oi["has_video"] and _dur_ok(info["duration"], oi["duration"])):
            part.unlink(missing_ok=True)
            row["status"], row["detail"] = "failed", (err or "output failed verification")[:300]
            return row
        os.replace(part, final)
        _copy_video_tags(src, final)
        try:
            st = src.stat()
            os.utime(final, (st.st_atime, st.st_mtime))
        except OSError:
            pass
        for sc in (src.name + ".json", src.name + ".supplemental-metadata.json"):
            sp = src.with_name(sc)
            if sp.exists():
                dp = final.with_name(final.name + sc[len(src.name):])
                if not dp.exists():
                    safe_copy(sp, dp)
    row["size_after"] = final.stat().st_size
    action = opts.get("action", "keep")
    try:
        if action == "delete":
            src.unlink()
            row["original"] = "deleted"
        elif action == "move":
            dest = Path(root) / ORIGINALS_DIR / src.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest = unique_dest(dest)
            safe_move(str(src), str(dest))
            row["original"] = f"moved to {dest}"
        else:
            row["original"] = "kept"
    except OSError as e:
        row["original"], row["detail"] = "not removed", f"converted, but could not remove the original: {e}"
    row["status"] = "already-converted" if resumed else "converted"
    return row


# ---------------------------------------------------------------- Part 5: remove empty folders
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r", ".localized"}
BUNDLE_SUFFIXES = (".photoslibrary", ".app", ".imovielibrary", ".fcpbundle", ".fcpxlibrary", ".bundle",
                   ".framework", ".lrdata", ".lrcat-data", ".xcodeproj", ".pkg", ".plugin", ".git")


def is_junk(name, ignore_junk):
    low = name.lower()
    return ignore_junk and (low in JUNK_NAMES or name.startswith("._"))


def is_bundle(name):
    return name.lower().endswith(BUNDLE_SUFFIXES)


def find_empty_dirs(root, ignore_junk=True, extra_ignored=None, include_root=False):
    """Folders below root that hold no files at all, however deep (a folder is empty only if everything in it is).

    Symbolic links, app/library bundles and unreadable folders count as content and are never entered.
    Returns (empty_dirs deepest-first, junk_files, scanned_count, skipped_count)."""
    order, content, blocked = [], set(), set()

    def onerror(e):
        blocked.add(getattr(e, "filename", None))
    for dirpath, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
        all_dirs, keep = list(dirs), []
        for d in dirs:
            p = os.path.join(dirpath, d)
            if os.path.islink(p) or is_bundle(d):
                content.add(p)
            else:
                keep.append(d)
        dirs[:] = keep
        order.append((dirpath, all_dirs, files))
    empty, junk = set(), []
    for dirpath, all_dirs, files in reversed(order):
        extra = extra_ignored or ()
        if any(not is_junk(f, ignore_junk) and os.path.join(dirpath, f) not in extra for f in files):
            continue
        if all(os.path.join(dirpath, d) in empty for d in all_dirs):
            empty.add(dirpath)
            junk += [os.path.join(dirpath, f) for f in files]
    if not include_root:
        empty.discard(str(root))
        empty.discard(os.fspath(root))
    ordered = sorted(empty, key=lambda p: -len(Path(p).parts))
    return ordered, junk, len(order), len(content) + len([b for b in blocked if b])


# ---------------------------------------------------------------- files with no (or a missing) extension
EXT_MAP = {"m4v": "mp4", "jpe": "jpg", "jpeg": "jpg"}


def bogus_ext(n):
    """True for names like 'IMG_1.fullsizerender': a long made-up 'extension' that is not a real file type."""
    suf = n.rsplit(".", 1)[-1] if "." in n else ""
    return len(suf) >= 8 and suf.isalpha()


def find_extensionless(folders):
    out = []
    for f in folders:
        for dp, dns, fns in os.walk(f, followlinks=False):
            dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d) or d == "_unrecognised")]
            for n in fns:
                if ("." not in n or bogus_ext(n)) and not n.startswith("._") and n.lower() not in JUNK_NAMES:
                    out.append(Path(dp) / n)
    return out


def detect_types(paths, progress=None, chunk=400):
    """Real file type from the file's contents. Returns {path: {"ext": 'jpg' or None, "warn": text or None}}."""
    out, done = {}, 0
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        fd, arg = tempfile.mkstemp(suffix=".args")
        os.close(fd)
        try:
            Path(arg).write_text("\n".join(str(p) for p in part), encoding="utf-8", errors="surrogateescape")
            r = subprocess.run(["exiftool", "-charset", "filename=utf8", "-j", "-FileTypeExtension", "-Warning", "-@", arg],
                               capture_output=True, text=True)
            try:
                for item in json.loads(r.stdout or "[]"):
                    out[item["SourceFile"]] = {"ext": item.get("FileTypeExtension"), "warn": item.get("Warning")}
            except ValueError:
                pass
        finally:
            Path(arg).unlink(missing_ok=True)
        done += len(part)
        if progress:
            progress("detecting types", done, len(paths))
    return out


def _companions(path):
    """Google info files that belong to a media file with this exact name (name.json, name.supplemental-metadata.json...)."""
    name, d = path.name, path.parent
    found = []
    try:
        for f in os.listdir(d):
            if f.endswith(".json") and f.startswith(name) and json_key(Path(f)) == name.lower():
                found.append((f, f[len(name):]))
    except OSError:
        pass
    return found


STRONG_MAGIC = [(b"\xff\xd8\xff", "JPEG"), (b"\x89PNG\r\n\x1a\n", "PNG"), (b"GIF87a", "GIF"), (b"GIF89a", "GIF"),
                (b"%PDF-", "PDF"), (b"PK\x03\x04", "ZIP"), (b"\x1a\x45\xdf\xa3", "MKV/WebM"), (b"FLV\x01", "FLV")]
FFPROBE_EXT = {"mov,mp4,m4a,3gp,3g2,mj2": "mp4", "matroska,webm": "mkv", "avi": "avi", "mpegts": "ts", "mpeg": "mpg",
               "asf": "wmv", "flv": "flv", "gif": "gif", "png_pipe": "png", "jpeg_pipe": "jpg", "webp_pipe": "webp",
               "tiff_pipe": "tif", "mp3": "mp3", "wav": "wav", "ogg": "ogg"}


def probe_container_ext(path):
    """Second opinion from ffprobe for files exiftool does not recognise (some video/audio containers)."""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return FFPROBE_EXT.get(r.stdout.strip()) if r.returncode == 0 else None


def diagnose_unknown(path):
    """A plain-language reason why a file's type could not be recognised."""
    try:
        with open(path, "rb") as fh:
            buf = fh.read(65536)
            size = os.fstat(fh.fileno()).st_size
    except OSError as e:
        return f"could not be read ({e})"
    if size == 0:
        return "the file is empty (0 bytes)"
    if buf.count(0) == len(buf):
        return "filled with zeros: a failed copy or a damaged part of the disk" + (" (first 64 KB checked)" if size > len(buf) else "")
    for sig, name in STRONG_MAGIC:
        i = buf.find(sig, 1)
        if i > 0:
            return f"contains {name} data starting at byte {i}: the first {i} bytes are damaged or extra"
    j = buf.find(b"ftyp", 1)
    if 4 <= j < 4096:
        return f"contains MP4/MOV data starting at byte {j - 4}: the first {j - 4} bytes are damaged or extra"
    if size < 1024 and all(32 <= c < 127 or c in (9, 10, 13) for c in buf[:512]):
        return "looks like text: " + buf[:60].decode("ascii", "replace").replace("\n", " ")
    head = " ".join(f"{c:02x}" for c in buf[:12])
    return f"unknown or truncated binary data (starts with {head}); {size / 1024:.0f} KB"


def fix_extensions(folders, dry_run, progress=None, rename_json=True, aside=False):
    """Give files that have no extension the right one, detected from their contents; keep their .json attached."""
    cands = find_extensionless(folders)
    det = detect_types(cands, progress)
    rows = []
    for k, p in enumerate(cands, 1):
        info = det.get(str(p), {})
        ext = info.get("ext")
        row = {"path": str(p), "new": "", "ext": "", "action": "", "detail": "", "json": 0, "size": 0}
        try:
            row["size"] = p.stat().st_size
        except OSError:
            pass
        if not ext and row["size"] > 0:
            ext = probe_container_ext(p)          # second opinion for containers exiftool does not know
            if ext:
                row["detail"] = "identified by ffprobe"
        bogus = bogus_ext(p.name)
        if not ext and bogus and row["size"] > 0:
            continue                               # an odd name we cannot identify: leave it where it is
        if ext and ("." + EXT_MAP.get(ext, ext)) not in MEDIA_EXT:
            ext = None                                    # a document, text file...: not ours to rename
        if not ext:
            row["action"] = "empty" if row["size"] == 0 else "unrecognised"
            row["detail"] = diagnose_unknown(p)
            if aside:
                root = next((Path(f) for f in folders if Path(f) in p.parents), p.parent)
                dest = root / "_unrecognised" / p.relative_to(root)
                row["new"] = str(dest)
                if dry_run:
                    row["action"] += " (would move aside)"
                else:
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        safe_move(str(p), _free_name(str(dest)) if dest.exists() else str(dest))
                        row["action"] += " (moved aside)"
                    except OSError as e:
                        row["detail"] += f"; could not move aside: {e}"
        else:
            ext = EXT_MAP.get(ext, ext)
            row["ext"] = ext
            target = p.with_name((p.name.rsplit(".", 1)[0] if bogus else p.name) + "." + ext)
            if target.exists():
                target = Path(_free_name(str(target)))
            row["new"] = str(target)
            comps = _companions(p) if rename_json else []
            row["json"] = len(comps)
            if info.get("warn"):
                row["detail"] = ((row["detail"] + "; ") if row["detail"] else "") + "may be damaged: " + str(info["warn"])[:80]
            if dry_run:
                row["action"] = "would-rename"
            else:
                try:
                    os.rename(p, target)
                    row["action"] = "renamed"
                    for f, rest in comps:
                        newj = p.parent / (target.name + rest)
                        if not newj.exists():
                            os.rename(p.parent / f, newj)
                except OSError as e:
                    row["action"], row["detail"] = "failed", str(e)
        rows.append(row)
        if progress and (k % 50 == 0 or k == len(cands)):
            progress("renaming", k, len(cands))
    return rows


SAFESAVE_RE = re.compile(r"\.sb-[0-9a-f]{8}-[A-Za-z0-9]{6}$")   # macOS 'safe save' temporary files, e.g. photo.gif.sb-33880f7b-LYvy9p

JUNK_KINDS = {
    "system": lambda n, sz=0: is_junk(n, True),
    "ithmb": lambda n, sz=0: n.lower().endswith(".ithmb"),                  # iPod / iTunes photo thumbnail caches
    "picasa": lambda n, sz=0: n.lower() in (".picasa.ini", "picasa.ini"),
    "thm": lambda n, sz=0: n.lower().endswith(".thm"),                      # camera video thumbnails
    "safesave": lambda n, sz=0: sz == 0 and bool(SAFESAVE_RE.search(n)),    # only EMPTY ones: a non-empty one may hold unsaved work
    "empty": lambda n, sz=0: sz == 0,                                       # zero-byte files of any kind
}


def find_safesave_kept(folders):
    """Safe-save temporary files that are NOT empty: never deleted, only reported."""
    out = []
    for f in folders:
        for dp, dns, fns in os.walk(f, followlinks=False):
            dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d))]
            for n in fns:
                if SAFESAVE_RE.search(n):
                    p = os.path.join(dp, n)
                    try:
                        if os.path.getsize(p) > 0:
                            out.append(p)
                    except OSError:
                        pass
    return out


def find_junk(folders, kinds):
    """Files that nothing needs (system leftovers, thumbnail caches). Returns [(path, size, kind)]."""
    items = []
    for f in folders:
        for dp, dns, fns in os.walk(f, followlinks=False):
            dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d))]
            for n in fns:
                p = os.path.join(dp, n)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = -1
                for k in kinds:
                    test = JUNK_KINDS.get(k)
                    if test and test(n, size):
                        items.append((p, max(size, 0), k))
                        break
    return items


def remove_empty_dir(path, ignore_junk=True):
    """Remove one folder if (and only if) it is still empty. Never deletes real files."""
    try:
        for n in os.listdir(path):
            if is_junk(n, ignore_junk) and not os.path.isdir(os.path.join(path, n)):
                os.remove(os.path.join(path, n))
        os.rmdir(path)  # refuses if anything is left
        return True, ""
    except OSError as e:
        return False, str(e)


# ---------------------------------------------------------------- Part 3b: tidy names
PAREN_RE = re.compile(r"\s*\(\d{1,2}\)$")
COPY_RE = re.compile(r"\s+(?:-\s*)?copy(?:\s+\d{1,2}|\s*\(\d{1,2}\))?$", re.I)


def clean_name(name, opts, is_file):
    """Remove duplicate-copy markers like ' (1)' or ' copy 2' and stray spaces. Years such as '(2019)' are kept."""
    stem, ext = os.path.splitext(name) if is_file else (name, "")
    for _ in range(3):
        before = stem
        if opts.get("paren"):
            stem = PAREN_RE.sub("", stem)
        if opts.get("copy"):
            stem = COPY_RE.sub("", stem)
        if stem == before:
            break
    if opts.get("spaces"):
        stem = re.sub(r"\s{2,}", " ", stem).strip()
    return stem + ext if stem.strip() else name


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _same_content(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and _sha256(a) == _sha256(b)
    except OSError:
        return False


def _free_name(path):
    p = Path(path)
    i = 1
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return str(cand)
        i += 1


def merge_dir(src, dst, dupes_action, stats):
    """Move everything from src into the existing folder dst; never overwrites. Identical files are duplicates."""
    for dp, dns, fns in os.walk(src, topdown=True, followlinks=False):
        dns[:] = [d for d in dns if not os.path.islink(os.path.join(dp, d))]
        rel = os.path.relpath(dp, src)
        tdir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(tdir, exist_ok=True)
        for f in fns:
            sp, tp = os.path.join(dp, f), os.path.join(tdir, f)
            if is_junk(f, True):
                os.remove(sp)
                continue
            if not os.path.exists(tp):
                safe_move(sp, tp)
                stats["moved"] += 1
            elif _same_content(sp, tp):
                stats["dupes"] += 1
                if dupes_action == "delete":
                    os.remove(sp)
                else:
                    aside = os.path.join(os.path.dirname(dst), "_duplicates", os.path.basename(dst), rel if rel != "." else "")
                    os.makedirs(aside, exist_ok=True)
                    safe_move(sp, _free_name(os.path.join(aside, f)) if os.path.exists(os.path.join(aside, f)) else os.path.join(aside, f))
            else:
                safe_move(sp, _free_name(tp))
                stats["moved"] += 1
                stats["conflicts"] += 1
    for dp, _, _ in os.walk(src, topdown=False):
        try:
            os.rmdir(dp)
        except OSError:
            pass


def _preview_merge(src, dst, stats):
    for dp, dns, fns in os.walk(src, topdown=True, followlinks=False):
        rel = os.path.relpath(dp, src)
        tdir = dst if rel == "." else os.path.join(dst, rel)
        for f in fns:
            if is_junk(f, True):
                continue
            sp, tp = os.path.join(dp, f), os.path.join(tdir, f)
            if not os.path.exists(tp):
                stats["moved"] += 1
            elif _same_content(sp, tp):
                stats["dupes"] += 1
            else:
                stats["moved"] += 1
                stats["conflicts"] += 1


def tidy_names(roots, opts, dry_run, progress=None):
    """Rename (and, when the clean name already exists, merge) folders; optionally files. Roots themselves are kept."""
    rows = []
    dirs = []
    for root in roots:
        for dp, dns, _ in os.walk(root, topdown=True, followlinks=False):
            keep = []
            for d in dns:
                p = os.path.join(dp, d)
                if os.path.islink(p) or is_bundle(d) or d.startswith("."):
                    continue
                keep.append(d)
                dirs.append(p)
            dns[:] = keep
    dirs.sort(key=lambda p: -len(Path(p).parts))  # innermost first, so parents are renamed after their contents
    todo = [(p, clean_name(os.path.basename(p), opts, False)) for p in dirs]
    todo = [(p, n) for p, n in todo if n != os.path.basename(p)]
    claimed = set()
    total = len(todo)
    for i, (p, new) in enumerate(todo, 1):
        target = os.path.join(os.path.dirname(p), new)
        row = {"kind": "folder", "old": p, "new": target, "action": "", "moved": 0, "dupes": 0, "conflicts": 0, "detail": ""}
        try:
            same = os.path.exists(target) and os.path.samefile(p, target)
            if (not os.path.exists(target) and target not in claimed) or same:
                row["action"] = "would-rename" if dry_run else "renamed"
                if dry_run:
                    claimed.add(target)
                else:
                    os.rename(p, target)
            elif os.path.isdir(target) or target in claimed:
                stats = {"moved": 0, "dupes": 0, "conflicts": 0}
                row["action"] = "would-merge" if dry_run else "merged"
                if dry_run:
                    if os.path.isdir(target):
                        _preview_merge(p, target, stats)
                    else:  # target will itself be created by an earlier rename in this run
                        stats["moved"] = sum(len(f) for _, _, f in os.walk(p))
                else:
                    merge_dir(p, target, opts.get("dupes", "delete"), stats)
                row.update(stats)
            else:
                row["action"], row["detail"] = "skipped", "a file with that name already exists"
        except OSError as e:
            row["action"], row["detail"] = "failed", str(e)
        rows.append(row)
        if progress:
            progress("folders", i, total)
    if opts.get("files"):
        files = []
        for root in roots:
            for dp, dns, fns in os.walk(root, topdown=True, followlinks=False):
                dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d) or d.startswith("."))]
                for f in fns:
                    if f.startswith(".") or f.lower().endswith(".json"):
                        continue
                    new = clean_name(f, opts, True)
                    if new != f:
                        files.append((os.path.join(dp, f), os.path.join(dp, new)))
        total2 = len(files)
        for i, (old, new) in enumerate(files, 1):
            row = {"kind": "file", "old": old, "new": new, "action": "", "moved": 0, "dupes": 0, "conflicts": 0, "detail": ""}
            try:
                if os.path.exists(new) or new in claimed:
                    row["action"], row["detail"] = "skipped", "a file with the clean name already exists"
                elif dry_run:
                    row["action"] = "would-rename"
                    claimed.add(new)
                else:
                    comps = _companions(Path(old))            # its Google info files, whatever suffix they carry
                    os.rename(old, new)
                    row["action"] = "renamed"
                    for fname, rest in comps:
                        oj, nj = os.path.join(os.path.dirname(old), fname), new + rest
                        if os.path.exists(oj) and not os.path.exists(nj):
                            os.rename(oj, nj)
            except OSError as e:
                row["action"], row["detail"] = "failed", str(e)
            rows.append(row)
            if progress:
                progress("files", i, total2)
    return rows


# ---------------------------------------------------------------- Merge folders (general purpose)
CONFLICTS_DIR = "_merge_conflicts"
DUPES_DIR = "_duplicates"
MERGE_SKIP_DIRS = {CONFLICTS_DIR, DUPES_DIR}


def check_merge_roots(roots, dest, move):
    """Refuse setups that could loop or lose data: nested sources, destination inside a source."""
    rs = [Path(r).resolve() for r in roots]
    for r in rs:
        if inside_photos_library(r):
            raise ValueError(f"{r.name} is a Photos library. Merge folders does not work on those: use Guided or Fix to copy its photos out (it is only ever read).")
    for i, a in enumerate(rs):
        for j, b in enumerate(rs):
            if i != j and (a == b or b in a.parents):
                raise ValueError(f"{roots[i]} and {roots[j]} overlap (one is inside the other)")
    if dest:
        d = Path(dest).resolve()
        for i, r in enumerate(rs):
            if d != rs[0] and (d == r or r in d.parents or d in r.parents):
                raise ValueError("The destination cannot be inside a source folder (or contain one), except the first source folder itself")
    elif not move:
        raise ValueError("Choose a destination, or tick Move to merge everything into the first source folder")
    if len(rs) < 2 and not dest:
        raise ValueError("Add at least two source folders to merge")


def merge_trees(roots, dest, opts, dry_run=False, on_progress=None, on_item=None):
    """Bring several folder trees together. Folders with the same path merge; identical files are kept once;
    different files with the same name are resolved by opts['conflict'] (both|newer|larger|first); a losing
    file is set aside in _merge_conflicts, never deleted.
    opts['takeout']: ignore 'Takeout N / Google Photos' wrapper folders and carry each photo's .json along.
    opts['global_dedupe']: skip exact duplicates anywhere, not only inside the same folder.
    Returns (rows, per_root, merged_dirs)."""
    roots = [Path(r).resolve() for r in roots]
    dest = Path(dest).resolve() if dest else roots[0]
    move = bool(opts.get("move"))
    tidy, nocase = bool(opts.get("tidy")), bool(opts.get("nocase", True))
    conflict, dupes = opts.get("conflict", "both"), opts.get("dupes", "delete")
    takeout, gdedupe = bool(opts.get("takeout")), bool(opts.get("global_dedupe"))
    name_opts = {"paren": tidy, "copy": tidy, "spaces": tidy}
    casemap, claimed, rows = {}, {}, []
    per_root = [{"root": str(r), "found": 0, "placed": 0, "identical": 0, "conflicts": 0, "failed": 0} for r in roots]
    dir_sources = defaultdict(set)

    def canon(parts):
        parts = list(parts)
        if takeout:
            while parts and _TAKEOUT_WRAP.match(parts[0]):
                parts.pop(0)
        out = []
        for p in parts:
            name = clean_name(p, name_opts, False) if tidy else p
            if nocase:
                name = casemap.setdefault((tuple(out), name.lower()), name)
            out.append(name)
        return out

    items, json_files = [], []
    for ri, root in enumerate(roots):
        for dp, dns, fns in os.walk(root, topdown=True, followlinks=False):
            keep = []
            for d in dns:
                p = os.path.join(dp, d)
                if d in MERGE_SKIP_DIRS or os.path.islink(p):
                    continue
                if is_bundle(d):
                    items.append((ri, p, Path(dp).relative_to(root).parts, d, True))
                else:
                    keep.append(d)
            dns[:] = keep
            rel = Path(dp).relative_to(root).parts
            for f in fns:
                if is_junk(f, True):
                    continue
                if takeout and f.lower().endswith(".json"):
                    json_files.append((ri, Path(dp) / f, rel))   # carried along with its photo, or placed at the end if unmatched
                    continue
                items.append((ri, os.path.join(dp, f), rel, f, False))
    idx = build_index([p for _, p, _ in json_files]) if json_files else None
    used_json = set()
    dupe_of = plan_duplicates([Path(it[1]) for it in items if not it[4]])[0] if gdedupe else {}
    state = {"k": 0, "total": len(items)}
    manifest = {}
    lock = threading.Lock()
    prog = argparse.Namespace(lock=lock, out_root=str(dest), dry_run=dry_run, manifest=manifest, manifest_file=MANIFEST_MERGE)
    if not dry_run:
        manifest.update(load_manifest(dest, MANIFEST_MERGE))

    def aside(base, rel_dir, name):
        d = dest / base / Path(*rel_dir) if rel_dir else dest / base
        d.mkdir(parents=True, exist_ok=True)
        return _free_name(str(d / name)) if (d / name).exists() else str(d / name)

    def put(src, target, is_bundle_item):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_bundle_item:
            safe_move(src, str(target)) if move else safe_copytree(src, str(target))
        else:
            place_file(Path(src), target, move)

    def json_for(src, final):
        """Copy the Google info file of src next to its placed copy, named after the placed file."""
        if idx is None or state.get("leftover"):
            return 0
        sc, how = find_sidecar(Path(src), idx)
        if how == "tree-ambiguous":
            sc = pick_closest(Path(src), sc)
        if not sc:
            return 0
        used_json.add(str(sc))
        jd = Path(str(final) + ".json")
        if not dry_run:
            if jd.exists():
                return 0
            jd.parent.mkdir(parents=True, exist_ok=True)
            safe_copy(sc, jd)
        return 1

    def json_mark(src):
        """A skipped duplicate's own .json is not a separate file to place."""
        if idx is None:
            return
        sc, how = find_sidecar(Path(src), idx)
        if how == "tree-ambiguous":
            sc = pick_closest(Path(src), sc)
        if sc:
            used_json.add(str(sc))

    def process_item(ri, src, rel, name, is_b):
        per_root[ri]["found"] += 1
        row = {"root": str(roots[ri]), "src": src, "dest": "", "status": "", "detail": "", "json": 0}
        try:
            done = manifest.get(src)
            if done and Path(done).exists():
                row["status"], row["dest"] = "already-done", done
                per_root[ri]["placed"] += 1
                return row
            dparts = canon(rel)
            target = dest.joinpath(*dparts, name)
            dir_key = str(dest.joinpath(*dparts))
            if src in dupe_of:  # identical to a copy that is kept elsewhere
                per_root[ri]["identical"] += 1
                dir_sources[dir_key].add(ri)
                row["status"], row["detail"] = "identical", "identical to " + os.path.basename(dupe_of[src]) + " kept elsewhere"
                json_mark(src)
                if move and not dry_run and not _same_file(src, dupe_of[src]):
                    if dupes == "delete":
                        os.remove(src)
                    else:
                        safe_move(src, aside(DUPES_DIR, dparts, name))
                    row["detail"] += "; extra copy " + ("deleted" if dupes == "delete" else "moved to _duplicates")
                return row
            row["dest"] = str(target)
            if str(target) == src:
                row["status"] = "in-place"
                claimed[str(target)] = src
                per_root[ri]["placed"] += 1
                dir_sources[dir_key].add(ri)
                return row
            existing = str(target) if target.exists() else claimed.get(str(target))
            if existing and os.path.exists(existing) and _same_file(src, existing):
                row["status"] = "in-place"                  # the very same file reached by another path: nothing to do, never delete it
                per_root[ri]["placed"] += 1
                dir_sources[dir_key].add(ri)
                return row
            if existing and not is_b and _same_content(src, existing):
                per_root[ri]["identical"] += 1
                dir_sources[dir_key].add(ri)
                row["status"] = "identical"
                json_mark(src)
                if move and not dry_run:
                    if dupes == "delete":
                        os.remove(src)
                    else:
                        safe_move(src, aside(DUPES_DIR, dparts, name))
                    row["detail"] = "extra copy " + ("deleted" if dupes == "delete" else "moved to _duplicates")
            elif existing:
                per_root[ri]["conflicts"] += 1
                dir_sources[dir_key].add(ri)
                if conflict == "both" or is_b:
                    alt = Path(_free_name(str(target)))
                    while str(alt) in claimed:
                        alt = Path(_free_name(str(alt)))
                    row["status"], row["dest"], row["detail"] = "kept-both", str(alt), f"second file named {alt.name}"
                    claimed[str(alt)] = src
                    if not dry_run:
                        put(src, alt, is_b)
                    row["json"] = json_for(src, alt)
                else:
                    try:
                        es, ss = os.stat(existing), os.stat(src)
                    except OSError:
                        es = ss = None
                    incoming_wins = bool(es and ((conflict == "newer" and ss.st_mtime > es.st_mtime) or
                                                 (conflict == "larger" and ss.st_size > es.st_size)))
                    if incoming_wins:
                        row["status"], row["detail"] = "replaced", f"{conflict} file takes the name; the other is in {CONFLICTS_DIR}"
                        if not dry_run:
                            if target.exists():
                                safe_move(str(target), aside(CONFLICTS_DIR, dparts, name))
                                oj = Path(str(target) + ".json")
                                if oj.exists():
                                    safe_move(str(oj), aside(CONFLICTS_DIR, dparts, name + ".json"))
                            put(src, target, is_b)
                        claimed[str(target)] = src
                        row["json"] = json_for(src, target)
                    else:
                        row["status"], row["detail"] = "kept-existing", f"this file is in {CONFLICTS_DIR}"
                        side = aside(CONFLICTS_DIR, dparts, name) if not dry_run else str(dest / CONFLICTS_DIR / name)
                        if not dry_run:
                            put(src, side, is_b)
                        row["json"] = json_for(src, side)
            else:
                row["status"] = "placed"
                claimed[str(target)] = src
                per_root[ri]["placed"] += 1
                dir_sources[dir_key].add(ri)
                if not dry_run:
                    put(src, target, is_b)
                row["json"] = json_for(src, target)
            if not dry_run and row["status"] in ("placed", "kept-both", "replaced", "in-place"):
                record_progress(prog, src, row["dest"])
        except OSError as e:
            row["status"], row["detail"] = "failed", str(e)
            per_root[ri]["failed"] += 1
        return row

    def finish(row):
        rows.append(row)
        state["k"] += 1
        if on_item:
            on_item(row)
        if on_progress and (state["k"] % 25 == 0 or state["k"] == state["total"]):
            on_progress(state["k"], state["total"])

    for ri, src, rel, name, is_b in items:
        finish(process_item(ri, src, rel, name, is_b))
    leftovers = [(ri, p, rel) for ri, p, rel in json_files if str(p) not in used_json]   # .json files that matched no photo
    state["total"] += len(leftovers)
    state["leftover"] = True
    for ri, p, rel in leftovers:
        finish(process_item(ri, str(p), rel, p.name, False))
    if move and not dry_run and opts.get("prune", True):
        prune_empty_dirs(roots)
    merged = {d: sorted(v) for d, v in dir_sources.items() if len(v) > 1}
    return rows, per_root, merged


# ---- Zip support: read Google Takeout .zip files without unzipping everything first --------------------------
import zipfile
import zlib

ZIP_STAGE = ".metadatafixer_stage"
ZIPS_LOG = ".metadatafixer_zips.jsonl"


def inside_photos_library(p):
    """True for a Photos library (.photoslibrary) or anything inside one. Backstory only ever reads those."""
    q = Path(p)
    return any(part.lower().endswith(".photoslibrary") for part in q.parts)


def split_sources(entries):
    """Sort what the user added into zip files and ordinary folders.
    A folder that holds .zip files at its top level counts as those zips."""
    zips, folders, seen = [], [], set()
    for r in entries:
        p = Path(r).expanduser()
        if p.is_file() and p.suffix.lower() == ".zip":
            found = [p]
        elif p.is_dir() and p.suffix.lower() == ".photoslibrary":
            orig = next((p / n for n in ("originals", "Masters") if (p / n).is_dir()), None)
            if orig is None:
                raise ValueError(f"{p.name} does not look like a Photos library (no originals folder)")
            if orig.resolve() not in seen:
                seen.add(orig.resolve())
                folders.append(orig)
            continue
        elif p.is_dir():
            found = sorted(q for q in p.iterdir() if q.is_file() and q.suffix.lower() == ".zip" and not q.name.startswith("._"))
            other = any(q.is_dir() or (q.is_file() and q.suffix.lower() in MEDIA_EXT) for q in p.iterdir())
            if not found or other:                    # a folder with zips AND photos/subfolders: use both, never drop one
                if p.resolve() not in seen:
                    seen.add(p.resolve())
                    folders.append(p)
                if not found:
                    continue
        else:
            raise ValueError(f"Not a folder or zip file: {p}")
        for q in found:
            if q.resolve() not in seen:
                seen.add(q.resolve())
                zips.append(q)
    return zips, folders


def _safe_member(name):
    """Relative path inside the zip, or None when the name is unsafe (absolute or climbing out with ..)."""
    parts = [x for x in name.replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or ".." in parts or name.startswith("/") or (len(parts[0]) == 2 and parts[0][1] == ":"):
        return None
    return parts


def _wanted_media(parts):
    n = parts[-1]
    if n.startswith("._") or n.lower() in JUNK_NAMES or n.startswith("."):
        return False
    return Path(n).suffix.lower() in MEDIA_EXT or "." not in n or bogus_ext(n)


def zip_plan(zips):
    """Quick look inside each zip (reads only its table of contents)."""
    plan = []
    for z in zips:
        item = {"path": str(z), "name": z.name, "media": 0, "json": 0, "bytes": 0, "error": "", "sizes": []}
        try:
            with zipfile.ZipFile(z) as zf:
                for i in zf.infolist():
                    if i.is_dir():
                        continue
                    parts = _safe_member(i.filename)
                    if not parts:
                        continue
                    if parts[-1].lower().endswith(".json"):
                        item["json"] += 1
                    elif _wanted_media(parts):
                        item["media"] += 1
                        item["bytes"] += i.file_size
                        item["sizes"].append(i.file_size)
        except (zipfile.BadZipFile, OSError) as e:
            item["error"] = f"{type(e).__name__}: {e}"[:200]
        plan.append(item)
    return plan


def _extract_member(zf, info, parts, tree):
    target = Path(tree).joinpath(*parts)
    root = os.path.realpath(tree)
    if os.path.commonpath([root, os.path.realpath(target.parent)]) != root:
        raise OSError("unsafe path in zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    except BaseException:
        try:
            os.unlink(target)                               # never leave a half-written file behind
        except OSError:
            pass
        raise
    try:
        t = time.mktime(info.date_time + (0, 0, -1))
        os.utime(target, (t, t))
    except (OverflowError, ValueError, OSError):
        pass
    return target


def stage_json(zips, tree, should_stop=None):
    """Copy every .json file from every zip into one folder tree, so a photo can find its info file in another zip."""
    n = 0
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            for i in zf.infolist():
                if i.is_dir():
                    continue
                parts = _safe_member(i.filename)
                if parts and parts[-1].lower().endswith(".json"):
                    if should_stop:
                        should_stop()
                    try:
                        _extract_member(zf, i, parts, tree)
                        n += 1
                    except ZIP_MEMBER_ERRORS:
                        pass                                # one odd member must not stop the whole job
    return n


ZIP_MEMBER_ERRORS = (OSError, zipfile.BadZipFile, RuntimeError, zlib.error, NotImplementedError, EOFError)


def stage_media(zpath, tree, should_stop=None, bad=None):
    """Extract the photos and videos of one zip into the same tree. Returns the extracted paths."""
    out = []
    with zipfile.ZipFile(zpath) as zf:
        for i in zf.infolist():
            if i.is_dir():
                continue
            parts = _safe_member(i.filename)
            if parts and not parts[-1].lower().endswith(".json") and _wanted_media(parts):
                if should_stop:
                    should_stop()
                try:
                    out.append(_extract_member(zf, i, parts, tree))
                except ZIP_MEMBER_ERRORS as ex:
                    if bad is not None:
                        bad.append("%s: %s" % (i.filename, ex))
    return out


def file_sig(path):
    """(size, CRC32) of a file: a cheap fingerprint for spotting the same photo in two different zips."""
    import zlib
    crc = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return (os.path.getsize(path), crc)


def zip_key(z):
    st = Path(z).stat()
    return f"{Path(z).name}|{st.st_size}|{int(st.st_mtime)}"


def zips_done(dest):
    done = set()
    try:
        with open(Path(dest) / ZIPS_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["zip"])
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return done


def mark_zip_done(dest, z):
    with open(Path(dest) / ZIPS_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"zip": zip_key(z)}) + "\n")


# ---- Check my files: a read-only look at real data, so the app can recommend what to do --------------------
def assess(entries, dest="", progress=None, should_stop=None, sample_n=120):
    """Facts about the user's Takeout (zip files and/or folders). Reads names and sizes (zip tables of contents are read
    without unpacking), then a small random sample of files for their existing metadata. Changes nothing."""
    import random
    import tempfile
    from collections import Counter
    from pathlib import PurePosixPath
    stop = should_stop or (lambda: None)
    say = progress or (lambda *a: None)
    zips, folders, zip_owner, folder_owner, labels = [], [], {}, {}, []
    for ei, ent in enumerate(entries):
        z_, f_ = split_sources([ent])
        zips += [z for z in z_ if z not in zip_owner]
        folders += [f for f in f_ if f not in folder_owner]
        zip_owner.update({z: ei for z in z_})
        folder_owner.update({f: ei for f in f_})
        pe = Path(ent).expanduser()
        labels.append(pe.name if pe.suffix.lower() != ".photoslibrary" else pe.name)
    own, src_stat = {}, {}
    F = {"zips": len(zips), "folders": len(folders), "media": 0, "media_bytes": 0, "json": 0, "by_ext": {}, "wrapper": False,
         "junk": 0, "zero_media": 0, "zero_other": 0, "extless": 0, "legacy": {}, "legacy_n": 0, "legacy_bytes": 0,
         "live_pairs": 0, "dup_n": 0, "dup_bytes": 0, "dup_exact": bool(zips), "empty_dirs": 0, "tidy_dirs": 0,
         "matched": 0, "unmatched": 0, "name_date_candidates": 0, "bad_zips": [], "zip_gaps": [], "zip_bytes": 0, "biggest_zip": 0,
         "sample": None, "free": None, "need": None, "no_ext_bogus": 0, "photos_libs": sum(1 for f in folders if inside_photos_library(f)),
         "sources": [], "overlap": []}
    items = []                       # (virtual path, size, crc or None, source, member)
    jsons = []                       # (virtual path, source, member)
    plan = zip_plan(zips) if zips else []
    for z in plan:
        if z["error"]:
            F["bad_zips"].append((z["name"], z["error"]))
    nums = sorted(int(m.group(1)) for z in zips for m in [re.search(r"-(\d{3})\.zip$", z.name)] if m)
    if nums:
        F["zip_gaps"] = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
    for z in zips:
        stop()
        say("Reading the contents of %s" % z.name, 0, 0)
        try:
            F["zip_bytes"] += z.stat().st_size
            F["biggest_zip"] = max(F["biggest_zip"], z.stat().st_size)
            with zipfile.ZipFile(z) as zf:
                for i in zf.infolist():
                    if i.is_dir():
                        continue
                    parts = _safe_member(i.filename)
                    if parts:
                        items.append((PurePosixPath(*parts), i.file_size, i.CRC, z, i.filename))
                        own[(z, i.filename)] = zip_owner.get(z, 0)
        except (zipfile.BadZipFile, OSError):
            continue
    nfold = 0
    for fo in folders:
        for dp, dns, fns in os.walk(fo, followlinks=False):
            stop()
            dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d))]
            nfold += 1
            if nfold % 200 == 0:
                say("Looking through %s" % Path(fo).name, 0, 0)
            if not fns and not dns:
                F["empty_dirs"] += 1
            for d in dns:
                if re.search(r" \(\d+\)$| copy( \d+)?$|  +", d):
                    F["tidy_dirs"] += 1
            for n in fns:
                p = Path(dp) / n
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                items.append((PurePosixPath(*p.parts[1:]) if p.is_absolute() else PurePosixPath(*p.parts), sz, None, p, ""))
                own[(p, "")] = folder_owner.get(fo, 0)
    media = []
    for vp, sz, crc, src, mem in items:
        n = vp.name
        low = n.lower()
        ext = vp.suffix.lower()
        if any(x.lower() in ("takeout", "google photos") for x in vp.parts):
            F["wrapper"] = True
        if low.endswith(".json"):
            F["json"] += 1
            F["json_bytes"] = F.get("json_bytes", 0) + sz
            jsons.append((vp, src, mem))
            continue
        if n.startswith("._") or low in JUNK_NAMES or ext in (".ithmb", ".thm") or low in ("picasa.ini", ".picasa.ini"):
            F["junk"] += 1
            continue
        if SAFESAVE_RE.search(n) and sz == 0:
            F["junk"] += 1
            continue
        if ext in MEDIA_EXT or "." not in n or bogus_ext(n):
            if sz == 0:
                F["zero_media"] += 1
                continue
            if ext not in MEDIA_EXT:
                F["extless"] += 1
            else:
                F["by_ext"][ext] = F["by_ext"].get(ext, 0) + 1
            F["media"] += 1
            F["media_bytes"] += sz
            media.append((vp, sz, crc, src, mem))
            oi = own.get((src, mem), 0)
            st_ = src_stat.setdefault(oi, {"media": 0, "bytes": 0})
            st_["media"] += 1
            st_["bytes"] += sz
            if ext in LEGACY_EXT:
                d = F["legacy"].setdefault(ext, [0, 0])
                d[0] += 1
                d[1] += sz
        elif sz == 0:
            F["zero_other"] += 1
    F["legacy_n"] = sum(v[0] for v in F["legacy"].values())
    F["legacy_bytes"] = sum(v[1] for v in F["legacy"].values())
    # Live Photo pairs: a still and a video with the same name in the same folder
    stills, vids = set(), set()
    for vp, *_ in media:
        e = vp.suffix.lower()
        key = (str(vp.parent), vp.stem.lower())
        if e in (".heic", ".jpg", ".jpeg", ".heif"):
            stills.add(key)
        elif e in (".mov", ".mp4"):
            vids.add(key)
    F["live_pairs"] = len(stills & vids)
    F["edited_pairs"] = len(find_edited_pairs([m[0] for m in media]))
    fdir = {}
    raw_pairs = 0
    for vp, sz, crc, src, mem in media:
        fdir.setdefault(str(vp.parent), []).append((vp.name, sz))
    for d_, ents in fdir.items():
        stems = {}
        for n_, sz_ in ents:
            stems.setdefault(os.path.splitext(n_)[0].lower(), set()).add(os.path.splitext(n_)[1].lower())
        raw_pairs += sum(1 for ex_ in stems.values() if ex_ & RAW_EXT and ex_ & {".jpg", ".jpeg", ".heic", ".heif"})
    F["raw_pairs"] = raw_pairs
    F["format_groups"] = len(find_format_duplicates({d_: e_ for d_, e_ in fdir.items() if len(e_) > 1}, False, cap=100000))
    # Matching against Google's info files (by name only)
    say("Matching photos to their info files", 0, 0)
    stop()
    idx = build_index([vp for vp, _, _ in jsons]) if jsons else None
    matched_paths = {}
    for k, (vp, sz, crc, src, mem) in enumerate(media):
        if k % 2000 == 0:
            stop()
        if vp.suffix.lower() not in MEDIA_EXT:
            continue                                  # no file type yet: counted as "missing file type"
        if idx is not None:
            sc, how = find_sidecar(vp, idx)
            if sc:
                F["matched"] += 1
                matched_paths[str(vp)] = sc[0] if how == "tree-ambiguous" else sc
                continue
        F["unmatched"] += 1
        if date_from_name(vp.name):
            F["name_date_candidates"] += 1
    # Exact duplicates
    say("Looking for exact duplicates", 0, 0)
    stop()
    by = {}
    if zips:
        for vp, sz, crc, src, mem in media:
            if sz and crc is not None and isinstance(src, Path) and src.suffix.lower() == ".zip":
                by.setdefault((sz, crc), []).append(sz)
        for g in by.values():
            if len(g) > 1:
                F["dup_n"] += len(g) - 1
                F["dup_bytes"] += g[0] * (len(g) - 1)
    sized = {}
    for vp, sz, crc, src, mem in media:
        if not (isinstance(src, Path) and src.suffix.lower() == ".zip") and sz:
            sized.setdefault(sz, []).append(src)
    cands = [g for g in sized.values() if len(g) > 1]
    done = 0
    todo = sum(len(g) for g in cands)
    for g in cands[:20000]:
        stop()
        h = {}
        for p in g:
            try:
                with open(p, "rb") as fh:
                    head = fh.read(65536)
                    fh.seek(max(0, os.path.getsize(p) - 65536))
                    tail = fh.read(65536)
                h.setdefault(hashlib.sha1(head + tail).hexdigest(), []).append(p)
            except OSError:
                pass
            done += 1
            if done % 200 == 0:
                say("Looking for exact duplicates", done, todo)
        for lst in h.values():
            if len(lst) > 1:
                F["dup_n"] += len(lst) - 1
                try:
                    F["dup_bytes"] += os.path.getsize(lst[0]) * (len(lst) - 1)
                except OSError:
                    pass
    # Overlap between the sources you added (the same photo present in more than one library)
    if len(entries) > 1:
        say("Comparing your libraries with each other", 0, 0)
        stop()
        sizes_by = {}
        for vp, sz, crc, src, mem in media:
            sizes_by.setdefault(sz, set()).add(own.get((src, mem), 0))
        sigs = {}
        computed = 0
        for vp, sz, crc, src, mem in media:
            if sz == 0 or len(sizes_by.get(sz, ())) < 2:
                continue
            oi = own.get((src, mem), 0)
            if crc is None:
                if computed >= 20000:
                    continue
                try:
                    crc = file_sig(src)[1]
                except OSError:
                    continue
                computed += 1
                if computed % 500 == 0:
                    stop()
            sigs.setdefault(oi, {})[(sz, crc)] = sz
        keys = sorted(sigs)
        for a_i in range(len(keys)):
            for b_i in range(a_i + 1, len(keys)):
                a, b = keys[a_i], keys[b_i]
                common = set(sigs[a]) & set(sigs[b])
                if common:
                    F["overlap"].append({"a": labels[a], "b": labels[b], "n": len(common), "bytes": sum(k[0] for k in common)})
    for oi in sorted(src_stat):
        ent = Path(entries[oi]).expanduser() if oi < len(entries) else Path("")
        kind = "photos library" if ent.suffix.lower() == ".photoslibrary" else ("zip files" if any(zip_owner.get(z) == oi for z in zips) else "folder")
        F["sources"].append({"label": labels[oi] if oi < len(labels) else "?", "kind": kind, "media": src_stat[oi]["media"], "bytes": src_stat[oi]["bytes"]})
    # Folder names that carry a date or a place (no file is opened for this)
    cache = {}
    hint_files = place_files = 0
    for m_ in media:
        par = "/" + str(m_[0].parent)
        if par not in cache:
            fp = par + "/x"
            cache[par] = (bool(date_hint_from_path(fp, None, None, 4)), bool(guess_place(fp, None, 4)))
        hint_files += cache[par][0]
        place_files += cache[par][1]
    F["hint_files"], F["place_files"] = hint_files, place_files
    # A small sample of real files: how many already have a date and a location, and what Google's info would add
    cand = [m for m in media if m[0].suffix.lower() in MEDIA_EXT and m[0].suffix.lower() not in NO_WRITE_EXT and 0 < m[1] <= 60 * 1024 * 1024]
    rnd = random.Random(7)
    pick = rnd.sample(cand, min(sample_n, len(cand))) if cand else []
    if pick and shutil.which("exiftool"):
        tmp = tempfile.mkdtemp(prefix="metadatafixer_assess_")
        story = []
        S = {"n": 0, "has_date": 0, "has_gps": 0, "has_desc": 0, "with_json": 0, "add_date": 0, "add_gps": 0, "add_desc": 0, "diff_date": 0, "diff_gps": 0,
             "date_odd": 0, "future": 0, "no_date_hint": 0, "gps_guess": 0}
        try:
            zfs = {}
            for k, (vp, sz, crc, src, mem) in enumerate(pick, 1):
                stop()
                say("Sampling your files", k, len(pick))
                try:
                    if isinstance(src, Path) and src.suffix.lower() == ".zip" and mem:
                        zf = zfs.get(src) or zfs.setdefault(src, zipfile.ZipFile(src))
                        real = Path(tmp) / ("m%d%s" % (k, vp.suffix))
                        with zf.open(mem) as a, open(real, "wb") as b:
                            shutil.copyfileobj(a, b, 1 << 20)
                    else:
                        real = src
                    ex = read_existing(real, vp.suffix.lower() in VIDEO_EXT)
                except Exception:
                    continue
                S["n"] += 1
                has_d = bool(ex.get("date")) and not str(ex.get("date")).startswith("0000")
                has_g = ex.get("lat") is not None and ex.get("lon") is not None
                has_t = bool(ex.get("desc"))
                S["has_date"] += has_d
                S["has_gps"] += has_g
                vpath = "/" + str(vp)
                hint_ = date_hint_from_path(vpath, None, None, 4)
                cur_ = _epoch(str(ex.get("date"))) if has_d else None
                why_ = date_problem(hint_, cur_) if (has_d and cur_) else None
                S["future"] += why_ == "future"
                S["date_odd"] += why_ == "year"
                S["no_date_hint"] += bool((not has_d) and hint_)
                S["gps_guess"] += bool((not has_g) and guess_place(vpath, None, 4))
                S["has_desc"] += has_t
                sc = matched_paths.get(str(vp))
                if sc is not None:
                    S["with_json"] += 1
                    sv = next((j for j in jsons if j[0] == sc), None)
                    try:
                        if sv and isinstance(sv[1], Path) and sv[1].suffix.lower() == ".zip":
                            with zipfile.ZipFile(sv[1]) as zf2:
                                d = json.loads(zf2.read(sv[2]).decode("utf-8", "replace"))
                        elif sv:
                            d = load_json(sv[1])
                        else:
                            d = None
                    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                        d = None
                    if d:
                        cl = classify(d, vp.suffix.lower(), ex, True, "earlier")
                        S["add_date"] += cl.get("date") in ("added", "replaced")
                        S["add_gps"] += cl.get("gps") in ("added", "replaced")
                        S["add_desc"] += cl.get("desc") in ("added", "replaced")
                        S["diff_date"] += cl.get("date") in ("kept", "replaced")
                        if len(story) < 6 and (cl.get("date") in ("added", "replaced") or cl.get("gps") in ("added", "replaced") or cl.get("desc") in ("added", "replaced")):
                            fake = {"file": str(vp), "date": cl.get("date"), "date_before": cl.get("date_before", ""), "date_google": cl.get("date_google", ""),
                                    "gps": cl.get("gps"), "gps_before": "", "gps_google": cl.get("gps_google", ""),
                                    "desc": cl.get("desc"), "desc_before": cl.get("desc_before", ""), "desc_google": cl.get("desc_google", ""), "output": "", "status": "", "live": "", "match": ""}
                            c_ = story_from_row(fake, None, thumb=False)
                            c_["thumb"] = thumb_b64(real)
                            c_["where"] = str(vp.parent)
                            if ex.get("lat") is not None and ex.get("lon") is not None:
                                c_["before"]["gps"] = "%.5f, %.5f" % (float(ex["lat"]), float(ex["lon"]))
                            if ex.get("desc"):
                                c_["before"]["desc"] = ex["desc"][:80]
                            story.append(c_)
                        S["diff_gps"] += cl.get("gps") in ("kept", "replaced")
            F["sample"] = S
            F["story"] = story
        finally:
            for zf in zfs.values():
                try:
                    zf.close()
                except Exception:
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                close_all()
            except Exception:
                pass
    # Space
    if dest:
        probe = Path(dest).expanduser()
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            F["free"] = shutil.disk_usage(probe).free
        except OSError:
            pass
    F["need"] = F["media_bytes"]
    return F


# ---- Smart folder consolidation: "Japan 2025", "delete-Japan 2025" and "Japan 2025-old" are one trip ----------
import unicodedata
import difflib

AFFIX_WORDS = {"delete", "deleted", "del", "todelete", "old", "older", "oldest", "copy", "copies", "backup", "backups", "bak", "bkp",
               "dup", "dupe", "dupes", "duplicate", "duplicates", "temp", "tmp", "new", "newer", "final", "orig", "original", "originals",
               "archive", "archived", "unused", "trash", "donotuse", "extra", "extras"}
# words that are also ordinary name parts ("Old Town", "New York", "Final Fantasy"): only ever removed from the END of a name
TRAIL_ONLY = {"old", "older", "oldest", "new", "newer", "final", "orig", "original", "originals", "extra", "extras", "archive", "archived"}


def _fold(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower() if s else s


def name_key(name):
    """What a folder name is really called: lower case, no accents, with leading/trailing words like delete, old, copy,
    backup, final and '(1)' markers removed. Returns (key, had_affix)."""
    base = PAREN_RE.sub("", name)
    base = COPY_RE.sub("", base)
    base = re.sub(r"[()\[\]{}]", " ", base)
    toks = [t for t in re.split(r"[\s_.\-]+", base) if t]
    removed = False
    while toks and _fold(toks[0]) in AFFIX_WORDS and _fold(toks[0]) not in TRAIL_ONLY and len(toks) > 1:
        toks.pop(0)
        removed = True
    while toks and _fold(toks[-1]) in AFFIX_WORDS and len(toks) > 1:
        toks.pop()
        removed = True
    key = " ".join(toks)
    return _fold(key), removed, key


def _count_files(path, cap=200000):
    n = 0
    for _, _, fns in os.walk(path):
        n += len(fns)
        if n >= cap:
            break
    return n


def find_similar_folders(roots, maybe=True):
    """Groups of sibling folders that look like the same thing under different names. Nothing is changed.
    Returns [{"parent", "confidence": "high"|"maybe", "target", "members": [{"name","files","affix"}]}]."""
    groups = []
    for root in roots:
        for dp, dns, _ in os.walk(root, topdown=True, followlinks=False):
            dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d) or d.startswith(".") or d in ("_duplicates", "_unrecognised", "_merge_conflicts", ORIGINALS_DIR))]
            kids = []
            for d in dns:
                key, removed, clean = name_key(d)
                if not key:
                    continue
                kids.append({"name": d, "key": key, "squash": key.replace(" ", ""), "affix": removed or key != _fold(d).strip(), "clean": clean})
            used = set()
            by = {}
            for k in kids:
                by.setdefault(k["squash"], []).append(k)
            for sq, lst in by.items():
                if len(lst) > 1:
                    groups.append((dp, "high", lst))
                    used.update(x["name"] for x in lst)
            if maybe:
                rest = [k for k in kids if k["name"] not in used]
                seen = set()
                bydig = {}
                for k in rest:
                    if len(k["squash"]) >= 6:
                        bydig.setdefault(re.sub(r"\D", "", k["squash"]), []).append(k)     # only names with the same numbers can match
                for lst in bydig.values():
                    if len(lst) < 2 or len(lst) > 400:
                        continue
                    for i, a in enumerate(lst):
                        if a["name"] in seen:
                            continue
                        cluster = [a]
                        for b in lst[i + 1:]:
                            if b["name"] in seen or abs(len(a["squash"]) - len(b["squash"])) > 3:
                                continue
                            sm = difflib.SequenceMatcher(None, a["squash"], b["squash"])
                            if sm.real_quick_ratio() >= 0.88 and sm.quick_ratio() >= 0.88 and sm.ratio() >= 0.88:
                                cluster.append(b)
                        if len(cluster) > 1:
                            seen.update(x["name"] for x in cluster)
                            groups.append((dp, "maybe", cluster))
    out = []
    for dp, conf, lst in groups:
        for m in lst:
            m["files"] = _count_files(os.path.join(dp, m["name"]))
        # the best name: a member that carries no extra words, with the most files; else a tidied name from the biggest member
        plain = [m for m in lst if not m["affix"]]
        if plain:
            target = max(plain, key=lambda m: m["files"])["name"]
        else:
            big = max(lst, key=lambda m: m["files"])
            target = big["clean"]
        out.append({"parent": dp, "confidence": conf, "target": target,
                    "members": sorted(({"name": m["name"], "files": m["files"], "affix": bool(m["affix"])} for m in lst), key=lambda m: -m["files"])})
    out.sort(key=lambda g: (g["confidence"] != "high", g["parent"], g["target"]))
    return out


def consolidate_groups(groups, dupes_action="delete", dry_run=True, progress=None, should_stop=None):
    """Merge each group's folders into its target folder (renamed if needed). Never overwrites; identical files are
    duplicates; different files with the same name are kept as name_1."""
    rows = []
    total = len(groups)
    for i, g in enumerate(groups, 1):
        if should_stop:
            should_stop()
        parent, target = g["parent"], g["target"].strip()
        row = {"parent": parent, "target": target, "members": [], "moved": 0, "dupes": 0, "conflicts": 0, "action": "", "detail": ""}
        if not target or "/" in target or "\\" in target or target in (".", ".."):
            row["action"], row["detail"] = "skipped", "that is not a valid folder name"
            rows.append(row)
            continue
        mem = [m["name"] if isinstance(m, dict) else m for m in g["members"]]
        # a member must be a plain folder name directly inside the parent (never a path, never a link)
        names = [m for m in mem if isinstance(m, str) and m and m == os.path.basename(m) and m not in (".", "..")
                 and os.path.isdir(os.path.join(parent, m)) and not os.path.islink(os.path.join(parent, m))]
        if os.path.isdir(os.path.join(parent, target)) and target not in names:
            row["action"], row["detail"] = "skipped", "a different folder already has that name"
            rows.append(row)
            continue
        row["members"] = names
        if len(names) < 2 and not (names and names[0] != target):
            row["action"], row["detail"] = "skipped", "nothing to merge"
            rows.append(row)
            continue
        try:
            tpath = os.path.join(parent, target)
            others = [n for n in names if n != target]
            stats = {"moved": 0, "dupes": 0, "conflicts": 0}
            if not os.path.isdir(tpath):
                # no member already has the target name: the biggest one gets it
                big = max(others, key=lambda n: _count_files(os.path.join(parent, n)))
                if not dry_run:
                    os.rename(os.path.join(parent, big), tpath)
                others.remove(big)
                stats["moved"] += 0
            for n in others:
                src = os.path.join(parent, n)
                if dry_run:
                    if os.path.isdir(tpath):
                        _preview_merge(src, tpath, stats)
                    else:
                        stats["moved"] += sum(len(f) for _, _, f in os.walk(src))
                else:
                    merge_dir(src, tpath, dupes_action, stats)
            row.update(stats)
            row["action"] = "would-merge" if dry_run else "merged"
        except OSError as e:
            row["action"], row["detail"] = "failed", str(e)
        rows.append(row)
        if progress:
            progress("groups", i, total)
    return rows


# ---- Google's "-edited" copies ---------------------------------------------------------------------------------
EDIT_SUFFIXES = ("-edited", "-bearbeitet", "-modifié", "-modifie", "-editado", "-modificato", "-bewerkt", "-redigerad", "-redigeret",
                 "-muokattu", "-edytowane", "-upravené", "-изменено", "-отредактировано", "-編集済み", "-已修改", "-편집됨")


def find_edited_pairs(media):
    """{edited_path: original_path} for photos that Google saved twice: IMG_1.jpg and IMG_1-edited.jpg in the same folder."""
    by_dir = {}
    for m in media:
        by_dir.setdefault(m.parent, {}).setdefault(m.stem.lower(), []).append(m)
    pairs = {}
    for d, stems in by_dir.items():
        for stem, files in stems.items():
            for suf in EDIT_SUFFIXES:
                if stem.endswith(suf):
                    orig = stems.get(stem[:-len(suf)])
                    if orig:
                        for f in files:
                            pairs[f] = orig[0]
                    break
    return pairs


def is_album_folder(name):
    return not re.fullmatch(r"Photos from \d{4}", name) and name.lower() not in ("takeout", "google photos")


# ---- Similar photos: the same picture saved at different sizes or qualities ---------------------------------
SIMILAR_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
SIMILAR_SKIP_DIRS = {"_similar_set_aside", "_duplicates", ORIGINALS_DIR, "_merge_conflicts", "_unrecognised"}


HASH_CACHE = {"path": None, "map": None, "lock": threading.Lock()}


def set_hash_cache(path):
    """Where fingerprints are remembered between runs (a rescan of an unchanged library then takes seconds)."""
    HASH_CACHE["path"], HASH_CACHE["map"] = str(path), None


def _hash_cache_load():
    if HASH_CACHE["map"] is None:
        m = {}
        try:
            with open(HASH_CACHE["path"], encoding="utf-8") as fh:
                for line in fh:
                    try:
                        k, v = json.loads(line)
                        m[k] = v
                    except (ValueError, TypeError):
                        continue
        except (OSError, TypeError):
            pass
        HASH_CACHE["map"] = m
    return HASH_CACHE["map"]


def dhash_image(path):
    """dhash_image_raw with a memory of earlier answers: same path, size and modified time means the same picture."""
    if not HASH_CACHE["path"]:
        return dhash_image_raw(path)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))
    with HASH_CACHE["lock"]:
        m = _hash_cache_load()
        if key in m:
            return m[key]
    h = dhash_image_raw(path)
    with HASH_CACHE["lock"]:
        m[key] = h
        try:
            os.makedirs(os.path.dirname(HASH_CACHE["path"]), exist_ok=True)
            if os.path.exists(HASH_CACHE["path"]) and os.path.getsize(HASH_CACHE["path"]) > 60_000_000:
                os.unlink(HASH_CACHE["path"])               # keep the cache small
            with open(HASH_CACHE["path"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps([key, h]) + "\n")
        except OSError:
            pass
    return h


def dhash_image_raw(path):
    """A 64-bit fingerprint of how a picture looks (a 'difference hash'); similar pictures give fingerprints that differ
    in only a few bits. Returns an int, or None if the picture cannot be read."""
    def run(cmd, data=None):
        try:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
            return r.stdout if r.returncode == 0 else b""
        except (OSError, subprocess.TimeoutExpired):
            return b""
    vf = "scale=9:8:flags=area,format=gray"
    link = None
    if "%" in str(path):                                   # ffmpeg would read "100%d.jpg" as a numbered sequence
        try:
            fd, link = tempfile.mkstemp(suffix=Path(path).suffix.lower(), prefix="mf_hash_")
            os.close(fd)
            os.unlink(link)
            os.symlink(os.path.abspath(path), link)
            path = link
        except OSError:
            link = None
    try:
        raw = run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"])
    finally:
        if link:
            try:
                os.unlink(link)
            except OSError:
                pass
    if len(raw) != 72 and shutil.which("exiftool"):
        thumb = run(["exiftool", "-b", "-ThumbnailImage", str(path)])
        if not thumb:
            thumb = run(["exiftool", "-b", "-PreviewImage", str(path)])
        if thumb:
            raw = run(["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"], thumb)
    if len(raw) != 72:
        return None
    h = 0
    for row in range(8):
        for col in range(8):
            h = (h << 1) | (1 if raw[row * 9 + col] > raw[row * 9 + col + 1] else 0)
    return h


def _popcount(x):
    return bin(x).count("1")

# ---- Duplicate matching strictness and "which copy do we keep" rules --------------------------------------------------
KEEPER_RULES = {
    "favorite": "A favourite (rated 5 stars) beats one that is not",
    "edited": "An edited version beats an untouched one",
    "resolution": "More pixels (higher resolution) wins",
    "filesize": "A bigger file wins (less compressed)",
    "metadata": "More complete information inside (date, location, caption, title, keywords) wins",
    "album": "A photo you already sorted into an album wins",
    "keywords": "More keywords wins",
    "format": "A modern format (HEIC) beats JPEG, which beats the rest",
    "yearfolder": "A copy in a 'Photos from YYYY' folder wins",
    "oldest": "The older file wins",
    "newest": "The newer file wins",
}
DEFAULT_KEEPER = ["favorite", "edited", "resolution", "filesize", "metadata", "album", "yearfolder"]
MATCH_CRITERIA = {
    "name": "The file name must match",
    "datetime": "The date and time taken must match",
    "dimensions": "The width and height must match",
    "format": "The file format must match",
    "size": "The file size must match",
}
_EDITOR_RE = re.compile(r"photoshop|lightroom|snapseed|pixelmator|affinity|gimp|capture one|darktable|luminar|picsart|vsco|facetune|canva", re.I)
EXIF_FACT_TAGS = ["-ImageWidth", "-ImageHeight", "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude", "-Rating", "-Software",
                  "-Title", "-ImageDescription", "-Description", "-Subject", "-Keywords"]


def _as_list(v):
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def member_facts(path, size, it):
    """What we know about one copy, for choosing a keeper: it = the exiftool record (may be empty)."""
    p = Path(path)
    kws = {str(x) for x in _as_list(it.get("Subject")) + _as_list(it.get("Keywords")) if str(x)}
    name = p.name.lower()
    has_desc = bool((it.get("ImageDescription") or it.get("Description") or "").__str__().strip())
    f = {"path": str(path), "size": size, "w": it.get("ImageWidth") or 0, "h": it.get("ImageHeight") or 0,
         "date": str(it.get("DateTimeOriginal") or "")[:19], "gps": it.get("GPSLatitude") is not None,
         "lat": it.get("GPSLatitude"), "lon": it.get("GPSLongitude"), "desc": str(it.get("ImageDescription") or it.get("Description") or "").strip(),
         "fav": (it.get("Rating") or 0) >= 5 if isinstance(it.get("Rating"), (int, float)) else False,
         "edited": ("-edited" in name or "(edited)" in name or bool(_EDITOR_RE.search(str(it.get("Software") or "")))),
         "album": is_album_folder(p.parent.name) or bool(kws), "keywords": len(kws), "kw": sorted(kws)[:20],
         "title": bool(str(it.get("Title") or "").strip()), "ext": p.suffix.lower(), "year_folder": p.parent.name.startswith("Photos from")}
    f["meta"] = int(bool(f["date"])) + int(f["gps"]) + int(has_desc) + int(f["title"]) + int(bool(kws))
    try:
        f["mtime"] = os.stat(path).st_mtime
    except OSError:
        f["mtime"] = 0
    return f


def keeper_key(f, rules=None):
    """Sort key: the best copy sorts first. Rules are applied in order; the first one that tells two copies apart decides."""
    k = []
    for r in (rules if rules is not None else DEFAULT_KEEPER):
        if r == "favorite":
            k.append(not f["fav"])
        elif r == "edited":
            k.append(not f["edited"])
        elif r == "resolution":
            k.append(-(f["w"] * f["h"]))
        elif r == "filesize":
            k.append(-f["size"])
        elif r == "metadata":
            k.append(-f["meta"])
        elif r == "album":
            k.append(not f["album"])
        elif r == "keywords":
            k.append(-f["keywords"])
        elif r == "format":
            k.append({".heic": 0, ".heif": 0, ".jpg": 1, ".jpeg": 1}.get(f["ext"], 2))
        elif r == "yearfolder":
            k.append(not f["year_folder"])
        elif r == "oldest":
            k.append(f["mtime"])
        elif r == "newest":
            k.append(-f["mtime"])
    k.append(f["path"])
    return tuple(k)


def split_by_criteria(members, must):
    """Keep only copies that also agree on every ticked criterion. Returns the sub-groups that still have two or more copies."""
    must = [m for m in (must or []) if m in MATCH_CRITERIA]
    if not must:
        return [members]
    def key(f):
        out = []
        for c in must:
            if c == "name":
                out.append(DUP_RE.sub("", re.sub(r"[-_ ]?(edited|copy)$", "", Path(f["path"]).stem.lower())).strip())
            elif c == "datetime":
                out.append(f["date"] or ("?" + f["path"]))              # no date: cannot be confirmed, so never matches
            elif c == "dimensions":
                out.append((f["w"], f["h"]) if f["w"] else ("?" + f["path"]))
            elif c == "format":
                out.append({".jpeg": ".jpg", ".heif": ".heic"}.get(f["ext"], f["ext"]))
            elif c == "size":
                out.append(f["size"])
        return tuple(out)
    by = {}
    for f in members:
        by.setdefault(key(f), []).append(f)
    return [g for g in by.values() if len(g) > 1]


def carry_over(keeper, dropped):
    """What the kept copy is missing that a dropped copy had: a location, a caption, album names. Never replaces anything."""
    c = {}
    if keeper.get("lat") is None:
        d = next((x for x in dropped if x.get("lat") is not None and x.get("lon") is not None), None)
        if d:
            c["lat"], c["lon"] = d["lat"], d["lon"]
    if not keeper.get("desc"):
        d = next((x for x in dropped if x.get("desc")), None)
        if d:
            c["desc"] = d["desc"]
    kws = set(keeper.get("kw") or [])
    add = []
    for x in dropped:
        for k in x.get("kw") or []:
            if k not in kws and k not in add:
                add.append(k)
        nm = Path(x["path"]).parent.name
        if is_album_folder(nm) and nm not in kws and nm not in add:
            add.append(nm)
    if add:
        c["keywords"] = add[:30]
    return c



def find_similar_photos(roots, threshold=6, progress=None, should_stop=None, workers=6, rules=None, must=None):
    """Groups of pictures that look the same. Reads pictures only; changes nothing.
    Returns (groups, scanned): each group is a list of {"path","size","w","h","date","gps"} with the suggested keeper first."""
    from concurrent.futures import ThreadPoolExecutor
    stop = should_stop or (lambda: None)
    files = []
    for root in roots:
        for dp, dns, fns in os.walk(root, followlinks=False):
            dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d) or d in SIMILAR_SKIP_DIRS or d.startswith("."))]
            for n in fns:
                if Path(n).suffix.lower() in SIMILAR_EXT and not n.startswith("."):
                    p = os.path.join(dp, n)
                    try:
                        sz = os.path.getsize(p)
                    except OSError:
                        continue
                    if sz > 0:
                        files.append((p, sz))
        stop()
    total, done = len(files), 0
    hashes = {}
    lock = threading.Lock()

    def work(item):
        nonlocal done
        stop()
        h = dhash_image(item[0])
        with lock:
            done += 1
            if h is not None:
                hashes[item[0]] = h
            if progress and (done % 25 == 0 or done == total):
                progress("Fingerprinting your pictures", done, total)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, files))
    sizes = dict(files)
    # buckets: split the 64 bits into threshold+1 pieces; two pictures within `threshold` bits share at least one piece exactly
    n_chunks = max(2, min(16, threshold + 1))
    bounds = [round(i * 64 / n_chunks) for i in range(n_chunks + 1)]
    items = [(p, h) for p, h in hashes.items() if h not in (0, (1 << 64) - 1)]
    buckets = {}
    for idx, (p, h) in enumerate(items):
        for c in range(n_chunks):
            lo, hi = bounds[c], bounds[c + 1]
            buckets.setdefault((c, (h >> lo) & ((1 << (hi - lo)) - 1)), []).append(idx)
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for ids in buckets.values():
        if len(ids) < 2 or len(ids) > 400:
            continue
        stop()
        for a in range(len(ids)):
            ha = items[ids[a]][1]
            for b in range(a + 1, len(ids)):
                if _popcount(ha ^ items[ids[b]][1]) <= threshold:
                    ra, rb = find(ids[a]), find(ids[b])
                    if ra != rb:
                        parent[ra] = rb
    groups_idx = {}
    for i in range(len(items)):
        groups_idx.setdefault(find(i), []).append(i)
    raw = [[items[i][0] for i in g] for g in groups_idx.values() if 2 <= len(g) <= 40]
    # details for the groups only: size in pixels, date, location
    need = sorted({p for g in raw for p in g})
    info = {}
    if need and shutil.which("exiftool"):
        for k in range(0, len(need), 300):
            stop()
            fd, arg = tempfile.mkstemp(suffix=".args")
            os.close(fd)
            try:
                Path(arg).write_text("\n".join(need[k:k + 300]), encoding="utf-8", errors="surrogateescape")
                r = subprocess.run(["exiftool", "-charset", "filename=utf8", "-j", "-n", "-@", arg] + EXIF_FACT_TAGS,
                                   capture_output=True, text=True)
                for it in json.loads(r.stdout or "[]"):
                    info[it["SourceFile"]] = it
            except (ValueError, OSError):
                pass
            finally:
                Path(arg).unlink(missing_ok=True)
    groups = []
    for g in raw:
        mem = []
        for p in g:
            f = member_facts(p, sizes.get(p, 0), info.get(p, {}))
            f["hash"] = hashes[p]
            mem.append(f)
        for sub in split_by_criteria(mem, must):
            sub.sort(key=lambda f: keeper_key(f, rules))
            groups.append(sub)
    groups.sort(key=lambda g: (-len(g), g[0]["path"]))
    return groups, total


# ---- Apple Photos: import in batches, so a nearly-full Mac can take a big library over time ---------------
PHOTOS_OK_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff", ".bmp", ".webp",
                 ".nef", ".cr2", ".cr3", ".arw", ".dng", ".orf", ".raf", ".rw2", ".mov", ".mp4", ".m4v"}
PHOTOS_LOG = ".backstory_photos_import.jsonl"
PHOTOS_SKIP_DIRS = {"_similar_set_aside", "_duplicates", ORIGINALS_DIR, "_merge_conflicts", "_unrecognised"}


def plan_photos_import(root, batch_bytes, order="oldest", albums=True, done=None, limit=None):
    """Split a finished library into batches for Apple Photos. A Live Photo's still and video stay together, and the files of an
    album folder are imported into that album. Returns {"batches": [...], "unsupported": {ext: n}, "files", "bytes", "skipped_done"}."""
    done = done or set()
    root = Path(root)
    units, unsupported, skipped = {}, {}, 0
    for dp, dns, fns in os.walk(root, followlinks=False):
        dns[:] = sorted(d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d) or d in PHOTOS_SKIP_DIRS or d.startswith(".")))
        for n in sorted(fns):
            if n.startswith(".") or n.startswith("._"):
                continue
            ext = Path(n).suffix.lower()
            p = Path(dp) / n
            if ext in (".json", ".csv", ".txt", ".html", ".xmp", ".log", ".jsonl") or n.lower() in JUNK_NAMES:
                continue
            if ext not in PHOTOS_OK_EXT:
                if ext in VIDEO_EXT or ext in IMAGE_EXT or ext in RAW_EXT:
                    unsupported[ext] = unsupported.get(ext, 0) + 1
                continue
            if str(p) in done:
                skipped += 1
                continue
            try:
                str(p).encode("utf-8")                 # names that are not valid text cannot be handed to Photos
                st = p.stat()
            except (OSError, UnicodeError):
                unsupported["(unreadable name)"] = unsupported.get("(unreadable name)", 0) + 1
                continue
            rel = p.relative_to(root)
            rp = [x for x in rel.parts]
            while len(rp) > 1 and rp[0].lower() in ("takeout", "google photos"):
                rp.pop(0)                              # Takeout/Google Photos/<Album>/...: the album is the folder under that
            top = rp[0] if len(rp) > 1 else ""
            album = top if (albums and top and is_album_folder(top)) else ""
            key = (str(p.parent), p.stem.lower())
            u = units.setdefault(key, {"files": [], "bytes": 0, "mtime": st.st_mtime, "album": album, "rel": str(rel)})
            u["files"].append(str(p))
            u["bytes"] += st.st_size
            u["mtime"] = min(u["mtime"], st.st_mtime)
    ulist = list(units.values())
    ulist.sort(key=(lambda u: (u["mtime"], u["rel"])) if order == "oldest" else (lambda u: u["rel"]))
    batches, cur, cur_b, cur_n, total_f, total_b = [], [], 0, 0, 0, 0
    for u in ulist:
        if limit is not None and total_f >= limit:
            break
        cur.append(u)
        cur_b += u["bytes"]
        cur_n += len(u["files"])
        total_f += len(u["files"])
        total_b += u["bytes"]
        if cur_b >= batch_bytes or cur_n >= 4000:
            batches.append(cur)
            cur, cur_b, cur_n = [], 0, 0
    if cur:
        batches.append(cur)
    out = []
    for i, b in enumerate(batches, 1):
        alb = {}
        for u in b:
            alb[u["album"]] = alb.get(u["album"], 0) + len(u["files"])
        out.append({"index": i, "files": sum(len(u["files"]) for u in b), "bytes": sum(u["bytes"] for u in b),
                    "first": time.strftime("%Y-%m-%d", time.localtime(min(u["mtime"] for u in b))),
                    "last": time.strftime("%Y-%m-%d", time.localtime(max(u["mtime"] for u in b))),
                    "albums": {k: v for k, v in alb.items() if k}, "units": b})
    return {"batches": out, "unsupported": unsupported, "files": total_f, "bytes": total_b, "skipped_done": skipped}


def _as_quote(p):
    return str(p).replace("\\", "\\\\").replace('"', '\\"')


def applescript_import(files, album=""):
    """AppleScript that imports files into Photos (and into an album, which is created if missing). Duplicates Photos already
    has are skipped."""
    lines = ['tell application "Photos"', '  set fl to {}']
    for f in files:
        lines.append('  set end of fl to POSIX file "%s"' % _as_quote(f))
    if album:
        a = _as_quote(album)
        lines += ['  if not (exists album named "%s") then make new album named "%s"' % (a, a),
                  '  import fl into album named "%s" skip check duplicates true' % a]
    else:
        lines.append("  import fl skip check duplicates true")
    lines.append("end tell")
    return "\n".join(lines)


def _pl(n, word):
    return f"{int(n):,} {word}" + ("" if int(n) == 1 else "s")


def fmt_bytes(b):
    return f"{b / 1e9:.2f} GB" if b > 1e9 else f"{b / 1e6:.1f} MB" if b > 1e6 else f"{round(b / 1e3)} KB"


# ---- Library health: space waste, folder problems, ghosts, duplicate formats and useful statistics ---------------
FORMAT_VIDEO_EXT = VIDEO_EXT | {".flv", ".vob", ".webm", ".ts"}
HEALTH_LEFTOVER_DIRS = ("_original_videos", "_duplicates", "_similar_set_aside", "_merge_conflicts", "_unrecognised", "_older_formats")
_TEMP_SUFFIX = (".part", ".tmp", ".temp", ".bak", ".crdownload", ".download")


def find_format_duplicates(files_by_dir, probe=True, progress=None, should_stop=None, cap=600):
    """Same picture or video saved in several formats next to each other (IMG_1.mov, IMG_1.mp4, IMG_1.avi...).
    files_by_dir: {dir: [(name, size)]}. A Live Photo (a still plus a video) is not a duplicate. Returns groups, best first."""
    stop = should_stop or (lambda: None)
    cands = []
    for d, items in files_by_dir.items():
        by = {}
        for n, sz in items:
            stem, ext = os.path.splitext(n)
            ext = ext.lower()
            if ext in FORMAT_VIDEO_EXT:
                by.setdefault(("video", stem.lower()), []).append((n, sz, ext))
            elif ext in IMAGE_EXT:
                by.setdefault(("image", stem.lower()), []).append((n, sz, ext))
        for (fam, stem), mem in by.items():
            if len({m[2] for m in mem} - ({".jpeg"} if {".jpg", ".jpeg"} <= {m[2] for m in mem} else set())) >= 2:
                cands.append((d, fam, stem, mem))
    groups = []
    for k, (d, fam, stem, mem) in enumerate(cands[:cap * 4], 1):
        stop()
        if progress and k % 20 == 0:
            progress("Comparing formats", k, len(cands))
        members = []
        for n, sz, ext in mem:
            info = {"path": os.path.join(d, n), "name": n, "ext": ext, "size": sz, "duration": 0.0, "w": 0, "h": 0}
            if fam == "video" and probe and shutil.which("ffprobe"):
                pv = probe_video(info["path"]) or {}
                info["duration"], info["w"], info["h"] = pv.get("duration") or 0.0, pv.get("w") or 0, pv.get("h") or 0
            members.append(info)
        if fam == "video" and probe:
            durs = [m["duration"] for m in members if m["duration"]]
            if durs and max(durs) - min(durs) > 1.5:
                continue                                    # different lengths: not the same video
        if fam == "video":
            members.sort(key=lambda m: (m["ext"] not in (".mp4", ".m4v"), -(m["w"] * m["h"]), -m["size"], m["name"]))
        else:
            members.sort(key=lambda m: (m["ext"] not in (".heic", ".heif", ".png"), -m["size"], m["name"]))
        groups.append({"kind": fam, "dir": d, "stem": stem, "members": members})
        if len(groups) >= cap:
            break
    return groups


def health_scan(roots, deep=False, progress=None, should_stop=None, exif_cap=40000):
    """A read-only look at one or more library folders. Returns {"findings", "stats", "formats", "score", "parts", ...}."""
    import random
    from collections import Counter
    stop = should_stop or (lambda: None)
    say = progress or (lambda *a: None)
    files, dirs_info = [], {}
    by_dir = {}
    ext_n, ext_b = Counter(), Counter()
    year_n = Counter()
    F = []                                           # findings
    junk = zero = temp = ghosts_icloud = dataless = longpath = 0
    junk_b = temp_b = 0
    leftovers = {}
    legacy = {"n": 0, "bytes": 0}
    paren_dirs, space_dirs, long_paths, case_clash = [], [], [], []
    nfiles = 0
    biggest = []
    folder_bytes = Counter()
    roots = [Path(r) for r in roots]
    for root in roots:
        for dp, dns, fns in os.walk(root, followlinks=False):
            stop()
            base = os.path.basename(dp)
            if base in HEALTH_LEFTOVER_DIRS:
                size = 0
                for d2, _, f2 in os.walk(dp):
                    for n in f2:
                        try:
                            size += os.path.getsize(os.path.join(d2, n))
                        except OSError:
                            pass
                leftovers[dp] = size
                dns[:] = []
                continue
            dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d))]
            low = [d.casefold() for d in dns]
            if len(set(low)) != len(low):
                case_clash.append(dp)
            for d in dns:
                if PAREN_RE.search(d) or COPY_RE.search(d):
                    paren_dirs.append(os.path.join(dp, d))
                if d != d.strip() or "  " in d:
                    space_dirs.append(os.path.join(dp, d))
            entries = []
            for n in fns:
                p = os.path.join(dp, n)
                try:
                    st = os.stat(p, follow_symlinks=False)
                except OSError:
                    continue
                nfiles += 1
                if nfiles % 3000 == 0:
                    say("Reading your library", nfiles, 0)
                sz = st.st_size
                ext = os.path.splitext(n)[1].lower()
                entries.append((n, sz))
                folder_bytes[dp] += sz
                if len(p) > 230:
                    long_paths.append(p)
                if n.startswith(".") and n.endswith(".icloud"):
                    ghosts_icloud += 1
                    continue
                if n.startswith("._") or n.lower() in JUNK_NAMES or ext in (".ithmb", ".thm") or n.lower() in ("picasa.ini", ".picasa.ini"):
                    junk += 1
                    junk_b += sz
                    continue
                if n.lower().endswith(_TEMP_SUFFIX) or n.endswith("~"):
                    temp += 1
                    temp_b += sz
                    continue
                if sz == 0:
                    zero += 1
                    continue
                if getattr(st, "st_flags", 0) & 0x40000000 or (sz > 0 and getattr(st, "st_blocks", 1) == 0 and sys.platform == "darwin"):
                    dataless += 1                       # iCloud Drive / File Provider file that is not on this disk
                if ext in MEDIA_EXT or ext in FORMAT_VIDEO_EXT:
                    ext_n[ext] += 1
                    ext_b[ext] += sz
                    year_n[time.strftime("%Y", time.localtime(st.st_mtime))] += 1
                    biggest.append((sz, p))
                    if len(biggest) > 400:
                        biggest.sort(reverse=True)
                        del biggest[20:]
                    files.append((p, sz, ext, st.st_mtime))
                    if ext in (".avi", ".mpg", ".mpeg", ".wmv", ".3gp", ".flv", ".mkv", ".mts", ".m2ts", ".vob"):
                        legacy["n"] += 1
                        legacy["bytes"] += sz
            by_dir[dp] = entries
    biggest.sort(reverse=True)
    total_media = len(files)
    total_bytes = sum(f[1] for f in files)
    # empty folders (a folder is empty when it and everything below it holds no files)
    empty = []
    for root in roots:
        for dp, dns, fns in os.walk(root, topdown=False, followlinks=False):
            if os.path.basename(dp) in HEALTH_LEFTOVER_DIRS or Path(dp) == root:
                continue
            try:
                if not os.listdir(dp):
                    empty.append(dp)
                elif all(f.lower() in JUNK_NAMES or f.startswith("._") for f in os.listdir(dp)):
                    empty.append(dp)
            except OSError:
                pass
    # exact-ish duplicates: same size, then the first and last 64 KB
    say("Looking for duplicate files", 0, 0)
    by_size = {}
    for p, sz, ext, mt in files:
        if sz > 0:
            by_size.setdefault(sz, []).append(p)
    dup_n = dup_b = 0
    cands = [g for g in by_size.values() if len(g) > 1]
    done = 0
    for g in cands[:30000]:
        stop()
        h = {}
        for p in g:
            try:
                with open(p, "rb") as fh:
                    head = fh.read(65536)
                    fh.seek(max(0, os.path.getsize(p) - 65536))
                    tail = fh.read(65536)
                h.setdefault(hashlib.sha1(head + tail).hexdigest(), []).append(p)
            except OSError:
                pass
            done += 1
        for lst in h.values():
            if len(lst) > 1:
                dup_n += len(lst) - 1
                dup_b += os.path.getsize(lst[0]) * (len(lst) - 1)
        if done % 400 == 0:
            say("Looking for duplicate files", done, sum(len(x) for x in cands))
    # RAW next to a JPEG/HEIC with the same name
    raw_pairs = raw_pair_raw_b = raw_pair_jpg_b = 0
    raw_total = raw_only = 0
    raw_total_b = live_pairs = 0
    for d, entries in by_dir.items():
        stems = {}
        for n, sz in entries:
            stem, ext = os.path.splitext(n)
            stems.setdefault(stem.lower(), []).append((ext.lower(), sz))
        for stem, lst in stems.items():
            exts = {e for e, _ in lst}
            if ".mov" in exts and exts & {".heic", ".heif", ".jpg", ".jpeg"}:
                live_pairs += 1
            raws = [x for x in lst if x[0] in RAW_EXT]
            flat = [x for x in lst if x[0] in (".jpg", ".jpeg", ".heic", ".heif")]
            raw_total += len(raws)
            raw_total_b += sum(sz for _, sz in raws)
            if raws and flat:
                raw_pairs += 1
                raw_pair_raw_b += sum(sz for _, sz in raws)
                raw_pair_jpg_b += sum(sz for _, sz in flat)
            elif raws:
                raw_only += 1
    # duplicate formats
    say("Looking for the same file in different formats", 0, 0)
    fdir = {d: [(n, sz) for n, sz in e if os.path.splitext(n)[1].lower() in (FORMAT_VIDEO_EXT | IMAGE_EXT)] for d, e in by_dir.items()}
    fdir = {d: e for d, e in fdir.items() if len(e) > 1}
    formats = find_format_duplicates(fdir, True, lambda m, a, b: say(m, a, b), stop)
    fmt_waste = sum(m["size"] for g in formats for m in g["members"][1:])
    # orphan info files
    json_n = json_b = orphan = 0
    media_names = set()
    for d, entries in by_dir.items():
        for n, sz in entries:
            if os.path.splitext(n)[1].lower() in MEDIA_EXT:
                media_names.add(n.lower())
    for d, entries in by_dir.items():
        for n, sz in entries:
            if n.lower().endswith(".json"):
                json_n += 1
                json_b += sz
                key = json_key(Path(n))
                if Path(key).suffix in MEDIA_EXT and key not in media_names and DUP_RE.sub("", key) not in media_names:
                    orphan += 1
    sim = find_similar_folders([str(r) for r in roots], False) if not any(inside_photos_library(r) for r in roots) else []
    # deep: read the metadata of the files (or a sample)
    deep_stats = None
    if files and shutil.which("exiftool"):
        pool = [f for f in files if f[2] in MEDIA_EXT]
        rnd = random.Random(11)
        pick = pool if (deep and len(pool) <= exif_cap) else rnd.sample(pool, min(len(pool), exif_cap if deep else 400))
        mism = no_date = no_gps = yr_mismatch = n_read = future = month_mis = fillable = guessable = 0
        date_ex, fut_ex, place_ex = [], [], []
        models = Counter()
        mism_list = []
        for k in range(0, len(pick), 300):
            stop()
            say("Reading photo metadata", min(k, len(pick)), len(pick))
            fd, arg = tempfile.mkstemp(suffix=".args")
            os.close(fd)
            try:
                Path(arg).write_text("\n".join(f[0] for f in pick[k:k + 300]), encoding="utf-8", errors="surrogateescape")
                r = subprocess.run(["exiftool", "-charset", "filename=utf8", "-j", "-n", "-FileTypeExtension", "-DateTimeOriginal", "-CreateDate", "-GPSLatitude", "-Model", "-@", arg],
                                   capture_output=True, text=True)
                for it in json.loads(r.stdout or "[]"):
                    n_read += 1
                    p = it["SourceFile"]
                    real = (it.get("FileTypeExtension") or "").lower()
                    cur = os.path.splitext(p)[1].lower().lstrip(".")
                    cur = {"jpeg": "jpg", "tiff": "tif", "heif": "heic", "m4v": "mp4"}.get(cur, cur)
                    real = {"jpeg": "jpg", "tiff": "tif", "heif": "heic", "m4v": "mp4"}.get(real, real)
                    if real and cur != real and not (cur in ("mov", "mp4") and real in ("mov", "mp4")):
                        mism += 1
                        if len(mism_list) < 30:
                            mism_list.append("%s is really .%s" % (os.path.basename(p), real))
                    d = str(it.get("DateTimeOriginal") or it.get("CreateDate") or "")
                    if not d or d.startswith("0000"):
                        no_date += 1
                        if date_hint_from_path(p, roots):
                            fillable += 1
                    else:
                        ep = _epoch(d)
                        why = date_problem(date_hint_from_path(p, roots), ep) if ep else None
                        if why == "future":
                            future += 1
                        elif why == "year":
                            yr_mismatch += 1
                        elif why == "month":
                            month_mis += 1
                        if why in ("future", "year") and len(date_ex if why == "year" else fut_ex) < 6:
                            (date_ex if why == "year" else fut_ex).append("%s (in '%s') says %s" % (os.path.basename(p), (date_hint_from_path(p, roots) or {}).get("folder", os.path.basename(os.path.dirname(p))), d[:10]))
                    if it.get("GPSLatitude") is None:
                        no_gps += 1
                        pg = guess_place(p, roots)
                        if pg:
                            guessable += 1
                            if len(place_ex) < 6:
                                place_ex.append("%s: %s" % (pg["folder"], pg["place"]))
                    if it.get("Model"):
                        models[str(it["Model"])] += 1
            except (ValueError, OSError):
                pass
            finally:
                Path(arg).unlink(missing_ok=True)
        deep_stats = {"read": n_read, "pool": len(pool), "mismatch": mism, "no_date": no_date, "no_gps": no_gps, "year_mismatch": yr_mismatch,
                      "future": future, "month_mismatch": month_mis, "fillable": fillable, "guessable": guessable, "date_examples": date_ex, "future_examples": fut_ex, "place_examples": place_ex,
                      "models": models.most_common(5), "mism_list": mism_list, "sampled": not deep or len(pool) > exif_cap}
    # ---------------------------------------------------------------- findings
    def add(fid, cat, sev, title, detail, count=0, nbytes=0, tab="", label="", extra=None):
        F.append({"id": fid, "cat": cat, "sev": sev, "title": title, "detail": detail, "count": count, "bytes": nbytes, "tab": tab, "label": label, **(extra or {})})
    if dup_n:
        add("dups", "space", "warn", "Duplicate files", "%s of the same file were found (matching size and content at both ends). Keeping one copy of each would free the space." % f"{dup_n:,}", dup_n, dup_b, "merge", "Open Merge")
    if formats:
        add("formats", "space", "warn", "The same file in more than one format", "%s of files share a name but are saved in different formats (like IMG_1.mov, IMG_1.mp4 and IMG_1.avi). This often happens when a video is converted and the old copy is kept. Review them below." % _pl(len(formats), "group"), len(formats), fmt_waste, "", "Review below")
    lo = sum(leftovers.values())
    if leftovers:
        add("leftovers", "space", "info", "Set-aside and leftover folders", "%s such as _original_videos, _duplicates and _similar_set_aside hold %s. Once you have checked them you can delete them yourself." % (_pl(len(leftovers), "folder"), fmt_bytes(lo)), len(leftovers), lo)
    if temp:
        add("temp", "space", "warn", "Temporary and partial files", "%s ending in .part, .tmp, .bak or ~ (often left by an interrupted conversion or download)." % _pl(temp, "file"), temp, temp_b, "clean", "Open Clean up")
    if junk:
        add("junk", "space", "info", "Junk and cache files", "%s system leftovers such as .DS_Store, Thumbs.db and thumbnail caches." % f"{junk:,}", junk, junk_b, "clean", "Open Clean up")
    if json_n:
        add("json", "space", "info", "Google .json info files", "%s taking %s. Once your photos carry their own dates and locations you can remove these." % (_pl(json_n, "file"), fmt_bytes(json_b)), json_n, json_b, "clean", "Open Clean up")
    if orphan:
        add("orphan", "files", "info", "Info files with no photo", "%s describe a photo that is not in this library." % _pl(orphan, ".json file"), orphan, 0, "clean", "Open Clean up")
    if legacy["n"]:
        add("legacy", "files", "info", "Old-format videos", "%s videos in formats that play badly on phones and cannot go into Apple Photos." % f"{legacy['n']:,}", legacy["n"], legacy["bytes"], "convert", "Open Convert")
    if zero:
        add("zero", "files", "bad", "Empty files (0 bytes)", "%s contain nothing. They are ghosts: they show a name but no picture." % _pl(zero, "file"), zero, 0, "clean", "Open Clean up")
    if ghosts_icloud or dataless:
        add("cloud", "cloud", "bad", "Files that are only in iCloud, not on this disk", "%s files are placeholders (iCloud Drive keeps the name but not the picture here). They cannot be backed up, merged or imported until they are downloaded: in Finder, right-click the folder and choose Download Now." % f"{ghosts_icloud + dataless:,}", ghosts_icloud + dataless)
    if deep_stats and deep_stats["mismatch"]:
        add("mismatch", "files", "warn", "Files with the wrong extension", "%s%s files are named one thing but are really another (for example a .jpg that is really a .heic)." % (f"{deep_stats['mismatch']:,}", " of the %s checked" % f"{deep_stats['read']:,}" if deep_stats["sampled"] else ""), deep_stats["mismatch"], 0, "clean", "Open Clean up", {"examples": deep_stats["mism_list"][:8]})
    if deep_stats and deep_stats["read"]:
        pct = round(100 * deep_stats["no_date"] / deep_stats["read"])
        if pct >= 2:
            add("nodate", "meta", "warn" if pct < 20 else "bad", "Photos with no date inside", "About %d%% of the files checked have no date taken, so apps cannot place them on your timeline." % pct, deep_stats["no_date"], 0, "guided", "Open Guided")
        if deep_stats["year_mismatch"]:
            add("yearmis", "meta", "warn", "Dates that do not fit the folder", "%s photos sit in a folder named for a year or month (like '2017' or '2026-06') but the date inside the photo is a different year. This usually means the picture lost its metadata somewhere along the way." % f"{deep_stats['year_mismatch']:,}",
                deep_stats["year_mismatch"], 0, "guided", "Open Guided", {"examples": deep_stats["date_examples"]})
        if deep_stats["future"]:
            add("future", "meta", "bad", "Dates in the future", "%s photos claim to be taken in the future, which cannot be right." % f"{deep_stats['future']:,}",
                deep_stats["future"], 0, "guided", "Open Guided", {"examples": deep_stats["future_examples"]})
        if deep_stats["fillable"]:
            add("fillable", "meta", "info", "No date inside, but the folder name has one", "%s photos have no date taken, yet their folder name gives the year. Backstory can fill it in." % f"{deep_stats['fillable']:,}", deep_stats["fillable"], 0, "guided", "Open Guided")
        if deep_stats["guessable"]:
            add("guessable", "meta", "info", "No location, but the folder names a place", "%s photos have no location, but the folder name mentions a place you could safely guess (for example %s). Backstory can add an approximate location and label it as a guess." % (f"{deep_stats['guessable']:,}", "; ".join(deep_stats["place_examples"][:2])),
                deep_stats["guessable"], 0, "guided", "Open Guided", {"examples": deep_stats["place_examples"]})
    if empty:
        add("empty", "folders", "info", "Empty folders", "%s hold no files." % _pl(len(empty), "folder"), len(empty), 0, "clean", "Open Clean up")
    if sim:
        add("similar_folders", "folders", "warn", "Look-alike folder names", "%s of folders look like the same thing under different names (for example %s)." % (_pl(len(sim), "group"), ", ".join(m["name"] for m in sim[0]["members"][:3])), len(sim), 0, "clean", "Open Clean up")
    if paren_dirs:
        add("paren", "folders", "info", "Folders named like 'Folder (1)' or 'Folder copy'", "%s carry copy markers." % _pl(len(paren_dirs), "folder"), len(paren_dirs), 0, "clean", "Open Clean up")
    if space_dirs:
        add("spaces", "folders", "info", "Folder names with stray spaces", "%s have leading, trailing or doubled spaces." % _pl(len(space_dirs), "folder"), len(space_dirs), 0, "clean", "Open Clean up")
    if case_clash:
        add("case", "folders", "warn", "Folders that differ only by capital letters", "%s places hold folders like 'photos' and 'Photos' side by side. They look the same on a Mac and cause trouble on other systems." % len(case_clash), len(case_clash), 0, "merge", "Open Merge")
    if long_paths:
        add("longpath", "folders", "info", "Very long file paths", "%s have a path over 230 characters, which some apps and drives cannot handle." % _pl(len(long_paths), "file"), len(long_paths))
    # ---------------------------------------------------------------- score: each area is 100, less penalties
    parts = {}
    waste = dup_b + fmt_waste + temp_b
    parts["space"] = max(0, 100 - min(60, round(100 * waste / max(1, total_bytes) * 2)) - (10 if leftovers and lo > 0.05 * max(1, total_bytes) else 0))
    fol_issues = len(empty) + len(sim) * 3 + len(paren_dirs) + len(case_clash) * 3 + len(space_dirs)
    parts["folders"] = max(0, 100 - min(70, round(100 * fol_issues / max(30, len(by_dir)))))
    parts["files"] = max(0, 100 - min(40, zero * 4) - min(30, round(100 * (deep_stats["mismatch"] / max(1, deep_stats["read"])) * 3) if deep_stats else 0) - min(20, round(100 * legacy["n"] / max(1, total_media))))
    parts["cloud"] = max(0, 100 - min(100, round(100 * (ghosts_icloud + dataless) / max(1, total_media + ghosts_icloud) * 2)))
    if deep_stats and deep_stats["read"]:
        parts["metadata"] = max(0, 100 - min(80, round(100 * deep_stats["no_date"] / deep_stats["read"] * 1.5)))
    score = round(sum(parts.values()) / len(parts)) if parts else 100
    top_folders = [(os.path.relpath(d, str(roots[0])) if roots and str(roots[0]) in d else d, b) for d, b in folder_bytes.most_common(8)]
    stats = {"files": total_media, "bytes": total_bytes, "by_ext": [(e, ext_n[e], ext_b[e]) for e, _ in ext_n.most_common(14)], "years": sorted(year_n.items()),
             "folders": len(by_dir), "biggest": [(os.path.basename(p), p, sz) for sz, p in biggest[:10]], "top_folders": top_folders,
             "raw": {"total": raw_total, "bytes": raw_total_b, "paired": raw_pairs, "paired_raw_bytes": raw_pair_raw_b, "paired_jpg_bytes": raw_pair_jpg_b, "raw_only": raw_only},
             "live_pairs": live_pairs,
             "deep": deep_stats, "waste_bytes": waste, "dup_n": dup_n, "dup_bytes": dup_b, "format_waste": fmt_waste}
    order = {"bad": 0, "warn": 1, "info": 2}
    F.sort(key=lambda f: (order.get(f["sev"], 3), -f["bytes"], -f["count"]))
    return {"findings": F, "stats": stats, "formats": formats, "score": score, "parts": parts, "deep": bool(deep), "roots": [str(r) for r in roots]}


def photos_db_health(lib):
    """Experimental, read-only hints from an Apple Photos library's own database (a copy of it is read). Apple does not document
    this database, so treat the numbers as hints."""
    import sqlite3
    lib = Path(lib)
    db = lib / "database" / "Photos.sqlite"
    if not db.exists():
        return {"ok": False, "why": "no database folder found"}
    tmp = tempfile.mkdtemp(prefix="backstory_photosdb_")
    try:
        for suf in ("", "-wal", "-shm"):
            src = Path(str(db) + suf)
            if src.exists():
                shutil.copy2(src, Path(tmp) / ("Photos.sqlite" + suf))
        con = sqlite3.connect("file:%s?mode=ro" % (Path(tmp) / "Photos.sqlite"), uri=True)
        cols = {r[1] for r in con.execute("PRAGMA table_info(ZASSET)")}
        if not cols:
            return {"ok": False, "why": "this Photos version stores things differently"}
        where = "WHERE ZTRASHEDSTATE = 0" if "ZTRASHEDSTATE" in cols else ""
        total = con.execute("SELECT COUNT(*) FROM ZASSET " + where).fetchone()[0]
        out = {"ok": True, "total": total}
        if "ZCLOUDLOCALSTATE" in cols:
            cond = (where + " AND " if where else "WHERE ") + "ZCLOUDLOCALSTATE = 0"
            out["not_in_cloud"] = con.execute("SELECT COUNT(*) FROM ZASSET " + cond).fetchone()[0]
        if "ZDIRECTORY" in cols and "ZFILENAME" in cols:
            missing = 0
            for d, f in con.execute("SELECT ZDIRECTORY, ZFILENAME FROM ZASSET " + where):
                if d and f and not (lib / "originals" / d / f).exists():
                    missing += 1
            out["original_not_on_disk"] = missing
        con.close()
        return out
    except Exception as ex:
        return {"ok": False, "why": str(ex)[:120]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_photos_libraries():
    """Photos libraries in the usual places (read-only listing)."""
    out = []
    for base in (Path.home() / "Pictures", Path("/Volumes")):
        try:
            if base.name == "Volumes":
                cands = [p for v in base.iterdir() if v.is_dir() for p in v.glob("*.photoslibrary")]
            else:
                cands = list(base.glob("*.photoslibrary"))
        except OSError:
            continue
        out += [str(p) for p in cands if (p / "database" / "Photos.sqlite").exists()]
    return out


def photos_upload_status(lib, wanted=None):
    """How much of a Photos library is in iCloud, read from a copy of its database (Apple does not document it, so this is
    best effort). wanted = [(original file name, size)] checks those specific imports. Returns a dict, with "ok": False and
    "why" when it cannot be read."""
    import sqlite3
    lib = Path(lib)
    db = lib / "database" / "Photos.sqlite"
    if not db.exists():
        return {"ok": False, "why": "no Photos database found in %s" % lib.name}
    tmp = tempfile.mkdtemp(prefix="backstory_upload_")
    con = None
    try:
        for suf in ("", "-wal", "-shm"):
            src = Path(str(db) + suf)
            if src.exists():
                shutil.copy2(src, Path(tmp) / ("Photos.sqlite" + suf))
        con = sqlite3.connect("file:%s?mode=ro" % (Path(tmp) / "Photos.sqlite"), uri=True)
        acols = {r[1] for r in con.execute("PRAGMA table_info(ZASSET)")}
        if not acols:
            return {"ok": False, "why": "this Photos version stores things differently"}
        live = "ZTRASHEDSTATE = 0" if "ZTRASHEDSTATE" in acols else "1=1"
        total = con.execute("SELECT COUNT(*) FROM ZASSET WHERE %s" % live).fetchone()[0]
        out = {"ok": True, "total": total, "uploaded": None, "pending": None, "icloud_on": None, "known_state": "ZCLOUDLOCALSTATE" in acols}
        if "ZCLOUDLOCALSTATE" in acols:
            up = con.execute("SELECT COUNT(*) FROM ZASSET WHERE %s AND ZCLOUDLOCALSTATE = 1" % live).fetchone()[0]
            out["uploaded"], out["pending"] = up, total - up
        guid = 0
        if "ZCLOUDASSETGUID" in acols:
            guid = con.execute("SELECT COUNT(*) FROM ZASSET WHERE %s AND ZCLOUDASSETGUID IS NOT NULL" % live).fetchone()[0]
        out["with_cloud_id"] = guid
        out["icloud_on"] = bool((out["uploaded"] or 0) > 0 or guid > 0)
        if wanted:
            tcols = {r[1] for r in con.execute("PRAGMA table_info(ZADDITIONALASSETATTRIBUTES)")}
            if "ZORIGINALFILENAME" in tcols and "ZCLOUDLOCALSTATE" in acols:
                join = "s.ZADDITIONALATTRIBUTES = a.Z_PK" if "ZADDITIONALATTRIBUTES" in acols else ("a.ZASSET = s.Z_PK" if "ZASSET" in tcols else "")
                if join:
                    size_col = "a.ZORIGINALFILESIZE" if "ZORIGINALFILESIZE" in tcols else "0"
                    state = {}
                    for nm, sz, cs in con.execute("SELECT lower(a.ZORIGINALFILENAME), %s, s.ZCLOUDLOCALSTATE FROM ZASSET s JOIN ZADDITIONALASSETATTRIBUTES a ON %s WHERE %s" % (size_col, join, live.replace("ZTRASHEDSTATE", "s.ZTRASHEDSTATE"))):
                        k = (nm, int(sz or 0))
                        state[k] = max(state.get(k, 0), 1 if cs == 1 else 0)
                    state_by_name = {}
                    for (nm, sz), v in state.items():
                        state_by_name[nm] = max(state_by_name.get(nm, 0), v)
                    matched = matched_up = uncertain = 0
                    missing = []
                    by_size = "ORIGINALFILESIZE" in "".join(tcols)
                    for nm, sz in wanted:
                        k = (nm.lower(), int(sz) if by_size else 0)
                        if k in state:
                            matched += 1
                            matched_up += state[k]
                        elif nm.lower() in state_by_name:           # same name but another size: a different picture, or Photos re-encoded it
                            uncertain += 1                           # never counted as "verified"
                            if len(missing) < 10:
                                missing.append(nm)
                        elif len(missing) < 10:
                            missing.append(nm)
                    out.update({"wanted": len(wanted), "matched": matched, "matched_uploaded": matched_up, "uncertain": uncertain,
                                "not_found": len(wanted) - matched, "missing_examples": missing})
                else:
                    out["wanted_note"] = "could not link files to Photos items"
            else:
                out["wanted_note"] = "this Photos version does not record original file names"
        return out
    except Exception as ex:
        return {"ok": False, "why": str(ex)[:140]}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# ---- Reading the logs Photos, iCloud and Backstory write, and saying what they mean ---------------------------
LOG_RULES = [
    {"id": "disk_full", "sev": "bad", "title": "The disk is full",
     "re": r"no space left on device|ENOSPC|NSPOSIXErrorDomain[^\n]{0,40}Code=28\b|insufficient (disk|storage) space|not enough (free )?(disk )?space|out of disk space",
     "meaning": "Photos, iCloud or Backstory tried to write a file and the disk had no room.",
     "fixes": ["Free space: empty the Trash, delete large files you no longer need, or move files to an external drive (Apple menu > System Settings > General > Storage shows what takes space).",
               "In Photos > Settings > iCloud choose Optimize Mac Storage so originals can be replaced by small copies once they are in iCloud.",
               "Pause sending to Photos until there is at least 20 GB free, then continue (Backstory's Photos tab can wait for room for you)."]},
    {"id": "icloud_quota", "sev": "bad", "title": "iCloud storage is full",
     "re": r"CKErrorQuotaExceeded|quota ?exceeded|QuotaExceeded|iCloud storage (is )?full|not enough iCloud storage|storage limit",
     "meaning": "Your iCloud plan has no room left, so Photos cannot upload more.",
     "fixes": ["Open System Settings > Apple ID (your name) > iCloud > Manage to see what uses the space.", "Buy a larger iCloud+ plan, or free space by deleting old backups and big files.",
               "Uploads resume by themselves once there is room."]},
    {"id": "network", "sev": "warn", "title": "Network trouble is slowing or stopping uploads",
     "re": r"NSURLErrorDomain[^\n]{0,60}(-1009|-1005|-1001|-1004|-1200|-1018)|not connected to the internet|network connection was lost|request timed out|could not connect to the server|CKErrorNetwork(Unavailable|Failure)",
     "meaning": "The connection dropped or timed out while Photos or iCloud was talking to Apple's servers.",
     "fixes": ["Check Wi-Fi or Ethernet and try opening a web page.", "Turn off any VPN or content filter, which can break large uploads.", "Restart the router, or move closer to it. Large libraries upload best on a steady wired or strong Wi-Fi link.",
               "If it keeps happening, in Photos > Settings > iCloud pause for a minute and resume to restart syncing."]},
    {"id": "not_signed_in", "sev": "bad", "title": "iCloud sign-in problem",
     "re": r"CKErrorNotAuthenticated|not authenticated|AuthenticationFailed|authentication (is )?required|account (changed|not signed in)|iCloud account is not available|CKAccountStatus(NoAccount|Restricted|CouldNotDetermine)",
     "meaning": "iCloud does not think this Mac is signed in (or needs you to sign in again), so nothing uploads.",
     "fixes": ["Open System Settings > Apple ID and check you are signed in; sign out and in again if it asks.", "Check iCloud > Photos is switched on.", "If a banner says 'Apple ID settings need updating', open it and follow the prompts."]},
    {"id": "db_corrupt", "sev": "bad", "title": "The Photos database may be damaged",
     "re": r"SQLITE_CORRUPT|database disk image is malformed|SQLite error 11|integrity[_ ]check failed|photos\.sqlite[^\n]{0,60}(corrupt|damaged|malformed)|PLPhotoLibrary[^\n]{0,60}(corrupt|damaged)|database (is )?corrupt",
     "meaning": "Photos' own database reports damage. Photos can misbehave, lose track of items or stop syncing.",
     "fixes": ["Back up the library first (copy the .photoslibrary file to another drive).", "Quit Photos, then hold Option and Command while opening Photos, and choose Repair.",
               "Do not import more photos until the repair finishes. If repair fails, ask for support with the log excerpt below."]},
    {"id": "library_repair", "sev": "warn", "title": "Photos is repairing or rebuilding the library",
     "re": r"rebuilding (the )?(photos )?library|library (needs|is being) (repair|rebuil)|repairing (photo )?library|PLLibraryRebuild|photolibraryd[^\n]{0,60}rebuild",
     "meaning": "Photos is rebuilding its internal index. This can take hours on a large library and uses a lot of CPU and disk.",
     "fixes": ["Leave Photos open and the Mac plugged in until it finishes.", "Avoid importing or running Backstory's Photos tab until it is done."]},
    {"id": "import_failed", "sev": "warn", "title": "Photos could not import some files",
     "re": r"(import|PHAssetCreationRequest)[^\n]{0,80}(failed|error)|PHPhotosErrorDomain[^\n]{0,30}(3300|3302|3303|3305|3311|3169)|unsupported (file )?(type|format)|cannot import|could not be imported",
     "meaning": "Specific files were rejected, usually an unsupported format (AVI, MKV, WMV...), a damaged file, or a 0-byte placeholder.",
     "fixes": ["On Backstory's Convert tab, turn old videos into MP4 first.", "Run the Health tab to find empty (0 byte) and wrongly-named files, and fix them.",
               "Re-run the Photos tab: files already imported are skipped, so only the missing ones are tried again."]},
    {"id": "low_power", "sev": "warn", "title": "Uploads are paused by Low Power Mode or the battery",
     "re": r"low power mode|LowPowerMode|battery[^\n]{0,40}(low|paus)|paus[a-z]{0,8} [^\n]{0,30}(battery|low power)|upload[^\n]{0,40}on battery",
     "meaning": "macOS holds back background uploads to save power.",
     "fixes": ["Plug the Mac in and turn off Low Power Mode (System Settings > Battery).", "Keep the Mac awake and the lid open while a big upload runs (Settings > Battery > Options: prevent sleep when the display is off)."]},
    {"id": "sync_paused", "sev": "warn", "title": "iCloud Photos is paused",
     "re": r"(sync|upload|iCloud Photos)[^\n]{0,40}paus|paus[a-z]{0,8}[^\n]{0,40}(sync|upload|iCloud Photos)|PauseCPL|CPLPaused|resume[^\n]{0,20}(in|after) [0-9]+ (hour|day)",
     "meaning": "Syncing was paused (by you, by Photos for a day, or by the system).",
     "fixes": ["Open Photos and scroll to the bottom of the Library view: it says 'Paused' with a Resume button.", "Press Resume. If it pauses again by itself, check Low Power Mode and available space."]},
    {"id": "permission", "sev": "bad", "title": "macOS blocked the request (permission)",
     "re": r"not authorized to send Apple events|errAEEventNotPermitted|NSOSStatusErrorDomain[^\n]{0,20}-1743|\(-1743\)|kTCCService|TCC[^\n]{0,30}(denied|deny)|Operation not permitted[^\n]{0,80}(photos|Pictures|Volumes)|not permitted to access",
     "meaning": "A privacy setting stops the app from controlling Photos or reading a folder.",
     "fixes": ["System Settings > Privacy & Security > Automation: allow Backstory to control Photos.", "Privacy & Security > Files and Folders (or Full Disk Access): allow Backstory to read your library folder or external drive.", "Quit and reopen Backstory after changing a permission."]},
    {"id": "thermal", "sev": "info", "title": "The Mac is hot and slowing down",
     "re": r"thermal (pressure|state|level)[^\n]{0,20}(serious|critical|heavy)|thermalPressure|thermal[^\n]{0,20}throttl",
     "meaning": "macOS slows background work to cool down, so uploads and analysis crawl.",
     "fixes": ["Let the Mac cool, keep vents clear, avoid soft surfaces.", "Pause large jobs until it is cooler."]},
    {"id": "crash", "sev": "bad", "title": "Photos (or a Photos helper) crashed",
     "re": r"EXC_BAD_ACCESS|EXC_CRASH|Termination Reason|Process (Photos|photolibraryd|cloudphotod|assetsd|mediaanalysisd)[^\n]{0,40}(crash|exited abnormally|terminated)|jetsam|Application Specific Information",
     "meaning": "A Photos process stopped unexpectedly. If it repeats, a damaged file or a damaged library is a common cause.",
     "fixes": ["Quit and reopen Photos. If it repeats, restart the Mac.", "Use smaller batches in Backstory's Photos tab and check the Health tab for damaged files.", "Try the Photos repair (Option + Command while opening Photos), after backing up."]},
    {"id": "bad_asset", "sev": "warn", "title": "A photo or video could not be read",
     "re": r"(asset|image|photo|video)[^\n]{0,60}(corrupt|damaged|unreadable|cannot be decoded|could not be decoded)|CGImageSource[^\n]{0,60}(failed|err)|AVFoundationErrorDomain[^\n]{0,20}-11800|kCGImageSourceStatus(Corrupt|ReadingHeader|UnknownType)",
     "meaning": "One or more files are damaged or in an unreadable format, so Photos could not show, analyse or upload them.",
     "fixes": ["Run the Health tab (deep check) to list 0-byte and wrongly-named files.", "Try opening the file in Preview or QuickTime. If it will not open, restore it from your Takeout or another backup."]},
    {"id": "io_error", "sev": "bad", "title": "The drive could not be read or written (I/O error)",
     "re": r"input/output error|I/O error|\bEIO\b|Errno 5\b|NSPOSIXErrorDomain[^\n]{0,40}Code=(5|6)\b|disk (read|write) error|device not configured|Errno 6\b",
     "meaning": "The drive stopped answering or has a bad sector. This is common with loose cables, drives that sleep, or failing drives.",
     "fixes": ["Reconnect the cable, try another port, and avoid hubs.", "Run Disk Utility > First Aid on the drive.", "Copy the affected files in Finder. If it fails, the drive may be failing: back up everything else now.",
               "Run Backstory again with the same Destination: finished files are skipped."]},
    {"id": "volume_gone", "sev": "warn", "title": "A drive or library was not available",
     "re": r"volume[^\n]{0,40}(ejected|not mounted|unavailable|went away|disconnected)|library[^\n]{0,40}(not found|missing|unavailable|could not be opened)|No such file or directory[^\n]{0,60}\.photoslibrary",
     "meaning": "The drive holding the library was disconnected or asleep, or the library was moved.",
     "fixes": ["Reconnect the drive and make sure it is mounted before opening Photos or Backstory.", "If you moved the library, open it again with Option held while opening Photos."]},
    {"id": "analysis", "sev": "info", "title": "Photos is analysing your library (busy, not broken)",
     "re": r"(mediaanalysisd|photoanalysisd)[^\n]{0,80}(analy[sz]|running|progress|started)|analysis (is )?(running|in progress)|scene analysis|face (detection|clustering)",
     "meaning": "After a big import Photos spends hours indexing faces and scenes. It is heavy on CPU and can slow uploads.",
     "fixes": ["Leave the Mac plugged in and awake: it finishes by itself.", "Wait for it to finish before importing the next big batch."]},
    {"id": "backstory_space", "sev": "bad", "title": "Backstory: not enough free space",
     "re": r"Not enough free space",
     "meaning": "Backstory checked before unpacking a zip and found too little room on the Destination drive.",
     "fixes": ["Choose a Destination on a bigger or external drive.", "You need about twice your largest single zip while it is processed, plus room for the finished library."]},
    {"id": "backstory_exiftool", "sev": "bad", "title": "Backstory: ExifTool is missing",
     "re": r"exiftool not found",
     "meaning": "The tool that writes dates and locations into photos could not be found.",
     "fixes": ["The Mac app includes it. If you run from source: brew install exiftool, then restart Backstory."]},
]


def interpret_log_lines(lines, max_examples=3):
    """Group log lines by what they mean. Returns (issues, unmatched) where issues carry a plain-language meaning and fixes."""
    comp = [(r, re.compile(r["re"], re.I)) for r in LOG_RULES]
    found = {}
    unmatched = {}
    for raw in lines:
        line = raw.strip()[:1500]                 # a pasted mega-line must never stall the checker
        if not line:
            continue
        hit = False
        for r, rx in comp:
            if rx.search(line):
                d = found.setdefault(r["id"], {"id": r["id"], "sev": r["sev"], "title": r["title"], "meaning": r["meaning"], "fixes": r["fixes"], "count": 0, "examples": [], "first": line[:19], "last": line[:19]})
                d["count"] += 1
                d["last"] = line[:19]
                if len(d["examples"]) < max_examples and line[:200] not in d["examples"]:
                    d["examples"].append(line[:240])
                hit = True
                break
        if not hit and re.search(r"\b(error|fault|failed|failure|exception)\b", line, re.I):
            key = re.sub(r"[0-9a-f]{8,}|\b\d+\b", "#", line[20:120]) if len(line) > 24 else line
            u = unmatched.setdefault(key, {"text": line[:240], "count": 0})
            u["count"] += 1
    order = {"bad": 0, "warn": 1, "info": 2}
    issues = sorted(found.values(), key=lambda d: (order.get(d["sev"], 3), -d["count"]))
    other = sorted(unmatched.values(), key=lambda u: -u["count"])[:6]
    return issues, other


MAC_LOG_PROCS = ("photolibraryd", "Photos", "cloudphotod", "assetsd", "mediaanalysisd", "photoanalysisd", "cloudd", "bird", "PhotosReliveWidget", "photoanalysisd")


def collect_mac_logs(hours=6, timeout=150):
    """Recent error and key status lines from the macOS log for Photos and iCloud. Returns (lines, note)."""
    if sys.platform != "darwin" or not shutil.which("log"):
        return [], "The macOS log is only available on a Mac. You can paste log text below instead."
    procs = " OR ".join('process == "%s"' % p for p in sorted(set(MAC_LOG_PROCS)))
    key = " OR ".join('eventMessage CONTAINS[c] "%s"' % w for w in ("paused", "quota", "low power", "no space", "not authenticated", "corrupt", "rebuild", "unsupported", "timed out", "not connected"))
    pred = "(%s) AND (messageType == error OR messageType == fault OR %s)" % (procs, key)
    try:
        r = subprocess.run(["log", "show", "--last", "%dh" % hours, "--style", "compact", "--predicate", pred], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return [], "Could not read the macOS log (%s). Try a shorter time range." % ex
    out = r.stdout.splitlines()
    note = ""
    if r.returncode != 0 or (not out and r.stderr):
        note = (r.stderr or "").strip()[:200]
    return out[-20000:], note


def collect_crash_reports(days=7):
    """Crash and hang reports for Photos processes (file names and dates only)."""
    base = Path.home() / "Library" / "Logs" / "DiagnosticReports"
    out = []
    try:
        cutoff = time.time() - days * 86400
        for p in base.iterdir():
            if p.suffix.lower() in (".ips", ".crash", ".hang", ".diag") and re.match(r"(Photos|photolibraryd|cloudphotod|assetsd|mediaanalysisd|photoanalysisd|Backstory)", p.name):
                if p.stat().st_mtime >= cutoff:
                    out.append("%s Process %s crashed or hung (%s)" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)), p.name.split("_")[0], p.suffix[1:]))
    except OSError:
        pass
    return sorted(out)


def thumb_b64(path, width=200):
    """A small JPEG of a picture or video frame as a data: address, for showing next to its details."""
    import base64
    def run(cmd, data=None):
        try:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=40)
            return r.stdout if r.returncode == 0 else b""
        except (OSError, subprocess.TimeoutExpired):
            return b""
    vf = "scale='min(%d,iw)':-2" % width
    out = run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", vf, "-q:v", "6", "-f", "mjpeg", "-"])
    if not out and shutil.which("exiftool"):
        th = run(["exiftool", "-b", "-ThumbnailImage", str(path)]) or run(["exiftool", "-b", "-PreviewImage", str(path)])
        if th:
            out = run(["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-vf", vf, "-q:v", "6", "-f", "mjpeg", "-"], th) or th
    return ("data:image/jpeg;base64," + base64.b64encode(out).decode()) if out else ""


def story_card(name, thumb, where, before, after, notes):
    """before/after: {"date","gps","desc"} text. Marks which fields change."""
    changes = {k: (before.get(k, "") != after.get(k, "")) for k in ("date", "gps", "desc")}
    return {"name": name, "thumb": thumb, "where": where, "before": before, "after": after, "changes": changes, "notes": notes}


def story_from_row(row, out_root=None, thumb=True):
    """A before/after card from one processed file (its row in the report)."""
    p = Path(row["file"])
    def after(field, google_key, before_key):
        if row.get(field) in ("added", "replaced"):
            return row.get(google_key, "")
        return row.get(before_key, "")
    b = {"date": row.get("date_before", "") or "", "gps": row.get("gps_before", "") or "", "desc": row.get("desc_before", "") or ""}
    a = {"date": (after("date", "date_google", "date_before") or ""), "gps": (after("gps", "gps_google", "gps_before") or ""), "desc": (after("desc", "desc_google", "desc_before") or "")}
    notes = []
    if row.get("match") == "filename-date":
        a["date"] = a["date"] or "from the file name"
        notes.append("Date read from the file name")
    if row.get("live") == "paired":
        notes.append("Live Photo re-paired with its video")
    if row.get("date_fix"):
        notes.append("Date " + ("filled in" if row["date_fix"] == "filled" else "corrected") + " from the folder name")
    if row.get("gps_guess"):
        notes.append("Location is a guess: " + row["gps_guess"])
    if row.get("status") == "duplicate":
        notes.append("Identical copy: the photo is kept once")
    if row.get("status") == "left-out":
        notes.append("Left out: " + (row.get("detail") or "").replace("left out: ", ""))
    where = ""
    if row.get("output"):
        try:
            where = str(Path(row["output"]).parent.relative_to(out_root)) if out_root else str(Path(row["output"]).parent)
        except ValueError:
            where = str(Path(row["output"]).parent)
    return story_card(p.name, thumb_b64(p) if thumb and p.exists() else "", where or "", b, a, notes)


# ---- Compare libraries: what is the same, what differs, and what a merge would do ----------------------------
def _canon_rel(parts):
    """Folder path of a file inside a library, Takeout wrapper folders ignored, case-folded."""
    parts = list(parts)
    while parts and _TAKEOUT_WRAP.match(parts[0]):
        parts.pop(0)
    return tuple(p.casefold() for p in parts)


def compare_libraries(entries, progress=None, should_stop=None, threshold=4, similar_cap=30000, meta_cap=200, workers=6):
    """Compare the first two libraries you added (folders, Photos libraries or Takeout zip files) and say how alike they are:
    identical files, the same file filed differently, same-named files that differ, the same picture at a different size,
    what only one has, and which folders differ. Read-only. A third or fourth library is only counted against the first."""
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import PurePosixPath
    stop = should_stop or (lambda: None)
    say = progress or (lambda *a: None)
    if len(entries) < 2:
        raise ValueError("Add at least two libraries in the Source list to compare them")
    libs = []
    for ei, ent in enumerate(entries[:4]):
        zips, folders = split_sources([ent])
        label = Path(ent).expanduser().name or str(ent)
        files = []                                   # (canonical dir tuple, name, size, sig, source, member, is_zip)
        for z in zips:
            stop()
            say("Reading %s" % z.name, 0, 0)
            try:
                with zipfile.ZipFile(z) as zf:
                    for i in zf.infolist():
                        parts = _safe_member(i.filename) if not i.is_dir() else None
                        if parts and _wanted_media(parts) and not parts[-1].lower().endswith(".json"):
                            files.append((_canon_rel(parts[:-1]), parts[-1], i.file_size, (i.file_size, i.CRC), z, i.filename, True))
            except (zipfile.BadZipFile, OSError):
                continue
        for fo in folders:
            for dp, dns, fns in os.walk(fo, followlinks=False):
                stop()
                dns[:] = [d for d in dns if not (os.path.islink(os.path.join(dp, d)) or is_bundle(d) or d in SIMILAR_SKIP_DIRS or d.startswith("."))]
                try:
                    rel = Path(dp).relative_to(fo).parts
                except ValueError:
                    rel = ()
                for n in fns:
                    p = os.path.join(dp, n)
                    ext = os.path.splitext(n)[1].lower()
                    if n.startswith(".") or ext not in MEDIA_EXT and ext not in FORMAT_VIDEO_EXT:
                        continue
                    try:
                        sz = os.path.getsize(p)
                    except OSError:
                        continue
                    if sz:
                        files.append((_canon_rel(rel), n, sz, None, p, "", False))
        libs.append({"label": label, "files": files})
    A, B = libs[0], libs[1]
    # content fingerprints, only where a size also occurs in the other library
    say("Comparing file contents", 0, 0)
    sizes_other = [set(f[2] for f in lib["files"]) for lib in libs]
    sig_cache = {}

    def sig_of(f, other_sizes):
        if f[3] is not None:
            return f[3]
        if f[2] not in other_sizes:
            return (f[2], None)
        key = f[4]
        if key not in sig_cache:
            try:
                sig_cache[key] = file_sig(key)
            except OSError:
                sig_cache[key] = (f[2], None)
        return sig_cache[key]
    for li, lib in enumerate(libs):
        other = set().union(*[sizes_other[j] for j in range(len(libs)) if j != li])
        lib["sigs"] = []
        for k, f in enumerate(lib["files"]):
            if k % 500 == 0:
                stop()
            lib["sigs"].append(sig_of(f, other))
    # index B
    b_by_sig, b_by_path = {}, {}
    for f, sg in zip(B["files"], B["sigs"]):
        if sg[1] is not None:
            b_by_sig.setdefault(sg, []).append(f)
        b_by_path.setdefault((f[0], f[1].casefold()), []).append((f, sg))
    R = {"labels": [l["label"] for l in libs], "a_files": len(A["files"]), "b_files": len(B["files"]),
         "a_bytes": sum(f[2] for f in A["files"]), "b_bytes": sum(f[2] for f in B["files"]),
         "identical": 0, "identical_bytes": 0, "refiled": 0, "refiled_examples": [], "conflicts": [], "conflict_n": 0, "only_a": 0, "only_b": 0,
         "only_a_bytes": 0, "only_b_bytes": 0, "similar": [], "similar_n": 0, "folders_only_a": [], "folders_only_b": [], "folders_diff": [],
         "meta_diffs": [], "matrix": [], "notes": []}
    matched_b = set()
    a_unmatched = []
    for f, sg in zip(A["files"], A["sigs"]):
        hit = b_by_sig.get(sg) if sg[1] is not None else None
        if hit:
            R["identical"] += 1
            R["identical_bytes"] += f[2]
            for h in hit:
                matched_b.add(id(h))
            same_place = any(h[0] == f[0] and h[1].casefold() == f[1].casefold() for h in hit)
            if not same_place:
                R["refiled"] += 1
                if len(R["refiled_examples"]) < 12:
                    R["refiled_examples"].append({"name": f[1], "a": "/".join(f[0]) or ".", "b": "/".join(hit[0][0]) or ".", "b_name": hit[0][1]})
        else:
            a_unmatched.append((f, sg))
    # same name in the same folder but different content
    conflicts, a_rest = [], []
    for f, sg in a_unmatched:
        cands = [(g, gs) for g, gs in b_by_path.get((f[0], f[1].casefold()), []) if id(g) not in matched_b]
        if cands:
            g, gs = cands[0]
            conflicts.append((f, g))
            matched_b.add(id(g))
        else:
            a_rest.append(f)
    b_rest = [g for g in B["files"] if id(g) not in matched_b]
    # similar pictures among what is left (folders only: needs pixels)
    def ext_of(f):
        return os.path.splitext(f[1])[1].lower()
    cand_a = [f for f in a_rest if not f[6] and ext_of(f) in SIMILAR_EXT]
    cand_b = [g for g in b_rest if not g[6] and ext_of(g) in SIMILAR_EXT]
    hashes = {}
    cmem = [m for c in conflicts for m in c if not m[6] and ext_of(m) in SIMILAR_EXT]
    todo = (cand_a + cand_b)[:similar_cap] + cmem
    done = 0
    lock = threading.Lock()

    def hw(f):
        nonlocal done
        stop()
        h = dhash_image(f[4])
        with lock:
            done += 1
            if h is not None:
                hashes[f[4]] = h
            if done % 25 == 0 or done == len(todo):
                say("Comparing how pictures look", done, len(todo))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(hw, todo))
    n_chunks = max(2, min(16, threshold + 1))
    bounds = [round(i * 64 / n_chunks) for i in range(n_chunks + 1)]
    bucket = {}
    for g in cand_b:
        h = hashes.get(g[4])
        if h is None or h in (0, (1 << 64) - 1):
            continue
        for c in range(n_chunks):
            bucket.setdefault((c, (h >> bounds[c]) & ((1 << (bounds[c + 1] - bounds[c])) - 1)), []).append(g)
    sim_pairs, used_b, used_a = [], set(), set()
    for f in cand_a:
        h = hashes.get(f[4])
        if h is None or h in (0, (1 << 64) - 1):
            continue
        best = None
        seen = set()
        for c in range(n_chunks):
            for g in bucket.get((c, (h >> bounds[c]) & ((1 << (bounds[c + 1] - bounds[c])) - 1)), []):
                if id(g) in seen or id(g) in used_b:
                    continue
                seen.add(id(g))
                d = bin(h ^ hashes[g[4]]).count("1")
                if d <= threshold and (best is None or d < best[0]):
                    best = (d, g)
        if best:
            used_b.add(id(best[1]))
            used_a.add(id(f))
            sim_pairs.append((f, best[1], best[0]))
    conflict_pairs = {(id(c[0]), id(c[1])) for c in conflicts}
    # describe conflicts and similar pairs (dimensions, dates), reading a limited number with exiftool
    want = []
    for f, g, d in sim_pairs[:meta_cap]:
        want += [f, g]
    for f, g in conflicts[:meta_cap]:
        want += [f, g]
    info = {}
    paths = [w[4] for w in want if not w[6]]
    if paths and shutil.which("exiftool"):
        for k in range(0, len(paths), 300):
            stop()
            fd, arg = tempfile.mkstemp(suffix=".args")
            os.close(fd)
            try:
                Path(arg).write_text("\n".join(paths[k:k + 300]), encoding="utf-8", errors="surrogateescape")
                r = subprocess.run(["exiftool", "-charset", "filename=utf8", "-j", "-n", "-ImageWidth", "-ImageHeight", "-DateTimeOriginal", "-CreateDate", "-GPSLatitude", "-GPSLongitude", "-ImageDescription", "-@", arg],
                                   capture_output=True, text=True)
                for it in json.loads(r.stdout or "[]"):
                    info[it["SourceFile"]] = it
            except (ValueError, OSError):
                pass
            finally:
                Path(arg).unlink(missing_ok=True)

    def desc(f):
        it = info.get(f[4], {})
        date = str(it.get("DateTimeOriginal") or it.get("CreateDate") or "")[:19]
        gps = "%.4f, %.4f" % (it["GPSLatitude"], it["GPSLongitude"]) if it.get("GPSLatitude") is not None and it.get("GPSLongitude") is not None else ""
        return {"name": f[1], "where": "/".join(f[0]) or ".", "size": f[2], "w": it.get("ImageWidth") or 0, "h": it.get("ImageHeight") or 0,
                "date": date, "gps": gps, "desc": str(it.get("ImageDescription") or "")[:60], "path": "" if f[6] else f[4]}

    def differences(da, db):
        out = []
        for key, label in (("date", "date"), ("gps", "location"), ("desc", "caption")):
            if da[key] != db[key]:
                if da[key] and not db[key]:
                    out.append("only %s has a %s" % ("the first library", label))
                elif db[key] and not da[key]:
                    out.append("only %s has a %s" % ("the second library", label))
                else:
                    out.append("the %s differs" % label)
        if da["w"] and db["w"] and da["w"] * da["h"] != db["w"] * db["h"]:
            out.append("different picture size (%s x %s against %s x %s)" % (da["w"], da["h"], db["w"], db["h"]))
        return out
    R["conflict_n"] = len(conflicts)
    for f, g in conflicts[:60]:
        da, db = desc(f), desc(g)
        h1, h2 = hashes.get(f[4]), hashes.get(g[4])
        same_pic = h1 is not None and h2 is not None and bin(h1 ^ h2).count("1") <= threshold
        R["conflicts"].append({"a": da, "b": db, "same_picture": same_pic, "differences": differences(da, db)})
    R["similar_n"] = len(sim_pairs)
    for f, g, d in sorted(sim_pairs, key=lambda x: x[2])[:80]:
        da, db = desc(f), desc(g)
        R["similar"].append({"a": da, "b": db, "bits": d, "differences": differences(da, db), "bigger": "a" if da["w"] * da["h"] > db["w"] * db["h"] else ("b" if db["w"] * db["h"] > da["w"] * da["h"] else "same")})
    a_only = [f for f in a_rest if id(f) not in used_a]
    b_only = [g for g in b_rest if id(g) not in used_b]
    R["only_a"], R["only_b"] = len(a_only), len(b_only)
    R["only_a_bytes"], R["only_b_bytes"] = sum(f[2] for f in a_only), sum(g[2] for g in b_only)
    # folders
    ca, cb = {}, {}
    for f in A["files"]:
        ca[f[0]] = ca.get(f[0], 0) + 1
    for g in B["files"]:
        cb[g[0]] = cb.get(g[0], 0) + 1
    R["folders_only_a"] = sorted(({"folder": "/".join(k) or ".", "files": v} for k, v in ca.items() if k not in cb), key=lambda x: -x["files"])[:15]
    R["folders_only_b"] = sorted(({"folder": "/".join(k) or ".", "files": v} for k, v in cb.items() if k not in ca), key=lambda x: -x["files"])[:15]
    R["folders_diff"] = sorted(({"folder": "/".join(k) or ".", "a": ca[k], "b": cb[k]} for k in ca if k in cb and ca[k] != cb[k]), key=lambda x: -abs(x["a"] - x["b"]))[:15]
    # what a merge would produce, and a one-line verdict
    small = max(1, min(R["a_files"], R["b_files"]))
    sp_n = 0
    for f, g in conflicts:
        h1, h2 = hashes.get(f[4]), hashes.get(g[4])
        if h1 is not None and h2 is not None and bin(h1 ^ h2).count("1") <= threshold:
            sp_n += 1
    R["same_picture_conflicts"] = sp_n
    R["alike_pct"] = min(100, round(100 * (R["identical"] + len(sim_pairs) + sp_n) / small))
    R["merged_files"] = R["a_files"] + len(b_only) + len(conflicts)
    R["merged_if_near_skipped"] = R["a_files"] + len(b_only) + (len(conflicts) - sp_n)
    # a third/fourth library counted against the first
    for li in range(2, len(libs)):
        sigs_a = {sg for sg in A["sigs"] if sg[1] is not None}
        same = sum(1 for sg in libs[li]["sigs"] if sg[1] is not None and sg in sigs_a)
        R["matrix"].append({"label": libs[li]["label"], "files": len(libs[li]["files"]), "identical_to_first": same})
    if any(f[6] for f in A["files"] + B["files"]):
        R["notes"].append("Zip files were compared by name and content fingerprint only: pictures inside zips are not compared by how they look.")
    return R


# ---- System activity: is Photos running, busy and making progress? ------------------------------------------------
PHOTOS_PROCS = ("photolibraryd", "cloudphotod", "Photos", "assetsd", "mediaanalysisd", "photoanalysisd", "bird", "cloudd")


def system_snapshot(want_activity=True):
    """A quick look at the Mac: the Photos-related processes and what they use, power, disk, DNS, and how busy the iCloud
    photo uploader has been in the last 10 minutes. Read-only; every part is optional."""
    out = {"mac": sys.platform == "darwin", "procs": [], "power": {}, "free": None, "dns": None, "activity": None, "photos_open": False}
    try:
        r = subprocess.run(["ps", "-axo", "pid=,pcpu=,rss=,etime=,comm="], capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) == 5:
                name = os.path.basename(parts[4].strip())
                if name in PHOTOS_PROCS:
                    out["procs"].append({"name": name, "cpu": float(parts[1]), "mb": round(int(parts[2]) / 1024), "up": parts[3]})
                    if name == "Photos":
                        out["photos_open"] = True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if shutil.which("pmset"):
        try:
            b = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"(\d+)%", b)
            out["power"] = {"battery": int(m.group(1)) if m else None, "ac": "AC Power" in b, "charging": "charging" in b and "discharging" not in b and "not charging" not in b}
            lp = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=10).stdout
            out["power"]["low_power"] = bool(re.search(r"lowpowermode\s+1", lp))
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        out["free"] = shutil.disk_usage(Path.home()).free
    except OSError:
        pass
    try:
        import socket
        socket.setdefaulttimeout(3)
        socket.gethostbyname("www.icloud.com")
        out["dns"] = True
    except Exception:
        out["dns"] = False
    if want_activity and sys.platform == "darwin" and shutil.which("log"):
        try:
            r = subprocess.run(["log", "show", "--last", "10m", "--style", "compact", "--predicate", 'process == "cloudphotod" OR process == "photolibraryd"'],
                               capture_output=True, text=True, timeout=90)
            out["activity"] = len([l for l in r.stdout.splitlines() if l[:2] == "20"])
        except (OSError, subprocess.TimeoutExpired):
            pass
    return out


def diagnose_system(snap, upload=None, issues=None, health=None):
    """Turn the pieces (system snapshot, upload progress, log issues, library health) into one verdict and a short
    prioritised list of what to do. Returns {"verdict", "headline", "progress", "actions", "checks"}."""
    checks, actions = [], []

    def chk(level, title, detail, action=""):
        checks.append({"level": level, "title": title, "detail": detail})
        if action and level in ("bad", "warn"):
            actions.append({"level": level, "title": title, "do": action})
    upload = upload or {}
    pending = upload.get("pending")
    eta = upload.get("eta") or {}
    # progress
    prog = {"state": "unknown", "text": "Upload progress could not be read."}
    if upload.get("ok") and pending is not None:
        if pending == 0:
            prog = {"state": "done", "text": "Everything in this library is uploaded to iCloud."}
        elif eta.get("stalled"):
            prog = {"state": "stalled", "text": "%s items are waiting and the number has not fallen for about 45 minutes." % f"{pending:,}"}
        elif eta.get("rate_per_hour", 0) > 0 and eta.get("eta_hours"):
            prog = {"state": "progressing", "text": "%s items waiting, uploading about %s an hour, roughly %s h left." % (f"{pending:,}", f"{eta['rate_per_hour']:,}", eta["eta_hours"])}
        elif eta.get("rate_per_hour", 0) < 0:
            prog = {"state": "growing", "text": "More items are arriving (%s waiting) than are uploading. This is normal while a big import runs." % f"{pending:,}"}
        else:
            prog = {"state": "unknown", "text": "%s items waiting. Check again in a few minutes to measure the speed." % f"{pending:,}"}
    # system
    procs = {p["name"]: p for p in snap.get("procs", [])}
    if snap.get("mac"):
        if pending and not snap.get("photos_open"):
            chk("warn", "Photos is not open", "Uploads are most reliable while Photos is open.", "Open Photos and leave it open until the upload finishes.")
        elif snap.get("photos_open"):
            chk("ok", "Photos is open", "The Photos app is running.")
        pw = snap.get("power") or {}
        if pw.get("low_power"):
            chk("warn", "Low Power Mode is on", "macOS holds back background uploads in Low Power Mode.", "Turn off Low Power Mode (System Settings > Battery).")
        if pw.get("battery") is not None and not pw.get("ac") and pw["battery"] < 30 and pending:
            chk("warn", "Running on battery (%d%%)" % pw["battery"], "Uploads slow down or pause on battery.", "Plug the Mac in until the upload finishes.")
        elif pw.get("ac"):
            chk("ok", "Plugged in", "Power is connected.")
        busy = [p for p in snap.get("procs", []) if p["name"] in ("mediaanalysisd", "photoanalysisd") and p["cpu"] > 30]
        if busy:
            chk("info", "Photos is analysing your library", "mediaanalysisd/photoanalysisd are using %d%% CPU. This is normal after a big import and slows uploads." % round(max(p["cpu"] for p in busy)),
                "Leave the Mac on and plugged in: it finishes by itself.")
        act = snap.get("activity")
        if pending and act is not None:
            if act < 5 and "cloudphotod" in procs:
                chk("warn", "No upload activity", "The iCloud Photos uploader wrote almost nothing to the log in the last 10 minutes although %s items are waiting." % f"{pending:,}",
                    "Open Photos and look at the bottom of the Library view for 'Paused'. Press Resume, then run the log check.")
            elif act is not None and act >= 5:
                chk("ok", "The uploader is active", "%s log entries from the iCloud Photos services in the last 10 minutes." % f"{act:,}")
        if pending and "cloudphotod" not in procs and snap.get("photos_open"):
            chk("warn", "The iCloud Photos service is not running", "cloudphotod handles uploads and is not running.", "Quit and reopen Photos; if it persists restart the Mac.")
    if snap.get("free") is not None:
        if snap["free"] < 5e9:
            chk("bad", "Very little free disk space", "Only %.1f GB free on this Mac." % (snap["free"] / 1e9), "Free space now: Photos cannot import or sync reliably when the disk is nearly full.")
        elif snap["free"] < 20e9:
            chk("warn", "Low free disk space", "%.1f GB free on this Mac." % (snap["free"] / 1e9), "Free some space or switch on Optimize Mac Storage.")
        else:
            chk("ok", "Disk space is fine", "%.0f GB free." % (snap["free"] / 1e9))
    if snap.get("dns") is False:
        chk("bad", "No internet connection", "www.icloud.com could not be reached.", "Check Wi-Fi or Ethernet, VPN and DNS.")
    elif snap.get("dns"):
        chk("ok", "Internet reachable", "iCloud's address resolves.")
    if upload.get("ok") and upload.get("icloud_on") is False:
        chk("bad", "iCloud Photos looks switched off", "Nothing in this library is marked as uploaded.", "Turn on iCloud Photos in Photos > Settings > iCloud.")
    if prog["state"] == "stalled":
        chk("bad", "Uploads look stuck", prog["text"], "Run the log check below and follow the fix for the top issue (Low Power Mode, a paused sync, no iCloud space and network loss are the usual causes).")
    for i in (issues or []):
        if i["sev"] in ("bad", "warn"):
            chk(i["sev"], i["title"] + (" (%dx in the logs)" % i["count"]), i["meaning"], i["fixes"][0] if i.get("fixes") else "")
    if health:
        sc = health.get("score")
        if sc is not None and sc < 70:
            chk("warn", "Library health score is %d" % sc, "See the Health tab for the findings.", "Open the Health tab and work through the top findings.")
        elif sc is not None:
            chk("ok", "Library health score is %d" % sc, "")
    levels = [c["level"] for c in checks]
    verdict = "problem" if "bad" in levels else ("attention" if "warn" in levels else "healthy")
    headline = {"problem": "Something needs fixing now.", "attention": "Mostly fine, a few things to look at.", "healthy": "Everything looks healthy."}[verdict]
    actions.sort(key=lambda a: 0 if a["level"] == "bad" else 1)
    return {"verdict": verdict, "headline": headline, "progress": prog, "actions": actions[:8], "checks": checks}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="folder containing all extracted 'Takeout N' folders")
    ap.add_argument("--out", type=Path, help="copy fixed files here instead of editing in place")
    ap.add_argument("--dry-run", action="store_true", help="match only; change nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace existing location and caption (dates follow --date-policy)")
    ap.add_argument("--date-policy", choices=["earlier", "photo", "google"], default="earlier",
                    help="when the photo already has a different date: keep the earlier one (default), the photo's, or Google's")
    ap.add_argument("--pair-live", action="store_true",
                    help="relink Live Photo videos to their still; videos become .MOV (works in place or with --out)")
    ap.add_argument("--dedupe", action="store_true", help="skip byte-identical duplicate files")
    ap.add_argument("--move", action="store_true", help="move files into --out instead of copying (frees space)")
    ap.add_argument("--sort-only", action="store_true",
                    help="only merge same-named folders and (with --dedupe) drop duplicates; no metadata changes")
    ap.add_argument("--name-dates", action="store_true", help="when a file has no .json and no date, read the date from its file name")
    ap.add_argument("--no-json", action="store_true", help="with --sort-only: do not bring .json files along")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report", type=Path, default=Path("takeout_report.csv"))
    args = ap.parse_args()

    if args.sort_only and not args.out and not args.move:
        sys.exit("--sort-only needs --out (or --move to merge into the folder you gave)")
    if args.sort_only and args.move and not args.out:
        args.out = args.root
    if not args.dry_run and not args.sort_only and not shutil.which("exiftool"):
        sys.exit("exiftool not found. macOS: brew install exiftool | Windows: https://exiftool.org")
    if args.move and not args.out:
        args.out = args.root  # merge into the folder given (nothing moves if there is only one)
    if not args.root.is_dir():
        sys.exit(f"{args.root} is not a folder")

    print("Scanning...")
    media, sidecars = scan(args.root)
    print(f"  {len(media)} media files, {len(sidecars)} json files")
    idx = build_index(sidecars)
    args.bring_json = not args.no_json
    if args.sort_only:
        args.manifest_file = MANIFEST_SORT
    args.out_root = args.out
    args.roots = [args.root]
    prepare(args, media)
    if args.dedupe:
        print(f"  {len(args.dupes)} exact duplicates will be skipped ({args.dupe_bytes / 1e9:.1f} GB)")

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, row in enumerate(ex.map(lambda m: guarded(sort_one if args.sort_only else process)(m, idx, args, args.out), media), 1):
            rows.append(row)
            if i % 500 == 0:
                print(f"  {i}/{len(media)}")

    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        w.writeheader()
        w.writerows(rows)

    close_all()
    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    print("Done. " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()

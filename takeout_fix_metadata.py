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
    # a duplicate marker can also sit before the suffix: "img(1).jpg.json" is not
    # used by Google, but "img.jpg(1).json" is, and is handled above.
    return norm(base + dup)


def media_candidates(name):
    """Sidecar keys that could describe this media filename, best match first."""
    stem, ext = os.path.splitext(name)
    names = [name]
    if stem.endswith("-edited"):  # edited copy shares the original's json
        names.append(stem[: -len("-edited")] + ext)
    out = []
    for n in names:
        s, e = os.path.splitext(n)
        m = DUP_RE.search(s)
        variants = [n]
        if m:  # IMG(1).jpg  ->  IMG.jpg(1)
            variants.append(s[:m.start()] + e + m.group(0))
        for v in variants:
            out.append(v)
            if len(v) > NAME_LIMIT:
                out.append(v[:NAME_LIMIT])
    seen, res = set(), []
    for v in out:
        if norm(v) not in seen:
            seen.add(norm(v))
            res.append(norm(v))
    return res


def scan(root):
    media, sidecars = [], []
    scan.noext = 0
    for dirpath, _, files in os.walk(root):
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
        self.p.stdin.write(("\n".join(a) + "\n").encode("utf-8"))
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
    """Existing date / GPS / description in a file, for before-and-after counting."""
    try:
        _, lines = tool().run(["-j", "-n", "-api", "QuickTimeUTC=1", "-DateTimeOriginal",
                               "-QuickTime:CreateDate", "-GPSLatitude", "-GPSLongitude", "-Keys:GPSCoordinates",
                               "-ImageDescription", "-XMP-dc:Description", str(path)])
    except (OSError, RuntimeError):
        return {}
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("["))
        d = json.loads("\n".join(lines[start:]))[0]
    except (StopIteration, ValueError, IndexError):
        return {}
    lat, lon = d.get("GPSLatitude"), d.get("GPSLongitude")
    kc = d.get("Keys:GPSCoordinates") or d.get("GPSCoordinates")
    if isinstance(kc, str):
        parts = kc.replace(",", " ").split()
        if len(parts) >= 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
            except ValueError:
                pass
    first, second = ("CreateDate", "DateTimeOriginal") if is_video else ("DateTimeOriginal", "CreateDate")
    date = str(d.get(first) or d.get(second) or "")
    desc = str(d.get("ImageDescription") or d.get("Description") or "").strip()
    return {"date": date, "lat": lat, "lon": lon, "desc": desc}


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
                 "gps", "gps_before", "gps_google",
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
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def keep_rank(p):
    """Which copy of an exact duplicate to keep: year folders before albums, then lowest Takeout number."""
    m = re.search(r"Takeout (\d+)", str(p))
    return (0 if p.parent.name.startswith("Photos from") else 1, int(m.group(1)) if m else 0, str(p))


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
        by_hash = defaultdict(list)
        for m in g:
            by_hash[file_hash(m)].append(m)
            done += 1
            if progress:
                progress(done, todo)
        for same in by_hash.values():
            if len(same) > 1:
                same.sort(key=keep_rank)
                for other in same[1:]:
                    dupes[str(other)] = str(same[0])
                    saved += other.stat().st_size
    return dupes, saved


STILL_SUFFIXES = (".HEIC", ".heic", ".JPG", ".jpg", ".JPEG", ".jpeg", ".HEIF", ".heif")


def read_content_ids(paths, progress=None, chunk=400):
    """ContentIdentifier of many files using a few exiftool runs instead of one per file."""
    ids, done = {}, 0
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        arg = Path(tempfile.mkstemp(suffix=".args")[1])
        try:
            arg.write_text("\n".join(str(p) for p in part), encoding="utf-8")
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


def place_file(src, dest, move):
    """Copy or move one file, retrying once and cleaning up a partial copy if it fails."""
    for attempt in (1, 2):
        try:
            if move:
                shutil.move(str(src), str(dest))
            else:
                shutil.copy2(src, dest)
            return
        except OSError:
            try:
                if dest.exists():
                    dest.unlink()
            except OSError:
                pass
            if attempt == 2:
                raise
            time.sleep(1)


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


def claim_dest(dest, args):
    """Reserve a unique output path (thread-safe, also avoids clashes between files in a dry run)."""
    with args.lock:
        cand, i = dest, 0
        while str(cand) in args.claimed or cand.exists():
            i += 1
            cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        args.claimed.add(str(cand))
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
    d = load_json(sc) if sc else None
    status0 = "no-json" if not sc else "bad-json"
    if d is None and getattr(args, "name_dates", False) and ext not in NO_WRITE_EXT:
        nt = date_from_name(m.name)
        if nt:
            have = read_existing(m, ext in VIDEO_EXT).get("date", "")
            if not have or have.startswith("0000"):          # only fills a missing date, never changes one
                d = {"photoTakenTime": {"timestamp": str(nt)}}
                row["match"] = "filename-date"
                row["detail"] = "date taken from the file name"
    skip, final_taken = set(), None
    if d and ext not in NO_WRITE_EXT:
        cl = classify(d, ext, read_existing(m, ext in VIDEO_EXT), args.overwrite, getattr(args, "date_policy", "earlier"))
        final_taken = cl.pop("_taken_final", None)
        row.update(cl)
        skip = {k for k in ("date", "gps", "desc") if row.get(k) in ("same", "kept", "none")}
    exif_args, taken = build_args(d, ext, args.overwrite, skip) if d else ([], None)
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
                args.claimed.add(str(m))
        elif adopt:
            dest, at_dest = first, True
            with args.lock:
                args.claimed.add(str(first))
            row["detail"] = "identical copy already in the destination: fixed there"
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
        row["status"] = status0
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
            shutil.copy2(sc, jd)
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
            "duration": dur, "has_video": v is not None}


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
    return a <= 0 or abs(a - b) <= max(1.0, 0.02 * a)


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
        if oi and oi["has_video"] and _dur_ok(info["duration"], oi["duration"]):
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
                    shutil.copy2(sp, dp)
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
            shutil.move(str(src), str(dest))
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


def find_empty_dirs(root, ignore_junk=True, extra_ignored=None):
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
            Path(arg).write_text("\n".join(str(p) for p in part), encoding="utf-8")
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
                        shutil.move(str(p), _free_name(str(dest)) if dest.exists() else str(dest))
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
                shutil.move(sp, tp)
                stats["moved"] += 1
            elif _same_content(sp, tp):
                stats["dupes"] += 1
                if dupes_action == "delete":
                    os.remove(sp)
                else:
                    aside = os.path.join(os.path.dirname(dst), "_duplicates", os.path.basename(dst), rel if rel != "." else "")
                    os.makedirs(aside, exist_ok=True)
                    shutil.move(sp, _free_name(os.path.join(aside, f)) if os.path.exists(os.path.join(aside, f)) else os.path.join(aside, f))
            else:
                shutil.move(sp, _free_name(tp))
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
                    os.rename(old, new)
                    row["action"] = "renamed"
                    oj, nj = old + ".json", new + ".json"
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
    roots = [Path(r) for r in roots]
    dest = Path(dest) if dest else roots[0]
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
            shutil.move(src, str(target)) if move else shutil.copytree(src, str(target), symlinks=True)
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
            shutil.copy2(sc, jd)
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
                if move and not dry_run:
                    if dupes == "delete":
                        os.remove(src)
                    else:
                        shutil.move(src, aside(DUPES_DIR, dparts, name))
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
            if existing and not is_b and _same_content(src, existing):
                per_root[ri]["identical"] += 1
                dir_sources[dir_key].add(ri)
                row["status"] = "identical"
                json_mark(src)
                if move and not dry_run:
                    if dupes == "delete":
                        os.remove(src)
                    else:
                        shutil.move(src, aside(DUPES_DIR, dparts, name))
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
                                shutil.move(str(target), aside(CONFLICTS_DIR, dparts, name))
                                oj = Path(str(target) + ".json")
                                if oj.exists():
                                    shutil.move(str(oj), aside(CONFLICTS_DIR, dparts, name + ".json"))
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

ZIP_STAGE = ".metadatafixer_stage"
ZIPS_LOG = ".metadatafixer_zips.jsonl"


def split_sources(entries):
    """Sort what the user added into zip files and ordinary folders.
    A folder that holds .zip files at its top level counts as those zips."""
    zips, folders, seen = [], [], set()
    for r in entries:
        p = Path(r).expanduser()
        if p.is_file() and p.suffix.lower() == ".zip":
            found = [p]
        elif p.is_dir():
            found = sorted(q for q in p.iterdir() if q.is_file() and q.suffix.lower() == ".zip" and not q.name.startswith("._"))
            if not found:
                if p.resolve() not in seen:
                    seen.add(p.resolve())
                    folders.append(p)
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
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
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
                    _extract_member(zf, i, parts, tree)
                    n += 1
    return n


def stage_media(zpath, tree, should_stop=None):
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
                out.append(_extract_member(zf, i, parts, tree))
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
    zips, folders = split_sources(entries)
    F = {"zips": len(zips), "folders": len(folders), "media": 0, "media_bytes": 0, "json": 0, "by_ext": {}, "wrapper": False,
         "junk": 0, "zero_media": 0, "zero_other": 0, "extless": 0, "legacy": {}, "legacy_n": 0, "legacy_bytes": 0,
         "live_pairs": 0, "dup_n": 0, "dup_bytes": 0, "dup_exact": bool(zips), "empty_dirs": 0, "tidy_dirs": 0,
         "matched": 0, "unmatched": 0, "name_date_candidates": 0, "bad_zips": [], "zip_gaps": [], "zip_bytes": 0, "biggest_zip": 0,
         "sample": None, "free": None, "need": None, "no_ext_bogus": 0}
    items = []                       # (virtual path, size, crc or None, source, member)
    jsons = []                       # (virtual path, source, member)
    plan = zip_plan(zips) if zips else []
    for z in plan:
        if z["error"]:
            F["bad_zips"].append((z["name"], z["error"]))
    nums = sorted(int(m.group(1)) for z in zips for m in [re.search(r"-(\d{3})\\.zip$", z.name)] if m)
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
                if re.search(r" \\(\d+\\)$| copy( \d+)?$|  +", d):
                    F["tidy_dirs"] += 1
            for n in fns:
                p = Path(dp) / n
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                items.append((PurePosixPath(*p.parts[1:]) if p.is_absolute() else PurePosixPath(*p.parts), sz, None, p, ""))
    media = []
    for vp, sz, crc, src, mem in items:
        n = vp.name
        low = n.lower()
        ext = vp.suffix.lower()
        if any(x.lower() in ("takeout", "google photos") for x in vp.parts[:2]):
            F["wrapper"] = True
        if low.endswith(".json"):
            F["json"] += 1
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
    # A small sample of real files: how many already have a date and a location, and what Google's info would add
    cand = [m for m in media if m[0].suffix.lower() in MEDIA_EXT and m[0].suffix.lower() not in NO_WRITE_EXT and 0 < m[1] <= 60 * 1024 * 1024]
    random.seed(7)
    pick = random.sample(cand, min(sample_n, len(cand))) if cand else []
    if pick and shutil.which("exiftool"):
        tmp = tempfile.mkdtemp(prefix="metadatafixer_assess_")
        S = {"n": 0, "has_date": 0, "has_gps": 0, "has_desc": 0, "with_json": 0, "add_date": 0, "add_gps": 0, "add_desc": 0}
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
                except (OSError, KeyError, zipfile.BadZipFile):
                    continue
                S["n"] += 1
                has_d = bool(ex.get("date")) and not str(ex.get("date")).startswith("0000")
                has_g = ex.get("lat") is not None and ex.get("lon") is not None
                has_t = bool(ex.get("desc"))
                S["has_date"] += has_d
                S["has_gps"] += has_g
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
            F["sample"] = S
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
               "archive", "archived", "unused", "trash", "donotuse", "extra", "extras", "v1", "v2", "v3"}


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
    while toks and _fold(toks[0]) in AFFIX_WORDS and len(toks) > 1:
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
                for i, a in enumerate(rest):
                    if a["name"] in seen:
                        continue
                    cluster = [a]
                    for b in rest[i + 1:]:
                        if b["name"] in seen or len(a["squash"]) < 6:
                            continue
                        da, db = re.sub(r"\D", "", a["squash"]), re.sub(r"\D", "", b["squash"])
                        if da == db and difflib.SequenceMatcher(None, a["squash"], b["squash"]).ratio() >= 0.88:
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
        names = [m for m in g["members"] if os.path.isdir(os.path.join(parent, m))]
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


def dhash_image(path):
    """A 64-bit fingerprint of how a picture looks (a 'difference hash'); similar pictures give fingerprints that differ
    in only a few bits. Returns an int, or None if the picture cannot be read."""
    def run(cmd, data=None):
        try:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
            return r.stdout if r.returncode == 0 else b""
        except (OSError, subprocess.TimeoutExpired):
            return b""
    vf = "scale=9:8:flags=area,format=gray"
    raw = run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"])
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


def find_similar_photos(roots, threshold=6, progress=None, should_stop=None, workers=6):
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
                Path(arg).write_text("\n".join(need[k:k + 300]), encoding="utf-8")
                r = subprocess.run(["exiftool", "-charset", "filename=utf8", "-j", "-n", "-ImageWidth", "-ImageHeight", "-DateTimeOriginal", "-GPSLatitude", "-@", arg],
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
            it = info.get(p, {})
            mem.append({"path": p, "size": sizes.get(p, 0), "w": it.get("ImageWidth") or 0, "h": it.get("ImageHeight") or 0,
                        "date": str(it.get("DateTimeOriginal") or "")[:19], "gps": it.get("GPSLatitude") is not None, "hash": hashes[p]})
        mem.sort(key=lambda m: (-(m["w"] * m["h"]), -m["size"], not m["gps"], not m["date"], m["path"]))
        groups.append(mem)
    groups.sort(key=lambda g: (-len(g), g[0]["path"]))
    return groups, total


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

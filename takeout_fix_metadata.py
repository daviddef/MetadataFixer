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
import csv
import json
import os
import re
import shutil
import subprocess
import sys
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
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            ext = p.suffix.lower()
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
        if by_folder.get((folder, k)):
            return by_folder[(folder, k)][0], "folder"
    for k in cands:
        if by_key.get(k):
            return by_key[k][0], "tree"
    stem = norm(os.path.splitext(m.name)[0])
    if by_stem.get((folder, stem)):
        return by_stem[(folder, stem)][0], "stem"
    return None, None


def ts(d, field):
    try:
        return int(d[field]["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None


def build_args(d, ext, overwrite):
    """exiftool arguments for one sidecar. Returns (args, taken_epoch)."""
    is_video = ext in VIDEO_EXT
    taken = ts(d, "photoTakenTime") or ts(d, "creationTime")
    a = []
    if taken:
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
    if desc:
        a += [f"-XMP-dc:Description={desc}"]
        if not is_video:
            a += [f"-ImageDescription={desc}"]
    for person in d.get("people") or []:
        name = (person or {}).get("name")
        if name:
            a += [f"-XMP-iptcExt:PersonInImage+={name}"]
    if d.get("favorited"):
        a += ["-XMP:Rating=5"]
    return a, taken


def run_exiftool(target, args, overwrite, sidecar_for_raw=False):
    cmd = ["exiftool", "-q", "-m", "-overwrite_original", "-P"]
    if not overwrite:
        cmd += ["-wm", "cg"]  # create missing tags, never replace existing ones
    cmd += args
    if sidecar_for_raw:
        xmp = str(target.with_suffix(".xmp"))
        # pull the 'taken' info into an xmp sidecar next to the raw file
        cmd = ["exiftool", "-q", "-m", "-P", "-o", xmp] + cmd[4:] + [str(target)]
        # xmp output can't hold EXIF-group tags; map AllDates onto XMP equivalents
        cmd = [c.replace("-AllDates=", "-XMP:DateCreated=") for c in cmd]
        cmd = [c for c in cmd if not c.startswith(("-ImageDescription", "-GPSAltitudeRef",
                                                   "-GPSLatitudeRef", "-GPSLongitudeRef"))]
        cmd = [c.replace("-GPSLatitude=", "-XMP:GPSLatitude=").replace("-GPSLongitude=", "-XMP:GPSLongitude=")
               .replace("-GPSAltitude=", "-XMP:GPSAltitude=") for c in cmd]
        if Path(xmp).exists():
            cmd.remove("-o"); cmd.remove(xmp)
            cmd.insert(cmd.index("-P") + 1, "-overwrite_original")
    else:
        cmd.append(str(target))
    r = subprocess.run(cmd, capture_output=True, text=True)
    msg = (r.stderr or r.stdout).strip()
    if r.returncode != 0 and not sidecar_for_raw and "looks more like" in msg:
        return _retry_real_type(target, cmd[:-1], msg)
    return r.returncode == 0, msg


def _retry_real_type(target, cmd, first_msg):
    """Google sometimes saves JPEGs as .HEIC (etc). Write via a temp name with the real extension."""
    r = subprocess.run(["exiftool", "-s3", "-FileTypeExtension", str(target)], capture_output=True, text=True)
    real = r.stdout.strip().lower()
    if not real:
        return False, first_msg
    tmp = target.with_name(target.stem + ".__fix__." + real)
    target.rename(tmp)
    try:
        r2 = subprocess.run(cmd + [str(tmp)], capture_output=True, text=True)
    finally:
        tmp.rename(target)
    return r2.returncode == 0, (r2.stderr or r2.stdout).strip()


def unique_dest(dest):
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


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


def process(m, idx, args, out_root):
    sc, how = find_sidecar(m, idx)
    row = {"file": str(m), "sidecar": str(sc) if sc else "", "match": how or "", "status": "",
           "detail": "", "live": ""}
    cid = None
    if getattr(args, "pair_live", False) and m.suffix.lower() in (".mp4", ".mov"):
        cid, why = live_id(m)
        row["live"] = "paired" if cid else why
    if not sc:
        row["status"] = "no-json"
        return row
    d = load_json(sc)
    if not d:
        row["status"] = "bad-json"
        return row
    ext = m.suffix.lower()
    exif_args, taken = build_args(d, ext, args.overwrite)
    if args.dry_run:
        row["status"] = "would-update"
        row["detail"] = " ".join(exif_args)[:200]
        return row
    target = m
    if out_root:
        dest = unique_dest(out_root / m.parent.name / m.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(m, dest)
        target = dest
    if exif_args and ext not in NO_WRITE_EXT:
        ok, msg = run_exiftool(target, exif_args, args.overwrite, sidecar_for_raw=ext in RAW_EXT)
        row["status"] = "updated" if ok else "exiftool-error"
        row["detail"] = msg[:300]
    else:
        row["status"] = "mtime-only"
    if cid and not args.dry_run:
        target, err = pair_live(target, cid)
        if err:
            row["live"] = "pair-error"
            row["detail"] = (row["detail"] + " " + err).strip()[:300]
    if taken:
        try:
            os.utime(target, (taken, taken))
        except OSError:
            pass
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="folder containing all extracted 'Takeout N' folders")
    ap.add_argument("--out", type=Path, help="copy fixed files here instead of editing in place")
    ap.add_argument("--dry-run", action="store_true", help="match only; change nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace existing EXIF values")
    ap.add_argument("--pair-live", action="store_true",
                    help="relink Live Photo videos to their still (needs --out); videos become .MOV")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report", type=Path, default=Path("takeout_report.csv"))
    args = ap.parse_args()

    if not args.dry_run and not shutil.which("exiftool"):
        sys.exit("exiftool not found. macOS: brew install exiftool | Windows: https://exiftool.org")
    if args.pair_live and not (args.out or args.dry_run):
        sys.exit("--pair-live renames videos, so it needs --out (or --dry-run)")
    if not args.root.is_dir():
        sys.exit(f"{args.root} is not a folder")

    print("Scanning...")
    media, sidecars = scan(args.root)
    print(f"  {len(media)} media files, {len(sidecars)} json files")
    idx = build_index(sidecars)

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, row in enumerate(ex.map(lambda m: process(m, idx, args, args.out), media), 1):
            rows.append(row)
            if i % 500 == 0:
                print(f"  {i}/{len(media)}")

    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "sidecar", "match", "status", "detail", "live"])
        w.writeheader()
        w.writerows(rows)

    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    print("Done. " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()

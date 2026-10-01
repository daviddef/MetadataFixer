#!/usr/bin/env python3
"""Local web UI for takeout_fix_metadata.py.

    python3 takeout_gui.py

Opens http://127.0.0.1:8765 in your browser. Nothing leaves your machine; the
server only listens on localhost. Needs exiftool (brew install exiftool).
"""
import argparse
import hashlib
import time
import urllib.request
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import takeout_fix_metadata as fx

VERSION = "2026.10.01-f"
STATE = {"state": "idle", "total": 0, "done": 0, "counts": {}, "message": "", "report": "", "scan": None, "summary": None, "extra": {}, "recent": [], "clean": {"state": "idle"}, "update": {"state": "idle", "files": []}, "phase": None, "kind": "fix", "version": VERSION, "boot": time.time()}
LOCK = threading.Lock()


def choose_folders(prompt):
    """Native macOS picker allowing several folders; returns a list of POSIX paths."""
    script = ('set fs to choose folder with prompt "%s" with multiple selections allowed\n'
              'set out to {}\nrepeat with f in fs\nset end of out to POSIX path of f\nend repeat\n'
              'set AppleScript\'s text item delimiters to linefeed\nreturn out as text' % prompt)
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    return [p.rstrip("/") for p in r.stdout.splitlines() if p.strip()] if r.returncode == 0 else []


BATCH_RE = re.compile(r"Takeout \d+")


def batch_of(path):
    m = BATCH_RE.search(path)
    return m.group(0) if m else "(other)"


def is_media_sidecar(p):
    key = fx.json_key(p)
    key = fx.DUP_RE.sub("", key)
    return Path(key).suffix in fx.MEDIA_EXT


def top(counter, n=15):
    return sorted(counter.items(), key=lambda kv: -kv[1])[:n]


def summarise(rows, sidecars, roots, dry_run):
    total = len(rows)
    st = defaultdict(int)
    match = defaultdict(int)
    ext = defaultdict(lambda: [0, 0])      # ext -> [matched, no_json]
    batch = defaultdict(lambda: [0, 0])
    album_nj = defaultdict(int)
    for r in rows:
        ok = r["status"] != "no-json"
        st[r["status"]] += 1
        if r["status"] == "duplicate":
            continue
        if r["match"]:
            match[r["match"]] += 1
        e = Path(r["file"]).suffix.lower()
        ext[e][0 if ok else 1] += 1
        batch[batch_of(r["file"])][0 if ok else 1] += 1
        if not ok:
            album_nj[Path(r["file"]).parent.name] += 1
    fields = {k: defaultdict(int) for k in ("date", "gps", "desc")}
    for r in rows:
        if r["status"] in ("updated", "would-update"):
            for k in fields:
                if r.get(k):
                    fields[k][r[k]] += 1
    def changed(k):
        return fields[k].get("added", 0) + fields[k].get("replaced", 0)
    people_tagged = sum(1 for r in rows if r["status"] in ("updated", "would-update") and r.get("people"))
    favourites = sum(1 for r in rows if r["status"] in ("updated", "would-update") and r.get("favourite"))
    changes = {"dates": changed("date"), "dates_added": fields["date"].get("added", 0),
               "dates_replaced": fields["date"].get("replaced", 0),
               "gps": changed("gps"), "gps_added": fields["gps"].get("added", 0),
               "gps_replaced": fields["gps"].get("replaced", 0),
               "desc": changed("desc"), "people": people_tagged, "favourites": favourites}
    replaced_files = sum(1 for r in rows if r["status"] in ("updated", "would-update")
                         and "replaced" in (r.get("date"), r.get("gps"), r.get("desc")))
    live = defaultdict(int)
    for r in rows:
        if r.get("live"):
            live[r["live"]] += 1
    used = {r["sidecar"] for r in rows if r["sidecar"]}
    used_keys = {(Path(u).parent.name, fx.json_key(Path(u))) for u in used}
    orphans = [p for p in sidecars if str(p) not in used and is_media_sidecar(p)
               and (p.parent.name, fx.json_key(p)) not in used_keys]
    nj = st.get("no-json", 0)
    dup = st.get("duplicate", 0)
    unique = total - dup
    folders = defaultdict(int)
    for r in rows:
        if r.get("output"):
            folders[str(Path(r["output"]).parent)] += 1
    base = os.path.commonpath(list(folders)) if folders else ""
    tips = []
    if nj and not orphans:
        tips.append("Every Google info file (.json) in these folders was used, so the %d files with no JSON have none in the folders "
                    "you added. Their JSON is probably in other Takeout batches: add those folders and run again." % nj)
    elif nj and orphans:
        tips.append("%d sidecars matched no photo while %d photos matched no sidecar. That can mean a naming "
                    "mismatch; check the no-json list." % (len(orphans), nj))
    if not nj:
        tips.append("Every file found a JSON sidecar.")
    upload_like = sum(1 for r in rows if r.get("date_note") and r.get("date") in ("added", "replaced")
                      and r["status"] in ("updated", "would-update"))
    if upload_like:
        tips.append("%d dates written from Google look like its upload time rather than when the photo was taken "
                    "(see the 'date_note' column in the report). Check a few of them." % upload_like)
    kept = sum(fields[k].get("kept", 0) for k in fields)
    if kept:
        tips.append("%d existing date/location/description values differ from Google's but were left alone. "
                    "Tick 'Replace location and caption already stored in the photo' to replace locations and captions, or change the date setting." % kept)
    if live.get("no-id") or live.get("no-still"):
        tips.append("Live Photos: %d videos had a still but no Apple ID to copy, %d had no matching still; those stay as separate videos."
                    % (live.get("no-id", 0), live.get("no-still", 0)))
    if st.get("copy-error") or st.get("error"):
        tips.append("%d files could not be copied or processed and were skipped; see the 'detail' column in the report. "
                    "'Input/output error' (Errno 5) usually means a problem reading or writing the drive or that file."
                    % (st.get("copy-error", 0) + st.get("error", 0)))
    if st.get("already-done"):
        tips.append("%d files were already in place from an earlier run and were skipped." % st["already-done"])
    if st.get("exiftool-error"):
        tips.append("%d files hit an exiftool error; see the 'detail' column in the report." % st["exiftool-error"])
    if dup:
        tips.append("%d byte-identical duplicates were skipped, keeping the copy in the 'Photos from YYYY' folder. "
                    "Album copies of the same photo are not repeated, so album membership is not preserved." % dup)
    if dry_run:
        tips.append("This was a preview: nothing was changed. Untick 'Preview only' to apply.")
    return {
        "total": total, "unique": unique, "duplicates": dup, "matched": unique - nj, "no_json": nj,
        "pct_matched": round(100 * (unique - nj) / unique, 1) if unique else 0,
        "out_folders": sorted(([os.path.relpath(k, base) if k != base else ".", v] for k, v in folders.items())),
        "out_base": base,
        "status": dict(st), "match": dict(match), "orphans": len(orphans),
        "ext": sorted(([k, v[0], v[1]] for k, v in ext.items()), key=lambda x: -(x[1] + x[2])),
        "batch": sorted(([k, v[0], v[1]] for k, v in batch.items()),
                        key=lambda x: int(re.sub(r"\D", "", x[0]) or 0)),
        "album_no_json": top(album_nj), "live": dict(live), "fields": {k: dict(v) for k, v in fields.items()}, "replaced_files": replaced_files, "changes": changes, "tips": tips, "roots": [str(r) for r in roots], "dry_run": dry_run,
    }


def write_text_summary(path, sm):
    L = ["TAKEOUT METADATA FIXER SUMMARY", "Mode: " + ("preview (no changes)" if sm["dry_run"] else "applied"),
         "Folders: " + "; ".join(sm["roots"]), "",
         f"Media files: {sm['total']}", f"Exact duplicates skipped: {sm['duplicates']} ({sm.get('dupe_bytes', 0) / 1e9:.1f} GB)",
         f"Unique files with JSON: {sm['matched']} ({sm['pct_matched']}%)",
         f"No JSON: {sm['no_json']}", f"Sidecars with no photo: {sm['orphans']}", "", "Status:"]
    L += [f"  {k}: {v}" for k, v in sm["status"].items()]
    L += ["", "By file type (with JSON / no JSON):"] + [f"  {e or '(none)'}: {a} / {b}" for e, a, b in sm["ext"]]
    L += ["", "By Takeout batch (with JSON / no JSON):"] + [f"  {e}: {a} / {b}" for e, a, b in sm["batch"]]
    L += ["", "Albums with most no-JSON files:"] + [f"  {k}: {v}" for k, v in sm["album_no_json"]]
    c = sm.get("changes")
    if c:
        verb = "would change" if sm["dry_run"] else "changed"
        L += ["", f"Dates {verb}: {c['dates']} ({c['dates_added']} added, {c['dates_replaced']} replaced)",
              f"Locations {verb}: {c['gps']} ({c['gps_added']} added, {c['gps_replaced']} replaced)",
              f"Descriptions {verb}: {c['desc']}", f"People tagged on: {c['people']} files",
              f"Favourites marked: {c['favourites']}"]
    L += ["", f"Files with at least one value replaced: {sm['replaced_files']}", "EXIF fields (added / replaced / kept / same / none):"]
    for k, label in (("date", "Date taken"), ("gps", "Location"), ("desc", "Description")):
        f = sm["fields"].get(k, {})
        L.append(f"  {label}: " + " / ".join(str(f.get(x, 0)) for x in ("added", "replaced", "kept", "same", "none")))
    if sm.get("live"):
        L += ["", "Live Photo pairing:"] + [f"  {k}: {v}" for k, v in sm["live"].items()]
    if sm.get("out_folders"):
        L += ["", f"Output folders (under {sm['out_base']}):"] + [f"  {k}: {v}" for k, v in sm["out_folders"]]
    L += [""] + sm["tips"]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def run_job(roots, out, dry_run, overwrite, pair_live=False, dedupe=False, move=False, date_policy="earlier"):
    with LOCK:
        STATE.update(state="scanning", total=0, done=0, counts={}, message="Scanning folders...",
                     report="", summary=None, scan=None, extra={}, recent=[], phase=None, kind="fix")
    try:
        resolved, seen = [], set()
        for r in roots:
            p = Path(r).expanduser()
            if not p.is_dir():
                raise ValueError(f"Not a folder: {p}")
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                resolved.append(p)
        if not resolved:
            raise ValueError("Add at least one folder")
        if (pair_live or move) and not out and not dry_run:
            raise ValueError("Live Photo pairing and moving need an output folder")
        if not dry_run and not shutil.which("exiftool"):
            raise ValueError("exiftool not found. In Terminal run: brew install exiftool")
        media, sidecars, mseen, sseen = [], [], set(), set()
        for p in resolved:
            m, sc = fx.scan(p)
            media += [x for x in m if x.resolve() not in mseen and not mseen.add(x.resolve())]
            sidecars += [x for x in sc if x.resolve() not in sseen and not sseen.add(x.resolve())]
        idx = fx.build_index(sidecars)
        with LOCK:
            STATE.update(state="running", total=len(media),
                         scan={"media": len(media), "json": len(sidecars), "folders": len(resolved)},
                         message=f"{len(media)} media files, {len(sidecars)} json files")
        args = argparse.Namespace(dry_run=dry_run, overwrite=overwrite, pair_live=pair_live,
                                  dedupe=dedupe, move=move, out_root=out or None, roots=resolved, date_policy=date_policy)
        with LOCK:
            STATE["message"] = "Finding exact duplicates..." if dedupe else "Preparing..."

        def hashing(stage, done, todo):
            with LOCK:
                STATE["phase"] = {"stage": stage, "done": done, "total": todo}
                if stage == "dedupe":
                    STATE["message"] = (f"Step 1: checking {todo:,} files that share a size with another file for "
                                        f"exact duplicates ({done:,}/{todo:,})")
                else:
                    STATE["message"] = f"Step 2: reading Live Photo IDs ({done:,}/{todo:,} stills)"
        fx.prepare(args, media, hashing)
        with LOCK:
            STATE["phase"] = None
        with LOCK:
            STATE["message"] = f"Step 3: fixing and placing {len(media):,} files"
        out_root = Path(out) if out else None
        rows, counts, extra = [], defaultdict(int), defaultdict(int)
        out_dirs, recent = set(), []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for row in ex.map(lambda m: fx.guarded(fx.process)(m, idx, args, out_root), media):
                rows.append(row)
                counts[row["status"]] += 1
                if row["status"] in ("updated", "would-update"):
                    if "replaced" in (row["date"], row["gps"], row["desc"]):
                        extra["replaced_files"] += 1
                    extra["fields_replaced"] += [row["date"], row["gps"], row["desc"]].count("replaced")
                if row["status"] in ("updated", "would-update"):
                    if row["date"] in ("added", "replaced"):
                        extra["dates_changed"] += 1
                    if row["gps"] in ("added", "replaced"):
                        extra["gps_changed"] += 1
                    if row["desc"] in ("added", "replaced"):
                        extra["desc_changed"] += 1
                if row["live"] == "paired":
                    extra["live_paired"] += 1
                if row["status"] in ("copy-error", "error", "exiftool-error"):
                    extra["errors"] += 1
                if row["status"] == "duplicate":
                    extra["duplicates"] += 1
                if row["output"]:
                    extra["written"] += 1
                    out_dirs.add(str(Path(row["output"]).parent))
                    extra["folders"] = len(out_dirs)
                recent.append(row)
                del recent[:-12]
                with LOCK:
                    STATE["done"] = len(rows)
                    STATE["counts"] = dict(counts)
                    STATE["extra"] = dict(extra)
                    STATE["recent"] = [{"name": Path(r["file"]).name, "status": r["status"],
                                        "to": (Path(r["output"]).parent.name + "/" if r["output"] else ""),
                                        "live": r["live"]} for r in recent]
        report_dir = out_root or Path.home() / "Desktop"
        report_dir.mkdir(parents=True, exist_ok=True)
        tag = "dryrun" if dry_run else "report"
        report = report_dir / f"takeout_{tag}.csv"
        fields = fx.REPORT_FIELDS
        with open(report, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        with open(report_dir / f"takeout_{tag}_no_json.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(r for r in rows if r["status"] == "no-json")
        changed = [r for r in rows if r["status"] in ("updated", "would-update")
                   and ({r["date"], r["gps"], r["desc"]} & {"added", "replaced", "kept"} or r["people"] or r["favourite"])]
        with open(report_dir / f"takeout_{tag}_changes.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(changed)
        fx.close_all()
        pruned = fx.prune_empty_dirs(resolved) if (move and out_root and not dry_run) else 0
        sm = summarise(rows, sidecars, resolved, dry_run)
        sm["pruned"] = pruned
        if pruned:
            sm["tips"].append("%d folders left empty by the move were removed. Google's .json files are left where they were; remove them with the Clean up tab, then use Empty folders to tidy the rest." % pruned)
        sm["dupe_bytes"] = getattr(args, "dupe_bytes", 0)
        sm["samples"] = [{"file": Path(r["file"]).name, "date": [r["date"], r["date_before"], r["date_google"], r.get("date_note", "")],
                          "gps": [r["gps"], r["gps_before"], r["gps_google"]],
                          "desc": [r["desc"], r["desc_before"], r["desc_google"]]}
                         for r in changed if "replaced" in (r["date"], r["gps"], r["desc"])
                         or "added" in (r["date"], r["gps"], r["desc"])][:15]
        write_text_summary(report_dir / f"takeout_{tag}_summary.txt", sm)
        with LOCK:
            STATE.update(state="done", report=str(report), summary=sm, message="Finished")
    except Exception as e:  # surface any failure in the UI
        with LOCK:
            STATE.update(state="error", message=str(e))


def run_sort(roots, out, dry_run, dedupe, move, bring_json):
    with LOCK:
        STATE.update(state="scanning", total=0, done=0, counts={}, message="Scanning folders...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="sort")
    try:
        resolved, seen = [], set()
        for r in roots:
            p = Path(r).expanduser()
            if not p.is_dir():
                raise ValueError(f"Not a folder: {p}")
            if p.resolve() not in seen:
                seen.add(p.resolve())
                resolved.append(p)
        if not resolved:
            raise ValueError("Add at least one folder")
        in_place = False
        if not out:
            if not move:
                raise ValueError("Choose an output folder, or tick Move to merge everything into the first folder in the list")
            out, in_place = str(resolved[0]), True
        media, sidecars, mseen, sseen = [], [], set(), set()
        for p in resolved:
            m, sc = fx.scan(p)
            media += [x for x in m if x.resolve() not in mseen and not mseen.add(x.resolve())]
            sidecars += [x for x in sc if x.resolve() not in sseen and not sseen.add(x.resolve())]
        idx = fx.build_index(sidecars) if bring_json else None
        args = argparse.Namespace(dry_run=dry_run, dedupe=dedupe, move=move, bring_json=bring_json,
                                  pair_live=False, overwrite=False, out_root=out, roots=resolved)
        with LOCK:
            STATE.update(state="running", total=len(media), scan={"media": len(media), "json": len(sidecars), "folders": len(resolved)},
                         message=f"Checking {len(media):,} files for exact duplicates..." if dedupe else "Preparing...")

        def hashing(stage, done, todo):
            with LOCK:
                STATE["phase"] = {"stage": stage, "done": done, "total": todo}
                STATE["message"] = f"Step 1: comparing {todo:,} files that share a size ({done:,}/{todo:,})"
        fx.prepare(args, media, hashing)
        with LOCK:
            STATE["phase"] = None
        with LOCK:
            STATE["message"] = f"Step 2: {'copying' if not move else 'moving'} {len(media):,} files into place"
        out_root = Path(out)
        rows, counts, extra, out_dirs, src_dirs, recent = [], defaultdict(int), defaultdict(int), set(), set(), []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for row in ex.map(lambda m: fx.guarded(fx.sort_one)(m, idx, args, out_root), media):
                rows.append(row)
                counts[row["status"]] += 1
                if row["status"] in ("copy-error", "error", "exiftool-error"):
                    extra["errors"] += 1
                if row["status"] == "duplicate":
                    extra["duplicates"] += 1
                if row["output"]:
                    extra["written"] += 1
                    out_dirs.add(str(Path(row["output"]).parent))
                    src_dirs.add(str(Path(row["file"]).parent))
                    extra["folders"] = len(out_dirs)
                    extra["merged_from"] = len(src_dirs)
                if row["sidecar"]:
                    extra["json_along"] += 1
                recent.append(row)
                del recent[:-12]
                with LOCK:
                    STATE["done"] = len(rows)
                    STATE["counts"] = dict(counts)
                    STATE["extra"] = dict(extra)
                    STATE["recent"] = [{"name": Path(r["file"]).name, "status": r["status"],
                                        "to": (Path(r["output"]).parent.name + "/" if r["output"] else ""),
                                        "live": ""} for r in recent]
        pruned = fx.prune_empty_dirs(resolved) if (move and not dry_run) else 0
        report_dir = (Path.home() / "Desktop") if dry_run else out_root
        report_dir.mkdir(parents=True, exist_ok=True)
        tag = "sort_preview" if dry_run else "sort_report"
        report = report_dir / f"takeout_{tag}.csv"
        with open(report, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fx.REPORT_FIELDS)
            w.writeheader()
            w.writerows(rows)
        folders = defaultdict(int)
        for r in rows:
            if r["output"]:
                folders[str(Path(r["output"]).parent)] += 1
        base = os.path.commonpath(list(folders)) if folders else str(out_root)
        sm = {"kind": "sort", "dry_run": dry_run, "move": move, "total": len(rows),
              "duplicates": counts.get("duplicate", 0), "dupe_bytes": getattr(args, "dupe_bytes", 0),
              "placed": extra.get("written", 0), "folders_in": len(src_dirs), "folders_out": len(out_dirs),
              "json_along": extra.get("json_along", 0), "in_place": in_place, "pruned": pruned, "dest": str(out_root),
              "out_folders": sorted([os.path.relpath(k, base) if k != base else ".", v] for k, v in folders.items()),
              "tips": []}
        errs = counts.get("copy-error", 0) + counts.get("error", 0)
        if errs:
            sm["tips"].append("%d files could not be copied or moved and were skipped; see the 'detail' column in the report. "
                              "'Input/output error' (Errno 5) usually means a problem reading or writing the drive or that file. "
                              "Fix the drive problem and run the same sort again: finished files are skipped." % errs)
        if counts.get("already-done"):
            sm["tips"].append("%d files were already in place from an earlier run and were skipped." % counts["already-done"])
        if dedupe and not sm["duplicates"]:
            sm["tips"].append("No exact duplicates were found.")
        if sm["duplicates"]:
            sm["tips"].append("%d identical copies were skipped (%.1f GB), keeping the copy in the 'Photos from YYYY' "
                              "folder. Album copies of the same photo are not repeated." % (sm["duplicates"], sm["dupe_bytes"] / 1e9))
        if in_place:
            sm["tips"].append("Sorted in place: everything was merged into %s (the first folder in your list)." % out)
        if pruned:
            sm["tips"].append("%d folders left empty by the move were removed." % pruned)
        if move and not dry_run:
            sm["tips"].append("Files were moved; duplicate copies and any .json files stay behind in the source folders "
                              "(use the clean-up panel to remove .json files).")
        if dry_run:
            sm["tips"].append("This was a preview: nothing was copied or moved.")
        with LOCK:
            STATE.update(state="done", report=str(report), summary=sm, message="Finished")
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e))


def run_convert(roots, dry_run, exts, include_live, quality, action):
    with LOCK:
        STATE.update(state="scanning", total=0, done=0, counts={}, message="Looking for old videos...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="convert")
    try:
        if not fx.have_ffmpeg():
            raise ValueError("ffmpeg not found. In Terminal run: brew install ffmpeg")
        folders, seen = [], set()
        for r in roots:
            p = Path(r).expanduser()
            if not p.is_dir():
                raise ValueError(f"Not a folder: {p}")
            if p.resolve() not in seen:
                seen.add(p.resolve())
                folders.append(p)
        if not folders:
            raise ValueError("Add at least one folder")
        exts = {e.lower() for e in exts} & set(fx.LEGACY_EXT)
        if not exts:
            raise ValueError("Tick .avi and/or .mov")
        items = fx.scan_legacy(folders, exts)
        if not items:
            raise ValueError("No .avi or .mov files found in those folders")
        with LOCK:
            STATE.update(state="running", total=len(items), message=f"Reading {len(items):,} videos...",
                         scan={"media": len(items), "json": 0, "folders": len(folders)})
        durations = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for (p, _), info in zip(items, ex.map(lambda it: fx.probe_video(it[0]), items)):
                durations[str(p)] = (info or {}).get("duration", 0) or 0
        total_secs = sum(durations.values())
        opts = {"dry_run": dry_run, "action": action, "include_live": include_live,
                "crf": fx.QUALITY_CRF.get(quality, 20)}
        rows, counts, extra, recent = [], defaultdict(int), defaultdict(int), []
        secs_done = 0.0
        for i, (p, root) in enumerate(items, 1):
            dur = durations.get(str(p), 0)
            with LOCK:
                STATE["message"] = f"{'Checking' if dry_run else 'Converting'} {i:,} of {len(items):,}: {p.name}"
                if not dry_run:
                    STATE["phase"] = {"stage": "convert", "done": secs_done, "total": total_secs}

            def cb(secs, base=secs_done):
                with LOCK:
                    STATE["phase"] = {"stage": "convert", "done": base + max(0.0, secs), "total": total_secs}
            row = fx.convert_file(p, root, opts, cb)
            secs_done += dur
            rows.append(row)
            counts[row["status"]] += 1
            if row["status"] in ("converted", "already-converted"):
                extra["converted"] += 1
            if row["status"] in ("failed", "unreadable"):
                extra["errors"] += 1
            if row["status"] == "skipped-live":
                extra["skipped_live"] += 1
            recent.append(row)
            del recent[:-12]
            with LOCK:
                STATE["done"] = i
                STATE["counts"] = dict(counts)
                STATE["extra"] = dict(extra)
                STATE["recent"] = [{"name": Path(r["file"]).name, "status": r["status"],
                                    "to": (r["mode"] + " " if r["mode"] else ""), "live": ""} for r in recent]
        fx.close_all()
        report_dir = Path.home() / "Desktop"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / ("takeout_convert_preview.csv" if dry_run else "takeout_convert_report.csv")
        with open(report, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fx.CONVERT_FIELDS)
            w.writeheader()
            w.writerows(rows)
        done_rows = [r for r in rows if r["status"] in ("converted", "already-converted")]
        todo_rows = [r for r in rows if r["status"] in ("would-convert", "would-finish")]
        sm = {"kind": "convert", "dry_run": dry_run, "action": action, "total": len(rows),
              "converted": len(done_rows), "would": len(todo_rows),
              "remux": sum(1 for r in done_rows + todo_rows if r["mode"] == "remux"),
              "encode": sum(1 for r in done_rows + todo_rows if r["mode"] == "encode"),
              "skipped_live": counts.get("skipped-live", 0), "failed": counts.get("failed", 0) + counts.get("unreadable", 0),
              "bytes_before": sum(int(r["size_before"] or 0) for r in done_rows or todo_rows),
              "bytes_after": sum(int(r["size_after"] or 0) for r in done_rows),
              "failures": [{"file": Path(r["file"]).name, "status": r["status"], "detail": r["detail"]}
                           for r in rows if r["status"] in ("failed", "unreadable")][:30], "tips": []}
        if sm["skipped_live"]:
            sm["tips"].append("%d Live Photo videos were left as .MOV so Apple Photos keeps them paired with their stills. "
                              "Tick 'Also convert Live Photo videos' only if you do not need that." % sm["skipped_live"])
        if sm["failed"]:
            sm["tips"].append("%d videos could not be converted; they were left untouched. See the report." % sm["failed"])
        if dry_run:
            sm["tips"].append("This was a preview: nothing was converted, moved or deleted. Re-encoding can take a long "
                              "time; remuxed videos (already H.264/HEVC) are fast and lossless.")
        elif action == "move" and sm["converted"]:
            sm["tips"].append("Original videos were moved to a '_original_videos' folder inside each folder you chose. "
                              "Delete those folders once you have checked the new .mp4 files.")
        with LOCK:
            STATE.update(state="done", report=str(report), summary=sm, message="Finished", phase=None)
    except Exception as e:
        fx.close_all()
        with LOCK:
            STATE.update(state="error", message=str(e))


def run_empty(roots, dry_run, ignore_junk, remove_top):
    with LOCK:
        STATE.update(state="scanning", total=0, done=0, counts={}, message="Scanning folders...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="empty")
    try:
        folders = check_clean_folders(roots)
        found, junk, scanned, skipped = [], [], 0, 0
        for i, f in enumerate(folders, 1):
            with LOCK:
                STATE["message"] = f"Scanning {f.name} ({i} of {len(folders)})..."
            e, j, n, k = fx.find_empty_dirs(f, ignore_junk)
            top = str(f)
            found += [(p, str(f)) for p in e if remove_top or p != top]
            junk += j
            scanned += n
            skipped += k
        found.sort(key=lambda t: -len(Path(t[0]).parts))
        empties = {p for p, _ in found}
        with LOCK:
            STATE.update(state="running", total=len(found), done=0,
                         message=f"Found {len(found):,} empty folders" if dry_run else f"Removing {len(found):,} empty folders...",
                         scan={"media": scanned, "json": 0, "folders": len(folders)})
        removed, failed, recent = 0, [], []
        rows = []
        for i, (p, root) in enumerate(found, 1):
            if dry_run:
                ok, err = True, ""
            else:
                ok, err = fx.remove_empty_dir(p, ignore_junk)
            removed += 1 if ok else 0
            if not ok:
                failed.append({"path": p, "detail": err})
            rows.append({"path": p, "status": ("would-remove" if dry_run else "removed") if ok else "failed", "detail": err})
            recent.append({"name": os.path.relpath(p, root) if p != root else Path(p).name,
                           "status": rows[-1]["status"], "to": "", "live": ""})
            del recent[:-12]
            if i % 20 == 0 or i == len(found):
                with LOCK:
                    STATE["done"] = i
                    STATE["counts"] = {"removed" if not dry_run else "would-remove": removed, "failed": len(failed)}
                    STATE["extra"] = {"empty_found": len(found), "errors": len(failed)}
                    STATE["recent"] = list(recent)
        # topmost empty folders (parent is not itself empty) with how many folders each hides
        groups = []
        for p, root in sorted(found, key=lambda t: t[0]):
            if str(Path(p).parent) not in empties:
                nested = sum(1 for q, _ in found if q != p and q.startswith(p + os.sep))
                groups.append({"path": os.path.relpath(p, root) if p != root else ".", "root": Path(root).name, "nested": nested})
        report_dir = Path.home() / "Desktop"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / ("takeout_empty_preview.csv" if dry_run else "takeout_empty_report.csv")
        with open(report, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["path", "status", "detail"])
            w.writeheader()
            w.writerows(rows)
        sm = {"kind": "empty", "dry_run": dry_run, "scanned": scanned, "empty": len(found), "removed": removed,
              "kept": scanned - len(found), "junk": len(junk), "skipped": skipped, "failed": len(failed),
              "groups": groups[:150], "group_count": len(groups), "tips": []}
        if not found:
            sm["tips"].append("No empty folders were found.")
        if skipped:
            sm["tips"].append("%d shortcuts, app/library bundles or unreadable folders were left alone and never entered." % skipped)
        if failed:
            sm["tips"].append("%d folders could not be removed (see the report)." % len(failed))
        if dry_run:
            sm["tips"].append("This was a preview: nothing was removed. Untick Preview only to remove them.")
        with LOCK:
            STATE.update(state="done", report=str(report), summary=sm, message="Finished", phase=None)
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e))


AUX_JSON = {"metadata.json", "print-subscriptions.json", "shared_album_comments.json",
            "user-generated-memory-titles.json"}


def classify_json(p):
    if p.name.lower() in AUX_JSON:
        return "album"
    return "photo" if is_media_sidecar(p) else "other"


def check_clean_folders(folders):
    out = []
    for f in folders:
        p = Path(f).expanduser()
        if not p.is_dir():
            raise ValueError(f"Not a folder: {p}")
        if p.resolve() in (Path("/"), Path.home().resolve()) or len(p.resolve().parts) <= 2:
            raise ValueError(f"Too broad to clean safely: {p}. Choose the specific folder.")
        out.append(p)
    if not out:
        raise ValueError("Add at least one folder")
    return out


def find_json(folders, include_other):
    cats = defaultdict(lambda: [0, 0])
    files = []
    seen = set()
    for f in folders:
        for dirpath, _, names in os.walk(f):
            for n in names:
                if not n.lower().endswith(".json"):
                    continue
                p = Path(dirpath) / n
                if p.is_symlink() or str(p) in seen:
                    continue
                seen.add(str(p))
                c = classify_json(p)
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                cats[c][0] += 1
                cats[c][1] += size
                if c != "other" or include_other:
                    files.append((p, size))
    return {k: {"files": v[0], "bytes": v[1]} for k, v in cats.items()}, files


def run_clean(folders, include_other):
    with LOCK:
        STATE["clean"] = {"state": "running", "total": 0, "done": 0, "deleted": 0, "errors": 0, "bytes": 0, "message": "Scanning..."}
    try:
        _, files = find_json(check_clean_folders(folders), include_other)
        with LOCK:
            STATE["clean"]["total"] = len(files)
        for p, size in files:
            try:
                os.remove(p)
                with LOCK:
                    STATE["clean"]["deleted"] += 1
                    STATE["clean"]["bytes"] += size
            except OSError:
                with LOCK:
                    STATE["clean"]["errors"] += 1
            with LOCK:
                STATE["clean"]["done"] += 1
        with LOCK:
            STATE["clean"]["state"] = "done"
            STATE["clean"]["message"] = "Finished"
    except Exception as e:
        with LOCK:
            STATE["clean"].update(state="error", message=str(e))


UPDATE_BASE = os.environ.get("METADATAFIXER_UPDATE_BASE", "https://raw.githubusercontent.com/daviddef/MetadataFixer/main/")
UPDATE_FILES = ["takeout_gui.py", "takeout_fix_metadata.py"]
HERE = Path(__file__).resolve().parent


def _fetch(name):
    req = urllib.request.Request(UPDATE_BASE + name, headers={"User-Agent": "MetadataFixer-updater"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def check_update():
    """Compare the local files with the latest published ones. Never raises."""
    with LOCK:
        STATE["update"] = {"state": "checking", "files": []}
    try:
        changed = []
        for name in UPDATE_FILES:
            remote = _fetch(name)
            local = (HERE / name).read_bytes() if (HERE / name).exists() else b""
            if remote and hashlib.sha256(remote).digest() != hashlib.sha256(local).digest():
                changed.append(name)
        with LOCK:
            STATE["update"] = {"state": "available" if changed else "current", "files": changed}
    except Exception as e:  # offline, repo moved, etc.: just say we could not check
        with LOCK:
            STATE["update"] = {"state": "unknown", "files": [], "message": str(e)[:120]}


def apply_update():
    """Download the latest files, check they are valid Python, replace the old ones (keeping .bak) and restart."""
    with LOCK:
        if STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running":
            return {"error": "A job is running. Wait for it to finish, then update."}
    try:
        fresh = {}
        for name in UPDATE_FILES:
            data = _fetch(name)
            compile(data, name, "exec")           # refuse anything that is not valid Python
            if b"def main" not in data:
                raise ValueError(f"{name} does not look like the right file")
            fresh[name] = data
        for name, data in fresh.items():
            path = HERE / name
            if path.exists():
                (HERE / (name + ".bak")).write_bytes(path.read_bytes())
            tmp = HERE / (name + ".new")
            tmp.write_bytes(data)
            os.replace(tmp, path)
    except Exception as e:
        return {"error": f"Update failed, nothing was changed: {e}"}
    with LOCK:
        STATE["update"] = {"state": "restarting", "files": []}

    def restart():
        time.sleep(1.2)
        argv = [sys.executable] + sys.argv + ([] if "--no-browser" in sys.argv else ["--no-browser"])
        os.execv(sys.executable, argv)
    threading.Thread(target=restart, daemon=True).start()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            with LOCK:
                self._send(200, json.dumps(STATE))
        else:
            self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        # only accept requests from our own page
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, "{}")
        if self.path == "/api/choose":
            self._send(200, json.dumps({"paths": choose_folders(body.get("prompt", "Choose folders"))}))
        elif self.path == "/api/start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running")
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            threading.Thread(target=run_job, daemon=True, args=(
                body.get("roots", []), body.get("out", ""),
                bool(body.get("dry_run")), bool(body.get("overwrite")), bool(body.get("pair_live")),
                bool(body.get("dedupe")), bool(body.get("move")), body.get("date_policy", "earlier"))).start()
            self._send(200, "{}")
        elif self.path == "/api/update_check":
            threading.Thread(target=check_update, daemon=True).start()
            self._send(200, "{}")
        elif self.path == "/api/update":
            self._send(200, json.dumps(apply_update()))
        elif self.path == "/api/empty_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            threading.Thread(target=run_empty, daemon=True, args=(
                body.get("roots", []), bool(body.get("dry_run")), bool(body.get("ignore_junk")),
                bool(body.get("remove_top")))).start()
            self._send(200, "{}")
        elif self.path == "/api/convert_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            threading.Thread(target=run_convert, daemon=True, args=(
                body.get("roots", []), bool(body.get("dry_run")), body.get("exts", []),
                bool(body.get("include_live")), body.get("quality", "high"), body.get("action", "move"))).start()
            self._send(200, "{}")
        elif self.path == "/api/sort_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            threading.Thread(target=run_sort, daemon=True, args=(
                body.get("roots", []), body.get("out", ""), bool(body.get("dry_run")), bool(body.get("dedupe")),
                bool(body.get("move")), bool(body.get("bring_json")))).start()
            self._send(200, "{}")
        elif self.path == "/api/clean_scan":
            try:
                cats, files = find_json(check_clean_folders(body.get("folders", [])), bool(body.get("include_other")))
                self._send(200, json.dumps({"cats": cats, "will_delete": len(files),
                                            "bytes": sum(s for _, s in files)}))
            except ValueError as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif self.path == "/api/clean_run":
            with LOCK:
                busy = STATE["clean"].get("state") == "running" or STATE["state"] in ("scanning", "running")
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            threading.Thread(target=run_clean, daemon=True,
                             args=(body.get("folders", []), bool(body.get("include_other")))).start()
            self._send(200, "{}")
        elif self.path == "/api/reveal":
            with LOCK:
                rep = STATE["report"]
            if rep:
                subprocess.run(["open", "-R", rep])
            self._send(200, "{}")
        else:
            self._send(404, "{}")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Takeout Metadata Fixer</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#dcdcd6;--acc:#2563eb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1e;--ink:#eeeeea;--mute:#9a9a94;--line:#34342f;--acc:#60a5fa;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif;padding:24px 16px}
main{max-width:760px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:18px 0 6px}p.sub{color:var(--mute);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
label.t{font-weight:600;display:block;margin-bottom:6px}small{color:var(--mute);display:block;margin-top:6px}
.row{display:flex;gap:8px}textarea,input[type=text]{flex:1;min-width:0;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:13px ui-monospace,Menlo,monospace}
textarea{min-height:92px;resize:vertical}textarea.over{border-color:var(--acc);outline:2px solid var(--acc)}
button{padding:9px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit;cursor:pointer;white-space:nowrap}
button.p{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}button:disabled{opacity:.5;cursor:default}
.opt{display:flex;gap:8px;align-items:flex-start;margin:8px 0}
.bar{background:var(--line);border-radius:12px;margin:10px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:10px 0}
.tile{border:1px solid var(--line);border-radius:10px;padding:10px 12px}.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--mute);font-size:13px}
.tile.bad b{color:var(--bad)}.tile.ok b{color:var(--ok)}.err{color:var(--bad)}.ok{color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}th{color:var(--mute);font-weight:500}td.n,th.n{text-align:right}
.tip{border-left:3px solid var(--warn);padding:6px 10px;margin:8px 0;background:var(--bg)}
.mini{display:inline-block;height:8px;background:var(--bad);border-radius:3px;vertical-align:middle;margin-left:6px}
.hero{background:linear-gradient(135deg,color-mix(in srgb,var(--acc) 14%,var(--card)),var(--card) 70%);border:1px solid var(--line);border-radius:16px;padding:20px 20px 16px;margin:0 0 22px}
.brand{display:flex;align-items:center;gap:14px}.logo{width:56px;height:56px;flex:none;filter:drop-shadow(0 2px 6px rgba(79,140,255,.35))}
.hero h1{font-size:25px;letter-spacing:-.02em;margin:0;line-height:1.15}.hero .tag{margin:4px 0 0;color:var(--mute);font-size:15px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);font-size:13px;text-decoration:none}
a.chip:hover{border-color:var(--acc)}.chip b{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;background:var(--acc);color:#fff;font-size:12px}
.chip.priv{color:var(--mute);border-style:dashed}html{scroll-behavior:smooth}
@media(max-width:560px){.brand{align-items:flex-start}.hero h1{font-size:21px}}
.bar{height:24px;position:relative;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),#7c5cff);transition:width .25s ease}
.bar>span{position:absolute;top:0;bottom:0;display:flex;align-items:center;font-size:12px;font-weight:700;font-variant-numeric:tabular-nums;color:#fff;white-space:nowrap;transition:left .25s ease;pointer-events:none}
.bar>span.out{color:var(--ink)}
.bar.indet>i{width:40%!important;animation:slide 1.2s ease-in-out infinite alternate}
@keyframes slide{from{margin-left:0}to{margin-left:60%}}
textarea{width:100%}
#frame{position:sticky;top:0;z-index:30;margin:0 -16px 16px;padding:10px 16px 8px;background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.tabs{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px;scrollbar-width:none}.tabs::-webkit-scrollbar{display:none}
.tab{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);font-size:14px;white-space:nowrap}
.tab b{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;background:var(--line);color:var(--ink);font-size:12px}
.tab.on{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}.tab.on b{background:#fff;color:var(--acc)}
.status{margin-top:8px}.srow{display:flex;justify-content:space-between;gap:10px;font-size:13px;color:var(--mute);min-height:20px}.srow #msg{min-width:0;overflow:hidden;text-overflow:ellipsis}.srow a{white-space:nowrap;color:var(--acc)}
.status .bar{margin:6px 0 0}
.pane{display:none}.ph{font-size:18px;margin:4px 0 12px}
.sel{margin-left:8px;padding:6px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);max-width:100%}
code{background:var(--bg);padding:1px 5px;border-radius:5px;font-size:12px}
.fbar{margin-top:8px}.fhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px}.flabel{color:var(--mute)}.flabel b{color:var(--ink)}
button.sm{padding:4px 10px;font-size:13px;border-radius:8px}
.fchips{display:flex;gap:6px;overflow-x:auto;margin-top:6px;scrollbar-width:none}.fchips:empty{display:none}.fchips::-webkit-scrollbar{display:none}
.fchip{display:inline-flex;align-items:center;padding:2px 2px 2px 10px;border:1px solid var(--line);border-radius:999px;background:var(--card);font-size:12px;white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis;flex:none}
.fx{border:0;background:transparent;padding:0 7px;font-size:15px;line-height:1;cursor:pointer;color:var(--mute)}
.fbar.over{outline:2px dashed var(--acc);border-radius:10px}
.usef{padding:10px 12px;border:1px dashed var(--line);border-radius:10px;color:var(--mute);font-size:13px;margin-bottom:12px;background:var(--bg)}.usef b{color:var(--ink)}
#fall{min-height:80px}
.sm2{display:none}@media(max-width:560px){.lg{display:none}.sm2{display:inline}.fhead{gap:6px}button.sm{padding:3px 8px;font-size:12px}#frame{padding-top:8px}.hero{padding:14px 14px 12px}.hero .logo{width:44px;height:44px}}
</style></head><body><main>
<div id="upd" style="display:none" class="card"><b>A newer version is available.</b> <span id="updmsg"></span>
<div style="margin-top:8px"><button class="p" id="updgo">Update now</button> <button id="updno">Not now</button></div></div>
<header class="hero">
  <div class="brand">
    <svg class="logo" viewBox="0 0 64 64" role="img" aria-label="Metadata Fixer logo">
      <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4f8cff"/><stop offset="1" stop-color="#7c5cff"/></linearGradient></defs>
      <rect width="64" height="64" rx="15" fill="url(#lg)"/>
      <rect x="11" y="14" width="38" height="30" rx="5" fill="#fff" opacity=".95"/>
      <circle cx="22" cy="24" r="4" fill="#ffb84d"/>
      <path d="M13 41l11-11 8 8 6-6 9 9v3a2 2 0 0 1-2 2H15a2 2 0 0 1-2-2z" fill="#4f8cff" opacity=".9"/>
      <circle cx="47" cy="45" r="11" fill="#22c55e" stroke="#fff" stroke-width="3"/>
      <path d="M42 45.5l3.6 3.6L52 42.5" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <div>
      <h1>Takeout Metadata Fixer</h1>
      <p class="tag">Put the right date, place and caption back on your Google Photos export. <span id="ver" style="opacity:.6;white-space:nowrap"></span></p>
    </div>
  </div>
  <div class="chips"><span class="chip priv">&#128274; Runs only on your computer: nothing is uploaded</span></div>
</header>

<div id="frame">
  <nav class="tabs" role="tablist">
    <button class="tab" data-tab="fix" role="tab"><b>1</b> Fix metadata</button>
    <button class="tab" data-tab="sort" role="tab"><b>2</b> Sort</button>
    <button class="tab" data-tab="clean" role="tab"><b>3</b> Clean up</button>
    <button class="tab" data-tab="convert" role="tab"><b>4</b> Convert videos</button>
    <button class="tab" data-tab="empty" role="tab"><b>5</b> Empty folders</button>
  </nav>
  <div class="fbar" id="fbar">
    <div class="fhead"><span class="flabel">&#128193; <span class="lg">Folders for every tab: </span><span class="sm2">Folders: </span><b id="fsum"></b></span><button id="fadd" class="sm">Add folders...</button><button id="fedit" class="sm">Edit list</button></div>
    <div class="fchips" id="fchips"></div>
    <div id="fpanel" style="display:none"><textarea id="fall" placeholder="One folder path per line (drag folders here too)" spellcheck="false"></textarea><div class="row" style="margin-top:6px"><button id="fdone" class="p sm">Done</button><button id="fclear" class="sm">Clear all</button></div></div>
  </div>
  <div class="status"><div class="srow"><span id="msg">Ready. Choose a tab, set it up and press Start.</span><a href="#" id="goto" style="display:none">View results &rarr;</a></div>
  <div class="bar" id="bar"><i id="fill"></i><span id="pct">0%</span></div></div>
</div>
<section class="pane" id="pane-fix">
<h2 class="ph">Fix dates, locations and captions</h2>
<div class="card usefcard"><b>1. Takeout folders</b><div class="usef" style="margin:8px 0 0"><span class="fnote"></span>. Add every Takeout batch: a photo in one batch finds its JSON in another.</div></div>

<div class="card"><label class="t">2. Where to put the fixed copies</label>
<div class="row"><input type="text" id="out" placeholder="Leave empty to fix files in place"><button id="b2">Choose folder</button></div>
<small>Recommended: a new folder, so originals stay untouched. Reports are saved here too (or on your Desktop if empty).</small></div>

<div class="card"><label class="t">3. Options</label>
<div class="opt"><input type="checkbox" id="dry" checked><div>Preview only<small>On by default. Works out what it would do and reports the numbers, but changes nothing. Untick to do it for real.</small></div></div>
<div class="opt"><input type="checkbox" id="dedupe" checked><div>Remove exact duplicates<small>Skips byte-identical copies (the same photo repeated across Takeouts or albums). Keeps the copy in 'Photos from YYYY'. Needs an extra read pass over files that share a size.</small></div></div>
<div class="opt"><input type="checkbox" id="move"><div>Move files instead of copying<small>Saves disk space but empties your Takeout folders as it goes. Off = safe copy (needs roughly as much free space again).</small></div></div>
<div class="opt"><input type="checkbox" id="live" checked><div>Re-pair Live Photos<small>Copies each still's Apple ID onto its video and saves the video as .MOV so Photos can treat them as one Live Photo. Needs an output folder.</small></div></div>
<div class="opt"><div style="flex:1"><label for="datepol" style="font-weight:600">When a photo already has a date and Google&#39;s is different</label>
<select id="datepol" class="sel"><option value="earlier" selected>Keep the earlier date (recommended)</option><option value="photo">Keep the photo&#39;s own date</option><option value="google">Use Google&#39;s date</option></select>
<small>Google sometimes records the day a photo was uploaded or re-saved instead of the day it was taken, and that day is always later. Keeping the earlier of the two is usually right. A photo with no date at all always gets Google&#39;s.</small></div></div>
<div class="opt"><input type="checkbox" id="ow" checked><div>Replace location and caption already stored in the photo<small>Every photo has hidden facts saved inside the file itself (called EXIF). <b>Off</b>: only fill in a location or caption that is missing. <b>On</b>: replace a different one with Google&#39;s version. Your pictures themselves are never altered.</small></div></div></div>

<button class="p" id="go">Start</button>

<div id="results">
<div class="card" id="prog" style="display:none;margin-top:14px">
<div class="tiles" id="tiles"></div>
<div id="recent" style="font:12px ui-monospace,Menlo,monospace;color:var(--mute);line-height:1.6;overflow:hidden"></div></div>

<div class="card" id="sum" style="display:none"><h2 style="margin-top:0">Summary</h2><div id="sumbody"></div>
<div style="margin-top:12px"><button id="rev">Show reports in Finder</button></div></div>


</div>
</section>
<section class="pane" id="pane-sort">
<h2 class="ph">Sort only: merge folders, remove duplicates</h2>
<div class="card"><small style="margin-top:0">Tidies the folder structure and nothing else: no dates, locations or captions are touched. All the same-named folders (every <i>Photos from 2012</i>) are merged into one, and identical duplicate photos are skipped. Use it on its own, or before Part 1.</small>
<div class="usef" style="margin-top:12px"><b>Folders to sort:</b> <span class="fnote"></span> (the first one in the list is where everything merges when you sort in place)</div>
<label class="t" style="margin-top:12px">Output folder <span style="font-weight:400;color:var(--mute)">(optional when moving)</span></label>
<div class="row"><input type="text" id="sout" placeholder="Where the sorted library goes"><button id="sb2">Choose folder</button></div>
<small>When you tick <b>Move</b> you can leave this empty: everything is then merged into the <b>first folder in the list above</b>, so you can sort in place. Folders with the same path (for example <i>2014/08</i> in two different folders, or every <i>Photos from 2012</i>) are merged into one; <i>Takeout N / Google Photos</i> wrappers are ignored.</small>
<div class="opt"><input type="checkbox" id="sdry" checked><div>Preview only<small>On by default. Reports what would happen; copies and moves nothing.</small></div></div>
<div class="opt"><input type="checkbox" id="sdedupe" checked><div>Skip exact duplicate photos<small>Compares file contents, so the same photo repeated in several folders, Takeouts or albums is kept once. Different photos that share a name are both kept (the second becomes <i>name_1</i>).</small></div></div>
<div class="opt"><input type="checkbox" id="sjson" checked><div>Bring the .json info files along<small>Puts each photo&#39;s .json file next to it, so you can still run Part 1 afterwards. Untick if you only want photos.</small></div></div>
<div class="opt"><input type="checkbox" id="smove"><div>Move instead of copy<small>Saves disk space but empties the source folders as it goes. Off = copy (needs about as much free space again).</small></div></div>
<button class="p" id="sgo" style="margin-top:6px">Start sorting</button></div>

</section>
<section class="pane" id="pane-clean">
<h2 class="ph">Clean up</h2>
<div class="card"><label class="t">Remove the leftover .json files (do this last)</label>
<small style="margin-top:0">Once you're happy with the fixed photos, delete the leftover Google .json files. They are no longer needed, but they are the only source of the date and location data, so run the fix first. Deleted files do not go to the Trash.</small>
<div class="usef" style="margin-top:8px"><b>Folders to clean:</b> <span class="fnote"></span></div>
<div class="row" style="margin-top:8px"><button id="cscan">Scan</button></div>
<div class="opt"><input type="checkbox" id="cother"><div>Also remove other .json files<small>Off = only Google Photos sidecars and album/memory data files. On = every .json in the folders.</small></div></div>
<div class="bar" id="cbar" style="display:none"><i id="cfill"></i><span id="cpct">0%</span></div>
<div id="cres" style="margin-top:8px"></div>
<button id="cdel" disabled style="margin-top:8px;border-color:var(--bad);color:var(--bad)">Delete .json files</button></div>
</section>
<section class="pane" id="pane-empty">
<h2 class="ph">Remove empty folders</h2>
<div class="card"><small style="margin-top:0">Checks the whole folder tree and removes every folder that is <b>properly empty</b>, however deep: a folder only counts if it holds no files at all, and every folder inside it is empty too. A folder with even one file in it, or a subfolder with one file, is kept. Only folders are removed; <b>no file is ever deleted</b>, apart from invisible system leftovers (below) if you leave that option on.</small>
<div class="usef" style="margin-top:12px"><b>Folders to check:</b> <span class="fnote"></span></div>
<div class="opt"><input type="checkbox" id="edry" checked><div>Preview only<small>On by default. Lists the empty folders it would remove and removes nothing.</small></div></div>
<div class="opt"><input type="checkbox" id="ejunk" checked><div>Treat system leftovers as empty<small>Finder and Windows leave invisible files such as <i>.DS_Store</i>, <i>Thumbs.db</i>, <i>desktop.ini</i> and <i>._something</i>. A folder holding only those still counts as empty, and those files are deleted with it. Off = such a folder is kept.</small></div></div>
<div class="opt"><input type="checkbox" id="etop"><div>Also remove the folders you chose, if they end up empty<small>Off by default: the folders you add are always kept, even if everything inside them is removed.</small></div></div>
<small>Never entered or removed: shortcuts/aliases (symbolic links), app and library bundles (such as <i>.photoslibrary</i> and <i>.app</i>), and folders it is not allowed to read.</small>
<button class="p" id="ego" style="margin-top:10px">Start</button></div>
</section>
<section class="pane" id="pane-convert">
<h2 class="ph">Convert old videos to MP4</h2>
<div class="card"><small style="margin-top:0">Turns <b>.avi</b> and <b>.mov</b> videos into <b>.mp4</b>, which plays on every phone, TV and app. Videos that are already H.264 or HEVC are simply re-wrapped (fast, no quality loss); others are re-encoded. Dates and locations are carried across. Needs <b>ffmpeg</b> (in Terminal: <code>brew install ffmpeg</code>).</small>
<div class="usef" style="margin-top:12px"><b>Folders to scan:</b> <span class="fnote"></span></div>
<div class="opt"><input type="checkbox" id="vdry" checked><div>Preview only<small>On by default. Counts what would be converted (and how), changes nothing.</small></div></div>
<div class="opt"><div><b>Convert these types</b><br><label><input type="checkbox" id="vavi" checked> .avi</label> &nbsp; <label><input type="checkbox" id="vmov" checked> .mov</label></div></div>
<div class="opt"><input type="checkbox" id="vlive"><div>Also convert Live Photo videos<small>Off by default. An iPhone Live Photo is a still picture plus a short .MOV video that Apple Photos links together. If a .MOV sits next to a photo with the same name, it is treated as a Live Photo video and left alone, because converting it to .mp4 would break the link. Your ordinary .mov and .avi videos are converted as normal.</small></div></div>
<div class="opt"><div style="flex:1"><label for="vq" style="font-weight:600">Quality when re-encoding</label>
<select id="vq" class="sel"><option value="veryhigh">Very high (largest files)</option><option value="high" selected>High (recommended)</option><option value="small">Smaller files</option></select></div></div>
<div class="opt"><div style="flex:1"><label for="vact" style="font-weight:600">What happens to the original video</label>
<select id="vact" class="sel"><option value="move" selected>Move to an _original_videos folder (safe)</option><option value="keep">Keep it where it is, next to the new .mp4</option><option value="delete">Delete it once the new .mp4 is verified (permanent)</option></select>
<small>Originals are only moved or deleted after the new file has been checked (it must play and match the original&#39;s length).</small></div></div>
<button class="p" id="vgo" style="margin-top:6px">Start converting</button></div>
</section>
<script>
const $=id=>document.getElementById(id), esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function post(u,b){const r=await fetch(u,{method:'POST',body:JSON.stringify(b||{})});return r.json()}

// ---- shared folder list (header bar, applies to every tab)
let FOLDERS=[];try{FOLDERS=JSON.parse(localStorage.getItem('folders')||'[]').filter(x=>typeof x==='string')}catch(e){}
const roots=()=>FOLDERS.slice(),sroots=roots,croots=roots,vroots=roots,eroots=roots;
function renderFolders(){
  const box=$('fchips');box.innerHTML='';
  FOLDERS.forEach((p,i)=>{const c=document.createElement('span');c.className='fchip';c.title=p;
    const nm=p.split('/').filter(Boolean).pop()||p;c.appendChild(document.createTextNode(nm));
    const x=document.createElement('button');x.textContent='\u00d7';x.className='fx';x.setAttribute('aria-label','Remove '+nm);x.onclick=()=>{FOLDERS.splice(i,1);saveFolders()};c.appendChild(x);box.appendChild(c)});
  $('fsum').textContent=FOLDERS.length?FOLDERS.length+(FOLDERS.length===1?' folder':' folders'):'none chosen yet';
  const note=FOLDERS.length?'using the '+(FOLDERS.length===1?'folder':FOLDERS.length+' folders')+' chosen at the top':'none chosen yet. Add folders in the bar at the top';
  document.querySelectorAll('.fnote').forEach(e=>{e.textContent=note});
  $('fall').value=FOLDERS.join('\n')}
function saveFolders(){try{localStorage.setItem('folders',JSON.stringify(FOLDERS))}catch(e){};renderFolders()}
function addFolders(list){list.forEach(p=>{p=(p||'').trim().replace(/\/+$/,'');if(p&&!FOLDERS.includes(p))FOLDERS.push(p)});saveFolders()}
$('fadd').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose one or more folders (hold Cmd to select several)'});if(r.paths)addFolders(r.paths)};
$('fedit').onclick=()=>{const p=$('fpanel');p.style.display=p.style.display==='none'?'block':'none'};
$('fdone').onclick=()=>{FOLDERS=[];addFolders($('fall').value.split('\n'));$('fpanel').style.display='none'};
$('fclear').onclick=()=>{FOLDERS=[];saveFolders()};
const fbar=$('fbar');
fbar.ondragover=e=>{e.preventDefault();fbar.classList.add('over')};fbar.ondragleave=()=>fbar.classList.remove('over');
fbar.ondrop=e=>{e.preventDefault();fbar.classList.remove('over');
  const t=(e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain')||'').split(/\r?\n/).filter(Boolean);
  const paths=t.filter(x=>x.startsWith('file://')||x.startsWith('/')).map(x=>x.startsWith('file://')?decodeURIComponent(x.replace(/^file:\/\/[^\/]*/,'')):x);
  if(paths.length)addFolders(paths);else alert('Your browser did not share the folder path. Use "Add folders..." or paste the paths into Edit list.')};
renderFolders();
$('b2').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose where to save the fixed files'});if(r.paths&&r.paths[0])$('out').value=r.paths[0]};
$('go').onclick=async()=>{
  if(!roots().length){alert('Add your folders in the bar at the top first');return}
  if(!$('dry').checked&&!$('out').value.trim()&&!confirm('No output folder: files will be edited IN PLACE. Continue?'))return;
  if($('move').checked&&!$('dry').checked&&!confirm('MOVE will take files out of your Takeout folders. Make sure you have another backup. Continue?'))return;
  $('sum').style.display='none';
  const r=await post('/api/start',{roots:roots(),out:$('out').value.trim(),dry_run:$('dry').checked,overwrite:$('ow').checked,pair_live:$('live').checked,dedupe:$('dedupe').checked,move:$('move').checked,date_policy:$('datepol').value});
  if(r.error)alert(r.error);else poll();
};
$('rev').onclick=()=>post('/api/reveal');
const tile=(n,l,c)=>`<div class="tile ${c||''}"><b>${n.toLocaleString()}</b><span>${l}</span></div>`;
function tbl(head,rows){return `<table><tr>${head.map((h,i)=>`<th class="${i?'n':''}">${h}</th>`).join('')}</tr>${rows.map(r=>`<tr>${r.map((c,i)=>`<td class="${i?'n':''}">${c}</td>`).join('')}</tr>`).join('')}</table>`}
function bars(rows){const m=Math.max(1,...rows.map(r=>r[2]));return rows.map(r=>[esc(r[0]||'(none)'),r[1].toLocaleString(),r[2].toLocaleString()+`<span class="mini" style="width:${Math.round(60*r[2]/m)}px"></span>`])}
function showSummary(s){
  if(s.kind==='empty'){showEmpty(s);return}
  if(s.kind==='convert'){showConvert(s);return}
  if(s.kind==='sort'){showSort(s);return}
  let h=`<div class="tiles">${tile(s.total,'media files')}${tile(s.duplicates,'exact duplicates skipped')}${tile(s.matched,'unique files matched ('+s.pct_matched+'%)','ok')}${tile(s.no_json,'no JSON found',s.no_json?'bad':'ok')}${tile(s.orphans,'JSON with no photo')}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  h+=`<div class="tiles">${tile(s.replaced_files,'files with a value replaced')}${tile((s.live||{}).paired||0,'Live Photos paired')}</div>`;
  const c=s.changes||{},v=s.dry_run?'would change':'changed';
  h+=`<h2>What ${s.dry_run?'would be':'was'} changed</h2><div class="tiles">${tile(c.dates,'dates '+v)}${tile(c.gps,'locations '+v)}${tile(c.desc,'captions '+v)}${tile(c.people,'files with people tagged')}${tile(c.favourites,'favourites marked')}</div>`;
  h+=`<div class="tip" style="border-color:var(--acc)">${s.dry_run?'Would change':'Changed'} <b>${c.dates.toLocaleString()}</b> dates (${c.dates_added.toLocaleString()} added, ${c.dates_replaced.toLocaleString()} replaced), <b>${c.gps.toLocaleString()}</b> locations (${c.gps_added.toLocaleString()} added, ${c.gps_replaced.toLocaleString()} replaced) and <b>${c.desc.toLocaleString()}</b> captions.</div>`;
  h+='<h2>Information stored in the photos</h2>'+tbl(['Field','Added','Replaced','Left alone (different)','Already correct'],[['date','Date taken'],['gps','Location'],['desc','Description']].map(([k,l])=>{const f=s.fields[k]||{};return [l,(f.added||0).toLocaleString(),(f.replaced||0).toLocaleString(),(f.kept||0).toLocaleString(),(f.same||0).toLocaleString()]}));
  h+='<h2>Result</h2>'+tbl(['Status','Files'],Object.entries(s.status).map(([k,v])=>[k,v.toLocaleString()]));
  h+='<h2>How files were matched</h2>'+tbl(['Match type','Files'],Object.entries(s.match).map(([k,v])=>[({folder:'Same album folder',tree:'Another folder / batch','tree-ambiguous':'Another folder, several candidates (closest date chosen)',stem:'Same name, other extension (RAW+JPG, live photo)'})[k]||k,v.toLocaleString()]));
  h+='<h2>By file type</h2>'+tbl(['Type','With JSON','No JSON'],bars(s.ext));
  h+='<h2>By Takeout batch</h2>'+tbl(['Batch','With JSON','No JSON'],bars(s.batch));
  if(s.live&&Object.keys(s.live).length)h+='<h2>Live Photo pairing</h2>'+tbl(['Result','Videos'],Object.entries(s.live).map(([k,v])=>[({paired:'Paired with its still','no-id':'Still has no Apple ID','no-still':'No matching still','pair-error':'Error'})[k]||k,v.toLocaleString()]));
  if((s.out_folders||[]).length)h+='<h2>Output folders</h2>'+tbl(['Folder','Files'],s.out_folders.slice(0,80).map(r=>[esc(r[0]),r[1].toLocaleString()]))+(s.out_folders.length>80?'<small>Showing 80 of '+s.out_folders.length+'; see the summary .txt for all.</small>':'');
  if(s.album_no_json.length)h+='<h2>Albums with the most no-JSON files</h2>'+tbl(['Album','No JSON'],s.album_no_json.map(r=>[esc(r[0]),r[1].toLocaleString()]));
  const fmt=([o,b,g,n])=>o==='same'?'<span style="color:var(--mute)">already correct</span>':o==='none'||!o?'-':o==='kept'?`<b>${esc(b||'(none)')}</b> <span class="ok">&#10003; kept</span><br><small style="display:inline">Google&#39;s date, not used: <s>${esc(g)}</s></small>${n?' <span title="'+esc(n)+'">&#9888;</span>':''}`:`${esc(b||'(none)')} &rarr; <b>${esc(g)}</b> <small style="display:inline">(${o})</small>${n?' <span title="'+esc(n)+'">&#9888;</span>':''}`;
  if((s.samples||[]).length)h+='<h2>Sample of changes (first 15)</h2>'+tbl(['File','Date taken','Location','Description'],s.samples.map(x=>[esc(x.file),fmt(x.date),fmt(x.gps),fmt(x.desc)]));
  h+='<small>Saved: full report CSV (with before/after values per file), a changes-only CSV, a CSV of just the no-JSON files, and a text summary.</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}
const fmtBytes=b=>b>1e9?(b/1e9).toFixed(2)+' GB':b>1e6?(b/1e6).toFixed(1)+' MB':Math.round(b/1e3)+' KB';
$('cscan').onclick=async()=>{
  $('cdel').disabled=true;$('cres').textContent='Scanning...';
  const r=await post('/api/clean_scan',{folders:croots(),include_other:$('cother').checked});
  if(r.error){$('cres').innerHTML='<span class="err">'+esc(r.error)+'</span>';return}
  const L={photo:'Google info files for photos',album:'Album / memory data files',other:'Other .json files'};
  $('cres').innerHTML=tbl(['Kind','Files','Size'],Object.entries(r.cats).map(([k,v])=>[L[k]||k,v.files.toLocaleString(),fmtBytes(v.bytes)]))+`<div style="margin-top:6px"><b>${r.will_delete.toLocaleString()}</b> files (${fmtBytes(r.bytes)}) would be deleted.</div>`;
  $('cdel').disabled=!r.will_delete;$('cdel').dataset.n=r.will_delete};
$('cdel').onclick=async()=>{
  const n=$('cdel').dataset.n;
  const t=prompt(`This permanently deletes ${n} .json files in:\n${croots().join('\n')}\nIt cannot be undone.\nType DELETE to confirm.`);
  if(t!=='DELETE')return;
  const r=await post('/api/clean_run',{folders:croots(),include_other:$('cother').checked});
  if(r.error){alert(r.error);return}
  $('cdel').disabled=true;
  const tm=setInterval(async()=>{const s=(await (await fetch('/api/status')).json()).clean;
    jobKind='clean';$('msg').innerHTML='<b>Part 3 Clean up</b> &middot; '+(s.state==='done'?'Finished':s.state==='error'?esc(s.message):'Deleting .json files...');setBar('bar','fill','pct',s.state==='done'?100:(s.total?100*s.done/s.total:0),s.state==='running'&&!s.total);
    $('cbar').style.display=s.state==='error'?'none':'block';setBar('cbar','cfill','cpct',s.state==='done'?100:(s.total?100*s.done/s.total:0),s.state==='running'&&!s.total);
    $('cres').innerHTML=s.state==='error'?'<span class="err">'+esc(s.message)+'</span>':`${s.state==='done'?'<span class="ok">Finished.</span> ':''}Deleted ${s.deleted.toLocaleString()} of ${s.total.toLocaleString()} (${fmtBytes(s.bytes)})${s.errors?`, <span class="err">${s.errors} errors</span>`:''}`;
    if(s.state!=='running')clearInterval(tm)},500)};

$('sb2').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose where the sorted library goes'});if(r.paths&&r.paths[0])$('sout').value=r.paths[0]};
$('sgo').onclick=async()=>{
  if(!sroots().length){alert('Add your folders in the bar at the top first');return}
  const inPlace=!$('sout').value.trim();
  if(inPlace&&!$('smove').checked){alert('Choose an output folder, or tick "Move" to merge everything into the first folder in the list');return}
  if($('smove').checked&&!$('sdry').checked&&!confirm(inPlace?'MOVE will merge everything into '+sroots()[0]+' and take files out of the other folders. Make sure you have another backup. Continue?':'MOVE takes files out of your source folders. Make sure you have another backup. Continue?'))return;
  $('sum').style.display='none';
  const r=await post('/api/sort_start',{roots:sroots(),out:$('sout').value.trim(),dry_run:$('sdry').checked,dedupe:$('sdedupe').checked,bring_json:$('sjson').checked,move:$('smove').checked});
  if(r.error)alert(r.error);else{$('prog').style.display='block';$('prog').scrollIntoView({behavior:'smooth'});poll()}};
function showSort(s){
  const w=s.dry_run?'would be ':'';
  let h=`<div class="tiles">${tile(s.total,'files found')}${tile(s.duplicates,'duplicates '+w+'skipped')}${tile(s.placed,'files '+w+'placed','ok')}${tile(s.folders_in,'source folders')}${tile(s.folders_out,'folders after merging')}${tile(s.json_along,'.json files brought along')}</div>`;
  h+=`<div class="tip" style="border-color:var(--acc)">${s.folders_in.toLocaleString()} folders ${s.dry_run?'would be ':'were '}merged into ${s.folders_out.toLocaleString()}.${s.duplicates?` ${s.duplicates.toLocaleString()} identical copies (${fmtBytes(s.dupe_bytes)}) ${s.dry_run?'would be ':'were '}skipped.`:''}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  if((s.out_folders||[]).length)h+='<h2>Resulting folders</h2>'+tbl(['Folder','Files'],s.out_folders.slice(0,80).map(r=>[esc(r[0]),r[1].toLocaleString()]))+(s.out_folders.length>80?'<small>Showing 80 of '+s.out_folders.length+'. See the report CSV for the rest.</small>':'');
  h+='<small>Saved: a CSV listing where every file went.</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}

(async function(){
  let boot=null;
  async function st(){try{const j=await (await fetch('/api/status')).json();if(j&&j.version)$('ver').textContent='Version '+j.version;return j}catch(e){return null}}
  for(let i=0;i<8;i++){const s=await st();if(s){boot=s.boot;if(s.update&&s.update.state==='available'){
    $('updmsg').textContent='Updated files: '+s.update.files.join(', ')+'. Your settings are not affected.';$('upd').style.display='block';break}
    if(s.update&&['current','unknown'].includes(s.update.state))break}
    await new Promise(r=>setTimeout(r,1500))}
  $('updno').onclick=()=>{$('upd').style.display='none'};
  $('updgo').onclick=async()=>{
    $('updgo').disabled=true;$('updmsg').textContent='Updating...';
    const r=await post('/api/update');
    if(r.error){$('updmsg').innerHTML='<span class="err">'+esc(r.error)+'</span>';$('updgo').disabled=false;return}
    $('updmsg').textContent='Updated. Restarting...';
    for(let i=0;i<30;i++){await new Promise(x=>setTimeout(x,1000));const s=await st();if(s&&s.boot!==boot){location.reload();return}}
    $('updmsg').textContent='Updated. If the page does not reload, restart the app in Terminal.'}
})();
function setBar(barId,fillId,pctId,pct,indet){
  const bar=$(barId),fill=$(fillId),lab=$(pctId);
  bar.classList.toggle('indet',!!indet);
  if(indet){lab.textContent='working...';lab.className='out';lab.style.left='12px';fill.style.width='';return}
  pct=Math.max(0,Math.min(100,pct));fill.style.width=pct+'%';lab.textContent=Math.floor(pct)+'%';
  if(pct>=12){lab.className='';lab.style.left='calc('+pct+'% - 44px)'}else{lab.className='out';lab.style.left='calc('+pct+'% + 8px)'}}

const TABS=['fix','sort','clean','convert','empty'];let jobKind='fix';
function showTab(t){if(!TABS.includes(t))t='fix';
  TABS.forEach(x=>{$('pane-'+x).style.display=x===t?'block':'none';document.querySelector('.tab[data-tab="'+x+'"]').classList.toggle('on',x===t)});
  try{localStorage.setItem('tab',t)}catch(e){}
  try{history.replaceState(null,'','#'+t)}catch(e){}
  updGoto()}
function updGoto(){const a=$('goto');const cur=TABS.find(x=>$('pane-'+x).style.display==='block');
  const has=$('prog').style.display!=='none'||$('sum').style.display!=='none';
  a.style.display=(has&&cur!==jobKind&&jobKind!=='clean')?'inline':'none'}
$('goto').onclick=e=>{e.preventDefault();showTab(jobKind);$('results').scrollIntoView({behavior:'smooth'})};
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
function placeResults(kind){const pane=$('pane-'+(kind==='clean'?'fix':kind));if(pane&&$('results').parentNode!==pane)pane.appendChild($('results'))}
let startTab='fix';try{startTab=location.hash.slice(1)||localStorage.getItem('tab')||'fix'}catch(e){}
showTab(startTab);

// ---- Part 4: convert
$('vgo').onclick=async()=>{
  if(!vroots().length){alert('Add your folders in the bar at the top first');return}
  const exts=[$('vavi').checked?'.avi':null,$('vmov').checked?'.mov':null].filter(Boolean);
  if(!exts.length){alert('Tick .avi and/or .mov');return}
  const act=$('vact').value;
  if(act==='delete'&&!$('vdry').checked){const t=prompt('This permanently deletes each original video after its .mp4 is verified. It cannot be undone.\nType DELETE to confirm.');if(t!=='DELETE')return}
  $('sum').style.display='none';
  const r=await post('/api/convert_start',{roots:vroots(),dry_run:$('vdry').checked,exts:exts,include_live:$('vlive').checked,quality:$('vq').value,action:act});
  if(r.error)alert(r.error);else{jobKind='convert';placeResults('convert');$('prog').style.display='block';poll()}};
function showConvert(s){
  const w=s.dry_run?'would be ':'';
  let h=`<div class="tiles">${tile(s.total,'videos found')}${tile(s.dry_run?s.would:s.converted,'videos '+w+'converted','ok')}${tile(s.remux,'re-wrapped (lossless)')}${tile(s.encode,'re-encoded')}${tile(s.skipped_live,'Live Photo videos skipped')}${tile(s.failed,'could not convert',s.failed?'bad':'')}</div>`;
  if(!s.dry_run&&s.converted)h+=`<div class="tip" style="border-color:var(--acc)">${fmtBytes(s.bytes_before)} became ${fmtBytes(s.bytes_after)} (${s.bytes_before?Math.round(100*(1-s.bytes_after/s.bytes_before)):0}% smaller). Originals: ${({move:'moved to _original_videos',keep:'kept in place',delete:'deleted'})[s.action]}.</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  if((s.failures||[]).length)h+='<h2>Problems</h2>'+tbl(['File','Result','Detail'],s.failures.map(f=>[esc(f.file),esc(f.status),esc(f.detail)]));
  h+='<small>Saved: a CSV with the result for every video (on your Desktop).</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}

function liveTiles(s,c,done,nj){
  if(!s.total)return '';const x=s.extra||{};const err=(x.errors?tile(x.errors,'files with errors','bad'):'');
  if(jobKind==='empty')return tile(s.total,'empty folders found')+tile(done,'processed so far','ok')+((s.extra||{}).errors?tile(s.extra.errors,'could not remove','bad'):'');
  if(jobKind==='convert')return tile(s.total,'videos found')+tile(done,'checked so far')+tile(x.converted||0,'converted','ok')+tile(x.skipped_live||0,'Live Photo videos skipped')+(x.errors?tile(x.errors,'could not convert','bad'):'');
  if(jobKind==='sort')return tile(s.total,'files found')+tile(x.duplicates||0,'duplicates skipped')+tile(x.written||0,'files placed','ok')+tile(x.merged_from||0,'source folders')+tile(x.folders||0,'folders after merging')+(x.json_along?tile(x.json_along,'.json brought along'):'')+err;
  return tile(s.total,'media files')+tile(done-nj,'matched so far','ok')+tile(nj,'no JSON so far',nj?'bad':'')+tile(x.dates_changed||0,'dates changed')+tile(x.gps_changed||0,'locations changed')+tile(x.desc_changed||0,'captions changed')+tile(x.replaced_files||0,'files with info replaced')+tile(x.live_paired||0,'Live Photos paired')+tile(x.duplicates||0,'duplicates skipped')+tile(x.written||0,'files placed')+tile(x.folders||0,'output folders')+err+(s.scan?tile(s.scan.json,'JSON files found'):'')}

// ---- Part 5: remove empty folders
$('ego').onclick=async()=>{
  if(!eroots().length){alert('Add your folders in the bar at the top first');return}
  if(!$('edry').checked&&!confirm('This will permanently remove every folder that is completely empty (no files inside, at any depth). No files are deleted. Continue?'))return;
  $('sum').style.display='none';
  const r=await post('/api/empty_start',{roots:eroots(),dry_run:$('edry').checked,ignore_junk:$('ejunk').checked,remove_top:$('etop').checked});
  if(r.error)alert(r.error);else{jobKind='empty';placeResults('empty');$('prog').style.display='block';poll()}};
function showEmpty(s){
  const w=s.dry_run?'would be ':'';
  let h=`<div class="tiles">${tile(s.scanned,'folders checked')}${tile(s.dry_run?s.empty:s.removed,'empty folders '+w+'removed','ok')}${tile(s.kept,'folders kept (have files)')}${tile(s.junk,'system leftovers '+w+'deleted')}${tile(s.skipped,'left alone (links, bundles)')}${tile(s.failed,'could not remove',s.failed?'bad':'')}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  if((s.groups||[]).length)h+=`<h2>Empty folders ${s.dry_run?'that would be':'that were'} removed (${s.group_count.toLocaleString()} top-level, each with everything inside it)</h2>`+tbl(['Folder','Folders inside'],s.groups.map(g=>[esc(g.root+'/'+g.path),g.nested.toLocaleString()]))+(s.group_count>s.groups.length?'<small>Showing '+s.groups.length+' of '+s.group_count+'. See the CSV for all.</small>':'');
  h+='<small>Saved: a CSV listing every folder (on your Desktop).</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}

let timer;function poll(){clearInterval(timer);timer=setInterval(async()=>{
  const s=await (await fetch('/api/status')).json();$('prog').style.display='block';
  const run=s.state==='scanning'||s.state==='running';$('go').disabled=run;
  jobKind=s.kind||'fix';placeResults(jobKind);
  let pct=0,indet=false;
  if(s.state==='done'){pct=100}
  else if(s.phase&&s.phase.stage==='convert'&&s.phase.total){pct=100*s.phase.done/s.phase.total}
  else if(s.done>0&&s.total){pct=100*s.done/s.total}
  else if(s.phase&&s.phase.total){pct=100*s.phase.done/s.phase.total}
  else if(s.state==='scanning'||s.state==='running'){indet=true}
  setBar('bar','fill','pct',pct,indet);
  const LBL={fix:'Part 1 Fix',sort:'Part 2 Sort',convert:'Part 4 Convert',empty:'Part 5 Empty folders'}[jobKind]||'';
  $('msg').innerHTML=(LBL?'<b>'+LBL+'</b> &middot; ':'')+(s.state==='error'?'<span class="err">'+esc(s.message)+'</span>':s.state==='done'?'<span class="ok">Finished.</span>':esc(s.message)+(s.done?` (${s.done.toLocaleString()} / ${s.total.toLocaleString()})`:''));
  const c=s.counts||{},done=s.done||0,nj=c['no-json']||0;
  $('tiles').innerHTML=liveTiles(s,c,done,nj);
  $('recent').innerHTML=(s.recent||[]).map(r=>`${esc(r.name)} &rarr; ${r.status==='duplicate'?'duplicate (skipped)':esc(r.to)+' ['+esc(r.status)+(r.live==='paired'?', live paired':'')+']'}`).reverse().join('<br>');
  if(s.state==='done'&&s.summary)showSummary(s.summary);
  updGoto();
  if(['done','error','idle'].includes(s.state))clearInterval(timer);
},500)}
(async function(){try{const s=await (await fetch('/api/status')).json();if(s.state&&s.state!=='idle'){jobKind=s.kind||'fix';placeResults(jobKind);$('prog').style.display='block';poll()}}catch(e){}})();
</script></main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-update-check", action="store_true", help="do not look for a newer version at startup")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    if not a.no_update_check:
        threading.Thread(target=check_update, daemon=True).start()
    print(f"Open {url}  (Ctrl+C to quit)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

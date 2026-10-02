#!/usr/bin/env python3
"""Local web UI for takeout_fix_metadata.py.

    python3 takeout_gui.py

Opens http://127.0.0.1:8765 in your browser. Nothing leaves your machine; the
server only listens on localhost. Needs exiftool (brew install exiftool).
"""
import argparse
import hashlib
import tempfile
from html import escape as html_escape
import platform
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import takeout_fix_metadata as fx

VERSION = "2026.10.02-n"
class Cancelled(Exception):
    pass


def check_cancel():
    if STATE.get("cancel"):
        raise Cancelled()


def stopped_state(what="Nothing further was changed."):
    with LOCK:
        STATE.update(state="idle", phase=None, cv=None, message="Stopped by you. " + what)


STATE = {"state": "idle", "total": 0, "done": 0, "counts": {}, "message": "", "report": "", "scan": None, "summary": None, "extra": {}, "recent": [], "clean": {"state": "idle"}, "update": {"state": "idle", "files": []}, "phase": None, "kind": "fix", "cv": None, "cancel": False, "guided": None, "photos": None, "run": None, "version": VERSION, "boot": time.time()}
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


def choose_zips(prompt):
    """Native macOS picker for Takeout .zip files (several allowed)."""
    script = ('set fs to choose file with prompt "%s" of type {"public.zip-archive", "zip"} with multiple selections allowed\n'
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


def run_job(roots, out, dry_run, overwrite, pair_live=False, dedupe=False, move=False, date_policy="earlier", name_dates=False, albums=False, edited="both"):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Scanning folders...",
                     report="", summary=None, scan=None, extra={}, recent=[], phase=None, kind="fix", **({} if IN_GUIDED[0] else {"guided": None}))
    stage_dir = None
    try:
        zips, folder_entries = fx.split_sources(roots)
        resolved, seen = [], set()
        for p in folder_entries:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                resolved.append(p)
        if not resolved and not zips:
            raise ValueError("Add at least one folder or Takeout zip file")
        if zips and not out:
            raise ValueError("Choose a Destination: Takeout zip files are never changed, so the fixed copies need somewhere to go.")
        if zips and move:
            raise ValueError("Move cannot be used with zip files (they are never changed). Untick Move.")
        if move and any(fx.inside_photos_library(p) for p in resolved):
            raise ValueError("Move cannot be used with a Photos library (it is only ever read). Untick Move.")
        if move and not out:
            out = str(resolved[0])  # Move with no destination: merge into the first source folder, like Sort
        if not dry_run and not shutil.which("exiftool"):
            raise ValueError("exiftool not found. In Terminal run: brew install exiftool")
        out_root = Path(out) if out else None
        rows, counts, extra = [], defaultdict(int), defaultdict(int)
        out_dirs, recent = set(), []
        all_sidecars, noext, dupe_bytes, claimed_all = [], 0, 0, set()
        shared_sizes, prior_sig = set(), {}
        album_links = {}
        zip_info = {"zips": 0, "skipped": 0, "bad": []}

        def run_pass(media, sidecars, pass_roots, ns=""):
            nonlocal dupe_bytes
            idx = fx.build_index(sidecars)
            args = argparse.Namespace(dry_run=dry_run, overwrite=overwrite, pair_live=pair_live,
                                      dedupe=dedupe, move=move, out_root=out or None, roots=pass_roots,
                                      date_policy=date_policy, manifest_ns=ns, name_dates=name_dates)
            args.skip = {}
            pass_start = len(rows)

            def hashing(stage, done, todo):
                check_cancel()
                with LOCK:
                    STATE["phase"] = {"stage": stage, "done": done, "total": todo}
                    if stage == "dedupe":
                        STATE["message"] = (f"Step 1: checking {todo:,} files that share a size with another file for "
                                            f"exact duplicates ({done:,}/{todo:,})")
                    else:
                        STATE["message"] = f"Step 2: reading Live Photo IDs ({done:,}/{todo:,} stills)"
            with LOCK:
                STATE["message"] = "Finding exact duplicates..." if dedupe else "Preparing..."
            fx.prepare(args, media, hashing)
            args.claimed |= claimed_all
            if edited in ("edited", "original"):
                pairs = fx.find_edited_pairs(media)
                for ed, og in pairs.items():
                    if edited == "edited":
                        args.skip[str(og)] = "left out: Google's edited version is kept"
                    else:
                        args.skip[str(ed)] = "left out: the original is kept"
                extra["edited_pairs"] += len(pairs)
            cur_sig = {}
            if dedupe and zips and shared_sizes:
                # the same photo in an earlier zip is not placed a second time (size + CRC32 fingerprint)
                for m in media:
                    try:
                        if str(m) in args.dupes or m.stat().st_size not in shared_sizes:
                            continue
                        sg = fx.file_sig(m)
                    except OSError:
                        continue
                    cur_sig[str(m)] = sg
                    if sg in prior_sig:
                        args.dupes[str(m)] = prior_sig[sg]
                        args.dupe_bytes += sg[0]
            with LOCK:
                STATE["phase"] = None
                STATE["message"] = (STATE.get("zipmsg", "") + f"fixing and placing {len(media):,} files") if zips else f"Step 3: fixing and placing {len(media):,} files"
            with ThreadPoolExecutor(max_workers=4) as ex:
                def work(m):
                    check_cancel()
                    return fx.guarded(fx.process)(m, idx, args, out_root)
                for row in ex.map(work, media):
                    rows.append(row)
                    counts[row["status"]] += 1
                    if row["status"] in ("updated", "would-update"):
                        if "replaced" in (row["date"], row["gps"], row["desc"]):
                            extra["replaced_files"] += 1
                        extra["fields_replaced"] += [row["date"], row["gps"], row["desc"]].count("replaced")
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
            if albums and dedupe and args.dupes:
                out_by_src = {r["file"]: r["output"] for r in rows[pass_start:] if r.get("output")}
                for dup, kept in args.dupes.items():
                    alb = Path(dup).parent.name
                    if not fx.is_album_folder(alb):
                        continue
                    kept_out = out_by_src.get(kept) or (kept if (out_root and str(kept).startswith(str(out_root)) and os.path.exists(kept)) else None)
                    if kept_out:
                        album_links.setdefault(kept_out, set()).add(alb)
            for r in rows[-len(media):] if media else []:
                sg = cur_sig.get(r["file"])
                if sg and r["output"] and sg not in prior_sig:
                    prior_sig[sg] = r["output"]
            dupe_bytes += getattr(args, "dupe_bytes", 0)
            claimed_all.update(args.claimed)

        # ---- ordinary folders
        if resolved:
            media, sidecars, mseen, sseen = [], [], set(), set()
            for p in resolved:
                m, sc = fx.scan(p)
                noext += getattr(fx.scan, "noext", 0)
                media += [x for x in m if x.resolve() not in mseen and not mseen.add(x.resolve())]
                sidecars += [x for x in sc if x.resolve() not in sseen and not sseen.add(x.resolve())]
            all_sidecars += sidecars
            plan = fx.zip_plan(zips) if zips else []
            with LOCK:
                STATE.update(state="running", total=len(media) + sum(z["media"] for z in plan),
                             scan={"media": len(media), "json": len(sidecars), "folders": len(resolved)},
                             message=f"{len(media)} media files, {len(sidecars)} json files")
            run_pass(media, sidecars, resolved)

        # ---- Takeout zip files: one at a time, so only one zip's photos are ever unpacked
        if zips:
            plan = fx.zip_plan(zips)
            bad = [z for z in plan if z["error"]]
            zip_info["bad"] = [(z["name"], z["error"]) for z in bad]
            good = [Path(z["path"]) for z in plan if not z["error"]]
            if not good:
                raise ValueError("None of the zip files could be opened: " + "; ".join(f"{n}: {e}" for n, e in zip_info["bad"]))
            if not dry_run:
                out_root.mkdir(parents=True, exist_ok=True)
            parent = out_root if out_root.exists() else Path(tempfile.gettempdir())
            stage_dir = parent / fx.ZIP_STAGE
            shutil.rmtree(stage_dir, ignore_errors=True)
            tree = stage_dir / "tree"
            tree.mkdir(parents=True)
            done_keys = fx.zips_done(out_root) if (out_root.exists() and not dry_run) else set()
            todo = [z for z in good if fx.zip_key(z) not in done_keys]
            zip_info["skipped"] = len(good) - len(todo)
            from collections import Counter
            sz = Counter(x for z in plan if Path(z["path"]) in todo for x in z["sizes"])
            shared_sizes.update(k for k, v in sz.items() if v > 1 and k)
            biggest = max((z["bytes"] for z in plan if Path(z["path"]) in todo), default=0)
            free = shutil.disk_usage(parent).free
            if todo and free < biggest * (1 if dry_run else 2) + 200 * 1024 * 1024:
                raise ValueError("Not enough free space on the destination drive: about %s is needed while the largest zip is unpacked "
                                 "(%s free). Free some space or choose another Destination." % (fmt_bytes(biggest * (1 if dry_run else 2)), fmt_bytes(free)))
            with LOCK:
                STATE.update(state="running", total=len(rows) + sum(z["media"] for z in plan if Path(z["path"]) in todo),
                             message="Reading the info (.json) files from all zips...")
            n_json = fx.stage_json(good, tree, check_cancel)
            _, json_sidecars = fx.scan(tree)
            all_sidecars += json_sidecars
            with LOCK:
                STATE["scan"] = {"media": sum(z["media"] for z in plan), "json": n_json, "folders": len(good), "zips": len(good)}
            for k, z in enumerate(todo, 1):
                check_cancel()
                with LOCK:
                    STATE["zipmsg"] = f"Zip {k} of {len(todo)} ({z.name}): "
                    STATE["message"] = STATE["zipmsg"] + "unpacking photos and videos"
                    STATE["phase"] = None
                start = len(rows)
                fx.stage_media(z, tree, check_cancel)
                fx.fix_extensions([tree], False, rename_json=True, aside=False)
                media, sidecars = fx.scan(tree)
                noext += getattr(fx.scan, "noext", 0)
                run_pass(media, sidecars, [tree], ns=fx.zip_key(z) + "::")
                zip_info["zips"] += 1
                for r in rows[start:]:
                    try:
                        r["file"] = z.name + "/" + str(Path(r["file"]).relative_to(tree))
                    except ValueError:
                        pass
                for dp, dns, fns in os.walk(tree):
                    for f in fns:
                        if not f.lower().endswith(".json"):
                            try:
                                os.remove(os.path.join(dp, f))
                            except OSError:
                                pass
                if not dry_run and not any(r["status"] in ("copy-error", "error", "exiftool-error") for r in rows[start:]):
                    fx.mark_zip_done(out_root, z)
            shutil.rmtree(stage_dir, ignore_errors=True)
            stage_dir = None
            sidecars = all_sidecars
        album_info = {"files": 0, "albums": 0, "written": 0}
        if albums and album_links:
            names = set()
            for outp, albs in album_links.items():
                names |= albs
                album_info["files"] += 1
                p = Path(outp)
                if not dry_run and p.exists() and p.suffix.lower() not in fx.NO_WRITE_EXT:
                    check_cancel()
                    ea = []
                    for a_ in sorted(albs):
                        ea += ["-XMP-dc:Subject+=" + a_, "-IPTC:Keywords+=" + a_]
                    ok_, _msg = fx.run_exiftool(p, ea, True, sidecar_for_raw=p.suffix.lower() in fx.RAW_EXT)
                    album_info["written"] += 1 if ok_ else 0
            album_info["albums"] = len(names)
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
        sm = summarise(rows, all_sidecars, resolved or [out_root], dry_run)
        sm["pruned"] = pruned
        if noext:
            sm["tips"].insert(0, "%d files with no file extension were skipped (Fix only works on files with a known type, such as .jpg or .heic). "
                              "Open the Clean up tab, tick 'Fix files with no extension', then run Fix again." % noext)
        if pruned:
            sm["tips"].append("%d folders left empty by the move were removed. Google's .json files are left where they were; remove them with the Clean up tab, then use Empty folders to tidy the rest." % pruned)
        nd = sum(1 for r in rows if r.get("match") == "filename-date" and r["status"] in ("updated", "would-update"))
        sm["name_dates"] = nd
        if nd:
            sm["tips"].append("%d files had no Google info file and no date of their own, so their date %s from the file name." % (nd, "would be taken" if dry_run else "was taken"))
        if album_links:
            sm["albums"] = album_info
            try:
                with open(report_dir / f"takeout_{tag}_albums.csv", "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(["file", "albums"])
                    for outp, albs in sorted(album_links.items()):
                        w.writerow([outp, "; ".join(sorted(albs))])
            except OSError:
                pass
            sm["tips"] = [t.replace(" Album copies of the same photo are not repeated, so album membership is not preserved.", " Album copies are not repeated; their album names are kept as keywords instead.") for t in sm["tips"]]
            sm["tips"].append("%d photo%s also lived in %d album%s. Their album names %s as keywords, and the full list is in takeout_%s_albums.csv." % (
                album_info["files"], "" if album_info["files"] == 1 else "s", album_info["albums"], "" if album_info["albums"] == 1 else "s", "would be saved" if dry_run else "were saved", tag))
        if extra.get("edited_pairs"):
            sm["edited_pairs"] = extra["edited_pairs"]
            sm["tips"].append("%d photo%s had a Google-edited copy next to the original. %s" % (
                extra["edited_pairs"], "" if extra["edited_pairs"] == 1 else "s", "Only the edited versions were kept." if edited == "edited" else "Only the originals were kept."))
        sm["problems"] = [{"file": Path(r["file"]).name, "status": r["status"], "detail": r.get("detail", "")} for r in rows
                          if r["status"] in ("copy-error", "error", "exiftool-error")][:100]
        sm["dupe_bytes"] = dupe_bytes
        if zip_info["zips"] or zip_info["skipped"] or zip_info["bad"]:
            sm["zip"] = zip_info
            if zip_info["zips"]:
                sm["tips"].insert(0, "Read %d Takeout zip file%s directly: nothing was unzipped to your drive and the zip files were not changed." % (zip_info["zips"], "" if zip_info["zips"] == 1 else "s"))
            if zip_info["skipped"]:
                sm["tips"].append("%d zip file%s already finished in an earlier run and %s skipped." % (zip_info["skipped"], "" if zip_info["skipped"] == 1 else "s", "was" if zip_info["skipped"] == 1 else "were"))
            for n, e in zip_info["bad"]:
                sm["tips"].append("Could not read %s (%s). It may be incomplete: download it again." % (n, e))
        sm["samples"] = [{"file": Path(r["file"]).name, "date": [r["date"], r["date_before"], r["date_google"], r.get("date_note", "")],
                          "gps": [r["gps"], r["gps_before"], r["gps_google"]],
                          "desc": [r["desc"], r["desc_before"], r["desc_google"]]}
                         for r in changed if "replaced" in (r["date"], r["gps"], r["desc"])
                         or "added" in (r["date"], r["gps"], r["desc"])][:15]
        write_text_summary(report_dir / f"takeout_{tag}_summary.txt", sm)
        with LOCK:
            STATE.update(state="done", report=str(report), summary=sm, message="Finished")
    except Cancelled:
        try:
            fx.close_all()
        except Exception:
            pass
        if stage_dir:
            shutil.rmtree(stage_dir, ignore_errors=True)
        stopped_state("Files already handled stay done; run it again to carry on.")
    except Exception as e:  # surface any failure in the UI
        if stage_dir:
            shutil.rmtree(stage_dir, ignore_errors=True)
        with LOCK:
            STATE.update(state="error", message=str(e))


IN_GUIDED = [False]


def run_guided(roots, out, dry_run, opts):
    try:
        _run_guided(roots, out, dry_run, opts)
    finally:
        IN_GUIDED[0] = False


def _run_guided(roots, out, dry_run, opts):
    """Fix my Takeout: the safe sequence in one go. Each step is an ordinary job; the results are combined."""
    IN_GUIDED[0] = True
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Getting ready...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="fix", cv=None,
                     guided={"steps": [], "i": 0, "final": False})
    try:
        zips, folders = fx.split_sources(roots)
        if not zips and not folders:
            raise ValueError("Add your Takeout folders or zip files in the bar at the top")
        if not out:
            raise ValueError("Choose a Destination in the bar at the top. Your originals are never changed: the finished library is copied there.")
        steps = []
        if opts.get("fix_ext") and folders:
            steps.append(("Repair files with a missing file type", lambda: run_cleanup(
                [str(p) for p in folders], dry_run, {"ext": {"json": True, "aside": False}, "json": False, "json_other": False,
                                                     "junk": None, "names": None, "empty": None})))
        steps.append(("Put dates, locations and captions back, and merge into one library", lambda: run_job(
            roots, out, dry_run, bool(opts.get("replace", True)), bool(opts.get("live", True)), bool(opts.get("dedupe", True)), False, "earlier", bool(opts.get("name_dates", True)), bool(opts.get("albums", True)), opts.get("edited", "both"))))
        if opts.get("convert") and not dry_run:
            steps.append(("Convert old videos to MP4", lambda: run_convert([out], False, list(fx.DEFAULT_EXT), False, "high", "move", False)))
        titles = [t for t, _ in steps]
        results = []
        for i, (title, fn) in enumerate(steps, 1):
            with LOCK:
                STATE["guided"] = {"steps": titles, "i": i, "final": False}
            fn()
            with LOCK:
                st, sm = STATE["state"], STATE.get("summary")
            if st != "done":
                with LOCK:
                    STATE["guided"] = {"steps": titles, "i": i, "final": True}
                return                      # an error or a Stop: leave that message on screen
            results.append({"title": title, "summary": sm})
            with LOCK:
                STATE.update(state="running", summary=None, message="Next step...")
        tips = []
        if dry_run:
            tips.append("This was a preview: nothing was changed. Untick Preview only and run again to do it for real.")
            if opts.get("convert"):
                tips.append("Video conversion is not part of a preview. Use the Convert tab to preview it once the library exists.")
            if opts.get("fix_ext") and folders:
                tips.append("Files with a missing file type are only counted in a preview, so the numbers below may be a little low.")
        else:
            tips.append("Your originals were not touched. Your finished library is in %s." % out)
        with LOCK:
            STATE.update(state="done", message="Finished", kind="guided", cv=None,
                         summary={"kind": "guided", "dry_run": dry_run, "steps": results, "tips": tips, "dest": out},
                         guided={"steps": titles, "i": len(steps), "final": True})
    except Cancelled:
        stopped_state("Steps that already finished stay done.")
        with LOCK:
            STATE["guided"] = {"steps": [], "i": 0, "final": True}
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e), guided={"steps": [], "i": 0, "final": True})


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
                                  pair_live=False, overwrite=False, out_root=out, roots=resolved,
                                  manifest_file=fx.MANIFEST_SORT)
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


def run_convert(roots, dry_run, exts, include_live, quality, action, estimate=False):
    with LOCK:
        STATE.update(state="scanning", total=0, done=0, counts={}, message="Looking for old videos...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="convert", **({} if IN_GUIDED[0] else {"guided": None}), cv=None, cancel=False)
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
            raise ValueError("Tick at least one video type")
        items = fx.scan_legacy(folders, exts)
        if not items:
            raise ValueError("No videos of the ticked types were found in those folders")
        n_items = len(items)
        with LOCK:
            STATE.update(state="running", total=0, done=0, message=f"Reading the details of {n_items:,} videos...",
                         phase={"stage": "pct", "done": 0, "total": n_items},
                         scan={"media": n_items, "json": 0, "folders": len(folders)})
        stop = lambda: bool(STATE.get("cancel"))
        probed, finished = {}, 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(fx.probe_video, it[0]): it for it in items}
            for fu in as_completed(futs):
                probed[str(futs[fu][0])] = fu.result()
                finished += 1
                if finished % 8 == 0 or finished == n_items:
                    with LOCK:
                        STATE["phase"] = {"stage": "pct", "done": finished, "total": n_items}
                        STATE["message"] = f"Reading the details of your videos: {finished:,} of {n_items:,}"
                if stop():
                    for f_ in futs:
                        f_.cancel()
                    raise Cancelled()
        MODE_LABEL = {"remux": "re-wrap", "audio": "audio only", "encode": "re-encode"}
        plan = {m: {"n": 0, "dur": 0.0, "bytes": 0} for m in MODE_LABEL}
        info_of = {}
        for p, _ in items:
            info = probed.get(str(p))
            if not info or not info["has_video"]:
                info_of[str(p)] = None
                continue
            skip = p.suffix.lower() == ".mov" and not include_live and fx.is_live_video(p)
            mode = fx.convert_mode(info)
            info_of[str(p)] = (mode, info["duration"] or 0.0, skip)
            if not skip:
                d_ = plan[mode]
                d_["n"] += 1
                d_["dur"] += info["duration"] or 0.0
                try:
                    d_["bytes"] += p.stat().st_size
                except OSError:
                    pass
        files_total = sum(d_["n"] for d_ in plan.values())
        total_secs = sum(d_["dur"] for d_ in plan.values())
        t0 = time.time()
        cv = None if dry_run else {"files_total": files_total, "files_done": 0, "secs_total": total_secs, "secs_done": 0.0,
                                   "plan": plan, "now": None, "recent": [], "bytes_before": 0, "bytes_after": 0,
                                   "eta": None, "elapsed": 0.0}
        with LOCK:
            STATE.update(total=n_items, done=0, cv=cv, phase=None if dry_run else {"stage": "convert", "done": 0, "total": max(total_secs, 1.0)},
                         message=("Checking" if dry_run else "Starting") + f" {n_items:,} videos...")
        opts_crf = fx.QUALITY_CRF.get(quality, 20)
        opts = {"dry_run": dry_run, "action": action, "include_live": include_live,
                "crf": opts_crf, "should_stop": stop}
        est_ratio, est_info = {}, {"samples": 0, "failed": 0}
        if dry_run and estimate and plan["encode"]["n"]:
            by_ext = defaultdict(list)
            for p, _ in items:
                meta = info_of.get(str(p))
                if meta and meta[0] == "encode" and not meta[2] and meta[1] >= 3:
                    by_ext[p.suffix.lower()].append(p)
            picks = []
            for e, lst in by_ext.items():
                lst = sorted(lst, key=lambda q: q.stat().st_size)
                step = max(1, len(lst) // 3)
                picks += [(e, q) for q in lst[::step][:3]]
            ratios = defaultdict(list)
            for k, (e, q) in enumerate(picks, 1):
                if stop():
                    raise Cancelled()
                with LOCK:
                    STATE["phase"] = {"stage": "pct", "done": k - 1, "total": max(1, len(picks))}
                    STATE["message"] = f"Estimating the new sizes: test-encoding a short sample ({k} of {len(picks)}): {q.name}"
                r_ = fx.sample_encode_ratio(q, probed[str(q)], opts_crf)
                if r_ is None:
                    est_info["failed"] += 1
                else:
                    ratios[e].append(r_)
                    est_info["samples"] += 1
            est_ratio = {e: sum(v) / len(v) for e, v in ratios.items()}
            with LOCK:
                STATE["phase"] = None
        rate = {m: [0.0, 0.0] for m in MODE_LABEL}      # mode -> [seconds of footage processed, wall seconds spent]
        done_by_mode = {m: 0.0 for m in MODE_LABEL}
        default_speed = {"remux": 40.0, "audio": 25.0, "encode": 1.5}

        def eta_secs(cur_mode=None, cur_secs=0.0):
            rem = 0.0
            for m, d_ in plan.items():
                left = max(0.0, d_["dur"] - done_by_mode[m] - (cur_secs if m == cur_mode else 0.0))
                sp = (rate[m][0] / rate[m][1]) if rate[m][1] > 5 else default_speed[m]
                rem += left / max(sp, 0.05)
            return rem

        rows, counts, extra, recent = [], defaultdict(int), defaultdict(int), []
        secs_done, stopped = 0.0, False
        for i, (p, root) in enumerate(items, 1):
            if stop():
                stopped = True
                break
            meta = info_of.get(str(p))
            mode, dur, skip = meta if meta else (None, 0.0, False)
            real = (not dry_run) and meta is not None and not skip
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            now = {"name": p.name, "dir": p.parent.name, "ext": p.suffix.lower(), "mode": mode, "size": size,
                   "dur": dur, "secs": 0.0, "speed": None} if real else None
            with LOCK:
                if real:
                    STATE["cv"]["now"] = dict(now)
                    STATE["cv"]["eta"] = eta_secs()
                    STATE["cv"]["elapsed"] = time.time() - t0
                    STATE["message"] = (f"Converting video {STATE['cv']['files_done'] + 1:,} of {files_total:,}: {p.name} "
                                        f"({MODE_LABEL[mode]}) · about {fmt_dur(STATE['cv']['eta'])} left")
                elif dry_run:
                    STATE["message"] = f"Checking {i:,} of {n_items:,}: {p.name}"

            def cb(secs, speed=None, _now=now, _mode=mode, _base=secs_done):
                if _now is None:
                    return
                _now["secs"], _now["speed"] = max(0.0, secs), speed
                with LOCK:
                    c = STATE["cv"]
                    c["now"] = dict(_now)
                    c["secs_done"] = _base + _now["secs"]
                    c["elapsed"] = time.time() - t0
                    c["eta"] = eta_secs(_mode, _now["secs"])
                    STATE["phase"] = {"stage": "convert", "done": c["secs_done"], "total": max(total_secs, 1.0)}
                    pct = int(100 * _now["secs"] / _now["dur"]) if _now["dur"] else 0
                    STATE["message"] = (f"Converting video {c['files_done'] + 1:,} of {files_total:,}: {_now['name']} "
                                        f"({MODE_LABEL[_mode]} {pct}%) · about {fmt_dur(c['eta'])} left")
            w0 = time.time()
            row = fx.convert_file(p, root, opts, cb)
            wall = time.time() - w0
            if real and row["status"] in ("converted", "already-converted", "failed"):
                secs_done += dur
                done_by_mode[mode] += dur
                if row["status"] == "converted":
                    rate[mode][0] += dur
                    rate[mode][1] += wall
            rows.append(row)
            counts[row["status"]] += 1
            if row["status"] in ("converted", "already-converted"):
                extra["converted"] += 1
                extra["bytes_before"] += int(row["size_before"] or 0)
                extra["bytes_after"] += int(row["size_after"] or 0)
            if row["status"] in ("failed", "unreadable"):
                extra["errors"] += 1
            if row["status"] == "skipped-live":
                extra["skipped_live"] += 1
            recent.append(row)
            del recent[:-12]
            with LOCK:
                if cv is not None and row["status"] in ("converted", "already-converted"):
                    c = STATE["cv"]
                    c["files_done"] += 1
                    c["bytes_before"] += int(row["size_before"] or 0)
                    c["bytes_after"] += int(row["size_after"] or 0)
                    c["recent"].append({"name": p.name, "mode": mode, "before": int(row["size_before"] or 0),
                                        "after": int(row["size_after"] or 0), "wall": round(wall, 1)})
                    del c["recent"][:-8]
                    c["now"] = None
                elif cv is not None and row["status"] == "failed":
                    STATE["cv"]["files_done"] += 1
                    STATE["cv"]["now"] = None
                STATE["done"] = i
                STATE["counts"] = dict(counts)
                STATE["extra"] = dict(extra)
                STATE["recent"] = [{"name": Path(r["file"]).name, "status": r["status"],
                                    "to": (r["mode"] + " " if r["mode"] else ""), "live": ""} for r in recent]
            if row["status"] == "cancelled":
                stopped = True
                break
        with LOCK:
            if STATE.get("cv"):
                STATE["cv"]["now"] = None
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
        sized = done_rows if done_rows else todo_rows
        by_mode, by_ext = {}, {}
        for r in sized:
            for table, key in ((by_mode, r["mode"]), (by_ext, Path(r["file"]).suffix.lower())):
                d = table.setdefault(key, {"n": 0, "before": 0, "after": 0})
                d["n"] += 1
                d["before"] += int(r["size_before"] or 0)
                d["after"] += int(r["size_after"] or 0)
        sm["by_mode"], sm["by_ext"] = by_mode, by_ext
        types = {}
        for r in rows:
            d = types.setdefault(Path(r["file"]).suffix.lower(), {"found": 0, "converted": 0, "live": 0, "failed": 0, "bytes": 0})
            d["found"] += 1
            d["bytes"] += int(r["size_before"] or 0)
            if r["status"] in ("converted", "already-converted", "would-convert", "would-finish"):
                d["converted"] += 1
                d["conv_before"] = d.get("conv_before", 0) + int(r["size_before"] or 0)
            elif r["status"] == "skipped-live":
                d["live"] += 1
            else:
                d["failed"] += 1
        sm["types"] = types
        for e, d in types.items():
            d["after"] = sum(int(r["size_after"] or 0) for r in done_rows if Path(r["file"]).suffix.lower() == e)
        if dry_run:
            est_total = 0
            have_est = bool(est_ratio)
            for e, d in types.items():
                tot = 0
                for r in todo_rows:
                    if Path(r["file"]).suffix.lower() != e:
                        continue
                    sz = int(r["size_before"] or 0)
                    if r["mode"] == "encode":
                        tot += sz * est_ratio.get(e, 0) if e in est_ratio else 0
                    else:
                        tot += sz
                d["est_after"] = int(tot) if (e in est_ratio or not any(r["mode"] == "encode" and Path(r["file"]).suffix.lower() == e for r in todo_rows)) else None
                d["est_ratio"] = est_ratio.get(e)
            sm["est"] = {"samples": est_info["samples"], "failed": est_info["failed"],
                         "after": sum(d["est_after"] or 0 for d in types.values()),
                         "complete": all(d.get("est_after") is not None for d in types.values() if d["converted"]),
                         "asked": bool(estimate)}
        sm["remux"] = sm["remux"] + sum(1 for r in done_rows + todo_rows if r["mode"] == "audio")
        sm["top"] = [{"file": Path(r["file"]).name, "mode": r["mode"], "before": int(r["size_before"] or 0), "after": int(r["size_after"] or 0)}
                     for r in sorted(sized, key=lambda r: -(int(r["size_before"] or 0) - int(r["size_after"] or 0)) if done_rows
                                     else -int(r["size_before"] or 0))[:15]]
        sm["elapsed"] = round(time.time() - t0)
        sm["stopped"] = stopped
        if stopped:
            sm["tips"].append("Stopped by you after %d videos. Nothing is half-done: press Start again with the same settings and "
                              "the videos already converted are recognised and skipped." % sm["converted"])
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
    except Cancelled:
        stopped_state("Nothing was changed.")
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


def run_cleanup(roots, dry_run, opts):
    """One Clean up job: .json files, junk files, name tidying, empty folders, always in that safe order."""
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Preparing...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="cleanup", **({} if IN_GUIDED[0] else {"guided": None}))
    try:
        folders = check_clean_folders(roots)
        tasks = [t for t in ("ext", "json", "junk", "names", "empty") if opts.get(t)]
        if not tasks:
            raise ValueError("Tick at least one thing to clean")
        n = len(tasks)
        titles = {"ext": "Files with no extension", "json": "Google .json files", "junk": "Junk and cache files", "names": "Tidy names", "empty": "Empty folders"}
        sections, rows, extra, recent, deleted = [], [], defaultdict(int), [], set()

        def disp(p):
            for f in folders:
                try:
                    return str(Path(f).name / Path(p).relative_to(f))
                except ValueError:
                    continue
            return str(p)

        def phase(i, frac, msg=None):
            check_cancel()
            with LOCK:
                STATE["state"] = "running"
                STATE["phase"] = {"stage": "pct", "done": 100 * (i + max(0.0, min(1.0, frac))) / n, "total": 100}
                if msg:
                    STATE["message"] = f"Step {i + 1} of {n}: {msg}"
                STATE["extra"] = dict(extra)
                STATE["recent"] = list(recent)

        def feed(name, status):
            recent.append({"name": name, "status": status, "to": "", "live": ""})
            del recent[:-12]

        def delete_items(i, items, label):
            count = size = failed = 0
            for k, (p, sz, _) in enumerate(items, 1):
                ok = True
                if not dry_run:
                    try:
                        os.remove(p)
                    except OSError as e:
                        ok = False
                        failed += 1
                        rows.append({"task": label, "kind": "file", "path": p, "new": "", "action": "failed", "detail": str(e)})
                if ok:
                    count += 1
                    size += sz
                    deleted.add(str(p))
                    rows.append({"task": label, "kind": "file", "path": p, "new": "",
                                 "action": "would-delete" if dry_run else "deleted", "detail": ""})
                    extra[label] += 1
                if k % 40 == 0 or k == len(items):
                    feed(os.path.basename(p), "would-delete" if dry_run else "deleted")
                    phase(i, k / max(1, len(items)), f"{'checking' if dry_run else 'deleting'} {label} files ({k:,}/{len(items):,})")
            extra["errors"] += failed
            return count, size, failed

        w = "would be " if dry_run else ""
        for i, task in enumerate(tasks):
            phase(i, 0, titles[task] + "...")
            if task == "ext":
                def cbx(stage, done, total):
                    phase(i, done / max(1, total) * (0.5 if stage == "detecting types" else 1.0), f"{stage} ({done:,}/{total:,})")
                res = fx.fix_extensions(folders, dry_run, cbx, bool(opts["ext"].get("json", True)), bool(opts["ext"].get("aside")))
                for r in res:
                    rows.append({"task": "ext", "kind": "file", "path": r["path"], "new": r["new"], "action": r["action"], "detail": r["detail"]})
                    if r["action"] in ("renamed", "would-rename"):
                        extra["renamed"] += 1
                    if r["action"] == "failed":
                        extra["errors"] += 1
                    feed(os.path.basename(r["path"]), r["action"])
                ok = [r for r in res if r["action"] in ("renamed", "would-rename")]
                bad = [r for r in res if r["action"].startswith(("unrecognised", "empty"))]
                dmg = [r for r in ok if r["detail"].startswith("may be damaged")]
                bytype = defaultdict(int)
                for r in ok:
                    bytype[r["ext"]] += 1
                sections.append({"task": task, "title": titles[task],
                                 "tiles": [[len(res), "files with no extension found"], [len(ok), f"files {w}given an extension", "ok"],
                                           [sum(r["json"] for r in ok), f"Google .json files {w}renamed to match"],
                                           [len(dmg), "recognised but possibly damaged", "bad" if dmg else ""],
                                           [len(bad), "not recognised (empty or damaged)", "bad" if bad else ""]],
                                 "table": {"head": ["Detected type", "Files"], "rows": [[f".{k}", str(v)] for k, v in sorted(bytype.items(), key=lambda kv: -kv[1])]},
                                 "table2": {"title": "Files that could not be recognised: why", "head": ["File", "Size", "Why"],
                                            "rows": [[disp(r["path"]), fmt_bytes(r["size"]), r["detail"] + (" [" + r["action"].split("(")[-1].rstrip(")") + "]" if "(" in r["action"] else "")] for r in bad[:60]]} if bad else None,
                                 "note": (("These cannot be repaired automatically. Open one to check, or restore it from your original export or backup. " if bad else "") +
                                          "Run this before the Fix metadata tab: Fix only looks at files with a known extension.")})
            elif task == "json":
                cats, files = find_json(folders, bool(opts.get("json_other")))
                c, sz, fl = delete_items(i, [(p, s_, "json") for p, s_ in files], "json")
                labels = {"photo": "Google info files for photos", "album": "Album / memory data files", "other": "Other .json files"}
                sections.append({"task": task, "title": titles[task],
                                 "tiles": [[c, f".json files {w}deleted", "ok"], [fmt_bytes(sz), "space freed"], [fl, "could not delete", "bad" if fl else ""]],
                                 "table": {"head": ["Kind found", "Files"], "rows": [[labels.get(k, k), str(v["files"])] for k, v in cats.items()]},
                                 "note": "" if opts.get("json_other") else "Other .json files were left alone."})
            elif task == "junk":
                items = fx.find_junk(folders, opts["junk"])
                c, sz, fl = delete_items(i, items, "junk")
                by = defaultdict(lambda: [0, 0])
                for _, s_, k in items:
                    by[k][0] += 1
                    by[k][1] += s_
                names = {"system": "System leftovers (.DS_Store, Thumbs.db, ...)", "ithmb": "iPod/iTunes thumbnail caches (.ithmb)",
                         "picasa": "Picasa.ini", "thm": "Camera video thumbnails (.thm)",
                         "safesave": "Empty temporary files left by macOS saving (.sb-...)", "empty": "Other empty (0-byte) files"}
                kept_sb = fx.find_safesave_kept(folders) if "safesave" in opts["junk"] else []
                examples = [it for it in items if it[2] in ("safesave", "empty")][:25]
                sections.append({"task": task, "title": titles[task],
                                 "tiles": [[c, f"junk files {w}deleted", "ok"], [fmt_bytes(sz), "space freed"], [fl, "could not delete", "bad" if fl else ""]],
                                 "table": {"head": ["Kind", "Files", "Size"], "rows": [[names.get(k, k), str(v[0]), fmt_bytes(v[1])] for k, v in by.items()]},
                                 "table2": {"title": "Examples of empty files", "head": ["File", "What it is"],
                                            "rows": [[disp(p_), names.get(k_, k_)] for p_, _s, k_ in examples]} if examples else None,
                                 "note": ("%d temporary .sb- files that are NOT empty were left alone (they may hold unsaved work): %s" %
                                          (len(kept_sb), "; ".join(os.path.basename(x) for x in kept_sb[:6]))) if kept_sb else ""})
            elif task == "names":
                def cb(stage, done, total):
                    phase(i, done / max(1, total), f"tidying {stage} ({done:,}/{total:,})")
                res = fx.tidy_names(folders, opts["names"], dry_run, cb)
                for r in res:
                    rows.append({"task": "names", "kind": r["kind"], "path": r["old"], "new": r["new"], "action": r["action"],
                                 "detail": r["detail"] or (f"{r['moved']} moved, {r['dupes']} identical, {r['conflicts']} renamed _1" if r["action"] in ("merged", "would-merge") else "")})
                    if r["action"] in ("renamed", "would-rename") and r["kind"] == "folder":
                        extra["renamed"] += 1
                    if r["action"] in ("merged", "would-merge"):
                        extra["merged"] += 1
                    if r["action"] == "failed":
                        extra["errors"] += 1
                    feed(os.path.basename(r["old"]), r["action"])
                ren = sum(1 for r in res if r["kind"] == "folder" and r["action"] in ("renamed", "would-rename"))
                mer = [r for r in res if r["action"] in ("merged", "would-merge")]
                fren = sum(1 for r in res if r["kind"] == "file" and r["action"] in ("renamed", "would-rename"))
                skp = sum(1 for r in res if r["action"] == "skipped")
                fail = sum(1 for r in res if r["action"] == "failed")
                sections.append({"task": task, "title": titles[task],
                                 "tiles": [[ren, f"folders {w}renamed", "ok"], [len(mer), f"folders {w}merged into an existing one"],
                                           [sum(r["moved"] for r in mer), "files moved in merges"],
                                           [sum(r["dupes"] for r in mer), "identical copies " + ("found" if dry_run else ("deleted" if opts["names"].get("dupes") == "delete" else "moved aside"))],
                                           [sum(r["conflicts"] for r in mer), "different files given a _1 name"],
                                           [fren, f"files {w}renamed"], [skp, "skipped (clean name taken)"], [fail, "failed", "bad" if fail else ""]],
                                 "table": {"head": ["Before", "After"], "rows": [[disp(r["old"]), os.path.basename(r["new"]) + ("  (merge)" if r["action"] in ("merged", "would-merge") else "")]
                                                                               for r in res if r["action"] not in ("skipped", "failed")][:100]},
                                 "note": ("Showing the first 100 changes. " if len(res) > 100 else "") + ("Years in brackets, such as (2019), are never changed." if opts["names"].get("paren") else "")})
            elif task == "empty":
                eo = opts["empty"]
                found, scanned, skipped = [], 0, 0
                for f in folders:
                    e, _, nscan, k = fx.find_empty_dirs(f, bool(eo.get("junk")), deleted if dry_run else None)
                    found += [p for p in e if eo.get("top") or p != str(f)]
                    scanned += nscan
                    skipped += k
                found.sort(key=lambda p: -len(Path(p).parts))
                removed = failed = 0
                for k, p in enumerate(found, 1):
                    ok, err = (True, "") if dry_run else fx.remove_empty_dir(p, bool(eo.get("junk")))
                    removed += 1 if ok else 0
                    failed += 0 if ok else 1
                    rows.append({"task": "empty", "kind": "folder", "path": p, "new": "", "action": ("would-remove" if dry_run else "removed") if ok else "failed", "detail": err})
                    extra["empty"] += 1 if ok else 0
                    extra["errors"] += 0 if ok else 1
                    if k % 20 == 0 or k == len(found):
                        feed(os.path.basename(p), "would-remove" if dry_run else "removed")
                        phase(i, k / max(1, len(found)), f"{'checking' if dry_run else 'removing'} empty folders ({k:,}/{len(found):,})")
                fs_ = set(found)
                groups = [p for p in sorted(found) if str(Path(p).parent) not in fs_]
                sections.append({"task": task, "title": titles[task],
                                 "tiles": [[removed, f"empty folders {w}removed", "ok"], [scanned - len(found), "folders kept (hold files)"],
                                           [skipped, "left alone (links, bundles)"], [failed, "could not remove", "bad" if failed else ""]],
                                 "table": {"head": ["Top-level empty folder"], "rows": [[disp(p)] for p in groups[:100]]},
                                 "note": ("Showing the first 100 of %d. " % len(groups) if len(groups) > 100 else "") +
                                         ("In a preview, files that would be deleted in the earlier steps are treated as gone, but folders emptied by name merges are not counted." if dry_run and n > 1 else "")})
        report_dir = Path.home() / "Desktop"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / ("takeout_cleanup_preview.csv" if dry_run else "takeout_cleanup_report.csv")
        with open(report, "w", newline="", encoding="utf-8") as fh:
            w_ = csv.DictWriter(fh, fieldnames=["task", "kind", "path", "new", "action", "detail"])
            w_.writeheader()
            w_.writerows(rows)
        tips = []
        if dry_run:
            tips.append("This was a preview: nothing was deleted, renamed, merged or removed. Untick Preview only to do it.")
        else:
            tips.append("Done. A CSV listing every change is on your Desktop.")
        with LOCK:
            STATE.update(state="done", report=str(report), message="Finished", phase=None,
                         summary={"kind": "cleanup", "dry_run": dry_run, "sections": sections, "tips": tips})
    except Cancelled:
        try:
            fx.close_all()
        except Exception:
            pass
        stopped_state("Steps that already finished stay done.")
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e))


def fmt_dur(s):
    s = int(max(0, s))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {sec:02d}s" if m else f"{sec}s")


def fmt_bytes(b):
    return f"{b / 1e9:.2f} GB" if b > 1e9 else f"{b / 1e6:.1f} MB" if b > 1e6 else f"{round(b / 1e3)} KB"


def run_merge(roots, dest, opts, dry_run):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Looking through the folders...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="merge", **({} if IN_GUIDED[0] else {"guided": None}))
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
            raise ValueError("Add the folders to merge in the bar at the top")
        fx.check_merge_roots([str(p) for p in resolved], dest or None, bool(opts.get("move")))
        in_place = not dest
        counts, extra, recent = defaultdict(int), defaultdict(int), []

        def prog(done, total):
            check_cancel()
            with LOCK:
                STATE.update(state="running", total=total, done=done,
                             message=f"{'Checking' if dry_run else 'Merging'} {done:,} of {total:,} files")

        def item(row):
            check_cancel()
            counts[row["status"]] += 1
            if row["status"] in ("placed", "kept-both", "replaced", "kept-existing"):
                extra["placed"] += 1
            if row["status"] in ("kept-both", "replaced", "kept-existing"):
                extra["clashes"] += 1
            if row["status"] == "identical":
                extra["identical"] += 1
            if row["status"] == "failed":
                extra["errors"] += 1
            if row.get("json"):
                extra["json_along"] += 1
            recent.append({"name": os.path.basename(row["src"]), "status": row["status"],
                           "to": (Path(row["dest"]).parent.name + "/") if row["dest"] else "", "live": ""})
            del recent[:-12]
            with LOCK:
                STATE["counts"] = dict(counts)
                STATE["extra"] = dict(extra)
                STATE["recent"] = list(recent)
        rows, per_root, merged = fx.merge_trees([str(p) for p in resolved], dest or None, opts, dry_run, prog, item)
        report_dir = Path.home() / "Desktop"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / ("takeout_merge_preview.csv" if dry_run else "takeout_merge_report.csv")
        with open(report, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["root", "src", "dest", "status", "detail"], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        st = defaultdict(int)
        for r in rows:
            st[r["status"]] += 1
        base = str(Path(dest or resolved[0]))
        sm = {"kind": "merge", "dry_run": dry_run, "move": bool(opts.get("move")), "in_place": in_place, "dest": base,
              "total": len(rows), "brought": st["placed"] + st["kept-both"] + st["replaced"] + st["kept-existing"],
              "in_place_files": st["in-place"] + st["already-done"], "identical": st["identical"],
              "clashes": st["kept-both"] + st["replaced"] + st["kept-existing"], "failed": st["failed"],
              "merged_dirs": len(merged), "json_along": sum(r.get("json", 0) for r in rows),
              "per_root": per_root,
              "merged": [[os.path.relpath(d, base) if d != base else ".", len(v)] for d, v in sorted(merged.items())][:60],
              "clash_rows": [{"file": os.path.relpath(r["src"], r["root"]), "status": r["status"], "detail": r["detail"]}
                             for r in rows if r["status"] in ("kept-both", "replaced", "kept-existing", "failed")][:60],
              "tips": []}
        c = opts.get("conflict", "both")
        if sm["clashes"]:
            sm["tips"].append(("%d different files shared a name and a folder. " % sm["clashes"]) +
                               ("Both were kept: the second one is named name_1." if c == "both" else
                                "The %s file kept the name; the other was set aside in the _merge_conflicts folder inside the destination, so nothing was lost." % c))
        if sm["identical"]:
            sm["tips"].append("%d identical copies were kept once%s." % (sm["identical"],
                              (" (the extra copy was " + ("deleted" if opts.get("dupes") == "delete" else "moved to a _duplicates folder") + ")") if opts.get("move") and not dry_run else ""))
        if in_place:
            sm["tips"].append("Merged in place: everything was brought into %s, the first folder in your list." % base)
        if opts.get("move") and not dry_run:
            sm["tips"].append("Folders left empty by the move were removed.")
        if dry_run:
            sm["tips"].append("This was a preview: nothing was copied, moved or deleted.")
        with LOCK:
            STATE.update(state="done", report=str(report), summary=sm, message="Finished", phase=None)
    except Cancelled:
        try:
            fx.close_all()
        except Exception:
            pass
        stopped_state("Files already placed stay put; run it again with the same settings to carry on.")
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
        if fx.inside_photos_library(p):
            raise ValueError(f"{p.name} is inside a Photos library. Backstory only reads Photos libraries and never changes them.")
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
DOC_FILES = ["USER_GUIDE.md", "THIRD_PARTY_NOTICES.md", "LICENSE"]   # documents travel with updates too (a missing one is ignored)
SUPPORT_EMAIL = "thestocksoup@gmail.com"
HERE = Path(__file__).resolve().parent


API_BASE = "https://api.github.com/repos/daviddef/MetadataFixer/contents/"


def _fetch(name):
    """Latest published file. Uses GitHub's API (fresh) and falls back to the raw download address,
    which GitHub caches for about 5 minutes. A custom METADATAFIXER_UPDATE_BASE (testing) skips the API."""
    if not os.environ.get("METADATAFIXER_UPDATE_BASE"):
        try:
            req = urllib.request.Request(API_BASE + name + "?ref=main", headers={
                "User-Agent": "MetadataFixer-updater", "Accept": "application/vnd.github.raw"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
            if data:
                return data
        except Exception:
            pass  # rate-limited or offline: try the raw address
    req = urllib.request.Request(UPDATE_BASE + name, headers={"User-Agent": "MetadataFixer-updater"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


FROZEN = bool(getattr(sys, "frozen", False))
RELEASES_URL = "https://github.com/daviddef/MetadataFixer/releases/latest"


def check_release():
    """Packaged app: compare with the newest GitHub release and point to the download."""
    req = urllib.request.Request("https://api.github.com/repos/daviddef/MetadataFixer/releases/latest",
                                 headers={"User-Agent": "MetadataFixer-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        tag = json.loads(r.read().decode()).get("tag_name", "").lstrip("v")
    newer = bool(tag) and tag != VERSION
    with LOCK:
        STATE["update"] = {"state": "available" if newer else "current", "files": [],
                           "message": f"Version {tag} is ready to download: {RELEASES_URL}" if newer else ""}


def check_update():
    """Compare the local files with the latest published ones. Never raises."""
    with LOCK:
        STATE["update"] = {"state": "checking", "files": []}
    if FROZEN:
        try:
            check_release()
        except Exception as e:
            with LOCK:
                STATE["update"] = {"state": "unknown", "files": [], "message": str(e)[:120]}
        return
    try:
        changed = []
        for name in UPDATE_FILES + DOC_FILES:
            try:
                remote = _fetch(name)
            except Exception:
                if name in UPDATE_FILES:
                    raise
                continue
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
    if FROZEN:
        return {"error": "Download the newest version from " + RELEASES_URL + " and replace the app in Applications."}
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
        for name in DOC_FILES:
            try:
                data = _fetch(name)
                if data.strip():
                    fresh[name] = data
            except Exception:
                pass
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


def doctor(roots, dest):
    """Is everything in place for a run? Used by the Guided checklist."""
    out = {"exiftool": "", "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "free": None, "need": None,
           "dest_ok": None, "sources": 0, "zips": 0, "error": ""}
    ex = shutil.which("exiftool")
    if ex:
        try:
            out["exiftool"] = subprocess.run([ex, "-ver"], capture_output=True, text=True, timeout=10).stdout.strip() or "ok"
        except Exception:
            out["exiftool"] = "ok"
    try:
        zips, folders = fx.split_sources(roots) if roots else ([], [])
        out["sources"], out["zips"] = len(zips) + len(folders), len(zips)
        if zips:
            out["need"] = sum(z.stat().st_size for z in zips)
    except (ValueError, OSError) as e:
        out["error"] = str(e)
    if dest:
        p = Path(dest).expanduser()
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            out["free"] = shutil.disk_usage(probe).free
            out["dest_ok"] = os.access(probe, os.W_OK)
        except OSError:
            out["dest_ok"] = False
    return out


def read_doc(name):
    """A documentation file: the local copy, or the published one when this install does not have it yet."""
    for base in (HERE, HERE.parent):
        p = base / name
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                pass
    try:
        return _fetch(name).decode("utf-8")
    except Exception:
        return ""


def build_recommendations(F, dest):
    """Turn the facts about the user's files into a plain-language plan. Every option and workflow Backstory has gets an
    answer here: recommended (with the numbers behind it), optional, or not needed."""
    def n(x):
        return f"{int(x):,}"

    def pl(x, one, many):
        return f"{int(x):,} " + (one if int(x) == 1 else many)
    recs, extras, warns, flow = [], [], [], []
    media = F["media"]
    S = F.get("sample") or {}
    multi = len(F.get("sources", [])) > 1
    free = F.get("free")
    pre = F.get("photos_pre") or {}
    # ---------------------------------------------------------------- warnings
    for name, err in F.get("bad_zips", []):
        warns.append("%s could not be read (%s). It may be incomplete: download it again from Google Takeout." % (name, err))
    if F.get("zip_gaps"):
        warns.append("Your Takeout zip numbering skips %s. If you meant to include them, add the missing zip file%s." % (
            ", ".join("%03d" % g for g in F["zip_gaps"][:8]), "s" if len(F["zip_gaps"]) > 1 else ""))
    copy_need = F["media_bytes"] * 1.05 + (F["biggest_zip"] * 2 if F["zips"] else 0)
    if dest and free is not None and free < copy_need:
        warns.append("The Destination may be too small: building the library needs about %s and only %s is free there. Choose a bigger drive (an external drive is fine) as the Destination." % (fmt_bytes(copy_need), fmt_bytes(free)))
    if not dest:
        warns.append("You have not chosen a Destination yet. Choose where the finished library should go (a new, empty folder, ideally on a drive with room) before running the plan.")
    if F["zero_media"]:
        warns.append("%s photos or videos are empty (0 bytes) and will be skipped." % n(F["zero_media"]))
    if media == 0:
        warns.append("No photos or videos were found in what you added. Check that you chose your Takeout zip files or the folders that hold them.")
    if F.get("photos_libs"):
        warns.append("%s of your sources is a Photos library. Backstory only reads it: its photos are copied out, and its albums and edits are not carried over." % pl(F["photos_libs"], "library", "libraries"))
    plan = {"dedupe": True, "live": False, "name_dates": False, "fix_ext": False, "replace": True, "convert": False, "albums": False}
    # ---------------------------------------------------------------- the library build (Guided options)
    if media:
        pct = round(100 * F["matched"] / media)
        if F["json"]:
            why = "%s of your %s photos and videos (%s%%) have a Google info file (.json) that holds the real date, location and caption." % (n(F["matched"]), n(media), pct)
            if S.get("n"):
                k = S["n"]
                est = lambda key: round(S[key] / max(1, S["with_json"]) * F["matched"]) if S.get("with_json") else 0
                why += " In a sample of %d of your files, %d%% had no date inside them and %d%% had no location; Google's info would add roughly %s dates, %s locations and %s captions across everything." % (
                    k, round(100 * (k - S["has_date"]) / k), round(100 * (k - S["has_gps"]) / k), n(est("add_date")), n(est("add_gps")), n(est("add_desc")))
            recs.append({"id": "restore", "title": "Put the real dates, locations and captions back", "why": why, "risk": "safe", "on": True, "fixed": True})
            if pct < 60 and F["matched"] < media:
                warns.append("Only %s%% of your files found an info file. Their info files may be in Takeout zips you have not added yet. Add them and check again." % pct)
        else:
            recs.append({"id": "restore", "title": "Put dates, locations and captions back", "risk": "safe", "on": True, "fixed": True,
                         "why": "No Google info files (.json) were found, so there is nothing to restore from. Dates will only come from the file names where possible. If this is a Takeout export, add the zip files that hold the .json files."})
        if S.get("n") and S.get("diff_date"):
            recs.append({"id": "datepol", "title": "Dates: the earlier date wins", "risk": "safe", "on": True, "fixed": True,
                         "why": "In your sample, %d of %d files with an info file already have a date that differs from Google's. Backstory keeps the earlier of the two, because Google often records the upload day. You can change this rule on the Fix tab." % (S["diff_date"], max(1, S["with_json"]))})
    if F["name_date_candidates"]:
        plan["name_dates"] = True
        recs.append({"id": "name_dates", "title": "Use the date in the file name where there is no info file", "risk": "safe", "on": True,
                     "why": "%s photos and videos have no info file but have a date in their name (like IMG_20190704_123456). Only a missing date is filled in; an existing date is never changed." % n(F["name_date_candidates"])})
    if multi or F["dup_n"] or F["wrapper"]:
        if F["dup_n"]:
            recs.append({"id": "dedupe", "title": "Merge folders and copy duplicates once", "risk": "safe", "on": True,
                         "why": "%s of the same photo %s found, taking %s. They will be copied once. Same-named folders from different sources merge into one." % (
                             pl(F["dup_n"], "extra copy" if F["dup_exact"] else "likely extra copy", "extra copies" if F["dup_exact"] else "likely extra copies"),
                             "was" if F["dup_n"] == 1 else "were", fmt_bytes(F["dup_bytes"]))})
        else:
            recs.append({"id": "dedupe", "title": "Merge same-named folders", "risk": "safe", "on": True,
                         "why": "No exact duplicates were found, but every folder with the same name (like Photos from 2012) across your sources will still become one folder."})
    else:
        recs.append({"id": "dedupe", "title": "Skip exact duplicates", "risk": "safe", "on": True, "why": "No duplicates were found. This stays on in case the run finds some."})
    if F["dup_n"] and (F["wrapper"] or F["zips"]):
        plan["albums"] = True
        recs.append({"id": "albums", "title": "Keep your album names", "risk": "safe", "on": True,
                     "why": "Photos that appear in an album and in a year folder are copied once. Their album names are saved as keywords on the kept copy, so you do not lose your albums, and a list of albums is saved with the reports."})
    if F.get("edited_pairs"):
        recs.append({"id": "edited", "title": "Google-edited copies", "risk": "safe", "on": True, "fixed": True,
                     "why": "%s have a Google-edited copy next to the original (IMG_1-edited.jpg). Both are kept by default. If you only want one, choose it in the options above." % pl(F["edited_pairs"], "photo", "photos")})
    if F["live_pairs"]:
        plan["live"] = True
        recs.append({"id": "live", "title": "Re-pair Live Photos", "risk": "safe", "on": True,
                     "why": "%s have a matching video with the same name, the signature of an iPhone Live Photo. Re-pairing lets Apple Photos show them together." % pl(F["live_pairs"], "photo", "photos")})
    if F["extless"]:
        plan["fix_ext"] = True
        recs.append({"id": "fix_ext", "title": "Repair files with a missing file type", "risk": "caution" if F["folders"] else "safe", "on": True,
                     "why": "%s no .jpg/.heic/.mp4 ending, so %s skipped. " % (pl(F["extless"], "file has", "files have"), "it would be" if F["extless"] == 1 else "they would be") + (
                         "Inside zip files they are repaired in the copy automatically." if not F["folders"] else
                         "For the folders you added this renames those files in your source folders (a warning, because it changes the originals' names).")})
    if media:
        recs.append({"id": "replace", "title": "Let Google's location and caption replace existing ones", "risk": "caution", "on": True,
                     "why": ("In your sample, %d files with an info file already have a location that differs from Google's. " % S["diff_gps"] if S.get("diff_gps") else "") +
                            "When a photo already has a different location or caption, Google's wins. Dates keep the earlier of the two. Untick this to only fill in what is missing."})
        recs.append({"id": "copy", "title": "Copy, do not move", "risk": "safe", "on": True, "fixed": True,
                     "why": "Guided always copies, so your originals stay untouched. " + (
                         "You have enough free space for that." if (free is None or free >= copy_need) else
                         "You do not have enough free space for a full copy here, so choose a bigger or external drive as the Destination. (On the Fix tab, Move saves space but empties your sources.)")})
    # ---------------------------------------------------------------- merging several libraries
    if multi:
        ov = F.get("overlap") or []
        lines = "; ".join("%s and %s share %s (%s)" % (o["a"], o["b"], pl(o["n"], "photo", "photos"), fmt_bytes(o["bytes"])) for o in sorted(ov, key=lambda o: -o["n"])[:4])
        recs.append({"id": "merge", "title": "Merge your %d libraries into one" % len(F["sources"]), "risk": "safe", "on": True, "fixed": True,
                     "why": "You added %d sources. Guided merges them into one library: same-named folders combine, identical photos are kept once, and different photos with the same name are both kept. %s The order of your Source list matters: when two sources hold the same folder name, the first one's name is used." % (
                         len(F["sources"]), ("Overlap found: " + lines + ".") if lines else "No photos appear in more than one of them.")})
    # ---------------------------------------------------------------- other workflows
    if F["legacy_n"]:
        extras.append({"id": "convert", "title": "Convert old videos to MP4", "tab": "convert", "risk": "caution",
                       "why": "%s old-format videos (%s) were found: %s. They play badly on phones and TVs, and Apple Photos cannot import most of them. Convert them after the library is built; the originals can be kept in a separate folder." % (
                           n(F["legacy_n"]), fmt_bytes(F["legacy_bytes"]), ", ".join("%s %s" % (n(v[0]), k) for k, v in sorted(F["legacy"].items(), key=lambda kv: -kv[1][0])[:5]))})
    if F.get("format_groups"):
        extras.append({"id": "formats", "title": "The same file in different formats", "tab": "health", "risk": "caution",
                       "why": "%s of files share a name but exist in more than one format (for example IMG_1.mov and IMG_1.mp4). This usually means a video was converted and the old copy kept. After the library is built, the Health tab compares their lengths and lets you set the older formats aside." % pl(F["format_groups"], "group", "groups")})
    if F.get("raw_pairs"):
        extras.append({"id": "rawpairs", "title": "RAW photos that also have a JPEG", "tab": "health", "risk": "safe",
                       "why": "%s a RAW file next to a JPEG or HEIC with the same name. Keeping both is normal if you edit RAW files. The Health tab shows how much space the RAW copies take; Backstory never deletes them for you." % pl(F["raw_pairs"], "photo has", "photos have")})
    if F.get("sim_groups"):
        extras.append({"id": "consolidate", "title": "Merge folders that are the same trip", "tab": "clean", "risk": "caution",
                       "why": "Found %s of folders with near-identical names (for example %s). Review them on the Clean up tab and merge the ones that are the same." % (
                           pl(F["sim_groups"], "group", "groups"), F.get("sim_example", ""))})
    if F["folders"] and (F["junk"] or F["empty_dirs"] or F["tidy_dirs"] or F["zero_other"]):
        bits = [x for x in (("%s junk or cache files" % n(F["junk"])) if F["junk"] else "", ("%s empty folders" % n(F["empty_dirs"])) if F["empty_dirs"] else "",
                            ("%s folders named like 'Folder (1)'" % n(F["tidy_dirs"])) if F["tidy_dirs"] else "", ("%s empty files" % n(F["zero_other"])) if F["zero_other"] else "") if x]
        extras.append({"id": "cleanup", "title": "Tidy your source folders", "tab": "clean", "risk": "caution",
                       "why": "Found " + ", ".join(bits) + ". The new library will not contain junk, so this is optional. If you want your source folders tidier, preview it on the Clean up tab."})
    if F["json"]:
        extras.append({"id": "rmjson", "title": "Later: remove the .json files", "tab": "clean", "risk": "caution",
                       "why": "%s Google info files (%s). Once your library is built and you have checked it, they are no longer needed in the library. Do not remove them from your Takeout until you are sure: they hold the only copy of the original dates." % (
                           n(F["json"]), fmt_bytes(F.get("json_bytes", 0)))})
    if media >= 50:
        extras.append({"id": "similar", "title": "Review similar photos", "tab": "similar", "risk": "safe",
                       "why": "Exact duplicates are handled for you, but the same picture saved at different sizes or re-saved is not an exact copy. After the library is built, the Similar tab finds those and lets you set aside the extras. Nothing is deleted."})
    if pre.get("mac") and pre.get("photos_app") and media:
        not_ok = sum(v for k, v in (F.get("by_ext_all") or {}).items() if k not in fx.PHOTOS_OK_EXT)
        low = free is not None and free < F["media_bytes"] * 1.1
        extras.append({"id": "photos", "title": "Send the finished library to Apple Photos", "tab": "photos", "risk": "caution",
                       "why": "Your library is about %s and this Mac has %s free. " % (fmt_bytes(F["media_bytes"]), fmt_bytes(pre["free"]) if pre.get("free") else "unknown space") + (
                           "That is not enough to import it all at once, so use the Photos tab: it sends oldest-first in batches and waits for iCloud to upload and macOS to free space between them. " if (pre.get("free") and pre["free"] < F["media_bytes"] * 1.2) else
                           "The Photos tab can send it in batches, oldest first, with album folders becoming albums. ") +
                           ("%s are in formats Photos cannot import; convert them first. " % pl(not_ok, "file", "files") if not_ok else "") +
                           "Photos has no undo for imports, so preview and send a small test first."})
    # ---------------------------------------------------------------- the suggested route
    step = 0

    def add(title, tab, why, kind="do"):
        nonlocal step
        step += 1
        flow.append({"n": step, "title": title, "tab": tab, "why": why, "kind": kind})
    add("Check my files", "guided", "Done: this is what you are reading.", "done")
    add("Build one clean library" if not multi else "Merge and build one clean library", "guided",
        "Restores dates, locations and captions, merges folders, removes duplicates, keeps albums. Preview first, then run it for real. Your originals stay untouched.")
    if F.get("sim_groups") or (F["folders"] and (F["tidy_dirs"] or F["empty_dirs"] or F["junk"])):
        add("Tidy folders", "clean", "Merge look-alike folders and clear junk, once the library exists.", "optional")
    if media >= 50 or F.get("format_groups"):
        add("Check the library's health", "health", "Space waste, duplicate formats, folder problems and ghost files, with a score you can track over time.", "optional")
    if media >= 50:
        add("Review similar photos", "similar", "Set aside the same picture saved twice. Nothing is deleted.", "optional")
    if F["legacy_n"]:
        add("Convert old videos", "convert", "Turn %s into MP4 so they play everywhere and can go into Photos." % pl(F["legacy_n"], "old video", "old videos"), "optional")
    if pre.get("mac") and pre.get("photos_app") and media:
        add("Send to Apple Photos", "photos", "In batches, with time for iCloud to catch up if space is tight.", "optional")
    if F["json"]:
        add("Remove the .json files from the finished library", "clean", "Only after you have checked everything. Optional.", "optional")
    return {"recs": recs, "extras": extras, "warnings": warns, "plan": plan, "flow": flow}


def run_assess(roots, dest):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Looking at your files...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="assess", cv=None, **({} if IN_GUIDED[0] else {"guided": None}))
    try:
        zips, folders = fx.split_sources(roots) if roots else ([], [])
        if not zips and not folders:
            raise ValueError("Add your Takeout zip files or folders in the bar at the top first")

        def prog(msg, done, total):
            check_cancel()
            with LOCK:
                STATE.update(state="running", message=msg + (" (%s of %s)" % (f"{done:,}", f"{total:,}") if total else ""))
                STATE["phase"] = {"stage": "pct", "done": done, "total": total} if total else None
        F = fx.assess(roots, dest, prog, check_cancel)
        F["by_ext_all"] = dict(F.get("by_ext", {}))
        F["photos_pre"] = photos_preflight()
        F["sim_groups"], F["sim_example"] = 0, ""
        if folders and not any(fx.inside_photos_library(f) for f in folders):
            try:
                with LOCK:
                    STATE["message"] = "Looking for look-alike folder names..."
                sg = fx.find_similar_folders([str(f) for f in folders], False)
                F["sim_groups"] = len(sg)
                if sg:
                    F["sim_example"] = ", ".join(m["name"] for m in sg[0]["members"][:3])
            except OSError:
                pass
        rec = build_recommendations(F, dest)
        tiles = [[F["media"], "photos and videos", ""], [F["matched"], "have an info file", "ok" if F["media"] and F["matched"] >= 0.6 * F["media"] else "bad"],
                 [F["dup_n"], "exact duplicates", ""], [F["legacy_n"], "old-format videos", ""], [F["extless"], "missing a file type", "bad" if F["extless"] else ""]]
        sm = {"kind": "assess", "dry_run": True, "facts": {k: v for k, v in F.items() if k not in ("by_ext",)}, "tiles": tiles, "size": fmt_bytes(F["media_bytes"]),
              "by_ext": sorted(F["by_ext"].items(), key=lambda kv: -kv[1])[:12], "dest": dest, "sources": F.get("sources", []), "overlap": F.get("overlap", []), **rec, "tips": []}
        with LOCK:
            STATE.update(state="done", message="Finished", summary=sm, phase=None)
    except Cancelled:
        stopped_state("Nothing was changed.")
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e))


def run_consolidate(groups, roots, dry_run, dupes_action):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=len(groups), done=0, counts={}, message="Merging similar folders...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="consolidate", cv=None, **({} if IN_GUIDED[0] else {"guided": None}))
    try:
        folders = check_clean_folders(roots)
        rootset = [f.resolve() for f in folders]
        ok = []
        for g in groups:
            par = Path(g.get("parent", "")).resolve()
            if not any(par == r or r in par.parents for r in rootset):
                raise ValueError("A folder to merge is outside the folders you chose: " + str(par))
            ok.append({"parent": str(par), "target": str(g.get("target", "")), "members": [str(m) for m in g.get("members", [])]})
        if not ok:
            raise ValueError("Tick at least one group to merge")

        def prog(stage, done, total):
            check_cancel()
            with LOCK:
                STATE.update(state="running", done=done, total=total, message="Merging similar folders (%d of %d)" % (done, total))
        rows = fx.consolidate_groups(ok, dupes_action, dry_run, prog, check_cancel)
        sm = {"kind": "consolidate", "dry_run": dry_run, "groups": len(rows),
              "merged": sum(1 for r in rows if r["action"] in ("merged", "would-merge")),
              "moved": sum(r["moved"] for r in rows), "dupes": sum(r["dupes"] for r in rows), "conflicts": sum(r["conflicts"] for r in rows),
              "failed": sum(1 for r in rows if r["action"] == "failed"),
              "rows": [{"parent": os.path.basename(r["parent"]) or r["parent"], "target": r["target"], "members": r["members"], "moved": r["moved"],
                        "dupes": r["dupes"], "conflicts": r["conflicts"], "action": r["action"], "detail": r["detail"]} for r in rows],
              "tips": []}
        if dry_run:
            sm["tips"].append("This was a preview: nothing was moved. Untick Preview only to merge these folders.")
        else:
            sm["tips"].append("Identical files were kept once (%s). Different files with the same name were kept as name_1. Merged folders are gone from their old places: this cannot be undone from the app." % (
                "the extra copy deleted" if dupes_action == "delete" else "the extra copy moved to a _duplicates folder"))
        with LOCK:
            STATE.update(state="done", message="Finished", summary=sm, phase=None)
    except Cancelled:
        stopped_state("Folders already merged stay merged.")
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e))


def _load_entry(rid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}x*", rid or ""):
        raise ValueError("bad id")
    return json.loads((HIST_DIR / (rid + ".json")).read_text(encoding="utf-8"))


def _read_undo(entry):
    u = entry.get("undo") or {}
    meta, pairs = {}, []
    with open(u["file"], encoding="utf-8") as fh:
        for k, line in enumerate(fh):
            d = json.loads(line)
            if k == 0:
                meta = d
            else:
                pairs.append(d)
    return meta, pairs


def undo_info(rid):
    try:
        e = _load_entry(rid)
        if not e.get("undo") or e["undo"].get("done"):
            return {"error": "This run cannot be undone (or already was)."}
        meta, pairs = _read_undo(e)
    except (OSError, ValueError, KeyError) as ex:
        return {"error": "Could not read the undo record: %s" % ex}
    exist = sum(1 for p in pairs if Path(p["dest"]).exists())
    return {"mode": meta.get("mode", "copy"), "total": len(pairs), "exist": exist, "dest": meta.get("dest", ""), "title": e["title"],
            "when": e["started"]}


def run_undo(rid):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Preparing to undo...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="undo", cv=None, guided=None)
    try:
        e = _load_entry(rid)
        if not e.get("undo") or e["undo"].get("done"):
            raise ValueError("This run cannot be undone (or already was).")
        meta, pairs = _read_undo(e)
        dest = Path(meta["dest"]).resolve()
        mode = meta.get("mode", "copy")
        res = {"removed": 0, "restored": 0, "missing": 0, "failed": 0, "skipped": 0}
        fails = []
        total = len(pairs)
        with LOCK:
            STATE.update(state="running", total=total)
        for k, p in enumerate(pairs, 1):
            check_cancel()
            dp = Path(p["dest"])
            try:
                rd = dp.resolve()
                if dest != rd and dest not in rd.parents:
                    res["skipped"] += 1                      # never touch anything outside the destination
                    continue
                if not dp.exists():
                    res["missing"] += 1
                elif mode == "move":
                    sp = Path(p["src"])
                    if sp.exists():
                        res["skipped"] += 1
                        fails.append("%s: the original place is occupied" % sp.name)
                    else:
                        sp.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dp), str(sp))
                        res["restored"] += 1
                else:
                    dp.unlink()
                    res["removed"] += 1
                    if p.get("json"):
                        j = Path(str(dp) + ".json")
                        if j.exists():
                            j.unlink()
            except OSError as ex:
                res["failed"] += 1
                fails.append("%s: %s" % (dp.name, ex))
            if k % 25 == 0 or k == total:
                with LOCK:
                    STATE.update(done=k, message="Undoing: %s of %s files" % (f"{k:,}", f"{total:,}"))
        # tidy: folders left empty by the undo (only the ones the undone files were in, never the destination itself)
        pruned = 0
        parents = sorted({str(Path(p["dest"]).parent) for p in pairs}, key=lambda x: -len(x))
        for d_ in parents:
            q = Path(d_)
            while q != dest and dest in q.parents:
                try:
                    q.rmdir()
                    pruned += 1
                except OSError:
                    break
                q = q.parent
        # forget the zips this run finished, so running again processes them
        zl = dest / fx.ZIPS_LOG
        if zl.exists():
            try:
                data = zl.read_bytes()[:meta.get("zips_before", 0)]
                zl.write_bytes(data)
            except OSError:
                pass
        e["undo"]["done"] = True
        e["undone_at"] = time.time()
        (HIST_DIR / (rid + ".json")).write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
        sm = {"kind": "undo", "dry_run": False, "mode": mode, "title": e["title"], "total": total, **res, "pruned": pruned, "fails": fails[:50],
              "tips": ["Your original photos and Takeout zip files were not touched." if mode == "copy" else
                       "Files were moved back to where they came from. Their metadata fixes were kept."]}
        with LOCK:
            STATE.update(state="done", message="Finished", summary=sm, phase=None)
    except Cancelled:
        stopped_state("Run it again to finish undoing.")
    except Exception as ex:
        with LOCK:
            STATE.update(state="error", message=str(ex))


SIMILAR = {"roots": [], "groups": [], "allowed": set()}


def make_thumb(path):
    """A small JPEG of a picture for the review screen (cached on disk)."""
    try:
        st = os.stat(path)
    except OSError:
        return b""
    key = hashlib.sha1(("%s|%s|%s" % (path, st.st_size, int(st.st_mtime))).encode()).hexdigest()
    THUMB_DIR = APP_HOME / "thumbs"
    cache = THUMB_DIR / (key + ".jpg")
    try:
        if cache.exists():
            return cache.read_bytes()
    except OSError:
        pass

    def run(cmd, data=None):
        try:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
            return r.stdout if r.returncode == 0 else b""
        except (OSError, subprocess.TimeoutExpired):
            return b""
    vf = "scale='min(260,iw)':-2"
    out = run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-frames:v", "1", "-vf", vf, "-q:v", "5", "-f", "mjpeg", "-"])
    if not out and shutil.which("exiftool"):
        th = run(["exiftool", "-b", "-ThumbnailImage", path]) or run(["exiftool", "-b", "-PreviewImage", path])
        if th:
            out = run(["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-vf", vf, "-q:v", "5", "-f", "mjpeg", "-"], th) or th
    if out:
        try:
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(out)
        except OSError:
            pass
    return out


def run_similar_scan(roots, threshold):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Looking for similar photos...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="similar", cv=None, guided=None)
    try:
        folders = check_clean_folders(roots)
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg is needed to compare pictures. In Terminal run: brew install ffmpeg (the Mac app includes it).")

        def prog(msg, done, total):
            check_cancel()
            with LOCK:
                STATE.update(state="running", total=total, done=done, message="%s (%s of %s)" % (msg, f"{done:,}", f"{total:,}"))
        groups, scanned = fx.find_similar_photos([str(f) for f in folders], threshold, prog, check_cancel)
        view, allowed = [], set()
        for gi, g in enumerate(groups[:300]):
            mem = []
            for k, m in enumerate(g):
                allowed.add(m["path"])
                rel = m["path"]
                for f in folders:
                    try:
                        rel = str(Path(m["path"]).relative_to(f.resolve() if False else f))
                        break
                    except ValueError:
                        continue
                mem.append({"path": m["path"], "name": os.path.basename(m["path"]), "where": os.path.dirname(rel) or ".", "size": m["size"],
                            "w": m["w"], "h": m["h"], "date": m["date"], "gps": m["gps"], "best": k == 0})
            view.append(mem)
        reclaim = sum(m["size"] for g in groups for m in g[1:])
        with LOCK:
            SIMILAR.update(roots=[str(f) for f in folders], groups=view, allowed=allowed)
        sm = {"kind": "similar", "dry_run": True, "scanned": scanned, "groups": view, "total_groups": len(groups),
              "extra": sum(len(g) - 1 for g in groups), "reclaim": fmt_bytes(reclaim), "threshold": threshold, "tips": []}
        with LOCK:
            STATE.update(state="done", message="Finished", summary=sm, phase=None)
    except Cancelled:
        stopped_state("Nothing was changed.")
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e))


def _setaside(items, store, folder, kind, nice):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=len(items), done=0, counts={}, message="Setting files aside...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind=kind, cv=None, guided=None)
    try:
        with LOCK:
            roots, allowed = [Path(r) for r in store["roots"]], set(store["allowed"])
        pairs, res, fails = [], {"moved": 0, "failed": 0, "skipped": 0}, []
        bases = set()
        for k, p in enumerate(items, 1):
            check_cancel()
            if p not in allowed or not os.path.exists(p) or fx.inside_photos_library(p):
                res["skipped"] += 1
                continue
            root = next((r for r in roots if r in Path(p).parents), None)
            if root is None:
                res["skipped"] += 1
                continue
            base = root / folder
            target = base / Path(p).relative_to(root)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                t = target if not target.exists() else Path(fx._free_name(str(target)))
                shutil.move(p, str(t))
                for sfx in (".json", ".supplemental-metadata.json"):
                    jp = Path(p + sfx)
                    if jp.exists():
                        shutil.move(str(jp), str(t) + sfx)
                pairs.append({"src": p, "dest": str(t), "json": 0})
                bases.add(str(base))
                res["moved"] += 1
            except OSError as ex:
                res["failed"] += 1
                fails.append("%s: %s" % (os.path.basename(p), ex))
            with LOCK:
                STATE.update(done=k, message="Setting files aside: %s of %s" % (k, len(items)))
        if pairs:
            with LOCK:
                STATE["undo_pairs"] = {"mode": "move", "pairs": pairs, "zips_before": 0, "dest": os.path.commonpath(list(bases)) if len(bases) > 1 else next(iter(bases))}
        sm = {"kind": "similar_apply", "dry_run": False, **res, "fails": fails[:30], "folders": sorted(bases),
              "tips": ["Nothing was deleted. The files were moved into a " + folder + " folder, keeping their folder structure. Review that folder, then delete it yourself when you are sure, or use Undo in History to put everything back."]}
        with LOCK:
            STATE.update(state="done", message="Finished", summary=sm, phase=None)
    except Cancelled:
        stopped_state("Photos already set aside stay there; use Undo in History to put them back.")
    except Exception as ex:
        with LOCK:
            STATE.update(state="error", message=str(ex))


OSA = os.environ.get("BACKSTORY_OSASCRIPT", "osascript")


def photos_preflight():
    app = next((p for p in ("/System/Applications/Photos.app", "/Applications/Photos.app") if os.path.exists(p)), "")
    try:
        free = shutil.disk_usage(Path.home()).free
    except OSError:
        free = None
    return {"mac": sys.platform == "darwin" or bool(os.environ.get("BACKSTORY_OSASCRIPT")), "osascript": bool(shutil.which(OSA)), "photos_app": bool(app),
            "free": free, "library": str(Path.home() / "Pictures" / "Photos Library.photoslibrary")}


def _upload_log_path():
    return APP_HOME / "upload.jsonl"


def upload_record(lib, st):
    if not st.get("ok") or st.get("pending") is None:
        return
    try:
        APP_HOME.mkdir(parents=True, exist_ok=True)
        with open(_upload_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "lib": str(lib), "total": st["total"], "uploaded": st["uploaded"], "pending": st["pending"]}) + "\n")
    except OSError:
        pass


def upload_eta(lib, window_h=4.0):
    """Upload speed, time left and whether it looks stuck, from the checks recorded so far."""
    pts = []
    try:
        for line in _upload_log_path().read_text(encoding="utf-8").splitlines()[-2000:]:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("lib") == str(lib) and time.time() - d["t"] <= window_h * 3600:
                pts.append(d)
    except OSError:
        return {}
    out = {"points": len(pts)}
    if len(pts) < 2:
        return out
    first, last = pts[0], pts[-1]
    dt = (last["t"] - first["t"]) / 3600.0
    if dt >= 0.08:
        rate = (first["pending"] - last["pending"]) / dt          # items per hour; negative when more is being added
        out["rate_per_hour"] = round(rate)
        if rate > 0 and last["pending"] > 0:
            out["eta_hours"] = round(last["pending"] / rate, 1)
    recent = [p for p in pts if last["t"] - p["t"] <= 45 * 60]
    if last["pending"] > 0 and len(recent) >= 3 and (recent[-1]["t"] - recent[0]["t"]) >= 30 * 60 and recent[0]["pending"] <= recent[-1]["pending"]:
        out["stalled"] = True
    return out


def adapt_batch(cur, seconds, lo=1e9, hi=50e9, fast=20 * 60, slow=3 * 3600):
    """Next batch size: bigger when iCloud kept up easily, smaller when the last batch took hours to upload."""
    if seconds < fast:
        return min(hi, cur * 1.5)
    if seconds > slow:
        return max(lo, cur * 0.5)
    return cur


def _wanted_for(files):
    out = []
    for f in files:
        try:
            out.append((os.path.basename(f), os.path.getsize(f)))
        except OSError:
            pass
    return out


def photos_verify_report(lib, root):
    """Check every file Backstory has sent so far against the Photos database."""
    log = Path(root) / fx.PHOTOS_LOG
    sent = []
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                sent.append(json.loads(line)["path"])
            except (ValueError, KeyError):
                pass
    except OSError:
        pass
    st = fx.photos_upload_status(lib, _wanted_for(sent) if sent else None)
    st["sent"] = len(sent)
    if st.get("ok"):
        upload_record(lib, st)
        st["eta"] = upload_eta(lib)
    return st


def run_photos(roots, opts, dry_run):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Planning the import...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="photos", cv=None, guided=None, photos=None)
        STATE.pop("photos_continue", None)
    try:
        folders = check_clean_folders(roots[:1])
        root = folders[0]
        batch_bytes = int(max(1e-6, float(opts.get("batch_gb", 10))) * 1e9)
        keep_free = int(max(0.0, float(opts.get("keep_free_gb", 20))) * 1e9)
        pace = opts.get("pace") if opts.get("pace") in ("verify", "space", "ask", "none") else "space"
        adaptive = bool(opts.get("adaptive", True)) and pace == "verify"
        lib = opts.get("library") or (fx.find_photos_libraries() or [str(Path.home() / "Pictures" / "Photos Library.photoslibrary")])[0]
        verify_pct = float(opts.get("verify_pct", 99)) / 100.0
        limit = int(opts["limit"]) if opts.get("limit") else None
        poll_s = 2 if os.environ.get("BACKSTORY_OSASCRIPT") else 60
        log = root / fx.PHOTOS_LOG
        done = set()
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    done.add(json.loads(line)["path"])
                except (ValueError, KeyError):
                    pass
        except OSError:
            pass
        order, albums = opts.get("order", "oldest"), bool(opts.get("albums", True))
        plan = fx.plan_photos_import(root, batch_bytes, order, albums, done, limit)
        batches = plan["batches"]
        pre = photos_preflight()
        pace_text = {"verify": "waits until Photos shows each batch as uploaded to iCloud", "space": "waits until your Mac has at least %s free" % fmt_bytes(keep_free),
                     "ask": "pauses until you press Continue", "none": "does not wait"}
        rows = [{"n": b["index"], "files": b["files"], "bytes": fmt_bytes(b["bytes"]), "from": b["first"], "to": b["last"],
                 "albums": ", ".join("%s (%d)" % kv for kv in list(b["albums"].items())[:4]), "status": "planned"} for b in batches]
        sm = {"kind": "photos", "dry_run": dry_run, "files": plan["files"], "bytes": fmt_bytes(plan["bytes"]), "batches": len(batches),
              "unsupported": sorted(plan["unsupported"].items(), key=lambda kv: -kv[1]), "already": plan["skipped_done"], "imported": 0, "failed": 0,
              "rows": rows[:300], "tips": [], "free": fmt_bytes(pre["free"]) if pre["free"] else "", "keep_free": fmt_bytes(keep_free), "pace": pace,
              "root": str(root), "limit": limit, "library": lib}
        if not batches:
            sm["tips"].append("Nothing left to import from this folder.")
        if dry_run:
            sm["tips"].append("This was a preview: nothing was sent to Photos. Each batch is about %s. Between batches Backstory %s.%s" % (
                fmt_bytes(batch_bytes), pace_text[pace], " The batch size adapts to how fast iCloud keeps up." if adaptive else ""))
            if pace == "verify":
                chk = fx.photos_upload_status(lib)
                sm["tips"].append(("Photos library found: %s items, %s uploaded to iCloud." % (f"{chk['total']:,}", f"{chk['uploaded']:,}") if chk.get("ok") and chk.get("uploaded") is not None else
                                   "Could not read the Photos database to verify uploads (%s). Backstory will fall back to waiting for free space." % chk.get("why", "unknown")))
            with LOCK:
                STATE.update(state="done", message="Finished", summary=sm, phase=None)
            return
        if not (pre["mac"] and pre["osascript"] and pre["photos_app"] or os.environ.get("BACKSTORY_OSASCRIPT")):
            raise ValueError("Sending to Apple Photos needs a Mac with the Photos app. Nothing was imported.")
        total = plan["files"]
        n_done = 0
        with LOCK:
            STATE.update(state="running", total=total, done=0)
        use_verify = pace == "verify" and fx.photos_upload_status(lib).get("ok")
        if pace == "verify" and not use_verify:
            sm["tips"].append("Backstory could not read the Photos database, so it waited for free space between batches instead of verifying uploads.")

        def free_now():
            return shutil.disk_usage(Path.home()).free

        def hold(mode_msg, cond, extra=None, cap_s=None):
            """Wait until cond() is true, the user presses Continue, or cap_s passes. Stop works throughout."""
            with LOCK:
                STATE["photos_continue"] = False
            t0 = time.time()
            while True:
                check_cancel()
                ok, info = cond()
                with LOCK:
                    cont = STATE.get("photos_continue")
                    STATE["photos"] = {"waiting": True, "batch": len(rows), **info}
                    STATE["message"] = mode_msg(info)
                if cont or ok:
                    break
                if cap_s and time.time() - t0 > cap_s:
                    return False
                time.sleep(poll_s)
            with LOCK:
                STATE["photos"] = {"waiting": False, "batch": len(rows)}
            return True

        def verify_batch(files, k):
            wanted = _wanted_for(files)
            t0 = time.time()

            def cond():
                st = fx.photos_upload_status(lib, wanted)
                if not st.get("ok"):
                    return False, {"verified": 0, "of": len(wanted), "note": st.get("why", "")}
                upload_record(lib, st)
                matched, up = st.get("matched", 0), st.get("matched_uploaded", 0)
                waited = time.time() - t0
                seen_enough = matched >= 0.9 * len(wanted) or waited > (15 * 60 if poll_s > 5 else 6)
                ok = bool(matched) and seen_enough and up >= verify_pct * matched
                eta = upload_eta(lib)
                return ok, {"verified": up, "of": matched or len(wanted), "eta": eta.get("eta_hours"), "stalled": bool(eta.get("stalled"))}

            def msg(info):
                s = "Batch %d sent. Verifying upload: %s of %s are in iCloud" % (k, f"{info.get('verified', 0):,}", f"{info.get('of', 0):,}")
                if info.get("stalled"):
                    s += ". Uploads look stuck: open the Monitor tab for likely causes."
                elif info.get("eta"):
                    s += ", about %s h left." % info["eta"]
                return s
            hold(msg, cond, cap_s=float(opts.get("verify_timeout_h", 12)) * 3600 if poll_s > 5 else 30)
            return time.time() - t0

        def wait_room(k, remaining):
            need_wait = remaining and ((pace == "ask") or (pace in ("space", "verify") and free_now() < keep_free))
            if not need_wait:
                return
            if pace == "ask":
                hold(lambda i: "Batch %d finished. Paused: press Continue when Photos has finished uploading." % k, lambda: (False, {"pace": "ask"}))
            else:
                hold(lambda i: "Waiting for room: %s free, %s wanted. macOS frees space as iCloud finishes uploading; this can take a while." % (fmt_bytes(i["free"]), fmt_bytes(keep_free)),
                     lambda: (free_now() >= keep_free, {"free": free_now(), "need": keep_free, "pace": pace}), cap_s=(20 * 60 if pace == "verify" and poll_s > 5 else None))

        if pace == "space" and free_now() < keep_free:
            wait_room(0, True)
        rows = []
        cur = batch_bytes
        k = 0
        while True:
            check_cancel()
            left = (limit - n_done) if limit else None
            if limit and left <= 0:
                break
            pl = fx.plan_photos_import(root, cur, order, albums, done, left)
            if not pl["batches"]:
                break
            b = pl["batches"][0]
            k += 1
            by_album = {}
            for u in b["units"]:
                by_album.setdefault(u["album"], []).extend(u["files"])
            sent_files = []
            for album, files in by_album.items():
                for j in range(0, len(files), 150):
                    check_cancel()
                    chunk = files[j:j + 150]
                    with LOCK:
                        STATE["photos"] = {"waiting": False, "batch": k}
                        STATE["message"] = "Batch %d: sending %d files to Photos%s" % (k, len(chunk), (" (album " + album + ")") if album else "")
                    script = fx.applescript_import(chunk, album)
                    try:
                        r = subprocess.run([OSA, "-"], input=script, capture_output=True, text=True, timeout=3 * 3600)
                    except subprocess.TimeoutExpired:
                        raise ValueError("Photos did not answer within three hours. Open Photos, check it is not stuck, and run this again: finished files are remembered.")
                    if r.returncode != 0:
                        err = (r.stderr or r.stdout or "").strip()[:400]
                        hint = " Allow it in System Settings > Privacy & Security > Automation, then run again." if "1743" in err or "not authorized" in err.lower() else ""
                        raise ValueError("Photos refused the import: %s.%s Files already imported are remembered, so running again carries on." % (err, hint))
                    with open(log, "a", encoding="utf-8") as fh:
                        for f in chunk:
                            fh.write(json.dumps({"path": f, "batch": k}, ensure_ascii=False) + "\n")
                    done.update(chunk)
                    sent_files += chunk
                    n_done += len(chunk)
                    with LOCK:
                        STATE.update(done=n_done, total=max(total, n_done))
            row = {"n": k, "files": len(sent_files), "bytes": fmt_bytes(b["bytes"]), "from": b["first"], "to": b["last"],
                   "albums": ", ".join("%s (%d)" % kv for kv in list(b["albums"].items())[:4]), "status": "sent"}
            rows.append(row)
            sm["imported"] = n_done
            remaining = bool(fx.plan_photos_import(root, cur, order, albums, done, (limit - n_done) if limit else None)["batches"])
            if use_verify:
                secs = verify_batch(sent_files, k)
                row["status"] = "verified in iCloud (%d min)" % round(secs / 60)
                if adaptive:
                    nxt = adapt_batch(cur, secs) if poll_s > 5 else cur
                    if nxt != cur:
                        row["status"] += "; next batch %s" % ("larger" if nxt > cur else "smaller")
                    cur = nxt
            wait_room(k, remaining)
            if not remaining:
                break
        sm["batches"], sm["rows"], sm["imported"] = len(rows), rows[:300], n_done
        if n_done:
            sm["tips"].append("%s files were sent to Photos. If iCloud Photos is on it uploads in the background, and with Optimize Mac Storage macOS keeps small copies once the originals are safely in iCloud. Leave Photos open until the upload finishes." % f"{n_done:,}")
        if plan["unsupported"]:
            sm["tips"].append("%s files in formats Photos cannot import were left out (%s). Convert them on the Convert tab, then run this again." % (
                f"{sum(plan['unsupported'].values()):,}", ", ".join(k_ for k_, _ in sm["unsupported"][:5])))
        with LOCK:
            STATE.update(state="done", message="Finished", summary=sm, phase=None, photos=None)
    except Cancelled:
        stopped_state("Files already sent are remembered; run it again to carry on.")
        with LOCK:
            STATE["photos"] = None
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e), photos=None)


def _monitor_log_path():
    return APP_HOME / "monitor.jsonl"


def run_monitor(hours, pasted):
    with LOCK:
        STATE.update(state="scanning", cancel=False, total=0, done=0, counts={}, message="Reading the Photos and iCloud logs...", report="",
                     summary=None, scan=None, extra={}, recent=[], phase=None, kind="monitor", cv=None, guided=None)
    try:
        lines, note, src = [], "", {}
        if pasted and pasted.strip():
            lines = pasted.splitlines()
            src["pasted"] = len(lines)
        else:
            lines, note = fx.collect_mac_logs(hours)
            src["mac_log"] = len(lines)
            check_cancel()
            crashes = fx.collect_crash_reports(max(1, hours // 24 + 1) if hours > 24 else 7)
            lines += crashes
            src["crash_reports"] = len(crashes)
            # Backstory's own recent problems
            mine = []
            for e in list_history(40):
                if e.get("state") in ("failed", "stopped") and e.get("message") and time.time() - e["started"] < max(hours, 24) * 3600:
                    mine.append("%s Backstory %s: %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["started"])), e["title"], e["message"]))
            lines += mine
            src["backstory"] = len(mine)
        issues, other = fx.interpret_log_lines(lines)
        prev = {}
        try:
            last = _monitor_log_path().read_text(encoding="utf-8").splitlines()[-1]
            prev = json.loads(last).get("issues", {})
        except (OSError, IndexError, ValueError):
            pass
        for i in issues:
            i["new"] = i["id"] not in prev and not pasted
        if not pasted:
            try:
                APP_HOME.mkdir(parents=True, exist_ok=True)
                with open(_monitor_log_path(), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": time.time(), "issues": {i["id"]: i["count"] for i in issues}}) + "\n")
            except OSError:
                pass
        sm = {"kind": "monitor", "dry_run": True, "issues": issues, "other": other, "note": note, "sources": src, "hours": hours, "pasted": bool(pasted and pasted.strip()),
              "lines": len(lines), "tips": []}
        if not issues and not other:
            sm["tips"].append("No problems found in the logs that were read." + (" (%s)" % note if note else ""))
        with LOCK:
            STATE.update(state="done", message="Finished", summary=sm, phase=None)
    except Cancelled:
        stopped_state("Nothing was changed.")
    except Exception as e:
        with LOCK:
            STATE.update(state="error", message=str(e))


# ---- Reports, run logs and history -------------------------------------------------------------------------
APP_HOME = Path(os.environ.get("METADATAFIXER_HOME") or (
    Path.home() / "Library" / "Application Support" / "MetadataFixer" if sys.platform == "darwin" else Path.home() / ".metadatafixer"))
HIST_DIR = APP_HOME / "history"
REPORTS_DIR = Path(os.environ.get("METADATAFIXER_REPORTS") or (Path.home() / "Documents" / "Backstory Reports"))
KIND_TITLE = {"monitor": "Photos and iCloud log check", "health": "Library health check", "formats_apply": "Set older formats aside", "photos": "Send to Apple Photos", "similar": "Find similar photos", "similar_apply": "Set similar photos aside", "undo": "Undo a run", "assess": "Check my files", "consolidate": "Merge similar folders", "fix": "Fix metadata", "merge": "Merge folders", "cleanup": "Clean up", "convert": "Convert videos", "guided": "Guided: Fix my Takeout"}


def _headline(sm):
    """One short line describing what a run did, from its summary."""
    if not isinstance(sm, dict):
        return ""
    k = sm.get("kind")
    try:
        if k == "monitor":
            return "%s issues found in %s log lines" % (len(sm.get("issues", [])), f"{sm.get('lines', 0):,}")
        if k == "health":
            return "Health score %s, %s findings" % (sm.get("score"), len(sm.get("findings", [])))
        if k == "formats_apply":
            return "%s older-format files set aside" % f"{sm.get('moved', 0):,}"
        if k == "photos":
            return ("%s files sent to Photos in %s batches" % (f"{sm.get('imported', 0):,}", sm.get("batches", 0))) if not sm.get("dry_run") else ("%s files planned in %s batches" % (f"{sm.get('files', 0):,}", sm.get("batches", 0)))
        if k == "similar":
            return "%s pictures checked, %s similar groups" % (f"{sm.get('scanned', 0):,}", f"{sm.get('total_groups', 0):,}")
        if k == "similar_apply":
            return "%s photos set aside" % f"{sm.get('moved', 0):,}"
        if k == "undo":
            return "%s files %s" % (f"{sm.get('removed', 0) + sm.get('restored', 0):,}", "moved back" if sm.get("mode") == "move" else "removed")
        if k == "consolidate":
            return "%s folder groups merged, %s files moved, %s duplicates" % (sm.get("merged", 0), f"{sm.get('moved', 0):,}", f"{sm.get('dupes', 0):,}")
        if k == "assess":
            return "%s files checked, %s recommendations" % (f"{sm['facts']['media']:,}", len(sm.get("recs", [])) + len(sm.get("extras", [])))
        if k == "guided":
            return "; ".join(h for h in (_headline(x.get("summary")) for x in sm.get("steps", [])) if h)
        if k == "merge":
            return "%s files brought in, %s identical copies, %s name clashes" % (f"{sm.get('brought', 0):,}", f"{sm.get('identical', 0):,}", f"{sm.get('clashes', 0):,}")
        if k == "convert":
            n = sm.get("converted") or sm.get("would") or 0
            return "%s videos %s" % (f"{n:,}", "converted" if sm.get("converted") else "would be converted")
        if k == "cleanup":
            tiles = [t for sec in sm.get("sections", []) for t in sec.get("tiles", []) if t and t[0]]
            return "; ".join("%s %s" % (f"{t[0]:,}", t[1]) for t in tiles[:3])
        c = sm.get("changes") or {}
        return "%s dates, %s locations, %s captions %s; %s duplicates skipped" % (
            f"{c.get('dates', 0):,}", f"{c.get('gps', 0):,}", f"{c.get('desc', 0):,}", "would change" if sm.get("dry_run") else "changed",
            f"{sm.get('duplicates', 0):,}")
    except Exception:
        return ""


def _tool_versions():
    out = {}
    for name, flag in (("exiftool", "-ver"), ("ffmpeg", "-version"), ("ffprobe", "-version")):
        exe = shutil.which(name)
        if not exe:
            out[name] = "not found"
            continue
        try:
            r = subprocess.run([exe, flag], capture_output=True, text=True, timeout=10)
            out[name] = " ".join(((r.stdout.splitlines() or ["?"])[0]).replace("version", "").split()[:3 if name != "exiftool" else 1])
        except Exception as e:
            out[name] = "error: %s" % e
    return out


def _problems(sm):
    """Problem rows from a summary, for the log."""
    if not isinstance(sm, dict):
        return []
    if sm.get("kind") == "guided":
        return [p for x in sm.get("steps", []) for p in _problems(x.get("summary"))]
    return [("%s: %s" % (p.get("file", ""), p.get("detail") or p.get("status", ""))) for p in (sm.get("problems") or sm.get("failures") or sm.get("clash_rows") or [])
            if p.get("status") in ("failed", "unreadable", "copy-error", "error", "exiftool-error")][:200]


UNDO_LOGS = (fx.MANIFEST, fx.MANIFEST_MERGE, fx.MANIFEST_SORT)


def _manifest_sizes(dest):
    out = {}
    for name in UNDO_LOGS + (fx.ZIPS_LOG,):
        try:
            out[name] = (Path(dest) / name).stat().st_size
        except OSError:
            out[name] = 0
    return out


def _collect_undo(dest, before, meta):
    """What this run placed in the destination (read from the progress logs the engine keeps), so it can be undone."""
    pairs = []
    opts = meta.get("options", {})
    moved = bool(opts.get("move"))
    for name in UNDO_LOGS:
        try:
            with open(Path(dest) / name, "rb") as fh:
                fh.seek(before.get(name, 0))
                data = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            src = d["src"].split("::", 1)[-1]
            pairs.append({"src": src, "dest": d["dest"], "json": 1 if (name == fx.MANIFEST_MERGE and opts.get("takeout")) else 0})
    if not pairs:
        return None
    return {"mode": "move" if moved else "copy", "pairs": pairs, "zips_before": before.get(fx.ZIPS_LOG, 0), "dest": str(dest)}


def write_run_record(run, timeline):
    """After a job ends: write its text log and a history entry."""
    with LOCK:
        st, msg, sm = STATE["state"], STATE["message"], STATE.get("summary")
    ended = time.time()
    state = {"done": "finished", "error": "failed"}.get(st, "stopped")
    title = KIND_TITLE.get(run["kind"], run["kind"])
    meta = run.get("meta", {})
    stamp = time.strftime("%Y-%m-%d %H.%M", time.localtime(run["started"]))
    folder = REPORTS_DIR / (f"{stamp} {title.split(':')[0]}" + (" (preview)" if meta.get("dry_run") else ""))
    n = 2
    while folder.exists():
        folder = REPORTS_DIR / (f"{stamp} {title.split(':')[0]}" + (" (preview)" if meta.get("dry_run") else "") + f" {n}")
        n += 1
    entry = {"id": run["id"], "kind": run["kind"], "title": title, "state": state, "message": msg if state != "finished" else "",
             "started": run["started"], "ended": ended, "duration": round(ended - run["started"], 1), "version": VERSION,
             "dry_run": bool(meta.get("dry_run")), "source": meta.get("source", []), "dest": meta.get("dest", ""),
             "options": meta.get("options", {}), "headline": _headline(sm) if state == "finished" else msg,
             "folder": "", "html": "", "log": ""}
    L = ["Backstory run log", "=" * 60,
         "Run:        %s%s" % (title, " (preview, nothing changed)" if entry["dry_run"] else ""),
         "Result:     %s%s" % (state.upper(), (" - " + msg) if entry["message"] else ""),
         "Started:    %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run["started"])),
         "Took:       %s" % fmt_dur(entry["duration"]),
         "Version:    %s (%s, Python %s%s)" % (VERSION, platform.platform(), platform.python_version(), ", packaged app" if FROZEN else ""),
         "Tools:      " + ", ".join("%s %s" % kv for kv in _tool_versions().items()),
         "Source:     " + "; ".join(entry["source"]),
         "Destination: " + (entry["dest"] or "(none)"),
         "Options:    " + ", ".join("%s=%s" % kv for kv in sorted(entry["options"].items())), "",
         "What happened", "-" * 60, entry["headline"] or "(no summary)", ""]
    for tip in (sm.get("tips", []) if isinstance(sm, dict) else []):
        L.append("* " + tip)
    probs = _problems(sm)
    L += ["", "Problems (%d)" % len(probs), "-" * 60] + (probs[:200] or ["none"])
    L += ["", "Timeline", "-" * 60]
    for t, tst, tmsg in timeline:
        L.append("%s  [%s] %s" % (time.strftime("%H:%M:%S", time.localtime(t)), tst, tmsg))
    L.append("%s  [end] %s" % (time.strftime("%H:%M:%S", time.localtime(ended)), state))
    L += ["", "Backstory is free software provided as is, without warranty. Back up your photos and read each preview before a real run.",
          "Support: " + SUPPORT_EMAIL]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "run.log").write_text("\n".join(L) + "\n", encoding="utf-8")
        entry["folder"], entry["log"] = str(folder), str(folder / "run.log")
    except OSError:
        pass
    und = run.get("undo")
    if und and entry["folder"]:
        try:
            with open(folder / "undo.jsonl", "w", encoding="utf-8") as fh:
                fh.write(json.dumps({k: v for k, v in und.items() if k != "pairs"}) + "\n")
                for p in und["pairs"]:
                    fh.write(json.dumps(p, ensure_ascii=False) + "\n")
            entry["undo"] = {"mode": und["mode"], "count": len(und["pairs"]), "file": str(folder / "undo.jsonl"), "done": False}
        except OSError:
            pass
    try:
        HIST_DIR.mkdir(parents=True, exist_ok=True)
        (HIST_DIR / (run["id"] + ".json")).write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    with LOCK:
        STATE["run"] = dict(run, saved=True)


def tracked(kind, meta, fn, *args):
    """Run a job and record it: text log, history entry (the page adds the HTML report when it sees the result)."""
    rid = time.strftime("%Y%m%d-%H%M%S")
    while (HIST_DIR / (rid + ".json")).exists():
        rid += "x"
    run = {"id": rid, "kind": kind, "started": time.time(), "meta": meta}
    with LOCK:
        STATE["run"] = dict(run, saved=False)
    undo_dest = Path(meta["dest"]).expanduser() if (kind in ("fix", "guided", "merge") and meta.get("dest") and not meta.get("dry_run")) else None
    before = _manifest_sizes(undo_dest) if undo_dest else {}
    stop, timeline = threading.Event(), []

    def sampler():
        last, lastt = None, 0
        while not stop.wait(1.0):
            with LOCK:
                msg, st = STATE["message"], STATE["state"]
            key = re.sub(r"[\d,.]+", "#", msg)
            if key != last or time.time() - lastt > 120:
                timeline.append((time.time(), st, msg))
                last, lastt = key, time.time()
    threading.Thread(target=sampler, daemon=True).start()
    try:
        fn(*args)
    except Exception as e:                      # the job functions handle their own errors; this is a safety net
        with LOCK:
            STATE.update(state="error", message=str(e))
    finally:
        stop.set()
        try:
            if undo_dest:
                run["undo"] = _collect_undo(undo_dest, before, meta)
            with LOCK:
                up = STATE.pop("undo_pairs", None)
            if up:
                run["undo"] = up
            write_run_record(run, timeline)
        except Exception:
            pass


def start_tracked(kind, body, fn, args):
    opts = body.get("opts") or {k: v for k, v in body.items() if k not in ("roots", "out", "dest", "dry_run", "opts")}
    meta = {"dry_run": bool(body.get("dry_run")), "source": [str(x) for x in body.get("roots", [])],
            "dest": body.get("out") or body.get("dest") or "", "options": {k: (v if isinstance(v, (bool, int, float, str)) else str(v)) for k, v in opts.items()}}
    threading.Thread(target=tracked, daemon=True, args=(kind, meta, fn) + tuple(args)).start()


def list_history(limit=200):
    out = []
    try:
        for p in sorted(HIST_DIR.glob("*.json"), reverse=True)[:limit]:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return out


def _page_css():
    m = re.search(r"<style>(.*?)</style>", PAGE, re.S)
    return m.group(1) if m else ""


def save_report_html(rid, body_html):
    """Write the report the page rendered as a standalone, printable HTML file."""
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}x*", rid or ""):
        return {"error": "bad id"}
    p = HIST_DIR / (rid + ".json")
    try:
        e = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": "unknown run"}
    folder = Path(e.get("folder") or "")
    if not e.get("folder"):
        return {"error": "no report folder"}
    meta_rows = [("Run", e["title"] + (" (preview: nothing was changed)" if e["dry_run"] else "")),
                 ("Result", e["state"].capitalize() + ((": " + e["message"]) if e["message"] else "")),
                 ("When", time.strftime("%A %d %B %Y, %H:%M", time.localtime(e["started"])) + " &middot; took " + fmt_dur(e["duration"])),
                 ("From", "<br>".join(html_escape(x) for x in e["source"]) or "-"), ("To", html_escape(e["dest"]) or "-"),
                 ("Version", html_escape(e["version"]))]
    doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>Backstory report</title><style>' + _page_css() +
           'body{padding:32px 20px}main{max-width:860px}.rhead{margin-bottom:18px}.rhead h1{font-size:26px}.rmeta{width:100%;margin:10px 0 4px}'
           '.rmeta td:first-child{width:90px;color:var(--mute)}.rfoot{margin-top:28px;color:var(--mute);font-size:12px}'
           '@media print{body{background:#fff;color:#000;padding:0}.card,.tile{break-inside:avoid}}</style></head><body><main>'
           '<div class="rhead"><h1>Backstory report</h1><div class="card"><table class="rmeta">' +
           "".join("<tr><td>%s</td><td>%s</td></tr>" % kv for kv in meta_rows) + '</table></div></div>' + body_html +
           '<p class="rfoot">Made by Backstory on your computer. Nothing was uploaded. The full text log is saved next to this report (run.log). Provided as is, without warranty: keep backups of your originals. Support: ' + SUPPORT_EMAIL + '</p></main></body></html>')
    try:
        (folder / "report.html").write_text(doc, encoding="utf-8")
        e["html"] = str(folder / "report.html")
        p.write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
    except OSError as ex:
        return {"error": str(ex)}
    return {"ok": True, "path": e["html"]}


def open_path(rid, what):
    try:
        e = json.loads((HIST_DIR / (rid + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": "unknown run"}
    target = {"report": e.get("html"), "log": e.get("log"), "folder": e.get("folder")}.get(what)
    if not target or not Path(target).exists():
        return {"error": "That file is not there any more"}
    opener = "open" if sys.platform == "darwin" else ("xdg-open" if shutil.which("xdg-open") else None)
    if not opener:
        return {"error": "Cannot open files on this system. Path: " + target}
    subprocess.Popen([opener, target])
    return {"ok": True}


def diagnostics_text():
    with LOCK:
        st = {k: STATE.get(k) for k in ("state", "kind", "message")}
    L = ["Backstory diagnostics", "Version: %s" % VERSION, "System: %s, Python %s%s" % (platform.platform(), platform.python_version(), " (packaged app)" if FROZEN else ""),
         "Tools: " + ", ".join("%s %s" % kv for kv in _tool_versions().items()), "Now: %s / %s / %s" % (st["state"], st["kind"], st["message"]), "", "Recent runs:"]
    for e in list_history(8):
        L.append("- %s  %s  %s%s  %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(e["started"])), e["title"], e["state"],
                                        " (preview)" if e["dry_run"] else "", (e.get("headline") or "")[:120]))
    last = next((e for e in list_history(8) if e.get("log")), None)
    if last:
        try:
            tail = Path(last["log"]).read_text(encoding="utf-8").splitlines()[-25:]
            L += ["", "End of the latest log (%s):" % last["log"]] + tail
        except OSError:
            pass
    L += ["", "Paths are included above; remove anything private before sharing."]
    return "\n".join(L)


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
        elif self.path.startswith("/thumb?"):
            if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
                return self._send(403, "{}")
            from urllib.parse import parse_qs, urlparse
            p = (parse_qs(urlparse(self.path).query).get("p") or [""])[0]
            with LOCK:
                ok = p in SIMILAR["allowed"]
            data = make_thumb(p) if ok else b""
            if not data:
                return self._send(404, "{}")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        # only accept requests from our own page
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, "{}")
        if self.path == "/api/choose":
            pick = choose_zips if body.get("kind") == "zip" else choose_folders
            self._send(200, json.dumps({"paths": pick(body.get("prompt", "Choose folders"))}))
        elif self.path == "/api/start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running")
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            start_tracked('fix', body, run_job, (
                body.get("roots", []), body.get("out", ""),
                bool(body.get("dry_run")), bool(body.get("overwrite")), bool(body.get("pair_live")),
                bool(body.get("dedupe")), bool(body.get("move")), body.get("date_policy", "earlier"), bool(body.get("name_dates")), bool(body.get("albums")),
                body.get("edited") if body.get("edited") in ("both", "edited", "original") else "both"))
            self._send(200, "{}")
        elif self.path == "/api/health_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            start_tracked("health", dict(body, dry_run=True, opts={"deep": bool(body.get("deep"))}), run_health, (body.get("roots", []), bool(body.get("deep"))))
            self._send(200, "{}")
        elif self.path == "/api/health_history":
            self._send(200, json.dumps({"runs": health_history(body.get("roots", []))}))
        elif self.path == "/api/formats_apply":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            start_tracked("formats_apply", {"roots": [], "dry_run": False, "opts": {"files": len(body.get("items", []))}}, run_formats_apply, ([str(x) for x in body.get("items", [])],))
            self._send(200, "{}")
        elif self.path == "/api/monitor_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            hrs = body.get("hours") if body.get("hours") in (1, 6, 24, 168) else 6
            pasted = str(body.get("pasted") or "")[:2_000_000]
            start_tracked("monitor", {"roots": [], "dry_run": True, "opts": {"hours": hrs, "pasted": bool(pasted)}}, run_monitor, (hrs, pasted))
            self._send(200, "{}")
        elif self.path == "/api/photos_libs":
            self._send(200, json.dumps({"libs": fx.find_photos_libraries()}))
        elif self.path == "/api/upload_status":
            lib = body.get("library") or (fx.find_photos_libraries() or [""])[0]
            if not lib:
                return self._send(200, json.dumps({"ok": False, "why": "no Photos library found"}))
            st = photos_verify_report(lib, body["root"]) if body.get("root") else fx.photos_upload_status(lib)
            if st.get("ok") and not body.get("root"):
                upload_record(lib, st)
                st["eta"] = upload_eta(lib)
            st["library"] = lib
            self._send(200, json.dumps(st))
        elif self.path == "/api/photos_preflight":
            self._send(200, json.dumps(photos_preflight()))
        elif self.path == "/api/photos_continue":
            with LOCK:
                STATE["photos_continue"] = True
            self._send(200, "{}")
        elif self.path == "/api/photos_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            o = body.get("opts", {})
            opts = {"batch_gb": o.get("batch_gb", 10), "keep_free_gb": o.get("keep_free_gb", 20), "pace": o.get("pace", "space"),
                    "albums": bool(o.get("albums", True)), "order": o.get("order", "oldest"), "limit": o.get("limit"),
                    "library": o.get("library") or "", "adaptive": bool(o.get("adaptive", True)), "verify_pct": o.get("verify_pct", 99)}
            start_tracked("photos", dict(body, opts=opts), run_photos, (body.get("roots", []), opts, bool(body.get("dry_run"))))
            self._send(200, "{}")
        elif self.path == "/api/similar_scan":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            thr = body.get("threshold") if body.get("threshold") in (3, 6, 10) else 6
            start_tracked("similar", dict(body, dry_run=True, opts={"sensitivity": thr}), run_similar_scan, (body.get("roots", []), thr))
            self._send(200, "{}")
        elif self.path == "/api/similar_apply":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            start_tracked("similar_apply", {"roots": [], "dry_run": False, "opts": {"photos": len(body.get("items", []))}}, run_similar_apply, ([str(x) for x in body.get("items", [])],))
            self._send(200, "{}")
        elif self.path == "/api/undo_info":
            self._send(200, json.dumps(undo_info(body.get("id", ""))))
        elif self.path == "/api/undo_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            start_tracked("undo", {"roots": [], "dry_run": False, "opts": {"run": body.get("id", "")}}, run_undo, (body.get("id", ""),))
            self._send(200, "{}")
        elif self.path == "/api/similar_folders":
            try:
                folders = check_clean_folders(body.get("roots", []))
                self._send(200, json.dumps({"groups": fx.find_similar_folders([str(f) for f in folders], bool(body.get("maybe", True)))}))
            except ValueError as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif self.path == "/api/consolidate_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            dup = body.get("dupes", "delete") if body.get("dupes") in ("delete", "aside") else "delete"
            start_tracked("consolidate", dict(body, opts={"dupes": dup}), run_consolidate,
                          (body.get("groups", []), body.get("roots", []), bool(body.get("dry_run")), dup))
            self._send(200, "{}")
        elif self.path == "/api/assess_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            b2 = dict(body, dry_run=True)
            start_tracked("assess", b2, run_assess, (body.get("roots", []), body.get("out", "")))
            self._send(200, "{}")
        elif self.path == "/api/guided_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            o = body.get("opts", {})
            opts = {k: bool(o.get(k)) for k in ("fix_ext", "convert", "replace", "live", "dedupe", "name_dates", "albums")}
            opts["edited"] = o.get("edited") if o.get("edited") in ("both", "edited", "original") else "both"
            start_tracked('guided', body, run_guided, (
                body.get("roots", []), body.get("out", ""), bool(body.get("dry_run")), opts))
            self._send(200, "{}")
        elif self.path == "/api/guide":
            self._send(200, json.dumps({"guide": read_doc("USER_GUIDE.md"), "notices": read_doc("THIRD_PARTY_NOTICES.md"),
                                        "license": read_doc("LICENSE"), "email": SUPPORT_EMAIL, "version": VERSION}))
        elif self.path == "/api/doctor":
            self._send(200, json.dumps(doctor(body.get("roots", []), body.get("dest", ""))))
        elif self.path == "/api/save_report":
            self._send(200, json.dumps(save_report_html(body.get("id", ""), body.get("html", ""))))
        elif self.path == "/api/history":
            self._send(200, json.dumps({"runs": list_history(), "dir": str(REPORTS_DIR)}))
        elif self.path == "/api/open_run":
            self._send(200, json.dumps(open_path(body.get("id", ""), body.get("what", "report"))))
        elif self.path == "/api/open_reports":
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            opener = "open" if sys.platform == "darwin" else ("xdg-open" if shutil.which("xdg-open") else None)
            if opener:
                subprocess.Popen([opener, str(REPORTS_DIR)])
            self._send(200, "{}")
        elif self.path == "/api/diagnostics":
            self._send(200, json.dumps({"text": diagnostics_text()}))
        elif self.path == "/api/update_check":
            with LOCK:
                STATE["update"] = {"state": "checking", "files": []}
            threading.Thread(target=check_update, daemon=True).start()
            self._send(200, "{}")
        elif self.path == "/api/update":
            self._send(200, json.dumps(apply_update()))
        elif self.path == "/api/cancel":
            with LOCK:
                if STATE["state"] in ("scanning", "running"):
                    STATE["cancel"] = True
            self._send(200, "{}")
        elif self.path == "/api/merge_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            o = body.get("opts", {})
            opts = {"move": bool(o.get("move")), "conflict": o.get("conflict", "both"), "dupes": o.get("dupes", "delete"),
                    "tidy": bool(o.get("tidy")), "takeout": bool(o.get("takeout")), "global_dedupe": bool(o.get("global_dedupe")), "nocase": bool(o.get("nocase", True)), "prune": True}
            start_tracked('merge', body, run_merge, (
                body.get("roots", []), body.get("dest", ""), opts, bool(body.get("dry_run"))))
            self._send(200, "{}")
        elif self.path == "/api/convert_scan":
            try:
                folders = check_clean_folders(body.get("roots", []))
                self._send(200, json.dumps({"types": fx.count_legacy(folders)}))
            except ValueError as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif self.path == "/api/cleanup_start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running") or STATE["clean"].get("state") == "running"
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            o = body.get("opts", {})
            opts = {"ext": o.get("ext") or None, "json": bool(o.get("json")), "json_other": bool(o.get("json_other")), "junk": list(o.get("junk") or []),
                    "names": o.get("names") or None, "empty": o.get("empty") or None}
            opts["junk"] = opts["junk"] or None
            start_tracked('cleanup', body, run_cleanup, (body.get("roots", []), bool(body.get("dry_run")), opts))
            self._send(200, "{}")
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
            start_tracked('convert', body, run_convert, (
                body.get("roots", []), bool(body.get("dry_run")), body.get("exts", []),
                bool(body.get("include_live")), body.get("quality", "high"), body.get("action", "move"), bool(body.get("estimate"))))
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
<title>Backstory</title><link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDI0IDEwMjQiIHdpZHRoPSIxMDI0IiBoZWlnaHQ9IjEwMjQiPgo8ZGVmcz4KPGxpbmVhckdyYWRpZW50IGlkPSJiZyIgeDE9IjAiIHkxPSIwIiB4Mj0iMSIgeTI9IjEiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzViNWJmMCIvPjxzdG9wIG9mZnNldD0iLjU1IiBzdG9wLWNvbG9yPSIjN2E0ZGYwIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjMTRiOGM0Ii8+PC9saW5lYXJHcmFkaWVudD4KPGxpbmVhckdyYWRpZW50IGlkPSJza3kiIHgxPSIwIiB5MT0iMCIgeDI9IjAiIHkyPSIxIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiM4ZmQzZmYiLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiNmZmU2YjMiLz48L2xpbmVhckdyYWRpZW50Pgo8ZmlsdGVyIGlkPSJzaCIgeD0iLTIwJSIgeT0iLTIwJSIgd2lkdGg9IjE0MCUiIGhlaWdodD0iMTUwJSI+PGZlRHJvcFNoYWRvdyBkeD0iMCIgZHk9IjE4IiBzdGREZXZpYXRpb249IjIyIiBmbG9vZC1jb2xvcj0iIzFhMTA1MCIgZmxvb2Qtb3BhY2l0eT0iLjM1Ii8+PC9maWx0ZXI+CjxmaWx0ZXIgaWQ9InNoMiIgeD0iLTMwJSIgeT0iLTMwJSIgd2lkdGg9IjE2MCUiIGhlaWdodD0iMTcwJSI+PGZlRHJvcFNoYWRvdyBkeD0iMCIgZHk9IjgiIHN0ZERldmlhdGlvbj0iMTAiIGZsb29kLWNvbG9yPSIjM2ExMDAwIiBmbG9vZC1vcGFjaXR5PSIuMzUiLz48L2ZpbHRlcj4KPC9kZWZzPgo8cmVjdCB3aWR0aD0iMTAyNCIgaGVpZ2h0PSIxMDI0IiByeD0iMjMwIiBmaWxsPSJ1cmwoI2JnKSIvPgo8Y2lyY2xlIGN4PSI4NjAiIGN5PSIxNzAiIHI9IjIzMCIgZmlsbD0iI2ZmZiIgb3BhY2l0eT0iLjA3Ii8+CjxnIHRyYW5zZm9ybT0icm90YXRlKC03IDQ4MCA1MjApIiBmaWx0ZXI9InVybCgjc2gpIj4KICA8cmVjdCB4PSIxOTAiIHk9IjE1MCIgd2lkdGg9IjYwMCIgaGVpZ2h0PSI3MjAiIHJ4PSIzNCIgZmlsbD0iI2ZmZmRmOCIvPgogIDxyZWN0IHg9IjI0MCIgeT0iMjAwIiB3aWR0aD0iNTAwIiBoZWlnaHQ9IjQ3MCIgcng9IjE0IiBmaWxsPSJ1cmwoI3NreSkiLz4KICA8Y2lyY2xlIGN4PSI2MTAiIGN5PSIzMjAiIHI9IjYyIiBmaWxsPSIjZmZiMzQ3Ii8+CiAgPHBhdGggZD0iTTI0MCA2MDAgTDQwMCA0MzAgTDUwMCA1NDAgTDU5MCA0NTAgTDc0MCA2MTAgTDc0MCA2NTYgYTE0IDE0IDAgMCAxIC0xNCAxNCBMMjU0IDY3MCBhMTQgMTQgMCAwIDEgLTE0IC0xNCBaIiBmaWxsPSIjM2Y2ZmQ4Ii8+CiAgPHBhdGggZD0iTTI0MCA2NDAgTDM2MCA1NDAgTDQ3MCA2MzAgTDU2MCA1NjAgTDc0MCA2NTAgTDc0MCA2NTYgYTE0IDE0IDAgMCAxIC0xNCAxNCBMMjU0IDY3MCBhMTQgMTQgMCAwIDEgLTE0IC0xNCBaIiBmaWxsPSIjMmM0ZmE4Ii8+CiAgPHJlY3QgeD0iMjUwIiB5PSI3MjIiIHdpZHRoPSIzMzAiIGhlaWdodD0iMjYiIHJ4PSIxMyIgZmlsbD0iI2M5Y2JlMCIvPgogIDxyZWN0IHg9IjI1MCIgeT0iNzcyIiB3aWR0aD0iMjIwIiBoZWlnaHQ9IjI2IiByeD0iMTMiIGZpbGw9IiNkY2RkZWQiLz4KPC9nPgo8ZyBmaWx0ZXI9InVybCgjc2gyKSIgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoNjkwIDYxMCkiPgogIDxwYXRoIGQ9Ik0wIC0xNTAgQy04NCAtMTUwIC0xNDYgLTg4IC0xNDYgLTEwIEMtMTQ2IDc4IC01MCAxNTAgMCAyMzIgQzUwIDE1MCAxNDYgNzggMTQ2IC0xMCBDMTQ2IC04OCA4NCAtMTUwIDAgLTE1MCBaIiBmaWxsPSIjZmY1YTRlIiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMjIiLz4KICA8Y2lyY2xlIGN4PSIwIiBjeT0iLTEwIiByPSI1NCIgZmlsbD0iI2ZmZiIvPgogIDxwYXRoIGQ9Ik0tMjYgLTggbDIwIDIyIGwzOCAtNDYiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmNWE0ZSIgc3Ryb2tlLXdpZHRoPSIyMCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CjwvZz4KPC9zdmc+Cg==">
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
.frow{display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap;font-size:13px}.frow .flabel{white-space:nowrap}
.frow .fchips{flex:1 1 160px;margin-top:0;min-width:0}.frow:not(:first-child) input[type=text]{flex:1 1 160px;min-width:0;padding:5px 8px;font-size:12px}
.sub{display:block;margin-top:7px;font-size:13px;color:var(--ink);line-height:1.4}.sub input{margin-right:6px}
.vtypes{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:8px}.vt{font-size:13px;white-space:nowrap}.vt .vc{color:var(--mute)}
.hrun .hhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.hwhen{margin-left:auto;color:var(--mute);font-size:13px}.hline{margin:6px 0 2px}.hbtns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}.badge{padding:1px 9px;border-radius:999px;background:var(--line);font-size:12px}.badge.okb{background:color-mix(in srgb,var(--ok) 22%,var(--card));color:var(--ok)}.badge.badb{background:color-mix(in srgb,var(--bad) 22%,var(--card));color:var(--bad)}
.cvbox{border:1px solid var(--line);border-radius:12px;padding:12px;margin:0 0 12px;background:var(--bg)}
.cvhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.cvhead .mode{padding:2px 9px;border-radius:999px;background:var(--acc);color:#fff;font-size:12px}
.cvname{font-weight:600;margin:6px 0 2px;word-break:break-all}.cvmeta{color:var(--mute);font-size:13px;margin-top:6px}
.mbar{height:16px;background:var(--line);border-radius:9px;overflow:hidden;position:relative}.mbar>i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),#7c5cff);transition:width .3s}.mbar>span{position:absolute;right:8px;top:0;line-height:16px;font-size:11px;font-weight:700;color:var(--ink)}
.cvgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-top:10px}.cvgrid>div{border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:var(--card)}.cvgrid b{display:block;font-size:16px;font-variant-numeric:tabular-nums}.cvgrid span{color:var(--mute);font-size:12px}
.cvrecent{font-size:13px;margin-top:10px}.cvrecent>div{padding:2px 0;display:flex;gap:8px;flex-wrap:wrap}
.hero .fbar{margin-top:14px;padding-top:10px;border-top:1px solid var(--line)}
.opt>div{min-width:0}.sel{max-width:100%}
.info{display:inline-grid;place-items:center;width:17px;height:17px;margin-left:7px;border-radius:50%;border:1px solid var(--mute);color:var(--mute);font:700 11px/1 system-ui,sans-serif;cursor:help;background:transparent;padding:0;vertical-align:middle;flex:none}
.info:hover,.info:focus-visible{background:var(--acc);border-color:var(--acc);color:#fff;outline:none}
#tip{position:fixed;z-index:200;max-width:min(380px,calc(100vw - 20px));background:var(--ink);color:var(--bg);padding:10px 12px;border-radius:10px;font-size:13px;line-height:1.45;box-shadow:0 8px 28px rgba(0,0,0,.28);display:none;pointer-events:none}
.warn{color:var(--warn);font-size:13px}.rknote{font-size:13px;color:var(--mute);margin:6px 0}.subline{margin:-6px 0 12px;color:var(--mute);font-size:14px}
.opt>div{line-height:1.35}

/* ---- v2 polish ---- */
:root{--r:14px;--shadow:0 1px 2px rgba(15,23,42,.05),0 8px 24px -12px rgba(15,23,42,.18);--acc2:#7c5cff;--soft:color-mix(in srgb,var(--acc) 8%,var(--card));--ring:0 0 0 3px color-mix(in srgb,var(--acc) 30%,transparent)}
@media (prefers-color-scheme:dark){:root{--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);--soft:color-mix(in srgb,var(--acc) 12%,var(--card))}}
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;letter-spacing:-.003em}
main{max-width:820px}
h1,h2{letter-spacing:-.018em}
.card{border-radius:var(--r);padding:20px 22px;box-shadow:var(--shadow);border-color:color-mix(in srgb,var(--line) 70%,transparent)}
.ph{font-size:22px;font-weight:700;margin:6px 0 4px}
.hero{border-radius:20px;box-shadow:var(--shadow);padding:22px 24px 18px;background:radial-gradient(900px 220px at 0% 0%,color-mix(in srgb,var(--acc) 20%,var(--card)),var(--card) 70%)}
.hero h1{font-size:28px;font-weight:800}
.logo{filter:drop-shadow(0 6px 14px rgba(79,140,255,.45))}
#frame{padding-top:12px}
.tabs{gap:4px;padding:4px;border:1px solid var(--line);border-radius:999px;background:var(--card);box-shadow:var(--shadow);width:100%}
.tab{flex:1 1 auto;justify-content:center;border:0;background:transparent;padding:9px 12px;font-weight:500;color:var(--mute);transition:background .15s,color .15s}
.tab:hover{color:var(--ink);background:var(--soft)}
.tab b{background:none!important;width:auto!important;height:auto!important;font-size:16px;color:inherit!important}
.tab.on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;box-shadow:0 4px 12px -4px color-mix(in srgb,var(--acc) 70%,transparent)}
.tab.on:hover{color:#fff;background:linear-gradient(135deg,var(--acc),var(--acc2))}
button{border-radius:10px;transition:background .15s,border-color .15s,box-shadow .15s,transform .05s}
button:hover:not(:disabled){border-color:var(--acc);background:var(--soft)}
button:active:not(:disabled){transform:translateY(1px)}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:none;box-shadow:var(--ring)}
button.p{background:linear-gradient(135deg,var(--acc),var(--acc2));border:0;padding:12px 22px;font-size:16px;border-radius:12px;box-shadow:0 6px 16px -6px color-mix(in srgb,var(--acc) 80%,transparent)}
button.p:hover:not(:disabled){background:linear-gradient(135deg,var(--acc),var(--acc2));filter:brightness(1.08);border:0}
input[type=checkbox]{accent-color:var(--acc);width:16px;height:16px;margin-top:3px;flex:none}
.opt{padding:7px 12px;margin:2px -12px;border-radius:10px;transition:background .12s}
.opt:hover{background:var(--soft)}
.sub{padding:3px 0}
.usef{border-radius:10px;background:var(--soft);border:1px solid color-mix(in srgb,var(--acc) 18%,var(--line));border-style:solid}
.tiles{gap:12px}
.tile{border-radius:12px;padding:14px 16px;background:var(--card);box-shadow:var(--shadow);border:1px solid color-mix(in srgb,var(--line) 70%,transparent);position:relative;overflow:hidden}
.tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(var(--acc),var(--acc2))}
.tile.ok::before{background:var(--ok)}.tile.bad::before{background:var(--bad)}
.tile b{font-size:28px;font-weight:700;letter-spacing:-.02em}
.tile span{font-size:12.5px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
table{border-radius:10px;overflow:hidden}
th{font-size:12px;text-transform:uppercase;letter-spacing:.05em}
tr:hover td{background:var(--soft)}
.tip{border-radius:0 10px 10px 0;background:var(--soft);padding:10px 14px}
.bar{height:24px;border-radius:999px;background:color-mix(in srgb,var(--line) 80%,transparent);box-shadow:inset 0 1px 2px rgba(0,0,0,.08)}
.bar>i{background:linear-gradient(90deg,var(--acc),var(--acc2));border-radius:999px}
.status{margin-top:10px}.srow{font-size:13.5px}
.srow #msg b{color:var(--ink)}
.badge{font-weight:600}
.hrun{transition:transform .12s,box-shadow .12s}.hrun:hover{transform:translateY(-1px)}
.gcheck{display:grid;gap:6px;margin:14px 0 6px}
.gc{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:10px;background:var(--soft);font-size:14px}
.gc i{font-style:normal;display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;flex:none;font-size:13px;font-weight:700;background:var(--line);color:var(--mute)}
a{color:var(--acc)}.gc.ok{color:var(--ink)}.gc.ok i{background:var(--ok);color:#fff}.gc.bad i{background:var(--bad);color:#fff}.gc.opt2 i{background:var(--line)}
.gc small{margin:0 0 0 auto;font-size:12.5px;display:inline}
.gc.bad{background:color-mix(in srgb,var(--bad) 10%,var(--card))}
@media(max-width:620px){.card{padding:16px}.tab{padding:8px 9px;font-size:13px}.tab b{font-size:15px}.hero h1{font-size:23px}}

.foot{margin:34px 0 8px;text-align:center;color:var(--mute);font-size:12.5px;line-height:1.7}
#ack{position:fixed;inset:0;z-index:100;background:rgba(10,12,20,.55);backdrop-filter:blur(6px);display:none;align-items:center;justify-content:center;padding:16px;overflow:auto}
.ackbox{background:var(--card);color:var(--ink);border-radius:20px;max-width:560px;padding:24px 26px;box-shadow:0 30px 80px -20px rgba(0,0,0,.5)}
.ackbox h2{font-size:22px;margin:0 0 6px}.ackbox ul{padding-left:20px;margin:8px 0 14px}.ackbox li{margin:6px 0}
.ackrow{display:flex;gap:10px;align-items:flex-start;margin:6px 0 14px;font-weight:600}
.ackbtns{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.hsubnav{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 12px}.hsubnav button{border-radius:999px;padding:7px 14px}.hsubnav button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.md h1{font-size:24px;margin:0 0 10px}.md h2{font-size:19px;margin:26px 0 8px;padding-top:8px;border-top:1px solid var(--line)}.md h3{font-size:16px;margin:18px 0 6px}
.md p{margin:8px 0}.md ul,.md ol{padding-left:22px;margin:8px 0}.md li{margin:4px 0}.md pre{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;font-size:12.5px}
.md pre code{background:none;padding:0}.md table{margin:10px 0;display:block;overflow-x:auto}.md blockquote{border-left:3px solid var(--acc);margin:10px 0;padding:2px 12px;color:var(--mute)}
.gtools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.gtools input{flex:1 1 200px}.gtools select{flex:1 1 200px;max-width:100%}
.hlight{background:color-mix(in srgb,var(--warn) 30%,transparent);border-radius:3px}

.chkbox{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:10px 0 14px;padding:14px 16px;border-radius:12px;background:linear-gradient(135deg,color-mix(in srgb,var(--acc) 12%,var(--card)),var(--card));border:1px solid color-mix(in srgb,var(--acc) 30%,var(--line))}
.mutes{color:var(--mute);font-size:13.5px}
.rec{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;margin:8px 0;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.rec input{margin-top:4px}.rec .why{color:var(--mute);font-size:13.5px;margin-top:3px;line-height:1.45}
.badge.warnb{background:color-mix(in srgb,var(--warn) 22%,var(--card));color:var(--warn)}

.simgrp{padding:14px}.simrow{display:flex;gap:12px;flex-wrap:wrap}
.simitem{display:flex;flex-direction:column;gap:6px;width:170px;cursor:pointer}
.simitem img{width:170px;height:130px;object-fit:cover;border-radius:10px;background:var(--line);display:block}
.simcap{display:flex;flex-direction:column;font-size:12px;color:var(--mute);line-height:1.35;overflow:hidden}.simcap b{color:var(--ink);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.simsel{font-size:13px;display:flex;align-items:center;gap:6px}

.flow{display:grid;gap:8px;margin:8px 0}
.flowstep{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.flowstep .fn{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;font-weight:700;font-size:14px;flex:none}
.flowstep.done{opacity:.65}.flowstep.optional .fn{background:var(--line);color:var(--ink)}
.flowstep .why{color:var(--mute);font-size:13.5px;margin-top:2px}

.hscore{display:flex;gap:22px;align-items:center;flex-wrap:wrap;margin:6px 0 14px}
.ring{--p:80;--c:var(--ok);width:130px;height:130px;border-radius:50%;background:conic-gradient(var(--c) calc(var(--p)*1%),var(--line) 0);display:grid;place-items:center;position:relative;flex:none}
.ring::before{content:"";position:absolute;inset:12px;border-radius:50%;background:var(--card)}
.ring b,.ring span{position:relative;display:block;text-align:center}.ring b{font-size:34px;line-height:1}.ring span{font-size:11px;color:var(--mute);margin-top:-30px}
.ring{align-content:center}
.parts{display:grid;gap:6px;margin-top:8px}.part{display:flex;align-items:center;gap:8px;font-size:13px}.part span{width:70px;color:var(--mute)}.part em{font-style:normal;width:28px;text-align:right}
.pbar{flex:1;height:8px;border-radius:99px;background:var(--line);overflow:hidden}.pbar i{display:block;height:100%;border-radius:99px}
.years{display:flex;align-items:flex-end;gap:3px;height:92px;overflow-x:auto;padding:6px 0}.yr{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:10px;color:var(--mute)}.yr i{display:block;width:14px;background:linear-gradient(var(--acc),var(--acc2));border-radius:4px 4px 0 0}
.fmtrow{display:flex;gap:12px;align-items:center;padding:4px 0;font-size:14px}.fmtrow>span:first-child{width:56px}
</style></head><body><div id="ack" style="display:none"><div class="ackbox" role="dialog" aria-modal="true" aria-labelledby="acktitle">
<h2 id="acktitle">Before you start</h2>
<p>Backstory changes, copies, moves and (if you choose) deletes files. Please read this once:</p>
<ul>
<li><b>Back up your originals</b> (your Takeout zip files or folders) before you begin.</li>
<li><b>Preview first.</b> Every tab starts with <i>Preview only</i> ticked. It changes nothing.</li>
<li>Options marked &#9888;&#65039; can delete, overwrite, rename or merge files. Read them before ticking them.</li>
<li>This free software is provided <b>&ldquo;as is&rdquo;, without warranty</b>. <b>You use it at your own risk</b>, and to the maximum extent permitted by law the author is not liable for any loss or damage, including lost or changed photos and data.</li>
</ul>
<label class="ackrow"><input type="checkbox" id="ackbox"> I have read this, I will keep backups, and I accept it.</label>
<div class="ackbtns"><button class="p" id="ackgo" disabled>Continue</button><button id="ackmore" class="sm">Read the full safety notice</button></div>
<small>Support: thestocksoup@gmail.com</small>
</div></div>
<main>
<div id="upd" style="display:none" class="card"><b>A newer version is available.</b> <span id="updmsg"></span>
<div style="margin-top:8px"><button class="p" id="updgo">Update now</button> <button id="updno">Not now</button></div></div>
<header class="hero">
  <div class="brand">
    <svg class="logo" viewBox="0 0 1024 1024" role="img" aria-label="Backstory logo"><defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5b5bf0"/><stop offset=".55" stop-color="#7a4df0"/><stop offset="1" stop-color="#14b8c4"/></linearGradient>
<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8fd3ff"/><stop offset="1" stop-color="#ffe6b3"/></linearGradient>
<filter id="sh" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow dx="0" dy="18" stdDeviation="22" flood-color="#1a1050" flood-opacity=".35"/></filter>
<filter id="sh2" x="-30%" y="-30%" width="160%" height="170%"><feDropShadow dx="0" dy="8" stdDeviation="10" flood-color="#3a1000" flood-opacity=".35"/></filter>
</defs>
<rect width="1024" height="1024" rx="230" fill="url(#bg)"/>
<circle cx="860" cy="170" r="230" fill="#fff" opacity=".07"/>
<g transform="rotate(-7 480 520)" filter="url(#sh)">
  <rect x="190" y="150" width="600" height="720" rx="34" fill="#fffdf8"/>
  <rect x="240" y="200" width="500" height="470" rx="14" fill="url(#sky)"/>
  <circle cx="610" cy="320" r="62" fill="#ffb347"/>
  <path d="M240 600 L400 430 L500 540 L590 450 L740 610 L740 656 a14 14 0 0 1 -14 14 L254 670 a14 14 0 0 1 -14 -14 Z" fill="#3f6fd8"/>
  <path d="M240 640 L360 540 L470 630 L560 560 L740 650 L740 656 a14 14 0 0 1 -14 14 L254 670 a14 14 0 0 1 -14 -14 Z" fill="#2c4fa8"/>
  <rect x="250" y="722" width="330" height="26" rx="13" fill="#c9cbe0"/>
  <rect x="250" y="772" width="220" height="26" rx="13" fill="#dcdded"/>
</g>
<g filter="url(#sh2)" transform="translate(690 610)">
  <path d="M0 -150 C-84 -150 -146 -88 -146 -10 C-146 78 -50 150 0 232 C50 150 146 78 146 -10 C146 -88 84 -150 0 -150 Z" fill="#ff5a4e" stroke="#fff" stroke-width="22"/>
  <circle cx="0" cy="-10" r="54" fill="#fff"/>
  <path d="M-26 -8 l20 22 l38 -46" fill="none" stroke="#ff5a4e" stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/>
</g>
</svg>
    <div>
      <h1>Backstory</h1>
      <p class="tag">Give every photo its backstory back: real dates, places and captions from your Google Photos export. <span id="ver" style="opacity:.6;white-space:nowrap"></span> <a href="#" id="vercheck" style="font-size:13px;white-space:nowrap">Check for updates</a> <span id="vermsg" style="font-size:13px;white-space:nowrap"></span></p>
    </div>
  </div>
  <div class="fbar" id="fbar">
    <div class="frow" id="frs"><span class="flabel">&#128193; Source <b id="fsum"></b></span><div class="fchips" id="fchips"></div><button id="fadd" class="sm">Add folders...</button><button id="fzip" class="sm" data-tip="Add Google Takeout .zip files directly. They are read one at a time and never changed, so there is no need to unzip them first.">Add zip files...</button><button id="fedit" class="sm">Edit list</button></div>
    <div id="fpanel" style="display:none"><textarea id="fall" placeholder="One folder path per line (drag folders here too)" spellcheck="false"></textarea><div class="row" style="margin-top:6px"><button id="fdone" class="p sm">Done</button><button id="fclear" class="sm">Clear all</button></div></div>
    <div class="frow" id="frd"><span class="flabel">&#127919; Destination</span><input type="text" id="fdest" placeholder="Where fixed or sorted copies go (optional when moving)" spellcheck="false"><button id="fdbtn" class="sm">Choose...</button><button id="fdclr" class="sm">Clear</button></div>
  </div>
</header>

<div id="frame">
  <nav class="tabs" role="tablist">
    <button class="tab" data-tab="guided" role="tab"><b>&#10024;</b> Guided</button>
    <button class="tab" data-tab="fix" role="tab"><b>&#128736;&#65039;</b> Fix</button>
    <button class="tab" data-tab="merge" role="tab"><b>&#128450;&#65039;</b> Merge</button>
    <button class="tab" data-tab="clean" role="tab"><b>&#129529;</b> Clean up</button>
    <button class="tab" data-tab="convert" role="tab"><b>&#127902;&#65039;</b> Convert</button>
    <button class="tab" data-tab="monitor" role="tab"><b>&#128225;</b> Monitor</button>
    <button class="tab" data-tab="health" role="tab"><b>&#129658;</b> Health</button>
    <button class="tab" data-tab="photos" role="tab"><b>&#127822;</b> Photos</button>
    <button class="tab" data-tab="similar" role="tab"><b>&#128269;</b> Similar</button>
    <button class="tab" data-tab="history" role="tab"><b>&#128196;</b> History</button>
    <button class="tab" data-tab="help" role="tab"><b>&#10067;</b> Help</button>
  </nav>
  <div class="status"><div class="srow"><span id="msg">Ready. Choose a tab, set it up and press Start.</span><a href="#" id="goto" style="display:none">View results &rarr;</a><button id="contbtn" class="sm p" style="display:none;margin-left:10px">Continue</button><button id="stopall" class="sm" style="display:none;margin-left:10px">Stop</button></div>
  <div class="bar" id="bar"><i id="fill"></i><span id="pct">0%</span></div></div>
</div>
<section class="pane" id="pane-guided">
<h2 class="ph">Fix my Takeout</h2>
<div class="card"><small style="margin-top:0">The easy way. Add your Google Takeout <b>zip files</b> (or the folders you unzipped) in the bar at the top, choose where the finished library should go, and press the button. Your originals are <b>never changed</b>: a clean, merged copy is made in the Destination, with the real dates, locations and captions put back, duplicates removed and your folder structure kept.</small>
<div class="gcheck" id="gcheck"></div>
<div class="chkbox"><div><b>Not sure what to tick?</b><br><span class="mutes">Let the app look at your real files and recommend a plan, with the reasons. It changes nothing.</span></div><button id="gchk" class="p">Check my files</button></div>
<div class="opt"><input type="checkbox" id="gdry" checked><div>Preview only<small>On by default. Shows what would happen and changes nothing. Untick to do it for real.</small></div></div>
<div class="opt"><input type="checkbox" id="gdedupe" checked><div>Merge same-named folders and remove exact duplicates<small>Every <i>Photos from 2012</i> across all your zips becomes one folder, and the same photo repeated in several albums is kept once.</small></div></div>
<div class="opt"><input type="checkbox" id="glive" checked><div>Re-pair Live Photos<small>Reconnects each Live Photo&#39;s still and video so Apple Photos shows them together.</small></div></div>
<div class="opt"><input type="checkbox" id="gnd" checked><div>Use the date in the file name when there is no .json<small>Fills in a missing date from names like <i>IMG_20190704_123456</i>. Never changes a date that is already there.</small></div></div>
<div class="opt"><input type="checkbox" id="galb" checked><div>Keep album names as keywords<small>When a photo that lived in an album is skipped as a duplicate, the album name is saved as a keyword on the kept copy so you do not lose your albums. A list of albums is saved with the reports.</small></div></div>
<div class="opt"><div style="flex:1"><label for="gedit" style="font-weight:600">Google-edited copies (IMG_1-edited.jpg)</label><select id="gedit" class="sel"><option value="both" selected>Keep both</option><option value="edited">Keep only the edited version</option><option value="original">Keep only the original</option></select></div></div>
<div class="opt"><input type="checkbox" id="gow" checked><div>&#9888;&#65039; Replace location and caption already stored in the photo<small>Google&#39;s values win for location and caption. Dates keep the earlier of the two. Change this in the Fix tab if you want other rules.</small></div></div>
<div class="opt"><input type="checkbox" id="gext" checked><div>&#9888;&#65039; Repair files with a missing file type<small>Some Takeout photos have no .jpg or .heic ending. Inside zip files they are repaired in the copy automatically. For folders you already unzipped, this renames those files in the source folders.</small></div></div>
<div class="opt"><input type="checkbox" id="gcv"><div>&#9888;&#65039; Also convert old videos (.avi, .mpg, .wmv...) to MP4 afterwards<small>Runs after the library is built, on the Destination. The old videos are moved into an <i>_original_videos</i> folder, not deleted. Not part of a preview.</small></div></div>
<button class="p" id="gst" style="margin-top:10px">Fix my Takeout</button></div>
</section>
<section class="pane" id="pane-fix">
<h2 class="ph" data-tip="Reads the .json file Google added to each photo and writes the real date taken, location, caption and tagged people back into the photo. The picture itself is never changed.">Fix dates, locations and captions</h2>
<div class="card usefcard"><b>1. Takeout folders</b><div class="usef" style="margin:8px 0 0"><span class="fnote"></span>. Add every Takeout batch: a photo in one batch finds its JSON in another.</div></div>

<div class="card usefcard"><b>2. Where the fixed copies go</b><div class="usef" style="margin:8px 0 0"><b>Destination:</b> <span class="dnote" data-empty="none chosen, so files are fixed in place (the originals are changed)"></span><br><small style="display:inline">Recommended: a new folder, so originals stay untouched. Reports are saved there too (or on your Desktop if none).</small></div></div>

<div class="card"><label class="t">3. Options</label>
<div class="opt"><input type="checkbox" id="dry" checked><div>Preview only<small>On by default. Works out what it would do and reports the numbers, but changes nothing. Untick to do it for real.</small></div></div>
<div class="opt"><input type="checkbox" id="dedupe" checked><div>Remove exact duplicates<small>Skips byte-identical copies (the same photo repeated across Takeouts or albums). Keeps the copy in 'Photos from YYYY'. Needs an extra read pass over files that share a size.</small></div></div>
<div class="opt"><input type="checkbox" id="move"><div>&#9888;&#65039; Move files instead of copying <span class="warn">(empties the source folders)</span><small>Saves disk space but empties your Takeout folders as it goes. Off = safe copy (needs roughly as much free space again).</small></div></div>
<div class="opt"><input type="checkbox" id="live" checked><div>Re-pair Live Photos<small>Copies each still's Apple ID onto its video and saves the video as .MOV so Photos can treat them as one Live Photo. Works in place too: the video is renamed to .MOV beside its photo.</small></div></div>
<div class="opt"><div style="flex:1"><label for="datepol" style="font-weight:600">When a photo already has a date and Google&#39;s is different</label>
<select id="datepol" class="sel"><option value="earlier" selected>Keep the earlier date (recommended)</option><option value="photo">Keep the photo&#39;s own date</option><option value="google">Use Google&#39;s date</option></select>
<small>Google sometimes records the day a photo was uploaded or re-saved instead of the day it was taken, and that day is always later. Keeping the earlier of the two is usually right. A photo with no date at all always gets Google&#39;s.</small></div></div>
<div class="opt"><input type="checkbox" id="ow" checked><div>&#9888;&#65039; Replace location and caption already stored in the photo<small>Every photo has hidden facts saved inside the file itself (called EXIF). <b>Off</b>: only fill in a location or caption that is missing. <b>On</b>: replace a different one with Google&#39;s version. Your pictures themselves are never altered.</small></div></div></div>
<div class="opt"><input type="checkbox" id="ndates" checked><div>Use the date in the file name when there is no .json<small>For photos with no Google info file and no date of their own, reads a date from names like <i>IMG_20190704_123456</i>, <i>PXL_20210512_...</i> or <i>Screenshot 2019-07-04 at 12.34.56</i>. It only fills in a missing date and never changes one that is already there.</small></div></div>
<div class="opt"><input type="checkbox" id="albums" checked><div>Keep album names when duplicates are removed<small>Google saves a photo once in its year folder and again in every album. When the album copies are skipped as duplicates, the album names are saved as keywords on the kept photo (Apple Photos and Lightroom show keywords), and a list of albums is saved with the reports.</small></div></div>
<div class="opt"><div style="flex:1"><label for="edited" style="font-weight:600">When Google saved an edited copy (IMG_1-edited.jpg) next to the original</label><select id="edited" class="sel"><option value="both" selected>Keep both</option><option value="edited">Keep only the edited version</option><option value="original">Keep only the original</option></select><small>The ones left out stay in your Takeout; they are just not copied into the new library.</small></div></div>

<button class="p" id="go">Start</button>

<div id="results">
<div class="card" id="prog" style="display:none;margin-top:14px">
<div id="cvlive" style="display:none"></div>
<div class="tiles" id="tiles"></div>
<div id="recent" style="font:12px ui-monospace,Menlo,monospace;color:var(--mute);line-height:1.6;overflow:hidden"></div></div>

<div class="card" id="sum" style="display:none"><h2 style="margin-top:0">Summary</h2><div id="sumbody"></div>
<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap"><button id="orep" class="p" disabled>Open full report</button><button id="olog" disabled>Open log</button><button id="rev">Show CSV in Finder</button></div><small id="repnote">Saving the report...</small></div>


</div>
</section>
<section class="pane" id="pane-merge">
<h2 class="ph">Merge folders</h2>
<div class="card"><small style="margin-top:0">Brings two or more folders together into one. Folders with the same name at any depth are merged, their files are combined, identical files are kept once, and different files with the same name are handled the way you choose below. Works on any folders, including Google Takeout exports, and gives you control over name clashes.</small>
<div class="usef" style="margin-top:10px"><b>Folders to merge (the Source list):</b> <span class="fnote"></span></div>
<div class="usef"><b>Merge into (the Destination):</b> <span class="dnote" data-empty="none chosen: with Move ticked, everything is merged into the first source folder"></span></div>
<div class="opt"><input type="checkbox" id="mgdry" checked><div>Preview only<small>On by default. Shows which folders would merge, how many files, identical copies and name clashes, and changes nothing.</small></div></div>
<div class="opt"><input type="checkbox" id="mgmove"><div>&#9888;&#65039; Move instead of copy <span class="warn">(empties the source folders)</span><small>Takes the files out of the source folders and empties them (needs no extra space). With no destination chosen, everything is merged into the <b>first</b> source folder. Off = copy into the Destination, leaving your sources untouched (needs about as much free space again).</small></div></div>
<div class="opt"><div style="flex:1"><label for="mgconf" style="font-weight:600">When two different files have the same name in the same folder</label>
<select id="mgconf" class="sel"><option value="both" selected>Keep both (the second is named name_1)</option><option value="newer">The newer file keeps the name</option><option value="larger">The larger file keeps the name</option><option value="first">The file from the first source folder keeps the name</option></select>
<small>With the last three, the other file is not deleted: it is set aside in a <i>_merge_conflicts</i> folder inside the destination so you can review it.</small></div></div>
<div class="opt"><div style="flex:1"><label for="mgdup" style="font-weight:600">&#9888;&#65039; Identical files (same content)</label>
<select id="mgdup" class="sel"><option value="delete" selected>Keep one copy; when moving, delete the extra copy (permanent)</option><option value="aside">Keep one copy; when moving, move the extra to a _duplicates folder</option></select>
<small>Files are compared by their content, not just their names. When copying, identical files are simply not copied twice.</small></div></div>
<div class="opt"><input type="checkbox" id="mgtidy" checked><div>Treat <i>Folder (1)</i>, <i>Folder copy</i> and extra spaces as the same folder as <i>Folder</i><small>Real names such as <i>Summer (2019)</i> are not changed. Applies to folder names only.</small></div></div>
<div class="opt"><input type="checkbox" id="mgcase" checked><div>Ignore upper and lower case in folder names<small>So <i>photos</i> and <i>Photos</i> become one folder (the first spelling found is used).</small></div></div>
<div class="opt"><input type="checkbox" id="mgtk"><div>These are Google Takeout folders<small>Ignores the <i>Takeout N / Google Photos</i> wrappers so every <i>Photos from 2012</i> becomes one folder, and brings each photo&#39;s .json info file along with it (named after the placed photo), so Part 1 still works afterwards.</small></div></div>
<div class="opt"><input type="checkbox" id="mgdd"><div>Find identical photos anywhere, not just in the same folder<small>Compares file contents across all folders, so the same photo repeated in several albums or Takeouts is kept once. Slower on big libraries.</small></div></div>
<small>Not touched: shortcuts. App and library bundles (such as <i>.photoslibrary</i>) are moved as a single item. Invisible system files (.DS_Store and the like) are left out.</small>
<button class="p" id="mgo" style="margin-top:10px">Start</button></div>
</section>
<section class="pane" id="pane-clean">
<h2 class="ph">Clean up</h2>
<div class="card"><small style="margin-top:0">Tick what you want tidied. The steps run in the order shown, so removing files first lets the last step catch the folders they leave empty. <b>Preview first</b>: it lists what would happen and changes nothing.</small>
<div class="usef" style="margin-top:10px"><b>Folders to clean:</b> <span class="fnote"></span></div>
<div class="opt"><input type="checkbox" id="cdry" checked><div>Preview only<small>On by default. Lists what would be deleted, renamed, merged or removed, and changes nothing.</small></div></div>

<div class="opt"><input type="checkbox" id="cx" checked><div><b>1. Fix files with no extension</b><small>Some photos and videos come out of Google Takeout with a name like <i>IMG_2438</i> and no <i>.jpg</i> or <i>.heic</i>, so Finder calls them "Document" and the Fix tab skips them. This reads the real type from inside each file and adds the right extension. Files that cannot be recognised (empty or damaged) are listed but not changed. Do this <b>before</b> Fix metadata.</small>
<label class="sub" data-tip="So the Fix tab can still match each photo to its Google info file."><input type="checkbox" id="cxj" checked> Also rename each file's Google .json to match</label>
<label class="sub" data-tip="Files that are empty or damaged are moved into an <i>_unrecognised</i> folder inside each folder you chose, so you can review or delete them. Otherwise they are only listed."><input type="checkbox" id="cxu"> Move unrecognised files into an <i>_unrecognised</i> folder</label></div></div>

<div class="opt"><input type="checkbox" id="cj"><div>&#9888;&#65039; <b>2. Remove Google .json files</b><small>The small info files Google adds to each photo. They hold the only copy of the original date and location, so do this <b>after</b> you have fixed your photos. Off by default.</small>
<label class="sub" data-tip="Off: only Google's photo info files and album/memory data files are removed. On: every .json file in the folders."><input type="checkbox" id="cother"> &#9888;&#65039; Also remove other .json files</label></div></div>

<div class="opt"><input type="checkbox" id="cjunk" checked><div>&#9888;&#65039; <b>3. Remove junk and cache files</b><small>Files nothing needs. Choose which kinds:</small>
<label class="sub" data-tip="Invisible files Finder and Windows leave behind: .DS_Store, Thumbs.db, desktop.ini and ._ files."><input type="checkbox" id="cjs" checked> System leftovers</label>
<label class="sub" data-tip="Thumbnail caches from iPod/iTunes photo syncing, such as T103.ithmb inside an <i>iPod Photo Cache</i> folder."><input type="checkbox" id="cji" checked> iPod/iTunes thumbnail caches (.ithmb)</label>
<label class="sub"><input type="checkbox" id="cjp" checked> Picasa.ini files</label>
<label class="sub"><input type="checkbox" id="cjt"> Camera video thumbnails (.thm)</label>
<label class="sub" data-tip="Names ending in <i>.sb-12345678-AbCdEf</i> and exactly 0 bytes: macOS makes these while saving a file and sometimes leaves them behind. A leftover that still contains data is never deleted, only reported."><input type="checkbox" id="cjb" checked> Empty temporary files left by macOS saving</label>
<label class="sub" data-tip="Zero-byte files of any name: nothing is stored in them."><input type="checkbox" id="cje" checked> &#9888;&#65039; Other empty files (0 bytes)</label></div></div>

<div class="opt"><input type="checkbox" id="cn" checked><div>&#9888;&#65039; <b>4. Tidy names</b><small>Fixes duplicate-style names such as <i>From Cris Drive - 2001(1)</i> to <i>From Cris Drive - 2001</i>. If a folder with the clean name already exists, the two are <b>merged</b>: identical files are kept once, and different files with the same name are both kept (the second becomes <i>name_1</i>). Real names such as <i>Summer (2019)</i> are never changed, and the folders you chose are not renamed.</small>
<label class="sub"><input type="checkbox" id="cnp" checked> Remove " (1)", " (2)" ... from names</label>
<label class="sub"><input type="checkbox" id="cnc" checked> Remove " copy", " copy 2" from names</label>
<label class="sub"><input type="checkbox" id="cns" checked> Trim and collapse extra spaces</label>
<label class="sub" data-tip="A file is only renamed when the clean name is free, and its .json is renamed with it. Best done after you have fixed your photos, because Fix matches photos to JSON by name."><input type="checkbox" id="cnf"> &#9888;&#65039; Also tidy file names</label>
<label class="sub" data-tip="Used when a renamed folder is merged into one that already exists and both hold the same file.">Identical copies found while merging: <select id="cnd" class="sel"><option value="delete" selected>delete the extra copy</option><option value="aside">move it to a _duplicates folder</option></select></label></div></div>

<div class="opt"><input type="checkbox" id="ce" checked><div>&#9888;&#65039; <b>5. Remove empty folders</b><small>Removes every folder with no files in it at any depth, only after the steps above. Only folders are removed. Shortcuts, app/library bundles (such as .photoslibrary) and unreadable folders are never entered.</small>
<label class="sub" data-tip="So a folder containing only .DS_Store or Thumbs.db files is still removed."><input type="checkbox" id="cejunk" checked> Folders holding only system leftovers count as empty</label>
<label class="sub" data-tip="Off by default: the folders you add at the top are always kept, even if everything inside them is removed."><input type="checkbox" id="cetop"> &#9888;&#65039; Also remove my chosen folders if they end up empty</label></div></div>

<button class="p" id="cgo" style="margin-top:6px">Start</button></div>
<div class="card" id="simcard">
<b>Smart folder consolidation</b>
<small style="margin-top:4px">Finds folders that look like the same thing under different names, such as <i>Japan 2025</i>, <i>delete-Japan 2025</i> and <i>Japan 2025-old</i>, and merges them into one. You review every group before anything happens.</small>
<div class="opt"><input type="checkbox" id="simmaybe" checked><div>Also look for likely spelling differences<small>For example <i>Italy 2024</i> and <i>Itly 2024</i>. These are shown as &ldquo;Check this&rdquo; and are not ticked for you.</small></div></div>
<button id="simfind">Find similar folders</button>
<div id="simout"></div>
</div>

</section>
<section class="pane" id="pane-convert">
<h2 class="ph">Convert old videos to MP4</h2>
<div class="card"><small style="margin-top:0">Turns older video formats (<b>.avi</b>, <b>.mov</b>, <b>.mpg</b>, <b>.wmv</b>, <b>.3gp</b> and more) into <b>.mp4</b>, which plays on every phone, TV and app. Videos that are already H.264 or HEVC are simply re-wrapped (fast, no quality loss); others are re-encoded. Dates and locations are carried across. Needs <b>ffmpeg</b> (in Terminal: <code>brew install ffmpeg</code>).</small>
<div class="usef" style="margin-top:12px"><b>Folders to scan:</b> <span class="fnote"></span></div>
<div class="opt"><input type="checkbox" id="vdry" checked><div>Preview only<small>On by default. Counts what would be converted (and how), changes nothing.</small></div></div>
<div class="opt"><input type="checkbox" id="vest" checked><div>Estimate the new sizes in the preview<small>Test-encodes a few short samples (about 8 seconds each, up to 3 per video type) with your quality setting, and uses the result to predict the size after conversion. Adds a minute or two to a preview of videos that need re-encoding.</small></div></div>
<div class="opt"><div style="flex:1"><b>Video types to convert</b> <button id="vscan" class="sm" style="margin-left:8px">Scan folders for counts</button>
<div id="vtypes" class="vtypes"></div>
<div id="vscansum" class="tip" style="display:none;margin-top:8px"></div>
<small>Tick the formats you want, each one separately. Scan to see how many videos of each type there are and how many .mov files are Live Photo videos. Types you leave unticked are not touched.</small></div></div>
<div class="opt"><input type="checkbox" id="vlive"><div>Also convert Live Photo videos<small>Off by default. An iPhone Live Photo is a still picture plus a short .MOV video that Apple Photos links together. If a .MOV sits next to a photo with the same name, it is treated as a Live Photo video and left alone, because converting it to .mp4 would break the link. Your ordinary .mov and .avi videos are converted as normal.</small></div></div>
<div class="opt"><div style="flex:1"><label for="vq" style="font-weight:600">Quality when re-encoding</label>
<select id="vq" class="sel"><option value="veryhigh">Very high (largest files)</option><option value="high" selected>High (recommended)</option><option value="small">Smaller files</option></select></div></div>
<div class="opt"><div style="flex:1"><label for="vact" style="font-weight:600">&#9888;&#65039; What happens to the original video</label>
<select id="vact" class="sel"><option value="move" selected>Move to an _original_videos folder (safe)</option><option value="keep">Keep it where it is, next to the new .mp4</option><option value="delete">&#9888;&#65039; Delete it once the new .mp4 is verified (permanent)</option></select>
<small>Originals are only moved or deleted after the new file has been checked (it must play and match the original&#39;s length).</small></div></div>
<button class="p" id="vgo" style="margin-top:6px">Start converting</button></div>
</section>
<section class="pane" id="pane-history">
<h2 class="ph">History and reports</h2>
<div class="card"><small style="margin-top:0">Every run is saved here with a full report and a plain-text log, so you can see exactly what happened, even weeks later.</small>
<div class="usef" style="margin:10px 0 0">Reports are kept in: <b id="repdir">Documents/Backstory Reports</b></div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button id="hfolder">Open reports folder</button><button id="hdiag" data-tip="Copies your version, system, tool versions and the end of the latest log, so you can paste it when asking for help. Check it for private paths first.">Copy diagnostic info</button></div></div>
<div id="hlist"></div>
</section>
<section class="pane" id="pane-help">
<h2 class="ph">Help</h2>
<div class="hsubnav" id="hsubnav"><button data-v="guide" class="on">User guide</button><button data-v="safety">Safety &amp; disclaimer</button><button data-v="support">Support</button><button data-v="about">About</button></div>
<div class="card" id="hview"></div>
</section>
<section class="pane" id="pane-similar">
<h2 class="ph">Similar photos</h2>
<div class="card"><small style="margin-top:0">Finds pictures that look the same but are not identical files: the same photo saved at a smaller size, re-saved, or lightly edited. You review each group and choose what to set aside. Nothing is deleted: the photos you pick are moved into a <i>_similar_set_aside</i> folder, and you can undo it from History.</small>
<div class="usef" style="margin-top:10px"><b>Folders to check:</b> <span class="fnote"></span></div>
<div class="opt"><div style="flex:1"><label for="simsens" style="font-weight:600">How alike must they be?</label>
<select id="simsens" class="sel"><option value="3">Very alike (fewest matches, safest)</option><option value="6" selected>Alike (recommended)</option><option value="10">Loosely alike (more matches, check carefully)</option></select></div></div>
<button class="p" id="simscan" style="margin-top:6px">Find similar photos</button></div>
</section>
<section class="pane" id="pane-photos">
<h2 class="ph">Send to Apple Photos</h2>
<div class="card"><small style="margin-top:0">Sends your finished library to the Photos app in batches, oldest first, so a Mac that is short of space can take a big library over time: Photos uploads each batch to iCloud, macOS frees the space, and Backstory waits before sending the next one. Live Photos stay together, and album folders become Photos albums.</small>
<div class="gcheck" id="pcheck"></div>
<div class="usef" style="margin-top:6px"><b>Library to send:</b> <span id="proot">the first folder in the Source list</span></div>
<div class="opt"><div style="flex:1"><label for="pbatch" style="font-weight:600">Batch size</label><select id="pbatch" class="sel"><option value="2">About 2 GB</option><option value="5">About 5 GB</option><option value="10" selected>About 10 GB</option><option value="25">About 25 GB</option><option value="50">About 50 GB</option></select></div></div>
<div class="opt"><div style="flex:1"><label for="ppace" style="font-weight:600">Between batches</label><select id="ppace" class="sel"><option value="verify" selected>Wait until Photos shows each batch as uploaded to iCloud (recommended)</option><option value="space">Wait until my Mac has enough free space</option><option value="ask">Pause and let me press Continue</option><option value="none">Do not wait</option></select>
<div style="margin-top:6px">Keep at least <input type="number" id="pfree" value="20" min="0" max="2000" style="width:80px;flex:none"> GB free</div></div></div>
<div class="opt"><input type="checkbox" id="padapt" checked><div>Adapt the batch size<small>Sends bigger batches when iCloud keeps up easily and smaller ones when a batch takes hours to upload. Only with upload verification.</small></div></div>
<div class="opt"><input type="checkbox" id="palb" checked><div>Create albums from album folders<small>Folders such as <i>Japan 2025</i> (not <i>Photos from 2012</i>) become albums in Photos.</small></div></div>
<div class="opt"><input type="checkbox" id="pdry" checked><div>Preview only<small>Shows the batches and changes nothing. Untick to send to Photos.</small></div></div>
<div class="tip">&#9888;&#65039; Photos has no undo for imports. Always preview, then send a small test first. Photos skips photos it already has, so running again does not duplicate them.</div>
<div class="hbtns"><button class="p" id="pgo">Preview the batches</button><button id="ptest">Send a small test (20 photos)</button></div></div>
</section>
<section class="pane" id="pane-health">
<h2 class="ph">Library health</h2>
<div class="card"><small style="margin-top:0">A health check for a finished library: wasted space, the same file saved in several formats, folder problems, empty and ghost files, files that are only in iCloud, and useful statistics. It only reads, and it keeps a history so you can see whether your library is getting healthier or messier.</small>
<div class="usef" style="margin-top:10px"><b>Library to check:</b> <span class="fnote"></span></div>
<div class="opt"><input type="checkbox" id="hdeep"><div>Deep check<small>Reads the metadata of up to 40,000 files (instead of a sample of about 400) to find wrong extensions, missing dates and photos in the wrong year folder. Slower.</small></div></div>
<div class="opt"><div style="flex:1"><label for="hauto" style="font-weight:600">Check again automatically</label><select id="hauto" class="sel"><option value="0">Off</option><option value="1">Every hour</option><option value="6">Every 6 hours</option><option value="24">Every day</option></select><small>Only while Backstory is open. A new check is skipped while another job is running.</small></div></div>
<button class="p" id="hgo" style="margin-top:6px">Check library health</button></div>
</section>
<section class="pane" id="pane-monitor">
<h2 class="ph">Photos and iCloud monitor</h2>
<div class="card"><b>Is everything in iCloud yet?</b>
<small style="margin-top:4px">Reads a copy of your Photos library's database to count what has uploaded, how fast it is going, and whether it looks stuck. It also checks the files Backstory sent from your first Source folder. Apple does not document this database, so treat the numbers as a strong hint and confirm in Photos and on iCloud.com.</small>
<div class="opt"><div style="flex:1"><label for="uplib" style="font-weight:600">Photos library</label><select id="uplib" class="sel"><option value="">Find it automatically</option></select></div></div>
<div class="opt"><input type="checkbox" id="upsent" checked><div>Also check the files Backstory sent<small>Compares each file sent from the first Source folder with Photos, by name and size.</small></div></div>
<div class="hbtns"><button class="p" id="upgo">Check upload status</button></div><div id="upres"></div></div>
<div class="card"><b>Log issues</b>
<small style="margin-top:4px">Reads recent Photos, iCloud and Backstory errors that you never see in Console and explains them in plain language, with fixes. It only reads. Nothing is uploaded.</small>
<div class="opt"><div style="flex:1"><label for="mhours" style="font-weight:600">Look back</label><select id="mhours" class="sel"><option value="1">1 hour</option><option value="6" selected>6 hours</option><option value="24">24 hours</option><option value="168">7 days</option></select></div></div>
<div class="opt"><div style="flex:1"><label for="mauto" style="font-weight:600">Check automatically</label><select id="mauto" class="sel"><option value="0">Off</option><option value="15">Every 15 minutes</option><option value="60">Every hour</option></select><small>Only while Backstory is open (macOS asks permission before showing notifications).</small></div></div>
<div class="hbtns"><button class="p" id="mgo">Check the logs</button></div>
<div class="opt"><div style="flex:1"><label for="mpaste" style="font-weight:600">Or paste log text</label><textarea id="mpaste" placeholder="Paste lines from Console or a crash report here" spellcheck="false" style="min-height:70px"></textarea></div></div>
<div class="hbtns"><button id="mpastego">Interpret the pasted text</button></div></div>
</section>




<footer class="foot">Free software provided &ldquo;as is&rdquo;, without warranty. Back up your photos first. &middot; <a href="#" data-help="safety">Safety &amp; disclaimer</a> &middot; Support: <a href="mailto:thestocksoup@gmail.com">thestocksoup@gmail.com</a></footer>


<script>
var docTimer=null,DOC=null;
const $=id=>document.getElementById(id), esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function post(u,b){const r=await fetch(u,{method:'POST',body:JSON.stringify(b||{})});return r.json()}

// ---- info buttons: long explanations live in hover / tap tooltips
const tipEl=document.createElement('div');tipEl.id='tip';tipEl.setAttribute('role','tooltip');document.body.appendChild(tipEl);
function showTip(icon){tipEl.innerHTML=icon._tip;tipEl.style.display='block';const r=icon.getBoundingClientRect(),w=tipEl.offsetWidth,h=tipEl.offsetHeight;
  let x=Math.min(Math.max(10,r.left+r.width/2-w/2),window.innerWidth-w-10),y=r.bottom+8;if(y+h>window.innerHeight-10)y=Math.max(10,r.top-h-8);tipEl.style.left=x+'px';tipEl.style.top=y+'px';tipEl._owner=icon}
function hideTip(){tipEl.style.display='none';tipEl._owner=null}
function makeIcon(html){const b=document.createElement('button');b.type='button';b.className='info';b.textContent='i';b.setAttribute('aria-label','More information');b._tip=html;b._pt='mouse';
  b.onpointerdown=e=>{b._pt=e.pointerType||'mouse'};
  b.onpointerenter=e=>{if(e.pointerType==='mouse')showTip(b)};b.onpointerleave=e=>{if(e.pointerType==='mouse')hideTip()};
  b.onfocus=()=>{if(b.matches(':focus-visible'))showTip(b)};b.onblur=hideTip;
  b.onclick=e=>{e.preventDefault();e.stopPropagation();if(b._pt==='mouse'){showTip(b);return}tipEl._owner===b?hideTip():showTip(b)};return b}
document.addEventListener('click',hideTip);window.addEventListener('scroll',hideTip,{passive:true});
const PANE_SUB={monitor:'Is it in iCloud yet, and what are the logs saying? Plain-language answers.',health:'Space waste, folder problems, ghosts and statistics for your library, tracked over time.',photos:'Send your finished library to Apple Photos in batches, with room for iCloud to catch up.',similar:'Find the same picture saved twice, and choose what to set aside.',help:'The user guide, safety notice and how to get support.',history:'Every run, with its full report and log.',guided:'The easy way: zips or folders in, a clean library out. Originals never change.',fix:'Put the real date, location and caption back into your photos.',merge:'Bring two or more folders together into one.',clean:'Tidy up leftovers once you are done.',convert:'Turn older video formats into MP4.'};
function decorate(){
  document.querySelectorAll('.opt').forEach(o=>{
    const box=o.querySelector(':scope > div');if(!box)return;
    const smalls=[...box.children].filter(c=>c.tagName==='SMALL');if(!smalls.length)return;
    const html=smalls.map(s=>s.innerHTML).join('<br><br>');smalls.forEach(s=>s.remove());
    const icon=makeIcon(html),t=box.querySelector(':scope > b, :scope > label:not(.sub)'),wn=box.querySelector(':scope > .warn');
    if(wn)wn.after(icon);else if(t)t.after(icon);else{const tn=[...box.childNodes].find(n=>n.nodeType===3&&n.textContent.trim());if(tn){const w=document.createElement('span');w.className='ttl';tn.replaceWith(w);w.appendChild(tn);w.after(icon)}else box.prepend(icon)}});
  document.querySelectorAll('.pane').forEach(p=>{
    const id=p.id.replace('pane-',''),h=p.querySelector('h2.ph');if(!h)return;
    const intro=p.querySelector('.card > small:first-child');
    if(intro){h.dataset.tip=intro.innerHTML;intro.remove()}
    if(PANE_SUB[id]&&!p.querySelector('.subline')){const sl=document.createElement('p');sl.className='subline';sl.textContent=PANE_SUB[id];h.after(sl)}});
  document.querySelectorAll('[data-tip]').forEach(el=>{if(!el.querySelector(':scope > .info'))el.appendChild(makeIcon(el.dataset.tip))})}
decorate();

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
  $('fall').value=FOLDERS.join('\n');if(typeof renderDest==='function')renderDest();if(typeof scheduleDoctor==='function')scheduleDoctor()}
function saveFolders(){try{localStorage.setItem('folders',JSON.stringify(FOLDERS))}catch(e){};renderFolders()}
function addFolders(list){list.forEach(p=>{p=(p||'').trim().replace(/\/+$/,'');if(p&&!FOLDERS.includes(p))FOLDERS.push(p)});saveFolders()}
$('fadd').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose one or more folders (hold Cmd to select several)'});if(r.paths)addFolders(r.paths)};
$('fzip').onclick=async()=>{const r=await post('/api/choose',{kind:'zip',prompt:'Choose your Google Takeout zip files (hold Cmd to select several)'});if(r.paths)addFolders(r.paths)};
$('fedit').onclick=()=>{const p=$('fpanel');p.style.display=p.style.display==='none'?'block':'none'};
$('fdone').onclick=()=>{FOLDERS=[];addFolders($('fall').value.split('\n'));$('fpanel').style.display='none'};
$('fclear').onclick=()=>{FOLDERS=[];saveFolders()};
const fbar=$('fbar');
fbar.ondragover=e=>{e.preventDefault();fbar.classList.add('over')};fbar.ondragleave=()=>fbar.classList.remove('over');
fbar.ondrop=e=>{e.preventDefault();fbar.classList.remove('over');
  const t=(e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain')||'').split(/\r?\n/).filter(Boolean);
  const paths=t.filter(x=>x.startsWith('file://')||x.startsWith('/')).map(x=>x.startsWith('file://')?decodeURIComponent(x.replace(/^file:\/\/[^\/]*/,'')):x);
  if(paths.length)addFolders(paths);else alert('Your browser did not share the folder path. Use "Add folders..." or paste the paths into Edit list.')};
// ---- shared destination + collapse (header)
let DEST='';try{DEST=localStorage.getItem('dest')||''}catch(e){}
const dest=()=>$('fdest').value.trim().replace(/\/+$/,'');
function renderDest(){
  if(typeof scheduleDoctor==='function')scheduleDoctor();
  document.querySelectorAll('.dnote').forEach(e=>{e.textContent=dest()||e.dataset.empty||'none chosen'});
  const nm=p=>p.split('/').filter(Boolean).pop()||p;
}
function saveDest(){try{localStorage.setItem('dest',dest())}catch(e){};renderDest()}
$('fdest').value=DEST;
$('fdest').oninput=saveDest;
$('fdbtn').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose the destination folder'});if(r.paths&&r.paths[0]){$('fdest').value=r.paths[0].replace(/\/+$/,'');saveDest()}};
$('fdclr').onclick=()=>{$('fdest').value='';saveDest()};
renderDest();
renderFolders();

$('go').onclick=async()=>{
  if(!roots().length){alert('Add your folders in the bar at the top first');return}
  if(!$('dry').checked&&!dest()&&!confirm('No output folder: files will be edited IN PLACE. Continue?'))return;
  if($('move').checked&&!$('dry').checked&&!confirm('MOVE will take files out of your Takeout folders. Make sure you have another backup. Continue?'))return;
  $('sum').style.display='none';
  const r=await post('/api/start',{roots:roots(),out:dest(),dry_run:$('dry').checked,overwrite:$('ow').checked,pair_live:$('live').checked,dedupe:$('dedupe').checked,move:$('move').checked,date_policy:$('datepol').value,name_dates:$('ndates').checked,albums:$('albums').checked,edited:$('edited').value});
  if(r.error)alert(r.error);else poll();
};
$('rev').onclick=()=>post('/api/reveal');
const tile=(n,l,c)=>`<div class="tile ${c||''}"><b>${n.toLocaleString()}</b><span>${l}</span></div>`;
function tbl(head,rows){return `<table><tr>${head.map((h,i)=>`<th class="${i?'n':''}">${h}</th>`).join('')}</tr>${rows.map(r=>`<tr>${r.map((c,i)=>`<td class="${i?'n':''}">${c}</td>`).join('')}</tr>`).join('')}</table>`}
function bars(rows){const m=Math.max(1,...rows.map(r=>r[2]));return rows.map(r=>[esc(r[0]||'(none)'),r[1].toLocaleString(),r[2].toLocaleString()+`<span class="mini" style="width:${Math.round(60*r[2]/m)}px"></span>`])}
function showSummary(s){
  if(s.kind==='cleanup'){showCleanup(s);return}
  if(s.kind==='merge'){showMerge(s);return}
  if(s.kind==='convert'){showConvert(s);return}
  if(s.kind==='guided'){showGuided(s);return}
  if(s.kind==='assess'){showAssess(s);return}
  if(s.kind==='consolidate'){showConsolidate(s);return}
  if(s.kind==='undo'){showUndo(s);return}
  if(s.kind==='similar'){showSimilar(s);return}
  if(s.kind==='photos'){showPhotos(s);return}
  if(s.kind==='health'){showHealth(s);return}
  if(s.kind==='monitor'){showMonitor(s);return}
  if(s.kind==='similar_apply'||s.kind==='formats_apply'){showSimilarApply(s);return}
  let h=`<div class="tiles">${tile(s.total,'media files')}${tile(s.duplicates,'exact duplicates skipped')}${tile(s.matched,'unique files matched ('+s.pct_matched+'%)','ok')}${tile(s.no_json,'no JSON found',s.no_json?'bad':'ok')}${tile(s.orphans,'JSON with no photo')}${s.name_dates?tile(s.name_dates,'dates from file names','ok'):''}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  h+=`<div class="tiles">${tile(s.replaced_files,'files with a value replaced')}${tile((s.live||{}).paired||0,'Live Photos paired')}</div>`;
  const c=s.changes||{},v=s.dry_run?'would change':'changed';
  h+=`<h2>What ${s.dry_run?'would be':'was'} changed</h2><div class="tiles">${tile(c.dates,'dates '+v)}${tile(c.gps,'locations '+v)}${tile(c.desc,'captions '+v)}${tile(c.people,'files with people tagged')}${tile(c.favourites,'favourites marked')}</div>`;
  h+=`<div class="tip" style="border-color:var(--acc)">${s.dry_run?'Would change':'Changed'} <b>${c.dates.toLocaleString()}</b> dates (${c.dates_added.toLocaleString()} added, ${c.dates_replaced.toLocaleString()} replaced), <b>${c.gps.toLocaleString()}</b> locations (${c.gps_added.toLocaleString()} added, ${c.gps_replaced.toLocaleString()} replaced) and <b>${c.desc.toLocaleString()}</b> captions.</div>`;
  h+='<h2>Information stored in the photos</h2>'+tbl(['Field','Added','Replaced','Left alone (different)','Already correct'],[['date','Date taken'],['gps','Location'],['desc','Description']].map(([k,l])=>{const f=s.fields[k]||{};return [l,(f.added||0).toLocaleString(),(f.replaced||0).toLocaleString(),(f.kept||0).toLocaleString(),(f.same||0).toLocaleString()]}));
  if((s.problems||[]).length)h+='<h2>Problems ('+s.problems.length+(s.problems.length>=100?'+':'')+')</h2>'+tbl(['File','What went wrong'],s.problems.map(p=>[esc(p.file),esc(p.detail||p.status)]))+'<small>These files were skipped. Every other file was processed. The full list is in the CSV report.</small>';
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
function setBar(barId,fillId,pctId,pct,indet){
  const bar=$(barId),fill=$(fillId),lab=$(pctId);
  bar.classList.toggle('indet',!!indet);
  if(indet){lab.textContent='working...';lab.className='out';lab.style.left='12px';fill.style.width='';return}
  pct=Math.max(0,Math.min(100,pct));fill.style.width=pct+'%';lab.textContent=Math.floor(pct)+'%';
  if(pct>=12){lab.className='';lab.style.left='calc('+pct+'% - 44px)'}else{lab.className='out';lab.style.left='calc('+pct+'% + 8px)'}}

(async function(){
  let boot=null;
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  async function st(){try{const j=await (await fetch('/api/status')).json();if(j&&j.version)$('ver').textContent='Version '+j.version;return j}catch(e){return null}}
  async function checkNow(manual){
    if(manual)$('vermsg').textContent='Checking...';
    try{await post('/api/update_check')}catch(e){}
    for(let i=0;i<12;i++){
      await sleep(800);const s=await st();if(!s)continue;boot=s.boot;const u=s.update||{};
      if(u.state==='available'){
        $('updmsg').textContent=u.message||('Updated files: '+u.files.join(', ')+'. Your settings are not affected.');$('upd').style.display='block';
        $('vermsg').innerHTML='<span class="ok">Update available</span>';return}
      if(u.state==='current'){$('vermsg').innerHTML=manual?'<span class="ok">&#10003; Up to date</span>':'';return}
      if(u.state==='unknown'){$('vermsg').innerHTML=manual?'<span class="err">Could not check (offline?)</span>':'';return}
    }
    if(manual)$('vermsg').textContent='';
  }
  $('vercheck').onclick=e=>{e.preventDefault();checkNow(true)};
  checkNow(false);setInterval(()=>checkNow(false),30*60*1000);
  $('updno').onclick=()=>{$('upd').style.display='none'};
  $('updgo').onclick=async()=>{
    $('updgo').disabled=true;$('updmsg').textContent='Updating...';
    const r=await post('/api/update');
    if(r.error){$('updmsg').innerHTML='<span class="err">'+esc(r.error)+'</span>';$('updgo').disabled=false;return}
    $('updmsg').textContent='Updated. Restarting...';
    for(let i=0;i<30;i++){await new Promise(x=>setTimeout(x,1000));const s=await st();if(s&&s.boot!==boot){location.reload();return}}
    $('updmsg').textContent='Updated. If the page does not reload, restart the app in Terminal.'}
})();
const TABS=['guided','fix','merge','clean','convert','health','monitor','photos','similar','history','help'];const tabOf=k=>({formats_apply:'health',similar_apply:'similar',undo:'history',consolidate:'clean',cleanup:'clean',sort:'merge',assess:'guided'}[k]||k);let curGuided=false;const paneKind=()=>curGuided?'guided':tabOf(jobKind);let jobKind='fix';
function showTab(t){if(!TABS.includes(t))t='fix';
  TABS.forEach(x=>{$('pane-'+x).style.display=x===t?'block':'none';document.querySelector('.tab[data-tab="'+x+'"]').classList.toggle('on',x===t)});
  try{localStorage.setItem('tab',t)}catch(e){}
  if(t==='history')loadHistory();
  if(t==='photos')renderPCheck();
  if(t==='monitor')loadLibs();
  if(t==='help'&&!$('hview').dataset.loaded){$('hview').dataset.loaded='1';helpView('guide')}
  try{history.replaceState(null,'','#'+t)}catch(e){}
  updGoto()}
function updGoto(){const a=$('goto');const cur=TABS.find(x=>$('pane-'+x).style.display==='block');
  const has=$('prog').style.display!=='none'||$('sum').style.display!=='none';
  a.style.display=(has&&cur!==paneKind()&&jobKind!=='clean')?'inline':'none'}
$('goto').onclick=e=>{e.preventDefault();showTab(paneKind());$('results').scrollIntoView({behavior:'smooth'})};
$('contbtn').onclick=async()=>{await post('/api/photos_continue');$('contbtn').style.display='none'};
$('stopall').onclick=async()=>{const b=$('stopall');b.disabled=true;b.textContent='Stopping...';await post('/api/cancel')};
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
function placeResults(kind){const pane=$('pane-'+(curGuided?'guided':(kind==='clean'?'fix':tabOf(kind))));if(pane&&$('results').parentNode!==pane)pane.appendChild($('results'))}
refreshDoctor();
let startTab='guided';try{startTab=location.hash.slice(1)||localStorage.getItem('tab')||'guided'}catch(e){}
if(startTab==='sort')startTab='merge';
showTab(startTab);

// ---- Part 4: convert
$('vgo').onclick=async()=>{
  if(!vroots().length){alert('Add your folders in the bar at the top first');return}
  const exts=[...document.querySelectorAll('#vtypes input:checked')].map(i=>i.dataset.ext);
  if(!exts.length){alert('Tick at least one video type');return}
  const act=$('vact').value;
  if(act==='delete'&&!$('vdry').checked){const t=prompt('This permanently deletes each original video after its .mp4 is verified. It cannot be undone.\nType DELETE to confirm.');if(t!=='DELETE')return}
  $('sum').style.display='none';
  const r=await post('/api/convert_start',{roots:vroots(),dry_run:$('vdry').checked,exts:exts,include_live:$('vlive').checked,quality:$('vq').value,action:act,estimate:$('vest').checked});
  if(r.error)alert(r.error);else{jobKind='convert';placeResults('convert');$('prog').style.display='block';poll()}};

const VT=[['.avi',1],['.mov',0],['.mpg',1],['.mpeg',1],['.wmv',1],['.3gp',1],['.flv',1],['.mkv',1],['.mts',1],['.m2ts',1],['.vob',1]];
let vsaved=null;try{vsaved=JSON.parse(localStorage.getItem('vtypes')||'null')}catch(e){}
$('vtypes').innerHTML=VT.map(([e,d])=>`<label class="vt"><input type="checkbox" data-ext="${e}" ${(vsaved?vsaved.includes(e):d)?'checked':''}> ${e} <span class="vc" data-ext="${e}"></span></label>`).join('');
$('vtypes').onchange=()=>{try{localStorage.setItem('vtypes',JSON.stringify([...document.querySelectorAll('#vtypes input:checked')].map(i=>i.dataset.ext)))}catch(e){}};
$('vscan').onclick=async()=>{
  if(!vroots().length){alert('Add your folders in the bar at the top first');return}
  const b=$('vscan');b.disabled=true;b.textContent='Scanning...';
  const r=await post('/api/convert_scan',{roots:vroots()});b.disabled=false;b.textContent='Scan folders for counts';
  if(r.error){alert(r.error);return}
  let tot=0,live=0,bytes=0;
  document.querySelectorAll('.vc').forEach(sp=>{const t=r.types[sp.dataset.ext];
    if(t){tot+=t.n;live+=t.live;bytes+=t.bytes}
    sp.textContent=t?'('+t.n.toLocaleString()+' · '+fmtBytes(t.bytes)+(t.live?' · '+t.live.toLocaleString()+' Live Photo':'')+')':'(none)'});
  const el=$('vscansum');el.style.display='block';
  el.innerHTML=tot?`Found <b>${tot.toLocaleString()}</b> videos (${fmtBytes(bytes)}) of these types.`+(live?` <b>${live.toLocaleString()}</b> of the .mov files are <b>Live Photo videos</b> (left alone unless you tick the Live Photo option below).`:''):'No videos of these types were found in the folders at the top.'};

// ---- Merge folders
$('mgo').onclick=async()=>{
  if(!roots().length){alert('Add the folders to merge in the bar at the top first');return}
  const mv=$('mgmove').checked;
  if(!mv&&!dest()){alert('Choose a Destination at the top, or tick "Move" to merge everything into the first source folder');return}
  if(mv&&!$('mgdry').checked&&!confirm(dest()?'MOVE will take the files out of your source folders and merge them into '+dest()+'. Make sure you have a backup. Continue?':'MOVE will merge everything into '+roots()[0]+' and take files out of the other folders. Make sure you have a backup. Continue?'))return;
  $('sum').style.display='none';
  const r=await post('/api/merge_start',{roots:roots(),dest:dest(),dry_run:$('mgdry').checked,opts:{move:mv,conflict:$('mgconf').value,dupes:$('mgdup').value,tidy:$('mgtidy').checked,nocase:$('mgcase').checked,takeout:$('mgtk').checked,global_dedupe:$('mgdd').checked}});
  if(r.error)alert(r.error);else{jobKind='merge';placeResults('merge');$('prog').style.display='block';poll()}};
function showGuided(s){
  const parts=[];
  (s.steps||[]).forEach((st,i)=>{showSummary(st.summary);parts.push('<h2 style="margin-top:18px">Step '+(i+1)+': '+esc(st.title)+'</h2>'+$('sumbody').innerHTML)});
  const tips=(s.tips||[]).map(t=>`<div class="tip" style="border-color:var(--acc)">${esc(t)}</div>`).join('');
  $('sumbody').innerHTML=tips+parts.join('');$('sum').style.display='block'}

function fmtB(b){return b>=1e12?(b/1e12).toFixed(1)+' TB':b>=1e9?(b/1e9).toFixed(1)+' GB':(b/1e6).toFixed(0)+' MB'}
function renderCheck(){
  const d=DOC||{},items=[];
  const n=FOLDERS.length;
  items.push(n?['ok','Takeout added',n+(n===1?' item':' items')+(d.zips?' ('+d.zips+' zip'+(d.zips===1?'':'s')+')':'')]:['','Add your Takeout zip files or folders','use the bar at the top']);
  if(!dest())items.push(['','Choose where the finished library goes','Destination, in the bar at the top']);
  else if(d.dest_ok===false)items.push(['bad','Cannot write to the Destination','choose another folder']);
  else{const low=d.need&&d.free!=null&&d.free<d.need*1.1;items.push([low?'bad':'ok','Destination ready',d.free!=null?fmtB(d.free)+' free'+(d.need?', Takeout is '+fmtB(d.need):''):''])}
  items.push(d.exiftool?['ok','ExifTool found','version '+d.exiftool]:(DOC?['bad','ExifTool is missing','install it: brew install exiftool']:['','Checking ExifTool...','']));
  items.push(DOC&&!d.ffmpeg?['opt2','ffmpeg not found','only needed to convert old videos']:['ok','ffmpeg found','for video conversion']);
  $('gcheck').innerHTML=items.map(i=>`<div class="gc ${i[0]}"><i>${i[0]==='ok'?'&#10003;':i[0]==='bad'?'!':i[0]==='opt2'?'&ndash;':'&middot;'}</i><span>${esc(i[1])}</span><small>${esc(i[2])}</small></div>`).join('');
}
async function refreshDoctor(){try{DOC=await post('/api/doctor',{roots:roots(),dest:dest()})}catch(e){}renderCheck()}
function scheduleDoctor(){renderCheck();clearTimeout(docTimer);docTimer=setTimeout(refreshDoctor,400)}
$('gst').onclick=async()=>{
  if(DOC&&!DOC.exiftool){alert('ExifTool is missing. In Terminal run: brew install exiftool');return}
  if(!roots().length){alert('Add your Takeout zip files or folders in the bar at the top first');return}
  if(!dest()){alert('Choose a Destination in the bar at the top. That is where your finished library will be created.');return}
  const real=!$('gdry').checked;
  if(real&&$('gcv').checked&&!confirm('After the library is built, old videos in the Destination will be converted to MP4 and the originals moved into an _original_videos folder. Continue?'))return;
  $('sum').style.display='none';curGuided=true;
  const r=await post('/api/guided_start',{roots:roots(),out:dest(),dry_run:$('gdry').checked,opts:{fix_ext:$('gext').checked,convert:$('gcv').checked,replace:$('gow').checked,live:$('glive').checked,dedupe:$('gdedupe').checked,name_dates:$('gnd').checked,albums:$('galb').checked,edited:$('gedit').value}});
  if(r.error){alert(r.error);curGuided=false}else{placeResults('guided');$('prog').style.display='block';poll()}};

// ---- Help: user guide (rendered from USER_GUIDE.md), safety notice, support, about
function slug(t){return t.toLowerCase().replace(/[^a-z0-9 -]/g,'').trim().replace(/\s+/g,'-')}
function mdInline(t){
  return t.split(/(`[^`]+`)/).map(p=>{
    if(p.length>1&&p[0]==='`'&&p[p.length-1]==='`')return '<code>'+esc(p.slice(1,-1))+'</code>';
    p=esc(p).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/(^|[^*\w])\*([^*\s][^*]*?)\*/g,'$1<i>$2</i>');
    return p.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(m,a,b)=>/^(https?:|mailto:)/.test(b)?`<a href="${b}" target="_blank" rel="noopener">${a}</a>`:b[0]==='#'?`<a href="${b}" data-jump="${b.slice(1)}">${a}</a>`:a)}).join('')}
function md(src){
  const L=src.replace(/\r/g,'').split('\n');let h='',i=0,para=[];
  const flush=()=>{if(para.length){h+='<p>'+mdInline(para.join(' '))+'</p>';para=[]}};
  while(i<L.length){let l=L[i];
    if(/^```/.test(l)){flush();let c=[];i++;while(i<L.length&&!/^```/.test(L[i]))c.push(L[i++]);i++;h+='<pre><code>'+esc(c.join('\n'))+'</code></pre>';continue}
    let m=l.match(/^(#{1,4})\s+(.*)$/);
    if(m){flush();const n=m[1].length;h+=`<h${n} id="${slug(m[2])}">${mdInline(m[2])}</h${n}>`;i++;continue}
    if(/^\s*\|.*\|\s*$/.test(l)&&i+1<L.length&&/^\s*\|[\s:|-]+\|\s*$/.test(L[i+1])){flush();
      const cells=r=>r.trim().replace(/^\||\|$/g,'').split('|').map(x=>x.trim());
      const head=cells(l);i+=2;const rows=[];while(i<L.length&&/^\s*\|.*\|\s*$/.test(L[i]))rows.push(cells(L[i++]));
      h+='<table><tr>'+head.map(x=>'<th>'+mdInline(x)+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+mdInline(x)+'</td>').join('')+'</tr>').join('')+'</table>';continue}
    if(/^\s*([-*]|\d+\.)\s+/.test(l)){flush();const ord=/^\s*\d+\./.test(l);let items=[];
      while(i<L.length&&/^\s*([-*]|\d+\.)\s+/.test(L[i])||(i<L.length&&/^\s{2,}\S/.test(L[i])&&items.length)){
        if(/^\s*([-*]|\d+\.)\s+/.test(L[i]))items.push(L[i].replace(/^\s*([-*]|\d+\.)\s+/,''));else items[items.length-1]+=' '+L[i].trim();i++}
      h+=(ord?'<ol>':'<ul>')+items.map(x=>'<li>'+mdInline(x)+'</li>').join('')+(ord?'</ol>':'</ul>');continue}
    if(/^>\s?/.test(l)){flush();h+='<blockquote>'+mdInline(l.replace(/^>\s?/,''))+'</blockquote>';i++;continue}
    if(/^---+\s*$/.test(l)){flush();h+='<hr>';i++;continue}
    if(!l.trim()){flush();i++;continue}
    para.push(l.trim());i++}
  flush();return h}
let GUIDE=null;
async function loadGuide(){if(!GUIDE)GUIDE=await post('/api/guide');return GUIDE}
function jumpTo(id){const e=document.getElementById(id);if(e){e.scrollIntoView({behavior:'smooth',block:'start'})}}
function bindJumps(root){root.querySelectorAll('a[data-jump]').forEach(a=>a.onclick=ev=>{ev.preventDefault();const id=a.dataset.jump;if(id==='support'){helpView('support');return}if(id==='safety-limitations-and-disclaimer'){helpView('safety');return}jumpTo(id)})}
function guideSections(text){const parts=text.split(/\n(?=## )/);return parts}
async function helpView(v){
  document.querySelectorAll('#hsubnav button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
  if(!document.getElementById('pane-help').offsetParent)showTab('help');
  const box=$('hview');box.innerHTML='<small style="margin:0">Loading...</small>';
  const g=await loadGuide();const email=g.email||'thestocksoup@gmail.com';
  if(v==='guide'){
    if(!g.guide){box.innerHTML='<p>The guide could not be loaded (it needs the USER_GUIDE.md file, or an internet connection). You can read it at <a href="https://github.com/daviddef/MetadataFixer/blob/main/USER_GUIDE.md" target="_blank" rel="noopener">github.com/daviddef/MetadataFixer</a>.</p>';return}
    const secs=guideSections(g.guide);
    const titles=secs.map(x=>(x.match(/^#{1,2}\s+(.*)$/m)||[])[1]).filter(Boolean);
    box.innerHTML='<div class="gtools"><input type="text" id="gsearch" placeholder="Search the guide..." spellcheck="false"><select id="gjump"><option value="">Jump to a section...</option>'+secs.slice(1).map(x=>{const t=(x.match(/^##\s+(.*)$/m)||[])[1];return t?`<option value="${slug(t)}">${esc(t)}</option>`:''}).join('')+'</select></div><div class="md" id="gmd">'+secs.map((x,k)=>`<div class="gsec">${md(x)}</div>`).join('')+'</div>';
    bindJumps(box);$('gjump').onchange=e=>{if(e.target.value)jumpTo(e.target.value);e.target.value=''};
    $('gsearch').oninput=e=>{const q=e.target.value.trim().toLowerCase();document.querySelectorAll('#gmd .gsec').forEach(s=>{s.style.display=!q||s.textContent.toLowerCase().includes(q)?'':'none'})};
    return}
  if(v==='safety'){
    const secs=guideSections(g.guide||'');const sec=secs.find(x=>/^##\s+Safety, limitations and disclaimer/m.test(x));
    box.innerHTML='<div class="md">'+(sec?md(sec):'<h2>Safety and disclaimer</h2><p>Back up your photos first. This free software is provided as is, without warranty, and you use it at your own risk. To the maximum extent permitted by law the author is not liable for any loss or damage.</p>')+'</div>';return}
  if(v==='support'){
    box.innerHTML=`<div class="md"><h2>Support</h2><p>Email <a href="mailto:${email}">${email}</a>. This is a free project, so replies are best-effort with no guaranteed response time.</p><p>To help us help you, include what you were trying to do, what happened, and the diagnostic info below. Please do not send your photos.</p></div>
    <div class="hbtns"><button class="p" id="sup1">Copy diagnostic info and email support</button><button id="sup2">Copy support email address</button></div><small id="supnote"></small>`;
    $('sup1').onclick=async()=>{const r=await post('/api/diagnostics');let ok=false;try{await navigator.clipboard.writeText(r.text);ok=true}catch(e){}
      $('supnote').textContent=ok?'Diagnostic info copied. Paste it into the email (Cmd+V). Check it for private paths first.':'Could not copy automatically: use History > Copy diagnostic info.';
      location.href='mailto:'+email+'?subject='+encodeURIComponent('Backstory support ('+g.version+')')+'&body='+encodeURIComponent('What I was trying to do:\n\nWhat happened:\n\nDiagnostic info (paste here):\n')};
    $('sup2').onclick=async()=>{try{await navigator.clipboard.writeText(email);$('supnote').textContent='Copied '+email}catch(e){prompt('Support email:',email)}};return}
  if(v==='about'){
    box.innerHTML=`<div class="md"><h2>About</h2><p><b>Backstory</b> version ${esc(g.version)}. Free, open source (MIT License). Everything runs on your computer; nothing is uploaded.</p><p>Not affiliated with Google or Apple. Support: <a href="mailto:${email}">${email}</a>. Project: <a href="https://github.com/daviddef/MetadataFixer" target="_blank" rel="noopener">github.com/daviddef/MetadataFixer</a></p>${md(g.notices||'')}<h2>License</h2><pre>${esc(g.license||'MIT License')}</pre></div>`;return}
}
document.querySelectorAll('#hsubnav button').forEach(b=>b.onclick=()=>helpView(b.dataset.v));
document.querySelectorAll('a[data-help]').forEach(a=>a.onclick=e=>{e.preventDefault();showTab('help');helpView(a.dataset.help)});
// first-run notice
(function(){let ok=false;try{ok=localStorage.getItem('ack-v1')==='1'}catch(e){}
  const ack=$('ack');if(ok)return;ack.style.display='flex';
  $('ackbox').onchange=()=>{$('ackgo').disabled=!$('ackbox').checked};
  $('ackgo').onclick=()=>{try{localStorage.setItem('ack-v1','1')}catch(e){}ack.style.display='none'};
  $('ackmore').onclick=()=>{ack.style.display='none';showTab('help');helpView('safety');
    const back=document.createElement('div');back.className='tip';back.innerHTML='Read the notice, then <a href="#" id="ackback">go back and accept it</a>.';$('hview').prepend(back);$('ackback').onclick=e=>{e.preventDefault();ack.style.display='flex'}}})();

const RECMAP={albums:'galb',dedupe:'gdedupe',live:'glive',name_dates:'gnd',fix_ext:'gext',replace:'gow'};
function applyRecs(s){
  Object.entries(RECMAP).forEach(([k,id])=>{const present=(s.recs||[]).find(r=>r.id===k);const cb=document.getElementById('rc_'+k);$(id).checked=!!present&&(!cb||cb.checked)});
  $('gcv').checked=false}
function showAssess(s){
  const f=s.facts||{};
  let h='<div class="tiles">'+s.tiles.map(t=>tile(t[0],t[1],t[2])).join('')+'</div><small>Total size of photos and videos: <b>'+esc(s.size)+'</b>'+(f.zips?' &middot; '+f.zips+' zip file'+(f.zips===1?'':'s'):'')+(f.folders?' &middot; '+f.folders+' folder'+(f.folders===1?'':'s'):'')+'</small>';
  h+=(s.warnings||[]).map(w=>`<div class="tip">${esc(w)}</div>`).join('');
  const TL={guided:'Guided',fix:'Fix',merge:'Merge',clean:'Clean up',convert:'Convert',photos:'Photos',similar:'Similar',health:'Health',history:'History'};
  if((s.sources||[]).length>1)h+='<h2>Your '+s.sources.length+' sources</h2>'+tbl(['Source','Type','Photos and videos','Size'],s.sources.map(x=>[esc(x.label),esc(x.kind),x.media.toLocaleString(),esc(fmtB(x.bytes))]))+((s.overlap||[]).length?'<small>Shared between sources: '+s.overlap.map(o=>esc(o.a)+' and '+esc(o.b)+': <b>'+o.n.toLocaleString()+'</b> identical files ('+esc(fmtB(o.bytes))+')').join('; ')+'</small>':'');
  if((s.flow||[]).length)h+='<h2>Your suggested route</h2><div class="flow">'+s.flow.map(f=>`<div class="flowstep ${f.kind}"><span class="fn">${f.n}</span><div style="flex:1;min-width:0"><b>${esc(f.title)}</b> <span class="badge ${f.kind==='do'?'okb':''}">${f.kind==='done'?'Done':f.kind==='do'?'Recommended':'Optional'}</span><div class="why">${esc(f.why)}</div></div>${f.kind==='done'?'':`<button class="sm" data-tab="${f.tab}">Open ${esc(TL[f.tab]||f.tab)}</button>`}</div>`).join('')+'</div>';
  h+='<h2>Settings for the library build</h2><small style="margin-top:0">Each option has the reason and the numbers from your files. Untick anything you do not want.</small>';
  h+=(s.recs||[]).map(r=>`<label class="rec"><input type="checkbox" id="rc_${r.id}" checked ${r.fixed?'disabled':''}><div><b>${esc(r.title)}</b> <span class="badge ${r.risk==='safe'?'okb':'warnb'}">${r.risk==='safe'?'Safe':'Check this'}</span><div class="why">${esc(r.why)}</div></div></label>`).join('');
  if((s.extras||[]).length)h+='<h2>Other tools that could help</h2>'+s.extras.map(r=>`<div class="rec"><div style="flex:1"><b>${esc(r.title)}</b> <span class="badge warnb">Check this</span><div class="why">${esc(r.why)}</div></div><button class="sm" data-tab="${r.tab}">Open ${esc(TL[r.tab]||r.tab)} tab</button></div>`).join('');
  h+='<div class="hbtns" style="margin-top:14px"><button class="p" id="rprev">Preview the recommended plan</button><button id="rapply">Apply these settings</button></div><small>A preview changes nothing. These are suggestions from a quick look: always read the preview before a real run.</small>';
  if((s.by_ext||[]).length)h+='<h2>What is in your files</h2>'+tbl(['Type','Files'],s.by_ext.map(r=>[esc(r[0]),r[1].toLocaleString()]));
  $('sumbody').innerHTML=h;$('sum').style.display='block';
  document.querySelectorAll('#sumbody button[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
  const rp=$('rprev'),ra=$('rapply');if(rp){rp.onclick=()=>{applyRecs(s);$('gdry').checked=true;$('gst').click()};ra.onclick=()=>{applyRecs(s);ra.textContent='Applied to the settings above';$('gdry').checked=true;window.scrollTo({top:0,behavior:'smooth'})}}}
$('gchk').onclick=async()=>{
  if(!roots().length){alert('Add your Takeout zip files or folders in the bar at the top first');return}
  $('sum').style.display='none';curGuided=false;
  const r=await post('/api/assess_start',{roots:roots(),out:dest()});
  if(r.error)alert(r.error);else{placeResults('assess');$('prog').style.display='block';poll()}};

let SIM=[];
function renderSim(){
  const o=$('simout');
  if(!SIM.length){o.innerHTML='<div class="tip" style="margin-top:12px">No similar folders found.</div>';return}
  o.innerHTML='<div style="margin-top:12px"><small style="margin-top:0">'+SIM.length+' group'+(SIM.length===1?'':'s')+' found. The folder name shown in the box is the one everything is merged into: change it if you like.</small>'+SIM.map((g,i)=>`<div class="rec simg"><input type="checkbox" data-i="${i}" class="simck" ${g.confidence==='high'?'checked':''}><div style="flex:1;min-width:0"><div><b>${esc(g.parent.split('/').slice(-2).join('/'))}</b> <span class="badge ${g.confidence==='high'?'okb':'warnb'}">${g.confidence==='high'?'Same name, different words':'Check this'}</span></div>
  <div class="why">${g.members.map(m=>esc(m.name)+' <span class="mutes">('+m.files.toLocaleString()+' files)</span>').join('<br>')}</div>
  <div style="margin-top:6px">Merge into: <input type="text" class="simt" data-i="${i}" value="${esc(g.target)}" spellcheck="false" style="max-width:320px"></div></div></div>`).join('')+
  `<div class="opt"><input type="checkbox" id="simdry" checked><div>Preview only<small>Shows what would be merged and changes nothing.</small></div></div>
  <div class="opt"><div style="flex:1"><label for="simdup" style="font-weight:600">&#9888;&#65039; Identical files</label><select id="simdup" class="sel"><option value="delete">Keep one copy and delete the extra (permanent)</option><option value="aside">Keep one copy and move the extra to a _duplicates folder</option></select></div></div>
  <button class="p" id="simgo">Merge the ticked groups</button></div>`;
  $('simgo').onclick=simRun}
async function simRun(){
  const groups=[];document.querySelectorAll('.simck').forEach(c=>{if(c.checked){const i=+c.dataset.i;const t=document.querySelector('.simt[data-i="'+i+'"]').value.trim();groups.push({parent:SIM[i].parent,target:t,members:SIM[i].members.map(m=>m.name)})}});
  if(!groups.length){alert('Tick at least one group');return}
  const real=!$('simdry').checked;
  if(real&&!confirm('This will move files out of '+groups.length+' folder group'+(groups.length===1?'':'s')+' and merge them into one folder each. It cannot be undone from the app. Make sure you have a backup. Continue?'))return;
  if(real&&$('simdup').value==='delete'){const t=prompt('Identical extra copies will be permanently deleted.\nType DELETE to confirm.');if(t!=='DELETE')return}
  $('sum').style.display='none';curGuided=false;
  const r=await post('/api/consolidate_start',{roots:croots(),groups,dry_run:$('simdry').checked,dupes:$('simdup').value});
  if(r.error)alert(r.error);else{placeResults('clean');$('prog').style.display='block';poll()}}
$('simfind').onclick=async()=>{
  if(!croots().length){alert('Add your folders in the bar at the top first');return}
  $('simfind').disabled=true;$('simfind').textContent='Looking...';
  const r=await post('/api/similar_folders',{roots:croots(),maybe:$('simmaybe').checked});
  $('simfind').disabled=false;$('simfind').textContent='Find similar folders';
  if(r.error){alert(r.error);return}SIM=r.groups||[];renderSim()};
function showUndo(s){
  let h=`<div class="tiles">${tile(s.total,'files in the run')}${s.mode==='move'?tile(s.restored,'moved back','ok'):tile(s.removed,'removed','ok')}${tile(s.missing,'already gone')}${s.failed||s.skipped?tile(s.failed+s.skipped,'not undone','bad'):''}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  if((s.fails||[]).length)h+='<h2>Not undone</h2><ul>'+s.fails.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}
async function undoRun(id){
  const i=await post('/api/undo_info',{id});
  if(i.error){alert(i.error);return}
  const msg=i.mode==='move'
    ?'Undo "'+i.title+'"?\n\n'+i.exist.toLocaleString()+' files will be moved back to where they came from. Metadata fixes already written to them stay.\n\nContinue?'
    :'Undo "'+i.title+'"?\n\nThis removes the '+i.exist.toLocaleString()+' files that run created in:\n'+i.dest+'\n\nYour originals and zip files are not touched. Files you added to that folder yourself are not removed. Continue?';
  if(!confirm(msg))return;
  $('sum').style.display='none';curGuided=false;
  const r=await post('/api/undo_start',{id});
  if(r.error)alert(r.error);else{showTab('history');placeResults('undo');$('prog').style.display='block';poll()}}
function showConsolidate(s){
  const w=s.dry_run?'would be ':'';
  let h=`<div class="tiles">${tile(s.merged,'folder groups '+w+'merged','ok')}${tile(s.moved,'files '+w+'moved')}${tile(s.dupes,'identical copies')}${tile(s.conflicts,'name clashes')}${s.failed?tile(s.failed,'problems','bad'):''}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  h+=tbl(['Merged into','In','From','Files','Identical','Result'],s.rows.map(r=>[esc(r.target),esc(r.parent),esc((r.members||[]).join(', ')),r.moved.toLocaleString(),r.dupes.toLocaleString(),esc(r.action+(r.detail?': '+r.detail:''))]));
  $('sumbody').innerHTML=h;$('sum').style.display='block'}

function showSimilar(s){
  let h=`<div class="tiles">${tile(s.scanned,'pictures checked')}${tile(s.total_groups,'similar groups','')}${tile(s.extra,'extra copies','')}</div>`;
  if(!s.groups.length){h+='<div class="tip">No similar photos found at this setting.</div>';$('sumbody').innerHTML=h;$('sum').style.display='block';return}
  h+=`<small>Potential space: <b>${esc(s.reclaim)}</b>. The best copy of each group (largest picture) is kept; the others are ticked to be set aside. Untick any you want to keep. ${s.total_groups>s.groups.length?'Showing the first '+s.groups.length+' groups.':''}</small>`;
  h+=s.groups.map((g,i)=>`<div class="card simgrp"><div class="simrow">${g.map((m,k)=>`<label class="simitem"><img loading="lazy" src="/thumb?p=${encodeURIComponent(m.path)}" alt=""><div class="simcap"><b title="${esc(m.name)}">${esc(m.name)}</b><span>${esc(m.where)}</span><span>${m.w&&m.h?m.w+' &times; '+m.h+' &middot; ':''}${fmtB(m.size)}${m.date?' &middot; '+esc(m.date.slice(0,10)):''}${m.gps?' &middot; has location':''}</span></div>${m.best?'<span class="badge okb">Best: kept</span>':`<span class="simsel"><input type="checkbox" class="simaside" data-p="${esc(m.path)}" checked> Set aside</span>`}</label>`).join('')}</div></div>`).join('');
  h+='<div class="hbtns" style="margin-top:14px"><button class="p" id="simapply">Set aside the ticked photos</button><button id="simnone">Untick all</button><button id="simall">Tick all</button></div><small>They are moved, not deleted, and stay in their folder structure inside <i>_similar_set_aside</i>. You can undo this from History.</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block';
  const setAll=v=>document.querySelectorAll('.simaside').forEach(c=>c.checked=v);
  $('simnone').onclick=()=>setAll(false);$('simall').onclick=()=>setAll(true);
  $('simapply').onclick=async()=>{const items=[...document.querySelectorAll('.simaside')].filter(c=>c.checked).map(c=>c.dataset.p);
    if(!items.length){alert('Nothing is ticked');return}
    if(!confirm('Move '+items.length.toLocaleString()+' photos into a _similar_set_aside folder? Nothing is deleted, and you can undo this from History.'))return;
    const r=await post('/api/similar_apply',{items});if(r.error)alert(r.error);else{placeResults('similar_apply');$('prog').style.display='block';poll()}}}
function showSimilarApply(s){
  let h=`<div class="tiles">${tile(s.moved,'set aside','ok')}${s.failed||s.skipped?tile(s.failed+s.skipped,'not moved','bad'):''}</div>`+s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  if((s.folders||[]).length)h+='<small>Set-aside folder: '+s.folders.map(esc).join(', ')+'</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}
$('simscan').onclick=async()=>{
  if(!roots().length){alert('Add your folders in the bar at the top first');return}
  $('sum').style.display='none';curGuided=false;
  const r=await post('/api/similar_scan',{roots:roots(),threshold:+$('simsens').value});
  if(r.error)alert(r.error);else{placeResults('similar');$('prog').style.display='block';poll()}};

function photosOpts(limit){return {batch_gb:+$('pbatch').value,keep_free_gb:+$('pfree').value,pace:$('ppace').value,albums:$('palb').checked,order:'oldest',limit:limit||null,adaptive:$('padapt').checked,library:($('uplib')||{}).value||''}}
async function renderPCheck(){
  $('proot').textContent=roots()[0]||'none chosen yet: add your finished library folder in the bar at the top';
  let p={};try{p=await post('/api/photos_preflight')}catch(e){}
  const it=[];
  it.push(roots().length?['ok','Library chosen',(roots()[0]||'').split('/').filter(Boolean).pop()||'']:['','Add your finished library folder','top bar']);
  it.push(p.mac&&p.osascript&&p.photos_app?['ok','Photos app found','']:['bad','Needs a Mac with the Photos app','imports are not possible here']);
  it.push(p.free!=null?[p.free<20e9?'bad':'ok','Free space on this Mac',fmtB(p.free)]:['','Free space unknown','']);
  $('pcheck').innerHTML=it.map(i=>`<div class="gc ${i[0]}"><i>${i[0]==='ok'?'&#10003;':i[0]==='bad'?'!':'&middot;'}</i><span>${esc(i[1])}</span><small>${esc(i[2])}</small></div>`).join('')}
async function photosGo(limit){
  if(!roots().length){alert('Add your finished library folder in the bar at the top first');return}
  const real=!$('pdry').checked;
  if(real&&!confirm('This sends photos to the Photos app'+(limit?' (a test of '+limit+' photos)':' in batches')+'.\nPhotos has no undo for imports. Photos you already have are skipped.\n\nContinue?'))return;
  $('sum').style.display='none';curGuided=false;
  const r=await post('/api/photos_start',{roots:roots(),dry_run:!real,opts:photosOpts(limit)});
  if(r.error)alert(r.error);else{placeResults('photos');$('prog').style.display='block';poll()}}
$('pgo').onclick=()=>photosGo(null);
$('ptest').onclick=()=>{$('pdry').checked=false;photosGo(20)};
function showPhotos(s){
  const w=s.dry_run?'planned':'sent';
  let h=`<div class="tiles">${tile(s.dry_run?s.files:s.imported,'files '+w,'ok')}${tile(s.batches,'batches')}${tile(s.already,'already sent earlier')}${s.unsupported&&s.unsupported.length?tile(s.unsupported.reduce((a,b)=>a+b[1],0),'not importable','bad'):''}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  if((s.unsupported||[]).length)h+='<small>Formats Photos cannot import: '+s.unsupported.map(u=>esc(u[0])+' ('+u[1]+')').join(', ')+'. Convert them on the Convert tab.</small>';
  if((s.rows||[]).length)h+='<h2>Batches</h2>'+tbl(['#','Files','Size','From','To','Albums','Status'],s.rows.map(r=>[r.n,r.files.toLocaleString(),esc(r.bytes),esc(r.from),esc(r.to),esc(r.albums),esc(r.status)]));
  $('sumbody').innerHTML=h;$('sum').style.display='block'}

const HCATS={space:'Space and duplicates',folders:'Folders',files:'Files and ghosts',cloud:'iCloud and missing files',meta:'Dates and metadata'};
const HTL={guided:'Guided',merge:'Merge',clean:'Clean up',convert:'Convert',similar:'Similar',photos:'Photos'};
function spark(tr){
  if(!tr||tr.length<2)return '';
  const w=260,h=50,xs=tr.map((t,i)=>i/(tr.length-1)*w),ys=tr.map(t=>h-(t.score/100)*h);
  return `<svg viewBox="0 0 ${w} ${h+4}" width="${w}" height="${h+4}" style="display:block"><polyline fill="none" stroke="var(--acc)" stroke-width="2.5" points="${xs.map((x,i)=>x.toFixed(1)+','+(ys[i]+2).toFixed(1)).join(' ')}"/>${xs.map((x,i)=>`<circle cx="${x.toFixed(1)}" cy="${(ys[i]+2).toFixed(1)}" r="3" fill="var(--acc)"/>`).join('')}</svg>`}
function showHealth(s){
  const st=s.stats,col=s.score>=80?'var(--ok)':s.score>=60?'var(--warn)':'var(--bad)';
  const word=s.score>=85?'Healthy':s.score>=70?'Fair':s.score>=50?'Needs attention':'Needs work';
  let h=`<div class="hscore"><div class="ring" style="--p:${s.score};--c:${col}"><b>${s.score}</b><span>out of 100</span></div><div style="flex:1;min-width:0"><div style="font-size:20px;font-weight:700">${word}</div>
  <div class="parts">${Object.entries(s.parts).map(([k,v])=>`<div class="part"><span>${esc(({space:'Space',folders:'Folders',files:'Files',cloud:'Cloud',metadata:'Metadata'})[k]||k)}</span><div class="pbar"><i style="width:${v}%;background:${v>=80?'var(--ok)':v>=60?'var(--warn)':'var(--bad)'}"></i></div><em>${v}</em></div>`).join('')}</div></div>${s.trend&&s.trend.length>1?`<div><small style="margin:0 0 4px">Score history</small>${spark(s.trend)}</div>`:''}</div>`;
  h+=`<div class="tiles">${tile(st.files,'photos and videos')}<div class="tile"><b>${esc(fmtB(st.bytes))}</b><span>library size</span></div><div class="tile ${st.waste_bytes>0?'bad':'ok'}"><b>${esc(fmtB(st.waste_bytes))}</b><span>possible wasted space</span></div>${tile(s.findings.length,'findings')}</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  (s.db||[]).forEach(d=>{h+=d.ok?`<div class="tip">Photos library <b>${esc(d.name)}</b> (experimental hints): ${d.total.toLocaleString()} items${d.not_in_cloud!=null?', '+d.not_in_cloud.toLocaleString()+' not yet in iCloud':''}${d.original_not_on_disk!=null?', '+d.original_not_on_disk.toLocaleString()+' originals not stored on this Mac (normal with Optimize Mac Storage)':''}.</div>`:`<div class="tip">Could not read the database of ${esc(d.name)}: ${esc(d.why||'')}. This check is experimental.</div>`});
  if(!s.findings.length)h+='<div class="tip" style="border-color:var(--ok)">No problems found. Your library looks healthy.</div>';
  Object.keys(HCATS).forEach(cat=>{const L=s.findings.filter(f=>f.cat===cat);if(!L.length)return;
    h+='<h2>'+HCATS[cat]+'</h2>'+L.map(f=>`<div class="rec"><div style="flex:1;min-width:0"><b>${esc(f.title)}</b> <span class="badge ${f.sev==='bad'?'badb':f.sev==='warn'?'warnb':''}">${f.sev==='bad'?'Fix':f.sev==='warn'?'Worth fixing':'For your information'}</span>${f.bytes?` <span class="mutes">&middot; ${esc(fmtB(f.bytes))}</span>`:''}<div class="why">${esc(f.detail)}</div>${(f.examples||[]).length?'<div class="why">'+f.examples.map(esc).join('<br>')+'</div>':''}</div>${f.tab&&HTL[f.tab]?`<button class="sm" data-tab="${f.tab}">${esc(f.label||'Open')}</button>`:''}</div>`).join('')});
  if((s.formats||[]).length){
    h+=`<h2>The same file in different formats</h2><small style="margin-top:0">Each group is one picture or video saved in more than one format, usually after converting and keeping the old copy. The best copy of each group is kept (ticked boxes are the older formats). They are moved to <i>_older_formats</i>, not deleted, and you can undo it from History. ${s.formats_total>s.formats.length?'Showing the first '+s.formats.length+' of '+s.formats_total+' groups.':''}</small>`;
    h+=s.formats.map(g=>`<div class="card simgrp"><b>${esc(g.stem)}</b> <span class="mutes">in ${esc(g.where)}</span><div style="margin-top:6px">${g.members.map(m=>`<label class="fmtrow"><span>${m.best?'<span class="badge okb">Keep</span>':`<input type="checkbox" class="fmtaside" data-p="${esc(m.path)}" checked>`}</span><b>${esc(m.ext)}</b><span>${esc(fmtB(m.size))}</span><span class="mutes">${m.duration?m.duration+'s':''}${m.w?' &middot; '+m.w+'&times;'+m.h:''}</span></label>`).join('')}</div></div>`).join('');
    h+='<div class="hbtns" style="margin-top:12px"><button class="p" id="fmtapply">Set aside the ticked older formats</button><button id="fmtnone">Untick all</button></div>'}
  const ex=st.by_ext||[];
  h+='<h2>Your library in numbers</h2>';
  if(ex.length){const mx=Math.max(...ex.map(e=>e[2]));h+=tbl(['Type','Files','Size',''],ex.map(e=>[esc(e[0]),e[1].toLocaleString(),esc(fmtB(e[2])),`<span class="mini" style="width:${Math.round(120*e[2]/mx)}px;background:var(--acc)"></span>`]))}
  if((st.years||[]).length){const my=Math.max(...st.years.map(y=>y[1]));h+='<small>Photos and videos by year (by file date)</small><div class="years">'+st.years.map(y=>`<div class="yr" title="${y[0]}: ${y[1].toLocaleString()}"><i style="height:${Math.max(3,Math.round(70*y[1]/my))}px"></i><span>${esc(y[0].slice(2))}</span></div>`).join('')+'</div>'}
  const r=st.raw||{};
  if(r.total)h+=`<div class="tip"><b>RAW photos:</b> ${r.total.toLocaleString()} (${esc(fmtB(r.bytes))}). ${r.paired.toLocaleString()} sit next to a JPEG or HEIC with the same name (the RAW files take ${esc(fmtB(r.paired_raw_bytes))}, the JPEGs ${esc(fmtB(r.paired_jpg_bytes))}), and ${r.raw_only.toLocaleString()} have no JPEG. Keeping both is normal for editing; if you never edit RAW files, the RAWs of those pairs are the biggest single saving. Backstory never deletes them for you.</div>`;
  if(st.live_pairs)h+=`<div class="tip"><b>Live Photos:</b> ${st.live_pairs.toLocaleString()} photos have a matching video, kept together.</div>`;
  if(st.deep&&st.deep.models&&st.deep.models.length)h+='<small>Most common cameras (in the files checked): '+st.deep.models.map(m=>esc(m[0])+' ('+m[1]+')').join(', ')+'</small>';
  if((st.top_folders||[]).length)h+='<h2>Biggest folders</h2>'+tbl(['Folder','Size'],st.top_folders.map(f=>[esc(f[0]),esc(fmtB(f[1]))]));
  if((st.biggest||[]).length)h+='<h2>Biggest files</h2>'+tbl(['File','Size'],st.biggest.map(f=>[esc(f[0]),esc(fmtB(f[2]))]));
  $('sumbody').innerHTML=h;$('sum').style.display='block';
  document.querySelectorAll('#sumbody button[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
  const fa=$('fmtapply');if(fa){$('fmtnone').onclick=()=>document.querySelectorAll('.fmtaside').forEach(c=>c.checked=false);
    fa.onclick=async()=>{const items=[...document.querySelectorAll('.fmtaside')].filter(c=>c.checked).map(c=>c.dataset.p);if(!items.length){alert('Nothing is ticked');return}
      if(!confirm('Move '+items.length.toLocaleString()+' older-format files into an _older_formats folder? Nothing is deleted, and you can undo this from History.'))return;
      const r=await post('/api/formats_apply',{items});if(r.error)alert(r.error);else{placeResults('formats_apply');$('prog').style.display='block';poll()}}}
}
async function startHealth(quiet){
  if(!roots().length){if(!quiet)alert('Add your library folders in the bar at the top first');return}
  if(!quiet){$('sum').style.display='none'}curGuided=false;
  const r=await post('/api/health_start',{roots:roots(),deep:$('hdeep').checked});
  try{localStorage.setItem('health_last',String(Date.now()))}catch(e){}
  if(r.error){if(!quiet)alert(r.error)}else{placeResults('health');$('prog').style.display='block';poll()}}
$('hgo').onclick=()=>startHealth(false);
(function(){let a='0';try{a=localStorage.getItem('health_auto')||'0'}catch(e){}$('hauto').value=a;
  $('hauto').onchange=()=>{try{localStorage.setItem('health_auto',$('hauto').value)}catch(e){}};
  setInterval(async()=>{const hrs=+$('hauto').value;if(!hrs||!roots().length)return;let last=0;try{last=+localStorage.getItem('health_last')||0}catch(e){}
    if(Date.now()-last<hrs*3600e3)return;try{const s=await (await fetch('/api/status')).json();if(s.state==='scanning'||s.state==='running')return}catch(e){return}
    startHealth(true)},60000)})();

function fmtEta(h){if(!h)return '';return h<1?Math.max(1,Math.round(h*60))+' minutes':h<48?h.toFixed(1)+' hours':Math.round(h/24)+' days'}
async function loadLibs(){try{const r=await post('/api/photos_libs');const sel=$('uplib');const cur=sel.value;sel.innerHTML='<option value="">Find it automatically</option>'+(r.libs||[]).map(l=>`<option value="${esc(l)}">${esc(l.split('/').slice(-2).join('/'))}</option>`).join('');sel.value=cur}catch(e){}}
async function checkUpload(){
  $('upgo').disabled=true;$('upres').innerHTML='<small>Reading the Photos database...</small>';
  const r=await post('/api/upload_status',{library:$('uplib').value,root:$('upsent').checked?(roots()[0]||''):''});
  $('upgo').disabled=false;
  if(!r.ok){$('upres').innerHTML=`<div class="tip">Could not read the Photos database${r.why?': '+esc(r.why):''}. This needs a Mac with a Photos library; the check is experimental.</div>`;return}
  const pct=r.total&&r.uploaded!=null?Math.round(100*r.uploaded/r.total):null;
  let h='';
  if(r.icloud_on===false)h+='<div class="tip">iCloud Photos looks <b>switched off</b> for this library: nothing is marked as uploaded. Turn it on in Photos > Settings > iCloud.</div>';
  if(r.uploaded!=null)h+=`<div class="tiles">${tile(r.total,'items in Photos')}${tile(r.uploaded,'in iCloud','ok')}${tile(r.pending,'waiting to upload',r.pending?'bad':'ok')}</div><div class="bar" style="margin:8px 0"><i style="width:${pct}%"></i></div>`;
  else h+='<div class="tip">This version of Photos does not expose upload state, so only the item count is available: '+r.total.toLocaleString()+' items.</div>';
  const eta=r.eta||{};
  if(eta.stalled)h+='<div class="tip" style="border-color:var(--bad)"><b>Uploads look stuck:</b> the number waiting has not fallen for about 45 minutes. <a href="#" id="whystuck">Check the logs for the cause</a> (Low Power Mode, a paused sync, no iCloud space and a lost network are the usual ones).</div>';
  else if(eta.rate_per_hour>0&&eta.eta_hours)h+=`<div class="tip" style="border-color:var(--acc)">Uploading about <b>${eta.rate_per_hour.toLocaleString()}</b> items an hour. About <b>${fmtEta(eta.eta_hours)}</b> left at that speed.</div>`;
  else if(r.pending>0&&(eta.points||0)<2)h+='<div class="tip">Press the button again in a few minutes and Backstory will work out the upload speed and time left.</div>';
  if(r.sent!=null&&r.sent>0){
    if(r.wanted_note)h+='<div class="tip">Could not verify the files Backstory sent ('+esc(r.wanted_note)+').</div>';
    else if(r.matched!=null){const ok=r.matched_uploaded===r.matched&&r.not_found===0;
      h+=`<div class="tip" style="border-color:${ok?'var(--ok)':'var(--warn)'}"><b>Files Backstory sent:</b> ${r.matched.toLocaleString()} of ${r.sent.toLocaleString()} are in Photos, ${r.matched_uploaded.toLocaleString()} of those are in iCloud${r.not_found?`; ${r.not_found.toLocaleString()} were not found in Photos (skipped as duplicates, still importing, or renamed)`:''}.${(r.missing_examples||[]).length?'<br>Not found: '+r.missing_examples.map(esc).join(', '):''}</div>`;
      if(ok)h+='<h2>Ready to retire the staging copy?</h2><div class="rec"><div class="why" style="margin:0">All files Backstory sent are in iCloud. Before you remove anything: (1) look through Photos and on iCloud.com (Photos) for a few years and albums; (2) compare the item counts; (3) keep a backup of your library on another drive; (4) <b>keep your Takeout zip files and the staging drive until you are satisfied</b>. The zips hold Google\'s original information and are your only copy of it.</div></div>'}}
  $('upres').innerHTML=h;const w=$('whystuck');if(w)w.onclick=e=>{e.preventDefault();startMonitor(false)}}
$('upgo').onclick=checkUpload;
async function startMonitor(quiet,pasted){
  if(!quiet){$('sum').style.display='none'}curGuided=false;
  const r=await post('/api/monitor_start',{hours:+$('mhours').value,pasted:pasted||''});
  try{localStorage.setItem('mon_last',String(Date.now()))}catch(e){}
  if(r.error){if(!quiet)alert(r.error)}else{placeResults('monitor');$('prog').style.display='block';poll()}}
$('mgo').onclick=()=>startMonitor(false);
$('mpastego').onclick=()=>{const t=$('mpaste').value;if(!t.trim()){alert('Paste some log text first');return}startMonitor(false,t)};
function showMonitor(s){
  const src=Object.entries(s.sources||{}).map(([k,v])=>({mac_log:'macOS log',crash_reports:'crash reports',backstory:'Backstory runs',pasted:'pasted text'}[k]+': '+v.toLocaleString()+' lines')).join(' &middot; ');
  let h=`<div class="tiles">${tile(s.issues.length,'issues found',s.issues.some(i=>i.sev==='bad')?'bad':s.issues.length?'':'ok')}${tile(s.lines,'log lines read')}${tile((s.other||[]).length,'unrecognised errors')}</div><small>${src}</small>`;
  if(s.note)h+=`<div class="tip">${esc(s.note)}</div>`;
  h+=(s.tips||[]).map(t=>`<div class="tip" style="border-color:var(--ok)">${esc(t)}</div>`).join('');
  h+=s.issues.map(i=>`<div class="card"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:16px">${esc(i.title)}</b><span class="badge ${i.sev==='bad'?'badb':i.sev==='warn'?'warnb':''}">${i.sev==='bad'?'Fix':i.sev==='warn'?'Worth fixing':'For your information'}</span>${i.new?'<span class="badge okb">New</span>':''}<span class="mutes">${i.count.toLocaleString()} time${i.count===1?'':'s'}${i.last&&i.last!==i.first?' &middot; last '+esc(i.last):''}</span></div>
  <div class="why" style="margin:6px 0">${esc(i.meaning)}</div><b style="font-size:13px">What to do</b><ol style="margin:4px 0 8px;padding-left:20px">${i.fixes.map(f=>'<li>'+esc(f)+'</li>').join('')}</ol>
  <details><summary class="mutes">Show the log lines</summary><pre style="white-space:pre-wrap;font-size:11.5px;margin:6px 0">${i.examples.map(esc).join('\n')}</pre></details></div>`).join('');
  if((s.other||[]).length)h+='<h2>Errors Backstory does not recognise</h2><small style="margin-top:0">These are errors without a known explanation. If something is not working, copy them into an email to support.</small><pre style="white-space:pre-wrap;font-size:11.5px">'+s.other.map(o=>esc(o.text)+'  (x'+o.count+')').join('\n')+'</pre>';
  $('sumbody').innerHTML=h;$('sum').style.display='block';
  const bad=s.issues.filter(i=>i.new&&i.sev==='bad');
  if(bad.length&&window.Notification&&Notification.permission==='granted'){try{new Notification('Backstory: '+bad[0].title,{body:bad[0].meaning})}catch(e){}}}
(function(){let a='0';try{a=localStorage.getItem('mon_auto')||'0'}catch(e){}$('mauto').value=a;
  $('mauto').onchange=()=>{try{localStorage.setItem('mon_auto',$('mauto').value)}catch(e){}if($('mauto').value!=='0'&&window.Notification&&Notification.permission==='default')Notification.requestPermission()};
  setInterval(async()=>{const m=+$('mauto').value;if(!m)return;let last=0;try{last=+localStorage.getItem('mon_last')||0}catch(e){}
    if(Date.now()-last<m*60e3)return;try{const s=await (await fetch('/api/status')).json();if(s.state==='scanning'||s.state==='running')return}catch(e){return}
    startMonitor(true)},60000)})();
function showMerge(s){
  const w=s.dry_run?'would be ':'';
  let h=`<div class="tiles">${tile(s.total,'files found')}${tile(s.brought,'files '+w+(s.move?'moved':'copied')+' in','ok')}${tile(s.in_place_files,'already in place')}${tile(s.identical,'identical copies (kept once)')}${tile(s.clashes,'name clashes resolved')}${tile(s.merged_dirs,'folders '+w+'merged from 2+ sources')}${s.json_along?tile(s.json_along,'.json files brought along'):''}${tile(s.failed,'problems',s.failed?'bad':'')}</div>`;
  h+=`<div class="tip" style="border-color:var(--acc)">Merged into <b>${esc(s.dest)}</b>${s.in_place?' (the first source folder)':''}.</div>`;
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  h+='<h2>By source folder</h2>'+tbl(['Source folder','Files','Brought in','Identical','Name clashes','Problems'],s.per_root.map(r=>[esc(r.root.split('/').filter(Boolean).pop()||r.root),r.found.toLocaleString(),r.placed.toLocaleString(),r.identical.toLocaleString(),r.conflicts.toLocaleString(),r.failed.toLocaleString()]));
  if(s.merged.length)h+=`<h2>Folders that ${s.dry_run?'would be ':''}came together from 2 or more sources</h2>`+tbl(['Folder','Sources'],s.merged.map(m=>[esc(m[0]),m[1]]))+(s.merged_dirs>s.merged.length?'<small>Showing '+s.merged.length+' of '+s.merged_dirs+'. See the CSV for all.</small>':'');
  if(s.clash_rows.length)h+='<h2>Name clashes and problems</h2>'+tbl(['File','Result','Detail'],s.clash_rows.map(r=>[esc(r.file),esc(r.status),esc(r.detail)]));
  h+='<small>Saved: a CSV of every file and where it went (on your Desktop).</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}

function fmtDur(sec){sec=Math.max(0,Math.round(sec||0));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return h?h+'h '+String(m).padStart(2,'0')+'m':m?m+'m '+String(s).padStart(2,'0')+'s':s+'s'}
function renderCvLive(s){
  const el=$('cvlive');const c=s.cv;
  if(jobKind!=='convert'||!c||s.state!=='running'||!c.files_total){el.style.display='none';return}
  const n=c.now,pl=c.plan||{};
  const MODEL={remux:'Re-wrap (fast, lossless)',audio:'Audio only',encode:'Re-encode (slow)'};
  const planTxt=Object.entries(pl).filter(([k,v])=>v.n).map(([k,v])=>`${v.n.toLocaleString()} ${({remux:'quick re-wraps',audio:'audio-only fixes',encode:'full re-encodes'})[k]} (${fmtDur(v.dur)} of footage)`).join(' &middot; ');
  const pctAll=c.secs_total?Math.min(100,100*c.secs_done/c.secs_total):0;
  const stopBtn=s.cancel?'<small>Stopping...</small>':'<button id="cvstop" class="sm">Stop</button>';
  let h='<div class="cvbox">';
  if(n){
    const p=n.dur?Math.min(100,100*n.secs/n.dur):0,left=(n.speed&&n.speed>0)?(n.dur-n.secs)/n.speed:null;
    h+=`<div class="cvhead"><b>Now converting</b><span class="mode">${MODEL[n.mode]||n.mode}</span><span style="flex:1"></span>${stopBtn}</div>
    <div class="cvname">${esc(n.name)}</div><small>in ${esc(n.dir)} &middot; ${fmtBytes(n.size)} &middot; ${fmtDur(n.dur)} long</small>
    <div class="mbar" style="margin-top:8px"><i style="width:${p}%"></i><span>${Math.floor(p)}%</span></div>
    <div class="cvmeta">${fmtDur(n.secs)} of ${fmtDur(n.dur)} done${n.speed?` &middot; running at ${n.speed.toFixed(1)}x real time`:''}${left!==null?` &middot; about ${fmtDur(left)} left for this video`:''}</div>`;
  }else h+=`<div class="cvhead"><b>Getting the next video ready...</b><span style="flex:1"></span>${stopBtn}</div>`;
  h+=`<div class="cvgrid"><div><b>${c.files_done.toLocaleString()} of ${c.files_total.toLocaleString()}</b><span>videos finished</span></div>
  <div><b>${fmtDur(c.secs_done)} of ${fmtDur(c.secs_total)}</b><span>of footage processed (${Math.floor(pctAll)}%)</span></div>
  <div><b>${fmtDur(c.elapsed)}</b><span>elapsed</span></div>
  <div><b>${c.eta==null?'...':'about '+fmtDur(c.eta)}</b><span>left (estimate)</span></div>
  <div><b>${c.bytes_before?fmtBytes(Math.max(0,c.bytes_before-c.bytes_after))+' ('+Math.round(100*(1-c.bytes_after/c.bytes_before))+'%)':'-'}</b><span>space saved so far</span></div></div>`;
  h+=`<div class="cvmeta">Plan: ${planTxt||'nothing to convert'}. The time left is an estimate: it gets better as videos finish, and re-encoding speed varies from video to video. Stopping is safe: finished videos are recognised and skipped next time.</div>`;
  if((c.recent||[]).length)h+='<div class="cvrecent"><b>Just finished</b>'+[...c.recent].reverse().map(r=>`<div>&#10003; <span>${esc(r.name)}</span> <span style="color:var(--mute)">${fmtBytes(r.before)} &rarr; ${fmtBytes(r.after)}${r.before?' ('+Math.round(100*(1-r.after/r.before))+'% smaller)':''} &middot; took ${fmtDur(r.wall)}</span></div>`).join('')+'</div>';
  h+='</div>';el.innerHTML=h;el.style.display='block';
  const b=$('cvstop');if(b)b.onclick=async()=>{b.disabled=true;b.textContent='Stopping...';await post('/api/cancel')}}
function showConvert(s){
  const w=s.dry_run?'would be ':'';
  const pc=(b,a)=>b?Math.round(100*(1-a/b)):0;
  let h=`<div class="tiles">${tile(s.total,'videos found')}${tile(s.dry_run?s.would:s.converted,'videos '+w+'converted','ok')}${tile(s.remux,'re-wrapped (lossless)')}${tile(s.encode,'re-encoded')}${tile(s.skipped_live,'Live Photo videos skipped')}${tile(s.failed,'could not convert',s.failed?'bad':'')}</div>`;
  if(s.elapsed)h+=`<div class="tip" style="border-color:var(--acc)">${s.stopped?'<b>Stopped by you.</b> ':''}${s.dry_run?'Checked':'Ran'} for ${fmtDur(s.elapsed)}.</div>`;
  if(!s.dry_run&&s.converted){
    const saved=s.bytes_before-s.bytes_after;
    h+=`<h2>Size before and after</h2><div class="tiles">${tile(fmtBytes(s.bytes_before),'before')}${tile(fmtBytes(s.bytes_after),'after')}${tile((saved>=0?'':'+')+fmtBytes(Math.abs(saved))+' ('+(saved>=0?'':'+')+Math.abs(pc(s.bytes_before,s.bytes_after))+'%)',saved>=0?'space saved':'space used','ok')}</div>`;
    const rows=[];const M={remux:'Re-wrapped (lossless)',audio:'Video kept, audio converted',encode:'Re-encoded'};
    Object.entries(s.by_mode||{}).forEach(([k,v])=>rows.push([M[k]||k,v.n.toLocaleString(),fmtBytes(v.before),fmtBytes(v.after),pc(v.before,v.after)+'%']));
    Object.entries(s.by_ext||{}).forEach(([k,v])=>rows.push([k+' files',v.n.toLocaleString(),fmtBytes(v.before),fmtBytes(v.after),pc(v.before,v.after)+'%']));
    h+=tbl(['','Videos','Before','After','Saved'],rows);
    if((s.top||[]).length)h+='<h2>Biggest savings</h2>'+tbl(['Video','How','Before','After','Saved'],s.top.map(t=>[esc(t.file),M[t.mode]||t.mode,fmtBytes(t.before),fmtBytes(t.after),fmtBytes(Math.max(0,t.before-t.after))+' ('+pc(t.before,t.after)+'%)']));
    h+=`<div class="tip" style="border-color:var(--acc)">Originals: ${({move:'moved to _original_videos (still using disk space until you delete that folder)',keep:'kept in place (still using disk space)',delete:'deleted, so the saving above is real free space'})[s.action]}.</div>`;
  } else if(s.dry_run){
    const e=s.est||{},now=s.bytes_before,after=e.after||0,have=e.asked&&e.samples>0;
    h+=`<h2>Size now and estimated size after</h2><div class="tiles">${tile(fmtBytes(now),'size now')}`
      +(have?tile((e.complete?'':'about ')+fmtBytes(after),'estimated size after','ok')+tile(fmtBytes(Math.max(0,now-after))+' ('+pc(now,after)+'%)','estimated space saved','ok'):tile('not estimated','estimated size after'))+`</div>`;
    h+=have?`<small>The estimate comes from test-encoding ${e.samples} short sample${e.samples===1?'':'s'} with your settings${e.failed?` (${e.failed} could not be sampled)`:''}. Re-wrapped videos are counted at about their current size. Real results vary with each video, typically within a few tens of percent.</small>`
      :`<small>${e.asked?'No samples could be encoded, so no estimate is available.':'Tick "Estimate the new sizes in the preview" and run the preview again to see an estimated size after conversion.'}</small>`;
    const M={remux:'Re-wrapped: stays about the same size, no quality loss',audio:'Video kept, only the audio is converted: about the same size',encode:'Re-encoded: usually much smaller (see the estimate above)'};
    const rows=Object.entries(s.by_mode||{}).map(([k,v])=>[M[k]||k,v.n.toLocaleString(),fmtBytes(v.before)]);
    h+=tbl(['','Videos','Size now'],rows);
    if((s.top||[]).length)h+='<h2>Largest videos to convert</h2>'+tbl(['Video','How','Size now'],s.top.map(t=>[esc(t.file),t.mode==='remux'?'Re-wrap':t.mode==='audio'?'Audio only':'Re-encode',fmtBytes(t.before)]));
  }
  if(Object.keys(s.types||{}).length){
    const dry=s.dry_run,pcT=(b,a)=>b?Math.round(100*(1-a/b)):0;
    const aft=v=>dry?(v.est_after==null?'n/a':'~'+fmtBytes(v.est_after)):(v.converted?fmtBytes(v.after):'-');
    const sav=v=>{const a=dry?v.est_after:v.after;if(a==null||!v.conv_before||!v.converted)return '-';return fmtBytes(Math.max(0,v.conv_before-a))+' ('+pcT(v.conv_before,a)+'%)'};
    h+='<h2>By video type: size now and '+(dry?'estimated size after':'size after')+'</h2>'+tbl(['Type','Found','Size now',dry?'Est. size after':'Size after',dry?'Est. saved':'Saved',dry?'Would convert':'Converted','Live Photo (skipped)','Problems'],
      Object.entries(s.types).map(([k,v])=>[k,v.found.toLocaleString(),fmtBytes(v.bytes),aft(v),sav(v),v.converted.toLocaleString(),v.live.toLocaleString(),v.failed.toLocaleString()]))}
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  if((s.failures||[]).length)h+='<h2>Problems</h2>'+tbl(['File','Result','Detail'],s.failures.map(f=>[esc(f.file),esc(f.status),esc(f.detail)]));
  h+='<small>Saved: a CSV with the before and after size of every video (on your Desktop).</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}

function liveTiles(s,c,done,nj){
  if(!s.total)return '';const x=s.extra||{};const err=(x.errors?tile(x.errors,'files with errors','bad'):'');
  if(['assess','consolidate','undo','similar','similar_apply','photos','health','formats_apply','monitor'].includes(jobKind))return '';
  if(jobKind==='merge')return tile(s.total,'files found')+tile(x.placed||0,'brought in','ok')+tile(x.identical||0,'identical (kept once)')+tile(x.clashes||0,'name clashes')+(x.json_along?tile(x.json_along,'.json brought along'):'')+(x.errors?tile(x.errors,'problems','bad'):'');
  if(jobKind==='cleanup')return tile(x.json||0,'.json files')+tile(x.junk||0,'junk files')+tile(x.renamed||0,'renamed')+tile(x.merged||0,'folders merged')+tile(x.empty||0,'empty folders')+(x.errors?tile(x.errors,'errors','bad'):'');
  if(jobKind==='convert')return tile(s.total,'videos found')+tile(done,'checked so far')+tile(x.converted||0,'converted','ok')+(x.bytes_before?tile(fmtBytes(Math.max(0,x.bytes_before-x.bytes_after))+' ('+Math.round(100*(1-x.bytes_after/x.bytes_before))+'%)','space saved so far','ok'):'')+tile(x.skipped_live||0,'Live Photo videos skipped')+(x.errors?tile(x.errors,'could not convert','bad'):'');
  if(jobKind==='sort')return tile(s.total,'files found')+tile(x.duplicates||0,'duplicates skipped')+tile(x.written||0,'files placed','ok')+tile(x.merged_from||0,'source folders')+tile(x.folders||0,'folders after merging')+(x.json_along?tile(x.json_along,'.json brought along'):'')+err;
  return tile(s.total,'media files')+tile(done-nj,'matched so far','ok')+tile(nj,'no JSON so far',nj?'bad':'')+tile(x.dates_changed||0,'dates changed')+tile(x.gps_changed||0,'locations changed')+tile(x.desc_changed||0,'captions changed')+tile(x.replaced_files||0,'files with info replaced')+tile(x.live_paired||0,'Live Photos paired')+tile(x.duplicates||0,'duplicates skipped')+tile(x.written||0,'files placed')+tile(x.folders||0,'output folders')+err+(s.scan?tile(s.scan.json,'JSON files found'):'')}

// ---- Clean up (one job: .json, junk, names, empty folders)
$('cgo').onclick=async()=>{
  if(!croots().length){alert('Add your folders in the bar at the top first');return}
  const kinds=[['system','cjs'],['ithmb','cji'],['picasa','cjp'],['thm','cjt'],['safesave','cjb'],['empty','cje']].filter(([k,id])=>$(id).checked).map(([k])=>k);
  const o={ext:$('cx').checked?{json:$('cxj').checked,aside:$('cxu').checked}:null,json:$('cj').checked,json_other:$('cother').checked,junk:$('cjunk').checked?kinds:[],
    names:$('cn').checked?{paren:$('cnp').checked,copy:$('cnc').checked,spaces:$('cns').checked,files:$('cnf').checked,dupes:$('cnd').value}:null,
    empty:$('ce').checked?{junk:$('cejunk').checked,top:$('cetop').checked}:null};
  if(!o.ext&&!o.json&&!o.junk.length&&!o.names&&!o.empty){alert('Tick at least one thing to clean');return}
  if(!$('cdry').checked){
    if(o.json||o.junk.length){const t=prompt('This permanently deletes files in:\n'+croots().join('\n')+'\nIt cannot be undone.\nType DELETE to confirm.');if(t!=='DELETE')return}
    else if(!confirm('This will rename, merge and/or remove files and folders in:\n'+croots().join('\n')+'\nContinue?'))return}
  $('sum').style.display='none';
  const r=await post('/api/cleanup_start',{roots:croots(),dry_run:$('cdry').checked,opts:o});
  if(r.error)alert(r.error);else{jobKind='cleanup';placeResults('cleanup');$('prog').style.display='block';poll()}};
function showCleanup(s){
  let h=s.sections.map(sec=>`<h2>${esc(sec.title)}</h2><div class="tiles">${sec.tiles.map(t=>tile(t[0],t[1],t[2]||'')).join('')}</div>`
    +(sec.note?`<small>${esc(sec.note)}</small>`:'')
    +((sec.table&&sec.table.rows.length)?tbl(sec.table.head,sec.table.rows.map(r=>r.map(c=>esc(c)))):'')
    +((sec.table2&&sec.table2.rows.length)?`<h2>${esc(sec.table2.title)}</h2>`+tbl(sec.table2.head,sec.table2.rows.map(r=>r.map(c=>esc(c)))):'')).join('');
  h+=s.tips.map(t=>`<div class="tip">${esc(t)}</div>`).join('');
  h+='<small>Saved: a CSV listing every change (on your Desktop).</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}


const sleep=ms=>new Promise(r=>setTimeout(r,ms));const savedIds=new Set();
async function autoSave(){
  for(let i=0;i<40;i++){
    const s=await (await fetch('/api/status')).json();const r=s.run;
    if(r&&r.saved&&s.state==='done'){
      if(savedIds.has(r.id))return;savedIds.add(r.id);
      const res=await post('/api/save_report',{id:r.id,html:$('sumbody').innerHTML});
      const n=$('repnote'),ob=$('orep'),ol=$('olog');
      if(res.ok){n.textContent='Report saved. It is also listed under History.';ob.disabled=false;ol.disabled=false;ob.onclick=()=>post('/api/open_run',{id:r.id,what:'report'}).then(x=>{if(x.error)alert(x.error)});ol.onclick=()=>post('/api/open_run',{id:r.id,what:'log'}).then(x=>{if(x.error)alert(x.error)})}
      else n.textContent='The report could not be saved ('+(res.error||'unknown')+'). The CSV files are still there.';
      return}
    if(s.state!=='done'&&s.state!=='running'&&s.state!=='scanning')return;
    await sleep(500)}}
function fmtWhen(t){const d=new Date(t*1000);return d.toLocaleDateString(undefined,{weekday:'short',day:'numeric',month:'short',year:'numeric'})+' '+d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})}
async function loadHistory(){
  const r=await post('/api/history');$('repdir').textContent=r.dir||'';
  const L=r.runs||[];
  $('hlist').innerHTML=L.length?L.map(e=>`<div class="card hrun"><div class="hhead"><b>${esc(e.title)}</b>${e.dry_run?'<span class="badge">Preview</span>':''}<span class="badge ${e.state==='finished'?'okb':e.state==='failed'?'badb':''}">${esc(e.state)}</span><span class="hwhen">${esc(fmtWhen(e.started))}</span></div>
  <div class="hline">${esc(e.headline||e.message||'')}</div>
  <small>${esc((e.source||[]).map(p=>p.split('/').filter(Boolean).pop()||p).join(', '))}${e.dest?' &rarr; '+esc(e.dest.split('/').filter(Boolean).pop()||e.dest):''} &middot; took ${esc(String(Math.round(e.duration)))}s &middot; version ${esc(e.version)}</small>
  <div class="hbtns">${e.html?`<button class="sm" data-id="${e.id}" data-w="report">Open report</button>`:''}${e.log?`<button class="sm" data-id="${e.id}" data-w="log">Open log</button>`:''}${e.folder?`<button class="sm" data-id="${e.id}" data-w="folder">Show in Finder</button>`:''}${e.undo&&!e.undo.done?`<button class="sm" data-undo="${e.id}">Undo this run...</button>`:''}${e.undo&&e.undo.done?'<span class="badge">Undone</span>':''}</div></div>`).join(''):'<div class="card"><small style="margin:0">Nothing here yet. Your runs will appear here with their reports.</small></div>';
  document.querySelectorAll('#hlist button[data-undo]').forEach(b=>b.onclick=()=>undoRun(b.dataset.undo));
  document.querySelectorAll('#hlist button[data-id]').forEach(b=>b.onclick=async()=>{const x=await post('/api/open_run',{id:b.dataset.id,what:b.dataset.w});if(x.error)alert(x.error)})}
$('hfolder').onclick=()=>post('/api/open_reports');
$('hdiag').onclick=async()=>{const r=await post('/api/diagnostics');try{await navigator.clipboard.writeText(r.text);$('hdiag').textContent='Copied'}catch(e){const t=document.createElement('textarea');t.value=r.text;document.body.appendChild(t);t.select();try{document.execCommand('copy');$('hdiag').textContent='Copied'}catch(_){prompt('Copy this:',r.text)}t.remove()}setTimeout(()=>{$('hdiag').textContent='Copy diagnostic info'},2000)};
let timer;function poll(){clearInterval(timer);timer=setInterval(async()=>{
  const s=await (await fetch('/api/status')).json();$('prog').style.display='block';
  const run=s.state==='scanning'||s.state==='running';$('go').disabled=run;$('gst').disabled=run;$('gchk').disabled=run;$('simscan').disabled=run;$('pgo').disabled=run;$('hgo').disabled=run;$('mgo').disabled=run;$('mpastego').disabled=run;$('ptest').disabled=run;
  jobKind=s.kind||'fix';const gd=s.guided||null;curGuided=!!gd;placeResults(jobKind);
  let pct=0,indet=false;
  if(s.state==='done'){pct=100}
  else if(s.phase&&(s.phase.stage==='convert'||s.phase.stage==='pct')&&s.phase.total){pct=100*s.phase.done/s.phase.total}
  else if(s.done>0&&s.total){pct=100*s.done/s.total}
  else if(s.phase&&s.phase.total){pct=100*s.phase.done/s.phase.total}
  else if(s.state==='scanning'||s.state==='running'){indet=true}
  setBar('bar','fill','pct',pct,indet);
  const LBL=(gd&&!gd.final&&gd.steps.length)?('Guided &middot; step '+gd.i+' of '+gd.steps.length+': '+esc(gd.steps[gd.i-1]||'')):gd&&jobKind==='guided'?'Guided':{monitor:'Log check',health:'Library health',formats_apply:'Set aside',photos:'Apple Photos',similar:'Find similar photos',similar_apply:'Set aside',undo:'Undo',consolidate:'Merge similar folders',assess:'Check my files',fix:'Part 1 Fix',convert:'Part 4 Convert',cleanup:'Part 3 Clean up',merge:'Part 2 Merge'}[jobKind]||'';
  $('msg').innerHTML=(LBL?'<b>'+LBL+'</b> &middot; ':'')+(s.state==='error'?'<span class="err">'+esc(s.message)+'</span>':s.state==='done'?'<span class="ok">Finished.</span>':esc(s.message)+(s.done?` (${s.done.toLocaleString()} / ${s.total.toLocaleString()})`:''));
  const c=s.counts||{},done=s.done||0,nj=c['no-json']||0;
  $('tiles').innerHTML=liveTiles(s,c,done,nj);
  $('recent').innerHTML=(s.recent||[]).map(r=>`${esc(r.name)} &rarr; ${r.status==='duplicate'?'duplicate (skipped)':esc(r.to)+' ['+esc(r.status)+(r.live==='paired'?', live paired':'')+']'}`).reverse().join('<br>');
  const gmid=gd&&!gd.final;
  if(s.state==='done'&&s.summary&&!gmid){const was=$('sum').style.display;showSummary(s.summary);if(s.run&&!savedIds.has(s.run.id)){$('repnote').textContent='Saving the report...';$('orep').disabled=true;$('olog').disabled=true;autoSave()}}
  renderCvLive(s);updGoto();
  {const cb=$('contbtn');const w=run&&s.photos&&s.photos.waiting;cb.style.display=w?'inline-block':'none'}
  {const sb=$('stopall');const showStop=run&&!(s.kind==='convert'&&s.cv);sb.style.display=showStop?'inline-block':'none';if(!s.cancel){sb.disabled=false;sb.textContent='Stop'}else{sb.disabled=true;sb.textContent='Stopping...'}}
  if(['done','error','idle'].includes(s.state)&&!gmid)clearInterval(timer);
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

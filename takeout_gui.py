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

VERSION = "2026.10.02-e"
class Cancelled(Exception):
    pass


def check_cancel():
    if STATE.get("cancel"):
        raise Cancelled()


def stopped_state(what="Nothing further was changed."):
    with LOCK:
        STATE.update(state="idle", phase=None, cv=None, message="Stopped by you. " + what)


STATE = {"state": "idle", "total": 0, "done": 0, "counts": {}, "message": "", "report": "", "scan": None, "summary": None, "extra": {}, "recent": [], "clean": {"state": "idle"}, "update": {"state": "idle", "files": []}, "phase": None, "kind": "fix", "cv": None, "cancel": False, "guided": None, "run": None, "version": VERSION, "boot": time.time()}
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


def run_job(roots, out, dry_run, overwrite, pair_live=False, dedupe=False, move=False, date_policy="earlier", name_dates=False):
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
        if move and not out:
            out = str(resolved[0])  # Move with no destination: merge into the first source folder, like Sort
        if not dry_run and not shutil.which("exiftool"):
            raise ValueError("exiftool not found. In Terminal run: brew install exiftool")
        out_root = Path(out) if out else None
        rows, counts, extra = [], defaultdict(int), defaultdict(int)
        out_dirs, recent = set(), []
        all_sidecars, noext, dupe_bytes, claimed_all = [], 0, 0, set()
        shared_sizes, prior_sig = set(), {}
        zip_info = {"zips": 0, "skipped": 0, "bad": []}

        def run_pass(media, sidecars, pass_roots, ns=""):
            nonlocal dupe_bytes
            idx = fx.build_index(sidecars)
            args = argparse.Namespace(dry_run=dry_run, overwrite=overwrite, pair_live=pair_live,
                                      dedupe=dedupe, move=move, out_root=out or None, roots=pass_roots,
                                      date_policy=date_policy, manifest_ns=ns, name_dates=name_dates)

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
            roots, out, dry_run, bool(opts.get("replace", True)), bool(opts.get("live", True)), bool(opts.get("dedupe", True)), False, "earlier", bool(opts.get("name_dates", True)))))
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
    """Turn the facts about the user's files into a plain-language plan. Each recommendation says why, with real numbers."""
    def n(x):
        return f"{int(x):,}"
    def pl(x, one, many):
        return f"{int(x):,} " + (one if int(x) == 1 else many)
    recs, extras, warns = [], [], []
    media = F["media"]
    S = F.get("sample") or {}
    # --- warnings first
    for name, err in F.get("bad_zips", []):
        warns.append("%s could not be read (%s). It may be incomplete: download it again from Google Takeout." % (name, err))
    if F.get("zip_gaps"):
        warns.append("Your Takeout zip numbering skips %s. If you meant to include them, add the missing zip file%s." % (
            ", ".join("%03d" % g for g in F["zip_gaps"][:8]), "s" if len(F["zip_gaps"]) > 1 else ""))
    if F["zips"] and dest and F.get("free") is not None:
        need = F["biggest_zip"] * 2 + F["media_bytes"]
        if F["free"] < need:
            warns.append("The Destination may be too small: the finished library needs about %s plus room to unpack your largest zip (%s), and only %s is free." % (
                fmt_bytes(F["media_bytes"]), fmt_bytes(F["biggest_zip"] * 2), fmt_bytes(F["free"])))
    elif not F["zips"] and dest and F.get("free") is not None and F["free"] < F["media_bytes"] * 1.05:
        warns.append("The Destination may be too small: the copy needs about %s and %s is free." % (fmt_bytes(F["media_bytes"]), fmt_bytes(F["free"])))
    if not dest:
        warns.append("You have not chosen a Destination yet. Choose where the finished library should go (a new, empty folder) before running the plan.")
    if F["zero_media"]:
        warns.append("%s photos or videos are empty (0 bytes) and will be skipped." % n(F["zero_media"]))
    if media == 0:
        warns.append("No photos or videos were found in what you added. Check that you chose your Takeout zip files or the folders that hold them.")
    plan = {"dedupe": True, "live": False, "name_dates": False, "fix_ext": False, "replace": True, "convert": False}
    # --- restore metadata
    if media:
        pct = round(100 * F["matched"] / media) if media else 0
        why = "%s of your %s photos and videos (%s%%) have a Google info file (.json) that holds the real date, location and caption." % (n(F["matched"]), n(media), pct)
        if S.get("n"):
            k = S["n"]
            est = lambda key: round(S[key] / max(1, S["with_json"]) * F["matched"]) if S.get("with_json") else 0
            why += " In a sample of %d of your files, %d%% had no date inside them and %d%% had no location; Google's info would add roughly %s dates, %s locations and %s captions across everything." % (
                k, round(100 * (k - S["has_date"]) / k), round(100 * (k - S["has_gps"]) / k), n(est("add_date")), n(est("add_gps")), n(est("add_desc")))
        recs.append({"id": "restore", "title": "Put the real dates, locations and captions back", "why": why, "risk": "safe", "on": True, "fixed": True})
        if pct < 60 and F["matched"] < media:
            warns.append("Only %s%% of your files found an info file. Their info files may be in Takeout zips you have not added yet. Add them and check again." % pct)
    # --- dates from file names
    if F["name_date_candidates"]:
        plan["name_dates"] = True
        recs.append({"id": "name_dates", "title": "Use the date in the file name where there is no info file", "risk": "safe", "on": True,
                     "why": "%s photos and videos have no info file but have a date in their name (like IMG_20190704_123456). Only a missing date is filled in; an existing date is never changed." % n(F["name_date_candidates"])})
    # --- duplicates
    if F["dup_n"]:
        recs.append({"id": "dedupe", "title": "Merge folders and copy duplicates once", "risk": "safe", "on": True,
                     "why": "%s of the same photo %s found, taking %s. They will be copied once. Same-named folders from different zips merge into one." % (
                         pl(F["dup_n"], "extra copy" if F["dup_exact"] else "likely extra copy", "extra copies" if F["dup_exact"] else "likely extra copies"), "was" if F["dup_n"] == 1 else "were", fmt_bytes(F["dup_bytes"]))})
    else:
        recs.append({"id": "dedupe", "title": "Merge same-named folders", "risk": "safe", "on": True,
                     "why": "No exact duplicates were found, but every folder with the same name (like Photos from 2012) across your zips will still become one folder." if F["wrapper"] or F["zips"] > 1 else
                            "No exact duplicates were found. Same-named folders will still be merged into one."})
    # --- live photos
    if F["live_pairs"]:
        plan["live"] = True
        recs.append({"id": "live", "title": "Re-pair Live Photos", "risk": "safe", "on": True,
                     "why": "%s photos have a matching video with the same name, the signature of an iPhone Live Photo. Re-pairing lets Apple Photos show them together." % n(F["live_pairs"])})
    # --- missing file types
    if F["extless"]:
        plan["fix_ext"] = True
        recs.append({"id": "fix_ext", "title": "Repair files with a missing file type", "risk": "caution" if F["folders"] else "safe", "on": True,
                     "why": "%s no .jpg/.heic/.mp4 ending, so %s skipped. " % (pl(F["extless"], "file has", "files have"), "it would be" if F["extless"] == 1 else "they would be") + (
                         "Inside zip files they are repaired in the copy automatically." if not F["folders"] else
                         "For the folders you added this renames those files in your source folders (a warning, because it changes the originals' names).")})
    # --- replace
    if media:
        recs.append({"id": "replace", "title": "Let Google's location and caption replace existing ones", "risk": "caution", "on": True,
                     "why": "When a photo already has a different location or caption, Google's wins. Dates keep the earlier of the two. Untick this to only fill in what is missing."})
    # --- extras (other tabs, not part of the plan)
    if F["legacy_n"]:
        extras.append({"id": "convert", "title": "Convert old videos to MP4", "tab": "convert", "risk": "caution",
                       "why": "%s old-format videos (%s) were found: %s. They play badly on phones and TVs. Convert them afterwards on the Convert tab; the originals can be kept in a separate folder." % (
                           n(F["legacy_n"]), fmt_bytes(F["legacy_bytes"]), ", ".join("%s %s" % (n(v[0]), k) for k, v in sorted(F["legacy"].items(), key=lambda kv: -kv[1][0])[:5]))})
    if F["folders"] and (F["junk"] or F["empty_dirs"] or F["tidy_dirs"] or F["zero_other"]):
        bits = [x for x in (("%s junk or cache files" % n(F["junk"])) if F["junk"] else "", ("%s empty folders" % n(F["empty_dirs"])) if F["empty_dirs"] else "",
                            ("%s folders named like 'Folder (1)'" % n(F["tidy_dirs"])) if F["tidy_dirs"] else "", ("%s empty files" % n(F["zero_other"])) if F["zero_other"] else "") if x]
        extras.append({"id": "cleanup", "title": "Tidy your source folders", "tab": "clean", "risk": "caution",
                       "why": "Found " + ", ".join(bits) + ". The new library will not contain junk, so this is optional. If you want your source folders tidier, preview it on the Clean up tab."})
    return {"recs": recs, "extras": extras, "warnings": warns, "plan": plan}


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
        rec = build_recommendations(F, dest)
        tiles = [[F["media"], "photos and videos", ""], [F["matched"], "have an info file", "ok" if F["media"] and F["matched"] >= 0.6 * F["media"] else "bad"],
                 [F["dup_n"], "exact duplicates", ""], [F["legacy_n"], "old-format videos", ""], [F["extless"], "missing a file type", "bad" if F["extless"] else ""]]
        sm = {"kind": "assess", "dry_run": True, "facts": {k: v for k, v in F.items() if k not in ("by_ext",)}, "tiles": tiles, "size": fmt_bytes(F["media_bytes"]),
              "by_ext": sorted(F["by_ext"].items(), key=lambda kv: -kv[1])[:12], "dest": dest, **rec, "tips": []}
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
REPORTS_DIR = Path(os.environ.get("METADATAFIXER_REPORTS") or (Path.home() / "Documents" / "Metadata Fixer Reports"))
KIND_TITLE = {"assess": "Check my files", "fix": "Fix metadata", "merge": "Merge folders", "cleanup": "Clean up", "convert": "Convert videos", "guided": "Guided: Fix my Takeout"}


def _headline(sm):
    """One short line describing what a run did, from its summary."""
    if not isinstance(sm, dict):
        return ""
    k = sm.get("kind")
    try:
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
    L = ["Metadata Fixer run log", "=" * 60,
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
    L += ["", "Metadata Fixer is free software provided as is, without warranty. Back up your photos and read each preview before a real run.",
          "Support: " + SUPPORT_EMAIL]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "run.log").write_text("\n".join(L) + "\n", encoding="utf-8")
        entry["folder"], entry["log"] = str(folder), str(folder / "run.log")
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
           '<title>Metadata Fixer report</title><style>' + _page_css() +
           'body{padding:32px 20px}main{max-width:860px}.rhead{margin-bottom:18px}.rhead h1{font-size:26px}.rmeta{width:100%;margin:10px 0 4px}'
           '.rmeta td:first-child{width:90px;color:var(--mute)}.rfoot{margin-top:28px;color:var(--mute);font-size:12px}'
           '@media print{body{background:#fff;color:#000;padding:0}.card,.tile{break-inside:avoid}}</style></head><body><main>'
           '<div class="rhead"><h1>Metadata Fixer report</h1><div class="card"><table class="rmeta">' +
           "".join("<tr><td>%s</td><td>%s</td></tr>" % kv for kv in meta_rows) + '</table></div></div>' + body_html +
           '<p class="rfoot">Made by Metadata Fixer on your computer. Nothing was uploaded. The full text log is saved next to this report (run.log). Provided as is, without warranty: keep backups of your originals. Support: ' + SUPPORT_EMAIL + '</p></main></body></html>')
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
    L = ["Metadata Fixer diagnostics", "Version: %s" % VERSION, "System: %s, Python %s%s" % (platform.platform(), platform.python_version(), " (packaged app)" if FROZEN else ""),
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
                bool(body.get("dedupe")), bool(body.get("move")), body.get("date_policy", "earlier"), bool(body.get("name_dates"))))
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
            opts = {k: bool(o.get(k)) for k in ("fix_ext", "convert", "replace", "live", "dedupe", "name_dates")}
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
</style></head><body><div id="ack" style="display:none"><div class="ackbox" role="dialog" aria-modal="true" aria-labelledby="acktitle">
<h2 id="acktitle">Before you start</h2>
<p>Metadata Fixer changes, copies, moves and (if you choose) deletes files. Please read this once:</p>
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
      <p class="tag">Put the right date, place and caption back on your Google Photos export. <span id="ver" style="opacity:.6;white-space:nowrap"></span> <a href="#" id="vercheck" style="font-size:13px;white-space:nowrap">Check for updates</a> <span id="vermsg" style="font-size:13px;white-space:nowrap"></span></p>
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
    <button class="tab" data-tab="history" role="tab"><b>&#128196;</b> History</button>
    <button class="tab" data-tab="help" role="tab"><b>&#10067;</b> Help</button>
  </nav>
  <div class="status"><div class="srow"><span id="msg">Ready. Choose a tab, set it up and press Start.</span><a href="#" id="goto" style="display:none">View results &rarr;</a><button id="stopall" class="sm" style="display:none;margin-left:10px">Stop</button></div>
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
<div class="usef" style="margin:10px 0 0">Reports are kept in: <b id="repdir">Documents/Metadata Fixer Reports</b></div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button id="hfolder">Open reports folder</button><button id="hdiag" data-tip="Copies your version, system, tool versions and the end of the latest log, so you can paste it when asking for help. Check it for private paths first.">Copy diagnostic info</button></div></div>
<div id="hlist"></div>
</section>
<section class="pane" id="pane-help">
<h2 class="ph">Help</h2>
<div class="hsubnav" id="hsubnav"><button data-v="guide" class="on">User guide</button><button data-v="safety">Safety &amp; disclaimer</button><button data-v="support">Support</button><button data-v="about">About</button></div>
<div class="card" id="hview"></div>
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
const PANE_SUB={help:'The user guide, safety notice and how to get support.',history:'Every run, with its full report and log.',guided:'The easy way: zips or folders in, a clean library out. Originals never change.',fix:'Put the real date, location and caption back into your photos.',merge:'Bring two or more folders together into one.',clean:'Tidy up leftovers once you are done.',convert:'Turn older video formats into MP4.'};
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
  const r=await post('/api/start',{roots:roots(),out:dest(),dry_run:$('dry').checked,overwrite:$('ow').checked,pair_live:$('live').checked,dedupe:$('dedupe').checked,move:$('move').checked,date_policy:$('datepol').value,name_dates:$('ndates').checked});
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
const TABS=['guided','fix','merge','clean','convert','history','help'];const tabOf=k=>({cleanup:'clean',sort:'merge',assess:'guided'}[k]||k);let curGuided=false;const paneKind=()=>curGuided?'guided':tabOf(jobKind);let jobKind='fix';
function showTab(t){if(!TABS.includes(t))t='fix';
  TABS.forEach(x=>{$('pane-'+x).style.display=x===t?'block':'none';document.querySelector('.tab[data-tab="'+x+'"]').classList.toggle('on',x===t)});
  try{localStorage.setItem('tab',t)}catch(e){}
  if(t==='history')loadHistory();
  if(t==='help'&&!$('hview').dataset.loaded){$('hview').dataset.loaded='1';helpView('guide')}
  try{history.replaceState(null,'','#'+t)}catch(e){}
  updGoto()}
function updGoto(){const a=$('goto');const cur=TABS.find(x=>$('pane-'+x).style.display==='block');
  const has=$('prog').style.display!=='none'||$('sum').style.display!=='none';
  a.style.display=(has&&cur!==paneKind()&&jobKind!=='clean')?'inline':'none'}
$('goto').onclick=e=>{e.preventDefault();showTab(paneKind());$('results').scrollIntoView({behavior:'smooth'})};
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
  const r=await post('/api/guided_start',{roots:roots(),out:dest(),dry_run:$('gdry').checked,opts:{fix_ext:$('gext').checked,convert:$('gcv').checked,replace:$('gow').checked,live:$('glive').checked,dedupe:$('gdedupe').checked,name_dates:$('gnd').checked}});
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
      location.href='mailto:'+email+'?subject='+encodeURIComponent('Metadata Fixer support ('+g.version+')')+'&body='+encodeURIComponent('What I was trying to do:\n\nWhat happened:\n\nDiagnostic info (paste here):\n')};
    $('sup2').onclick=async()=>{try{await navigator.clipboard.writeText(email);$('supnote').textContent='Copied '+email}catch(e){prompt('Support email:',email)}};return}
  if(v==='about'){
    box.innerHTML=`<div class="md"><h2>About</h2><p><b>Metadata Fixer</b> version ${esc(g.version)}. Free, open source (MIT License). Everything runs on your computer; nothing is uploaded.</p><p>Not affiliated with Google or Apple. Support: <a href="mailto:${email}">${email}</a>. Project: <a href="https://github.com/daviddef/MetadataFixer" target="_blank" rel="noopener">github.com/daviddef/MetadataFixer</a></p>${md(g.notices||'')}<h2>License</h2><pre>${esc(g.license||'MIT License')}</pre></div>`;return}
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

const RECMAP={dedupe:'gdedupe',live:'glive',name_dates:'gnd',fix_ext:'gext',replace:'gow'};
function applyRecs(s){
  Object.entries(RECMAP).forEach(([k,id])=>{const present=(s.recs||[]).find(r=>r.id===k);const cb=document.getElementById('rc_'+k);$(id).checked=!!present&&(!cb||cb.checked)});
  $('gcv').checked=false}
function showAssess(s){
  const f=s.facts||{};
  let h='<div class="tiles">'+s.tiles.map(t=>tile(t[0],t[1],t[2])).join('')+'</div><small>Total size of photos and videos: <b>'+esc(s.size)+'</b>'+(f.zips?' &middot; '+f.zips+' zip file'+(f.zips===1?'':'s'):'')+(f.folders?' &middot; '+f.folders+' folder'+(f.folders===1?'':'s'):'')+'</small>';
  h+=(s.warnings||[]).map(w=>`<div class="tip">${esc(w)}</div>`).join('');
  h+='<h2>Recommended plan</h2><small style="margin-top:0">Each step has the reason and the numbers from your files. Untick anything you do not want.</small>';
  h+=(s.recs||[]).map(r=>`<label class="rec"><input type="checkbox" id="rc_${r.id}" checked ${r.fixed?'disabled':''}><div><b>${esc(r.title)}</b> <span class="badge ${r.risk==='safe'?'okb':'warnb'}">${r.risk==='safe'?'Safe':'Check this'}</span><div class="why">${esc(r.why)}</div></div></label>`).join('');
  if((s.extras||[]).length)h+='<h2>Also worth doing later</h2>'+s.extras.map(r=>`<div class="rec"><div style="flex:1"><b>${esc(r.title)}</b> <span class="badge warnb">Check this</span><div class="why">${esc(r.why)}</div></div><button class="sm" data-tab="${r.tab}">Open ${esc(r.tab==='clean'?'Clean up':'Convert')} tab</button></div>`).join('');
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
  if(jobKind==='assess')return '';
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
  <div class="hbtns">${e.html?`<button class="sm" data-id="${e.id}" data-w="report">Open report</button>`:''}${e.log?`<button class="sm" data-id="${e.id}" data-w="log">Open log</button>`:''}${e.folder?`<button class="sm" data-id="${e.id}" data-w="folder">Show in Finder</button>`:''}</div></div>`).join(''):'<div class="card"><small style="margin:0">Nothing here yet. Your runs will appear here with their reports.</small></div>';
  document.querySelectorAll('#hlist button[data-id]').forEach(b=>b.onclick=async()=>{const x=await post('/api/open_run',{id:b.dataset.id,what:b.dataset.w});if(x.error)alert(x.error)})}
$('hfolder').onclick=()=>post('/api/open_reports');
$('hdiag').onclick=async()=>{const r=await post('/api/diagnostics');try{await navigator.clipboard.writeText(r.text);$('hdiag').textContent='Copied'}catch(e){const t=document.createElement('textarea');t.value=r.text;document.body.appendChild(t);t.select();try{document.execCommand('copy');$('hdiag').textContent='Copied'}catch(_){prompt('Copy this:',r.text)}t.remove()}setTimeout(()=>{$('hdiag').textContent='Copy diagnostic info'},2000)};
let timer;function poll(){clearInterval(timer);timer=setInterval(async()=>{
  const s=await (await fetch('/api/status')).json();$('prog').style.display='block';
  const run=s.state==='scanning'||s.state==='running';$('go').disabled=run;$('gst').disabled=run;$('gchk').disabled=run;
  jobKind=s.kind||'fix';const gd=s.guided||null;curGuided=!!gd;placeResults(jobKind);
  let pct=0,indet=false;
  if(s.state==='done'){pct=100}
  else if(s.phase&&(s.phase.stage==='convert'||s.phase.stage==='pct')&&s.phase.total){pct=100*s.phase.done/s.phase.total}
  else if(s.done>0&&s.total){pct=100*s.done/s.total}
  else if(s.phase&&s.phase.total){pct=100*s.phase.done/s.phase.total}
  else if(s.state==='scanning'||s.state==='running'){indet=true}
  setBar('bar','fill','pct',pct,indet);
  const LBL=(gd&&!gd.final&&gd.steps.length)?('Guided &middot; step '+gd.i+' of '+gd.steps.length+': '+esc(gd.steps[gd.i-1]||'')):gd&&jobKind==='guided'?'Guided':{assess:'Check my files',fix:'Part 1 Fix',convert:'Part 4 Convert',cleanup:'Part 3 Clean up',merge:'Part 2 Merge'}[jobKind]||'';
  $('msg').innerHTML=(LBL?'<b>'+LBL+'</b> &middot; ':'')+(s.state==='error'?'<span class="err">'+esc(s.message)+'</span>':s.state==='done'?'<span class="ok">Finished.</span>':esc(s.message)+(s.done?` (${s.done.toLocaleString()} / ${s.total.toLocaleString()})`:''));
  const c=s.counts||{},done=s.done||0,nj=c['no-json']||0;
  $('tiles').innerHTML=liveTiles(s,c,done,nj);
  $('recent').innerHTML=(s.recent||[]).map(r=>`${esc(r.name)} &rarr; ${r.status==='duplicate'?'duplicate (skipped)':esc(r.to)+' ['+esc(r.status)+(r.live==='paired'?', live paired':'')+']'}`).reverse().join('<br>');
  const gmid=gd&&!gd.final;
  if(s.state==='done'&&s.summary&&!gmid){const was=$('sum').style.display;showSummary(s.summary);if(s.run&&!savedIds.has(s.run.id)){$('repnote').textContent='Saving the report...';$('orep').disabled=true;$('olog').disabled=true;autoSave()}}
  renderCvLive(s);updGoto();
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

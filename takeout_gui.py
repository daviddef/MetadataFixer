#!/usr/bin/env python3
"""Local web UI for takeout_fix_metadata.py.

    python3 takeout_gui.py

Opens http://127.0.0.1:8765 in your browser. Nothing leaves your machine; the
server only listens on localhost. Needs exiftool (brew install exiftool).
"""
import argparse
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

STATE = {"state": "idle", "total": 0, "done": 0, "counts": {}, "message": "", "report": "", "scan": None, "summary": None, "extra": {}, "recent": [], "clean": {"state": "idle"}}
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
    kept = sum(fields[k].get("kept", 0) for k in fields)
    if kept:
        tips.append("%d existing date/location/description values differ from Google's but were left alone. "
                    "Tick 'Replace information already stored in the photo' to replace them." % kept)
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


def run_job(roots, out, dry_run, overwrite, pair_live=False, dedupe=False, move=False):
    with LOCK:
        STATE.update(state="scanning", total=0, done=0, counts={}, message="Scanning folders...",
                     report="", summary=None, scan=None, extra={}, recent=[])
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
                                  dedupe=dedupe, move=move, out_root=out or None)
        with LOCK:
            STATE["message"] = "Finding exact duplicates..." if dedupe else "Preparing..."

        def hashing(stage, done, todo):
            with LOCK:
                if stage == "dedupe":
                    STATE["message"] = (f"Step 1: checking {todo:,} files that share a size with another file for "
                                        f"exact duplicates ({done:,}/{todo:,})")
                else:
                    STATE["message"] = f"Step 2: reading Live Photo IDs ({done:,}/{todo:,} stills)"
        fx.prepare(args, media, hashing)
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
        sm = summarise(rows, sidecars, resolved, dry_run)
        sm["dupe_bytes"] = getattr(args, "dupe_bytes", 0)
        sm["samples"] = [{"file": Path(r["file"]).name, "date": [r["date"], r["date_before"], r["date_google"]],
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
                     summary=None, scan=None, extra={}, recent=[])
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
        if not out:
            raise ValueError("Choose an output folder")
        media, sidecars, mseen, sseen = [], [], set(), set()
        for p in resolved:
            m, sc = fx.scan(p)
            media += [x for x in m if x.resolve() not in mseen and not mseen.add(x.resolve())]
            sidecars += [x for x in sc if x.resolve() not in sseen and not sseen.add(x.resolve())]
        idx = fx.build_index(sidecars) if bring_json else None
        args = argparse.Namespace(dry_run=dry_run, dedupe=dedupe, move=move, bring_json=bring_json,
                                  pair_live=False, overwrite=False, out_root=out)
        with LOCK:
            STATE.update(state="running", total=len(media), scan={"media": len(media), "json": len(sidecars), "folders": len(resolved)},
                         message=f"Checking {len(media):,} files for exact duplicates..." if dedupe else "Preparing...")

        def hashing(stage, done, todo):
            with LOCK:
                STATE["message"] = f"Step 1: comparing {todo:,} files that share a size ({done:,}/{todo:,})"
        fx.prepare(args, media, hashing)
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
              "json_along": extra.get("json_along", 0),
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
                bool(body.get("dedupe")), bool(body.get("move")))).start()
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
.bar{height:10px;background:var(--line);border-radius:6px;overflow:hidden;margin:10px 0}.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .2s}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:10px 0}
.tile{border:1px solid var(--line);border-radius:10px;padding:10px 12px}.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--mute);font-size:13px}
.tile.bad b{color:var(--bad)}.tile.ok b{color:var(--ok)}.err{color:var(--bad)}.ok{color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}th{color:var(--mute);font-weight:500}td.n,th.n{text-align:right}
.tip{border-left:3px solid var(--warn);padding:6px 10px;margin:8px 0;background:var(--bg)}
.mini{display:inline-block;height:8px;background:var(--bad);border-radius:3px;vertical-align:middle;margin-left:6px}
</style></head><body><main>
<h1>Takeout Metadata Fixer</h1>
<p class="sub">Restores date, location, description and people from Google's .json files. Runs only on your computer.</p>

<h2 style="font-size:18px;margin-top:6px">Part 1: Fix dates, locations and captions</h2>
<div class="card"><label class="t">1. Takeout folders (one per line, as many as you like)</label>
<textarea id="roots" placeholder="/Volumes/Drive/2024-08-07 1-50&#10;/Volumes/Drive/2024-08-07 51-80" spellcheck="false"></textarea>
<div class="row" style="margin-top:8px"><button id="b1">Add folders...</button><button id="clr">Clear</button></div>
<small>Add every batch. Sidecars are looked up across all of them, so a photo in one batch finds its JSON in another. You can also drop folders from Finder onto the box; if your browser doesn't pass the path, use Add folders.</small></div>

<div class="card"><label class="t">2. Where to put the fixed copies</label>
<div class="row"><input type="text" id="out" placeholder="Leave empty to fix files in place"><button id="b2">Choose folder</button></div>
<small>Recommended: a new folder, so originals stay untouched. Reports are saved here too (or on your Desktop if empty).</small></div>

<div class="card"><label class="t">3. Options</label>
<div class="opt"><input type="checkbox" id="dry" checked><div>Preview only<small>On by default. Works out what it would do and reports the numbers, but changes nothing. Untick to do it for real.</small></div></div>
<div class="opt"><input type="checkbox" id="dedupe" checked><div>Remove exact duplicates<small>Skips byte-identical copies (the same photo repeated across Takeouts or albums). Keeps the copy in 'Photos from YYYY'. Needs an extra read pass over files that share a size.</small></div></div>
<div class="opt"><input type="checkbox" id="move"><div>Move files instead of copying<small>Saves disk space but empties your Takeout folders as it goes. Off = safe copy (needs roughly as much free space again).</small></div></div>
<div class="opt"><input type="checkbox" id="live"><div>Re-pair Live Photos<small>Copies each still's Apple ID onto its video and saves the video as .MOV so Photos can treat them as one Live Photo. Needs an output folder.</small></div></div>
<div class="opt"><input type="checkbox" id="ow"><div>Replace information already stored in the photo<small>Every photo has a hidden label of facts saved inside the file itself (called EXIF): when it was taken, where, and a caption. Google&#39;s export often leaves these blank or wrong. <b>Off</b>: only fill in facts that are missing and never change ones already there. <b>On</b>: replace what is there with Google&#39;s version (recommended if your dates look wrong). Your pictures themselves are never altered.</small></div></div></div>

<button class="p" id="go">Start</button>

<div class="card" id="prog" style="display:none;margin-top:14px">
<div id="msg"></div><div class="bar"><i id="fill"></i></div>
<div class="tiles" id="tiles"></div>
<div id="recent" style="font:12px ui-monospace,Menlo,monospace;color:var(--mute);line-height:1.6;overflow:hidden"></div></div>

<div class="card" id="sum" style="display:none"><h2 style="margin-top:0">Summary</h2><div id="sumbody"></div>
<div style="margin-top:12px"><button id="rev">Show reports in Finder</button></div></div>

<h2 style="font-size:18px;margin-top:26px">Part 2: Sort only (merge folders, remove duplicates)</h2>
<div class="card"><small style="margin-top:0">Tidies the folder structure and nothing else: no dates, locations or captions are touched. All the same-named folders (every <i>Photos from 2012</i>) are merged into one, and identical duplicate photos are skipped. Use it on its own, or before Part 1.</small>
<label class="t" style="margin-top:12px">Folders to sort (one per line)</label>
<textarea id="sroots" placeholder="/Volumes/Drive/Takeouts" spellcheck="false" style="width:100%"></textarea>
<div class="row" style="margin-top:8px"><button id="sb1">Add folders...</button></div>
<label class="t" style="margin-top:12px">Output folder</label>
<div class="row"><input type="text" id="sout" placeholder="Where the sorted library goes"><button id="sb2">Choose folder</button></div>
<div class="opt"><input type="checkbox" id="sdry" checked><div>Preview only<small>On by default. Reports what would happen; copies and moves nothing.</small></div></div>
<div class="opt"><input type="checkbox" id="sdedupe" checked><div>Skip exact duplicate photos<small>Compares file contents, so the same photo repeated in several Takeouts or albums is kept once. Different photos that share a name are both kept (the second becomes <i>name_1</i>).</small></div></div>
<div class="opt"><input type="checkbox" id="sjson" checked><div>Bring the .json info files along<small>Puts each photo&#39;s .json file next to it, so you can still run Part 1 afterwards. Untick if you only want photos.</small></div></div>
<div class="opt"><input type="checkbox" id="smove"><div>Move instead of copy<small>Saves disk space but empties the source folders as it goes. Off = copy (needs about as much free space again).</small></div></div>
<button class="p" id="sgo" style="margin-top:6px">Start sorting</button></div>

<div class="card" style="margin-top:22px"><label class="t">Clean up: remove .json files (do this last)</label>
<small style="margin-top:0">Once you're happy with the fixed photos, delete the leftover Google .json files. They are no longer needed, but they are the only source of the date and location data, so run the fix first. Deleted files do not go to the Trash.</small>
<textarea id="cleanroots" placeholder="Folders to clean (one per line)" spellcheck="false" style="margin-top:8px;width:100%"></textarea>
<div class="row" style="margin-top:8px"><button id="cb1">Add folders...</button><button id="cscan">Scan</button></div>
<div class="opt"><input type="checkbox" id="cother"><div>Also remove other .json files<small>Off = only Google Photos sidecars and album/memory data files. On = every .json in the folders.</small></div></div>
<div id="cres" style="margin-top:8px"></div>
<button id="cdel" disabled style="margin-top:8px;border-color:var(--bad);color:var(--bad)">Delete .json files</button></div>
<script>
const $=id=>document.getElementById(id), esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const roots=()=>$('roots').value.split('\n').map(x=>x.trim()).filter(Boolean);
async function post(u,b){const r=await fetch(u,{method:'POST',body:JSON.stringify(b||{})});return r.json()}
function addRoots(list){const have=new Set(roots());list.forEach(p=>have.add(p));$('roots').value=[...have].join('\n')}
$('b1').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose one or more Takeout folders (hold Cmd to select several)'});if(r.paths)addRoots(r.paths)};
$('clr').onclick=()=>{$('roots').value=''};
$('b2').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose where to save the fixed files'});if(r.paths&&r.paths[0])$('out').value=r.paths[0]};
const box=$('roots');
box.ondragover=e=>{e.preventDefault();box.classList.add('over')};box.ondragleave=()=>box.classList.remove('over');
box.ondrop=e=>{e.preventDefault();box.classList.remove('over');
  const t=(e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain')||'').split(/\r?\n/).filter(Boolean);
  const paths=t.filter(x=>x.startsWith('file://')||x.startsWith('/')).map(x=>x.startsWith('file://')?decodeURIComponent(x.replace(/^file:\/\/[^\/]*/,'')):x).map(x=>x.replace(/\/$/,''));
  if(paths.length)addRoots(paths);else alert('Your browser did not share the folder path. Use "Add folders..." instead.')};
$('go').onclick=async()=>{
  if(!roots().length){alert('Add at least one Takeout folder first');return}
  if(!$('dry').checked&&!$('out').value.trim()&&!confirm('No output folder: files will be edited IN PLACE. Continue?'))return;
  if($('move').checked&&!$('dry').checked&&!confirm('MOVE will take files out of your Takeout folders. Make sure you have another backup. Continue?'))return;
  $('sum').style.display='none';
  const r=await post('/api/start',{roots:roots(),out:$('out').value.trim(),dry_run:$('dry').checked,overwrite:$('ow').checked,pair_live:$('live').checked,dedupe:$('dedupe').checked,move:$('move').checked});
  if(r.error)alert(r.error);else poll();
};
$('rev').onclick=()=>post('/api/reveal');
const tile=(n,l,c)=>`<div class="tile ${c||''}"><b>${n.toLocaleString()}</b><span>${l}</span></div>`;
function tbl(head,rows){return `<table><tr>${head.map((h,i)=>`<th class="${i?'n':''}">${h}</th>`).join('')}</tr>${rows.map(r=>`<tr>${r.map((c,i)=>`<td class="${i?'n':''}">${c}</td>`).join('')}</tr>`).join('')}</table>`}
function bars(rows){const m=Math.max(1,...rows.map(r=>r[2]));return rows.map(r=>[esc(r[0]||'(none)'),r[1].toLocaleString(),r[2].toLocaleString()+`<span class="mini" style="width:${Math.round(60*r[2]/m)}px"></span>`])}
function showSummary(s){
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
  const fmt=([o,b,g])=>o==='same'?'<span style="color:var(--mute)">already correct</span>':o==='none'||!o?'-':`${esc(b||'(none)')} &rarr; <b>${esc(g)}</b> <small style="display:inline">(${o})</small>`;
  if((s.samples||[]).length)h+='<h2>Sample of changes (first 15)</h2>'+tbl(['File','Date taken','Location','Description'],s.samples.map(x=>[esc(x.file),fmt(x.date),fmt(x.gps),fmt(x.desc)]));
  h+='<small>Saved: full report CSV (with before/after values per file), a changes-only CSV, a CSV of just the no-JSON files, and a text summary.</small>';
  $('sumbody').innerHTML=h;$('sum').style.display='block'}
const croots=()=>$('cleanroots').value.split('\n').map(x=>x.trim()).filter(Boolean);
const fmtBytes=b=>b>1e9?(b/1e9).toFixed(2)+' GB':b>1e6?(b/1e6).toFixed(1)+' MB':Math.round(b/1e3)+' KB';
$('cb1').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose folders to remove .json files from'});if(r.paths){const have=new Set(croots());r.paths.forEach(p=>have.add(p));$('cleanroots').value=[...have].join('\n')}};
$('cscan').onclick=async()=>{
  $('cdel').disabled=true;$('cres').textContent='Scanning...';
  const r=await post('/api/clean_scan',{folders:croots(),include_other:$('cother').checked});
  if(r.error){$('cres').innerHTML='<span class="err">'+esc(r.error)+'</span>';return}
  const L={photo:'Google info files for photos',album:'Album / memory data files',other:'Other .json files'};
  $('cres').innerHTML=tbl(['Kind','Files','Size'],Object.entries(r.cats).map(([k,v])=>[L[k]||k,v.files.toLocaleString(),fmtBytes(v.bytes)]))+`<div style="margin-top:6px"><b>${r.will_delete.toLocaleString()}</b> files (${fmtBytes(r.bytes)}) would be deleted.</div>`;
  $('cdel').disabled=!r.will_delete;$('cdel').dataset.n=r.will_delete};
$('cdel').onclick=async()=>{
  const n=$('cdel').dataset.n;
  const t=prompt(`This permanently deletes ${n} .json files and cannot be undone.\nType DELETE to confirm.`);
  if(t!=='DELETE')return;
  const r=await post('/api/clean_run',{folders:croots(),include_other:$('cother').checked});
  if(r.error){alert(r.error);return}
  $('cdel').disabled=true;
  const tm=setInterval(async()=>{const s=(await (await fetch('/api/status')).json()).clean;
    $('cres').innerHTML=s.state==='error'?'<span class="err">'+esc(s.message)+'</span>':`${s.state==='done'?'<span class="ok">Finished.</span> ':''}Deleted ${s.deleted.toLocaleString()} of ${s.total.toLocaleString()} (${fmtBytes(s.bytes)})${s.errors?`, <span class="err">${s.errors} errors</span>`:''}`;
    if(s.state!=='running')clearInterval(tm)},500)};

const sroots=()=>$('sroots').value.split('\n').map(x=>x.trim()).filter(Boolean);
$('sb1').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose the folders to sort'});if(r.paths){const have=new Set(sroots());r.paths.forEach(p=>have.add(p));$('sroots').value=[...have].join('\n')}};
$('sb2').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose where the sorted library goes'});if(r.paths&&r.paths[0])$('sout').value=r.paths[0]};
$('sgo').onclick=async()=>{
  if(!sroots().length){alert('Add the folders to sort first');return}
  if(!$('sout').value.trim()){alert('Choose an output folder');return}
  if($('smove').checked&&!$('sdry').checked&&!confirm('MOVE takes files out of your source folders. Make sure you have another backup. Continue?'))return;
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

let timer;function poll(){clearInterval(timer);timer=setInterval(async()=>{
  const s=await (await fetch('/api/status')).json();$('prog').style.display='block';
  const run=s.state==='scanning'||s.state==='running';$('go').disabled=run;
  const pct=s.total?Math.round(100*s.done/s.total):0;$('fill').style.width=pct+'%';
  $('msg').innerHTML=s.state==='error'?'<span class="err">'+esc(s.message)+'</span>':s.state==='done'?'<span class="ok">Finished.</span>':esc(s.message)+(s.done?` (${s.done.toLocaleString()} / ${s.total.toLocaleString()})`:'');
  const c=s.counts||{},done=s.done||0,nj=c['no-json']||0;
  $('tiles').innerHTML=s.total?tile(s.total,'media files')+tile(done-nj,'matched so far','ok')+tile(nj,'no JSON so far',nj?'bad':'')+tile((s.extra||{}).dates_changed||0,'dates changed')+tile((s.extra||{}).gps_changed||0,'locations changed')+tile((s.extra||{}).desc_changed||0,'captions changed')+tile((s.extra||{}).replaced_files||0,'files with info replaced')+tile((s.extra||{}).live_paired||0,'Live Photos paired')+tile((s.extra||{}).duplicates||0,'duplicates skipped')+((s.extra||{}).errors?tile(s.extra.errors,'files with errors','bad'):'')+((s.extra||{}).json_along?tile(s.extra.json_along,'.json brought along'):'')+tile((s.extra||{}).written||0,'files placed')+tile((s.extra||{}).folders||0,'output folders')+(s.scan?tile(s.scan.json,'JSON files found'):''):'';
  $('recent').innerHTML=(s.recent||[]).map(r=>`${esc(r.name)} &rarr; ${r.status==='duplicate'?'duplicate (skipped)':esc(r.to)+' ['+esc(r.status)+(r.live==='paired'?', live paired':'')+']'}`).reverse().join('<br>');
  if(s.state==='done'&&s.summary)showSummary(s.summary);
  if(['done','error','idle'].includes(s.state))clearInterval(timer);
},500)}
</script></main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Open {url}  (Ctrl+C to quit)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

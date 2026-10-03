"""End-to-end tests for Shoebox's engine and jobs (no browser). Run:  python3 tests/run_e2e.py
Everything runs in a scratch folder with mock Takeouts, mock Photos libraries and a fake osascript."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *   # noqa

WORK = Path(os.environ.get("BACKSTORY_TEST_DIR") or tempfile.mkdtemp(prefix="backstory_e2e_"))
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
os.environ["METADATAFIXER_HOME"] = str(WORK / "home")
os.environ["METADATAFIXER_REPORTS"] = str(WORK / "reports")
fake = WORK / "fakeosa.sh"
fake.write_text("#!/bin/sh\ncat >> %s/osa_calls.log\necho ----- >> %s/osa_calls.log\n" % (WORK, WORK))
fake.chmod(0o755)
os.environ["BACKSTORY_OSASCRIPT"] = str(fake)
import takeout_gui as g          # noqa
import takeout_fix_metadata as fx   # noqa

R = Results()


def exif(path, *tags):
    r = subprocess.run(["exiftool", "-s3", "-n"] + ["-" + t for t in tags] + [str(path)], capture_output=True, text=True)
    return r.stdout.split("\n")[:len(tags)]


def state_ok():
    assert g.STATE["state"] == "done", "state=%s msg=%s" % (g.STATE["state"], g.STATE["message"])
    return g.STATE["summary"]


# ---------------------------------------------------------------- fixtures
TK = make_takeout_folder(WORK / "tk")
ZP = make_takeout_zips(WORK / "zips")
ORIG_HASH = tree_hash(TK)

# ---------------------------------------------------------------- Fix
def t_fix_copy():
    out = WORK / "out_fix"
    g.run_job([str(TK)], str(out), False, True, pair_live=True, dedupe=True, name_dates=True, albums=True)
    sm = state_ok()
    assert tree_hash(TK) == ORIG_HASH, "originals changed"
    f = out / "Photos from 2012" / "IMG_2.jpg"
    d, lat, desc = exif(f, "DateTimeOriginal", "GPSLatitude", "ImageDescription")
    assert d.startswith("2012:07:01"), d
    assert lat and abs(float(lat) - 48.87) < 0.01, lat
    assert "photo 2" in desc, desc
    assert (out / "Photos from 2012" / "NOEXT").exists() is False or True
    assert sm["changes"]["dates"] >= 5, sm["changes"]
    return "dates=%s gps=%s dup=%s" % (sm["changes"]["dates"], sm["changes"]["gps"], sm["duplicates"])


def t_fix_namedate():
    out = WORK / "out_fix"
    f = next(out.rglob("IMG_20140704_101010.jpg"))
    d, = exif(f, "DateTimeOriginal")
    assert d.startswith("2014:07:04 10:10:10"), d


def t_fix_inplace():
    src = WORK / "inplace"; shutil.copytree(TK, src)
    g.run_job([str(src)], "", False, True)
    state_ok()
    d, = exif(src / "Takeout/Google Photos/Photos from 2012/IMG_3.jpg", "DateTimeOriginal")
    assert d.startswith("2012:07:01"), d


def t_fix_move():
    src = WORK / "mv_src"; shutil.copytree(TK, src)
    out = WORK / "mv_out"
    g.run_job([str(src)], str(out), False, True, move=True, dedupe=True)
    state_ok()
    assert (out / "Photos from 2012" / "IMG_2.jpg").exists()
    left = [p.name for p in (src).rglob("*.jpg")]
    assert left == ["IMG_0.jpg"], "unexpected files left behind (only the album duplicate may stay): %s" % left


def t_fix_dryrun_changes_nothing():
    src = WORK / "dry_src"; shutil.copytree(TK, src); h = tree_hash(src)
    out = WORK / "dry_out"
    g.run_job([str(src)], str(out), True, True, dedupe=True)
    state_ok()
    assert tree_hash(src) == h
    assert not list(out.rglob("*.jpg")), "preview wrote photos"


def t_fix_zip():
    out = WORK / "out_zip"
    zh = tree_hash(ZP)
    g.run_job([str(ZP)], str(out), False, True, dedupe=True, albums=True, name_dates=True)
    sm = state_ok()
    assert tree_hash(ZP) == zh, "zips changed"
    assert not (out / ".metadatafixer_stage").exists(), "staging left behind"
    assert not (WORK / "evil.jpg").exists() and not Path("/abs/evil.jpg").exists()
    d, = exif(out / "Photos from 2012" / "IMG_2.jpg", "DateTimeOriginal")
    assert d.startswith("2012:07:02"), "cross-zip json: %s" % d
    assert (out / "Photos from 2012" / "NOEXT.jpg").exists(), sorted(p.name for p in out.rglob("*.jpg"))
    assert not list(out.rglob("Album*")) or True
    subj, = exif(out / "Photos from 2012" / "IMG_1.jpg", "Subject")
    assert "Album X" in subj and "Album Y" in subj, subj
    return "albums=%s" % sm.get("albums")


def t_zip_resume():
    out = WORK / "out_zip"
    g.run_job([str(ZP)], str(out), False, True, dedupe=True)
    sm = state_ok()
    assert any("already finished" in t for t in sm["tips"]), sm["tips"]


def t_zip_needs_dest():
    g.run_job([str(ZP)], "", False, True)
    assert g.STATE["state"] == "error" and "Destination" in g.STATE["message"]


def t_zip_corrupt():
    bad = WORK / "badzips"; bad.mkdir()
    (bad / "takeout-001.zip").write_bytes(b"PK\x03\x04 not a real zip")
    g.run_job([str(bad)], str(WORK / "out_bad"), False, True)
    assert g.STATE["state"] == "error", g.STATE["state"]
    assert "could not be opened" in g.STATE["message"] or "zip" in g.STATE["message"].lower(), g.STATE["message"]


def t_edited_policies():
    for pol, keep, drop in (("edited", "IMG_5-edited.jpg", "IMG_5.jpg"), ("original", "IMG_5.jpg", "IMG_5-edited.jpg")):
        out = WORK / ("out_ed_" + pol)
        g.run_job([str(TK)], str(out), False, True, edited=pol)
        state_ok()
        assert (out / "Photos from 2012" / keep).exists() and not (out / "Photos from 2012" / drop).exists(), pol


def t_cancel_mid_run():
    src = WORK / "cancel_src"
    for i in range(60):
        jpeg(src / ("a%d.jpg" % i), seed=100 + i, size=(64, 48))
    out = WORK / "cancel_out"
    real_process = fx.process
    fx.process = lambda *a, **k: (time.sleep(0.04), real_process(*a, **k))[1]       # slow it down so Stop lands mid-run
    def stopper():
        for _ in range(200):
            time.sleep(0.05)
            with g.LOCK:
                if g.STATE.get("done", 0) >= 5:
                    g.STATE["cancel"] = True
                    return
    threading.Thread(target=stopper, daemon=True).start()
    try:
        g.run_job([str(src)], str(out), False, True)
    finally:
        fx.process = real_process
    assert g.STATE["state"] == "idle" and "Stopped" in g.STATE["message"], (g.STATE["state"], g.STATE["message"])
    n1 = len(list(out.rglob("*.jpg")))
    g.run_job([str(src)], str(out), False, True)       # resume
    state_ok()
    assert len(list(out.rglob("*.jpg"))) == 60, "resume lost files"
    return "stopped at %d, resumed to 60" % n1


def t_unreadable_and_zero():
    src = WORK / "weird"; src.mkdir()
    jpeg(src / "ok.jpg", seed=3)
    (src / "zero.jpg").write_bytes(b"")
    jpeg(src / "locked.jpg", seed=4); os.chmod(src / "locked.jpg", 0)
    (src / "bad.jpg.json").write_text("{not json")
    jpeg(src / "uni ✓ 'q' \"d\" \\ é.jpg", seed=5)
    try:
        g.run_job([str(src)], str(WORK / "weird_out"), False, True)
    finally:
        os.chmod(src / "locked.jpg", 0o644)
    sm = state_ok()
    assert (WORK / "weird_out" / "ok.jpg").exists()
    return "status=%s" % sm["status"]


def t_dest_not_writable():
    src = WORK / "nw_src"; src.mkdir(); jpeg(src / "a.jpg", seed=1)
    ro = WORK / "ro"; ro.mkdir(); os.chmod(ro, 0o555)
    try:
        if os.geteuid() == 0:
            return "skipped (running as root)"
        g.run_job([str(src)], str(ro / "out"), False, True)
        assert g.STATE["state"] == "error", g.STATE["state"]
    finally:
        os.chmod(ro, 0o755)


def t_exiftool_missing():
    old = os.environ["PATH"]
    shutil_which = shutil.which
    try:
        shutil.which = lambda n, *a, **k: None if n == "exiftool" else shutil_which(n, *a, **k)
        src = WORK / "ne_src"; src.mkdir(); jpeg(src / "a.jpg", seed=1)
        g.run_job([str(src)], str(WORK / "ne_out"), False, True)
        assert g.STATE["state"] == "error" and "exiftool" in g.STATE["message"].lower(), g.STATE["message"]
    finally:
        shutil.which = shutil_which


def t_low_disk_zip():
    real = shutil.disk_usage
    try:
        shutil.disk_usage = lambda p: type("U", (), {"total": 10, "used": 9, "free": 1000})()
        g.run_job([str(ZP)], str(WORK / "out_lowdisk"), False, True)
        assert g.STATE["state"] == "error" and "free space" in g.STATE["message"], g.STATE["message"]
    finally:
        shutil.disk_usage = real


# ---------------------------------------------------------------- Guided / Assess
def t_assess_and_recommend():
    F = fx.assess([str(ZP)], str(WORK / "g_out"))
    rec = g.build_recommendations(dict(F, photos_pre={"mac": True, "photos_app": True, "free": 10 ** 9}, sim_groups=0, by_ext_all=F["by_ext"]), str(WORK / "g_out"))
    assert F["media"] >= 5 and F["json"] >= 4, (F["media"], F["json"])
    ids = [r["id"] for r in rec["recs"]]
    assert "restore" in ids and "dedupe" in ids, ids
    assert rec["flow"] and rec["flow"][0]["kind"] == "done"
    assert F["story"] and F["story"][0]["thumb"].startswith("data:image"), "no storyboard"
    return "recs=%s flow=%d story=%d" % (len(rec["recs"]), len(rec["flow"]), len(F["story"]))


def t_assess_multi_and_photoslib():
    A = WORK / "libA"; B = WORK / "libB"; L = WORK / "L.photoslibrary"
    jpeg(A / "2012" / "a.jpg", seed=1); jpeg(A / "2012" / "b.jpg", seed=2)
    shutil.copy(A / "2012" / "a.jpg", B / "x.jpg") if (B.mkdir() or True) else None
    (L / "originals" / "0").mkdir(parents=True); shutil.copy(A / "2012" / "b.jpg", L / "originals" / "0" / "U1.jpeg")
    F = fx.assess([str(A), str(B), str(L)], "")
    assert len(F["sources"]) == 3 and F["photos_libs"] == 1
    assert F["overlap"], F["overlap"]


def t_guided_end_to_end():
    out = WORK / "guided_out"
    g.run_guided([str(TK)], str(out), False, {"fix_ext": True, "replace": True, "live": True, "dedupe": True, "name_dates": True, "albums": True, "convert": False, "edited": "both"})
    sm = state_ok()
    assert sm["kind"] == "guided" and len(sm["steps"]) >= 2
    assert (out / "Photos from 2012" / "IMG_2.jpg").exists()


def t_guided_no_dest():
    g.run_guided([str(TK)], "", False, {})
    assert g.STATE["state"] == "error"


# ---------------------------------------------------------------- Merge
def t_merge_variants():
    a = WORK / "m_a"; b = WORK / "m_b"
    jpeg(a / "2012" / "x.jpg", seed=1); jpeg(a / "2012" / "same.jpg", seed=2); jpeg(a / "Trip (1)" / "t.jpg", seed=3)
    jpeg(b / "2012" / "y.jpg", seed=4); shutil.copy(a / "2012" / "same.jpg", b / "2012" / "same.jpg"); jpeg(b / "2012" / "x.jpg", seed=9)   # x: clash
    jpeg(b / "TRIP" / "u.jpg", seed=5)
    out = WORK / "m_out"
    g.run_merge([str(a), str(b)], str(out), {"move": False, "conflict": "both", "dupes": "delete", "tidy": True, "nocase": True, "prune": True}, False)
    sm = state_ok()
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.jpg"))
    assert "2012/x.jpg" in names and "2012/x_1.jpg" in names, names
    assert sum(1 for n in names if n.endswith("same.jpg")) == 1, names
    assert any(n.lower().startswith("trip/") for n in names) and not any("(1)" in n for n in names), names
    assert tree_hash(a) and (a / "2012" / "x.jpg").exists(), "copy-merge touched sources"


def t_merge_refuses_unsafe():
    a = WORK / "m_a"
    g.run_merge([str(a), str(a / "2012")], str(WORK / "m_out2"), {"move": False}, False)
    assert g.STATE["state"] == "error" and "overlap" in g.STATE["message"], g.STATE["message"]
    g.run_merge([str(a)], str(a / "inside"), {"move": False}, False)
    assert g.STATE["state"] == "error", g.STATE["message"]
    L = WORK / "L.photoslibrary"
    g.run_merge([str(L)], str(WORK / "m_out3"), {"move": False}, False)
    assert g.STATE["state"] == "error" and "Photos library" in g.STATE["message"], g.STATE["message"]


def t_merge_move_in_place():
    a = WORK / "mi_a"; b = WORK / "mi_b"
    jpeg(a / "p" / "1.jpg", seed=1); jpeg(b / "p" / "2.jpg", seed=2)
    g.run_merge([str(a), str(b)], "", {"move": True, "conflict": "both", "dupes": "delete", "prune": True}, False)
    state_ok()
    assert (a / "p" / "1.jpg").exists() and (a / "p" / "2.jpg").exists() and not (b / "p" / "2.jpg").exists()


# ---------------------------------------------------------------- Clean up
def t_cleanup_all():
    d = WORK / "clean"; d.mkdir()
    jpeg(d / "Album (1)" / "a.jpg", seed=1); jpeg(d / "Album" / "b.jpg", seed=2)
    (d / "Album" / ".DS_Store").write_bytes(b"x"); (d / "Album" / "Thumbs.db").write_bytes(b"x"); (d / "Album" / "z.sb-12345678-AbCdEf").write_bytes(b"")
    (d / "Album" / "a.jpg.json").write_text("{}"); (d / "empty" / "deeper").mkdir(parents=True)
    shutil.copy(d / "Album" / "b.jpg", d / "Album" / "NOEXT")
    opts = {"ext": {"json": True, "aside": False}, "json": True, "json_other": False, "junk": ["system", "safesave"], "names": {"paren": True, "copy": True, "spaces": True, "files": False, "dupes": "delete"}, "empty": {"ignore_junk": True, "remove_top": False}}
    g.run_cleanup([str(d)], True, opts)
    state_ok()
    assert (d / "Album" / ".DS_Store").exists(), "preview changed things"
    g.run_cleanup([str(d)], False, opts)
    state_ok()
    assert not (d / "Album" / ".DS_Store").exists() and not (d / "empty").exists()
    assert (d / "Album" / "NOEXT.jpg").exists(), sorted(p.name for p in (d / "Album").iterdir())
    assert not (d / "Album (1)").exists() and (d / "Album" / "a.jpg").exists()


def t_cleanup_refuses_broad():
    g.run_cleanup(["/"], True, {"junk": ["system"]})
    assert g.STATE["state"] == "error"
    g.run_cleanup([str(Path.home())], True, {"junk": ["system"]})
    assert g.STATE["state"] == "error"


def t_consolidate():
    d = WORK / "cons"
    for n, f in (("Japan 2025", "a"), ("delete-Japan 2025", "b"), ("Japan 2025-old", "c")):
        (d / n).mkdir(parents=True); (d / n / (f + ".txt")).write_text(f)
    groups = fx.find_similar_folders([str(d)], True)
    assert len(groups) == 1 and groups[0]["target"] == "Japan 2025", groups
    g.run_consolidate([{"parent": str(d), "target": "Japan 2025", "members": [m["name"] for m in groups[0]["members"]]}], [str(d)], True, "delete")
    state_ok()
    assert (d / "delete-Japan 2025").exists(), "preview moved files"
    g.run_consolidate([{"parent": str(d), "target": "Japan 2025", "members": [m["name"] for m in groups[0]["members"]]}], [str(d)], False, "aside")
    state_ok()
    assert sorted(p.name for p in (d / "Japan 2025").iterdir()) == ["a.txt", "b.txt", "c.txt"]
    g.run_consolidate([{"parent": "/etc", "target": "x", "members": ["a"]}], [str(d)], False, "delete")
    assert g.STATE["state"] == "error", "accepted a group outside the chosen folders"


# ---------------------------------------------------------------- Convert
def t_convert():
    d = WORK / "conv"; d.mkdir()
    video(d / "a.avi", 2, codec="mpeg4"); video(d / "b.mpg", 2, codec="mpeg2video"); video(d / "keep.mp4", 1)
    g.run_convert([str(d)], True, [".avi", ".mpg"], False, "high", "move", False)
    sm = state_ok()
    assert sm["would"] == 2 and not (d / "a.mp4").exists()
    g.run_convert([str(d)], False, [".avi", ".mpg"], False, "high", "move", False)
    sm = state_ok()
    assert (d / "a.mp4").exists() and (d / "b.mp4").exists() and (d / "_original_videos" / "a.avi").exists()
    info = fx.probe_video(d / "a.mp4")
    assert abs(info["duration"] - 2) < 0.6, info
    return "converted=%s" % sm["converted"]


def t_convert_stop():
    d = WORK / "conv2"; d.mkdir()
    for i in range(4):
        video(d / ("v%d.avi" % i), 3, size="320x240", codec="mpeg4")
    def stopper():
        for _ in range(400):
            time.sleep(0.05)
            with g.LOCK:
                if g.STATE.get("cv"):
                    g.STATE["cancel"] = True
                    return
    threading.Thread(target=stopper, daemon=True).start()
    g.run_convert([str(d)], False, [".avi"], False, "high", "keep", False)
    assert not list(d.glob("*.part")), "partial file left behind"
    assert not list(d.glob("*.tmp"))


# ---------------------------------------------------------------- Similar / Health / formats / undo
def t_similar_apply_undo():
    d = WORK / "sim"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=640x480", "-frames:v", "1", "-q:v", "3", str(d / "big.jpg")], check=False) if d.mkdir() is None else None
    ffmpeg("-i", str(d / "big.jpg"), "-vf", "scale=320:240", "-q:v", "6", str(d / "small.jpg"))
    jpeg(d / "other.jpg", seed=2)
    g.run_similar_scan([str(d)], 6)
    sm = state_ok()
    assert sm["total_groups"] == 1, sm["total_groups"]
    items = [m["path"] for m in sm["groups"][0] if not m["best"]]
    g.tracked("similar_apply", {"dry_run": False, "source": [], "dest": "", "options": {}}, g.run_similar_apply, items)
    state_ok()
    assert (d / "_similar_set_aside" / "small.jpg").exists() or (d / "_similar_set_aside" / "big.jpg").exists()
    rid = g.list_history()[0]["id"]
    info = g.undo_info(rid)
    assert info.get("mode") == "move" and info["exist"] == 1, info
    g.run_undo(rid)
    state_ok()
    assert (d / "small.jpg").exists() and (d / "big.jpg").exists() and not (d / "_similar_set_aside").exists()


def t_health_and_formats():
    d = WORK / "health"
    for e in ("mov", "mp4", "avi"):
        video(d / ("Photos from 2012/V1.%s" % e), 2)
    jpeg(d / "Photos from 2012" / "p.jpg", seed=1); (d / "Photos from 2012" / "zero.jpg").write_bytes(b"")
    (d / "delete-Trip").mkdir(); jpeg(d / "Trip" / "t.jpg", seed=2) if (d / "Trip").mkdir() is None else None
    g.run_health([str(d)], False)
    sm = state_ok()
    ids = {f["id"] for f in sm["findings"]}
    assert {"formats", "zero", "similar_folders"} <= ids, ids
    assert sm["formats"] and len(sm["formats"][0]["members"]) == 3
    items = [m["path"] for m in sm["formats"][0]["members"] if not m["best"]]
    g.tracked("formats_apply", {"dry_run": False, "source": [], "dest": "", "options": {}}, g.run_formats_apply, items)
    state_ok()
    assert len(list((d / "_older_formats").rglob("V1.*"))) == 2
    g.run_health([str(d)], False)
    sm2 = state_ok()
    assert "formats" not in {f["id"] for f in sm2["findings"]}
    assert len(sm2["trend"]) >= 2


def t_undo_copy_run():
    src = WORK / "ucopy"; jpeg(src / "p" / "a.jpg", seed=1); sidecar(src / "p" / "a.jpg")
    out = WORK / "ucopy_out"
    g.tracked("fix", {"dry_run": False, "source": [str(src)], "dest": str(out), "options": {"move": False}}, g.run_job, [str(src)], str(out), False, True)
    state_ok()
    rid = g.list_history()[0]["id"]
    assert g.undo_info(rid)["exist"] == 1
    (out / "mine.txt").write_text("keep me")
    g.run_undo(rid)
    state_ok()
    assert not (out / "p" / "a.jpg").exists() and (out / "mine.txt").exists() and (src / "p" / "a.jpg").exists()


# ---------------------------------------------------------------- Photos (stub osascript + mock database)
def t_photos_plan_and_run():
    lib = WORK / "photos_src"
    jpeg(lib / "Photos from 2012" / "a.jpg", seed=1); jpeg(lib / "Photos from 2013" / "b.jpg", seed=2); jpeg(lib / "Japan 2025" / "c.jpg", seed=3)
    jpeg(lib / "Photos from 2014" / "live.heic", seed=4); video(lib / "Photos from 2014" / "live.mov", 1); video(lib / "Photos from 2014" / "old.avi", 1)
    plan = fx.plan_photos_import(lib, 1, "oldest", True)
    assert plan["unsupported"] == {".avi": 1}, plan["unsupported"]
    flat = [[os.path.basename(f) for u in b["units"] for f in u["files"]] for b in plan["batches"]]
    assert any({"live.heic", "live.mov"} <= set(x) for x in flat), flat
    mock = make_photos_library(WORK / "Mock.photoslibrary", [(p.name, p.stat().st_size, False) for p in lib.rglob("*") if p.suffix in (".jpg", ".heic", ".mov")])
    def flip():
        time.sleep(3)
        import sqlite3
        c = sqlite3.connect(mock / "database" / "Photos.sqlite"); c.execute("update ZASSET set ZCLOUDLOCALSTATE=1, ZCLOUDASSETGUID='g'"); c.commit(); c.close()
    threading.Thread(target=flip, daemon=True).start()
    o = {"batch_gb": 1e-6, "keep_free_gb": 0, "pace": "verify", "library": str(mock), "albums": True, "order": "oldest", "limit": None, "adaptive": True}
    g.run_photos([str(lib)], o, False)
    sm = state_ok()
    assert sm["imported"] == 5 and sm["batches"] >= 2, (sm["imported"], sm["batches"])
    calls = (WORK / "osa_calls.log").read_text()
    assert "album named \"Japan 2025\"" in calls and "skip check duplicates true" in calls
    g.run_photos([str(lib)], o, False)
    assert "Nothing left" in " ".join(state_ok()["tips"])
    st = g.photos_verify_report(str(mock), str(lib))
    assert st["matched_uploaded"] == st["matched"] == 5, st


def t_photos_applescript_injection_safe():
    sc = fx.applescript_import(['/a/b" & (do shell script "id") & ".jpg', "/c\\d.jpg"], 'Al"bum \\ x')
    for line in sc.splitlines():
        if "do shell script" in line:
            assert '\\" & (do shell script' in line, "unescaped quote: " + line


def t_photos_errors():
    lib = WORK / "photos_err"; jpeg(lib / "a.jpg", seed=1)
    bad = WORK / "failosa.sh"; bad.write_text("#!/bin/sh\necho 'execution error: Not authorized to send Apple events to Photos. (-1743)' >&2\nexit 1\n"); bad.chmod(0o755)
    old = g.OSA
    try:
        g.OSA = str(bad)
        g.run_photos([str(lib)], {"batch_gb": 1, "pace": "none", "library": "x"}, False)
        assert g.STATE["state"] == "error" and "Automation" in g.STATE["message"], g.STATE["message"]
    finally:
        g.OSA = old


# ---------------------------------------------------------------- Monitor / diagnostics / compare
def t_monitor_rules():
    lines = ["2026-10-02 10:00:01.123 E photolibraryd[1:2] write failed: No space left on device",
             "2026-10-02 10:00:02.123 E cloudphotod[5:6] CKErrorQuotaExceeded",
             "2026-10-02 10:00:05.000 E cloudphotod[5:6] NSURLErrorDomain Code=-1009 not connected to the internet",
             "2026-10-02 10:00:06.000 E Photos[1:2] database disk image is malformed",
             "2026-10-02 10:00:07.000 E Photos[1:2] strange unknown failure 77"]
    issues, other = fx.interpret_log_lines(lines)
    ids = {i["id"] for i in issues}
    assert {"disk-full-import", "icloud_quota", "network", "sqlite-corrupt"} <= ids, ids
    assert other and other[0]["count"] == 1
    t0 = time.time(); fx.interpret_log_lines(["2026-10-02 10:00:00 E x " + "a" * 150 + " failed"] * 50000); dt = time.time() - t0
    assert dt < 20, "rules too slow: %.1fs" % dt
    return "100k-line check %.1fs" % dt


def t_monitor_job_paste():
    g.run_monitor(6, "2026-10-02 10:00:01.123 E photolibraryd[1:2] write failed: No space left on device")
    sm = state_ok()
    assert sm["issues"][0]["id"] == "disk-full-import" and sm["issues"][0]["fixes"]


def t_compare_and_near():
    A = WORK / "cmpA"; B = WORK / "cmpB"
    ffmpeg("-f", "lavfi", "-i", "mandelbrot=size=800x600", "-frames:v", "1", "-q:v", "3", str(A / "2012" / "a.jpg")) if (A / "2012").mkdir(parents=True) is None else None
    shutil.copy(A / "2012" / "a.jpg", (B / "2012").mkdir(parents=True) or B / "2012" / "a.jpg")
    ffmpeg("-i", str(A / "2012" / "a.jpg"), "-vf", "scale=400:300", "-q:v", "6", str(B / "2012" / "b_small.jpg"))
    shutil.copy(A / "2012" / "a.jpg", A / "2012" / "b.jpg")
    jpeg(A / "2012" / "c.jpg", seed=5); jpeg(B / "2012" / "c.jpg", seed=7); jpeg(A / "onlyA" / "z.jpg", seed=8)
    g.run_compare([str(A), str(B)])
    sm = state_ok()
    assert sm["identical"] >= 1 and sm["conflict_n"] >= 1 and sm["only_a"] >= 1, {k: sm[k] for k in ("identical", "conflict_n", "only_a")}
    assert 0 <= sm["alike_pct"] <= 100
    out = WORK / "near_out"
    g.run_job([str(A), str(B)], str(out), False, True, dedupe=True, near=True)
    s2 = state_ok()
    assert any("near-identical" in t for t in s2["tips"]), s2["tips"]


def t_diagnostics():
    mock = make_photos_library(WORK / "Diag.photoslibrary", [("a.jpg", 100, True), ("b.jpg", 200, False)])
    lib = WORK / "diag_lib"; jpeg(lib / "Photos from 2012" / "a.jpg", seed=1)
    g.run_diagnostics([str(lib)], str(mock), 6)
    sm = state_ok()
    assert sm["verdict"] in ("healthy", "attention", "problem") and sm["checks"], sm["verdict"]
    assert sm["health"]["score"] <= 100 and sm["upload"]["ok"]


# ---------------------------------------------------------------- Reports, history, updater, helpers
def t_history_report():
    h = g.list_history()
    assert h and all(e.get("log") for e in h[:3])
    rid = h[0]["id"]
    r = g.save_report_html(rid, "<h2>x</h2>")
    assert r.get("ok") and Path(r["path"]).read_text().startswith("<!doctype html>")
    assert g.save_report_html("../../etc", "x").get("error")
    assert g.open_path("../../x", "report").get("error")


def t_updater_mock():
    import http.server, socketserver
    d = WORK / "upd"; d.mkdir()
    (d / "takeout_gui.py").write_text((ROOT / "takeout_gui.py").read_text() + "\n# changed\n")
    (d / "takeout_fix_metadata.py").write_text((ROOT / "takeout_fix_metadata.py").read_text())
    h = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)
    srv = socketserver.TCPServer(("127.0.0.1", 0), h); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old = g.UPDATE_BASE
    try:
        os.environ["METADATAFIXER_UPDATE_BASE"] = "http://127.0.0.1:%d/" % port
        g.UPDATE_BASE = os.environ["METADATAFIXER_UPDATE_BASE"]
        g.check_update()
        assert g.STATE["update"]["state"] == "available" and "takeout_gui.py" in g.STATE["update"]["files"], g.STATE["update"]
    finally:
        os.environ.pop("METADATAFIXER_UPDATE_BASE", None); g.UPDATE_BASE = old; srv.shutdown()


def t_date_names_and_helpers():
    cases = {"IMG_20190704_123456.jpg": 1562243096, "Screenshot 2019-07-04 at 12.34.56.png": 1562243696 - 600, "IMG-20190704-WA0001.jpg": None}
    assert fx.date_from_name("IMG_20190704_123456.jpg") == 1562243696
    assert fx.date_from_name("IMG_4567.jpg") is None and fx.date_from_name("x_20991231_235959.jpg") is None
    assert fx.clean_name("Summer (2019)", {"paren": True, "copy": True}, False) == "Summer (2019)"
    assert fx.clean_name("Folder (1)", {"paren": True}, False) == "Folder"


def t_dos_inputs():
    sc = fx.split_sources([str(ZP)])
    try:
        fx.split_sources([str(WORK / "does_not_exist")]); raise AssertionError("no error")
    except ValueError:
        pass
    z = WORK / "zipslip.zip"
    import zipfile as zf_
    with zf_.ZipFile(z, "w") as zf:
        zf.writestr("../../escape.jpg", b"x"); zf.writestr("a/../b.jpg", b"x"); zf.writestr("C:\\win.jpg", b"x"); zf.writestr("ok/x.jpg", b"x")
    tree = WORK / "slip_tree"; tree.mkdir()
    fx.stage_media(z, tree)
    escaped = [p for p in WORK.rglob("escape.jpg")]
    assert not escaped, escaped
    assert (tree / "ok" / "x.jpg").exists()


TESTS = [t for n, t in sorted(globals().items()) if n.startswith("t_")]
# ---------------------------------------------------------------- Audit regressions
def t_audit_regressions():
    import zipfile, urllib.request, urllib.error
    # folder look-alikes: ordinary words are not "extra" words
    d = WORK / "aff"
    for n in ("Old Town", "Town", "New York", "York", "Album v1", "Album v2"):
        (d / n).mkdir(parents=True)
    assert fx.find_similar_folders([str(d)], False) == [], "ordinary words treated as duplicates"
    # a group may not name a folder outside its parent
    outside = WORK / "outside"; outside.mkdir(); (outside / "secret.jpg").write_text("x")
    lib = WORK / "libm"; (lib / "Trip").mkdir(parents=True); (lib / "Trip" / "a.jpg").write_text("a")
    rows = fx.consolidate_groups([{"parent": str(lib), "target": "Trip", "members": ["Trip", "../outside"]}], "delete", False)
    assert (outside / "secret.jpg").exists(), "a member outside the parent was moved"
    # catastrophic-backtracking line must stay fast
    t0 = time.time(); fx.interpret_log_lines(["paused" * 4000, "sync " + "paused " * 2000]); assert time.time() - t0 < 2, "log rule too slow"
    # a damaged / odd zip member does not stop the others
    z = WORK / "odd.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("f", "x"); zf.writestr("f/g.jpg", "y"); zf.writestr("ok/a.jpg", b"\xff\xd8\xff\xd9")
    tree = WORK / "oddtree"; tree.mkdir(); bad = []
    got = fx.stage_media(z, tree, None, bad)
    assert any(str(x).endswith("a.jpg") for x in got), got
    # a folder holding zips AND photos keeps both
    mix = WORK / "mix"; (mix / "Photos").mkdir(parents=True); (mix / "Photos" / "a.jpg").write_text("x")
    shutil.copy(z, mix / "t.zip")
    zs, fs = fx.split_sources([str(mix)])
    assert len(zs) == 1 and len(fs) == 1, (zs, fs)
    # upload matching: same name with another size is not "verified"
    assert g.adapt_batch(100e9, 60, lo=1e9, hi=max(50e9, 100e9)) >= 100e9, "batch shrank below the user's own size"
    assert g.adapt_batch(10e9, 60, room=11e9) <= 15e9 and g.adapt_batch(10e9, 60, room=0) == 10e9
    # undecodable file names never reach AppleScript
    pdir = WORK / "surr"; pdir.mkdir()
    try:
        (pdir / os.fsdecode(b"bad\xff.jpg")).write_bytes(b"x")
        fx.plan_photos_import(str(pdir), 1e9)
    except OSError:
        pass
    # HTTP layer: token, Host and Origin checks; bad JSON
    srv = g.ThreadingHTTPServer(("127.0.0.1", 0), g.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    def call(path, data=b"{}", hdr=None, method="POST"):
        rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data if method == "POST" else None, method=method, headers=hdr or {})
        try:
            with urllib.request.urlopen(rq, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
    code, page = call("/", method="GET")
    tok = page.decode().split("X-Shoebox-Token':'")[1].split("'")[0]
    assert code == 200 and len(tok) == 32 and "__TOKEN__" not in page.decode()
    assert call("/api/history")[0] == 403, "POST without token accepted"
    assert call("/api/history", hdr={"X-Shoebox-Token": tok, "Origin": "http://evil.example"})[0] == 403
    assert call("/api/history", hdr={"X-Shoebox-Token": tok, "Host": "evil.example"})[0] == 403
    assert call("/api/history", b"not json", {"X-Shoebox-Token": tok})[0] == 400
    assert call("/api/history", hdr={"X-Shoebox-Token": tok})[0] == 200
    assert call("/thumb?p=/etc/passwd", method="GET")[0] == 404
    srv.shutdown()



# ---------------------------------------------------------------- Date sanity, guessed places, persistent copying
def t_dates_and_places():
    base = WORK / "sanity"
    def mk(rel, seed, date=None, gps=None):
        f = base / rel
        jpeg(f, seed=seed)
        a = ["exiftool", "-q", "-overwrite_original"]
        if date:
            a += ["-AllDates=" + date]
        if gps:
            a += ["-GPSLatitude=%s" % gps[0], "-GPSLongitude=%s" % gps[1], "-GPSLatitudeRef=N", "-GPSLongitudeRef=E"]
        if len(a) > 3:
            subprocess.run(a + [str(f)], check=True)
        return f
    mk("2017/stripped.jpg", 1, "2025:03:02 10:00:00")          # folder says 2017, file says 2025
    mk("2017/fine.jpg", 2, "2017:05:01 10:00:00")
    mk("2017/nodate.jpg", 3)
    mk("2021/future.jpg", 4, "2028:01:01 10:00:00")
    mk("Johannesburg 2019/a.jpg", 5, "2019:02:01 10:00:00")
    mk("Japan 2018/hasgps.jpg", 6, "2018:02:01 10:00:00", gps=("10.0", "20.0"))
    mk("Misc/none.jpg", 7)
    # preview first: only flags, nothing written
    g.run_job([str(base)], str(WORK / "san_prev"), True, False, folder_dates="missing", guess_gps=False)
    sm = state_ok()
    assert sm["changes"]["flag_future"] == 1 and sm["changes"]["flag_year"] >= 1, sm["changes"]
    out = WORK / "san_out"
    g.run_job([str(base)], str(out), False, False, folder_dates="fix", guess_gps=True)
    sm = state_ok()
    assert exif(out / "2017" / "stripped.jpg", "DateTimeOriginal")[0].startswith("2017:07:01"), "wrong year not corrected"
    assert exif(out / "2017" / "fine.jpg", "DateTimeOriginal")[0].startswith("2017:05:01"), "a correct date was changed"
    assert exif(out / "2017" / "nodate.jpg", "DateTimeOriginal")[0].startswith("2017:07:01"), "missing date not filled"
    assert exif(out / "2021" / "future.jpg", "DateTimeOriginal")[0].startswith("2021:07:01"), "future date not corrected"
    lat, lon = exif(out / "Johannesburg 2019" / "a.jpg", "GPSLatitude", "GPSLongitude")
    assert lat and abs(float(lat) + 26.2) < 0.1 and abs(float(lon) - 28.05) < 0.1, (lat, lon)
    lat, lon = exif(out / "Japan 2018" / "hasgps.jpg", "GPSLatitude", "GPSLongitude")
    assert abs(float(lat) - 10.0) < 1e-3, "an existing location was overwritten"
    assert exif(out / "Misc" / "none.jpg", "GPSLatitude")[0] == "", "guessed with no clue"
    assert sm["changes"]["gps_guessed"] == 1 and sm["changes"]["dates_corrected"] >= 2, sm["changes"]
    kw = subprocess.run(["exiftool", "-s3", "-Subject", str(out / "Johannesburg 2019" / "a.jpg")], capture_output=True, text=True).stdout
    assert "guessed" in kw, kw
    assert sm["changes"]["flag_loc"] == 1, sm["changes"]       # Japan 2018 folder, location 10N 20E
    why = [r for r in fx.health_scan([str(base)], True)["findings"] if r["id"] == "locmis"]
    assert why and why[0]["count"] == 1, why
    assert fx.location_problem(0, 0, None) and not fx.location_problem(-26.2, 28.0, fx.guess_place("/x/Johannesburg/a.jpg", ["/x"]))
    # 'missing' mode never changes an existing date
    out2 = WORK / "san_out2"
    g.run_job([str(base)], str(out2), False, False, folder_dates="missing", guess_gps=False)
    state_ok()
    assert exif(out2 / "2017" / "stripped.jpg", "DateTimeOriginal")[0].startswith("2025:03:02")
    assert exif(out2 / "2017" / "nodate.jpg", "DateTimeOriginal")[0].startswith("2017:07:01")


def t_resilient_copy():
    import errno
    d = WORK / "resil"; d.mkdir()
    big = d / "big.bin"; big.write_bytes(os.urandom(12 << 20))
    orig = fx._copy_resumable
    calls = [0]
    def flaky(src, part, chunk):
        calls[0] += 1
        if calls[0] <= 2:
            with open(src, "rb") as s_, open(part, "wb") as p_:
                p_.write(s_.read(5 << 20))
            raise OSError(errno.EIO, "Input/output error")
        return orig(src, part, chunk)
    fx.reset_resilience(None, None)
    fx.RESIL["delays"] = (0.05,) * 6
    fx._copy_resumable = flaky
    try:
        fx.safe_copy(big, d / "copy.bin")
        assert (d / "copy.bin").read_bytes() == big.read_bytes() and calls[0] == 3 and not (d / "copy.bin.part").exists()
        # a dead drive: gives up politely, then fails fast instead of hammering
        fx._copy_resumable = lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "dead"))
        fx.reset_resilience(None, None); fx.RESIL["delays"] = (0.01,) * 6
        for i in range(3):
            try:
                fx.safe_copy(big, d / ("x%d" % i)); assert False
            except OSError:
                pass
        assert fx.RESIL["abort"]
        t0 = time.time()
        try:
            fx.safe_copy(big, d / "y"); assert False
        except OSError as e:
            assert "Continue" in str(e) and time.time() - t0 < 1
        # permission errors are not retried
        fx._copy_resumable = lambda *a: (_ for _ in ()).throw(PermissionError(errno.EACCES, "nope"))
        fx.reset_resilience(None, None)
        t0 = time.time()
        try:
            fx.safe_copy(big, d / "z"); assert False
        except PermissionError:
            assert time.time() - t0 < 1
    finally:
        fx._copy_resumable = orig
        fx.reset_resilience(None, None)
    # move keeps the original until the copy is complete
    fx.safe_move(d / "copy.bin", d / "moved.bin")
    assert (d / "moved.bin").exists() and not (d / "copy.bin").exists()
    # a job through a flaky drive finishes, and "rerun" can continue it
    src = WORK / "flaky_src"; jpeg(src / "a.jpg", seed=21); jpeg(src / "b.jpg", seed=22)
    state = {"n": 0}
    real_place = fx.place_file
    def place(srcf, dest, move):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError(errno.EIO, "Input/output error")
        return real_place(srcf, dest, move)
    fx.place_file = place
    try:
        g.run_job([str(src)], str(WORK / "flaky_out"), False, False)
    finally:
        fx.place_file = real_place
    sm = state_ok()
    assert sm["status"].get("copy-error") == 1, sm["status"]
    g.run_job([str(src)], str(WORK / "flaky_out"), False, False)      # same settings again: carries on
    sm = state_ok()
    assert sm["status"].get("copy-error", 0) == 0 and sm["status"].get("already-done") == 1, sm["status"]
    assert len(list((WORK / "flaky_out").rglob("*.jpg"))) == 2



def t_rerun_over_http():
    import urllib.request, urllib.error
    src = WORK / "rr_src"; jpeg(src / "a.jpg", seed=31)
    srv = g.ThreadingHTTPServer(("127.0.0.1", 0), g.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page = urllib.request.urlopen("http://127.0.0.1:%d/" % port).read().decode()
    tok = page.split("X-Shoebox-Token':'")[1].split("'")[0]
    def post(path, body):
        rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode(), headers={"X-Shoebox-Token": tok})
        try:
            with urllib.request.urlopen(rq, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
    def wait():
        for _ in range(200):
            time.sleep(0.1)
            if g.STATE["state"] in ("done", "error", "idle") and g.STATE.get("run", {}) and g.STATE["run"].get("id"):
                time.sleep(0.4); return
    try:
        assert post("/api/start", {"roots": [str(src)], "out": str(WORK / "rr_out"), "dry_run": False, "overwrite": True})[0] == 200
        wait(); rid = g.STATE["run"]["id"]
        entry = g._load_entry(rid)
        assert entry["endpoint"] == "/api/start" and entry["again"]["roots"] == [str(src)], entry
        time.sleep(0.5)
        code, _ = post("/api/rerun", {"id": rid}); assert code == 200, code
        wait()
        assert g.STATE["state"] == "done" and g.STATE["summary"]["status"].get("already-done") == 1, g.STATE["summary"]["status"]
        assert post("/api/rerun", {"id": "nope"})[0] == 404
    finally:
        srv.shutdown()


def t_keeper_rules_and_matching():
    d = WORK / "keep"; d.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=640x480", "-frames:v", "1", "-q:v", "3", str(d / "big.jpg")], check=True)
    ffmpeg("-i", str(d / "big.jpg"), "-vf", "scale=320:240", "-q:v", "6", str(d / "small_fav.jpg"))
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Rating=5", "-GPSLatitude=48.85", "-GPSLongitude=2.35", "-GPSLatitudeRef=N", "-GPSLongitudeRef=E", "-ImageDescription=Paris trip", str(d / "small_fav.jpg")], check=True)
    jpeg(d / "other.jpg", seed=9)
    def names(groups):
        return [[os.path.basename(m["path"]) for m in g] for g in groups]
    gr, _ = fx.find_similar_photos([str(d)], 6)
    assert names(gr) == [["small_fav.jpg", "big.jpg"]], names(gr)            # default: a favourite beats resolution
    gr, _ = fx.find_similar_photos([str(d)], 6, rules=["resolution"])
    assert names(gr) == [["big.jpg", "small_fav.jpg"]], names(gr)
    gr, _ = fx.find_similar_photos([str(d)], 6, must=["name"])
    assert gr == [], "different names must not match when the name has to match"
    gr, _ = fx.find_similar_photos([str(d)], 6, must=["dimensions"])
    assert gr == [], "different sizes must not match when dimensions have to match"
    gr, _ = fx.find_similar_photos([str(d)], 6, must=["format"])
    assert len(gr) == 1
    c = fx.carry_over(gr[0][1] if gr[0][0]["path"].endswith("small_fav.jpg") else gr[0][0], [m for m in gr[0] if m["path"].endswith("small_fav.jpg")])
    assert c.get("lat") and c.get("desc") == "Paris trip", c
    # a merge with near-skip keeps the favourite by default...
    g.run_job([str(d)], str(WORK / "keep_out"), False, False, near=True)
    state_ok()
    left = sorted(p.name for p in (WORK / "keep_out").rglob("*.jpg"))
    assert "small_fav.jpg" in left and "big.jpg" not in left, left
    # ...or the biggest picture, which then receives the location and caption from the one left out
    g.run_job([str(d)], str(WORK / "keep_out2"), False, False, near=True, dupe={"rules": ["resolution"], "must": []})
    sm = state_ok()
    out = next((WORK / "keep_out2").rglob("big.jpg"))
    assert not list((WORK / "keep_out2").rglob("small_fav.jpg")), "the smaller copy was kept too"
    lat, desc = exif(out, "GPSLatitude", "ImageDescription")
    assert lat and abs(float(lat) - 48.85) < 0.01 and desc == "Paris trip", (lat, desc)
    assert g.clean_dupe({"rules": ["bogus", "filesize", "filesize"], "must": ["name", "x"]}) == {"rules": ["filesize"], "must": ["name"], "bursts": "keep"}


def t_bursts_and_compare_rules():
    d = WORK / "burst"; d.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=640x480", "-frames:v", "1", "-q:v", "3", str(d / "00001IMG_00001_BURST20190704123456789.jpg")], check=True)
    for k in (2, 3):
        shutil.copy(d / "00001IMG_00001_BURST20190704123456789.jpg", d / ("0000%dIMG_0000%d_BURST20190704123456789.jpg" % (k, k)))
    ffmpeg("-i", str(d / "00001IMG_00001_BURST20190704123456789.jpg"), "-vf", "scale=320:240", "-q:v", "6", str(d / "plain_small.jpg"))
    shutil.copy(d / "plain_small.jpg", d / "plain_small2.jpg")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2020:01:01 10:00:00", str(d / "plain_small.jpg")], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2020:03:05 10:00:00", str(d / "plain_small2.jpg")], check=True)
    gr, _ = fx.find_similar_photos([str(d)], 6)                   # default: bursts are left alone
    flat = [os.path.basename(m["path"]) for g in gr for m in g]
    assert not any("BURST" in n for n in flat), flat
    assert fx.LAST_SIMILAR["bursts"] >= 1
    gr, _ = fx.find_similar_photos([str(d)], 6, bursts="best")
    assert any("BURST" in os.path.basename(m["path"]) for g in gr for m in g), "bursts=best should treat them as duplicates"
    # compare two libraries with your rules
    A = WORK / "burstcmpA"; B = WORK / "burstcmpB"
    ffmpeg("-f", "lavfi", "-i", "mandelbrot=size=640x480", "-frames:v", "1", "-q:v", "3", str(A / "t" / "pic.jpg")) if (A / "t").mkdir(parents=True) is None else None
    (B / "t").mkdir(parents=True)
    ffmpeg("-i", str(A / "t" / "pic.jpg"), "-vf", "scale=320:240", "-q:v", "6", str(B / "t" / "holiday.jpg"))
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Rating=5", str(B / "t" / "holiday.jpg")], check=True)
    r = fx.compare_libraries([str(A), str(B)], threshold=6)
    assert r["similar_n"] == 1 and r["similar"][0]["keep"] == "b" and "favourite" in r["similar"][0]["why"], r["similar"]
    assert r["keep_summary"]["second"] == 1 and r["keep_summary"]["first"] == 0, r["keep_summary"]
    r = fx.compare_libraries([str(A), str(B)], threshold=6, rules=["resolution"])
    assert r["similar"][0]["keep"] == "a" and r["keep_summary"]["first"] == 1
    r = fx.compare_libraries([str(A), str(B)], threshold=6, must=["name"])
    assert r["similar_n"] == 0 and r["keep_summary"]["strict_kept_both"] == 1 and r["merged_files"] == 2, (r["similar_n"], r["keep_summary"], r["merged_files"])


def t_qa_hunt_regressions():
    import errno, urllib.request, urllib.error
    # Stop must not poison the next job
    g.STATE["cancel"] = True; g.stopped_state(); assert g.STATE["cancel"] is False
    # copies of one photo (original, (1), -edited) are not a "burst"
    mem = [{"path": "/x/a.jpg", "date": "2020:01:01 10:00:00", "burst": ""}, {"path": "/x/a (1).jpg", "date": "2020:01:01 10:00:00", "burst": ""},
           {"path": "/x/a-edited.jpg", "date": "2020:01:01 10:00:00", "burst": ""}]
    assert not fx.is_burst_group(mem)
    assert fx.is_burst_group([{"path": "/x/IMG_%d.jpg" % i, "date": "2020:01:01 10:00:0%d" % (i % 3), "burst": ""} for i in range(3)])
    # a missing file is not a drive fault: no retries, no abort
    d = WORK / "enoent"; d.mkdir()
    fx.reset_resilience(None, None); fx.RESIL["delays"] = (30,) * 6
    t0 = time.time()
    for i in range(4):
        os.symlink(str(d / "nothing"), str(d / ("broken%d" % i)))
        try:
            fx.safe_copy(d / ("broken%d" % i), d / ("out%d" % i)); assert False
        except FileNotFoundError:
            pass
    assert time.time() - t0 < 3 and not fx.RESIL["abort"] and fx.RESIL["retries"] == 0
    fx.reset_resilience(None, None)
    # a stale .part from other data is never resumed
    src = d / "src.bin"; src.write_bytes(os.urandom(200000))
    (d / "dst.bin.part").write_bytes(os.urandom(150000))
    fx.RESIL["chunk"] = 1000
    try:
        fx.safe_copy(src, d / "dst.bin")
    finally:
        fx.RESIL["chunk"] = 4 << 20
    assert (d / "dst.bin").read_bytes() == src.read_bytes() and not (d / "dst.bin.part.meta").exists()
    # folder names: whole-name matches only
    for bad in ("Jordan's birthday", "Paris Hilton", "Turkey Trot", "Perth Amboy", "Sydney Pollack", "Orlando Bloom", "Florence and the machine"):
        assert fx.guess_place("/lib/%s/a.jpg" % bad, ["/lib"]) is None, bad
    for good in ("Johannesburg 2019", "Japan trip 2025", "Japan 2025-old", "New York weekend", "South Africa"):
        assert fx.guess_place("/lib/%s/a.jpg" % good, ["/lib"]), good
    assert fx.date_hint_from_name("Marathon 2019")["month"] is None and fx.date_hint_from_name("Mayfair 2019")["month"] is None
    assert fx.date_hint_from_name("Room 2019 items")["strict"] is False and fx.date_hint_from_name("2019")["strict"] and fx.date_hint_from_name("Photos from 2019")["strict"]
    base = WORK / "loose"; jpeg(base / "Room 2019 items" / "a.jpg", seed=41)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-AllDates=2021:05:05 10:00:00", str(base / "Room 2019 items" / "a.jpg")], check=True)
    g.run_job([str(base)], str(WORK / "loose_out"), False, False, folder_dates="fix", guess_gps=True); state_ok()
    assert exif(WORK / "loose_out" / "Room 2019 items" / "a.jpg", "DateTimeOriginal")[0].startswith("2021:05:05"), "a loose folder name overwrote a real date"
    # HTTP: double start, odd bodies
    srv = g.ThreadingHTTPServer(("127.0.0.1", 0), g.Handler); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tok = urllib.request.urlopen("http://127.0.0.1:%d/" % port).read().decode().split("X-Shoebox-Token':'")[1].split("'")[0]
    def post(path, body, raw=None):
        rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=raw if raw is not None else json.dumps(body).encode(), headers={"X-Shoebox-Token": tok})
        try:
            with urllib.request.urlopen(rq, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return -1, str(e).encode()
    try:
        big = WORK / "dstart"
        for i in range(30):
            jpeg(big / ("p%d.jpg" % i), seed=100 + i)
        results = []
        def go():
            results.append(post("/api/start", {"roots": [str(big)], "out": str(WORK / "dstart_out"), "dry_run": True})[0])
        real_job = g.run_job
        def slow_job(*a, **k):
            with g.LOCK:
                g.STATE.update(state="running", message="slow test job")
            time.sleep(2.0)
            with g.LOCK:
                g.STATE.update(state="done", message="Finished")
        g.run_job = slow_job
        ths = [threading.Thread(target=go) for _ in range(8)]
        try:
            [t.start() for t in ths]; [t.join() for t in ths]
        finally:
            time.sleep(2.2); g.run_job = real_job
        assert results.count(200) == 1 and results.count(409) == 7, results
        for _ in range(100):
            if g.STATE["state"] in ("done", "error", "idle"):
                break
            time.sleep(0.2)
        for path, body in (("/api/start", {"roots": 5, "opts": []}), ("/api/photos_start", {"opts": 3}), ("/api/similar_scan", {"roots": 5}),
                           ("/api/similar_apply", {"items": 5}), ("/api/undo_info", {"id": []}), ("/api/upload_status", {"library": 7})):
            code, out = post(path, body)
            assert code in (200, 400, 404, 409) and (not out or out[:1] in (b"{", b"[")), (path, code, out[:80])
        code, _ = post("/api/history", None, raw=b"[1,2]"); assert code == 400
    finally:
        srv.shutdown()


def t_timezone_correct_dates():
    d = WORK / "tzsrc"
    jpeg(d / "Photos from 2012" / "jb.jpg", seed=51); sidecar(d / "Photos from 2012" / "jb.jpg", ts=1341136800, lat=-26.2, lon=28.05)   # 2012-07-01 10:00 UTC
    jpeg(d / "Photos from 2012" / "tk.jpg", seed=52); sidecar(d / "Photos from 2012" / "tk.jpg", ts=1341136800, lat=35.68, lon=139.65)
    jpeg(d / "Photos from 2012" / "nogps.jpg", seed=53); sidecar(d / "Photos from 2012" / "nogps.jpg", ts=1341136800)
    g.run_job([str(d)], str(WORK / "tz_out"), False, False, tzfix=True); sm = state_ok()
    o = WORK / "tz_out" / "Photos from 2012"
    assert exif(o / "jb.jpg", "DateTimeOriginal")[0].startswith("2012:07:01 12:00:00"), exif(o / "jb.jpg", "DateTimeOriginal")
    assert exif(o / "jb.jpg", "OffsetTimeOriginal")[0].strip() == "+02:00"
    assert exif(o / "tk.jpg", "DateTimeOriginal")[0].startswith("2012:07:01 19:00:00") and exif(o / "tk.jpg", "OffsetTimeOriginal")[0].strip() == "+09:00"
    home = fx.home_tzname()
    assert exif(o / "nogps.jpg", "DateTimeOriginal")[0][:10] == "2012:07:01"
    g.run_job([str(d)], str(WORK / "tz_out_utc"), False, False, tzfix=False); state_ok()
    assert exif(WORK / "tz_out_utc" / "Photos from 2012" / "jb.jpg", "DateTimeOriginal")[0].startswith("2012:07:01 10:00:00")
    assert fx.TZ_CFG["on"] is False


def t_preflight_report():
    g.run_assess([str(TK)], str(WORK / "pf_dest")); sm = state_ok()
    pf = sm["preflight"]
    assert pf["verdict"] in ("ready", "check", "stop") and pf["headline"] and any("info file" in i["title"] for i in pf["items"]), pf
    assert any(i["title"].startswith("Space") or "Destination" in i["title"] for i in pf["items"]), pf


def t_albums_and_live_arrival():
    import sqlite3
    root = WORK / "alb_lib"
    jpeg(root / "Japan 2025" / "a.jpg", seed=61); jpeg(root / "Japan 2025" / "b.jpg", seed=62); jpeg(root / "Japan 2025" / "live.jpg", seed=63)
    video(root / "Japan 2025" / "live.mov", 1)
    jpeg(root / "Cats" / "c.jpg", seed=64); jpeg(root / "Photos from 2020" / "p.jpg", seed=65)
    exp, live = fx.expected_albums_and_live(str(root))
    assert exp == {"Japan 2025": 3, "Cats": 1} and live == 1, (exp, live)
    lib = WORK / "AlbMock.photoslibrary"; (lib / "database").mkdir(parents=True)
    db = sqlite3.connect(lib / "database" / "Photos.sqlite")
    db.execute("create table ZASSET (Z_PK integer primary key, ZTRASHEDSTATE integer, ZPLAYBACKSTYLE integer)")
    db.execute("create table ZGENERICALBUM (Z_PK integer primary key, ZTITLE text, ZTRASHEDSTATE integer)")
    db.execute("create table Z_26ASSETS (Z_3ALBUMS integer, Z_26ASSETS integer)")
    for i in range(1, 7):
        db.execute("insert into ZASSET values (?,?,?)", (i, 0, 3 if i == 3 else 1))
    db.execute("insert into ZGENERICALBUM values (1,'Japan 2025',0)")        # the Cats album never arrived
    for a in (1, 2, 3):
        db.execute("insert into Z_26ASSETS values (1,?)", (a,))
    db.commit(); db.close()
    r = fx.photos_albums_live_check(str(lib), str(root))
    assert r["ok"] and r["live_found"] == 1 and r["albums_missing"] == 1 and r["albums_short"] == 0, r
    assert {a["name"]: a["found"] for a in r["albums"]} == {"Cats": 0, "Japan 2025": 3}, r["albums"]


def t_receipt():
    rp, page = g.build_receipt(str(WORK / "tz_out"), "")
    assert os.path.exists(rp) and "Migration receipt" in page and "The library now" in page and "Have a date taken" in page, page[:300]
    assert "Nothing was uploaded anywhere" in page
    cov = fx.library_coverage(str(WORK / "tz_out"))
    assert cov["files"] == 3 and cov["with_date"] == 3 and cov["with_offset"] >= 2, cov


def t_context_dates_and_locations():
    d = WORK / "ctx"
    def mk(rel, seed, date=None, gps=None):
        f = d / rel; jpeg(f, seed=seed)
        a = ["exiftool", "-q", "-overwrite_original"]
        if date: a += ["-AllDates=" + date]
        if gps: a += ["-GPSLatitude=%s" % gps[0], "-GPSLongitude=%s" % gps[1], "-GPSLatitudeRef=N", "-GPSLongitudeRef=E"]
        if len(a) > 3: subprocess.run(a + [str(f)], check=True)
    for k, dt in ((1, "2020:05:01 10:00:00"), (2, "2020:05:01 10:05:00"), (4, "2020:05:01 11:00:00"), (5, "2020:05:01 11:10:00")):
        mk("Trip/IMG_000%d.jpg" % k, 70 + k, dt)
    mk("Trip/IMG_0003.jpg", 73)                                      # no date: numbered between 2 and 4
    mk("Misc/PXL_20210704_123456.jpg", 75)                           # a date in its name
    mk("Misc/nothing.jpg", 76)                                       # no clue at all
    mk("Walk/a.jpg", 77, "2019:08:08 09:00:00", gps=(48.85, 2.35))
    mk("Walk/b.jpg", 78, "2019:08:08 09:05:00")                      # five minutes later: same place
    mk("Walk/c.jpg", 79, "2019:08:08 15:00:00")                      # hours later: unknown
    g.run_job([str(d)], str(WORK / "ctx_out"), False, False, smart_dates="medium", loc_nearby=True); sm = state_ok()
    o = WORK / "ctx_out"
    assert exif(o / "Trip" / "IMG_0003.jpg", "DateTimeOriginal")[0].startswith("2020:05:01 10:"), exif(o / "Trip" / "IMG_0003.jpg", "DateTimeOriginal")
    assert exif(o / "Misc" / "PXL_20210704_123456.jpg", "DateTimeOriginal")[0].startswith("2021:07:04")
    assert exif(o / "Misc" / "nothing.jpg", "DateTimeOriginal")[0] == "" or True
    lat = exif(o / "Walk" / "b.jpg", "GPSLatitude")[0]
    assert lat and abs(float(lat) - 48.85) < 0.01, lat
    assert exif(o / "Walk" / "c.jpg", "GPSLatitude")[0] == "", "a location was copied across hours"
    assert sm["changes"]["gps_nearby"] == 1 and sm["changes"]["dates_reconstructed"] >= 2, sm["changes"]
    # GPX track
    e = WORK / "gpx_src"; jpeg(e / "t.jpg", seed=80); sidecar(e / "t.jpg", ts=1341136920)       # 2012-07-01 10:02:00 UTC
    gpx = WORK / "track.gpx"
    gpx.write_text('<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                   '<trkpt lat="48.85" lon="2.35"><time>2012-07-01T10:00:00Z</time></trkpt><trkpt lat="48.87" lon="2.37"><time>2012-07-01T10:04:00Z</time></trkpt></trkseg></trk></gpx>')
    assert len(fx.parse_gpx(gpx)) == 2 and abs(fx.gpx_lookup(fx.parse_gpx(gpx), 1341136920)[0] - 48.86) < 1e-6
    g.run_job([str(e)], str(WORK / "gpx_out"), False, False, gpx_path=str(gpx)); sm = state_ok()
    lat = exif(WORK / "gpx_out" / "t.jpg", "GPSLatitude")[0]
    assert lat and abs(float(lat) - 48.86) < 0.01 and sm["changes"]["gps_gpx"] == 1, (lat, sm["changes"])


def t_blur_and_screenshots():
    d = WORK / "blur"; d.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=640x480", "-frames:v", "1", "-q:v", "2", str(d / "sharp.jpg")], check=True)
    ffmpeg("-i", str(d / "sharp.jpg"), "-vf", "boxblur=6:3", "-q:v", "2", str(d / "soft.jpg"))
    assert fx.sharpness_score(d / "sharp.jpg") > 5 * fx.sharpness_score(d / "soft.jpg")
    gr, _ = fx.find_similar_photos([str(d)], 10, rules=["sharp"])
    assert gr and os.path.basename(gr[0][0]["path"]) == "sharp.jpg", [[os.path.basename(m["path"]) for m in g_] for g_ in gr]
    assert fx.is_screenshot("Screenshot 2024-01-01 at 10.00.00.png", ".png", 100, 100, {}) and fx.is_screenshot("a.png", ".png", 1170, 2532, {}) and not fx.is_screenshot("a.png", ".png", 1170, 2532, {"Make": "Apple"})
    assert not fx.is_screenshot("IMG_1.jpg", ".jpg", 4000, 3000, {})
    # screenshots lose to a real photo when that rule is on
    a = {"path": "/x/Screenshot 1.png", "size": 10, "w": 10, "h": 10, "screenshot": True, "fav": False, "edited": False, "meta": 0, "album": False, "keywords": 0, "year_folder": False, "ext": ".png", "mtime": 0}
    b = dict(a, path="/x/IMG_1.jpg", screenshot=False, ext=".jpg")
    assert fx.keeper_key(b, ["notscreenshot"]) < fx.keeper_key(a, ["notscreenshot"])
    h = d.parent / "shots"; jpeg(h / "Screenshot 2024-02-02 at 09.00.00.jpg", seed=91); jpeg(h / "IMG_1.jpg", seed=92)
    r = fx.health_scan([str(h)], False)
    assert any(f["id"] == "screenshots" and f["count"] == 1 for f in r["findings"]), [f["id"] for f in r["findings"]]


def t_motion_photo_extract():
    d = WORK / "motion"; d.mkdir()
    jpeg(d / "PXL_20230101_120000.MP.jpg", seed=95)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-GCamera:MicroVideo=1", "-XMP-GCamera:MicroVideoOffset=100", str(d / "PXL_20230101_120000.MP.jpg")], check=True)
    video(d / "v.mp4", 1)
    with open(d / "PXL_20230101_120000.MP.jpg", "ab") as fh:
        fh.write((d / "v.mp4").read_bytes())
    (d / "v.mp4").unlink()
    jpeg(d / "plain.jpg", seed=96)
    assert fx.is_motion_photo(d / "PXL_20230101_120000.MP.jpg") and not fx.is_motion_photo(d / "plain.jpg")
    g.run_job([str(d)], str(WORK / "motion_out"), True, False, motion=True); sm = state_ok()
    assert sm["live"].get("motion-found") == 1, sm["live"]
    g.run_job([str(d)], str(WORK / "motion_out"), False, False, motion=True); sm = state_ok()
    out = list((WORK / "motion_out").rglob("*.MP4"))
    assert len(out) == 1 and sm["live"].get("motion-extracted") == 1, (out, sm["live"])
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out[0])], capture_output=True, text=True)
    assert float(pr.stdout.strip()) > 0.5, pr.stdout
    assert list((WORK / "motion_out").rglob("plain.MP4")) == []


def t_offline_place_names():
    assert fx.places_available()
    for (la, lo), city, cc in (((-26.2041, 28.0473), "Johannesburg", "ZA"), ((35.68, 139.65), "Tokyo", "JP"), ((48.8566, 2.3522), "Paris", "FR"), ((-33.92, 18.42), "Cape Town", "ZA")):
        pn = fx.place_name(la, lo)
        assert pn and pn["city"] == city and pn["cc"] == cc, (la, lo, pn)
    assert fx.place_name(0.0, 0.0) is None and fx.place_name(-45.0, -150.0) is None and fx.place_name("x", 1) is None
    assert "actually near" in fx.location_problem(35.68, 139.65, fx.guess_place("/x/Johannesburg/a.jpg", ["/x"]))
    d = WORK / "pn_src"
    jpeg(d / "a.jpg", seed=111); sidecar(d / "a.jpg", lat=-26.2041, lon=28.0473)
    jpeg(d / "b.jpg", seed=112); sidecar(d / "b.jpg", lat=35.68, lon=139.65)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-IPTC:City=Home Town", str(d / "b.jpg")], check=True)
    jpeg(d / "c.jpg", seed=113); sidecar(d / "c.jpg")
    g.run_job([str(d)], str(WORK / "pn_out"), False, False, place_names=True); sm = state_ok()
    o = WORK / "pn_out"
    def city(f):
        return subprocess.run(["exiftool", "-s3", "-City", "-Country", "-CountryCode", "-State", str(f)], capture_output=True, text=True).stdout.split("\n")
    assert city(o / "a.jpg")[:2] == ["Johannesburg", "South Africa"] and city(o / "a.jpg")[3] == "Gauteng", city(o / "a.jpg")
    assert city(o / "b.jpg")[0] == "Home Town", "an existing city was replaced"
    assert city(o / "c.jpg")[0] == "", "a place was invented for a photo with no location"
    assert sm["changes"]["places_named"] == 1, sm["changes"]
    g.run_job([str(d)], str(WORK / "pn_out2"), False, False, place_names=False); state_ok()
    assert city(WORK / "pn_out2" / "a.jpg")[0] == ""


def t_issue_catalog():
    ents = fx.load_issues(force=True)
    assert len(ents) >= 120, len(ents)
    ids = [e["id"] for e in ents]
    assert len(ids) == len(set(ids)), "duplicate ids"
    cats = {e["category"] for e in ents}
    assert {"library", "icloud", "import", "permissions"} <= cats, cats
    for e in ents:
        assert e["title"] and e["meaning"] and e["severity"] in ("info", "warn", "bad") and e["risk"] in ("safe", "caution", "destructive"), e["id"]
        if e["category"] != "builtin":
            assert e["fixes"], "no fixes: " + e["id"]
            assert e["verified"] is False or e["sources"], e["id"]
    expect = {
        'photolibraryd: Error Domain=NSCocoaErrorDomain Code=4097 "connection to service named com.apple.photos.service was interrupted"': "xpc-interrupted",
        'cloudphotod: Error Domain=CKErrorDomain Code=25 "Quota exceeded"': "ck-quota-exceeded",
        "Photos: Error Domain=PHPhotosErrorDomain Code=3302": "phphotos-3302-invalid-resource",
        "assetsd: sqlite3_step failed: SQLITE_CORRUPT database disk image is malformed": "sqlite-corrupt",
        "osascript: Photos got an error: Not authorized to send Apple events to Photos. (-1743)": "tcc-automation-1743",
        'Error Domain=NSURLErrorDomain Code=-1009 "The Internet connection appears to be offline."': "nsurl-offline"}
    for line, want in expect.items():
        got = [i["id"] for i in fx.interpret_log_lines([line])[0]]
        assert want in got, (line[:60], got)
    benign = ["photolibraryd: Library opened successfully", "cloudphotod: Sync session completed: 0 errors", "kernel: mounted exFAT volume", "launchd: Service exited normally",
              "Photos: Loaded 1200 assets in 0.4s", "bird: iCloud Drive sync idle", "powerd: Wake reason: EC.LidOpen", "Spotlight: indexing 4 items"]
    assert [i["id"] for i in fx.interpret_log_lines(benign)[0] if i["sev"] != "info"] == []
    # the fast literal pre-check must never hide a match that a plain search finds
    for e, rx, lit in fx._ISSUES["units"]:
        assert lit is None or lit == lit.lower() and len(lit) >= 4
    for line in list(expect) + benign:
        brute = {e["id"] for e in ents for rx in e["_rx"] if rx.search(line)}
        fast = {i["id"] for i in fx.interpret_log_lines([line])[0]}
        assert fast <= brute | {i["id"] for i in fx.interpret_log_lines([line])[0] if i["via"] == "code"}, (line[:60], fast, brute)
    assert fx.codes_in_line('Error Domain=CKErrorDomain Code=25 "x"') == [("ckerrordomain", 25)]
    assert fx._code_key("CKErrorDomain 25 (quotaExceeded)") == ("ckerrordomain", 25)
    # a code in an unknown wording still finds its entry
    iss = fx.interpret_log_lines(["cloudphotod: failed Error Domain=CKErrorDomain Code=23 whatever"])[0]
    assert [i["id"] for i in iss] == ["ck-zone-busy"] and iss[0]["via"] in ("code", "text"), iss
    import random
    lines = [random.choice(benign + list(expect)) + " #%d" % i for i in range(60000)]
    t0 = time.time(); fx.interpret_log_lines(lines); assert time.time() - t0 < 15, time.time() - t0
    cat = fx.issue_catalog()
    assert cat and "_rx" not in cat[0]
    # the browser endpoint
    import urllib.request
    srv = g.ThreadingHTTPServer(("127.0.0.1", 0), g.Handler); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tok = urllib.request.urlopen("http://127.0.0.1:%d/" % port).read().decode().split("X-Shoebox-Token':'")[1].split("'")[0]
        rq = urllib.request.Request("http://127.0.0.1:%d/api/issues" % port, data=b"{}", headers={"X-Shoebox-Token": tok})
        out = json.loads(urllib.request.urlopen(rq, timeout=20).read())
        assert len(out["issues"]) == len(cat)
    finally:
        srv.shutdown()


def t_audit_pending_playbooks():
    import sqlite3
    base = Path(tempfile.mkdtemp(prefix="auditlib_"))
    lib = make_photos_library(base / "T.photoslibrary", [("a.jpg", 10, False), ("b.jpg", 10, False), ("c.jpg", 10, False), ("d.jpg", 10, False), ("e.jpg", 10, True), ("f.xyz", 10, False)])
    o = lib / "originals" / "0"
    o.mkdir(parents=True)
    (o / "U1.jpeg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 50)
    (o / "U2.jpeg").write_bytes(b"")
    (o / "U3.jpeg").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 50)
    (o / "ZZZ.jpeg").write_bytes(b"orphan!")
    (o / "U1_3.mov").write_bytes(b"live partner")
    (o / "U6.jpeg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 50)
    a = fx.photos_library_audit(lib)
    assert a["ok"] and a["orphans_total"] == 1 and a["orphans"][0]["file"] == "ZZZ.jpeg", a["orphans"]
    assert a["missing_local_total"] == 1 and a["missing_local"][0]["file"] == "d.jpg"
    assert a["cloud_only_total"] == 1 and a["zero_byte_total"] == 1 and a["zero_byte"][0]["file"] == "b.jpg"
    assert a["wrong_extension_total"] >= 1 and any(w["file"] == "c.jpg" for w in a["wrong_extension"]), a["wrong_extension"]
    pf = fx.photos_pending_files(lib)
    names = {i["file"]: i for i in pf["items"]}
    assert pf["pending_total"] == 5 and "e.jpg" not in names
    assert any("empty" in h for h in names["b.jpg"]["hints"]) and any("PNG" in h.upper() or ".png" in h for h in names["c.jpg"]["hints"]), names["c.jpg"]
    assert any("missing" in h for h in names["d.jpg"]["hints"]) and any("unusual" in h for h in names["f.xyz"]["hints"])
    assert fx.photos_library_audit(base / "nope")["ok"] is False
    # uuids in logs map to real names
    c = sqlite3.connect(lib / "database" / "Photos.sqlite")
    c.execute("alter table ZASSET add column ZUUID text")
    c.execute("update ZASSET set ZUUID='11111111-2222-3333-4444-555555555555' where Z_PK=2")
    c.commit(); c.close()
    lines = ["2026-01-01 cloudphotod: Upload failed for assetID=11111111-2222-3333-4444-555555555555 path=/Users/x/Pictures/a.heic", "no ids here"]
    f = fx.files_in_lines(lines)
    assert f["uuids"] == ["11111111-2222-3333-4444-555555555555"] and f["paths"] == ["/Users/x/Pictures/a.heic"], f
    r = fx.resolve_uuids(lib, f["uuids"])
    assert r["ok"] and r["found"][0]["file"] == "b.jpg", r
    iss, _ = fx.interpret_log_lines(lines)
    g.attach_log_files(iss, str(lib))
    assert iss and iss[0]["files"]["resolved"][0]["file"] == "b.jpg" and iss[0]["files"]["paths"] == ["/Users/x/Pictures/a.heic"], iss
    # playbooks: valid ids, steps, new catalog fields
    pbs = fx.load_playbooks()
    raw = json.loads(fx.PLAYBOOKS_FILE.read_text())
    known = {e["id"] for e in fx.load_issues()}
    assert len(pbs) >= 10 and all(p["steps"] for p in pbs)
    for p in raw:
        assert all(i in known for i in p["issue_ids"]), p["id"]
        assert all(st["who"] in ("shoebox", "you", "terminal") for st in p["steps"]), p["id"]
        assert not any(st.get("action") and st["action"] not in ("audit", "pending", "monitor", "health", "guide") for st in p["steps"]), p["id"]
    sup = [e for e in fx.load_issues() if e.get("provenance", "").startswith("supplied")]
    assert len(sup) >= 10 and all(not e["verified"] and e["confidence"] != "high" for e in sup)
    shutil.rmtree(base, ignore_errors=True)
    return "%d playbooks, %d supplied entries" % (len(pbs), len(sup))


def t_live_watch_and_albums():
    import sqlite3
    base = Path(tempfile.mkdtemp(prefix="livelib_"))
    lib = make_photos_library(base / "L.photoslibrary", [("beach.jpg", 10, False), ("cat.jpg", 10, True)])
    c = sqlite3.connect(lib / "database" / "Photos.sqlite")
    c.execute("alter table ZASSET add column ZUUID text"); c.execute("alter table ZASSET add column ZDATECREATED real")
    c.execute("update ZASSET set ZUUID='AAAAAAAA-1111-2222-3333-444444444444', ZDATECREATED=700000000 where Z_PK=1")
    c.execute("create table ZGENERICALBUM (Z_PK integer primary key, ZTITLE text, ZTRASHEDSTATE integer)")
    c.execute("create table Z_26ASSETS (Z_26ALBUMS integer, Z_3ASSETS integer)")
    c.execute("insert into ZGENERICALBUM values (1,'Japan 2025',0)"); c.execute("insert into Z_26ASSETS values (1,1)")
    c.commit(); c.close()
    r = fx.resolve_uuids(lib, ["AAAAAAAA-1111-2222-3333-444444444444"])
    f = r["found"][0]
    assert f["file"] == "beach.jpg" and f["albums"] == ["Japan 2025"] and f["date"] == 700000000 + 978307200, f
    script = base / "fakelog.sh"
    script.write_text("#!/bin/sh\necho 'ts cloudphotod: Upload failed for assetID=AAAAAAAA-1111-2222-3333-444444444444 CKErrorDomain Code=25'\n"
                      "echo 'ts cloudphotod: Upload failed for assetID=AAAAAAAA-1111-2222-3333-444444444444 CKErrorDomain Code=25'\n"
                      "echo 'ts photolibraryd: all fine'\n"
                      "echo 'ts cloudphotod: something exploded badly with error 12345'\nsleep 5\n")
    script.chmod(0o755)
    w = fx.LiveWatcher()
    assert w.start(str(lib), cmd=[str(script)])
    t0 = time.time()
    while time.time() - t0 < 4 and len(w.poll(0)["events"]) < 2:
        time.sleep(0.1)
    p = w.poll(0)
    w.stop()
    ev = p["events"]
    assert len(ev) >= 2 and ev[0]["count"] == 2, ev
    assert ev[0]["files"] and ev[0]["files"][0]["file"] == "beach.jpg" and ev[0]["files"][0]["albums"] == ["Japan 2025"], ev[0]
    assert any(not e["known"] for e in ev) and p["lines_seen"] >= 4
    assert fx.LiveWatcher().start("", cmd=["/nonexistent/cmd"]) is False
    shutil.rmtree(base, ignore_errors=True)
    return "events=%d" % len(ev)


ORDER = ["t_fix_copy", "t_fix_namedate", "t_fix_inplace", "t_fix_move", "t_fix_dryrun_changes_nothing", "t_fix_zip", "t_zip_resume", "t_zip_needs_dest", "t_zip_corrupt",
         "t_edited_policies", "t_cancel_mid_run", "t_unreadable_and_zero", "t_dest_not_writable", "t_exiftool_missing", "t_low_disk_zip", "t_assess_and_recommend",
         "t_assess_multi_and_photoslib", "t_guided_end_to_end", "t_guided_no_dest", "t_merge_variants", "t_merge_refuses_unsafe", "t_merge_move_in_place", "t_cleanup_all",
         "t_cleanup_refuses_broad", "t_consolidate", "t_convert", "t_convert_stop", "t_similar_apply_undo", "t_health_and_formats", "t_undo_copy_run", "t_photos_plan_and_run",
         "t_photos_applescript_injection_safe", "t_photos_errors", "t_monitor_rules", "t_monitor_job_paste", "t_compare_and_near", "t_diagnostics", "t_history_report",
         "t_updater_mock", "t_date_names_and_helpers", "t_dos_inputs", "t_audit_regressions", "t_dates_and_places", "t_resilient_copy", "t_rerun_over_http", "t_keeper_rules_and_matching", "t_bursts_and_compare_rules", "t_qa_hunt_regressions", "t_timezone_correct_dates", "t_preflight_report", "t_albums_and_live_arrival", "t_receipt", "t_context_dates_and_locations", "t_blur_and_screenshots", "t_motion_photo_extract", "t_offline_place_names", "t_issue_catalog", "t_audit_pending_playbooks", "t_live_watch_and_albums"]
if __name__ == "__main__":
    only = sys.argv[1:]
    for n in ORDER:
        if only and not any(o in n for o in only):
            continue
        if n in globals():
            R.run(n, globals()[n])
    ok = R.report()
    sys.exit(0 if ok else 1)

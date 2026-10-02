"""Shared fixtures for the Backstory end-to-end tests. Everything is created under a scratch folder; nothing touches real data."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def sha(p):
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def tree_hash(root):
    """Hash of every file (relative path + content) below root, to prove originals were not touched."""
    h = hashlib.sha1()
    for dp, dns, fns in sorted(os.walk(root)):
        dns.sort()
        for n in sorted(fns):
            p = os.path.join(dp, n)
            h.update(os.path.relpath(p, root).encode())
            h.update(sha(p).encode())
    return h.hexdigest()


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def jpeg(path, size=(160, 120), seed=0, q=3):
    """A distinct test picture (different seed = different picture)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    src = ["gradients=size=%dx%d:seed=%d" % (size[0], size[1], seed + 1)] if seed % 2 == 0 else ["mandelbrot=size=%dx%d" % size]
    if seed % 3 == 1:
        src = ["testsrc2=size=%dx%d:rate=1" % size]
    ffmpeg("-f", "lavfi", "-i", src[0], "-frames:v", "1", "-q:v", str(q), str(path))
    if seed % 5 == 4:   # make solid variations distinct even when a generator ignores the seed
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-Comment=seed%d" % seed, str(path)], check=False)


def video(path, seconds=2, size="128x96", codec=None):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", "testsrc=size=%s:d=%s" % (size, seconds), "-f", "lavfi", "-i", "sine=frequency=440:duration=%s" % seconds, "-pix_fmt", "yuv420p"]
    if codec:
        args += ["-c:v", codec]
    ffmpeg(*args, "-shortest", str(path))


def sidecar(path, ts=1341100000, lat=None, lon=None, desc=None, name=None):
    d = {"title": Path(path).name, "photoTakenTime": {"timestamp": str(ts)}, "creationTime": {"timestamp": str(ts + 86400 * 400)}}
    if lat is not None:
        d["geoData"] = {"latitude": lat, "longitude": lon, "altitude": 0.0}
    if desc:
        d["description"] = desc
    Path(str(path) + ".json").write_text(json.dumps(d), encoding="utf-8")


def make_takeout_folder(base, n=6):
    """A small unzipped Takeout: year folders, an album with duplicates, an extension-less file, an edited copy, live pair, no-json file."""
    base = Path(base)
    P = base / "Takeout" / "Google Photos"
    for i in range(n):
        f = P / "Photos from 2012" / ("IMG_%d.jpg" % i)
        jpeg(f, seed=i)
        sidecar(f, ts=1341100000 + i * 3600, lat=48.85 + i / 100, lon=2.35, desc="photo %d" % i)
    shutil.copy(P / "Photos from 2012" / "IMG_0.jpg", P / "Trip" / "IMG_0.jpg") if (P / "Trip").mkdir(parents=True, exist_ok=True) is None else None
    sidecar(P / "Trip" / "IMG_0.jpg", ts=1341100000)
    jpeg(P / "Photos from 2012" / "IMG_5-edited.jpg", seed=11)
    sidecar(P / "Photos from 2012" / "IMG_5-edited.jpg", ts=1341200000)
    shutil.copy(P / "Photos from 2012" / "IMG_1.jpg", P / "Photos from 2012" / "NOEXT")
    (P / "Photos from 2012" / "NOEXT.json").write_text(json.dumps({"photoTakenTime": {"timestamp": "1341300000"}}))
    jpeg(P / "Photos from 2014" / "IMG_20140704_101010.jpg", seed=12)       # no json, date in the name
    jpeg(P / "Photos from 2014" / "live.heic", seed=13)
    video(P / "Photos from 2014" / "live.mov", 1)
    sidecar(P / "Photos from 2014" / "live.heic", ts=1404000000)
    return base


def make_takeout_zips(base, n=2):
    """Two zips with cross-zip json, an album duplicate, an extension-less file and a hostile entry."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / "_t"
    P = "Takeout/Google Photos/"
    for i in range(6):
        jpeg(tmp / ("p%d.jpg" % i), seed=20 + i)
    js = lambda ts, **k: json.dumps({"photoTakenTime": {"timestamp": str(ts)}, "geoData": {"latitude": k.get("lat", 1.5), "longitude": 2.5, "altitude": 0}})
    with zipfile.ZipFile(base / "takeout-001.zip", "w") as z:
        z.write(tmp / "p0.jpg", P + "Photos from 2012/IMG_1.jpg"); z.writestr(P + "Photos from 2012/IMG_1.jpg.json", js(1341100000))
        z.write(tmp / "p1.jpg", P + "Photos from 2012/IMG_2.jpg")                       # json lives in zip 2
        z.write(tmp / "p0.jpg", P + "Album X/IMG_1.jpg"); z.writestr(P + "Album X/IMG_1.jpg.json", js(1341100000))
        z.write(tmp / "p2.jpg", P + "Photos from 2012/NOEXT"); z.writestr(P + "Photos from 2012/NOEXT.json", js(1341300000))
        z.writestr("../evil.jpg", b"x"); z.writestr("/abs/evil.jpg", b"x")
    with zipfile.ZipFile(base / "takeout-002.zip", "w") as z:
        z.write(tmp / "p3.jpg", P + "Photos from 2013/IMG_9.jpg"); z.writestr(P + "Photos from 2013/IMG_9.jpg.json", js(1370000000))
        z.writestr(P + "Photos from 2012/IMG_2.jpg.json", js(1341200000))
        z.write(tmp / "p0.jpg", P + "Album Y/IMG_1.jpg"); z.writestr(P + "Album Y/IMG_1.jpg.json", js(1341100000))
    shutil.rmtree(tmp)
    return base


def make_photos_library(path, files):
    """A mock Photos library database with the columns Backstory reads. files = [(original name, size, uploaded)]."""
    lib = Path(path)
    (lib / "database").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(lib / "database" / "Photos.sqlite")
    db.execute("create table ZASSET (Z_PK integer primary key, ZTRASHEDSTATE integer, ZCLOUDLOCALSTATE integer, ZCLOUDASSETGUID text, ZADDITIONALATTRIBUTES integer, ZDIRECTORY text, ZFILENAME text)")
    db.execute("create table ZADDITIONALASSETATTRIBUTES (Z_PK integer primary key, ZORIGINALFILENAME text, ZORIGINALFILESIZE integer)")
    for i, (n, sz, up) in enumerate(files, 1):
        db.execute("insert into ZADDITIONALASSETATTRIBUTES values (?,?,?)", (i, n, sz))
        db.execute("insert into ZASSET values (?,?,?,?,?,?,?)", (i, 0, 1 if up else 0, "g%d" % i if up else None, i, "0", "U%d.jpeg" % i))
    db.commit()
    db.close()
    return lib


class Results:
    def __init__(self):
        self.rows = []

    def run(self, name, fn):
        t0 = time.time()
        try:
            detail = fn() or ""
            self.rows.append((name, True, str(detail)[:200], time.time() - t0))
        except Exception as e:                     # noqa
            import traceback
            self.rows.append((name, False, ("%s: %s" % (type(e).__name__, e))[:300] + " | " + traceback.format_exc().splitlines()[-3].strip()[:160], time.time() - t0))

    def report(self):
        w = max(len(r[0]) for r in self.rows)
        for n, ok, d, s in self.rows:
            print("%s  %-*s  %5.1fs  %s" % ("PASS" if ok else "FAIL", w, n, s, d))
        bad = [r for r in self.rows if not r[1]]
        print("\n%d tests, %d passed, %d failed" % (len(self.rows), len(self.rows) - len(bad), len(bad)))
        return not bad

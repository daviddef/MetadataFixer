#!/usr/bin/env python3
"""Local web UI for takeout_fix_metadata.py.

    python3 takeout_gui.py

Opens http://127.0.0.1:8765 in your browser. Nothing leaves your machine; the
server only listens on localhost. Needs exiftool (brew install exiftool).
"""
import argparse
import csv
import json
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

STATE = {"state": "idle", "total": 0, "done": 0, "counts": {}, "message": "", "report": "", "scan": None}
LOCK = threading.Lock()


def choose_folder(prompt):
    """Native macOS folder picker; returns a POSIX path or ''."""
    script = f'POSIX path of (choose folder with prompt "{prompt}")'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    return r.stdout.strip().rstrip("/") if r.returncode == 0 else ""


def run_job(root, out, dry_run, overwrite):
    with LOCK:
        STATE.update(state="scanning", total=0, done=0, counts={}, message="Scanning folders...", report="")
    try:
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"Not a folder: {root}")
        if not dry_run and not shutil.which("exiftool"):
            raise ValueError("exiftool not found. In Terminal run: brew install exiftool")
        media, sidecars = fx.scan(root)
        idx = fx.build_index(sidecars)
        with LOCK:
            STATE.update(state="running", total=len(media),
                         message=f"{len(media)} media files, {len(sidecars)} json files")
        args = argparse.Namespace(dry_run=dry_run, overwrite=overwrite)
        out_root = Path(out) if out else None
        rows, counts = [], defaultdict(int)
        with ThreadPoolExecutor(max_workers=4) as ex:
            for row in ex.map(lambda m: fx.process(m, idx, args, out_root), media):
                rows.append(row)
                counts[row["status"]] += 1
                with LOCK:
                    STATE["done"] = len(rows)
                    STATE["counts"] = dict(counts)
        report_dir = out_root or Path.home() / "Desktop"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / ("takeout_dryrun.csv" if dry_run else "takeout_report.csv")
        with open(report, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["file", "sidecar", "match", "status", "detail"])
            w.writeheader()
            w.writerows(rows)
        with LOCK:
            STATE.update(state="done", report=str(report), message="Finished")
    except Exception as e:  # surface any failure in the UI
        with LOCK:
            STATE.update(state="error", message=str(e))


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
            self._send(200, json.dumps({"path": choose_folder(body.get("prompt", "Choose a folder"))}))
        elif self.path == "/api/start":
            with LOCK:
                busy = STATE["state"] in ("scanning", "running")
            if busy:
                return self._send(409, json.dumps({"error": "A job is already running"}))
            threading.Thread(target=run_job, daemon=True, args=(
                body.get("root", ""), body.get("out", ""),
                bool(body.get("dry_run")), bool(body.get("overwrite")))).start()
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
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#dcdcd6;--acc:#2563eb;--ok:#15803d;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1e;--ink:#eeeeea;--mute:#9a9a94;--line:#34342f;--acc:#60a5fa;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif;padding:24px 16px}
main{max-width:640px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--mute);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
label.t{font-weight:600;display:block;margin-bottom:6px}small{color:var(--mute);display:block;margin-top:6px}
.drop{display:flex;gap:8px}input[type=text]{flex:1;min-width:0;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
button{padding:9px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit;cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}button:disabled{opacity:.5;cursor:default}
.opt{display:flex;gap:8px;align-items:flex-start;margin:8px 0}
.bar{height:10px;background:var(--line);border-radius:6px;overflow:hidden;margin:10px 0}.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .2s}
.counts{display:flex;flex-wrap:wrap;gap:6px 14px;font-variant-numeric:tabular-nums}.err{color:var(--bad)}.ok{color:var(--ok)}
</style></head><body><main>
<h1>Takeout Metadata Fixer</h1>
<p class="sub">Restores date, location, description and people from Google's .json files. Runs locally.</p>
<div class="card"><label class="t">1. Folder containing your extracted Takeout folders</label>
<div class="drop"><input type="text" id="root" placeholder="/Volumes/Drive/Takeouts"><button id="b1">Choose folder</button></div>
<small>Pick the parent that holds Takeout 1, Takeout 2, ... (zips must already be extracted).</small></div>
<div class="card"><label class="t">2. Where to put the fixed copies</label>
<div class="drop"><input type="text" id="out" placeholder="Leave empty to fix files in place"><button id="b2">Choose folder</button></div>
<small>Recommended: choose a new folder. Originals stay untouched. Empty = edit in place.</small></div>
<div class="card"><label class="t">3. Options</label>
<div class="opt"><input type="checkbox" id="dry" checked><div>Preview only (dry run)<small>Matches files and writes a report, changes nothing. Run this first.</small></div></div>
<div class="opt"><input type="checkbox" id="ow"><div>Overwrite existing EXIF values<small>Off = only fill in missing tags.</small></div></div></div>
<button class="p" id="go">Start</button>
<div class="card" id="prog" style="display:none;margin-top:14px">
<div id="msg"></div><div class="bar"><i id="fill"></i></div><div class="counts" id="counts"></div>
<div style="margin-top:10px"><button id="rev" style="display:none">Show report in Finder</button></div></div>
<script>
const $=id=>document.getElementById(id);
async function post(u,b){const r=await fetch(u,{method:'POST',body:JSON.stringify(b||{})});return r.json()}
$('b1').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose the folder containing your Takeout folders'});if(r.path)$('root').value=r.path};
$('b2').onclick=async()=>{const r=await post('/api/choose',{prompt:'Choose where to save the fixed files'});if(r.path)$('out').value=r.path};
$('go').onclick=async()=>{
  if(!$('root').value.trim()){alert('Choose the Takeout folder first');return}
  if(!$('dry').checked&&!$('out').value.trim()&&!confirm('No output folder: files will be edited IN PLACE. Continue?'))return;
  const r=await post('/api/start',{root:$('root').value.trim(),out:$('out').value.trim(),dry_run:$('dry').checked,overwrite:$('ow').checked});
  if(r.error)alert(r.error);else poll();
};
$('rev').onclick=()=>post('/api/reveal');
let timer;function poll(){clearInterval(timer);timer=setInterval(async()=>{
  const s=await (await fetch('/api/status')).json();$('prog').style.display='block';
  $('go').disabled=s.state==='scanning'||s.state==='running';
  const pct=s.total?Math.round(100*s.done/s.total):0;$('fill').style.width=pct+'%';
  $('msg').innerHTML=(s.state==='error'?'<span class="err">'+s.message+'</span>':s.state==='done'?'<span class="ok">Finished.</span> Report: '+s.report:s.message+(s.total?` (${s.done}/${s.total})`:''));
  $('counts').innerHTML=Object.entries(s.counts).map(([k,v])=>`<span>${k}: <b>${v}</b></span>`).join('');
  $('rev').style.display=s.report?'inline-block':'none';
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

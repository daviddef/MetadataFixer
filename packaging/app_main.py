"""Launcher for the packaged Mac app: starts the local server and shows it in a native window."""
import os
import socket
import stat
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path


def bundle_dirs():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    exe = Path(sys.executable).resolve().parent
    return [base, exe.parent / "Resources", exe]


def find(rel):
    for d in bundle_dirs():
        p = d / rel
        if p.exists():
            return p
    return None


def setup_tools():
    """Make the bundled ffmpeg/ffprobe/exiftool visible to the engine (it calls them by name)."""
    home = Path.home() / "Library" / "Application Support" / "MetadataFixer" / "bin"
    home.mkdir(parents=True, exist_ok=True)
    dirs = []
    binroot = find("bin")
    if binroot:
        dirs.append(str(binroot))
    et = find("exiftool/exiftool")
    if et:
        wrapper = home / "exiftool"
        wrapper.write_text('#!/bin/sh\nexec /usr/bin/perl "%s" "$@"\n' % et)
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        dirs.append(str(home))
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", ""), "/usr/local/bin", "/opt/homebrew/bin"])


def free_port(preferred=8765):
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port")


def main():
    setup_tools()
    import takeout_gui as g
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), g.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=g.check_update, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        import webview
        webview.create_window("Metadata Fixer", url, width=1100, height=900, min_size=(700, 600))
        webview.start()                     # returns when the window is closed
    except Exception:
        webbrowser.open(url)                # no native window available: use the browser; keep running
        while True:
            time.sleep(3600)
    os._exit(0)


if __name__ == "__main__":
    main()

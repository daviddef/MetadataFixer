# Third-party notices

Metadata Fixer is released under the MIT License (see `LICENSE`). It uses, and the packaged Mac app includes, the following third-party software. Each remains under its own license and copyright; Metadata Fixer is not affiliated with their authors.

| Component | Used for | License | Source |
|---|---|---|---|
| ExifTool by Phil Harvey | Reading and writing photo and video metadata | Perl Artistic License or GNU GPL (your choice) | https://exiftool.org and https://github.com/exiftool/exiftool |
| FFmpeg (ffmpeg and ffprobe) | Reading and converting videos | LGPL 2.1+ or GPL 2+ depending on the build | https://ffmpeg.org (source code available there) |
| Python | The programming language runtime | Python Software Foundation License | https://www.python.org |
| PyInstaller | Packaging the Mac app | GPL 2+ with a special exception that allows bundling | https://pyinstaller.org |
| pywebview | The Mac app window | BSD 3-Clause | https://pywebview.flowrl.com |

The FFmpeg binaries bundled in the Mac app are third-party builds. You can obtain the corresponding FFmpeg source code and licence text from https://ffmpeg.org and from the build provider named in the release notes. If you want a different FFmpeg, install your own and the app will use the one on your PATH when the bundled one is absent.

The names Google, Google Photos, Google Takeout, Apple, iPhone, Live Photos and macOS are trademarks of their owners. Metadata Fixer is an independent project and is not affiliated with, endorsed by or sponsored by them.

# Shoebox

Gets every photo out of the shoebox and back where it belongs. A free, local, private app for taking control of a photo and video library, starting with Google Photos Takeout: it puts the real dates, locations, captions and people back, merges and de-duplicates libraries, tidies the mess, checks the library's health and sends the result to Apple Photos. Nothing is uploaded; everything runs on your own computer.

## What it does
- **Fix metadata** from Google Takeout `.json` files (dates, locations, captions, people, favourites), across zip files and batches, with Live Photo re-pairing, edited-copy handling and dates from file names.
- **Time zones, dates and places:** dates written as local time with a UTC offset; missing dates rebuilt from every clue (file name, neighbouring photo numbers, folder, file time) with a confidence; locations filled from nearby photos or a GPX track; offline place names (city, region, country) written from any location; a pre-flight check before anything is touched; a shareable migration receipt after.
- **Dates and places from folder names:** fill a missing date from a folder called `2017` or `2026-06`; optionally correct dates that disagree with the folder or lie in the future; optionally guess an approximate location from a folder like `Johannesburg` (labelled as a guess, never over an existing location); flag locations that look mismatched.
- **Merge and de-duplicate** libraries: exact duplicates, near-identical pictures with matching rules and ordered keeper rules (favourite, edited, resolution, size, metadata, album...), bursts kept by default, missing metadata carried onto the kept copy. **Compare libraries** shows how alike two libraries are and which copy a merge would keep.
- **Clean up, convert and check:** junk and odd files, look-alike folders, old video formats to MP4, the same file in several formats, and a library **Health** score with full **Diagnostics**.
- **Apple Photos:** send a library in batches, oldest first, with upload verification and stall detection; a Monitor that reads Photos and iCloud logs and explains problems, backed by a built-in catalog of about 140 known Photos, iCloud and macOS problems and error codes (a draft, with confidence levels).
- **Safe by design:** preview first, copy by default, Stop, undo, reports and logs, and copying that waits, retries and resumes when a drive misbehaves ("Continue where I left off").
- **Styles:** Safest, Balanced, Fastest, Thorough or "I like risk" set every option in the app to suit your patience, risk appetite and goals.

## Requirements
- Python 3.8+ and [exiftool](https://exiftool.org) (macOS: `brew install exiftool`); [ffmpeg](https://ffmpeg.org) for video conversion and picture comparison (`brew install ffmpeg`). The packaged Mac app includes both.

## Easiest: the local app
```
python3 takeout_gui.py
```
Opens a page in your browser, served only on your own machine (it also checks a private per-launch token, so other websites cannot drive it). Both `.py` files must sit in the same folder. Start on the **Guided** tab: add your Takeout zips or folders, choose a Destination, press **Check my files**, then **Preview the recommended plan**.

## Command line
The command line covers the core metadata fix on folders (the app also reads zip files directly and has all the newer features above). Extract your Takeout zips into one parent folder, then:

```
python3 takeout_fix_metadata.py ~/Takeouts --dry-run       # preview, writes only the report
python3 takeout_fix_metadata.py ~/Takeouts --out ~/Photos  # copy to a new library, fixed (safest)
python3 takeout_fix_metadata.py ~/Takeouts                 # fix in place (work on a copy)
```

Existing EXIF values are kept and only missing tags are filled in; add `--overwrite` to replace them. Every file's outcome is written to `takeout_report.csv`.

## Notes
- Handles `.supplemental-metadata.json` (including truncated), 46-character name truncation, `(1)` duplicates, `-edited` copies and live-photo videos.
- Google stores times in UTC. The app writes local time plus a UTC offset (option *Correct the time zone*, on by default); the command line still writes them as-is.
- Formats exiftool can't write (avi, mkv, wmv, mpg, mts, bmp) only get their modified time fixed.
- Re-pairing Live Photos (`--pair-live` with `--out`, or the app's option) copies each still's Apple ContentIdentifier onto its video and saves it as `.MOV`. Stills that lost their Apple ID are reported as `no-id` / `no-still`.

## Testing
`python3 tests/run_e2e.py` runs the end-to-end suite on mock Takeouts, mock Photos libraries and a fake `osascript` (needs exiftool and ffmpeg). `node tests/ui_sweep.js PORT` and `node tests/ui_profiles.js PORT`, `node tests/ui_route.js PORT` drive a running copy of the app in a headless browser. See [tests/README.md](tests/README.md).

Full instructions: [USER_GUIDE.md](USER_GUIDE.md) (also built into the app's **Help** tab). Plans: [ROADMAP.md](ROADMAP.md), [BACKLOG.md](BACKLOG.md).

## Support
Email **thestocksoup@gmail.com** (best-effort, free project) or open an issue on GitHub. In the app, **History > Copy diagnostic info** gives you text to include.

## Important
**Back up your photos first.** This software changes files, and some options delete or move them. It is provided "as is", without warranty, and you use it at your own risk; to the maximum extent permitted by law the author is not liable for any loss or damage. Always run a preview first. See the **Safety, limitations and disclaimer** section of the [user guide](USER_GUIDE.md#safety-limitations-and-disclaimer). Not affiliated with Google or Apple.

## License
MIT, see [LICENSE](LICENSE). Third-party components: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

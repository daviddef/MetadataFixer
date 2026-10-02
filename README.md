# MetadataFixer

Puts Google Photos Takeout `.json` metadata back into your photos and videos.

Takeout exports repeat the same album folders across many `Takeout N` folders, and a photo's `.json` is not always next to the photo. This script indexes every sidecar in the whole tree, matches each file to its sidecar, and writes date taken, GPS, description, people and favourite rating with exiftool. RAW files get an `.xmp` sidecar instead of being edited. File modified times are set to the date taken.

## Requirements
- Python 3.8+
- [exiftool](https://exiftool.org) (macOS: `brew install exiftool`)

## Easiest: the local app
```
python3 takeout_gui.py
```
Opens a page in your browser (served only on your own machine). Click **Choose folder** to pick the Takeout parent folder and, ideally, a separate output folder, leave **Preview only** ticked for a first pass, then press Start. Progress and the CSV report are shown in the page. Both .py files must sit in the same folder.

## Command line
The command line works on folders (the app also reads zip files directly). Extract your Takeout zips into one parent folder, then:

```
python3 takeout_fix_metadata.py ~/Takeouts --dry-run       # preview, writes only the report
python3 takeout_fix_metadata.py ~/Takeouts --out ~/Photos  # copy to a new library, fixed (safest)
python3 takeout_fix_metadata.py ~/Takeouts                 # fix in place (work on a copy)
```

Existing EXIF values are kept and only missing tags are filled in; add `--overwrite` to replace them. Every file's outcome is written to `takeout_report.csv`.

## Notes
- Handles `.supplemental-metadata.json` (including truncated), 46-character name truncation, `(1)` duplicates, `-edited` copies and live-photo videos.
- Google stores times in UTC and they are written as-is.
- Formats exiftool can't write (avi, mkv, wmv, mpg, mts, bmp) only get their modified time fixed.

## Live Photos
Tick **Re-pair Live Photos** in the app (or pass `--pair-live` with `--out`) to copy each still's Apple ContentIdentifier onto its video and save the video as `.MOV`, so Apple Photos can import them as one Live Photo. Works only where the still still has its Apple ID; others are reported as `no-id` / `no-still`.

Full instructions: [USER_GUIDE.md](USER_GUIDE.md) (also built into the app's **Help** tab)

## Support
Email **thestocksoup@gmail.com** (best-effort, free project) or open an issue on GitHub. In the app, **History > Copy diagnostic info** gives you text to include.

## Important
**Back up your photos first.** This software changes files, and some options delete or move them. It is provided "as is", without warranty, and you use it at your own risk; to the maximum extent permitted by law the author is not liable for any loss or damage. Always run a preview first. See the **Safety, limitations and disclaimer** section of the [user guide](USER_GUIDE.md#safety-limitations-and-disclaimer). Not affiliated with Google or Apple.

## License
MIT, see [LICENSE](LICENSE). Third-party components: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

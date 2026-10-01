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
Extract all Takeout zips into one parent folder, then:

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

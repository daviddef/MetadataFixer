# MetadataFixer user guide

Restores the real date, location, description and people to photos and videos exported from Google Photos with Google Takeout.

## Why you need this

A Takeout export gives you photos and videos plus a `.json` file for each one. The date, GPS location, description and tagged people live **only in the `.json`**, not in the photo, and the `.json` can be in a different folder or a different `Takeout N` batch from its photo. This tool finds the right `.json` for every file and writes the information back.

## What you need

- A Mac (the app's folder pickers use macOS; the command line also works on Linux/Windows)
- Python 3.8+ (`python3 --version`)
- exiftool: `brew install exiftool`
- All your Takeout zips **extracted** into folders (the tool reads folders, not zips)
- Free disk space roughly equal to your photo library if you use an output folder (recommended)

## Install

```
git clone https://github.com/daviddef/MetadataFixer ~/MetadataFixer
```

Or download `takeout_fix_metadata.py` and `takeout_gui.py` from the repo into one folder. The two files must be from the same version.

## Using the app (recommended)

```
cd ~/MetadataFixer
python3 takeout_gui.py
```

A page opens at `http://127.0.0.1:8765`. It runs only on your computer. Leave Terminal open while you use it; press Ctrl+C there to quit.

### 1. Add your Takeout folders
Click **Add folders...** and Cmd-click to select several, or type paths one per line. Add **every** Takeout batch. A photo in one batch can find its `.json` in another, so the more batches you include, the fewer files are left unmatched. Dropping folders from Finder onto the box works only if your browser passes the path; otherwise use the button.

### 2. Choose an output folder
Pick a new empty folder. Fixed copies and reports are saved there, and your Takeout originals are not touched. If you leave it empty, files are edited in place (you are asked to confirm). Live Photo repair needs an output folder.

### 3. Pick options
| Option | Default | What it does |
|---|---|---|
| Preview only (dry run) | **on** | Matches files and reports counts. Changes nothing. |
| Remove exact duplicates | **on** | Skips byte-identical copies of the same photo (repeated across Takeouts or albums); keeps the one in `Photos from YYYY`. |
| Output layout | merge folders | *Merge folders*: all `Photos from 2012` folders become one. *Year / Month*: `2012/2012-07` by date taken; files with no date go to `Unknown date`. |
| Move instead of copy | off | Moves files into the output folder instead of copying. Saves disk space but empties your Takeout folders as it goes. |
| Re-pair Live Photos | off | Re-links iPhone Live Photo videos to their still (see below). |
| Overwrite existing EXIF values | off | Off: only fill in missing values. On: replace existing values with Google's. |

### 4. Run a preview first
Leave Preview on and press **Start**. Read the summary (below). If **No JSON found** is high, add more Takeout folders and preview again.

### 5. Run it for real
Untick **Preview only**. Because Google's exports often carry wrong dates, tick **Overwrite existing EXIF values** too. Press **Start**. Large libraries take a while; the progress bar and live counts show how far along it is.

### 6. Check the result
Use **Show reports in Finder**, and spot-check a few files:
```
exiftool -DateTimeOriginal -GPSPosition -ImageDescription "/path/to/output/Photos from 2012/DSC_2865.JPG"
```
Keep your Takeout folders until you are happy with the output.

## Restructuring: copy vs move

With an output folder the tool **builds a new, merged library there and leaves your Takeout folders untouched** (a second copy, so you need roughly as much free space again). Tick **Move** to relocate files instead; it uses no extra space but empties the Takeout folders, so have another backup first.

Every file is placed, including those with no JSON (they keep their existing data). If two different files share a name, the second becomes `name_1`. Exact duplicates are detected by content, not name. Because duplicate album copies are skipped, **album membership is not preserved**; the photo stays in its `Photos from YYYY` folder. Use a fresh output folder each run: re-running into the same one creates `_1` copies.

While it runs you see live counts (duplicates skipped, files placed, output folders) and the last dozen files with where each went. The summary ends with a table of the output folders.

## Reading the summary

Live counters while running, then a full summary:

- **Media files / matched / no JSON** with the percent matched
- **JSON with no photo**: sidecars that matched no file
- **Files with a value replaced** and **Live Photos paired**
- **EXIF values** table, per field (date taken, location, description):
  - *Added*: the file had no value; Google's was written
  - *Replaced*: the file had a different value; it was overwritten (overwrite on)
  - *Kept*: the file had a different value and was left alone (overwrite off). In a preview this shows what overwriting *would* change
  - *Already correct*: the value matched Google's
- **How files were matched** (see next section)
- Breakdowns **by file type**, **by Takeout batch** and the **albums with most no-JSON files**
- Plain-language tips (for example, "their JSON is probably in other batches")

The app also shows a **Sample of changes** table (first 15 files) with each value as `before -> Google's value`.

Saved in the output folder (or on your Desktop if none); preview runs use `takeout_dryrun*` names:
- `takeout_report.csv`: every file. Per field it shows the outcome plus the exact values: `date` / `date_before` / `date_google`, `gps` / `gps_before` / `gps_google` (latitude, longitude), `desc` / `desc_before` / `desc_google`, plus `people`, `favourite`, `live` (Live Photo result), `match` and `status`
- `takeout_report_changes.csv`: only files where something was added, replaced or left alone
- `takeout_report_no_json.csv`: only unmatched files (these are still placed in the output folder)
- the `output` column of the main report says where each file went
- `takeout_report_summary.txt`: the totals

`*_google` is the value written when the outcome is *added* or *replaced*. `*_before` is what the file held (blank if it had nothing). Dates are shown as UTC.

## How files are matched to their JSON

The tool indexes every `.json` under all folders you added, then matches each photo by name:

1. **Same album folder** (for example `Photos from 2012`) in any batch
2. **Another folder or batch**: the JSON can be in a completely different subfolder. If several sidecars share the same filename (iPhones restart numbering, so `IMG_0647.HEIC` can repeat across years), it picks the one whose date is closest to the file's own date (`Another folder, several candidates`)
3. **Same name, other extension, same folder**: a RAW with its JPG's JSON, or a Live Photo video with its still's JSON

It copes with Google's naming quirks: `.supplemental-metadata.json` (including truncated forms), names cut at 46 characters, `(1)` duplicates and `-edited` copies.

**Limit:** the same-name fallback in step 3 only works inside one folder. A Live Photo video with no JSON whose still lives in a different folder is reported as no-JSON.

## What gets written

- **Date taken** (and the file's modified time)
- **GPS location** (latitude, longitude, altitude); empty 0,0 locations are skipped
- **Description**
- **People** tagged in Google Photos
- **Favourite** as a 5-star rating

Not written: album membership, camera/device details, upload time, view counts, URLs, trash/archive flags, comments.

Special cases:
- **RAW files** (NEF, CR2, ARW, DNG...) are not edited; a `.xmp` sidecar is created next to them.
- **AVI, MKV, WMV, MPG, MTS, BMP** can't be written by exiftool; only their modified time is fixed.
- **Files with the wrong extension** (for example a JPEG saved as `.HEIC`) are handled automatically.
- Google stores times in UTC and they are written as-is, so some apps may show a timezone shift.

## Live Photos

Takeout splits a Live Photo into a still (`IMG_1234.HEIC`) and a video (`IMG_1234.MP4`) and strips the Apple ID that links them. With **Re-pair Live Photos** on, the tool copies the still's Apple ContentIdentifier onto the video and saves it as `.MOV` so Apple Photos can import the two as one Live Photo.

- Needs an output folder (it renames videos).
- Works only where the still still has its Apple ID. Others are counted as "Still has no Apple ID" or "No matching still" and remain separate videos.
- Check a few pairs in the Photos app after importing.

## Command line

```
python3 takeout_fix_metadata.py "/path/to/Takeouts" --dry-run
python3 takeout_fix_metadata.py "/path/to/Takeouts" --out "/path/to/Fixed" --overwrite --pair-live
```

| Flag | Meaning |
|---|---|
| `--dry-run` | Match and report only |
| `--out DIR` | Write fixed copies here instead of editing in place |
| `--overwrite` | Replace existing EXIF values |
| `--pair-live` | Re-pair Live Photos (needs `--out`) |
| `--dedupe` | Skip byte-identical duplicate files |
| `--layout folder\|yearmonth` | Merge same-named folders (default) or `YYYY/YYYY-MM` by date taken (needs `--out`) |
| `--move` | Move into `--out` instead of copying (needs `--out`) |
| `--report FILE` | Where to save the CSV (default `takeout_report.csv`) |
| `--workers N` | Parallel workers (default 4) |

The command line takes one folder; put your batches under a common parent folder, or use the app for several.

## Troubleshooting

| Problem | Fix |
|---|---|
| "exiftool not found" | `brew install exiftool` |
| Lots of **No JSON** | Add the other Takeout batches and preview again. If *JSON with no photo* is also zero, the missing sidecars were not in your export |
| Dates look wrong by some hours | Timezone: Google's times are UTC |
| **Kept** counts are high | Tick **Overwrite existing EXIF values** |
| Folder buttons do nothing | Type or paste the paths instead |
| `exiftool-error` rows | See the `detail` column in the report |
| Live Photo not recognised in Photos | The still probably lost its Apple ID; see the Live Photos section |

## Safety

- Use an output folder so originals stay untouched.
- Preview first; nothing is changed until you untick Preview.
- If you edit in place, work on a copy.

# MetadataFixer user guide

Restores the real date, location, description and people to photos and videos exported from Google Photos with Google Takeout.

## Why you need this

A Takeout export gives you photos and videos plus a `.json` file for each one. The date, GPS location, description and tagged people live **only in the `.json`**, not in the photo, and the `.json` can be in a different folder or a different `Takeout N` batch from its photo. This tool finds the right `.json` for every file and writes the information back.

## Plain-language glossary

- **EXIF (the photo's hidden label):** facts saved *inside* every photo or video file, separate from the picture: the date and time taken, the GPS location, the camera, and an optional caption. Apps like Photos and Lightroom read it to sort your library by date and show it on a map. Google's export often leaves this blank or wrong, which is the problem this tool fixes. It never changes the picture itself.
- **.json file (Google's info file):** a small text file Google gives each photo in a Takeout, holding the real date, location, caption and people. This is where the facts come back from.
- **Sidecar:** any extra file that travels alongside a photo and describes it. The `.json` files are sidecars.
- **Preview only (dry run):** a rehearsal. It does all the matching and counting but changes nothing.
- **Replace / overwrite:** off means "only fill in what is missing"; on means "use Google's version even if the photo already has something".
- **Live Photo:** an iPhone photo with a 2-3 second video. Takeout splits it into a still and a video; the pairing option links them again.
- **Duplicate:** a byte-for-byte identical copy of the same file.

## What you need

- A Mac (the app's folder pickers use macOS; the command line also works on Linux/Windows)
- Python 3.8+ (`python3 --version`)
- exiftool: `brew install exiftool`
- All your Takeout zips **extracted** into folders (the tool reads folders, not zips)
- Free disk space roughly equal to your photo library if you use an output folder (recommended)

## Updates

The app checks the MetadataFixer repository for a newer version when it starts, every time you load or reload the page, and every 30 minutes while it is open. You can also press **Check for updates** next to the version number in the header, which says "Up to date" or shows the update banner. If there is one, a banner appears at the top of the page listing the changed files. Press **Update now** and the app downloads the two program files, checks they are valid, keeps your old copies as `takeout_gui.py.bak` and `takeout_fix_metadata.py.bak`, restarts itself and reloads the page. It will not update while a job is running, and it never changes anything until you press the button. Your reports and output folders are untouched.

- Needs an internet connection. If you are offline, nothing happens and the app works as normal.
- To skip the check, start it with `python3 takeout_gui.py --no-update-check`.
- If you ever want to go back, copy the `.bak` files over the current ones.
- Only these two files, from `https://raw.githubusercontent.com/daviddef/MetadataFixer/main/`, are downloaded.

## Install

```
git clone https://github.com/daviddef/MetadataFixer ~/MetadataFixer
```

Or download `takeout_fix_metadata.py` and `takeout_gui.py` from the repo into one folder. The two files must be from the same version.

## Layout

The app has five tabs: **1 Fix metadata**, **2 Sort**, **3 Merge folders**, **4 Clean up** (`.json` files, junk, names, empty folders) and **5 Convert videos**.

## Choosing your folders (once, for every tab)

The title block at the top holds two rows that every tab uses, so you only choose them once:

- **Source:** the folders to work on. Press **Add folders...** (hold Cmd in the Finder dialog to pick several), or **Edit list** to type or paste one path per line, or drag folders onto the title block. Each folder shows as a chip; press the **x** on a chip to remove it.
- **Destination:** where the fixed, sorted or merged copies go. Type or paste a path, or press **Choose...**. Fix, Sort and Merge use it; the other tabs ignore it. Leave it empty to fix files in place (Fix), or, with Move ticked, to merge everything into the first source folder (Sort, Merge).
- Both are remembered in your browser. Under the title block, the tabs and the progress bar stay frozen at the top of the window as you scroll.
- Each tab shows which folders and destination it is using.
- **Order matters in one place:** when you merge or sort in place, everything goes into the **first** source folder. Use **Edit list** to reorder.
- Because the same list is used everywhere, check it before a destructive step: the Clean up tab and the confirmation prompts list the folders they will act on.

## Using the app (recommended)

```
cd ~/MetadataFixer
python3 takeout_gui.py
```

A page opens at `http://127.0.0.1:8765`. It runs only on your computer. Leave Terminal open while you use it; press Ctrl+C there to quit.

### 1. Add your Takeout folders
Use the **Folders bar** at the top (see above). Add **every** Takeout batch. A photo in one batch can find its `.json` in another, so the more batches you include, the fewer files are left unmatched. Dropping folders from Finder onto the box works only if your browser passes the path; otherwise use the button.

### 2. Choose a destination
Set the **Destination** in the header (a new empty folder is best). Fixed copies and reports are saved there, and your Takeout originals are not touched. If you leave it empty, files are edited in place (you are asked to confirm).

### 3. Pick options
| Option | Default | What it does |
|---|---|---|
| Preview only (dry run) | **on** | Matches files and reports counts. Changes nothing. |
| Remove exact duplicates | **on** | Skips byte-identical copies of the same photo (repeated across Takeouts or albums); keeps the one in `Photos from YYYY`. |
| Move instead of copy | off | Moves files into the output folder instead of copying. Saves disk space but empties your Takeout folders as it goes. |
| Re-pair Live Photos | off | Re-links iPhone Live Photo videos to their still (see below). |
| When a photo already has a date and Google's is different | **Keep the earlier date** | Google sometimes records the day a photo was uploaded or re-saved, which is later than when it was taken, so the earlier of the two wins. Other choices: always keep the photo's own date, or always use Google's. A photo with no date gets Google's. |
| Replace location and caption already stored in the photo (overwrite) | on | Off: only fill in a missing location or caption. On: replace a different one with Google's. |

### 4. Run a preview first
Leave Preview on and press **Start**. Read the summary (below). If **No JSON found** is high, add more Takeout folders and preview again.

### 5. Run it for real
Untick **Preview only**. Because Google's exports often carry wrong dates, tick **Replace location and caption already stored in the photo** too. Press **Start**. Large libraries take a while; the progress bar and live counts show how far along it is.

### 6. Check the result
Use **Show reports in Finder**, and spot-check a few files:
```
exiftool -DateTimeOriginal -GPSPosition -ImageDescription "/path/to/output/Photos from 2012/DSC_2865.JPG"
```
Keep your Takeout folders until you are happy with the output.

## Restructuring: copy vs move

With an output folder the tool **builds a new, merged library there and leaves your Takeout folders untouched** (a second copy, so you need roughly as much free space again). Tick **Move** to relocate files instead; it uses no extra space but empties the Takeout folders, so have another backup first.

Every file is placed, including those with no JSON (they keep their existing data). Folder names stay exactly as Google exported them: same-named folders from different Takeouts merge into one, and album folders stay as albums. If two different images land in the same folder with the same name (iPhones restart numbering), nothing is overwritten: the second is saved as `name_1.ext`. Each image is matched to its own JSON (the one in its own folder first, then the one with the closest date), so each keeps its own date and location. Exact duplicates are detected by content, not name. Because duplicate album copies are skipped, **album membership is not preserved**; the photo stays in its `Photos from YYYY` folder. You can re-run into the same output folder to resume an interrupted or partly failed run: finished files are skipped.

While it runs you see live counts (duplicates skipped, files placed, output folders) and the last dozen files with where each went. The summary ends with a table of the output folders.

## How dates are decided

Every photo can carry its own date (stored inside the file, in a label called EXIF), and Google's `.json` file carries another. The **date setting** on the Fix tab decides what happens when both exist and disagree:

- **Keep the earlier date (default).** Google often records the day a photo was *uploaded or re-saved* rather than when it was taken, and that is always later than the real date. Keeping the earlier one protects correct dates (for example a 2006 photo that Google dated 2018) and still fixes a camera date that is too late.
- **Keep the photo's own date.** Never changes a date that is already there.
- **Use Google's date.** Always replaces it.

A photo with **no date at all** always gets Google's date. Where Google's date is exactly its upload time, the report notes it in the `date_note` column (and the summary counts them, and the sample table shows a warning mark), because that date may not be when the photo was taken. One limit: a camera with a dead clock battery can stamp very early dates (such as the year 2000); "keep the earlier date" would keep those, so for such photos choose **Use Google's date**. The file's own modified date is set to the date that wins.

## Reading the summary

Live counters while running, then a full summary:

- **Changes made:** how many dates, locations and captions were changed (split into added and replaced), how many files had people tagged, and how many favourites were marked. A preview says "would change".
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

- Works in place or with a destination. In place, the video is renamed to `.MOV` beside its still, and its Google info file is renamed with it.
- Works only where the still still has its Apple ID. Others are counted as "Still has no Apple ID" or "No matching still" and remain separate videos.
- Check a few pairs in the Photos app after importing.

## Part 2: Sort only (merge folders, remove duplicates)

The **Sort only** panel does just the tidying, with no dates, locations or captions touched, so you can sort your library without the metadata work. It is independent of Part 1, and you can run it before or after.

1. Add the folders to sort, and choose an output folder. **If you tick Move you can leave the output empty**: everything is then merged into the **first folder in your list** (sorting in place), and any folders left empty by the move are removed.
2. Leave **Preview only** ticked and press **Start sorting** to see what would happen: how many folders would merge into how many, and how many duplicates would be skipped.
3. Untick Preview to do it for real.

What it does:
- **Merges folders with the same path.** The folder path below each folder you chose decides where a file goes, so `2014/08` in two different folders becomes one `2014/08`, and every `Photos from 2012` across your Takeouts becomes one `Photos from 2012` (the `Takeout N / Google Photos` wrapper folders are ignored). Your own folder structure is kept; nothing is flattened, and no date folders are created.
- **Skips exact duplicates** (optional, ticked by default). It compares file contents, so the same photo repeated across Takeouts or albums is kept once, preferring the copy in `Photos from YYYY`. Two *different* photos that share a name are both kept; the second becomes `name_1`.
- **Brings the `.json` files along** (optional, ticked by default), placing each beside its photo, so Part 1 still works on the sorted folder. Untick it if you only want photos.
- **Duplicates when moving:** an exact duplicate is not moved; it stays behind in its original folder, so you can review and delete it.
- **Copy or move.** Copy leaves the source untouched and needs about as much free space again. Move needs no extra space but empties the source folders (duplicates and `.json` files stay behind; remove the `.json` files later with the clean-up panel). Back up first if you choose Move.

You get live counts while it runs, then a summary: files found, duplicates skipped (and the space they take), files placed, and "N folders merged into M", plus a table of the resulting folders and a CSV of where every file went. A preview saves its CSV to your Desktop and leaves the output folder untouched.

Command line: `python3 takeout_fix_metadata.py "/path/to/Takeouts" --sort-only --dedupe --out "/path/to/Sorted"` (add `--move`, `--dry-run` or `--no-json` as needed).

## Convert old videos to MP4 (tab 5)

The **Convert videos** tab turns older video formats into `.mp4`, which plays on every phone, TV and app, and is usually smaller. It needs **ffmpeg** (`brew install ffmpeg`).

**Choose the formats one by one.** Each type has its own tick box: `.avi`, `.mov`, `.mpg`, `.mpeg`, `.wmv`, `.3gp`, `.flv` (ticked by default) and `.mkv`, `.mts`, `.m2ts`, `.vob` (unticked). Press **Scan folders for counts** to see, next to each type, how many videos there are and how much space they use, plus how many of the `.mov` files are **Live Photo videos** (these are left alone unless you tick the Live Photo option). The preview and the final summary also include a **By video type** table: found, converted (or would convert), Live Photo videos skipped, and problems.

1. Add the folders to scan. Leave **Preview only** ticked and press **Start converting** to see how many videos would be converted, and which would be quickly re-wrapped (lossless) and which re-encoded.
2. Choose what happens to the **original** video:
   - **Move to an `_original_videos` folder** (default, safe): the original is moved aside, keeping its folder structure.
   - **Keep it where it is**, next to the new `.mp4`.
   - **Delete it** (permanent; you must type `DELETE` to confirm).
   In every case the original is only moved or deleted **after** the new `.mp4` has been checked (it must play and match the original's length).
3. Untick Preview and start.

How it works:
- Three methods, chosen per video: **re-wrapped** (video H.264 or HEVC with AAC/MP3/AC3 audio: nothing is re-encoded, so it is fast and lossless); **video kept, audio converted** (H.264/HEVC video with other audio, such as Vorbis or Opus: the video is untouched and only the audio becomes AAC); and **re-encoded** (everything else, such as MPEG-2 `.mpg`, `.wmv` and old `.avi`: converted to H.264/AAC at your quality setting, which can take a long time for big files). Interlaced MPEG-2 video is de-interlaced.
- The date taken and GPS location are carried across to the new file, and its modified date matches the original. If the original had a `.json` file beside it, a copy is made for the new name.
- If an unrelated `.mp4` already has the same name, the new file is called `name_converted.mp4`; nothing is overwritten.
- **Live Photos:** an iPhone Live Photo is a still (`.HEIC`) plus a short `.MOV` video that Apple Photos links together. A `.MOV` sitting next to a photo with the same name is treated as a Live Photo video and skipped, because converting it would break the link. Tick **Also convert Live Photo videos** to override.
- A video that cannot be read, or whose conversion fails the check, is left untouched and listed in the report.
- An interrupted run can be repeated: finished conversions are recognised and skipped.
- Progress is shown by video length, so the percentage moves smoothly through long conversions.
- **Live feedback.** While reading the videos you see a real counter and percentage. While converting, a live panel shows the video in progress (its name, how it is being converted, its own progress bar, its speed in times real time and the time left for it), the overall progress (videos finished, hours of footage processed, time elapsed and an estimated time left, which improves as videos finish), the space saved so far, the plan (how many quick re-wraps versus slow re-encodes), and a **Just finished** list showing each video's before and after size and how long it took. The frozen status line shows the same in short form. **Stop** is always safe: it ends the current conversion, removes the half-written file, and the videos already finished are recognised and skipped when you press Start again.
- **Estimated sizes in the preview.** With **Estimate the new sizes in the preview** ticked (the default), the preview test-encodes short samples (about 8 seconds, up to 3 per video type) with your quality setting and uses them to predict the size after conversion, shown next to the current size, per type and in total. It is an estimate, not a promise: in my test it was within about 20% overall. Re-wrapped videos are counted at about their current size.
- **Default types:** every type is ticked except `.mov` (which is often an iPhone Live Photo video or a modern phone clip). Your tick boxes are remembered.
- **Sizes:** a preview shows how many videos (and how much space) would be re-wrapped versus re-encoded, and the largest ones; the exact saving can only be known after converting. During a real run a **space saved so far** tile updates as each video finishes. The final summary shows total size **before, after and saved** (with a percentage), a breakdown by method (re-wrapped vs re-encoded) and by file type, and a **Biggest savings** table. Re-wrapped videos stay about the same size; re-encoded ones usually shrink a lot. The saving is real free space only if the originals are deleted; if you keep them or move them to `_original_videos`, they still use disk space until you delete them.

Suggested order: run Part 1 (it creates the Live Photo `.MOV` files), then Part 4 for your other videos.

## Merge folders (tab 3)

Brings two or more folders together into one, for any folders (not only Google Takeout). Folders with the same name at any depth are merged, their files are combined, identical files are kept once, and different files with the same name are handled the way you choose. (The **Sort** tab is built around Takeout: it also carries `.json` files along and prefers the "Photos from YYYY" copy.)

1. Put the folders to merge in the **Source** list and choose a **Destination** (or tick **Move** with no destination to merge everything into the first source folder).
2. Leave **Preview only** ticked and press **Start**. You see how many files would come in, how many are identical, how many names clash, and which folders would come together, and nothing changes.
3. Untick Preview and start.

Choices:
- **Copy or Move.** Copy leaves your sources untouched (needs about as much free space again). Move empties the sources, and folders left empty are removed.
- **When two different files have the same name in the same folder:** *keep both* (the second becomes `name_1`), or *the newer*, *the larger*, or *the one from the first source folder* keeps the name. With those last three the other file is **not deleted**: it is set aside in a `_merge_conflicts` folder inside the destination for you to review.
- **Identical files** (same content, compared by content not name) are kept once. When moving, the extra copy is deleted or moved to `_duplicates`, as you choose.
- **Treat `Folder (1)`, `Folder copy` and extra spaces as the same folder as `Folder`**, and **ignore upper and lower case** (so `photos` and `Photos` become one folder). Real names such as `Summer (2019)` are not changed.

Safety: sources that overlap, or a destination inside a source, are refused. App and library bundles (such as `.photoslibrary`) are moved as one item. Invisible system files are left out. An interrupted merge can be run again with the same settings: files already placed are skipped. A CSV of every file and where it went is saved on your Desktop; the summary shows each source folder, the folders that came together, and every name clash.

## Clean up (tab 4)

One tab, five optional tasks. Tick what you want; they always run in this order, so earlier steps leave work for later ones (for example, deleting cache files leaves folders empty, which the last step removes). Use **Preview only** first: it lists everything that would happen and changes nothing.

1. **Fix files with no extension** (on by default). Some photos and videos come out of Google Takeout named like `IMG_2438` with no `.jpg`, `.heic` or `.mp4`, so Finder calls them "Document" and the Fix tab skips them (its summary warns you how many it skipped). They are usually **not corrupt**: this step reads the real type from inside each file (not from its name) and adds the right extension, and renames each file's Google `.json` to match so Fix can still find it. A short video with no extension next to a `.HEIC` of the same name is typically the video half of an iPhone Live Photo; once it has `.mp4` the Live Photo re-pairing in Fix can use it. Files whose type still cannot be recognised are listed with a plain-language **reason**: the file is empty; it is filled with zeros (a failed copy or a bad disk sector); it contains JPEG, MP4 or other data but the first N bytes are damaged or extra; it looks like a text note; or it is unknown or truncated binary data. They are not changed unless you tick the option to move them into an `_unrecognised` folder inside each folder you chose, so you can review or delete them. Truly damaged files cannot be repaired automatically: restore them from your original export or a backup. Do this **before** Fix metadata.
2. **Remove Google `.json` files** (off by default). The info files Google adds to each photo. They hold the only copy of the original dates and locations, so do this only **after** you have fixed your photos. Option: also remove other `.json` files.
3. **Remove junk and cache files.** System leftovers (`.DS_Store`, `Thumbs.db`, `desktop.ini`, `._` files), iPod/iTunes thumbnail caches (`.ithmb`, such as `T103.ithmb` inside an *iPod Photo Cache* folder), `Picasa.ini`, optionally camera video thumbnails (`.thm`), **empty temporary files left by macOS when saving** (names ending in `.sb-12345678-AbCdEf`, 0 bytes; a leftover that still contains data is never deleted, only reported), and **other empty (0-byte) files** of any name. Each kind has its own tick box, and the preview lists examples.
4. **Tidy names.** Fixes duplicate-style names such as `From Cris Drive - 2001(1)` to `From Cris Drive - 2001`:
   - Removes ` (1)`, ` (2)` and so on (small numbers only, so real names such as `Summer (2019)` are never changed), ` copy` / ` copy 2`, and extra spaces.
   - If a folder with the clean name already exists, the two are **merged**. Identical files are kept once (the extra copy is deleted, or moved to a `_duplicates` folder if you choose). Different files with the same name are both kept: the second becomes `name_1`. Folders inside are merged the same way.
   - Names are tidied from the innermost folder outwards, and the folders you chose are not renamed themselves.
   - Optionally tidy **file** names too. A file is only renamed when the clean name is free, and its `.json` is renamed with it. Best done after fixing your photos, since the fixer matches photos to JSON by name.
5. **Remove empty folders.** Removes every folder with **no files at all**, however deep: a folder counts only if everything inside it is empty too. Chains such as `a/b/c/d` go from the bottom up. A folder holding only system leftovers counts as empty (tick box). Shortcuts, app/library bundles (such as `.photoslibrary`) and unreadable folders are never entered. The folders you chose are kept unless you tick the option to remove them if they end up empty.

Safety:
- Before a real run that deletes files, you must type `DELETE`; the prompt lists the folders it will act on. Deleting is permanent (no Trash).
- Very broad folders such as `/` or your home folder are refused.
- The summary shows each step separately (counts, space freed, a before and after list of renames, the top-level empty folders), and a CSV of every change is saved to your Desktop.
- In a preview, files that would be deleted by earlier steps are treated as gone when counting empty folders, but folders emptied by a name merge are not counted until you run it.

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
| **Kept** counts are high | Tick **Replace information already stored in the photo** |
| Folder buttons do nothing | Type or paste the paths instead |
| `Input/output error` (Errno 5) on a file | The drive could not read or write that file (a failing drive, loose cable or power, or a damaged file). The tool skips the file, records it as `copy-error` in the report and carries on. Check the drive in Disk Utility (First Aid), try copying that file in Finder, then run the same job again |
| A run stopped or was interrupted | Run it again with the **same output folder**. Files already placed are skipped (the tool keeps a hidden progress log (`.metadatafixer_fix.jsonl`, `.metadatafixer_sort.jsonl` or `.metadatafixer_merge.jsonl`, one per tab) in the output folder, and for sorting it also recognises identical files already there), so you get no `_1` copies |
| `exiftool-error` rows | See the `detail` column in the report |
| Live Photo not recognised in Photos | The still probably lost its Apple ID; see the Live Photos section |

## Safety

- Use an output folder so originals stay untouched.
- Preview first; nothing is changed until you untick Preview.
- If you edit in place, work on a copy.

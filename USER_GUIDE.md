# Backstory user guide

Restores the real date, location, caption and people to photos and videos exported from Google Photos with Google Takeout, and helps you tidy the result. Free. Everything runs on your own computer; nothing is uploaded.

Questions or problems: **thestocksoup@gmail.com** (see [Support](#support)).

## Quick start

1. **Back up first.** Keep your original Takeout zip files (or folders) somewhere safe. Backstory never changes your zips and, by default, only makes copies, but a backup is always the right first step.
2. Open the app and go to the **Guided** tab.
3. In the bar at the top, press **Add zip files...** (or **Add folders...**) and choose your Takeout. Then choose a **Destination**: a new, empty folder on a drive with enough free space.
4. Press **Check my files**. The app looks at your real files and recommends what to do and why.
5. Press **Preview the recommended plan**. Nothing is changed in a preview. Read the summary.
6. If you are happy, untick **Preview only** and press **Fix my Takeout**. Your finished library appears in the Destination. Your originals are untouched.

## Before you start: important

- **You are responsible for your files.** Keep backups. See [Safety, limitations and disclaimer](#safety-limitations-and-disclaimer).
- **Preview first.** Every tab starts with **Preview only** ticked. A preview shows what would happen and changes nothing.
- Options marked with a warning sign can delete, overwrite, rename or merge things. Read them before you tick them.
- If something looks wrong, press **Stop**. Then check the report before running anything again.

## Why you need this

A Takeout export gives you photos and videos plus a `.json` file for each one. The date, GPS location, caption and tagged people live **only in the `.json`**, not in the photo, and the `.json` can be in a different folder or a different zip from its photo. This tool finds the right `.json` for every file and writes the information back.

## Plain-language glossary

- **EXIF (the photo's hidden label):** facts saved *inside* every photo or video file, separate from the picture: the date and time taken, the GPS location, the camera, and an optional caption. Apps like Photos and Lightroom read it to sort your library by date and show it on a map. Google's export often leaves this blank or wrong, which is the problem this tool fixes. It never changes the picture itself.
- **.json file (Google's info file):** a small text file Google gives each photo in a Takeout, holding the real date, location, caption and people. This is where the facts come back from.
- **Sidecar:** any extra file that travels alongside a photo and describes it. The `.json` files are sidecars.
- **Preview only (dry run):** a rehearsal. It does all the matching and counting but changes nothing.
- **Replace / overwrite:** off means "only fill in what is missing"; on means "use Google's version even if the photo already has something".
- **Live Photo:** an iPhone photo with a 2-3 second video. Takeout splits it into a still and a video; the pairing option links them again.
- **Duplicate:** a byte-for-byte identical copy of the same file.
- **Destination:** the folder where your finished, fixed copies are created.

## What you need

- **The Mac app (recommended):** download the `.dmg` from the Releases page, drag the app to Applications and open it. ExifTool and ffmpeg are included.
- **Or run from the source files:** a Mac (or Linux/Windows for the command line), Python 3.8+, ExifTool (`brew install exiftool`) and, for video conversion, ffmpeg (`brew install ffmpeg`).
- Free disk space: for zip files, about twice your largest single zip while it is processed, plus room for the finished library. The Guided checklist shows what you have.

## Install and updates

**Mac app:** the app checks for a newer release when it starts and shows a banner with a download link. Download the new version and replace the old app in Applications.

**From source:**
```
git clone https://github.com/daviddef/MetadataFixer ~/MetadataFixer
cd ~/MetadataFixer
python3 takeout_gui.py
```
A page opens at `http://127.0.0.1:8765`. It is served only on your own computer. The app checks for a newer version when it starts, when you reload the page, and every 30 minutes; press **Check for updates** next to the version number to check now. When an update is available a banner lists the changed files. **Update now** downloads them, checks they are valid, keeps your old copies as `.bak`, restarts, and reloads the page. It never updates while a job is running and never without you pressing the button. To skip the check: `python3 takeout_gui.py --no-update-check`. To go back, copy the `.bak` files over the current ones.

## Reading the screen

The longer explanations sit behind small **i** buttons: hover over one (or tap it on a phone) to read it. Each tab has a one-line description under its title. Warnings that matter, such as "(empties the source folders)", stay visible.

The app has eleven tabs: **Guided** (the easy way), **Fix** (metadata), **Merge** (folders), **Clean up** (`.json` files, junk, names, empty folders, similar folders), **Convert** (videos), **Health** (library health, duplicate formats and statistics), **Monitor** (iCloud upload status and log issues), **Photos** (send to Apple Photos), **Similar** (the same photo saved twice), **History** (past runs, reports and undo) and **Help** (this guide, support and the disclaimer).

## The warning sign

Options marked with a warning sign (⚠️) can delete, overwrite, merge, rename or empty things. Everything else is low risk. Every tab has **Preview only** as its first option, ticked by default.

## Stopping a job

Every job (preview or real) has a **Stop** button in the status bar at the top. Stopping is safe: files already handled stay done, nothing is left half-written, and running the same settings again carries on where it stopped. Stopping a preview changes nothing.

## Choosing your folders (once, for every tab)

The title block at the top holds two rows that every tab uses, so you only choose them once:

- **Source:** the Takeout zip files or folders to work on. Press **Add zip files...** or **Add folders...** (hold Cmd in the Finder dialog to pick several), or **Edit list** to type or paste one path per line, or drag items onto the title block. Each item shows as a chip; press the **x** to remove it.
- **Destination:** where the fixed, sorted or merged copies go. Type or paste a path, or press **Choose...**. Guided, Fix and Merge use it; the other tabs ignore it.
- Both are remembered in your browser. The tabs and progress bar stay frozen at the top as you scroll.
- **Order matters in one place:** when you merge in place, everything goes into the **first** source folder. Use **Edit list** to reorder.
- Because the same list is used everywhere, check it before a destructive step: Clean up and the confirmation prompts list the folders they will act on.

## Check my files (recommendations)

On the **Guided** tab, **Check my files** reads your Source (zip files, folders, Photos libraries, or a mix) and tells you what it found and what to do, based on your real data. Zip files are read without unpacking them, so even a large Takeout is checked quickly.

It reports:
- how many photos and videos you have, and how many have a Google `.json` (so how much can be restored);
- a sample of your files, to estimate how many lack a date or location, and how many already hold a different date or location than Google's;
- exact duplicates and the space they take, and (when you added several sources) which sources share the same photos;
- Live Photo pairs, Google-edited copies, files with no or a wrong file extension, old video formats, junk files, empty folders and look-alike folder names;
- whether your Destination has enough free space, and zip files that look incomplete or numbered with gaps;
- on a Mac with Photos, whether your library will fit and which files Photos cannot import.

You get three things:
1. **Your suggested route:** the steps in a sensible order (build the library, tidy, review similar photos, convert old videos, send to Apple Photos, remove `.json`), each marked Done, Recommended or Optional, with a button that opens the right tab.
2. **Settings for the library build:** every Guided option with the reason and the numbers behind it and a Safe or Check-this label. Untick anything, then **Apply these settings** or **Preview the recommended plan**.
3. **Other tools that could help:** the tabs outside the build (Convert, Clean up, Similar, Photos).

It changes nothing. The recommendations are suggestions, not guarantees: always read the preview.

## Merging several libraries into one

Add every library to the Source list: Takeout zip files, folders, and even a Photos library (`.photoslibrary`, read-only) in any mix. Then use **Guided** (or **Fix**). Backstory builds one library in the Destination:
- folders with the same name combine, and identical photos are kept once, even across libraries;
- different photos that happen to share a name are both kept (the second becomes `name_1`);
- your folder structures are kept; nothing is flattened;
- the **order of your Source list matters**: when two sources hold the same folder name, the first one's spelling is used. Use **Edit list** to reorder;
- **Check my files** shows how many photos each source holds and how many are shared between them, before you run anything.

For Google Takeout sources the dates, locations and captions are restored from the `.json` files; for other libraries the files are copied as they are (with dates from file names where a date is missing). A **Photos library** is only ever read: its photos are copied out, but its albums, edits and Photos-only metadata are not carried over. To keep those, export from Photos first (File > Export).

### Comparing two libraries ("how alike are they?")

Open the **Merge** tab and use **Compare libraries** (it follows the matching and keeper rules in the duplicate settings). Pick two libraries (folders, a Photos library or zips) and Backstory tells you, without changing anything:
- how alike they are, as a percentage;
- which photos are in both, which are only in the first and which are only in the second;
- photos that are **nearly the same** (the same picture saved at a different size or quality) and not just byte-for-byte copies, with small thumbnails so you can see the difference.

The report also says, for every near-identical pair, **which library's copy a merge would keep and why** (for example *a favourite beats one that is not*), how many pictures would be kept from each library, how many would receive a location, caption or album name from the copy that is left out, how many burst pairs are left alone, and how many look alike but fail a matching rule you ticked (both are kept).

When you then merge, tick **Also skip near-identical pictures** to keep only the best version of each. This option is off by default, because it is a judgement about pictures: look at the comparison first.

To merge two **folders** without any metadata work, the **Merge** tab does that, with control over name clashes.

## Choosing your style (Guided tab)

At the top of **Guided** you pick how you like to play it. One tap sets the options across the whole app (Guided, Fix, Similar and Photos). Each style shows three dot meters: **Time**, **Risk** and **Reward**.

| Style | What it does | Time | Risk | Reward |
|---|---|---|---|---|
| 🛟 **Safest** | Only fills gaps. Never replaces a location or caption, no guessing, no near-duplicate skipping, strict duplicate matching. | ●●● | ● | ●● |
| ⚖️ **Balanced** | The recommended mix. | ●●● | ●● | ●●● |
| ⚡ **Fastest** | Fewest passes: skips the duplicate pass, Live Photo pairing, folder dates and albums; bigger Photos batches. | ● | ●● | ● |
| 🎯 **Thorough** | Everything safe, done as well as possible, with small Photos batches that wait for iCloud. | ●●●●● | ●● | ●●●● |
| 🎲 **I like risk** | Corrects dates from folder names, guesses locations, skips near-identical pictures, keeps only the best burst frame, converts old videos, prefers edited versions. | ●●● | ●●●●● | ●●●●● |

**Preview stays on in every style**, so you always see what would happen before it does. After a style is chosen you can still change any single option; the card then says *Customised*. **Check my files** tunes the options to your own data on top of the style you chose.

## Guided: Fix my Takeout (the easy way)

The **Guided** tab does the whole job in one go, in the safe order, for people who just want a clean library.

1. In the bar at the top, add your Takeout **zip files** (press **Add zip files...**, or **Add folders...** and choose the folder that holds the zips) or the folders you already unzipped.
2. Choose a **Destination**: where the finished library will be created. Your originals are never changed.
3. (Recommended) Press **Check my files** first and follow its suggestions. Then leave **Preview only** ticked and press **Fix my Takeout**. You get a summary of what would happen.
4. Untick Preview and run it for real.

What it does, in order:
- **Repairs files with a missing file type** (only for folders you unzipped; this renames those files in the source folders. Zip files are handled automatically in the copy).
- **Puts dates, locations and captions back, merges same-named folders and removes exact duplicates**, keeping your folder structure and re-pairing Live Photos.
- Optionally **converts old videos to MP4** afterwards (not part of a preview). The old videos go into an `_original_videos` folder.

You see which step is running in the status bar, and **Stop** works at any time. If a run is interrupted, run it again with the same settings and it carries on.


## Takeout zip files

You do not need to unzip Google Takeout downloads. Add the `.zip` files (or the folder that holds them) to the Source list and use **Guided** or **1 Fix metadata**:

- The info (`.json`) files from **all** the zips are read first, so a photo in one zip finds its info file even if it is in another zip.
- The zips are then processed **one at a time**: only that zip's photos are unpacked, fixed and copied to the Destination, then the temporary files are deleted. You need free space for the largest single zip (twice over), not for the whole Takeout.
- A **Destination is required** and **Move is not available**, because the zip files are never changed.
- A photo that appears in more than one zip is copied once.
- Zips that finished are remembered in the Destination, so an interrupted run carries on with the remaining zips.
- Limits: Live Photo pairing works when the still and its video are in the same zip (Google normally keeps them together). The other tabs (Merge, Clean up, Convert) work on folders, not zip files.


## Reports, logs and history

Every run is recorded automatically:
- A **report**: the same summary you see on screen, saved as a page you can open, print or keep (**Open full report** under the summary).
- A plain-text **log**: the settings used, what happened step by step, how long it took, the version and tools, and a list of any problems. Useful if something goes wrong.
- A **History** tab listing every run with its headline result, and buttons to open the report, the log, or the folder.

Reports and logs are saved in `Documents/Backstory Reports`, one folder per run. Nothing is uploaded. If you need help, press **Copy diagnostic info** in the History tab and paste it into your message (check it for private paths first).

On the Guided tab, a short **checklist** shows whether your Takeout and Destination are set, whether the Destination has enough free space for your zip files, and whether ExifTool and ffmpeg are installed.


## Sending your library to Apple Photos (Photos tab)

The **Photos** tab sends a finished library to the Photos app, **in batches**, so that a Mac with limited space can take a large library over time.

How it works:
- It plans batches of about 2, 5, 10, 25 or 50 GB, **oldest first**, so your timeline fills in order. A Live Photo's still and video always travel together. Folders that are albums (not `Photos from 2012`) become Photos albums.
- Photos skips photos it already has, so running it again never duplicates.
- Between batches it waits so Photos can upload to iCloud. **Wait until Photos shows each batch as uploaded to iCloud** (the default) verifies the uploads by reading Photos' database, and can **adapt the batch size**: bigger when iCloud keeps up easily, smaller when a batch takes hours. Other choices: **Wait until my Mac has enough free space** (you choose how many GB to keep free), **Pause and let me press Continue**, or **Do not wait**. A **Continue** button appears in the status bar while it waits. Stop works at any time, and finished files are remembered.
- Always **preview** first (it shows every batch), then **send a small test (20 photos)** and look at the result in Photos before sending everything.
- Files Photos cannot import (such as AVI, MKV, WMV) are listed and left out: convert them on the Convert tab first.

Before you start, in **Photos > Settings > iCloud**, turn on **iCloud Photos** and choose **Optimize Mac Storage**. With that setting, once an original is safely in iCloud macOS can replace it on your Mac with a small copy when it needs room; that is what makes the waiting step work. macOS decides when it frees space, so with a very full disk the wait can take hours. Leave Photos open and the Mac awake.

**If your Mac really has no room:**
1. Build the library on an **external drive** (choose it as the Destination on the Guided tab). The library does not need to live on your Mac.
2. Either send it to Photos in batches as above, or move your **Photos library itself to the external drive** (quit Photos, copy the library there, hold Option while opening Photos and choose it, then Photos > Settings > General > Use as System Photo Library). With iCloud Photos on, the library on the drive is kept in sync.
3. Keep the external drive and your Takeout zips until you have checked everything in Photos and iCloud.

**Important:** Photos has no undo for imports and Backstory cannot take photos back out of Photos. Sending to Photos needs permission: the first time, macOS asks if Backstory may control Photos (allow it in System Settings > Privacy & Security > Automation).

## Library health (Health tab)

The **Health** tab checks a finished library (a folder, or several) and gives it a **score out of 100**, with a plain-language list of what is wrong and a button to the tab that fixes it. It only reads. Each time you run it, the result is saved, so you see a **score history** and whether the library is getting healthier or messier.

What it looks for:
- **Space and duplicates:** duplicate files (same size and content at both ends), the same file saved in several formats, temporary and partial files (`.part`, `.tmp`, `.bak`), leftover set-aside folders, `.json` files, junk and cache files, and an estimate of **possible wasted space**.
- **Folders:** empty folders, look-alike names (`Japan 2025`, `delete-Japan 2025`), `(1)` and `copy` markers, stray spaces, folders that differ only by capital letters, and very long paths.
- **Files and ghosts:** empty (0-byte) files, files with the wrong extension (a `.jpg` that is really a `.heic`), old-format videos, and `.json` files whose photo is gone.
- **iCloud and missing files:** files that are only in iCloud Drive and not on this disk (placeholders), which cannot be backed up, merged or imported until downloaded. If you add a Photos library, Backstory also reads a copy of its database for **experimental hints** such as items not yet uploaded to iCloud. Apple does not document that database, so treat those numbers as hints.
- **Dates and metadata:** photos with no date inside and photos in the wrong "Photos from YYYY" folder. A quick check reads a sample of about 400 files; tick **Deep check** to read up to 40,000.

**The same file in different formats.** When a video is converted and the old copy is kept, you end up with `IMG_1.mov`, `IMG_1.mp4` and `IMG_1.avi`. Backstory finds files with the same name in the same folder that are the same video (their lengths match within about a second) or the same picture in different formats (for example HEIC and JPEG). A Live Photo (a still plus a video) is not counted. Each group shows the formats, sizes, lengths and resolutions, keeps the best one (preferring MP4, then the largest picture) and ticks the older ones. **Set aside the ticked older formats** moves them into an `_older_formats` folder. Nothing is deleted, and you can undo it from History.

**Statistics.** Library size by file type, photos and videos by year, the biggest folders and files, RAW photos (how many have a JPEG or HEIC with the same name, and how much space each takes; Backstory never deletes RAW files), Live Photos and the most common cameras.

**Check again automatically.** Choose every hour, 6 hours or day. This runs only while Backstory is open and never while another job is running. A health check that runs in the background all the time, even when the app is closed, is on the roadmap.

## Full diagnostics (Health tab)

**Run full diagnostics** gives one verdict ("healthy", "needs attention" or "problem") in plain words. It combines four checks and shows what to do next for each:
1. **Library health**: empty, wrongly named and duplicate files, older formats, folders that look alike.
2. **Photos upload progress**: whether Apple Photos is progressing or stuck, with time left. Uploads are matched by file name **and** size, so a different picture with the same name is never counted as uploaded.
3. **Photos and iCloud logs**: problems in the system logs, explained with fixes.
4. **This Mac**: free space, battery, power mode and heat.

Nothing is changed by diagnostics.

## Speed and safety notes

- A repeat scan of an unchanged library is fast: Backstory remembers how each picture looks.
- A zip with a damaged or password-protected file inside no longer stops the run: that file is listed in the report, the rest carry on, and that zip is not marked as finished so you can run it again.
- The app only answers requests from its own window.

## Dates and places from folder names (Guided and Fix)

Folder names carry clues. A folder called **2017**, **2026-06**, **June 2015** or **Photos from 2019** tells us when its pictures were probably taken; a folder called **Johannesburg**, **South Africa** or **Japan 2025** tells us where. Backstory can use these clues, **only to fill gaps or to flag things that look wrong**. Each option below is also recommended (with your own numbers) by **Check my files**, and each is in Guided and on the Fix tab. Preview first.

- **Fill missing dates from the folder name** (on by default, safe). A photo with *no* date inside, in a dated folder, gets the date from the folder (the middle of the year or month when only that is known). A date that already exists is never changed by this.
- **Correct dates that disagree with the folder name, and dates in the future** (⚠️, off by default). If a photo in the *2017* folder says 2025, it probably lost its metadata somewhere (a "metadata strip"), and a date in the future (say 2028) cannot be right. This sets the date from the folder name. Only turn it on if you trust your folder names. Dates that are only a few months off from a month-named folder are flagged in the report, not changed.
- **Guess a location from the folder name** (⚠️, off by default). Only for photos with **no** location, in a folder that names a city or country (Backstory has a built-in offline list of about 300 well-known places). The location is the middle of that place, so it is approximate. Each guessed photo carries the keyword *Backstory: location guessed from folder name* so you can find or remove them, and the report says which folder it came from. A location a photo already has is **never** touched.

The **Health** tab also reports *Dates in the future*, *Dates that do not fit the folder*, *No date inside, but the folder name has one* and *No location, but the folder names a place*, with examples, and **Open Guided** takes you to the fix. It also reports **Locations that look mismatched**: a photo whose stored location is far from the place its folder names (more than about 300 km for a city, 1,500 km for a country, more for very large countries), or sits at 0, 0, which is almost always an error. These are only flagged (and listed in the report's *gps_flag* column), never changed.

## Slow or faulty drives (copying and moving)

External drives hiccup. Every copy and move in Backstory is now built to cope:
- a file is written under a temporary name and only given its real name when it is complete and the right size;
- if the drive reports an error, Backstory **waits, slows down and tries again** (up to six times with growing pauses), resuming a partly copied file instead of starting again;
- if the drive disappears (cable pulled, drive asleep), Backstory waits up to ten minutes for it to come back;
- when you **move** between drives, the original is only removed after the copy is complete and checked;
- if the drive stops answering altogether, Backstory pauses instead of failing every file, and tells you. Reconnect the drive and press **Continue where I left off** (in the results, or on the run in **History**): everything that finished is remembered, so it carries on from where it stopped.

A banner under the progress bar shows when Backstory is pausing for the drive. If it keeps happening, the drive or its cable may be failing: back up what you can.

## Duplicate matching and which copy to keep

Wherever Backstory finds the same picture twice (the **Similar** tab, **Guided**, and **Fix** with *skip near-identical pictures*), a panel called **How duplicates are matched, and which copy is kept** lets you set the rules. Your choices are remembered.

**Matching.** Pictures are always compared by how they *look*. Tick any of these to be stricter: the file name must match, the date and time taken must match, the width and height must match, the file format must match, the file size must match. Leave them all unticked to match by looks only.

**Keeping one copy.** From each group of duplicates one copy is kept. Backstory goes down your list: the first rule that tells two copies apart decides. The recommended order is:
1. a **favourite** (5 stars) beats one that is not;
2. an **edited** version beats an untouched one, so your crops and colour fixes are not lost;
3. **more pixels** (higher resolution) wins;
4. a **bigger file** wins (less compressed);
5. **more complete information** inside (date, location, caption, title, keywords) wins;
6. a photo already sorted into an **album** wins;
7. a copy in a *Photos from YYYY* folder wins.

You can untick any rule, change the order with the arrows, or add *more keywords*, *modern format (HEIC)*, *older file* and *newer file*. **Reset** brings back the recommended order.

**Nothing is lost from the kept copy.** If the copy that is kept has no location, no caption or no album names but a copy that was left out did, those are copied onto it. Existing values are never replaced.

On the Similar tab you still review every group yourself: the best copy is marked, the others are ticked to be set aside (moved, never deleted), and each picture shows tags such as favourite, edited, in an album. During a merge, the copies that are left out stay in your source, untouched.

**Bursts.** A burst is a run of photos taken a split second apart (Apple's burst photos, Google's *BURST* files, or three or more look-alike pictures taken within three seconds). By default **every frame of a burst is kept**: bursts are left out of the duplicate groups, and the Similar tab says how many were left alone. Choose *Treat burst photos like any other duplicates* in the same panel if you want only the best frame of each burst.

## Storyboard: before and after

Before you commit, Backstory shows what will happen to a few of **your real photos**. In **Check my files** and in the Guided and Fix summaries (preview and real runs) you see cards with a thumbnail, the folder it goes to, and for each of **Date**, **Place** and **Caption** what it is now and what it becomes. Changed values are highlighted; unchanged ones are marked. Notes explain special cases: a date read from the file name, a Live Photo re-paired, an identical copy kept once (and the album name saved as a keyword), or a copy left out. A four-step strip above it summarises the run: found, restored, merged, result. The thumbnails are saved inside the report, so the report still shows them later.

## Photos and iCloud monitor (Monitor tab)

**Is everything in iCloud yet?** Backstory reads a copy of your Photos library's database (never the original) and shows how many items are in Photos, how many are uploaded to iCloud, and how many are waiting. If you check again later it works out your **upload speed and the time left**, and if the number waiting has not fallen for about 45 minutes it tells you uploads look **stuck** and offers to check the logs for the cause. It also checks **the files Backstory itself sent** (by name and size) against Photos, so you can see that every one of them arrived and uploaded. When they all have, it shows a short **Ready to retire the staging copy?** checklist. Keep your Takeout zip files and the staging drive until you have looked through Photos and iCloud.com. The zips hold Google's original information.

Apple does not document this database, and it changes between macOS versions, so these numbers are a strong hint rather than a guarantee. If it cannot be read, Backstory says so, and the Photos tab falls back to waiting for free space.

**Log issues.** Photos and iCloud write errors that you never see in Console. **Check the logs** reads the last hour, 6 hours, day or week of Photos and iCloud errors from the macOS log, recent Photos crash reports, and Backstory's own failed runs. It groups them and explains each in plain language with what to do: a full disk, a full iCloud plan, a dropped network, an iCloud sign-in problem, a damaged Photos database, a repair or rebuild in progress, files Photos refused to import, Low Power Mode, a paused sync, permissions that block Backstory, a hot Mac, crashes, unreadable files, and drive I/O errors. Errors it does not recognise are listed so you can send them to support. You can also **paste log text** (from Console or a crash report) to have it interpreted, on any computer. **Check automatically** repeats the check while Backstory is open and can show a notification for a new serious problem. Reading the macOS log needs no special permission for normal use, but some entries may be hidden by macOS privacy rules.

## Albums and Google-edited copies

**Albums.** Google saves a photo once in its year folder and again in each album it belongs to. When duplicates are removed, the album copies are skipped, so Backstory saves the album names as **keywords** on the kept photo (Apple Photos and Lightroom show keywords) and writes a list of albums with the reports (`takeout_report_albums.csv`). It is ticked by default in Guided and Fix.

**Edited copies.** When you edit a photo in Google Photos, Takeout contains both `IMG_1.jpg` and `IMG_1-edited.jpg` (in other languages the word differs; the common ones are recognised). Choose **Keep both** (default), **Keep only the edited version** or **Keep only the original**. The copies left out are not copied into the new library; they stay in your Takeout.

## Similar photos (Similar tab)

Finds pictures that look the same but are not identical files: the same photo saved smaller, re-saved, or lightly edited. It compares how pictures look (not their names), shows each group with thumbnails, and suggests keeping the largest. Photos you tick are **moved** into a `_similar_set_aside` folder inside your folder, keeping their structure. Nothing is deleted, and you can undo it from History. Choose how alike the pictures must be: *Very alike* is safest; *Loosely alike* finds more but needs a careful look. Needs ffmpeg (included in the Mac app). On large libraries the scan takes a while.

## Smart folder consolidation (Clean up tab)

Folders such as `Japan 2025`, `delete-Japan 2025`, `Japan 2025-old` and `Japan2025 (1)` are usually the same trip. **Find similar folders** looks at folders that sit side by side and groups the ones whose names differ only by words like *delete, old, copy, backup, final, new, temp*, by `(1)` markers, spaces, punctuation or capital letters. Optionally it also lists likely spelling differences (`Italy 2024` and `Itly 2024`), marked **Check this** and not ticked for you.

For each group you see the folders and how many files each holds, and the name they will be merged into (you can edit it). Tick the groups you want, keep **Preview only** on for a first look, and press **Merge the ticked groups**. Identical files are kept once; different files with the same name are kept as `name_1`. Merging moves files and cannot be undone from the app, so preview first and keep a backup.

## Undo (History tab)

Runs that copied or moved files into a Destination (Guided, Fix, Merge, and setting similar photos aside) get an **Undo this run...** button in History. Undoing a copy run removes the files that run created (your originals and zip files are not touched). Undoing a move run moves the files back. Not everything can be undone: deleting `.json` files, junk, duplicates and original videos, renaming files in your source folders, merging similar folders, and in-place edits cannot be undone from the app.

## Dates from file names

Photos with no Google `.json` and no date of their own can get a date from their name: `IMG_20190704_123456.jpg`, `PXL_20210512_153045123.jpg`, `VID_20180102_030405.mp4`, `Screenshot 2019-07-04 at 12.34.56.png`, `IMG-20190704-WA0001.jpg`. This is ticked by default on Guided and Fix. It only fills in a **missing** date and never changes one that is already there. The time is used as written in the name; if the name has only a date, noon is used. The summary shows how many dates came from names.


## The Fix tab (all the options)

Use **Fix** when you want full control instead of Guided.

| Option | Default | What it does |
|---|---|---|
| Preview only | **on** | Matches files and reports counts. Changes nothing. |
| Remove exact duplicates | **on** | Skips byte-identical copies of the same photo (repeated across Takeouts or albums); keeps the one in `Photos from YYYY`. |
| Move instead of copy | off | Moves files into the Destination instead of copying. Saves space but empties your source folders as it goes. Not available for zip files. |
| Re-pair Live Photos | on | Re-links iPhone Live Photo videos to their still (see Live Photos). |
| Replace location and caption already stored in the photo | on | Off: only fill in a missing location or caption. On: replace a different one with Google's. |
| Use the date in the file name when there is no .json | on | Fills in a missing date from the name. Never changes an existing date. |
| When a photo already has a date and Google's is different | **Keep the earlier date** | See How dates are decided. |

Always run a preview first and read the summary. If **No JSON found** is high, add more Takeout zips or folders and preview again. Spot-check a few files after a real run:
```
exiftool -DateTimeOriginal -GPSPosition -ImageDescription "/path/to/output/Photos from 2012/DSC_2865.JPG"
```
Keep your Takeout until you are happy with the output.

## Copy or move (Fix tab)

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

## Convert old videos to MP4 (Convert tab)

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

Suggested order: run Fix first (it creates the Live Photo `.MOV` files), then Convert for your other videos.

## Merge folders (Merge tab)

Brings two or more folders together into one, for any folders, including Google Takeout exports (this tab replaces the old *Sort* tab). Folders with the same name at any depth are merged, their files are combined, identical files are kept once, and different files with the same name are handled the way you choose.

1. Put the folders to merge in the **Source** list and choose a **Destination** (or tick **Move** with no destination to merge everything into the first source folder).
2. Leave **Preview only** ticked and press **Start**. You see how many files would come in, how many are identical, how many names clash, and which folders would come together, and nothing changes.
3. Untick Preview and start.

Choices:
- **Copy or Move.** Copy leaves your sources untouched (needs about as much free space again). Move empties the sources, and folders left empty are removed.
- **When two different files have the same name in the same folder:** *keep both* (the second becomes `name_1`), or *the newer*, *the larger*, or *the one from the first source folder* keeps the name. With those last three the other file is **not deleted**: it is set aside in a `_merge_conflicts` folder inside the destination for you to review.
- **Identical files** (same content, compared by content not name) are kept once. When moving, the extra copy is deleted or moved to `_duplicates`, as you choose.
- **These are Google Takeout folders:** ignores the `Takeout N / Google Photos` wrappers so every `Photos from 2012` becomes one folder, and brings each photo's `.json` file along beside it, so tab 1 still works on the merged result. Leave it unticked for ordinary folders.
- **Find identical photos anywhere:** compares contents across all folders (not only within the same folder), so a photo repeated in several albums is kept once. Slower on big libraries.
- **Treat `Folder (1)`, `Folder copy` and extra spaces as the same folder as `Folder`**, and **ignore upper and lower case** (so `photos` and `Photos` become one folder). Real names such as `Summer (2019)` are not changed.

Safety: sources that overlap, or a destination inside a source, are refused. App and library bundles (such as `.photoslibrary`) are moved as one item. Invisible system files are left out. An interrupted merge can be run again with the same settings: files already placed are skipped. A CSV of every file and where it went is saved on your Desktop; the summary shows each source folder, the folders that came together, and every name clash.

## Clean up (Clean up tab)

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
python3 takeout_fix_metadata.py "/path/to/Takeouts" --out "/path/to/Fixed" --overwrite --pair-live --dedupe --name-dates
```

| Flag | Meaning |
|---|---|
| `--dry-run` | Match and report only |
| `--out DIR` | Write fixed copies here instead of editing in place |
| `--overwrite` | Replace existing EXIF values |
| `--date-policy` | `earlier` (default), `photo` or `google` |
| `--pair-live` | Re-pair Live Photos (needs `--out`) |
| `--dedupe` | Skip byte-identical duplicate files |
| `--name-dates` | Use a date in the file name when a file has no `.json` and no date |
| `--move` | Move into `--out` instead of copying (needs `--out`) |
| `--report FILE` | Where to save the CSV (default `takeout_report.csv`) |
| `--workers N` | Parallel workers (default 4) |

The command line takes one folder (not zip files); put your batches under a common parent folder, or use the app.

## Troubleshooting

| Problem | Fix |
|---|---|
| "exiftool not found" | `brew install exiftool` (the Mac app includes it) |
| Lots of **No JSON** | Add the other Takeout zips or folders and preview again. If *JSON with no photo* is also zero, the missing info files were not in your export |
| Dates look wrong by some hours | Time zone: Google's times are UTC |
| **Kept** counts are high | Tick **Replace location and caption already stored in the photo** |
| Folder buttons do nothing | Type or paste the paths instead |
| `Input/output error` (Errno 5) on a file | The drive could not read or write that file (a failing drive, loose cable or power, or a damaged file). The tool skips the file, records it as `copy-error` in the report and carries on. Check the drive in Disk Utility (First Aid), try copying that file in Finder, then run the same job again |
| A run stopped or was interrupted | Run it again with the **same Destination**. Files and zips already finished are skipped (the tool keeps hidden progress logs in the Destination), so you get no `_1` copies |
| `exiftool-error` rows | See the `detail` column in the report and the Problems table |
| Live Photo not recognised in Photos | The still probably lost its Apple ID; see the Live Photos section |
| "Not enough free space" | Free some space or choose another Destination. For zips you need about twice your largest zip while it is processed |
| A zip "could not be read" | It is probably incomplete or damaged: download it again from Google Takeout |
| The app does not open on a Mac ("unidentified developer") | Right-click the app, choose Open, then Open. Release builds are signed and notarized; unsigned test builds are not |

If you are stuck, open **History**, press **Copy diagnostic info**, and email it to **thestocksoup@gmail.com**.

## Safety, limitations and disclaimer

### Staying safe
- **Back up before you start.** Keep your original zip files or folders until you have checked the result.
- Use a **Destination** so originals stay untouched. Zip files are never modified.
- **Preview first.** Nothing is changed until you untick Preview only.
- Read the **warning sign (⚠️)** options before ticking them. Deleting `.json` files, junk, empty folders, original videos, and **Move** cannot be undone from inside the app.
- Spot-check a few results in your photo app before deleting anything.
- If you edit files in place (no Destination), work on a copy.

### What this tool cannot do
- It is **not a backup tool** and does not check that your photos are safe elsewhere.
- It works from a Google Takeout export. It cannot read Google Photos directly, upload anything, or recover photos that are not in your export.
- It writes metadata only to formats ExifTool can write. Some video formats (AVI, MKV, WMV, MPG, MTS and similar) only get their file modified time fixed.
- Matching a photo to its `.json` uses name rules and is not perfect. Rare cases may match the wrong file or not match at all; the report shows how each file was matched.
- Duplicate removal compares file contents. Album names of skipped copies are saved as keywords, but the album folders themselves are not recreated.
- Live Photo pairing needs the still to still have its Apple ID, and (in zip mode) both parts in the same zip.
- Times from Google are UTC; the tool cannot know your local time zone for a photo.
- Video conversion that re-encodes is lossy by nature. Quick re-wraps are not. Estimates of sizes and times are rough.
- Date-from-filename is a guess based on the name, used only when there is no other date.
- Importing into Apple Photos uses Apple's scripting interface and has not been tested on every macOS version or library; always preview and test with a few photos first.
- It has been tested mainly on macOS with typical Takeout exports. Unusual drives, network shares, very old or very new OS versions, damaged files, and very large libraries may behave differently.
- It does not guarantee that your photo app (Apple Photos, Lightroom, etc.) will display or import the results the way you expect.

### Disclaimer
Backstory is free software provided **"as is"**, without warranty of any kind, express or implied, including but not limited to merchantability, fitness for a particular purpose, accuracy, and non-infringement. **You use it entirely at your own risk.**

It changes, copies, moves, renames and (when you choose the relevant options) deletes files. You are responsible for your own backups and for checking what each option does before you run it, including by using Preview. **To the maximum extent permitted by law, the author and contributors are not liable for any loss or damage of any kind arising from the use of, or inability to use, this software**, including but not limited to loss, corruption or alteration of photos, videos, metadata or other data, loss of albums or organisation, incorrect dates or locations, hardware or drive problems, lost time, or any indirect or consequential loss, whether or not caused by a bug, a mistake, or by you choosing the wrong option.

Nothing in this disclaimer excludes or limits any right or liability that cannot lawfully be excluded or limited, such as rights you may have under consumer protection law where you live.

Backstory is an independent project. It is **not affiliated with, endorsed by or sponsored by Google, Apple or any other company**. Google, Google Photos, Takeout, Apple, iPhone, Live Photos, macOS and other names are trademarks of their owners and are used only to describe compatibility.

The software is released under the MIT License (see `LICENSE`). Third-party components and their licenses are listed in `THIRD_PARTY_NOTICES.md`.

## Support

Email **thestocksoup@gmail.com**. This is a free project, so replies are best-effort and there is no guaranteed response time or service level. To help us help you, include:
- what you were trying to do and what happened;
- the output of **History > Copy diagnostic info** (check it for private paths first);
- the `run.log` from the run, if there is one (History > Open log).

Please do not send your photos. You can also report problems or suggest features at https://github.com/daviddef/MetadataFixer/issues.

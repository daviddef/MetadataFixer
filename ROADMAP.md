# Shoebox roadmap

Goal: the safest, simplest, free tool for taking control of a photo and video library, starting with Google Takeout and growing into "get my whole library in order, then keep it that way".

## Where we are

| Area | Today |
|---|---|
| Fix metadata | Dates, GPS, captions, people, favourites from Google `.json`; zips read directly; matches across batches; date policy; Live Photo re-pairing; edited copies; dates from file names |
| Dates and places | Time-zone-correct dates; reconstruct missing dates from every clue with confidence; fill missing dates from folder names; optionally correct wrong-year and future dates; optional guessed locations from folder names, nearby photos or a GPX track (labelled); mismatched-location flags |
| Merge and duplicates | Same-name folder merge, exact duplicates, near-identical pictures with matching rules and ordered keeper rules, bursts, metadata carry-over, Compare libraries |
| Clean up and convert | Odd files, junk, name tidy, look-alike folders, empty folders; old videos to MP4, verified |
| Health and diagnostics | Library health score, duplicate formats, date/location checks, full diagnostics, Photos and iCloud log monitor |
| Apple Photos | Batched import, oldest first, upload verification, stall detection, adaptive batches |
| Trust | Preview by default, copy by default, Stop, undo, run logs and reports, retrying/resuming copies for faulty drives, local-only server with a private token |
| Ease | Guided mode, Check my files recommendations, five styles (Safest to I like risk), animated friendly UI, in-app guide |
| Delivery | Python app now; signed Mac app pipeline written (not yet built or tested) |

## Our edge over the competition

Most Takeout tools are one-shot scripts: they fix dates and stop, with no preview, no resume, and no help with the mess around the photos. Our edge to defend:

1. **Safety you can see.** Preview first, copy by default, verified conversion, Stop, a log of everything.
2. **The whole job, not one step.** Fix, merge, clean and convert in one place, in a sensible order.
3. **Plain language.** Every option explained, nothing assumed.
4. **Free, local, private.** No account, no upload, no subscription.

## Roadmap

### Now: make it installable (next 4-6 weeks)
- **Signed Mac app** (pipeline written): get the first build working, then release v1.0.
- **Landing page** (GitHub Pages): Download button, 3-step guide, short screen recording, privacy promise.
- ~~**Guided mode:**~~ DONE (v2026.10.01-z4): one "Fix my Takeout" button that runs Merge, Fix, Clean up in the right order with a single preview and a single summary.
- ~~**First-run check:**~~ DONE (Guided checklist): detect missing tools, low disk space, drive problems, and say what to do.
- ~~**Copy diagnostic info** button~~ DONE (History tab).
- **Windows build** with the same pipeline (needs a code-signing certificate or SmartScreen will warn).

### Next: close the gaps people hit (2-3 months)
- ~~**Takeout zip support:**~~ DONE (v2026.10.01-z4): read `.zip` files directly and extract as it goes, so users never unzip 50 GB first. Biggest ease-of-use win.
- ~~**Album preservation:**~~ DONE (keywords + albums csv): Takeout turns albums into folders and duplicates photos into them. Keep one copy, and write albums out as a list or as keywords so they are not lost.
- ~~**Google edits handling:**~~ DONE: pair `-edited` versions with originals; choose keep both, keep edited or keep original.
- **Missing and broken sidecars report:** list photos with no `.json`, ~~and offer a date from the filename~~ DONE (dates from file names) (`IMG_20190704_...`, `PXL_...`, `Screenshot ...`) when there is nothing else.
- ~~**Time zone care**~~ DONE: local time with UTC offset, from the photo's location or this Mac's zone.
- ~~**Takeout pre-flight report**~~ DONE (top of Check my files).
- ~~**Review screen for near-duplicates:**~~ DONE (Similar tab): same photo at different sizes or re-saved versions, shown side by side, with a keep-best suggestion. Never automatic deletion.
- ~~**Undo:**~~ DONE (History tab): one-click "put everything back" from the manifest for copy and move runs.
- **Faster:** parallel hashing, smarter resume, progress that survives sleep.

### Later: beat the paid tools (3-6 months)
- **Import-ready output:** presets for Apple Photos, Immich, PhotoPrism, Synology, Lightroom, Plex (folder layout, sidecars, naming).
- **Other sources:** iCloud Photos export, Facebook/Instagram downloads, Amazon Photos, OneDrive, old phone backups, SD cards.
- ~~**Library health report:**~~ DONE (Health tab). One page of what is wrong with a library: missing dates, no GPS, duplicates, odd formats, corrupt files, biggest folders.
- **Corrupt file triage:** detect truncated or damaged photos and videos, and repair the simple cases (like the leading-bytes problem we saw).
- **HEIC/RAW/format helpers:** optional convert for HEIC to JPG, and old RAW to DNG, with the same verify-before-replace rule.
- **Location tools:** ~~guess from folder names~~ DONE; ~~fill from neighbouring photos~~ DONE, ~~GPX track import~~ DONE; ~~offline place-name lookup~~ DONE (about 144,000 towns and cities).
- ~~**Receipt**~~ DONE (Monitor tab): a shareable page with metadata coverage, Photos/iCloud counts, albums and Live Photos.
- ~~**Quality helpers:**~~ blurry and screenshot detection DONE (keeper rules, Health). Still to do: side-by-side zoom compare.
- ~~**Motion Photos**~~ extraction DONE (video saved as .MP4). Pairing as Apple Live Photos is not possible with exiftool alone.
- **Face/people names** carried through as keywords for apps that read them.
- **Scheduled "keep tidy"** for a watched folder.

### Maybe (only if people ask)
- Browser-only version of the Fix tab (no install, Chrome/Edge, no video).
- Mac App Store version (needs sandbox rework).
- Translations (Spanish, German, French, Portuguese first).
- Optional AI helpers that run on the Mac: "is this a screenshot?", "which of these 6 is best?".

## Quality and trust (always)
- Automated tests on a bundle of mock Takeouts, run on every change.
- A public "tested on" list of real-world Takeouts and edge cases.
- Never delete without a preview, a warning sign and a way back.
- Keep it open source and free; no accounts, no telemetry unless a user turns on diagnostics.

## How we will know it works
- Downloads and returning users.
- Percent of runs that finish without an error.
- Support questions per 100 users (should fall as guided mode lands).
- Reviews that say "it just worked".


## Ideas under consideration
- **Verify the Photos problem catalog** against Apple's pages and real Console output, raise confidence levels, and add the exact log lines users send in. Also: check the supplied-CSV entries (marked supplied, unverified) and the 13 fix guides against real Macs.
- See `BACKLOG.md` for pricing/licensing notes and more ideas.
- **Always-on library health:** a background agent that rechecks and notifies.
- ~~**Verify uploads to iCloud**~~ DONE (Photos tab; reads a copy of Photos' own database, so treat it as a hint).
- **Set up a Photos library on an external drive** with a guided checklist.
- **Merge Photos libraries with albums and edits** (via an export step) rather than originals only.
- ~~**Storyboard preview**~~ DONE.
- **Date and structure options for merged libraries:** keep folders (default), or reorganise by year/month when merging very different libraries.
- **Scheduled "keep tidy"** for a watched folder.

# Tests

- `run_e2e.py`: end-to-end suite (about 47 tests) covering Fix, zips, Merge, Clean up, Convert, Similar, Health, Compare, Diagnostics, Photos (fake `osascript`), Monitor, undo, updater, duplicate/keeper rules, bursts, date and place helpers, persistent copying and the HTTP security layer. Run `python3 tests/run_e2e.py` (or `python3 tests/run_e2e.py keeper` to run tests whose name contains "keeper"). Needs exiftool and ffmpeg. Scratch files go to a temporary folder (`BACKSTORY_TEST_DIR` overrides it).
- `helpers.py`: mock Takeout and Photos library builders.
- `ui_sweep.js PORT`: opens every tab of a running app in headless Chromium and reports console errors and duplicate ids.
- `ui_profiles.js PORT`: clicks each style (Safest, Balanced, Fastest, Thorough, I like risk) and checks the options it sets.

Start a private copy of the app for the UI scripts with `METADATAFIXER_HOME=/tmp/x python3 takeout_gui.py --port 8801 --no-browser --no-update-check`.
- `ui_route.js PORT`: checks the FROM/TO bar (empty hint, chip folding, status line, which tabs show TO, remembered destinations).

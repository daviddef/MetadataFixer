# Tests

- `run_e2e.py`: end-to-end suite (about 47 tests) covering Fix, zips, Merge, Clean up, Convert, Similar, Health, Compare, Diagnostics, Photos (fake `osascript`), Monitor, undo, updater, duplicate/keeper rules, bursts, date and place helpers, persistent copying and the HTTP security layer. Run `python3 tests/run_e2e.py` (or `python3 tests/run_e2e.py keeper` to run tests whose name contains "keeper"). Needs exiftool and ffmpeg. Scratch files go to a temporary folder (`BACKSTORY_TEST_DIR` overrides it).
- `helpers.py`: mock Takeout and Photos library builders.
- `ui_sweep.js PORT`: opens every tab of a running app in headless Chromium and reports console errors and duplicate ids.
- `ui_profiles.js PORT`: clicks each style (Safest, Balanced, Fastest, Thorough, I like risk) and checks the options it sets.

Start a private copy of the app for the UI scripts with `METADATAFIXER_HOME=/tmp/x python3 takeout_gui.py --port 8801 --no-browser --no-update-check`.
- `ui_route.js PORT`: checks the FROM/TO bar (empty hint, chip folding, status line, which tabs show TO, remembered destinations).
- `live_ui.js PORT [shot.png]`: needs the server started with `BACKSTORY_LOG_STREAM_CMD=<script that prints a log line>`; starts Live watch and checks an event card appears.
- `fix_ui.js PORT [sync.png] [fix.png]`: needs `BACKSTORY_NETTOP_CMD` (prints nettop-style CSV) and `BACKSTORY_LOG_STREAM_CMD`; checks the Sync meter, a live card's fix button and the one-click set-up and undo.
- `cmp_ui.js PORT [shot1] [shot2]`: opens the five-style comparison table on the Guided tab, checks the rows, the differences filter and applying a style.
- `home_ui.js PORT`: Home screen, goal cards, Simple view, problem picker and guide opening; saves screenshots in /tmp/shots4.
- `ui_lint.js PORT`: plain-language lint over every visible title (jargon, long words, reading grade).
- Older `ui_*.js` and the `*_ui.js` scripts switch to the full tab view first (they set `localStorage mode=full`).
- `pb_ui.js PORT [shot.png]`: opens the Fix guides box on the Monitor tab, ticks a step and runs the library audit.
- `ui_kb.js PORT`: opens the Known Photos problems box on the Monitor tab and checks the list, search and filter.

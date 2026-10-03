# Backlog and ideas

## Pricing and licensing (for thought, not decided)

Idea: a paid app at about **US$20 for a lifetime licence** (or per major version), possibly with a free tier.

Why it is plausible
- The tool now does a lot (Takeout fixing, library merge, similar photos, health tracking, Apple Photos staging). Comparable single-purpose photo utilities sell for roughly US$10-60, and subscriptions are common.
- The value is easy to explain to someone with a Takeout problem: it saves hours and protects their photos.

Things to decide before charging
1. **Licence.** The repository is public and under the **MIT licence**, which lets anyone copy, fork, build and even sell it. Charging only works if you either (a) keep a closed-source paid app and an open free engine ("open core"), (b) change the licence for future versions (past versions stay MIT), or (c) sell convenience (signed, notarized Mac app, updates, support) and accept that forks exist. This is the main decision.
2. **What is free.** Option: free to **check, preview and see health**, paid to **run for real** (a licence unlocks Guided/Fix/Merge/Photos). Free previews build trust and make the paid step obvious.
3. **How to take payment.** A "merchant of record" such as Paddle, Lemon Squeezy or Gumroad handles card payments and sales tax (GST/VAT) worldwide and can issue licence keys. The Mac App Store is another route (30% or 15% fee, and a sandbox that makes "read any folder, control Photos" harder).
4. **Licence keys without a server.** The app is local-first, so use signed keys checked offline (no account, no phone-home), consistent with the privacy promise.
5. **Consumer law and refunds.** Charging brings statutory consumer guarantees (for example Australian Consumer Law) that a disclaimer cannot remove: acceptable quality, refunds for major failures. Offer a clear refund window, keep the "preview first" safety, and get the terms reviewed by a lawyer. Consider business registration/ABN, GST, and professional indemnity insurance.
6. **Bundled software.** ffmpeg builds can be GPL: selling is allowed but you must offer the source and licence text (see THIRD_PARTY_NOTICES.md); an LGPL build avoids most of the burden. ExifTool and the others are fine to bundle.
7. **Support load.** Paying customers expect replies. Budget time, and keep the diagnostic copy/paste flow.
8. **Price test.** Start free with an email list, then test US$19-29 lifetime with a launch price. "Lifetime" = all 1.x updates; paid upgrade for 2.0.
9. **Platform risk.** Google can change the Takeout format; Apple can change Photos scripting. A paid product needs a plan to keep up (one reason to prefer per-major-version pricing).

## Other ideas
- Always-on health monitoring: a small background agent (launch agent) that rechecks and notifies, even when the app is closed.
- Learnings from your previous Photos-library work: share a summary and each lesson can become a health check or a recommendation.
- Verify iCloud upload completion before the next Photos batch.
- Storyboard preview with before/after thumbnails.
- Windows build.

## From competitor research (PowerPhotos, Gemini, PhotoSweeper, immich-go, osxphotos, Google Takeout helpers)
- Time-zone-correct dates (Takeout times are UTC); highest-value fix.
- Takeout pre-flight report and a before/after "receipt" after a migration.
- Albums and Live/Motion Photos preserved through the Apple Photos handoff; verify them afterwards.
- Date reconstruction timeline combining EXIF, `.json`, file names, folder names and neighbouring photos, with confidence and a review queue.
- GPX / neighbouring-photo location fill with a map review.
- Blur, screenshot and low-quality scoring; pause/resume of long scans; side-by-side zoom compare.
- RAW+JPEG stacking choices; copy photos between Photos libraries keeping edits (PowerPhotos does this; large effort).
- Notes: never write to the Photos database; duplicate false positives are the biggest reputational risk, so default to review; a signed, notarized Mac build is expected by privacy-minded users.


## From the Photos handover brief (2026-10-03)
- Rescue exporter: export the files the library thinks are not in iCloud to another disk, de-duplicated, with each file's modified time set to its capture date so re-import dates correctly (the brief mentions a `photos_rescue.py` that was never finished).
- Album creation from a UUID list via AppleScript (`media item id (uuid & "/L0/001")`, fall back to the plain id): resolved 364 of 364 where the plain id resolved 2 of 5. Candidate for "make an album of everything not uploaded".
- Calibrate the ZCLOUDLOCALSTATE meaning (values 0/1/2/4 seen) against ZINTERNALRESOURCE on several libraries, then replace the "no established meaning" caveat.
- Open question: what the `[D]` flag on a CPLAlbumChange record means (delete?). Unverified.
- Timeline view of the sync engine backlog (the backlog check already saves readings in the app folder).

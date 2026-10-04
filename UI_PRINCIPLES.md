# How Shoebox's screens are designed

For anyone changing the interface. These rules come from well-established usability guidance (Nielsen's heuristics, Hick's law, progressive disclosure, jobs-to-be-done, plain-language and accessibility guidelines). **They have not been checked with real users yet.** See "Test it with five people" at the end.

## The person we design for
Someone who has a pile of photos, a vague worry ("are they safe?", "why is my storage full?") and no interest in how any of it works. They do not know what metadata, a batch or a duplicate hash is. They want to be told what to do, and they are afraid of losing photos.

## Rules, and how the app follows them

| Rule | What it means here |
|---|---|
| **Fewer choices up front** (Hick's law) | The first screen (Home) has eight goals, not eleven tabs. The tab bar is hidden in Simple view. |
| **Start from the person's problem, not our features** | Goals read as what someone says: "Videos won't play", "Something's not right", "Is everything in iCloud?". "Something's not right" opens a plain list of symptoms, and each one opens a checklist. |
| **One suggested next step** | The card at the top of Home always offers a single best next step (choose photos, choose a folder, look first, run it for real, move to Apple Photos). It follows what the person has already done. |
| **Progressive disclosure** | Expert tools (live log watching, sync meter, look-up, known-problems list) sit behind one button. Every option has an **i** button for the long explanation. Style buttons (Safest to I like risk) set many options at once. |
| **Plain words** | Titles are short, in everyday language, with no jargon. `tests/ui_lint.js` fails the build if a visible title uses jargon, has a very long word, or the average reading grade goes above 6.5 (it is about 3.4 now). |
| **Say what will happen** | Start buttons change with the Preview box: "Show me what would change" when preview is on, "Fix my photos for real" when it is off. |
| **Safe by default, reversible where possible** | Preview is on in every style. Risky options carry a warning sign. One-click set-ups never press Start for you, and list what they changed with an Undo. |
| **Feel good to use** | Bold colour per goal, big tap targets, chunky buttons that press down, gentle motion, confetti when something finishes. Motion is switched off for people who ask their device for reduced motion. |
| **Accessible** | White text on dark gradients (large, bold), visible focus rings, buttons larger than 44 px on a phone, nothing that depends on colour alone. |
| **Honest about limits** | Anything unverified says so ("draft", "not verified", confidence levels). |

## Words we avoid in titles
metadata, EXIF, timestamp, dedupe, perceptual, reconstruct, heuristic, daemon, sidecar, re-encode, orphan, checksum, hash, and any word of 13 letters or more. Put the detail behind the **i** button instead.

## Test it with five people
Give five people who are not technical a phone with the app and one task each, without explaining anything:
1. "You have a zip file from Google Photos. Get your photos fixed."
2. "Your iPhone says storage is full. Find out why."
3. "Videos from an old camera will not play. Fix that."
4. "Find out whether all your photos are safely in iCloud."
5. "You are not sure what to do. Get advice."

Watch where they pause, what they tap that is not the right thing, and which words they ask about. Five people find most of the big problems. Change the words they stumbled on and the order of the goals, then repeat.

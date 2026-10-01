# Building the Mac app

The GitHub workflow `.github/workflows/mac-release.yml` builds a signed, notarized `.dmg` for Apple silicon and Intel Macs.

## One-time setup (repository Settings > Secrets and variables > Actions)

| Secret | What it is |
|---|---|
| `MACOS_CERT_P12` | Your **Developer ID Application** certificate exported from Keychain Access as a `.p12`, then base64-encoded: `base64 -i cert.p12 \| pbcopy` |
| `MACOS_CERT_PASSWORD` | The password you set when exporting the `.p12` |
| `APPLE_ID` | Your Apple ID email |
| `APPLE_APP_PASSWORD` | An app-specific password from appleid.apple.com |
| `APPLE_TEAM_ID` | Your 10-character Team ID (developer.apple.com > Membership) |

Without the secrets the workflow still runs and produces an **unsigned** `.dmg` (fine for testing; Gatekeeper will warn).

## Releasing

1. Run it by hand first: Actions > Build Mac app > Run workflow. Download the `.dmg` from the run's artifacts.
2. To publish: `git tag v1.0.0 && git push origin v1.0.0`. The `.dmg` files are attached to a GitHub release.

## What is inside the app

Python, the app, ExifTool (run with the Mac's built-in Perl) and ffmpeg/ffprobe. Nothing needs installing.
The app window is a native web view (pywebview); if that is unavailable it opens your browser instead.

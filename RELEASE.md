# Releasing a new version

Everything happens on GitHub now — there is no Google Drive, no manifest file to
upload, and no local build required.

## TL;DR

1. Bump `version.py` to the new version (for example `2.1.0`).
2. Commit and push it.
3. GitHub → **Releases** → **Draft a new release** → type the tag `v2.1.0` →
   **Publish release**.
4. Wait ~15 minutes. The Release ends up carrying the Windows installer, the
   self-update ZIP and both `.dmg` files, and installed Windows copies update
   themselves from it.

That is the whole process. Today's installed copies take one final update through
the old Google Drive route, and after that Drive is not used at all - see
"Retiring Google Drive" below.

## Why a tag, not a button

The workflow (`.github/workflows/release.yml`) runs on two triggers:

| Trigger | What happens |
|---|---|
| **Run workflow** button | Builds all three platforms, keeps the results as *artifacts* (temporary, login-protected, expire after ~90 days). Useful to test a build. |
| Pushing a `v*` tag | Builds all three platforms **and** attaches them to a Release: a permanent public download link. |

Creating the Release in the GitHub web UI is the easiest way to push that tag,
and it is the same act as publishing the download page.

## The tag must match version.py

The artifact filenames come from `version.py`, but the Release is named after
the tag. Tagging `v2.1.0` while `version.py` still says `2.0.0` would publish a
"2.1.0" Release full of files called 2.0.0 — and installed copies compare
themselves against the version number, so they would never see the update.

The `release` job therefore fails on a mismatch and tells you which of the two to
fix. If you see that error, either bump `version.py` or re-tag.

## What a Release contains

| File | Who it is for |
|---|---|
| `mysetup210.exe` | Windows installer — a fresh install |
| `UniversalAudioStudio_2.1.0_update.zip` | Windows in-app updater. Users never download this by hand; the app fetches it by itself. |
| `UniversalAudioStudio-2.1.0-arm64.dmg` | Apple Silicon Macs (M1/M2/M3/M4, any Mac from late 2020 on) |
| `UniversalAudioStudio-2.1.0-x86_64.dmg` | Intel Macs |

Two `.dmg` files exist because a single universal one is impossible: `numpy`
ships architecture-specific wheels, so there is no universal2 build.

## How updates reach people

### Windows - automatic

`updater.py` asks the public GitHub Releases API for the newest published
Release, compares its version against the running app's, and when it is newer
downloads the `_update.zip` from that Release and installs it in place (Windows
shows a UAC prompt, because the app lives under `Program Files`). That happens
once at startup and whenever **Check for App Updates** is pressed.

There is no manifest to maintain and no upload step: the Release you published
*is* the update source. Only two things matter:

* the Release must be **published**, not a draft - a draft has no downloadable
  assets, so there is nothing to update to;
* the Release must **not** be marked as a pre-release. The app asks for
  `releases/latest`, which deliberately skips pre-releases, so ticking that box
  silently stops updates for everyone;
* the Release must contain the `*_update.zip` asset, which the workflow uploads
  automatically.

### Installs made before this change (2.0.0 and earlier)

Windows copies built before updates moved to GitHub read their update manifest
from a Google Drive file. That code is already inside those copies, so no new
Release can reach them on its own - they will never look at GitHub by themselves.

They are moved across by one final update through the old channel, which is what
the next section is for. It is the last time Google Drive is involved at all.

### macOS - manual, for now

The in-app updater is Windows-only, because it works by replacing a running
`.exe` behind a UAC elevation prompt. On a Mac the **Check for App Updates**
button says so instead. Macs update by installing a new `.dmg`: send the Release
link and the new app is dragged over the old one in Applications.

This is the main known gap - see below.

## Retiring Google Drive - done, for the record

**This has already happened.** 2.1.0 was the hand-over release: it shipped with
the GitHub updater inside it and reached the old copies through the mechanism they
already understood. Releases from 2.1.1 onward need none of this - just tag and
publish, as described at the top of this file.

One thing to keep: **leave `update.json` on Drive.** A copy still on 2.0.0 that
has not been launched since the hop needs it to make the jump, and deleting it
would strand that copy on 2.0.0 permanently. It costs nothing to keep.

### Why almost nothing gets uploaded

The old copies download whichever ZIP the Drive manifest names - and that URL may
point anywhere. A GitHub Release asset is a plain HTTPS link. So the manifest for
this release points straight at the ZIP attached to the GitHub Release, and the
old app downloads the new build **from GitHub**.

The only thing that goes to Drive is the manifest itself: about 120 bytes.

### Doing it

1. **Push** and publish a Release with the tag `v2.1.0` (version.py is already
   bumped to match).
2. When the build finishes, download **`update.json`** from that Release. The
   workflow writes and attaches it for exactly this purpose.
3. In Google Drive, open the `update.json` already sitting there, right-click →
   **Manage versions** → **Upload new version** → pick the file you downloaded.
4. That is all. Every installed copy checks that Drive file at startup, sees
   2.1.0, downloads the new build from GitHub and installs it.

Do not create a *new* file and do not rename anything: the old copies look up one
fixed file ID, and replacing the *contents* is what preserves it.

### What the file contains

```json
{
    "version": "2.1.0",
    "download_url": "https://github.com/bruhdreamyxr-alt/path/releases/download/v2.1.0/UniversalAudioStudio_2.1.0_update.zip"
}
```

To eyeball it without building anything:

```bash
python tools/make_update_manifest.py --release-dir dist \
    --repo bruhdreamyxr-alt/path --tag v2.1.0 --print
```

### Afterwards

Drive is finished. Delete the old ZIP from it if you like - nothing reads it
again. Every release from now on reaches everyone through the Release alone.

### The version number is load-bearing

The old updater only accepts a version **strictly greater** than the one it is
running, and everything out there is 2.0.0. So this transition release must be
2.1.0 or higher; shipped as another 2.0.0 it would be ignored by every copy,
silently, and you would be left wondering why nobody updated.
`tests/test_update_manifest.py` fails if that ever regresses.

## Building locally instead (optional)

With this in place you never *need* to build locally; the cloud build is the
release. But both platforms can still be built on their own machine.

**Windows** - the same script the CI Windows job runs, so the output is
identical:

```powershell
pwsh -File tools\build_windows.ps1
```

It produces:

```
dist\UniversalAudioStudio\UniversalAudioStudio.exe     the app
dist\UniversalAudioStudio_<version>_update.zip         self-update payload
mysetup<versiondigits>.exe                             installer
```

Useful switches: `-SkipTests` (quick iteration), `-SkipDeps` (reuse the current
environment), `-Python <path>`, `-Iscc <path>`.

**macOS**:

```bash
bash tools/build_macos.sh
```

## What each piece is for

| File | Purpose |
|---|---|
| `version.py` | The single source of truth for the version. Artifact names, the installer and the comparison the updater performs all derive from it. |
| `.github/workflows/release.yml` | Builds macOS (arm64 + x86_64) and Windows, then publishes one Release. |
| `tools/build_windows.ps1` | The local *and* CI Windows build. |
| `installer_200.iss` | Inno Setup script. Version and output name are injected with `/D` from `version.py`, so the installer cannot drift. |
| `tools/fetch_win_helpers.ps1` | Downloads `ffmpeg.exe` / `ffprobe.exe` / `yt-dlp.exe` / `aria2c.exe`, which are too large to commit. |
| `tools/fetch_mac_helpers.sh` | The macOS equivalent, fetching Mach-O builds. |
| `_make_update_package.py` | Zips the built app for the in-app updater. |
| `tools/make_update_manifest.py` | Writes the legacy Drive manifest, for the one transition release that retires Drive. |
| `updater.py` | Reads the newest Release, downloads it and applies it. |

## Known gaps

* **Mac users are not told about updates.** The app could check the same
  Releases API on macOS and offer to open the download page; it does not yet, so
  you have to tell them yourself.
* **`installer_config.iss`** is a leftover from Inno Setup's wizard (it produces
  `mysetup.exe`). Nothing uses it; `installer_200.iss` is the real one.
* **`flownet.bin` / `flownet.param`** (10.4 MB) are committed but referenced by
  nothing.

## If the build fails

* **Inno Setup missing** - the workflow installs it with Chocolatey, and
  `build_windows.ps1` looks for `ISCC.exe` in both Program Files locations, then
  on `PATH`. Pass `-Iscc <path>` to override.
* **A dependency with no wheel** - a plain pip failure naming the package. Every
  pin currently resolves to a wheel on CPython 3.14 / win_amd64.
* **A helper download failing** - the run gets an `::error::` annotation naming
  the mirror that failed. `tools/fetch_win_helpers.ps1` prints every URL it uses
  and verifies each binary by running it.
* **A version mismatch** - the release job refuses to publish and says whether
  to fix the tag or `version.py`.

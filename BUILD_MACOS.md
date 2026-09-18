# Building the macOS version (.dmg)

**There is no `.dmg` in this folder, and none can be created on Windows.**
A Mac build must be compiled *on macOS*:

* PyInstaller only builds for the operating system it runs on (no cross-compiling).
* The bundled helpers in this folder (`ffmpeg.exe`, `ffprobe.exe`, `yt-dlp.exe`,
  `aria2c.exe`) are Windows binaries. macOS needs Mach-O ones, so the Mac build
  downloads its own copies (see `tools/fetch_mac_helpers.sh`).

You have two ways to get the `.dmg`. Route 1 needs **no Mac at all**.

---

## Route 1 (recommended): build it on GitHub's Mac servers, free

GitHub gives you real macOS machines for free. This produces the `.dmg` and you
just download it.

### Step 1 - Put this folder on GitHub (using the GUI, no command line)

This folder is **already set up as a Git repository**: the GitHub remote is
configured and three commits are waiting to upload. There is nothing to create -
you only have to push.

1. Install **GitHub Desktop** from <https://desktop.github.com> if you don't
   have it, then open it and sign in as **bruhdreamyxr-alt** (the account that
   owns the repository).
2. Menu **File -> Add local repository...**
3. Paste the path (it is already on your clipboard):
   `C:\Users\antho\Downloads\scripts\path`
4. GitHub Desktop lists the repository with **3 commits to push**.
5. Click **Push origin** at the top. The upload takes a minute or two.

> The `.gitignore` file in this folder is what makes this upload possible. It
> excludes the huge Windows `.exe` files - `ffmpeg.exe` alone is 222 MB, and
> GitHub rejects any single file over 100 MB. Only 29 files (~11 MB) are sent.

### Step 2 - Run the Mac build

1. Go to <https://github.com> and open your new repository.
2. Click the **Actions** tab. (If it asks you to enable workflows, click the
   button to enable them.)
3. In the left sidebar click **Build macOS app**.
4. On the right, click **Run workflow** -> then the green **Run workflow** button.
5. Wait. It takes about 10-15 minutes. Refresh the page to see progress.
   A green tick means it worked.
6. Click on the finished run, scroll to the bottom, and download the artifact
   named **UniversalAudioStudio-macOS-dmg**. It arrives as a `.zip` -
   unzip it and you have your `UniversalAudioStudio-2.0.0.dmg`.

### Step 3 - Give it to your friend

The `.dmg` is roughly 250-350 MB. Upload it to Google Drive (the same way you
share the Windows update zip) and send them the link.

---

## Route 2: you or your friend already has a Mac

On the Mac, in a terminal:

```bash
# one-off: install Python and the toolchain
xcode-select --install          # click Install when prompted
brew install python@3.12        # https://brew.sh if you don't have brew

# then, from inside this project folder:
bash tools/build_macos.sh
```

That single script installs the dependencies, downloads the macOS helper
binaries, runs the test suite, builds the app, signs it, and produces:

```
dist/UniversalAudioStudio.app
dist/UniversalAudioStudio-2.0.0.dmg   <-- send this one
```

---

## What your friend sees on first launch

The app is **ad-hoc signed but not notarised** (notarisation costs an Apple
Developer certificate, ~$99/year), so macOS will refuse a normal double-click
the first time. It is not a virus - it's macOS being strict about unknown
developers.

Tell them to do this **once**:

1. Open the `.dmg`, drag **UniversalAudioStudio** onto **Applications**.
2. Open **Applications** in Finder, **right-click** the app -> **Open**.
3. A dialog appears - click **Open** again.

After that it launches normally forever. (Terminal alternative:
`xattr -dr com.apple.quarantine /Applications/UniversalAudioStudio.app`)

---

## Notes and gotchas

* **Private repos bill macOS minutes at a 10x multiplier.** GitHub's free tier
  includes 2,000 minutes/month, so a private repo affords roughly 20 Mac builds
  a month at ~10 minutes each. Public repos are free and unmetered.
* **Do not delete the `tools/` folder or `UniversalAudioStudio_mac.spec`** -
  the cloud build needs them.
* **The build scripts must stay bash 3.2 compatible.** macOS ships bash 3.2 as
  `/bin/bash`, and so do the GitHub macOS runners (they report
  "Bash 3.2.57(1)-release"). bash 3.2 **cannot parse a here-document nested
  inside a command substitution** - it reads to EOF hunting for the matching
  `)`, then dies with `unexpected EOF while looking for matching ')'` and exit
  code 2, without naming the real problem. That is exactly how the first cloud
  build failed. `tests/test_build_tooling.py` now fails if a here-document is
  reintroduced, and the version is read via `tools/get_version.py` instead.
* **aria2c is deliberately not bundled on Mac.** No static macOS build exists,
  and Homebrew's version depends on `/opt/homebrew` libraries that won't exist
  on your friend's Mac. Downloads fall back to yt-dlp's built-in downloader:
  slightly slower, completely fine.
* **The in-app updater is disabled on Mac.** It works by replacing a running
  `.exe` and asking for UAC elevation, which is a Windows-only mechanism. On Mac
  the button explains this; updates mean downloading a new `.dmg`.
* **On Mac, app data lives in `~/Library/Application Support/AudioDownloader`.**
  History, the download queue, UI preferences and the artwork cache all go
  there. The app must never write inside its own `.app` bundle: Gatekeeper runs
  a downloaded app from a read-only translocated copy, and writing into the
  bundle also invalidates the ad-hoc signature. (`APPDATA` and `LOCALAPPDATA`
  do not exist on macOS, which is why the lookup needs the macOS branch in
  `download_queue._user_data_base` / `downloader._user_data_base`.)

---

## If the cloud build fails

The macOS-only steps have never actually been executed - that needs a real Mac.
The test suite does run as step 3 of the build (48 tests, including simulated
macOS checks for the data paths above), so a failure there aborts before the
build. If the first run fails, open the failed step to see the error. The most
likely culprits:

* a dependency in `requirements.txt` lacking a wheel for the runner's Python,
* a helper download URL having moved (`tools/fetch_mac_helpers.sh` prints each
  URL it uses and verifies `--version` on every binary it fetches).

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

1. Download **GitHub Desktop** from <https://desktop.github.com> and install it.
2. Open it and sign in. If you don't have an account, choose **Create your free
   account** and make one.
3. Menu **File -> Add local repository...**
4. For the path, paste:
   `C:\Users\antho\Downloads\scripts\path`
5. GitHub Desktop will say this folder isn't a Git repository yet - click
   **create a repository** here.
6. In the box that appears, leave the name as is and click **Create repository**.
7. Click **Publish repository** at the top.
   * Keep **Keep this code private** ticked if you only want to build for
     yourself - you'll download the `.dmg` and send it to your friend yourself.
   * Untick it if you want a public download link for your friend.
8. Click **Publish repository**. The upload takes a few minutes.

> The `.gitignore` file already in this folder is what makes this upload
> possible. It excludes the huge Windows `.exe` files - `ffmpeg.exe` alone is
> 222 MB, and GitHub rejects any single file over 100 MB.

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
* **aria2c is deliberately not bundled on Mac.** No static macOS build exists,
  and Homebrew's version depends on `/opt/homebrew` libraries that won't exist
  on your friend's Mac. Downloads fall back to yt-dlp's built-in downloader:
  slightly slower, completely fine.
* **The in-app updater is disabled on Mac.** It works by replacing a running
  `.exe` and asking for UAC elevation, which is a Windows-only mechanism. On Mac
  the button explains this; updates mean downloading a new `.dmg`.

---

## If the cloud build fails

The workflow and the shell scripts have only been checked by reading them -
they have never been executed, because that requires a Mac. If the first run
fails, open the failed step to see the error, and fix or send the log. The most
likely culprits:

* a dependency in `requirements.txt` lacking a wheel for the runner's Python,
* a helper download URL having moved (`tools/fetch_mac_helpers.sh` prints each
  URL it uses and verifies `--version` on every binary it fetches).

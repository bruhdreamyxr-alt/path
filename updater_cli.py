"""Standalone updater subprocess (compiled separately to ``updater_cli.exe``).

Usage::

    updater_cli.exe <old_exe> <new_package> <parent_pid> [restart_exe] [arg1 arg2 ...]

Waits for *parent_pid* (the main app) to exit, then installs *new_package*:

* ``.zip``  - a full release package (main EXE + ``_internal\\`` + extras).
              Extracted over the app's install directory (replaces everything).
* any other - a single EXE. Atomically replaces ``old_exe``.

If ``restart_exe`` is provided the app is relaunched with the optional
command-line args.

Behaviour notes:

* Everything is logged to ``%TEMP%\\updater_cli.log`` — this build is a
  ``--noconsole`` executable, so plain print() output would be lost.
* If the install directory refuses writes (e.g. ``C:\\Program Files``) and
  the updater is not elevated, it relaunches itself with admin rights via
  the standard UAC prompt before doing any work.
* When the updater itself runs elevated, the app is relaunched through
  ``explorer.exe`` so it does not inherit admin rights.
"""

import os
import shutil
import subprocess
import sys
import time
import zipfile

LOG_PATH = os.path.join(
    os.environ.get("TEMP", os.path.expanduser("~")), "updater_cli.log"
)


def _log(message: str) -> None:
    """Print *message* and append it to the shared log file."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _is_elevated() -> bool:
    """Return True when running with admin rights."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _dir_is_writable(directory: str) -> bool:
    """Return True when *directory* accepts file writes for this process."""
    probe = os.path.join(directory, "_update_probe.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        os.remove(probe)
        return True
    except OSError:
        return False


def _self_elevate_and_exit():
    """Relaunch this updater with admin rights (UAC prompt), then exit.

    Used when the install directory refuses writes and the updater was
    started non-elevated. The elevated instance re-runs the whole flow.
    """
    if os.name != "nt":
        _log("ERROR: Cannot elevate on this platform.")
        sys.exit(4)
    import ctypes

    params = subprocess.list2cmdline(sys.argv[1:])
    _log(f"Requesting elevation for: {sys.executable} {params}")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1  # SW_SHOWNORMAL
    )
    if int(ret) > 32:
        _log("Elevated instance launched; handing off.")
        sys.exit(0)
    _log(f"ERROR: Elevation declined or failed (ShellExecuteW returned {ret}).")
    sys.exit(4)


def _extract_zip_into(package_path: str, app_dir: str):
    """Extract a release ZIP over *app_dir*, replacing files in place.

    Refuses to write any member outside *app_dir* (zip-slip guard). Retries
    briefly on locked files (e.g. the still-running exe on some systems).
    """
    app_dir = os.path.normpath(os.path.abspath(app_dir))
    with zipfile.ZipFile(package_path, "r") as zf:
        members = zf.infolist()
        _log(f"Installing {len(members)} package entries into {app_dir}")
        for member in members:
            target = os.path.normpath(os.path.join(app_dir, member.filename))
            if target != app_dir and not target.startswith(app_dir + os.sep):
                _log(f"Refusing to extract member outside app dir: {member.filename}")
                continue
            if member.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            for attempt in range(5):
                try:
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    break
                except (PermissionError, OSError) as e:
                    _log(f"Extract attempt {attempt + 1} failed for {member.filename}: {e}")
                    time.sleep(1)
            else:
                _log(f"ERROR: Could not write {member.filename}")
                sys.exit(2)


def _wait_for_process_exit(pid, timeout=10):
    """Wait until the process with *pid* no longer exists."""
    if not pid:
        return
    try:
        import psutil  # optional, lighter fallback below
        try:
            p = psutil.Process(int(pid))
            p.wait(timeout=timeout)
        except ValueError:
            pass  # process already gone
        except psutil.TimeoutExpired:
            pass
        except Exception:
            pass
        return
    except ImportError:
        pass

    # Fallback: poll /proc or tasklist
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return

    if os.name == "nt":
        for _ in range(timeout * 10):
            try:
                import ctypes
                SYNCHRONIZE = 0x100000
                handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                if handle == 0:
                    break
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                break
            time.sleep(0.1)
    else:
        for _ in range(timeout * 10):
            if not os.path.exists(f"/proc/{pid}"):
                break
            time.sleep(0.1)


def _relaunch_app(restart_exe: str, restart_args) -> bool:
    """Start the updated app again.

    When this updater runs elevated, relaunch through ``explorer.exe`` so
    the app itself does not inherit admin rights. The explorer route only
    supports a bare exe (no args); with args we start it directly.
    """
    try:
        if os.name == "nt" and not restart_args and _is_elevated():
            _log("Elevated updater: restarting app de-elevated via explorer.exe.")
            subprocess.Popen(["explorer.exe", os.path.normpath(restart_exe)], close_fds=True)
        else:
            subprocess.Popen([restart_exe, *restart_args], close_fds=True)
        return True
    except Exception as e:
        _log(f"Update complete but failed to restart: {e}")
        return False


def main():
    if len(sys.argv) < 4:
        _log("Usage: updater_cli.exe <old_exe> <new_package> <parent_pid> [restart_exe] [args...]")
        sys.exit(1)

    _log(f"updater_cli started; argv={sys.argv[1:]}")

    old_exe = sys.argv[1]
    new_package = sys.argv[2]

    try:
        parent_pid = int(sys.argv[3])
    except ValueError:
        parent_pid = 0

    restart_exe = sys.argv[4] if len(sys.argv) > 4 else None
    restart_args = sys.argv[5:] if len(sys.argv) > 5 else []

    app_dir = os.path.dirname(os.path.abspath(old_exe))

    # If the install dir refuses writes and we are not elevated, hand off to
    # an elevated copy of ourselves (UAC). That instance redoes this whole
    # flow with admin rights.
    if not _dir_is_writable(app_dir) and not _is_elevated():
        _log(f"Install dir not writable for this process: {app_dir}")
        _self_elevate_and_exit()  # exits

    _log("Waiting for the main app to exit...")
    time.sleep(1)
    _wait_for_process_exit(parent_pid, timeout=10)

    if zipfile.is_zipfile(new_package):
        _log("Installing full update package...")
        _extract_zip_into(new_package, app_dir)
    else:
        # Single-EXE replacement (works for onefile builds).
        _log("Installing single-EXE update...")
        for attempt in range(5):
            try:
                shutil.move(new_package, old_exe)
                break
            except (PermissionError, OSError) as e:
                _log(f"Replace attempt {attempt + 1} failed: {e}")
                time.sleep(1)
        else:
            _log("ERROR: Could not replace the application EXE.")
            sys.exit(2)

    _log("Update installed successfully.")

    if restart_exe:
        if _relaunch_app(restart_exe, restart_args):
            _log(f"App relaunched: {restart_exe}")
        else:
            sys.exit(3)

    _log("updater_cli finished OK.")


if __name__ == "__main__":
    main()

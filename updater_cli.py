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
import tempfile
import time
import zipfile

LOG_PATH = os.path.join(
    os.environ.get("TEMP", os.path.expanduser("~")), "updater_cli.log"
)

# Set on the relocated copy of this updater so it never relocates itself again.
_RELOCATED_ENV = "UAS_UPDATER_RELOCATED"


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


def _copy_member(zf, member, target):
    """Write one zip member to *target*. Return an error string, or ``None``.

    A short write is treated as a failure: a truncated DLL or EXE would leave
    an app that cannot start, and that is far worse than retrying.
    """
    try:
        with zf.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    except (PermissionError, OSError) as e:
        return str(e)

    try:
        written = os.path.getsize(target)
    except OSError as e:
        return str(e)
    if member.file_size and written != member.file_size:
        return f"short write ({written} of {member.file_size} bytes)"
    return None


def _relocate_if_inside_app(app_dir: str) -> bool:
    """Re-run this updater from outside *app_dir*, and report whether it did.

    A safety net for the case where something launches the copy inside the
    install directly. Windows will not replace a running EXE, and it resolves a
    process's DLLs from that process's own directory, so an updater running from
    ``_internal`` keeps ``updater_cli.exe``, ``python3.dll`` and both
    ``VCRUNTIME140*.dll`` locked for as long as it runs - the update finishes as
    "UPDATE INCOMPLETE" and the updater itself never improves.

    The app also relocates the helper before launching it (see
    ``updater._prepare_updater_helper``), so in practice this rarely fires; it
    exists so the behaviour does not depend on the app being up to date.

    Returns True when a relocated copy has been started, in which case the
    caller should exit without doing any work.
    """
    if os.environ.get(_RELOCATED_ENV) == "1":
        return False  # already relocated once; never loop

    try:
        running = os.path.normpath(os.path.abspath(sys.executable))
        app_dir = os.path.normpath(os.path.abspath(app_dir))
    except Exception:
        return False

    if not running.lower().startswith(app_dir.lower() + os.sep):
        return False

    try:
        temp_dir = tempfile.mkdtemp(prefix="uas-updater-")
        relocated = os.path.join(temp_dir, os.path.basename(running) or "updater_cli.exe")
        shutil.copy2(running, relocated)
        env = dict(os.environ)
        env[_RELOCATED_ENV] = "1"
        subprocess.Popen([relocated, *sys.argv[1:]], close_fds=True, env=env)
    except OSError as e:
        _log(f"Could not relocate out of {app_dir} ({e}); continuing in place")
        return False

    _log(f"Relocated to {relocated} so the files in {app_dir} can be replaced")
    return True


def _extract_zip_into(package_path: str, app_dir: str, retry_seconds: int = 120):
    """Extract a release ZIP over *app_dir*, replacing files in place.

    Refuses to write any member outside *app_dir* (zip-slip guard).

    Returns True only when every member was written and verified.

    Locked files are the normal case, not an exceptional one: the previous
    process may still be releasing its DLLs, and virus scanners routinely hold
    a freshly written binary for a few seconds. So a file that cannot be
    written is set aside and everything else still gets installed; the failures
    are then retried for up to *retry_seconds*.

    An earlier version gave a file five one-second attempts and then called
    sys.exit, which abandoned the entire rest of the package. In the wild that
    left an app with a new main EXE but the old _internal beside it - and if the
    lock had landed one file later, with a new _tkinter.pyd and no matching
    tcl90.dll, the app would not have started at all.
    """
    app_dir = os.path.normpath(os.path.abspath(app_dir))
    with zipfile.ZipFile(package_path, "r") as zf:
        members = zf.infolist()
        _log(f"Installing {len(members)} package entries into {app_dir}")

        written = 0
        pending = []
        for member in members:
            target = os.path.normpath(os.path.join(app_dir, member.filename))
            if target != app_dir and not target.startswith(app_dir + os.sep):
                _log(f"Refusing to extract member outside app dir: {member.filename}")
                continue
            if member.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)

            error = _copy_member(zf, member, target)
            if error is None:
                written += 1
            else:
                # Keep going: one locked file must not stop the other 160.
                _log(f"Locked, will retry: {member.filename} ({error})")
                pending.append((member, target, error))

        deadline = time.time() + max(0, retry_seconds)
        pass_number = 0
        while pending and time.time() < deadline:
            pass_number += 1
            time.sleep(1)
            still_pending = []
            for member, target, error in pending:
                new_error = _copy_member(zf, member, target)
                if new_error is None:
                    written += 1
                    _log(f"Retry pass {pass_number}: wrote {member.filename}")
                else:
                    still_pending.append((member, target, new_error))
            if still_pending:
                _log(f"Retry pass {pass_number}: {len(still_pending)} file(s) still locked")
            pending = still_pending

        if pending:
            for member, target, error in pending:
                _log(f"ERROR: could not write {member.filename}: {error}")
            _log(f"UPDATE INCOMPLETE: {written} written, {len(pending)} failed")
            return False

    _log(f"All {written} files written and verified")
    return True


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

    # Never work from inside the directory being replaced - see
    # _relocate_if_inside_app. A relocated copy repeats everything below.
    if _relocate_if_inside_app(app_dir):
        sys.exit(0)

    # If the install dir refuses writes and we are not elevated, hand off to
    # an elevated copy of ourselves (UAC). That instance redoes this whole
    # flow with admin rights.
    if not _dir_is_writable(app_dir) and not _is_elevated():
        _log(f"Install dir not writable for this process: {app_dir}")
        _self_elevate_and_exit()  # exits

    _log("Waiting for the main app to exit...")
    time.sleep(1)
    _wait_for_process_exit(parent_pid, timeout=10)

    update_complete = True
    if zipfile.is_zipfile(new_package):
        _log("Installing full update package...")
        update_complete = _extract_zip_into(new_package, app_dir)
    else:
        # Single-EXE replacement (works for onefile builds). Same patience as
        # the package path: a locked EXE is usually a release delay, not a
        # permanent failure.
        _log("Installing single-EXE update...")
        update_complete = False
        deadline = time.time() + 120
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                shutil.move(new_package, old_exe)
                update_complete = True
                break
            except (PermissionError, OSError) as e:
                _log(f"Replace attempt {attempt} failed: {e}")
                time.sleep(2)

    if update_complete:
        _log("Update installed successfully.")
    else:
        # Relaunch anyway: a partly written install usually still runs, and
        # leaving the user with nothing to click would be worse. The log is the
        # record, and running the installer is the repair.
        _log("UPDATE INCOMPLETE - relaunching anyway. If the app does not start, "
             "run the installer again.")

    if restart_exe:
        if _relaunch_app(restart_exe, restart_args):
            _log(f"App relaunched: {restart_exe}")
        else:
            sys.exit(3)

    _log("updater_cli finished OK.")
    if not update_complete:
        sys.exit(2)


if __name__ == "__main__":
    main()

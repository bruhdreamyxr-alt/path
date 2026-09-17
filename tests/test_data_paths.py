"""Regression tests for where the app stores runtime data (history/queue).

The packaged app is installed under ``C:\\Program Files`` by the Inno Setup
installer. That directory is read-only for standard users, so if history is
written next to the executable the write fails and the History tab is always
empty. These tests pin the behaviour that fixed that bug:

  * frozen builds resolve the data dir to a writable per-user location
  * existing history/queue from an older install location is migrated once
  * clearing history is not undone by a re-import on the next launch
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD_SCRIPT = textwrap.dedent(
    '''
    import json, os, sys
    sys.frozen = True
    sys.executable = os.path.join(os.environ["LEGACY_DIR"], "UniversalAudioStudio.exe")
    sys._MEIPASS = os.path.join(os.environ["LEGACY_DIR"], "_internal")
    import download_queue as dq
    import downloader

    phase = sys.argv[1]
    obj = dq.DownloadQueue(None, None, None, None)

    if phase == "first":
        print("DATA_DIR=" + dq.APP_DATA_DIR)
        print("HISTORY_FILE=" + dq.HISTORY_FILE)
        print("HISTORY=" + json.dumps(obj.get_history()))
        print("QUEUE=" + json.dumps(dq.load_pending_queue()))
        obj.upsert_history_entry(
            {"url": "https://new", "title": "New Song", "status": "done"}
        )
        print("COUNT_AFTER_UPSERT=" + str(len(obj.get_history())))
        print("FILE_EXISTS=" + str(os.path.isfile(dq.HISTORY_FILE)))
        # Artwork cache must also avoid the read-only install directory.
        user_dir = downloader._get_user_data_dir()
        print("DOWNLOADER_USER_DIR=" + user_dir)
        cache_dir = os.path.join(user_dir, ".artwork_cache")
        os.makedirs(cache_dir, exist_ok=True)
        print("CACHE_WRITABLE=" + str(os.path.isdir(cache_dir)))
        print("YTDLP_EXE=" + str(downloader._get_ytdlp_exe()))
    elif phase == "clear":
        obj.clear_history()
        print("COUNT_AFTER_CLEAR=" + str(len(obj.get_history())))
    elif phase == "reopen":
        print("COUNT_AFTER_REOPEN=" + str(len(obj.get_history())))
    '''
)


class DataPathTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="tunelab_datapath_")
        self.legacy = os.path.join(self.base, "ProgramFiles", "AudioDownloader")
        self.appdata = os.path.join(self.base, "AppData")
        os.makedirs(self.legacy)
        os.makedirs(self.appdata)

        self._write(self.legacy, "download_history.json",
                    [{"url": "https://legacy", "title": "Legacy Song",
                      "status": "done"}])
        self._write(self.legacy, "download_queue.json",
                    [{"url": "https://legacyq", "is_video": False,
                      "status": "pending"}])

        self.script = os.path.join(self.base, "child.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(_CHILD_SCRIPT)

    @staticmethod
    def _write(directory, name, payload):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _run(self, phase):
        env = dict(os.environ)
        env["LEGACY_DIR"] = self.legacy
        env["APPDATA"] = self.appdata
        env["LOCALAPPDATA"] = self.appdata
        env["PYTHONPATH"] = PROJECT_DIR
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, self.script, phase],
            capture_output=True, text=True, env=env, cwd=PROJECT_DIR,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out[key] = value
        return out

    def test_data_dir_is_writable_per_user_location(self):
        out = self._run("first")
        expected = os.path.join(self.appdata, "AudioDownloader")
        self.assertEqual(os.path.abspath(out["DATA_DIR"]).lower(),
                         os.path.abspath(expected).lower())
        self.assertEqual(os.path.abspath(os.path.dirname(out["HISTORY_FILE"])).lower(),
                         os.path.abspath(expected).lower())
        # Not buried in the read-only install directory.
        self.assertNotIn(os.path.abspath(self.legacy).lower(),
                         os.path.abspath(out["HISTORY_FILE"]).lower())

    def test_writes_succeed_and_are_readable(self):
        out = self._run("first")
        self.assertEqual(out["FILE_EXISTS"], "True")
        # 1 migrated + 1 upserted
        self.assertEqual(out["COUNT_AFTER_UPSERT"], "2")

    def test_legacy_history_and_queue_are_migrated(self):
        out = self._run("first")
        history = json.loads(out["HISTORY"])
        queue = json.loads(out["QUEUE"])
        self.assertEqual([e["url"] for e in history], ["https://legacy"])
        self.assertEqual([e["url"] for e in queue], ["https://legacyq"])

    def test_cleared_history_is_not_reimported(self):
        self._run("first")
        cleared = self._run("clear")
        self.assertEqual(cleared["COUNT_AFTER_CLEAR"], "0")
        reopened = self._run("reopen")
        self.assertEqual(reopened["COUNT_AFTER_REOPEN"], "0")

    def test_artwork_cache_dir_is_writable(self):
        """Same bug class: the artwork cache used to target the install dir."""
        out = self._run("first")
        expected = os.path.join(self.appdata, "AudioDownloader")
        self.assertEqual(os.path.abspath(out["DOWNLOADER_USER_DIR"]).lower(),
                         os.path.abspath(expected).lower())
        self.assertEqual(out["CACHE_WRITABLE"], "True")
        self.assertNotIn(os.path.abspath(self.legacy).lower(),
                         os.path.abspath(out["DOWNLOADER_USER_DIR"]).lower())


if __name__ == "__main__":
    unittest.main()

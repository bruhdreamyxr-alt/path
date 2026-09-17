import unittest

import downloader


class ErrorSanitizationTests(unittest.TestCase):
    def test_strip_ansi_removes_terminal_escape_sequences(self):
        raw = "\x1b[0;31mERROR: [Instagram] Private post\x1b[0m"
        self.assertEqual(downloader._strip_ansi(raw), "ERROR: [Instagram] Private post")

    def test_strip_ansi_keeps_plain_text_unchanged(self):
        raw = "MP4 Error: Could not download from Instagram."
        self.assertEqual(downloader._strip_ansi(raw), raw)


if __name__ == "__main__":
    unittest.main()

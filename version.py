"""Application version metadata for self-update checks."""
# 2.1.0 exists to retire Google Drive: copies of 2.0.0 and earlier only accept an
# update whose version is *strictly greater* than their own, so the release that
# hands them the GitHub-based updater has to be a higher number than 2.0.0.

__version__ = "2.1.0"
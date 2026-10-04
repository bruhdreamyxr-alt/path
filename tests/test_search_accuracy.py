"""The search has to return the song that was asked for.

Every test here is offline: the iTunes catalogue and the SoundCloud page are
stubbed, because the failure this pins down is not "the network was down", it is
"the lookup answered and the answer was believed".

The bug: ``_canonical_metadata`` scored a *title* match at 0.7 and the artist at
0.3, so a catalogue entry for a completely different recording was accepted and
overwrote good metadata. Asking for Alex G's "Mary" turned the query into
"thepianokid Mary (Piano Version)" - iTunes' answer for that album - and the
search then faithfully downloaded a piano cover.
"""
import unittest
from unittest import mock

import downloader as d

SC_URL = ("https://soundcloud.com/alexg232/mary?si=d815e4b8b8a34b06b92f685f90652643"
          "&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing")

SC_PAGE = (
    '<html><head>'
    '<meta property="og:title" content="Mary">'
    '<meta property="og:description" content="Listen to Mary by Alex G #np on '
    'SoundCloud">'
    '</head><body></body></html>'
)


def itunes(*results):
    return {"results": list(results)}


def song(track, artist, album="Album", ms=205000, art="http://x/100x100.jpg"):
    return {"trackName": track, "artistName": artist, "collectionName": album,
            "primaryGenreName": "Alternative",
            "releaseDate": "2024-01-01T00:00:00Z",
            "trackTimeMillis": ms, "artworkUrl100": art}


class FakeResponse:
    def __init__(self, text):
        self._text = text

    def read(self, _n=0):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def lookup(artist, title, catalogue):
    with mock.patch.object(d, "_http_get_json", return_value=catalogue):
        return d._canonical_metadata(artist, title)


def accepted(artist, title, catalogue):
    """The metadata that *was* accepted, failing the test if it was refused."""
    meta = lookup(artist, title, catalogue)
    if meta is None:
        raise AssertionError(f"{artist!r} / {title!r} was rejected")
    return meta


def picked(entries, tokens):
    """The entry that was chosen, failing the test if nothing was."""
    best = d._pick_best_entry(entries, tokens)
    if best is None:
        raise AssertionError(f"no pick out of {[e.get('title') for e in entries]}")
    return best


class CanonicalMetadataTests(unittest.TestCase):
    """The catalogue may correct a spelling. It may not change the song."""

    def test_a_different_artist_in_the_catalogue_is_not_canonical(self):
        # What iTunes actually answers for "Alex G Mary": a tribute album.
        self.assertIsNone(lookup("Alex G", "Mary", itunes(
            song("Mary (Piano Version)", "thepianokid",
                 "Piano Covers Tribute to Alex G - EP"))))

    def test_a_title_only_lookup_never_guesses_the_song(self):
        # "Mary" is a hundred different recordings; the catalogue would happily
        # return one, which is how an unrelated track got downloaded.
        self.assertIsNone(lookup("", "Mary", itunes(
            song("Mary", "Big Thief", "Capacity"))))
        self.assertIsNone(lookup("", "Roygbiv", itunes(
            song("Roygbiv", "Boards of Canada"))))

    def test_a_mix_the_caller_did_not_ask_for_is_rejected(self):
        # Same artist, same song, but the catalogue hands back the remix.
        self.assertIsNone(lookup("The Weeknd", "Blinding Lights", itunes(
            song("Blinding Lights (Remix)", "The Weeknd & ROSALA",
                 "Blinding Lights (Remix) - Single"))))

    def test_a_version_the_caller_did_ask_for_is_not_re_attributed(self):
        # The user wants the piano version - but the catalogue must not be
        # allowed to swap in a different act's version of it either.
        self.assertIsNone(lookup("Alex G", "Mary (Piano Version)", itunes(
            song("Mary (Piano Version)", "thepianokid",
                 "Piano Covers Tribute to Alex G - EP"))))

    def test_the_artist_alone_does_not_make_a_different_track_canonical(self):
        # An artist has a whole catalogue: sharing the artist and one word is
        # not the same song. (iTunes really does answer this with "Headroom
        # Piano", from a different album.)
        self.assertIsNone(lookup("Alex G", "Mary (Piano Version)", itunes(
            song("Headroom Piano", "Alex G", "God Save the Animals"))))

    def test_a_genuine_match_is_still_used(self):
        meta = accepted("Radiohead", "Karma Police", itunes(
            song("Karma Police", "Radiohead", "OK Computer", 264067)))
        self.assertEqual(meta["title"], "Karma Police")
        self.assertEqual(meta["album"], "OK Computer")
        self.assertAlmostEqual(meta["duration"], 264.067, places=2)
        self.assertIn("600x600", meta["artwork_url"])

    def test_a_collaboration_credit_does_not_leak_into_the_query(self):
        # iTunes spells the artist with a feature credit, which makes a worse
        # search query than the artist the user actually linked.
        meta = accepted("Fred again..", "adore u", itunes(
            song("adore u", "Fred again.. & Obongjayar", "adore u - Single")))
        self.assertEqual(meta["artist"], "Fred again..")

    def test_nothing_corrobates_so_nothing_is_returned(self):
        self.assertIsNone(lookup("Alex G", "Mary", None))
        self.assertIsNone(lookup("Alex G", "Mary", itunes()))


class SoundCloudQueryTests(unittest.TestCase):
    """The reported link, end to end and offline."""

    def _build(self, catalogue, page=SC_PAGE):
        # yt-dlp cannot extract a DRM-protected track, which is what sends this
        # path to the page scrape in the first place.
        with mock.patch.object(d, "yt_dlp", None), \
                mock.patch.object(d, "_http_get_json", return_value=catalogue), \
                mock.patch("urllib.request.urlopen",
                           return_value=FakeResponse(page)):
            return d._build_soundcloud_search_query(SC_URL)

    def test_the_query_keeps_the_songs_own_artist(self):
        query = self._build(itunes(
            song("Mary (Piano Version)", "thepianokid",
                 "Piano Covers Tribute to Alex G - EP")))
        self.assertEqual(query, "ytsearch1:Alex G Mary official audio")

    def test_the_query_is_right_even_with_no_catalogue_at_all(self):
        self.assertEqual(self._build(None),
                         "ytsearch1:Alex G Mary official audio")

    def test_a_real_catalogue_hit_still_improves_the_spelling(self):
        query = self._build(itunes(song("Mary", "Alex G", "US", 198000)),
                            page=SC_PAGE.replace("Mary", "mary"))
        self.assertEqual(query, "ytsearch1:Alex G Mary official audio")

    def test_the_generic_protected_link_path_uses_the_page_not_the_slug(self):
        # The other route into a YouTube search ("this link is protected, find
        # it") used to search the URL slug - and a SoundCloud handle is not an
        # artist, so YouTube returned whatever else shares the title.
        with mock.patch.object(d, "yt_dlp", None), \
                mock.patch.object(d, "_http_get_json", return_value=None), \
                mock.patch("urllib.request.urlopen",
                           return_value=FakeResponse(SC_PAGE)):
            self.assertEqual(d._soundcloud_fallback_text(SC_URL), "Alex G Mary")

    def test_that_path_ignores_links_it_cannot_help_with(self):
        # No page request at all for a link that is not SoundCloud's.
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("must not fetch")) as fetch:
            self.assertEqual(
                d._soundcloud_fallback_text("https://example.com/a/b"), "")
            self.assertEqual(
                d._soundcloud_fallback_text("https://youtube.com/watch?v=x"), "")
            self.assertEqual(fetch.call_count, 0)


class PickBestEntryTests(unittest.TestCase):
    """Scoring is relative, so it cannot catch "the only hit is a cover"."""

    def _entry(self, title, uploader="", vid="x", duration=200):
        return {"title": title, "uploader": uploader, "id": vid,
                "duration": duration}

    def test_a_lone_cover_is_refused_when_none_was_asked_for(self):
        entries = [self._entry("Alex G - Mary (Piano Cover)", "someone")]
        self.assertIsNone(d._pick_best_entry(entries, ["alex", "g", "mary"]))

    def test_a_cover_is_fine_when_the_query_asks_for_one(self):
        entries = [self._entry("Mary (Piano Version)", "thepianokid")]
        best = picked(entries, ["mary", "piano", "version"])
        self.assertEqual(best["title"], "Mary (Piano Version)")

    def test_the_original_wins_over_a_cover(self):
        entries = [self._entry("Mary (Piano Cover)", "coverer"),
                   self._entry("Alex G - Mary", "Alex G")]
        best = picked(entries, ["alex", "g", "mary"])
        self.assertEqual(best["title"], "Alex G - Mary")

    def test_the_token_coverage_gate_still_applies(self):
        entries = [self._entry("Completely Different Song", "nobody")]
        self.assertIsNone(d._pick_best_entry(entries, ["alex", "g", "mary"]))


class RefusalReasonTests(unittest.TestCase):
    """A search that never ran must not be reported as a spelling mistake.

    The reported symptom: searching "mary alex g" answered "Could not find any
    matching track. Please check the spelling and try again." The spelling was
    fine. yt-dlp had been turned away at the door ("Sign in to confirm you're
    not a bot"), the search never happened, and the message sent the user off to
    retype a title that was already correct. The wall answers identically for
    every track, so the fix is to say *which* wall was hit.
    """

    def test_no_refusal_for_a_plain_empty_result(self):
        # YouTube answered and nothing matched: the spelling hint is honest.
        self.assertEqual(d._youtube_refusal_reason(""), "")
        self.assertEqual(d._youtube_refusal_reason(None), "")
        self.assertEqual(
            d._youtube_refusal_reason("No matching YouTube track was found."), "")

    def test_the_bot_wall_is_named_and_not_blamed_on_spelling(self):
        for text in (
            "ERROR: Sign in to confirm you're not a bot. Use --cookies-from-browser",
            "Sign in to confirm you’re not a bot",
            "ERROR: [youtube] x: Sign in to confirm you're not a bot.",
        ):
            reason = d._youtube_refusal_reason(text)
            self.assertIn("not a bot", reason.lower())
            self.assertIn("not a spelling problem", reason.lower())
            # The one escape hatch that genuinely bypasses YouTube is offered.
            self.assertIn("soundcloud", reason.lower())

    def test_a_403_is_reported_as_a_refusal(self):
        reason = d._youtube_refusal_reason("ERROR: unable to download: HTTP Error 403: Forbidden")
        self.assertIn("403", reason)
        self.assertIn("not a spelling problem", reason.lower())

    def test_rate_limiting_is_reported_separately(self):
        reason = d._youtube_refusal_reason("HTTP Error 429: Too Many Requests")
        self.assertIn("429", reason)
        self.assertNotIn("403", reason)

    def test_a_real_status_code_is_still_recognised(self):
        self.assertIn("403", d._youtube_refusal_reason("HTTP Error 403: Forbidden"))
        self.assertIn("429", d._youtube_refusal_reason("HTTP Error 429: Too Many Requests"))

    def test_a_video_id_containing_403_is_not_a_403(self):
        # yt-dlp always embeds the video ID, so a plain substring test reads
        # this as a blocked request and blames the network for a dead track.
        reason = d._youtube_refusal_reason("ERROR: [youtube] 8sN4030kX: Video unavailable")
        self.assertEqual(reason, "")

    def test_a_video_id_containing_429_is_not_a_429(self):
        reason = d._youtube_refusal_reason("ERROR: [youtube] x4290abcdef: Video unavailable")
        self.assertEqual(reason, "")

    def test_the_message_survives_a_raw_exception_object(self):
        # download_track passes the caught exception itself, not a string.
        class DownloadError(Exception):
            pass

        reason = d._youtube_refusal_reason(
            DownloadError("ERROR: Sign in to confirm you're not a bot."))
        self.assertIn("not a bot", reason.lower())

    def test_a_search_that_merely_found_nothing_is_not_a_refusal(self):
        # A genuine network outage is not YouTube refusing; keep the old hint
        # rather than inventing a wall that was not hit.
        self.assertEqual(
            d._youtube_refusal_reason("getaddrinfo failed: Name or service not known"), "")


class SoundCloudFailureReasonTests(unittest.TestCase):
    """The direct-attempt failure has to reach the message the user finally sees.

    The reported track is DRM-protected on SoundCloud (yt-dlp really says
    "ERROR: [soundcloud] 1441844539: This video is DRM protected"), so the
    direct attempt can never succeed. Without saying so, the fallback's
    "check the spelling and try again" sends the user to retype a correct title
    and to retry a download that is impossible for anyone.
    """

    SC_LINK = "https://soundcloud.com/alexg232/mary"

    def setUp(self):
        # These tests deliberately record real refusals, and the cache is module
        # global: without this, one test's bot wall would make the next test
        # fail fast and assert against the wrong message.
        d.clear_network_block()

    def tearDown(self):
        d.clear_network_block()

    def _context(self, exc=None):
        """A stand-in for `with _build_youtube_dl(...) as ydl`."""
        cm = mock.MagicMock()
        cm.__enter__.return_value.download.side_effect = exc
        return cm

    def _run(self, *, direct_exc, search_exc=None, search_result=None,
             fallback_exc=None, raw_input=None):
        errors, statuses = [], []
        contexts = [self._context(direct_exc), self._context(fallback_exc)]
        search = (mock.Mock(side_effect=search_exc) if search_exc is not None
                  else mock.Mock(return_value=search_result))
        with mock.patch.object(d, "build_fast_yt_dlp_options", return_value={}), \
                mock.patch.object(d, "media_output_template", return_value="out.%(ext)s"), \
                mock.patch.object(d, "get_base_dir", return_value="."), \
                mock.patch.object(d, "_apply_platform_headers"), \
                mock.patch.object(d, "_mod_yt_dlp", return_value=object()), \
                mock.patch.object(d, "_snapshot_download_files", return_value=set()), \
                mock.patch.object(d, "_remove_new_media_files"), \
                mock.patch.object(d, "_report_mp3_artwork_status"), \
                mock.patch.object(d, "_build_youtube_dl", side_effect=contexts), \
                mock.patch.object(d, "_build_soundcloud_search_query",
                                  return_value="ytsearch1:Alex G Mary official audio"), \
                mock.patch.object(d, "_search_and_download_best", search):
            d.download_track(
                raw_input if raw_input is not None else self.SC_LINK,
                status_callback=lambda text, color: statuses.append(text),
                success_callback=lambda folder: None,
                error_callback=errors.append,
            )
        return errors, statuses

    def test_a_drm_track_plus_the_bot_wall_does_not_blame_the_spelling(self):
        errors, statuses = self._run(
            direct_exc=RuntimeError(
                "ERROR: [soundcloud] 1441844539: This video is DRM protected"),
            search_exc=RuntimeError(
                "ERROR: Sign in to confirm you're not a bot. Use --cookies"),
        )
        message = errors[-1]
        self.assertIn("DRM-protected", message)
        self.assertIn("not a bot", message.lower())
        self.assertNotIn("check the spelling", message.lower())
        self.assertTrue(any("DRM-protected" in s for s in statuses))

    def test_a_drm_track_is_explained_even_when_youtube_just_had_nothing(self):
        # YouTube answered and had nothing; the old hint is not wrong here, but
        # on its own it hides the real dead end: the SoundCloud copy is DRM.
        errors, _ = self._run(
            direct_exc=RuntimeError("ERROR: This video is DRM protected"),
            search_result=None,
            fallback_exc=RuntimeError("ERROR: [youtube:search] No video results"),
        )
        message = errors[-1]
        self.assertIn("check the spelling", message.lower())
        self.assertIn("DRM-protected", message)
        self.assertIn("SoundCloud", message)

    def test_a_plain_text_search_that_hits_the_bot_wall_says_so(self):
        # This is the reported search. `_search_and_download_best` swallows the
        # wall and returns None, so the reason only survives in download_error.
        # A non-URL search opens just one yt-dlp session, hence direct_exc.
        errors, _ = self._run(
            direct_exc=RuntimeError(
                "ERROR: [youtube] Sign in to confirm you're not a bot."),
            search_result=None,
            raw_input="mary alex g",
        )
        self.assertIn("not a bot", errors[-1].lower())
        self.assertNotIn("check the spelling", errors[-1].lower())

    def test_a_plain_search_that_really_found_nothing_keeps_the_hint(self):
        errors, _ = self._run(
            direct_exc=RuntimeError("ERROR: [youtube:search] No video results"),
            search_result=None,
            raw_input="mary alex g",
        )
        self.assertIn("check the spelling", errors[-1].lower())

    def test_a_second_search_does_not_repay_for_a_known_wall(self):
        """The wall is the network's, so re-searching re-proves the same fact."""
        wall = RuntimeError("Sign in to confirm you're not a bot")
        first, _ = self._run(
            direct_exc=wall, search_result=None, raw_input="mary alex g")
        self.assertIn("not a bot", first[-1].lower())

        statuses: list = []
        errors: list = []
        # No yt-dlp session is stubbed here on purpose: if the code tries to
        # search again it would hit the real network and fail the test.
        with mock.patch.object(d, "build_fast_yt_dlp_options", return_value={}), \
                mock.patch.object(d, "media_output_template", return_value="out.%(ext)s"), \
                mock.patch.object(d, "get_base_dir", return_value="."), \
                mock.patch.object(d, "_apply_platform_headers"), \
                mock.patch.object(d, "_mod_yt_dlp", return_value=object()), \
                mock.patch.object(d, "_snapshot_download_files", return_value=set()), \
                mock.patch.object(d, "_remove_new_media_files"), \
                mock.patch.object(d, "_search_and_download_best") as search:
            d.download_track(
                "some other track",
                status_callback=lambda text, color: statuses.append(text),
                success_callback=lambda folder: None,
                error_callback=errors.append,
            )
        search.assert_not_called()
        self.assertIn("not a bot", errors[-1].lower())
        self.assertTrue(any("refused" in s.lower() for s in statuses))


class NetworkBlockCacheTests(unittest.TestCase):
    """The bot wall is a property of the network, so it must not be re-discovered
    by paying a full failed search for every new title."""

    def setUp(self):
        d.clear_network_block()

    def tearDown(self):
        d.clear_network_block()

    def test_nothing_is_cached_until_something_is_refused(self):
        self.assertEqual(d.cached_network_block(), "")

    def test_a_refusal_is_reusable_for_the_next_search(self):
        reason = d._youtube_refusal_reason("Sign in to confirm you're not a bot")
        d.note_network_block(reason)
        self.assertEqual(d.cached_network_block(), reason)

    def test_a_successful_download_clears_a_stale_refusal(self):
        d.note_network_block("refused")
        d.clear_network_block()
        self.assertEqual(d.cached_network_block(), "")

    def test_an_old_refusal_expires_so_the_network_is_probed_again(self):
        # Walls lift when the user changes network or toggles a VPN; failing
        # fast for hours would be worse than the round-trip we are saving.
        d.note_network_block("refused")
        d._network_blocked_at -= (d._NETWORK_BLOCK_TTL + 1)
        self.assertEqual(d.cached_network_block(), "")


class RetryBackoffTests(unittest.TestCase):
    """Backoff must cost nothing unless a request actually fails.

    The first attempt at this used sleep_interval/sleep_requests, which are
    unconditional: yt-dlp slept before every download and between every
    extraction request, adding ~7s to a download that never failed. These
    assertions exist so that cannot silently come back.
    """

    def test_no_unconditional_sleep_in_the_library_options(self):
        opts = d.build_fast_yt_dlp_options(".", "out.%(ext)s", audio_only=True)
        for key in ("sleep_interval", "sleep_interval_requests", "max_sleep_interval"):
            self.assertNotIn(
                key, opts,
                f"{key} sleeps on the happy path; use retry_sleep instead")

    def test_backoff_is_configured_and_retry_only(self):
        opts = d.build_fast_yt_dlp_options(".", "out.%(ext)s", audio_only=True)
        self.assertEqual(opts["retry_sleep"]["http"], "exp=1:20")

    def test_the_exe_path_passes_no_invalid_or_unconditional_sleep_flags(self):
        """The EXE path must survive argument parsing.

        --sleep-interval-max does not exist, so yt-dlp.exe exited 2 on it at
        parse time and the whole packaged-EXE fallback died before downloading
        anything. Asserted against the real builder rather than a copy.
        """
        recorded: list = []

        def _fake_run_yt_dlp_exe(cmd, status_callback=None, **kw):
            recorded.append(cmd)

        with mock.patch.object(d, "get_fast_downloader_path", return_value=None), \
                mock.patch.object(d, "_run_yt_dlp_exe", _fake_run_yt_dlp_exe):
            d._fallback_download_with_ytdlp_exe(
                "https://soundcloud.com/x/y", "out.%(ext)s", True,
                status_callback=lambda *a: None)

        self.assertTrue(recorded, "the EXE path never invoked yt-dlp")
        cmd = recorded[0]
        self.assertNotIn("--sleep-interval-max", cmd)
        for bad in ("--sleep-interval", "--sleep-requests"):
            self.assertNotIn(bad, cmd, f"{bad} slows every download, not just retries")
        self.assertIn("--retry-sleep", cmd)

        # Every --retry-sleep must be followed by a parseable value, because a
        # missing value would silently swallow the next flag.
        for i, tok in enumerate(cmd[:-1]):
            if tok == "--retry-sleep":
                self.assertRegex(cmd[i + 1], r"^(http|fragment|extractor):")


if __name__ == "__main__":
    unittest.main()

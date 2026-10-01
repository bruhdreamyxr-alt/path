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


if __name__ == "__main__":
    unittest.main()

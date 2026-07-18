"""URL canonicalisation, hard dedup, and change detection."""

from __future__ import annotations

from crawler.fetch import canonical_url, content_hash, url_hash


class TestCanonicalUrl:
    def test_resolves_relative_against_base(self):
        assert (
            canonical_url("/bandi/uno", base="https://www.example.it/pagina")
            == "https://www.example.it/bandi/uno"
        )

    def test_strips_tracking_params_but_keeps_real_ones(self):
        out = canonical_url("https://www.example.it/b?id=42&utm_source=news&fbclid=xyz")
        assert out == "https://www.example.it/b?id=42"

    def test_strips_fragment(self):
        assert canonical_url("https://www.example.it/b#requisiti") == "https://www.example.it/b"

    def test_lowercases_host_but_not_path(self):
        """Hosts are case-insensitive; paths are not."""
        out = canonical_url("https://WWW.Example.IT/Bandi/Uno")
        assert out == "https://www.example.it/Bandi/Uno"

    def test_normalises_trailing_slash(self):
        assert canonical_url("https://www.example.it/bandi/") == canonical_url(
            "https://www.example.it/bandi"
        )


class TestUrlHash:
    def test_urls_differing_only_by_tracking_share_a_hash(self):
        """Hard dedup (§5): the same grant must not be stored twice."""
        assert url_hash("https://www.example.it/b?utm_source=a") == url_hash(
            "https://www.example.it/b"
        )

    def test_different_grants_differ(self):
        assert url_hash("https://www.example.it/b/1") != url_hash("https://www.example.it/b/2")

    def test_is_stable_across_calls(self):
        assert url_hash("https://www.example.it/b") == url_hash("https://www.example.it/b")


class TestContentHash:
    def test_whitespace_changes_do_not_count_as_a_change(self):
        """A cosmetic reflow must not trigger a needless LLM call."""
        assert content_hash("Bando   Voucher\n\nDigitale") == content_hash(
            "Bando Voucher Digitale"
        )

    def test_real_edits_do_count(self):
        assert content_hash("Scadenza: 30 settembre") != content_hash("Scadenza: 15 ottobre")

    def test_empty_is_stable(self):
        assert content_hash("") == content_hash("   ")


class TestMalformedUrlRepair:
    """Real feeds ship broken URLs. Sardegna Ricerche's notizie feed publishes
    every link with the scheme twice, which makes 'http' the hostname and fails
    every fetch. Found by running against the live source, not by unit tests."""

    def test_double_scheme_is_repaired(self):
        assert (
            canonical_url("http://http://www.sardegnaricerche.it/index.php?xsl=370")
            == "http://www.sardegnaricerche.it/index.php?xsl=370"
        )

    def test_double_https_is_repaired(self):
        assert canonical_url("https://https://www.example.it/b") == "https://www.example.it/b"

    def test_repair_composes_with_tracking_strip(self):
        assert (
            canonical_url("http://http://www.example.it/b?id=1&utm_source=rss")
            == "http://www.example.it/b?id=1"
        )

    def test_well_formed_urls_are_untouched(self):
        assert canonical_url("https://www.example.it/b") == "https://www.example.it/b"

    def test_scheme_relative_urls_still_resolve(self):
        """//host/path is legal and must not be mistaken for a double scheme."""
        assert (
            canonical_url("//www.example.it/b", base="https://www.example.it/list")
            == "https://www.example.it/b"
        )

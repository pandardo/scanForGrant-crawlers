"""Pagination discovery (§6.4).

Grants deeper than page 1 must not be silently missed — the Sardegna Ricerche
case, where a real bando sat on page 2 of a ~10-page list of plain <a href>
links. This is navigation, not interaction: "next" is an href we fetch.
"""

from __future__ import annotations

from crawler.paginate import MAX_PAGES, find_next_page, page_urls

BASE = "https://www.example.it/bandi"


def page(next_href: str | None, *, rel=False, text="avanti »") -> str:
    if next_href is None:
        return "<html><body><ul class='list'><li>item</li></ul></body></html>"
    rel_attr = ' rel="next"' if rel else ""
    return f"<html><body><a{rel_attr} href='{next_href}'>{text}</a></body></html>"


class TestFindNextPage:
    def test_rel_next_is_preferred(self):
        html = page("/bandi?p=2", rel=True, text="whatever")
        assert find_next_page(html, BASE) == "https://www.example.it/bandi?p=2"

    def test_finds_avanti_link(self):
        assert find_next_page(page("/bandi?p=2"), BASE) == "https://www.example.it/bandi?p=2"

    def test_recognises_various_next_phrasings(self):
        for text in ("Successiva", "pagina successiva", "next", "»", "→"):
            assert find_next_page(page("/bandi?p=2", text=text), BASE) is not None, text

    def test_returns_none_on_the_last_page(self):
        assert find_next_page(page(None), BASE) is None

    def test_never_points_at_the_current_page(self):
        """Some pagers link 'next' to the current page on the final page."""
        assert find_next_page(page("/bandi", text="avanti »"), BASE) is None

    def test_ignores_off_site_links(self):
        html = page("https://other-site.it/bandi?p=2")
        assert find_next_page(html, BASE) is None

    def test_reads_aria_label_when_text_is_an_icon(self):
        html = '<html><body><a href="/bandi?p=2" aria-label="Pagina successiva"><svg/></a></body></html>'
        assert find_next_page(html, BASE) == "https://www.example.it/bandi?p=2"


class TestPageUrls:
    def _paginated_site(self, n_pages: int) -> dict[str, str]:
        """A fake site of n_pages, each linking to the next."""
        pages = {}
        for i in range(1, n_pages + 1):
            url = BASE if i == 1 else f"{BASE}?p={i}"
            nxt = f"/bandi?p={i + 1}" if i < n_pages else None
            pages[url] = page(nxt)
        return pages

    def test_walks_every_page(self):
        site = self._paginated_site(4)
        urls = page_urls(site[BASE], BASE, lambda u: site.get(u))
        assert len(urls) == 4
        assert urls[0] == BASE

    def test_single_page_yields_just_itself(self):
        urls = page_urls(page(None), BASE, lambda u: None)
        assert urls == [BASE]

    def test_stops_at_the_cap(self):
        # A pager that always advertises a next page must not loop forever.
        def always_next(u):
            n = int(u.split("p=")[-1]) if "p=" in u else 1
            return page(f"/bandi?p={n + 1}")

        urls = page_urls(page("/bandi?p=2"), BASE, always_next)
        assert len(urls) == MAX_PAGES

    def test_stops_when_a_page_cannot_be_fetched(self):
        site = {BASE: page("/bandi?p=2")}  # page 2 is missing
        urls = page_urls(site[BASE], BASE, lambda u: site.get(u))
        assert urls == [BASE]

    def test_a_cycle_does_not_loop(self):
        """Page 2 pointing back to page 1 must terminate."""
        site = {BASE: page("/bandi?p=2"), f"{BASE}?p=2": page("/bandi")}
        urls = page_urls(site[BASE], BASE, lambda u: site.get(u))
        assert len(urls) == 2  # page 1, page 2, then the cycle is caught


class TestDocumentLinks:
    """A 'next' control must lead to a list page, not a document. Sardegna
    Ricerche had a link with a '→' glyph pointing at a .pdf, which the renderer
    cannot open — following it crashed the entire scan."""

    def test_pdf_next_link_is_not_followed(self):
        html = page("/documenti/13_107.pdf", text="→")
        assert find_next_page(html, BASE) is None

    def test_office_documents_are_not_followed(self):
        for ext in (".doc", ".docx", ".xlsx", ".zip"):
            html = page(f"/allegato{ext}", text="avanti »")
            assert find_next_page(html, BASE) is None, ext

    def test_a_real_next_page_is_still_followed(self):
        html = page("/index.php?xsl=376&p=1", text="avanti »")
        assert find_next_page(html, BASE) is not None
